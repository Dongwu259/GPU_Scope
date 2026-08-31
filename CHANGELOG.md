# 更新日志 (Changelog)

本项目遵循「功能累加」式记录（暂未采用严格 SemVer）。版本号以 `gpu_monitor.py` 的 `__version__` 为准。

## 0.1.7 — 系统详细信息页 + 规格库校准 + 硬编码回退清除

- **新增「系统信息」页面（静态硬件规格，不含实时使用率）**：后端新增 `GET /api/system`，前端新增「系统信息」侧栏导航与渲染。尽可能详尽地罗列当前设备规格，全部字段优雅降级（`None` / `N/A` / 空串），绝不编造：
  - **CPU（逐路）**：型号、核心/线程数、最大/基础频率、电压、TDP、family/model/stepping、L2/L3 缓存（KB）、架构、厂商、processor id、虚拟化、指令集。Windows 指令集用 `IsProcessorFeaturePresent` **真实探测**（SSE→AVX-512F），不再为空壳。
  - **GPU（逐卡，取自实时快照 + 规格库）**：型号、UUID、驱动/CUDA 版本、架构、CUDA 核心、Tensor 核心、显存容量、带宽、加速频率、FP32/FP16/Tensor FP16 稠密峰值、功耗上限、PCIe 链路（代际/宽度/最大代际）、计算模式。
  - **内存**：总容量、频率，以及**每条 DIMM** 的容量/速率/厂商/型号/插槽/形态/类型/序列号。
  - **主板 / BIOS / 电源**：厂商/型号/版本/序列号、BIOS 版本与发布日期、系统类型（台式机/笔记本/…）、电池（笔记本）、当前活动电源计划（中文 Windows 正确解码，无乱码）。
- **校准 GPU 规格库数值（对齐官方 datasheet）**：上一轮把规格库从 5 张扩到 95 张时，部分数值为近似推算，本轮校准：
  - 数据中心卡 FP32 着色器峰值改用**官方标称值**（H100-SXM-80GB=51.2、H100-PCIe-80GB=48.4、H800-SXM-80GB=51.2、L40S=91.6、L40=90.5 TFLOPS）。`_dc()` 新增 `fp32_override` 参数固定官方值，不再用 `CC×2×boost` 推算（该推算与 Hopper/新架构官方值偏差大）。
  - 修正 **Intel XMX 乘数**：原 `_intel()` 误设 `matrix_mult=2.0`，使 XMX FP16 被高估一倍；改为 `1.0`（Intel XMX FP16 稠密 = 2×FP32，与 NVIDIA Tensor 的 4×FP32 不同）。
- **前端渲染性能优化（消除每帧全文档翻译冗余）**：原 `tick()` 每次刷新都对**整棵文档**（含导航/侧栏/所有隐藏页）跑一遍 `applyTranslations()` 的 TreeWalker + 属性扫描，是历史记录里标注的渲染瓶颈。本轮：
  - `applyTranslations()` 新增语言状态守卫 `_appliedLang`：**中文（源语言）且语言自上次完整翻译以来未变时直接短路**，零开销（模板本身已是中文）。仅当语言发生 zh⇄en 切换时才做完整遍历，翻译正确性不受影响。
  - 每帧 `applyTranslations()` 从「整文档」收窄为「当前可见页」（`page-<activePage>`）；隐藏页在切换时由 `setPage`/`applyLang` 负责整体翻译。英文用户每帧也只翻译正在看的页面，不再扫全树。
  - 经 jsdom 实跑校验：zh 稳定态 50 次重复翻译恒为无操作且不抛错；zh⇄en 双向切换文案均正确还原。
- **清除残余的硬编码回退（与原始 bug 同根）**：名称缺失时 `resolve_spec("")` 此前回退到 **RTX 5080 的高性能参数**；现改为全库 FP32 中位数保守估算，并删除 `DEFAULT_SPEC` 常量。新增回归测试锁定，确保不再套用某张具体高端卡。
- **修复 Windows 指令集探测为空**：原 `_wmi_cpu_detail()` 以 `flags=None` 调用 `_cpu_instruction_sets()` 且无任何真实探测，导致「系统信息」页指令集永远为空；新增 `_wmi_instruction_sets()` 用 `IsProcessorFeaturePresent` 真实探测，并修正架构标签匹配（同时接受 `"x86_64"` 与 `"x86-64"`）。
- 测试：`tests/test_crossplatform.py` 由 94 项扩至 **120 项**（新增空名称规格回归、系统详情结构、指令集真实探测三组）。

