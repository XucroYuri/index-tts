[CmdletBinding()]
param(
    [string]$OperationId = "",
    [string]$ManagedBy = "direct",
    [switch]$NoUi,
    [switch]$OfferImport,
    [ValidateRange(1, 65535)][Nullable[int]]$PortOverride = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:Context = $null

class PortableStartException : System.Exception {
    [string]$Code

    PortableStartException([string]$code, [string]$message) : base($message) {
        $this.Code = $code
    }
}

$validationScript = Join-Path $PSScriptRoot "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $validationScript -PathType Leaf)) {
    throw [PortableStartException]::new("PACKAGE_CORRUPT", "Portable-Validation.ps1 is missing from the controller bundle")
}
. $validationScript

function Throw-PortableStartError {
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message
    )
    throw [PortableStartException]::new($Code, $Message)
}

function Resolve-PortablePowerShellHost {
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($name in @("powershell.exe", "pwsh.exe")) {
        try { $candidates.Add((Join-Path $PSHOME $name)) } catch { }
    }
    try {
        if (![string]::IsNullOrWhiteSpace($env:SystemRoot)) {
            $candidates.Add((Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"))
        }
    } catch { }
    foreach ($candidate in @($candidates)) {
        if (![string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    Throw-PortableStartError "PACKAGE_CORRUPT" "PowerShell host executable is unavailable for portable child launch"
}

function Test-PathWithinRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if ([string]::Equals($resolvedRoot, $resolvedPath, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    $prefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    return $resolvedPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-PackageRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Value) -or [IO.Path]::IsPathRooted($Value) -or $Value.Contains(":")) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label must be a package-relative path"
    }
    $segments = @($Value -split '[\\/]')
    if ($segments -contains "..") {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label cannot escape the package"
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $Root $Value))
    if (!(Test-PathWithinRoot -Root $Root -Path $resolved)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label resolves outside the package"
    }
    $current = [IO.Path]::GetFullPath($Root)
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq ".") { continue }
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            $attributes = [IO.File]::GetAttributes($current)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Throw-PortableStartError "PACKAGE_CORRUPT" "$Label traverses a reparse point outside the portable package boundary"
            }
        }
    }
    return $resolved
}

