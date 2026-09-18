"""
System Audio Monitor & Cross-Process Speech Arbitration Module for Agent Live.
- Detects macOS media playback (Music, Spotify, Chrome/Safari videos, meetings) via pmset/coreaudiod.
- Coordinates multi-instance live_sidecar processes (Antigravity vs Cursor) via shared file leases.
"""

import os
import re
import time
import json
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

LOCK_FILE_PATH = Path.home() / ".agent_live_audio.lock"


class AudioPlaybackDetector:
    """
    轻量高效检测 macOS 系统是否有媒体、视频会议或其他非当前应用的声音在播放。
    利用 macOS 内核 / coreaudiod 的 pmset assertions 信息，平均耗时 ~12ms，配有短时缓存。
    """

    def __init__(self, cache_ttl: float = 0.3):
        self.cache_ttl = cache_ttl
        self._last_check_time: float = 0.0
        self._cached_result: Optional[Dict[str, Any]] = None

    def get_status(self, self_pid: Optional[int] = None) -> Dict[str, Any]:
        if self_pid is None:
            self_pid = os.getpid()

        now = time.time()
        if self._cached_result is not None and (now - self._last_check_time) < self.cache_ttl:
            return self._cached_result

        try:
            out = subprocess.check_output(
                ["/usr/bin/pmset", "-g", "assertions"],
                text=True,
                timeout=1.0,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            return {
                "external_media_playing": False,
                "media_apps": [],
                "sibling_sidecars": [],
            }

        lines = out.splitlines()
        audio_out_pids = set()

        for i, line in enumerate(lines):
            if "Resources: audio-out" in line or "Resources: audio-out " in line:
                for k in range(max(0, i - 3), i + 1):
                    m = re.search(r"Created for PID:\s*(\d+)", lines[k])
                    if m:
                        audio_out_pids.add(int(m.group(1)))
                        break

        media_apps: List[Dict[str, Any]] = []
        sibling_sidecars: List[Dict[str, Any]] = []

        for pid in audio_out_pids:
            if pid == self_pid:
                continue
            try:
                comm = subprocess.check_output(
                    ["/bin/ps", "-p", str(pid), "-o", "comm="],
                    text=True,
                    stderr=subprocess.DEVNULL
                ).strip()
                base_comm = os.path.basename(comm)
                cmd = subprocess.check_output(
                    ["/bin/ps", "-p", str(pid), "-o", "command="],
                    text=True,
                    stderr=subprocess.DEVNULL
                ).strip()

                if "live_sidecar" in cmd:
                    sibling_sidecars.append({"pid": pid, "app": "live_sidecar", "cmd": cmd})
                else:
                    media_apps.append({"pid": pid, "app": base_comm, "cmd": cmd})
            except Exception:
                pass

        result = {
            "external_media_playing": len(media_apps) > 0,
            "media_apps": media_apps,
            "sibling_sidecars": sibling_sidecars,
        }

        self._last_check_time = now
        self._cached_result = result
        return result


class InterProcessSpeechLease:
    """
    跨进程/跨工作区实时语音播报租约管理器。
    防止用户在 Cursor 与 Antigravity 中同时启动两个 live_sidecar 实例时发生重叠播报。
    """

    def __init__(self, lock_path: Optional[Path] = None):
        self.lock_path = lock_path or LOCK_FILE_PATH

    def is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def get_holder(self) -> Optional[Dict[str, Any]]:
        """获取当前持有活跃租约的进程信息，若无或已过期返回 None。"""
        if not self.lock_path.is_file():
            return None
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            expires_at = data.get("expires_at", 0)
            pid = data.get("pid", 0)
            if data.get("state") == "speaking" and expires_at > time.time():
                if self.is_pid_alive(pid):
                    return data
                else:
                    # 进程已死亡，清除僵尸锁
                    self.lock_path.unlink(missing_ok=True)
                    return None
            return None
        except Exception:
            return None

    def acquire(
        self,
        self_pid: int,
        source: str = "unknown",
        workspace: Optional[str] = None,
        ttl_sec: float = 3.5,
    ) -> bool:
        """
        尝试获取播音独占锁。若当前被其他活跃进程持有且未过期，返回 False。
        """
        holder = self.get_holder()
        now = time.time()
        if holder and holder.get("pid") != self_pid:
            return False

        payload = {
            "pid": self_pid,
            "source": source,
            "workspace": workspace or "",
            "state": "speaking",
            "updated_at": now,
            "expires_at": now + ttl_sec,
        }
        try:
            tmp_path = self.lock_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            tmp_path.replace(self.lock_path)
            return True
        except Exception:
            return False

    def heartbeat(self, self_pid: int, ttl_sec: float = 3.5) -> bool:
        """为当前持有的租约续期"""
        holder = self.get_holder()
        if not holder or holder.get("pid") != self_pid:
            return False
        now = time.time()
        holder["updated_at"] = now
        holder["expires_at"] = now + ttl_sec
        try:
            tmp_path = self.lock_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(holder), encoding="utf-8")
            tmp_path.replace(self.lock_path)
            return True
        except Exception:
            return False

    def release(self, self_pid: int) -> bool:
        """播报结束，释放独占锁"""
        if not self.lock_path.is_file():
            return True
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if data.get("pid") == self_pid:
                self.lock_path.unlink(missing_ok=True)
            return True
        except Exception:
            return False
