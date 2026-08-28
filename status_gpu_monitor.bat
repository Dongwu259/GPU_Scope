@echo off
REM ============================================================
REM  GPU real-time monitor - STATUS
REM ============================================================
setlocal
cd /d "%~dp0"
set "HOST=127.0.0.1"
set "PORT=%GPU_MONITOR_PORT%"
if not defined PORT set "PORT=8080"
REM Follow the port the service actually bound to (may differ on port conflict)
if exist monitor.port set /p PORT=<monitor.port

curl -s -m 5 http://%HOST%:%PORT%/api/settings >nul 2>nul
if not errorlevel 1 (
  echo [GPU Monitor] Running: http://%HOST%:%PORT%
  echo --- Meter overview ---
  curl -s -m 5 http://%HOST%:%PORT%/api/settings
  echo.
) else (
  echo [GPU Monitor] Not running. Run start_gpu_monitor.bat to start.
)
echo.
echo Press any key to close this window...
pause >nul
endlocal
