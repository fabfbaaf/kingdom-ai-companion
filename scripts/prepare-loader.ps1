<#
.SYNOPSIS
  Prepare pinned loader dependencies in this project's cache. Never launches or writes to the game.
.NOTES
  Cpp2IL patch functions adapted from FredApps/KingdomMod at
  c0f1fd9fdafc2b12b8680ebbd15eb2b2c9734b86 (MIT, KingdomMod contributors).
  See third_party/KingdomMod/LICENSE and THIRD_PARTY_NOTICES.md.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Dotnet,
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$cache = Join-Path $ProjectRoot '.cache'
if (-not $Dotnet) { $Dotnet = Join-Path $ProjectRoot '.tools\dotnet\dotnet.exe' }
$Cpp2IlVersion = '2022.1.0-pre-release.21'
$InteropCommit = 'f03c8f4ae507d47ea814f3d11d1ec6b0391c1576'
$srcRoot = Join-Path $cache 'Cpp2IL-src'
$payload = Join-Path $cache 'loader-payload-v0.7.3-unity6000.0.66'
New-Item -ItemType Directory -Path $cache -Force | Out-Null
. (Join-Path $PSScriptRoot 'script-common.ps1')

function Get-DependencyFile {
    param([string]$Uri, [string]$OutFile, [string]$Sha256, [string]$Sha512)
    if (-not (Test-Path -LiteralPath $OutFile)) {
        if ($Offline) { throw "Offline cache missing: $OutFile" }
        $partial = $OutFile + '.download'
        $downloaded = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Invoke-WebRequest -Uri $Uri -OutFile $partial -UseBasicParsing -TimeoutSec 90
                if ((Get-Item -LiteralPath $partial).Length -eq 0) { throw 'Downloaded dependency is empty.' }
                $downloaded = $true
                break
            } catch {
                if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds 2
            }
        }
        if (-not $downloaded) { throw "Dependency download failed: $Uri" }
        Move-Item -LiteralPath $partial -Destination $OutFile -Force
    }
    if ((Get-Item -LiteralPath $OutFile).Length -eq 0) { throw "Empty dependency: $OutFile" }
    if ($Sha256 -and (Get-FileHash -LiteralPath $OutFile -Algorithm SHA256).Hash -ne $Sha256) { throw "Dependency SHA-256 mismatch: $OutFile" }
    if ($Sha512 -and (Get-FileHash -LiteralPath $OutFile -Algorithm SHA512).Hash -ne $Sha512) { throw "Dependency SHA-512 mismatch: $OutFile" }
}

function Replace-ExactText {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Old,
        [Parameter(Mandatory=$true)][string]$New
    )

    $text = Get-Content -LiteralPath $Path -Raw
    if ($text.Contains($New)) { return }
    if (-not $text.Contains($Old)) {
        throw "Could not apply KingdomMod Cpp2IL patch to '$Path'; expected source text was not found."
    }
    Set-Content -LiteralPath $Path -Value ($text.Replace($Old, $New)) -Encoding UTF8 -NoNewline
}

function Replace-RegexText {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Pattern,
        [Parameter(Mandatory=$true)][string]$Replacement,
        [Parameter(Mandatory=$true)][string]$Description,
        [string]$AlreadyPatchedPattern
    )

    $text = Get-Content -LiteralPath $Path -Raw
    if ($AlreadyPatchedPattern -and $text -match $AlreadyPatchedPattern) { return }
    $newText = [regex]::Replace($text, $Pattern, $Replacement, 1)
    if ($newText -eq $text) {
        throw "Could not apply KingdomMod Cpp2IL patch to '$Path'; $Description was not found."
    }
    Set-Content -LiteralPath $Path -Value $newText -Encoding UTF8 -NoNewline
}

function Set-TargetFrameworkText {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Element,
        [Parameter(Mandatory=$true)][string]$Value
    )

    $text = Get-Content -LiteralPath $Path -Raw
    $pattern = "<$Element>[^<]+</$Element>"
    $replacement = "<$Element>$Value</$Element>"
    if ($text -match [regex]::Escape($replacement)) { return }
    if ($text -notmatch $pattern) {
        throw "Could not find <$Element> in '$Path'."
    }
    Set-Content -LiteralPath $Path -Value ([regex]::Replace($text, $pattern, $replacement, 1)) -Encoding UTF8 -NoNewline
}

