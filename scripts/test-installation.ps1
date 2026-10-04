<#
.SYNOPSIS
  Offline installer regression in a new temporary, synthetic game directory.
.NOTES
  Uses inert payload files and a generated Unity version-resource DLL. No game
  binary, real game path, real save folder or real loader executable is used.
  The copied installer changes only save-folder discovery to a fixture folder.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'script-common.ps1')
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('kingdom-ai-install-regression-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
$workspace = Resolve-ContainedPath -Root $fixtureRoot -Relative 'project'
$game = Resolve-ContainedPath -Root $fixtureRoot -Relative 'fake-game'
$fixtureScripts = Join-Path $workspace 'scripts'
New-Item -ItemType Directory -Path $fixtureScripts -Force | Out-Null
foreach ($name in @('script-common.ps1','prepare-loader.ps1','install-mod.ps1','uninstall-mod.ps1')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $fixtureScripts $name)
}
$installer = Join-Path $fixtureScripts 'install-mod.ps1'
$source = Get-Content -LiteralPath $installer -Raw
$oldSaveDiscovery = '$saveBase = Join-Path ([Environment]::GetFolderPath(''UserProfile'')) ''AppData\LocalLow\noio'''
if (-not $source.Contains($oldSaveDiscovery)) { throw 'Installer save discovery changed; review the fixture isolation substitution.' }
Write-Utf8File -Path $installer -Content $source.Replace($oldSaveDiscovery, '$saveBase = Join-Path $root ''fixture-saves''')
$assertions = [Collections.Generic.List[string]]::new()
function Assert-Regression {
    param([bool]$Condition, [string]$Description)
    if (-not $Condition) { throw "Installer regression failed: $Description" }
    $assertions.Add($Description)
}
function Write-FixtureFile {
    param([string]$Root, [string]$Relative, [string]$Content)
    Write-Utf8File -Path (Resolve-ContainedPath -Root $Root -Relative $Relative) -Content $Content
}
foreach ($relative in @('KingdomTwoCrowns.exe','GameAssembly.dll','KingdomTwoCrowns_Data\il2cpp_data\Metadata\global-metadata.dat')) {
    Write-FixtureFile -Root $game -Relative $relative -Content 'inert fake game marker; never execute'
}
Write-FixtureFile -Root $game -Relative 'BuildInfo.txt' -Content "Version: 2.4.2`r`nHash: 116fe7e048`r`n"
# Create our own DLL with a Windows version resource; do not copy Unity.
# The SDK is used only to generate this test fixture, not by the installer.
$fixtureBuild = Join-Path $fixtureRoot 'fake-unity-version'
$fixtureProject = Join-Path $fixtureBuild 'FixtureUnity.csproj'
Write-Utf8File -Path $fixtureProject -Content '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework><FileVersion>6000.0.66.0</FileVersion><AssemblyName>FixtureUnity</AssemblyName><NuGetAudit>false</NuGetAudit></PropertyGroup></Project>'
Write-Utf8File -Path (Join-Path $fixtureBuild 'FixtureUnity.cs') -Content 'public class InertSyntheticUnityVersionMarker {}'
$fixtureDotnet = Join-Path $projectRoot '.tools\dotnet\dotnet.exe'
if (-not (Test-Path -LiteralPath $fixtureDotnet)) { throw 'The regression fixture needs the project .NET SDK to generate a synthetic version resource.' }
& $fixtureDotnet build $fixtureProject --nologo -c Release -m:1 -p:RestoreIgnoreFailedSources=true | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Cannot create the synthetic Unity version-resource fixture.' }
Copy-Item -LiteralPath (Join-Path $fixtureBuild 'bin\Release\net8.0\FixtureUnity.dll') -Destination (Join-Path $game 'UnityPlayer.dll')
Assert-SupportedKingdomBuild -GameDir $game
Assert-Regression -Condition $true -Description 'Synthetic supported build passes version, hash and Unity check'
$originalVersion = 'original version.dll bytes'
$originalBridge = 'original unrelated bridge.dll bytes'
Write-FixtureFile -Root $game -Relative 'version.dll' -Content $originalVersion
Write-FixtureFile -Root $game -Relative 'Mods\KingdomAI.Bridge.dll' -Content $originalBridge
Write-FixtureFile -Root $game -Relative 'Mods\OtherMod.dll' -Content 'unrelated mod must survive'
Write-FixtureFile -Root $game -Relative 'UserData\player-notes.txt' -Content 'user data must survive'
$save = Join-Path $workspace 'fixture-saves\KingdomTwoCrowns\slot-one.sav'
Write-Utf8File -Path $save -Content 'inert save marker; not a game save'
$saveHash = (Get-FileHash -LiteralPath $save -Algorithm SHA256).Hash
$bridge = Join-Path $workspace 'artifacts\mod\KingdomAI.Bridge.dll'
Write-Utf8File -Path $bridge -Content 'self-owned fake bridge; never execute'
$payload = Join-Path $workspace '.cache\loader-payload-v0.7.3-unity6000.0.66'
$required = @(
    'version.dll',
    'MelonLoader\net6\MelonLoader.dll',
    'MelonLoader\Dependencies\Il2CppAssemblyGenerator\Cpp2IL\Cpp2IL.exe',
    'MelonLoader\Dependencies\Il2CppAssemblyGenerator\UnityDependencies\UnityEngine.CoreModule.dll',
    'MelonLoader\Dependencies\dotnet\shared\Microsoft.NETCore.App\6.0.36\System.Private.CoreLib.dll',
    'MelonLoader\Documentation\third-party\Il2CppInterop\COPYING',
    'MelonLoader\Documentation\third-party\Il2CppInterop\COPYING.LESSER',
    'MelonLoader\Documentation\third-party\Il2CppInterop\LICENSE.CI',
    'MelonLoader\Documentation\third-party\Il2CppInterop\Il2CppInterop-f03c8f4ae507d47ea814f3d11d1ec6b0391c1576-source.zip',
    'MelonLoader\Documentation\third-party\HarmonyX\LICENSE',
    'MelonLoader\Documentation\third-party\dotnet8\LICENSE.txt',
    'MelonLoader\Documentation\third-party\dotnet8\ThirdPartyNotices.txt',
    'MelonLoader\fixture\owned.txt'
)
$entries = foreach ($relative in $required) {
    Write-FixtureFile -Root $payload -Relative $relative -Content ('inert fixture: ' + $relative)
    @{relative=$relative;sha256=(Get-FileHash -LiteralPath (Join-Path $payload $relative) -Algorithm SHA256).Hash}
}
Write-FixtureFile -Root $payload -Relative 'MelonLoader\fixture\unlisted.txt' -Content 'must never be installed'
Write-Utf8File -Path (Join-Path $payload 'kingdom-ai-loader-ready.json') -Content (@{format=1;loader='0.7.3';unity='6000.0.66';interop_source='f03c8f4ae507d47ea814f3d11d1ec6b0391c1576';files=@($entries)} | ConvertTo-Json -Depth 6)
Assert-Regression -Condition (Test-LoaderPayload -Payload $payload) -Description 'Inert manifest verifies and requires no SDK or download'
$beforeDryRun = @(Get-ChildItem -LiteralPath $game -Recurse -File).Count
& $installer -GameDir $game -Offline -DryRun
Assert-Regression -Condition (@(Get-ChildItem -LiteralPath $game -Recurse -File).Count -eq $beforeDryRun) -Description 'DryRun writes no game files'
& $installer -GameDir $game -Offline
$configPath = Join-Path $game 'UserData\KingdomAI\bridge.local.json'
$manifestPath = Join-Path $game 'UserData\KingdomAI\install-manifest.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$firstToken = $config.token
Assert-Regression -Condition ($firstToken -match '^[A-Za-z0-9_-]{43}$') -Description 'First install generates a 32-byte base64url token'
Assert-Regression -Condition ($config.base_url -eq 'http://127.0.0.1:48861') -Description 'Bridge address remains loopback'
Assert-Regression -Condition (-not (Test-Path -LiteralPath (Join-Path $game 'MelonLoader\fixture\unlisted.txt'))) -Description 'Only manifest files are installed'
$firstManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$firstVersionRecord = @($firstManifest.files | Where-Object {$_.relative -eq 'version.dll'})[0]
$originalVersionBackup = $firstVersionRecord.original_backup
Assert-Regression -Condition ((Get-Content -LiteralPath (Join-Path $game $originalVersionBackup) -Raw) -eq $originalVersion) -Description 'First install backs up overwritten original'
$saveBackups = @(Get-ChildItem -LiteralPath (Join-Path $game 'UserData\KingdomAI\backups') -Recurse -File -Filter 'slot-one.sav')
Assert-Regression -Condition ($saveBackups.Count -eq 1 -and (Get-FileHash -LiteralPath $saveBackups[0].FullName -Algorithm SHA256).Hash -eq $saveHash) -Description 'Detected fixture save is backed up byte-for-byte'
& $installer -GameDir $game -Offline
$secondConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$secondManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$secondVersionRecord = @($secondManifest.files | Where-Object {$_.relative -eq 'version.dll'})[0]
Assert-Regression -Condition ($secondConfig.token -eq $firstToken) -Description 'Reinstall preserves the existing token'
Assert-Regression -Condition ($secondVersionRecord.original_backup -eq $originalVersionBackup) -Description 'Reinstall preserves the first original backup'
Assert-Regression -Condition (@($secondManifest.files).Count -eq @($firstManifest.files).Count) -Description 'Reinstall does not duplicate ownership records'
Assert-Regression -Condition ((Get-FileHash -LiteralPath $save -Algorithm SHA256).Hash -eq $saveHash) -Description 'Both installs leave save contents unchanged'
Write-FixtureFile -Root $game -Relative 'MelonLoader\fixture\owned.txt' -Content 'external edit must survive'
Write-FixtureFile -Root $game -Relative 'MelonLoader\latest-runtime.log' -Content 'new log must survive'
$beforeUninstallHash = (Get-FileHash -LiteralPath (Join-Path $game 'version.dll') -Algorithm SHA256).Hash
& (Join-Path $fixtureScripts 'uninstall-mod.ps1') -GameDir $game -DryRun
Assert-Regression -Condition ((Get-FileHash -LiteralPath (Join-Path $game 'version.dll') -Algorithm SHA256).Hash -eq $beforeUninstallHash) -Description 'Uninstall DryRun leaves installed files unchanged'
& (Join-Path $fixtureScripts 'uninstall-mod.ps1') -GameDir $game
Assert-Regression -Condition ((Get-Content -LiteralPath (Join-Path $game 'version.dll') -Raw) -eq $originalVersion) -Description 'Uninstall restores original version.dll after reinstall'
Assert-Regression -Condition ((Get-Content -LiteralPath (Join-Path $game 'Mods\KingdomAI.Bridge.dll') -Raw) -eq $originalBridge) -Description 'Uninstall restores the overwritten original bridge file'
Assert-Regression -Condition ((Get-Content -LiteralPath (Join-Path $game 'MelonLoader\fixture\owned.txt') -Raw) -eq 'external edit must survive') -Description 'Uninstall preserves an externally edited owned file'
Assert-Regression -Condition (Test-Path -LiteralPath (Join-Path $game 'MelonLoader\latest-runtime.log')) -Description 'Uninstall preserves newly created runtime logs'
Assert-Regression -Condition ((Get-FileHash -LiteralPath $save -Algorithm SHA256).Hash -eq $saveHash) -Description 'Uninstall leaves saves unchanged'
Assert-Regression -Condition (Test-Path -LiteralPath (Join-Path $game $originalVersionBackup)) -Description 'Uninstall keeps the original backup material'
Assert-Regression -Condition (Test-Path -LiteralPath (Join-Path $game 'Mods\OtherMod.dll')) -Description 'Uninstall preserves unrelated mods'
Assert-Regression -Condition (Test-Path -LiteralPath (Join-Path $game 'UserData\player-notes.txt')) -Description 'Uninstall preserves unrelated user data'
Assert-Regression -Condition (-not (Test-Path -LiteralPath $configPath)) -Description 'Uninstall removes unchanged generated token config'
$remaining = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
Assert-Regression -Condition (@($remaining.files).Count -eq 1 -and $remaining.files[0].relative -eq 'MelonLoader\fixture\owned.txt') -Description 'Partial uninstall manifest tracks only preserved external modification'
$rejected = $false
Write-FixtureFile -Root $game -Relative 'BuildInfo.txt' -Content "Version: 2.4.2`r`nHash: 0000000000`r`n"
try { & $installer -GameDir $game -Offline -DryRun } catch {$rejected=$true}
Assert-Regression -Condition $rejected -Description 'Same game version with an unknown build hash is rejected'
$rejected = $false
Write-FixtureFile -Root $game -Relative 'BuildInfo.txt' -Content "Version: 2.4.3`r`nHash: 116fe7e048`r`n"
try { & $installer -GameDir $game -Offline -DryRun } catch {$rejected=$true}
Assert-Regression -Condition $rejected -Description 'Unsupported game version is rejected'
Write-FixtureFile -Root $game -Relative 'BuildInfo.txt' -Content "Version: 2.4.2`r`nHash: 116fe7e048`r`n"
Write-FixtureFile -Root $game -Relative 'UnityPlayer.dll' -Content 'not a Unity 6000.0.66 version-resource DLL'
$rejected = $false
try { & $installer -GameDir $game -Offline -DryRun } catch {$rejected=$true}
Assert-Regression -Condition $rejected -Description 'Missing or unsupported Unity version metadata is rejected'
$report = @{passed=$assertions.Count;assertions=@($assertions.ToArray());fixture_root=$fixtureRoot;scope='Synthetic directory only; original game and saves untouched; no loader execution';installer_sha256=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'install-mod.ps1') -Algorithm SHA256).Hash;uninstaller_sha256=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'uninstall-mod.ps1') -Algorithm SHA256).Hash}
Write-Utf8File -Path (Join-Path $fixtureRoot 'regression-result.json') -Content ($report | ConvertTo-Json -Depth 6)
Write-Host "Installer regression: $($assertions.Count) assertions passed."
Write-Host "Fixture and token-free report preserved at $fixtureRoot"
