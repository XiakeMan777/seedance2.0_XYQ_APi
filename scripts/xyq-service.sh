#!/bin/bash
# 小云雀服务管理脚本：start | stop | restart | status | logs
#
# 用 PID 文件管理进程，日志写入 logs/xyq-seedance.log。
# 需要登录后自动常驻，请使用 deploy/com.leng.xyq-seedance.plist（launchd 用户级）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

RUN_DIR="$SCRIPT_DIR/run"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$RUN_DIR/xyq-seedance.pid"
LOG_FILE="$LOG_DIR/xyq-seedance.log"
PORT="${PORT:-8033}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

start() {
  if is_running; then
    echo "已在运行 (PID $(cat "$PID_FILE")) -> http://127.0.0.1:$PORT"
    return 0
  fi
  echo "启动小云雀服务..."
  nohup "$SCRIPT_DIR/start.sh" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 3
  if is_running; then
    echo "已启动 (PID $(cat "$PID_FILE")) -> http://127.0.0.1:$PORT"
    echo "日志：$LOG_FILE"
  else
    echo "启动失败，请查看日志：$LOG_FILE"
    tail -20 "$LOG_FILE" || true
    return 1
  fi
}

stop() {
  if ! is_running; then
    echo "未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "停止 PID $pid ..."
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    is_running || break
    sleep 0.5
  done
  if is_running; then
    echo "优雅停止超时，强制结束"
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
  echo "已停止"
}

status() {
  if is_running; then
    echo "运行中 (PID $(cat "$PID_FILE"))"
    curl -s --max-time 5 "http://127.0.0.1:$PORT/api/health" || echo "（健康检查暂不可用）"
    echo
  else
    echo "未运行"
    return 1
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) tail -f "$LOG_FILE" ;;
  *) echo "用法: $0 {start|stop|restart|status|logs}"; exit 1 ;;
esac