function Set-XmlElementText {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Element,
        [Parameter(Mandatory=$true)][string]$Value
    )

    $text = Get-Content -LiteralPath $Path -Raw
    $pattern = "<$Element>[^<]*</$Element>"
    $replacement = "<$Element>$Value</$Element>"
    if ($text -match [regex]::Escape($replacement)) { return }
    if ($text -match $pattern) {
        Set-Content -LiteralPath $Path -Value ([regex]::Replace($text, $pattern, $replacement, 1)) -Encoding UTF8 -NoNewline
        return
    }

    $insertPattern = '</PropertyGroup>'
    if ($text -notmatch $insertPattern) {
        throw "Could not find a PropertyGroup in '$Path' while setting <$Element>."
    }
    $insert = "        <$Element>$Value</$Element>`r`n    </PropertyGroup>"
    Set-Content -LiteralPath $Path -Value ([regex]::Replace($text, $insertPattern, $insert, 1)) -Encoding UTF8 -NoNewline
}

function Set-GlobalJsonSdk {
    param([Parameter(Mandatory=$true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        Set-Content -LiteralPath $Path -Value "{`r`n  `"sdk`": {`r`n    `"version`": `"8.0.0`",`r`n    `"rollForward`": `"latestMajor`",`r`n    `"allowPrerelease`": false`r`n  }`r`n}`r`n" -Encoding UTF8 -NoNewline
        return
    }

    $json = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if (-not $json.sdk) {
        $json | Add-Member -MemberType NoteProperty -Name sdk -Value ([pscustomobject]@{})
    }
    $json.sdk | Add-Member -MemberType NoteProperty -Name version -Value '8.0.0' -Force
    $json.sdk | Add-Member -MemberType NoteProperty -Name rollForward -Value 'latestMajor' -Force
    if ($null -eq $json.sdk.allowPrerelease) {
        $json.sdk | Add-Member -MemberType NoteProperty -Name allowPrerelease -Value $false
    }
    Set-Content -LiteralPath $Path -Value ($json | ConvertTo-Json -Depth 8) -Encoding UTF8
}

function Ensure-Cpp2IlSource {
    $project = Join-Path $srcRoot 'Cpp2IL\Cpp2IL.csproj'
    if (Test-Path -LiteralPath $project) { return }
    $zip = Join-Path $cache "Cpp2IL-$Cpp2IlVersion.zip"
    Get-DependencyFile -Uri "https://github.com/SamboyCoding/Cpp2IL/archive/refs/tags/$Cpp2IlVersion.zip" -OutFile $zip -Sha256 '845139032578bbdf7c5afcb8196a819e2def53b7794ef7c89701314c7a7266db'
    $extract = Join-Path $cache ('cpp2il-extract-' + [guid]::NewGuid().ToString('N'))
    Expand-SafeZip -Archive $zip -Destination $extract
    $expanded = Get-ChildItem -LiteralPath $extract -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'Cpp2IL\Cpp2IL.csproj') } | Select-Object -First 1
    if (-not $expanded) { throw 'Cpp2IL source archive did not contain the expected project.' }
    Move-Item -LiteralPath $expanded.FullName -Destination $srcRoot
    [IO.File]::WriteAllText((Join-Path $srcRoot 'Directory.Build.props'), '<Project />')
}