function Get-PackageContext {
    param([Parameter(Mandatory = $true)][string]$Root)

    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    try { Assert-PortablePackageRootPathBudget -Root $resolvedRoot } catch {
        Throw-PortableStartError "PACKAGE_PATH_TOO_DEEP" $_.Exception.Message
    }
    $manifestPath = Join-Path $resolvedRoot "package\tts-more-package.json"
    $manifest = $null
    $isStaged = Test-Path -LiteralPath $manifestPath -PathType Leaf
    if ($isStaged) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        } catch {
            Throw-PortableStartError "PACKAGE_CORRUPT" "The staged package manifest is not valid JSON: $($_.Exception.Message)"
        }
        if ($null -eq $manifest.PSObject.Properties["schema_version"] -or $manifest.schema_version -isnot [int] -or [int]$manifest.schema_version -notin @(1, 2)) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "The staged package manifest schema is unsupported"
        }
        if ([int]$manifest.schema_version -eq 2) {
            try { Assert-PortableV2Manifest -Root $resolvedRoot -Manifest $manifest } catch {
                Throw-PortableStartError "PACKAGE_CORRUPT" "The staged v2 manifest is invalid: $($_.Exception.Message)"
            }
        }
    }

    $sourceRoot = $resolvedRoot
    $bundle = Join-Path $resolvedRoot "scripts"
    foreach ($candidate in @(
        (Join-Path $resolvedRoot "app\tts_more"),
        (Join-Path $resolvedRoot "tts_more")
    )) {
        if (Test-Path -LiteralPath (Join-Path $candidate "component.json") -PathType Leaf) {
            $bundle = $candidate
            break
        }
    }
    $componentConfig = $null
    $componentConfigPath = Join-Path $bundle "component.json"
    if (Test-Path -LiteralPath $componentConfigPath -PathType Leaf) {
        try { $componentConfig = Get-Content -LiteralPath $componentConfigPath -Raw | ConvertFrom-Json } catch {
            Throw-PortableStartError "PACKAGE_CORRUPT" "The component configuration is not valid JSON"
        }
        $pathsScript = Join-Path $bundle "Portable-Paths.ps1"
        if (!(Test-Path -LiteralPath $pathsScript -PathType Leaf)) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "Portable-Paths.ps1 is missing from the worker bundle"
        }
        . $pathsScript
        try {
            $workerPaths = Get-PortableWorkerPaths -BundleRoot $bundle -PackageRoot $resolvedRoot
            $sourceRoot = $workerPaths.SourceRoot
        } catch {
            Throw-PortableStartError "PACKAGE_CORRUPT" "The worker source_root is invalid: $($_.Exception.Message)"
        }
    }

    if ($isStaged) {
        $component = [string]$manifest.component
        if ([string]::IsNullOrWhiteSpace($component)) { Throw-PortableStartError "PACKAGE_CORRUPT" "The package component is missing" }
        $profile = if ([int]$manifest.schema_version -eq 2) { [string]$manifest.package_profile } else { "bootstrap" }
        if ($profile -notin @("bootstrap", "full")) { Throw-PortableStartError "PACKAGE_CORRUPT" "The package profile is invalid" }
        if ([int]$manifest.schema_version -eq 2) {
            try {
                $requiredText = @("component", "package_id", "release_version", "version", "build_id", "api_contract")
                foreach ($name in $requiredText) {
                    if ($null -eq $manifest.PSObject.Properties[$name] -or [string]::IsNullOrWhiteSpace([string]$manifest.$name)) { throw "$name is required" }
                }
                if ([string]$manifest.component -notin @("tts-more", "gpt-sovits", "indextts", "cosyvoice")) { throw "component is unsupported" }
                if ([string]$manifest.platform -ne "windows-x64") { throw "platform must be windows-x64" }
                if ([string]$manifest.api_contract -ne "tts-more-v1") { throw "api_contract must be tts-more-v1" }
                if ([string]$manifest.protocol.name -ne "tts-more-v1" -or [string]::IsNullOrWhiteSpace([string]$manifest.protocol.version) -or [string]::IsNullOrWhiteSpace([string]$manifest.protocol.controller_range)) { throw "protocol identity is invalid" }
                if ([string]$manifest.source.repository -notmatch '^https://' -or [string]$manifest.source.revision -notmatch '^[0-9a-fA-F]{40,64}$') { throw "source identity is invalid" }
                if ([string]::IsNullOrWhiteSpace([string]$manifest.integration.version) -or [string]$manifest.integration.source_revision -notmatch '^[0-9a-fA-F]{40,64}$' -or [string]$manifest.integration.bundle_sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw "integration identity is invalid" }
                $expectedPython = [string]$manifest.runtime.python_version
                if ($expectedPython -notin @("3.10", "3.10.11", "3.11", "3.11.9")) { throw "runtime.python_version is unsupported" }
                if ($component -eq "tts-more" -and $expectedPython -notin @("3.11", "3.11.9")) { throw "tts-more requires Python 3.11" }
                if ($manifest.runtime.device_profiles -is [string]) { throw "runtime.device_profiles must be an array" }
                $deviceProfiles = @($manifest.runtime.device_profiles)
                if ($deviceProfiles.Count -eq 0) { throw "runtime.device_profiles is required" }
                foreach ($deviceProfile in $deviceProfiles) { if ([string]$deviceProfile -notin @("auto", "cu128", "cu126", "cpu")) { throw "runtime.device_profiles contains an unsupported profile" } }
                if ($manifest.models.required -isnot [bool]) { throw "models.required must be boolean" }
                foreach ($name in @("user", "local", "cache", "operations")) { [void](Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data.$name) -Label "data.$name") }
                [void](Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data_root) -Label "data_root")
                foreach ($name in @("initialize", "start", "stop", "repair", "build")) { [void](Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.launchers.$name) -Label "launchers.$name" -MustExist) }
                if ([string]$manifest.endpoint.default_url -notmatch '^http://' -or [int]$manifest.endpoint.port -lt 1 -or [int]$manifest.endpoint.port -gt 65535) { throw "endpoint URL or port is invalid" }
                foreach ($name in @("health_path", "capabilities_path")) { if ([string]$manifest.endpoint.$name -notmatch '^/') { throw "endpoint.$name must start with /" } }
                if ([string]$manifest.endpoint.bind_policy -notin @("loopback", "trusted-lan")) { throw "endpoint.bind_policy is invalid" }
                if ($manifest.capabilities -is [string] -or @($manifest.capabilities).Count -eq 0) { throw "capabilities must be a non-empty array" }
                $operationsRoot = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.data.operations) -Label "data.operations"
                $statePath = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.runtime.state_path) -Label "runtime.state_path"
                $runtimeLock = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.runtime.lock) -Label "runtime.lock" -MustExist
                $modelLock = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.models.lock) -Label "models.lock" -MustExist
                $sha256Manifest = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.sha256_manifest) -Label "sha256_manifest" -MustExist
                $licenses = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath ([string]$manifest.licenses) -Label "licenses" -MustExist
            } catch {
                Throw-PortableStartError "PACKAGE_CORRUPT" "The staged v2 manifest is invalid: $($_.Exception.Message)"
            }
        } else {
            $operationsRoot = Join-Path $resolvedRoot "data\local\operations"
            $statePath = Join-Path $resolvedRoot "data\local\install-state.json"
            $runtimeLock = ""
            $modelLock = ""
            $sha256Manifest = ""
            $licenses = ""
            $expectedPython = "3.11"
        }
        $buildId = [string]$manifest.build_id
        $port = if ($null -ne $manifest.endpoint -and $null -ne $manifest.endpoint.port) { [int]$manifest.endpoint.port } elseif ($componentConfig) { [int]$componentConfig.port } else { 8000 }
        $healthPath = if ($null -ne $manifest.endpoint) { [string]$manifest.endpoint.health_path } else { "" }
    } else {
        $component = if ($componentConfig) { [string]$componentConfig.component } else { "tts-more" }
        if ([string]::IsNullOrWhiteSpace($component)) { $component = "tts-more" }
        $profile = "source-checkout"
        $operationsRoot = Join-Path $resolvedRoot "data\local\operations"
        $statePath = Join-Path $resolvedRoot "data\local\install-state.json"
        $runtimeLock = if ($component -eq "tts-more") { Join-Path $resolvedRoot "packaging\portable\runtime.lock.json" } else { Join-Path $bundle "locks\runtime.lock.json" }
        $modelLock = if ($component -eq "tts-more") { Join-Path $resolvedRoot "packaging\portable\models.lock.json" } else { Join-Path $bundle "locks\models.lock.json" }
        $buildId = "source-checkout"
        $port = if ($componentConfig) { [int]$componentConfig.port } else { 8000 }
        $healthPath = if ($component -eq "tts-more") { "/api/health" } else { "/health" }
        $sha256Manifest = ""
        $licenses = ""
        try {
            $runtimePayload = Get-Content -LiteralPath $runtimeLock -Raw | ConvertFrom-Json
            $expectedPython = [string]$runtimePayload.python_version
            if ([string]::IsNullOrWhiteSpace($expectedPython)) { $expectedPython = if ($component -eq "tts-more") { "3.11" } else { [string]$componentConfig.python } }
        } catch { $expectedPython = if ($component -eq "tts-more") { "3.11" } else { [string]$componentConfig.python } }
    }

    $initializeScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\initialize-portable.ps1" } else { Join-Path $bundle "Initialize.ps1" }
    $serviceScript = if ($component -eq "tts-more") { Join-Path $resolvedRoot "scripts\start-production.ps1" } else { Join-Path $bundle "Start-Worker.ps1" }
    if (!(Test-Path -LiteralPath $initializeScript -PathType Leaf) -or !(Test-Path -LiteralPath $serviceScript -PathType Leaf)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Portable initialize or start controller is missing"
    }

    $importProbe = if ($componentConfig -and $componentConfig.PSObject.Properties["import_probe"]) { [string]$componentConfig.import_probe } elseif ($component -eq "tts-more") { "import fastapi,pydantic,uvicorn" } else { "import sys" }
    try {
        if ($runtimeLock -and (Test-Path -LiteralPath $runtimeLock -PathType Leaf)) {
            $runtimePayload = Get-Content -LiteralPath $runtimeLock -Raw | ConvertFrom-Json
            if ($runtimePayload.PSObject.Properties["import_probe"] -and ![string]::IsNullOrWhiteSpace([string]$runtimePayload.import_probe)) { $importProbe = [string]$runtimePayload.import_probe }
        }
    } catch { Throw-PortableStartError "PACKAGE_CORRUPT" "The runtime lock is invalid JSON" }

    $importCore = Join-Path $bundle "import_portable_data.py"
    $importCli = Join-Path $bundle "import-portable-data.py"
    $importSelector = Join-Path $bundle "select-portable-folder.ps1"
    $bootstrapScript = Join-Path $bundle "bootstrap-conda.ps1"
    $packageSchema = if ($component -eq "tts-more") {
        Join-Path $resolvedRoot "packaging\portable\tts-more-package.schema.json"
    } else {
        Join-Path $bundle "tts-more-package.schema.json"
    }
    $toolchainLock = if ($component -eq "tts-more") {
        "packaging/portable/toolchain.lock.json"
    } else {
        $relativeToolchain = (Join-Path $bundle "locks\toolchain.lock.json").Substring($resolvedRoot.Length).TrimStart('\', '/').Replace('\', '/')
        $relativeToolchain
    }
    $importDecisionPath = Join-Path $resolvedRoot "data\local\portable-import-decision.json"
    $importCoverage = @($importCore, $importCli, $importSelector, $packageSchema, $bootstrapScript, $PSCommandPath)
    $requiredCoverage = @($initializeScript, $serviceScript, $runtimeLock, $modelLock, $licenses, $manifestPath, $validationScript) + $importCoverage | Where-Object { $_ }

    return [pscustomobject]@{
        Root = $resolvedRoot
        SourceRoot = $sourceRoot
        Bundle = $bundle
        IsStaged = [bool]$isStaged
        Profile = $profile
        Component = $component
        BuildId = $buildId
        OperationsRoot = [IO.Path]::GetFullPath($operationsRoot)
        StatePath = [IO.Path]::GetFullPath($statePath)
        RuntimeLock = if ($runtimeLock) { [IO.Path]::GetFullPath($runtimeLock) } else { "" }
        ModelLock = if ($modelLock) { [IO.Path]::GetFullPath($modelLock) } else { "" }
        Sha256Manifest = if ($sha256Manifest) { [IO.Path]::GetFullPath($sha256Manifest) } else { "" }
        Licenses = if ($licenses) { [IO.Path]::GetFullPath($licenses) } else { "" }
        ExpectedPython = $expectedPython
        ImportProbe = $importProbe
        RequiredCoverage = @($requiredCoverage)
        InitializeScript = $initializeScript
        ServiceScript = $serviceScript
        Port = $port
        HealthPath = $healthPath
        EndpointUrl = "http://127.0.0.1:$port"
        ImportCore = [IO.Path]::GetFullPath($importCore)
        ImportCli = [IO.Path]::GetFullPath($importCli)
        ImportSelector = [IO.Path]::GetFullPath($importSelector)
        BootstrapScript = [IO.Path]::GetFullPath($bootstrapScript)
        PackageSchema = [IO.Path]::GetFullPath($packageSchema)
        ToolchainLock = $toolchainLock
        ImportDecisionPath = [IO.Path]::GetFullPath($importDecisionPath)
        ImportCoverage = @($importCoverage | ForEach-Object { [IO.Path]::GetFullPath($_) })
    }
}

