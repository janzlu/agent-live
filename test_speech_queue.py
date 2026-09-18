"""
Unit & integration tests for SpeechCoordinator, priority queue, and media audio avoidance.
"""

import os
import time
import asyncio
import tempfile
from pathlib import Path

from audio_monitor import AudioPlaybackDetector, InterProcessSpeechLease
from speech_coordinator import SpeechCoordinator, SpeechItem, SpeechCategory


def test_interprocess_lease_concurrency():
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / "test_speech.lock"
        lease = InterProcessSpeechLease(lock_path=lock_file)

        # 1. 进程 1 抢占成功
        assert lease.acquire(self_pid=1001, source="cursor", workspace="/ws1", ttl_sec=2.0)
        holder = lease.get_holder()
        assert holder and holder["pid"] == 1001
        assert holder["source"] == "cursor"

        # 2. 进程 2 抢占失败（被互斥阻塞）
        assert not lease.acquire(self_pid=1002, source="antigravity", workspace="/ws2", ttl_sec=2.0)

        # 3. 进程 1 续期
        assert lease.heartbeat(self_pid=1001, ttl_sec=5.0)

        # 4. 进程 1 释放
        assert lease.release(self_pid=1001)
        assert lease.get_holder() is None

        # 5. 进程 2 现在可成功获取
        assert lease.acquire(self_pid=1002, source="antigravity", workspace="/ws2", ttl_sec=2.0)
        assert lease.get_holder()["pid"] == 1002
        lease.release(self_pid=1002)
    print("✓ 跨进程租约互斥与协同测试通过")


async def test_speech_queue_priority_and_deduplication():
    spoken = []

    async def mock_send(item: SpeechItem):
        spoken.append(item.summary)

    coordinator = SpeechCoordinator(
        self_pid=99999,
        send_speech_fn=mock_send,
        silence_stabilization_sec=0.01,
    )

    # 投递：任务完成、旧解说、新解说、严重错误
    await coordinator.enqueue(
        SpeechItem(
            category=SpeechCategory.TASK_COMPLETED,
            source="cursor",
            task_key="task-A",
            prompt_text="完成",
            summary="任务A完成",
        )
    )
    await coordinator.enqueue(
        SpeechItem(
            category=SpeechCategory.NARRATION,
            source="antigravity",
            task_key="narrate-1",
            prompt_text="旧进展",
            summary="施工解说1",
        )
    )
    await coordinator.enqueue(
        SpeechItem(
            category=SpeechCategory.NARRATION,
            source="antigravity",
            task_key="narrate-2",
            prompt_text="新进展",
            summary="施工解说2",
        )
    )
    await coordinator.enqueue(
        SpeechItem(
            category=SpeechCategory.ERROR,
            source="cursor",
            task_key="err-1",
            prompt_text="报错",
            summary="执行报错",
        )
    )

    # 验证去重：旧解说已被新解说覆盖，队列中应只剩 3 项
    assert coordinator.queue_size == 3
    # 验证优先级排序：Error (1) -> Task Completed (3) -> Narration (4)
    categories = [q.category for q in coordinator._queue]
    assert categories == [SpeechCategory.ERROR, SpeechCategory.TASK_COMPLETED, SpeechCategory.NARRATION]
    print("✓ 队列优先级与瞬态解说自动去重测试通过")


async def test_media_playback_wait_and_queue():
    spoken = []
    hud_states = []

    is_media_playing = True

    class MockAudioDetector:
        def get_status(self, self_pid=None):
            return {
                "external_media_playing": is_media_playing,
                "media_apps": [{"app": "Music", "pid": 888}],
                "sibling_sidecars": [],
            }

    async def mock_send(item: SpeechItem):
        spoken.append(item.summary)

    def mock_hud(state: str):
        hud_states.append(state)

    shutdown = asyncio.Event()

    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / "test_speech.lock"
        lease = InterProcessSpeechLease(lock_path=lock_file)

        coordinator = SpeechCoordinator(
            self_pid=99999,
            audio_detector=MockAudioDetector(),
            lease_manager=lease,
            send_speech_fn=mock_send,
            update_hud_fn=mock_hud,
            silence_stabilization_sec=0.05,
        )

        worker_task = asyncio.create_task(coordinator.worker_loop(shutdown))

        # 1. 投递一项任务完成通报
        await coordinator.enqueue(
            SpeechItem(
                category=SpeechCategory.TASK_COMPLETED,
                source="cursor",
                task_key="test-task",
                prompt_text="完成",
                summary="战报口播",
            )
        )

        # 此时媒体正在播放中，等待 0.4s 观察
        await asyncio.sleep(0.4)
        assert len(spoken) == 0, "媒体播放期间严禁播报，必须排队！"
        assert any("媒体播放中" in s for s in hud_states), "仪表盘应提示正在排队避让媒体"

        # 2. 模拟长官暂停/关闭媒体播放
        is_media_playing = False

        # 等待静音稳定期与出队执行
        await asyncio.sleep(0.6)
        assert spoken == ["战报口播"], "媒体停止后应自动从队列有序出队播报"

        shutdown.set()
        await worker_task
    print("✓ 媒体播放静默排队与媒体暂停后自动恢复播报测试通过")


def test_stale_narration_purge():
    coordinator = SpeechCoordinator(self_pid=99999)
    # 创建一条 TTL 为 0.1s 的瞬态解说，以及一条 TTL 为 10s 的战报
    coordinator._queue.append(
        SpeechItem(
            category=SpeechCategory.NARRATION,
            source="antigravity",
            task_key="n1",
            prompt_text="",
            summary="过时解说",
            created_at=time.time() - 0.2,
            ttl=0.1,
        )
    )
    coordinator._queue.append(
        SpeechItem(
            category=SpeechCategory.TASK_COMPLETED,
            source="cursor",
            task_key="t1",
            prompt_text="",
            summary="重要战报",
            created_at=time.time(),
            ttl=10.0,
        )
    )
    coordinator.purge_stale_items()
    assert coordinator.queue_size == 1
    assert coordinator._queue[0].summary == "重要战报"
    print("✓ 队列过时瞬态解说自动剔除测试通过")


if __name__ == "__main__":
    test_interprocess_lease_concurrency()
    test_stale_narration_purge()
    asyncio.run(test_speech_queue_priority_and_deduplication())
    asyncio.run(test_media_playback_wait_and_queue())
    print("\n★ 全部单通道排队与媒体避让单元测试 100% 通过！")
