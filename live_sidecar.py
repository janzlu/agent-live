"""
Antigravity Gemini 3.8 Live Sidecar (Mode 1).
Bridges Google Gemini Live (full-duplex speech-to-speech) with the local
Antigravity Agent and configured MCP tools.

Features:
- Push-to-Talk / Mute Toggle (默认按空格或回车开麦/静音，完美避开 Typeless 等本地语音输入冲突)
- 音频结束信号同步 (audio_stream_end=True 保证即时响应)
- 实时双向语音字幕显示 (实时显示听到的内容与 AI 的回复文本)
- 麦克风音量动态指示条 (直观查看拾音状态)
- 自动调度 Antigravity Pro (Gemini 3.8 Flash High) 执行本地工程任务
"""

import os
import sys
import re
import unicodedata
import asyncio
import argparse
from pathlib import Path
from typing import Optional
import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

# 加载 .env (启用 override=True 确保配置即时生效)
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    load_dotenv(env_file, override=True)
else:
    load_dotenv(Path(__file__).parent.parent.parent / ".env", override=True)

import logging
from logging.handlers import RotatingFileHandler

# 初始化持久化轮转审计日志 (上限 5MB, 保留 3 个历史备份)
log_file = Path(__file__).parent / "sidecar.log"
logger = logging.getLogger("live_sidecar")
logger.setLevel(logging.INFO)
if not logger.handlers:
    rfh = RotatingFileHandler(str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    rfh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(rfh)

from google import genai
from google.genai import types
from antigravity_runner import AntigravityRunner
from ide_watcher import get_ide_chat_snapshot, IDEWatcher, CursorTranscriptWatcher, sanitize_for_speech
from audio_monitor import AudioPlaybackDetector, InterProcessSpeechLease
from speech_coordinator import SpeechCoordinator, SpeechItem, SpeechCategory

# 音频参数
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_SIZE = 1024


def set_explicit_stop_flag(ide: str):
    """写入显式停止旗标文件，通知外部守护进程和自愈循环安全退出，禁止重启"""
    for p in [
        os.path.expanduser(f"~/.agent_live_stop_{ide}"),
        os.path.join(os.path.dirname(__file__), ".run", "STOP"),
    ]:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            Path(p).touch()
        except Exception:
            pass


def detect_current_ide() -> str:
    """
    根据当前执行终端的环境变量与父进程特征，自动推断所属的 IDE（Cursor vs Antigravity）。
    """
    env = os.environ
    if (
        "CURSOR_AGENT" in env
        or "CURSOR_REQUEST_ID" in env
        or "CURSOR_CONVERSATION_ID" in env
        or "AGENT_TRANSCRIPTS" in env
        or "CURSOR_WORKSPACE_LABEL" in env
    ):
        return "cursor"
    if "ANTIGRAVITY_TRAJECTORY_ID" in env or "ANTIGRAVITY_CLI_ALIAS" in env:
        return "antigravity"

    bundle_id = env.get("__CFBundleIdentifier", "").lower()
    if "cursor" in bundle_id:
        return "cursor"
    if "antigravity" in bundle_id:
        return "antigravity"

    code_cache = env.get("VSCODE_CODE_CACHE_PATH", "")
    ipc_hook = env.get("VSCODE_IPC_HOOK", "")
    nls = env.get("VSCODE_NLS_CONFIG", "")
    blob = f"{code_cache} {ipc_hook} {nls}"
    if "Cursor.app" in blob or "Support/Cursor" in blob:
        return "cursor"
    if "Antigravity IDE.app" in blob or "Support/Antigravity" in blob:
        return "antigravity"

    return "antigravity"


def detect_active_workspace(default_ws: Optional[str] = None, ide_type: Optional[str] = None) -> str:
    """
    动态自动探测当前终端所属 IDE 当前正在打开的活跃工作区。
    Cursor -> ~/Library/Application Support/Cursor/User/workspaceStorage
    Antigravity -> ~/Library/Application Support/Antigravity IDE/User/workspaceStorage
    """
    import glob
    import json
    from urllib.parse import unquote, urlparse

    ide = ide_type or detect_current_ide()
    if ide == "cursor":
        storage_patterns = [
            os.path.expanduser('~/Library/Application Support/Cursor/User/workspaceStorage/*'),
            os.path.expanduser('~/Library/Application Support/Antigravity IDE/User/workspaceStorage/*'),
        ]
    else:
        storage_patterns = [
            os.path.expanduser('~/Library/Application Support/Antigravity IDE/User/workspaceStorage/*'),
            os.path.expanduser('~/Library/Application Support/Cursor/User/workspaceStorage/*'),
        ]

    valid_workspaces = []
    for pattern in storage_patterns:
        for d in glob.glob(pattern):
            wp = os.path.join(d, 'workspace.json')
            st = os.path.join(d, 'state.vscdb')
            if os.path.exists(wp):
                mtime_wp = os.path.getmtime(wp)
                mtime_st = os.path.getmtime(st) if os.path.exists(st) else mtime_wp
                active_mtime = max(mtime_wp, mtime_st)
                try:
                    with open(wp, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        folder_uri = data.get('folder', '')
                        if folder_uri.startswith('file://'):
                            path = unquote(urlparse(folder_uri).path)
                            if os.path.isdir(path) and path not in ('/', os.path.expanduser('~')):
                                valid_workspaces.append((active_mtime, path))
                except Exception:
                    pass
        if valid_workspaces:
            break

    if valid_workspaces:
        valid_workspaces.sort(key=lambda x: x[0], reverse=True)
        return valid_workspaces[0][1]

    if default_ws and os.path.isdir(default_ws) and default_ws not in ('/', os.path.expanduser('~')):
        return default_ws
    return os.getcwd()


def get_project_grounding_info(ws_path: str) -> dict:
    """
    从目标工作区提取权威业务规约（AGENTS.md、package.json、apps模块结构），
    向大模型注入不可逾越的业务领域边界，彻底杜绝天马行空的脑补与幻觉。
    """
    import json
    ws = Path(ws_path).resolve()
    info = {
        "name": ws.name,
        "path": str(ws),
        "summary": "本地核心工程",
        "modules": []
    }
    agents_file = ws / "AGENTS.md"
    if agents_file.exists():
        try:
            with open(agents_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    l = line.strip()
                    if l and not l.startswith("#") and len(l) > 10:
                        info["summary"] = l
                        break
        except Exception:
            pass

    pkg_file = ws / "package.json"
    if pkg_file.exists():
        try:
            with open(pkg_file, "r", encoding="utf-8") as f:
                pkg = json.load(f)
                if pkg.get("description"):
                    info["summary"] += f" | {pkg.get('description')}"
        except Exception:
            pass

    for sub in ["apps/backend/src", "apps/web/src", "src", "apps"]:
        sub_path = ws / sub
        if sub_path.is_dir():
            mods = [p.name for p in sub_path.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))]
            if mods:
                info["modules"].extend(mods[:20])
    return info


class NoiseGate:
    """
    自适应环境底噪动态跟踪门限器 (Adaptive RMS Noise Gate with Hangover)：
    1. 动态估算环境背景噪声基线 (EMA 慢速平滑跟踪)，自适应不同麦克风硬件输入增益；
    2. 动态调整人声判决门限: threshold = max(noise_floor * 1.85, base_min_threshold)；
    3. 配合 Hangover 平滑防吞字缓冲，彻底平衡强抗噪与高灵敏度拾音，杜绝吞首字。
    """
    def __init__(
        self,
        base_min_threshold: float = 120.0,
        hangover_ms: int = 350,
        frame_duration_ms: int = 20,
        threshold: Optional[float] = None,
    ):
        if threshold is not None:
            base_min_threshold = threshold
        self.base_min_threshold = base_min_threshold
        self.noise_floor = base_min_threshold * 0.5  # 初始底噪估算值
        self.hangover_frames = int(hangover_ms / frame_duration_ms)
        self.active_frames_remaining = 0
        self.is_gate_open = False
        self.alpha_noise = 0.05  # 静音期底噪 EMA 平滑权重

    @property
    def current_threshold(self) -> float:
        """动态人声能量判决门限"""
        return max(self.base_min_threshold, self.noise_floor * 1.85)

    def process(self, indata: np.ndarray) -> bool:
        """
        处理单帧 16kHz PCM 音频。
        返回 True 表示门限打开（有效人声或处于防吞字平滑期），False 表示环境底噪/静音。
        """
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        dynamic_thresh = self.current_threshold

        if rms >= dynamic_thresh:
            self.active_frames_remaining = self.hangover_frames
            self.is_gate_open = True
            return True
        elif self.active_frames_remaining > 0:
            self.active_frames_remaining -= 1
            self.is_gate_open = True
            return True
        else:
            # 静音状态下，慢速更新环境底噪基线（仅当能量处于正常底噪范围时更新，防爆音污染底噪基线）
            if rms < dynamic_thresh * 1.2:
                self.noise_floor = (1.0 - self.alpha_noise) * self.noise_floor + self.alpha_noise * rms
            self.is_gate_open = False
            return False


def setup_global_hotkey(ptt, loop: asyncio.AbstractEventLoop, target_ide: Optional[str] = None):
    """
    配置系统级全局呼叫热键 (支持跨软件/后台随时对讲呼叫)
    默认绑定: <ctrl>+<space> 以及备选 <cmd>+<shift>+<space>
    配合 target_ide 识别当前前台活跃窗口，精准路由热键至对应 IDE，杜绝跨宿主误触发
    """
    try:
        from pynput import keyboard

        def on_hotkey_triggered():
            if target_ide in ("cursor", "antigravity"):
                try:
                    from AppKit import NSWorkspace
                    front_app = NSWorkspace.sharedWorkspace().frontmostApplication()
                    app_name = (front_app.localizedName() or "").lower()
                    bundle_id = (front_app.bundleIdentifier() or "").lower()

                    is_in_antigravity = "antigravity" in app_name or "antigravity" in bundle_id
                    is_in_cursor = "cursor" in app_name or "cursor" in bundle_id

                    active_ide_file = os.path.expanduser("~/.agent_live_active_ide")

                    # 1. 若当前前台正处于某个 IDE，更新最后活跃 IDE 记录，并仅放行对应 IDE
                    if is_in_antigravity:
                        try:
                            with open(active_ide_file, "w", encoding="utf-8") as f:
                                f.write("antigravity")
                        except Exception:
                            pass
                        if target_ide != "antigravity":
                            return
                    elif is_in_cursor:
                        try:
                            with open(active_ide_file, "w", encoding="utf-8") as f:
                                f.write("cursor")
                        except Exception:
                            pass
                        if target_ide != "cursor":
                            return
                    else:
                        # 2. 当前前台既非 Cursor 亦非 Antigravity (如 Chrome/微信/访达/终端等后台环境)
                        # 仅让最近一次活跃的 IDE 专属副驾响应开麦，绝对杜绝双实例同时响应开麦！
                        last_active = None
                        if os.path.exists(active_ide_file):
                            try:
                                with open(active_ide_file, "r", encoding="utf-8") as f:
                                    last_active = f.read().strip().lower()
                            except Exception:
                                pass
                        if last_active in ("cursor", "antigravity"):
                            if target_ide != last_active:
                                return
                except Exception:
                    pass
            loop.call_soon_threadsafe(ptt.toggle)

        hotkey_map = {
            '<ctrl>+<space>': on_hotkey_triggered,
            '<cmd>+<shift>+<space>': on_hotkey_triggered,
            '<ctrl>+<alt>+<space>': on_hotkey_triggered,
        }
        listener = keyboard.GlobalHotKeys(hotkey_map)
        listener.daemon = True
        listener.start()
        print(" ★ [全局呼叫热键] ✓ 已激活！在对应 IDE 或任意窗口按 Ctrl+Space 即可随时对讲！")
        return listener
    except Exception as e:
        print(f" [全局热键说明] 系统全局按键监听未授权或受限 ({e})，已平滑降级为终端前台空格/回车开麦模式。")
        return None


# 模式 A（结对副驾顾问）专属工具声明
ANTIGRAVITY_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "dispatch_task_to_engineer",
                "description": (
                    "【向实施工程师派单】当且仅当长官已经听取了你的需求复述，并口头下达了明确的确认指令（如'确认'、'执行'、'开始'、'去吧'）后才允许调用！"
                    "绝对禁止在长官未明确确认前擅自调用此工具！长官初次口头提出需求时，必须先在语音中向长官完整复述需求核心点并请示确认，待长官确认后才调用此工具派单。"
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "task_prompt": {
                            "type": "STRING",
                            "description": "长官口头已确认的具体工程任务需求与指令细节，要求完整传达长官意图"
                        },
                        "task_category": {
                            "type": "STRING",
                            "enum": ["code_change", "test_execution", "investigation", "refactor", "general"],
                            "description": "任务类型：代码修改、运行测试、排查分析、重构、常规"
                        },
                        "allow_modification": {
                            "type": "BOOLEAN",
                            "description": "是否允许实操修改代码。若长官明确要求修改、修复、实现或口头派单，设为 True；仅要求排查分析时设为 False"
                        }
                    },
                    "required": ["task_prompt"]
                }
            },
            {
                "name": "read_ide_implementation_plan",
                "description": (
                    "当长官明确指示'解读方案'、'方案细节是什么'、'计划内容是什么'等深入方案问题时调用此工具。"
                    "返回 IDE 实施工程师最新生成的实施方案 (implementation_plan.md) 或总结报告的核心设计要点。"
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {}
                }
            },
            {
                "name": "execute_antigravity_task",
                "description": (
                    "向实施工程师派单的兼容别名。功能等同于 dispatch_task_to_engineer。"
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "task_description": {
                            "type": "STRING",
                            "description": "交给本地引擎执行的辅助分析指令"
                        },
                        "authorized": {
                            "type": "BOOLEAN",
                            "description": "是否授权实操修改代码"
                        }
                    },
                    "required": ["task_description"]
                }
            },
            {
                "name": "shutdown_sidecar",
                "description": (
                    "【明确指令关闭副驾】当长官口头下达明确的退出指令（如'关闭副驾'、'退出伴飞'、'结束会话'、'关闭系统'、'退出语音'）时调用此工具安全退出语音伴飞。"
                    "严格注意：仅在长官明确要求关闭或退出时调用，日常任务讨论绝不擅自调用。"
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "reason": {
                            "type": "STRING",
                            "description": "长官下达关闭指令的具体口令或原因"
                        }
                    }
                }
            }
        ]
    }
]