function Apply-KingdomModCpp2IlPatch {
    Ensure-Cpp2IlSource

    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'Cpp2IL.Core.Tests\Cpp2IL.Core.Tests.csproj') -Element 'TargetFramework' -Value 'net8.0'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'Cpp2IL.Plugin.BuildReport\Cpp2IL.Plugin.BuildReport.csproj') -Element 'TargetFramework' -Value 'net8.0'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'Cpp2IL.Plugin.ControlFlowGraph\Cpp2IL.Plugin.ControlFlowGraph.csproj') -Element 'TargetFramework' -Value 'net8.0'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'Cpp2IL.Plugin.Pdb\Cpp2IL.Plugin.Pdb.csproj') -Element 'TargetFramework' -Value 'net8.0'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'Cpp2IL.Plugin.StrippedCodeRegSupport\Cpp2IL.Plugin.StrippedCodeRegSupport.csproj') -Element 'TargetFramework' -Value 'net8.0'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'LibCpp2ILTests\LibCpp2ILTests.csproj') -Element 'TargetFramework' -Value 'net8.0'

    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'Cpp2IL.Core\Cpp2IL.Core.csproj') -Element 'TargetFrameworks' -Value 'net8.0;net7.0;net6.0;netstandard2.0'
    Set-XmlElementText -Path (Join-Path $srcRoot 'Cpp2IL.Core\Cpp2IL.Core.csproj') -Element 'LangVersion' -Value 'preview'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'Cpp2IL\Cpp2IL.csproj') -Element 'TargetFrameworks' -Value 'net8.0;net472'
    Set-XmlElementText -Path (Join-Path $srcRoot 'Cpp2IL\Cpp2IL.csproj') -Element 'RollForward' -Value 'LatestMajor'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'LibCpp2IL\LibCpp2IL.csproj') -Element 'TargetFrameworks' -Value 'net8.0;net7.0;net6.0;netstandard2.0'
    Set-TargetFrameworkText -Path (Join-Path $srcRoot 'WasmDisassembler\WasmDisassembler.csproj') -Element 'TargetFrameworks' -Value 'net8.0;net7.0;net6.0;netstandard2.0'

    Set-GlobalJsonSdk -Path (Join-Path $srcRoot 'global.json')

    Replace-RegexText -Path (Join-Path $srcRoot 'LibCpp2IL\Metadata\Il2CppPropertyDefinition.cs') -Pattern '(?s)\s*public Il2CppTypeReflectionData\? PropertyType => LibCpp2IlMain\.TheMetadata == null \? null : Getter == null \? Setter!\.Parameters!\[0\]\.Type : Getter!\.ReturnType;\s*public Il2CppType\? RawPropertyType => LibCpp2IlMain\.TheMetadata == null \? null : Getter == null \? Setter!\.Parameters!\[0\]\.RawType : Getter!\.RawReturnType;\s*public bool IsStatic => Getter == null \? Setter!\.IsStatic : Getter!\.IsStatic;' -Description 'stripped property metadata null-safety block' -AlreadyPatchedPattern 'Setter\?\.Parameters\?\[0\]\.Type' -Replacement @'

    public Il2CppTypeReflectionData? PropertyType => LibCpp2IlMain.TheMetadata == null ? null : Getter != null ? Getter.ReturnType : Setter?.Parameters?[0].Type;

    public Il2CppType? RawPropertyType => LibCpp2IlMain.TheMetadata == null ? null : Getter != null ? Getter.RawReturnType : Setter?.Parameters?[0].RawType;

    public bool IsStatic => Getter != null ? Getter.IsStatic : Setter?.IsStatic ?? false;
'@

    Replace-RegexText -Path (Join-Path $srcRoot 'Cpp2IL.Core\Utils\AsmResolver\AsmResolverAssemblyPopulator.cs') -Pattern 'foreach\s*\(\s*var\s+property\s+in\s+type\.Properties\s*\)\s*CopyCustomAttributes\s*\(\s*property\s*,\s*property\.GetExtraData<PropertyDefinition>\("AsmResolverProperty"\)!\.CustomAttributes\s*\)\s*;' -Description 'AsmResolver property custom attribute null-safety block' -AlreadyPatchedPattern 'var\s+asmProp\s*=\s*property\.GetExtraData<PropertyDefinition>\("AsmResolverProperty"\)' -Replacement @'
                foreach (var property in type.Properties)
                {
                    var asmProp = property.GetExtraData<PropertyDefinition>("AsmResolverProperty");
                    if (asmProp == null) continue;
                    CopyCustomAttributes(property, asmProp.CustomAttributes);
                }
'@

    Replace-RegexText -Path (Join-Path $srcRoot 'Cpp2IL.Core\Utils\AsmResolver\AsmResolverAssemblyPopulator.cs') -Pattern 'foreach\s*\(\s*var\s+propertyCtx\s+in\s+typeContext\.Properties\s*\)\s*\{\s*var\s+propertyTypeSig\s*=\s*propertyCtx\.ToTypeSignature\(importer\.TargetModule\)\s*;\s*var\s+propertySignature\s*=\s*propertyCtx\.IsStatic' -Description 'AsmResolver property construction null-safety block' -AlreadyPatchedPattern 'propertyCtx\.Getter\s*==\s*null\s*&&\s*propertyCtx\.Setter\s*==\s*null' -Replacement @'
        foreach (var propertyCtx in typeContext.Properties)
        {
            if (propertyCtx.Getter == null && propertyCtx.Setter == null)
                continue;

            var propertyTypeSig = propertyCtx.ToTypeSignature(importer.TargetModule);
            if (propertyTypeSig == null)
                continue;
            var propertySignature = propertyCtx.IsStatic
'@
}