function Resolve-PortableStartRoot {
    $bundle = [IO.Path]::GetFullPath($PSScriptRoot)
    $pathsScript = Join-Path $bundle "Portable-Paths.ps1"
    if (
        (Test-Path -LiteralPath (Join-Path $bundle "component.json") -PathType Leaf) -and
        (Test-Path -LiteralPath $pathsScript -PathType Leaf)
    ) {
        . $pathsScript
        try { return [string](Get-PortableWorkerPaths -BundleRoot $bundle).PackageRoot } catch {
            Throw-PortableStartError "PACKAGE_CORRUPT" "Unable to resolve worker package root: $($_.Exception.Message)"
        }
    }
    return [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
}

function Assert-PackageWritable {
    param([Parameter(Mandatory = $true)][string]$Root)

    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    if ($resolvedRoot -match '(?i)\.zip([\\/]|$)') {
        Throw-PortableStartError "PACKAGE_NOT_WRITABLE" "Extract the ZIP before starting the package"
    }
    foreach ($programFilesRoot in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($programFilesRoot -and (Test-PathWithinRoot -Root $programFilesRoot -Path $resolvedRoot)) {
            Throw-PortableStartError "PACKAGE_NOT_WRITABLE" "Move the package outside Program Files; elevation is not requested"
        }
    }
    try {
        $attributes = [IO.File]::GetAttributes($resolvedRoot)
        if (($attributes -band [IO.FileAttributes]::ReadOnly) -ne 0) { throw "root is read-only" }
        $probe = Join-Path $resolvedRoot (".tts-more-write-probe-{0}.tmp" -f [guid]::NewGuid().ToString("N"))
        [IO.File]::WriteAllText($probe, "write probe", $script:Utf8NoBom)
        [IO.File]::Delete($probe)
    } catch {
        Throw-PortableStartError "PACKAGE_NOT_WRITABLE" "The package root is not writable: $($_.Exception.Message)"
    }
}

function Open-PackageOperationLock {
    param([Parameter(Mandatory = $true)][string]$Root)

    $context = if ($script:Context -and [string]::Equals($script:Context.Root, [IO.Path]::GetFullPath($Root), [StringComparison]::OrdinalIgnoreCase)) { $script:Context } else { Get-PackageContext -Root $Root }
    New-Item -ItemType Directory -Force -Path $context.OperationsRoot | Out-Null
    $lockPath = Join-Path $context.OperationsRoot ".start.lock"
    try {
        return [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        Throw-PortableStartError "OPERATION_ACTIVE" "Another package start operation owns the controller lock"
    }
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Payload
    )
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), [guid]::NewGuid().ToString("N"))
    $backup = ""
    $json = ($Payload | ConvertTo-Json -Depth 12 -Compress) + "`n"
    [IO.File]::WriteAllText($temporary, $json, $script:Utf8NoBom)
    try {
        $lastWriteError = $null
        foreach ($attempt in 1..15) {
            try {
                if (Test-Path -LiteralPath $Path -PathType Leaf) {
                    if ([string]::IsNullOrWhiteSpace($backup)) {
                        $backup = Join-Path $parent (".{0}.{1}.backup" -f (Split-Path -Leaf $Path), [guid]::NewGuid().ToString("N"))
                    }
                    # File.Replace is the sole destination replacement primitive; it never deletes the old destination first.
                    [IO.File]::Replace($temporary, $Path, $backup)
                    if (Test-Path -LiteralPath $backup -PathType Leaf) { [IO.File]::Delete($backup) }
                } else {
                    [IO.File]::Move($temporary, $Path)
                }
                $lastWriteError = $null
                break
            } catch [IO.IOException] {
                $lastWriteError = $_
                Start-Sleep -Milliseconds ([Math]::Min(500, 40 * $attempt))
            } catch [UnauthorizedAccessException] {
                $lastWriteError = $_
                Start-Sleep -Milliseconds ([Math]::Min(500, 40 * $attempt))
            }
        }
        if ($null -ne $lastWriteError) {
            throw $lastWriteError
        }
    } catch {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) { [IO.File]::Delete($temporary) }
        throw
    }
}

function Read-PortableImportDecision {
    param([Parameter(Mandatory = $true)][object]$Context)

    try {
        $path = Resolve-PortablePackagePath -Root $Context.Root -RelativePath "data/local/portable-import-decision.json" -Label "portable import decision"
        if (![string]::Equals([IO.Path]::GetFullPath($path), [IO.Path]::GetFullPath($Context.ImportDecisionPath), [StringComparison]::OrdinalIgnoreCase)) { return $null }
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
        $file = Get-Item -LiteralPath $path
        if ($file.Length -gt 4096) { return $null }
        $payload = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        $names = @($payload.PSObject.Properties.Name | Sort-Object)
        $expected = @("build_id", "schema_version", "status", "timestamp")
        if (($names -join "`n") -ne ($expected -join "`n")) { return $null }
        if ($payload.schema_version -isnot [int] -or [int]$payload.schema_version -ne 1) { return $null }
        if ([string]$payload.build_id -ne [string]$Context.BuildId) { return $null }
        if ([string]$payload.status -notin @("completed", "declined", "cancelled")) { return $null }
        if ([string]::IsNullOrWhiteSpace([string]$payload.timestamp)) { return $null }
        return $payload
    } catch { return $null }
}

function Write-PortableImportDecision {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [Parameter(Mandatory = $true)][ValidateSet("completed", "declined", "cancelled")][string]$Status
    )

    try {
        $path = Resolve-PortablePackagePath -Root $Context.Root -RelativePath "data/local/portable-import-decision.json" -Label "portable import decision"
        if (![string]::Equals([IO.Path]::GetFullPath($path), [IO.Path]::GetFullPath($Context.ImportDecisionPath), [StringComparison]::OrdinalIgnoreCase)) { throw "unexpected marker location" }
        Write-JsonAtomic -Path $path -Payload ([ordered]@{
            schema_version = 1
            build_id = [string]$Context.BuildId
            status = $Status
            timestamp = [DateTime]::UtcNow.ToString("o")
        })
    } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Unable to record the portable import decision"
    }
}

function Confirm-PortableImport {
    param([Parameter(Mandatory = $true)][string]$Prompt)

    Write-Host $Prompt
    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    $choice = Join-Path $systemDirectory "choice.exe"
    if (!(Test-Path -LiteralPath $choice -PathType Leaf)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The fixed Windows confirmation tool is unavailable"
    }
    $choiceResult = Invoke-PortableCapturedProcess -FilePath $choice -Arguments @("/C", "YN", "/N") -MaximumBytes 65536
    if ($choiceResult.Exceeded) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The fixed Windows confirmation tool returned invalid output"
    }
    if ($choiceResult.ExitCode -eq 1) { return $true }
    if ($choiceResult.ExitCode -eq 2) { return $false }
    Throw-PortableStartError "PACKAGE_CORRUPT" "The confirmation tool did not receive an explicit choice"
}

