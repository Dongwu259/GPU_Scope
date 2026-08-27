@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  Install LibreHardwareMonitor (LHM)
echo  Enables real CPU temperature / package power / memory temp
echo  that Windows does not expose via WMI by default.
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
  echo [ERROR] Download failed. Please download and extract manually:
  echo   https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases
  pause
  exit /b 1
)
echo.
echo ============================================================
echo  Done! To enable real temperature / power readings:
echo   1) Run  LibreHardwareMonitor\LibreHardwareMonitor.exe  as Administrator
echo   2) In the Options menu, tick:
echo        - Enable WMI
echo        - Run Web server (localhost:8085)
echo   3) Reopen the monitor panel. CPU temp/power and memory temp
echo      will now show measured values automatically.
echo      (Falls back to N/A / TDP estimates if detection fails.)
echo ============================================================
pause
