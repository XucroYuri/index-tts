@echo off
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\Invoke-PortableStart.ps1" %*
exit /b %errorlevel%
