# 小云雀 (XiaoYunque) v3.0 - Docker 生产环境
FROM python:3.11-slim-bookworm

# 构建参数
ARG PLAYWRIGHT_VERSION=1.40.0

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    # 应用配置
    APP_MODULE=app_v3:app \
    PORT=8033 \
    HOST=0.0.0.0 \
    # 并发配置（单 Worker + 多线程，因为 BrowserSession/AsyncTaskManager 是单例）
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=20 \
    MAX_WORKERS=20 \
    # 浏览器配置
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HEADLESS=true \
    BROWSER_IDLE_TIMEOUT=600 \
    # 内存优化
    CHROMIUM_MEMORY_LIMIT=400 \
    # 超时配置
    TASK_TIMEOUT=1800 \
    API_TIMEOUT=60 \
    UPLOAD_TIMEOUT=120 \
    DOWNLOAD_TIMEOUT=600

# 替换为国内镜像源（解决 Debian 官方源 404/超时问题）
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    || sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list

# 第一步：安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright Chromium 运行依赖
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libxfixes3 \
    libx11-xcb1 \
    libxcb1 \
    libx11-6 \
    libxext6 \
    libexpat1 \
    libglib2.0-0 \
    libdbus-1-3 \
    libatspi2.0-0 \
    libwayland-client0 \
    # 工具
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 第二步：安装 Playwright 和 Chromium（系统依赖已安装，不需要 --with-deps）
RUN pip install playwright==${PLAYWRIGHT_VERSION} \
    && playwright install chromium

# 安装 Python 依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制应用代码
WORKDIR /app
COPY xiaoyunque_v3.py /app/
COPY app_v3.py /app/
COPY static/ /app/static/

# 创建数据目录
RUN mkdir -p /app/cookies /app/data/async-tasks /app/uploads /app/downloads \
    && chmod -R 755 /app

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# 暴露端口
EXPOSE ${PORT}

# 使用 tini 作为 init 进程，正确处理信号
ENTRYPOINT ["tini", "--"]

# 启动命令（Gunicorn 生产模式）
CMD ["sh", "-c", "gunicorn ${APP_MODULE} \
    --bind ${HOST}:${PORT} \
    --workers ${GUNICORN_WORKERS} \
    --threads ${GUNICORN_THREADS} \
    --worker-class gthread \
    --timeout ${TASK_TIMEOUT:-1800} \
    --keep-alive 5 \
    --error-logfile - \
    --log-level warning \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --graceful-timeout 30"]
