#!/usr/bin/env bash
# ==============================================================================
# Agent Live 独立看门狗（意外退出自动重启；仅显式 stop 才永久关闭）
#
# 用法:
#   ./watchdog.sh --ide cursor --workspace /path --voice Zephyr
#   ./watchdog.sh --stop
#   ./watchdog.sh --status
# ==============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

RUN_DIR="$DIR/.run"
mkdir -p "$RUN_DIR"
GLOBAL_STOP_FLAG="$RUN_DIR/STOP"

IDE="cursor"
WORKSPACE=""
VOICE="${VOICE_NAME:-Zephyr}"
DO_STOP=false
DO_STATUS=false
INTERVAL=3
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ide) IDE="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --voice) VOICE="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --stop|-s) DO_STOP=true; shift ;;
    --status) DO_STATUS=true; shift ;;
    *) PASSTHROUGH+=("$1"); shift ;;
  esac
done

STOP_FLAG="$RUN_DIR/STOP_${IDE}"
PID_FILE="$RUN_DIR/watchdog_${IDE}.pid"
ARGS_FILE="$RUN_DIR/watchdog_${IDE}.args"
LOG_FILE="$RUN_DIR/watchdog_${IDE}.log"
SIDECAR_LOG="$RUN_DIR/sidecar_${IDE}.out"

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  # INNER 的 stdout 已重定向到 LOG_FILE，避免重复写入
  if [ "${AGENT_LIVE_WATCHDOG_INNER:-}" = "1" ]; then
    echo "$msg"
  else
    echo "$msg" | tee -a "$LOG_FILE"
  fi
}

is_sidecar_alive() {
  if [ -n "$IDE" ] && [ "$IDE" != "auto" ] && [ "$IDE" != "all" ]; then
    pgrep -f "python.*live_sidecar.py.*--ide ${IDE}" >/dev/null 2>&1
  else
    pgrep -f "python.*live_sidecar.py" >/dev/null 2>&1
  fi
}

kill_stack() {
  if [ -n "$IDE" ] && [ "$IDE" != "auto" ] && [ "$IDE" != "all" ]; then
    pkill -f "python.*live_sidecar.py.*--ide ${IDE}" 2>/dev/null || true
    pkill -f "start.sh.*--ide ${IDE}" 2>/dev/null || true
  else
    pkill -f "python.*live_sidecar.py" 2>/dev/null || true
    pkill -f "$DIR/start.sh" 2>/dev/null || true
  fi
  rm -f "$HOME/.agent_live_audio.lock" 2>/dev/null || true
}

watchdog_alive() {
  [ -f "$PID_FILE" ] || return 1
  local wpid
  wpid=$(cat "$PID_FILE" 2>/dev/null || true)
  [ -n "$wpid" ] || return 1
  kill -0 "$wpid" 2>/dev/null
}

if [ "$DO_STATUS" = true ]; then
  if [ -f "$STOP_FLAG" ]; then
    echo "状态: 已显式停止 (存在 STOP 旗标)"
  elif watchdog_alive; then
    echo "状态: 看门狗运行中 (PID $(cat "$PID_FILE"))"
  else
    echo "状态: 看门狗未运行"
  fi
  if is_sidecar_alive; then
    echo "副驾: 运行中"
    pgrep -fl "python.*live_sidecar.py" || true
  else
    echo "副驾: 未运行"
  fi
  [ -f "$ARGS_FILE" ] && echo "启动参数: $(cat "$ARGS_FILE")"
  exit 0
fi

if [ "$DO_STOP" = true ]; then
  log "[STOP] 收到显式关闭指令 (${IDE})，写入 STOP 旗标并终止会话"
  touch "$STOP_FLAG"
  if [ "$IDE" == "all" ]; then
    touch "$GLOBAL_STOP_FLAG"
  fi
  kill_stack
  if watchdog_alive; then
    WPID=$(cat "$PID_FILE")
    kill "$WPID" 2>/dev/null || true
    sleep 0.3
    kill -9 "$WPID" 2>/dev/null || true
  fi
  # 清理该 IDE 的看门狗
  for p in $(pgrep -f "$DIR/watchdog.sh.*--ide ${IDE}" 2>/dev/null || true); do
    if [ "$p" != "$$" ]; then
      kill "$p" 2>/dev/null || true
    fi
  done
  rm -f "$PID_FILE"
  log "[STOP] ✓ Agent Live [${IDE}] 已显式关闭，不会自动重启"
  echo "✓ 已显式关闭 Agent Live [${IDE}]（需再次 ./watchdog.sh --ide ${IDE} 才会启动）"
  exit 0
