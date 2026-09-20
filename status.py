#!/usr/bin/env python3
# ==============================================================================
# Agent Live 状态监测与诊断仪表盘 (CLI Status Dashboard) - 纯标准库实现
# ==============================================================================

import os
import sys
import time
import json
import subprocess
import datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent
RUN_DIR = DIR / ".run"

# ANSI 颜色定义
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_GREEN_BOLD = "\033[1;32m"
C_YELLOW = "\033[33m"
C_YELLOW_BOLD = "\033[1;33m"
C_BLUE = "\033[34m"
C_BLUE_BOLD = "\033[1;34m"
C_CYAN = "\033[36m"
C_CYAN_BOLD = "\033[1;36m"
C_RED = "\033[31m"
C_RED_BOLD = "\033[1;31m"
C_MAGENTA = "\033[35m"


def get_sidecar_processes():
    """获取所有运行中的 live_sidecar 进程信息"""
    procs = []
    try:
        cmd = ["ps", "-eo", "pid,ppid,tty,%cpu,%mem,etime,command"]
        out = subprocess.check_output(cmd, text=True)
        for line in out.splitlines()[1:]:
            if "live_sidecar.py" in line and not "grep" in line and not "status.py" in line:
                parts = line.strip().split(None, 6)
                if len(parts) >= 7:
                    pid, ppid, tty, cpu, mem, etime, command = parts
                    procs.append({
                        "pid": int(pid),
                        "ppid": int(ppid),
                        "tty": tty,
                        "cpu": cpu,
                        "mem": mem,
                        "etime": etime,
                        "command": command,
                    })
    except Exception:
        pass
    return procs


def get_lock_info(ide="antigravity"):
    lock_file = Path.home() / f".agent_live_{ide}.instance.lock"
    if lock_file.exists():
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def get_audio_lock():
    lock_file = Path.home() / ".agent_live_audio.lock"
    if lock_file.exists():
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"raw": lock_file.read_text().strip()}
    return None


def get_recent_logs(lines_count=6):
    log_file = DIR / "sidecar.log"
    if not log_file.exists():
        return []
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = [l.rstrip() for l in f if l.strip()]
            return all_lines[-lines_count:]
    except Exception:
        return []


