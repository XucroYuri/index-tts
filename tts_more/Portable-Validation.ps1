Set-StrictMode -Version Latest

function Assert-PortablePackageRootPathBudget {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [ValidateRange(1, 32767)][int]$SafeWindowsPathBudget = 240
    )

    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $generatedRuntimePath = Join-Path $resolvedRoot "runtime\staging\Lib\site-packages\transformers\utils\dummy_essentia_and_librosa_and_pretty_midi_and_scipy_and_torch_objects.py"
    if ($generatedRuntimePath.Length -gt $SafeWindowsPathBudget) {
        throw "Portable package path is too deep for the bundled runtime (projected path length $($generatedRuntimePath.Length) exceeds $SafeWindowsPathBudget). Move the package to a shorter directory such as D:\TTS\GPT and run again."
    }
}

function ConvertTo-PortableWindowsArgumentLine {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Arguments
    )

    $encoded = New-Object 'System.Collections.Generic.List[string]'
    foreach ($argument in @($Arguments)) {
        if ($null -eq $argument) { throw "Windows process arguments must not contain null" }

        $value = [string]$argument
        $builder = New-Object System.Text.StringBuilder
        [void]$builder.Append('"')
        $backslashCount = 0

        foreach ($character in $value.ToCharArray()) {
            if ($character -eq '\') {
                $backslashCount++
                continue
            }
            if ($character -eq '"') {
                [void]$builder.Append(('\' * (($backslashCount * 2) + 1)))
                [void]$builder.Append('"')
                $backslashCount = 0
                continue
            }
            if ($backslashCount -gt 0) {
                [void]$builder.Append(('\' * $backslashCount))
                $backslashCount = 0
            }
            [void]$builder.Append($character)
        }

        if ($backslashCount -gt 0) {
            [void]$builder.Append(('\' * ($backslashCount * 2)))
        }
        [void]$builder.Append('"')
        [void]$encoded.Add($builder.ToString())
    }

    return $encoded -join ' '
}

function Get-PortableFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() }
    finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
}

function Assert-PortableJsonObject {
    param([object]$Value, [string]$Label)
    if ($null -eq $Value -or $Value.GetType().FullName -ne "System.Management.Automation.PSCustomObject") { throw "$Label must be an object" }
}

function Assert-PortableJsonProperties {
    param([object]$Value, [string]$Label, [string[]]$Required)
    Assert-PortableJsonObject -Value $Value -Label $Label
    $actual = @($Value.PSObject.Properties.Name)
    foreach ($name in $Required) { if ($name -notin $actual) { throw "$Label.$name is required" } }
    foreach ($name in $actual) { if ($name -notin $Required) { throw "$Label.$name is not allowed" } }
}

function Assert-PortableJsonString {
    param([object]$Value, [string]$Label)
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$Value)) { throw "$Label must be a non-empty string" }
}

function Assert-PortableJsonStringArray {
    param([object]$Value, [string]$Label, [string[]]$Allowed = @())
    if ($null -eq $Value -or $Value.GetType().FullName -ne "System.Object[]" -or @($Value).Count -eq 0) { throw "$Label must be a non-empty array" }
    $seen = @{}
    foreach ($item in @($Value)) {
        Assert-PortableJsonString -Value $item -Label "$Label item"
        if ($Allowed.Count -gt 0 -and [string]$item -notin $Allowed) { throw "$Label contains an unsupported value" }
        if ($seen.ContainsKey([string]$item)) { throw "$Label items must be unique" }
        $seen[[string]$item] = $true
    }
}

function Assert-PortableV2Manifest {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][object]$Manifest)
    $top = @("schema_version", "component", "package_id", "release_version", "version", "build_id", "package_profile", "platform", "api_contract", "protocol", "source", "integration", "runtime", "models", "data_root", "data", "launchers", "endpoint", "capabilities", "sha256_manifest", "licenses")
    Assert-PortableJsonProperties -Value $Manifest -Label "manifest" -Required $top
    if ($Manifest.schema_version -isnot [int] -or [int]$Manifest.schema_version -ne 2) { throw "schema_version must be integer 2" }
    foreach ($name in @("component", "package_id", "release_version", "version", "build_id", "package_profile", "platform", "api_contract", "data_root", "sha256_manifest", "licenses")) { Assert-PortableJsonString -Value $Manifest.$name -Label $name }
    if ([string]$Manifest.component -notin @("tts-more", "gpt-sovits", "indextts", "cosyvoice")) { throw "component is unsupported" }
    if ([string]$Manifest.package_profile -notin @("bootstrap", "full")) { throw "package_profile is invalid" }
    if ([string]$Manifest.platform -ne "windows-x64") { throw "platform must be windows-x64" }
    if ([string]$Manifest.api_contract -ne "tts-more-v1") { throw "api_contract must be tts-more-v1" }

    Assert-PortableJsonProperties -Value $Manifest.protocol -Label "protocol" -Required @("name", "version", "controller_range")
    foreach ($name in @("name", "version", "controller_range")) { Assert-PortableJsonString -Value $Manifest.protocol.$name -Label "protocol.$name" }
    if ([string]$Manifest.protocol.name -ne "tts-more-v1") { throw "protocol.name must be tts-more-v1" }
    Assert-PortableJsonProperties -Value $Manifest.source -Label "source" -Required @("repository", "revision")
    foreach ($name in @("repository", "revision")) { Assert-PortableJsonString -Value $Manifest.source.$name -Label "source.$name" }
    if ([string]$Manifest.source.repository -notmatch '^https://') { throw "source.repository must use https" }
    if ([string]$Manifest.source.revision -notmatch '^[0-9a-fA-F]{40,64}$') { throw "source.revision is invalid" }
    Assert-PortableJsonProperties -Value $Manifest.integration -Label "integration" -Required @("version", "source_revision", "bundle_sha256")
    foreach ($name in @("version", "source_revision", "bundle_sha256")) { Assert-PortableJsonString -Value $Manifest.integration.$name -Label "integration.$name" }
    if ([string]$Manifest.integration.source_revision -notmatch '^[0-9a-fA-F]{40,64}$' -or [string]$Manifest.integration.bundle_sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw "integration immutable identity is invalid" }

    Assert-PortableJsonProperties -Value $Manifest.runtime -Label "runtime" -Required @("python_version", "device_profiles", "lock", "state_path")
    foreach ($name in @("python_version", "lock", "state_path")) { Assert-PortableJsonString -Value $Manifest.runtime.$name -Label "runtime.$name" }
    if ([string]$Manifest.runtime.python_version -notin @("3.10", "3.10.11", "3.11", "3.11.9")) { throw "runtime.python_version is unsupported" }
    Assert-PortableJsonStringArray -Value $Manifest.runtime.device_profiles -Label "runtime.device_profiles" -Allowed @("auto", "cu128", "cu126", "cpu")
    Assert-PortableJsonProperties -Value $Manifest.models -Label "models" -Required @("lock", "required")
    Assert-PortableJsonString -Value $Manifest.models.lock -Label "models.lock"
    if ($Manifest.models.required -isnot [bool]) { throw "models.required must be boolean" }
    Assert-PortableJsonProperties -Value $Manifest.data -Label "data" -Required @("user", "local", "cache", "operations")
    foreach ($name in @("user", "local", "cache", "operations")) { Assert-PortableJsonString -Value $Manifest.data.$name -Label "data.$name" }
    Assert-PortableJsonProperties -Value $Manifest.launchers -Label "launchers" -Required @("initialize", "start", "stop", "repair", "build")
    foreach ($name in @("initialize", "start", "stop", "repair", "build")) { Assert-PortableJsonString -Value $Manifest.launchers.$name -Label "launchers.$name" }
    Assert-PortableJsonProperties -Value $Manifest.endpoint -Label "endpoint" -Required @("default_url", "port", "health_path", "capabilities_path", "bind_policy")
    foreach ($name in @("default_url", "health_path", "capabilities_path", "bind_policy")) { Assert-PortableJsonString -Value $Manifest.endpoint.$name -Label "endpoint.$name" }
    if ($Manifest.endpoint.port -isnot [int] -or [int]$Manifest.endpoint.port -lt 1 -or [int]$Manifest.endpoint.port -gt 65535) { throw "endpoint.port must be an integer between 1 and 65535" }
    if ([string]$Manifest.endpoint.default_url -notmatch '^http://') { throw "endpoint.default_url must use http" }
    foreach ($name in @("health_path", "capabilities_path")) { if ([string]$Manifest.endpoint.$name -notmatch '^/') { throw "endpoint.$name must start with /" } }
    if ([string]$Manifest.endpoint.bind_policy -notin @("loopback", "trusted-lan")) { throw "endpoint.bind_policy is invalid" }
    Assert-PortableJsonStringArray -Value $Manifest.capabilities -Label "capabilities"

    foreach ($entry in @(
        @{ Value = [string]$Manifest.data_root; Label = "data_root"; File = $false },
        @{ Value = [string]$Manifest.runtime.lock; Label = "runtime.lock"; File = $true },
        @{ Value = [string]$Manifest.runtime.state_path; Label = "runtime.state_path"; File = $false },
        @{ Value = [string]$Manifest.models.lock; Label = "models.lock"; File = $true },
        @{ Value = [string]$Manifest.sha256_manifest; Label = "sha256_manifest"; File = $true },
        @{ Value = [string]$Manifest.licenses; Label = "licenses"; File = $true }
    )) { [void](Resolve-PortablePackagePath -Root $Root -RelativePath $entry.Value -Label $entry.Label -MustExist:([bool]$entry.File)) }
    foreach ($name in @("user", "local", "cache", "operations")) { [void](Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$Manifest.data.$name) -Label "data.$name") }
    foreach ($name in @("initialize", "start", "stop", "repair", "build")) { [void](Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$Manifest.launchers.$name) -Label "launchers.$name" -MustExist) }
}

