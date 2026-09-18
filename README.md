# 🎙️ Agent Live

> **专为 Cursor、Antigravity IDE 等现代 AI 编辑器量身打造的超低延迟实时双工语音顾问与伴飞副驾。**  
> Powered by **Gemini 3.8 Live API** + **Full-Duplex Multimodal Stream** + **Tactical HUD**.

---

## 🌟 核心特性与设计理念

在传统 AI 编程中，开发者通常在“不断打字描述需求”与“等待大模型输出”之间来回切换。  
**Agent Live** 彻底打破这种割裂，作为您常驻的“外置语音军师”与“敏捷执行工程师”：

1. **⚡ 端到端全双工实时语音**：基于 Google Gemini 3.8 Live 原生多模态 WebSocket 管道，毫秒级响应，支持随时插话打断（Barge-in）。原生配置 `zh-CN` 标准普通话（北方官话播音腔），字正腔圆，杜绝怪异方言与幻觉。
2. **🔘 全局呼叫热键 (Push-to-Talk, `Ctrl+Space`)**：系统级全局捕获。无论您在 Cursor 中浏览代码、在浏览器查阅文档，还是在调试终端，随手一按即可开麦对讲。
3. **📊 动态战况仪表盘 (Tactical HUD)**：直接内嵌在 IDE 底部终端，以丰富 ANSI 终端色彩实时可视化展现麦克风拾音能量条、当前对讲状态、实施工程师后台动作与实时伴随字幕。
4. **👁️ Cursor & Antigravity 双源实时感知总线**：同时并发监听 Antigravity 与 Cursor 本地 Agent 对话转录（`~/.cursor/projects/<slug>/agent-transcripts/*.jsonl`），智能识别用户提问、工具调用（`Read`/`Write`/`Grep`/`Shell` 等）与任务结束事件，并在后台通过防抖总线进行语音伴随解说与战报播报。
5. **🚦 单通道优先级排队与双 IDE 冲突仲裁**：内置 `SpeechCoordinator` 与跨实例租约锁（`InterProcessSpeechLease`），彻底解决 Antigravity 与 Cursor 同时发声踩音问题。所有语音汇报统一按军规级优先级（异常预警 > 派单回报 > 任务完工 > 过程解说）串行化排队；瞬态解说自动覆盖去重；跨工作区多实例自动互斥让行。
6. **🔇 系统媒体播放与会议声音智能避让**：内置 `AudioPlaybackDetector`，基于 macOS 核心音频机制（<12ms）精准感知 Apple Music、Spotify、Bilibili/YouTube 视频或腾讯会议、飞书、Zoom 等发声状态。当检测到长官正在播放媒体或开会通话时，**绝不冒然插话，自动在后台队列中安静排队**（HUD 动态提示 `[排队待播] 媒体播放中...`），待媒体停止且静音稳定后自动有序汇报。
7. **🛠️ 本地工程直连执行引擎 (Direct Engineering Engine)**：支持通过口头下发指令（如*“帮我检查一下当前分支状态”*、*“跑一下所有单元测试并汇报”*），内置引擎直接在当前工作区安全执行并语音向您汇报军规级战报。
8. **🛡️ 硬件回音抑制门限 (Echo Gate) 与双向隔离**：AI 开口回复瞬间自动毫秒级闭麦，彻底消灭外放扬声器引起的音频自激；平时保持静音，绝不干扰您的 Typeless 语音输入法或日常办公。

---

## 🖥️ 适用编程工具矩阵 (Supported IDEs & Editors)

| 编程工具 | 适配程度 | 使用方式与体验 |
| :--- | :---: | :--- |
| **Cursor** | **原生深度协同** | 直接常驻于 Cursor 底部 `Terminal` 面板；**内置 Cursor Transcript 实时监听引擎**，自动映射工程并追踪 Cursor Agent 交互过程与工具调用，提供语音伴随解说与任务完成口播。 |
| **Antigravity IDE** | **原生深度协同** | 完美联动；支持伴随解说总线、实时感知实施工程师工具调用轨迹并脱口播报战报。 |
| **VS Code / Windsurf** | **原生完美适配** | 完全兼容 VS Code 及其所有衍生产品；内置 `.vscode/tasks.json` 一键即开。 |
| **独立终端 (Terminal / iTerm / WezTerm / Ghostty)** | **系统级全支持** | 可以在任何终端作为独立的浮动桌面语音副驾使用，配合 JetBrains、Xcode、Neovim 等任意开发环境。 |

---

## 🚀 极速上手 (Quick Start)

