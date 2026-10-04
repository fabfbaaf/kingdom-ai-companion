# Fixed local protocol helper. User text arrives only as UTF-8 JSON on stdin.
# GetInstalledVoices / SelectVoice / Speak reference:
# https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$synth = $null
$exitCode = 0
try {
    $buffer = New-Object char[] 8193
    $length = 0
    while ($length -lt $buffer.Length) {
        $read = [Console]::In.Read($buffer, $length, $buffer.Length - $length)
        if ($read -eq 0) { break }
        $length += $read
    }
    if ($length -gt 8192) { throw 'Request too large.' }
    $request = ([string]::new($buffer, 0, $length)) | ConvertFrom-Json
    Add-Type -AssemblyName System.Speech
    $synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
    # Enumerating voices explicitly routes synthesis to nowhere and never opens
    # a microphone or plays a test phrase.
    $synth.SetOutputToNull()
    $voices = @($synth.GetInstalledVoices())
    if ($request.op -ceq 'voices') {
        if ($voices.Count -gt 64) { throw 'Too many voices.' }
        $publicVoices = @($voices | ForEach-Object {
            [ordered]@{
                name = $_.VoiceInfo.Name
                culture = $_.VoiceInfo.Culture.Name
                enabled = [bool]$_.Enabled
            }
        })
        [Console]::Out.WriteLine((@{ ok = $true; voices = $publicVoices } |
            ConvertTo-Json -Depth 4 -Compress))
    } elseif ($request.op -ceq 'say') {
        if ($request.text -isnot [string] -or $request.text.Length -gt 120 -or
            [string]::IsNullOrWhiteSpace($request.text) -or
            $request.voice_name -isnot [string] -or $request.voice_name.Length -gt 200) {
            throw 'Invalid speech request.'
        }
        $matching = @($voices | Where-Object {
            $_.Enabled -and $_.VoiceInfo.Name -ceq $request.voice_name -and
            ($_.VoiceInfo.Culture.Name -match '^zh(?:-|$)')
        })
        if ($matching.Count -ne 1) { throw 'Chinese voice unavailable.' }
        $synth.SelectVoice($request.voice_name)
        # SelectVoice uses substring matching; verify the actual full voice name.
        if ($synth.Voice.Name -cne $request.voice_name -or
            $synth.Voice.Culture.Name -notmatch '^zh(?:-|$)') {
            throw 'Voice selection mismatch.'
        }
        $synth.SetOutputToDefaultAudioDevice()
        $synth.Speak([string]$request.text)
        [Console]::Out.WriteLine('{"ok":true}')
    } else {
        throw 'Unknown operation.'
    }
} catch {
    # Do not return private device/registry paths or the spoken text in errors.
    [Console]::Out.WriteLine('{"ok":false,"error":"local_speech_failed"}')
    $exitCode = 1
} finally {
    if ($null -ne $synth) { $synth.Dispose() }
}
exit $exitCode
