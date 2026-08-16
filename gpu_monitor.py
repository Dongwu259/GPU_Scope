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

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys

# 后台(pythonw)运行时, 调用控制台子进程(powershell 等)必须隐藏其窗口,
# 否则 Windows 会为每个被调用的 powershell 弹出一个控制台窗口。
# CREATE_NO_WINDOW 仅 Windows 有效, 其它平台取 0 (无副作用)。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import threading

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


def _proc_name(pid):
    """尽力获取进程名 (无 psutil 时使用 ctypes 调用 Windows API)。"""
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


def _query_pdh_proc_mem():
    out = {}
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
# Monitor
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, meter):
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
        self._start_time = time.time()
        for h in self.handles:
            try:
                e0 = self.nvml.nvmlDeviceGetTotalEnergyConsumption(h)
            except Exception:
                e0 = None
            self._energy_start.append(e0)
            self._gpu_last_energy_wh.append((e0 / 3.6e6) if e0 is not None else 0.0)
            self._gpu_last_ts.append(None)

    # ---- PDH 后台线程 ----
    def mem_thread(self):
        while not self._stop:
            # 多 GPU 时 PDH 不区分卡, 复制到每个槽位 (消费级通常单卡)
            m = _query_pdh_proc_mem()
            with self._mem_lock:
                for i in range(self.count):
                    self.proc_mem_mb[i] = m
            time.sleep(2)

    def get_snapshot(self, idx=None):
        with self.lock:
            if idx is None:
                return [s for s in self.snapshots if s is not None]
            return self.snapshots[idx] if 0 <= idx < self.count else None

    def poll_once(self):
        for i, h in enumerate(self.handles):
            try:
                snap = self._read_device(h, i)
            except Exception as e:
                snap = {"index": i, "error": str(e)}
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
        _now = time.time()
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
            el = time.time() - self._start_time
            if el > 1:
                avg_power = ej / el
        # 持久化累计 (跨重启) —— 直接来自 Meter
        gpu_cum_wh = _meter_snap["gpu_energy_wh"]
        gpu_cum_kwh = gpu_cum_wh / 1000.0
        gpu_cum_cost = gpu_cum_kwh * _rate

        return {
            "index": idx, "name": name, "uuid": uuid, "spec": spec,
            "driver": self.driver, "cuda": self.cuda, "timestamp": time.time(),
            "uptime_s": round(time.time() - self._start_time, 1),
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

    def run(self, interval):
        # 启动 PDH 线程
        t = threading.Thread(target=self.mem_thread, daemon=True)
        t.start()
        while not self._stop:
            self.poll_once()
            time.sleep(interval)


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

    def update(self):
        now = time.time()
        with self._lock:
            due = (not self.available) or (now - self._last > self.poll_interval)
        if not due:
            return
        self._probe_once()
        with self._lock:
            self._last = now

    def snapshot(self):
        with self._lock:
            return {
                "available": self.available, "method": self.method,
                "cpu_temp_c": self.cpu_temp_c, "cpu_power_w": self.cpu_power_w,
                "mem_temp_c": self.mem_temp_c,
            }


def _make_lhm():
    try:
        return LHMClient()
    except Exception:
        return None


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


def _wmi_json(script):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return r.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def _guess_tdp(name):
    if not name:
        return float(max(65, (psutil.cpu_count(logical=False) or 8) * 8))
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
    return float(max(65, (psutil.cpu_count(logical=False) or 8) * 8))


def _init_sys_static():
    s = SYS_STATIC
    if psutil:
        s["cpu_cores"] = psutil.cpu_count(logical=False)
        s["cpu_threads"] = psutil.cpu_count(logical=True)
    # 多路 CPU: Win32_Processor 每路返回一条记录, 逐路解析型号/核心/线程/频率
    cpus = []
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
    if not cpus:
        # 兜底: 无 WMI 时按 psutil 总核心数估一个单路
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
    out = _wmi_json("Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum | Select -ExpandProperty Sum")
    try:
        if out and str(out).strip().isdigit():
            s["ram_total_gib"] = round(int(str(out).strip()) / (1024 ** 3), 1)
    except Exception:
        pass
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
        self._cpu_last_acc_t = time.time()
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
                now = time.time()
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
        perf_commit = (
            "Get-Counter -Counter '\\Memory\\% Committed Bytes In Use' "
            "-ErrorAction SilentlyContinue | Select -ExpandProperty CounterSamples "
            "| ForEach-Object { $_.CookedValue }"
        )
        while not self._stop:
            try:
                if psutil:
                    vm = psutil.virtual_memory()
                    now = time.time()
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
                # 提交内存占比 (负载) via 性能计数器
                try:
                    r = subprocess.run(["powershell", "-NoProfile", "-Command", perf_commit],
                                       capture_output=True, text=True, timeout=10,
                                       creationflags=CREATE_NO_WINDOW)
                    vals = [float(x) for x in r.stdout.split() if x.strip()]
                    if vals:
                        with self.lock:
                            self.mem["load_percent"] = round(vals[0], 1)
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(2)

    # ---- 网络 / 磁盘 I/O 线程: 每 ~2s ----
    def io_thread(self):
        while not self._stop:
            try:
                now = time.time()
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
            "temperature_source": "LibreHardwareMonitor" if cpu_temp is not None else None,
            "power_source": "LibreHardwareMonitor" if not power_est else "TDP 估算",
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
            "temperature_source": "LibreHardwareMonitor" if mem_temp is not None else None,
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
        import sqlite3
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS samples("
            "ts REAL PRIMARY KEY, gpu_pw REAL, gpu_util REAL, gpu_temp REAL, gpu_e_wh REAL,"
            " cpu_util REAL, cpu_pw REAL, cpu_e_wh REAL, mem_pct REAL, total_pw REAL,"
            " total_e_wh REAL, total_cost REAL)")
        self.conn.commit()
        # 兼容旧库 (运行过早期版本, 仅 12 列): 缺失的网络/磁盘列用 ALTER 补齐
        self._migrate()

    def _migrate(self):
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
        try:
            cutoff = time.time() - self.retention_days * 86400.0
            self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self.conn.commit()
        except Exception:
            pass

    def query(self, range_key="24h"):
        secs = self.RANGES.get(range_key, 86400)
        start = time.time() - secs
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

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    monitor = None
    sys = None
    meter = None
    history = None
    html_path = None
    auth_token = None
    server_version = "GPUMonitor/1.3"

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            try:
                with open(self.html_path, "r", encoding="utf-8") as f:
                    html = f.read()
                # 注入本地 API 令牌, 使同源页面在开放(--host 0.0.0.0)时也能写操作
                if Handler.auth_token:
                    html = html.replace("__API_TOKEN__", Handler.auth_token)
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if path == "/api/metrics":
            gpus = self.monitor.get_snapshot()
            self._send(200, json.dumps({
                "gpus": gpus,
                "cluster": self.monitor.gpu_cluster_summary(),
                "server_time": time.time(),
            }))
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
            self._send(200, json.dumps(self.meter.snapshot() if self.meter else {"error": "no meter"}))
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
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # 写操作鉴权: 本地 (127.0.0.1/::1) 信任; 远程调用需携带 X-Api-Token
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            if not Handler.auth_token or self.headers.get("X-Api-Token") != Handler.auth_token:
                self._send(403, json.dumps({"error": "forbidden: 远程写操作需要 X-Api-Token 令牌"}))
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
            meter.reset_energy()
            self._send(200, json.dumps({"ok": True}))
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

    # 持久化计量表 (累计能耗 / 电费 / 电价 / 每日明细)
    meter_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meter.json")
    meter = Meter(meter_path, rate=0.60)
    if args.price is not None:
        meter.set_rate(args.price)

    # 时序历史落盘 (SQLite) —— 供历史页回放
    history_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
    history = History(history_path, interval=args.hist_interval, retention_days=args.hist_retention)

    print("> 正在初始化 NVML ...")
    monitor = Monitor(meter=meter)
    poll_thread = threading.Thread(target=monitor.run, args=(args.interval,), daemon=True)
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
    Handler.html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://localhost:{args.port}"

    # 后台周期落盘, 保证异常退出也能持久化
    def _saver():
        while True:
            time.sleep(10)
            meter.save()
    threading.Thread(target=_saver, daemon=True).start()

    # 后台历史采样写入 (按 --hist-interval 采样当前快照到 history.db)
    def _history_writer():
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
                history.prune()
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
        os._exit(0)
    try:
        import signal
        signal.signal(signal.SIGTERM, _graceful_stop)
    except Exception:
        pass
    import atexit
    atexit.register(lambda: _errlog("atexit: process exiting (clean exit -> NOT killed/OOM)"))
    atexit.register(lambda: _safe_save(meter))

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
            server = ThreadingHTTPServer((args.host, args.port), Handler)
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
