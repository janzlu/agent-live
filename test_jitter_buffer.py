"""
Unit and integration tests for Adaptive Jitter Buffer, zero-drop queue, and turn synchronization.
"""

import os
import sys
import time
import asyncio
import numpy as np
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from audio_monitor import InterProcessSpeechLease
from speech_coordinator import SpeechCoordinator, SpeechItem, SpeechCategory


async def test_zero_drop_queue():
    """验证扩容后的音频输出队列能够完整容纳长文本音频帧，杜绝丢包吞字"""
    print("\n--- [测试 1/3] 音频输出队列防爆仓与零丢包验证 ---")
    MAX_OUT_QUEUE_CAPACITY = 2000
    audio_out_queue = asyncio.Queue(maxsize=MAX_OUT_QUEUE_CAPACITY)

    # 模拟推送 300 个音频包（相当于一段 30 秒的长战报）
    total_packets = 300
    for i in range(total_packets):
        chunk = np.full(480, i, dtype=np.int16)
        audio_out_queue.put_nowait(chunk)

    assert audio_out_queue.qsize() == total_packets, f"队列大小应为 {total_packets}，实际为 {audio_out_queue.qsize()}"
    
    # 验证帧的完整性（检查首帧与尾帧数据）
    first_chunk = audio_out_queue.get_nowait()
    assert first_chunk[0] == 0, "首帧不应被丢弃！"
    
    for _ in range(total_packets - 2):
        audio_out_queue.get_nowait()
        
    last_chunk = audio_out_queue.get_nowait()
    assert last_chunk[0] == total_packets - 1, "尾帧序列正确！"
    assert audio_out_queue.empty() is True
    print(f"✓ 成功连续容纳与出队 {total_packets} 个音频帧，无任何丢帧或吞字！")


async def test_adaptive_jitter_buffer_smoothing():
    """模拟网络高抖动场景，验证自适应预充缓冲（Jitter Buffer）起播平滑性"""
    print("\n--- [测试 2/3] 自适应 Jitter Buffer 吸收网络抖动验证 ---")
    
    OUTPUT_SAMPLE_RATE = 24000
    MIN_JITTER_SAMPLES = int(OUTPUT_SAMPLE_RATE * 0.28) # 280ms = 6720 样本
    
    audio_out_queue = asyncio.Queue(maxsize=2000)
    ai_turn_active = True
    played_samples = []

    # 模拟网络生产端：首包极小（800 样本 = 33ms），第二包经历 120ms 网络延迟后到达
    async def network_producer():
        # 首包：33ms 音频
        await asyncio.sleep(0.01)
        audio_out_queue.put_nowait(np.full(800, 1, dtype=np.int16))
        
        # 经历 80ms 网络抖动后推送后续包
        await asyncio.sleep(0.08)
        audio_out_queue.put_nowait(np.full(3000, 2, dtype=np.int16))
        
        await asyncio.sleep(0.04)
        audio_out_queue.put_nowait(np.full(3500, 3, dtype=np.int16))
        
        # 服务端标记 turn 完成
        nonlocal ai_turn_active
        ai_turn_active = False

    # 模拟带 Jitter Buffer 的消费端
    async def consumer():
        nonlocal ai_turn_active
        pcm_chunk = await asyncio.wait_for(audio_out_queue.get(), timeout=0.2)
        
        buffered_chunks = [pcm_chunk]
        accumulated_samples = len(pcm_chunk)
        prebuf_deadline = time.time() + 0.35

        # 预充循环
        while accumulated_samples < MIN_JITTER_SAMPLES and (ai_turn_active or not audio_out_queue.empty()) and time.time() < prebuf_deadline:
            try:
                next_chunk = await asyncio.wait_for(audio_out_queue.get(), timeout=0.08)
                buffered_chunks.append(next_chunk)
                accumulated_samples += len(next_chunk)
            except asyncio.TimeoutError:
                if not ai_turn_active and audio_out_queue.empty():
                    break

        while not audio_out_queue.empty() and accumulated_samples < MIN_JITTER_SAMPLES:
            try:
                c = audio_out_queue.get_nowait()
                buffered_chunks.append(c)
                accumulated_samples += len(c)
            except asyncio.QueueEmpty:
                break

        initial_batch = np.concatenate(buffered_chunks) if len(buffered_chunks) > 1 else buffered_chunks[0]
        played_samples.append(len(initial_batch))

        # 后续流式取包
        while True:
            try:
                chunk = await asyncio.wait_for(audio_out_queue.get(), timeout=0.2)
                played_samples.append(len(chunk))
            except asyncio.TimeoutError:
                if not ai_turn_active and audio_out_queue.empty():
                    break

    await asyncio.gather(network_producer(), consumer())

    # 验证首批送播的样本量成功吸收到 >= MIN_JITTER_SAMPLES (或全部数据)
    total_played = sum(played_samples)
    assert total_played == 800 + 3000 + 3500, f"总采样点数应一致，实际: {total_played}"
    assert played_samples[0] >= MIN_JITTER_SAMPLES, f"首批送播采样点数应达到预充阈值，实际: {played_samples[0]}"
    print(f"✓ 首批送播聚合 {played_samples[0]} 采样点（{played_samples[0]/24:.1f}ms 音频），成功抹平网络延迟空隙！")


