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
# Use the interpreter that launched us (no hard-coded machine paths):
# watchdog is normally started with pythonw, so sys.executable already
# points at the correct pythonw.exe; fall back to pythonw next to python.exe.
PY = sys.executable
if os.path.basename(PY).lower() == "python.exe":
    _cand = PY[:-4] + "w.exe"
    if os.path.exists(_cand):
        PY = _cand
SCRIPT = os.path.join(BASE, "gpu_monitor.py")
PIDFILE = os.path.join(BASE, "monitor.pid")
STOPFLAG = os.path.join(BASE, "stop.flag")
LOCK = os.path.join(BASE, "watchdog.lock")
WATCHLOG = os.path.join(BASE, "watchdog.log")
PORTFILE = os.path.join(BASE, "monitor.port")
DEFAULT_PORT = "8080"
INTERVAL = 15
# 主服务启动宽限期(秒): 冷启动时 NVML / WMI 初始化可能较慢, 这段时间内
# 即使探测不到端口也不重复拉起, 避免与主服务抢端口产生无谓的崩溃。
GRACE = 20


def port():
    """主服务实际监听的端口。

    优先级: monitor.port (主服务启动时写入, 是端口冲突自动避让后的真实端口)
            > GPU_MONITOR_PORT 环境变量 (手动指定, 供自定义启动方式使用)
            > 8080 (默认)
    这样端口只有一处真源 —— 主服务端口被占用而自动换端口时, watchdog 与启停
    脚本会跟着走, 不会出现"服务在 8081 上跑、watchdog 一直探测 8080 并反复重启"。

    文件里只有一个端口号(纯文本), 因为 .bat / .sh 启停脚本也要读它, 而它们
    没有 JSON 解析器。
    """
    try:
        if os.path.exists(PORTFILE):
            with open(PORTFILE, encoding="ascii") as f:
                p = f.read().strip()
            if p.isdigit():
                return p
    except Exception:
        pass
    return os.environ.get("GPU_MONITOR_PORT") or DEFAULT_PORT


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
        if os.name == "nt":
            h = _kernel().OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if h:
                _kernel().CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)  # POSIX: signal 0 only probes existence
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except Exception:
        return False


def is_main_process(pid):
    """确认 pid 确实是本项目的主服务进程, 防止 PID 复用导致误杀无关进程。

    monitor.pid 可能严重过期 (例如主服务由 start 脚本直接启动、watchdog 从未
    重启过它), 而 Windows 会积极回收 PID —— 直接按 PID 强杀可能杀掉完全无关的
    进程。这里读取该进程的命令行, 只有确认包含 gpu_monitor.py 才允许终止。
    无法确认时一律返回 False (宁可不杀), stop 流程还有 /api/shutdown 与按端口
    兜底, 不会因此留下僵尸进程。
    """
    if not pid or pid <= 0 or pid == os.getpid():
        return False
    # POSIX: 直接读 /proc, 无额外依赖
    fp = "/proc/%d/cmdline" % pid
    try:
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                return "gpu_monitor.py" in f.read().decode("utf-8", "replace")
    except Exception:
        return False
    # Windows / macOS: 借助 psutil (项目自带 pylibs/)
    try:
        sys.path.insert(0, os.path.join(BASE, "pylibs"))
        import psutil
        try:
            cmd = " ".join(psutil.Process(pid).cmdline())
        except Exception:
            return False
        return "gpu_monitor.py" in cmd
    except Exception:
        return False


def terminate(pid):
    try:
        if os.name == "nt":
            h = _kernel().OpenProcess(1, False, pid)  # PROCESS_TERMINATE
            if h:
                _kernel().TerminateProcess(h, 0)
                _kernel().CloseHandle(h)
                return True
        else:
            os.kill(pid, 15)  # SIGTERM
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
        urllib.request.urlopen("http://127.0.0.1:%s/api/settings" % port(), timeout=3).close()
        return True
    except Exception:
        return False


