#!/usr/bin/env bash
# ==============================================================================
# Antigravity Gemini Live 语音副驾一键启动与战况指示看守器
# ==============================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=================================================================="
echo " ★ [Antigravity Live Sidecar] 语音副驾与战况指示看板启动器"
echo "=================================================================="

# 1. 检查 Python 虚拟环境
if [ ! -d ".venv" ]; then
    echo "[环境检查] 未检测到 .venv 虚拟环境，正在通过 uv 初始化..."
    uv venv .venv --python python3.12
    source .venv/bin/activate
    uv pip install -r requirements.txt
else
    source .venv/bin/activate
fi

# 2. 检查 .env 配置文件
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
    echo "[配置检查] ✓ 已加载 tools/live-sidecar/.env"
else
    echo "[配置警告] ⚠️ 未找到 .env 文件！请确认已配置 GEMINI_API_KEY。"
fi

# 3. 检查全局热键 macOS 辅助功能权限提醒
echo "[热键提醒] 全局呼叫热键绑定: Ctrl+Space (跨软件随时对讲)"
echo "[热键提醒] 若首次在 macOS 运行，请确保终端已获得『系统设置 -> 隐私与安全性 -> 辅助功能』权限"

# 4. 运行自愈启动循环
echo "------------------------------------------------------------------"
echo " ★ 正在启动实时双工语音伴飞引擎... (按 Ctrl+C 彻底退出)"
echo "------------------------------------------------------------------"

# 捕获退出信号，确保退出或杀进程时自愈循环能彻底终止
trap "echo -e '\n[Sidecar] 收到终止信号，会话安全退出。'; exit 0" SIGINT SIGTERM

# 智能补全宿主通道参数（若命令行未显式传入 --ide）
EXTRA_ARGS=()
if [[ "$*" != *"--ide"* ]]; then
    if [ -n "$CURSOR_AGENT" ] || [ -n "$CURSOR_REQUEST_ID" ] || [ -n "$AGENT_TRANSCRIPTS" ] || [ -n "$CURSOR_WORKSPACE_LABEL" ]; then
        EXTRA_ARGS+=("--ide" "cursor")
    elif [ -n "$ANTIGRAVITY_TRAJECTORY_ID" ] || [ -n "$ANTIGRAVITY_CLI_ALIAS" ]; then
        EXTRA_ARGS+=("--ide" "antigravity")
    fi
fi

set +e
while true; do
    if [ -f ".env" ]; then
        export $(grep -v '^#' .env | xargs)
    fi
    python live_sidecar.py "${EXTRA_ARGS[@]}" "$@"
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 130 ] || [ $EXIT_CODE -eq 143 ]; then
        echo ""
        echo "[Sidecar] 长官主动中止或收到退出信号，会话已安全关闭。"
        break
    else
        echo ""
        echo "[Sidecar] ⚠️ 进程异常退出 (code: $EXIT_CODE)，2秒后自动尝试自愈重启..."
        sleep 2
    fi
done
