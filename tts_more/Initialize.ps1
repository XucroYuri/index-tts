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
$PathsScript = Join-Path $PSScriptRoot "Portable-Paths.ps1"
if (!(Test-Path -LiteralPath $PathsScript -PathType Leaf)) { throw "Portable-Paths.ps1 is missing" }
. $PathsScript
$paths = Get-PortableWorkerPaths -BundleRoot $PSScriptRoot -PackageRoot $PackageRoot
$Bundle = $paths.BundleRoot
$Root = $paths.PackageRoot
$SourceRoot = $paths.SourceRoot
$config = $paths.Config
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

function Repair-PortableWorkerStaleState {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$LivePath,
        [Parameter(Mandatory = $true)][string]$BundleRoot,
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$BuildId,
        [Parameter(Mandatory = $true)][string]$RuntimeLockPath,
        [Parameter(Mandatory = $true)][string]$ModelLockPath,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [Parameter(Mandatory = $true)][string]$ImportProbe,
        [Parameter(Mandatory = $true)][object]$RuntimeLockPayload
    )
    $existingState = if (Test-Path -LiteralPath $StatePath -PathType Leaf) { try { Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json } catch { $null } } else { $null }
    $requestedProfile = if ($existingState -and ![string]::IsNullOrWhiteSpace([string]$existingState.profile)) { [string]$existingState.profile } else { "" }
    $selectedProfile = Resolve-PortableSupportedProfile -RuntimeLockPayload $RuntimeLockPayload -RequestedProfile $requestedProfile
    $runtimeSha = Get-PortableFileSha256 -Path $RuntimeLockPath
    $modelSha = Get-PortableFileSha256 -Path $ModelLockPath
    & (Join-Path $LivePath "python.exe") (Join-Path $BundleRoot "portable_install.py") write-state --path $StatePath --component $Component --build-id $BuildId --profile $selectedProfile --runtime-lock-sha256 $runtimeSha --model-lock-sha256 $modelSha
    if ($LASTEXITCODE -ne 0) { throw "failed to repair stale install-state.json" }
    $repairedStateComplete = Test-PortableInstallStateComplete -Root $Root -SourceRoot $SourceRoot -StatePath $StatePath -Component $Component -BuildId $BuildId -RuntimeLock $RuntimeLockPath -ModelLock $ModelLockPath -ExpectedPython $ExpectedPython -ImportProbe $ImportProbe -ValidateAssets
    if (!$repairedStateComplete) { throw "repaired install-state.json failed complete validation" }
}

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

