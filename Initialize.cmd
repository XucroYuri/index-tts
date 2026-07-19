@echo off
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tts_more\Initialize.ps1" %*
exit /b %errorlevel%
