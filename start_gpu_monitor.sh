#!/usr/bin/env bash
# ============================================================
#  GPU real-time monitor - START (Linux/macOS)
#  Runs in background (nohup). A watchdog (watchdog.py) is also
#  launched to auto-restart the service if it ever exits.
#  Logs: server.log / server_err.log / watchdog.log
#  To stop: ./stop_gpu_monitor.sh
# ============================================================
set -e
cd "$(dirname "$0")"

# Locate a Python 3.8+ interpreter
PY=""
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
  echo "[GPU Monitor] ERROR: python3 not found in PATH. Install Python 3.8+."
  exit 1
fi

HOST=127.0.0.1
PORT=8080

# Skip launching main if already running
if curl -s -m 3 "http://${HOST}:${PORT}/api/settings" >/dev/null 2>&1; then
  echo "[GPU Monitor] Main service already running: http://${HOST}:${PORT}"
else
  echo "[GPU Monitor] Starting service (${PY})..."
  nohup "$PY" gpu_monitor.py --port "$PORT" --interval 0.5 \
      >> server.log 2>> server_err.log &
  echo $! > monitor.pid

  # Wait up to ~10s for the service to come up
  ok=0
  for i in $(seq 1 10); do
    sleep 1
    if curl -s -m 2 "http://${HOST}:${PORT}/api/settings" >/dev/null 2>&1; then
      ok=1; break
    fi
  done
  if [ "$ok" = "1" ]; then
    echo "[GPU Monitor] Started OK: http://${HOST}:${PORT}"
  else
    echo "[GPU Monitor] Start FAILED! Recent errors (server_err.log):"
    echo "------------------------------------------------------------"
    tail -20 server_err.log 2>/dev/null || echo "(no server_err.log)"
    echo "------------------------------------------------------------"
  fi
fi

# Ensure watchdog is running (single-instance, safe to call repeatedly)
if ! kill -0 "$(cat watchdog.lock 2>/dev/null || echo 0)" 2>/dev/null; then
  nohup "$PY" watchdog.py >> watchdog.log 2>&1 &
  echo "[GPU Monitor] Watchdog started (auto-restart enabled)."
else
  echo "[GPU Monitor] Watchdog already running."
fi