function Publish-PortableRuntimeTransaction {
    param(
        [Parameter(Mandatory = $true)][string]$Staging,
        [Parameter(Mandatory = $true)][string]$Live,
        [Parameter(Mandatory = $true)][string]$Backup,
        [Parameter(Mandatory = $true)][scriptblock]$CommitState
    )
    if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }
    $previousMoved = $false
    try {
        if (Test-Path -LiteralPath $Live) {
            Move-Item -LiteralPath $Live -Destination $Backup
            $previousMoved = $true
        }
        Move-Item -LiteralPath $Staging -Destination $Live
        & $CommitState
    }
    catch {
        $failure = $_
        try {
            if (Test-Path -LiteralPath $Live) { Remove-Item -LiteralPath $Live -Recurse -Force }
            if ($previousMoved -and (Test-Path -LiteralPath $Backup)) { Move-Item -LiteralPath $Backup -Destination $Live }
        }
        catch { Write-Warning "runtime rollback encountered a secondary failure: $($_.Exception.Message)" }
        throw $failure
    }
    if ($previousMoved -and (Test-Path -LiteralPath $Backup)) {
        try { Remove-Item -LiteralPath $Backup -Recurse -Force }
        catch { Write-Warning "committed runtime is valid, but previous runtime cleanup failed: $($_.Exception.Message)" }
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
$installStateComplete = Test-PortableInstallStateComplete -Root $Root -SourceRoot $SourceRoot -StatePath $state -Component ([string]$config.component) -BuildId $buildId -RuntimeLock $runtimeLockPath -ModelLock $modelLockPath -ExpectedPython $expectedPython -ImportProbe $importProbe -ValidateAssets
if ($installStateComplete) { Write-Host "verified runtime and install state already exist"; exit 0 }
$lockedAssetsComplete = Test-PortableLockedAssets -Root $Root -ModelLock $modelLockPath
$runtimeComplete = if ($lockedAssetsComplete) {
    Test-PortableRuntime -Root $Root -SourceRoot $SourceRoot -PythonPath (Join-Path $live "python.exe") -ExpectedVersion $expectedPython -ImportProbe $importProbe
} else { $false }
if ($lockedAssetsComplete -and $runtimeComplete) {
    Repair-PortableWorkerStaleState -SourceRoot $SourceRoot -Root $Root -StatePath $state -LivePath $live -BundleRoot $Bundle -Component ([string]$config.component) -BuildId $buildId -RuntimeLockPath $runtimeLockPath -ModelLockPath $modelLockPath -ExpectedPython $expectedPython -ImportProbe $importProbe -RuntimeLockPayload $runtimeLock
    Write-Host "verified package-private assets and repaired stale install state"
    exit 0
}
if ($Repair) { Write-Host "repairing only missing or invalid locked assets; user data is preserved" }

. (Join-Path $Bundle "portable-python.ps1")
if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
try {
    $PortableRuntime = Install-PortablePythonRuntime `
        -PackageRoot $Root `
        -RuntimeLock $runtimeLockPath `
        -Destination $staging `
        -OperationRoot $OperationRoot `
        -CancelFile $CancelFile
}
catch [System.OperationCanceledException] {
    exit 20
}
Assert-PortableNotCancelled

& $PortableRuntime.Python -c "import platform,sys; raise SystemExit(0 if platform.python_version() == sys.argv[1] else 1)" $expectedPython
if ($LASTEXITCODE -ne 0) { throw "embedded Python patch version does not match runtime lock: $expectedPython" }

$controllers = Join-Path $Root "data\cache\portable\video-controllers.json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $controllers) | Out-Null
$videoControllers = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{name=[string]$_.Name;driver_version=[string]$_.DriverVersion} })
ConvertTo-Json -InputObject $videoControllers | Set-Content -LiteralPath $controllers -Encoding UTF8
$selected = (& $PortableRuntime.Python (Join-Path $Bundle "portable_install.py") select-device --runtime-lock $runtimeLockPath --requested $Device.ToLowerInvariant() --controllers $controllers).Trim()
if ($LASTEXITCODE -ne 0) { throw "device profile selection failed" }
$profile = $runtimeLock.profiles.$selected

$payloads = if ($null -ne $runtimeLock.PSObject.Properties['payloads']) { @($runtimeLock.payloads) } else { @() }
foreach ($asset in $payloads) {
    $assetLock = Join-Path $Root "data\cache\portable\locks\$($asset.id).json"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $assetLock) | Out-Null
    $asset | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $assetLock -Encoding UTF8
    $archivePath = Join-Path $Root ([string]$asset.target)
    & $PortableRuntime.Python (Join-Path $Bundle "portable_install.py") ensure-asset --asset $assetLock --path $archivePath @DownloadArguments
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
    & $PortableRuntime.Python (Join-Path $Bundle "portable_install.py") ensure-asset --asset $assetLock --path (Join-Path $Root ([string]$asset.target)) @DownloadArguments
    if ($LASTEXITCODE -eq 20) { exit 20 }
    if ($LASTEXITCODE -ne 0) { throw "locked model asset failed: $($asset.id)" }
}
foreach ($requiredModelPath in @($modelLock.required_paths)) {
    if (!(Test-Path -LiteralPath (Join-Path $Root ([string]$requiredModelPath)))) {
        throw "required model asset is missing after locked initialization: $requiredModelPath"
    }
}

if ($runtimeLock.dependency_mode -in @("uv-project", "uv-check-requirements")) {
    & $PortableRuntime.Uv lock --check --project $SourceRoot
    if ($LASTEXITCODE -ne 0) { throw "upstream uv.lock drift detected" }
}
if ($runtimeLock.dependency_mode -eq "uv-project") {
    $requirements = Join-Path $staging "frozen-requirements.txt"
    & $PortableRuntime.Uv export --frozen --no-dev --no-emit-project --no-header --project $SourceRoot --output-file $requirements
    if ($LASTEXITCODE -ne 0) { throw "failed to export frozen upstream dependencies" }
    & $PortableRuntime.Uv pip install --python $PortableRuntime.Python --target $PortableRuntime.SitePackages --link-mode copy --requirement $requirements
} else {
    $installArguments = @("pip", "install", "--python", $PortableRuntime.Python, "--target", $PortableRuntime.SitePackages, "--link-mode", "copy", "--requirement", (Join-Path $Bundle "locks\$([string]$profile.dependency_lock)"))
    $buildConstraint = Join-Path $Bundle "locks\build-constraints.lock.txt"
    if (Test-Path -LiteralPath $buildConstraint) { $installArguments += @("--build-constraint", $buildConstraint) }
    & $PortableRuntime.Uv @installArguments
}
if ($LASTEXITCODE -ne 0) { throw "frozen dependency synchronization failed" }
& $PortableRuntime.Uv pip check --python $PortableRuntime.Python
if ($LASTEXITCODE -ne 0) { throw "uv pip check failed" }
& $PortableRuntime.Python (Join-Path $Bundle "portable_install.py") prune-console-launchers --site-packages $PortableRuntime.SitePackages
if ($LASTEXITCODE -ne 0) { throw "failed to prune non-relocatable dependency launchers" }
Invoke-PortablePythonSourceProbe -Root $Root -SourceRoot $SourceRoot -PythonPath $PortableRuntime.Python -ImportProbe $importProbe
if ($selected -ne "cpu") {
    $expectedCuda = if ($selected -eq "cu128") { "12.8" } else { "12.6" }
    $cudaProbe = "import torch; assert torch.cuda.is_available(); assert torch.version.cuda.startswith('$expectedCuda'); print(torch.cuda.get_device_name(0))"
    try {
        Invoke-PortablePythonSourceProbe -Root $Root -SourceRoot $SourceRoot -PythonPath $PortableRuntime.Python -ImportProbe $cudaProbe
    } catch {
        throw "explicit $selected package Torch/CUDA probe failed; CPU fallback is prohibited: $($_.Exception.Message)"
    }
}

$runtimeSha = Get-PortableFileSha256 -Path $runtimeLockPath
$modelSha = Get-PortableFileSha256 -Path $modelLockPath
$backup = Join-Path $Root "runtime\previous"
Publish-PortableRuntimeTransaction -Staging $staging -Live $live -Backup $backup -CommitState {
    & (Join-Path $live "python.exe") (Join-Path $Bundle "portable_install.py") write-state --path $state --component ([string]$config.component) --build-id $buildId --profile $selected --runtime-lock-sha256 $runtimeSha --model-lock-sha256 $modelSha
    if ($LASTEXITCODE -ne 0) { throw "install state commit failed" }
}
Write-Host "$($config.component) initialization completed for $selected"
