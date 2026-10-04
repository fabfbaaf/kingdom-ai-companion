[CmdletBinding()]
param([string]$Python)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw 'Create .venv and install the project plus pyinstaller first.' }
Push-Location -LiteralPath $root
try {
    $staticPath = Join-Path $root 'companion\static'
    $audioHooks = Join-Path $root 'scripts\pyinstaller-hooks'
    $speechHelper = Join-Path $root 'companion\speech-helper.ps1'
    & $Python -m PyInstaller --noconfirm --clean --onefile --noconsole --name KingdomAI --distpath artifacts\app --workpath build\app --specpath build --additional-hooks-dir $audioHooks --add-data "$staticPath;companion\static" --add-data "$speechHelper;companion" --collect-submodules uvicorn --collect-all vosk --hidden-import sounddevice --hidden-import fastapi --hidden-import httpx --hidden-import companion.game_installation launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Portable companion build failed.' }
    Write-Host (Join-Path $root 'artifacts\app\KingdomAI.exe')
} finally { Pop-Location }
