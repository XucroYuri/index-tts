@echo off
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\Start-WebUI.ps1" %*
exit /b %errorlevel%
