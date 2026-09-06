param(
    [ValidateSet("Single", "Portable", "Both")]
    [string]$Mode = "Single"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "Continue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host ""
Write-Host "=== TorrentCreator - Windows Builder ===" -ForegroundColor Cyan
Write-Host "Build mode: $Mode"
Write-Host ""

function Find-PythonLauncher {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @("py", "-3")
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }
    throw "Python 3 was not found. Install Python 3 from python.org and run BUILD_WINDOWS_EXE.bat again."
}

function Invoke-Python {
    param([string[]]$Arguments)
    $launcher = @(Find-PythonLauncher)
    $exe = $launcher[0]
    $prefix = @()
    if ($launcher.Count -gt 1) { $prefix = $launcher[1..($launcher.Count - 1)] }
    & $exe @prefix @Arguments
    if ($LASTEXITCODE -ne 0) { throw "The Python command failed." }
}

$BuildTools = Join-Path $Root ".build-tools"
$Venv = Join-Path $BuildTools "venv"
$FfmpegDir = Join-Path $BuildTools "ffmpeg"
$FfmpegZip = Join-Path $BuildTools "ffmpeg-release-essentials.zip"
$FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

New-Item -ItemType Directory -Force -Path $BuildTools | Out-Null

# 1) Download FFmpeg only if it is not already available in the build cache.
$ffmpeg = Get-ChildItem -Path $FfmpegDir -Filter "ffmpeg.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
$ffprobe = Get-ChildItem -Path $FfmpegDir -Filter "ffprobe.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1

if (-not $ffmpeg -or -not $ffprobe) {
    Write-Host "Downloading FFmpeg Essentials..." -ForegroundColor Yellow
    if (Test-Path $FfmpegDir) { Remove-Item $FfmpegDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
    $partFile = "$FfmpegZip.part"
    if (Test-Path $partFile) { Remove-Item $partFile -Force }

    # curl.exe shows real download progress and supports retries/timeouts.
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        Write-Host "Downloading FFmpeg (progress is shown below)..." -ForegroundColor Yellow
        & $curl.Source --fail --location --progress-bar --retry 3 --retry-delay 2 --connect-timeout 20 -o $partFile $FfmpegUrl
        if ($LASTEXITCODE -ne 0) { throw "FFmpeg download failed (curl exit code $LASTEXITCODE)." }
    } else {
        Write-Host "curl.exe was not found. Using PowerShell download..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $FfmpegUrl -OutFile $partFile -UseBasicParsing -TimeoutSec 600
    }

    if (-not (Test-Path $partFile)) { throw "The FFmpeg archive was not downloaded." }
    $downloadSize = (Get-Item $partFile).Length
    if ($downloadSize -lt 10MB) {
        throw "The FFmpeg archive appears incomplete ($([math]::Round($downloadSize / 1MB, 1)) MB)."
    }

    Move-Item -Path $partFile -Destination $FfmpegZip -Force
    Write-Host "FFmpeg downloaded: $([math]::Round((Get-Item $FfmpegZip).Length / 1MB, 1)) MB" -ForegroundColor Green
    Write-Host "Extracting FFmpeg..." -ForegroundColor Yellow
    Expand-Archive -Path $FfmpegZip -DestinationPath $FfmpegDir -Force
    $ffmpeg = Get-ChildItem -Path $FfmpegDir -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
    $ffprobe = Get-ChildItem -Path $FfmpegDir -Filter "ffprobe.exe" -Recurse | Select-Object -First 1
}

if (-not $ffmpeg -or -not $ffprobe) {
    throw "Could not find ffmpeg.exe/ffprobe.exe after the download."
}

Write-Host "FFmpeg:  $($ffmpeg.FullName)"
Write-Host "FFprobe: $($ffprobe.FullName)"

# 2) Use a dedicated virtual environment so the user's regular Python installation is not modified.
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Write-Host "Creating temporary Python build environment..." -ForegroundColor Yellow
    Invoke-Python -Arguments @("-m", "venv", $Venv)
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
Write-Host "Installing/updating Pillow, drag & drop support, and PyInstaller..." -ForegroundColor Yellow
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip update failed." }
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }

$PyInstaller = Join-Path $Venv "Scripts\pyinstaller.exe"
$Source = Join-Path $Root "torrent_creator.py"
$VersionInfo = Join-Path $Root "version_info.txt"
$DistRoot = Join-Path $Root "dist-windows"
$BuildRoot = Join-Path $Root "build-windows"
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

function Build-App {
    param(
        [ValidateSet("Single", "Portable")]
        [string]$Kind
    )

    $kindLower = $Kind.ToLowerInvariant()
    $dist = Join-Path $DistRoot $kindLower
    $work = Join-Path $BuildRoot $kindLower
    if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
    if (Test-Path $work) { Remove-Item $work -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    New-Item -ItemType Directory -Force -Path $work | Out-Null

    $pyiArgs = @(
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name", "TorrentCreator",
        "--version-file", $VersionInfo,
        "--distpath", $dist,
        "--workpath", $work,
        "--specpath", $work,
        "--add-binary", "$($ffmpeg.FullName);.",
        "--add-binary", "$($ffprobe.FullName);.",
        "--collect-all", "PIL",
        "--collect-all", "tkinterdnd2"
    )

    if ($Kind -eq "Single") {
        $pyiArgs += "--onefile"
    } else {
        $pyiArgs += "--onedir"
        $pyiArgs += @("--contents-directory", "_internal")
    }

    $pyiArgs += $Source

    Write-Host ""
    Write-Host "Building $Kind version..." -ForegroundColor Cyan
    & $PyInstaller @pyiArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for the $Kind version." }

    Copy-Item (Join-Path $Root "README.md") $dist -Force
    Copy-Item (Join-Path $Root "THIRD_PARTY_NOTICES.txt") $dist -Force

    if ($Kind -eq "Single") {
        $exe = Join-Path $dist "TorrentCreator.exe"
        if (-not (Test-Path $exe)) { throw "The Windows EXE was not created where expected." }
        Write-Host "DONE: $exe" -ForegroundColor Green
    } else {
        $portableFolder = Join-Path $dist "TorrentCreator"
        if (-not (Test-Path (Join-Path $portableFolder "TorrentCreator.exe"))) {
            throw "The portable Windows folder was not created where expected."
        }
        Copy-Item (Join-Path $Root "README.md") $portableFolder -Force
        Copy-Item (Join-Path $Root "THIRD_PARTY_NOTICES.txt") $portableFolder -Force
        $portableZip = Join-Path $dist "TorrentCreator-portable.zip"
        if (Test-Path $portableZip) { Remove-Item $portableZip -Force }
        Compress-Archive -Path "$portableFolder\*" -DestinationPath $portableZip -CompressionLevel Optimal
        Write-Host "DONE: $portableZip" -ForegroundColor Green
    }
}

if ($Mode -eq "Single" -or $Mode -eq "Both") { Build-App -Kind "Single" }
if ($Mode -eq "Portable" -or $Mode -eq "Both") { Build-App -Kind "Portable" }

Write-Host ""
Write-Host "Build complete. Output is located in:" -ForegroundColor Green
Write-Host "  $DistRoot"
Write-Host ""
Write-Host "FFmpeg and FFprobe are bundled with the Windows build. End users do not need to install Python, Pillow, tkinterdnd2, or FFmpeg." -ForegroundColor Green
