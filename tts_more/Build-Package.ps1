[CmdletBinding()]
param(
    [ValidateSet("Bootstrap", "Full")][string]$Profile = "Bootstrap",
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [string]$Version = "0.2.0",
    [string]$OutputRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Profile -eq "Full" -and $env:GITHUB_ACTIONS -eq "true") { throw "profile=full is local-only and cannot be built by a GitHub upload workflow" }
if ($Version -notmatch "^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$") { throw "package Version must contain only ASCII letters, digits, dot, underscore, or hyphen (maximum 128 characters)" }
$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle))
$config = Get-Content -LiteralPath (Join-Path $Bundle "component.json") -Raw | ConvertFrom-Json
$modelLock = Get-Content -LiteralPath (Join-Path $Bundle "locks\models.lock.json") -Raw | ConvertFrom-Json
if (!$modelLock.complete) { throw "release package blocked by incomplete model lock: $($modelLock.missing_required_paths -join ', ')" }
$profileName = $Profile.ToLowerInvariant()
if (!$OutputRoot) { $OutputRoot = Join-Path $Root "artifacts\portable\$profileName" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$packageName = "$($config.component)-$Version-windows-x64-$profileName"
$work = Join-Path $Root "artifacts\portable\.work\$($config.component)-$profileName"
$stage = Join-Path $work $packageName
$revision = (& git -C $Root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $revision -notmatch "^[0-9a-f]{40}$") { throw "source revision is not available" }

if ($null -ne $config.PSObject.Properties['submodules']) {
    foreach ($submodule in $config.submodules.PSObject.Properties) {
        $path = Join-Path $Root $submodule.Name
        if (!(Test-Path -LiteralPath $path) -or !(Get-ChildItem -LiteralPath $path -Force | Select-Object -First 1)) { throw "locked submodule is not initialized: $($submodule.Name)" }
        $actual = (& git -C $path rev-parse HEAD).Trim()
        if ($actual -ne [string]$submodule.Value) { throw "submodule drift: $($submodule.Name) expected $($submodule.Value), found $actual" }
    }
}

function Copy-PortableTree {
    param([string]$Source, [string]$Destination)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($entry in Get-ChildItem -LiteralPath $Source -Force) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $target = Join-Path $Destination $entry.Name
        if ($entry.PSIsContainer) {
            Copy-PortableTree -Source $entry.FullName -Destination $target
        } else {
            Copy-Item -LiteralPath $entry.FullName -Destination $target -Force
        }
    }
}

