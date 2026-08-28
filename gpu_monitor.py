#!/usr/bin/env python3
"""
实时 GPU 监测监控服务 (Real-time GPU Monitor)

后端: NVML 轮询线程 + 纯标准库 HTTP 服务 (无 Flask 等外部依赖)。
前端: 同目录下的 index.html (无外部依赖, 纯 Canvas 图表)。

特性:
  - 实时利用率 / 显存活动 / 功率 / 温度 / 风扇 / 时钟 / 节流原因 / 进程
  - 等效算力估算 (FP32 / FP16 / Tensor FP16 稠密 / 稀疏)
  - 显存带宽占用估算
  - 每个进程的 SM/显存/编码/解码 活跃度 + 真实显存占用 (Windows 性能计数器)
  - NVENC / NVDEC 媒体引擎利用率
  - 性能状态(P-State) / PCIe 链路与吞吐 / BAR1 / 累计能耗

用法:
  python gpu_monitor.py [--port 8080] [--interval 0.5]
"""

# 版本号唯一真源: CHANGELOG / pyproject.toml / HTTP Server 头 / 前端「关于」卡片
# 都以此为准。发版时只需改这里 + CHANGELOG 条目。
__version__ = "0.1.5"

import argparse
import ctypes
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys

# 后台(pythonw)运行时, 调用控制台子进程(powershell 等)必须隐藏其窗口,
# 否则 Windows 会为每个被调用的 powershell 弹出一个控制台窗口。
# CREATE_NO_WINDOW 仅 Windows 有效, 其它平台取 0 (无副作用)。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import threading

# ---- 平台识别: 各平台专属采集后端按此分发 ----
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

# 统一的诊断日志: 记录进程生命周期关键事件 (SIGTERM / 异常崩溃 / 自愈重启),
# 用于排查"服务莫名关闭"类问题 —— server_err.log 平时为空即说明非 Python 崩溃。
def _errlog(msg):
    try:
        _ef = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_err.log"),
                   "a", encoding="utf-8")
        _ef.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        _ef.close()
    except Exception:
        pass

import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 将同目录的 pylibs (pynvml / psutil 等本地依赖) 加入导入路径,
# 避免依赖外部 PYTHONPATH 环境变量 (某些沙箱环境会剥离该变量)。
_PYLIBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pylibs")
if os.path.isdir(_PYLIBS) and _PYLIBS not in sys.path:
    sys.path.insert(0, _PYLIBS)

try:
    import psutil
except Exception:
    psutil = None

# ---------------------------------------------------------------------------
# 健壮的 NVML 加载 (Windows 上 pynvml 有时找不到 nvml.dll)
# ---------------------------------------------------------------------------
def _load_nvml():
    try:
        import pynvml  # noqa
        pynvml.nvmlInit()
        return pynvml
    except Exception:
        pass
    if IS_WINDOWS:
        candidates = [r"C:\Windows\System32\nvml.dll", r"C:\Windows\SysWOW64\nvml.dll"]
        try:
            import glob
            candidates += glob.glob(r"C:\NVIDIA\*\nvml.dll")
        except Exception:
            pass
        for path in candidates:
            if os.path.exists(path):
                try:
                    os.add_dll_directory(os.path.dirname(path))
                except Exception:
                    pass
                try:
                    import pynvml
                    pynvml.nvmlInit()
                    return pynvml
                except Exception:
                    continue
    # 非 Windows (或 Windows 未找到 nvml.dll): 交给 pynvml 默认查找
    # (Linux 会加载 libnvidia-ml.so.1; macOS Intel 卡同样走 NVML)
    import pynvml
    pynvml.nvmlInit()
    return pynvml


# ---------------------------------------------------------------------------
# GPU 规格数据库 (峰值理论吞吐, 用于换算等价 FLOPS)
# ---------------------------------------------------------------------------
# 峰值理论吞吐 (TFLOPS), 数据来自 NVIDIA 官方 datasheet。
# 说明: Blackwell 消费卡 NVIDIA 营销的 "AI TOPS" = FP4 稀疏; 各精度档位关系:
#   FP16/BF16 张量稠密 ≈ 4×FP32,  FP16 张量稀疏(2:4) ≈ 8×FP32,
#   FP8 张量稠密 ≈ 8×FP32(=FP16稀疏), FP8 张量稀疏 ≈ 16×FP32,
#   FP4 张量稠密 ≈ 16×FP32(=FP8稀疏), FP4 张量稀疏 ≈ 32×FP32 (=AI TOPS)。
# A100(GA100) 张量单元远强于着色器, 故其 FP16 张量稠密=312 (≠4×FP32), 单独给定。
GPU_SPECS = {
    "NVIDIA GeForce RTX 5080": {
        "arch": "Blackwell (GB203)", "cuda_cores": 10752, "tensor_cores": 336,
        "memory_GiB": 16, "bandwidth_GBps": 960, "boost_clock_ghz": 2.62,
        "fp32_tflops": 56.3, "fp16_tflops": 112.6,
        "tensor_fp16_dense_tflops": 225.1, "tensor_fp16_sparse_tflops": 450.2,
        "tensor_fp8_dense_tflops": 450.2, "tensor_fp8_sparse_tflops": 900.4,
        "tensor_fp4_dense_tflops": 900.4, "tensor_fp4_sparse_tflops": 1800.8,
    },
    "NVIDIA GeForce RTX 5090": {
        "arch": "Blackwell (GB202)", "cuda_cores": 21760, "tensor_cores": 680,
        "memory_GiB": 32, "bandwidth_GBps": 1792, "boost_clock_ghz": 2.41,
        "fp32_tflops": 104.8, "fp16_tflops": 209.6,
        "tensor_fp16_dense_tflops": 419.1, "tensor_fp16_sparse_tflops": 838.2,
        "tensor_fp8_dense_tflops": 838.2, "tensor_fp8_sparse_tflops": 1676.4,
        "tensor_fp4_dense_tflops": 1676.4, "tensor_fp4_sparse_tflops": 3352.8,
    },
    "NVIDIA GeForce RTX 4090": {
        "arch": "Ada Lovelace (AD102)", "cuda_cores": 16384, "tensor_cores": 512,
        "memory_GiB": 24, "bandwidth_GBps": 1008, "boost_clock_ghz": 2.52,
        "fp32_tflops": 82.6, "fp16_tflops": 165.2,
        "tensor_fp16_dense_tflops": 330.3, "tensor_fp16_sparse_tflops": 660.6,
        "tensor_fp8_dense_tflops": 660.6, "tensor_fp8_sparse_tflops": 1321.0,
        "tensor_fp4_dense_tflops": 1321.0, "tensor_fp4_sparse_tflops": 2642.0,
    },
    "NVIDIA GeForce RTX 3090": {
        "arch": "Ampere (GA102)", "cuda_cores": 10496, "tensor_cores": 328,
        "memory_GiB": 24, "bandwidth_GBps": 936, "boost_clock_ghz": 1.70,
        "fp32_tflops": 35.6, "fp16_tflops": 71.2,
        "tensor_fp16_dense_tflops": 142.4, "tensor_fp16_sparse_tflops": 284.8,
        "tensor_fp8_dense_tflops": 284.8, "tensor_fp8_sparse_tflops": 569.6,
        "tensor_fp4_dense_tflops": 569.6, "tensor_fp4_sparse_tflops": 1139.2,
    },
    "NVIDIA A100-SXM4-80GB": {
        "arch": "Ampere (GA100)", "cuda_cores": 6912, "tensor_cores": 432,
        "memory_GiB": 80, "bandwidth_GBps": 2039, "boost_clock_ghz": 1.41,
        "fp32_tflops": 19.5, "fp16_tflops": 39.0,
        "tensor_fp16_dense_tflops": 312.0, "tensor_fp16_sparse_tflops": 624.0,
        "tensor_fp8_dense_tflops": 624.0, "tensor_fp8_sparse_tflops": 1248.0,
        "tensor_fp4_dense_tflops": 1248.0, "tensor_fp4_sparse_tflops": 2496.0,
    },
}
DEFAULT_SPEC = GPU_SPECS["NVIDIA GeForce RTX 5080"]

# ---------------------------------------------------------------------------
# 等效 H100 GPU 小时: 把各型号 GPU 的"利用率 × 时长"折算成 H100 等效算力时长。
# 基准 = H100(SXM) FP16 Tensor 稠密峰值 (~989 TFLOPS)。系数 = 该卡 FP16 Tensor
# 稠密 TFLOPS / 989。用于累计"本机累计贡献了多少 H100 等效算力时长"。
# ---------------------------------------------------------------------------
_H100_TF16_DENSE = 989.0  # H100 FP16 Tensor 稠密峰值 (TFLOPS)

# 规格表未收录的常见数据中心/计算卡, 按名称子串匹配给定相对系数
_H100_FACTOR_TABLE = {
    "H100": 1.0, "H800": 1.0, "H20": 0.62, "A100": 0.315, "A800": 0.315,
    "L40S": 0.30, "L40": 0.24, "A40": 0.18, "A30": 0.13, "A10": 0.10,
    "A10G": 0.10, "V100": 0.125, "T4": 0.035, "P100": 0.06, "M60": 0.03,
    "RTX 6000": 0.42, "RTX A6000": 0.22, "RTX A5000": 0.16,
    "RTX A4000": 0.11, "RTX 5090": 0.4235, "RTX 5080": 0.2276,
    "RTX 5070": 0.18, "RTX 4090": 0.3338, "RTX 4080": 0.23, "RTX 4070": 0.15,
    "RTX 4060": 0.10, "RTX 3090": 0.1440, "RTX 3080": 0.10, "RTX 3060": 0.06,
}


def h100_factor(name):
    """返回某 GPU 相对 H100 的算力系数 (基于 FP16 Tensor 稠密 TFLOPS)。"""
    if not name:
        return 0.1
    if name in GPU_SPECS:
        return max(0.01, GPU_SPECS[name]["tensor_fp16_dense_tflops"] / _H100_TF16_DENSE)
    for key, f in _H100_FACTOR_TABLE.items():
        if key in name:
            return f
    return 0.1  # 未知型号给保守默认

THROTTLE_REASONS = {
    0x1: "GPU 空闲降频", 0x2: "应用自定义时钟设置", 0x4: "软件功率上限 (Power Cap)",
    0x8: "硬件减速 (过热/供电)", 0x10: "Sync Boost 同步", 0x20: "软件热节流 (SW Thermal)",
    0x40: "硬件热节流 (HW Thermal)", 0x80: "硬件功率保护节流 (Power Brake)", 0x100: "显示时钟设置",
}


def decode_throttle(reason_bits):
    if reason_bits == 0:
        return []
    return [label for bit, label in THROTTLE_REASONS.items() if reason_bits & bit]


def _classify_nvml_error(e):
    """把 NVML 初始化异常归类成前端可直接展示的原因码。

    前端据此显示降级提示, 而不是让用户对着空白面板或永久的"正在连接"发呆。
    """
    msg = (str(e) or "").strip()
    low = msg.lower()
    if isinstance(e, ImportError) or "no module named" in low:
        return "pynvml_missing"
    # 驱动/库相关问题: pynvml 的异常消息不一定包含 "nvml" 字样
    # (例如 "Driver/library version mismatch" / "Driver Not Loaded"), 故单独匹配
    if any(k in low for k in ("driver", "library", "mismatch", "not loaded")):
        return "nvml_driver"
    if type(e).__name__.startswith("NVMLError") or "nvml" in low:
        return "nvml_error"
    return "unknown"


def _proc_name(pid):
    """尽力获取进程名 (跨平台: psutil 优先; Windows 兜底 ctypes API)。"""
    if psutil:
        try:
            return psutil.Process(pid).name()
        except Exception:
            pass
    if IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            h = kernel32.OpenProcess(0x0400, False, pid)
            if not h:
                return None
            buf = ctypes.create_unicode_buffer(1024)
            if psapi.GetModuleFileNameExW(h, 0, buf, 1024):
                return buf.value.split("\\")[-1]
            return None
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# 每进程显存 (Windows 性能计数器 PDH) —— NVML 在 WDDM 下不提供, 这里补齐
# 实例名形如 pid_4020_luid_0x..._phys_0 , phys_0 = 本地(专用)显存, 单位 KB
# ---------------------------------------------------------------------------
_PDH_SCRIPT = (
    "$e=Get-Counter -Counter '\\GPU Process Memory(*)\\Local Usage' "
    "-ErrorAction SilentlyContinue; "
    "if($e){$e.CounterSamples | ForEach-Object { "
    "if($_.InstanceName -match 'pid_(\\d+)_luid_.*_phys_0$'){ "
    "'{0}={1}' -f $Matches[1], [math]::Round($_.CookedValue/1MB,1) } }}"
)

# PDH 查询缓存: 每进程显存变化缓慢, 没必要跟着 0.5s 的采样间隔反复 spawn PowerShell
PROC_MEM_TTL = 5.0
_PROC_MEM_CACHE = {}
_PROC_MEM_CACHE_TS = 0.0
_PROC_MEM_SKIP_NEXT = False


def _query_pdh_proc_mem():
    out = {}
    if not IS_WINDOWS:
        # 非 Windows 无 PDH 性能计数器; 每进程显存由 NVML 进程 API 提供
        return out
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PDH_SCRIPT],
            capture_output=True, text=True, timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in r.stdout.splitlines():
            if "=" in line:
                pid_s, mb_s = line.strip().split("=", 1)
                try:
                    out[int(pid_s)] = float(mb_s)
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _query_pdh_proc_mem_cached():
    """每进程显存的带缓存查询。返回 (结果 dict, 本轮是否真的查询过)。

    每 2 秒 spawn 一个 PowerShell 去读 PDH 计数器, 是本项目在 Windows 上最主要的
    子进程开销 —— 而 PDH 的用途只是给"GPU 进程列表"补一列专用显存, 变化很慢。
    这里做两件事:
      1. 缓存 PROC_MEM_TTL 秒, 采样间隔(默认 0.5s)远小于它时不再重复 spawn;
      2. 上一轮查到"没有任何进程占用 GPU"时, 跳过下一轮查询 —— 空闲机器(监控
         软件的常态)上这个子进程基本不出现。

    注意"只跳过一轮": 跳过标志必须在本轮清零, 否则一旦进入跳过分支就再也不会
    查询, 新启动的 GPU 任务永远拿不到显存数据。
    """
    global _PROC_MEM_CACHE_TS, _PROC_MEM_CACHE, _PROC_MEM_SKIP_NEXT
    now = time.monotonic()
    if now - _PROC_MEM_CACHE_TS < PROC_MEM_TTL:
        return _PROC_MEM_CACHE, False
    _PROC_MEM_CACHE_TS = now
    if _PROC_MEM_SKIP_NEXT:
        _PROC_MEM_SKIP_NEXT = False
        return {}, False
    _PROC_MEM_CACHE = _query_pdh_proc_mem()
    _PROC_MEM_SKIP_NEXT = not _PROC_MEM_CACHE
    return _PROC_MEM_CACHE, True


