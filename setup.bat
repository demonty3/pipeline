@echo off
REM ============================================================
REM  CCHQ Pipeline - one-time setup (Windows)
REM  Double-click this file. It builds a local Python
REM  environment and installs everything the app needs.
REM ============================================================
cd /d "%~dp0"

echo.
echo === CCHQ Pipeline setup ===
echo.

REM --- find a usable Python (3.10-3.13; avoid brand-new 3.14) ---
set "PY="
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY ( py -3.13 --version >nul 2>nul && set "PY=py -3.13" )
if not defined PY ( py -3.11 --version >nul 2>nul && set "PY=py -3.11" )
if not defined PY ( py -3.10 --version >nul 2>nul && set "PY=py -3.10" )
if not defined PY ( python --version >nul 2>nul && set "PY=python" )
if not defined PY (
  echo Could not find Python.
  echo Install Python 3.12 from:
  echo     https://www.python.org/downloads/
  echo IMPORTANT: on the first installer screen, tick "Add python.exe to PATH".
  echo Then double-click setup.bat again.
  echo.
  pause
  exit /b 1
)

echo Using %PY%
%PY% --version

REM --- create the virtual environment ---
if not exist ".venv\Scripts\python.exe" (
  echo Creating local environment in .venv ...
  %PY% -m venv .venv
)

REM --- install dependencies ---
echo Installing dependencies (this can take a minute) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r app\requirements.txt
if errorlevel 1 (
  echo.
  echo Dependency install failed - check your internet connection and run setup.bat again.
  pause
  exit /b 1
)

REM --- seed the .env file ---
if not exist "app\.env" (
  copy "app\.env.example" "app\.env" >nul
  echo Created app\.env from the template.
)

echo.
echo === Setup complete ===
echo.
echo Next steps:
echo   1. Open  app\.env  in Notepad and paste your CH_API_KEY.
echo   2. Double-click  run.bat  to start the app.
echo   3. In your browser, go to  http://localhost:5050
echo.
pause
