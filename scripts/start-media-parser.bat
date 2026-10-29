@echo off
setlocal
set "PROJECT=%~dp0.."
"%PROJECT%\.venv\Scripts\python.exe" "%PROJECT%\scripts\media-parser-server.py" %*
if errorlevel 1 pause
