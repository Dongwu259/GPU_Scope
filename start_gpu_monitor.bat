@echo off
REM ============================================================
REM  GPU real-time monitor - START (double-click to run)
REM  Runs in background (pythonw, no console window).
REM  A watchdog (watchdog.py) is also launched to auto-restart the
REM  service if it ever exits, so you never have to restart manually.
REM  Output/errors are logged to server.log / server_err.log / watchdog.log
REM  To stop: run stop_gpu_monitor.bat
REM
REM  Port: defaults to 8080. Override with the GPU_MONITOR_PORT env var.
REM  The service writes the port it actually bound to into monitor.port,
REM  so if 8080 is taken it silently moves to 8081 and this script (plus
REM  the watchdog and stop/status scripts) follows it automatically.
REM ============================================================
setlocal
cd /d "%~dp0"

REM Locate a Python 3.8+ interpreter (deps are bundled in pylibs/, no pip needed)
set "PY="
where pythonw >nul 2>nul && set "PY=pythonw.exe"
if not defined PY where python >nul 2>nul && set "PY=python.exe"
if not defined PY goto :nopy

set "SCRIPT=%~dp0gpu_monitor.py"
set "HOST=127.0.0.1"
set "PORT=%GPU_MONITOR_PORT%"
if not defined PORT set "PORT=8080"

REM Read the port used by an already-running instance, if any
if exist monitor.port set /p PORT=<monitor.port

REM Skip launching main if already running
curl -s -m 3 http://%HOST%:%PORT%/api/settings >nul 2>nul
if not errorlevel 1 (
  echo [GPU Monitor] Main service already running: http://%HOST%:%PORT%
  goto :wd
)

REM Drop a stale port file so we never health-check a port nobody listens on
if exist monitor.port del /q monitor.port >nul 2>nul

echo [GPU Monitor] Starting service (%PY%)...
start "GPU-Monitor" "%PY%" "%SCRIPT%" --port %PORT% --interval 0.5

REM Wait up to ~15s for the service to come up. Re-read monitor.port each
REM round: the service may have fallen back to the next free port.
set TRIES=0

:wait
timeout /t 1 >nul 2>nul
set "ACTUAL=%PORT%"
if exist monitor.port set /p ACTUAL=<monitor.port
curl -s -m 2 http://%HOST%:%ACTUAL%/api/settings >nul 2>nul
if not errorlevel 1 goto :ok
set /a TRIES+=1
if %TRIES% LSS 15 goto :wait

echo [GPU Monitor] Start FAILED! Recent errors (server_err.log):
echo ------------------------------------------------------------
if exist server_err.log (
  powershell -NoProfile -Command "Get-Content server_err.log -Tail 20"
) else (
  echo (no server_err.log - pythonw path may be wrong or script crashed early)
)
echo ------------------------------------------------------------
goto :wd

:ok
echo [GPU Monitor] Started OK: http://%HOST%:%ACTUAL%

:wd
REM Ensure watchdog is running (single-instance, safe to call repeatedly)
start "GPU-Monitor-WD" "%PY%" "%~dp0watchdog.py"
echo [GPU Monitor] Watchdog started (auto-restart enabled).

:done
echo.
echo Press any key to close this window (service + watchdog keep running in background)...
pause >nul
endlocal
exit /b

:nopy
echo [GPU Monitor] ERROR: Python not found in PATH.
echo   Install Python 3.8+ (tick "Add python.exe to PATH") from https://www.python.org/downloads/
echo   then run this script again. Runtime deps are bundled in pylibs/, no pip install needed.
pause
exit /b 1
