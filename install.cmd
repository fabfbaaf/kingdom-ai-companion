@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0scripts\setup.ps1"
set "install_result=%errorlevel%"
if not "%install_result%"=="0" pause
exit /b %install_result%
