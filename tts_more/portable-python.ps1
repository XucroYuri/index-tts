Set-StrictMode -Version Latest

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.Net.Http

function Get-PortablePythonFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Test-PortablePythonCancelled {
    param([string]$CancelFile)
    return $CancelFile -and [System.IO.File]::Exists($CancelFile)
}

function New-PortableOwnedSiblingPath {
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][ValidateSet('px', 'pi', 'pu', 'pb')][string]$Prefix
    )

    $destinationFull = [System.IO.Path]::GetFullPath($Destination)
    $parent = [System.IO.Path]::GetDirectoryName($destinationFull)
    if ([string]::IsNullOrWhiteSpace($parent)) { throw "portable owned path requires a parent directory" }
    $nonceBytes = New-Object byte[] 16
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($nonceBytes) }
    finally { $generator.Dispose() }
    $nonce = ([System.BitConverter]::ToString($nonceBytes)).Replace('-', '').ToLowerInvariant()
    return Join-Path $parent ".$Prefix-$nonce"
}

function Set-PortableDownloadHeaders {
    param(
        [Parameter(Mandatory = $true)][System.Net.Http.HttpRequestMessage]$Request,
        [Parameter(Mandatory = $true)][string]$Url
    )

    $uri = [System.Uri]$Url
    if ($uri.Host -ieq "api.github.com" -and $uri.AbsolutePath -match '^/repos/[^/]+/[^/]+/releases/assets/\d+$') {
        $Request.Headers.Accept.ParseAdd("application/octet-stream")
        $Request.Headers.UserAgent.ParseAdd("tts-more-portable-installer")
    }
}

function Test-PortableLockedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Asset
    )

    if (![System.IO.File]::Exists($Path)) { return $false }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne [int64]$Asset.size_bytes) { return $false }
    return (Get-PortablePythonFileSha256 -Path $Path) -eq ([string]$Asset.sha256).ToLowerInvariant()
}

function Get-PortableLockedAsset {
    param(
        [Parameter(Mandatory = $true)]$Asset,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string]$CancelFile
    )

    if (!$Asset.id -or !$Asset.sha256 -or [int64]$Asset.size_bytes -le 0 -or @($Asset.urls).Count -eq 0) {
        throw "portable asset lock is incomplete"
    }
    $Destination = [System.IO.Path]::GetFullPath($Destination)
    $parent = [System.IO.Path]::GetDirectoryName($Destination)
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    if (Test-PortableLockedFile -Path $Destination -Asset $Asset) { return $Destination }
    if ([System.IO.File]::Exists($Destination)) {
        throw "locked asset destination already exists but is invalid: $Destination"
    }

    $partial = "$Destination.partial"
    if ([System.IO.File]::Exists($partial)) {
        $partialLength = (Get-Item -LiteralPath $partial).Length
        if ($partialLength -eq [int64]$Asset.size_bytes) {
            if (Test-PortableLockedFile -Path $partial -Asset $Asset) {
                [System.IO.File]::Move($partial, $Destination)
                return $Destination
            }
            Remove-Item -LiteralPath $partial -Force
        }
        elseif ($partialLength -gt [int64]$Asset.size_bytes) {
            Remove-Item -LiteralPath $partial -Force
        }
    }
    $errors = New-Object System.Collections.Generic.List[string]
    foreach ($url in @($Asset.urls)) {
        if (Test-PortablePythonCancelled -CancelFile $CancelFile) { throw [System.OperationCanceledException]::new("portable runtime installation cancelled") }
        $baselineExisted = [System.IO.File]::Exists($partial)
        $resumeFrom = if ($baselineExisted) { (Get-Item -LiteralPath $partial).Length } else { 0 }
        $baselineLength = $resumeFrom
        $client = New-Object System.Net.Http.HttpClient
        $response = $null
        $request = $null
        $attempt = $null
        try {
            $request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, [string]$url)
            Set-PortableDownloadHeaders -Request $request -Url ([string]$url)
            if ($resumeFrom -gt 0) {
                $request.Headers.Range = "bytes=$resumeFrom-"
            }
            $response = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
            if (!$response.IsSuccessStatusCode) {
                throw "HTTP $([int]$response.StatusCode)"
            }
            $statusCode = [int]$response.StatusCode
            if ($statusCode -eq 206) {
                $contentRange = [string]$response.Content.Headers.ContentRange
                if ($contentRange -notmatch '^bytes (\d+)-(\d+)/(\d+)$' -or
                    [int64]$Matches[1] -ne $resumeFrom -or
                    [int64]$Matches[2] -lt $resumeFrom -or
                    [int64]$Matches[2] -ge [int64]$Asset.size_bytes -or
                    [int64]$Matches[3] -ne [int64]$Asset.size_bytes) {
                    throw "invalid Content-Range for locked asset: $contentRange"
                }
            }
            $append = $resumeFrom -gt 0 -and $statusCode -eq 206
            if ($resumeFrom -gt 0 -and !$append) {
                $attempt = "$partial.restart-$([guid]::NewGuid().ToString('N'))"
            }
            $writePath = if ($attempt) { $attempt } else { $partial }
            $mode = if ($append) { [System.IO.FileMode]::Append } else { [System.IO.FileMode]::Create }
            $input = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $output = New-Object System.IO.FileStream($writePath, $mode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try {
                $buffer = New-Object byte[] 1048576
                while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    if (Test-PortablePythonCancelled -CancelFile $CancelFile) { throw [System.OperationCanceledException]::new("portable runtime installation cancelled") }
                    $output.Write($buffer, 0, $read)
                }
                $output.Flush($true)
            }
            finally {
                $output.Dispose()
                $input.Dispose()
            }
            if (!(Test-PortableLockedFile -Path $writePath -Asset $Asset)) {
                throw "downloaded asset failed size or SHA-256 validation"
            }
            [System.IO.File]::Move($writePath, $Destination)
            if ($attempt -and [System.IO.File]::Exists($partial)) { Remove-Item -LiteralPath $partial -Force }
            return $Destination
        }
        catch {
            $errors.Add("$url`: $($_.Exception.Message)")
            if ($attempt -and [System.IO.File]::Exists($attempt)) { Remove-Item -LiteralPath $attempt -Force }
            if ([System.IO.File]::Exists($partial)) {
                if ($baselineExisted) {
                    $rollback = New-Object System.IO.FileStream($partial, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                    try { $rollback.SetLength($baselineLength) } finally { $rollback.Dispose() }
                }
                else {
                    Remove-Item -LiteralPath $partial -Force
                }
            }
            if ($_.Exception -is [System.OperationCanceledException]) { throw }
        }
        finally {
            if ($response) { $response.Dispose() }
            if ($request) { $request.Dispose() }
            $client.Dispose()
        }
    }
    throw "asset download failed for $($Asset.id): $($errors -join '; ')"
}

