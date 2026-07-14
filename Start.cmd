@echo off
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\Start-Worker.ps1" %*
exit /b %errorlevel%
