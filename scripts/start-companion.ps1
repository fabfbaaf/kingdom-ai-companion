param(
    [string]$BridgeConfig,
    [string]$Python = "",
    [int]$Port = 48860,
    [switch]$OpenBrowser
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $localPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $localPython) { $localPython } else { "python" }
}
if ($BridgeConfig -and -not [System.IO.Path]::IsPathRooted($BridgeConfig)) {
    throw "BridgeConfig 必须是 bridge.local.json 的绝对路径。"
}
$arguments = @("-m", "companion", "--port", "$Port")
if ($BridgeConfig) { $arguments += @("--bridge-config", $BridgeConfig) }
Push-Location -LiteralPath $projectRoot
try {
    if ($OpenBrowser) { Start-Process "http://127.0.0.1:$Port" }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "后台启动失败（退出码 $LASTEXITCODE）。" }
} finally {
    Pop-Location
}
