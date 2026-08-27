# GPU Monitor — 实时 GPU 监测面板

一个轻量的本地 GPU 监测服务：通过 NVIDIA NVML 实时采集利用率、显存、功率、温度、风扇、时钟、节流状态与每进程占用，并在浏览器面板中可视化；同时汇总 CPU / 内存与整机功率，按电价累计耗电量与电费。

> 状态：**个人项目 / 技术预览（pre-release）**。以 [MIT 许可证](LICENSE) 开源。
>
> **平台支持**：
> - **Windows** — 全功能（NVML + WMI + 性能计数器 + LibreHardwareMonitor 真实温度/功率），已实机验证。
> - **Linux** — NVIDIA GPU 全功能（NVML 复用）；CPU 多路（`/proc/cpuinfo`）、温度/功率（hwmon + RAPL，功率需 root 读 RAPL）、systemd 开机自启。代码级验证完成，**待真实 Linux 服务器实机验证**。
> - **macOS** — 基础指标（psutil：CPU/内存/网络/磁盘）可用；Apple Silicon 无 NVML，GPU 检测与功耗/温度暂不可用（待后续版本）。

---

## 功能

- **GPU 实时指标**（NVML）：利用率、显存使用/带宽占用、功率、温度、风扇 RPM、核心/显存频率、P-State、PCIe 链路与吞吐、BAR1、累计能耗。
- **等效算力估算**：FP32 / FP16 / Tensor FP16（稠密 / 稀疏）峰值 × 利用率。
- **每进程明细**：SM% / 显存% / 编码% / 解码% + 真实显存占用（Windows 性能计数器）。
- **CPU / 内存监测**：利用率、每核心负载、估算功率、频率、温度（需 LibreHardwareMonitor，见下）、PCIe 实时传输。
- **网络 / 磁盘监测**：各网卡 IP / 速率 / 收发带宽、各物理磁盘读写吞吐、分区容量占用；历史页含网络 / 磁盘趋势曲线。
- **整机概览**：GPU + CPU + 内存汇总，整机估算功率与累计电费。
- **多卡 GPU / 多路 CPU 集群支持**：自动识别多张 NVIDIA GPU（NVML `deviceGetCount` 逐卡轮询）与多路 CPU（WMI `Win32_Processor` 逐路解析）；GPU 监测页进入先展示**多卡选择卡片网格**（每张卡显示型号、编号、核心利用率、显存利用率与实时 H100 等效算力，点击进入该卡详情），单卡则只显示一张卡片；概览新增「算力集群总览」卡片（GPU 卡数、CPU 路数/总核数、集群 AI 算力、集群 H100 等效），CPU 监测页按路展示每路利用率/功率/核心数；历史落盘改为全部 GPU 聚合。*多卡 / 多路路径当前在单卡 / 单路环境实机验证，多卡 / 多路实机验证待确认（见「验证状态与已知限制」）。*
- **持久化计量**：累计耗电量（度）与电费跨重启保存。
- **累计 H100 等效算力时长**：将各卡「利用率 × 时长」按 FP16 Tensor 稠密峰值折算成 H100 等效 GPU 小时（`gpu_h100_hours`），跨重启持久化，概览「等效 AI 算力」卡片可查看。
- **历史回放**：功率 / 利用率 / 温度 / 累计能耗 趋势曲线（SQLite 落盘，默认保留 30 天）。
- **数据导出**：历史库可一键导出为 **CSV**（Excel 友好，含可读时间列与 17 个指标列）或 **JSON**（含工具名/生成时间/范围/列定义等元数据），支持 1h / 6h / 24h / 7d / 30d / 全部历史。设置页「数据管理」卡片内选择格式与范围即可下载。
- **设置页**：采集与历史采样间隔、显示偏好（温度/功率单位、货币符号、界面主题、面板刷新频率）、告警阈值（温度/利用率 + 浏览器桌面通知）、Windows 开机自启、电价与数据管理。偏好落盘 `prefs.json`（自动生成，已 gitignore）。
- **多语言（中 / 英）**：自动识别浏览器语言——中文环境显示中文，其他语言显示英文；也可在侧栏底部手动切换并记住选择。静态文案、图表悬浮名称、算力阶梯标签与节流原因均随语言切换。

### 关于温度与真实 CPU 功率

Windows 下 WMI 通常不暴露 CPU / 内存温度传感器，因此默认显示 `N/A`。安装并运行 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)（勾选「启用 WMI / 运行 Web 服务」，建议管理员运行）后，面板会**自动**读取真实 CPU 封装温度、CPU 封装功率与内存温度，无需重启。

---

## 架构

```
gpu_monitor.py  ── NVML 轮询线程 + 纯标准库 HTTP 服务 (无 Flask 依赖)
index.html      ── 前端面板 (无外部依赖, 纯 Canvas 图表)
meter.json      ── 持久化计量 (累计能耗/电费/电价, 自动生成, 已 gitignore)
prefs.json       ── 用户偏好设置 (采集/显示/告警/自启, 自动生成, 已 gitignore)
auth.json       ── 本地 API 令牌 (自动生成, 已 gitignore)
history.db      ── 历史时序数据 (SQLite, 自动生成, 已 gitignore)
watchdog.py     ── 看门狗守护进程 (端口探测 + 主服务崩溃自动重启, 自愈)
```

