@echo off
REM ============================================================
REM  GPU real-time monitor - START (double-click to run)
REM  Runs in background (pythonw, no console window).
REM  A watchdog (watchdog.py) is also launched to auto-restart the
REM  service if it ever exits, so you never have to restart manually.
REM  Output/errors are logged to server.log / server_err.log / watchdog.log
REM  To stop: run stop_gpu_monitor.bat
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
set "PORT=8080"

REM Skip launching main if already running
curl -s -m 3 http://%HOST%:%PORT%/api/settings >nul 2>nul
if not errorlevel 1 (
  echo [GPU Monitor] Main service already running: http://%HOST%:%PORT%
  goto :wd
)

echo [GPU Monitor] Starting service (%PY%)...
start "GPU-Monitor" "%PY%" "%SCRIPT%" --port %PORT% --interval 0.5

REM Wait up to ~10s for the service to come up
for /L %%i in (1,1,10) do (
  timeout /t 1 >nul 2>nul
  curl -s -m 2 http://%HOST%:%PORT%/api/settings >nul 2>nul
  if not errorlevel 1 goto :ok
)

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
echo [GPU Monitor] Started OK: http://%HOST%:%PORT%

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
