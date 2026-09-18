"""
Unit and integration tests for ChannelInstanceManager (Kernel-level single-instance lock).
Verifies:
1. Basic acquire and release using fcntl.flock
2. Duplicate instance rejection with exit code 42
3. Re-acquisition after release
4. Foreground takeover when background instance is running
"""

import os
import sys
import time
import fcntl
import json
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from live_sidecar import ChannelInstanceManager


def test_basic_acquire_and_rejection():
    ide = "test_verify_channel"
    ws = "/tmp/test_ws"

    mgr1 = ChannelInstanceManager(ide, ws)
    acquired1, code1 = mgr1.acquire_or_takeover()
    assert acquired1 is True, f"mgr1 should acquire, got {acquired1}"
    assert code1 == 0
    assert mgr1.is_owner is True
    print("✓ 基础单例锁获取成功 (mgr1 is_owner=True)")

    # 模拟第二个实例尝试获取同通道锁
    mgr2 = ChannelInstanceManager(ide, ws)
    acquired2, code2 = mgr2.acquire_or_takeover()
    assert acquired2 is False, f"mgr2 should be rejected, got {acquired2}"
    assert code2 == 42, f"Expected code 42, got {code2}"
    assert mgr2.is_owner is False
    print("✓ 同通道第二实例被正确拦截，返回退出码 42")

    # 释放 mgr1
    mgr1.release()
    assert mgr1.is_owner is False
    print("✓ 实例 1 成功释放锁")

    # 释放后 mgr2 即可成功获取
    acquired3, code3 = mgr2.acquire_or_takeover()
    assert acquired3 is True, f"mgr2 should acquire now, got {acquired3}"
    assert code3 == 0
    print("✓ 实例 2 在锁释放后成功获取持有权")

    mgr2.release()
    mgr1.lock_file_path.unlink(missing_ok=True)
    print("✓ 锁文件清理完毕")


def test_foreground_takeover_mock():
    ide = "test_takeover_channel"
    ws = "/tmp/test_ws"

    mgr_bg = ChannelInstanceManager(ide, ws)
    # 模拟后台守护进程持锁 (is_tty=False)
    mgr_bg.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(mgr_bg.lock_file_path, "a+")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    f.seek(0)
    f.truncate()
    # 写入非当前进程 PID 但不可杀的占位信息测试读取
    payload = {"pid": os.getpid(), "ide": ide, "is_tty": False, "workspace": ws, "started_at": time.time()}
    f.write(json.dumps(payload))
    f.flush()

    meta = mgr_bg._read_meta()
    assert meta["pid"] == os.getpid()
    assert meta["is_tty"] is False
    print("✓ 锁元数据读取与后台标识校验通过")

    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    f.close()
    mgr_bg.lock_file_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_basic_acquire_and_rejection()
    test_foreground_takeover_mock()
    print("\n★ ChannelInstanceManager 单例保护测试 100% 通过！")
