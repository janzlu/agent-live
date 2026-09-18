#!/usr/bin/env bash
# ==============================================================================
# Agent Live 语音会话全量/定向重启管理器
# 用法:
#   ./restart.sh          # 杀掉所有旧会话，在当前终端重启当前 IDE 专属副驾
#   ./restart.sh --all    # 杀掉所有旧会话，并在 Cursor 与 Antigravity IDE 分别拉起各自专属终端
#   ./restart.sh --ide cursor       # 仅重启 Cursor 专属副驾
#   ./restart.sh --ide antigravity  # 仅重启 Antigravity 专属副驾
# ==============================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

TARGET="current"
if [ "$1" == "--all" ] || [ "$1" == "-a" ]; then
    TARGET="all"
elif [ "$1" == "--ide" ] && [ -n "$2" ]; then
    TARGET="$2"
elif [ -n "$1" ]; then
    TARGET="$1"
fi

echo "=================================================================="
echo " ★ [Agent Live] 语音会话重启清理管理器"
echo "=================================================================="

# 1. 彻底杀掉所有残留旧进程与自愈脚本
echo "[1/3] 正在安全终止旧会话进程..."
pkill -9 -f "start.sh" 2>/dev/null || true
pkill -9 -f "python.*live_sidecar.py" 2>/dev/null || true
sleep 1

# 2. 清理音频仲裁锁与残留状态
echo "[2/3] 正在释放跨进程音频锁与 STOP 旗标..."
rm -f ~/.agent_live_audio.lock "$DIR/.run/STOP"* 2>/dev/null || true

# 3. 按目标重新拉起
echo "[3/3] 正在准备重新拉起..."
if [ "$TARGET" == "all" ]; then
    echo " -> 正在为 Cursor 与 Antigravity IDE 分别激活专属独立终端..."
    ./ensure_running.sh --ide cursor
    sleep 1
    ./ensure_running.sh --ide antigravity
    echo ""
    echo "=================================================================="
    echo " ✓ 全部重启完成！Cursor 与 Antigravity IDE 已各自拥有独立终端会话。"
    echo "=================================================================="
elif [ "$TARGET" == "cursor" ]; then
    echo " -> 正在拉起 Cursor 专属终端会话..."
    ./ensure_running.sh --ide cursor
elif [ "$TARGET" == "antigravity" ]; then
    echo " -> 正在拉起 Antigravity 专属终端会话..."
    ./ensure_running.sh --ide antigravity
else
    # 默认模式：在当前交互终端直接拉起当前 IDE 专属副驾
    if [ -t 1 ]; then
        echo " -> 正在当前终端直接启动专属语音伴飞引擎..."
        exec ./start.sh
    else
        echo " -> 当前非交互终端，正在通过智能看守器自动拉起..."
        ./ensure_running.sh
    fi
fi