### 1. 系统前置要求
- **操作系统**：macOS (Apple Silicon / Intel)，已在 macOS Sonoma (14) 与 Sequoia (15) 深度验证；
- **Python 环境**：Python 3.10+，强烈推荐安装轻量极速包管理器 [uv](https://github.com/astral-sh/uv)：
  ```bash
  # 安装 uv (推荐)
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # 或通过 Homebrew 安装: brew install uv
  ```
- **音频驱动依赖**（若系统缺少底层音频编译库）：
  ```bash
  brew install portaudio
  ```

---

### 2. 克隆与配置

```bash
# 1. 克隆项目
git clone https://github.com/janzlu/agent-live.git
cd agent-live

# 2. 复制配置文件模板
cp .env.example .env
```

编辑 `.env` 文件，填入您的 Gemini API 密钥：
```env
# 必填: 可从 Google AI Studio 免费获取: https://aistudio.google.com/app/api-keys
GEMINI_API_KEY=AIzaSyYourSecretKeyHere

# 选填: 实时多模态模型 (默认 Gemini 3.8 Live)
LIVE_MODEL_NAME=models/gemini-3.8-live

# 选填: 音色名称 (可选: Puck, Charon, Kore, Fenrir, Aoede)
VOICE_NAME=Puck

# 选填: 网络代理设置 (若国内网络直连 Google API 受限，配置本地代理端口，如 Clash 默认 7897)
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
```

---

### 3. 一键启动

在终端或 IDE 底部 Terminal 中运行一键启动脚本：
```bash
./start.sh
# 或显式指定所属 IDE 模式（默认 auto 自动探测当前终端所属 IDE，实现专属独立监听）:
# ./start.sh --ide cursor
# ./start.sh --ide antigravity
```
> **IDE 隔离与专属绑定**：系统会自动嗅探当前终端属于 **Cursor** 还是 **Antigravity IDE**，自动锁定当前 IDE 正在编辑的专属工程与对话转录，彻底杜绝跨 IDE 串音与重复感知。若长官在不同 IDE 中分别启动独立的 sidecar 终端实例，底层跨进程租约锁（`InterProcessSpeechLease`）会自动排队互斥，绝不抢麦踩音。

---

### 4. 首次运行 macOS 权限设置 (关键)

为保证系统的最佳体验，首次在 macOS 上运行时请确认以下两项系统权限：

1. **麦克风访问权限**：
   - 首次启动录音时，系统会弹出窗口申请麦克风权限，点击 **“允许 (Allow)”**。
2. **全局热键辅助功能权限 (Accessibility)**：
   - 为了让全局对讲呼叫快捷键（`Ctrl+Space`）在跨软件时生效：
   - 打开 **系统设置 (System Settings) -> 隐私与安全性 (Privacy & Security) -> 辅助功能 (Accessibility)**；
   - 确保勾选了您正在使用的终端或 IDE（例如：**Cursor**、**Antigravity IDE** 或 **终端/iTerm**）。

---

## 💡 日常使用指南 (Daily Workflow)

```
       [ 键盘快捷键: Ctrl + Space ]
                    │
                    ▼
     ┌──────────────────────────────┐
     │   🎙️ 开麦监听 (能量跳动)     │  ◄── 您说话: "帮我跑一下测试"
     └──────────────┬───────────────┘
                    │ 再次按下 Ctrl + Space (或自动静音检测)
                    ▼
     ┌──────────────────────────────┐
     │   ⚙️ 引擎思考 / 执行指令     │  ◄── 直连引擎本地执行 pytest / npm test
     └──────────────┬───────────────┘
                    │
                    ▼
     ┌──────────────────────────────┐
     │   🔊 实时语音脱口播报战报    │  ◄── "报告长官！测试全部通过！"
     │   (AI 回复时扬声器自动防自激) │
     └──────────────────────────────┘
```

### 1. 对讲机 (Push-to-Talk) 模式
- **默认待命**：终端指示为 `[🔇 对讲静音]`，完全静音保护，绝不干扰输入法或周围交谈；
- **开麦对话**：按下 **`Ctrl+Space`**（或在终端界面按 **空格/回车**），状态瞬间变为 `[🎙️ 麦克风已开启]`，拾音能量条开始跳动；
- **结束发言**：说完后再次按下 **`Ctrl+Space`**（或在终端按 **空格/回车**），麦克风自动关闭，AI 立即开始语音作答。

### 2. 强占打断 (Supreme Barge-in)
- 当 AI 正在滔滔不绝语音回复时，只要您按下 **`Ctrl+Space`** 开麦，系统会在 **< 1ms 瞬间切断 AI 播音并清空扬声器队列**，完全以您的声音为最高优先级！

### 3. 口头直发任务范例
您可以随时按热键对语音顾问说：
- *“帮我看一下当前 Git 的分支和修改状态”*
- *“帮我跑一下单元测试，看看有没有失败用例”*
- *“分析一下当前项目的目录架构与核心入口”*
- *“我想重构用户认证模块，给我讲讲业内推荐的最佳实践”*

直连工程引擎会直接在当前项目工作区调度执行，并在终端以字幕与语音同步向您汇报战果。

---

## 🛠️ 在 Cursor 与 Antigravity 中深度集成

### Cursor
1. 将本项目放入您的工具目录，或直接在项目工作区打开；
2. 仓库已包含预设的 `.vscode/tasks.json` 与 `.vscode/settings.json`；
3. 打开 Cursor 时，可在底部终端直接开启，或者在命令面板（`Cmd+Shift+P`）选择 `Tasks: Run Task` -> `Agent Live Voice Copilot`；
4. 战况动态仪表盘将稳固常驻在 Cursor 底端面板，与 Cursor AI 对话框完美共存。

### Antigravity IDE
- 支持通过 `.agents/hooks.json` 挂载 `PreInvocation` 钩子，每次在聊天框发起任务时，系统自动在后台自检并无感拉起语音副驾。

---

## 📂 项目结构说明

```text
agent-live/
├── .env.example              # 环境变量配置模版 (不含私密凭据)
├── .gitignore                # 严密过滤规则 (严格隔离 .env 与虚拟环境)
├── README.md                 # 完整实操与架构说明文档
├── requirements.txt          # Python 核心依赖清单
├── start.sh                  # 一键环境自检、依赖自愈与启动脚本 (自动感知当前 IDE 通道)
├── restart.sh                # 语音会话重启清理管理器 (一键全量或定向安全重启)
├── ensure_running.sh         # 跨 IDE 智能感知与看守器 (支持 --ide, --restart, --stop)
├── live_sidecar.py           # 核心引擎: Gemini 3.8 Live 双向多模态 WebSocket 管道、PTT调度与强占切断
├── speech_coordinator.py     # 单通道排队调度器 (优先级排序、瞬态解说自动去重与过时丢弃)
├── audio_monitor.py          # 系统媒体音频活动检测 (pmset/coreaudiod) 与跨进程播报互斥租约
├── ide_watcher.py            # IDE 动作感知总线 (支持 Antigravity 与 Cursor 双源对话转录监听、动作防抖聚合与字幕纯化)
├── antigravity_runner.py     # 本地工程直连执行引擎 (Direct Engine) 与自动化任务派单器
├── mcp_loader.py             # 标准 MCP 协议加载器与工具注册模块
├── .vscode/
│   ├── tasks.json            # VS Code / Cursor 开箱即用任务配置文件
│   └── settings.json         # 自动任务静默授权配置
├── test_speech_queue.py      # 单通道语音排队、优先级调度与媒体声音避让测试套件
├── test_cursor_transcript_watcher.py # Cursor 转录监听、动作抽取与任务完成口播测试套件
└── test_*.py                 # 针对强占打断、防抖聚合与执行引擎的单元与集成测试套件
```

---

## ❓ 常见问题排查 (FAQ)

<details>
<summary><b>1. 无法连接 Gemini Live API，提示连接超时或握手失败？</b></summary>
Google Gemini Live API 在部分地区存在网络限制。请在 <code>.env</code> 中正确配置您的本地代理地址与端口（例如 Clash 默认 <code>HTTP_PROXY=http://127.0.0.1:7897</code> 和 <code>HTTPS_PROXY=http://127.0.0.1:7897</code>）。
</details>

<details>
<summary><b>2. 按 <code>Ctrl+Space</code> 没有反应，或者无法开麦？</b></summary>
全局热键依赖 macOS 的系统辅助功能权限。请前往 <b>系统设置 -> 隐私与安全性 -> 辅助功能</b>，确认您运行脚本所使用的应用（Cursor / Antigravity IDE / 终端）已勾选授权。
</details>

<details>
<summary><b>3. 报错 <code>PortAudio</code> 或 <code>sounddevice</code> 找不到动态库？</b></summary>
在 macOS 上通过 Homebrew 安装底层音频库即可解决：
<pre><code>brew install portaudio</code></pre>
</details>

<details>
<summary><b>4. 团队协作时如何避免泄露 API Key？</b></summary>
本项目 <code>.gitignore</code> 已严密屏蔽 <code>.env</code>、<code>.venv</code>、<code>*.log</code>。团队成员请务必遵循：各自从 <code>.env.example</code> 复制出本地私有 <code>.env</code>，严禁将包含真实 API 密钥的文件提交至远程仓库。
</details>

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源发布。
欢迎提交 Issue 与 PR 共同完善多 IDE 实时语音结对生态！
