#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watchdog.py - GPU Monitor self-heal watchdog.

Keeps the main monitor service (gpu_monitor.py) alive:
- If the main process dies (OOM / SIGKILL / unhandled crash), restart it automatically.
- If stop.flag exists, kill the main process and exit (used by stop_gpu_monitor.bat).
- Single instance: only one watchdog runs at a time.
"""
import os
import sys
import time
import subprocess
import ctypes
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Users\admin\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
SCRIPT = os.path.join(BASE, "gpu_monitor.py")
PIDFILE = os.path.join(BASE, "monitor.pid")
STOPFLAG = os.path.join(BASE, "stop.flag")
LOCK = os.path.join(BASE, "watchdog.lock")
WATCHLOG = os.path.join(BASE, "watchdog.log")
PORT = "8080"
INTERVAL = 15


def log(msg):
    try:
        with open(WATCHLOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _kernel():
    return ctypes.windll.kernel32


def alive(pid):
    if not pid:
        return False
    try:
        h = _kernel().OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
        if h:
            _kernel().CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def terminate(pid):
    try:
        h = _kernel().OpenProcess(1, False, pid)  # PROCESS_TERMINATE
        if h:
            _kernel().TerminateProcess(h, 0)
            _kernel().CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def main_pid():
    try:
        with open(PIDFILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def service_up():
    """Liveness check by probing the service port (works no matter who started it)."""
    try:
        urllib.request.urlopen("http://127.0.0.1:%s/api/settings" % PORT, timeout=3).close()
        return True
    except Exception:
        return False


def launch():
    try:
        p = subprocess.Popen(
            [PY, SCRIPT, "--port", PORT, "--interval", "0.5"],
            stdout=open(os.path.join(BASE, "server.log"), "a"),
            stderr=open(os.path.join(BASE, "server_err.log"), "a"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(PIDFILE, "w") as f:
            f.write(str(p.pid))
        return p.pid
    except Exception as e:
        log("launch main failed: %r" % (e,))
        return None


# single instance guard
if os.path.exists(LOCK):
    try:
        with open(LOCK) as f:
            if alive(int(f.read().strip())):
                log("another watchdog already running, exit")
                sys.exit(0)
    except Exception:
        pass
with open(LOCK, "w") as f:
    f.write(str(os.getpid()))

log("watchdog started (interval=%ds)" % INTERVAL)
try:
    while True:
        if os.path.exists(STOPFLAG):
            try:
                os.remove(STOPFLAG)
            except Exception:
                pass
            mp = main_pid()
            if mp and alive(mp):
                terminate(mp)
                log("stop flag seen -> terminated main pid=%s, watchdog exit" % mp)
            else:
                log("stop flag seen -> main not running, watchdog exit")
            break
        if not service_up():
            newpid = launch()
            log("main service down -> restarted pid=%s" % newpid)
        time.sleep(INTERVAL)
finally:
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass
