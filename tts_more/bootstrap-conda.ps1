[CmdletBinding()]
param(
    [string]$CacheRoot = "data/cache/portable/conda",
    [string]$LockPath = "packaging/portable/toolchain.lock.json",
    [string]$PackageRoot = "",
    [string]$OperationRoot = "",
    [string]$CancelFile = "",
    [switch]$DryRun,
    [switch]$PassThru
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

function Resolve-PortableFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-WindowsSafeDirectName {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Label,
        [string]$RequiredExtension = ""
    )
    $deviceBase = [regex]::Match($Name, "^[^.]*").Value
    if (
        [string]::IsNullOrWhiteSpace($Name) -or
        [IO.Path]::IsPathRooted($Name) -or
        $Name -in @(".", "..") -or
        $Name -ne [IO.Path]::GetFileName($Name) -or
        $Name -match '[<>:"/\\|?*\x00-\x1F]' -or
        $Name.EndsWith(" ") -or
        $Name.EndsWith(".") -or
        $deviceBase -match '^(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$' -or
        (![string]::IsNullOrEmpty($RequiredExtension) -and !$Name.EndsWith($RequiredExtension, [StringComparison]::OrdinalIgnoreCase))
    ) {
        throw "$Label must be a Windows-safe direct file name"
    }
}

function ConvertTo-LockedPositiveSize {
    param([Parameter(Mandatory = $true)]$Value, [Parameter(Mandatory = $true)][string]$Label)
    $size = [int64]0
    if (
        ![int64]::TryParse(
            [string]$Value,
            [Globalization.NumberStyles]::None,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$size
        ) -or $size -le 0
    ) {
        throw "$Label must be a positive integer"
    }
    return $size
}

function Assert-LockedAssetUrl {
    param([Parameter(Mandatory = $true)][string]$Url, [Parameter(Mandatory = $true)][string]$Label)
    $uri = $null
    if (
        [string]::IsNullOrWhiteSpace($Url) -or
        $Url -ne $Url.Trim() -or
        ![Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri) -or
        !(($uri.Scheme -eq [Uri]::UriSchemeHttps) -or ($uri.Scheme -eq [Uri]::UriSchemeHttp -and $uri.IsLoopback)) -or
        ![string]::IsNullOrEmpty($uri.UserInfo)
    ) {
        throw "$Label must be an absolute HTTPS URL (HTTP is allowed only for loopback fixtures)"
    }
}

function Resolve-OperationContract {
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [string]$OperationRoot = "",
        [string]$CancelFile = ""
    )

    $hasOperation = ![string]::IsNullOrWhiteSpace($OperationRoot)
    $hasCancel = ![string]::IsNullOrWhiteSpace($CancelFile)
    if ($hasOperation -ne $hasCancel) {
        throw "OperationRoot and CancelFile must be provided together"
    }
    $resolvedPackage = Resolve-PortableFullPath $PackageRoot
    if (!$hasOperation) {
        return [pscustomobject]@{ PackageRoot = $resolvedPackage; OperationRoot = ""; CancelFile = "" }
    }

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
    $operations = Resolve-PortableFullPath (Join-Path $resolvedPackage $operationsRelative)
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
    $resolvedOperation = Resolve-PortableFullPath $OperationRoot
    $operationParent = Resolve-PortableFullPath (Split-Path -Parent $resolvedOperation)
    if (![string]::Equals($operationParent, $operations, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OperationRoot must be a UUID-named direct child of the package operations root"
    }
    $operationId = Split-Path -Leaf $resolvedOperation
    $parsedId = [guid]::Empty
    if (![guid]::TryParse($operationId, [ref]$parsedId)) {
        throw "OperationRoot name must be a valid UUID"
    }
    $resolvedCancel = Resolve-PortableFullPath $CancelFile
    $expectedCancel = Resolve-PortableFullPath (Join-Path $resolvedOperation "cancel.requested")
    if (![string]::Equals($resolvedCancel, $expectedCancel, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "CancelFile must resolve exactly to OperationRoot/cancel.requested"
    }
    return [pscustomobject]@{
        PackageRoot = $resolvedPackage
        OperationRoot = $resolvedOperation
        CancelFile = $resolvedCancel
    }
}

if ([string]::IsNullOrWhiteSpace($PackageRoot)) { $PackageRoot = Split-Path -Parent $PSScriptRoot }
$contract = Resolve-OperationContract -PackageRoot $PackageRoot -OperationRoot $OperationRoot -CancelFile $CancelFile
$script:RepoRoot = $contract.PackageRoot
$OperationRoot = $contract.OperationRoot
$CancelFile = $contract.CancelFile
if (!(Test-Path -LiteralPath $script:RepoRoot -PathType Container)) {
    throw "PackageRoot is missing: $script:RepoRoot"
}

function Assert-PortableNotCancelled {
    if (![string]::IsNullOrWhiteSpace($CancelFile) -and (Test-Path -LiteralPath $CancelFile -PathType Leaf)) {
        throw [System.OperationCanceledException]::new("Portable initialization cancelled")
    }
}

function Resolve-RepoPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $script:RepoRoot $Path))
}

