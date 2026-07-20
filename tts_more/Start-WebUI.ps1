[CmdletBinding()]
param(
    [string]$PackageRoot = "",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$WebUIArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"
$pathsScript = Join-Path $PSScriptRoot "Portable-Paths.ps1"
if (!(Test-Path -LiteralPath $pathsScript -PathType Leaf)) { throw "Portable-Paths.ps1 is missing" }
. $pathsScript
$paths = Get-PortableWorkerPaths -BundleRoot $PSScriptRoot -PackageRoot $PackageRoot
[void](Set-PortableWorkerMutableCacheEnvironment -PackageRoot $paths.PackageRoot)
$Root = $paths.PackageRoot
$SourceRoot = $paths.SourceRoot
$config = $paths.Config
$privatePython = Join-Path $Root "runtime\live\python.exe"
$checkoutPython = Join-Path $SourceRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $privatePython -PathType Leaf) { $privatePython } else { $checkoutPython }
$isStaged = Test-Path -LiteralPath (Join-Path $Root "package\tts-more-package.json") -PathType Leaf

Push-Location -LiteralPath $SourceRoot
try {
    switch ([string]$config.component) {
        "gpt-sovits" {
            if ($isStaged) {
                if (!(Test-Path -LiteralPath $privatePython -PathType Leaf)) { throw "GPT-SoVITS package runtime is missing; run Initialize.cmd first" }
                $arguments = @("-I", "-B", (Join-Path $SourceRoot "webui.py"), "zh_CN") + @($WebUIArguments)
                & $privatePython @arguments
            } else {
                $launcher = Join-Path $SourceRoot "go-webui.bat"
                if (!(Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "GPT-SoVITS go-webui.bat is missing from source_root" }
                & $launcher @WebUIArguments
            }
        }
        "indextts" {
            if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { throw "IndexTTS package runtime is missing; run Initialize.cmd first" }
            & $Python (Join-Path $SourceRoot "webui.py") @WebUIArguments
        }
        "cosyvoice" {
            if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { throw "CosyVoice package runtime is missing; run Initialize.cmd first" }
            & $Python (Join-Path $SourceRoot "webui.py") @WebUIArguments
        }
        default { throw "unsupported worker component: $($config.component)" }
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
