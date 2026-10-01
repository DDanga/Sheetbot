@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Sheetbot
set PYTHONUTF8=1

where python >/dev/null 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.12+ from python.org and tick "Add Python to PATH".
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo First run: setting things up, this takes a minute...
  python -m venv .venv
  if errorlevel 1 goto fail
)

.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail

.venv\Scripts\python.exe -m sheetbot
echo.
pause
exit /b 0

:fail
echo.
echo Setup failed, see the messages above.
pause
exit /b 1
