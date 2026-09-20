"""
Speech Coordinator & Priority Queue Module for Agent Live.
Arbitrates speech announcements from Antigravity and Cursor,
avoids interrupting system media playback, and enforces single-channel serialization.
"""

import os
import time
import asyncio
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Callable, Awaitable, Tuple
from audio_monitor import AudioPlaybackDetector, InterProcessSpeechLease


class SpeechCategory:
    ERROR = 1            # 最高优先级：IDE 错误与异常预警 (TTL: 300s)
    DISPATCH_RESULT = 2  # 高优先级：长官派单执行完毕汇报 (TTL: 300s)
    TASK_COMPLETED = 3   # 正常优先级：实施工程师任务完工通报 (TTL: 180s)
    NARRATION = 4        # 低优先级/易逝：施工过程伴随解说 (TTL: 25s, 新解说自动覆盖旧解说)


@dataclass
class SpeechItem:
    category: int
    source: str          # "antigravity" | "cursor" | "dispatch" | "system"
    task_key: str        # 用于去重的唯一键或任务名
    prompt_text: str     # 投递给 Gemini Live 会话的提示词
    summary: str         # 终端 HUD 与日志显示的摘要
    created_at: float = 0.0
    ttl: float = 60.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class SpeechCoordinator:
    """
    单通道语音排队与协同仲裁器。
    1. 进程内：串行化 Antigravity 与 Cursor 的所有语音汇报，按优先级调度，去重瞬态伴随解说；
    2. 进程间：通过 InterProcessSpeechLease 与同机其他 sidecar 实例互斥；
    3. 系统级：检测到系统正在播放媒体、音乐或会议声音时，静默暂缓播报并在队列中排队。
    """

    def __init__(
        self,
        self_pid: Optional[int] = None,
        workspace: Optional[str] = None,
        audio_detector: Optional[AudioPlaybackDetector] = None,
        lease_manager: Optional[InterProcessSpeechLease] = None,
        is_user_speaking_fn: Optional[Callable[[], bool]] = None,
        is_local_ai_speaking_fn: Optional[Callable[[], bool]] = None,
        is_audio_busy_fn: Optional[Callable[[], bool]] = None,
        send_speech_fn: Optional[Callable[[SpeechItem], Awaitable[None]]] = None,
        update_hud_fn: Optional[Callable[[str], None]] = None,
        silence_stabilization_sec: float = 0.6,
    ):
        self.self_pid = self_pid or os.getpid()
        self.workspace = workspace or ""
        self.audio_detector = audio_detector or AudioPlaybackDetector()
        self.lease_manager = lease_manager or InterProcessSpeechLease()
        self.is_user_speaking_fn = is_user_speaking_fn or (lambda: False)
        self.is_local_ai_speaking_fn = is_local_ai_speaking_fn or (lambda: False)
        self.is_audio_busy_fn = is_audio_busy_fn or (lambda: False)
        self.send_speech_fn = send_speech_fn
        self.update_hud_fn = update_hud_fn
        self.silence_stabilization_sec = silence_stabilization_sec

        self._queue: List[SpeechItem] = []
        self._lock = asyncio.Lock()
        self._is_broadcasting = False
        self._pending_play_event = asyncio.Event()
        self._last_blocked_reason: str = ""
        self._blocked_since: float = 0.0

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    async def enqueue(self, item: SpeechItem):
        """入队语音播报项，附带去重与超量修剪"""
        async with self._lock:
            # 1. 施工伴随解说 (NARRATION)：属于瞬态解说，若队列中已有解说，用最新进展覆盖旧进展
            if item.category == SpeechCategory.NARRATION:
                self._queue = [q for q in self._queue if q.category != SpeechCategory.NARRATION]
                self._queue.append(item)
            # 2. 任务完成战报 (TASK_COMPLETED)：若已有同 task_key 的项，更新之
            elif item.category == SpeechCategory.TASK_COMPLETED:
                idx = next((i for i, q in enumerate(self._queue) if q.category == item.category and q.task_key == item.task_key), None)
                if idx is not None:
                    self._queue[idx] = item
                else:
                    self._queue.append(item)
            else:
                self._queue.append(item)

            # 按优先级排序 (数值越小优先级越高，同优先级按创建时间先后)
            self._queue.sort(key=lambda x: (x.category, x.created_at))
            self._pending_play_event.set()

    def purge_stale_items(self):
        """清除队列中超时的过期项"""
        now = time.time()
        kept: List[SpeechItem] = []
        for q in self._queue:
            if now - q.created_at <= q.ttl:
                kept.append(q)
        self._queue = kept

    def cancel_transient(self):
        """长官按热键主动插话时，清理所有瞬态施工解说，并将租约释放给长官交互"""
        self._queue = [q for q in self._queue if q.category != SpeechCategory.NARRATION]
        self.lease_manager.release(self.self_pid)

    def check_can_speak(self) -> Tuple[bool, str]:
        """
        全维检测当前是否获准向长官播音。
        返回 (True, "就绪") 或 (False, 原因描述)
        """
        # 1. 长官是否正在开麦说话
        if self.is_user_speaking_fn():
            return False, "长官开麦对讲中"

        # 2. 本地 AI 是否正在播音或缓冲区有未播音频
        if self.is_local_ai_speaking_fn() or self.is_audio_busy_fn():
            return False, "本地语音播报中"

        # 3. 跨进程租约：是否有其他 sidecar 实例正在播音
        holder = self.lease_manager.get_holder()
        if holder and holder.get("pid") != self.self_pid:
            src = holder.get("source", "另一实例")
            return False, f"避让 {src} 播音"

        # 4. 系统媒体/通话声音检测
        audio_status = self.audio_detector.get_status(self_pid=self.self_pid)
        if audio_status.get("external_media_playing"):
            media_apps = audio_status.get("media_apps", [])
            app_names = "/".join(a.get("app", "媒体") for a in media_apps[:2])
            return False, f"媒体播放中({app_names})"

        return True, "就绪"

    async def worker_loop(self, shutdown_event: asyncio.Event):
        """常驻后台排队协调工作协程"""
        while not shutdown_event.is_set():
            self.purge_stale_items()

            if not self._queue:
                self._pending_play_event.clear()
                try:
                    await asyncio.wait_for(self._pending_play_event.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                continue

            # 检测当前放行门限
            can_speak, reason = self.check_can_speak()
            if not can_speak:
                now_t = time.time()
                if self._last_blocked_reason != reason:
                    self._last_blocked_reason = reason
                    self._blocked_since = now_t
                elif now_t - self._blocked_since >= 8.0:
                    # 持续阻塞超过 8 秒，打印调试信息
                    self._blocked_since = now_t
                    if self.update_hud_fn:
                        self.update_hud_fn(f"[持续待播] {reason} (队列: {len(self._queue)})")

                if self.update_hud_fn:
                    self.update_hud_fn(f"[排队待播] {reason} (队列: {len(self._queue)})")
                try:
                    await asyncio.sleep(0.4)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    break
                continue

            # 成功放行，清空阻塞计时
            self._last_blocked_reason = ""
            self._blocked_since = 0.0

            # 静音稳定期二次确认 (避免音乐刚巧微停顿或语间喘息时冒然插话)
            if self.silence_stabilization_sec > 0:
                await asyncio.sleep(self.silence_stabilization_sec)
                can_speak, reason = self.check_can_speak()
                if not can_speak:
                    continue

            # 从队列头部取出最高优先级就绪项
            async with self._lock:
                if not self._queue:
                    continue
                item = self._queue.pop(0)

            # 抢占跨进程租约锁
            acquired = self.lease_manager.acquire(
                self_pid=self.self_pid,
                source=item.source,
                workspace=self.workspace,
                ttl_sec=4.0
            )
            if not acquired:
                # 出现瞬态竞态，重新塞回队列头等待
                async with self._lock:
                    self._queue.insert(0, item)
                await asyncio.sleep(0.3)
                continue

            # 执行语音发送与播报
            self._is_broadcasting = True
            hb_task = None
            try:
                # 启动租约心跳保活
                async def heartbeat_loop():
                    while self._is_broadcasting and not shutdown_event.is_set():
                        self.lease_manager.heartbeat(self.self_pid, ttl_sec=4.0)
                        await asyncio.sleep(1.2)

                hb_task = asyncio.create_task(heartbeat_loop())

                if self.send_speech_fn:
                    await self.send_speech_fn(item)

                # 等待本地语音彻底播报完毕（设 20 秒安全硬超时，防止声卡状态偶发脱节卡死）
                wait_time = 0.0
                while (self.is_local_ai_speaking_fn() or self.is_audio_busy_fn()) and wait_time < 20.0:
                    if shutdown_event.is_set():
                        break
                    await asyncio.sleep(0.12)
                    wait_time += 0.12

                # 播报完毕，额外冷却 0.35s 防止连续播报黏连
                await asyncio.sleep(0.35)

            except Exception as err:
                print(f"[SpeechCoordinator 异常] {err}")
            finally:
                self._is_broadcasting = False
                if hb_task:
                    hb_task.cancel()
                self.lease_manager.release(self.self_pid)
