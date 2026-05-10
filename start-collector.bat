@echo off
REM ============================================================================
REM start-collector.bat
REM
REM Starts the Lab-Controller collector on Windows. Performs preflight checks
REM (.env, .venv, hub URL placeholder, collector token, MCC driver), then runs
REM uvicorn on port 8001 and writes a PID file the stop script can use.
REM
REM Run from Command Prompt at the repo root, or via Task Scheduler.
REM ============================================================================

setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "PORT=8001"
set "PID_FILE=.collector.pid"
set "LOG_DIR=logs"
set "LOG_FILE=%LOG_DIR%\collector.log"

echo.
echo === Starting Lab-Controller collector ===
echo Repo root: %CD%
echo.

REM ---- Preflight: .venv -------------------------------------------------------
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv is missing. Run setup-collector-windows.bat first.
    exit /b 1
)

REM ---- Preflight: .env --------------------------------------------------------
if not exist ".env" (
    echo [ERROR] .env is missing. Run setup-collector-windows.bat to create it,
    echo         then edit it before starting the collector.
    exit /b 1
)

REM Read .env into env vars (skip blank lines and comments). This is best-effort:
REM values containing '=' are preserved correctly because we only split on the
REM first '='.
for /f "usebackq tokens=1* delims==" %%A in ("%~dp0.env") do (
    set "_K=%%A"
    set "_V=%%B"
    if not "!_K!"=="" if not "!_K:~0,1!"=="#" (
        REM Strip surrounding double quotes from value if present.
        if defined _V (
            set "_V=!_V:"=!"
        )
        set "!_K!=!_V!"
    )
)

REM ---- Preflight: APP_MODE ----------------------------------------------------
if /I not "%APP_MODE%"=="collector" (
    echo [ERROR] APP_MODE in .env is "%APP_MODE%" but must be "collector"
    echo         on a collector machine. Edit .env and set APP_MODE=collector.
    exit /b 1
)

REM ---- Preflight: HUB_BASE_URL ------------------------------------------------
if "%HUB_BASE_URL%"=="" (
    echo [ERROR] HUB_BASE_URL is empty in .env.
    exit /b 1
)
echo %HUB_BASE_URL% | findstr /I "HOME_TAILSCALE_IP YOUR_HUB_TAILSCALE_IP placeholder localhost 127.0.0.1" >nul
if not errorlevel 1 (
    echo [ERROR] HUB_BASE_URL still contains a placeholder or local-loopback host:
    echo         %HUB_BASE_URL%
    echo         Edit .env and point HUB_BASE_URL at the hub's Tailscale IP/URL,
    echo         e.g. http://100.64.1.10:8000  or  https://lab-hub.ts.net
    exit /b 1
)

REM ---- Preflight: COLLECTOR_API_TOKEN -----------------------------------------
if "%COLLECTOR_API_TOKEN%"=="" (
    echo [ERROR] COLLECTOR_API_TOKEN is empty in .env. It MUST match the hub's value.
    exit /b 1
)
if /I "%COLLECTOR_API_TOKEN%"=="change-me-collector-token" (
    echo [ERROR] COLLECTOR_API_TOKEN is still the default placeholder.
    echo         Generate a long random secret and set the SAME value on the hub.
    exit /b 1
)

REM ---- Preflight: COLLECTOR_ID / COLLECTOR_NAME -------------------------------
if "%COLLECTOR_ID%"=="" (
    echo [ERROR] COLLECTOR_ID is empty in .env. Each collector needs a unique ID.
    exit /b 1
)
if "%COLLECTOR_NAME%"=="" (
    echo [WARN] COLLECTOR_NAME is empty; will display as the COLLECTOR_ID.
)

REM ---- Preflight: relay controller --------------------------------------------
if /I "%RELAY_CONTROLLER%"=="mcc_usb1208fs_plus" (
    echo Checking that mcculw is importable in .venv ...
    call ".venv\Scripts\activate.bat" >nul
    python -c "import mcculw" 2>nul
    if errorlevel 1 (
        echo [ERROR] RELAY_CONTROLLER=mcc_usb1208fs_plus but the mcculw Python
        echo         module is not importable. To fix:
        echo           1) Install MCC Universal Library / InstaCal from
        echo              https://www.mccdaq.com/Software-Downloads
        echo           2) Configure your USB-1208FS-Plus as board 0 in InstaCal.
        echo           3) Run:  .venv\Scripts\activate ^&^& pip install -r requirements-windows.txt
        exit /b 1
    )
    echo mcculw OK.
) else if /I "%RELAY_CONTROLLER%"=="mock" (
    echo [WARN] RELAY_CONTROLLER=mock — no real relays will be controlled.
    echo        Set RELAY_CONTROLLER=mcc_usb1208fs_plus in .env for real hardware.
) else (
    echo [WARN] RELAY_CONTROLLER=%RELAY_CONTROLLER% (unrecognized).
)

REM ---- Activate venv if not already active ------------------------------------
if "%VIRTUAL_ENV%"=="" call ".venv\Scripts\activate.bat"

REM ---- Make logs/ -------------------------------------------------------------
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo.
echo Configuration summary:
echo   APP_MODE          = %APP_MODE%
echo   COLLECTOR_ID      = %COLLECTOR_ID%
echo   COLLECTOR_NAME    = %COLLECTOR_NAME%
echo   HUB_BASE_URL      = %HUB_BASE_URL%
echo   RELAY_CONTROLLER  = %RELAY_CONTROLLER%
echo   Listening on      = http://0.0.0.0:%PORT%
echo   Local health      = http://127.0.0.1:%PORT%/api/health
echo   Log file          = %LOG_FILE%
echo.
echo Starting uvicorn ... (Ctrl+C to stop, or run stop-collector.bat in another window)
echo.

REM Use Python's os.getpid() to write a PID file BEFORE uvicorn takes over.
REM We launch uvicorn through Python so the PID we record matches the live
REM process. Output is tee'd to the log file via PowerShell when present;
REM otherwise it streams only to the console.

set "RUNNER=python -c "import os,sys; open('.collector.pid','w').write(str(os.getpid())); from uvicorn import main; sys.argv=['uvicorn','app.main:app','--host','0.0.0.0','--port','%PORT%']; main()""

where powershell >nul 2>&1
if %ERRORLEVEL%==0 (
    powershell -NoProfile -Command "& { %RUNNER% 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append }"
) else (
    %RUNNER%
)

set "EXITCODE=%ERRORLEVEL%"

REM Clean up PID file on exit.
if exist "%PID_FILE%" del /q "%PID_FILE%" >nul 2>&1

endlocal & exit /b %EXITCODE%