function ConvertTo-PortableNativeArgument {
    param([AllowEmptyString()][Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
            continue
        }
        if ($character -eq [char]34) {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
        } else {
            if ($backslashes -gt 0) { [void]$builder.Append(('\' * $backslashes)) }
            [void]$builder.Append($character)
        }
        $backslashes = 0
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-PortableSafeSystemPath {
    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    if ([string]::IsNullOrWhiteSpace($systemDirectory)) { return "" }
    $windowsDirectory = Split-Path -Parent $systemDirectory
    $entries = @(
        $systemDirectory,
        $windowsDirectory,
        (Join-Path $systemDirectory "Wbem"),
        (Join-Path $systemDirectory "WindowsPowerShell\v1.0")
    ) | Where-Object { ![string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_ -PathType Container) }
    return (@($entries | Select-Object -Unique) -join ";")
}

function Get-PortableSafeModulePath {
    $entries = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in @(
        (Join-Path $PSHOME "Modules"),
        $(if (![string]::IsNullOrWhiteSpace($env:ProgramFiles)) { Join-Path $env:ProgramFiles "WindowsPowerShell\Modules" } else { "" }),
        $(if (![string]::IsNullOrWhiteSpace($env:SystemRoot)) { Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules" } else { "" })
    )) {
        if (![string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            $entries.Add([IO.Path]::GetFullPath($candidate))
        }
    }
    return (@($entries | Select-Object -Unique) -join ";")
}

function Invoke-PortableCapturedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [AllowEmptyCollection()][Parameter(Mandatory = $true)][string[]]$Arguments,
        [ValidateRange(1, 4194304)][int]$MaximumBytes = 1048576,
        [switch]$Utf8
    )

    $process = $null
    try {
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = $FilePath
        $startInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-PortableNativeArgument -Value $_ }) -join ' ')
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $pathKey = @($startInfo.EnvironmentVariables.Keys | Where-Object { [string]$_ -ieq "PATH" } | Select-Object -First 1)
        if ([string]::IsNullOrWhiteSpace($pathKey)) { $pathKey = "PATH" }
        if ([string]::IsNullOrWhiteSpace([string]$startInfo.EnvironmentVariables[$pathKey])) {
            $safePath = Get-PortableSafeSystemPath
            if (![string]::IsNullOrWhiteSpace($safePath)) {
                $startInfo.EnvironmentVariables[$pathKey] = $safePath
            }
        }
        $modulePathKey = @($startInfo.EnvironmentVariables.Keys | Where-Object { [string]$_ -ieq "PSModulePath" } | Select-Object -First 1)
        if ([string]::IsNullOrWhiteSpace($modulePathKey)) { $modulePathKey = "PSModulePath" }
        $safeModulePath = Get-PortableSafeModulePath
        if (![string]::IsNullOrWhiteSpace($safeModulePath)) {
            $currentModulePath = [string]$startInfo.EnvironmentVariables[$modulePathKey]
            if ([string]::IsNullOrWhiteSpace($currentModulePath)) {
                $startInfo.EnvironmentVariables[$modulePathKey] = $safeModulePath
            } elseif (!$currentModulePath.ToLowerInvariant().Contains($PSHOME.ToLowerInvariant())) {
                $startInfo.EnvironmentVariables[$modulePathKey] = $safeModulePath + ";" + $currentModulePath
            }
        }
        if ($Utf8) {
            $startInfo.StandardOutputEncoding = $script:Utf8NoBom
            $startInfo.StandardErrorEncoding = $script:Utf8NoBom
            $startInfo.EnvironmentVariables["PYTHONUTF8"] = "1"
            $startInfo.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
        }
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $startInfo
        if (!$process.Start()) { throw "start failed" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = [string]$stdoutTask.Result
        $stderr = [string]$stderrTask.Result
        $exceeded = ($script:Utf8NoBom.GetByteCount($stdout) + $script:Utf8NoBom.GetByteCount($stderr)) -gt $MaximumBytes
        return [pscustomobject]@{
            ExitCode = [int]$process.ExitCode
            StdOut = $stdout
            StdErr = $stderr
            Exceeded = [bool]$exceeded
        }
    } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Unable to execute the fixed package tool"
    } finally {
        if ($process) { $process.Dispose() }
    }
}

function Resolve-PortableImportPython {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [string]$Operation = ""
    )

    $livePython = Join-Path $Context.Root "runtime\live\python.exe"
    if (Test-PortableRuntime -Root $Context.Root -SourceRoot $Context.SourceRoot -PythonPath $livePython -ExpectedVersion $Context.ExpectedPython -ImportProbe $Context.ImportProbe) {
        $jsonschemaProbe = Invoke-PortableCapturedProcess -FilePath $livePython -Arguments @("-c", "import jsonschema") -MaximumBytes 65536 -Utf8
        if ($jsonschemaProbe.ExitCode -eq 0 -and !$jsonschemaProbe.Exceeded) { return [IO.Path]::GetFullPath($livePython) }
    }

    $bootstrapArguments = @(
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", [string]$Context.BootstrapScript,
        "-CacheRoot", "data/cache/portable/conda",
        "-LockPath", [string]$Context.ToolchainLock,
        "-PackageRoot", [string]$Context.Root,
        "-PassThru"
    )
    if (![string]::IsNullOrWhiteSpace($Operation)) {
        $bootstrapArguments += @("-OperationRoot", $Operation, "-CancelFile", (Join-Path $Operation "cancel.requested"))
    }
    $bootstrapPowerShell = Resolve-PortablePowerShellHost
    $bootstrapResult = Invoke-PortableCapturedProcess -FilePath $bootstrapPowerShell -Arguments $bootstrapArguments
    if ($bootstrapResult.ExitCode -ne 0 -or $bootstrapResult.Exceeded) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The locked package bootstrap for import failed"
    }
    $bootstrapOutput = @($bootstrapResult.StdOut -split '\r?\n')
    $cacheRoot = [IO.Path]::GetFullPath((Join-Path $Context.Root "data\cache\portable\conda"))
    $condaCandidates = @($bootstrapOutput | ForEach-Object { [string]$_ } | Where-Object {
        ![string]::IsNullOrWhiteSpace($_) -and [IO.Path]::IsPathRooted($_) -and
        (Test-Path -LiteralPath $_ -PathType Leaf) -and ((Split-Path -Leaf $_) -ieq "conda.bat") -and
        (Test-PathWithinRoot -Root $cacheRoot -Path ([IO.Path]::GetFullPath($_)))
    } | ForEach-Object { [IO.Path]::GetFullPath($_) } | Select-Object -Unique)
    if ($condaCandidates.Count -ne 1) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The locked package bootstrap did not produce one valid runtime"
    }
    $condaRoot = Split-Path -Parent (Split-Path -Parent $condaCandidates[0])
    $python = Join-Path $condaRoot "python.exe"
    $relativePython = $python.Substring(([IO.Path]::GetFullPath($Context.Root)).TrimEnd('\', '/').Length).TrimStart('\', '/').Replace('\', '/')
    try {
        $resolvedPython = Resolve-PortablePackagePath -Root $Context.Root -RelativePath $relativePython -Label "import runtime" -MustExist
    } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The locked package bootstrap runtime is outside its fixed cache"
    }
    if (!(Test-PathWithinRoot -Root $cacheRoot -Path $resolvedPython)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The locked package bootstrap runtime is outside its fixed cache"
    }
    $versionProbe = if ([string]$Context.ExpectedPython -match '^\d+\.\d+\.\d+$') { "import platform;print(platform.python_version())" } else { "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')" }
    $versionResult = Invoke-PortableCapturedProcess -FilePath $resolvedPython -Arguments @("-c", $versionProbe) -MaximumBytes 65536 -Utf8
    if ($versionResult.ExitCode -ne 0 -or $versionResult.Exceeded -or $versionResult.StdOut.Trim() -ne [string]$Context.ExpectedPython) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The locked package bootstrap runtime version is invalid"
    }
    $jsonschemaResult = Invoke-PortableCapturedProcess -FilePath $resolvedPython -Arguments @("-c", "import jsonschema") -MaximumBytes 65536 -Utf8
    if ($jsonschemaResult.ExitCode -ne 0 -or $jsonschemaResult.Exceeded) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The package import validator is unavailable"
    }
    return [IO.Path]::GetFullPath($resolvedPython)
}

function ConvertFrom-PortableBoundedJson {
    param(
        [Parameter(Mandatory = $true)][object[]]$Output,
        [Parameter(Mandatory = $true)][string]$Label,
        [ValidateRange(1, 1048576)][int]$MaximumBytes = 1048576
    )

    $text = $Output -join "`n"
    if ($script:Utf8NoBom.GetByteCount($text) -gt $MaximumBytes) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label exceeded its fixed output limit"
    }
    try {
        $payload = $text | ConvertFrom-Json
        if ($null -eq $payload -or $payload -is [string] -or $payload.GetType().IsArray) { throw "not an object" }
        return $payload
    } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label did not return one valid JSON object"
    }
}

function Select-PortableImportFolder {
    param([Parameter(Mandatory = $true)][object]$Context)

    $powerShell = Resolve-PortablePowerShellHost
    $selectorResult = Invoke-PortableCapturedProcess -FilePath $powerShell -Arguments @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", [string]$Context.ImportSelector) -MaximumBytes 65536 -Utf8
    if ($selectorResult.ExitCode -ne 0 -or $selectorResult.Exceeded) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The fixed previous-version folder selector failed"
    }
    $payload = ConvertFrom-PortableBoundedJson -Output @($selectorResult.StdOut) -Label "The fixed previous-version folder selector" -MaximumBytes 65536
    if ($payload.PSObject.Properties["cancelled"] -and $payload.cancelled -eq $true) { return "" }
    if (!$payload.PSObject.Properties["selected_path"] -or [string]::IsNullOrWhiteSpace([string]$payload.selected_path)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The fixed previous-version folder selector returned an invalid selection"
    }
    $selected = [IO.Path]::GetFullPath([string]$payload.selected_path)
    if (!(Test-Path -LiteralPath $selected -PathType Container)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The selected previous-version package is unavailable"
    }
    return $selected
}