# ---------------------------------------------------------------------------
# 提交内存占比 (load_percent) —— 语义: 已提交内存 / 提交上限
#   这与任务管理器"已提交 X / Y GB"里的百分比是同一个量。
#   Windows: kernel32.GlobalMemoryStatusEx, 进程内调用, 不 spawn 子进程。
#   Linux  : /proc/meminfo 的 Committed_AS / CommitLimit。
#   macOS  : 无等价概念, 返回 None (前端显示 N/A, 不伪造数值)。
# ---------------------------------------------------------------------------
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _commit_percent_windows():
    """Windows 提交内存占比 (%), 失败返回 None。

    GlobalMemoryStatusEx 的 ullTotalPageFile / ullAvailPageFile 就是提交上限与
    剩余可提交量 —— 与性能计数器 "\\\\Memory\\\\% Committed Bytes In Use" 同源,
    但在本进程内一次调用即可拿到, 省掉一个 PowerShell 子进程。
    """
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        if not st.ullTotalPageFile:
            return None
        used = st.ullTotalPageFile - st.ullAvailPageFile
        return round(used * 100.0 / st.ullTotalPageFile, 1)
    except Exception:
        return None


def _commit_percent_linux():
    """Linux 提交内存占比 (%), 数据缺失返回 None。

    /proc/meminfo 提供 Committed_AS(已提交) 与 CommitLimit(提交上限)。内核在
    启发式 overcommit 模式下仍会给出 CommitLimit, 因此这个比值与 Windows 的
    语义可比; 若内核未导出 CommitLimit (部分容器/精简内核) 则返回 None, 而不是
    拿别的量凑数。
    """
    try:
        vals = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("Committed_AS:", "CommitLimit:")):
                    k, v = line.split(":", 1)
                    parts = v.split()
                    if parts:
                        vals[k] = float(parts[0])  # kB
        committed = vals.get("Committed_AS")
        limit = vals.get("CommitLimit")
        if not committed or not limit:
            return None
        return round(committed * 100.0 / limit, 1)
    except Exception:
        return None


def _commit_percent():
    if IS_WINDOWS:
        return _commit_percent_windows()
    if IS_LINUX:
        return _commit_percent_linux()
    return None  # macOS: 无提交内存概念, 保持 N/A


# ---------------------------------------------------------------------------
# 持久化计量表 (累计能耗 / 电费 / 电价 / 每日明细) —— 跨重启保存于 meter.json
# ---------------------------------------------------------------------------
_METER_DEFAULT = {
    "gpu_energy_wh": 0.0, "cpu_energy_wh": 0.0, "total_energy_wh": 0.0,
    "elec_cost_yuan": 0.0, "elec_rate": 0.60,
    "gpu_hours": 0.0, "gpu_h100_hours": 0.0,
    "first_seen": None, "updated_at": None, "daily": {},
}


class Meter:
    """进程级累计计量，落盘到 meter.json，保证重启后电费/能量累计不丢失。

    GPU 与 CPU 各自的能量按「每次采样与上次采样的差值」累积 (delta 累加)，
    因此天然支持跨重启：文件里保存的是累计总量，采样只往上加增量。
    """

    def __init__(self, path, rate=0.60):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(_METER_DEFAULT)
        self.data["elec_rate"] = rate
        self._load()
        self._last_save = 0

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k, v in _METER_DEFAULT.items():
                    d.setdefault(k, v)
                if not isinstance(d.get("daily"), dict):
                    d["daily"] = {}
                self.data.update(d)
        except Exception:
            pass

    def save(self, force=False):
        now = time.time()
        with self.lock:
            if not force and (now - self._last_save) < 5:
                return
            self.data["updated_at"] = now
            # 限制每日明细最多保留最近 365 天
            daily = self.data.get("daily", {})
            if len(daily) > 400:
                keys = sorted(daily.keys())[-365:]
                self.data["daily"] = {k: daily[k] for k in keys}
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                self._last_save = now
            except Exception:
                pass

    def _recalc(self):
        t = self.data["gpu_energy_wh"] + self.data["cpu_energy_wh"]
        self.data["total_energy_wh"] = t
        self.data["elec_cost_yuan"] = round(t / 1000.0 * self.data["elec_rate"], 4)

    def add_gpu(self, wh):
        if wh <= 0:
            return
        with self.lock:
            if self.data["first_seen"] is None:
                self.data["first_seen"] = time.time()
            self.data["gpu_energy_wh"] += wh
            self._recalc()
            self._add_daily(wh)
        self.save()

    def add_cpu(self, wh):
        if wh <= 0:
            return
        with self.lock:
            if self.data["first_seen"] is None:
                self.data["first_seen"] = time.time()
            self.data["cpu_energy_wh"] += wh
            self._recalc()
            self._add_daily(wh)
        self.save()

    def add_gpu_hours(self, gpu_hours, h100_hours):
        """累计 GPU 运行小时: gpu_hours=原始(GPU数×利用率×时长), h100_hours=H100 等效。

        前者是"本机所有卡按利用率加权后的真实运行小时",后者折算成 H100 等效算力时长。
        """
        if gpu_hours <= 0 and h100_hours <= 0:
            return
        with self.lock:
            if self.data["first_seen"] is None:
                self.data["first_seen"] = time.time()
            self.data["gpu_hours"] += max(0.0, gpu_hours)
            self.data["gpu_h100_hours"] += max(0.0, h100_hours)
        self.save()

    def _add_daily(self, wh):
        day = time.strftime("%Y-%m-%d")
        self.data.setdefault("daily", {})
        self.data["daily"][day] = self.data["daily"].get(day, 0.0) + wh

    def set_rate(self, rate):
        with self.lock:
            self.data["elec_rate"] = float(rate)
            self._recalc()
        self.save(force=True)

    def reset_energy(self):
        with self.lock:
            self.data["gpu_energy_wh"] = 0.0
            self.data["cpu_energy_wh"] = 0.0
            self.data["total_energy_wh"] = 0.0
            self.data["elec_cost_yuan"] = 0.0
            self.data["gpu_hours"] = 0.0
            self.data["gpu_h100_hours"] = 0.0
            self.data["daily"] = {}
            self.data["first_seen"] = time.time()
        self.save(force=True)

    def snapshot(self):
        with self.lock:
            return {
                "gpu_energy_wh": round(self.data["gpu_energy_wh"], 3),
                "cpu_energy_wh": round(self.data["cpu_energy_wh"], 3),
                "total_energy_wh": round(self.data["total_energy_wh"], 3),
                "elec_cost_yuan": round(self.data["elec_cost_yuan"], 4),
                "elec_rate": self.data["elec_rate"],
                "gpu_hours": round(self.data["gpu_hours"], 4),
                "gpu_h100_hours": round(self.data["gpu_h100_hours"], 4),
                "first_seen": self.data["first_seen"],
                "updated_at": self.data["updated_at"],
                "daily": {k: round(v, 3) for k, v in self.data.get("daily", {}).items()},
            }


# ---------------------------------------------------------------------------
# Prefs — 用户偏好设置 (与计量 meter 分离, 落盘 prefs.json)
# ---------------------------------------------------------------------------
_PREFS_DEFAULT = {
    "sampling_interval": 0.5,     # GPU 轮询间隔 (秒)
    "hist_interval": 5.0,         # 历史采样间隔 (秒)
    "hist_retention_days": 30,    # 历史保留天数
    "currency": "¥",              # 货币符号
    "temp_unit": "C",             # C | F
    "power_unit": "W",            # W | kW
    "theme": "auto",              # light | dark | auto
    "refresh_interval": 1.0,      # 前端面板刷新频率 (秒)
    "alert_temp": 0,              # GPU 温度告警阈值 (°C, 0=关闭)
    "alert_util_low": 0,          # GPU 利用率低于该值告警 (%, 0=关闭)
    "autostart": False,           # Windows 开机自启
}


