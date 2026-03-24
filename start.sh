#!/bin/bash
# 启动 xiaoyunque Web API 服务器

echo "=========================================="
echo "小云雀 (XiaoYunque) Web API 服务器"
echo "=========================================="

# 检查 Python 版本
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python 版本: $python_version"

# 检查依赖
echo "检查依赖..."
if ! python3 -c "import flask" 2>/dev/null; then
    echo "安装 Flask..."
    pip install flask werkzeug requests
fi

if ! python3 -c "import playwright" 2>/dev/null; then
    echo "安装 Playwright..."
    pip install playwright
    echo "安装 Playwright 浏览器..."
    python3 -m playwright install chromium
fi

# 检查 cookies.json
if [ ! -f "cookies.json" ]; then
    echo "警告: cookies.json 文件不存在"
    echo "请从浏览器导出小云雀平台的 cookies 并保存为 cookies.json"
    echo "或者使用现有的 cookies.json 文件"
fi

# 创建必要的目录
mkdir -p uploads downloads

# 设置环境变量
export PORT=${PORT:-6033}
export HOST=${HOST:-0.0.0.0}

echo "=========================================="
echo "启动参数:"
echo "  主机: $HOST"
echo "  端口: $PORT"
echo "  上传目录: $(pwd)/uploads"
echo "  下载目录: $(pwd)/downloads"
echo "=========================================="

# 启动服务器
echo "启动服务器..."
python3 app.py