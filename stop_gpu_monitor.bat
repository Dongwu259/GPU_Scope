@echo off
REM ============================================================
REM  GPU 实时监测面板 - 停止 (优雅退出, 落盘计量数据)
REM ============================================================
setlocal
cd /d C:\Users\admin\WorkBuddy\GPUmonitor

echo [GPU Monitor] 正在停止 (优雅退出)...
curl -s -m 5 -X POST http://localhost:8080/api/shutdown >nul 2>nul
timeout /t 2 >nul

REM 兜底: 若端口仍被占用, 按端口强制结束进程
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8080" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a 2>nul
)
echo [GPU Monitor] 已停止。
endlocal