function Test-PortablePathWithinRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $candidate = [IO.Path]::GetFullPath($Path)
    if ([string]::Equals($rootPath, $candidate, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $candidate.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-PortablePackageRootChain {
    param([Parameter(Mandatory = $true)][string]$Root)
    if ([string]::IsNullOrWhiteSpace($Root)) { throw "portable package root is required" }
    $lexicalRoot = [IO.Path]::GetFullPath($Root)
    $pathRoot = [IO.Path]::GetPathRoot($lexicalRoot)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) { throw "portable package root has no filesystem root" }
    $trimmedRoot = $lexicalRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    if ($trimmedRoot.Length -ge $pathRoot.Length) { $lexicalRoot = $trimmedRoot }
    $current = [IO.Path]::GetFullPath($pathRoot)
    $chain = [Collections.Generic.List[string]]::new()
    [void]$chain.Add($current)
    $relative = $lexicalRoot.Substring($pathRoot.Length).TrimStart([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $segments = @($relative -split '[\\/]' | Where-Object { ![string]::IsNullOrWhiteSpace($_) })
    foreach ($segment in $segments) {
        $current = [IO.Path]::GetFullPath((Join-Path $current $segment))
        [void]$chain.Add($current)
    }
    foreach ($candidate in $chain) {
        if (!(Test-Path -LiteralPath $candidate -PathType Container)) { throw "portable package root or ancestor is missing" }
        if ((((Get-Item -LiteralPath $candidate -Force).Attributes) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "portable package root or ancestor cannot be a reparse point"
        }
    }
    return $lexicalRoot
}

function Assert-PortablePackageRoot {
    param([Parameter(Mandatory = $true)][string]$Root)
    return Assert-PortablePackageRootChain -Root $Root
}

function Resolve-PortablePackagePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$MustExist
    )
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Contains(":")) {
        throw "$Label must be a package-relative path"
    }
    $segments = @($RelativePath -split '[\\/]')
    if ($segments -contains "..") { throw "$Label cannot escape the package" }
    $resolvedRoot = Assert-PortablePackageRoot -Root $Root
    $resolved = [IO.Path]::GetFullPath((Join-Path $resolvedRoot $RelativePath))
    if (!(Test-PortablePathWithinRoot -Root $resolvedRoot -Path $resolved)) { throw "$Label resolves outside the package" }
    $current = $resolvedRoot
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq ".") { continue }
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            if (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label traverses a reparse point"
            }
        }
    }
    if ($MustExist -and !(Test-Path -LiteralPath $resolved)) { throw "$Label is missing: $RelativePath" }
    return $resolved
}

