@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title TorrentCreator In-Place Updater

echo.
echo TorrentCreator In-Place Updater
echo This updates your EXISTING TorrentCreator folder and keeps FFmpeg, VLC and settings.
echo.

if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_existing.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_existing.ps1" -TargetDir "%~1"
)

if errorlevel 1 (
  echo.
  echo UPDATE FAILED. The updater attempted to restore the previous version.
  pause
  exit /b 1
)

echo.
echo Update finished successfully.
pause