class Prefs:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(_PREFS_DEFAULT)
        self._load()
        self._last_save = 0

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k, v in _PREFS_DEFAULT.items():
                    if k not in d:
                        d[k] = v
                self.data.update(d)
        except Exception:
            pass

    def save(self, force=False):
        now = time.time()
        with self.lock:
            if not force and (now - self._last_save) < 2:
                return
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                self._last_save = now
            except Exception:
                pass

    # 各键的取值范围 / 允许值 (防止把界面设置成无意义的值, 例如采样间隔 1e9)
    LIMITS = {
        "sampling_interval": (0.1, 10.0),
        "hist_interval": (1.0, 300.0),
        "hist_retention_days": (1, 3650),
        "refresh_interval": (0.5, 10.0),
        "alert_temp": (0, 120),
        "alert_util_low": (0, 100),
    }
    ENUMS = {
        "temp_unit": ("C", "F"),
        "power_unit": ("W", "kW"),
        "theme": ("auto", "light", "dark"),
    }

    @staticmethod
    def _coerce(key, val):
        default = _PREFS_DEFAULT.get(key)
        if isinstance(default, bool):
            # 不能直接用 bool(val): 从表单/JSON 字符串传来的 "false" 会被判为 True
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
            return bool(val)
        if isinstance(default, bool) or isinstance(default, int):
            try:
                num = int(float(val))
            except Exception:
                return default
        elif isinstance(default, float):
            try:
                num = float(val)
            except Exception:
                return default
        else:
            s = str(val)
            allowed = Prefs.ENUMS.get(key)
            if allowed and s not in allowed:
                return default          # 非法枚举值回退默认, 不写脏数据
            # 货币符号等自由文本: 限制长度, 避免塞进超长字符串
            return s[:8] if key == "currency" else s
        lo, hi = Prefs.LIMITS.get(key, (None, None))
        if lo is not None and num < lo:
            return lo
        if hi is not None and num > hi:
            return hi
        return num

    def update(self, patch):
        with self.lock:
            for k, v in patch.items():
                if k in _PREFS_DEFAULT:
                    self.data[k] = self._coerce(k, v)
        self.save(force=True)

    def get(self, k, default=None):
        return self.data.get(k, default)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, meter):
        self._last_nvml_reinit = 0.0
        # GPU 不可用时的诊断信息 (供 /api/metrics 的 gpu_unavailable_reason 使用)
        self.init_error = None
        self.init_error_kind = None
        try:
            self._init_nvml()
        except Exception as e:
            # NVML 初始化失败(启动时无驱动/驱动未就绪): 降级运行, 后续由 poll_once 自愈重试
            self.nvml = None
            self.count = 0
            self.handles = []
            self.driver = "unknown"
            self.cuda = "unknown"
            self.init_error = str(e)
            self.init_error_kind = _classify_nvml_error(e)
            _errlog("NVML init failed (%s): %s" % (self.init_error_kind, e))
        self.snapshots = [None] * self.count
        self.lock = threading.Lock()
        self._stop = False
        self.meter = meter  # 持久化计量表
        # 每进程 NVML 利用率增量时间戳 + 缓存
        self._last_proc_ts = [0] * self.count
        self._proc_util = [{} for _ in range(self.count)]  # pid -> {sm,mem,enc,dec}
        # Windows PDH 每进程显存 (后台线程刷新)
        self.proc_mem_mb = [{} for _ in range(self.count)]
        self._mem_lock = threading.Lock()
        # 能耗基线 (驱动实测累计能量, 用于计算采样间隔内的增量)
        self._energy_start = []
        self._gpu_last_energy_wh = []
        # 每 GPU 上次采样时间戳 (用于累计 GPU 运行小时)
        self._gpu_last_ts = []
        self._start_time = time.monotonic()
        for h in self.handles:
            try:
                e0 = self.nvml.nvmlDeviceGetTotalEnergyConsumption(h)
            except Exception:
                e0 = None
            self._energy_start.append(e0)
            self._gpu_last_energy_wh.append((e0 / 3.6e6) if e0 is not None else 0.0)
            self._gpu_last_ts.append(None)

    # ---- NVML 初始化 / 自愈 ----
    def _init_nvml(self):
        """加载 NVML 并获取设备数与句柄。失败抛异常, 由调用方处理。"""
        self.nvml = _load_nvml()
        self.count = self.nvml.nvmlDeviceGetCount()
        self.handles = [self.nvml.nvmlDeviceGetHandleByIndex(i) for i in range(self.count)]
        try:
            self.driver = self.nvml.nvmlSystemGetDriverVersion()
        except Exception:
            self.driver = "unknown"
        try:
            cv = self.nvml.nvmlSystemGetCudaDriverVersion()
            self.cuda = f"{cv // 1000}.{(cv % 1000) // 10}"
        except Exception:
            self.cuda = "unknown"

    def _reinit_nvml_if_needed(self):
        """采样遇到 NVML 异常时, 限频(>=30s)重初始化 NVML。

        典型场景: 驱动中途重启(睡眠唤醒/崩溃恢复/Windows 更新装驱动)后,
        启动时创建的句柄已失效, 表现为 'access violation' / NVML 错误且永久无法识别。
        重初始化可拿到新句柄自愈, 无需手动重启服务。
        返回 True 表示已执行一次重初始化尝试(无论成败), 调用方应放弃本轮、下轮用新句柄全量重采。
        """
        now = time.monotonic()
        if now - self._last_nvml_reinit < 30:
            return False
        self._last_nvml_reinit = now
        try:
            self._init_nvml()
        except Exception:
            return True  # 此刻仍失败(驱动可能还在加载), 下轮再试
        # 重建依赖 handles 的状态
        try:
            self._energy_start = []
            self._gpu_last_energy_wh = []
            self._gpu_last_ts = []
            self._last_proc_ts = [0] * self.count
            self._proc_util = [{} for _ in range(self.count)]
            self.proc_mem_mb = [{} for _ in range(self.count)]
            self.snapshots = [None] * self.count
            for h in self.handles:
                try:
                    e0 = self.nvml.nvmlDeviceGetTotalEnergyConsumption(h)
                except Exception:
                    e0 = None
                self._energy_start.append(e0)
                self._gpu_last_energy_wh.append((e0 / 3.6e6) if e0 is not None else 0.0)
                self._gpu_last_ts.append(None)
            self._start_time = time.monotonic()
        except Exception:
            pass
        return True

    # ---- PDH 后台线程 ----
    def mem_thread(self):
        while not self._stop:
            # 多 GPU 时 PDH 不区分卡, 复制到每个槽位 (消费级通常单卡)
            m, _queried = _query_pdh_proc_mem_cached()
            with self._mem_lock:
                for i in range(self.count):
                    self.proc_mem_mb[i] = m
            time.sleep(2)

    def get_snapshot(self, idx=None):
        with self.lock:
            if idx is None:
                return [s for s in self.snapshots if s is not None]
            return self.snapshots[idx] if 0 <= idx < self.count else None

    def unavailable_reason(self):
        """GPU 数据不可用时的原因说明; 数据可用则返回 None。

        前端据此显示降级提示 (区分"没装依赖" / "驱动未就绪" / "没有 NVIDIA 卡" /
        "NVML 运行时错误"), 避免用户面对一个永远停在「正在连接 NVML …」的空白面板。
        """
        snaps = [s for s in (self.snapshots or []) if s is not None]
        if any("error" not in s for s in snaps):
            return None              # 至少有一张卡正常 -> 数据可用
        if snaps:
            detail = ""
            for s in snaps:
                if "error" in s:
                    detail = str(s["error"])
                    break
            return {"code": "nvml_runtime", "detail": detail}
        if self.count:
            return None              # 已识别到卡, 只是还没采到第一轮快照
        kind = self.init_error_kind
        if not kind or (kind == "unknown" and not self.init_error):
            # NVML 正常初始化但设备数为 0 -> 机器上没有 NVIDIA GPU
            kind = "no_gpu"
            detail = ""      # 没有异常详情, 避免残留上一次的误导信息
        else:
            detail = self.init_error or ""
        return {"code": kind, "detail": detail}

    def poll_once(self):
        if not self.handles:
            # 启动时不支持/无 GPU: 限频尝试重初始化(驱动就绪后自愈)
            if self._reinit_nvml_if_needed():
                return
        for i, h in enumerate(self.handles):
            try:
                snap = self._read_device(h, i)
            except Exception as e:
                snap = {"index": i, "error": str(e)}
                # 句柄可能已失效(驱动中途重启/睡眠唤醒), 限频重初始化自愈
                if self._reinit_nvml_if_needed():
                    return  # 用新句柄下轮全量重采
            with self.lock:
                self.snapshots[i] = snap

    def gpu_cluster_summary(self):
        """服务器算力集群聚合: 多卡 GPU 的算力/功率/数量汇总, 以及折算 H100 等效。"""
        gpus = self.get_snapshot()
        summary = {
            "gpu_count": 0, "total_effective_tflops": 0.0, "total_peak_tflops": 0.0,
            "total_power_w": 0.0, "h100_equiv_effective_tflops": 0.0,
            "h100_equiv_peak_tflops": 0.0, "gpu_names": [],
        }
        for g in gpus:
            if not g or g.get("error"):
                continue
            f = h100_factor(g["name"])
            e = g["compute"]["effective_tflops"]
            pk = g["compute"]["peak_tflops"]
            summary["gpu_count"] += 1
            summary["total_effective_tflops"] += e
            summary["total_peak_tflops"] += pk
            summary["total_power_w"] += (g["power"]["watts"] or 0)
            summary["h100_equiv_effective_tflops"] += e / _H100_TF16_DENSE
            summary["h100_equiv_peak_tflops"] += pk / _H100_TF16_DENSE
            summary["gpu_names"].append(g["name"])
        for k in ("total_effective_tflops", "total_peak_tflops", "total_power_w",
                  "h100_equiv_effective_tflops", "h100_equiv_peak_tflops"):
            summary[k] = round(summary[k], 2)
        return summary

    def _read_device(self, h, idx):
        nv = self.nvml
        name = nv.nvmlDeviceGetName(h)
        try:
            uuid = nv.nvmlDeviceGetUUID(h)
        except Exception:
            uuid = "N/A"

        util = nv.nvmlDeviceGetUtilizationRates(h)
        mem = nv.nvmlDeviceGetMemoryInfo(h)
        power_mw = nv.nvmlDeviceGetPowerUsage(h)
        try:
            power_limit_mw = nv.nvmlDeviceGetEnforcedPowerLimit(h)
        except Exception:
            try:
                power_limit_mw = nv.nvmlDeviceGetPowerManagementLimit(h)
            except Exception:
                power_limit_mw = None
        temp = nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)
        try:
            temp_limit = nv.nvmlDeviceGetTemperatureThreshold(h, nv.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN)
        except Exception:
            temp_limit = 100
        sm_clock = nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_SM)
        mem_clock = nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_MEM)
        try:
            sm_max = nv.nvmlDeviceGetMaxClockInfo(h, nv.NVML_CLOCK_SM)
        except Exception:
            sm_max = None
        try:
            mem_max = nv.nvmlDeviceGetMaxClockInfo(h, nv.NVML_CLOCK_MEM)
        except Exception:
            mem_max = None
        try:
            fan = nv.nvmlDeviceGetFanSpeed(h)
        except Exception:
            fan = None
        try:
            fan_rpm = nv.nvmlDeviceGetFanSpeedRPM(h)
        except Exception:
            fan_rpm = None
        try:
            throttle = nv.nvmlDeviceGetClocksThrottleReasons(h)
        except Exception:
            throttle = 0

        # 媒体引擎
        try:
            enc = nv.nvmlDeviceGetEncoderUtilization(h)[0]
        except Exception:
            enc = None
        try:
            dec = nv.nvmlDeviceGetDecoderUtilization(h)[0]
        except Exception:
            dec = None

        # 性能状态 / PCIe / 能耗 / BAR1
        try:
            pstate = nv.nvmlDeviceGetPerformanceState(h)
        except Exception:
            pstate = None
        try:
            pcie_gen = nv.nvmlDeviceGetCurrPcieLinkGeneration(h)
            pcie_width = nv.nvmlDeviceGetCurrPcieLinkWidth(h)
            pcie_max_gen = nv.nvmlDeviceGetMaxPcieLinkGeneration(h)
        except Exception:
            pcie_gen = pcie_width = pcie_max_gen = None
        try:
            pcie_rx = nv.nvmlDeviceGetPcieThroughput(h, 0) / 1024.0  # KB/s -> MB/s
            pcie_tx = nv.nvmlDeviceGetPcieThroughput(h, 1) / 1024.0
        except Exception:
            pcie_rx = pcie_tx = None
        try:
            bar1 = nv.nvmlDeviceGetBAR1MemoryInfo(h)
            bar1_used = bar1.bar1Used / (1024 ** 3)
            bar1_total = bar1.bar1Total / (1024 ** 3)
        except Exception:
            bar1_used = bar1_total = None
        try:
            cm = nv.nvmlDeviceGetComputeMode(h)
            compute_mode = {0: "Default", 1: "Exclusive Thread",
                            2: "Prohibited", 3: "Exclusive Process"}.get(cm, str(cm))
        except Exception:
            compute_mode = None
        try:
            pcie_replay = nv.nvmlDeviceGetPcieReplayCounter(h)
        except Exception:
            pcie_replay = None
        try:
            # 注意: NVML 返回的单位是毫焦 (mJ), 不是微焦 (uJ)
            energy_uJ = nv.nvmlDeviceGetTotalEnergyConsumption(h)
        except Exception:
            energy_uJ = None

        # ---- 能耗基线自愈 ----
        # 启动瞬间 NVML 可能尚未就绪, 导致 __init__ 取到的基线是 None; 此后
        # 「平均功率(自监控启动)」会永久显示 N/A, 且不会自愈 (自愈只在 NVML
        # 抛异常时触发, 而这里 NVML 一直是正常的)。首次拿到有效读数时补设基线。
        if energy_uJ is not None:
            if self._energy_start[idx] is None:
                self._energy_start[idx] = energy_uJ
                self._start_time = time.monotonic()
            elif energy_uJ < self._energy_start[idx]:
                # 驱动重启 / 计数器回绕: 能耗计数被清零后重新计数, 旧基线会让
                # 差分为负。重置基线, 从本轮重新开始统计, 避免显示负功率。
                self._energy_start[idx] = energy_uJ
                self._start_time = time.monotonic()

        # ---- 持久化累计: 把本次与上次采样的能量差累加到 Meter (跨重启不丢) ----
        if energy_uJ is not None:
            _cur_wh = energy_uJ / 3.6e6
            _delta = _cur_wh - self._gpu_last_energy_wh[idx]
            if _delta > 0:
                self.meter.add_gpu(_delta)
            self._gpu_last_energy_wh[idx] = _cur_wh
        # ---- 累计 GPU 运行小时 (H100 等效) ----
        # 每次采样累加: 利用率(0~1) × 采样间隔(小时) → 该卡本段"加权运行小时",
        # 再乘该卡相对 H100 的算力系数 → H100 等效算力时长。跨重启不丢 (落 meter.json)。
        _now = time.monotonic()
        if self._gpu_last_ts[idx] is not None:
            _dt = _now - self._gpu_last_ts[idx]
            if 0 < _dt < 3600:  # 防异常大跳变 (如进程挂起/时钟回拨)
                _frac = max(0.0, float(util.gpu)) / 100.0
                _gh = _frac * (_dt / 3600.0)
                self.meter.add_gpu_hours(gpu_hours=_gh, h100_hours=_gh * h100_factor(name))
        self._gpu_last_ts[idx] = _now
        _meter_snap = self.meter.snapshot()
        _rate = self.meter.data["elec_rate"]

        # ---- 进程: NVML 运行进程(名称) + 增量利用率 + PDH 显存 ----
        proc_pids = {}  # pid -> {"name":.., "compute":bool}
        for fn, is_compute in (
            (nv.nvmlDeviceGetComputeRunningProcesses, True),
            (nv.nvmlDeviceGetGraphicsRunningProcesses, False),
        ):
            try:
                for p in fn(h):
                    pid = int(p.pid)
                    nm = getattr(p, "name", None) or _proc_name(pid) or f"pid:{pid}"
                    if pid not in proc_pids:
                        proc_pids[pid] = {"name": nm, "compute": is_compute}
                    else:
                        proc_pids[pid]["compute"] = proc_pids[pid]["compute"] or is_compute
            except Exception:
                pass

        # 增量每进程利用率
        try:
            samples = nv.nvmlDeviceGetProcessUtilization(h, self._last_proc_ts[idx])
            for s in samples:
                self._last_proc_ts[idx] = max(self._last_proc_ts[idx], int(s.timeStamp))
                self._proc_util[idx][int(s.pid)] = {
                    "sm": int(s.smUtil), "mem": int(s.memUtil),
                    "enc": int(s.encUtil), "dec": int(s.decUtil),
                }
        except Exception:
            pass

        with self._mem_lock:
            mem_map = self.proc_mem_mb[idx]

        processes = []
        for pid, info in proc_pids.items():
            u = self._proc_util[idx].get(pid, {})
            vram = mem_map.get(pid)
            sm_u = u.get("sm", 0)
            mem_u = u.get("mem", 0)
            enc_u = u.get("enc", 0)
            dec_u = u.get("dec", 0)
            # 过滤: 仅显示真正占用 GPU 的进程
            if not info["compute"] and sm_u == 0 and mem_u == 0 and (vram or 0) < 1:
                continue
            processes.append({
                "pid": pid, "name": info["name"], "compute": info["compute"],
                "sm_percent": sm_u, "mem_percent": mem_u,
                "enc_percent": enc_u, "dec_percent": dec_u,
                "vram_mb": round(vram, 1) if vram is not None else None,
            })
        processes.sort(key=lambda p: (p["vram_mb"] or 0), reverse=True)

        # 收窄 proc_util: 仅保留当前活跃进程 PID, 防止字典随短命进程(GPU 任务频繁启停)无限增长导致内存泄漏/OOM
        _alive = set(proc_pids.keys())
        self._proc_util[idx] = {p: self._proc_util[idx].get(p, {}) for p in _alive}

        spec = GPU_SPECS.get(name, DEFAULT_SPEC)
        gpu_u = float(util.gpu)
        mem_u = float(util.memory)
        mem_used_mb = mem.used / (1024 * 1024)
        mem_total_mb = mem.total / (1024 * 1024)
        power_w = power_mw / 1000.0
        power_limit_w = (power_limit_mw / 1000.0) if power_limit_mw else None

        # 等效算力: 精度阶梯 (峰值 × 利用率), 利用率来自 NVML SM 活跃度计数器
        def eff(key):
            return spec[key] * gpu_u / 100.0
        ladder = [
            ("fp32",            "FP32 (Shader)",                    spec["fp32_tflops"],                          eff("fp32_tflops")),
            ("fp16",            "FP16/BF16 (Shader)",               spec["fp16_tflops"],                         eff("fp16_tflops")),
            ("fp16_tc_dense",   "FP16/BF16 Tensor 稠密",            spec["tensor_fp16_dense_tflops"],            eff("tensor_fp16_dense_tflops")),
            ("fp16_tc_sparse",  "FP16/BF16 Tensor 稀疏(2:4)",      spec["tensor_fp16_sparse_tflops"],           eff("tensor_fp16_sparse_tflops")),
            ("fp8_tc_sparse",   "FP8 Tensor 稀疏",                  spec["tensor_fp8_sparse_tflops"],            eff("tensor_fp8_sparse_tflops")),
            ("fp4_tc_sparse",   "FP4 Tensor 稀疏 (AI TOPS)",       spec["tensor_fp4_sparse_tflops"],            eff("tensor_fp4_sparse_tflops")),
        ]
        live_fp32 = (spec["cuda_cores"] * 2 * (sm_clock / 1e6) * gpu_u / 100.0) if sm_clock else eff("fp32_tflops")
        bw_used = spec["bandwidth_GBps"] * mem_u / 100.0

        # 能耗指标 (驱动实测, 自驱动启动)
        drv_wh = (energy_uJ / 3.6e6) if energy_uJ is not None else None
        avg_power = None
        if energy_uJ is not None and self._energy_start[idx] is not None:
            ej = (energy_uJ - self._energy_start[idx]) / 1e3  # J (原始单位为 mJ)
            el = time.monotonic() - self._start_time
            # ej < 0 说明计数被重置(驱动重启/回绕), 基线已在上面重设, 本轮跳过
            if el > 1 and ej > 0:
                avg_power = ej / el
        # 持久化累计 (跨重启) —— 直接来自 Meter
        gpu_cum_wh = _meter_snap["gpu_energy_wh"]
        gpu_cum_kwh = gpu_cum_wh / 1000.0
        gpu_cum_cost = gpu_cum_kwh * _rate

        return {
            "index": idx, "name": name, "uuid": uuid, "spec": spec,
            "driver": self.driver, "cuda": self.cuda, "timestamp": time.time(),
            "uptime_s": round(time.monotonic() - self._start_time, 1),
            "utilization": {"gpu": round(gpu_u, 1), "memory": round(mem_u, 1)},
            "memory": {
                "used_mb": round(mem_used_mb, 1), "total_mb": round(mem_total_mb, 1),
                "free_mb": round(mem_total_mb - mem_used_mb, 1),
                "used_percent": round(mem_used_mb / mem_total_mb * 100.0, 1) if mem_total_mb else 0,
            },
            "power": {
                "watts": round(power_w, 1),
                "limit_watts": round(power_limit_w, 1) if power_limit_w else None,
                "percent": round(power_w / power_limit_w * 100.0, 1) if power_limit_w else None,
            },
            "temperature": {"c": temp, "limit_c": temp_limit},
            "clocks": {"sm_mhz": sm_clock, "mem_mhz": mem_clock,
                       "sm_max_mhz": sm_max, "mem_max_mhz": mem_max,
                       "sm_headroom_pct": round((sm_max - sm_clock) / sm_max * 100.0, 1) if (sm_max and sm_clock) else None,
                       "mem_headroom_pct": round((mem_max - mem_clock) / mem_max * 100.0, 1) if (mem_max and mem_clock) else None},
            "fan_rpm": fan_rpm,
            "fan_percent": fan, "throttle_reasons": decode_throttle(throttle),
            "media": {"encoder_percent": enc, "decoder_percent": dec},
            "system": {
                "pstate": pstate, "pcie_gen": pcie_gen, "pcie_width": pcie_width,
                "pcie_max_gen": pcie_max_gen, "pcie_rx_mbs": round(pcie_rx, 1) if pcie_rx else None,
                "pcie_tx_mbs": round(pcie_tx, 1) if pcie_tx else None,
                "bar1_used_gib": round(bar1_used, 2) if bar1_used else None,
                "bar1_total_gib": round(bar1_total, 2) if bar1_total else None,
                "energy_wh": round(drv_wh, 3) if drv_wh is not None else None,
                "energy_kwh": round(drv_wh / 1000.0, 6) if drv_wh is not None else None,
                "electricity_cost_yuan": round(gpu_cum_cost, 4),
                "elec_rate_yuan_per_kwh": _rate,
                "energy_wh_since_start": round(gpu_cum_wh, 3),
                "energy_kwh_since_start": round(gpu_cum_kwh, 6),
                "elec_cost_since_start": round(gpu_cum_cost, 4),
                "energy_wh_cum": round(gpu_cum_wh, 3),
                "energy_kwh_cum": round(gpu_cum_kwh, 6),
                "elec_cost_cum": round(gpu_cum_cost, 4),
                "gpu_hours_cum": round(_meter_snap.get("gpu_hours", 0.0), 3),
                "gpu_h100_hours_cum": round(_meter_snap.get("gpu_h100_hours", 0.0), 3),
                "avg_power_since_start_w": round(avg_power, 1) if avg_power else None,
                "compute_mode": compute_mode,
                "pcie_replay_count": pcie_replay,
            },
            "processes": processes,
            "compute": {
                # 头条: 视频生成(扩散模型)以 FP16/BF16 张量矩阵乘为主, 故用稠密档作等效 AI 算力
                "headline_key": "fp16_tc_dense",
                "headline_label": "FP16/BF16 Tensor 稠密",
                "effective_tflops": round(spec["tensor_fp16_dense_tflops"] * gpu_u / 100.0, 2),
                # 实时 H100 等效算力 = 该卡有效算力 ÷ H100 FP16 稠密峰值, 即"相当于几张 H100" (前端 GPU 卡片/详情展示用)
                "h100_equiv_effective_tflops": round(
                    spec["tensor_fp16_dense_tflops"] * gpu_u / 100.0 / _H100_TF16_DENSE, 4),
                "peak_tflops": spec["tensor_fp16_dense_tflops"],
                "effective_fp32_live_tflops": round(live_fp32, 2),
                # 兼容旧字段 (已修正: 旧 effective_tensor_tflops 实为 FP8 稀疏档)
                "effective_tensor_tflops": round(spec["tensor_fp16_dense_tflops"] * gpu_u / 100.0, 2),
                "effective_tensor_sparse_tflops": round(spec["tensor_fp16_sparse_tflops"] * gpu_u / 100.0, 2),
                "effective_fp16_tflops": round(spec["fp16_tflops"] * gpu_u / 100.0, 2),
                "effective_fp32_tflops": round(spec["fp32_tflops"] * gpu_u / 100.0, 2),
                "ladder": [
                    {"key": k, "label": lab, "peak_tflops": round(pk, 2), "effective_tflops": round(e, 2)}
                    for (k, lab, pk, e) in ladder
                ],
                "bandwidth_used_GBps": round(bw_used, 1),
                "bandwidth_peak_GBps": spec["bandwidth_GBps"],
            },
        }

    def run(self, prefs):
        # 启动 PDH 线程
        t = threading.Thread(target=self.mem_thread, daemon=True)
        t.start()
        while not self._stop:
            self.poll_once()
            # 采样间隔可由设置页动态调整 (prefs.sampling_interval)
            try:
                iv = float(prefs.get("sampling_interval", 0.5))
            except Exception:
                iv = 0.5
            if iv < 0.1:
                iv = 0.1
            time.sleep(iv)


