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

function Resolve-PortableFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path)
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

function Get-LockedMiniforge {
    param([Parameter(Mandatory = $true)][string]$ResolvedLockPath)

    if (!(Test-Path -LiteralPath $ResolvedLockPath -PathType Leaf)) {
        throw "Portable toolchain lock is missing: $ResolvedLockPath"
    }
    $lock = Get-Content -LiteralPath $ResolvedLockPath -Raw | ConvertFrom-Json
    $toolchain = $lock.miniforge
    foreach ($field in @("version", "archive", "url", "sha256")) {
        if ([string]::IsNullOrWhiteSpace([string]$toolchain.$field)) {
            throw "Portable toolchain lock field is missing: miniforge.$field"
        }
    }
    return $toolchain
}

function Test-LockedSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    return $actual -eq $ExpectedSha256.ToLowerInvariant()
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
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    $partial = "$Archive.partial"
    Assert-PortableNotCancelled
    if ((Test-Path -LiteralPath $partial -PathType Leaf) -and (Test-LockedSha256 -Path $partial -ExpectedSha256 $ExpectedSha256)) {
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
    if (!(Test-LockedSha256 -Path $partial -ExpectedSha256 $ExpectedSha256)) {
        throw "downloaded Miniforge .partial failed SHA-256 verification; it was not promoted: $partial"
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
    $cache = Resolve-RepoPath $CacheRoot
    $resolvedLockPath = Resolve-RepoPath $LockPath
    $toolchain = Get-LockedMiniforge $resolvedLockPath
    $archive = Join-Path $cache $toolchain.archive
    $installRoot = Join-Path $cache ("miniforge-" + $toolchain.version)
    $conda = Join-Path $installRoot "condabin/conda.bat"
    $packageCache = Join-Path $cache "conda-pkgs"

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
        Receive-LockedArchive -Url $toolchain.url -Archive $archive -ExpectedSha256 $toolchain.sha256
    }
    if (!(Test-LockedSha256 -Path $archive -ExpectedSha256 $toolchain.sha256)) {
        throw "Miniforge SHA-256 does not match toolchain.lock.json: $archive"
    }

    $arguments = @(
        "/InstallationType=JustMe",
        "/RegisterPython=0",
        "/AddToPath=0",
        "/S",
        "/D=$installRoot"
    )
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