function Assert-NoReparsePathSegments {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd("\", "/")
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $boundary = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    if (!$resolvedPath.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain below PackageRoot"
    }
    $relative = $resolvedPath.Substring($boundary.Length)
    $current = $resolvedRoot
    foreach ($segment in @($relative -split '[\\/]' | Where-Object { $_ })) {
        $current = Join-Path $current $segment
        if (!(Test-Path -LiteralPath $current)) { continue }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label must not traverse a reparse point: $current"
        }
    }
}

function Resolve-PackageChildPath {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $resolved = Resolve-RepoPath $Path
    Assert-NoReparsePathSegments -Root $script:RepoRoot -Path $resolved -Label $Label
    return $resolved
}

function Assert-DirectChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $resolvedParent = [IO.Path]::GetFullPath($Parent).TrimEnd("\", "/")
    $resolvedPathParent = [IO.Path]::GetFullPath((Split-Path -Parent ([IO.Path]::GetFullPath($Path)))).TrimEnd("\", "/")
    if (![string]::Equals($resolvedPathParent, $resolvedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain a direct child of its package-private cache"
    }
}

function Get-LockedMiniforge {
    param([Parameter(Mandatory = $true)][string]$ResolvedLockPath)

    if (!(Test-Path -LiteralPath $ResolvedLockPath -PathType Leaf)) {
        throw "Portable toolchain lock is missing: $ResolvedLockPath"
    }
    $lock = Get-Content -LiteralPath $ResolvedLockPath -Raw | ConvertFrom-Json
    if ([int]$lock.schema_version -ne 1) {
        throw "Portable toolchain lock schema_version must be 1"
    }
    $toolchain = $lock.miniforge
    $version = [string]$toolchain.version
    Assert-WindowsSafeDirectName -Name $version -Label "Portable toolchain lock miniforge.version"
    $archive = [string]$toolchain.archive
    Assert-WindowsSafeDirectName -Name $archive -Label "Portable toolchain lock miniforge.archive" -RequiredExtension ".exe"
    $url = [string]$toolchain.url
    Assert-LockedAssetUrl -Url $url -Label "Portable toolchain lock miniforge.url"
    $sha256 = [string]$toolchain.sha256
    if ($sha256 -notmatch "^[0-9a-fA-F]{64}$") {
        throw "Portable toolchain lock miniforge.sha256 must be exactly 64 hexadecimal characters"
    }
    $size = ConvertTo-LockedPositiveSize -Value $toolchain.size_bytes -Label "Portable toolchain lock miniforge.size_bytes"
    return [pscustomobject]@{
        version = $version
        archive = $archive
        url = $url
        sha256 = $sha256.ToLowerInvariant()
        size_bytes = $size
    }
}

function Test-LockedSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    $actual = Get-PortableFileSha256 -Path $Path
    return $actual -eq $ExpectedSha256.ToLowerInvariant()
}

function Test-LockedArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][int64]$ExpectedSize
    )
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $Path -Force).Length -ne $ExpectedSize) { return $false }
    return Test-LockedSha256 -Path $Path -ExpectedSha256 $ExpectedSha256
}

function Open-PortableHttpResponse {
    param(
        [Parameter(Mandatory = $true)][System.Net.Http.HttpClient]$Client,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][int64]$ResumeFrom
    )

    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Url)
    try {
        $headers = @{}
        if ($ResumeFrom -gt 0) {
            $headers = @{ Range = "bytes=$resumeFrom-" }
            [void]$request.Headers.TryAddWithoutValidation("Range", [string]$headers.Range)
        }
        $task = $Client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        return $task.GetAwaiter().GetResult()
    } finally {
        $request.Dispose()
    }
}