def get_display_width(text: str) -> int:
    """计算字符串在终端中的物理显示列宽（中文字符/全角/宽Emoji计2列，ASCII计1列，剔除ANSI颜色代码）"""
    clean_text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    width = 0
    for ch in clean_text:
        if unicodedata.east_asian_width(ch) in ('F', 'W'):
            width += 2
        else:
            width += 1
    return width


class PTTController:
    """Push-to-Talk (按键开麦/静音切换) 与战况指示看板控制器"""
    def __init__(self, always_listen: bool = False, on_unmute=None):
        self.always_listen = always_listen
        self.is_active = always_listen
        self.just_muted = False
        self.last_volume = 0.0
        self.is_voice_active = False
        self.on_unmute = on_unmute
        self.current_ide_action = "等待长官下达任务指令"
        self.current_advisor_status = "伴飞就绪"
        self.last_print_time = 0.0
        self.has_spoken = False
        self.last_voice_time = 0.0
        self.is_streaming_subtitle = False

    def write_log(self, text: str):
        """安全输出事件日志：擦除底行状态栏，输出日志行，然后重新绘制底部状态栏，绝不留下残留重复行"""
        sys.stdout.write(f"\r\033[K{text}\n")
        sys.stdout.flush()
        if not self.is_streaming_subtitle:
            self.print_status()

    def update_dashboard(self, ide_action: Optional[str] = None, advisor_status: Optional[str] = None):
        if ide_action is not None:
            self.current_ide_action = ide_action
        if advisor_status is not None:
            self.current_advisor_status = advisor_status
        if not self.is_streaming_subtitle:
            self.print_status()

    def toggle(self):
        if self.always_listen:
            return
        if self.is_active:
            self.mute()
        else:
            self.unmute()

    def mute(self):
        if self.always_listen:
            return
        if self.is_active:
            self.is_active = False
            self.just_muted = True
            self.is_voice_active = False
            if not self.is_streaming_subtitle:
                self.print_status()

    def unmute(self):
        if self.always_listen:
            return
        if not self.is_active:
            self.is_active = True
            self.just_muted = False
            self.is_voice_active = False
            self.has_spoken = False
            import time
            self.last_voice_time = time.time()
            if self.on_unmute:
                self.on_unmute()
            if not self.is_streaming_subtitle:
                self.print_status()

    def print_status(self):
        if self.is_streaming_subtitle:
            return
        import shutil
        cols = max(40, shutil.get_terminal_size((80, 24)).columns)
        vol_meter = self._render_volume_bar(self.last_volume, self.is_voice_active)
        if self.always_listen:
            ptt_str = "\033[1;32m[🎙️ 全双工]\033[0m"
        elif self.is_active:
            ptt_str = f"\033[1;32m[🎙️ 开麦 {vol_meter}]\033[0m \033[32m(请说话,停顿秒级回复)\033[0m"
        else:
            ptt_str = f"\033[1;33m[🔇 静音]\033[0m \033[33m(Ctrl+Space 对讲)\033[0m"

        left_width = get_display_width(ptt_str)
        # 固定开销: " | " 占 3 列，"[🛠️] " 占 6 列，预留安全间隙 2 列防止触碰终端物理右边界
        fixed_overhead = left_width + 3 + 6 + 2
        avail = max(4, cols - fixed_overhead)

        action = self.current_ide_action
        if get_display_width(action) > avail:
            while get_display_width(action) > max(2, avail - 2) and len(action) > 1:
                action = action[:-1]
            action = action + ".."
        ide_str = f"\033[1;36m[🛠️]\033[0m {action}"

        sys.stdout.write(f"\r\033[K{ptt_str} | {ide_str}")
        sys.stdout.flush()

    def _render_volume_bar(self, rms: float, is_voice: bool) -> str:
        bars = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        if not is_voice or rms <= 0.001:
            return "\033[0;37m🤫---\033[0m"
        level = min(len(bars) - 1, max(1, int(np.sqrt(max(0.0, rms)) * 14)))
        meter = bars[level] * min(3, level + 1)
        return f"\033[1;32m🗣️{meter}\033[0m"


