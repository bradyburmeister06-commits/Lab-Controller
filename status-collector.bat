@echo off
REM ============================================================================
REM status-collector.bat
REM
REM Reports whether the Lab-Controller collector is running locally, prints
REM its configured COLLECTOR_ID and HUB_BASE_URL, hits the local health
REM endpoint, and (if curl is available) checks hub reachability.
REM ============================================================================

setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "PORT=8001"
set "PID_FILE=.collector.pid"

echo.
echo === Lab-Controller collector status ===
echo Repo root: %CD%
echo.

REM ---- Read .env --------------------------------------------------------------
if exist ".env" (
    for /f "usebackq tokens=1* delims==" %%A in (".env") do (
        set "_K=%%A"
        set "_V=%%B"
        if not "!_K!"=="" if not "!_K:~0,1!"=="#" (
            if defined _V (
                set "_V=!_V:"=!"
            )
            set "!_K!=!_V!"
        )
    )
    echo .env summary:
    echo   APP_MODE          = %APP_MODE%
    echo   COLLECTOR_ID      = %COLLECTOR_ID%
    echo   COLLECTOR_NAME    = %COLLECTOR_NAME%
    echo   HUB_BASE_URL      = %HUB_BASE_URL%
    echo   RELAY_CONTROLLER  = %RELAY_CONTROLLER%
) else (
    echo [WARN] .env not found in this folder.
)
echo.

REM ---- Local process check ----------------------------------------------------
set "FOUND="
if exist "%PID_FILE%" (
    set /p PID_FROM_FILE=<"%PID_FILE%"
    if defined PID_FROM_FILE (
        tasklist /FI "PID eq !PID_FROM_FILE!" /NH 2>nul | findstr /I "python" >nul
        if !ERRORLEVEL!==0 (
            echo Collector process: RUNNING (PID !PID_FROM_FILE! from %PID_FILE%)
            set "FOUND=1"
        ) else (
            echo PID file lists !PID_FROM_FILE! but that process is not alive.
        )
    )
)

if not defined FOUND (
    for /f "tokens=5" %%P in ('netstat -ano -p TCP 2^>nul ^| findstr /R /C:":%PORT% .*LISTENING"') do (
        echo Collector process: RUNNING (PID %%P listening on TCP :%PORT%)
        set "FOUND=1"
    )
)

if not defined FOUND (
    echo Collector process: NOT RUNNING (no PID file, nothing on TCP :%PORT%)
)
echo.

REM ---- Local health probe -----------------------------------------------------
where curl >nul 2>&1
if %ERRORLEVEL%==0 (
    echo Local health probe -- http://127.0.0.1:%PORT%/api/health
    curl -fsS --max-time 3 "http://127.0.0.1:%PORT%/api/health"
    if errorlevel 1 (
        echo.
        echo [WARN] Local /api/health did not respond.
    )
    echo.
    echo.
    if not "%HUB_BASE_URL%"=="" (
        echo Hub reachability probe -- %HUB_BASE_URL%/api/health
        curl -fsS --max-time 5 "%HUB_BASE_URL%/api/health"
        if errorlevel 1 (
            echo.
            echo [WARN] Hub /api/health did not respond. Check Tailscale and HUB_BASE_URL.
        )
        echo.
    )
) else (
    echo curl not found on PATH; skipping HTTP probes.
    echo Install curl (built into Windows 10+) or test manually:
    echo   http://127.0.0.1:%PORT%/api/health
    if not "%HUB_BASE_URL%"=="" echo   %HUB_BASE_URL%/api/health
)

endlocal & exit /b 0
