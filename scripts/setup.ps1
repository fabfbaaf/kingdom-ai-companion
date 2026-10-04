<#
.SYNOPSIS
  Interactive entry point for a local portable package. Never launches the game.
#>
[CmdletBinding()]
param([string]$GameDir, [switch]$Offline)
$ErrorActionPreference = 'Stop'
if (-not $GameDir) {
    Write-Host 'Kingdom AI Companion - install local P2 bridge'
    Write-Host 'Close the game first. Paste the folder containing KingdomTwoCrowns.exe.'
    $GameDir = (Read-Host 'Game directory').Trim().Trim('"')
}
try {
    & (Join-Path $PSScriptRoot 'install-mod.ps1') -GameDir $GameDir -Offline:$Offline
    Write-Host 'Installed. Start KingdomAI.exe or start.cmd, then launch the game from Steam.'
    Write-Host 'AI stays off until you enable it. First launch may take time to generate local game interfaces.'
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