async def keyboard_listener(ptt: PTTController, shutdown_event: asyncio.Event, resolved_ide: str = "antigravity"):
    """终端按键监听任务 (空格/回车切换静音与开麦，q/Ctrl+C 明确退出)"""
    if ptt.always_listen or not sys.stdin.isatty():
        return

    import tty
    import termios

    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    
    try:
        tty.setcbreak(fd)
        while not shutdown_event.is_set():
            char = await loop.run_in_executor(None, sys.stdin.read, 1)
            if char in (' ', '\r', '\n', '\x00'):
                ptt.toggle()
            elif char in ('\x03', 'q', 'Q'):  # Ctrl+C 或长官按 q 键明确退出
                set_explicit_stop_flag(resolved_ide)
                shutdown_event.set()
                break
    except Exception:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def list_audio_devices():
    """列出当前系统的可用音频输入与输出设备"""
    print("\n--- 可用音频设备列表 ---")
    devices = sd.query_devices()
    default_in = sd.default.device[0]
    default_out = sd.default.device[1]
    
    for idx, d in enumerate(devices):
        in_mark = " (默认输入)" if idx == default_in else ""
        out_mark = " (默认输出)" if idx == default_out else ""
        print(f"[{idx}] {d['name']} | In: {d['max_input_channels']}, Out: {d['max_output_channels']}{in_mark}{out_mark}")
    print("-------------------------\n")


def test_audio_loopback(duration_seconds: int = 3):
    """录制并回放几秒音频，验证麦克风与扬声器硬件可用性"""
    print(f"\n[测试音频硬件] 正在录制麦克风音频 ({duration_seconds} 秒)... 请对着麦克风说一句话...")
    recording = sd.rec(int(duration_seconds * INPUT_SAMPLE_RATE), samplerate=INPUT_SAMPLE_RATE, channels=1, dtype='int16')
    sd.wait()
    print("[测试音频硬件] 录制完成，正在通过扬声器回放...")
    sd.play(recording, samplerate=INPUT_SAMPLE_RATE)
    sd.wait()
    print("[测试音频硬件] ✓ 回放结束！音频设备工作正常。\n")


