# GPU Monitor — 实时 GPU 监测面板

一个轻量的本地 GPU 监测服务：通过 NVIDIA NVML 实时采集利用率、显存、功率、温度、风扇、时钟、节流状态与每进程占用，并在浏览器面板中可视化；同时汇总 CPU / 内存与整机功率，按电价累计耗电量与电费。

> 状态：**个人项目 / 技术预览（pre-release）**。当前聚焦 Windows 平台（部分指标依赖 Windows 性能计数器 / WMI）。以 [MIT 许可证](LICENSE) 开源。

---

## 功能

- **GPU 实时指标**（NVML）：利用率、显存使用/带宽占用、功率、温度、风扇 RPM、核心/显存频率、P-State、PCIe 链路与吞吐、BAR1、累计能耗。
- **等效算力估算**：FP32 / FP16 / Tensor FP16（稠密 / 稀疏）峰值 × 利用率。
- **每进程明细**：SM% / 显存% / 编码% / 解码% + 真实显存占用（Windows 性能计数器）。
- **CPU / 内存监测**：利用率、每核心负载、估算功率、频率、温度（需 LibreHardwareMonitor，见下）、PCIe 实时传输。
- **网络 / 磁盘监测**：各网卡 IP / 速率 / 收发带宽、各物理磁盘读写吞吐、分区容量占用；历史页含网络 / 磁盘趋势曲线。
- **整机概览**：GPU + CPU + 内存汇总，整机估算功率与累计电费。
- **持久化计量**：累计耗电量（度）与电费跨重启保存。
- **累计 H100 等效算力时长**：将各卡「利用率 × 时长」按 FP16 Tensor 稠密峰值折算成 H100 等效 GPU 小时（`gpu_h100_hours`），跨重启持久化，概览「等效 AI 算力」卡片与设置「关于」卡片均可查看。
- **历史回放**：功率 / 利用率 / 温度 / 累计能耗 趋势曲线（SQLite 落盘，默认保留 30 天）。
- **设置页**：调整电价、查看每日明细、清除累计。

### 关于温度与真实 CPU 功率

Windows 下 WMI 通常不暴露 CPU / 内存温度传感器，因此默认显示 `N/A`。安装并运行 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)（勾选「启用 WMI / 运行 Web 服务」，建议管理员运行）后，面板会**自动**读取真实 CPU 封装温度、CPU 封装功率与内存温度，无需重启。

---

## 架构

```
gpu_monitor.py  ── NVML 轮询线程 + 纯标准库 HTTP 服务 (无 Flask 依赖)
index.html      ── 前端面板 (无外部依赖, 纯 Canvas 图表)
meter.json      ── 持久化计量 (累计能耗/电费/电价, 自动生成, 已 gitignore)
auth.json       ── 本地 API 令牌 (自动生成, 已 gitignore)
history.db      ── 历史时序数据 (SQLite, 自动生成, 已 gitignore)
watchdog.py     ── 看门狗守护进程 (端口探测 + 主服务崩溃自动重启, 自愈)
```

后端使用 Python 标准库 `http.server`，仅依赖 `pynvml`（NVML 绑定）与 `psutil`（系统指标）。

---

## 安装

要求 Python ≥ 3.8，且已安装 NVIDIA 驱动（含 NVML）。

```bash
pip install -r requirements.txt
```

依赖：`nvidia-ml-py`（导入名 `pynvml`）、`psutil`。

> 若处于离线/受限环境，`requirements.txt` 中的包也可放入同目录 `pylibs/`，程序会自动将其加入导入路径。

---

## 运行

### 便捷脚本（Windows）

- `start_gpu_monitor.bat` — 启动（后台 `pythonw`，无黑窗口）
- `stop_gpu_monitor.bat` — 优雅停止（落盘后退出）
- `status_gpu_monitor.bat` — 查看运行状态
- `install_lhm.bat` — 下载 LibreHardwareMonitor（用于真实温度/功率）

启动后访问：**http://localhost:8080**

### 命令行

```bash
python gpu_monitor.py --port 8080 --interval 0.5
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8080 | 监听端口 |
| `--host` | 127.0.0.1 | 绑定地址；局域网访问改用 `0.0.0.0`（远程写操作需 API 令牌） |
| `--interval` | 0.5 | GPU 采样间隔（秒） |
| `--price` | 使用已存值/0.60 | 电价（元/度） |
| `--hist-interval` | 5 | 历史采样间隔（秒） |
| `--hist-retention` | 30 | 历史保留天数 |

---

## 安全说明

- 敏感写操作（`/api/shutdown`、`/api/settings/...`）受本地 API 令牌保护：
  - **本机（127.0.0.1）调用无需令牌**（信任本地）。
  - **远程调用必须携带 `X-Api-Token` 头**（令牌由 `auth.json` 自动生成）。
- 默认仅绑定 `127.0.0.1`。如需局域网访问，请显式使用 `--host 0.0.0.0`，并注意令牌不应泄露。

---

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/metrics` | 所有 GPU 实时快照 |
| GET | `/api/metrics/<idx>` | 指定 GPU 快照 |
| GET | `/api/cpu` | CPU 快照（含 LHM 状态） |
| GET | `/api/memory` | 内存快照 |
| GET | `/api/settings` | 持久化计量状态 |
| GET | `/api/history?range=24h` | 历史数据（1h/6h/24h/7d/30d） |
| POST | `/api/settings/price` | 设置电价（需令牌，远程） |
| POST | `/api/settings/reset_meter` | 清除累计能耗与电费（需令牌） |
| POST | `/api/settings/reset_all` | 清除全部历史（需令牌） |
| POST | `/api/shutdown` | 优雅停止（需令牌，远程） |

---

## 测试

```bash
python tests/test_core.py
```

覆盖 Meter 累计/跨重启、History 落盘/裁剪、节流原因解码等核心逻辑（无需 GPU）。

---

## 路线图

- [ ] 跨平台（Linux 下 `nvidia-smi` 后端）
- [ ] 多 GPU 支持（当前只读 index 0）
- [ ] 阈值告警（桌面通知 / Webhook）
- [ ] 系统托盘常驻
- [ ] CSV / 长期归档导出
- [x] 许可证与发布流程（MIT）

---

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。详见 [LICENSE](LICENSE) 文件。
