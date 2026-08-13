@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  下载 LibreHardwareMonitor (LHM)
echo  用于读取 Windows 不暴露的 CPU 真实温度 / 封装功率 / 内存温度
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$api='https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest';" ^
  "$rel=Invoke-RestMethod -Uri $api -Headers @{'User-Agent'='gpu-monitor'};" ^
  "$asset=$rel.assets | Where-Object { $_.name -like 'LibreHardwareMonitor*.zip' } | Select-Object -First 1;" ^
  "if(-not $asset){ throw 'asset not found' };" ^
  "Write-Host ('Downloading: '+$asset.browser_download_url);" ^
  "Invoke-WebRequest -Uri $asset.browser_download_url -OutFile 'lhm.zip' -Headers @{'User-Agent'='gpu-monitor'};" ^
  "Expand-Archive -Path 'lhm.zip' -DestinationPath 'LibreHardwareMonitor' -Force;" ^
  "Remove-Item 'lhm.zip' -Force;" ^
  "Write-Host 'Download and extraction complete.'"
if errorlevel 1 (
  echo.
  echo [错误] 下载失败。请手动前往以下地址下载并解压：
  echo   https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases
  pause
  exit /b 1
)
echo.
echo ============================================================
echo  下载完成！请按以下步骤启用真实温度 / 功率：
echo   1) 以【管理员身份】运行  LibreHardwareMonitor\LibreHardwareMonitor.exe
echo   2) 菜单 “选项(Options)” 中勾选：
echo        - 启用 WMI (Enable WMI)
echo        - 运行 Web 服务器 (Run Web server on localhost:8085)
echo   3) 重新打开监测面板，CPU 温度/功率 与 内存温度 将自动变为实测值
echo      （无需修改本面板任何配置，检测失败时会自动回退为 N/A / TDP 估算）
echo ============================================================
pause
