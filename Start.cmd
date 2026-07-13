@echo off
setlocal EnableExtensions
set "TTS_MORE_ROOT=%~dp0"
set "TTS_MORE_PORT=7860"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$root = $env:TTS_MORE_ROOT; $python = Join-Path $root '.venv\Scripts\python.exe'; if (!(Test-Path -LiteralPath $python)) { throw 'IndexTTS virtual environment is missing' }; if (Get-NetTCPConnection -State Listen -LocalPort ([int]$env:TTS_MORE_PORT) -ErrorAction SilentlyContinue) { throw 'IndexTTS port 7860 is already in use' }; $run = Join-Path $root 'data\local\run'; New-Item -ItemType Directory -Path $run -Force | Out-Null; $process = Start-Process -FilePath $python -ArgumentList @('webui.py', '--host', '127.0.0.1', '--port', $env:TTS_MORE_PORT) -WorkingDirectory $root -WindowStyle Hidden -PassThru; @{ pid = [int]$process.Id; executable_path = $python; port = [int]$env:TTS_MORE_PORT } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'worker.pid.json') -Encoding UTF8"
exit /b %errorlevel%
