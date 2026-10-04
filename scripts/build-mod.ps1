<#
.SYNOPSIS
  Build the bridge without launching or modifying the game.
#>
[CmdletBinding()]
param(
    [string]$Project = (Join-Path (Split-Path -Parent $PSScriptRoot) 'mod\src\KingdomAI.Bridge\KingdomAI.Bridge.csproj'),
    [string]$LoaderDir,
    [string]$LoaderRefs,
    [string]$Dotnet,
    [switch]$Offline,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Dotnet) { $Dotnet = Join-Path $root '.tools\dotnet\dotnet.exe' }
$automaticRefs = -not $LoaderRefs -and -not $LoaderDir
if (-not $LoaderRefs) {
    if ($LoaderDir) { $LoaderRefs = Join-Path $LoaderDir 'net6' }
    else {
        $candidates = @('.cache\MelonLoader\MelonLoader\net6','dependencies\loader-payload\MelonLoader\net6','.cache\loader-payload-v0.7.3-unity6000.0.66\MelonLoader\net6') | ForEach-Object { Join-Path $root $_ }
        $LoaderRefs = @($candidates | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'MelonLoader.dll') }) | Select-Object -First 1
        if (-not $LoaderRefs) { $LoaderRefs = $candidates[-1] }
    }
}
$out = Join-Path $root 'artifacts\mod'
if ($DryRun) {
    Write-Host "Build project: $Project"
    Write-Host "Loader references: $LoaderRefs"
    Write-Host "Output: $out"
    return
}
if (-not (Test-Path -LiteralPath $Project -PathType Leaf)) { throw "Bridge project missing: $Project" }
if (-not (Test-Path -LiteralPath $Dotnet -PathType Leaf)) { throw 'Missing .tools/dotnet SDK. Install .NET SDK 8 there or pass -Dotnet.' }
if ($automaticRefs -and -not (Test-Path -LiteralPath (Join-Path $LoaderRefs 'MelonLoader.dll'))) {
    $prepared = & (Join-Path $PSScriptRoot 'prepare-loader.ps1') -ProjectRoot $root -Dotnet $Dotnet -Offline:$Offline
    $LoaderRefs = Join-Path ([string](@($prepared)[-1])) 'MelonLoader\net6'
}
foreach ($assembly in @('MelonLoader.dll', '0Harmony.dll', 'Il2CppInterop.Runtime.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $LoaderRefs $assembly))) { throw "Missing loader reference $assembly. Prepare/download MelonLoader 0.7.3 first." }
}
$oldRoot = $env:DOTNET_ROOT
try {
    $env:DOTNET_ROOT = Split-Path -Parent $Dotnet
    $arguments = @('build', $Project, '-c', 'Release', '--nologo', '-m:1', '-o', $out, "-p:LoaderRefs=$LoaderRefs")
    if ($Offline) { $arguments += '--no-restore' }
    & $Dotnet @arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Bridge build failed with exit code $LASTEXITCODE." }
} finally { $env:DOTNET_ROOT = $oldRoot }
$bridge = Join-Path $out 'KingdomAI.Bridge.dll'
if (-not (Test-Path -LiteralPath $bridge)) { throw 'Build completed without KingdomAI.Bridge.dll.' }
Write-Host "Built bridge: $bridge"
