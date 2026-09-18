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

for arg in "$@"; do
    if [ "$arg" == "--help" ] || [ "$arg" == "-h" ]; then
        exec python live_sidecar.py --help
    fi
done


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

# 智能推断与提取专属 IDE 通道
CURRENT_IDE=""
for ((i=1; i<=$#; i++)); do
    if [ "${!i}" == "--ide" ]; then
        j=$((i+1))
        CURRENT_IDE="${!j}"
        break
    fi
done
if [ -z "$CURRENT_IDE" ]; then
    if [ -n "$CURSOR_AGENT" ] || [ -n "$CURSOR_REQUEST_ID" ] || [ -n "$AGENT_TRANSCRIPTS" ] || [ -n "$CURSOR_WORKSPACE_LABEL" ]; then
        CURRENT_IDE="cursor"
    elif [ -n "$ANTIGRAVITY_TRAJECTORY_ID" ] || [ -n "$ANTIGRAVITY_CLI_ALIAS" ]; then
        CURRENT_IDE="antigravity"
    else
        CURRENT_IDE="cursor"
    fi
fi

STOP_FLAG="$RUN_DIR/STOP_${CURRENT_IDE}"
GLOBAL_STOP_FLAG="$RUN_DIR/STOP"

# 显式启动时仅清理当前 IDE 的历史残留 STOP 旗标，确保全新启动且绝不影响另一 IDE
rm -f "$STOP_FLAG" 2>/dev/null || true

# 显式关闭处理：终端前台 Ctrl+C 直接写入 STOP 旗标并退出；外部信号仅在有 STOP 旗标时正常退出
_on_sigint() {
    echo -e "\n[Sidecar] 收到长官终端中断指令 (Ctrl+C)，已记录 STOP_${CURRENT_IDE} 旗标并安全退出。"
    touch "$STOP_FLAG" 2>/dev/null || true
    exit 0
}
trap _on_sigint SIGINT

_on_sigterm() {
    if [ -f "$STOP_FLAG" ] || [ -f "$GLOBAL_STOP_FLAG" ]; then
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
    EXTRA_ARGS+=("--ide" "$CURRENT_IDE")
fi

# 智能前台接管与单例防护：避免同通道进程并发
EXISTING_PIDS=$(pgrep -f "python.*live_sidecar.py.*--ide ${CURRENT_IDE}" 2>/dev/null || true)
if [ -n "$EXISTING_PIDS" ]; then
    if [ -t 1 ]; then
        echo "[前台接管] 检测到专属通道 [${CURRENT_IDE}] 已有实例运行 (PID: $EXISTING_PIDS)，正在平滑终止并由当前终端接管..."
        for p in $EXISTING_PIDS; do
            kill "$p" 2>/dev/null || true
        done
        sleep 0.8
    else
        echo "[单例互斥] 专属通道 [${CURRENT_IDE}] 已有实例 (PID: $EXISTING_PIDS) 正常运行，无需重复拉起。"
        exit 0
    fi
fi

set +e
while true; do
    if [ -f "$STOP_FLAG" ] || [ -f "$GLOBAL_STOP_FLAG" ]; then
        echo "[Sidecar] 检测到 STOP 旗标 (${CURRENT_IDE})，停止自愈循环。"
        break
    fi
    if [ -f ".env" ]; then
        export $(grep -v '^#' .env | xargs)
    fi
    python live_sidecar.py "${EXTRA_ARGS[@]}" "$@"
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 42 ]; then
        echo "[Sidecar] 检测到专属通道已有活跃实例，退出当前自愈循环以杜绝串音。"
        break
    fi
    if [ -f "$STOP_FLAG" ] || [ -f "$GLOBAL_STOP_FLAG" ]; then
        echo ""
        echo "[Sidecar] 显式关闭完成 (${CURRENT_IDE})，不再自动重启。"
        break
    fi
    # 0 / 130(Ctrl+C) / 143(SIGTERM) 仅在有 STOP 旗标时视为主动退出；否则继续自愈
    if [ $EXIT_CODE -eq 0 ] && ([ -f "$STOP_FLAG" ] || [ -f "$GLOBAL_STOP_FLAG" ]); then
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

