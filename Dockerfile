# 小云雀 (XiaoYunque) Web API 服务器 Dockerfile v2.1
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=8033 \
    HOST=0.0.0.0 \
    NODE_ENV=production

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium

COPY . .

RUN mkdir -p uploads downloads data static

RUN chmod +x /app/app.py

EXPOSE 8033

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8033/api/health || exit 1

CMD ["python", "app.py"]