<#
.SYNOPSIS
  Revert files installed by this project's manifest; preserve changed files, logs, saves and backups.
#>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$GameDir, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'script-common.ps1')
$game = Resolve-KingdomGame -GameDir $GameDir
Assert-GameStopped
$manifestPath = Resolve-ContainedPath -Root $game -Relative 'UserData\KingdomAI\install-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'No Kingdom AI install manifest found. No files were changed.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.format -ne 1 -or $manifest.game_dir -ne $game) { throw 'Install manifest does not match this game directory.' }
$remaining = [Collections.Generic.List[object]]::new()
$records = @($manifest.files)
[Array]::Reverse($records)
foreach ($record in $records) {
    Assert-OwnedInstallPath -Relative $record.relative
    $dest = Resolve-ContainedPath -Root $game -Relative $record.relative
    if (Test-Path -LiteralPath $dest) {
        if (-not $record.installed_sha256 -or (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash -ne $record.installed_sha256) {
            Write-Warning "Preserved modified file: $($record.relative)"
            $remaining.Add($record)
            continue
        }
    }
    $backup = $null
    if ($record.original_backup) {
        if (-not $record.original_backup.StartsWith('UserData\KingdomAI\backups\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected backup location in install manifest.' }
        $backup = Resolve-ContainedPath -Root $game -Relative $record.original_backup
        if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) {
            Write-Warning "Missing backup; preserved file: $($record.relative)"
            $remaining.Add($record)
            continue
        }
    }
    if ($DryRun) {
        Write-Host $(if ($backup) { "Would restore: $($record.relative)" } else { "Would remove unchanged installed file: $($record.relative)" })
        continue
    }
    Assert-GameStopped
    if ($backup) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
        Copy-Item -LiteralPath $backup -Destination $dest -Force
    } elseif (Test-Path -LiteralPath $dest -PathType Leaf) {
        Remove-Item -LiteralPath $dest -Force
    }
}
if (-not $DryRun) {
    # Keep the original manifest for audit; it has hashes/paths, never token contents.
    $history = Resolve-ContainedPath -Root $game -Relative ('UserData\KingdomAI\backups\uninstall-' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff') + '\install-manifest.json')
    New-Item -ItemType Directory -Path (Split-Path -Parent $history) -Force | Out-Null
    Copy-Item -LiteralPath $manifestPath -Destination $history
    if ($remaining.Count) {
        $manifest.files = @($remaining.ToArray())
        Write-Utf8File -Path $manifestPath -Content ($manifest | ConvertTo-Json -Depth 8)
    } else { Remove-Item -LiteralPath $manifestPath -Force }
}
Write-Host 'Uninstall finished. Changed files, logs, save files and backup directories were preserved.'
