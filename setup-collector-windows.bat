@echo off
REM ============================================================================
REM setup-collector-windows.bat
REM
REM First-time setup for a Windows-native Lab-Controller collector. Creates the
REM Python virtualenv, installs dependencies (including the Windows-only
REM mcculw driver), and seeds .env from .env.collector.example if missing.
REM
REM Run this from a Command Prompt at the repo root, OR by double-clicking it
REM (the script cd's into its own directory).
REM ============================================================================

setlocal EnableExtensions EnableDelayedExpansion

REM cd into the directory this script lives in so it works from anywhere.
cd /d "%~dp0"

echo.
echo === Lab-Controller collector setup (Windows) ===
echo Repo root: %CD%
echo.

REM ---- 1. Locate Python --------------------------------------------------------
set "PY_CMD="
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>&1
    if !ERRORLEVEL!==0 (
        set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] Could not find Python. Install Python 3.11 or 3.12 64-bit
    echo         from https://www.python.org/downloads/windows/ and tick
    echo         "Add python.exe to PATH" during install.
    exit /b 1
)
echo Using Python launcher: %PY_CMD%
%PY_CMD% --version

REM ---- 2. Create virtualenv ----------------------------------------------------
if not exist ".venv" (
    echo Creating virtualenv at .venv ...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtualenv.
        exit /b 1
    )
) else (
    echo Found existing virtualenv at .venv
)

REM ---- 3. Activate and install requirements -----------------------------------
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate .venv.
    exit /b 1
)

echo Upgrading pip ...
python -m pip install --upgrade pip
if errorlevel 1 goto pip_fail

echo Installing requirements.txt ...
python -m pip install -r requirements.txt
if errorlevel 1 goto pip_fail

echo Installing requirements-windows.txt (mcculw for MCC USB-1208FS-Plus) ...
python -m pip install -r requirements-windows.txt
if errorlevel 1 (
    echo [WARN] requirements-windows.txt failed. The collector will still
    echo        start in RELAY_CONTROLLER=mock mode, but real MCC relay
    echo        control needs MCC Universal Library / InstaCal installed
    echo        first. Download from:
    echo        https://www.mccdaq.com/Software-Downloads
    echo        Then re-run this script.
)

REM ---- 4. Seed .env if missing -------------------------------------------------
if not exist ".env" (
    if exist ".env.collector.example" (
        copy /Y ".env.collector.example" ".env" >nul
        echo Created .env from .env.collector.example
    ) else (
        echo [WARN] .env.collector.example missing — cannot create .env automatically.
    )
) else (
    echo .env already exists; not overwriting.
)

echo.
echo === Setup complete ===
echo.
echo NEXT STEPS:
echo   1. Edit .env in this folder. At minimum set:
echo        APP_MODE=collector
echo        HUB_BASE_URL=http://YOUR_HUB_TAILSCALE_IP:8000
echo        COLLECTOR_API_TOKEN=  (must match the hub's value EXACTLY)
echo        COLLECTOR_ID=         (unique per collector, e.g. lab-mcc-controller)
echo        COLLECTOR_NAME=
echo        RELAY_CONTROLLER=mcc_usb1208fs_plus  (for real hardware)
echo.
echo   2. For real MCC USB-1208FS-Plus relay control you MUST install
echo      MCC Universal Library / InstaCal first and configure board 0,
echo      then re-run this script if the mcculw install warned above.
echo.
echo   3. Start the collector:
echo        start-collector.bat
echo.

endlocal
exit /b 0

:pip_fail
echo [ERROR] pip install failed. Check the output above.
endlocal
exit /b 1
