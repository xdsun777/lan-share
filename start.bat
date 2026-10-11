@echo off
rem ============================================================
rem  LAN Share - Windows start script
rem  Double-click to run, or: start.bat --port 9000 --password 1234
rem  (Keep this file ASCII-only: cmd.exe parses .bat in OEM codepage,
rem   non-ASCII text would be garbled on Chinese Windows.)
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PY_BIN="
if defined PYTHON set "PY_BIN=%PYTHON%"
if not defined PY_BIN if exist "%~dp0venv\Scripts\python.exe" set "PY_BIN=%~dp0venv\Scripts\python.exe"
if not defined PY_BIN (
  where python >nul 2>nul && set "PY_BIN=python"
)
if not defined PY_BIN (
  where py >nul 2>nul && set "PY_BIN=py -3"
)
if not defined PY_BIN (
  echo [ERROR] Python not found.
  echo Please install Python 3.8+ from https://www.python.org/downloads/
  echo and check "Add Python to PATH" during installation.
  pause
  exit /b 1
)

echo Interpreter: %PY_BIN%
echo.
%PY_BIN% server.py %*

echo.
echo Server stopped.
pause
