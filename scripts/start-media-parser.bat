@echo off
setlocal
for %%I in ("%~dp0..") do set "PROJECT=%%~fI"
"%PROJECT%\.venv\Scripts\python.exe" "%PROJECT%\scripts\media-parser-server.py" %*
if errorlevel 1 pause
