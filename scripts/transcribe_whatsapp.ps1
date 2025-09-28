<#
.SYNOPSIS
    Transcribe WhatsApp voice notes using OpenAI Whisper.

.DESCRIPTION
    This script wraps ffmpeg and the openai-whisper CLI to provide an easy
    way to transcribe WhatsApp ``.opus`` files.  It can optionally preprocess
    the audio (denoise, trim silence, normalise), install missing
    dependencies, and pick a sensible default model for you.

.EXAMPLE
    .\transcribe_whatsapp.ps1 "C:\path\to\audio.opus" -AutoDeps -AutoModel -TrimSilence

    Transcribes the supplied file, automatically installing dependencies and
    enabling the trim-silence pre-processing step.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateScript({ Test-Path $_ -PathType Leaf })]
    [string]$InputFile,

    [string]$OutputDirectory,

    [switch]$AutoDeps,
    [switch]$GPU,
    [switch]$AutoModel,

    [switch]$Denoise,
    [switch]$TrimSilence,
    [switch]$Normalize,

    [string]$Model = "small",
    [string]$Language
)

$ErrorActionPreference = 'Stop'

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Resolve-Binary {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    return $null
}

function Ensure-PathContains {
    param(
        [string]$PathToAdd
    )

    if ([string]::IsNullOrWhiteSpace($PathToAdd)) {
        return
    }

    $normalized = [System.IO.Path]::GetFullPath($PathToAdd)
    $pathParts = $env:Path -split ';'
    if ($pathParts -notcontains $normalized) {
        $env:Path = ($pathParts + $normalized) -join ';'
    }
}

function Ensure-FFmpeg {
    if (Resolve-Binary 'ffmpeg') {
        return $true
    }

    if (-not $AutoDeps) {
        Write-Warn 'ffmpeg konnte nicht gefunden werden. Installiere es manuell (z.B. "winget install Gyan.FFmpeg").'
        return $false
    }

    Write-Info 'Versuche ffmpeg über winget zu installieren …'
    try {
        winget install --exact --id Gyan.FFmpeg -h
    }
    catch {
        Write-Warn 'Automatische ffmpeg-Installation fehlgeschlagen. Bitte manuell installieren.'
        return $false
    }

    $ffmpeg = Resolve-Binary 'ffmpeg'
    if (-not $ffmpeg) {
        Write-Warn 'ffmpeg ist nach der Installation weiterhin nicht auffindbar.'
        return $false
    }

    Ensure-PathContains (Split-Path $ffmpeg -Parent)
    return $true
}

function Ensure-Python {
    $python = Resolve-Binary 'python'
    if ($python) {
        return $python
    }

    if (-not $AutoDeps) {
        Write-Warn 'Python wurde nicht gefunden. Installiere es manuell von https://www.python.org/downloads/.'
        return $null
    }

    Write-Info 'Python wird über winget installiert …'
    try {
        winget install --exact --id Python.Python.3 -h
    }
    catch {
        Write-Warn 'Automatische Python-Installation fehlgeschlagen. Bitte manuell installieren.'
        return $null
    }

    $python = Resolve-Binary 'python'
    if (-not $python) {
        Write-Warn 'Python ist nach der Installation weiterhin nicht auffindbar.'
        return $null
    }

    Ensure-PathContains (Split-Path $python -Parent)
    return $python
}

function Ensure-Whisper {
    param([string]$Python)

    if (Resolve-Binary 'whisper') {
        return $true
    }

    if (-not $Python) {
        return $false
    }

    Write-Info 'Installiere/aktualisiere openai-whisper …'
    try {
        & $Python -m pip install --upgrade pip | Out-Null
        & $Python -m pip install --upgrade openai-whisper | Out-Null
    }
    catch {
        Write-Warn "Installation von openai-whisper schlug fehl: $($_.Exception.Message)"
        return $false
    }

    $whisperPath = Resolve-Binary 'whisper'
    if ($whisperPath) {
        Ensure-PathContains (Split-Path $whisperPath -Parent)
        return $true
    }

    Write-Warn 'whisper konnte nach der Installation nicht gefunden werden.'
    return $false
}

