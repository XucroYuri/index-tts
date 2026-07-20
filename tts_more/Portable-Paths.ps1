Set-StrictMode -Version Latest

function Test-PortableWorkerPathWithinRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if ([string]::Equals($resolvedRoot, $resolvedPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $resolvedPath.StartsWith(
        $resolvedRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-PortableWorkerPackageRootChain {
    param([Parameter(Mandatory = $true)][string]$Root)
    $lexicalRoot = [IO.Path]::GetFullPath($Root)
    $pathRoot = [IO.Path]::GetPathRoot($lexicalRoot)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) { throw "portable package root has no filesystem root" }
    $current = [IO.Path]::GetFullPath($pathRoot)
    $relative = $lexicalRoot.Substring($pathRoot.Length).TrimStart(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    foreach ($segment in @($relative -split '[\\/]' | Where-Object { ![string]::IsNullOrWhiteSpace($_) })) {
        $current = [IO.Path]::GetFullPath((Join-Path $current $segment))
        if (!(Test-Path -LiteralPath $current -PathType Container)) { throw "portable package root or ancestor is missing" }
        if (((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "portable package root or ancestor cannot be a reparse point"
        }
    }
    return $lexicalRoot
}

function Assert-PortableWorkerPathChain {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    if (!(Test-PortableWorkerPathWithinRoot -Root $Root -Path $Path)) { throw "worker path resolves outside the package" }
    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $relative = $resolvedPath.Substring($resolvedRoot.Length).TrimStart(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $current = $resolvedRoot
    foreach ($segment in @($relative -split '[\\/]' | Where-Object { ![string]::IsNullOrWhiteSpace($_) })) {
        $current = Join-Path $current $segment
        if (!(Test-Path -LiteralPath $current)) { throw "worker path is missing" }
        if (((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "worker path traverses a reparse point"
        }
    }
    return $resolvedPath
}

function Set-PortableWorkerMutableCacheEnvironment {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)

    $resolvedPackage = Assert-PortableWorkerPackageRootChain -Root $PackageRoot
    $current = $resolvedPackage
    foreach ($segment in @("data", "cache", "numba")) {
        $candidate = [IO.Path]::GetFullPath((Join-Path $current $segment))
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (!$item.PSIsContainer) { throw "portable mutable cache path is not a directory" }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "portable mutable cache path cannot be a reparse point"
            }
        } else {
            [void][IO.Directory]::CreateDirectory($candidate)
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "portable mutable cache path cannot be a reparse point"
            }
        }
        $current = $candidate
    }
    $env:NUMBA_CACHE_DIR = $current
    return $current
}

function Resolve-PortableWorkerSourceRoot {
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][object]$ComponentConfig
    )
    $relative = "."
    if ($ComponentConfig.PSObject.Properties["source_root"]) {
        $relative = [string]$ComponentConfig.source_root
    }
    if (
        [string]::IsNullOrWhiteSpace($relative) -or
        [IO.Path]::IsPathRooted($relative) -or
        $relative.Contains(":") -or
        @($relative -split '[\\/]') -contains ".."
    ) {
        throw "component source_root must be a safe package-relative path"
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $PackageRoot $relative))
    if (!(Test-PortableWorkerPathWithinRoot -Root $PackageRoot -Path $resolved)) {
        throw "component source_root resolves outside the package"
    }
    $current = [IO.Path]::GetFullPath($PackageRoot)
    if (!(Test-Path -LiteralPath $current -PathType Container)) { throw "portable package root is missing" }
    if (((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "portable package root cannot be a reparse point"
    }
    foreach ($segment in @($relative -split '[\\/]')) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq ".") { continue }
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            if (((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "component source_root traverses a reparse point"
            }
        }
    }
    if (!(Test-Path -LiteralPath $resolved -PathType Container)) { throw "component source_root is missing" }
    return $resolved
}

function Get-PortableWorkerPaths {
    param(
        [string]$BundleRoot = $PSScriptRoot,
        [string]$PackageRoot = ""
    )
    $bundle = [IO.Path]::GetFullPath($BundleRoot)

    if ([string]::IsNullOrWhiteSpace($PackageRoot)) {
        $candidate = [IO.Path]::GetFullPath((Split-Path -Parent $bundle))
        $resolvedPackage = $candidate
        $manifestFound = $false
        for ($depth = 0; $depth -lt 3; $depth++) {
            if (Test-Path -LiteralPath (Join-Path $candidate "package\tts-more-package.json") -PathType Leaf) {
                $resolvedPackage = $candidate
                $manifestFound = $true
                break
            }
            $parent = Split-Path -Parent $candidate
            if ([string]::IsNullOrWhiteSpace($parent) -or [string]::Equals($parent, $candidate, [StringComparison]::OrdinalIgnoreCase)) {
                break
            }
            $candidate = [IO.Path]::GetFullPath($parent)
        }
    } else {
        $resolvedPackage = [IO.Path]::GetFullPath($PackageRoot)
        $manifestFound = Test-Path -LiteralPath (Join-Path $resolvedPackage "package\tts-more-package.json") -PathType Leaf
    }

    $resolvedPackage = Assert-PortableWorkerPackageRootChain -Root $resolvedPackage
    [void](Assert-PortableWorkerPathChain -Root $resolvedPackage -Path $bundle)
    $configPath = Join-Path $bundle "component.json"
    if (!(Test-Path -LiteralPath $configPath -PathType Leaf)) { throw "worker component.json is missing" }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json

    $configuredSource = if ($config.PSObject.Properties["source_root"]) { [string]$config.source_root } else { "." }
    if ($configuredSource -ne "." -and !$manifestFound) {
        throw "staged worker source_root requires a package manifest"
    }

    $sourceRoot = Resolve-PortableWorkerSourceRoot -PackageRoot $resolvedPackage -ComponentConfig $config
    $expectedBundle = [IO.Path]::GetFullPath((Join-Path $sourceRoot "tts_more"))
    if (![string]::Equals($expectedBundle, $bundle, [StringComparison]::OrdinalIgnoreCase)) {
        throw "worker bundle does not match package source_root"
    }
    return [pscustomobject]@{
        PackageRoot = $resolvedPackage
        SourceRoot = $sourceRoot
        BundleRoot = $bundle
        Component = [string]$config.component
        Config = $config
    }
}