# ---------------------------------------------------------------------------
# LibreHardwareMonitor 客户端
# 用来读取 Windows 上 WMI 不暴露的数据: CPU 封装温度、CPU 封装功率、内存温度。
# LHM 提供两种读取方式: 内置 HTTP 服务 (默认 8085/data.json) 与 WMI 命名空间
# (root\LibreHardwareMonitor)。本客户端两者都尝试, 优先 HTTP。
# 若 LHM 未安装/未运行, 所有字段返回 None, 上层保持原 N/A / 估算 行为。
# ---------------------------------------------------------------------------
class LHMClient:
    def __init__(self, http_port=8085, poll_interval=4.0):
        self.http_port = http_port
        self.poll_interval = poll_interval
        self.available = False
        self.method = None          # 'http' | 'wmi' | None
        self.cpu_temp_c = None
        self.cpu_power_w = None
        self.mem_temp_c = None
        self._last = 0
        self._lock = threading.Lock()
        self._probe_once()
        self._consec_fail = 0 if self.available else 1

    def _set(self, avail, method, ct, cp, mt):
        with self._lock:
            self.available, self.method = avail, method
            self.cpu_temp_c, self.cpu_power_w, self.mem_temp_c = ct, cp, mt

    def _probe_once(self):
        # 1) HTTP 接口
        try:
            import urllib.request
            url = f"http://localhost:{self.http_port}/data.json"
            req = urllib.request.Request(url, headers={"User-Agent": "gpu-monitor"})
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
            res = self._parse_http(data)
            if res["cpu_temp"] is not None or res["cpu_power"] is not None:
                self._set(True, "http", res["cpu_temp"], res["cpu_power"], res["mem_temp"])
                return
        except Exception:
            pass
        # 2) WMI 接口
        try:
            res = self._query_wmi()
            if res["cpu_temp"] is not None or res["cpu_power"] is not None:
                self._set(True, "wmi", res["cpu_temp"], res["cpu_power"], res["mem_temp"])
                return
        except Exception:
            pass
        self._set(False, None, None, None, None)

    def _parse_http(self, nodes):
        cpu_temp = cpu_power = mem_temp = None

        def walk(ns):
            nonlocal cpu_temp, cpu_power, mem_temp
            for n in (ns or []):
                name = (n.get("Text") or "").lower()
                st = n.get("SensorType")
                val = n.get("Value")
                if st == "Temperature" and val is not None:
                    if "cpu" in name:
                        cpu_temp = val if cpu_temp is None else max(cpu_temp, val)
                    if "memory" in name or "dram" in name:
                        mem_temp = val if mem_temp is None else max(mem_temp, val)
                elif st == "Power" and val is not None:
                    if "cpu" in name and ("package" in name or "total" in name or cpu_power is None):
                        cpu_power = val
                for ch in (n.get("Children") or []):
                    walk([ch])
        walk(nodes)
        return {"cpu_temp": cpu_temp, "cpu_power": cpu_power, "mem_temp": mem_temp}

    def _query_wmi(self):
        script = (
            "Get-CimInstance -Namespace root\\LibreHardwareMonitor -ClassName Sensor | "
            "Where-Object { $_.SensorType -in @('Temperature','Power') } | "
            "Select-Object Name, SensorType, Value | ConvertTo-Json -Compress"
        )
        out = _wmi_json(script)
        cpu_temp = cpu_power = mem_temp = None
        if out:
            try:
                arr = json.loads(out)
                if isinstance(arr, dict):
                    arr = [arr]
                for s in arr:
                    name = (s.get("Name") or "").lower()
                    st = s.get("SensorType")
                    val = s.get("Value")
                    if val is None:
                        continue
                    if st == "Temperature":
                        if "cpu" in name:
                            cpu_temp = val if cpu_temp is None else max(cpu_temp, val)
                        if "memory" in name or "dram" in name:
                            mem_temp = val if mem_temp is None else max(mem_temp, val)
                    elif st == "Power":
                        if "cpu" in name and ("package" in name or "total" in name or cpu_power is None):
                            cpu_power = val
            except Exception:
                pass
        return {"cpu_temp": cpu_temp, "cpu_power": cpu_power, "mem_temp": mem_temp}

    # 探测失败后的退避上限(秒): LHM 未安装时, 原来的实现会每 poll_interval
    # 就重试一次(实际是每 2 秒 spawn 一个 PowerShell 进程), 长期空转很浪费。
    # 失败越多、间隔越长(指数退避), 一旦成功立即恢复正常频率。
    BACKOFF_MAX = 300.0

    def update(self):
        now = time.monotonic()
        with self._lock:
            backoff = 0.0 if self.available else min(
                self.BACKOFF_MAX, self.poll_interval * (2 ** min(self._consec_fail, 6)))
            due = (now - self._last) > max(self.poll_interval, backoff)
        if not due:
            return
        self._probe_once()
        with self._lock:
            self._last = now
            if self.available:
                self._consec_fail = 0
            else:
                self._consec_fail += 1

    def snapshot(self):
        with self._lock:
            return {
                "available": self.available, "method": self.method,
                "cpu_temp_c": self.cpu_temp_c, "cpu_power_w": self.cpu_power_w,
                "mem_temp_c": self.mem_temp_c,
            }


class LinuxThermal:
    """Linux 温度/功率后端 (接口与 LHMClient 一致: update / snapshot)。

    - 温度: hwmon (coretemp / k10temp / zenpower / cpu_thermal), 通常无需 root。
    - 封装功率: RAPL energy_uj 差分 (intel-rapl:0 / amd_rapl:0); 多数发行版该文件
      仅 root 可读, 无权限时 cpu_power_w 返回 None, 上层自动走 TDP 估算降级。
    """

    def __init__(self, poll_interval=4.0):
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self.available = False
        self.method = None
        self.cpu_temp_c = None
        self.cpu_power_w = None
        self.mem_temp_c = None
        self._last = 0.0
        self._rapl_prev = None
        self._rapl_prev_t = None

    # hwmon 设备名 -> 是否 CPU 温度传感器。用子串匹配而非全等: 同一驱动在不同
    # 内核/平台上可能叫 coretemp / coretemp.0 / cpu-thermal / soc_thermal。
    _CPU_TEMP_NAMES = ("coretemp", "k10temp", "zenpower", "cpu_thermal",
                       "cpu-thermal", "soc_thermal", "k8temp")

    @staticmethod
    def _normalize_milli_c(v):
        """毫摄氏度 -> 摄氏度。极少数驱动直接以摄氏度上报, 用数量级区分。"""
        return round(v / 1000.0 if abs(v) > 1000 else float(v), 1)

    @classmethod
    def _read_hwmon_temp(cls):
        try:
            base = "/sys/class/hwmon"
            for h in sorted(os.listdir(base)):
                p = os.path.join(base, h)
                name = ""
                try:
                    with open(os.path.join(p, "name")) as f:
                        name = f.read().strip().lower()
                except Exception:
                    pass
                if not any(k in name for k in cls._CPU_TEMP_NAMES):
                    continue
                # 直接尝试打开, 不做 os.path.exists 预检: 预检与打开之间存在
                # TOCTOU 窗口, 而这里的 try/except 已经能处理"文件不存在"。
                for f in ("temp1_input", "temp0_input", "temp_input"):
                    try:
                        with open(os.path.join(p, f)) as fh:
                            return cls._normalize_milli_c(int(fh.read().strip()))
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    @staticmethod
    def _read_thermal_zone_temp():
        """回退温度源: /sys/class/thermal/thermal_zone*/temp。

        hwmon 白名单覆盖不到的平台 (树莓派 / 多数 ARM 开发板 / 部分虚拟机) 只暴露
        thermal_zone。取舍: 优先取 type 带 cpu / soc / pkg 的分区; 一个都匹配不上
        时取所有分区的最高值 —— 对"整机有没有过热"这个用途来说, 宁可报偏高也不报
        缺失。有匹配项时只用匹配项, 不把 GPU / 电池的分区温度冒充成 CPU 温度。
        """
        try:
            base = "/sys/class/thermal"
            zones = []
            for z in sorted(os.listdir(base)):
                if not z.startswith("thermal_zone"):
                    continue
                p = os.path.join(base, z)
                try:
                    with open(os.path.join(p, "temp")) as f:
                        c = LinuxThermal._normalize_milli_c(int(f.read().strip()))
                    t = ""
                    try:
                        with open(os.path.join(p, "type")) as f:
                            t = f.read().strip().lower()
                    except Exception:
                        pass
                    if 0 < c < 150:  # 过滤 0 / 负数等"不支持"占位值
                        zones.append((t, c))
                except Exception:
                    continue
            if not zones:
                return None
            preferred = [c for t, c in zones if any(k in t for k in ("cpu", "soc", "pkg", "x86"))]
            if preferred:
                return max(preferred)
            return max(c for _, c in zones)
        except Exception:
            return None

    @classmethod
    def _read_cpu_temp(cls):
        t = cls._read_hwmon_temp()
        if t is not None:
            return t
        return cls._read_thermal_zone_temp()

    @staticmethod
    def _read_rapl_energy():
        for cand in ("intel-rapl:0", "intel-rapl:0:0", "amd_rapl:0"):
            fp = os.path.join("/sys/class/powercap", cand, "energy_uj")
            if not os.path.exists(fp):
                continue
            try:
                with open(fp) as f:
                    return int(f.read().strip())
            except Exception:
                return None
        return None

    def update(self):
        now = time.monotonic()
        with self._lock:
            due = (not self.available) or (now - self._last > self.poll_interval)
        if not due:
            return
        temp = self._read_cpu_temp()
        energy = self._read_rapl_energy()
        pw = None
        if energy is not None and self._rapl_prev is not None and self._rapl_prev_t:
            dt = now - self._rapl_prev_t
            if dt > 0:
                w = round((energy - self._rapl_prev) / dt / 1e6, 1)  # uJ/s -> W
                if 0 < w < 5000:  # 合理范围过滤 (计数器回绕/异常)
                    pw = w
        self._rapl_prev = energy
        self._rapl_prev_t = now
        with self._lock:
            if temp is not None or energy is not None:
                self.available = True
                self.method = "hwmon/rapl"
            self.cpu_temp_c = temp
            self.cpu_power_w = pw
            self._last = now

    def snapshot(self):
        with self._lock:
            return {
                "available": self.available, "method": self.method,
                "cpu_temp_c": self.cpu_temp_c, "cpu_power_w": self.cpu_power_w,
                "mem_temp_c": self.mem_temp_c,
            }