def launch():
    """拉起主服务。

    注意: 传给 Popen 的 stdout/stderr 文件对象必须在父进程侧显式关闭 —— 子进程
    spawn 后已经拿到句柄副本, 父进程的引用只会白白占着一个 fd。watchdog 是长期
    运行的守护进程, 每次重启主服务泄漏两个句柄, 累计数十次后就会撞到 Windows
    默认 512 的 fd 上限 (表现为日志文件再也写不进去, 且无任何报错)。
    """
    out = err = None
    try:
        out = open(os.path.join(BASE, "server.log"), "a")
        err = open(os.path.join(BASE, "server_err.log"), "a")
        p = subprocess.Popen(
            [PY, SCRIPT, "--port", port(), "--interval", "0.5"],
            stdout=out, stderr=err,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(PIDFILE, "w") as f:
            f.write(str(p.pid))
        return p.pid
    except Exception as e:
        log("launch main failed: %r" % (e,))
        return None
    finally:
        for fh in (out, err):
            try:
                if fh:
                    fh.close()
            except Exception:
                pass


def acquire_lock():
    """单实例守卫。返回 False 表示已有 watchdog 在运行, 本进程应退出。"""
    if os.path.exists(LOCK):
        try:
            with open(LOCK) as f:
                if alive(int(f.read().strip())):
                    log("another watchdog already running, exit")
                    return False
        except Exception:
            pass
    try:
        with open(LOCK, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def stopflag_armed(since):
    """stop.flag 是否是针对本轮守护发出的信号。

    判断依据是文件 mtime 而非单纯的存在性: 若 flag 的时间戳早于本进程启动时间,
    它就是上一轮遗留的旧信号, 不应生效。

    这样做的原因: 以前只判断"文件是否存在", 一旦 stop.flag 因故残留 (stop 时
    watchdog 没在运行、或删除失败 —— 文件被占用/只读/沙箱拦截), 下次 start 拉起
    watchdog 后第一轮就会命中它, 立刻杀掉刚启动的主服务并退出, 表现为"服务起来了
    但没有任何守护"的静默失效。按时间戳判断则天然幂等, 即使删不掉也不会误触发。
    """
    try:
        if not os.path.exists(STOPFLAG):
            return False
        return os.path.getmtime(STOPFLAG) >= since - 1.0
    except Exception:
        return False


def main():
    # 单实例检查必须在 main() 内 —— 写在模块级会导致 `import watchdog` 直接
    # sys.exit(0), 使本模块无法被测试或复用。
    if not acquire_lock():
        return 0
    started_at = time.time()
    # 启动宽限期: 主服务冷启动(NVML / WMI 初始化)可能超过一个探测周期, 直接
    # 判定"已死"会重复拉起第二个实例(端口冲突后退出, 留下无谓的崩溃日志)。
    # 这里给主服务 GRACE 秒的启动时间, 宽限期内不主动拉起。
    grace_until = started_at + GRACE
    # 尽力清理上一轮遗留的 stop.flag (删不掉也没关系, stopflag_armed 会按时间戳忽略它)
    if os.path.exists(STOPFLAG) and not stopflag_armed(started_at):
        try:
            os.remove(STOPFLAG)
            log("removed stale stop.flag left by a previous run")
        except Exception as e:
            log("NOTE: stale stop.flag could not be deleted (%r); ignoring it by mtime" % (e,))
    log("watchdog started (interval=%ds)" % INTERVAL)
    try:
        while True:
            if stopflag_armed(started_at):
                try:
                    os.remove(STOPFLAG)
                except Exception:
                    pass
                mp = main_pid()
                if mp and alive(mp) and is_main_process(mp):
                    terminate(mp)
                    log("stop flag seen -> terminated main pid=%s, watchdog exit" % mp)
                elif mp and alive(mp):
                    # PID 已被复用给别的进程 (monitor.pid 过期) —— 绝不误杀
                    log("stop flag seen -> pid=%s is NOT gpu_monitor.py (stale pid file), "
                        "skip kill; watchdog exit" % mp)
                else:
                    log("stop flag seen -> main not running, watchdog exit")
                break
            if not service_up():
                if time.time() < grace_until:
                    log("main service not up yet (grace period, waiting)")
                else:
                    newpid = launch()
                    log("main service down -> restarted pid=%s" % newpid)
                    # 拉起后再给一段宽限期, 避免刚启动的实例被连续判定为死亡
                    grace_until = time.time() + GRACE
            time.sleep(INTERVAL)
    finally:
        release_lock()
    return 0


def release_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
