#!/usr/bin/env bash
# ============================================================
#  GPU real-time monitor - STOP (graceful shutdown, flushes meter)
#  Signals the watchdog to exit (via stop.flag) and shuts down the
#  main service. The watchdog will NOT restart it after a stop.
# ============================================================
set -e
cd "$(dirname "$0")"
HOST=127.0.0.1
PORT=8080

echo "[GPU Monitor] Stopping (graceful shutdown)..."

# 1) Tell the watchdog to exit (so it won't auto-restart the service)
echo 1 > stop.flag

# 2) Ask the main service to shut down gracefully
curl -s -m 5 -X POST "http://${HOST}:${PORT}/api/shutdown" >/dev/null 2>&1 || true

# 3) Wait for the watchdog to react (one full check interval + margin)
sleep 18

# 4) Fallback: if the port is still occupied, force-kill by port
if curl -s -m 2 "http://${HOST}:${PORT}/api/settings" >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
  pkill -f "gpu_monitor.py --port ${PORT}" >/dev/null 2>&1 || true
fi

rm -f monitor.pid
echo "[GPU Monitor] Stopped."
