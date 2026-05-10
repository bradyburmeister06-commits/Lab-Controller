@echo off
REM ============================================================================
REM stop-collector.bat
REM
REM Stops a Lab-Controller collector started by start-collector.bat. Prefers
REM the PID file written by the start script. Falls back to looking for the
REM uvicorn process bound to TCP port 8001.
REM ============================================================================

setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "PORT=8001"
set "PID_FILE=.collector.pid"

echo.
echo === Stopping Lab-Controller collector ===
echo.

set "TARGET_PID="

if exist "%PID_FILE%" (
    set /p TARGET_PID=<"%PID_FILE%"
    if defined TARGET_PID (
        echo Found PID file: %PID_FILE% -> PID !TARGET_PID!
    )
)

if not defined TARGET_PID (
    echo No PID file found; searching for a Python process listening on port %PORT% ...
    for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /R /C:":%PORT% .*LISTENING"') do (
        set "TARGET_PID=%%P"
    )
)

if not defined TARGET_PID (
    echo No collector process found (PID file missing and nothing listening on port %PORT%).
    echo Nothing to do.
    if exist "%PID_FILE%" del /q "%PID_FILE%" >nul 2>&1
    endlocal & exit /b 0
)

echo Stopping PID %TARGET_PID% ...
taskkill /PID %TARGET_PID% /T /F
set "EC=%ERRORLEVEL%"

if exist "%PID_FILE%" del /q "%PID_FILE%" >nul 2>&1

if %EC%==0 (
    echo Collector stopped.
) else (
    echo [WARN] taskkill returned exit code %EC%. The process may already be gone.
)

endlocal & exit /b 0