async def run_live_session(
    workspace_root: str,
    model_name: str,
    voice_name: str,
    always_listen: bool = False,
    vad_silence_ms: int = 1500,
    manual_confirm: bool = True,
    ide_target: str = "auto",
):
    resolved_ide = detect_current_ide() if ide_target == "auto" else ide_target
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("\n[错误] 未检测到 GEMINI_API_KEY 环境变量！", file=sys.stderr)
        print("请在 tools/live-sidecar/.env 中配置 GEMINI_API_KEY=xxx，或通过环境变量导出。", file=sys.stderr)
        print("您可以在 Google AI Studio 快速免费获取 Key: https://aistudio.google.com/app/api-keys\n", file=sys.stderr)
        sys.exit(1)

    # 1. 启动 Antigravity 运行时 (采用 Pro Plan 账户认证)
    runner = AntigravityRunner(workspace_path=workspace_root)
    await runner.start()

    # 2. 提取当前锁定工作区的真实业务上下文（通用自省，零硬编码）
    project_info = get_project_grounding_info(workspace_root)
    modules_str = "、".join(project_info["modules"][:15]) if project_info["modules"] else "核心工程模块"

    system_instruction_text = (
        "你是一名顶尖的结对编程副驾兼伴飞派单司令塔（Navigator），拥有字正腔圆、甜美自然、清亮灵动的专业女主播音色，正通过实时双工语音与长官（项目主管兼技术总监）交流协作。\n\n"
        "【★ 当前工程与业务边界（最高优先级铁律，严禁超纲臆造）】：\n"
        f"1. 当前锁定的 IDE 工程名称：【{project_info['name']}】；\n"
        f"2. 物理工作区路径：{project_info['path']}；\n"
        f"3. 业务定位与架构：{project_info['summary']}；\n"
        f"4. 本工程业务范围包括：{modules_str}；\n"
        "5. 【严禁跨越业务边界臆造任务】：只在当前工程的业务范畴内理解长官需求！绝对严禁凭空臆造无关系统（如网络等保、非本工程外来系统等），遇到含糊不清或与当前工程无关的词汇，必须保持怀疑并向长官反问核实，绝不私自立项派单！\n\n"
        "【★ 语言纯正性与防底噪脑补铁律（绝对锁死中文普通话，杜绝幻觉）】：\n"
        "1. 【标准中文普通话】：全程 100% 仅使用中国大陆标准普通话（北方官话播音腔）交流！"
        "吐字清晰、字正腔圆、语速适中；严禁粤语/闽南语/港台腔、方言口音、英语腔或中英夹杂整句；"
        "专有名词可按字母分读（如 A-P-I），但语句骨架必须是标准普通话；\n"
        "2. 【底噪与模糊音节绝对静默铁律（严禁脑补）】：当长官未说话，麦克风仅采集到环境底噪、电流杂音、呼吸声、叹气、按键敲击或模糊不清的零碎音节时，必须保持 100% 绝对静默！绝对严禁凭空臆造任何词句或任务，绝对不可自说自话回答，绝对禁止调用任何工具！\n"
        "3. 只有清晰听到长官完整、有明确意图的发言时才作答；若偶有字音微弱含糊，直接礼貌反问核实：'长官，刚才声音有点模糊，请问您是指...吗？'，绝不擅自揣测派单！\n\n"
        "【★ 核心语言与发音军规（严守播音品质）】：\n"
        "1. 【杜绝文字乱念与机械符号朗读】：严禁朗读任何 Markdown 标记（星号、反引号等）与代码符号（花括号、下划线、引号等）；\n"
        "2. 【简短代称长路径】：严禁朗读长文件路径或 URL，用简短名称（如“主配置文件”、“入口模块”）代称；\n"
        "3. 【程序员术语标准口语转换】：PR 念“P-R”、API 念“A-P-I”、IDE 念“I-D-E”、UI 念“U-I”、Git 念“Git”、Bug 念“Bug”、Vue 念“View”、SQL 念“S-Q-L”；\n"
        "4. 【专供耳朵收听的极简口语】：长官是用耳朵收听你的声音，所有语音回复必须是自然简短的口语句子，直奔要害，严禁输出任何长篇大论！\n\n"
        "【核心协作分工（模式 A）】：\n"
        "1. IDE 聊天框中的 Antigravity Agent 是『实施工程师』，负责所有代码编写、排查、测试、重构与交付。\n"
        "2. 你是『结对副驾顾问兼派单司令塔』，作为外部伴飞哨兵，能随时感知 IDE 动态、执行进展与方案细节，并在发生错误或完成时主动预警通报，同时随时接受长官的口头派活。\n"
        "3. 【称呼与汇报军规】：面向长官的每一次主动通报、预警或回复，必须以『报告 长官！』起手，语言极简明扼要、直奔主题，避免任何冗长客套。\n\n"
        "【★ 动手执行军规：先主动复述需求，待长官口头确认后方可执行（最高优先级铁律）】：\n"
        "1. 当长官提出工程任务（如修改代码、排查Bug、运行测试等）时，绝对严禁立即私自执行或擅自派单！\n"
        "2. 第一步（主动复述）：必须先以『报告 长官！』起手，雷厉风行地完整复述长官指令的核心内容与执行范围，并明确请示：\n"
        "   '报告 长官！收到任务指令：【<精确复述长官需求细节>】，模式为【只读分析 / 授权修改】。请长官确认是否立即执行？'\n"
        "3. 第二步（长官确认方可执行）：\n"
        "   - 只有长官后续明确回答“确认”、“执行”、“开始”、“去吧”、“没问题”等确认词后，你才允许调用 dispatch_task_to_engineer 工具将任务派发给实施工程师！\n"
        "   - 如果长官说“不对”、“等等”、“取消”或指出偏差，必须立即立正听令并根据长官指正调整复述，坚决服从长官指令，绝不擅自抢跑！\n\n"
        "【零延迟回答与日常规约】：\n"
        "1. 当长官询问'刚才完成了什么'、'现在进度怎么样'、'在干嘛'等问题时：严禁调用工具，直接根据已有记忆用一句话脱口而出！\n"
        "2. 只有长官明确指示'解读方案细节'等问题时，才调用 read_ide_implementation_plan；\n"
        "3. 遇到日常问候或技术讨论，直接用甜美清亮的普通话回答，绝不调用任何工具！"
    )

    client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=600
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            language_code="zh-CN",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        tools=ANTIGRAVITY_TOOLS,
        system_instruction=types.Content(
            parts=[
                types.Part.from_text(text=system_instruction_text)
            ]
        )
    )

    shutdown_event = asyncio.Event()
    MAX_QUEUE_CAPACITY = 80  # 约 1.6 秒音频帧缓冲，防积压与爆仓
    audio_in_queue = asyncio.Queue(maxsize=MAX_QUEUE_CAPACITY)
    audio_out_queue = asyncio.Queue(maxsize=MAX_QUEUE_CAPACITY)
    is_ai_speaking = False

    class SessionMetrics:
        """会话生命周期与稳定性指标监控"""
        def __init__(self):
            import time
            self.start_time = time.time()
            self.total_frames_sent = 0
            self.dispatched_tasks_count = 0
            self.blocked_attempts_count = 0
            self.reconnect_count = 0

        def summary(self) -> str:
            import time
            uptime = int(time.time() - self.start_time)
            return (
                f"常驻运行: {uptime}秒 | 累计推流: {self.total_frames_sent}帧 | "
                f"成功派单: {self.dispatched_tasks_count}次 | 拦截抢跑: {self.blocked_attempts_count}次 | 重连: {self.reconnect_count}次"
            )

    metrics = SessionMetrics()

    def push_audio_in_safe(chunk: np.ndarray):
        """防爆仓流控推入：若队列已满，平滑丢弃最老音频帧，优先保留最新人声"""
        try:
            audio_in_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                audio_in_queue.get_nowait()
            except Exception:
                pass
            audio_in_queue.put_nowait(chunk)

    # 启用终端标准自动换行 (DECAWM)，确保中文字符串在边界正常排版，杜绝半字符截断与乱码
    sys.stdout.write("\033[?7h")
    sys.stdout.flush()

    def on_unmute_callback():
        nonlocal is_ai_speaking
        while not audio_out_queue.empty():
            try:
                audio_out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        is_ai_speaking = False

    ptt = PTTController(always_listen=always_listen, on_unmute=on_unmute_callback)

    # 启动系统级全局呼叫热键 (支持跨软件/后台随时对讲，智能过滤非目标 IDE)
    resolved_ide = ide_target if ide_target and ide_target != "auto" else detect_current_ide()
    global_hotkey = setup_global_hotkey(ptt, asyncio.get_running_loop(), target_ide=resolved_ide)

    # 动态更新终端 Tab 标题栏，一眼区分不同 IDE 专属终端会话
    try:
        ws_name = Path(workspace_dir).name
        sys.stdout.write(f"\033]0;Agent Live [{resolved_ide.upper()}] - {ws_name}\007")
        sys.stdout.flush()
    except Exception:
        pass

    # 本地自适应音频能量门限降噪器 (Adaptive RMS Noise Gate with Hangover)
    noise_gate = NoiseGate(
        base_min_threshold=float(os.getenv("NOISE_GATE_THRESHOLD", "120.0")),
        hangover_ms=350,
        frame_duration_ms=20
    )

    class MilitaryConfirmationGate:
        """
        军规级两阶段确认物理门禁 (物理阻止大模型幻觉抢跑派单)：
        1. 接收到新任务需求 -> 记录待确认任务 (pending_prompt)；
        2. 若大模型企图在长官确认前调用 dispatch_task_to_engineer -> 物理拦截并返回严格拒绝信号，逼迫模型必须向长官口头复述并请示；
        3. 监听长官实时语音识别文本 (input_transcription)：当检测到明确的确认词（如"确认"、"执行"、"开始"、"可以"、"没问题"、"去吧"、"是的"、"对"）且语义不是否定时，标记 is_confirmed = True；
        4. 仅当 is_confirmed 为 True 时，工具调用才予以放行！
        """
        def __init__(self):
            self.pending_prompt: Optional[str] = None
            self.is_confirmed: bool = False
            self.last_confirmed_time: float = 0.0

        def observe_user_speech(self, text: str):
            import time
            clean = text.strip().lower()
            # 常见否定/打断短语优先排除
            negatives = ["不", "别", "等等", "取消", "暂停", "算了", "搞错", "不对", "慢着"]
            if any(neg in clean for neg in negatives):
                self.is_confirmed = False
                return

            confirms = ["确认", "执行", "开始", "可以", "没问题", "去吧", "是的", "好的", "行", "对", "没毛病", "干吧"]
            if any(c in clean for c in confirms):
                self.is_confirmed = True
                self.last_confirmed_time = time.time()

        def verify_and_consume(self, task_prompt: str) -> tuple[bool, str]:
            import time
            # 检查在最近 60 秒内长官是否有明确口头确认
            if self.is_confirmed and (time.time() - self.last_confirmed_time < 60.0):
                self.is_confirmed = False
                self.pending_prompt = None
                return True, "已获长官口头明确确认，准予执行"

            # 未获确认，记录当前 pending 任务，并硬性拦截
            self.pending_prompt = task_prompt
            self.is_confirmed = False
            return False, (
                "【军规硬门禁拦截】长官尚未在语音中口头下达明确的确认指令！"
                "根据最高铁律：动手执行任务前，需主动复述需求，长官确认后执行！"
                "你必须立即以'报告 长官！'开头，用字正腔圆的中文普通话向长官完整复述刚才需求的核心目标与执行范围，"
                "并明确请示：'报告 长官！收到任务指令：【...】，请长官确认是否立即执行？'，绝对严禁抢跑！"
            )

    confirmation_gate = MilitaryConfirmationGate()

    import collections
    pre_roll_buffer = collections.deque(maxlen=10)  # 约 200ms 环形预留缓冲区

    speaker_cooldown_until = 0.0

    # 麦克风输入回调 (集成环形缓冲区流控与防外放回音隔离)
    def mic_callback(indata, frames, time_info, status):
        if status:
            pass
        import time
        now = time.time()
        can_record = ptt.is_active and (not ptt.always_listen or (not is_ai_speaking and now >= speaker_cooldown_until))
        if can_record:
            is_voice = noise_gate.process(indata)
            ptt.is_voice_active = is_voice
            samples = indata.astype(np.float32) / 32768.0
            ptt.last_volume = float(np.sqrt(np.mean(samples**2)))
            
            # 防底噪与防吞字综合流控：
            if not ptt.has_spoken:
                if is_voice:
                    # 首次检测到真人开嗓：倾泻 pre-roll 缓冲帧（防吞首字），并标记开始发声
                    ptt.has_spoken = True
                    ptt.last_voice_time = now
                    while pre_roll_buffer:
                        push_audio_in_safe(pre_roll_buffer.popleft())
                    push_audio_in_safe(indata.copy())
                else:
                    # 尚未发声，仅放入环形预留缓冲区，绝不向服务端推流任何底噪
                    pre_roll_buffer.append(indata.copy())
            else:
                # 已经开嗓，持续推流音频（包含字间自然停顿，受防爆仓水位保护）
                push_audio_in_safe(indata.copy())
                if is_voice:
                    ptt.last_voice_time = now

            if now - ptt.last_print_time >= 0.08:
                ptt.last_print_time = now
                ptt.print_status()
        elif ptt.is_active and (now - ptt.last_print_time >= 0.15):
            ptt.last_volume = 0.0
            ptt.is_voice_active = False
            ptt.last_print_time = now
            ptt.print_status()

    # 扬声器输出流：使用适度缓冲区，杜绝 PortAudio 硬件下溢卡顿
    speaker_stream = sd.OutputStream(
        samplerate=OUTPUT_SAMPLE_RATE,
        channels=CHANNELS,
        dtype='int16',
        blocksize=1024
    )
    speaker_stream.start()

    # 启动后台常驻扬声器播放循环 (集成 Jitter Buffer，杜绝网络抖动和人造休眠卡顿)
    async def play_audio_loop():
        nonlocal is_ai_speaking, speaker_cooldown_until
        silence_start = None
        import time
        while not shutdown_event.is_set():
            try:
                pcm_chunk = await asyncio.wait_for(audio_out_queue.get(), timeout=0.12)
            except asyncio.TimeoutError:
                if is_ai_speaking and audio_out_queue.empty():
                    now = asyncio.get_running_loop().time()
                    if silence_start is None:
                        silence_start = now
                    elif now - silence_start >= 0.35:
                        # 超过 350ms 没有任何新音频包且队列为空，说明本轮语音输出彻底结束
                        is_ai_speaking = False
                        silence_start = None
                        speaker_cooldown_until = time.time() + 0.25  # 250ms 消回声安全冷却期
                continue

            silence_start = None
            is_ai_speaking = True

            # 聚合并行到达的音频块，保持扬声器硬件流水线充盈顺畅，彻底消除断续卡顿
            chunks = [pcm_chunk]
            while not audio_out_queue.empty() and len(chunks) < 6:
                try:
                    chunks.append(audio_out_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            if len(chunks) == 1:
                batch = chunks[0]
            else:
                batch = np.concatenate(chunks)

            await asyncio.to_thread(speaker_stream.write, batch)

    speaker_task = asyncio.create_task(play_audio_loop())
    keyboard_task = asyncio.create_task(keyboard_listener(ptt, shutdown_event, resolved_ide=resolved_ide))

    ws_send_lock = asyncio.Lock()
    is_first_connect = True

    try:
        # 会话永久保活循环：遇网络抖动、空闲超时或服务端波动自动毫秒级重连，绝不断开！
        while not shutdown_event.is_set():
            try:
                if is_first_connect:
                    print(f"\n[Live API] 正在连接实时多模态管道 ({model_name})...")
                else:
                    print(f"\n[Live API 保持] 正在重新建立语音多模态管道 ({model_name})...")

                is_ai_speaking = False
                speaker_cooldown_until = 0.0
                while not audio_out_queue.empty():
                    try:
                        audio_out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                while not audio_in_queue.empty():
                    try:
                        audio_in_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                async with client.aio.live.connect(model=model_name, config=config) as session:
                    if is_first_connect:
                        print("\n" + "="*62)
                        print(" ★ Antigravity 实时语音助手已就绪！(永久保活常驻)")
                        if always_listen:
                            print(" ★ 模式: 全双工持续监听 (建议佩戴耳机)")
                        else:
                            print(" ★ 模式: 对讲机 Push-to-Talk (按 空格 或 回车 开始说话)")
                            print(" ★ 特性: AI 开始回复时自动闭麦，彻底杜绝外放回音自激！")
                            print(" ★ 优势: 平时静音，完全不影响 Typeless 语音打字！")
                        print(f" ★ 音色: {voice_name} | 引擎: {model_name}")
                        print(f" ★ 专属 IDE 协同模式: \033[1;36m{resolved_ide.upper()}\033[0m (独立监听专属工作区，绝不跨 IDE 串音)")
                        print(" ★ 挂载能力: Antigravity Pro (Gemini 3.8 Flash High) + Stitch MCP")
                        print(" ★ 按 Ctrl+C 退出会话")
                        print("="*62 + "\n")
                        is_first_connect = False
                    else:
                        print("\033[1;32m[Live API 保持] ✓ 会话重连成功，语音伴随已就绪！\033[0m\n")

                    ptt.print_status()

                    # 单通道语音排队调度器（集中仲裁 Cursor、Antigravity、派单回报与媒体避让）
                    async def do_send_speech_prompt(item: SpeechItem):
                        content = types.Content(role="user", parts=[types.Part.from_text(text=item.prompt_text)])
                        async with ws_send_lock:
                            await session.send_client_content(turns=content, turn_complete=True)

                    speech_coordinator = SpeechCoordinator(
                        self_pid=os.getpid(),
                        workspace=workspace_root,
                        is_user_speaking_fn=lambda: ptt.is_active,
                        is_local_ai_speaking_fn=lambda: is_ai_speaking,
                        is_audio_busy_fn=lambda: (not audio_out_queue.empty()),
                        send_speech_fn=do_send_speech_prompt,
                        update_hud_fn=lambda msg: ptt.update_dashboard(advisor_status=msg[:30]),
                        silence_stabilization_sec=0.5,
                    )
                    ptt.on_unmute = speech_coordinator.cancel_transient

                    # 任务 A: 麦克风音频流式发送
                    async def send_mic_loop():
                        try:
                            with sd.InputStream(
                                samplerate=INPUT_SAMPLE_RATE,
                                channels=CHANNELS,
                                dtype='int16',
                                blocksize=512,
                                latency='low',
                                callback=mic_callback
                            ):
                                while not shutdown_event.is_set():
                                    # 智能断句闭麦：若长官在对讲中已开嗓说完话且停顿超过 800ms，自动触发闭麦并发送 audio_stream_end 通知 AI 秒级回复
                                    import time
                                    now = time.time()
                                    if not ptt.always_listen and ptt.is_active and ptt.has_spoken:
                                        if now - ptt.last_voice_time >= 0.8:
                                            ptt.mute()

                                    # 当用户按键闭麦或智能断句闭麦时：
                                    if ptt.just_muted:
                                        ptt.just_muted = False
                                        if not ptt.has_spoken:
                                            # 长官开麦期间未开嗓发声（纯环境底噪或误触）：
                                            # 彻底清空队列与预留缓冲区，绝不向服务端推流，绝对不发送 audio_stream_end，物理斩断模型脑补！
                                            while not audio_in_queue.empty():
                                                try:
                                                    audio_in_queue.get_nowait()
                                                except asyncio.QueueEmpty:
                                                    break
                                            pre_roll_buffer.clear()
                                            ptt.print_status()
                                            continue

                                        # 若长官已开嗓发声，毫秒级倾泻发送队列残留尾音包，并发送 audio_stream_end 结束信号通知服务端推理！
                                        while not audio_in_queue.empty():
                                            try:
                                                tail_chunk = audio_in_queue.get_nowait()
                                                async with ws_send_lock:
                                                    await session.send_realtime_input(
                                                        audio=types.Blob(
                                                            mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                                                            data=tail_chunk.tobytes()
                                                        )
                                                    )
                                            except asyncio.QueueEmpty:
                                                break
                                            except Exception:
                                                pass

                                        try:
                                            async with ws_send_lock:
                                                await session.send_realtime_input(audio_stream_end=True)
                                        except Exception:
                                            pass
                                        continue

                                    try:
                                        chunk = await asyncio.wait_for(audio_in_queue.get(), timeout=0.04)
                                    except asyncio.TimeoutError:
                                        continue

                                    # 当开麦时推流 (PTT 模式下长官拥有绝对话语权，微聚合 40-60ms 批量推流降低 WebSocket 开销)
                                    can_send = ptt.is_active and (not ptt.always_listen or not is_ai_speaking)
                                    if can_send:
                                        chunks_to_send = [chunk]
                                        while not audio_in_queue.empty() and len(chunks_to_send) < 3:
                                            try:
                                                chunks_to_send.append(audio_in_queue.get_nowait())
                                            except asyncio.QueueEmpty:
                                                break

                                        if len(chunks_to_send) == 1:
                                            batch_bytes = chunks_to_send[0].tobytes()
                                        else:
                                            batch_bytes = np.concatenate(chunks_to_send).tobytes()

                                        try:
                                            async with ws_send_lock:
                                                await session.send_realtime_input(
                                                    audio=types.Blob(
                                                        mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                                                        data=batch_bytes
                                                    )
                                                )
                                            metrics.total_frames_sent += len(chunks_to_send)
                                        except Exception:
                                            break
                        except (asyncio.CancelledError, KeyboardInterrupt):
                            pass

                    # 派单任务后台异步执行与军规级主动语音汇报
                    async def run_dispatched_task(task_desc: str, authorized: bool = False, category: str = "general"):
                        mode_str = "已授权实操" if authorized else "只读分析模式"
                        banner = (
                            f"\033[1;32m{'='*65}\033[0m\n"
                            f"\033[1;32m★ [派单司令塔] 长官口头派单 ->【实施工程师】已成功接单！\033[0m\n"
                            f"\033[1;37m需求指令: {task_desc}\033[0m\n"
                            f"\033[1;36m任务分类: {category} | 授权状态: {mode_str}\033[0m\n"
                            f"\033[1;33m提示: 语音通话保持畅通，您可以随时按 Ctrl+Space 开麦继续交流！\033[0m\n"
                            f"\033[1;32m{'='*65}\033[0m"
                        )
                        ptt.write_log(banner)

                        try:
                            task_output = await runner.run_task(task_desc, authorized=authorized)
                            is_error = any(sig in task_output for sig in ("FAIL", "AssertionError", "Command failed", "Traceback"))
                        except Exception as e:
                            task_output = f"执行出错: {e}"
                            is_error = True

                        ptt.write_log("\033[1;32m[派单任务执行完毕] 正在通过排队总线向长官通报结果...\033[0m")

                        try:
                            clean_task = sanitize_for_speech(task_desc, max_chars=60)
                            clean_out = sanitize_for_speech(task_output, max_chars=360)
                            if is_error:
                                prompt_text = (
                                    f"[系统警报: 实施工程师执行长官派单任务遇到异常]\n"
                                    f"派单任务: {clean_task}\n"
                                    f"异常输出要点: {clean_out}\n\n"
                                    f"【发音军规】: 必须以'报告 长官！'开头，用 2 到 3 句标准普通话汇报："
                                    f"说明异常位置、核心原因、建议下一步。严禁朗读代码符号或文件路径！"
                                )
                            else:
                                prompt_text = (
                                    f"[系统通知: 实施工程师已完成长官派发任务 ({mode_str})]\n"
                                    f"派单任务: {clean_task}\n"
                                    f"执行结论要点: {clean_out}\n\n"
                                    f"【发音军规】: 必须以'报告 长官！'开头，用 2 到 3 句标准普通话汇报："
                                    f"说明派单已完成，概括核心成果与影响面。严禁念出代码符号或文件路径！"
                                )

                            await speech_coordinator.enqueue(
                                SpeechItem(
                                    category=SpeechCategory.ERROR if is_error else SpeechCategory.DISPATCH_RESULT,
                                    source="dispatch",
                                    task_key=clean_task,
                                    prompt_text=prompt_text,
                                    summary=f"派单汇报: {clean_task}",
                                    ttl=300.0,
                                )
                            )
                        except Exception as e:
                            ptt.write_log(f"\033[1;31m[派单汇报推送排队异常] {e}\033[0m")

                    # 任务 C: 接收服务端消息（持续多轮监听，绝不单轮退出）
                    async def receive_loop():
                        nonlocal is_ai_speaking
                        ai_subtitle_streaming = False
                        try:
                            while not shutdown_event.is_set():
                                async for response in session.receive():
                                    if shutdown_event.is_set():
                                        break
                                    sc = response.server_content
                                    if sc:
                                        # 1. 检测服务端打断信号：仅当长官主动开麦或全双工模式下才允许打断！
                                        if sc.interrupted:
                                            if ptt.is_active or ptt.always_listen:
                                                while not audio_out_queue.empty():
                                                    try:
                                                        audio_out_queue.get_nowait()
                                                    except asyncio.QueueEmpty:
                                                        break
                                                is_ai_speaking = False
                                                speech_coordinator.cancel_transient()
                                                if ai_subtitle_streaming:
                                                    sys.stdout.write(" \033[1;33m[语音已打断]\033[0m\n")
                                                    sys.stdout.flush()
                                                    ai_subtitle_streaming = False
                                                    ptt.is_streaming_subtitle = False
                                                ptt.print_status()
                                            else:
                                                # 长官处于静音状态，此打断属于网络残包或回声误触发，坚决忽略，继续完整播放 AI 语音！
                                                pass

                                        # 2. 话语权守护：长官开麦后若 AI 开始回复，自动切换为闭麦倾听模式，彻底杜绝外放回音并立即播放！
                                        if sc.model_turn and ptt.is_active and not ptt.always_listen:
                                            ptt.mute()

                                        # 3. 实时打印开发者语音转写，并驱动军规确认门禁状态机
                                        if sc.input_transcription and sc.input_transcription.text:
                                            speech_text = sc.input_transcription.text
                                            confirmation_gate.observe_user_speech(speech_text)
                                            logger.info(f"[长官语音] {speech_text}")
                                            if ai_subtitle_streaming:
                                                sys.stdout.write("\n")
                                                sys.stdout.flush()
                                                ai_subtitle_streaming = False
                                                ptt.is_streaming_subtitle = False
                                            ptt.write_log(f"\033[1;34m[你识别为]\033[0m {speech_text}")

                                        # 4. 实时流式平滑打印 AI 语音回复字幕（独立行首，自动换行，杜绝碎片折行与乱码）
                                        if sc.output_transcription and sc.output_transcription.text:
                                            if not ai_subtitle_streaming:
                                                ai_subtitle_streaming = True
                                                ptt.is_streaming_subtitle = True
                                                sys.stdout.write("\r\033[K\033[1;35m[AI字幕]\033[0m ")
                                            sys.stdout.write(sc.output_transcription.text)
                                            sys.stdout.flush()

                                        # 5. 处理语音音频数据包 (带防爆仓抛弃老帧机制)
                                        if sc.model_turn:
                                            if not ptt.always_listen and ptt.is_active:
                                                ptt.mute()
                                            is_ai_speaking = True
                                            for part in sc.model_turn.parts:
                                                if part.text and not sc.output_transcription:
                                                    if not ai_subtitle_streaming:
                                                        ai_subtitle_streaming = True
                                                        ptt.is_streaming_subtitle = True
                                                        sys.stdout.write("\r\033[K\033[1;35m[AI字幕]\033[0m ")
                                                    sys.stdout.write(part.text)
                                                    sys.stdout.flush()
                                                if part.inline_data and part.inline_data.mime_type.startswith("audio/pcm"):
                                                    audio_chunk = np.frombuffer(part.inline_data.data, dtype=np.int16)
                                                    if audio_out_queue.full():
                                                        try:
                                                            audio_out_queue.get_nowait()
                                                        except Exception:
                                                            pass
                                                    try:
                                                        audio_out_queue.put_nowait(audio_chunk)
                                                    except asyncio.QueueFull:
                                                        pass

                                        # 6. 一轮对话完成，强制静音麦克风，杜绝空闲底噪与回音误触发
                                        if sc.turn_complete:
                                            if ai_subtitle_streaming:
                                                sys.stdout.write("\n")
                                                sys.stdout.flush()
                                                ai_subtitle_streaming = False
                                                ptt.is_streaming_subtitle = False
                                            if not ptt.always_listen:
                                                ptt.mute()
                                            ptt.print_status()

                                    # 7. 调度执行 Tool Call (模式 A 工具集)
                                    if response.tool_call:
                                        if ai_subtitle_streaming:
                                            sys.stdout.write("\n")
                                            sys.stdout.flush()
                                            ai_subtitle_streaming = False
                                            ptt.is_streaming_subtitle = False
                                        for call in response.tool_call.function_calls:
                                            if call.name == "get_ide_status_and_context":
                                                ptt.write_log("\033[1;36m[IDE 上下文获取] 正在读取 IDE 聊天状态与执行进展...\033[0m")
                                                snapshot = get_ide_chat_snapshot(workspace_root=workspace_root, ide_target=resolved_ide)
                                                async with ws_send_lock:
                                                    await session.send_tool_response(
                                                        function_responses=[
                                                            types.FunctionResponse(
                                                                name=call.name,
                                                                id=call.id,
                                                                response=snapshot
                                                            )
                                                        ]
                                                    )
                                                if not ptt.always_listen:
                                                    ptt.mute()
                                                ptt.print_status()

                                            elif call.name == "read_ide_implementation_plan":
                                                ptt.write_log("\033[1;36m[IDE 方案读取] 正在读取实施方案与总结报告...\033[0m")
                                                snapshot = get_ide_chat_snapshot()
                                                plan_path = snapshot.get("plan_path")
                                                plan_text = ""
                                                if plan_path and os.path.exists(plan_path):
                                                    try:
                                                        with open(plan_path, "r", encoding="utf-8") as f:
                                                            plan_text = f.read(2000)
                                                    except Exception as e:
                                                        plan_text = f"读取方案失败: {e}"
                                                else:
                                                    conv_id = snapshot.get("conversation_id")
                                                    if conv_id:
                                                        walk_path = os.path.expanduser(f"~/.gemini/antigravity-ide/brain/{conv_id}/walkthrough.md")
                                                        if os.path.exists(walk_path):
                                                            try:
                                                                with open(walk_path, "r", encoding="utf-8") as f:
                                                                    plan_text = f.read(2000)
                                                            except Exception:
                                                                pass

                                                clean_plan = sanitize_for_speech(plan_text, max_chars=400) if plan_text else ""
                                                async with ws_send_lock:
                                                    await session.send_tool_response(
                                                        function_responses=[
                                                            types.FunctionResponse(
                                                                name=call.name,
                                                                id=call.id,
                                                                response={
                                                                    "found": bool(clean_plan),
                                                                    "content": clean_plan or "当前尚未生成实施方案或总结报告文件。",
                                                                }
                                                            )
                                                        ]
                                                    )
                                                if not ptt.always_listen:
                                                    ptt.mute()
                                                ptt.print_status()

                                            elif call.name in ("shutdown_sidecar", "stop_voice_copilot"):
                                                ptt.write_log("\033[1;31m[明确关闭指令] 收到长官明确口令指示，正在退出语音副驾...\033[0m")
                                                async with ws_send_lock:
                                                    await session.send_tool_response(
                                                        function_responses=[
                                                            types.FunctionResponse(
                                                                name=call.name,
                                                                id=call.id,
                                                                response={"status": "shutting_down", "message": "遵命长官，语音副驾已为您关闭，随时待命！"}
                                                            )
                                                        ]
                                                    )
                                                # 写入明确停止标记，告知守护脚本停止自愈重启
                                                set_explicit_stop_flag(resolved_ide)
                                                await asyncio.sleep(0.8)
                                                shutdown_event.set()
                                                break

                                            elif call.name in ("dispatch_task_to_engineer", "execute_antigravity_task"):
                                                task_prompt = call.args.get("task_prompt") or call.args.get("task_description", "")
                                                category = call.args.get("task_category", "general")
                                                allow_mod = call.args.get("allow_modification")
                                                if allow_mod is None:
                                                    allow_mod = call.args.get("authorized", False)

                                                # 口头语义智能提权：长官只要说修改、修复、实现、跑测试等动词，判定为长官口头派单实操
                                                action_verbs = ["改", "修", "加", "删", "写", "跑", "测试", "实现", "优化", "重构"]
                                                if any(v in task_prompt for v in action_verbs):
                                                    allow_mod = True

                                                # ★【军规级两阶段确认物理硬门禁】：未获长官明确口头确认，坚决物理拦截，绝不抢跑！
                                                passed, reason = confirmation_gate.verify_and_consume(task_prompt)
                                                if not passed:
                                                    metrics.blocked_attempts_count += 1
                                                    logger.warning(f"[军规硬门禁拦截抢跑] 任务='{task_prompt}', 原因='{reason}'")
                                                    ptt.write_log(f"\033[1;33m[军规硬门禁拦截] 拦截擅自抢跑派单: {task_prompt[:35]}... -> 逼退复述请示\033[0m")
                                                    async with ws_send_lock:
                                                        await session.send_tool_response(
                                                            function_responses=[
                                                                types.FunctionResponse(
                                                                    name=call.name,
                                                                    id=call.id,
                                                                    response={
                                                                        "status": "blocked_by_military_gate",
                                                                        "passed": False,
                                                                        "instruction": reason
                                                                    }
                                                                )
                                                            ]
                                                        )
                                                    if not ptt.always_listen:
                                                        ptt.mute()
                                                    ptt.print_status()
                                                    continue

                                                metrics.dispatched_tasks_count += 1
                                                logger.info(f"[门禁放行派单] 任务='{task_prompt}', 授权实操={allow_mod}, 类别={category}")
                                                ptt.write_log(f"\033[1;32m[派单司令塔] 长官已口头确认，准予执行: {task_prompt} (实操权限: {allow_mod})\033[0m")

                                                # 1. 毫秒级返回 ToolResponse，消除阻塞
                                                async with ws_send_lock:
                                                    await session.send_tool_response(
                                                        function_responses=[
                                                            types.FunctionResponse(
                                                                name=call.name,
                                                                id=call.id,
                                                                response={
                                                                    "status": "dispatched",
                                                                    "authorized": allow_mod,
                                                                    "message": (
                                                                        f"任务【{task_prompt}】已成功分派至后台实施工程师执行（{'已授权实操' if allow_mod else '只读分析'}）。"
                                                                        f"请立即用极其干练的一句话向长官回执（必须以'报告 长官！'开头）：'报告 长官！任务已派发给实施工程师，正在执行：{task_prompt[:35]}，请稍候。'，"
                                                                        "并说明在此期间长官可以随时继续正常交谈。"
                                                                    )
                                                                }
                                                            )
                                                        ]
                                                    )
                                                if not ptt.always_listen:
                                                    ptt.mute()
                                                ptt.print_status()

                                                # 2. 后台异步协程拉起派单任务，绝不阻塞 receive_loop
                                                asyncio.create_task(run_dispatched_task(task_prompt, authorized=allow_mod, category=category))
                        finally:
                            if ai_subtitle_streaming:
                                sys.stdout.write("\n")
                                sys.stdout.flush()
                                ai_subtitle_streaming = False
                                ptt.is_streaming_subtitle = False

                    # 模式 A: IDE 状态与事件实时监听回调（通过单通道协调器统一排队）
                    async def on_ide_task_completed(task_name: str, summary: str, source: str = "ide"):
                        src_title = "Cursor" if source == "cursor" else "Antigravity"
                        ptt.update_dashboard(ide_action=f"已完成: {task_name[:25]}", advisor_status="通报排队中")
                        ptt.write_log(f"\033[1;32m[{src_title} 状态感知] 实施工程师已完成任务: {task_name[:50]}\033[0m")

                        clean_task = sanitize_for_speech(task_name, max_chars=60)
                        clean_summary = sanitize_for_speech(summary, max_chars=420)
                        prompt_text = (
                            f"[系统通知: {src_title} 实施工程师已完成任务]\n"
                            f"任务: {clean_task}\n"
                            f"完成要点: {clean_summary}\n\n"
                            f"【发音军规】: 必须以'报告 长官！'开头，用标准普通话做 2 到 3 句战报："
                            f"第1句说明任务已完成；第2句概括 2 到 3 个关键结论或改动；"
                            f"若有风险或待办，第3句简要提醒。严禁念代码符号、括号、下划线或文件路径。"
                        )
                        await speech_coordinator.enqueue(
                            SpeechItem(
                                category=SpeechCategory.TASK_COMPLETED,
                                source=source,
                                task_key=clean_task,
                                prompt_text=prompt_text,
                                summary=f"[{src_title}] 完成: {clean_task}",
                                ttl=180.0,
                            )
                        )

                    async def on_ide_error_detected(action: str, error_snippet: str, source: str = "ide"):
                        src_title = "Cursor" if source == "cursor" else "Antigravity"
                        ptt.update_dashboard(ide_action=f"异常: {action[:25]}", advisor_status="预警排队中")
                        err_banner = (
                            f"\033[1;31m[{src_title} 异常预警] 实施工程师执行失败: {action}\033[0m\n"
                            f"\033[31m  -> 核心原因: {error_snippet}\033[0m"
                        )
                        ptt.write_log(err_banner)

                        clean_act = sanitize_for_speech(action, max_chars=30)
                        clean_err = sanitize_for_speech(error_snippet, max_chars=100)
                        alert_text = (
                            f"[紧急预警: {src_title} 实施工程师执行出错]\n"
                            f"操作动作: {clean_act}\n"
                            f"报错核心原因: {clean_err}\n\n"
                            f"【发音军规】: 请用清晰利落的女主播语气（必须以'报告 长官！'开头），用一句纯正自然的中文普通话向长官说明哪项任务遇到什么问题。严禁朗读任何代码符号或文件路径！"
                        )
                        await speech_coordinator.enqueue(
                            SpeechItem(
                                category=SpeechCategory.ERROR,
                                source=source,
                                task_key=clean_act,
                                prompt_text=alert_text,
                                summary=f"[{src_title}] 异常: {clean_act}",
                                ttl=300.0,
                            )
                        )

                    async def on_ide_action(action: str, source: str = "ide"):
                        ptt.update_dashboard(ide_action=action[:40])

                    async def on_ide_narration(narration_text: str, source: str = "ide"):
                        src_title = "Cursor" if source == "cursor" else "Antigravity"
                        ptt.update_dashboard(advisor_status=narration_text[:30])
                        # 长官若开麦对讲，或 AI 正在播音，坚决不插话干扰
                        if ptt.is_active or is_ai_speaking:
                            return
                        clean_narration = sanitize_for_speech(narration_text, max_chars=90)
                        prompt_text = (
                            f"[{src_title} 实施工程师伴随解说]\n"
                            f"施工进展: {clean_narration}\n\n"
                            f"请用标准普通话向长官播报当前进展（控制在 28 到 40 字）："
                            f"说明正在做什么、对象是什么；切勿寒暄，严禁念出技术符号与绝对路径。"
                        )
                        await speech_coordinator.enqueue(
                            SpeechItem(
                                category=SpeechCategory.NARRATION,
                                source=source,
                                task_key=f"narration_{source}",
                                prompt_text=prompt_text,
                                summary=f"[{src_title}] 施工进展: {clean_narration}",
                                ttl=25.0,
                            )
                        )

                    async def on_ide_user_input(req: str, source: str = "ide"):
                        src_title = "Cursor" if source == "cursor" else "Antigravity"
                        ptt.update_dashboard(ide_action=f"新需求: {req[:25]}")
                        ptt.write_log(f"\033[1;34m[{src_title} 状态感知] 监测到长官发送了新需求: {req[:60]}...\033[0m")
                        try:
                            clean_req = sanitize_for_speech(req, max_chars=80)
                            c = types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_text(
                                        text=(
                                            f"[系统静默记忆: 长官在 {src_title} 下达了新需求: \"{clean_req}\"，实施工程师已接单分析中。"
                                            f"此条仅供记忆储备，无需发声。]"
                                        )
                                    )
                                ]
                            )
                            async with ws_send_lock:
                                await session.send_client_content(turns=c, turn_complete=False)
                        except Exception:
                            pass

                    watcher_tasks = []
                    if resolved_ide in ("antigravity", "all"):
                        ide_watcher = IDEWatcher(
                            poll_interval=0.5,
                            on_completed=lambda t, s: on_ide_task_completed(t, s, source="antigravity"),
                            on_user_input=lambda r: on_ide_user_input(r, source="antigravity"),
                            on_error=lambda a, e: on_ide_error_detected(a, e, source="antigravity"),
                            on_action=lambda a: on_ide_action(a, source="antigravity"),
                            on_narration=lambda n: on_ide_narration(n, source="antigravity"),
                        )
                        watcher_tasks.append(asyncio.create_task(ide_watcher.start(shutdown_event)))

                    if resolved_ide in ("cursor", "all"):
                        cursor_watcher = CursorTranscriptWatcher(
                            workspace_root=workspace_root,
                            poll_interval=0.5,
                            on_completed=lambda t, s: on_ide_task_completed(t, s, source="cursor"),
                            on_user_input=lambda r: on_ide_user_input(r, source="cursor"),
                            on_error=lambda a, e: on_ide_error_detected(a, e, source="cursor"),
                            on_action=lambda a: on_ide_action(a, source="cursor"),
                            on_narration=lambda n: on_ide_narration(n, source="cursor"),
                        )
                        watcher_tasks.append(asyncio.create_task(cursor_watcher.start(shutdown_event)))

                    # 并发执行输入、接收、语音排队协调与专属 IDE 监听（仅发生异常时退出重连）
                    core_tasks = [
                        asyncio.create_task(send_mic_loop()),
                        asyncio.create_task(receive_loop()),
                        asyncio.create_task(speech_coordinator.worker_loop(shutdown_event)),
                    ]
                    done, pending = await asyncio.wait(
                        core_tasks + watcher_tasks,
                        return_when=asyncio.FIRST_EXCEPTION
                    )
                    for p in pending:
                        p.cancel()
                    for d in done:
                        if not d.cancelled():
                            exc = d.exception()
                            if exc and not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                                raise exc

            except (asyncio.CancelledError, KeyboardInterrupt):
                shutdown_event.set()
                break
            except Exception as err:
                if shutdown_event.is_set():
                    break
                metrics.reconnect_count += 1
                ptt.write_log(f"\033[1;33m[Live API 保持] ⚠️ 会话连接波动 ({err})，正在自动恢复重连... (第{metrics.reconnect_count}次, 1.5秒后)\033[0m")
                while not audio_in_queue.empty():
                    try:
                        audio_in_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                while not audio_out_queue.empty():
                    try:
                        audio_out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                pre_roll_buffer.clear()
                await asyncio.sleep(1.5)

    finally:
        sys.stdout.write("\033[?7h\n")
        sys.stdout.flush()
        shutdown_event.set()
        logger.info(f"[Live Sidecar 退出] 最终统计: {metrics.summary()}")
        if global_hotkey:
            try:
                global_hotkey.stop()
            except Exception:
                pass
        keyboard_task.cancel()
        speaker_task.cancel()
        speaker_stream.stop()
        speaker_stream.close()
        await runner.close()
        print(f"\n\n[Live Sidecar] 会话已安全断开。运行统计: {metrics.summary()}\n")


