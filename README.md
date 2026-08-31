# GPU Monitor — 实时 GPU 监测面板

一个轻量的本地 GPU 监测服务：通过 NVIDIA NVML 实时采集利用率、显存、功率、温度、风扇、时钟、节流状态与每进程占用，并在浏览器面板中可视化；同时汇总 CPU / 内存与整机功率，按电价累计耗电量与电费。

> 状态：**个人项目 / 技术预览（pre-release）**。以 [MIT 许可证](LICENSE) 开源。
>
> **平台支持**：
> - **Windows** — 全功能（NVML + WMI + 性能计数器 + LibreHardwareMonitor 真实温度/功率），已实机验证。
> - **Linux** — NVIDIA GPU 全功能（NVML 复用）；CPU 多路（`/proc/cpuinfo`）、温度/功率（hwmon + RAPL，功率需 root 读 RAPL）、systemd 开机自启。代码级验证完成，**待真实 Linux 服务器实机验证**。
> - **macOS** — 基础指标（psutil：CPU/内存/网络/磁盘）可用；Apple Silicon 无 NVML，GPU 检测与功耗/温度暂不可用（待后续版本）。

> 当前版本：**0.1.7**（与 `gpu_monitor.py` 的 `__version__` 保持一致）。

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
- **系统详细信息页**：侧栏「系统信息」静态罗列当前设备的 CPU（逐路型号/核心/线程/频率/电压/TDP/缓存/架构/虚拟化/指令集）、GPU（型号/架构/CUDA 与 Tensor 核心/显存/带宽/峰值算力/功耗上限/PCIe 链路/计算模式）、内存（总容量/频率 + 每条 DIMM 明细）、主板、BIOS、电源（系统类型/电池/活动电源计划）等详尽规格。**仅展示静态硬件规格，不含任何实时使用率**；无对应传感器时显示 `N/A` 而非编造。

### 关于温度与真实 CPU 功率

Windows 下 WMI 通常不暴露 CPU / 内存温度传感器，因此默认显示 `N/A`。安装并运行 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)（勾选「启用 WMI / 运行 Web 服务」，建议管理员运行）后，面板会**自动**读取真实 CPU 封装温度、CPU 封装功率与内存温度，无需重启。

---

## 架构

