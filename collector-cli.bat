@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "PORT=8001"
set "PID_FILE=.collector.pid"
set "LOG_DIR=logs"
set "LOG_FILE=%LOG_DIR%\collector.log"

call :load_env

if "%~1"=="" goto :usage
set "CMD=%~1"

if /I "%CMD%"=="start" goto :start
if /I "%CMD%"=="stop" goto :stop
if /I "%CMD%"=="status" goto :status
if /I "%CMD%"=="health" goto :health
if /I "%CMD%"=="relay-on" goto :relay_on
if /I "%CMD%"=="relay-off" goto :relay_off
if /I "%CMD%"=="relay-toggle" goto :relay_toggle
goto :usage

:start
if not exist ".venv\Scripts\activate.bat" (
  echo [ERROR] Missing .venv. Run setup-collector-windows.bat first.
  exit /b 1
)
if not exist ".env" (
  echo [ERROR] Missing .env. Configure collector values first.
  exit /b 1
)
if /I not "%APP_MODE%"=="collector" (
  echo [ERROR] APP_MODE must be collector on this machine. Current: %APP_MODE%
  exit /b 1
)
if "%COLLECTOR_API_TOKEN%"=="" (
  echo [ERROR] COLLECTOR_API_TOKEN is empty in .env
  exit /b 1
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if exist "%PID_FILE%" (
  set /p EXISTING_PID=<"%PID_FILE%"
  tasklist /FI "PID eq !EXISTING_PID!" /NH | findstr /I "python" >nul
  if !ERRORLEVEL!==0 (
    echo Collector already running with PID !EXISTING_PID!.
    exit /b 0
  )
  del /q "%PID_FILE%" >nul 2>&1
)

powershell -NoProfile -Command "$p=Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','app.main:app','--host','0.0.0.0','--port','%PORT%' -WorkingDirectory '%CD%' -RedirectStandardOutput '%LOG_FILE%' -RedirectStandardError '%LOG_FILE%' -PassThru; $p.Id | Out-File -Encoding ascii '%PID_FILE%'"
if errorlevel 1 (
  echo [ERROR] Failed to start collector process.
  exit /b 1
)
echo Collector started. PID file: %PID_FILE%
goto :status

:stop
set "TARGET_PID="
if exist "%PID_FILE%" set /p TARGET_PID=<"%PID_FILE%"
if not defined TARGET_PID (
  for /f "tokens=5" %%P in ('netstat -ano -p TCP 2^>nul ^| findstr /R /C:":%PORT% .*LISTENING"') do set "TARGET_PID=%%P"
)
if not defined TARGET_PID (
  echo Collector is not running.
  if exist "%PID_FILE%" del /q "%PID_FILE%" >nul 2>&1
  exit /b 0
)
taskkill /PID %TARGET_PID% /T /F >nul 2>&1
if exist "%PID_FILE%" del /q "%PID_FILE%" >nul 2>&1
echo Stopped collector PID %TARGET_PID%.
exit /b 0

:status
echo.
echo === Collector status ===
echo APP_MODE=%APP_MODE%
echo COLLECTOR_ID=%COLLECTOR_ID%
echo HUB_BASE_URL=%HUB_BASE_URL%
echo RELAY_CONTROLLER=%RELAY_CONTROLLER%
set "FOUND="
if exist "%PID_FILE%" (
  set /p PID_FROM_FILE=<"%PID_FILE%"
  tasklist /FI "PID eq !PID_FROM_FILE!" /NH | findstr /I "python" >nul
  if !ERRORLEVEL!==0 (
    echo Process: RUNNING PID !PID_FROM_FILE!
    set "FOUND=1"
  )
)
if not defined FOUND echo Process: NOT RUNNING
echo Local health: http://127.0.0.1:%PORT%/api/health
exit /b 0

:health
curl -fsS --max-time 5 "http://127.0.0.1:%PORT%/api/health"
echo.
exit /b %ERRORLEVEL%

:relay_on
if "%~2"=="" (
  echo Usage: collector-cli.bat relay-on relay-1
  exit /b 1
)
curl -fsS -u "%ADMIN_USERNAME%:%ADMIN_PASSWORD%" -X POST "http://127.0.0.1:%PORT%/api/relays/%~2/on"
echo.
exit /b %ERRORLEVEL%

:relay_off
if "%~2"=="" (
  echo Usage: collector-cli.bat relay-off relay-1
  exit /b 1
)
curl -fsS -u "%ADMIN_USERNAME%:%ADMIN_PASSWORD%" -X POST "http://127.0.0.1:%PORT%/api/relays/%~2/off"
echo.
exit /b %ERRORLEVEL%

:relay_toggle
if "%~2"=="" (
  echo Usage: collector-cli.bat relay-toggle relay-1
  exit /b 1
)
curl -fsS -u "%ADMIN_USERNAME%:%ADMIN_PASSWORD%" -X POST "http://127.0.0.1:%PORT%/api/relays/%~2/toggle"
echo.
exit /b %ERRORLEVEL%

:load_env
if not exist ".env" goto :eof
for /f "usebackq tokens=1* delims==" %%A in (".env") do (
  set "_K=%%A"
  set "_V=%%B"
  if not "!_K!"=="" if not "!_K:~0,1!"=="#" (
    if defined _V set "_V=!_V:"=!"
    set "!_K!=!_V!"
  )
)
goto :eof

:usage
echo.
echo Lab-Controller Windows CLI
echo.
echo Usage:
echo   collector-cli.bat start
echo   collector-cli.bat stop
echo   collector-cli.bat status
echo   collector-cli.bat health
echo   collector-cli.bat relay-on relay-1
echo   collector-cli.bat relay-off relay-1
echo   collector-cli.bat relay-toggle relay-1
exit /b 1