## 0.1.6 — 审查修复（P0 缺陷 + 文档可信度）

- **修复 Windows 开机自启完全失效（P0）**：原实现把 `start_gpu_monitor.bat` 复制到启动文件夹，但源脚本用 `%~dp0` 定位自身目录，副本会 `cd` 到**启动文件夹**再去找 `gpu_monitor.py`，必然失败；而设置页因只检测文件存在仍显示"已开启"，属静默失效。现改为**生成写死项目绝对路径的启动器**（主服务 + watchdog），并用系统 ANSI 编码写入以兼容含非 ASCII 的项目路径。
- **修复无 GPU / 缺依赖时前端零提示（P0）**：此前 GPU 页会永久停在"正在连接 NVML …"。后端 `/api/metrics` 在 GPU 数据为空时新增 `gpu_unavailable_reason`（`pynvml_missing` / `nvml_driver` / `nvml_runtime` / `no_gpu` / `unknown` + 原始错误），前端渲染对应的中英文降级提示。
- **修复陈旧 `monitor.pid` 可能误杀无关进程（P0）**：Windows 的 start 脚本此前不写 PID 文件（只有 watchdog 重启时才写），stop 时 watchdog 会按可能是几天前的 PID 直接 `TerminateProcess`，PID 被系统复用即误杀。现由主服务启动时自行写入 `monitor.pid`，watchdog 在终止前**校验该进程命令行确实包含 `gpu_monitor.py`**；无法确认则不杀（stop 仍有 `/api/shutdown` 与按端口兜底）。
- **补 Linux 跨平台自动化测试（P0，文档可信度）**：0.1.5 曾声明"Linux 分支单元测试全部通过"，但仓库中并无此类测试，验证不可复现。新增 `tests/test_crossplatform.py`（模拟 `/proc/cpuinfo` 双路与 aarch64、hwmon 温度、RAPL 功率差分的负增量 / 计数器回绕 / 时钟回拨 / 无权限四档降级）并接入 CI。
- **修复「平均功率(自监控启动)」恒为 N/A**：能耗基线只在 `__init__` 取一次，启动瞬间 NVML 未就绪就会取到 `None` 且永不自愈（本机连续运行 22 小时的实例仍在显示 N/A）。现增加基线自愈（首次拿到有效读数时补设），并处理驱动重启 / 计数器回绕导致读数倒退的情况（重置基线，避免显示负功率）。
- **修复 ARM Linux（aarch64）核心数塌陷为 1**：`/proc/cpuinfo` 不提供 `physical id` / `core id`，原实现所有逻辑核会塌缩成同一个 key。现退化为用 `processor` 编号作核心标识；顺带修正 cpufreq 路径使用真实 processor 编号（此前用 `range(threads)`，多路机器会读到第一路的频率）。
- **修复 `reset_all` 名不副实**：此前它等价于 `reset_meter`，完全不动 `history.db`。现真正清空时序样本（`DELETE` + `VACUUM`），前端确认文案同步更新，清除后自动刷新历史页。
- **安全加固**：
  - **CSRF**：写接口增加 `Origin` 校验，阻断恶意网页以表单（`text/plain` 简单请求）跨站触发 `/api/shutdown`、`/api/settings/reset_meter`。不带 `Origin` 的客户端（curl / 启停脚本）与携带有效令牌的请求不受影响。
  - **令牌不再无条件下发**：默认绑定回环时**不注入** `__API_TOKEN__`（本地请求本就免鉴权）；仅 `--host 0.0.0.0` 对外暴露时才注入。
  - **XSS**：前端新增 `esc()`，进程名 / 网卡名 / 磁盘名 / 挂载点 / IP 在插入 `innerHTML` 前统一转义。
