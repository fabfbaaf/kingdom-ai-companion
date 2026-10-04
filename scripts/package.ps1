<#
.SYNOPSIS
  Package only project-owned source, documentation, scripts and the standalone bridge.
#>
[CmdletBinding()]
param([string]$Version = '0.1.0-dev', [switch]$SourceOnly, [switch]$IncludeApp, [switch]$IncludeLoaderPayload, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'script-common.ps1')
$root = Split-Path -Parent $PSScriptRoot
if ($Version -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$') { throw 'Invalid package version.' }
$bridge = Join-Path $root 'artifacts\mod\KingdomAI.Bridge.dll'
if (-not $SourceOnly -and -not (Test-Path -LiteralPath $bridge)) { throw 'Build the bridge before packaging, or pass -SourceOnly.' }
$roots = @('companion','mod','scripts','docs','third_party','tests')
$rootFiles = @('README.md','LICENSE','THIRD_PARTY_NOTICES.md','pyproject.toml','requirements.txt','requirements.lock.txt','.gitignore','install.cmd','start.cmd','launcher.py')
if ($SourceOnly -and ($IncludeApp -or $IncludeLoaderPayload)) { throw 'SourceOnly cannot include binary app or loader payload.' }
$files = [Collections.Generic.List[object]]::new()
foreach ($dir in $roots) {
    $source = Join-Path $root $dir
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { continue }
    foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File -Force) {
        $relative = $file.FullName.Substring($root.Length + 1)
        if ($relative -match '(^|[\\/])(bin|obj|\.cache|\.tools|\.git|\.venv|__pycache__|node_modules|refs|Il2CppAssemblies|UserData|save-backups)([\\/]|$)') { continue }
        if ($relative -match '\.(dll|exe|pdb|db|sqlite|log|local\.json|pyc|key|pem|pfx|jks)$' -or $file.Name -eq '.env') { continue }
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing linked package source: $relative" }
        $files.Add([pscustomobject]@{source=$file.FullName;relative=$relative})
    }
}
foreach ($name in $rootFiles) {
    $source = Join-Path $root $name
    if (Test-Path -LiteralPath $source -PathType Leaf) { $files.Add([pscustomobject]@{source=$source;relative=$name}) }
}
if (-not $SourceOnly) { $files.Add([pscustomobject]@{source=$bridge;relative='artifacts\mod\KingdomAI.Bridge.dll'}) }
if ($IncludeApp) {
    $portableExe = Join-Path $root 'artifacts\app\KingdomAI.exe'
    if (-not (Test-Path -LiteralPath $portableExe -PathType Leaf)) { throw 'Build the portable app first with scripts/build-app.ps1.' }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'third_party\python\LICENSES.json') -PathType Leaf)) { throw 'Preserve app dependency licenses first: .venv\Scripts\python.exe scripts\collect-app-licenses.py' }
    $files.Add([pscustomobject]@{source=$portableExe;relative='KingdomAI.exe'})
}
if ($IncludeLoaderPayload) {
    $payload = Join-Path $root '.cache\loader-payload-v0.7.3-unity6000.0.66'
    if (-not (Test-LoaderPayload -Payload $payload)) { throw 'Prepared loader payload is missing or corrupt. Run prepare-loader.ps1 first.' }
    $payloadManifest = Get-Content -LiteralPath (Join-Path $payload 'kingdom-ai-loader-ready.json') -Raw | ConvertFrom-Json
    foreach ($entry in $payloadManifest.files) {
        if ($entry.relative -match '(?i)(Il2CppAssemblies|cpp2il_out|Assembly-CSharp|GameAssembly|global-metadata|bridge\.local\.json)') { throw "Game-derived or private file in prepared payload: $($entry.relative)" }
        $files.Add([pscustomobject]@{source=(Resolve-ContainedPath -Root $payload -Relative $entry.relative);relative=('dependencies\loader-payload\' + $entry.relative)})
    }
    $files.Add([pscustomobject]@{source=(Join-Path $payload 'kingdom-ai-loader-ready.json');relative='dependencies\loader-payload\kingdom-ai-loader-ready.json'})
}
$zip = Join-Path $root ("dist\kingdom-ai-companion-$Version.zip")
if ($DryRun) { Write-Host "Would package $($files.Count) explicitly selected files to $zip; excludes game DLLs, generated interop, caches, logs and local credentials."; return }
if (Test-Path -LiteralPath $zip) { throw 'Package already exists. Choose another version or preserve/move the old artifact first.' }
$stage = Join-Path $root ('artifacts\package-' + [guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $stage 'kingdom-ai-companion'
$manifest = [Collections.Generic.List[object]]::new()
foreach ($entry in $files) {
    $dest = Resolve-ContainedPath -Root $packageRoot -Relative $entry.relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
    Copy-Item -LiteralPath $entry.source -Destination $dest
    $manifest.Add([pscustomobject]@{path=$entry.relative.Replace('\','/');sha256=(Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash})
}
Write-Utf8File -Path (Join-Path $packageRoot 'PACKAGE-MANIFEST.json') -Content (@{version=$Version;files=@($manifest.ToArray())} | ConvertTo-Json -Depth 6)
New-Item -ItemType Directory -Path (Split-Path -Parent $zip) -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip)
Write-Host "Package created: $zip"
Write-Host ('SHA-256: ' + (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash)
