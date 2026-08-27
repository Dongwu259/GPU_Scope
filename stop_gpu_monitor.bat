@echo off
REM ============================================================
REM  GPU real-time monitor - STOP (graceful shutdown, flushes meter)
REM  Signals the watchdog to exit (via stop.flag) and shuts down the
REM  main service. The watchdog will NOT restart it after a stop.
REM ============================================================
setlocal
cd /d "%~dp0"
set "HOST=127.0.0.1"
set "PORT=8080"

echo [GPU Monitor] Stopping (graceful shutdown)...

REM 1) Tell the watchdog to exit (so it won't auto-restart the service)
echo 1 > stop.flag

REM 2) Ask the main service to shut down gracefully
curl -s -m 5 -X POST http://%HOST%:%PORT%/api/shutdown >nul 2>nul

REM 3) Wait for the watchdog to react (one full check interval + margin)
timeout /t 18 >nul

REM 4) Fallback: if the port is still occupied, force-kill by port
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a 2>nul
)

echo [GPU Monitor] Stopped.
endlocal