fi

START_ARGS=(--ide "$IDE" --voice "$VOICE")
if [ -n "$WORKSPACE" ]; then
  START_ARGS+=(--workspace "$WORKSPACE")
fi
if [ ${#PASSTHROUGH[@]} -gt 0 ]; then
  START_ARGS+=("${PASSTHROUGH[@]}")
fi
printf '%q ' "${START_ARGS[@]}" > "$ARGS_FILE"

# ---- 外层：拉起脱离会话的守护循环 ----
if [ "${AGENT_LIVE_WATCHDOG_INNER:-}" != "1" ]; then
  if watchdog_alive; then
    echo "看门狗已在运行 (PID $(cat "$PID_FILE"))。"
    is_sidecar_alive && echo "副驾: up" || echo "副驾: down（看门狗会自动拉起）"
    exit 0
  fi
  rm -f "$STOP_FLAG"
  export AGENT_LIVE_WATCHDOG_INNER=1
  # 只把 inner 的 PID 写入；外层不写短命 PID，避免竞态
  nohup env AGENT_LIVE_WATCHDOG_INNER=1 "$DIR/watchdog.sh" \
    --ide "$IDE" ${WORKSPACE:+--workspace "$WORKSPACE"} --voice "$VOICE" \
    "${PASSTHROUGH[@]}" >>"$LOG_FILE" 2>&1 &
  INNER_PID=$!
  echo "$INNER_PID" > "$PID_FILE"
  disown "$INNER_PID" 2>/dev/null || true
  sleep 1
  echo "✓ 看门狗已后台启动 [${IDE}] (PID $INNER_PID)"
  echo "  日志: $LOG_FILE"
  echo "  显式关闭: $DIR/watchdog.sh --ide ${IDE} --stop  或  $DIR/stop.sh"
  exit 0
fi

# ---- 内层守护循环 ----
echo $$ > "$PID_FILE"
rm -f "$STOP_FLAG"

# macOS：忽略 SIGHUP，避免终端关闭带走守护
trap '' SIGHUP
trap 'if [ -f "$STOP_FLAG" ] || [ -f "$GLOBAL_STOP_FLAG" ]; then rm -f "$PID_FILE"; exit 0; fi' SIGTERM SIGINT

log "[BOOT] 看门狗启动 IDE=$IDE WORKSPACE=${WORKSPACE:-.} VOICE=$VOICE pid=$$"

launch_sidecar() {
  cd "$DIR" || return 1
  # 若 start.sh 已在为该 IDE 运行中，优先等待其内层循环自愈，杜绝双进程竞争
  if pgrep -f "start.sh.*--ide ${IDE}" >/dev/null 2>&1; then
    sleep 2.5
    if is_sidecar_alive; then
      return 0
    fi
  fi
  nohup ./start.sh "${START_ARGS[@]}" >>"$SIDECAR_LOG" 2>&1 &
  disown $! 2>/dev/null || true
}

while true; do
  if [ -f "$STOP_FLAG" ] || [ -f "$GLOBAL_STOP_FLAG" ]; then
    log "[STOP] 检测到 STOP 旗标 (${IDE})，看门狗退出"
    rm -f "$PID_FILE"
    exit 0
  fi

  if ! is_sidecar_alive; then
    log "[RESTART] 副驾未运行，正在拉起 start.sh ${START_ARGS[*]}"
    launch_sidecar
    sleep 3
    if is_sidecar_alive; then
      log "[RESTART] ✓ 副驾已恢复"
    else
      log "[RESTART] ⚠ 拉起后仍未检测到进程，${INTERVAL}s 后重试"
      if [ -f "$SIDECAR_LOG" ]; then
        tail -n 15 "$SIDECAR_LOG" >>"$LOG_FILE" 2>/dev/null || true
      fi
    fi
  fi

  sleep "$INTERVAL"
done