后端使用 Python 标准库 `http.server`，仅依赖 `pynvml`（NVML 绑定）与 `psutil`（系统指标）。

---

## 安装

要求 Python ≥ 3.8。GPU 指标需要 NVIDIA 驱动（含 NVML）；非 NVIDIA 平台 GPU 页显示降级提示。

```bash
pip install -r requirements.txt
```

依赖：`nvidia-ml-py`（导入名 `pynvml`）、`psutil`。

> 若处于离线/受限环境，`requirements.txt` 中的包也可放入同目录 `pylibs/`，程序会自动将其加入导入路径。

---

## 运行

### 便捷脚本（Windows）

- `start_gpu_monitor.bat` — 启动（后台 `pythonw`，无黑窗口；自动使用 PATH 中的 Python，无需修改脚本）
- `stop_gpu_monitor.bat` — 优雅停止（落盘后退出）
- `status_gpu_monitor.bat` — 查看运行状态
- `install_lhm.bat` — 下载 LibreHardwareMonitor（用于真实温度/功率）

> Windows 便捷脚本会自动定位脚本所在目录与 PATH 中的 Python 解释器（`pythonw` 优先），clone 到任意位置均可直接运行。

### 便捷脚本（Linux / macOS）

```bash
chmod +x start_gpu_monitor.sh stop_gpu_monitor.sh status_gpu_monitor.sh
./start_gpu_monitor.sh     # 后台启动主服务 + 看门狗
./status_gpu_monitor.sh    # 查看状态
./stop_gpu_monitor.sh      # 优雅停止
```

- Linux 上 CPU 温度来自 hwmon（`coretemp`/`k10temp`）；封装功率来自 RAPL（`/sys/class/powercap/.../energy_uj`），非 root 不可读时自动降级为 TDP 估算。
- 开机自启：在设置页开启后，会写入 `~/.config/systemd/user/gpu-monitor.service`（systemd 用户级，登录会话后生效）。

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

## 验证状态与已知限制

### 验证边界（诚实说明）

- 多卡 GPU / 多路 CPU 的适配逻辑（枚举、逐卡快照、卡片网格、集群汇总、历史聚合）已实现并通过单元测试，但**当前仅在单卡 / 单路环境实机运行验证**；多卡 / 多路路径尚未在真实多卡 / 多路机器上验证，如遇问题请在 Issues 反馈。
- **Linux**：平台分支（NVML 复用、`/proc/cpuinfo` 多路、hwmon/RAPL 温度功率、systemd 自启）已通过代码级单元测试（模拟数据源），**尚未在真实 Linux 服务器实机验证**；如你有 Linux 服务器（尤其是多卡/多路），欢迎部署后反馈。
- **macOS**：当前仅基础指标（psutil）可用，Apple Silicon 的 GPU/功耗/温度后端待实现，界面显示降级提示。
- CI 覆盖核心逻辑（Meter / History / NVML 自愈等），不包含真实硬件采集。

### 估算值

界面中标 `*` 的数值为估算值，非实测：

- **CPU 功率**：按 TDP 曲线估算（未安装 LibreHardwareMonitor 时）；安装并运行 LHM 后为实测封装功率。
- **AVX / AVX-512 算力**：按「每核每周期 16/32 FLOP × 核心数 × 最高频率」FLOP 模型估算的上界，不同微架构（如双 FMA 单元）未细分。
- **TDP**：按 CPU 型号猜测的典型值。
- **整机功率** = GPU 实测 + CPU 估算，不含主板 / 风扇 / 外设基础功耗，属近似上界。

### 已知限制

- 多卡轮询为**串行**（逐卡 NVML），卡数较多（≥16）或驱动繁忙时，单轮采样耗时可能接近采样间隔。
- 同一轮多卡快照存在毫秒级时间差，非严格原子快照。
- 逐路 CPU 利用率按「每路线程数对称切片」估算，非对称多路 / NUMA 拓扑下逐路数值可能错位。

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
| GET | `/api/settings` | 持久化计量状态 + 用户偏好（prefs / autostart_active） |
| GET | `/api/history?range=24h` | 历史数据（1h/6h/24h/7d/30d） |
| GET | `/api/export?format=csv&range=24h` | 导出历史为 CSV / JSON（`format=csv\|json`，`range=1h\|6h\|24h\|7d\|30d\|all`；响应带 `Content-Disposition` 下载头） |
| POST | `/api/settings` | 持久化计量状态 + 用户偏好（prefs / autostart_active） |
| POST | `/api/settings/price` | 设置电价（需令牌，远程） |
| POST | `/api/settings/prefs` | 批量更新偏好（采样/单位/主题/刷新/告警，运行时生效，需令牌） |
| POST | `/api/settings/autostart` | 开关 Windows 开机自启（需令牌） |
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
- [x] 多 GPU / 多路 CPU 支持（NVML 逐卡轮询 + WMI 逐路解析，GPU / CPU 卡片网格）
- [ ] 阈值告警（桌面通知 / Webhook）
- [ ] 系统托盘常驻
- [ ] CSV / 长期归档导出
- [x] 许可证与发布流程（MIT）

---

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。详见 [LICENSE](LICENSE) 文件。
