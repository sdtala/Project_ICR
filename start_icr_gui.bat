@echo off
setlocal
cd /d "%~dp0"
title ICR Site Selector

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo ERROR: The project virtual environment was not found.
  echo Expected: %CD%\.venv\Scripts\python.exe
  echo.
  echo Recreate it with: python -m venv .venv
  pause
  exit /b 1
)

echo.
echo Starting ICR Site Selector...
echo The browser will open at http://127.0.0.1:5000/
echo Press Ctrl+C in this window to stop the application.
echo.

".venv\Scripts\python.exe" -m icr_analysis.web %*
set "ICR_EXIT_CODE=%ERRORLEVEL%"

if not "%ICR_EXIT_CODE%"=="0" (
  echo.
  echo The ICR application stopped with error code %ICR_EXIT_CODE%.
  pause
)

exit /b %ICR_EXIT_CODE%