if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage, (Join-Path $stage "package") | Out-Null
$excluded = @(".git", ".venv", "runtime", "data", "artifacts", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
$excludedFiles = @(".env", ".env.local")
foreach ($entry in Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -notin $excluded -and $_.Name -notin $excludedFiles }) {
    if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
    $destination = Join-Path $stage $entry.Name
    if ($entry.PSIsContainer) {
        Copy-PortableTree -Source $entry.FullName -Destination $destination
    } else {
        Copy-Item -LiteralPath $entry.FullName -Destination $destination -Force
    }
}
$alwaysLocalModelDirectories = @("SoVITS_weights", "GPT_weights")
@(Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force | Where-Object { $_.Name -in $alwaysLocalModelDirectories } | Sort-Object FullName -Descending) | ForEach-Object {
    $resolved = [System.IO.Path]::GetFullPath($_.FullName)
    if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to remove user model data outside package stage: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
if ($Profile -eq "Bootstrap") {
    $defaultModelDirectories = @("pretrained_models", "checkpoints")
    @(Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force | Where-Object { $_.Name -in $defaultModelDirectories } | Sort-Object FullName -Descending) | ForEach-Object {
        $resolved = [System.IO.Path]::GetFullPath($_.FullName)
        if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to remove model data outside package stage: $resolved" }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    $embeddedModelFiles = @(Get-ChildItem -LiteralPath $stage -File -Recurse -Force | Where-Object {
        $_.Name -match "\.(safetensors|ckpt|pth|pt|onnx|bin)$"
    })
    foreach ($embeddedModelFile in $embeddedModelFiles) {
        $resolved = [System.IO.Path]::GetFullPath($embeddedModelFile.FullName)
        if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to remove embedded model data outside package stage: $resolved" }
        Remove-Item -LiteralPath $resolved -Force
    }
}
@(Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force | Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") } | Sort-Object FullName -Descending) | ForEach-Object {
    $resolved = [System.IO.Path]::GetFullPath($_.FullName)
    if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to clean outside package stage: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

$integrationManifest = Get-Content -LiteralPath (Join-Path $stage "tts_more\integration.manifest.json") -Raw | ConvertFrom-Json
$integrationSha = (Get-FileHash -LiteralPath (Join-Path $stage "tts_more\integration.manifest.json") -Algorithm SHA256).Hash.ToLowerInvariant()
$deviceProfiles = if ($Device -eq "Auto") { @("auto", "cu128", "cu126", "cpu") } else { @($Device.ToLowerInvariant()) }
$capabilities = switch ([string]$config.component) {
    "gpt-sovits" { @("tts", "trained_weights_voice", "reference_audio_voice", "artifact-transfer") }
    "indextts" { @("tts", "reference_audio_voice", "emotion-text", "artifact-transfer") }
    "cosyvoice" { @("tts", "sft", "zero-shot", "cross-lingual", "instruct", "artifact-transfer") }
}
$manifest = [ordered]@{
    schema_version = 2; component = [string]$config.component; version = $Version
    build_id = "$($config.component)-$Version-$($revision.Substring(0, 12))"; package_profile = $profileName
    platform = "windows-x64"; api_contract = "tts-more-v1"
    source = @{ repository = [string]$config.upstream_repository; revision = $revision }
    integration = @{ version = [string]$integrationManifest.integration_version; source_revision = [string]$integrationManifest.source_revision; bundle_sha256 = $integrationSha }
    runtime = @{ python_version = [string]$config.python; device_profiles = @($deviceProfiles); lock = "tts_more/locks/runtime.lock.json"; state_path = "data/local/install-state.json" }
    models = @{ lock = "tts_more/locks/models.lock.json"; required = $true }
    data_root = "data/local"
    launchers = @{ initialize = "Initialize.cmd"; start = "Start.cmd"; stop = "Stop.cmd"; repair = "Repair.cmd"; build = "Build-Package.ps1" }
    endpoint = @{ default_url = "http://127.0.0.1:$($config.port)"; port = [int]$config.port; health_path = "/health"; capabilities_path = "/capabilities"; bind_policy = "loopback" }
    capabilities = $capabilities; sha256_manifest = "SHA256SUMS.txt"; licenses = "THIRD_PARTY_NOTICES.json"
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $stage "package\tts-more-package.json") -Encoding UTF8
@{ schema_version = 1; component = $config.component; integration_license = "Apache-2.0"; upstream_license = "LICENSE"; model_license = $modelLock.license; model_repository = $modelLock.upstream_repository; model_snapshot_revision = $modelLock.snapshot_revision } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stage "THIRD_PARTY_NOTICES.json") -Encoding UTF8

if ($Profile -eq "Full") {
    & (Join-Path $stage "tts_more\Initialize.ps1") -Device $Device
    if ($LASTEXITCODE -ne 0) { throw "full package initialization failed" }
}
if ($Profile -eq "Bootstrap") {
    $forbidden = @(Get-ChildItem -LiteralPath $stage -Recurse -Force | Where-Object {
        $_.FullName -match "[\\/](\.venv|runtime[\\/]live|data[\\/](cache|local|models))([\\/]|$)" -or $_.Name -match "\.(safetensors|ckpt|pth|pt|onnx|bin)$"
    })
    if ($forbidden.Count -gt 0) { throw "bootstrap audit found forbidden runtime/model asset: $($forbidden.FullName -join ', ')" }
}
$machinePathLeak = @(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.Length -lt 5MB } | Select-String -SimpleMatch -Pattern $Root -ErrorAction SilentlyContinue)
if ($machinePathLeak.Count -gt 0) { throw "package contains a build-machine absolute path: $($machinePathLeak[0].Path)" }