function Get-PortableContentRange {
    param([Parameter(Mandatory = $true)][System.Net.Http.HttpResponseMessage]$Response)

    try { $range = $Response.Content.Headers.ContentRange } catch { return $null }
    if ($null -eq $range -or $range.Unit -ne "bytes" -or !$range.HasRange -or !$range.HasLength) {
        return $null
    }
    $start = [int64]$range.From
    $end = [int64]$range.To
    $total = [int64]$range.Length
    if ($end -lt $start -or $total -le $end) { return $null }
    $contentLength = $Response.Content.Headers.ContentLength
    if ($null -ne $contentLength -and [int64]$contentLength -ne ($end - $start + 1)) {
        return $null
    }
    return [pscustomobject]@{ Start = $start; End = $end; Total = $total }
}

function Get-PortableDownloadPlan {
    param(
        [Parameter(Mandatory = $true)][System.Net.Http.HttpResponseMessage]$Response,
        [Parameter(Mandatory = $true)][int64]$ResumeFrom
    )

    $status = [int]$Response.StatusCode
    if ($status -eq 200) {
        $contentLength = $Response.Content.Headers.ContentLength
        $total = if ($null -ne $contentLength) { [int64]$contentLength } else { 0 }
        return [pscustomobject]@{ Append = $false; Start = [int64]0; Total = $total }
    }
    if ($status -eq 206) {
        $range = Get-PortableContentRange -Response $Response
        if ($null -ne $range -and $range.Start -eq $ResumeFrom) {
            return [pscustomobject]@{ Append = ($ResumeFrom -gt 0); Start = $range.Start; Total = $range.Total }
        }
        if ($ResumeFrom -gt 0) { return $null }
        throw "HTTP 206 response has an invalid zero-based Content-Range"
    }
    if ($status -eq 416 -and $ResumeFrom -gt 0) { return $null }
    [void]$Response.EnsureSuccessStatusCode()
    throw "Unexpected HTTP download status: $status"
}

