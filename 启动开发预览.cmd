@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" goto install
"%~dp0.venv\Scripts\python.exe" "%~dp0environment_check.py" --quick >nul 2>&1
if errorlevel 1 goto install
start "" /d "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
exit /b 0
:install
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装环境.ps1" -Launch
if errorlevel 1 (
  echo 安装或启动没有完成，请查看上面的提示。
  pause
  exit /b 1
)
exit /b 0