function ConvertTo-PortableNonNegativeInteger {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (
        $Value -isnot [byte] -and $Value -isnot [int16] -and $Value -isnot [int32] -and
        $Value -isnot [int64] -and $Value -isnot [uint16] -and $Value -isnot [uint32] -and
        $Value -isnot [uint64]
    ) { Throw-PortableStartError "PACKAGE_CORRUPT" "$Label must be a non-negative integer" }
    $number = [decimal]$Value
    if ($number -lt 0 -or $number -gt [int64]::MaxValue) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "$Label must be a non-negative integer"
    }
    return [int64]$number
}

function Assert-PortableImportAssetName {
    param([Parameter(Mandatory = $true)][object]$Value)

    if ($Value -isnot [string]) { Throw-PortableStartError "PACKAGE_CORRUPT" "An import asset name is invalid" }
    $name = [string]$Value
    if (
        [string]::IsNullOrWhiteSpace($name) -or [IO.Path]::IsPathRooted($name) -or
        $name.Contains("\") -or $name.Contains(":") -or $name -match '[\x00-\x1f\x7f]' -or
        $name.StartsWith("/") -or $name.EndsWith("/")
    ) { Throw-PortableStartError "PACKAGE_CORRUPT" "An import asset name is invalid" }
    foreach ($segment in @($name -split '/')) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -in @(".", "..")) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "An import asset name is invalid"
        }
    }
    return $name
}

function Assert-PortableImportServiceStopped {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port
    )

    try {
        $recordPath = Resolve-PortablePackagePath -Root $Context.Root -RelativePath "data/local/run/worker.pid.json" -Label "portable process record"
        if (Test-Path -LiteralPath $recordPath -PathType Leaf) {
            $record = Get-Item -LiteralPath $recordPath
            if ($record.Length -gt 65536) { throw "oversized process record" }
            $payload = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
            $pidValue = 0
            if (!$payload.PSObject.Properties["pid"] -or ![int]::TryParse([string]$payload.pid, [ref]$pidValue) -or $pidValue -le 0) {
                throw "invalid process record"
            }
            if ($null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) {
                Throw-PortableStartError "PACKAGE_CORRUPT" "Stop the running worker/service before importing previous-version data"
            }
        }
    } catch [PortableStartException] {
        throw
    } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Unable to prove that the package worker/service is stopped"
    }
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
        if ($listeners.Count -gt 0) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "Stop the running worker/service before importing previous-version data"
        }
    } catch [PortableStartException] {
        throw
    } catch {
        if ($_.FullyQualifiedErrorId -eq "CmdletizationQuery_NotFound,Get-NetTCPConnection") { return }
        Throw-PortableStartError "PACKAGE_CORRUPT" "Unable to prove that the target service port is stopped"
    }
}

function Assert-PortableImportNotCancelled {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [string]$Operation = ""
    )

    if ([string]::IsNullOrWhiteSpace($Operation)) { return }
    try {
        $cancelFile = Join-Path $Operation "cancel.requested"
        [void](Assert-PortableExactOperationContract -OperationsRoot $Context.OperationsRoot -OperationRoot $Operation -CancelFile $cancelFile -RequireOperation)
    } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" "The import cancellation contract is invalid"
    }
    if (Test-Path -LiteralPath $cancelFile -PathType Leaf) {
        Throw-PortableStartError "CANCELLED" "Previous-version import was cancelled"
    }
}

function Invoke-PortableImportOffer {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [string]$Operation = "",
        [Parameter(Mandatory = $true)][string]$ManagedBy,
        [switch]$NoUi,
        [switch]$OfferImport,
        [ValidateRange(0, 65535)][int]$Port = 0
    )

    $result = [ordered]@{ Status = "skipped"; MarkAfterReady = $false }
    if (!$Context.IsStaged -or $ManagedBy -ne "direct" -or $NoUi) { return [pscustomobject]$result }
    if (!$OfferImport -and $null -ne (Read-PortableImportDecision -Context $Context)) { return [pscustomobject]$result }
    $effectivePort = if ($Port -gt 0) { $Port } else { [int]$Context.Port }

    if (!(Confirm-PortableImport -Prompt "Import user data and reusable models from a previous portable package? [Y/N]")) {
        return [pscustomobject]@{ Status = "declined"; MarkAfterReady = $true }
    }

    try {
        Assert-PortableSha256Manifest -Root $Context.Root -ManifestPath $Context.Sha256Manifest -RequiredCoverage $Context.ImportCoverage
    } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" "SHA256SUMS verification failed for the fixed import toolchain"
    }
    Assert-PortableImportServiceStopped -Context $Context -Port $effectivePort
    Assert-PortableImportNotCancelled -Context $Context -Operation $Operation

    $selected = Select-PortableImportFolder -Context $Context
    if ([string]::IsNullOrWhiteSpace($selected)) {
        return [pscustomobject]@{ Status = "cancelled"; MarkAfterReady = $true }
    }
    Assert-PortableImportNotCancelled -Context $Context -Operation $Operation

    $python = Resolve-PortableImportPython -Context $Context -Operation $Operation
    Assert-PortableImportNotCancelled -Context $Context -Operation $Operation
    Assert-PortableImportServiceStopped -Context $Context -Port $effectivePort
    $planResult = Invoke-PortableCapturedProcess -FilePath $python -Arguments @([string]$Context.ImportCli, "plan", "--old-root", $selected, "--new-root", [string]$Context.Root) -Utf8
    if ($planResult.ExitCode -ne 0 -or $planResult.Exceeded) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Previous-version import planning failed"
    }
    Assert-PortableImportNotCancelled -Context $Context -Operation $Operation
    Assert-PortableImportServiceStopped -Context $Context -Port $effectivePort
    $plan = ConvertFrom-PortableBoundedJson -Output @($planResult.StdOut) -Label "Previous-version import planning"
    if (!$plan.PSObject.Properties["plan_digest"] -or [string]$plan.plan_digest -notmatch '^[0-9a-fA-F]{64}$') {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Previous-version import planning returned an invalid digest"
    }
    if (!$plan.PSObject.Properties["old_package_preserved"] -or $plan.old_package_preserved -ne $true) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Previous-version import planning did not preserve the old package"
    }
    foreach ($propertyName in @("user_file_count", "user_bytes", "reusable_asset_bytes")) {
        if (!$plan.PSObject.Properties[$propertyName]) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "Previous-version import planning omitted a safe summary field"
        }
    }
    $userFiles = ConvertTo-PortableNonNegativeInteger -Value $plan.user_file_count -Label "Imported user file count"
    $userBytes = ConvertTo-PortableNonNegativeInteger -Value $plan.user_bytes -Label "Imported user byte count"
    $assetBytes = ConvertTo-PortableNonNegativeInteger -Value $plan.reusable_asset_bytes -Label "Reusable asset byte count"
    foreach ($listName in @("reusable_assets", "skipped_assets", "already_present")) {
        if (!$plan.PSObject.Properties[$listName] -or !($plan.$listName -is [System.Array])) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "Previous-version import planning returned an invalid asset list"
        }
        if (@($plan.$listName).Count -gt 100000) {
            Throw-PortableStartError "PACKAGE_CORRUPT" "Previous-version import planning returned too many assets"
        }
    }
    $assets = @($plan.reusable_assets)
    $safeAssets = @($assets | ForEach-Object { Assert-PortableImportAssetName -Value $_ })
    $skippedAssetCount = @($plan.skipped_assets).Count
    $alreadyPresentCount = @($plan.already_present).Count

    Write-Host ("Import plan: user files {0}, user bytes {1}; reusable assets {2}, asset bytes {3}; skipped {4}; already present {5}." -f $userFiles, $userBytes, $safeAssets.Count, $assetBytes, $skippedAssetCount, $alreadyPresentCount)
    foreach ($asset in @($safeAssets | Select-Object -First 20)) { Write-Host ("  - {0}" -f $asset) }
    if ($safeAssets.Count -gt 20) { Write-Host ("  (plus {0} more not shown)" -f ($safeAssets.Count - 20)) }
    Write-Host "The previous portable package will be left unchanged; import copies into this package."
    Write-Host "Workers/services must stay stopped during import; the launcher starts services after import completes."
    if (!(Confirm-PortableImport -Prompt "Apply this import now? [Y/N]")) {
        return [pscustomobject]@{ Status = "declined"; MarkAfterReady = $true }
    }
    Assert-PortableImportNotCancelled -Context $Context -Operation $Operation
    Assert-PortableImportServiceStopped -Context $Context -Port $effectivePort

    $digest = [string]$plan.plan_digest
    $applyResult = Invoke-PortableCapturedProcess -FilePath $python -Arguments @([string]$Context.ImportCli, "apply", "--old-root", $selected, "--new-root", [string]$Context.Root, "--confirmed-digest", $digest) -Utf8
    if ($applyResult.ExitCode -ne 0 -or $applyResult.Exceeded) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "Previous-version import failed"
    }
    [void](ConvertFrom-PortableBoundedJson -Output @($applyResult.StdOut) -Label "Previous-version import")
    Write-PortableImportDecision -Context $Context -Status "completed"
    Assert-PortableImportNotCancelled -Context $Context -Operation $Operation
    Write-Host "Previous portable package import completed; the original package was not modified."
    return [pscustomobject]@{ Status = "completed"; MarkAfterReady = $false }
}

