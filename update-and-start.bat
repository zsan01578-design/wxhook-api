@echo off
setlocal
rem Parse the whole block before the update can replace this batch file.
(
if /I "%~1"=="--local" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-project.ps1" -Local
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-project.ps1"
)
if errorlevel 1 (
    echo [ERROR] Restart failed. See the details above.
    echo If Git update failed, copy the diagnosis and file list shown above.
    echo Error summary, when available: "%~dp0data\update-last-error.log"
    pause
    exit /b 1
)
exit /b 0
)
