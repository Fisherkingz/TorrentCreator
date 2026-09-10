param(
    [string]$TargetDir = ""
)

$ErrorActionPreference = "Stop"
$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$NewVersion = "3.4.0"

function Get-TorrentCreatorVersion {
    param([string]$Root)
    $source = Join-Path $Root "torrent_creator.py"
    if (-not (Test-Path $source)) { return "unknown" }
    $text = Get-Content $source -Raw -ErrorAction SilentlyContinue
    $m = [regex]::Match($text, 'APP_VERSION\s*=\s*[''"]([^''"]+)[''"]')
    if ($m.Success) { return $m.Groups[1].Value }
    return "unknown"
}

function Select-TargetFolder {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "Select your EXISTING TorrentCreator project folder"
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw "Update cancelled. No existing TorrentCreator folder was selected."
    }
    return $dialog.SelectedPath
}

Write-Host ""
Write-Host "=== TorrentCreator In-Place Updater ===" -ForegroundColor Cyan
Write-Host "New version: $NewVersion"
Write-Host ""

if (-not $TargetDir) {
    $TargetDir = Select-TargetFolder
}
$TargetDir = [System.IO.Path]::GetFullPath($TargetDir)
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)

if ($TargetDir.TrimEnd('\\') -eq $SourceRoot.TrimEnd('\\')) {
    throw "Choose your existing TorrentCreator folder, not the newly downloaded update folder."
}
if (-not (Test-Path (Join-Path $TargetDir "torrent_creator.py"))) {
    throw "The selected folder does not look like a TorrentCreator project: torrent_creator.py is missing."
}
if (-not (Test-Path (Join-Path $TargetDir "build_windows.ps1"))) {
    throw "The selected folder does not look like a build-kit installation: build_windows.ps1 is missing."
}

$running = Get-Process -Name "TorrentCreator" -ErrorAction SilentlyContinue
if ($running) {
    throw "TorrentCreator is currently running. Close the application completely and run the updater again."
}

$OldVersion = Get-TorrentCreatorVersion -Root $TargetDir
Write-Host "Current version: $OldVersion"
Write-Host "Target folder:   $TargetDir"
Write-Host ""

# Files that form the program/build project. Cached runtimes, venv, build output,
# generated media, and user settings are deliberately not touched.
$ProjectFiles = @(
    ".gitignore",
    "BUILD_PORTABLE.bat",
    "BUILD_WINDOWS_EXE.bat",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.txt",
    "build_windows.ps1",
    "requirements-build.txt",
    "requirements.txt",
    "torrent_creator.py",
    "version_info.txt",
    "UPDATE_TORRENTCREATOR.bat",
    "update_existing.ps1"
)

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupRoot = Join-Path $TargetDir ".updates\backup-$OldVersion-$stamp"
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
$createdFiles = New-Object System.Collections.Generic.List[string]
$distSingle = Join-Path $TargetDir "dist-windows\single"
$distBackup = Join-Path $BackupRoot "dist-single"
$distWasMoved = $false

try {
    Write-Host "Backing up current project files..." -ForegroundColor Yellow
    foreach ($relative in $ProjectFiles) {
        $old = Join-Path $TargetDir $relative
        if (Test-Path $old) {
            $backup = Join-Path $BackupRoot $relative
            $parent = Split-Path -Parent $backup
            if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            Copy-Item $old $backup -Force
        } else {
            $createdFiles.Add($relative)
        }
    }

    # Move the current EXE/output directory aside rather than copying its large
    # bundled VLC/FFmpeg payload. Moving on the same drive is fast and allows rollback.
    if (Test-Path $distSingle) {
        Write-Host "Preserving the current Windows build until the new build succeeds..." -ForegroundColor Yellow
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $distBackup) | Out-Null
        Move-Item $distSingle $distBackup -Force
        $distWasMoved = $true
    }

    Write-Host "Updating project files in place..." -ForegroundColor Yellow
    foreach ($relative in $ProjectFiles) {
        $src = Join-Path $SourceRoot $relative
        if (-not (Test-Path $src)) {
            throw "The update package is incomplete: missing $relative"
        }
        $dest = Join-Path $TargetDir $relative
        $parent = Split-Path -Parent $dest
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item $src $dest -Force
    }

    Write-Host ""
    Write-Host "Preserved:" -ForegroundColor Green
    Write-Host "  .build-tools\ffmpeg"
    Write-Host "  .build-tools\vlc"
    Write-Host "  .build-tools\venv"
    Write-Host "  %APPDATA%\TorrentCreator settings"
    Write-Host "  Windows Credential Manager credentials"
    Write-Host ""
    Write-Host "Building the updated TorrentCreator.exe..." -ForegroundColor Yellow

    Push-Location $TargetDir
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $TargetDir "build_windows.ps1") -Mode Single
        if ($LASTEXITCODE -ne 0) { throw "The Windows build failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }

    $newExe = Join-Path $TargetDir "dist-windows\single\TorrentCreator.exe"
    if (-not (Test-Path $newExe)) {
        throw "The update build finished without creating dist-windows\single\TorrentCreator.exe."
    }

    # New build is good; old binary can now be removed from the rollback area.
    if ($distWasMoved -and (Test-Path $distBackup)) {
        Remove-Item $distBackup -Recurse -Force
        $distWasMoved = $false
    }

    Write-Host ""
    Write-Host "UPDATE COMPLETE" -ForegroundColor Green
    Write-Host "TorrentCreator $OldVersion -> $NewVersion"
    Write-Host "New EXE: $newExe"
    Write-Host "Backup of previous source files: $BackupRoot"
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "UPDATE FAILED - restoring the previous version..." -ForegroundColor Red

    # Restore project files that existed before the update.
    foreach ($relative in $ProjectFiles) {
        $backup = Join-Path $BackupRoot $relative
        $dest = Join-Path $TargetDir $relative
        if (Test-Path $backup) {
            $parent = Split-Path -Parent $dest
            if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            Copy-Item $backup $dest -Force
        } elseif ($createdFiles.Contains($relative) -and (Test-Path $dest)) {
            Remove-Item $dest -Force
        }
    }

    # Restore the previous dist-windows\single directory when it was moved aside.
    if ($distWasMoved -and (Test-Path $distBackup)) {
        if (Test-Path $distSingle) { Remove-Item $distSingle -Recurse -Force }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $distSingle) | Out-Null
        Move-Item $distBackup $distSingle -Force
        $distWasMoved = $false
    }

    Write-Host "Previous project files restored." -ForegroundColor Yellow
    Write-Host $_.Exception.Message -ForegroundColor Red
    throw
}
