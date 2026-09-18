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

from google import genai
from google.genai import types
from antigravity_runner import AntigravityRunner
from ide_watcher import get_ide_chat_snapshot, IDEWatcher, sanitize_for_speech

# 音频参数
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_SIZE = 1024


class NoiseGate:
    """本地轻量级音频能量门限与防吞字降噪器 (RMS Noise Gate with Hangover)"""
    def __init__(self, threshold: float = 260.0, hangover_ms: int = 350, frame_duration_ms: int = 20):
        self.threshold = threshold
        self.hangover_frames = int(hangover_ms / frame_duration_ms)
        self.active_frames_remaining = 0
        self.is_gate_open = False

    def process(self, indata: np.ndarray) -> bool:
        """
        处理单帧 16kHz PCM 音频。
        返回 True 表示门限打开（有效人声或处于防吞字平滑期），False 表示环境底噪/静音。
        """
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        if rms >= self.threshold:
            self.active_frames_remaining = self.hangover_frames
            self.is_gate_open = True
            return True
        elif self.active_frames_remaining > 0:
            self.active_frames_remaining -= 1
            self.is_gate_open = True
            return True
        else:
            self.is_gate_open = False
            return False


def setup_global_hotkey(ptt, loop: asyncio.AbstractEventLoop):
    """
    配置系统级全局呼叫热键 (支持跨软件/后台随时对讲呼叫)
    默认绑定: <ctrl>+<space> 以及备选 <cmd>+<shift>+<space>
    """
    try:
        from pynput import keyboard

        def on_hotkey_triggered():
            loop.call_soon_threadsafe(ptt.toggle)

        hotkey_map = {
            '<ctrl>+<space>': on_hotkey_triggered,
            '<cmd>+<shift>+<space>': on_hotkey_triggered,
        }
        listener = keyboard.GlobalHotKeys(hotkey_map)
        listener.daemon = True
        listener.start()
        print(" ★ [全局呼叫热键] ✓ 已激活！在任何软件/窗口按 Ctrl+Space 即可随时对讲！")
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
                    "【向实施工程师派单】当长官口头要求实施工程师执行具体的开发、修改代码、运行测试、排查分析、重构等工程任务时调用。"
                    "此工具会将任务直接派发至本地 Antigravity 实施工程师引擎异步执行，长官无需切回 IDE 打字输入。"
                    "调用后，语音助手必须立即用极其干练的一句话向长官回执：'报告 长官！任务已派发给实施工程师，正在执行：<简述任务>，请稍候。'，"
                    "随后后台在独立协程中执行，期间长官可继续正常语音交谈，任务完成后系统会自动触发主动语音汇报！"
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "task_prompt": {
                            "type": "STRING",
                            "description": "长官口头下达的具体工程任务需求与指令细节，要求完整传达长官意图"
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

    def update_dashboard(self, ide_action: Optional[str] = None, advisor_status: Optional[str] = None):
        if ide_action is not None:
            self.current_ide_action = ide_action
        if advisor_status is not None:
            self.current_advisor_status = advisor_status
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
            self.print_status()

    def unmute(self):
        if self.always_listen:
            return
        if not self.is_active:
            self.is_active = True
            self.just_muted = False
            self.is_voice_active = False
            if self.on_unmute:
                self.on_unmute()
            self.print_status()

    def print_status(self):
        import shutil
        cols = max(40, shutil.get_terminal_size((80, 24)).columns)
        vol_meter = self._render_volume_bar(self.last_volume, self.is_voice_active)
        if self.always_listen:
            ptt_str = "\033[1;32m[🎙️ 全双工]\033[0m"
        elif self.is_active:
            ptt_str = f"\033[1;32m[🎙️ 开麦 {vol_meter}]\033[0m \033[32m(说完按 Ctrl+Space)\033[0m"
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


async def keyboard_listener(ptt: PTTController, shutdown_event: asyncio.Event):
    """终端按键监听任务 (空格/回车切换静音与开麦)"""
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
            if char in (' ', '\r', '\n'):
                ptt.toggle()
            elif char == '\x03':  # Ctrl+C
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
    manual_confirm: bool = True
):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("\n[错误] 未检测到 GEMINI_API_KEY 环境变量！", file=sys.stderr)
        print("请在 tools/live-sidecar/.env 中配置 GEMINI_API_KEY=xxx，或通过环境变量导出。", file=sys.stderr)
        print("您可以在 Google AI Studio 快速免费获取 Key: https://aistudio.google.com/app/api-keys\n", file=sys.stderr)
        sys.exit(1)

    # 1. 启动 Antigravity 运行时 (采用 Pro Plan 账户认证)
    runner = AntigravityRunner(workspace_path=workspace_root)
    await runner.start()

    # 2. 准备 Live API 客户端
    client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        # 禁用非必要思考延迟，实现毫秒级快速响应
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        # 智能宽容断句检测 (VAD)：默认留出 1500ms（或指定延时）充裕停顿与思考时间，LOW 灵敏度防抢话
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                silence_duration_ms=max(800, vad_silence_ms)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        tools=ANTIGRAVITY_TOOLS,
        system_instruction=types.Content(
            parts=[
                types.Part.from_text(
                    text=(
                        "你是一名顶尖的结对编程副驾兼伴飞派单司令塔（Navigator），拥有字正腔圆、甜美自然、清亮灵动的专业女主播音色，正通过实时双工语音与长官（项目主管兼技术总监）交流协作。\n\n"
                        "【★ 语言纯正性铁律（绝对锁死中文普通话）】：\n"
                        "必须全程 100% 仅使用纯正中文普通话交流！无论长官说话带有何种口音、或背景出现何种杂音或外来语词，绝对严禁回答韩语、日语、英语或任何其他语言！一律理解为中文普通话，并用字正腔圆的标准普通话作答！\n\n"
                        "【★ 核心语言与发音军规（最高优先级，严守播音品质）】：\n"
                        "1. 【纯正普通话发音】：必须全程使用纯正、标准、自然的中文普通话。语调像专业科技电台女主播一样亲切清亮、字正腔圆、悦耳灵动，严禁怪异口音、生硬语调或机械顿挫！\n"
                        "2. 【绝对杜绝文字乱念与机械符号朗读】：\n"
                        "   - 严禁朗读任何 Markdown 标记（绝不念 星号、井号、反引号、横线减号、大于号、波浪号 等）！\n"
                        "   - 严禁朗读任何代码符号（绝不念 花括号、中括号、尖括号、反斜杠、斜杠、下划线、引号 等）！\n"
                        "   - 严禁朗读任何文件长路径（如 /Users/...）或网址 URL！提到文件直接用简短名称（如“主配置文件”、“入口模块”）代称，代码内容直接口语一笔带过！\n"
                        "3. 【程序员术语标准口语转换】：\n"
                        "   - 英文缩写请使用程序员通俗自然发音：PR 念“P-R”、API 念“A-P-I”、IDE 念“I-D-E”、UI 念“U-I”、URL 念“链接”；\n"
                        "   - 常见词汇自然读出：Git 念“Git”、Bug 念“Bug”、Vue 念“View”、CSS 念“C-S-S”、SQL 念“S-Q-L”、npm 念“N-P-M”；\n"
                        "   - 版本号与数字请用中文自然读出（如 3.8 念“三点八”，200 念“两百”）。\n"
                        "4. 【专供耳朵收听的极简口语】：长官是用耳朵收听你的声音，所有语音回复必须是自然简短的口语句子，直奔要害，严禁输出任何长篇书面大论、代码块或列表清单！\n\n"
                        "【核心协作分工（模式 A）】：\n"
                        "1. IDE 聊天框中的 Antigravity Agent 是『实施工程师』，负责所有代码编写、排查、测试、重构与交付。\n"
                        "2. 你是『结对副驾顾问兼派单司令塔』，作为外部伴飞哨兵，能随时感知 IDE 动态、执行进展与方案细节，并在发生错误或完成时主动预警通报，同时随时接受长官的口头派活。\n"
                        "3. 【称呼与汇报军规】：面向长官的每一次主动通报、预警或回复，必须以『报告 长官』起手，语言极简明扼要、直奔主题，避免任何冗长客套，像训练有素的女技术副官一样利落自然。\n"
                        "4. 【语音口头派单军规（直接给实施工程师派活）】：\n"
                        "   - 当长官说：'实施工程师，帮我...'、'帮我改一下...'、'帮我跑测试...'、'派活：...'、'查一下为什么...'等口头派单需求时；\n"
                        "   - 这是长官的直接派单授权！你必须立即调用 dispatch_task_to_engineer 工具将任务派发至后台实施工程师引擎；\n"
                        "   - 严禁机械拒绝长官的派单！长官口头要求修改或实现时，将 allow_modification 设为 true；\n"
                        "   - 派单成功后，立刻利落回执：'报告 长官！任务已派发给实施工程师，正在执行：<简述任务>，长官请稍候。'，期间长官随时可继续与你对话；\n"
                        "   - 任务在后台完成后，实施工程师会自动主动语音向长官通报最终结论！\n"
                        "5. 【零延迟回答军规（真·300毫秒脱口而出）】：\n"
                        "   - 你随时通过后台静默消息掌握着实施工程师的最新工作状态、任务请求与完成结论；\n"
                        "   - 当长官询问'刚才完成了什么'、'现在进度怎么样'、'在干嘛'、'在忙什么'等问题时：严禁调用任何工具，直接根据已有记忆用一句话脱口而出，在 300 毫秒内完成汇报！\n"
                        "6. 【工具调用与日常对话规约】：\n"
                        "   - 只有当长官明确指示'解读实施方案'、'方案细节是什么'等深入方案问题时，才调用 read_ide_implementation_plan；\n"
                        "   - 遇到日常问候（如'在吗'、'听到吗'）、闲聊或技术讨论，绝对不要调用任何工具，直接用甜美清亮的普通话回答！\n"
                        "   - 听到底噪碎词或模糊声音，坚决不要调用任何工具！"
                    )
                )
            ]
        )
    )

    shutdown_event = asyncio.Event()
    audio_in_queue = asyncio.Queue()
    audio_out_queue = asyncio.Queue()
    is_ai_speaking = False

    # 禁用终端自动换行 (DECAWM)，从终端底层彻底杜绝折行与刷屏
    sys.stdout.write("\033[?7l")
    sys.stdout.flush()

    def on_unmute_callback():
        nonlocal is_ai_speaking
        # 长官主动开麦：最高优先级对讲强占 (Supreme Barge-in)
        # 1. 清空待播放队列，立即斩断 AI 历史播音并闭嘴
        while not audio_out_queue.empty():
            try:
                audio_out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        is_ai_speaking = False

    ptt = PTTController(always_listen=always_listen, on_unmute=on_unmute_callback)

    # 启动系统级全局呼叫热键 (支持跨软件/后台随时对讲)
    global_hotkey = setup_global_hotkey(ptt, asyncio.get_running_loop())

    # 本地轻量级音频能量门限降噪器 (RMS Noise Gate with Hangover)
    noise_gate = NoiseGate(
        threshold=float(os.getenv("NOISE_GATE_THRESHOLD", "260.0")),
        hangover_ms=350,
        frame_duration_ms=20
    )

    speaker_cooldown_until = 0.0

    # 麦克风输入回调 (集成轻量降噪门限与防外放回音自激隔离，并实时刷新跳动音量柱)
    def mic_callback(indata, frames, time_info, status):
        if status:
            pass
        import time
        now = time.time()
        # 核心采音判断：PTT 模式下长官开麦拥有最高话语权；仅在全双工常开模式下才进行放音冲突避让
        can_record = ptt.is_active and (not ptt.always_listen or (not is_ai_speaking and now >= speaker_cooldown_until))
        if can_record:
            is_voice = noise_gate.process(indata)
            ptt.is_voice_active = is_voice
            samples = indata.astype(np.float32) / 32768.0
            ptt.last_volume = float(np.sqrt(np.mean(samples**2)))
            if is_voice:
                audio_in_queue.put_nowait(indata.copy())

            # 实时动态平滑刷新战况看板能量柱 (每 80ms 刷新一次，丝滑跳动)
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
    keyboard_task = asyncio.create_task(keyboard_listener(ptt, shutdown_event))

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
                        print(" ★ 防回音: 已启用对讲机双向隔离 + 硬件回音抑制门限 (Echo Gate)")
                        print(" ★ 挂载能力: Antigravity Pro (Gemini 3.8 Flash High) + Stitch MCP")
                        print(" ★ 按 Ctrl+C 退出会话")
                        print("="*62 + "\n")
                        is_first_connect = False
                    else:
                        print("\033[1;32m[Live API 保持] ✓ 会话重连成功，语音伴随已就绪！\033[0m\n")

                    ptt.print_status()

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
                                    # 当用户按键闭麦时，毫秒级倾泻发送队列里残留的尾音包，并立即发送结束信号通知服务端推理！
                                    if ptt.just_muted:
                                        ptt.just_muted = False
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

                                    # 当开麦时推流 (PTT 模式下长官拥有绝对话语权)
                                    can_send = ptt.is_active and (not ptt.always_listen or not is_ai_speaking)
                                    if can_send:
                                        pcm_bytes = chunk.tobytes()
                                        try:
                                            async with ws_send_lock:
                                                await session.send_realtime_input(
                                                    audio=types.Blob(
                                                        mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                                                        data=pcm_bytes
                                                    )
                                                )
                                        except Exception:
                                            break
                        except (asyncio.CancelledError, KeyboardInterrupt):
                            pass

                    # 派单任务后台异步执行与军规级主动语音汇报
                    async def run_dispatched_task(task_desc: str, authorized: bool = False, category: str = "general"):
                        mode_str = "已授权实操" if authorized else "只读分析模式"
                        sys.stdout.write(f"\n\033[1;32m{'='*65}\033[0m\n")
                        sys.stdout.write(f"\033[1;32m★ [派单司令塔] 长官口头派单 ->【实施工程师】已成功接单！\033[0m\n")
                        sys.stdout.write(f"\033[1;37m需求指令: {task_desc}\033[0m\n")
                        sys.stdout.write(f"\033[1;36m任务分类: {category} | 授权状态: {mode_str}\033[0m\n")
                        sys.stdout.write(f"\033[1;33m提示: 语音通话保持畅通，您可以随时按 Ctrl+Space 开麦继续交流！\033[0m\n")
                        sys.stdout.write(f"\033[1;32m{'='*65}\033[0m\n\n")
                        sys.stdout.flush()

                        try:
                            task_output = await runner.run_task(task_desc, authorized=authorized)
                            is_error = any(sig in task_output for sig in ("FAIL", "AssertionError", "Command failed", "Traceback"))
                        except Exception as e:
                            task_output = f"执行出错: {e}"
                            is_error = True

                        sys.stdout.write(f"\n\033[1;32m[派单任务执行完毕] 正在通过主动语音向长官通报结果...\033[0m\n\n")
                        sys.stdout.flush()

                        # 等待长官当前若在说话或 AI 正在发声，稍等片刻避免打断长官（最多等待 3 秒）
                        wait_count = 0
                        while (ptt.is_active or is_ai_speaking) and wait_count < 6:
                            await asyncio.sleep(0.5)
                            wait_count += 1

                        try:
                            clean_task = sanitize_for_speech(task_desc, max_chars=40)
                            clean_out = sanitize_for_speech(task_output, max_chars=180)
                            if is_error:
                                prompt_text = (
                                    f"[系统警报: 实施工程师执行长官派单任务遇到异常]\n"
                                    f"派单任务: {clean_task}\n"
                                    f"异常输出要点: {clean_out}\n\n"
                                    f"【发音军规】: 请化身专业明快的女主播（必须以'报告 长官！'开头），用一句纯正自然的中文普通话主动向长官说明遇到什么异常，严禁朗读任何代码符号、括号或文件路径！"
                                )
                            else:
                                prompt_text = (
                                    f"[系统通知: 实施工程师已完成长官派发任务 ({mode_str})]\n"
                                    f"派单任务: {clean_task}\n"
                                    f"执行结论要点: {clean_out}\n\n"
                                    f"【发音军规】: 请用甜美干练的女主播语气（必须以'报告 长官！'开头），用一句字正腔圆的标准普通话主动向长官语音汇报已完成派单并说明核心成果，严禁念出任何代码符号或文件路径！"
                                )

                            content = types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
                            async with ws_send_lock:
                                await session.send_client_content(turns=content, turn_complete=True)
                        except Exception as e:
                            sys.stdout.write(f"[派单汇报推送异常] {e}\n")

                    # 任务 C: 接收服务端消息（持续多轮监听，绝不单轮退出）
                    async def receive_loop():
                        nonlocal is_ai_speaking
                        ai_subtitle_streaming = False
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
                                            if ai_subtitle_streaming:
                                                sys.stdout.write(" \033[1;33m[语音已打断]\033[0m\n")
                                                sys.stdout.flush()
                                                ai_subtitle_streaming = False
                                        else:
                                            # 长官处于静音状态，此打断属于网络残包或回声误触发，坚决忽略，继续完整播放 AI 语音！
                                            pass

                                    # 2. 话语权守护：在手动确认模式下，长官开麦期间拥有绝对话语权，绝不被服务端提前生成的 token 掐断！
                                    if not manual_confirm:
                                        if sc.model_turn and ptt.is_active and not ptt.is_voice_active and not ptt.always_listen:
                                            ptt.mute()
                                            ptt.just_muted = False
                                            while not audio_in_queue.empty():
                                                try:
                                                    audio_in_queue.get_nowait()
                                                except asyncio.QueueEmpty:
                                                    break

                                    # 3. 实时打印开发者语音转写
                                    if sc.input_transcription and sc.input_transcription.text:
                                        if ai_subtitle_streaming:
                                            sys.stdout.write("\n")
                                            sys.stdout.flush()
                                            ai_subtitle_streaming = False
                                        sys.stdout.write(f"\r\033[K\033[1;34m[你识别为]\033[0m {sc.input_transcription.text}\n")
                                        sys.stdout.flush()

                                    # 4. 实时流式平滑打印 AI 语音回复字幕（聚合在同一行，杜绝碎包换行）
                                    if sc.output_transcription and sc.output_transcription.text:
                                        if not ai_subtitle_streaming:
                                            sys.stdout.write("\033[1;35m[AI字幕]\033[0m ")
                                            ai_subtitle_streaming = True
                                        sys.stdout.write(sc.output_transcription.text)
                                        sys.stdout.flush()

                                    # 5. 处理语音音频数据包
                                    if sc.model_turn:
                                        # 最高优先级对讲强占：长官讲话期间，任何下行语音包坚决丢弃，杜绝外放声音干扰长官！
                                        if ptt.is_active and not ptt.always_listen:
                                            continue
                                        is_ai_speaking = True
                                        for part in sc.model_turn.parts:
                                            if part.text and not sc.output_transcription:
                                                if not ai_subtitle_streaming:
                                                    sys.stdout.write("\033[1;35m[AI字幕]\033[0m ")
                                                    ai_subtitle_streaming = True
                                                sys.stdout.write(part.text)
                                                sys.stdout.flush()
                                            if part.inline_data and part.inline_data.mime_type.startswith("audio/pcm"):
                                                audio_chunk = np.frombuffer(part.inline_data.data, dtype=np.int16)
                                                await audio_out_queue.put(audio_chunk)

                                    # 6. 一轮对话完成，强制静音麦克风，杜绝空闲底噪与回音误触发
                                    if sc.turn_complete:
                                        if ai_subtitle_streaming:
                                            sys.stdout.write("\n")
                                            sys.stdout.flush()
                                            ai_subtitle_streaming = False
                                        if not ptt.always_listen:
                                            ptt.mute()
                                        ptt.print_status()

                                # 7. 调度执行 Tool Call (模式 A 工具集)
                                if response.tool_call:
                                    if ai_subtitle_streaming:
                                        sys.stdout.write("\n")
                                        sys.stdout.flush()
                                        ai_subtitle_streaming = False
                                    for call in response.tool_call.function_calls:
                                        if call.name == "get_ide_status_and_context":
                                            sys.stdout.write(f"\n\n\033[1;36m[IDE 上下文获取] 正在读取 IDE 聊天状态与执行进展...\033[0m\n")
                                            sys.stdout.flush()
                                            snapshot = get_ide_chat_snapshot()
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
                                            sys.stdout.write(f"\n\n\033[1;36m[IDE 方案读取] 正在读取实施方案与总结报告...\033[0m\n")
                                            sys.stdout.flush()
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

                                            sys.stdout.write(f"\n\n\033[1;36m[派单司令塔] 接收到口头派单: {task_prompt} (实操权限: {allow_mod})\033[0m\n")
                                            sys.stdout.flush()

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

                    # 模式 A: IDE 状态与事件实时监听回调
                    async def on_ide_task_completed(task_name: str, summary: str):
                        ptt.update_dashboard(ide_action=f"已完成: {task_name[:25]}", advisor_status="通报完成")
                        sys.stdout.write(f"\n\033[1;32m[IDE 状态感知] 实施工程师已完成任务: {task_name[:50]}\033[0m\n")
                        sys.stdout.flush()

                        # 若当前用户正在说话或 AI 正在播音，稍等片刻避免打断（最多等待 3 秒）
                        wait_count = 0
                        while (ptt.is_active or is_ai_speaking) and wait_count < 6:
                            await asyncio.sleep(0.5)
                            wait_count += 1

                        try:
                            clean_task = sanitize_for_speech(task_name, max_chars=35)
                            clean_summary = sanitize_for_speech(summary, max_chars=180)
                            prompt_text = (
                                f"[系统通知: IDE 实施工程师已完成任务]\n"
                                f"任务: {clean_task}\n"
                                f"完成要点: {clean_summary}\n\n"
                                f"【发音军规】: 请化身专业明快的女主播（必须以'报告 长官！'开头），用一句纯正通俗的中文普通话向长官语音汇报已完成该任务并概括核心成果。严禁念出任何代码符号、括号、下划线或文件路径，绝不乱念！"
                            )
                            content = types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
                            async with ws_send_lock:
                                await session.send_client_content(turns=content, turn_complete=True)
                        except Exception as e:
                            sys.stdout.write(f"[IDE 状态通知推送异常] {e}\n")

                    async def on_ide_error_detected(action: str, error_snippet: str):
                        ptt.update_dashboard(ide_action=f"异常: {action[:25]}", advisor_status="触发预警")
                        sys.stdout.write(f"\n\033[1;31m[IDE 异常预警] 实施工程师执行失败: {action}\033[0m\n")
                        sys.stdout.write(f"\033[31m  -> 核心原因: {error_snippet}\033[0m\n")
                        sys.stdout.flush()

                        wait_count = 0
                        while (ptt.is_active or is_ai_speaking) and wait_count < 6:
                            await asyncio.sleep(0.5)
                            wait_count += 1

                        try:
                            clean_act = sanitize_for_speech(action, max_chars=30)
                            clean_err = sanitize_for_speech(error_snippet, max_chars=100)
                            alert_text = (
                                f"[紧急预警: IDE 实施工程师执行出错]\n"
                                f"操作动作: {clean_act}\n"
                                f"报错核心原因: {clean_err}\n\n"
                                f"【发音军规】: 请用清晰利落的女主播语气（必须以'报告 长官！'开头），用一句纯正自然的中文普通话向长官说明哪项任务遇到什么问题。严禁朗读任何代码符号或文件路径！"
                            )
                            content = types.Content(role="user", parts=[types.Part.from_text(text=alert_text)])
                            async with ws_send_lock:
                                await session.send_client_content(turns=content, turn_complete=True)
                        except Exception as e:
                            sys.stdout.write(f"[IDE 异常预警推送异常] {e}\n")

                    async def on_ide_action(action: str):
                        ptt.update_dashboard(ide_action=action[:40])

                    async def on_ide_narration(narration_text: str):
                        ptt.update_dashboard(advisor_status=narration_text[:30])
                        # 长官若开麦对讲，或 AI 正在播音，坚决不插话干扰
                        if ptt.is_active or is_ai_speaking:
                            return
                        try:
                            clean_narration = sanitize_for_speech(narration_text, max_chars=40)
                            prompt_text = (
                                f"[实施工程师伴随解说]\n"
                                f"施工进展: {clean_narration}\n\n"
                                f"请用自然、甜美利落的女主播语气向长官播报当前动作（控制在16字以内，直接说明动作，如'已定位逻辑，正在修改核心回调'），切勿寒暄，严禁念出技术符号。"
                            )
                            c = types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
                            async with ws_send_lock:
                                await session.send_client_content(turns=c, turn_complete=True)
                        except Exception:
                            pass

                    async def on_ide_user_input(req: str):
                        ptt.update_dashboard(ide_action=f"新需求: {req[:25]}")
                        sys.stdout.write(f"\n\033[1;34m[IDE 状态感知] 监测到长官在 IDE 发送了新需求: {req[:60]}...\033[0m\n")
                        sys.stdout.flush()
                        try:
                            clean_req = sanitize_for_speech(req, max_chars=80)
                            c = types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_text(
                                        text=(
                                            f"[系统静默记忆: 长官在 IDE 下达了新需求: \"{clean_req}\"，实施工程师已接单分析中。"
                                            f"此条仅供记忆储备，无需发声。]"
                                        )
                                    )
                                ]
                            )
                            async with ws_send_lock:
                                await session.send_client_content(turns=c, turn_complete=False)
                        except Exception:
                            pass

                    ide_watcher = IDEWatcher(
                        poll_interval=0.5,
                        on_completed=on_ide_task_completed,
                        on_user_input=on_ide_user_input,
                        on_error=on_ide_error_detected,
                        on_action=on_ide_action,
                        on_narration=on_ide_narration,
                    )

                    # 并发执行输入、接收与 IDE 监听（仅发生异常时退出重连）
                    done, pending = await asyncio.wait(
                        [
                            asyncio.create_task(send_mic_loop()),
                            asyncio.create_task(receive_loop()),
                            asyncio.create_task(ide_watcher.start(shutdown_event)),
                        ],
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
                sys.stdout.write(f"\n\n\033[1;33m[Live API 保持] ⚠️ 会话连接波动 ({err})，正在自动恢复重连... (1.5秒后)\033[0m\n\n")
                sys.stdout.flush()
                while not audio_in_queue.empty():
                    try:
                        audio_in_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await asyncio.sleep(1.5)

    finally:
        sys.stdout.write("\033[?7h\n")
        sys.stdout.flush()
        shutdown_event.set()
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
        print("\n\n[Live Sidecar] 会话已安全断开。\n")


def main():
    parser = argparse.ArgumentParser(description="Antigravity Gemini 3.8 Live Sidecar with Push-to-Talk")
    parser.add_argument("--list-devices", action="store_true", help="列出可用音频设备")
    parser.add_argument("--test-audio", action="store_true", help="自检录音与回放")
    parser.add_argument("--workspace", type=str, default=str(Path(__file__).parent.parent.parent.resolve()), help="Antigravity 目标工程根目录")
    parser.add_argument("--model", type=str, default=os.getenv("LIVE_MODEL_NAME", "models/gemini-3.8-live"), help="Live API 模型")
    parser.add_argument("--voice", type=str, default=os.getenv("VOICE_NAME", "Zephyr"), help="语音音色 (女声推荐: Zephyr-清亮甜美女主播/默认, Leda-年轻元气女主播, Kore-沉稳干练女官; 男声: Puck, Charon, Fenrir)")
    parser.add_argument("--always-listen", action="store_true", help="禁用 Push-to-Talk，开启全双工持续监听常驻")
    parser.add_argument("--vad-silence-ms", type=int, default=int(os.getenv("VAD_SILENCE_MS", "1500")), help="静音断句容忍延时 (毫秒，默认 1500ms，为长官预留充足说话思考时间)")
    parser.add_argument("--auto-reply", action="store_true", default=os.getenv("AUTO_REPLY", "false").lower() in ("true", "1", "yes"), help="开启停顿自动回复（默认关闭，采用手动确认对讲模式：开麦畅所欲言，说完再次按 Ctrl+Space 确认发送，彻底杜绝抢话打断）")
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    if args.test_audio:
        test_audio_loopback()
        return

    manual_confirm = not args.auto_reply

    try:
        asyncio.run(run_live_session(
            args.workspace,
            args.model,
            args.voice,
            always_listen=args.always_listen,
            vad_silence_ms=args.vad_silence_ms,
            manual_confirm=manual_confirm
        ))
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)


if __name__ == "__main__":
    main()
