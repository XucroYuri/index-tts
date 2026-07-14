[CmdletBinding()]
param(
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [switch]$Repair
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle))
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
if ($Root.Length -gt 180) { throw "package path is too long for reliable Windows model tooling: $Root" }
$requiredSpace = [int64]$runtimeLock.required_free_bytes + [int64]$modelLock.required_free_bytes
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($Root).Substring(0, 1)) -ErrorAction SilentlyContinue
if ($drive -and $drive.Free -lt $requiredSpace) { throw "insufficient free space: need $requiredSpace bytes" }

function Test-LiveRuntime {
    $python = Join-Path $live "python.exe"
    if (!(Test-Path -LiteralPath $python)) { return $false }
    & $python -m pip check *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    & $python -c ([string]$config.import_probe) *> $null
    return $LASTEXITCODE -eq 0
}
if ((Test-Path -LiteralPath $state) -and (Test-LiveRuntime)) { Write-Host "verified runtime and install state already exist"; exit 0 }
if ($Repair) { Write-Host "repairing only missing or invalid locked assets; user data is preserved" }

$bootstrap = Join-Path $Bundle "bootstrap-conda.ps1"
$Conda = (& $bootstrap -CacheRoot "data/cache/portable/conda" -LockPath "tts_more/locks/toolchain.lock.json" -PassThru | Select-Object -Last 1)
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
    & $BootstrapPython (Join-Path $Bundle "portable_install.py") ensure-asset --asset $assetLock --path $archivePath
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
    & $BootstrapPython (Join-Path $Bundle "portable_install.py") ensure-asset --asset $assetLock --path (Join-Path $Root ([string]$asset.target))
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
& $BootstrapPython (Join-Path $Bundle "portable_install.py") ensure-asset --asset $uvLock --path $uvWheel
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
& (Join-Path $live "python.exe") (Join-Path $Bundle "portable_install.py") write-state --path $state --component ([string]$config.component) --build-id source-checkout --profile $selected --runtime-lock-sha256 $runtimeSha --model-lock-sha256 $modelSha
if ($LASTEXITCODE -ne 0) { throw "install state commit failed" }
Write-Host "$($config.component) initialization completed for $selected"