function Test-PortableExtractionReparseTraversal {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Target)

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $current = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($Target))
    while ($current -and $current.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        if ([System.IO.Directory]::Exists($current)) {
            $attributes = [System.IO.File]::GetAttributes($current)
            if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "ZIP extraction would traverse a reparse point: $current"
            }
        }
        if ($current.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) { break }
        $current = [System.IO.Path]::GetDirectoryName($current)
    }
}

function Expand-PortablePythonArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    if ($ExpectedVersion -notmatch '^(\d+)\.(\d+)\.(\d+)$') { throw "exact Python patch version is required" }
    $pythonTag = "$($Matches[1])$($Matches[2])"
    $expectedPth = "python$pythonTag._pth"
    $expectedZip = "python$pythonTag.zip"
    $Destination = [System.IO.Path]::GetFullPath($Destination)
    if ([System.IO.Directory]::Exists($Destination) -or [System.IO.File]::Exists($Destination)) {
        throw "portable Python destination already exists: $Destination"
    }
    $parent = [System.IO.Path]::GetDirectoryName($Destination)
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary = New-PortableOwnedSiblingPath -Destination $Destination -Prefix 'px'
    [System.IO.Directory]::CreateDirectory($temporary) | Out-Null
    $zip = $null
    try {
        $zip = [System.IO.Compression.ZipFile]::OpenRead([System.IO.Path]::GetFullPath($Archive))
        $targets = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        $pthEntries = New-Object System.Collections.Generic.List[string]
        $pythonEntries = 0
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('/', '\')
            if ([string]::IsNullOrWhiteSpace($name) -or $name.StartsWith('\') -or $name -match '^[A-Za-z]:') {
                throw "ZIP contains an absolute or empty path: $($entry.FullName)"
            }
            $parts = @($name.Split('\') | Where-Object { $_ -ne '' })
            if ($parts.Count -eq 0 -or $parts -contains '..' -or $parts -contains '.') {
                throw "ZIP contains path traversal: $($entry.FullName)"
            }
            if (@($parts | Where-Object { $_.Contains(':') }).Count -gt 0) {
                throw "ZIP path segment contains a colon: $($entry.FullName)"
            }
            $normalized = $parts -join '\'
            if (!$targets.Add($normalized)) { throw "ZIP contains a duplicate normalized target: $normalized" }
            if (($entry.ExternalAttributes -band 0x400) -ne 0) { throw "ZIP contains a reparse-point entry: $normalized" }
            if ([System.IO.Path]::GetFileName($normalized).Equals('pyvenv.cfg', [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "ZIP must not contain pyvenv.cfg"
            }
            if ($normalized.EndsWith('._pth', [System.StringComparison]::OrdinalIgnoreCase)) { $pthEntries.Add($normalized) }
            if ($normalized.Equals('python.exe', [System.StringComparison]::OrdinalIgnoreCase)) { $pythonEntries++ }
        }
        if ($pythonEntries -ne 1) { throw "ZIP must contain exactly one python.exe" }
        if ($pthEntries.Count -ne 1 -or !$pthEntries[0].Equals($expectedPth, [System.StringComparison]::Ordinal)) {
            throw "ZIP must contain exactly one expected _pth file: $expectedPth"
        }
        if (!$targets.Contains($expectedZip)) { throw "ZIP is missing the expected standard-library archive: $expectedZip" }

        foreach ($entry in $zip.Entries) {
            $parts = @($entry.FullName.Replace('/', '\').Split('\') | Where-Object { $_ -ne '' })
            $target = Join-Path $temporary ($parts -join '\')
            $targetFull = [System.IO.Path]::GetFullPath($target)
            $prefix = $temporary.TrimEnd('\') + '\'
            if (!$targetFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "ZIP target escaped extraction root" }
            Test-PortableExtractionReparseTraversal -Root $temporary -Target $targetFull
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) {
                [System.IO.Directory]::CreateDirectory($targetFull) | Out-Null
                continue
            }
            [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($targetFull)) | Out-Null
            $input = $entry.Open()
            $output = New-Object System.IO.FileStream($targetFull, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose() }
        }
        $pthPath = Join-Path $temporary $expectedPth
        [System.IO.File]::WriteAllLines(
            $pthPath,
            @($expectedZip, '.', 'Lib\site-packages', 'import site'),
            [System.Text.Encoding]::ASCII
        )
        [System.IO.Directory]::CreateDirectory((Join-Path $temporary 'Lib\site-packages')) | Out-Null
        [System.IO.Directory]::Move($temporary, $Destination)
    }
    catch {
        if ([System.IO.Directory]::Exists($temporary)) { Remove-Item -LiteralPath $temporary -Recurse -Force }
        throw
    }
    finally {
        if ($zip) { $zip.Dispose() }
    }
}

function Export-PortableUvExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Wheel,
        [Parameter(Mandatory = $true)][string]$ArchiveEntry,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $Destination = [System.IO.Path]::GetFullPath($Destination)
    if ([string]::IsNullOrWhiteSpace($ArchiveEntry)) { throw "uv archive_entry is required" }
    if ([System.IO.Directory]::Exists($Destination)) { throw "uv destination is a directory: $Destination" }
    $zip = [System.IO.Compression.ZipFile]::OpenRead([System.IO.Path]::GetFullPath($Wheel))
    try {
        $matches = @($zip.Entries | Where-Object { $_.FullName -ceq $ArchiveEntry })
        if ($matches.Count -ne 1) { throw "uv wheel must contain exactly one declared entry: $ArchiveEntry" }
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Destination)) | Out-Null
        $temporary = New-PortableOwnedSiblingPath -Destination $Destination -Prefix 'pu'
        try {
            $input = $matches[0].Open()
            $output = New-Object System.IO.FileStream($temporary, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try { $input.CopyTo($output); $output.Flush($true) } finally { $output.Dispose(); $input.Dispose() }
            if ([System.IO.File]::Exists($Destination)) {
                $sameLength = (Get-Item -LiteralPath $temporary).Length -eq (Get-Item -LiteralPath $Destination).Length
                $sameSha = $sameLength -and (Get-PortablePythonFileSha256 -Path $temporary) -eq (Get-PortablePythonFileSha256 -Path $Destination)
                if ($sameSha) {
                    Remove-Item -LiteralPath $temporary -Force
                }
                else {
                    $backup = New-PortableOwnedSiblingPath -Destination $Destination -Prefix 'pb'
                    try {
                        [System.IO.File]::Replace($temporary, $Destination, $backup, $true)
                        Remove-Item -LiteralPath $backup -Force
                    }
                    finally {
                        if ([System.IO.File]::Exists($backup)) { Remove-Item -LiteralPath $backup -Force }
                    }
                }
            }
            else {
                [System.IO.File]::Move($temporary, $Destination)
            }
        }
        catch {
            if ([System.IO.File]::Exists($temporary)) { Remove-Item -LiteralPath $temporary -Force }
            throw
        }
    }
    finally { $zip.Dispose() }
}

function Write-PortableAssetLock {
    param([Parameter(Mandatory = $true)]$Asset, [Parameter(Mandatory = $true)][string]$Path)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Asset | ConvertTo-Json -Depth 10), $encoding)
}

function Install-PortablePythonRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeLock,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string]$OperationRoot,
        [string]$CancelFile
    )

    foreach ($path in @($PackageRoot, $RuntimeLock, $Destination)) {
        if (![System.IO.Path]::IsPathRooted($path)) { throw "portable runtime paths must be absolute: $path" }
    }
    $PackageRoot = [System.IO.Path]::GetFullPath($PackageRoot)
    $RuntimeLock = [System.IO.Path]::GetFullPath($RuntimeLock)
    $Destination = [System.IO.Path]::GetFullPath($Destination)
    if ([System.IO.Directory]::Exists($Destination) -or [System.IO.File]::Exists($Destination)) {
        throw "portable Python destination already exists: $Destination"
    }
    $lock = Get-Content -LiteralPath $RuntimeLock -Raw -Encoding UTF8 | ConvertFrom-Json
    if (!$lock.assets.python -or !$lock.assets.uv -or ([string]$lock.python_version -notmatch '^\d+\.\d+\.\d+$')) {
        throw "runtime lock does not contain exact Python and uv assets"
    }
    $cache = Join-Path $PackageRoot 'data\cache\portable'
    $assets = Join-Path $cache 'assets'
    $locks = Join-Path $cache 'locks'
    [System.IO.Directory]::CreateDirectory($assets) | Out-Null
    [System.IO.Directory]::CreateDirectory($locks) | Out-Null
    $pythonArchive = Join-Path $assets ("$($lock.assets.python.id).zip")
    Get-PortableLockedAsset -Asset $lock.assets.python -Destination $pythonArchive -CancelFile $CancelFile | Out-Null

    $destinationParent = [System.IO.Path]::GetDirectoryName($Destination)
    [System.IO.Directory]::CreateDirectory($destinationParent) | Out-Null
    $candidate = New-PortableOwnedSiblingPath -Destination $Destination -Prefix 'pi'
    try {
        Expand-PortablePythonArchive -Archive $pythonArchive -Destination $candidate -ExpectedVersion ([string]$lock.python_version)
        $candidatePython = Join-Path $candidate ([string]$lock.assets.python.archive_entry)
        if (![System.IO.File]::Exists($candidatePython)) { throw "Python archive entry is missing after extraction" }
        $actualVersion = (& $candidatePython -c "import platform; print(platform.python_version())" 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $actualVersion -ne [string]$lock.python_version) {
            throw "embedded Python version mismatch: expected $($lock.python_version), got $actualVersion"
        }

        $uvLockPath = Join-Path $locks 'uv-asset.json'
        Write-PortableAssetLock -Asset $lock.assets.uv -Path $uvLockPath
        $uvWheel = Join-Path $assets ("$($lock.assets.uv.id).whl")
        $portableInstall = Join-Path $PSScriptRoot 'portable_install.py'
        if (![System.IO.File]::Exists($portableInstall)) { throw "portable_install.py is missing beside portable-python.ps1" }
        $arguments = @($portableInstall, 'ensure-asset', '--asset', $uvLockPath, '--path', $uvWheel, '--package-root', $PackageRoot)
        if ($OperationRoot) { $arguments += @('--operation-root', $OperationRoot) }
        if ($CancelFile) { $arguments += @('--cancel-file', $CancelFile) }
        $portableInstallBootstrap = "import os,runpy,sys; script=sys.argv[1]; sys.argv=sys.argv[1:]; sys.path.insert(0,os.path.dirname(script)); runpy.run_path(script,run_name='__main__')"
        & $candidatePython -c $portableInstallBootstrap @arguments 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 20) { throw [System.OperationCanceledException]::new("portable runtime installation cancelled") }
        if ($LASTEXITCODE -ne 0) { throw "portable_install.py ensure-asset failed for uv" }

        if ([string]::IsNullOrWhiteSpace([string]$lock.assets.uv.archive_entry)) { throw "uv archive_entry is required" }
        $uvEntry = [string]$lock.assets.uv.archive_entry
        if ([string]$lock.assets.uv.id -notmatch '^uv-(\d+\.\d+\.\d+)-') { throw "uv asset id does not contain an exact version" }
        $uvPath = Join-Path $cache ("tools\uv-$($Matches[1])\uv.exe")
        Export-PortableUvExecutable -Wheel $uvWheel -ArchiveEntry $uvEntry -Destination $uvPath
        [System.IO.Directory]::Move($candidate, $Destination)
        return [pscustomobject]@{
            Python = [System.IO.Path]::GetFullPath((Join-Path $Destination ([string]$lock.assets.python.archive_entry)))
            Uv = [System.IO.Path]::GetFullPath($uvPath)
            SitePackages = [System.IO.Path]::GetFullPath((Join-Path $Destination 'Lib\site-packages'))
        }
    }
    catch {
        if ([System.IO.Directory]::Exists($candidate)) { Remove-Item -LiteralPath $candidate -Recurse -Force }
        throw
    }
}