- **偏好设置校验**：新增 `Prefs.LIMITS` / `Prefs.ENUMS` —— 采样间隔、历史间隔、保留天数、刷新频率、告警阈值均钳制到合理范围；`theme` / 温度单位 / 功率单位非法值回退默认；布尔字段不再把字符串 `"false"` 判为 `True`（原先 `bool("false") == True`）。
- **历史库性能与并发**：`prune()` 从「每 5 秒全表扫描」改为「每小时一次」；SQLite 启用 WAL 并为所有操作加 `RLock`，消除读写并发抛 `database is locked` 导致历史图表偶发空白的问题；新增 `History.clear()`。
- **运维与体验**：watchdog 新增 20 秒启动宽限期（此前主服务冷启动稍慢就会被判定死亡并重复拉起）；LHM 探测失败改为指数退避（上限 5 分钟，此前 LHM 未安装时每 2 秒 spawn 一个 PowerShell 空转）；`status` / `stop` 两个 .bat 补 `pause`（双击不再一闪而过）；`requirements.txt` 与 `pyproject.toml` 给 `nvidia-ml-py` 加 `<14` 上界并补 `[tool.setuptools] py-modules`；更正 `load_percent` 注释（非 Windows 恒为 `N/A`，早期注释声称的"用 psutil percent"从未实现）。
- **文档修正**：删除 README 中不存在的 `POST /api/settings`；把"Linux 已通过单元测试"改为准确的"仅有代码级自动化测试、未实机验证"；路线图补全已实现项与待办项；安全说明补充 **GET 接口无鉴权**与令牌注入条件；架构小节补充 `monitor.pid` / `stop.flag` / `watchdog.lock`。
- **版本号统一**：`CHANGELOG` / `pyproject.toml` / HTTP `Server` 头 / 前端「关于」卡片统一由 `gpu_monitor.py` 的 `__version__` 驱动（此前四处不一致：0.1.5 / 0.1.0 / `GPUMonitor/1.3` / 0.1.0；前端版本号现由服务端注入 `__VERSION__`）。
- **修复 stop.flag 残留导致看门狗静默失效**：此前只按"文件是否存在"判断停止信号，一旦 flag 因故残留（stop 时 watchdog 未运行、或删除失败），下次 start 拉起 watchdog 后第一轮就会命中它，立刻杀掉刚启动的主服务并退出 —— 表现为"服务起来了但没有任何守护"。现改为**按文件 mtime 判断新旧**（早于本进程启动时间的 flag 视为旧信号并忽略），删除失败也不会误触发。
- **watchdog.py 可测试性重构**：单实例检查与主循环从模块级移入 `main()`（此前 `import watchdog` 会直接 `sys.exit(0)`，模块无法被复用或测试），并新增 `acquire_lock` / `release_lock` / `stopflag_armed` 等可独立调用的函数。
- **Windows 开机自启启动器编码修正**：用 `GetACP()` 取系统 ANSI 代码页（中文 Windows = cp936）写入 .bat，而不是 `locale.getpreferredencoding()`（Python 在 UTF-8 模式下会返回 utf-8，与 cmd 解析 .bat 的代码页不一致，中文项目路径会写成乱码）。
- `.gitignore` 补充 `stop.flag`、`monitor.port`（守护进程/运行时状态，此前未忽略）。

### 0.1.6 — 第二轮：健壮性、性能与一致性（P2 全清）

