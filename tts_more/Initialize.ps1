[CmdletBinding()]
param(
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [switch]$Repair,
    [string]$PackageRoot = "",
    [string]$OperationRoot = "",
    [string]$CancelFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ValidationScript = Join-Path $PSScriptRoot "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $ValidationScript -PathType Leaf)) { throw "Portable-Validation.ps1 is missing" }
. $ValidationScript
$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = if ([string]::IsNullOrWhiteSpace($PackageRoot)) {
    [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle))
} else {
    [System.IO.Path]::GetFullPath($PackageRoot)
}
$config = Get-Content -LiteralPath (Join-Path $Bundle "component.json") -Raw | ConvertFrom-Json
$runtimeLockPath = Join-Path $Bundle "locks\runtime.lock.json"
$modelLockPath = Join-Path $Bundle "locks\models.lock.json"
$runtimeLock = Get-Content -LiteralPath $runtimeLockPath -Raw | ConvertFrom-Json
$modelLock = Get-Content -LiteralPath $modelLockPath -Raw | ConvertFrom-Json
if (!$modelLock.complete) {
    throw "model lock is incomplete and cannot initialize a release package: $($modelLock.missing_required_paths -join ', ')"
}
$live = Join-Path $Root "runtime\live"
$staging = Join-Path $Root "runtime\staging"
$state = Join-Path $Root "data\local\install-state.json"

function Resolve-OperationContract {
    param([string]$PackageRoot, [string]$OperationRoot = "", [string]$CancelFile = "")

    $hasOperation = ![string]::IsNullOrWhiteSpace($OperationRoot)
    $hasCancel = ![string]::IsNullOrWhiteSpace($CancelFile)
    if ($hasOperation -ne $hasCancel) { throw "OperationRoot and CancelFile must be provided together" }
    $resolvedPackage = [System.IO.Path]::GetFullPath($PackageRoot)
    if (!$hasOperation) { return [pscustomobject]@{ OperationRoot = ""; CancelFile = "" } }
    $operationsRelative = "data\local\operations"
    $manifestPath = Join-Path $resolvedPackage "package\tts-more-package.json"
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ([int]$manifest.schema_version -eq 2) {
            $operationsRelative = [string]$manifest.data.operations
            $segments = @($operationsRelative -split '[\\/]')
            if ([string]::IsNullOrWhiteSpace($operationsRelative) -or [IO.Path]::IsPathRooted($operationsRelative) -or $operationsRelative.Contains(":") -or $segments -contains "..") {
                throw "manifest data.operations must be a package-relative path"
            }
        }
    }
    $operations = [System.IO.Path]::GetFullPath((Join-Path $resolvedPackage $operationsRelative))
    $packagePrefix = $resolvedPackage.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (!$operations.StartsWith($packagePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "manifest data.operations resolves outside the package" }
    $current = $resolvedPackage
    foreach ($segment in @($operationsRelative -split '[\\/]')) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq ".") { continue }
        $current = Join-Path $current $segment
        if ((Test-Path -LiteralPath $current) -and (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "manifest data.operations traverses a reparse point"
        }
    }
    $resolvedOperation = [System.IO.Path]::GetFullPath($OperationRoot)
    $operationParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $resolvedOperation))
    if (![string]::Equals($operationParent, $operations, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OperationRoot must be a UUID-named direct child of the package operations root"
    }
    $parsedId = [guid]::Empty
    if (![guid]::TryParse((Split-Path -Leaf $resolvedOperation), [ref]$parsedId)) {
        throw "OperationRoot name must be a valid UUID"
    }
    $resolvedCancel = [System.IO.Path]::GetFullPath($CancelFile)
    $expectedCancel = [System.IO.Path]::GetFullPath((Join-Path $resolvedOperation "cancel.requested"))
    if (![string]::Equals($resolvedCancel, $expectedCancel, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "CancelFile must resolve exactly to OperationRoot/cancel.requested"
    }
    return Assert-PortableExactOperationContract -OperationsRoot $operations -OperationRoot $resolvedOperation -CancelFile $resolvedCancel -RequireOperation
}

$contract = Resolve-OperationContract -PackageRoot $Root -OperationRoot $OperationRoot -CancelFile $CancelFile
$OperationRoot = $contract.OperationRoot
$CancelFile = $contract.CancelFile
$DownloadArguments = @("--package-root", $Root)
if (![string]::IsNullOrWhiteSpace($OperationRoot)) {
    $DownloadArguments += @("--operation-root", $OperationRoot, "--cancel-file", $CancelFile)
}

function Assert-PortableNotCancelled {
    if (![string]::IsNullOrWhiteSpace($CancelFile) -and (Test-Path -LiteralPath $CancelFile -PathType Leaf)) {
        exit 20
    }
}

Assert-PortableNotCancelled
if ($Root.Length -gt 180) { throw "package path is too long for reliable Windows model tooling: $Root" }
$requiredSpace = [int64]$runtimeLock.required_free_bytes + [int64]$modelLock.required_free_bytes
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($Root).Substring(0, 1)) -ErrorAction SilentlyContinue
if ($drive -and $drive.Free -lt $requiredSpace) { throw "insufficient free space: need $requiredSpace bytes" }