def _make_lhm():
    if IS_WINDOWS:
        try:
            return LHMClient()
        except Exception:
            return None
    if IS_LINUX:
        # 温度/功率走 hwmon + RAPL (接口兼容 LHMClient)
        try:
            return LinuxThermal()
        except Exception:
            return None
    return None  # macOS / 其他: 暂无温度/功率后端, 走 TDP 估算降级


# ---------------------------------------------------------------------------
# 系统 (CPU / 内存) 采集
# 说明: Windows 下 CPU 温度 / 内存温度 WMI 通常不暴露 (需 LibreHardwareMonitor
#       等第三方), 故温度字段可能为 None; CPU 功率为基于 TDP 的估算; 电压来自
#       WMI CurrentVoltage (VID, 单位非标准, 仅供参考); PCIe 实时传输量通过
#       psutil 磁盘累计字节差分得到 (即走 PCIe 的存储吞吐)。
#       若安装了 LibreHardwareMonitor, 会优先用其读取真实 CPU 温度/功率与内存温度。
# ---------------------------------------------------------------------------
SYS_STATIC = {
    "sockets": 1,
    # 每路 CPU (多路服务器): {name, cores, threads, max_mhz, voltage_v, tdp_w}
    "cpus": [],
    "cpu_name": None, "cpu_tdp_w": 125.0, "ram_speed_mhz": None,
    "cpu_voltage_v": None, "cpu_cores": None, "cpu_threads": None,
    "cpu_max_mhz": None, "ram_total_gib": None,
}


# PowerShell 的控制台输出编码默认是系统 OEM 代码页 (简体中文 Windows 为 936/GBK),
# 而 WMI 可能返回非 ASCII 内容 (中文 CPU 型号、中文内存品牌、中文设备名)。若不显式
# 设置, Python 侧按 UTF-8 解码会得到乱码甚至替换字符 —— 界面上表现为"锟斤拷"式的
# CPU 名称。在每个脚本前强制把输出编码切成 UTF-8, 才能与下文的 decode("utf-8") 对齐。
_PS_UTF8_PREFIX = (
    "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
)


def _wmi_json(script):
    if not IS_WINDOWS:
        return None  # WMI 为 Windows 专属; 其他平台走 psutil / sysfs 后端
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            _PS_UTF8_PREFIX + script],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return r.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def _guess_tdp(name):
    def _core_guess():
        n = (psutil.cpu_count(logical=False) if psutil else None) or 8
        return float(max(65, n * 8))
    if not name:
        return _core_guess()
    n = (name or "").upper()
    table = {
        "9950X": 170, "9900X": 120, "9700X": 65, "9600X": 65,
        "7950X": 170, "7900X": 170, "7800X3D": 120, "7700X": 105, "7600X": 105,
        "5950X": 105, "5900X": 105, "5800X3D": 105, "5600X": 65,
        "14900K": 125, "13900K": 125, "13700K": 125, "13600K": 125, "12900K": 125,
        "RYZEN 9": 105, "RYZEN 7": 65, "RYZEN 5": 65,
        "CORE I9": 125, "CORE I7": 125, "CORE I5": 65,
    }
    for k, v in table.items():
        if k in n:
            return float(v)
    return _core_guess()


def _linux_cpu_info():
    """Linux 多路 CPU: 解析 /proc/cpuinfo, 按 physical id 分组 (对称多路常见)。

    每路汇总: 型号 / 物理核心数 (core id 去重) / 线程数 / 最高频率 (MHz)。
    /proc/cpuinfo 的 cpu MHz 是当前频率, 尽量用 cpufreq cpuinfo_max_freq 提升。
    """
    cpus = []
    phys, order = {}, []

    def _flush(cur):
        if "processor" not in cur:
            return
        pid = cur.get("physical id", "0")
        if pid not in phys:
            phys[pid] = {"name": cur.get("model name")
                         or cur.get("Hardware") or cur.get("hardware")
                         or "CPU",
                         "core_ids": set(), "threads": 0, "max_mhz": 0,
                         "procs": []}
            order.append(pid)
        core_id = cur.get("core id")
        if core_id is None or core_id == "":
            # aarch64 (以及部分 ARM 平台) 不提供 physical id / core id:
            # 退化用 processor 编号作为核心标识。若仍用 "(physical id, core id)"
            # 作 key, 所有逻辑核都会塌缩成同一个 key, 核心数被误算成 1。
            key = ("processor", cur.get("processor"))
        else:
            key = (cur.get("physical id", "0"), core_id)
        phys[pid]["core_ids"].add(key)
        phys[pid]["procs"].append(cur.get("processor", ""))
        try:
            phys[pid]["max_mhz"] = max(phys[pid]["max_mhz"],
                                       int(float(cur.get("cpu MHz", 0) or 0)))
        except Exception:
            pass
        phys[pid]["threads"] += 1

    try:
        cur = {}
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    _flush(cur)
                    cur = {}
                    continue
                k, _, v = line.partition(":")
                cur[k.strip()] = v.strip()
        _flush(cur)  # 末尾无空行时收尾最后一块
    except Exception:
        return cpus
    for pid in order:
        p = phys[pid]
        name = p["name"].strip()
        # 尝试 cpufreq 标称最高频率 (kHz -> MHz); 失败则回退到当前频率估算。
        # 必须用真实的 processor 编号 —— 此前用 range(threads) 从 0 开始拼路径,
        # 多路机器上第二路会读到第一路的 cpufreq 文件。
        mhz = None
        for proc_id in p.get("procs") or []:
            try:
                fp = "/sys/devices/system/cpu/cpu%s/cpufreq/cpuinfo_max_freq" % proc_id
                with open(fp) as f:
                    mhz = int(f.read().strip()) // 1000
                break
            except Exception:
                continue
        cpus.append({
            "name": name,
            "cores": len(p["core_ids"]) or p["threads"],
            "threads": p["threads"],
            "max_mhz": mhz or (p["max_mhz"] or 3000),
            "voltage_v": None,
            "tdp_w": _guess_tdp(name),
        })
    return cpus