- **修复两个主服务实例能同时启动（本次实测发现）**：Windows 的 `SO_REUSEADDR` 语义与 POSIX 不同 —— 它允许**两个进程绑定同一个端口**，因此第二个实例的 `bind()` 不会失败，两者会静默共存（本机实测 `pid 33408` 与 `pid 6340` 同时 `LISTEN` 8080）。后果是双份 NVML 轮询、双份历史写入，且 `meter.json` 由两者互相覆盖导致能耗增量丢失。现 `MonitorHTTPServer` 在 Windows 上关闭 `SO_REUSEADDR` 并启用 `SO_EXCLUSIVEADDRUSE`（POSIX 保留 `SO_REUSEADDR` 以便快速重启），另加**显式单实例检测**：`/api/settings` 返回 `pid`，启动时若发现该端口上已有本服务实例则打印提示并退出。检测必须早于写 `monitor.pid`，否则重复实例会先篡改 PID 文件使检测失效。
- **端口冲突自动避让 + 单一真源**：端口此前硬编码在 `bat` / `sh` / `watchdog.py` 三处且与同机其他 8080 服务冲突时无任何提示。现在主服务绑定失败（仅 `EADDRINUSE` / `WSAEADDRINUSE`）时向上探测最多 10 个端口，实际端口写入 `monitor.port`；watchdog 与启停脚本一律从该文件读取（环境变量 `GPU_MONITOR_PORT` 可覆盖，仅在该文件缺失时生效）。非"端口被占"的绑定错误（权限、非法地址）照旧抛出，不会被避让逻辑掩盖。
- **测量时长改用单调时钟**：RAPL 功率差分、网络/磁盘速率差分、CPU 能耗累加、`prune` 节流此前都用 `time.time()`，NTP 时钟回拨会让整段数据被丢弃。现统一改用 `time.monotonic()`；落盘时间戳（`history.db` / `meter.json` / `first_seen`）保持墙钟语义不变。
- **修复中文 Windows 上 WMI 返回乱码**：`_wmi_json` 用 UTF-8 解码 PowerShell 输出，而 PowerShell 默认按系统 OEM 代码页（cp936）输出，中文 CPU 型号 / 内存品牌会变成乱码。现在脚本前强制设置 `[Console]::OutputEncoding`（实测：修复前 `锟斤拷` 式乱码，修复后完整）。
- **提交内存负载不再依赖子进程**：该指标原先每 2 秒 spawn 一个 PowerShell 读取性能计数器，现 Windows 改用 `kernel32.GlobalMemoryStatusEx`（与任务管理器"已提交 X/Y"同源，实测与计数器读数**完全一致**），Linux 用 `/proc/meminfo` 的 `Committed_AS / CommitLimit`；macOS 无等价概念，保持 `N/A` 不伪造。
- **Linux 温度增加 thermal_zone 回退**：hwmon 白名单改为子串匹配，且未命中时回退 `/sys/class/thermal/thermal_zone*/temp`（树莓派等 ARM 平台此前完全没有温度）。hwmon 命中时仍优先，不把 GPU / 电池分区温度冒充成 CPU 温度。
- **减少 Windows 子进程开销**：每进程显存的 PDH 查询加 5 秒 TTL 缓存，且上一轮为空时跳过一轮（注意"只跳过一轮"，否则会永久自锁）。实测主服务拉起 PowerShell 的频率从 **1.0 个/秒降至 0.187 个/秒（-83%）**；完全根治需要改用 ctypes 直调 PDH，留作后续。
- **watchdog 文件句柄泄漏**：`launch()` 每次重启主服务都 `open()` 两个日志文件且不关闭，长期运行会累积泄漏（Windows 默认 fd 上限 512）。现改为显式关闭父进程侧句柄。
- **命名统一**：对外产品名统一为 **GPU Monitor**（前端"关于"卡片、桌面通知标题、开机启动项文件名、systemd 单元描述、API 的 `tool` 字段此前混用 "GPU_Scope"）。GitHub 仓库地址 `GPU_Scope` 不变 —— 那是仓库名，不是产品名。
- **工程卫生**：清理垃圾文件 `server_8090.log` / `lhm_probe.txt`；`nul` 因是 Windows 保留设备名无法用常规方式删除，已在 CONTRIBUTING 说明成因与手动清除方法，并提示"勿在 Git Bash 下使用 `> nul`"。CONTRIBUTING 另补充第二套测试的使用方式与端口约定。
- 排查确认：**此前观察到的"孤立 pythonw 进程"（pid 13136，运行 32 小时）属于另一个项目**（`katago_control_agent.py`），与本服务无关。
- 测试：`tests/test_crossplatform.py` 由 68 项扩至 **81 项**（新增端口与单实例、提交内存负载、Linux 温度回退三组）。

## 0.1.5 — 跨平台骨架 + Linux 后端（Windows 全功能不变）

