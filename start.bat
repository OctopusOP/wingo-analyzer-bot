@echo off
REM ===========================================================================
REM start.bat - sets up and starts the WinGo Alert app (Windows)
REM Double-click this file, or run:
REM
REM     start.bat            open the window
REM     start.bat --server   no window: watch + send alerts in this console
REM                          (stop with Ctrl+C)
REM
REM   1. Finds Python 3.8+  (install from python.org, tick "Add to PATH")
REM   2. Creates a private Python environment in ".venv" (first run only)
REM   3. Installs the packages listed in requirements.txt
REM   4. Starts the app (window) or server.py (console)
REM ===========================================================================

REM Always work inside the folder this script lives in
cd /d "%~dp0"

REM ---- 1. find Python ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.8+ not found. Install it from https://www.python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

REM ---- 2. private environment ----
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment in .venv ^(first run only^)...
  %PY% -m venv .venv || (echo Could not create .venv & pause & exit /b 1)
)

REM ---- 3. packages (pip skips ones that are already installed) ----
echo Checking packages from requirements.txt...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
  echo Package install failed. Check your internet connection.
  pause
  exit /b 1
)

REM ---- 4a. server mode: python.exe in THIS console, logs shown here ----
if /i "%~1"=="--server" (
  echo Starting WinGo Alert in server mode ^(Ctrl+C to stop^)...
  ".venv\Scripts\python.exe" server.py
  exit /b %errorlevel%
)

REM ---- 4b. window mode (pythonw = no extra black console window) ----
echo Starting WinGo Alert...
start "" ".venv\Scripts\pythonw.exe" app.py
