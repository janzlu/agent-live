#!/usr/bin/env bash
# ==============================================================================
# 跨 IDE 智能感知与双轨语音副驾看守器 (Cursor & Antigravity IDE 独立看守与自愈)
# 支持参数:
#   --ide [cursor|antigravity|auto]   指定目标 IDE 专属通道
#   --workspace [PATH]               指定绑定的工作区目录
#   --restart                        安全杀掉旧会话后彻底重启
#   --stop                           仅停止当前或全部会话
# ==============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_IDE=""
WORKSPACE=""
DO_RESTART=false
DO_STOP=false
EXPLICIT_IDE=""
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ide)
            TARGET_IDE="$2"
            EXPLICIT_IDE="$2"
            shift 2
            ;;
        --workspace)
            WORKSPACE="$2"
            shift 2
            ;;
        --restart|-r)
            DO_RESTART=true
            shift
            ;;
        --stop|-s)
            DO_STOP=true
            shift
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done

# 1. 自动推断目标 IDE
if [ -z "$TARGET_IDE" ] || [ "$TARGET_IDE" == "auto" ]; then
    if [ -n "$CURSOR_AGENT" ] || [ -n "$CURSOR_REQUEST_ID" ] || [ -n "$AGENT_TRANSCRIPTS" ] || [ -n "$CURSOR_WORKSPACE_LABEL" ]; then
        TARGET_IDE="cursor"
    elif [ -n "$ANTIGRAVITY_TRAJECTORY_ID" ] || [ -n "$ANTIGRAVITY_CLI_ALIAS" ]; then
        TARGET_IDE="antigravity"
    else
        # 探测当前前台或活跃进程特征
        bundle_id="${__CFBundleIdentifier:-}"
        if [[ "${bundle_id,,}" == *"cursor"* ]]; then
            TARGET_IDE="cursor"
        elif [[ "${bundle_id,,}" == *"antigravity"* ]]; then
            TARGET_IDE="antigravity"
        elif pgrep -x "Cursor" > /dev/null 2>&1 && ! pgrep -x "Antigravity IDE" > /dev/null 2>&1; then
            TARGET_IDE="cursor"
        else
            TARGET_IDE="antigravity"
        fi
    fi
fi

# 2. 处理停止/重启逻辑
if [ "$DO_STOP" = true ] || [ "$DO_RESTART" = true ]; then
    echo "[Ensure] 正在安全停止运行中的副驾会话..."
    if [ "$DO_STOP" = true ]; then
        # 显式关闭：写 STOP 旗标，禁止看门狗自动拉起
        "$DIR/watchdog.sh" --stop
        exit 0
    fi
    # 重启：先 stop 再清旗标由后续启动逻辑拉起
    "$DIR/watchdog.sh" --stop >/dev/null 2>&1 || true
    rm -f "$DIR/.run/STOP"
    sleep 0.5
fi

# 3. 幂等性检查：判断该 IDE 专属实例是否已在运行
RUNNING_PID=$(pgrep -f "python.*live_sidecar.py.*--ide ${TARGET_IDE}" | head -n 1)
if [ -n "$RUNNING_PID" ]; then
    echo "[Ensure] ✓ 目标 IDE [${TARGET_IDE^^}] 专属副驾已在运行 (PID: ${RUNNING_PID})，无需重复拉起。"
    exit 0
fi

# 组装启动参数
START_CMD="./start.sh --ide ${TARGET_IDE}"
if [ -n "$WORKSPACE" ]; then
    START_CMD="${START_CMD} --workspace \"${WORKSPACE}\""
fi
if [ ${#PASSTHROUGH_ARGS[@]} -gt 0 ]; then
    START_CMD="${START_CMD} ${PASSTHROUGH_ARGS[*]}"
fi

# 4. 启动终端实例
if [ -t 1 ]; then
    echo "[Ensure] 在当前前台交互式终端直接拉起 [${TARGET_IDE^^}] 专属副驾..."
    cd "$DIR" && exec ./start.sh --ide "${TARGET_IDE}" ${WORKSPACE:+--workspace "${WORKSPACE}"} "${PASSTHROUGH_ARGS[@]}"
else
    # 匹配目标宿主 macOS Application 名称
    APP_NAME="Terminal"
    if [ "$TARGET_IDE" == "cursor" ]; then
        APP_NAME="Cursor"
    elif [ "$TARGET_IDE" == "antigravity" ]; then
        APP_NAME="Antigravity IDE"
    fi

    echo "[Ensure] 正在通知 ${APP_NAME} 打开专属内置终端并启动副驾..."
    osascript << EOF > /dev/null 2>&1
tell application "System Events"
    if exists (process "${APP_NAME}") then
        tell application "${APP_NAME}" to activate
        tell process "${APP_NAME}"
            click menu item "New Terminal" of menu 1 of menu bar item "Terminal" of menu bar 1
            delay 0.6
            keystroke "cd \"$DIR\" && ${START_CMD}"
            key code 36
        end tell
    else
        tell application "Terminal" to activate
        tell application "Terminal" to do script "cd \"$DIR\" && ${START_CMD}"
    end if
end tell
EOF
    echo "[Ensure] ✓ 已向 ${APP_NAME} 下发终端启动指令。"
fi