def _init_sys_static():
    s = SYS_STATIC
    if psutil:
        s["cpu_cores"] = psutil.cpu_count(logical=False)
        s["cpu_threads"] = psutil.cpu_count(logical=True)
    cpus = []
    if IS_WINDOWS:
        # 多路 CPU: Win32_Processor 每路返回一条记录, 逐路解析型号/核心/线程/频率
        out = _wmi_json("Get-CimInstance Win32_Processor | Select Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentVoltage | ConvertTo-Json")
        try:
            if out:
                d = json.loads(out)
                if isinstance(d, dict):
                    d = [d]
                for item in d:
                    name = (item.get("Name") or "").strip()
                    cores = int(item.get("NumberOfCores") or 0) or None
                    threads = int(item.get("NumberOfLogicalProcessors") or 0) or None
                    max_mhz = int(item.get("MaxClockSpeed") or 0) or None
                    cv = item.get("CurrentVoltage")
                    volt = round(cv * 0.1, 3) if cv else None
                    cpus.append({
                        "name": name, "cores": cores, "threads": threads,
                        "max_mhz": max_mhz, "voltage_v": volt,
                        "tdp_w": _guess_tdp(name),
                    })
        except Exception:
            pass
    elif IS_LINUX:
        cpus = _linux_cpu_info()
    # macOS / 其他: 走下方 psutil 单路兜底
    if not cpus:
        # 兜底: 无 WMI / 无 /proc/cpuinfo 时按 psutil 总核心数估一个单路
        n = psutil.cpu_count(logical=False) or 8
        cpus.append({
            "name": "CPU", "cores": n, "threads": psutil.cpu_count(logical=True),
            "max_mhz": 3000, "voltage_v": None, "tdp_w": _guess_tdp(None),
        })
    s["cpus"] = cpus
    s["sockets"] = len(cpus)
    s["cpu_cores"] = sum(c["cores"] or 0 for c in cpus) or (psutil.cpu_count(logical=False) or 8)
    s["cpu_threads"] = sum(c["threads"] or 0 for c in cpus) or (psutil.cpu_count(logical=True) or 8)
    s["cpu_name"] = cpus[0]["name"]
    s["cpu_max_mhz"] = cpus[0]["max_mhz"]
    s["cpu_voltage_v"] = cpus[0]["voltage_v"]
    # 总 TDP = 各路 TDP 之和 (服务器多路叠加)
    s["cpu_tdp_w"] = sum(c["tdp_w"] or 0 for c in cpus) or 125.0
    # 内存容量: Windows 走 WMI; 其他平台直接 psutil (macOS 统一内存也等于系统 RAM)
    if IS_WINDOWS:
        out = _wmi_json("Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum | Select -ExpandProperty Sum")
        try:
            if out and str(out).strip().isdigit():
                s["ram_total_gib"] = round(int(str(out).strip()) / (1024 ** 3), 1)
        except Exception:
            pass
    elif psutil:
        s["ram_total_gib"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    if IS_WINDOWS:
        out = _wmi_json("Get-CimInstance Win32_PhysicalMemory | Select -First 1 Speed | ConvertTo-Json")
        try:
            if out:
                d = json.loads(out)
                sp = d.get("Speed") if isinstance(d, dict) else None
                if sp:
                    s["ram_speed_mhz"] = int(sp)
        except Exception:
            pass


def _cpu_peaks():
    avx2 = avx512 = 0.0
    # 逐路 CPU 叠加 (服务器多路): 每路 核心数 × 最高频率 × 每周期 FLOP
    for cpu in SYS_STATIC["cpus"]:
        cores = cpu["cores"] or 8
        ghz = (cpu["max_mhz"] or 3000) / 1000.0
        # 每核心每周期: AVX2(256-bit)=16 FLOP, AVX-512(512-bit)=32 FLOP (单 FMA 单元)
        avx2 += cores * ghz * 16.0
        avx512 += cores * ghz * 32.0
    return avx2, avx512


class SysMonitor:
    def __init__(self, meter):
        _init_sys_static()
        self.lock = threading.Lock()
        self.meter = meter
        self.cpu = {
            "util": 0.0, "per_core": [], "freq": None, "temp": None,
            "voltage_v": SYS_STATIC["cpu_voltage_v"], "power_w": None,
            "sockets": [],  # 每路 CPU: {name,cores,threads,util,per_core,power_w,tdp_w}
        }
        self._cpu_last_acc_t = time.monotonic()
        self.mem = {
            "percent": 0.0, "used_gib": 0.0, "total_gib": SYS_STATIC["ram_total_gib"] or 0.0,
            "available_gib": 0.0, "speed_mhz": SYS_STATIC["ram_speed_mhz"],
            "temp": None, "load_percent": None, "pcie_read_mbs": None, "pcie_write_mbs": None,
        }
        self._stop = False
        self._last_disk = None
        self._last_disk_t = 0
        # 网络 / 磁盘 I/O 差分状态 (io_thread 使用)
        self._last_net = None
        self._last_net_t = 0
        self._last_disk_io = None
        self._last_disk_io_t = 0
        # 每网卡 / 每物理磁盘 差分状态 (进阶版明细)
        self._last_net_pernic = {}
        self._last_net_pernic_t = 0
        self._last_disk_perdisk = {}
        self._last_disk_perdisk_t = 0
        self.net = {"rx_mbs": None, "tx_mbs": None, "ifaces": []}
        self.disk = {"read_mbs": None, "write_mbs": None, "parts": [], "disks": []}
        # LibreHardwareMonitor 客户端 (真实 CPU 温度/功率, 内存温度); 无则 None
        self.lhm = _make_lhm()
        threading.Thread(target=self._lhm_loop, daemon=True).start()
        threading.Thread(target=self.io_thread, daemon=True).start()

    # ---- LHM 轮询线程: 每 ~2s 触发一次 (内部按 poll_interval 节流) ----
    def _lhm_loop(self):
        while not self._stop:
            try:
                if self.lhm is not None:
                    self.lhm.update()
            except Exception:
                pass
            time.sleep(2)

    # ---- CPU 线程: 每 ~1s ----
    def cpu_thread(self):
        while not self._stop:
            try:
                per = psutil.cpu_percent(interval=1.0, percpu=True)
                util = (sum(per) / len(per)) if per else 0.0
                freq = psutil.cpu_freq()
                # 逐路 CPU: 把逻辑核心按每路线程数切片 (对称多路假设), 各路独立估算功率
                sockets = []
                idx = 0
                for cpu in SYS_STATIC["cpus"]:
                    n = cpu["threads"] or (cpu["cores"] or 1)
                    chunk = per[idx:idx + n]
                    idx += n
                    su = (sum(chunk) / len(chunk)) if chunk else 0.0
                    tdp = cpu["tdp_w"] or 125.0
                    idle = max(15.0, tdp * 0.15)
                    u = su / 100.0
                    spw = idle + (tdp * 1.3 - idle) * (u ** 1.1)  # 估算到 ~1.3×TDP
                    sockets.append({
                        "name": cpu["name"], "cores": cpu["cores"], "threads": cpu["threads"],
                        "util": round(su, 1), "per_core": [round(x, 1) for x in chunk],
                        "power_w": round(spw, 1), "tdp_w": tdp,
                    })
                total_pw = sum(s["power_w"] for s in sockets)
                now = time.monotonic()
                dt = now - self._cpu_last_acc_t
                if dt > 0:
                    self.meter.add_cpu(total_pw * dt / 3600.0)  # Wh
                    self._cpu_last_acc_t = now
                with self.lock:
                    self.cpu["util"] = round(util, 1)
                    self.cpu["per_core"] = [round(x, 1) for x in per]
                    self.cpu["freq"] = round(freq.current, 0) if (freq and freq.current) else None
                    self.cpu["power_w"] = round(total_pw, 1)
                    self.cpu["sockets"] = sockets
            except Exception:
                time.sleep(1)

    # ---- 内存线程: 每 ~2s ----
    def mem_thread(self):
        while not self._stop:
            try:
                if psutil:
                    vm = psutil.virtual_memory()
                    now = time.monotonic()
                    io = psutil.disk_io_counters()
                    with self.lock:
                        self.mem["percent"] = round(vm.percent, 1)
                        self.mem["used_gib"] = round(vm.used / (1024 ** 3), 1)
                        self.mem["available_gib"] = round(vm.available / (1024 ** 3), 1)
                        if self._last_disk and io:
                            dt = now - self._last_disk_t
                            if dt > 0:
                                self.mem["pcie_read_mbs"] = round(
                                    (io.read_bytes - self._last_disk[0]) / dt / 1e6, 1)
                                self.mem["pcie_write_mbs"] = round(
                                    (io.write_bytes - self._last_disk[1]) / dt / 1e6, 1)
                        if io:
                            self._last_disk = (io.read_bytes, io.write_bytes)
                            self._last_disk_t = now
                # 提交内存占比 (负载): 语义是"已提交内存 / 提交上限", 与任务管理器
                # "已提交 X / Y GB" 的百分比是同一个量。
                #   Windows -> kernel32.GlobalMemoryStatusEx (进程内, 零子进程)
                #   Linux   -> /proc/meminfo 的 Committed_AS / CommitLimit
                #   macOS   -> 无等价概念, 返回 None, 前端显示 N/A (不伪造数值)
                # 早期实现为每 2 秒 spawn 一个 PowerShell 读性能计数器, 已改为上面的
                # 进程内实现, 顺带消除了 Windows 上的子进程开销。
                _cp = _commit_percent()
                if _cp is not None:
                    with self.lock:
                        self.mem["load_percent"] = _cp
            except Exception:
                pass
            time.sleep(2)

    # ---- 网络 / 磁盘 I/O 线程: 每 ~2s ----
    def io_thread(self):
        while not self._stop:
            try:
                now = time.monotonic()
                # 网络: 所有网卡合计收发字节差分 -> MB/s
                try:
                    n = psutil.net_io_counters()
                    if n:
                        if self._last_net:
                            dt = now - self._last_net_t
                            if dt > 0:
                                with self.lock:
                                    self.net["rx_mbs"] = round((n.bytes_recv - self._last_net[0]) / dt / 1e6, 1)
                                    self.net["tx_mbs"] = round((n.bytes_sent - self._last_net[1]) / dt / 1e6, 1)
                        self._last_net = (n.bytes_recv, n.bytes_sent)
                        self._last_net_t = now
                except Exception:
                    pass
                # 每网卡明细: 收发速率 + IP + 链路速率 (进阶版)
                try:
                    n_per = psutil.net_io_counters(pernic=True)
                    if n_per:
                        dt = now - self._last_net_pernic_t
                        addrs = psutil.net_if_addrs()
                        stats = psutil.net_if_stats()
                        ifaces = []
                        for name, c in n_per.items():
                            prev = self._last_net_pernic.get(name)
                            rx = tx = None
                            if prev and dt > 0:
                                rx = round((c.bytes_recv - prev[0]) / dt / 1e6, 1)
                                tx = round((c.bytes_sent - prev[1]) / dt / 1e6, 1)
                            ip = ""
                            sa = addrs.get(name, [])
                            for sn in sa:
                                if sn.family == socket.AF_INET:
                                    ip = sn.address
                                    break
                            if not ip:
                                for sn in sa:
                                    if sn.family == socket.AF_INET6:
                                        ip = sn.address
                                        break
                            sp = stats.get(name)
                            speed = sp.speed if sp else 0
                            up = sp.isup if sp else None
                            ifaces.append({"name": name, "rx_mbs": rx, "tx_mbs": tx,
                                           "ip": ip, "speed_mbps": speed or 0, "up": bool(up)})
                        with self.lock:
                            self.net["ifaces"] = ifaces
                        self._last_net_pernic = {k: (c.bytes_recv, c.bytes_sent) for k, c in n_per.items()}
                        self._last_net_pernic_t = now
                except Exception:
                    pass
                # 磁盘 I/O: 所有物理盘合计读写字节差分 -> MB/s
                try:
                    d = psutil.disk_io_counters()
                    if d:
                        if self._last_disk_io:
                            dt = now - self._last_disk_io_t
                            if dt > 0:
                                with self.lock:
                                    self.disk["read_mbs"] = round((d.read_bytes - self._last_disk_io[0]) / dt / 1e6, 1)
                                    self.disk["write_mbs"] = round((d.write_bytes - self._last_disk_io[1]) / dt / 1e6, 1)
                        self._last_disk_io = (d.read_bytes, d.write_bytes)
                        self._last_disk_io_t = now
                except Exception:
                    pass
                # 每物理磁盘读写速率明细 (进阶版)
                try:
                    d_per = psutil.disk_io_counters(perdisk=True)
                    if d_per:
                        dt = now - self._last_disk_perdisk_t
                        disks = []
                        for name, c in d_per.items():
                            prev = self._last_disk_perdisk.get(name)
                            rd = wr = None
                            if prev and dt > 0:
                                rd = round((c.read_bytes - prev[0]) / dt / 1e6, 1)
                                wr = round((c.write_bytes - prev[1]) / dt / 1e6, 1)
                            disks.append({"name": name, "read_mbs": rd, "write_mbs": wr})
                        with self.lock:
                            self.disk["disks"] = disks
                        self._last_disk_perdisk = {k: (c.read_bytes, c.write_bytes) for k, c in d_per.items()}
                        self._last_disk_perdisk_t = now
                except Exception:
                    pass
                # 磁盘容量使用率: 各挂载点
                try:
                    parts = []
                    for p in psutil.disk_partitions(all=False):
                        try:
                            u = psutil.disk_usage(p.mountpoint)
                            parts.append({
                                "mount": p.mountpoint,
                                "fstype": p.fstype,
                                "total_gib": round(u.total / (1024 ** 3), 1),
                                "used_gib": round(u.used / (1024 ** 3), 1),
                                "pct": round(u.percent, 1),
                            })
                        except Exception:
                            continue
                    with self.lock:
                        self.disk["parts"] = parts
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(2)

    def io_snapshot(self):
        with self.lock:
            return {
                "net": dict(self.net),
                "disk": {
                    "read_mbs": self.disk["read_mbs"],
                    "write_mbs": self.disk["write_mbs"],
                    "parts": list(self.disk["parts"]),
                    "disks": list(self.disk["disks"]),
                },
            }

    def cpu_snapshot(self):
        with self.lock:
            c = dict(self.cpu)
        avx2, avx512 = _cpu_peaks()
        util = c.get("util") or 0.0
        m = self.meter.snapshot()
        rate = self.meter.data["elec_rate"]
        cpu_cum_wh = m["cpu_energy_wh"]
        lhm = self.lhm.snapshot() if self.lhm else {"available": False}
        cpu_temp = lhm.get("cpu_temp_c") if (lhm.get("available") and lhm.get("cpu_temp_c") is not None) else None
        real_pw = lhm.get("cpu_power_w") if (lhm.get("available") and lhm.get("cpu_power_w") is not None) else None
        power_est = real_pw is None
        pw = real_pw if real_pw is not None else c.get("power_w")
        sockets = c.get("sockets") or []
        socket_list = [{
            "name": s["name"], "cores": s["cores"], "threads": s["threads"],
            "utilization": s["util"], "per_core": s["per_core"],
            "power_w": s["power_w"], "tdp_w": s["tdp_w"],
        } for s in sockets]
        total_cores = sum((s["cores"] or 0) for s in sockets) or (SYS_STATIC["cpu_cores"] or 0)
        total_threads = sum((s["threads"] or 0) for s in sockets) or (SYS_STATIC["cpu_threads"] or 0)
        return {
            "name": SYS_STATIC["cpu_name"], "cores": SYS_STATIC["cpu_cores"],
            "threads": SYS_STATIC["cpu_threads"], "max_mhz": SYS_STATIC["cpu_max_mhz"],
            "tdp_w": SYS_STATIC["cpu_tdp_w"],
            "utilization": c.get("util"), "per_core": c.get("per_core"),
            "frequency_mhz": c.get("freq"), "temperature_c": cpu_temp,
            "voltage_v": c.get("voltage_v"),
            "power_w": pw, "power_estimated": power_est,
            "energy_wh": round(cpu_cum_wh, 3),
            "energy_kwh": round(cpu_cum_wh / 1000.0, 6),
            "electricity_cost_yuan": round(cpu_cum_wh / 1000.0 * rate, 4),
            "elec_rate_yuan_per_kwh": rate,
            "compute": {
                "avx2_peak_gflops": round(avx2, 1),
                "avx512_peak_gflops": round(avx512, 1),
                "avx2_effective_gflops": round(avx2 * util / 100.0, 1),
                "avx512_effective_gflops": round(avx512 * util / 100.0, 1),
            },
            "sockets": socket_list,
            "cluster": {
                "sockets": len(socket_list),
                "total_cores": total_cores,
                "total_threads": total_threads,
                "total_tdp_w": SYS_STATIC["cpu_tdp_w"],
                "total_avx2_peak_gflops": round(avx2, 1),
                "total_avx512_peak_gflops": round(avx512, 1),
                "total_avx2_effective_gflops": round(avx2 * util / 100.0, 1),
                "total_avx512_effective_gflops": round(avx512 * util / 100.0, 1),
            },
            "lhm": lhm,
            "temperature_source": (lhm.get("method") or "LibreHardwareMonitor") if cpu_temp is not None else None,
            "power_source": (lhm.get("method") or "LibreHardwareMonitor") if not power_est else "TDP 估算",
            "timestamp": time.time(),
        }

    def mem_snapshot(self):
        with self.lock:
            m = dict(self.mem)
        lhm = self.lhm.snapshot() if self.lhm else {"available": False}
        mem_temp = lhm.get("mem_temp_c") if (lhm.get("available") and lhm.get("mem_temp_c") is not None) else None
        return {
            "percent": m.get("percent"), "used_gib": m.get("used_gib"),
            "total_gib": m.get("total_gib"), "available_gib": m.get("available_gib"),
            "speed_mhz": m.get("speed_mhz"), "temperature_c": mem_temp,
            "temperature_source": (lhm.get("method") or "LibreHardwareMonitor") if mem_temp is not None else None,
            "load_percent": m.get("load_percent"),
            "pcie_read_mbs": m.get("pcie_read_mbs"), "pcie_write_mbs": m.get("pcie_write_mbs"),
            "timestamp": time.time(),
        }


# ---------------------------------------------------------------------------
# 时序历史落盘 (SQLite)
# 后台写入线程按固定间隔采样当前快照；支持按时间范围回放（前端历史页用）。
# ---------------------------------------------------------------------------
class History:
    COLS = ["ts", "gpu_pw", "gpu_util", "gpu_temp", "gpu_e_wh",
            "cpu_util", "cpu_pw", "cpu_e_wh", "mem_pct",
            "total_pw", "total_e_wh", "total_cost",
            "net_rx_mbs", "net_tx_mbs", "disk_read_mbs", "disk_write_mbs"]
    RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}

    def __init__(self, path, interval=5.0, retention_days=30):
        self.path = path
        self.interval = interval
        self.retention_days = retention_days
        # 写入线程与 HTTP 查询线程会并发使用同一个连接: check_same_thread=False
        # 允许跨线程, 但必须自己加锁, 否则写事务(prune)与读(query)并发可能抛
        # "database is locked", 被 except 吞掉后表现为历史图表偶发空白。
        self.lock = threading.RLock()
        import sqlite3
        self.conn = sqlite3.connect(path, check_same_thread=False)
        try:
            # WAL 让读写不再互相阻塞 (会额外产生 history.db-wal / -shm 文件)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS samples("
            "ts REAL PRIMARY KEY, gpu_pw REAL, gpu_util REAL, gpu_temp REAL, gpu_e_wh REAL,"
            " cpu_util REAL, cpu_pw REAL, cpu_e_wh REAL, mem_pct REAL, total_pw REAL,"
            " total_e_wh REAL, total_cost REAL)")
        self.conn.commit()
        # 兼容旧库 (运行过早期版本, 仅 12 列): 缺失的网络/磁盘列用 ALTER 补齐
        self._migrate()

    def _migrate(self):
        with self.lock:
            try:
                existing = {r[1] for r in self.conn.execute("PRAGMA table_info(samples)").fetchall()}
                for col, ctype in [("net_rx_mbs", "REAL"), ("net_tx_mbs", "REAL"),
                                   ("disk_read_mbs", "REAL"), ("disk_write_mbs", "REAL")]:
                    if col not in existing:
                        self.conn.execute("ALTER TABLE samples ADD COLUMN %s %s" % (col, ctype))
                self.conn.commit()
            except Exception:
                pass

    def add(self, gpu_pw, gpu_util, gpu_temp, gpu_e_wh, cpu_util, cpu_pw, cpu_e_wh,
            mem_pct, total_pw, total_e_wh, total_cost,
            net_rx_mbs=None, net_tx_mbs=None, disk_read_mbs=None, disk_write_mbs=None):
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), gpu_pw, gpu_util, gpu_temp, gpu_e_wh, cpu_util, cpu_pw,
                     cpu_e_wh, mem_pct, total_pw, total_e_wh, total_cost,
                     net_rx_mbs, net_tx_mbs, disk_read_mbs, disk_write_mbs))
                self.conn.commit()
            except Exception:
                pass

    def prune(self):
        """删除超过保留期的样本。

        调用方应控制频率 (目前每小时一次) —— 这是对整表的扫描式 DELETE,
        每次采样都跑一遍会在历史库变大后造成明显的无谓 IO。
        """
        with self.lock:
            try:
                cutoff = time.time() - self.retention_days * 86400.0
                self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                self.conn.commit()
            except Exception:
                pass

    def clear(self):
        """清空全部历史样本并回收空间 (「清除全部历史」)。"""
        with self.lock:
            try:
                self.conn.execute("DELETE FROM samples")
                self.conn.commit()
                try:
                    self.conn.execute("VACUUM")
                except Exception:
                    pass
                return True
            except Exception:
                return False

    def query(self, range_key="24h"):
        secs = self.RANGES.get(range_key, 86400)
        start = time.time() - secs
        with self.lock:
            try:
                cur = self.conn.execute(
                    "SELECT ts,gpu_pw,gpu_util,gpu_temp,gpu_e_wh,cpu_util,cpu_pw,cpu_e_wh,"
                    "mem_pct,total_pw,total_e_wh,total_cost,"
                    "net_rx_mbs,net_tx_mbs,disk_read_mbs,disk_write_mbs "
                    "FROM samples WHERE ts>=? ORDER BY ts ASC",
                    (start,))
                rows = cur.fetchall()
            except Exception:
                rows = []
        if len(rows) > 720:
            step = len(rows) // 720
            rows = rows[::step]
        return [dict(zip(self.COLS, r)) for r in rows]

    # ---- 导出 (CSV / JSON) ----
    def rows(self, range_key="24h"):
        """返回原始行 (按时间升序)。range_key='all' 导出全量历史。"""
        if range_key == "all":
            start = 0.0
        else:
            secs = self.RANGES.get(range_key, 86400)
            start = time.time() - secs
        with self.lock:
            try:
                cur = self.conn.execute(
                    "SELECT ts,gpu_pw,gpu_util,gpu_temp,gpu_e_wh,cpu_util,cpu_pw,cpu_e_wh,"
                    "mem_pct,total_pw,total_e_wh,total_cost,"
                    "net_rx_mbs,net_tx_mbs,disk_read_mbs,disk_write_mbs "
                    "FROM samples WHERE ts>=? ORDER BY ts ASC",
                    (start,))
                return cur.fetchall()
            except Exception:
                return []

    def export_csv(self, range_key="24h"):
        rows = self.rows(range_key)
        header = ["time", "ts"] + self.COLS[1:]
        out = [",".join(header)]
        for r in rows:
            ts = r[0]
            dt = datetime.fromtimestamp(ts)
            tstr = dt.strftime("%Y-%m-%d %H:%M:%S") + (".%03d" % int(round((ts - int(ts)) * 1000)))
            vals = [tstr, "%.3f" % ts]
            for v in r[1:]:
                vals.append("" if v is None else "%.3f" % v)
            out.append(",".join(vals))
        return "\r\n".join(out)

    def export_json(self, range_key="24h"):
        rows = self.rows(range_key)
        samples = []
        for r in rows:
            ts = r[0]
            dt = datetime.fromtimestamp(ts)
            rec = {"ts": ts,
                   "time": dt.strftime("%Y-%m-%d %H:%M:%S") +
                           (".%03d" % int(round((ts - int(ts)) * 1000)))}
            for k, v in zip(self.COLS[1:], r[1:]):
                rec[k] = v
            samples.append(rec)
        return {
            # 对外产品名统一为 GPU Monitor (前端"关于"卡片、systemd 描述、启动项
            # 文件都用它)。GitHub 仓库地址仍为 GPU_Scope —— 那是仓库名, 不是产品名。
            "tool": "GPU Monitor",
            "generated_at": time.time(),
            "range": range_key,
            "count": len(samples),
            "columns": ["time", "ts"] + self.COLS[1:],
            "samples": samples,
        }

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
def _is_loopback(host):
    """该绑定地址是否只监听本机回环。"""
    return (host or "") in ("127.0.0.1", "localhost", "::1") or \
        (host or "").startswith("127.")


