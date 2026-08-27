#!/usr/bin/env bash
# ============================================================
#  GPU real-time monitor - STATUS (Linux/macOS)
# ============================================================
cd "$(dirname "$0")"
HOST=127.0.0.1
PORT=8080

if curl -s -m 5 "http://${HOST}:${PORT}/api/settings" >/dev/null 2>&1; then
  echo "[GPU Monitor] Running: http://${HOST}:${PORT}"
  echo "--- Meter overview ---"
  curl -s -m 5 "http://${HOST}:${PORT}/api/settings"
  echo ""
else
  echo "[GPU Monitor] Not running. Run ./start_gpu_monitor.sh to start."
fi
