@echo off
REM ============================================================
REM  GPU real-time monitor - STATUS
REM ============================================================
setlocal
cd /d C:\Users\admin\WorkBuddy\GPUmonitor
set "HOST=127.0.0.1"
set "PORT=8080"

curl -s -m 5 http://%HOST%:%PORT%/api/settings >nul 2>nul
if not errorlevel 1 (
  echo [GPU Monitor] Running: http://%HOST%:%PORT%
  echo --- Meter overview ---
  curl -s -m 5 http://%HOST%:%PORT%/api/settings
  echo.
) else (
  echo [GPU Monitor] Not running. Run start_gpu_monitor.bat to start.
)
endlocal