$ready = Join-Path $payload 'kingdom-ai-loader-ready.json'
# Portable packages carry the prepared open-source toolchain, not game-derived interop.
$portable = Join-Path $ProjectRoot 'dependencies\loader-payload'
if (Test-LoaderPayload -Payload $portable) { Write-Output $portable; return }
if (Test-Path -LiteralPath $ready) {
    if (Test-LoaderPayload -Payload $payload) {
        Write-Output $payload
        return
    }
    throw 'Loader dependency cache is incomplete. Rename the incomplete loader-payload directory and prepare again.'
}
if (-not (Test-Path -LiteralPath $Dotnet)) { throw 'Project .NET SDK is missing. Use a portable package with prepared dependencies, install .NET SDK 8 in .tools/dotnet, or pass -Dotnet.' }
$loaderZip = Join-Path $cache 'MelonLoader.x64.zip'
Get-DependencyFile -Uri 'https://github.com/LavaGang/MelonLoader/releases/download/v0.7.3/MelonLoader.x64.zip' -OutFile $loaderZip -Sha256 '5b2b2f3d1cd42b59ec886c5bdc2663edae87a0097a4f4a8f58c0965a99dda416'
Expand-SafeZip -Archive $loaderZip -Destination $payload
$unityZip = Join-Path $cache 'UnityDependencies_6000.0.66.zip'
Get-DependencyFile -Uri 'https://github.com/LavaGang/MelonLoader.UnityDependencies/releases/download/6000.0.66/Managed.zip' -OutFile $unityZip -Sha512 '39D1B7C1D65D8413AC7AFB6B19FD6F5F1BB9D234DE8FCFECB4A5B42D1F23F6A005B788FA739EA5A38116E3C32B86257D437A4D078C724B0256AC1CB9F4DF1F9D'
$gen = Join-Path $payload 'MelonLoader\Dependencies\Il2CppAssemblyGenerator'
Expand-SafeZip -Archive $unityZip -Destination (Join-Path $gen 'UnityDependencies')
Copy-Item -LiteralPath $unityZip -Destination (Join-Path $gen 'UnityDependencies_6000.0.66.zip') -Force

Apply-KingdomModCpp2IlPatch
$source = Join-Path $srcRoot 'Cpp2IL\bin\Release\net8.0'
$pluginSource = Join-Path $srcRoot 'Cpp2IL.Plugin.StrippedCodeRegSupport\bin\Release\net8.0'
$dotnetEnv = $env:DOTNET_ROOT
try {
    $env:DOTNET_ROOT = Split-Path -Parent $Dotnet
    $args = @('build', (Join-Path $srcRoot 'Cpp2IL\Cpp2IL.csproj'), '-c', 'Release', '-f', 'net8.0', '--nologo', '-m:1', '-p:SkipRefsCheck=true')
    if ($Offline) { $args += '--no-restore' }
    & $Dotnet @args | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Patched Cpp2IL build failed.' }
    $args[1] = Join-Path $srcRoot 'Cpp2IL.Plugin.StrippedCodeRegSupport\Cpp2IL.Plugin.StrippedCodeRegSupport.csproj'
    & $Dotnet @args | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Cpp2IL stripped-code-reg plugin build failed.' }
} finally { $env:DOTNET_ROOT = $dotnetEnv }
$cppTarget = Join-Path $gen 'Cpp2IL'
New-Item -ItemType Directory -Path $cppTarget -Force | Out-Null
Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $cppTarget -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $cppTarget 'Plugins') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $pluginSource 'Cpp2IL.Plugin.StrippedCodeRegSupport.dll') -Destination (Join-Path $cppTarget 'Plugins') -Force
Copy-Item -LiteralPath (Join-Path $srcRoot 'LICENSE') -Destination (Join-Path $cppTarget 'LICENSE.Cpp2IL.txt') -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'third_party\KingdomMod\LICENSE') -Destination (Join-Path $cppTarget 'LICENSE.KingdomMod-patch.txt') -Force