```
gpu_monitor.py  ── NVML 轮询线程 + 纯标准库 HTTP 服务 (无 Flask 依赖)
index.html      ── 前端面板 (无外部依赖, 纯 Canvas 图表)
meter.json      ── 持久化计量 (累计能耗/电费/电价, 自动生成, 已 gitignore)
prefs.json      ── 用户偏好设置 (采集/显示/告警/自启, 自动生成, 已 gitignore)
auth.json       ── 本地 API 令牌 (自动生成, 已 gitignore)
history.db      ── 历史时序数据 (SQLite, 自动生成, 已 gitignore)
watchdog.py     ── 看门狗守护进程 (端口探测 + 主服务崩溃自动重启, 自愈)
monitor.pid     ── 主服务 PID (由主服务自己写入; stop 时按此终止, 会先校验命令行)
monitor.port    ── 主服务实际监听的端口 (端口冲突自动避让后写入; 脚本与 watchdog 统一读它)
stop.flag       ── stop 脚本创建的信号文件 (watchdog 见到即停止守护并退出, 避免 stop 后被立刻拉起)
watchdog.lock   ── watchdog 单实例锁 (存 watchdog 自身 PID)
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

### 端口

默认 **8080**。若已被其他程序占用，主服务会**自动向上探测**（最多 +10）并把实际端口写入 `monitor.port`；watchdog 与 `start` / `stop` / `status` 脚本都读这个文件，因此三者始终一致。

想固定端口，设置环境变量 `GPU_MONITOR_PORT`；它只在 `monitor.port` 不存在时生效 —— 服务一旦启动，实际端口以 `monitor.port` 为准。

同一端口上若已有本服务实例在跑，新的启动会**直接退出**并打印已有实例的 PID（避免两个实例同时写 `meter.json` 与 `history.db`）。

---

## 验证状态与已知限制

### 验证边界（诚实说明）

- 多卡 GPU / 多路 CPU 的适配逻辑（枚举、逐卡快照、卡片网格、集群汇总、历史聚合）已实现，但**当前仅在单卡 / 单路环境实机运行验证**；多卡 / 多路路径尚未在真实多卡 / 多路机器上验证，如遇问题请在 Issues 反馈。
- **Linux**：平台分支（NVML 复用、`/proc/cpuinfo` 多路、hwmon/RAPL 温度功率、systemd 自启）**仅有代码级自动化测试**（`tests/test_crossplatform.py`，用模拟数据源构造 `/proc/cpuinfo` / hwmon / RAPL 读数，不含真实采集），**尚未在真实 Linux 服务器实机验证**；如你有 Linux 服务器（尤其是多卡/多路/ARM），欢迎部署后反馈。
- **macOS**：当前仅基础指标（psutil）可用，Apple Silicon 的 GPU/功耗/温度后端待实现，界面显示降级提示。
- **CI**：在 `ubuntu-latest` 上运行 `tests/test_core.py`（Meter / History / 节流解码）与 `tests/test_crossplatform.py`（跨平台数据源模拟），**均为纯逻辑与模拟数据测试，不包含真实硬件采集**，也不覆盖 Windows / macOS 的实机路径。
- **ARM Linux（aarch64）**：`/proc/cpuinfo` 不提供 `physical id` / `core id`，拓扑解析依赖 `processor` 编号退化实现，多路 ARM 服务器的分路可能不准确（单路已修正，见「已知限制」）。

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
- **仅支持 NVIDIA GPU**（依赖 NVML）。AMD / Intel 独显与 Apple Silicon 的 GPU 指标、功耗、温度均不可用，GPU 页会显示降级原因提示。
- **ARM Linux（aarch64）**：已按 `processor` 编号退化解析核心数（此前会误算为 1），但由于缺少 `physical id` 字段，**所有核心会被归为同一路**，真正的多路 ARM 服务器分路仍不准确。x86_64 不受影响。
- **「内存提交负载」（`load_percent`）**：Windows 取自 `GlobalMemoryStatusEx`，Linux 取自 `/proc/meminfo` 的 `Committed_AS / CommitLimit`（内核未导出 `CommitLimit` 时为 `N/A`）；**macOS 无等价概念，恒为 `N/A`**，不做近似伪造。
- **Windows 上每进程显存**仍依赖 PowerShell 读 PDH 计数器（NVML 在 WDDM 模式下不提供该值）。已加 5 秒缓存与空闲跳过，实测子进程频率下降约 83%；彻底改为 ctypes 直调 PDH 是后续项。

---

## 安全说明

- 敏感**写**操作（`/api/shutdown`、`/api/settings/...`）受本地 API 令牌保护：
  - **本机（127.0.0.1）调用无需令牌**（信任本地）。
  - **远程调用必须携带 `X-Api-Token` 头**（令牌由 `auth.json` 自动生成）。
- **所有 GET 接口无鉴权**：`--host 0.0.0.0` 时，任何能访问该端口的人都可读取进程列表、网卡 IP、磁盘分区、累计电费等信息。**请勿在不可信网络（公共 Wi-Fi、公网）上以 `0.0.0.0` 运行。**
- 令牌会注入到页面 HTML 中供前端调用写接口，因此**任何能打开面板的人都能拿到令牌**；令牌只用于区分"读到面板的人"，不构成真正的访问控制。
- **CSRF 防护**：写接口校验 `Origin` 头，来自其他站点的请求会被拒绝（除非同时携带有效令牌，这是局域网面板正常工作的必要取舍）。不带 `Origin` 的客户端（curl / 启停脚本）不受影响。
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
| POST | `/api/settings/price` | 设置电价（需令牌，远程） |
| POST | `/api/settings/prefs` | 批量更新偏好（采样/单位/主题/刷新/告警，运行时生效，需令牌） |
| POST | `/api/settings/autostart` | 开关开机自启（Windows 启动文件夹 / Linux systemd user，需令牌） |
| POST | `/api/settings/reset_meter` | 清除累计能耗与电费 / 每日明细（电价保留，需令牌） |
| POST | `/api/settings/reset_all` | 在上一条基础上**清空 `history.db` 中的全部时序样本**（历史曲线不可恢复，需令牌） |
| POST | `/api/shutdown` | 优雅停止（需令牌，远程） |

> `/api/metrics` 在 GPU 数据为空时会额外返回 `gpu_unavailable_reason`（`code` 取值：`pynvml_missing` / `nvml_driver` / `nvml_runtime` / `no_gpu` / `unknown`，附 `detail` 原始错误），前端据此显示本地化的降级提示。
>
> `reset_meter` 只清零持久化计量（`meter.json`）；`reset_all` 在此基础上还会 `DELETE FROM samples` + `VACUUM`，**清空 `history.db` 中的全部时序样本**（历史曲线不可恢复）。两者都保留电价设置，且都需要二次确认。

---

## 测试

```bash
python tests/test_core.py           # Meter 累计/跨重启、History 落盘/裁剪、节流原因解码
python tests/test_crossplatform.py  # 跨平台数据源模拟: /proc/cpuinfo 多路解析、hwmon 温度、RAPL 功率差分
```

两套测试均为**纯逻辑 / 模拟数据**，不需要 GPU，也不需要真实 Linux 环境（通过注入模拟的 `/proc/cpuinfo` 等文件内容实现）。CI 中会同时运行这两套。

---

## 路线图

- [x] 跨平台骨架（Linux：NVML 复用 + `/proc/cpuinfo` 多路 + hwmon/RAPL + systemd 自启；macOS：psutil 基础指标）
- [x] 多 GPU / 多路 CPU 支持（NVML 逐卡轮询 + WMI 逐路解析，GPU / CPU 卡片网格）
- [x] CSV / JSON 历史导出
- [x] 阈值告警（温度 / 低利用率 + 浏览器桌面通知）
- [x] 许可证与发布流程（MIT）
- [x] ARM Linux（aarch64）CPU 拓扑解析修正（此前核心数误算为 1）
- [x] 写接口 CSRF 校验（`Origin` 头检查）
- [x] 端口冲突自动避让 + 单一真源（`monitor.port`，脚本与 watchdog 统一读取）
- [x] 单实例保护（阻止两个实例同时监听同一端口互相覆盖计量数据）
- [x] Linux 提交内存负载 / thermal_zone 温度回退
- [ ] Webhook 告警（当前仅浏览器桌面通知）
- [ ] 系统托盘常驻
- [ ] 开机自启 / 看门狗的 Linux 与 macOS 实机验证
- [ ] 多卡 / 多路实机验证
- [ ] **aarch64 与 thermal_zone 的真机验证**（当前仅模拟数据覆盖）
- [ ] 历史库降采样归档（当前 30 天约 19 MB，全量细粒度保存）
- [ ] 每进程显存改用 ctypes 直调 PDH（当前仍每 ~5 秒 spawn 一个 PowerShell）

---

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。详见 [LICENSE](LICENSE) 文件。
