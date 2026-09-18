#!/usr/bin/env bash
# 显式关闭 Agent Live（写入 STOP 旗标，禁止自动重启）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/watchdog.sh" --stop "$@"