def main():
    parser = argparse.ArgumentParser(description="Antigravity Gemini 3.8 Live Sidecar with Push-to-Talk")
    parser.add_argument("--list-devices", action="store_true", help="列出可用音频设备")
    parser.add_argument("--test-audio", action="store_true", help="自检录音与回放")
    parser.add_argument("--workspace", type=str, default=None, help="Antigravity 目标工程根目录 (默认自动探测 IDE 当前活跃工程)")
    parser.add_argument("--model", type=str, default=os.getenv("LIVE_MODEL_NAME", "models/gemini-3.8-live"), help="Live API 模型")
    parser.add_argument("--voice", type=str, default=os.getenv("VOICE_NAME", "Zephyr"), help="语音音色 (女声推荐: Zephyr-清亮甜美女主播/默认, Leda-年轻元气女主播, Kore-沉稳干练女官; 男声: Puck, Charon, Fenrir)")
    parser.add_argument("--always-listen", action="store_true", help="禁用 Push-to-Talk，开启全双工持续监听常驻")
    parser.add_argument("--vad-silence-ms", type=int, default=int(os.getenv("VAD_SILENCE_MS", "1500")), help="静音断句容忍延时 (毫秒，默认 1500ms，为长官预留充足说话思考时间)")
    parser.add_argument("--auto-reply", action="store_true", default=os.getenv("AUTO_REPLY", "false").lower() in ("true", "1", "yes"), help="开启停顿自动回复（默认关闭，采用手动确认对讲模式：开麦畅所欲言，说完再次按 Ctrl+Space 确认发送，彻底杜绝抢话打断）")
    parser.add_argument("--ide", type=str, default="auto", choices=["auto", "cursor", "antigravity", "all"], help="指定当前终端专属协同的 IDE 模式 (默认 auto: 自动识别当前终端所属 IDE)")
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    if args.test_audio:
        test_audio_loopback()
        return

    # 动态锁定活跃工作区：优先命令行显式指定；未指定时自动探测 IDE 当前正在打开的活跃工程，杜绝路径逃逸
    target_workspace = args.workspace
    if not target_workspace or not os.path.isdir(target_workspace):
        target_workspace = detect_active_workspace(ide_type=args.ide)

    target_ide = args.ide if args.ide != "auto" else detect_current_ide()
    target_path = Path(target_workspace).resolve()
    project_meta = get_project_grounding_info(str(target_path))

    # 动态设定终端选项卡标签，方便在 IDE 终端列表中精准区分会话
    try:
        sys.stdout.write(f"\033]0;Agent Live [{target_ide.upper()}] - {target_path.name}\007")
        sys.stdout.flush()
    except Exception:
        pass

    sys.stdout.write("\n" + "="*65 + "\n")
    sys.stdout.write(f" ★ [IDE 宿主模式] 专属独立通道: \033[1;36m{target_ide.upper()}\033[0m\n")
    sys.stdout.write(f" ★ [动态工作区锁定] 已锚定当前工程: \033[1;32m{target_path.name}\033[0m\n")
    sys.stdout.write(f" ★ 物理路径: {target_path}\n")
    sys.stdout.write(f" ★ 业务基线: {project_meta['summary'][:65]}\n")
    if project_meta['modules']:
        sys.stdout.write(f" ★ 核心模块: {'、'.join(project_meta['modules'][:8])}\n")
    sys.stdout.write("="*65 + "\n\n")
    sys.stdout.flush()

    logger.info(f"Locked active workspace: {target_path} (name={target_path.name})")
    logger.info(f"Project grounding summary: {project_meta['summary']}")

    manual_confirm = not args.auto_reply

    try:
        asyncio.run(run_live_session(
            str(target_path),
            args.model,
            args.voice,
            always_listen=args.always_listen,
            vad_silence_ms=args.vad_silence_ms,
            manual_confirm=manual_confirm,
            ide_target=args.ide,
        ))
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)


if __name__ == "__main__":
    main()