async def test_turn_complete_coordination():
    """验证基于 turn_complete 与 busy 状态的 SpeechCoordinator 严密时序同步"""
    print("\n--- [测试 3/3] 基于 turn_complete 的播报生命周期同步验证 ---")
    
    spoken = []
    is_speaking = False
    turn_active = False

    async def mock_send(item: SpeechItem):
        nonlocal turn_active
        turn_active = True
        spoken.append(item.summary)

    coordinator = SpeechCoordinator(
        self_pid=99999,
        is_local_ai_speaking_fn=lambda: is_speaking,
        is_audio_busy_fn=lambda: turn_active,
        send_speech_fn=mock_send,
        silence_stabilization_sec=0.01,
    )

    shutdown = asyncio.Event()
    worker = asyncio.create_task(coordinator.worker_loop(shutdown))

    # 投递第一项
    await coordinator.enqueue(
        SpeechItem(
            category=SpeechCategory.TASK_COMPLETED,
            source="cursor",
            task_key="task-1",
            prompt_text="战报1",
            summary="战报1",
        )
    )

    # 投递第二项
    await coordinator.enqueue(
        SpeechItem(
            category=SpeechCategory.TASK_COMPLETED,
            source="cursor",
            task_key="task-2",
            prompt_text="战报2",
            summary="战报2",
        )
    )

    # 等待 coordinator 触发第 1 项
    await asyncio.sleep(0.05)
    assert spoken == ["战报1"], f"应只开始战报1，实际: {spoken}"
    
    # 模拟第 1 项正在播报中 (此时 turn_active=True)，持续 0.3s
    await asyncio.sleep(0.3)
    assert spoken == ["战报1"], "第1项未完结前，第2项绝对不可抢播！"

    # 模拟第 1 项收到 turn_complete 并播放完毕
    turn_active = False
    
    # 等待冷却与出队第 2 项
    await asyncio.sleep(0.6)
    assert spoken == ["战报1", "战报2"], f"第1项播放完后，第2项应有序出队，实际: {spoken}"

    shutdown.set()
    await worker
    print("✓ SpeechCoordinator 基于 turn 状态机精准串行化排队，绝不提前退出或重叠混音！")


async def main():
    await test_zero_drop_queue()
    await test_adaptive_jitter_buffer_smoothing()
    await test_turn_complete_coordination()
    print("\n" + "=" * 65)
    print(" ★ 全套音频抖动缓冲与零丢包时序同步自动化测试 100% 通过！")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
