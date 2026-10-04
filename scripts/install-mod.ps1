<#
.SYNOPSIS
  Install the standalone P2 bridge, with file and save backups. Does not launch the game.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$GameDir,
    [string]$BridgeDll,
    [string]$BaseUrl = 'http://127.0.0.1:48861',
    [switch]$Offline,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'script-common.ps1')
$root = Split-Path -Parent $PSScriptRoot
$game = Resolve-KingdomGame -GameDir $GameDir
Assert-GameStopped
if (-not $BridgeDll) { $BridgeDll = Join-Path $root 'artifacts\mod\KingdomAI.Bridge.dll' }
$uri = [Uri]$BaseUrl
if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne 'http' -or $uri.Host -ne '127.0.0.1' -or $uri.Port -lt 1024 -or $uri.Port -gt 65535 -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') {
    throw 'BaseUrl must be a local http://127.0.0.1:<port> address with no credentials or path.'
}
Assert-SupportedKingdomBuild -GameDir $game
$configPath = Resolve-ContainedPath -Root $game -Relative 'UserData\KingdomAI\bridge.local.json'
$manifestPath = Resolve-ContainedPath -Root $game -Relative 'UserData\KingdomAI\install-manifest.json'
$saveBase = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'AppData\LocalLow\noio'
$saveDirs = @('KingdomTwoCrowns', 'Kingdom Two Crowns') | ForEach-Object { Join-Path $saveBase $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Container }
if ($DryRun) {
    Write-Host "Dry run: game=$game; game version=2.4.2; build=116fe7e048; Unity=6000.0.66; process stopped."
    Write-Host 'Plan: prepare pinned MelonLoader 0.7.3, patched Cpp2IL, Unity 6000.0.66 and private .NET 6/8 dependencies in project cache.'
    Write-Host "Plan: back up existing files and $(@($saveDirs).Count) detected save folder(s), then install $BridgeDll."
    Write-Host "Plan: write private bridge configuration: $configPath"
    Write-Host 'Plan: preserve existing token; generate a secure random token if absent. No game launch or security settings changes.'
    return
}
if (-not (Test-Path -LiteralPath $BridgeDll -PathType Leaf)) { throw 'Build the bridge first with scripts/build-mod.ps1.' }
# Complete downloads/builds before touching the game. Never execute upstream MSI scripts.
$dependencyOutput = & (Join-Path $PSScriptRoot 'prepare-loader.ps1') -ProjectRoot $root -Offline:$Offline
$payload = [string](@($dependencyOutput)[-1])
if (-not (Test-LoaderPayload -Payload $payload)) { throw 'Loader preparation did not return a verified payload.' }
$payloadManifest = Get-Content -LiteralPath (Join-Path $payload 'kingdom-ai-loader-ready.json') -Raw | ConvertFrom-Json
Assert-GameStopped
$existingManifest = $null
if (Test-Path -LiteralPath $manifestPath) { $existingManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json }
if ($existingManifest -and $existingManifest.game_dir -ne $game) { throw 'Existing install manifest belongs to another game directory.' }
$session = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
$backupRoot = Resolve-ContainedPath -Root $game -Relative ("UserData\KingdomAI\backups\$session")
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
foreach ($dir in $saveDirs) {
    # Refuse a linked save folder; copy normal local saves without loading their contents.
    if (((Get-Item -LiteralPath $dir).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Refusing a linked save directory.' }
    if (Get-ChildItem -LiteralPath $dir -Recurse -Force | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 } | Select-Object -First 1) { throw 'Refusing a save directory containing a link or junction.' }
    $saveTarget = Join-Path $backupRoot ('saves\' + (Split-Path -Leaf $dir))
    New-Item -ItemType Directory -Path (Split-Path -Parent $saveTarget) -Force | Out-Null
    Copy-Item -LiteralPath $dir -Destination $saveTarget -Recurse -Force
}
$records = [Collections.Generic.List[object]]::new()
if ($existingManifest) { foreach ($record in $existingManifest.files) { $records.Add($record) } }
function Save-Manifest {
    $manifest = @{ format=1; game_dir=$game; installed_at=(Get-Date -Format o); loader='0.7.3'; game_version='2.4.2'; game_build='116fe7e048'; unity='6000.0.66'; files=@($records.ToArray()) }
    Write-Utf8File -Path $manifestPath -Content ($manifest | ConvertTo-Json -Depth 8)
}
function Install-File {
    param([string]$Source, [string]$Relative, [string]$ExpectedSha256)
    Assert-GameStopped
    Assert-OwnedInstallPath -Relative $Relative
    $sourceHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
    if ($ExpectedSha256 -and $sourceHash -ne $ExpectedSha256) { throw "Source changed after payload verification: $Relative" }
    $dest = Resolve-ContainedPath -Root $game -Relative $Relative
    $record = @($records | Where-Object { $_.relative -eq $Relative }) | Select-Object -First 1
    if (-not $record) {
        $previous = $null
        if (Test-Path -LiteralPath $dest) {
            $previous = "UserData\KingdomAI\backups\$session\files\$Relative"
            $backup = Resolve-ContainedPath -Root $game -Relative $previous
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $dest -Destination $backup -Force
        }
        $record = [pscustomobject]@{relative=$Relative; original_backup=$previous; installed_sha256=$null}
        $records.Add($record)
    } elseif ((Test-Path -LiteralPath $dest) -and $record.installed_sha256 -and (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash -ne $record.installed_sha256) {
        throw "A previously installed file has changed externally; preserving it: $Relative"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
    $record.installed_sha256 = $sourceHash
    Save-Manifest
    Copy-Item -LiteralPath $Source -Destination $dest -Force
    if ((Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash -ne $record.installed_sha256) { throw "Installed file verification failed: $Relative" }
}
foreach ($file in $payloadManifest.files) {
    $source = Resolve-ContainedPath -Root $payload -Relative $file.relative
    Install-File -Source $source -Relative $file.relative -ExpectedSha256 $file.sha256
}
Install-File -Source $BridgeDll -Relative 'Mods\KingdomAI.Bridge.dll'
$token = $null
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.token -match '^[A-Za-z0-9_-]{40,128}$') { $token = $config.token }
    else { throw 'Existing bridge config has an invalid token. Preserve it and resolve the configuration before reinstalling.' }
}
if (-not $token) {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
}
# Config is private. Never print its content, put it on a command line, or ship it in a ZIP.
$temporaryConfig = Join-Path $root ('.cache\bridge-install-' + [guid]::NewGuid().ToString('N') + '.local.json')
try {
    Write-Utf8File -Path $temporaryConfig -Content (@{base_url=$BaseUrl.TrimEnd('/');token=$token} | ConvertTo-Json)
    Install-File -Source $temporaryConfig -Relative 'UserData\KingdomAI\bridge.local.json'
} finally {
    if (Test-Path -LiteralPath $temporaryConfig) { Remove-Item -LiteralPath $temporaryConfig -Force }
}
Write-Host "Installed P2 bridge. Backups: $backupRoot"
Write-Host "Start the backend with --bridge-config `"$configPath`". Launch the game manually, join P2, and enable AI in the companion UI."
Write-Host 'The installer did not launch the game or enter a save.'
