@echo off
setlocal
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0runtime\live\python.exe"
"%PYTHON%" "%~dp0webui.py" %*
exit /b %errorlevel%
