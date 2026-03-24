# 小云雀 (XiaoYunque) Web API 服务器 Dockerfile
# 基于 Python 3.11 和 Playwright

FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PORT=6033 \
    HOST=0.0.0.0 \
    NODE_ENV=production

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 复制 requirements.txt 并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright 浏览器
RUN playwright install chromium

# 复制应用程序代码
COPY . .

# 创建必要的目录
RUN mkdir -p uploads downloads

# 设置权限
RUN chmod +x /app/app.py

# 暴露端口
EXPOSE 6033

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:6033/api/health || exit 1

# 启动命令
CMD ["python", "app.py"]