function Get-PortableMutableTreeSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $resolved = Resolve-PortablePackagePath -Root $Root -RelativePath $RelativePath -Label $Label
    if (!(Test-Path -LiteralPath $resolved)) { return @() }
    if (!(Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "$Label must be a directory"
    }
    $pending = [Collections.Generic.Queue[string]]::new()
    $entries = [Collections.Generic.List[object]]::new()
    $pending.Enqueue($resolved)
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            $attributes = [IO.File]::GetAttributes($child.FullName)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse point: $($child.FullName)"
            }
            $isDirectory = ($attributes -band [IO.FileAttributes]::Directory) -ne 0
            $entries.Add([pscustomobject]@{
                FullName = [IO.Path]::GetFullPath($child.FullName)
                IsDirectory = $isDirectory
            })
            if ($isDirectory) { $pending.Enqueue($child.FullName) }
        }
    }
    return @($entries)
}

function Assert-PortableMutableTreeBoundary {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    [void](Resolve-PortablePackagePath -Root $Root -RelativePath $RelativePath -Label $Label)
    [void](Get-PortableMutableTreeSnapshot -Root $Root -RelativePath $RelativePath -Label $Label)
}

function Remove-PortableMutableDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $resolvedRoot = Assert-PortablePackageRoot -Root $Root
    $resolved = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath $RelativePath -Label $Label
    if (!(Test-Path -LiteralPath $resolved)) { return }
    $entries = @(Get-PortableMutableTreeSnapshot -Root $resolvedRoot -RelativePath $RelativePath -Label $Label)

    foreach ($entry in @($entries | Where-Object { !$_.IsDirectory } | Sort-Object { $_.FullName.Length } -Descending)) {
        $relative = $entry.FullName.Substring($resolvedRoot.Length).TrimStart('\', '/')
        $current = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath $relative -Label $Label -MustExist
        $attributes = [IO.File]::GetAttributes($current)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or ($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
            throw "$Label changed during safe cleanup"
        }
        if (($attributes -band [IO.FileAttributes]::ReadOnly) -ne 0) {
            [IO.File]::SetAttributes($current, ($attributes -band (-bnot [IO.FileAttributes]::ReadOnly)))
        }
        [IO.File]::Delete($current)
    }
    foreach ($entry in @($entries | Where-Object { $_.IsDirectory } | Sort-Object { $_.FullName.Length } -Descending)) {
        $relative = $entry.FullName.Substring($resolvedRoot.Length).TrimStart('\', '/')
        $current = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath $relative -Label $Label -MustExist
        $attributes = [IO.File]::GetAttributes($current)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or ($attributes -band [IO.FileAttributes]::Directory) -eq 0) {
            throw "$Label changed during safe cleanup"
        }
        [IO.Directory]::Delete($current, $false)
    }
    $resolved = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath $RelativePath -Label $Label -MustExist
    if (([IO.File]::GetAttributes($resolved) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label changed during safe cleanup"
    }
    [IO.Directory]::Delete($resolved, $false)
}

function Move-PortableMutableDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SourceRelativePath,
        [Parameter(Mandatory = $true)][string]$DestinationRelativePath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $source = Resolve-PortablePackagePath -Root $Root -RelativePath $SourceRelativePath -Label "$Label source" -MustExist
    [void](Get-PortableMutableTreeSnapshot -Root $Root -RelativePath $SourceRelativePath -Label "$Label source")
    $destination = Resolve-PortablePackagePath -Root $Root -RelativePath $DestinationRelativePath -Label "$Label destination"
    if (Test-Path -LiteralPath $destination) { throw "$Label destination already exists" }
    [IO.Directory]::Move($source, $destination)
}

