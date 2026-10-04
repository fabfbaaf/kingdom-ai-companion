<#
.SYNOPSIS
  Refresh only Il2CppInterop corresponding source/license and loader manifest.
  No game files, runtime binaries or generated interop are changed.
#>
[CmdletBinding()]
param([string]$Payload, [switch]$Offline)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'script-common.ps1')
$root = Split-Path -Parent $PSScriptRoot
if (-not $Payload) { $Payload = Join-Path $root '.cache\loader-payload-v0.7.3-unity6000.0.66' }
$Payload = [IO.Path]::GetFullPath($Payload).TrimEnd('\')
$ready = Resolve-ContainedPath -Root $Payload -Relative 'kingdom-ai-loader-ready.json'
$manifest = Get-Content -LiteralPath $ready -Raw | ConvertFrom-Json
if ($manifest.format -ne 1 -or $manifest.loader -ne '0.7.3' -or $manifest.unity -ne '6000.0.66') { throw 'Unsupported loader payload manifest.' }
$commit = 'f03c8f4ae507d47ea814f3d11d1ec6b0391c1576'
$sourceName = "Il2CppInterop-$commit-source.zip"
$archive = Join-Path $root ('.cache\' + $sourceName)
if (-not (Test-Path -LiteralPath $archive)) {
    if ($Offline) { throw 'The exact CI corresponding source archive is missing from the offline cache.' }
    New-Item -ItemType Directory -Path (Split-Path -Parent $archive) -Force | Out-Null
    Invoke-WebRequest -Uri "https://github.com/BepInEx/Il2CppInterop/archive/$commit.zip" -OutFile $archive -UseBasicParsing -TimeoutSec 90
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '85C68282251B4FE86C864EA02F2A107CE04BEF07AD39C2246D02516F0E4C475D') { throw 'Il2CppInterop source archive checksum mismatch.' }
# Verify all previously recorded files before refreshing any legal material.
foreach ($entry in $manifest.files) {
    Assert-OwnedInstallPath -Relative $entry.relative
    $source = Resolve-ContainedPath -Root $Payload -Relative $entry.relative
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $entry.sha256) { throw "Payload changed before source refresh: $($entry.relative)" }
}
$legalRelative = 'MelonLoader\Documentation\third-party\Il2CppInterop'
$legal = Resolve-ContainedPath -Root $Payload -Relative $legalRelative
Copy-Item -LiteralPath $archive -Destination (Join-Path $legal $sourceName) -Force
Copy-Item -LiteralPath (Join-Path $root 'third_party\Il2CppInterop\LICENSE.CI') -Destination (Join-Path $legal 'LICENSE.CI') -Force
# Keep an old stable-tag archive on disk as recoverable material, but do not
# represent it as corresponding source or include it in install/package manifests.
$oldSource = $legalRelative + '\Il2CppInterop-v1.5.1-source.zip'
$files = [Collections.Generic.List[object]]::new()
foreach ($entry in $manifest.files) {
    if ($entry.relative -eq $oldSource -or $entry.relative -eq ($legalRelative + '\LICENSE.CI') -or $entry.relative -eq ($legalRelative + '\' + $sourceName)) { continue }
    $files.Add($entry)
}
foreach ($relative in @(($legalRelative + '\LICENSE.CI'), ($legalRelative + '\' + $sourceName))) {
    $path = Resolve-ContainedPath -Root $Payload -Relative $relative
    $files.Add([pscustomobject]@{relative=$relative;sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash})
}
$manifest.files = @($files.ToArray())
$manifest | Add-Member -MemberType NoteProperty -Name interop_source -Value $commit -Force
Write-Utf8File -Path $ready -Content ($manifest | ConvertTo-Json -Depth 6)
if (-not (Test-LoaderPayload -Payload $Payload)) { throw 'Source-refreshed payload failed validation.' }
Write-Host "Corresponding source refreshed for commit $commit; runtime binaries unchanged."
