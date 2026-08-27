# 更新日志 (Changelog)

本项目遵循「功能累加」式记录（暂未采用严格 SemVer）。

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
