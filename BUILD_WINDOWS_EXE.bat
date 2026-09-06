@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build TorrentCreator Windows EXE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" -Mode Single
if errorlevel 1 (
  echo.
  echo BUILD FAILED. Read the error message above.
  pause
  exit /b 1
)
echo.
echo DONE. The EXE is located at dist-windows\single\TorrentCreator.exe
pause
