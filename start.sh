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
echo " ★ 正在启动实时双工语音伴飞引擎..."
echo " ★ 异常退出将自动重启；显式关闭请执行: ./stop.sh 或 ./watchdog.sh --stop"
echo "------------------------------------------------------------------"

RUN_DIR="$DIR/.run"
mkdir -p "$RUN_DIR"
STOP_FLAG="$RUN_DIR/STOP"

# 显式启动时清理历史残留 STOP 旗标，确保全新启动
rm -f "$STOP_FLAG" "$RUN_DIR/STOP_"* 2>/dev/null || true

# 显式关闭处理：终端前台 Ctrl+C 直接写入 STOP 旗标并退出；外部信号仅在有 STOP 旗标时正常退出
_on_sigint() {
    echo -e "\n[Sidecar] 收到长官终端中断指令 (Ctrl+C)，已记录 STOP 旗标并安全退出。"
    touch "$STOP_FLAG" 2>/dev/null || true
    exit 0
}
trap _on_sigint SIGINT

_on_sigterm() {
    if [ -f "$STOP_FLAG" ]; then
        echo -e "\n[Sidecar] 收到显式关闭指令 (SIGTERM)，会话安全退出。"
        exit 0
    fi
    echo -e "\n[Sidecar] 收到意外终止信号 (SIGTERM)，交由守护机制自愈重启..."
    exit 99
}
trap _on_sigterm SIGTERM

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
    if [ -f "$STOP_FLAG" ]; then
        echo "[Sidecar] 检测到 STOP 旗标，停止自愈循环。"
        break
    fi
    if [ -f ".env" ]; then
        export $(grep -v '^#' .env | xargs)
    fi
    python live_sidecar.py "${EXTRA_ARGS[@]}" "$@"
    EXIT_CODE=$?
    if [ -f "$STOP_FLAG" ]; then
        echo ""
        echo "[Sidecar] 显式关闭完成，不再自动重启。"
        break
    fi
    # 0 / 130(Ctrl+C) / 143(SIGTERM) 仅在有 STOP 旗标时视为主动退出；否则继续自愈
    if [ $EXIT_CODE -eq 0 ] && [ -f "$STOP_FLAG" ]; then
        echo ""
        echo "[Sidecar] 长官主动中止，会话已安全关闭。"
        break
    else
        echo ""
        echo "[Sidecar] ⚠️ 进程异常退出 (code: $EXIT_CODE)，2秒后自动尝试自愈重启..."
        echo "[Sidecar] （若要彻底关闭请执行 ./stop.sh）"
        sleep 2
    fi
done