- **平台守卫**：新增 `IS_WINDOWS / IS_LINUX / IS_MAC` 常量，63 处 Windows 专属依赖（WMI / PDH / LHM / 启动文件夹 / windll）全部分支化；非 Windows 平台自动降级，不再报错。
- **watchdog.py 跨平台**：`ctypes.windll` 进程探测/终止改为 `os.kill(SIGTERM/0)`（POSIX 分支），Windows 行为不变。
- **Linux 后端**：
  - GPU：NVML 复用（`libnvidia-ml.so.1`），多卡/每进程显存/能耗全部可用。
  - CPU 多路：新增 `_linux_cpu_info()` 解析 `/proc/cpuinfo` 按 physical id 分组（含 EOF 收尾修复），逐路核心/线程/频率/TDP。
  - 温度/功率：新增 `LinuxThermal` 后端（hwmon 温度 + RAPL energy_uj 差分功率，接口与 LHMClient 兼容）；RAPL 无权限时自动降级 TDP 估算。
  - 内存/网络/磁盘：psutil（macOS 亦适用）。
  - 开机自启：设置页开启后写 `~/.config/systemd/user/gpu-monitor.service` 并尝试 enable。
- **Linux 部署脚本**：新增 `start/stop/status_gpu_monitor.sh`（nohup 后台 + watchdog，纯 ASCII）。
- **健壮性**：`_guess_tdp` 在 psutil 缺失时不再崩溃（`psutil.cpu_count` 判空）。
- **macOS**：基础指标（psutil）可用，Apple Silicon GPU/功耗/温度后端待后续版本（界面降级提示）。
- 验证：Linux 分支**当时仅以临时脚本验证**（模拟 `/proc/cpuinfo` 双路解析等），未落入仓库、无法复现；对应自动化测试已在 **0.1.6** 补齐（`tests/test_crossplatform.py`）。Windows 测试套件回归全绿。Linux 实机验证待用户服务器部署。

## 0.1.4 — 估算值标记与验证边界声明

- **估算值标记**：CPU 功率、AVX/AVX-512 算力、TDP、整机功率（含 CPU 估算）等数值统一加 `*` 标记，CPU 页与概览页新增图例说明（中英双语），明确「带 * 为估算值」。
- **验证边界声明**：README 新增「验证状态与已知限制」章节，诚实标注多卡/多路路径当前仅在单卡/单路环境实机验证；明确估算模型（CPU 功率按 TDP 曲线、AVX 算力按 FLOP 模型、TDP 按型号猜测）与已知限制（多卡串行轮询、非原子快照、对称多路切片假设）。

## 0.1.3 — 开源前清理（可移植性）

- 消除全部硬编码本机路径：`start/stop/status_gpu_monitor.bat` 改用 `%~dp0` 定位脚本目录；`start_gpu_monitor.bat` 与 `watchdog.py` 自动探测 Python 解释器（PATH 中 `pythonw` 优先，其次 `python`），不再依赖特定安装路径，clone 到任意机器即可运行。
- `install_lhm.bat` 文案由 GBK 中文改为纯 ASCII 英文，任何代码页的 Windows 均可正常显示与执行。
- 开源前审计：运行时文件（`history.db` / `meter.json` / `prefs.json` / `auth.json` / 日志 / `pylibs/` / `.workbuddy/`）均已被 `.gitignore` 排除且从未进入 git 历史；已扫描确认无 token / 密钥 / 私钥 / 内网 IP / 本机用户名泄露。

## 0.1.2 — NVML 句柄自愈（驱动中途重启不再需手动重启）

- 修复 GPU 突然无法识别：根因为启动时一次性创建的 NVML 句柄在驱动不稳定（睡眠唤醒 / 驱动重启 / 系统更新）时损坏，被 `poll_once` 永久复用导致永久 `error` 但不崩溃。
- 抽 `_init_nvml()`；`poll_once` 采样遇 NVML 异常时限频（≥30s）调 `_reinit_nvml_if_needed()` 重初始化并重建能耗 / 快照 / 进程缓存等依赖；`__init__` NVML 失败降级（count=0）由轮询自愈。

## 0.1.1 — 数据导出与设置增强

