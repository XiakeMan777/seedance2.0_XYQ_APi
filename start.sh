#!/bin/bash
# 启动 小云雀 (XiaoYunque) Web API 服务
#
# 默认行为（推荐）：连接本机已运行、已登录的小云雀 Chrome，复用其登录态。
#   - 不新启动任何浏览器实例
#   - 不读取、不上传、不导出任何 Cookie 文件
#   - 连不上现有 Chrome 时会明确报错并退出（不会偷偷另起浏览器）
#
# 独立部署（无现有浏览器时）才设置 ALLOW_BROWSER_LAUNCH=true 并清空 BROWSER_CDP_URL。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 读取项目根目录的 .env（若存在）。只做最简单的 KEY=VALUE 赋值，不执行文件内容；
# 已存在的同名环境变量优先，不会被 .env 覆盖。
if [ -f "$SCRIPT_DIR/.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      ''|*[!A-Za-z0-9_]*) continue ;;
    esac
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < "$SCRIPT_DIR/.env"
fi

PYTHON="${PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "错误：未找到虚拟环境 Python -> $PYTHON"
  echo "请先执行："
  echo "  python3.12 -m venv .venv"
  echo "  ./.venv/bin/pip install -r requirements.txt"
  exit 1
fi

mkdir -p data uploads downloads logs cookies

# 服务本身没有鉴权，默认只监听回环地址，不要直接暴露到公网
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8033}"
export MAX_WORKERS="${MAX_WORKERS:-1}"

# 外部浏览器模式：连接本机已登录 Chrome 的 CDP 端点
export BROWSER_CDP_URL="${BROWSER_CDP_URL:-http://127.0.0.1:18800}"
# 显式开启独立 Chromium 才需要；默认关闭，避免悄悄另起浏览器
export ALLOW_BROWSER_LAUNCH="${ALLOW_BROWSER_LAUNCH:-false}"

echo "=========================================="
echo " 小云雀 (XiaoYunque) Web API 服务器"
echo "=========================================="
echo "  监听地址 : http://${HOST}:${PORT}"
echo "  监听范围 : ${HOST}（回环，仅本机可访问）"
echo "  浏览器   : 连接现有 Chrome ${BROWSER_CDP_URL:-（未设置）}"
echo "  允许自启 : ${ALLOW_BROWSER_LAUNCH}"
echo "  最大并发 : ${MAX_WORKERS}"
echo "  日志目录 : ${SCRIPT_DIR}/logs"
echo "=========================================="

exec "$PYTHON" app_v3.py
