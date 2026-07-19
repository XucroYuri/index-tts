[CmdletBinding()]
param([string]$PackageRoot = "")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$candidateRoot = if ([string]::IsNullOrWhiteSpace($PackageRoot)) { [IO.Path]::GetFullPath((Split-Path -Parent $Bundle)) } else { [IO.Path]::GetFullPath($PackageRoot) }
if ([string]::IsNullOrWhiteSpace($PackageRoot) -and !(Test-Path -LiteralPath (Join-Path $candidateRoot "package\tts-more-package.json") -PathType Leaf)) {
    $candidateParent = [IO.Path]::GetFullPath((Split-Path -Parent $candidateRoot))
    if (Test-Path -LiteralPath (Join-Path $candidateParent "package\tts-more-package.json") -PathType Leaf) { $candidateRoot = $candidateParent }
}
$candidateRoot = Assert-PortablePackageRootChain -Root $candidateRoot
$PathsScript = Join-Path $Bundle "Portable-Paths.ps1"
if (!(Test-Path -LiteralPath $PathsScript -PathType Leaf)) { throw "Portable-Paths.ps1 is missing" }
. $PathsScript
$paths = Get-PortableWorkerPaths -BundleRoot $Bundle -PackageRoot $candidateRoot
$Bundle = $paths.BundleRoot
$Root = $paths.PackageRoot
$SourceRoot = $paths.SourceRoot
$Root = Assert-PortablePackageRootChain -Root $Root
$ValidationScript = Join-Path $Bundle "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $ValidationScript -PathType Leaf)) { throw "Portable-Validation.ps1 is missing" }
. $ValidationScript
$Root = Assert-PortablePackageRoot -Root $Root
$recordPath = Resolve-PortablePackagePath -Root $Root -RelativePath "data\local\run\worker.pid.json" -Label "PID record"
if (!(Test-Path -LiteralPath $recordPath)) { Write-Host "worker is not running"; exit 0 }
$runtimeLockRelative = (Join-Path $Bundle "locks\runtime.lock.json").Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
$runtimeLockPath = Resolve-PortablePackagePath -Root $Root -RelativePath $runtimeLockRelative -Label "runtime lock" -MustExist
$runtimeLock = Get-Content -LiteralPath $runtimeLockPath -Raw | ConvertFrom-Json
$expectedPython = [string]$runtimeLock.python_version
if ($expectedPython -notin @("3.10", "3.10.11", "3.11", "3.11.9")) { throw "worker runtime lock has an unsupported Python version" }
$Python = Join-Path $Root "runtime\live\python.exe"
[void](Assert-PortableRuntime -Root $Root -SourceRoot $SourceRoot -PythonPath $Python -ExpectedVersion $expectedPython -ImportProbe "")
$launcherRelative = (Join-Path $Bundle "portable_launcher.py").Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
$Launcher = Resolve-PortablePackagePath -Root $Root -RelativePath $launcherRelative -Label "portable launcher" -MustExist
& $Python $Launcher stop-worker --package-root $Root
if ($LASTEXITCODE -eq 2) { throw "owned worker stopped but port release was not confirmed; PID record preserved" }
if ($LASTEXITCODE -ne 0) { throw "safe worker stop failed with exit code $LASTEXITCODE" }
Write-Host "worker stopped and port released"