# MelonLoader requires .NET 6; the patched Cpp2IL tool requires .NET 8.
# Keep both in its private runtime. Do not install or modify machine-wide runtimes.
$rt6InfoFile = Join-Path $cache 'dotnet6-releases.json'
Get-DependencyFile -Uri 'https://builds.dotnet.microsoft.com/dotnet/release-metadata/6.0/releases.json' -OutFile $rt6InfoFile
$rt6Info = Get-Content -LiteralPath $rt6InfoFile -Raw | ConvertFrom-Json
$release = @($rt6Info.releases | Where-Object { $_.runtime.version -eq '6.0.36' })[0]
$rt6 = @($release.runtime.files | Where-Object { $_.rid -eq 'win-x64' -and $_.name -eq 'dotnet-runtime-win-x64.zip' })[0]
if (-not $rt6 -or -not $rt6.hash) { throw 'Pinned .NET 6 runtime metadata missing.' }
$rt6Zip = Join-Path $cache 'dotnet-runtime-6.0.36-win-x64.zip'
Get-DependencyFile -Uri 'https://dotnetcli.blob.core.windows.net/dotnet/Runtime/6.0.36/dotnet-runtime-6.0.36-win-x64.zip' -OutFile $rt6Zip -Sha512 $rt6.hash
$runtime = Join-Path $payload 'MelonLoader\Dependencies\dotnet'
Expand-SafeZip -Archive $rt6Zip -Destination $runtime
$dotnetRoot = Split-Path -Parent $Dotnet
foreach ($part in @('dotnet.exe', 'host', 'shared\Microsoft.NETCore.App')) {
    $item = Join-Path $dotnetRoot $part
    if (-not (Test-Path -LiteralPath $item)) { throw "SDK runtime component missing: $part" }
    $parent = Split-Path -Parent (Join-Path $runtime $part)
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $item -Destination $parent -Recurse -Force
}
$runtimes = & (Join-Path $runtime 'dotnet.exe') --list-runtimes
if ($LASTEXITCODE -ne 0 -or -not ($runtimes -match 'Microsoft.NETCore.App 6\.') -or -not ($runtimes -match 'Microsoft.NETCore.App 8\.')) { throw 'Private .NET 6/8 runtime preparation failed.' }
# The loader carries Il2CppInterop 1.5.1-ci.845 at $InteropCommit (LGPL-3.0).
# Its stable v1.5.1 tag is different and is not the corresponding source. Preserve
# complete license texts, the corresponding upstream source and replacement rights.
$legal = Join-Path $payload 'MelonLoader\Documentation\third-party'
foreach ($component in @('Il2CppInterop','HarmonyX')) {
    $destination = Join-Path $legal $component
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $ProjectRoot ('third_party\' + $component)) -File | Copy-Item -Destination $destination -Force
}
$interopSourceName = "Il2CppInterop-$InteropCommit-source.zip"
$interopSource = Join-Path $cache $interopSourceName
Get-DependencyFile -Uri "https://github.com/BepInEx/Il2CppInterop/archive/$InteropCommit.zip" -OutFile $interopSource -Sha256 '85c68282251b4fe86c864ea02f2a107ce04bef07ad39c2246d02516f0e4c475d'
Copy-Item -LiteralPath $interopSource -Destination (Join-Path (Join-Path $legal 'Il2CppInterop') $interopSourceName) -Force
$runtime8Legal = Join-Path $legal 'dotnet8'
New-Item -ItemType Directory -Path $runtime8Legal -Force | Out-Null
foreach ($name in @('LICENSE.txt','ThirdPartyNotices.txt')) { Copy-Item -LiteralPath (Join-Path $dotnetRoot $name) -Destination $runtime8Legal -Force }
$payloadFiles = Get-ChildItem -LiteralPath $payload -Recurse -File | Where-Object { $_.FullName -ne $ready } | ForEach-Object {
    @{relative=$_.FullName.Substring($payload.Length+1);sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
}
[IO.File]::WriteAllText($ready, (@{format=1;loader='0.7.3'; unity='6000.0.66'; cpp2il=$Cpp2IlVersion; interop_source=$InteropCommit; patch_source='c0f1fd9fdafc2b12b8680ebbd15eb2b2c9734b86';files=@($payloadFiles)} | ConvertTo-Json -Depth 6))
if (-not (Test-LoaderPayload -Payload $payload)) { throw 'Prepared payload failed its own integrity check.' }
Write-Output $payload
