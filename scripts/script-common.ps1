# Shared filesystem checks. This file does not perform installation on import.
$ErrorActionPreference = 'Stop'

function Resolve-ContainedPath {
    param([string]$Root, [string]$Relative)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if ([IO.Path]::IsPathRooted($Relative)) { throw 'Expected a relative path.' }
    if ($Relative.Contains(':')) { throw 'Refusing a drive or alternate stream in a relative path.' }
    $full = [IO.Path]::GetFullPath((Join-Path $rootFull $Relative))
    if (-not $full.StartsWith($rootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes its intended directory: $Relative"
    }
    $cursor = $full
    while ($cursor -and $cursor.Length -ge $rootFull.Length) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing a link or junction: $cursor" }
        }
        $next = Split-Path -Parent $cursor
        if ($next -eq $cursor) { break }
        $cursor = $next
    }
    return $full
}

function Assert-GameStopped {
    # Do not kill a user's running game. Installation must wait for it to close.
    if (Get-Process -Name 'KingdomTwoCrowns' -ErrorAction SilentlyContinue) {
        throw 'Kingdom Two Crowns is running. Close the game before installing or uninstalling.'
    }
}

function Assert-OwnedInstallPath {
    param([string]$Relative)
    if ($Relative -eq 'version.dll' -or $Relative -eq 'Mods\KingdomAI.Bridge.dll' -or
        $Relative -eq 'UserData\KingdomAI\bridge.local.json' -or
        $Relative.StartsWith('MelonLoader\', [StringComparison]::OrdinalIgnoreCase)) { return }
    throw "Not a Kingdom AI installer-owned path: $Relative"
}

function Resolve-KingdomGame {
    param([string]$GameDir)
    if (-not $GameDir) { throw 'Pass the game directory with -GameDir.' }
    $full = (Resolve-Path -LiteralPath $GameDir).Path.TrimEnd('\')
    foreach ($relative in @('KingdomTwoCrowns.exe', 'GameAssembly.dll', 'UnityPlayer.dll', 'KingdomTwoCrowns_Data\il2cpp_data\Metadata\global-metadata.dat')) {
        $item = Resolve-ContainedPath -Root $full -Relative $relative
        if (-not (Test-Path -LiteralPath $item -PathType Leaf)) { throw "Not a supported Windows IL2CPP game directory: missing $relative" }
    }
    return $full
}

function Assert-SupportedKingdomBuild {
    param([string]$GameDir)
    $infoPath = Resolve-ContainedPath -Root $GameDir -Relative 'BuildInfo.txt'
    if (-not (Test-Path -LiteralPath $infoPath -PathType Leaf)) { throw 'BuildInfo.txt is missing; game compatibility cannot be confirmed.' }
    $info = Get-Content -LiteralPath $infoPath -Raw
    if ($info -notmatch '(?m)^Version:\s*2\.4\.2\s*$' -or $info -notmatch '(?m)^Hash:\s*116fe7e048\s*$') {
        throw 'This bridge supports only game 2.4.2 / build 116fe7e048. Other builds require a new compatibility review.'
    }
    $unityPath = Resolve-ContainedPath -Root $GameDir -Relative 'UnityPlayer.dll'
    if (-not (Test-Path -LiteralPath $unityPath -PathType Leaf)) { throw 'UnityPlayer.dll is missing; Unity compatibility cannot be confirmed.' }
    $unity = [Diagnostics.FileVersionInfo]::GetVersionInfo($unityPath).FileVersion
    if ($unity -notmatch '^6000\.0\.66\.') { throw 'This bridge supports Unity 6000.0.66 only.' }
}

function Expand-SafeZip {
    param([string]$Archive, [string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            if ([string]::IsNullOrEmpty($entry.FullName)) { continue }
            $relative = $entry.FullName.Replace('/', '\')
            $target = Resolve-ContainedPath -Root $Destination -Relative $relative
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) {
                New-Item -ItemType Directory -Path $target -Force | Out-Null
            } else {
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
            }
        }
    } finally { $zip.Dispose() }
}

function Write-Utf8File {
    param([string]$Path, [string]$Content)
    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

function Test-LoaderPayload {
    param([string]$Payload)
    $ready = Join-Path $Payload 'kingdom-ai-loader-ready.json'
    if (-not (Test-Path -LiteralPath $ready -PathType Leaf)) { return $false }
    try {
        $manifest = Get-Content -LiteralPath $ready -Raw | ConvertFrom-Json
        if ($manifest.format -ne 1 -or $manifest.loader -ne '0.7.3' -or $manifest.unity -ne '6000.0.66' -or
            $manifest.interop_source -ne 'f03c8f4ae507d47ea814f3d11d1ec6b0391c1576' -or @($manifest.files).Count -lt 10) { return $false }
        $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($file in $manifest.files) {
            if (-not $seen.Add([string]$file.relative) -or $file.sha256 -notmatch '^[A-Fa-f0-9]{64}$') { return $false }
            if ($file.relative -match '(?i)(Il2CppAssemblies|cpp2il_out|Assembly-CSharp|GameAssembly|global-metadata|bridge\.local\.json)') { return $false }
            Assert-OwnedInstallPath -Relative $file.relative
            $source = Resolve-ContainedPath -Root $Payload -Relative $file.relative
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { return $false }
            if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $file.sha256) { return $false }
        }
        foreach ($required in @('version.dll','MelonLoader\net6\MelonLoader.dll','MelonLoader\Dependencies\Il2CppAssemblyGenerator\Cpp2IL\Cpp2IL.exe','MelonLoader\Dependencies\Il2CppAssemblyGenerator\UnityDependencies\UnityEngine.CoreModule.dll','MelonLoader\Dependencies\dotnet\shared\Microsoft.NETCore.App\6.0.36\System.Private.CoreLib.dll','MelonLoader\Documentation\third-party\Il2CppInterop\COPYING','MelonLoader\Documentation\third-party\Il2CppInterop\COPYING.LESSER','MelonLoader\Documentation\third-party\Il2CppInterop\LICENSE.CI','MelonLoader\Documentation\third-party\Il2CppInterop\Il2CppInterop-f03c8f4ae507d47ea814f3d11d1ec6b0391c1576-source.zip','MelonLoader\Documentation\third-party\HarmonyX\LICENSE','MelonLoader\Documentation\third-party\dotnet8\LICENSE.txt','MelonLoader\Documentation\third-party\dotnet8\ThirdPartyNotices.txt')) {
            if (-not $seen.Contains($required)) { return $false }
        }
        return $true
    } catch { return $false }
}