$manifestPath = Join-Path $Root "package\tts-more-package.json"
$buildId = if (Test-Path -LiteralPath $manifestPath -PathType Leaf) { [string](Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).build_id } else { "source-checkout" }
$expectedPython = if ([string]::IsNullOrWhiteSpace([string]$runtimeLock.python_version)) { [string]$config.python } else { [string]$runtimeLock.python_version }
$importProbe = if ($runtimeLock.PSObject.Properties["import_probe"] -and ![string]::IsNullOrWhiteSpace([string]$runtimeLock.import_probe)) { [string]$runtimeLock.import_probe } else { [string]$config.import_probe }
if (Test-PortableInstallStateComplete -Root $Root -StatePath $state -Component ([string]$config.component) -BuildId $buildId -RuntimeLock $runtimeLockPath -ModelLock $modelLockPath -ExpectedPython $expectedPython -ImportProbe $importProbe -ValidateAssets) { Write-Host "verified runtime and install state already exist"; exit 0 }
if ((Test-PortableLockedAssets -Root $Root -ModelLock $modelLockPath) -and (Test-PortableRuntime -Root $Root -PythonPath (Join-Path $live "python.exe") -ExpectedVersion $expectedPython -ImportProbe $importProbe)) {
    $existingState = if (Test-Path -LiteralPath $state -PathType Leaf) { try { Get-Content -LiteralPath $state -Raw | ConvertFrom-Json } catch { $null } } else { $null }
    $requestedProfile = if ($existingState -and ![string]::IsNullOrWhiteSpace([string]$existingState.profile)) { [string]$existingState.profile } else { "" }
    $selectedProfile = Resolve-PortableSupportedProfile -RuntimeLockPayload $runtimeLock -RequestedProfile $requestedProfile
    $runtimeSha = (Get-FileHash -LiteralPath $runtimeLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $modelSha = (Get-FileHash -LiteralPath $modelLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    & (Join-Path $live "python.exe") (Join-Path $Bundle "portable_install.py") write-state --path $state --component ([string]$config.component) --build-id $buildId --profile $selectedProfile --runtime-lock-sha256 $runtimeSha --model-lock-sha256 $modelSha
    if ($LASTEXITCODE -ne 0) { throw "failed to repair stale install-state.json" }
    if (!(Test-PortableInstallStateComplete -Root $Root -StatePath $state -Component ([string]$config.component) -BuildId $buildId -RuntimeLock $runtimeLockPath -ModelLock $modelLockPath -ExpectedPython $expectedPython -ImportProbe $importProbe -ValidateAssets)) { throw "repaired install-state.json failed complete validation" }
    Write-Host "verified package-private assets and repaired stale install state"
    exit 0
}
if ($Repair) { Write-Host "repairing only missing or invalid locked assets; user data is preserved" }

$bootstrap = Join-Path $Bundle "bootstrap-conda.ps1"
$Conda = (& $bootstrap -CacheRoot "data/cache/portable/conda" -LockPath "tts_more/locks/toolchain.lock.json" -PackageRoot $Root -OperationRoot $OperationRoot -CancelFile $CancelFile -PassThru | Select-Object -Last 1)
if ($LASTEXITCODE -eq 20) { exit 20 }
$CondaRoot = Split-Path -Parent (Split-Path -Parent $Conda)
$BootstrapPython = Join-Path $CondaRoot "python.exe"
if (!(Test-Path -LiteralPath $BootstrapPython)) { throw "private bootstrap Python is missing" }
$controllers = Join-Path $Root "data\cache\portable\video-controllers.json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $controllers) | Out-Null
$videoControllers = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{name=[string]$_.Name;driver_version=[string]$_.DriverVersion} })
ConvertTo-Json -InputObject $videoControllers | Set-Content -LiteralPath $controllers -Encoding UTF8
$selected = (& $BootstrapPython (Join-Path $Bundle "portable_install.py") select-device --runtime-lock $runtimeLockPath --requested $Device.ToLowerInvariant() --controllers $controllers).Trim()
if ($LASTEXITCODE -ne 0) { throw "device profile selection failed" }
$profile = $runtimeLock.profiles.$selected

$payloads = if ($null -ne $runtimeLock.PSObject.Properties['payloads']) { @($runtimeLock.payloads) } else { @() }
foreach ($asset in $payloads) {
    $assetLock = Join-Path $Root "data\cache\portable\locks\$($asset.id).json"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $assetLock) | Out-Null
    $asset | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $assetLock -Encoding UTF8
    $archivePath = Join-Path $Root ([string]$asset.target)
    & $BootstrapPython (Join-Path $Bundle "portable_install.py") ensure-asset --asset $assetLock --path $archivePath @DownloadArguments
    if ($LASTEXITCODE -eq 20) { exit 20 }
    if ($LASTEXITCODE -ne 0) { throw "locked runtime asset failed: $($asset.id)" }
    if ($asset.extract_to) {
        $destination = Join-Path $Root ([string]$asset.extract_to)
        $marker = Join-Path $destination ".tts-more-asset.sha256"
        if (!(Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne [string]$asset.sha256) {
            $extractStage = "$destination.staging"
            if (Test-Path -LiteralPath $extractStage) { Remove-Item -LiteralPath $extractStage -Recurse -Force }
            Expand-Archive -LiteralPath $archivePath -DestinationPath $extractStage
            $children = @(Get-ChildItem -LiteralPath $extractStage -Force)
            $payloadRoot = if ($children.Count -eq 1 -and $children[0].PSIsContainer) { $children[0].FullName } else { $extractStage }
            if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
            if ($payloadRoot -eq $extractStage) { Move-Item -LiteralPath $extractStage -Destination $destination } else { Move-Item -LiteralPath $payloadRoot -Destination $destination; Remove-Item -LiteralPath $extractStage -Recurse -Force }
            [string]$asset.sha256 | Set-Content -LiteralPath (Join-Path $destination ".tts-more-asset.sha256") -Encoding ASCII
        }
    }
}

