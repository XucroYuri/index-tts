[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle))
$recordPath = Join-Path $Root "data\local\run\worker.pid.json"
if (!(Test-Path -LiteralPath $recordPath)) { Write-Host "worker is not running"; exit 0 }
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
$Python = [string]$record.executable_path
if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { throw "recorded package Python is missing; preserving PID record" }
& $Python (Join-Path $Bundle "portable_launcher.py") stop-worker --package-root $Root
if ($LASTEXITCODE -eq 2) { throw "owned worker stopped but port release was not confirmed; PID record preserved" }
if ($LASTEXITCODE -ne 0) { throw "safe worker stop failed with exit code $LASTEXITCODE" }
Write-Host "worker stopped and port released"
