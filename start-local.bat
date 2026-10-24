@echo off
call "%~dp0update-and-start.bat" --local
exit /b %errorlevel%