function Initialize-Operation {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$OperationId,
        [Parameter(Mandatory = $true)][string]$Initiator
    )
    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    $parsed = [guid]::Empty
    if (![guid]::TryParse($OperationId, [ref]$parsed)) { Throw-PortableStartError "PACKAGE_CORRUPT" "OperationId must be a valid UUID" }
    $canonicalId = $parsed.ToString()
    $operationRoot = [IO.Path]::GetFullPath((Join-Path $context.OperationsRoot $canonicalId))
    if (!(Test-PathWithinRoot -Root $context.OperationsRoot -Path $operationRoot) -or ![string]::Equals((Split-Path -Parent $operationRoot), [IO.Path]::GetFullPath($context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase)) {
        Throw-PortableStartError "PACKAGE_CORRUPT" "OperationRoot must be a UUID direct child of data.operations"
    }
    New-Item -ItemType Directory -Force -Path $operationRoot | Out-Null
    try { [void](Assert-PortableExactOperationContract -OperationsRoot $context.OperationsRoot -OperationRoot $operationRoot -CancelFile (Join-Path $operationRoot "cancel.requested") -RequireOperation) } catch {
        Throw-PortableStartError "PACKAGE_CORRUPT" $_.Exception.Message
    }
    $operationPath = Join-Path $operationRoot "operation.json"
    if (Test-Path -LiteralPath $operationPath -PathType Leaf) { Throw-PortableStartError "OPERATION_ACTIVE" "Operation already exists: $canonicalId" }
    $operation = [ordered]@{
        operation_id = $canonicalId
        component = $context.Component
        action = "start"
        initiator = $Initiator
        started_at = [DateTime]::UtcNow.ToString("o")
        status = "not_initialized"
        exit_code = $null
    }
    Write-JsonAtomic -Path $operationPath -Payload $operation
    return $operationRoot
}

function Add-OperationEvent {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][ValidateSet("not_initialized", "checking", "downloading", "installing", "validating", "starting", "ready", "stopped", "repairable", "blocked")][string]$Phase,
        [Parameter(Mandatory = $true)][string]$Message,
        [string]$ErrorCode = "",
        [Nullable[double]]$Percent = $null
    )
    if ($script:Context) {
        try { [void](Assert-PortableExactOperationContract -OperationsRoot $script:Context.OperationsRoot -OperationRoot $Operation -CancelFile (Join-Path $Operation "cancel.requested") -RequireOperation) } catch { Throw-PortableStartError "PACKAGE_CORRUPT" $_.Exception.Message }
    }
    $eventsPath = Join-Path $Operation "events.jsonl"
    $sequence = 1
    if (Test-Path -LiteralPath $eventsPath -PathType Leaf) {
        $sequence = @([IO.File]::ReadAllLines($eventsPath) | Where-Object { ![string]::IsNullOrWhiteSpace($_) }).Count + 1
    }
    $event = [ordered]@{
        seq = $sequence
        timestamp = [DateTime]::UtcNow.ToString("o")
        phase = $Phase
        message = $Message
    }
    if ($null -ne $Percent) { $event.percent = [Math]::Max(0.0, [Math]::Min(100.0, [double]$Percent)) }
    if (![string]::IsNullOrWhiteSpace($ErrorCode)) { $event.error_code = $ErrorCode }
    $line = ($event | ConvertTo-Json -Depth 6 -Compress) + "`n"
    $bytes = $script:Utf8NoBom.GetBytes($line)
    $stream = New-Object IO.FileStream($eventsPath, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}

function Complete-Operation {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][int]$ExitCode
    )
    $operationPath = Join-Path $Operation "operation.json"
    $payload = Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json
    $payload.status = $Status
    $payload.exit_code = $ExitCode
    $payload | Add-Member -NotePropertyName finished_at -NotePropertyValue ([DateTime]::UtcNow.ToString("o")) -Force
    Write-JsonAtomic -Path $operationPath -Payload $payload
}

function Get-PortableErrorCode {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    if ($ErrorRecord.Exception -is [PortableStartException]) { return [string]$ErrorRecord.Exception.Code }
    $message = [string]$ErrorRecord.Exception.Message
    if ($message -match '(?i)port .*in use|port .*occupied|PORT_IN_USE') { return "PORT_IN_USE" }
    if ($message -match '(?i)space|disk') { return "DISK_SPACE_INSUFFICIENT" }
    if ($message -match '(?i)CUDA') { return "CUDA_PROBE_FAILED" }
    if ($message -match '(?i)download|network|HTTP') { return "DOWNLOAD_NETWORK_INTERRUPTED" }
    return "PACKAGE_CORRUPT"
}

function Format-PortableErrorMessage {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)

    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in @(
        $ErrorRecord.Exception.Message,
        $(if ($ErrorRecord.ErrorDetails) { $ErrorRecord.ErrorDetails.Message } else { "" }),
        $(if ($ErrorRecord.InvocationInfo) { $ErrorRecord.InvocationInfo.PositionMessage } else { "" }),
        ([string]$ErrorRecord)
    )) {
        $message = ([string]$candidate) -replace '[\r\n]+', ' '
        $message = $message.Trim()
        if (![string]::IsNullOrWhiteSpace($message) -and !$parts.Contains($message)) {
            $parts.Add($message)
        }
    }
    if ($parts.Count -eq 0) { return "No diagnostic message was provided by PowerShell" }
    $combined = $parts -join " | "
    if ($combined.Length -gt 2048) { return $combined.Substring(0, 2048) + "..." }
    return $combined
}

function Fail-Operation {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord
    )
    if (!(Test-Path -LiteralPath (Join-Path $Operation "operation.json") -PathType Leaf)) { return }
    $code = Get-PortableErrorCode -ErrorRecord $ErrorRecord
    $exitCode = Resolve-PortableExitCode -ErrorRecord $ErrorRecord
    $status = if ($exitCode -eq 20) { "stopped" } else { "blocked" }
    Add-OperationEvent -Operation $Operation -Phase $status -Message (Format-PortableErrorMessage -ErrorRecord $ErrorRecord) -ErrorCode $code
    Complete-Operation -Operation $Operation -Status $status -ExitCode $exitCode
}

function Test-InstallState {
    param([Parameter(Mandatory = $true)][string]$Root, [switch]$Full)

    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    return Test-PortableInstallStateComplete -Root $context.Root -SourceRoot $context.SourceRoot -StatePath $context.StatePath -Component $context.Component -BuildId $context.BuildId -RuntimeLock $context.RuntimeLock -ModelLock $context.ModelLock -ExpectedPython $context.ExpectedPython -ImportProbe $context.ImportProbe -ValidateAssets:$Full -Sha256Manifest $context.Sha256Manifest -RequiredCoverage $context.RequiredCoverage
}