function Get-ModelName {
    param(
        [string]$RequestedModel,
        [switch]$Auto,
        [switch]$UseGpu
    )

    if (-not $Auto) {
        return $RequestedModel
    }

    if ($UseGpu) {
        return 'medium'
    }

    return 'small'
}

function Preprocess-Audio {
    param(
        [string]$Input,
        [string]$Output,
        [switch]$EnableDenoise,
        [switch]$EnableTrimSilence,
        [switch]$EnableNormalize
    )

    $filters = @()
    if ($EnableDenoise) {
        $filters += 'afftdn=nf=-25'
    }
    if ($EnableTrimSilence) {
        $filters += 'silenceremove=start_periods=1:start_threshold=-35dB:start_silence=0.3:stop_periods=1:stop_threshold=-35dB:stop_silence=0.6'
    }
    if ($EnableNormalize) {
        $filters += 'dynaudnorm'
    }

    if ($filters.Count -eq 0) {
        Copy-Item -LiteralPath $Input -Destination $Output -Force
        return
    }

    $filterChain = ($filters -join ',')
    Write-Info "Preprocessing Audio mit Filter: $filterChain"

    $ffmpegArgs = @(
        '-y',
        '-i', $Input,
        '-ac', '1',
        '-ar', '16000',
        '-af', $filterChain,
        $Output
    )

    $process = Start-Process -FilePath 'ffmpeg' -ArgumentList $ffmpegArgs -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "ffmpeg fehlgeschlagen (ExitCode: $($process.ExitCode))"
    }
}

function Invoke-Whisper {
    param(
        [string]$AudioPath,
        [string]$Destination,
        [string]$ModelName,
        [switch]$UseGpu,
        [string]$LanguageOverride
    )

    $arguments = @(
        $AudioPath,
        '--model', $ModelName,
        '--output_dir', $Destination
    )

    if ($UseGpu) {
        $arguments += @('--device', 'cuda')
    }

    if ($LanguageOverride) {
        $arguments += @('--language', $LanguageOverride)
    }

    Write-Info "Starte Transkription mit Modell '$ModelName' …"

    $process = Start-Process -FilePath 'whisper' -ArgumentList $arguments -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Whisper beendete sich mit ExitCode $($process.ExitCode)."
    }
}

Write-Info 'Überprüfe Eingabedatei …'
$resolvedInput = (Resolve-Path -LiteralPath $InputFile).ProviderPath

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path -Path (Split-Path $resolvedInput -Parent) -ChildPath 'transcripts'
}

if (-not (Test-Path $OutputDirectory)) {
    Write-Info "Erstelle Ausgabeverzeichnis: $OutputDirectory"
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}

$ffmpegReady = Ensure-FFmpeg
$pythonPath = Ensure-Python
$whisperReady = Ensure-Whisper -Python $pythonPath

if (-not $ffmpegReady -or -not $pythonPath -or -not $whisperReady) {
    throw 'Abbruch: Erforderliche Abhängigkeiten konnten nicht bereitgestellt werden.'
}

$modelToUse = Get-ModelName -RequestedModel $Model -Auto:$AutoModel -UseGpu:$GPU

$tempFile = Join-Path -Path ([System.IO.Path]::GetTempPath()) -ChildPath ([System.IO.Path]::GetRandomFileName() + '.wav')
try {
    Preprocess-Audio -Input $resolvedInput -Output $tempFile -EnableDenoise:$Denoise -EnableTrimSilence:$TrimSilence -EnableNormalize:$Normalize

    Invoke-Whisper -AudioPath $tempFile -Destination $OutputDirectory -ModelName $modelToUse -UseGpu:$GPU -LanguageOverride $Language

    Write-Host '[DONE] Transkription abgeschlossen.' -ForegroundColor Green
    Write-Info "Ausgaben befinden sich in: $OutputDirectory"
}
finally {
    if (Test-Path $tempFile) {
        Remove-Item $tempFile -ErrorAction SilentlyContinue
    }
}