def _origin_allowed(origin):
    """写请求的 Origin 是否可信 (CSRF 防护)。

    浏览器发起的跨站请求会带 Origin 头。恶意网页可以用 <form enctype="text/plain">
    构造无需预检的「简单请求」打到 127.0.0.1:8080, 此前后端不做任何校验, 于是
    /api/shutdown、/api/settings/reset_meter 都能被跨站触发。

    规则:
      - 没有 Origin 头 (curl / stop 脚本 / 原生客户端) -> 允许, 由 IP + 令牌兜底;
      - Origin 为回环地址 -> 允许 (面板自身发起的请求);
      - 其它 Origin -> 只有在携带正确令牌时才允许 (局域网面板会拿到令牌)。
    """
    if not origin:
        return True
    try:
        from urllib.parse import urlparse as _up
        host = (_up(origin).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


class Handler(BaseHTTPRequestHandler):
    monitor = None
    sys = None
    meter = None
    history = None
    prefs = None
    html_path = None
    auth_token = None
    bind_host = "127.0.0.1"   # 由 main() 写入, 用于判断是否对外暴露
    server_version = "GPUMonitor/%s" % __version__

    def _send(self, code, body, content_type="application/json", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            try:
                with open(self.html_path, "r", encoding="utf-8") as f:
                    html = f.read()
                # 注入本地 API 令牌, 使同源页面在开放(--host 0.0.0.0)时也能写操作。
                # 仅绑定回环时不注入: 本地请求本就免鉴权, 没必要把令牌放进页面
                # (任何能打开面板的人都能 view-source 拿到它)。
                if Handler.auth_token and not _is_loopback(Handler.bind_host):
                    html = html.replace("__API_TOKEN__", Handler.auth_token)
                else:
                    html = html.replace("__API_TOKEN__", "")
                # 版本号与 __version__ 保持单一真源
                html = html.replace("__VERSION__", __version__)
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if path == "/api/metrics":
            gpus = self.monitor.get_snapshot()
            payload = {
                "gpus": gpus,
                "cluster": self.monitor.gpu_cluster_summary(),
                "server_time": time.time(),
            }
            # GPU 数据为空时附带不可用原因, 前端据此显示降级提示
            if not gpus:
                try:
                    reason = self.monitor.unavailable_reason()
                except Exception:
                    reason = None
                if reason:
                    payload["gpu_unavailable_reason"] = reason
            self._send(200, json.dumps(payload))
            return
        if path.startswith("/api/metrics/"):
            try:
                idx = int(path.split("/")[-1])
                data = self.monitor.get_snapshot(idx)
                self._send(404 if data is None else 200,
                           json.dumps({"error": "gpu not found"} if data is None else data))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if path == "/api/cpu":
            if self.sys:
                try:
                    d = self.sys.cpu_snapshot()
                    try:
                        d.update(self.sys.io_snapshot())
                    except Exception:
                        pass
                    self._send(200, json.dumps(d))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            else:
                self._send(200, json.dumps({"error": "sys monitor unavailable"}))
            return
        if path == "/api/memory":
            self._send(200, json.dumps(self.sys.mem_snapshot() if self.sys else {"error": "sys monitor unavailable"}))
            return
        if path == "/api/settings":
            snap = self.meter.snapshot() if self.meter else {"error": "no meter"}
            if isinstance(snap, dict) and "error" not in snap and Handler.prefs is not None:
                snap["prefs"] = Handler.prefs.data
                snap["autostart_active"] = _autostart_active()
            # 带上自己的 PID: 启动时据此判断"端口上是不是已经有本服务的实例",
            # 避免重复启动出两个主服务 (它们会同时写 meter.json / history.db)。
            if isinstance(snap, dict):
                snap["pid"] = os.getpid()
            self._send(200, json.dumps(snap))
            return
        if path == "/api/history":
            from urllib.parse import parse_qs
            rng = parse_qs(parsed.query).get("range", ["24h"])[0]
            try:
                data = self.history.query(rng) if self.history else []
            except Exception as e:
                data = []
            self._send(200, json.dumps({"range": rng, "count": len(data), "samples": data}))
            return
        if path == "/api/export":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            fmt = (q.get("format", ["csv"])[0] or "csv").lower()
            rng = (q.get("range", ["24h"])[0] or "24h").lower()
            if fmt not in ("csv", "json"):
                fmt = "csv"
            if rng not in ("1h", "6h", "24h", "7d", "30d", "all"):
                rng = "24h"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if self.history is None:
                self._send(503, json.dumps({"error": "历史库不可用"}))
                return
            if fmt == "csv":
                csv_text = self.history.export_csv(rng)
                fname = "gpu_scope_history_%s_%s.csv" % (rng, stamp)
                self._send(200, csv_text, "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="%s"' % fname})
            else:
                js = json.dumps(self.history.export_json(rng), ensure_ascii=False)
                fname = "gpu_scope_history_%s_%s.json" % (rng, stamp)
                self._send(200, js, "application/json; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="%s"' % fname})
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # 写操作鉴权: 本地 (127.0.0.1/::1) 信任; 远程调用需携带 X-Api-Token
        client_ip = self.client_address[0]
        _tok = self.headers.get("X-Api-Token") or ""
        _has_token = bool(Handler.auth_token) and _tok == Handler.auth_token
        if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            if not _has_token:
                self._send(403, json.dumps({"error": "forbidden: 远程写操作需要 X-Api-Token 令牌"}))
                return
        # CSRF: 浏览器跨站请求会带 Origin。非本服务页面的来源一律拒绝,
        # 除非它同时持有有效令牌 (局域网面板会拿到令牌, 正常可用)。
        if not _origin_allowed(self.headers.get("Origin")) and not _has_token:
            self._send(403, json.dumps({"error": "forbidden: 跨站请求被拒绝 (CSRF 防护)"}))
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        meter = Handler.meter
        if meter is not None and path == "/api/settings/price":
            try:
                price = float(body.get("price", meter.data["elec_rate"]))
                if price <= 0:
                    price = 0.01
                meter.set_rate(price)
                self._send(200, json.dumps({"ok": True, "elec_rate": meter.data["elec_rate"]}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if meter is not None and path == "/api/settings/reset_meter":
            meter.reset_energy()
            self._send(200, json.dumps({"ok": True}))
            return
        if meter is not None and path == "/api/settings/reset_all":
            # 真正的"清除全部历史": 除计量(meter.json)外, 还要清掉 history.db
            # 里的时序样本 —— 此前只做了前者, 与按钮/文档的说法不符。
            meter.reset_energy()
            cleared = False
            try:
                if Handler.history is not None:
                    cleared = bool(Handler.history.clear())
            except Exception:
                cleared = False
            _errlog("reset_all: meter cleared, history cleared=%s" % cleared)
            self._send(200, json.dumps({"ok": True, "history_cleared": cleared}))
            return
        if path == "/api/settings/prefs":
            try:
                prefs = Handler.prefs
                if prefs is None:
                    self._send(500, json.dumps({"error": "prefs unavailable"}))
                    return
                patch = {k: v for k, v in (body or {}).items() if k in _PREFS_DEFAULT}
                prefs.update(patch)
                # 运行时生效: 历史采样间隔 / 保留天数 直接作用于 history 实例
                hist = Handler.history
                if hist is not None:
                    try:
                        hist.interval = float(prefs.get("hist_interval", hist.interval))
                    except Exception:
                        pass
                    try:
                        hist.retention_days = int(prefs.get("hist_retention_days", hist.retention_days))
                    except Exception:
                        pass
                self._send(200, json.dumps({"ok": True, "prefs": prefs.data}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if path == "/api/settings/autostart":
            try:
                enabled = bool((body or {}).get("enabled", False))
                res = _set_autostart(enabled)
                self._send(200, json.dumps(res))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if path == "/api/shutdown":
            # 优雅退出: 先响应, 再落盘并退出 (供 stop 脚本调用)
            try:
                self._send(200, json.dumps({"ok": True, "msg": "shutting down"}))
            except Exception:
                pass
            def _shutdown():
                try:
                    _errlog("shutdown via /api/shutdown")
                except Exception:
                    pass
                try:
                    if Handler.monitor:
                        Handler.monitor._stop = True
                except Exception:
                    pass
                try:
                    if Handler.meter:
                        Handler.meter.save(force=True)
                except Exception:
                    pass
                try:
                    if Handler.history:
                        Handler.history.close()
                except Exception:
                    pass
                time.sleep(0.3)
                os._exit(0)
            threading.Thread(target=_shutdown, daemon=True).start()
            return
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------------------
# 开机自启: Windows = 启动文件夹快捷脚本; Linux = systemd user unit
# ---------------------------------------------------------------------------
def _bat_encoding():
    """写入 .bat 时使用的编码。

    cmd.exe 按系统 ANSI 代码页解析 .bat (中文 Windows 为 GBK/936), 因此生成
    启动脚本时必须用同一编码, 否则含非 ASCII 的项目路径 (如中文目录) 会被
    误读成乱码, 导致开机自启静默失败。
    """
    if not IS_WINDOWS:
        return "utf-8"
    # 用 GetACP() 拿系统 ANSI 代码页 (中文 Windows = 936), 这才是 cmd.exe 解析
    # .bat 时实际使用的编码。不能用 locale.getpreferredencoding(): Python 在
    # UTF-8 模式下会返回 utf-8, 与 cmd 的代码页不一致, 中文路径会写成乱码。
    try:
        import ctypes
        acp = ctypes.windll.kernel32.GetACP()
        if acp:
            return "cp%s" % acp
    except Exception:
        pass
    try:
        import locale
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        return "utf-8"


def _launcher_python():
    """开机启动器使用的解释器: 优先 pythonw (无控制台窗口)。"""
    exe = sys.executable or ""
    if exe:
        if exe.lower().endswith("python.exe"):
            cand = exe[:-4] + "w.exe"
            if os.path.exists(cand):
                return cand
        return exe
    return "pythonw"


def _autostart_launcher(base, port=8080):
    """生成开机启动器内容 (Windows)。

    注意: 不能直接复制 start_gpu_monitor.bat —— 源脚本用 %~dp0 定位自身目录,
    复制到启动文件夹后 %~dp0 会解析成启动文件夹, 导致找不到 gpu_monitor.py
    (功能静默失效)。这里把项目路径与解释器路径全部写成绝对路径。
    """
    py = _launcher_python()
    script = os.path.join(base, "gpu_monitor.py")
    watchdog = os.path.join(base, "watchdog.py")
    return (
        '@echo off\r\n'
        'REM Auto-generated by gpu_monitor.py -- do not edit by hand.\r\n'
        'cd /d "%s"\r\n'
        'start "GPU-Monitor" "%s" "%s" --port %s --interval 0.5\r\n'
        'REM 给主服务一点启动时间, 再拉起看门狗 (避免 watchdog 误判服务已死而重复拉起)\r\n'
        'timeout /t 5 /nobreak >nul 2>nul\r\n'
        'if exist "%s" start "GPU-Monitor-WD" "%s" "%s"\r\n'
        % (base, py, script, port, watchdog, py, watchdog)
    )


def _autostart_path():
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            return None
        return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                            "Programs", "Startup", "GPU-Monitor-startup.bat")
    if IS_LINUX:
        return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user",
                            "gpu-monitor.service")
    return None


def _autostart_active():
    p = _autostart_path()
    return bool(p and os.path.exists(p))


def _try_systemctl(action):
    try:
        subprocess.run(["systemctl", "--user", action, "gpu-monitor.service"],
                       capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def _set_systemd_autostart(enabled):
    """Linux: 写/删 ~/.config/systemd/user/gpu-monitor.service 并尝试 enable/disable。"""
    dest = _autostart_path()
    if dest is None:
        return {"ok": False, "error": "无法定位 systemd user 目录"}
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_monitor.py")
    unit = (
        "[Unit]\n"
        "Description=GPU Monitor\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        'ExecStart="%s" "%s" --port 8080 --interval 0.5\n'
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n" % (sys.executable, script)
    )
    try:
        d = os.path.dirname(dest)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        if enabled:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(unit)
            _try_systemctl("enable")
            return {"ok": True, "enabled": True, "note": "systemd user unit (登录会话后生效)"}
        _try_systemctl("disable")
        if os.path.exists(dest):
            os.remove(dest)
        return {"ok": True, "enabled": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _set_autostart(enabled):
    if IS_WINDOWS:
        dest = _autostart_path()
        if dest is None:
            return {"ok": False, "error": "找不到启动文件夹"}
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            if enabled:
                # 生成写死绝对路径的启动器 (复制 start_gpu_monitor.bat 会因 %~dp0
                # 指向启动文件夹而失效, 详见 _autostart_launcher 注释)
                # 启动文件夹在某些精简系统上可能不存在, 需自行创建
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                # newline="" -> 不做换行翻译, 内容里的 CRLF 原样写入
                with open(dest, "w", encoding=_bat_encoding(), newline="") as f:
                    f.write(_autostart_launcher(base))
                return {"ok": True, "enabled": True}
            else:
                if os.path.exists(dest):
                    os.remove(dest)
                return {"ok": True, "enabled": False}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    if IS_LINUX:
        return _set_systemd_autostart(enabled)
    return {"ok": False, "error": "当前平台暂不支持开机自启"}


# ---------------------------------------------------------------------------
# 端口: 冲突自动避让 + 运行时信息落盘
#
# 端口此前硬编码在 start/stop/status 脚本与 watchdog.py 三处, 改一处忘两处;
# 更糟的是与同机其他 8080 服务冲突时没有任何提示, 服务静默起不来。现在:
#   - 主服务绑定失败(端口被占)时向上探测最多 PORT_SCAN 个端口;
#   - 实际端口写入 monitor.port, watchdog 与启停脚本都从这里读, 单一真源。
#
# 用纯文本(只有一个端口号)而非 JSON 是刻意的选择: 启停脚本是 .bat / .sh, 它们
# 没有 JSON 解析器, 而 `set /p PORT=<monitor.port` 与 `$(cat monitor.port)`
# 在这两种脚本里都是零依赖的。
# ---------------------------------------------------------------------------
PORT_SCAN = 10
PORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.port")


def _write_port_file(port):
    try:
        with open(PORT_FILE, "w", encoding="ascii") as f:
            f.write("%d\n" % port)
    except Exception:
        pass


def _remove_port_file():
    try:
        if os.path.exists(PORT_FILE):
            os.remove(PORT_FILE)
    except Exception:
        pass


PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.pid")


def _live_main_pid():
    """monitor.pid 中"存活且确实本服务"的 PID; 没有或无法确认则返回 None。

    与 watchdog.is_main_process 同样的谨慎: 必须校验命令行里含 gpu_monitor.py,
    否则 PID 被系统复用给别的进程时会误判成"已有一个实例"。
    """
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except Exception:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    if psutil is None:
        return None              # 无 psutil 时无法校验, 宁可不检测也不误判
    try:
        cmd = " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return None
    return pid if "gpu_monitor.py" in cmd else None


def _serving_pid(port):
    """探测该端口上提供本服务 API 的进程 PID; 没有则 None。"""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/settings" % port,
                                    timeout=2) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        p = data.get("pid")
        return int(p) if p else None
    except Exception:
        return None


def _detect_duplicate(port):
    """是否已有本服务实例在该端口上服务。返回其 PID, 没有则 None。

    背景: 以前两个实例能同时 LISTEN 同一个端口 (Windows 的 SO_REUSEADDR 允许
    重复绑定), 结果就是双份 NVML 轮询、双份历史写入, 而 meter.json 由两者互相
    覆盖 —— 能耗累计因此丢失增量。MonitorHTTPServer 关掉 SO_REUSEADDR 并启用
    SO_EXCLUSIVEADDRUSE 之后, 端口冲突会真实报错; 这里再补一道显式检测, 让
    "重复启动"变成一个明确的、有日志的动作, 而不是悄悄跑起来第二个实例。
    """
    if not _live_main_pid():
        return None             # 没有疑似实例 -> 立即放行, 不增加启动耗时
    # 确实有一个本服务进程在跑: 它可能在冷启动中还没监听端口, 给它一点时间
    for _ in range(20):         # 最多约 10 秒
        pid = _serving_pid(port)
        if pid and pid != os.getpid():
            return pid
        time.sleep(0.5)
    return None


class MonitorHTTPServer(ThreadingHTTPServer):
    """按平台区分 SO_REUSEADDR 语义的 HTTP 服务器。

    POSIX 上需要 SO_REUSEADDR —— 否则服务重启会卡在前一个连接的 TIME_WAIT 上,
    表现为"刚 stop 就 start 起不来"。

    Windows 上 SO_REUSEADDR 的含义完全不同: 它允许两个进程绑定同一个端口
    (除非对方设置了 SO_EXCLUSIVEADDRUSE)。后果有两重, 都必须避免:
      1. 端口冲突检测形同虚设 —— 别的程序占着 8080, 我们的 bind 依然"成功",
         于是 _bind_server 的自动避让永远不会被触发;
      2. 更糟的是同机其他进程可以后来居上绑到同一个端口, 劫持本服务的流量。
    因此 Windows 上关掉 SO_REUSEADDR 并显式设置 SO_EXCLUSIVEADDRUSE: 端口被占
    时 bind 会真实失败, 冲突才会被上面 _bind_server 看到并自动换端口。
    """

    allow_reuse_address = not IS_WINDOWS

    def server_bind(self):
        if IS_WINDOWS:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        super().server_bind()


def _bind_server(host, port):
    """绑定端口; 被占用时向上探测最多 PORT_SCAN 个。返回 (server, 实际端口)。

    只在"端口确实被占用"时避让 —— 其他绑定错误(权限不足、地址无效)直接抛出,
    否则会把真正的配置错误掩盖成"换了个端口悄悄跑了"。
    """
    last = None
    for offset in range(PORT_SCAN + 1):
        candidate = port + offset
        try:
            srv = MonitorHTTPServer((host, candidate), Handler)
            if offset:
                print(f"> 端口 {port} 已被占用, 自动改用 {candidate}")
                _errlog("port %d in use -> fell back to %d" % (port, candidate))
            return srv, candidate
        except OSError as e:
            last = e
            if getattr(e, "errno", None) not in (98, 10048):  # EADDRINUSE / WSAEADDRINUSE
                raise
            continue
    raise last


def main():
    ap = argparse.ArgumentParser(description="实时 GPU 监测服务")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址 (默认 127.0.0.1 仅本机; 如需局域网访问改用 0.0.0.0, 远程写操作需令牌)")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--price", type=float, default=None,
                    help="电价 元/度 (默认使用已保存的 meter.json 值, 若不存在则用 0.60)")
    ap.add_argument("--hist-interval", type=float, default=5.0,
                    help="历史采样间隔(秒, 默认 5)")
    ap.add_argument("--hist-retention", type=int, default=30,
                    help="历史数据保留天数(默认 30)")
    args = ap.parse_args()

    # 单实例检查必须最先做 —— 早到"写 monitor.pid"之前。否则重复启动的实例会先把
    # PID 文件改写成自己的 PID, 再往下走检测时 _live_main_pid() 已经读到自己
    # (或读到已被覆盖掉的假值), 检测形同虚设; 更糟的是健康实例的 PID 被弄丢,
    # 之后谁都认不出"到底哪个进程在服务"。
    _dup = _detect_duplicate(args.port)
    if _dup:
        msg = ("已有实例在端口 %d 上运行 (pid=%d), 本次启动退出 —— "
               "如需重启请先执行 stop_gpu_monitor" % (args.port, _dup))
        print("> " + msg)
        _errlog("duplicate instance detected: %s" % msg)
        return

    # 本地 API 令牌: 远程写操作鉴权用 (localhost 信任, 无需令牌)
    auth_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.json")
    auth_token = None
    try:
        if os.path.exists(auth_path):
            with open(auth_path, "r", encoding="utf-8") as f:
                auth_token = json.load(f).get("token")
        if not auth_token:
            auth_token = secrets.token_hex(16)
            with open(auth_path, "w", encoding="utf-8") as f:
                json.dump({"token": auth_token}, f)
            os.chmod(auth_path, 0o600)
    except Exception:
        auth_token = secrets.token_hex(16)
    Handler.auth_token = auth_token

    # 写入 PID 文件: stop 脚本与 watchdog 用它精确定位本进程。
    # 必须由主服务自己写 —— Windows 的 start 脚本用 `start` 启动拿不到子进程 PID,
    # 此前 monitor.pid 会长期停留在几天前的旧值, stop 时存在 PID 复用误杀风险。
    # (watchdog 终止前会校验该 PID 的命令行确实包含 gpu_monitor.py, 双重保险)
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    # 持久化计量表 (累计能耗 / 电费 / 电价 / 每日明细)
    meter_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meter.json")
    meter = Meter(meter_path, rate=0.60)
    if args.price is not None:
        meter.set_rate(args.price)

    # 时序历史落盘 (SQLite) —— 供历史页回放
    history_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
    history = History(history_path, interval=args.hist_interval, retention_days=args.hist_retention)

    # 用户偏好设置 (落盘 prefs.json, 与计量 meter 分离)
    prefs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prefs.json")
    prefs = Prefs(prefs_path)
    # 用设置页里的值覆盖启动期 CLI 参数 (CLI 仅在无 prefs 时才有意义)
    history.interval = float(prefs.get("hist_interval", args.hist_interval))
    history.retention_days = int(prefs.get("hist_retention_days", args.hist_retention))

    print("> 正在初始化 NVML ...")
    monitor = Monitor(meter=meter)
    poll_thread = threading.Thread(target=monitor.run, args=(prefs,), daemon=True)
    poll_thread.start()
    monitor.poll_once()
    print(f"> 检测到 {monitor.count} 张 GPU:")
    for s in monitor.get_snapshot():
        if s and "error" not in s:
            print(f"    [{s['index']}] {s['name']}  |  驱动 {s['driver']}  |  CUDA {s['cuda']}")

    # 系统 (CPU / 内存) 监控
    sys_monitor = None
    if psutil is not None:
        print("> 正在初始化 CPU/内存 监控 ...")
        sys_monitor = SysMonitor(meter=meter)
        threading.Thread(target=sys_monitor.cpu_thread, daemon=True).start()
        threading.Thread(target=sys_monitor.mem_thread, daemon=True).start()
        if SYS_STATIC["cpu_name"]:
            print(f"    CPU: {SYS_STATIC['cpu_name']}  |  {SYS_STATIC['cpu_cores']}核/{SYS_STATIC['cpu_threads']}线程")
        if SYS_STATIC["ram_total_gib"]:
            print(f"    内存: {SYS_STATIC['ram_total_gib']} GiB @ {SYS_STATIC['ram_speed_mhz']} MHz")
    else:
        print("> 警告: psutil 未安装, CPU/内存 监控不可用。请用 `pip install psutil` 安装到 pylibs。")

    Handler.monitor = monitor
    Handler.sys = sys_monitor
    Handler.meter = meter
    Handler.history = history
    Handler.prefs = prefs
    Handler.html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    Handler.bind_host = args.host
    server, port = _bind_server(args.host, args.port)
    url = f"http://localhost:{port}"
    _write_port_file(port)

    # 后台周期落盘, 保证异常退出也能持久化
    def _saver():
        while True:
            time.sleep(10)
            meter.save()
    threading.Thread(target=_saver, daemon=True).start()

    # 后台历史采样写入 (按 --hist-interval 采样当前快照到 history.db)
    def _history_writer():
        # prune 是对整表的扫描式 DELETE, 历史库到几十万行后每次采样都跑一遍
        # 会造成明显的无谓 IO —— 改为每小时一次 (并保留启动时的一次)。
        _last_prune = 0.0
        while not getattr(monitor, "_stop", False):
            try:
                time.sleep(history.interval)
                # 多卡 GPU 聚合: 总功率/平均利用率/最高温度/累计能耗之和
                gpus = monitor.get_snapshot()
                gpus = [g for g in gpus if g and "error" not in g]
                if gpus:
                    gpw = sum((g["power"]["watts"] or 0) for g in gpus)
                    gu = sum(g["utilization"]["gpu"] for g in gpus) / len(gpus)
                    gt = max((g["temperature"]["c"] or 0) for g in gpus)
                    ge = sum((g["system"].get("energy_wh_cum") or 0) for g in gpus)
                else:
                    gpw = gu = gt = None; ge = 0
                c = sys_monitor.cpu_snapshot() if sys_monitor else None
                m = sys_monitor.mem_snapshot() if sys_monitor else None
                ms = meter.snapshot()
                if c:
                    cu = c["utilization"]; cpw = c["power_w"]; ce = c["energy_wh"]
                else:
                    cu = cpw = None; ce = 0
                mp = m["percent"] if m else None
                te = ms["total_energy_wh"]; tc = ms["elec_cost_yuan"]
                total_pw = (gpw or 0) + (cpw or 0)
                io = sys_monitor.io_snapshot() if sys_monitor else None
                nrx = io["net"]["rx_mbs"] if io else None
                ntx = io["net"]["tx_mbs"] if io else None
                dr = io["disk"]["read_mbs"] if io else None
                dw = io["disk"]["write_mbs"] if io else None
                history.add(gpw, gu, gt, ge, cu, cpw, ce, mp, total_pw, te, tc,
                            nrx, ntx, dr, dw)
                if time.monotonic() - _last_prune > 3600:
                    history.prune()
                    _last_prune = time.monotonic()
            except Exception:
                pass
    threading.Thread(target=_history_writer, daemon=True).start()

    print(f"> 监控面板已启动: {url}")
    print("> 按 Ctrl+C 退出 (后台模式请用 stop 脚本)。")
    # 落盘启动日志 (pythonw 无控制台, 便于后台排障)
    try:
        _lf = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log"), "a", encoding="utf-8")
        _lf.write("%s 监控面板已启动: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), url))
        _lf.close()
    except Exception:
        pass

    def _safe_save(m):
        try:
            m.save(force=True)
        except Exception:
            pass

    # 优雅退出: 支持后台 stop 脚本的 taskkill (SIGTERM)
    def _graceful_stop(signum, frame):
        try:
            _errlog("SIGTERM received, graceful shutdown")
        except Exception:
            pass
        try:
            monitor._stop = True
        except Exception:
            pass
        try:
            meter.save(force=True)
        except Exception:
            pass
        try:
            if history is not None:
                history.close()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass
        # os._exit 不触发 atexit, 这里必须显式清理端口文件, 否则 watchdog 会
        # 继续探测一个已经没人监听的端口。
        _remove_port_file()
        os._exit(0)
    try:
        import signal
        signal.signal(signal.SIGTERM, _graceful_stop)
    except Exception:
        pass
    import atexit
    atexit.register(lambda: _errlog("atexit: process exiting (clean exit -> NOT killed/OOM)"))
    atexit.register(lambda: _safe_save(meter))
    # 退出时清掉 monitor.port: 残留它会让 watchdog 继续探测一个已经没人监听的端口,
    # 表现为"服务已停但 watchdog 认为还活着/或不断重启"。
    atexit.register(_remove_port_file)

    # serve_forever 自愈: 若因异常退出(非 KeyboardInterrupt), 记录并重建 server 继续服务, 避免"莫名关闭"
    while True:
        try:
            server.serve_forever()
            break  # 正常情况下 serve_forever 不会主动返回
        except KeyboardInterrupt:
            print("\n> 正在关闭 ...")
            monitor._stop = True
            meter.save(force=True)
            try:
                if history is not None:
                    history.close()
            except Exception:
                pass
            server.shutdown()
            break
        except Exception as _e:
            _errlog("serve_forever crashed: %r -- auto-restarting server" % (_e,))
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
            time.sleep(1)
            server, port = _bind_server(args.host, port)
            continue


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 启动期崩溃落盘 (pythonw 无控制台, 否则直接闪退无信息)
        import traceback as _tb
        try:
            _ef = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_err.log"), "a", encoding="utf-8")
            _ef.write("\n=== FATAL %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            _tb.print_exc(file=_ef)
            _ef.close()
        except Exception:
            pass
        raise