function Receive-LockedArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][int64]$ExpectedSize
    )

    $partial = "$Archive.partial"
    Assert-PortableNotCancelled
    if (Test-LockedArchive -Path $partial -ExpectedSha256 $ExpectedSha256 -ExpectedSize $ExpectedSize) {
        Move-Item -LiteralPath $partial -Destination $Archive -Force
        return
    }
    $resumeFrom = if (Test-Path -LiteralPath $partial) { (Get-Item -LiteralPath $partial).Length } else { 0 }

    Add-Type -AssemblyName System.Net.Http
    $client = [System.Net.Http.HttpClient]::new()
    $response = $null
    $source = $null
    $destination = $null
    try {
        $response = Open-PortableHttpResponse -Client $client -Url $Url -ResumeFrom $resumeFrom
        $plan = Get-PortableDownloadPlan -Response $response -ResumeFrom $resumeFrom
        if ($null -eq $plan) {
            $response.Dispose()
            $response = $null
            Assert-PortableNotCancelled
            $response = Open-PortableHttpResponse -Client $client -Url $Url -ResumeFrom 0
            $plan = Get-PortableDownloadPlan -Response $response -ResumeFrom 0
            if ($null -eq $plan) { throw "Clean HTTP download did not provide a complete zero-based response" }
        }
        $mode = if ($plan.Append) { [System.IO.FileMode]::Append } else { [System.IO.FileMode]::Create }
        $destination = [System.IO.File]::Open($partial, $mode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        $sourceTask = $response.Content.ReadAsStreamAsync()
        $source = $sourceTask.GetAwaiter().GetResult()
        $total = [int64]$plan.Total
        $written = [int64]$plan.Start
        $buffer = [byte[]]::new(1024 * 1024)
        while ($true) {
            Assert-PortableNotCancelled
            $read = $source.Read($buffer, 0, $buffer.Length)
            if ($read -le 0) { break }
            Assert-PortableNotCancelled
            $destination.Write($buffer, 0, $read)
            $destination.Flush()
            $written += $read
            if ($total -gt 0) {
                Write-Progress -Activity "Downloading pinned Miniforge" -Status "$written / $total bytes" -PercentComplete ([Math]::Min(100, 100 * $written / $total))
            }
        }
        Write-Progress -Activity "Downloading pinned Miniforge" -Completed
    } finally {
        if ($null -ne $destination) { $destination.Dispose() }
        if ($null -ne $source) { $source.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        $client.Dispose()
    }
    Assert-PortableNotCancelled
    if (!(Test-LockedArchive -Path $partial -ExpectedSha256 $ExpectedSha256 -ExpectedSize $ExpectedSize)) {
        throw "downloaded Miniforge .partial failed size/SHA-256 verification; it was not promoted: $partial"
    }
    Move-Item -LiteralPath $partial -Destination $archive -Force
}

function Ensure-BuildConda {
    param(
        [string]$CacheRoot = "data/cache/portable/conda",
        [string]$LockPath = "packaging/portable/toolchain.lock.json",
        [switch]$DryRun
    )

    Assert-PortableNotCancelled
    $cache = Resolve-PackageChildPath -Path $CacheRoot -Label "private Conda cache"
    $resolvedLockPath = Resolve-PackageChildPath -Path $LockPath -Label "portable toolchain lock"
    $toolchain = Get-LockedMiniforge $resolvedLockPath
    $archive = Resolve-PackageChildPath -Path (Join-Path $cache $toolchain.archive) -Label "locked Miniforge archive"
    Assert-DirectChildPath -Parent $cache -Path $archive -Label "locked Miniforge archive"
    $partial = Resolve-PackageChildPath -Path "$archive.partial" -Label "locked Miniforge partial"
    Assert-DirectChildPath -Parent $cache -Path $partial -Label "locked Miniforge partial"
    $installRoot = Resolve-PackageChildPath -Path (Join-Path $cache ("miniforge-" + $toolchain.version)) -Label "private Miniforge installation"
    Assert-DirectChildPath -Parent $cache -Path $installRoot -Label "private Miniforge installation"
    $conda = Resolve-PackageChildPath -Path (Join-Path $installRoot "condabin/conda.bat") -Label "private Conda command"
    $packageCache = Resolve-PackageChildPath -Path (Join-Path $cache "conda-pkgs") -Label "private Conda package cache"
    Assert-DirectChildPath -Parent $cache -Path $packageCache -Label "private Conda package cache"

    $env:CONDA_PKGS_DIRS = $packageCache
    if (Test-Path -LiteralPath $conda -PathType Leaf) {
        Write-Host "[portable-conda] reuse private Conda: $conda"
        return $conda
    }
    if (Test-Path -LiteralPath $installRoot) {
        throw "Private Conda installation is incomplete: $installRoot. Delete only this cache directory, then rerun."
    }
    if ($DryRun) {
        Write-Host "[dry-run] cache root: $cache"
        Write-Host "[dry-run] pinned archive: $archive"
        Write-Host "[dry-run] pinned SHA-256: $($toolchain.sha256)"
        Write-Host "[dry-run] private install root: $installRoot"
        return $conda
    }

    New-Item -ItemType Directory -Force -Path $cache, $packageCache | Out-Null
    if (!(Test-Path -LiteralPath $archive -PathType Leaf)) {
        Write-Host "[portable-conda] downloading pinned Miniforge archive"
        Receive-LockedArchive -Url $toolchain.url -Archive $archive -ExpectedSha256 $toolchain.sha256 -ExpectedSize $toolchain.size_bytes
    }
    if (!(Test-LockedArchive -Path $archive -ExpectedSha256 $toolchain.sha256 -ExpectedSize $toolchain.size_bytes)) {
        throw "Miniforge size/SHA-256 does not match toolchain.lock.json: $archive"
    }

    $arguments = @(
        "/InstallationType=JustMe",
        "/RegisterPython=0",
        "/AddToPath=0",
        "/S",
        "/D=$installRoot"
    )
    $archive = Resolve-PackageChildPath -Path $archive -Label "locked Miniforge archive"
    Assert-DirectChildPath -Parent $cache -Path $archive -Label "locked Miniforge archive"
    $installRoot = Resolve-PackageChildPath -Path $installRoot -Label "private Miniforge installation"
    Write-Host "[portable-conda] installing private Miniforge below $cache"
    $process = Start-Process -FilePath $archive -ArgumentList $arguments -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -ne 0) {
        throw "Private Miniforge installer failed with exit code $($process.ExitCode)"
    }
    if (!(Test-Path -LiteralPath $conda -PathType Leaf)) {
        throw "Private Miniforge did not provide conda.bat: $conda"
    }
    return $conda
}

try {
    $privateConda = Ensure-BuildConda -CacheRoot $CacheRoot -LockPath $LockPath -DryRun:$DryRun
} catch [System.OperationCanceledException] {
    Write-Error -ErrorAction Continue $_.Exception.Message
    exit 20
}
Write-Host "[portable-conda] conda command: $privateConda"
if ($PassThru) {
    Write-Output $privateConda
}
