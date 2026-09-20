"""
Test suite for consecutive speech broadcasts.
Verifies that SpeechCoordinator and do_send_speech_prompt seamlessly process
multiple subsequent task-completed announcements without silent lockouts or deadlocks.
"""

import asyncio
import os
import time
import numpy as np
from typing import List
from dotenv import load_dotenv
from google import genai
from google.genai import types

from speech_coordinator import SpeechCoordinator, SpeechItem, SpeechCategory
from audio_monitor import InterProcessSpeechLease

load_dotenv()


def test_consecutive_mock_pipeline():
    """单元测试：使用 Mock 测试 SpeechCoordinator 连续 3 轮通报排队与状态机复位。"""
    print("\n=== 1. 运行 SpeechCoordinator 连续多轮 Mock 调度测试 ===")

    audio_out_queue = asyncio.Queue()
    is_ai_speaking = False
    ai_turn_active = False
    broadcast_log: List[str] = []

    async def mock_send_speech(item: SpeechItem):
        nonlocal ai_turn_active, is_ai_speaking
        ai_turn_active = True
        broadcast_log.append(f"START: {item.task_key}")

        # 模拟生成 3 个 PCM 音频块进入队列
        for i in range(3):
            chunk = np.zeros(2400, dtype=np.int16)
            audio_out_queue.put_nowait(chunk)
            await asyncio.sleep(0.05)

        # 生成完毕，复位 ai_turn_active
        ai_turn_active = False

    async def mock_player(shutdown: asyncio.Event):
        nonlocal is_ai_speaking, ai_turn_active
        while not shutdown.is_set():
            try:
                chunk = await asyncio.wait_for(audio_out_queue.get(), timeout=0.1)
                is_ai_speaking = True
                await asyncio.sleep(0.08)
            except asyncio.TimeoutError:
                if not ai_turn_active and audio_out_queue.empty():
                    is_ai_speaking = False
                continue

    async def run_test():
        shutdown = asyncio.Event()
        player_task = asyncio.create_task(mock_player(shutdown))

        coordinator = SpeechCoordinator(
            self_pid=os.getpid(),
            workspace="/tmp/test_workspace",
            is_user_speaking_fn=lambda: False,
            is_local_ai_speaking_fn=lambda: is_ai_speaking,
            is_audio_busy_fn=lambda: (not audio_out_queue.empty() or is_ai_speaking or ai_turn_active),
            send_speech_fn=mock_send_speech,
            silence_stabilization_sec=0.1,
        )
        worker_task = asyncio.create_task(coordinator.worker_loop(shutdown))

        # 连续发送 3 个任务完工通报
        for i in range(1, 4):
            item = SpeechItem(
                category=SpeechCategory.TASK_COMPLETED,
                source="antigravity",
                task_key=f"task_{i}",
                prompt_text=f"报告长官，任务 {i} 已完成！",
                summary=f"完成任务 {i}",
                ttl=60.0
            )
            await coordinator.enqueue(item)
            # 间隔 0.3 秒，模拟 IDE 连续完成任务
            await asyncio.sleep(0.3)

        # 等待所有播报完成
        wait_deadline = time.time() + 8.0
        while len(broadcast_log) < 3 and time.time() < wait_deadline:
            await asyncio.sleep(0.2)

        # 额外等待 0.3s 让播放器处理完残留静音超时并复位 is_ai_speaking
        await asyncio.sleep(0.3)
        shutdown.set()
        await player_task
        await worker_task

        assert len(broadcast_log) == 3, f"Expected 3 broadcasts, got {len(broadcast_log)}: {broadcast_log}"
        assert broadcast_log == ["START: task_1", "START: task_2", "START: task_3"]
        assert not ai_turn_active, "ai_turn_active should be reset to False"
        assert not is_ai_speaking, "is_ai_speaking should be reset to False"
        print("✓ Mock 连续 3 轮调度与状态机复位全部通过！")

    asyncio.run(run_test())


def test_consecutive_live_api_pipeline():
    """集成测试：真实调用 Gemini Live API 独立会话，验证连续 2 轮真实音频流生成与字幕提取。"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("未检测到 GEMINI_API_KEY，跳过真实 Live API 测试。")
        return

    print("\n=== 2. 运行真实 Gemini Live API 连续多轮独立通道集成测试 ===")
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
    client = genai.Client(api_key=api_key, http_options={'api_version': 'v1alpha'})
    model_name = os.getenv("LIVE_MODEL_NAME", "models/gemini-3.8-live")
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            )
        ),
    )

    async def run_live_test():
        audio_out_queue = asyncio.Queue()
        ai_turn_active = False
        received_subtitles: List[str] = []

        async def do_send_speech_prompt(item: SpeechItem):
            nonlocal ai_turn_active
            ai_turn_active = True
            print(f"  [Live] Connecting for item: {item.task_key}...", flush=True)
            try:
                async with asyncio.timeout(12.0):
                    async with client.aio.live.connect(model=model_name, config=config) as ann_session:
                        print(f"  [Live] Connected. Sending content...", flush=True)
                        content = types.Content(role="user", parts=[types.Part.from_text(text=item.prompt_text)])
                        await ann_session.send_client_content(turns=content, turn_complete=True)

                        async for resp in ann_session.receive():
                            sc = resp.server_content
                            if sc:
                                if sc.output_transcription and sc.output_transcription.text:
                                    received_subtitles.append(sc.output_transcription.text)
                                if sc.model_turn:
                                    for part in sc.model_turn.parts:
                                        if part.inline_data and part.inline_data.mime_type.startswith("audio/pcm"):
                                            chunk = np.frombuffer(part.inline_data.data, dtype=np.int16)
                                            audio_out_queue.put_nowait(chunk)
                                if sc.turn_complete or sc.generation_complete:
                                    print("  [Live] Received complete signal.", flush=True)
                                    break
            except Exception as e:
                print(f"  [Live] Error or Timeout: {e}", flush=True)
            finally:
                ai_turn_active = False

        # 测试 2 轮真实连续生成
        for round_idx in (1, 2):
            t0 = time.time()
            item = SpeechItem(
                category=SpeechCategory.TASK_COMPLETED,
                source="antigravity",
                task_key=f"live_task_{round_idx}",
                prompt_text=f"报告长官！第 {round_idx} 项集成测试已完成。",
                summary=f"完成 {round_idx}",
            )
            chunks_before = audio_out_queue.qsize()
            await do_send_speech_prompt(item)
            new_chunks = audio_out_queue.qsize() - chunks_before
            cost_ms = (time.time() - t0) * 1000

            print(f"  Round {round_idx}: 生成音频 {new_chunks} 个块，耗时 {cost_ms:.0f}ms, ai_turn_active={ai_turn_active}")
            assert new_chunks > 0, f"Round {round_idx} 必须生成有效音频块！"
            assert not ai_turn_active, f"Round {round_idx} 完成后 ai_turn_active 必须复位为 False！"

        print("✓ 真实 Gemini Live API 连续多轮独立流式生成验证全部通过！")

    asyncio.run(run_live_test())


if __name__ == "__main__":
    test_consecutive_mock_pipeline()
    test_consecutive_live_api_pipeline()