function Get-PortableInstallStateDiagnostic {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [switch]$ProbeRuntime
    )

    $parts = New-Object System.Collections.Generic.List[string]
    try {
        $stateExists = Test-Path -LiteralPath $Context.StatePath -PathType Leaf
        $parts.Add("state=" + $(if ($stateExists) { "present" } else { "missing" }))
        if ($stateExists) {
            try {
                $state = Get-Content -LiteralPath $Context.StatePath -Raw | ConvertFrom-Json
                $parts.Add("state_component=$([string]$state.component)")
                $parts.Add("state_build_id=$([string]$state.build_id)")
                $parts.Add("state_profile=$([string]$state.profile)")
                if ([string]::IsNullOrWhiteSpace([string]$state.runtime_lock_sha256)) { $parts.Add("runtime_lock_sha256=missing") }
                if ([string]::IsNullOrWhiteSpace([string]$state.model_lock_sha256)) { $parts.Add("model_lock_sha256=missing") }
            } catch {
                $parts.Add("state_json=invalid")
            }
        }
        foreach ($entry in @(
            @("runtime_lock", [string]$Context.RuntimeLock),
            @("model_lock", [string]$Context.ModelLock)
        )) {
            $label = [string]$entry[0]
            $path = [string]$entry[1]
            if ([string]::IsNullOrWhiteSpace($path)) {
                $parts.Add("$label=not_configured")
            } else {
                $parts.Add("$label=" + $(if (Test-Path -LiteralPath $path -PathType Leaf) { "present" } else { "missing" }))
            }
        }
        $python = Join-Path $Context.Root "runtime\live\python.exe"
        $pythonExists = Test-Path -LiteralPath $python -PathType Leaf
        $parts.Add("runtime_python=" + $(if ($pythonExists) { "present" } else { "missing" }))
        $parts.Add("expected_python=$([string]$Context.ExpectedPython)")
        if ($pythonExists -and $ProbeRuntime) {
            try {
                $versionProbe = if ([string]$Context.ExpectedPython -match '^\d+\.\d+\.\d+$') { "import platform;print(platform.python_version())" } else { "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" }
                $versionOutput = @(& $python -c $versionProbe 2>&1)
                $versionText = (($versionOutput -join " ") -replace '[\r\n]+', ' ').Trim()
                if ($versionText.Length -gt 160) { $versionText = $versionText.Substring(0, 160) + "..." }
                $parts.Add("python_version_exit=$LASTEXITCODE")
                if (![string]::IsNullOrWhiteSpace($versionText)) { $parts.Add("python_version_output=$versionText") }
            } catch {
                $parts.Add("python_version_error=$(Format-PortableErrorMessage -ErrorRecord $_)")
            }
        } elseif ($pythonExists) {
            $parts.Add("runtime_probe=skipped_until_integrity_passes")
        }
    } catch {
        $parts.Add("diagnostic_error=$(Format-PortableErrorMessage -ErrorRecord $_)")
    }
    return ($parts -join "; ")
}

function Invoke-ChildPowerShell {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $powerShell = Resolve-PortablePowerShellHost
    $command = @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $Script) + $Arguments
    $result = Invoke-PortableCapturedProcess -FilePath $powerShell -Arguments $command
    $output = @()
    if (![string]::IsNullOrEmpty($result.StdOut)) { $output += @($result.StdOut -split '\r?\n') }
    if (![string]::IsNullOrEmpty($result.StdErr)) { $output += @($result.StdErr -split '\r?\n') }
    foreach ($line in $output) { Write-Host ([string]$line) }
    return [pscustomobject]@{ ExitCode = [int]$result.ExitCode; Output = ($output -join "`n") }
}

function Invoke-Initialize {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Operation
    )
    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    $cancelFile = Join-Path $Operation "cancel.requested"
    try { [void](Assert-PortableExactOperationContract -OperationsRoot $context.OperationsRoot -OperationRoot $Operation -CancelFile $cancelFile -RequireOperation) } catch { Throw-PortableStartError "PACKAGE_CORRUPT" $_.Exception.Message }
    $result = Invoke-ChildPowerShell -Script $context.InitializeScript -Arguments @("-PackageRoot", $context.Root, "-OperationRoot", $Operation, "-CancelFile", $cancelFile)
    if ($result.ExitCode -eq 20) { Throw-PortableStartError "CANCELLED" "Portable initialization was cancelled" }
    if ($result.ExitCode -ne 0) {
        if ($result.Output -match '(?i)space|disk') { Throw-PortableStartError "DISK_SPACE_INSUFFICIENT" $result.Output }
        if ($result.Output -match '(?i)CUDA') { Throw-PortableStartError "CUDA_PROBE_FAILED" $result.Output }
        if ($result.Output -match '(?i)download|network|HTTP') { Throw-PortableStartError "DOWNLOAD_NETWORK_INTERRUPTED" $result.Output }
        Throw-PortableStartError "PACKAGE_CORRUPT" "Initialization failed: $($result.Output)"
    }
}

function Invoke-ServiceStart {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Operation,
        [Nullable[int]]$PortOverride = $null
    )
    $context = if ($script:Context) { $script:Context } else { Get-PackageContext -Root $Root }
    try { [void](Assert-PortableExactOperationContract -OperationsRoot $context.OperationsRoot -OperationRoot $Operation -CancelFile (Join-Path $Operation "cancel.requested") -RequireOperation) } catch { Throw-PortableStartError "PACKAGE_CORRUPT" $_.Exception.Message }
    $arguments = @("-PackageRoot", $context.Root, "-OperationRoot", $Operation)
    if ($null -ne $PortOverride) { $arguments += @("-PortOverride", [string]$PortOverride) }
    $result = Invoke-ChildPowerShell -Script $context.ServiceScript -Arguments $arguments
    if ($result.ExitCode -ne 0) {
        if ($result.Output -match '(?i)PORT_IN_USE|port .*in use|port .*occupied') { Throw-PortableStartError "PORT_IN_USE" $result.Output }
        Throw-PortableStartError "PACKAGE_CORRUPT" "Service start failed: $($result.Output)"
    }
}

function Resolve-PortableExitCode {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    $code = Get-PortableErrorCode -ErrorRecord $ErrorRecord
    switch ($code) {
        "CANCELLED" { return 20 }
        "PACKAGE_NOT_WRITABLE" { return 21 }
        "PACKAGE_CORRUPT" { return 22 }
        "PORT_IN_USE" { return 23 }
        "DISK_SPACE_INSUFFICIENT" { return 24 }
        "CUDA_PROBE_FAILED" { return 25 }
        "DOWNLOAD_NETWORK_INTERRUPTED" { return 26 }
        default { return 1 }
    }
}

function Start-ProgressWindow {
    param(
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string]$Url
    )
    $progressScript = Join-Path $PSScriptRoot "Show-PortableProgress.ps1"
    if (!(Test-Path -LiteralPath $progressScript -PathType Leaf)) { return }
    $quotedScript = '"{0}"' -f $progressScript.Replace('"', '\"')
    $quotedOperation = '"{0}"' -f $Operation.Replace('"', '\"')
    $quotedUrl = '"{0}"' -f $Url.Replace('"', '\"')
    Start-Process -FilePath (Resolve-PortablePowerShellHost) -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $quotedScript, "-OperationRoot", $quotedOperation, "-Url", $quotedUrl) -WindowStyle Normal | Out-Null
}

function Wait-ForActiveOperation {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [switch]$NoUi
    )
    $activePath = Join-Path $Context.OperationsRoot "active-start.json"
    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    while (!(Test-Path -LiteralPath $activePath -PathType Leaf) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 50 }
    if (!(Test-Path -LiteralPath $activePath -PathType Leaf)) { Throw-PortableStartError "OPERATION_ACTIVE" "The active operation pointer was not published" }
    try { $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json } catch { Throw-PortableStartError "OPERATION_ACTIVE" "The active operation pointer is unreadable" }
    $parsed = [guid]::Empty
    if (![guid]::TryParse([string]$active.operation_id, [ref]$parsed)) { Throw-PortableStartError "OPERATION_ACTIVE" "The active operation id is invalid" }
    $operation = [IO.Path]::GetFullPath((Join-Path $Context.OperationsRoot $parsed.ToString()))
    if (![string]::Equals((Split-Path -Parent $operation), [IO.Path]::GetFullPath($Context.OperationsRoot), [StringComparison]::OrdinalIgnoreCase)) {
        Throw-PortableStartError "OPERATION_ACTIVE" "The active operation is outside data.operations"
    }
    Write-Host "Attaching to active operation $($parsed.ToString())"
    $operationPath = Join-Path $operation "operation.json"
    $deadOwnerDeadline = $null
    while ($true) {
        if (Test-Path -LiteralPath $operationPath -PathType Leaf) {
            try { $payload = Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json } catch { $payload = $null }
            if ($payload -and $null -ne $payload.exit_code) { return [int]$payload.exit_code }
        }
        $ownerAlive = $false
        try {
            $owner = Get-Process -Id ([int]$active.owner_pid) -ErrorAction Stop
            $ownerStarted = $owner.StartTime.ToUniversalTime().ToString("o")
            $ownerAlive = [string]::Equals($ownerStarted, [string]$active.owner_started_at, [StringComparison]::OrdinalIgnoreCase)
        } catch { $ownerAlive = $false }
        try {
            $probeLock = Open-PackageOperationLock -Root $Context.Root
            $probeLock.Dispose()
            return -999
        } catch {
            if ((Get-PortableErrorCode -ErrorRecord $_) -ne "OPERATION_ACTIVE") { throw }
        }
        if ($ownerAlive) {
            $deadOwnerDeadline = $null
        } elseif ($null -eq $deadOwnerDeadline) {
            $deadOwnerDeadline = [DateTime]::UtcNow.AddSeconds(12)
        } elseif ([DateTime]::UtcNow -ge $deadOwnerDeadline) {
            Throw-PortableStartError "OPERATION_ACTIVE" "The dead active-operation owner did not release the package lock before the recovery deadline"
        }
        Start-Sleep -Milliseconds 200
    }
}

