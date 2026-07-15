[CmdletBinding()]
param(
    [string]$PackageRoot = "",
    [string]$OperationRoot = "",
    [ValidateRange(1, 65535)][Nullable[int]]$PortOverride = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ValidationScript = Join-Path $PSScriptRoot "Portable-Validation.ps1"
if (!(Test-Path -LiteralPath $ValidationScript -PathType Leaf)) { throw "Portable-Validation.ps1 is missing" }
. $ValidationScript
$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = if ([string]::IsNullOrWhiteSpace($PackageRoot)) { [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle)) } else { [System.IO.Path]::GetFullPath($PackageRoot) }
$config = Get-Content -LiteralPath (Join-Path $Bundle "component.json") -Raw | ConvertFrom-Json
$Port = if ($null -ne $PortOverride) { [int]$PortOverride } elseif ($env:TTS_MORE_PORT) { [int]$env:TTS_MORE_PORT } else { [int]$config.port }
$Python = Join-Path $Root "runtime\live\python.exe"
$RuntimeLock = Get-Content -LiteralPath (Join-Path $Bundle "locks\runtime.lock.json") -Raw | ConvertFrom-Json
$ExpectedPython = if ([string]::IsNullOrWhiteSpace([string]$RuntimeLock.python_version)) { [string]$config.python } else { [string]$RuntimeLock.python_version }
$ImportProbe = if ($RuntimeLock.PSObject.Properties["import_probe"] -and ![string]::IsNullOrWhiteSpace([string]$RuntimeLock.import_probe)) { [string]$RuntimeLock.import_probe } else { [string]$config.import_probe }
[void](Assert-PortableRuntime -Root $Root -PythonPath $Python -ExpectedVersion $ExpectedPython -ImportProbe $ImportProbe)
$manifestPath = Join-Path $Root "package\tts-more-package.json"
$buildId = if (Test-Path -LiteralPath $manifestPath -PathType Leaf) { [string](Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).build_id } else { "source-checkout" }
$arguments = @("-m", "uvicorn", [string]$config.module, "--app-dir", $Bundle, "--host", "127.0.0.1", "--port", [string]$Port)
$recordPath = Join-Path $Root "data\local\run\worker.pid.json"
$Launcher = Join-Path $Bundle "portable_launcher.py"
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, Path })
    $verifyArguments = @($Launcher, "verify-owned-listener", "--package-root", $Root, "--record-path", $recordPath, "--port", [string]$Port, "--build-id", $buildId, "--executable", $Python)
    foreach ($listenerPid in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) { $verifyArguments += @("--listener-pid", [string]$listenerPid) }
    $verifyArguments += "--"
    $verifyArguments += $arguments
    & $Python @verifyArguments *> $null
    $owned = $LASTEXITCODE -eq 0
    if ($owned) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if ($health) { Write-Host "$($config.component) ready: http://127.0.0.1:$Port"; exit 0 }
        } catch { }
    }
    throw "PORT_IN_USE: worker port $Port is occupied by $($owners | ConvertTo-Json -Compress). No process was terminated."
}
switch ([string]$config.component) {
    "gpt-sovits" { $env:TTS_MORE_GPTSOVITS_REPO = $Root }
    "indextts" { $env:TTS_MORE_INDEXTTS_REPO = $Root; $env:TTS_MORE_INDEXTTS_PYTHON = $Python }
    "cosyvoice" { $env:TTS_MORE_COSYVOICE_REPO = $Root; $env:TTS_MORE_COSYVOICE_MODEL_DIR = (Join-Path $Root "pretrained_models\CosyVoice-300M") }
}
$process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru
$created = $process.StartTime.ToUniversalTime().ToString("o")
& $Python $Launcher write-process-record --package-root $Root --record-path $recordPath --pid $process.Id --parent-pid $PID --process-created-at $created --executable $Python --port $Port --build-id $buildId -- @arguments
if ($LASTEXITCODE -ne 0) { throw "failed to write worker ownership record" }
$deadline = [DateTime]::UtcNow.AddSeconds(120)
do {
    if ($process.HasExited) { throw "worker exited during startup with code $($process.ExitCode)" }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($null -ne $health.ready) { Write-Host "$($config.component) worker ready: http://127.0.0.1:$Port"; exit 0 }
    } catch { Start-Sleep -Milliseconds 500 }
} while ([DateTime]::UtcNow -lt $deadline)
throw "worker health endpoint did not respond within 120 seconds"