$sumPath = Join-Path $stage "SHA256SUMS.txt"
@(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.FullName -ne $sumPath } | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($stage.Length).TrimStart("\", "/").Replace("\", "/")
    "$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant())  $relative"
}) | Set-Content -LiteralPath $sumPath -Encoding UTF8

$buildPython = if ($env:TTS_MORE_BUILD_PYTHON) { $env:TTS_MORE_BUILD_PYTHON } elseif (Test-Path -LiteralPath (Join-Path $Root "runtime\live\python.exe")) { Join-Path $Root "runtime\live\python.exe" } elseif (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) { Join-Path $Root ".venv\Scripts\python.exe" } else {
    $conda = (& (Join-Path $Bundle "bootstrap-conda.ps1") -CacheRoot "data/cache/portable/conda" -LockPath "tts_more/locks/toolchain.lock.json" -PassThru | Select-Object -Last 1)
    Join-Path (Split-Path -Parent (Split-Path -Parent $conda)) "python.exe"
}
& $buildPython (Join-Path $stage "tts_more\portable_packages.py") validate-manifest --manifest (Join-Path $stage "package\tts-more-package.json") --package-root $stage
if ($LASTEXITCODE -ne 0) { throw "staged schema v2 manifest validation failed" }

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$zip = Join-Path $OutputRoot "$packageName.zip"
& $buildPython (Join-Path $stage "tts_more\portable_packages.py") create-zip --package-root $stage --output $zip
if ($LASTEXITCODE -ne 0) { throw "ZIP64 package creation failed" }
$auditPassed = $false
if ($Profile -eq "Bootstrap") {
    & $buildPython (Join-Path $stage "tts_more\portable_packages.py") audit-release --zip $zip
    if ($LASTEXITCODE -ne 0) { throw "GitHub bootstrap release audit failed" }
    $auditPassed = $true
}
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $([IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath "$zip.sha256" -Encoding ASCII
@{ component=$config.component; version=$Version; profile=$profileName; source_revision=$revision; integration_revision=$integrationManifest.source_revision; model_snapshot=$modelLock.snapshot_revision; sha256=$hash } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath "$zip.provenance.json" -Encoding UTF8

$selectedLock = Join-Path $Bundle "locks\requirements-$($Device.ToLowerInvariant()).lock.txt"
if ($Device -eq "Auto") { $selectedLock = Join-Path $Bundle "locks\requirements-cu128.lock.txt" }
$packages = @()
if (Test-Path -LiteralPath $selectedLock) {
    foreach ($line in Get-Content -LiteralPath $selectedLock) { if ($line -match "^([A-Za-z0-9_.-]+)==([^ \\]+)") { $spdxId = ($Matches[1] -replace '[^A-Za-z0-9.-]', '-'); $packages += @{ SPDXID="SPDXRef-Package-$spdxId"; name=$Matches[1]; versionInfo=$Matches[2]; downloadLocation="NOASSERTION"; filesAnalyzed=$false } } }
}
@{ spdxVersion="SPDX-2.3"; dataLicense="CC0-1.0"; SPDXID="SPDXRef-DOCUMENT"; name=$packageName; documentNamespace="https://tts-more.local/spdx/$($config.component)/$Version/$hash"; creationInfo=@{created=[DateTime]::UtcNow.ToString("o");creators=@("Tool: TTS-More-Build-Package-2.0.0")}; packages=$packages } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$zip.spdx.json" -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $stage "THIRD_PARTY_NOTICES.json") -Destination "$zip.licenses.json"
@{ schema_version=1; component=$config.component; profile=$profileName; manifest_valid=$true; bootstrap_audit=$auditPassed; machine_path_scan=$true; generated_at=[DateTime]::UtcNow.ToString("o") } | ConvertTo-Json | Set-Content -LiteralPath "$zip.acceptance.json" -Encoding UTF8
Write-Host "Created $Profile package: $zip"