- **历史数据导出**：设置页「数据管理」卡片新增一键导出，支持 **CSV**（Excel 友好，含可读时间列 + 17 指标列）与 **JSON**（含工具名/生成时间/范围/列元数据）；范围可选 1h / 6h / 24h / 7d / 30d / 全部历史。后端新增 `GET /api/export?format=csv|json&range=...`，响应带 `Content-Disposition` 下载头。
- **设置页大扩充**（前序提交）：采集与历史采样间隔、显示偏好（温度/功率单位、货币、主题、刷新频率）、告警阈值（温度/利用率 + 桌面通知）、Windows 开机自启；偏好落盘 `prefs.json`，新增 `/api/settings/prefs`、`/api/settings/autostart` 接口。
- **多语言（中 / 英）**：自动识别浏览器语言（中文→中文，其他→英文），侧栏底部可手动切换并持久化。实现：`ZH2EN` 静态文案精确匹配翻译层（每帧幂等、可还原）+ `t(zh,en,sub)` 处理含插值动态文案 + `LADDER_EN`/`THROTTLE_EN` 映射后端算力阶梯与节流原因；图表 canvas 悬浮名称随语言重绘。

## 0.1.0 — 开源基准版

首个面向开源的功能集合，核心能力如下：

### 监测能力
- **GPU 实时指标**（NVML）：利用率、显存使用/带宽、功率、温度、风扇 RPM、核心/显存频率、P-State、PCIe 链路与吞吐、BAR1、累计能耗。
- **等效算力估算**：FP32 / FP16 / Tensor FP16（稠密 / 稀疏）峰值 × 利用率。
- **实时 H100 等效算力**：以 H100 FP16 Tensor 稠密 ≈ 989 TFLOPS 为基准，展示「当前算力 ≈ 几张 H100」。
- **每进程明细**：SM% / 显存% / 编码% / 解码% + 真实显存占用（Windows 性能计数器）。
- **CPU / 内存监测**：利用率、每核心负载、估算功率、频率、温度（需 LibreHardwareMonitor）。
- **网络 / 磁盘监测**：网卡 IP / 速率 / 收发带宽、磁盘读写吞吐、分区占用。

### 多卡 / 多路集群
- 自动识别多张 NVIDIA GPU（NVML 逐卡轮询）与多路 CPU（WMI 逐路解析）。
- GPU 监测页进入先展示**多卡选择卡片网格**（型号 / 编号 / 核心利用率 / 显存利用率 / 实时 H100 等效），点击进入该卡详情；单卡只显示一张卡片。
- CPU 监测页按路展示每路利用率 / 功率 / 核心数（卡片网格形式，适配多 CPU）。
- 概览新增「算力集群总览」卡片（GPU 卡数、CPU 路数/总核数、集群 AI 算力、集群 H100 等效）。
- 历史落盘改为全部 GPU 聚合。

### 可视化与持久化
- 所有折线图带有 **y 轴刻度数值 + 单位**，鼠标悬浮显示竖直参考线与各曲线带单位的具体数值。
- **持久化计量**：累计耗电量（度）与电费跨重启保存。
- **累计 H100 等效算力时长**：各卡「利用率 × 时长」按 FP16 Tensor 稠密峰值折算成 H100 等效 GPU 小时，跨重启持久化。
- **历史回放**：功率 / 利用率 / 温度 / 累计能耗趋势曲线（SQLite 落盘，默认保留 30 天）。
- **设置页**：调整电价、查看每日明细、清除累计。

### 工程与运维
- 纯标准库 `http.server` 后端，仅依赖 `pynvml` / `psutil`，前端单文件无外部依赖。
- `watchdog.py` 看门狗：端口探测 + 主服务崩溃自动重启（自愈）。
- Windows 便捷脚本：启动 / 停止 / 状态 / 安装 LHM。
- 核心逻辑测试（`tests/test_core.py`，无需 GPU）。

### 开源合规
- MIT 许可证（LICENSE）。
- `.gitignore` 已忽略所有运行时/凭证文件（`auth.json`、`history.db`、`meter.json`、日志、`*.pid`、`*.lock` 等）。
- `.gitattributes` 强制 `*.bat` 以 CRLF 入库，避免 Windows 乱码。
- 本文档集：README、CONTRIBUTING、SECURITY、CODE_OF_CONDUCT、CHANGELOG、CI 工作流与 Issue/PR 模板。
