@echo off
REM ============================================================
REM  CCHQ Pipeline - start the web app (Windows)
REM  Double-click this file, then open http://localhost:5050
REM ============================================================
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Local environment not found. Double-click setup.bat first.
  echo.
  pause
  exit /b 1
)

cd /d "%~dp0app"
echo Starting the app...  open  http://localhost:5050  in your browser.
echo (Leave this window open. Close it or press Ctrl+C to stop.)
echo.
"%~dp0.venv\Scripts\python.exe" app.py
pause