def render_status_dashboard():
    width = 72
    procs = get_sidecar_processes()
    lock_antigravity = get_lock_info("antigravity")
    lock_cursor = get_lock_info("cursor")
    audio_lock = get_audio_lock()

    # 读取 .env 配置
    env_file = DIR / ".env"
    model_name = "models/gemini-3.8-live"
    voice_name = "Puck"
    http_proxy = "未配置"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("LIVE_MODEL_NAME="):
                    model_name = line.split("=", 1)[1].strip()
                elif line.startswith("VOICE_NAME="):
                    voice_name = line.split("=", 1)[1].strip()
                elif line.startswith("HTTP_PROXY="):
                    http_proxy = line.split("=", 1)[1].strip()
        except Exception:
            pass

    out = []
    out.append("")
    out.append(f"{C_CYAN_BOLD}╔══════════════════════════════════════════════════════════════════════╗{C_RESET}")
    out.append(f"{C_CYAN_BOLD}║{C_RESET}       {C_BOLD}🎙️  Agent Live 语音顾问与伴飞副驾 · 实时运行状态看板{C_RESET}        {C_CYAN_BOLD}║{C_RESET}")
    out.append(f"{C_CYAN_BOLD}╚══════════════════════════════════════════════════════════════════════╝{C_RESET}")

    # 1. 核心进程状态
    out.append(f"\n{C_BOLD}● 核心进程与宿主状态 (Process Status){C_RESET}")
    if procs:
        for p in procs:
            cmd = p["command"]
            ide_label = "Antigravity IDE" if "--ide antigravity" in cmd else ("Cursor" if "--ide cursor" in cmd else "独立终端")
            tty_str = p["tty"] if p["tty"] != "??" else "后台守护 (无 TTY)"
            
            out.append(f"  • 服务状态: {C_GREEN_BOLD}[🟢 活跃运行中 / RUNNING]{C_RESET}")
            out.append(f"  • 进程 PID: {C_CYAN_BOLD}{p['pid']}{C_RESET}")
            out.append(f"  • 宿主通道: {C_YELLOW_BOLD}{ide_label}{C_RESET} (终端 TTY: {C_CYAN}{tty_str}{C_RESET})")
            out.append(f"  • 资源消耗: CPU {C_BOLD}{p['cpu']}%{C_RESET} | 内存 {C_BOLD}{p['mem']}%{C_RESET}")
            out.append(f"  • 累计运行: {C_BOLD}{p['etime']}{C_RESET}")
    else:
        out.append(f"  • 服务状态: {C_RED_BOLD}[🔴 未运行 / STOPPED]{C_RESET}")
        out.append(f"  • 提示说明: 当前没有检测到运行中的 live_sidecar 进程。")

    # 2. 绑定工作区 (Grounding Workspace)
    out.append(f"\n{C_BOLD}● 当前绑定工作区 (Active Workspace){C_RESET}")
    active_ws = None
    if lock_antigravity and lock_antigravity.get("workspace"):
        active_ws = lock_antigravity.get("workspace")
    elif lock_cursor and lock_cursor.get("workspace"):
        active_ws = lock_cursor.get("workspace")

    if active_ws:
        ws_path = Path(active_ws)
        out.append(f"  • 工程名称: {C_GREEN_BOLD}{ws_path.name}{C_RESET}")
        out.append(f"  • 物理路径: {C_DIM}{active_ws}{C_RESET}")
    else:
        out.append(f"  • 工程绑定: {C_DIM}当前工作区遵循 IDE 活跃前台窗口{C_RESET}")

    # 3. 音频外设与对讲状态
    out.append(f"\n{C_BOLD}● 音频调度与交互状态 (Audio & PTT){C_RESET}")
    out.append(f"  • 全局呼叫热键: {C_YELLOW_BOLD}Ctrl + Space{C_RESET} (系统级 Push-to-Talk 对讲)")
    if audio_lock:
        out.append(f"  • 声卡播音租约: {C_YELLOW_BOLD}[占线播音中]{C_RESET} {audio_lock}")
    else:
        out.append(f"  • 声卡播音租约: {C_GREEN}[空闲待命 / Idle]{C_RESET} (扬声器随时可发声)")

    # 4. 云端多模态配置
    out.append(f"\n{C_BOLD}● 引擎与模型通道 (Model & Network){C_RESET}")
    out.append(f"  • 实时多模态模型: {C_CYAN}{model_name}{C_RESET}")
    out.append(f"  • 播音音色名称:   {C_CYAN}{voice_name}{C_RESET} (北方官话播音腔)")
    out.append(f"  • 网络代理通道:   {C_DIM}{http_proxy}{C_RESET}")

    # 5. 最近事件与交互流水
    recent_logs = get_recent_logs(5)
    if recent_logs:
        out.append(f"\n{C_BOLD}● 最近事件流水 (Recent Logs){C_RESET}")
        for log in recent_logs:
            log_clean = log.replace("\033[K", "")
            out.append(f"  {C_DIM}›{C_RESET} {log_clean}")

    # 6. 快捷操作提示
    out.append(f"\n{C_BOLD}● 快捷操作指令 (Actions){C_RESET}")
    if procs:
        proc_tty = procs[0]["tty"]
        out.append(f"  • 查看当前实时动态 HUD:  在 IDE 终端标签栏点击切换至 {C_CYAN}{proc_tty}{C_RESET} 终端")
    out.append(f"  • 迁移 HUD 至当前终端:    {C_GREEN_BOLD}agent-live restart{C_RESET} (或 {DIR}/restart.sh)")
    out.append(f"  • 安全关闭语音副驾:      {C_RED_BOLD}agent-live stop{C_RESET}    (或 {DIR}/stop.sh)")
    out.append(f"  • 持续动态刷新监控:      {C_CYAN_BOLD}agent-live status -w{C_RESET}")
    out.append("")

    print("\n".join(out))


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-w", "--watch"):
        try:
            while True:
                os.system("clear")
                render_status_dashboard()
                time.sleep(2)
        except KeyboardInterrupt:
            print("\n已退出状态监控。")
    else:
        render_status_dashboard()


if __name__ == "__main__":
    main()