foreach ($asset in @($modelLock.assets)) {
    $assetLock = Join-Path $Root "data\cache\portable\locks\$($asset.id).json"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $assetLock) | Out-Null
    $asset | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $assetLock -Encoding UTF8
    & $BootstrapPython (Join-Path $Bundle "portable_install.py") ensure-asset --asset $assetLock --path (Join-Path $Root ([string]$asset.target)) @DownloadArguments
    if ($LASTEXITCODE -eq 20) { exit 20 }
    if ($LASTEXITCODE -ne 0) { throw "locked model asset failed: $($asset.id)" }
}
foreach ($requiredModelPath in @($modelLock.required_paths)) {
    if (!(Test-Path -LiteralPath (Join-Path $Root ([string]$requiredModelPath)))) {
        throw "required model asset is missing after locked initialization: $requiredModelPath"
    }
}

if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
& $Conda create --yes --prefix $staging "python=$($config.python)" pip
if ($LASTEXITCODE -ne 0) { throw "temporary package runtime creation failed" }
$StagePython = Join-Path $staging "python.exe"
$uv = $runtimeLock.assets.uv
$uvLock = Join-Path $Root "data\cache\portable\locks\uv.json"
$uvWheel = Join-Path $Root "data\cache\portable\assets\$($uv.id).whl"
$uv | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $uvLock -Encoding UTF8
& $BootstrapPython (Join-Path $Bundle "portable_install.py") ensure-asset --asset $uvLock --path $uvWheel @DownloadArguments
if ($LASTEXITCODE -eq 20) { exit 20 }
if ($LASTEXITCODE -ne 0) { throw "locked uv download failed" }
& $StagePython -m pip install --no-deps $uvWheel
$UvExe = Join-Path $staging "Scripts\uv.exe"
if ($runtimeLock.dependency_mode -in @("uv-project", "uv-check-requirements")) {
    & $UvExe lock --check --project $Root
    if ($LASTEXITCODE -ne 0) { throw "upstream uv.lock drift detected" }
}
if ($runtimeLock.dependency_mode -eq "uv-project") {
    $requirements = Join-Path $staging "frozen-requirements.txt"
    & $UvExe export --frozen --no-dev --no-emit-project --project $Root --output-file $requirements
    & $UvExe pip install --python $StagePython --requirement $requirements
} else {
    $installArguments = @("pip", "install", "--python", $StagePython, "--requirement", (Join-Path $Bundle "locks\$([string]$profile.dependency_lock)"))
    $buildConstraint = Join-Path $Bundle "locks\build-constraints.lock.txt"
    if (Test-Path -LiteralPath $buildConstraint) { $installArguments += @("--build-constraint", $buildConstraint) }
    & $UvExe @installArguments
}
if ($LASTEXITCODE -ne 0) { throw "frozen dependency synchronization failed" }
& $StagePython -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed" }
& $StagePython -c ([string]$config.import_probe)
if ($LASTEXITCODE -ne 0) { throw "core import/ONNX probe failed" }
if ($selected -ne "cpu") {
    $expectedCuda = if ($selected -eq "cu128") { "12.8" } else { "12.6" }
    & $StagePython -c "import torch; assert torch.cuda.is_available(); assert torch.version.cuda.startswith('$expectedCuda'); print(torch.cuda.get_device_name(0))"
    if ($LASTEXITCODE -ne 0) { throw "explicit $selected package Torch/CUDA probe failed; CPU fallback is prohibited" }
}

$backup = Join-Path $Root "runtime\previous"
if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
if (Test-Path -LiteralPath $live) { Move-Item -LiteralPath $live -Destination $backup }
Move-Item -LiteralPath $staging -Destination $live
if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
$runtimeSha = (Get-FileHash -LiteralPath $runtimeLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
$modelSha = (Get-FileHash -LiteralPath $modelLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
& (Join-Path $live "python.exe") (Join-Path $Bundle "portable_install.py") write-state --path $state --component ([string]$config.component) --build-id $buildId --profile $selected --runtime-lock-sha256 $runtimeSha --model-lock-sha256 $modelSha
if ($LASTEXITCODE -ne 0) { throw "install state commit failed" }
Write-Host "$($config.component) initialization completed for $selected"
