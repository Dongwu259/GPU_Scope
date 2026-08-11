@echo off
REM ============================================================
REM  GPU 实时监测面板 - 启动 (双击运行)
REM  后台运行 (pythonw, 无黑窗口), 日志在 server.log / server_err.log
REM  停止: 运行 stop_gpu_monitor.bat
REM ============================================================
setlocal
set "PY=C:\Users\admin\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
set "PIP=C:\Users\admin\.workbuddy\binaries\python\versions\3.13.12\python.exe -m pip"
set "SCRIPT=C:\Users\admin\WorkBuddy\GPUmonitor\gpu_monitor.py"
cd /d C:\Users\admin\WorkBuddy\GPUmonitor

REM 已在运行则跳过
curl -s -m 3 http://localhost:8080/api/settings >nul 2>nul
if %errorlevel%==0 (
  echo [GPU Monitor] 已在运行: http://localhost:8080
  goto :eof
)

REM 依赖自检: 缺失则尝试安装 (优先系统/venv, 失败则回退 pylibs)
"%PY%" -c "import pynvml, psutil" >nul 2>nul
if errorlevel 1 (
  echo [GPU Monitor] 缺少依赖 (pynvml/psutil), 正在尝试安装...
  %PIP% install -r requirements.txt
  if errorlevel 1 (
    echo [GPU Monitor] 自动安装失败, 请手动: pip install -r requirements.txt
    pause
    goto :eof
  )
)

start "" "%PY%" "%SCRIPT%" --port 8080 --interval 0.5
timeout /t 2 >nul
curl -s -m 5 http://localhost:8080/api/settings >nul 2>nul
if %errorlevel%==0 (
  echo [GPU Monitor] 启动成功: http://localhost:8080
) else (
  echo [GPU Monitor] 启动失败, 请查看 server.log
)
endlocal
