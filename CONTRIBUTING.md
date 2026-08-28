# 贡献指南 (Contributing)

感谢你考虑为 **GPU Monitor** 做出贡献！无论是报告 Bug、提出功能建议，还是提交代码，都欢迎。

---

## 开发环境搭建

要求 **Python ≥ 3.8**，并安装 NVIDIA 驱动（含 NVML，仅运行实时采集时需要）。

```bash
git clone https://github.com/Dongwu259/GPU_Scope.git
cd GPU_Scope
pip install -r requirements.txt
```

依赖仅两个：`nvidia-ml-py`（导入名 `pynvml`）、`psutil`。

> 离线/受限环境下，也可把这两个包放进同目录 `pylibs/`，程序会自动加入导入路径。

---

## 本地运行

```bash
# 命令行方式
python gpu_monitor.py --port 8080 --interval 0.5

# 或用 Windows 便捷脚本（后台无黑窗口）
start_gpu_monitor.bat      # 启动
status_gpu_monitor.bat     # 查看状态
stop_gpu_monitor.bat       # 优雅停止
```

启动后访问 **http://127.0.0.1:8080**。

---

## 运行测试

核心逻辑测试（Meter 累计 / History 落盘 / 节流解码）**无需 GPU**，纯标准库即可运行：

```bash
python tests/test_core.py
```

测试退出码非 0 表示有失败项。提交前请确保测试通过。

跨平台分支（Linux 的 `/proc/cpuinfo`、hwmon、RAPL、systemd 自启；Windows 自启启动器；
偏好校验；安全护栏）由第二套测试覆盖，**同样无需 GPU，且在 Windows 上也能跑**
（内部用模拟数据源）：

```bash
python tests/test_crossplatform.py
```

提交前两套测试都应通过。

也可使用 pytest（可选）：

```bash
pip install -e ".[dev]"
pytest
```

---

## 代码风格与约定

- **后端 `gpu_monitor.py`**：保持 *纯 Python 标准库* HTTP 服务（`http.server`），**不要引入 Flask / FastAPI 等重依赖**。
- **前端 `index.html`**：单文件、无构建步骤、无外部 CDN 依赖；图表用原生 Canvas 绘制。
- 缩进 4 空格，遵循 PEP 8；中文注释无限制，鼓励写清楚「为什么」。
- 涉及 Windows 性能计数器 / WMI / `subprocess` 调用时，注意无控制台弹窗（参考 `CREATE_NO_WINDOW` 用法）。
- `.bat` 文件保持 **纯 ASCII**（Windows cmd 默认 GBK 解析，UTF-8 中文会乱码）。
- **不要在 Git Bash / MSYS 里执行 `curl ... > nul`**：`nul` 是 Windows 的保留设备名，
  cmd 里代表"丢弃输出"，但 Git Bash 会把它当成**普通文件名**，在项目根目录留下一个
  叫 `nul` 的垃圾文件。它已被 `.gitignore` 忽略，不会入库；由于是保留设备名，常规
  `rm` / 资源管理器都删不掉，需要手动执行：

  ```bat
  del "\\?\C:\完整路径\GPUmonitor\nul"
  ```

  PowerShell 下等价写法：`Remove-Item -LiteralPath "\\?\C:\完整路径\GPUmonitor\nul"`。
  在脚本里想丢弃输出请用 `>NUL`（cmd）或 `>/dev/null`（Git Bash）。

---

## 提交规范

- 提交信息建议清晰描述「做了什么、为什么」：
  - `feat: 增加多路 CPU 卡片网格`
  - `fix: 修正实时 H100 等效算力双重系数`
  - `docs: 补充 CONTRIBUTING 与安全政策`
- 一个 PR 聚焦一件事，便于 review。
- 修改功能后请同步更新 README / 本文档 / 路线图。

---

## 报告问题 / 提交 PR

1. 先搜索 [Issues](https://github.com/Dongwu259/GPU_Scope/issues)，避免重复。
2. 使用模板提供：操作系统、Python 版本、GPU 型号、复现步骤、日志（`server_err.log` / `watchdog.log`，注意**不要包含 `auth.json`**）。
3. 提交 PR 时填写 PR 模板中的自查清单。

安全相关的问题**请勿公开 Issue**，请按 [SECURITY.md](SECURITY.md) 私有渠道报告。

---

## 目录结构速览

```
gpu_monitor.py        主服务: NVML 轮询线程 + 标准库 HTTP 服务
index.html            前端面板 (原生 Canvas 图表, 无外部依赖)
watchdog.py           看门狗: 端口探测 + 主服务崩溃自动重启
install_lhm.bat       下载 LibreHardwareMonitor (真实 CPU/内存温度功率)
*.bat / *.sh          启动/停止/状态 便捷脚本 (Windows / Linux+macOS)
tests/test_core.py           核心逻辑测试 (无需 GPU)
tests/test_crossplatform.py  跨平台与安全护栏测试 (模拟数据源, 无需 GPU)
```

## 端口约定

默认端口 **8080**。若被其他程序占用，主服务会**自动向上探测**（最多 +10）并把实际
端口写入 `monitor.port`；watchdog 与启停脚本都读这个文件，因此三者始终一致，不需要
手动改脚本。

想固定端口，设置环境变量 `GPU_MONITOR_PORT`（脚本与 watchdog 都会遵守）；它只在
`monitor.port` 不存在时生效 —— 服务一旦启动，实际端口以 `monitor.port` 为准。