function Clear-StaleActivePointer {
    param([Parameter(Mandatory = $true)][object]$Context)
    $pointer = Join-Path $Context.OperationsRoot "active-start.json"
    if (!(Test-Path -LiteralPath $pointer -PathType Leaf)) { return }
    try {
        $active = Get-Content -LiteralPath $pointer -Raw | ConvertFrom-Json
        $parsed = [guid]::Empty
        if (![guid]::TryParse([string]$active.operation_id, [ref]$parsed)) { throw "invalid operation id" }
        $staleOperation = Join-Path $Context.OperationsRoot $parsed.ToString()
        [void](Assert-PortableExactOperationContract -OperationsRoot $Context.OperationsRoot -OperationRoot $staleOperation -CancelFile (Join-Path $staleOperation "cancel.requested") -RequireOperation)
        $operationPath = Join-Path $staleOperation "operation.json"
        if (Test-Path -LiteralPath $operationPath -PathType Leaf) {
            $payload = Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json
            if ($null -eq $payload.exit_code) {
                Add-OperationEvent -Operation $staleOperation -Phase "blocked" -Message "Previous start controller lost the package lock; this operation was safely recovered" -ErrorCode "PACKAGE_CORRUPT"
                Complete-Operation -Operation $staleOperation -Status "blocked" -ExitCode 22
            }
        }
    } catch {
        Write-Warning "Ignoring unsafe or unreadable stale active-operation evidence: $($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $pointer -Force -ErrorAction SilentlyContinue
    }
}

# Dot-sourcing is used only by dependency-free tests of the controller's atomic helpers.
if ($MyInvocation.InvocationName -eq ".") { return }

$root = [IO.Path]::GetFullPath((Resolve-PortableStartRoot))
$operation = ""
$lock = $null
$activePath = ""
$ownsActivePointer = $false
$exitCode = 0
try {
    if ([string]::IsNullOrWhiteSpace($ManagedBy) -or $ManagedBy.Length -gt 128) { Throw-PortableStartError "PACKAGE_CORRUPT" "ManagedBy must be non-empty and at most 128 characters" }
    $script:Context = Get-PackageContext -Root $root
    Assert-PackageWritable -Root $root
    while (!$lock) {
        try {
            $lock = Open-PackageOperationLock -Root $root
        } catch {
            if ((Get-PortableErrorCode -ErrorRecord $_) -eq "OPERATION_ACTIVE") {
                $attachedExitCode = Wait-ForActiveOperation -Context $script:Context -NoUi:$NoUi
                if ($attachedExitCode -ne -999) { exit $attachedExitCode }
                continue
            }
            throw
        }
    }
    Clear-StaleActivePointer -Context $script:Context

    if ([string]::IsNullOrWhiteSpace($OperationId)) { $OperationId = [guid]::NewGuid().ToString() }
    $operation = Initialize-Operation -Root $root -OperationId $OperationId -Initiator $ManagedBy
    $OperationId = Split-Path -Leaf $operation
    $activePath = Join-Path $script:Context.OperationsRoot "active-start.json"
    $ownerStartedAt = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
    Write-JsonAtomic -Path $activePath -Payload ([ordered]@{ operation_id = $OperationId; owner_pid = $PID; owner_started_at = $ownerStartedAt; published_at = [DateTime]::UtcNow.ToString("o") })
    $ownsActivePointer = $true
    Add-OperationEvent -Operation $operation -Phase "checking" -Message "Checking portable package install state" -Percent 0
    $urlPort = if ($null -ne $PortOverride) { [int]$PortOverride } else { [int]$script:Context.Port }
    $url = "http://127.0.0.1:$urlPort"
    if (!$NoUi) { Start-ProgressWindow -Operation $operation -Url $url }

    $installed = Test-InstallState -Root $root -Full:($script:Context.Profile -eq "full")
    if (!$installed -and $script:Context.Profile -eq "full") {
        $installDiagnostic = Get-PortableInstallStateDiagnostic -Context $script:Context
        Throw-PortableStartError "PACKAGE_CORRUPT" "Full package assets are missing or invalid; Start will not download replacements ($installDiagnostic)"
    }
    $importOutcome = Invoke-PortableImportOffer -Context $script:Context -Operation $operation -ManagedBy $ManagedBy -NoUi:$NoUi -OfferImport:$OfferImport -Port $urlPort
    if (!$installed) {
        Add-OperationEvent -Operation $operation -Phase "installing" -Message "Initializing private package runtime" -Percent 5
        Invoke-Initialize -Root $root -Operation $operation
        if (!(Test-InstallState -Root $root)) {
            $installDiagnostic = Get-PortableInstallStateDiagnostic -Context $script:Context -ProbeRuntime
            Throw-PortableStartError "PACKAGE_CORRUPT" "Initialization did not produce a valid package-private runtime state ($installDiagnostic)"
        }
    }
    if (Test-Path -LiteralPath (Join-Path $operation "cancel.requested") -PathType Leaf) { Throw-PortableStartError "CANCELLED" "Portable start was cancelled" }
    Add-OperationEvent -Operation $operation -Phase "starting" -Message "Starting local service" -Percent 95
    Invoke-ServiceStart -Root $root -Operation $operation -PortOverride $PortOverride
    Add-OperationEvent -Operation $operation -Phase "ready" -Message "Service ready: $url" -Percent 100
    Complete-Operation -Operation $operation -Status "ready" -ExitCode 0
    if ($importOutcome.MarkAfterReady) {
        try {
            Write-PortableImportDecision -Context $script:Context -Status $importOutcome.Status
        } catch {
            Write-Warning "Unable to record the deferred import decision; the ready service remains available"
        }
    }
    Write-Host "$($script:Context.Component) ready: $url"
    if (!$NoUi) {
        if ($script:Context.Component -eq "tts-more") {
            Start-Process $url | Out-Null
        } else {
            try { Set-Clipboard -Value $url -ErrorAction Stop; Write-Host "Worker URL copied to clipboard." } catch { Write-Host "Copy the worker URL shown above." }
        }
    }
} catch {
    $primaryError = $_
    $exitCode = Resolve-PortableExitCode -ErrorRecord $primaryError
    $code = Get-PortableErrorCode -ErrorRecord $primaryError
    if ($operation) {
        try {
            Fail-Operation -Operation $operation -ErrorRecord $primaryError
        } catch {
            Write-Warning "Unable to persist failed operation state; preserving the original start failure"
        }
    }
    $flatMessage = Format-PortableErrorMessage -ErrorRecord $primaryError
    [Console]::Error.WriteLine(("PORTABLE_START_ERROR:{0}:{1}" -f $code, $flatMessage))
    Write-Error "[$code] $flatMessage" -ErrorAction Continue
} finally {
    if ($ownsActivePointer -and $activePath -and (Test-Path -LiteralPath $activePath -PathType Leaf)) {
        try {
            $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
            if ([string]$active.operation_id -eq $OperationId) { Remove-Item -LiteralPath $activePath -Force }
        } catch { }
    }
    if ($lock) { $lock.Dispose() }
}
exit $exitCode
