@echo off
REM ============================================================
REM  GPU 实时监测面板 - 状态查询
REM ============================================================
setlocal
cd /d C:\Users\admin\WorkBuddy\GPUmonitor

curl -s -m 5 http://localhost:8080/api/settings >nul 2>nul
if %errorlevel%==0 (
  echo [GPU Monitor] 运行中: http://localhost:8080
  echo --- 计量概览 ---
  curl -s -m 5 http://localhost:8080/api/settings
  echo.
) else (
  echo [GPU Monitor] 未运行。运行 start_gpu_monitor.bat 启动。
)
endlocal
