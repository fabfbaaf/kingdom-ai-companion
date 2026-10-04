@echo off
setlocal
cd /d "%~dp0"
if exist "KingdomAI.exe" (
  start "" "KingdomAI.exe"
  exit /b 0
)
if exist "artifacts\app\KingdomAI.exe" (
  start "" "artifacts\app\KingdomAI.exe"
  exit /b 0
)
if exist ".venv\Scripts\python.exe" goto source
echo KingdomAI.exe was not found. Use the portable package, or create the Python environment described in README.md.
pause
exit /b 1
:source
".venv\Scripts\python.exe" launcher.py
exit /b %errorlevel%
