@echo off
setlocal EnableExtensions
set "TTS_MORE_ROOT=%~dp0"
if not defined TTS_MORE_PORT set "TTS_MORE_PORT=7860"
set "NO_PROXY=127.0.0.1,localhost,%NO_PROXY%"
set "no_proxy=%NO_PROXY%"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$root = $env:TTS_MORE_ROOT; $python = Join-Path $root '.venv\Scripts\python.exe'; if (!(Test-Path -LiteralPath $python)) { throw 'IndexTTS virtual environment is missing' }; if (Get-NetTCPConnection -State Listen -LocalPort ([int]$env:TTS_MORE_PORT) -ErrorAction SilentlyContinue) { throw ('IndexTTS port {0} is already in use' -f $env:TTS_MORE_PORT) }; $run = Join-Path $root 'data\local\run'; New-Item -ItemType Directory -Path $run -Force | Out-Null; $process = Start-Process -FilePath $python -ArgumentList @('webui.py', '--host', '127.0.0.1', '--port', $env:TTS_MORE_PORT) -WorkingDirectory $root -WindowStyle Hidden -PassThru; @{ pid = [int]$process.Id; executable_path = $python; port = [int]$env:TTS_MORE_PORT } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'worker.pid.json') -Encoding UTF8"
exit /b %errorlevel%
