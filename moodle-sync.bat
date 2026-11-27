@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "TARGET=%SCRIPT_DIR%moodle_sync.py"
set "PYTHON_BIN="

if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    set "PYTHON_BIN=%SCRIPT_DIR%.venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\.venv\Scripts\python.exe" (
    set "PYTHON_BIN=%USERPROFILE%\.venv\Scripts\python.exe"
)

if defined PYTHON_BIN (
    "%PYTHON_BIN%" "%TARGET%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 "%TARGET%" %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>&1
if not errorlevel 1 (
    python "%TARGET%" %*
    exit /b %ERRORLEVEL%
)

echo [FEHLER] Keine Python-Umgebung gefunden (.venv, py oder python im PATH). 1>&2
exit /b 1
