@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build TorrentCreator Portable Windows Version
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" -Mode Portable
if errorlevel 1 (
  echo.
  echo BUILD FAILED. Read the error message above.
  pause
  exit /b 1
)
echo.
echo DONE. The ZIP file is located under dist-windows\portable\
pause