function Assert-PortableExactOperationContract {
    param(
        [Parameter(Mandatory = $true)][string]$OperationsRoot,
        [Parameter(Mandatory = $true)][string]$OperationRoot,
        [Parameter(Mandatory = $true)][string]$CancelFile,
        [switch]$RequireOperation
    )
    $resolvedOperations = [IO.Path]::GetFullPath($OperationsRoot)
    $resolvedOperation = [IO.Path]::GetFullPath($OperationRoot)
    if (![string]::Equals((Split-Path -Parent $resolvedOperation), $resolvedOperations, [StringComparison]::OrdinalIgnoreCase)) {
        throw "OperationRoot must be a UUID-named direct child of the package operations root"
    }
    $parsed = [guid]::Empty
    if (![guid]::TryParse((Split-Path -Leaf $resolvedOperation), [ref]$parsed)) { throw "OperationRoot name must be a valid UUID" }
    if ($RequireOperation -and !(Test-Path -LiteralPath $resolvedOperation -PathType Container)) { throw "OperationRoot is missing" }
    if (Test-Path -LiteralPath $resolvedOperation) {
        if (([IO.File]::GetAttributes($resolvedOperation) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "OperationRoot cannot be a reparse point"
        }
    }
    $resolvedCancel = [IO.Path]::GetFullPath($CancelFile)
    $expectedCancel = [IO.Path]::GetFullPath((Join-Path $resolvedOperation "cancel.requested"))
    if (![string]::Equals($resolvedCancel, $expectedCancel, [StringComparison]::OrdinalIgnoreCase)) {
        throw "CancelFile must resolve exactly to OperationRoot/cancel.requested"
    }
    return [pscustomobject]@{ OperationRoot = $resolvedOperation; CancelFile = $resolvedCancel }
}

function Assert-PortableRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [string]$ImportProbe = "import sys"
    )
    $resolvedRoot = Assert-PortablePackageRoot -Root $Root
    $expectedPath = [IO.Path]::GetFullPath((Join-Path $resolvedRoot "runtime\live\python.exe"))
    $resolvedPython = [IO.Path]::GetFullPath($PythonPath)
    if (![string]::Equals($resolvedPython, $expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "package runtime must be runtime/live/python.exe"
    }
    $relative = $resolvedPython.Substring($resolvedRoot.Length).TrimStart('\', '/')
    [void](Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath $relative -Label "package runtime" -MustExist)
    if (!(Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { throw "package runtime is missing" }
    [void](Assert-PortablePackageRoot -Root $resolvedRoot)
    [void](Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath $relative -Label "package runtime" -MustExist)
    $versionProbe = if ($ExpectedVersion -match '^\d+\.\d+\.\d+$') {
        "import platform;print(platform.python_version())"
    } else {
        "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    }
    $versionOutput = @(& $resolvedPython -c $versionProbe 2>&1)
    if ($LASTEXITCODE -ne 0 -or ($versionOutput -join "").Trim() -ne $ExpectedVersion) {
        throw "package runtime Python version must be $ExpectedVersion"
    }
    if (![string]::IsNullOrWhiteSpace($ImportProbe)) {
        Invoke-PortablePythonSourceProbe -Root $resolvedRoot -SourceRoot $SourceRoot -PythonPath $resolvedPython -ImportProbe $ImportProbe
    }
    return $resolvedPython
}

function Invoke-PortablePythonSourceProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ImportProbe,
        [string]$RuntimeRoot = ""
    )
    $resolvedRoot = Assert-PortablePackageRoot -Root $Root
    $resolvedRuntimeRoot = if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
        $resolvedRoot
    } else {
        Assert-PortablePackageRoot -Root $RuntimeRoot
    }
    $resolvedPython = [IO.Path]::GetFullPath($PythonPath)
    if (!(Test-PortablePathWithinRoot -Root $resolvedRuntimeRoot -Path $resolvedPython)) {
        throw "source probe Python must be inside its runtime root"
    }
    $pythonRelative = $resolvedPython.Substring($resolvedRuntimeRoot.Length).TrimStart('\', '/')
    [void](Resolve-PortablePackagePath -Root $resolvedRuntimeRoot -RelativePath $pythonRelative -Label "source probe Python" -MustExist)
    if (!(Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { throw "source probe Python is missing" }

    $resolvedSourceRoot = [IO.Path]::GetFullPath($SourceRoot)
    if (!(Test-PortablePathWithinRoot -Root $resolvedRoot -Path $resolvedSourceRoot)) {
        throw "runtime source root must be inside the package"
    }
    $sourceRelative = if ([string]::Equals($resolvedSourceRoot, $resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        "."
    } else {
        $resolvedSourceRoot.Substring($resolvedRoot.Length).TrimStart('\', '/')
    }
    $resolvedSourceRoot = Resolve-PortablePackagePath -Root $resolvedRoot -RelativePath $sourceRelative -Label "runtime source root" -MustExist
    if (!(Test-Path -LiteralPath $resolvedSourceRoot -PathType Container)) { throw "runtime source root is missing" }
    if ([string]::IsNullOrWhiteSpace($ImportProbe)) { return }

    $bootstrap = "import os,sys; source_root=os.path.abspath(sys.argv[1]); os.chdir(source_root); sys.path.insert(0,source_root); exec(compile(sys.argv[2],'<portable-import-probe>','exec'),{'__name__':'__main__'})"
    $previousErrorActionPreference = $ErrorActionPreference
    $probeOutput = @()
    $probeExitCode = -1
    try {
        $ErrorActionPreference = "Continue"
        $probeOutput = @(& $resolvedPython -B -c $bootstrap $resolvedSourceRoot $ImportProbe 2>&1)
        $probeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($probeExitCode -ne 0) {
        $probeLines = @($probeOutput | ForEach-Object { [string]$_ })
        $selectedLines = if ($probeLines.Count -le 8) {
            @($probeLines)
        } else {
            @($probeLines | Select-Object -First 3) +
                @("... [$($probeLines.Count - 7) diagnostic lines omitted] ...") +
                @($probeLines | Select-Object -Last 4)
        }
        $boundedLines = @($selectedLines | ForEach-Object {
            $line = [string]$_
            if ($line.Length -le 448) { $line }
            else { $line.Substring(0, 224) + "... [line truncated] ..." + $line.Substring($line.Length - 224) }
        })
        $detail = ($boundedLines -join [Environment]::NewLine).Trim()
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = "no diagnostic output" }
        throw "package runtime import probe failed: $detail"
    }
}

function Test-PortableRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [string]$ImportProbe = "import sys"
    )
    try {
        [void](Assert-PortableRuntime -Root $Root -SourceRoot $SourceRoot -PythonPath $PythonPath -ExpectedVersion $ExpectedVersion -ImportProbe $ImportProbe)
        return $true
    } catch { return $false }
}

function Resolve-PortableSupportedProfile {
    param([Parameter(Mandatory = $true)][object]$RuntimeLockPayload, [string]$RequestedProfile = "")
    if (!$RuntimeLockPayload.PSObject.Properties["profiles"]) { throw "runtime lock profiles are missing" }
    $profiles = $RuntimeLockPayload.profiles
    Assert-PortableJsonObject -Value $profiles -Label "runtime lock profiles"
    if (![string]::IsNullOrWhiteSpace($RequestedProfile) -and $profiles.PSObject.Properties[$RequestedProfile]) { return $RequestedProfile }
    if ($profiles.PSObject.Properties["cpu"]) { return "cpu" }
    if ($RuntimeLockPayload.PSObject.Properties["auto_order"] -and $RuntimeLockPayload.auto_order.GetType().FullName -eq "System.Object[]") {
        foreach ($candidate in @($RuntimeLockPayload.auto_order)) {
            if ($candidate -is [string] -and $profiles.PSObject.Properties[[string]$candidate]) { return [string]$candidate }
        }
    }
    throw "runtime lock has no deterministic supported fallback profile"
}

function Test-PortableRequestedProfileMatchesState {
    param(
        [Parameter(Mandatory = $true)][object]$RuntimeLockPayload,
        [Parameter(Mandatory = $true)][string]$RequestedProfile,
        [Parameter(Mandatory = $true)][string]$StatePath
    )
    $requested = $RequestedProfile.Trim().ToLowerInvariant()
    if ($requested -eq "auto") { return $true }
    if (!$RuntimeLockPayload.PSObject.Properties["profiles"]) { throw "runtime lock profiles are missing" }
    Assert-PortableJsonObject -Value $RuntimeLockPayload.profiles -Label "runtime lock profiles"
    if (!$RuntimeLockPayload.profiles.PSObject.Properties[$requested]) {
        throw "requested device profile is not supported by this package: $requested"
    }
    if (!(Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $false }
    try { $existingState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json }
    catch { return $false }
    return [string]::Equals([string]$existingState.profile, $requested, [StringComparison]::OrdinalIgnoreCase)
}

function Test-PortableLockedAssets {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ModelLock,
        [bool]$VerifyHashes = $true
    )
    try {
        if (!(Test-Path -LiteralPath $ModelLock -PathType Leaf)) { return $false }
        $lock = Get-Content -LiteralPath $ModelLock -Raw | ConvertFrom-Json
        $requiredPaths = if ($lock.PSObject.Properties["required_paths"]) { @($lock.required_paths) } else { @() }
        foreach ($relative in $requiredPaths) {
            if ([string]::IsNullOrWhiteSpace([string]$relative)) { continue }
            $required = Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$relative) -Label "model required path" -MustExist
            if (!(Test-Path -LiteralPath $required)) { return $false }
        }
        $assets = if ($lock.PSObject.Properties["assets"]) { @($lock.assets) } else { @() }
        foreach ($asset in $assets) {
            if ($null -eq $asset -or [string]::IsNullOrWhiteSpace([string]$asset.target)) { continue }
            $target = Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$asset.target) -Label "model asset" -MustExist
            if (!(Test-Path -LiteralPath $target -PathType Leaf)) { return $false }
            if ($null -ne $asset.PSObject.Properties["size_bytes"] -and [int64]$asset.size_bytes -ne (Get-Item -LiteralPath $target).Length) { return $false }
            if ($VerifyHashes -and $null -ne $asset.PSObject.Properties["sha256"] -and ![string]::IsNullOrWhiteSpace([string]$asset.sha256)) {
                if (![string]::Equals((Get-PortableFileSha256 -Path $target), [string]$asset.sha256, [StringComparison]::OrdinalIgnoreCase)) { return $false }
            }
        }
        return $true
    } catch { return $false }
}

function Assert-PortableSha256Manifest {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [string[]]$RequiredCoverage = @()
    )
    if (!(Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw "SHA256SUMS manifest is missing" }
    $covered = @{}
    foreach ($line in [IO.File]::ReadAllLines($ManifestPath)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})\s{2}(.+)$') { throw "SHA256SUMS contains an invalid record" }
        $relative = $Matches[2].Replace('/', '\')
        $path = Resolve-PortablePackagePath -Root $Root -RelativePath $relative -Label "SHA256SUMS entry" -MustExist
        $actual = Get-PortableFileSha256 -Path $path
        if (![string]::Equals($actual, $Matches[1], [StringComparison]::OrdinalIgnoreCase)) { throw "SHA256SUMS hash mismatch: $relative" }
        $covered[[IO.Path]::GetFullPath($path).ToLowerInvariant()] = $true
    }
    foreach ($path in $RequiredCoverage) {
        if ([string]::IsNullOrWhiteSpace($path)) { continue }
        $key = [IO.Path]::GetFullPath($path).ToLowerInvariant()
        if (!$covered.ContainsKey($key)) { throw "SHA256SUMS does not cover required package file: $path" }
    }
}

function Get-PortableIntegrityCoverage {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RuntimeLock,
        [Parameter(Mandatory = $true)][string]$ModelLock,
        [string[]]$RequiredCoverage = @()
    )
    $coverage = [Collections.Generic.List[string]]::new()
    foreach ($path in $RequiredCoverage) { if (![string]::IsNullOrWhiteSpace($path)) { $coverage.Add([IO.Path]::GetFullPath($path)) } }
    $python = Resolve-PortablePackagePath -Root $Root -RelativePath "runtime\live\python.exe" -Label "package runtime" -MustExist
    $coverage.Add($python)
    $runtimeRoot = Join-Path $Root "runtime\live"
    foreach ($file in @(Get-ChildItem -LiteralPath $runtimeRoot -File -Recurse -Force)) {
        $relative = $file.FullName.Substring([IO.Path]::GetFullPath($Root).TrimEnd('\', '/').Length).TrimStart('\', '/')
        [void](Resolve-PortablePackagePath -Root $Root -RelativePath $relative -Label "runtime file" -MustExist)
        $coverage.Add($file.FullName)
    }
    foreach ($lockPath in @($RuntimeLock, $ModelLock)) {
        $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
        foreach ($collectionName in @("assets", "payloads")) {
            if (!$lock.PSObject.Properties[$collectionName]) { continue }
            foreach ($asset in @($lock.$collectionName)) {
                if ($asset.PSObject.Properties["target"] -and ![string]::IsNullOrWhiteSpace([string]$asset.target)) {
                    $target = Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$asset.target) -Label "locked asset" -MustExist
                    if (Test-Path -LiteralPath $target -PathType Leaf) { $coverage.Add($target) }
                }
            }
        }
        if ($lock.PSObject.Properties["required_paths"]) {
            foreach ($relative in @($lock.required_paths)) {
                $target = Resolve-PortablePackagePath -Root $Root -RelativePath ([string]$relative) -Label "locked required path" -MustExist
                if (Test-Path -LiteralPath $target -PathType Leaf) { $coverage.Add($target) }
            }
        }
    }
    return @($coverage | Select-Object -Unique)
}

function Test-PortableInstallStateComplete {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$BuildId,
        [Parameter(Mandatory = $true)][string]$RuntimeLock,
        [Parameter(Mandatory = $true)][string]$ModelLock,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [string]$ImportProbe = "import sys",
        [switch]$ValidateAssets,
        [switch]$CheckLockedAssets,
        [string]$Sha256Manifest = "",
        [string[]]$RequiredCoverage = @()
    )
    try {
        if (!(Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $false }
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        if ([int]$state.schema_version -ne 1 -or !$state.ready) { return $false }
        if ([string]$state.component -ne $Component -or [string]$state.build_id -ne $BuildId) { return $false }
        if ([string]::IsNullOrWhiteSpace([string]$state.profile)) { return $false }
        if (Test-Path -LiteralPath $RuntimeLock -PathType Leaf) {
            $runtimePayload = Get-Content -LiteralPath $RuntimeLock -Raw | ConvertFrom-Json
            if ($ValidateAssets -and (!$runtimePayload.PSObject.Properties["component"] -or [string]$runtimePayload.component -ne $Component)) { return $false }
            if ($runtimePayload.PSObject.Properties["python_version"] -and [string]$runtimePayload.python_version -ne $ExpectedPython) { return $false }
            if ($runtimePayload.PSObject.Properties["profiles"] -and !$runtimePayload.profiles.PSObject.Properties[[string]$state.profile]) { return $false }
            $runtimeHash = Get-PortableFileSha256 -Path $RuntimeLock
            if ([string]$state.runtime_lock_sha256 -ne $runtimeHash) { return $false }
        }
        if (Test-Path -LiteralPath $ModelLock -PathType Leaf) {
            $modelPayload = Get-Content -LiteralPath $ModelLock -Raw | ConvertFrom-Json
            if ($ValidateAssets -and (!$modelPayload.PSObject.Properties["component"] -or [string]$modelPayload.component -ne $Component)) { return $false }
            $modelHash = Get-PortableFileSha256 -Path $ModelLock
            if ([string]$state.model_lock_sha256 -ne $modelHash) { return $false }
        }
        $python = Join-Path $Root "runtime\live\python.exe"
        if ($ValidateAssets -or $CheckLockedAssets) {
            if ($ValidateAssets -and $Sha256Manifest) {
                $integrityCoverage = Get-PortableIntegrityCoverage -Root $Root -RuntimeLock $RuntimeLock -ModelLock $ModelLock -RequiredCoverage $RequiredCoverage
                Assert-PortableSha256Manifest -Root $Root -ManifestPath $Sha256Manifest -RequiredCoverage $integrityCoverage
            }
            if (!(Test-PortableLockedAssets -Root $Root -ModelLock $ModelLock -VerifyHashes:([bool]$ValidateAssets))) { return $false }
        }
        if (!(Test-PortableRuntime -Root $Root -SourceRoot $SourceRoot -PythonPath $python -ExpectedVersion $ExpectedPython -ImportProbe $ImportProbe)) { return $false }
        return $true
    } catch { return $false }
}
