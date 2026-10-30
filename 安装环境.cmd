@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装环境.ps1"
if errorlevel 1 (
  echo 环境安装没有完成，请查看上面的提示。
  pause
  exit /b 1
)
pause
exit /b 0
