<div align="center">

# 🎬 小云雀 (XiaoYunque) v3.0

**剪映小云雀 AI 视频生成服务的 API 化封装**

基于 Playwright 浏览器自动化，将剪映小云雀 (Seedance 2.0) 的 Web 操作流程封装为标准化 REST API 和 OpenAI 兼容接口，支持多账号轮换、并发任务队列和 Docker 一键部署。

[![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/Flask-2.3+-green.svg)](https://flask.palletsprojects.com/)
[![Playwright](https://img.shields.io/badge/Playwright-1.40-orange.svg)](https://playwright.dev/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](#english) · [功能特性](#-功能特性) · [快速开始](#-快速开始) · [API 文档](#-api-文档) · [部署指南](#-部署指南) · [架构设计](#-架构设计)

</div>

---

## ✨ 功能特性

### 🎥 视频生成
- **图生视频** — 上传参考图片 + 文字描述，AI 自动生成视频
- **双模型支持** — Seedance 2.0 Fast（5积分/秒）和 Seedance 2.0 满血版（8积分/秒）
- **灵活参数** — 支持 5/10/15 秒时长，16:9 横屏和 9:16 竖屏
- **安全审核** — 自动进行文字和图片内容审核，避免违规内容被拦截

### 🔄 多账号管理
- **Cookie 轮换** — 积分不足或限流时自动切换下一个账号
- **积分查询** — 单个/批量查询所有账号剩余积分
- **一键上传** — 支持文件上传和 JSON 粘贴两种方式

### ⚡ 高并发引擎
- **异步任务队列** — 提交/查询分离，支持最多 20 个任务并发执行
- **浏览器会话复用** — 单例 Chromium + 多 Context，按账号隔离
- **空闲自动回收** — 浏览器 Context 10 分钟无操作自动释放
- **僵尸任务检测** — 60 秒一次巡检，超时任务自动标记失败

### 🌐 双 API 体系
- **REST API** — 传统风格，功能完整，适合自用
- **OpenAI 兼容 API** — 标准 `/v1/` 路由，可直接对接现有 AI 工具链

### 🖥️ Web 管理界面
- 暗色主题，响应式布局
- 任务创建/列表/筛选/重试/取消
- Cookie 上传/测试/批量积分查询
- 运行时配置调整（无需重启）

### 🐳 Docker 部署
- 一行命令启动，自带健康检查
- Gunicorn 生产级 WSGI 服务器
- 可选 Nginx 反向代理（支持 SSL）
- 内存/CPU 资源限制，防止 OOM

---

## 🚀 快速开始

### 前置条件

1. **剪映小云雀账号** — 需要在 [xyq.jianying.com](https://xyq.jianying.com) 注册并拥有积分
2. **Cookie 文件** — 从浏览器导出小云雀的 Cookie（推荐使用 [EditThisCookie](https://www.editthiscookie.com/) 或浏览器 DevTools）

### Docker 部署（推荐）

```bash
# 克隆仓库
git clone https://github.com/XiakeMan777/seedance2.0_XYQ_APi.git
cd seedance2.0_XYQ_APi

# 将 Cookie 文件放入 cookies 目录
cp ~/downloads/my_cookie.json cookies/

# 启动服务
docker compose up -d

# 查看日志
docker compose logs -f
```

服务启动后访问 `http://localhost:8033` 即可使用 Web 管理界面。

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt
playwright install chromium

# 将 Cookie 文件放入 cookies 目录
mkdir -p cookies
cp ~/downloads/my_cookie.json cookies/

# 启动服务
python app_v3.py
```

或者使用启动脚本：

```bash
chmod +x start.sh
./start.sh
```

---

## 📖 API 文档

### OpenAI 兼容 API

完全兼容 OpenAI 视频生成 API 格式，可直接对接支持 OpenAI API 的客户端。

#### 获取模型列表

```bash
curl http://localhost:8033/v1/models
```

#### 提交视频生成任务

```bash
# Multipart 上传（推荐，支持图片文件）
curl -X POST http://localhost:8033/v1/videos/generations \
  -F "model=seedance-2.0-fast" \
  -F "prompt=阳光下的海边，一个女孩在跳舞" \
  -F "ratio=16:9" \
  -F "duration=10" \
  -F "files=@photo1.jpg" \
  -F "files=@photo2.jpg"
```

```bash
# JSON + 图片路径（服务端已有图片时使用）
curl -X POST http://localhost:8033/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2.0-fast",
    "prompt": "阳光下的海边，一个女孩在跳舞",
    "ratio": "16:9",
    "duration": 10,
    "file_paths": ["/app/uploads/photo1.jpg"]
  }'
```

#### 查询任务状态

```bash
# 轮询模式
curl http://localhost:8033/v1/videos/generations/{task_id}

# 阻塞等待模式（服务端等待任务完成后返回）
curl http://localhost:8033/v1/videos/generations/async/{task_id}?timeout=1200
```

#### 下载视频

```bash
curl -o video.mp4 http://localhost:8033/api/video/{task_id}
```

### 传统 REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/stats` | 任务统计 |
| `POST` | `/api/generate-video` | 提交视频生成（multipart） |
| `GET` | `/api/task/{id}` | 查询任务状态 |
| `POST` | `/api/task/{id}/retry` | 重试失败任务 |
| `DELETE` | `/api/task/{id}` | 删除任务 |
| `POST` | `/api/task/{id}/cancel` | 取消任务 |
| `GET` | `/api/tasks` | 任务列表（支持筛选/分页） |
| `POST` | `/api/tasks/clear` | 清空所有任务 |
| `GET` | `/api/video/{id}` | 下载视频 |
| `GET` | `/api/cookies` | Cookie 列表 |
| `POST` | `/api/cookies` | 上传 Cookie |
| `POST` | `/api/cookies/{name}/test` | 测试单个 Cookie 积分 |
| `POST` | `/api/cookies/check-all` | 批量查询积分 |
| `DELETE` | `/api/cookies/{name}` | 删除 Cookie |
| `GET` | `/api/settings` | 获取设置 |
| `POST` | `/api/settings` | 更新设置（运行时生效） |

---

## 🐳 部署指南

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8033` | 服务端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `MAX_WORKERS` | `1`（本地）/ `20`（Docker） | 最大并发任务数 |
| `TASK_TIMEOUT` | `1200`（本地）/ `7200`（Docker） | 单任务超时（秒） |
| `HEADLESS` | `true` | 无头浏览器模式 |
| `BROWSER_IDLE_TIMEOUT` | `600` | 浏览器空闲超时（秒） |
| `DEBUG_MODE` | `false` | 调试模式（跳过真实生成） |
| `MEMORY_LIMIT` | `4G` | Docker 内存限制 |
| `CPU_LIMIT` | `2.0` | Docker CPU 限制 |

### Docker Compose

```bash
# 基础部署
docker compose up -d

# 带 Nginx 反向代理
docker compose --profile with-nginx up -d
```

### 资源配置建议

| 并发数 | 内存 | CPU | 说明 |
|--------|------|-----|------|
| 1-3 | 2 GB | 1 核 | 轻量使用 |
| 5-10 | 4 GB | 2 核 | 推荐配置 |
| 10-20 | 8 GB | 4 核 | 高并发 |

> 每个 Chromium Context 约占 300-500MB 内存，请根据服务器配置合理设置 `MAX_WORKERS`。

---

## 🏗️ 架构设计

### 整体架构

```
┌─────────────┐     ┌──────────────────────────────────────────┐
│   Client     │────▶│           Flask Web Server               │
│  (Web/API)   │◀────│  ┌──────────┐  ┌─────────────────────┐  │
└─────────────┘     │  │ REST API  │  │ OpenAI Compatible   │  │
                    │  └────┬─────┘  └──────────┬──────────┘  │
                    │       │                    │              │
                    │  ┌────▼────────────────────▼──────────┐  │
                    │  │        AsyncTaskManager            │  │
                    │  │  ┌─────────────────────────────┐   │  │
                    │  │  │   ThreadPoolExecutor         │   │  │
                    │  │  │  ┌───────┐ ┌───────┐        │   │  │
                    │  │  │  │Task 1 │ │Task 2 │ ...    │   │  │
                    │  │  │  └───┬───┘ └───┬───┘        │   │  │
                    │  │  └──────┼─────────┼────────────┘   │  │
                    │  └────────┼─────────┼────────────────┘  │
                    └───────────┼─────────┼───────────────────┘
                                │         │
                    ┌───────────▼─────────▼───────────────────┐
                    │         Persistent Event Loop            │
                    │  ┌────────────────────────────────────┐  │
                    │  │        BrowserSession (单例)        │  │
                    │  │  ┌─────────┐ ┌─────────┐          │  │
                    │  │  │Context 1│ │Context 2│ ...      │  │
                    │  │  │(Cookie A)│ │(Cookie B)│          │  │
                    │  │  └────┬────┘ └────┬────┘          │  │
                    │  │       │            │                │  │
                    │  │  ┌────▼────────────▼────────────┐  │  │
                    │  │  │      Chromium (Headless)      │  │  │
                    │  │  └──────────────────────────────┘  │  │
                    │  └────────────────────────────────────┘  │
                    └─────────────────────────────────────────┘
                                │
                    ┌───────────▼─────────────────────────────┐
                    │         xyq.jianying.com                 │
                    │     (剪映小云雀 AI 视频生成平台)          │
                    └─────────────────────────────────────────┘
```

### 核心文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `app_v3.py` | ~1560 | Flask Web 服务 + 异步任务管理 + API 路由 |
| `xiaoyunque_v3.py` | ~860 | Playwright 核心引擎 + 浏览器会话管理 + API 交互 |
| `static/index.html` | ~1240 | Web 管理界面（纯 HTML/CSS/JS） |
| `Dockerfile` | ~110 | 生产环境容器构建 |
| `docker-compose.yml` | ~90 | Docker Compose 编排 |

### 关键设计决策

#### 1. 持久化 Event Loop

Playwright 的 Browser/Context/Page 对象绑定到创建它们的 event loop。Flask 是同步框架，直接 `asyncio.run()` 会在请求结束后关闭 loop，导致后续回调失败。

**解决方案**：在后台守护线程中运行一个永不关闭的 event loop，所有 Playwright 操作通过 `run_coroutine_threadsafe()` 提交到该 loop 执行。

```python
# 后台线程运行持久化 event loop
_pw_loop_thread = Thread(target=lambda: loop.run_forever(), daemon=True)

# 同步接口桥接
def run_async(coro, timeout=None):
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
```

#### 2. 浏览器单例 + 多 Context

每个 Cookie 对应一个 BrowserContext，共享同一个 Chromium 实例：

- **资源节省** — 一个浏览器进程 vs N 个浏览器进程
- **Cookie 隔离** — 不同账号的 Cookie 互不干扰
- **健康检查** — 每次获取 Context 时验证可用性，失败自动重建
- **空闲回收** — 10 分钟未使用的 Context 自动关闭释放内存

#### 3. 三层持久化

任务数据在三个层级保存，确保不丢失：

```
内存 Dict → JSON 文件 → SQLite 数据库
  (快速)    (崩溃恢复)   (持久查询)
```

#### 4. 多 Cookie 轮换

```
Cookie A → 积分不足？→ Cookie B → 限流？→ Cookie C → ... → 全部失败
```

只有「积分不足」和「被限流」会触发自动切换，其他错误（Token 过期、审核拒绝等）直接标记当前任务失败。

#### 5. API 调用走浏览器内 fetch

所有对小云雀 API 的请求都通过 `page.evaluate()` 在浏览器页面内执行 `fetch()`，而非 Python 直接请求：

- **天然携带 Cookie** — 与正常浏览器行为一致
- **绕过反爬** — 请求来自真实浏览器环境
- **无需手动管理 Token** — Cookie 自动注入

---

## 🔧 获取 Cookie

### 方法一：浏览器扩展（推荐）

1. 安装 [EditThisCookie](https://www.editthiscookie.com/) 扩展
2. 登录 [xyq.jianying.com](https://xyq.jianying.com)
3. 点击扩展图标 → 导出 Cookie
4. 将导出的 JSON 保存为 `cookies/my_account.json`

### 方法二：浏览器 DevTools

1. 登录 [xyq.jianying.com](https://xyq.jianying.com)
2. 按 `F12` 打开开发者工具
3. 切换到 `Application` → `Cookies` → `https://xyq.jianying.com`
4. 复制所有 Cookie 为 JSON 数组格式：

```json
[
  {
    "name": "sessionid",
    "value": "xxx",
    "domain": ".jianying.com",
    "path": "/",
    "expires": 1735689600,
    "httpOnly": true,
    "secure": true
  }
]
```

> ⚠️ Cookie 有效期有限，过期后需要重新导出。

---

## 📊 积分消耗参考

| 模型 | 积分/秒 | 5秒 | 10秒 | 15秒 |
|------|---------|-----|------|------|
| Seedance 2.0 Fast | 5 | 25 | 50 | 75 |
| Seedance 2.0 满血版 | 8 | 40 | 80 | 120 |

---

## ⚠️ 注意事项

1. **本项目仅供学习交流**，请勿用于商业用途
2. **Cookie 安全** — Cookie 文件包含账号凭证，请妥善保管，不要泄露
3. **API 依赖** — 项目依赖剪映小云雀内部 API，随时可能因变更而中断
4. **积分消耗** — 每次生成视频都会消耗账号积分，请注意余额
5. **内容合规** — 生成内容需符合平台审核规则，违规内容会被拦截

---

## 🛠️ 技术栈

- **后端** — Python 3.11 + Flask + Playwright
- **前端** — 原生 HTML/CSS/JS（零依赖）
- **数据库** — SQLite（WAL 模式）
- **服务器** — Gunicorn（gthread worker）
- **容器** — Docker + Docker Compose
- **浏览器** — Chromium (Headless)

---

## 📁 项目结构

```
seedance2.0_XYQ_APi/
├── app_v3.py              # Flask Web 服务 + 任务管理
├── xiaoyunque_v3.py       # Playwright 核心引擎
├── static/
│   └── index.html         # Web 管理界面
├── cookies/               # Cookie 存储目录
├── data/                  # SQLite 数据库 + 任务 JSON
├── uploads/               # 上传图片 + 临时文件
├── downloads/             # 生成的视频文件
├── logs/                  # 运行日志
├── Dockerfile             # Docker 镜像构建
├── docker-compose.yml     # Docker Compose 编排
├── requirements.txt       # Python 依赖
├── start.sh               # 本地启动脚本
└── README.md              # 本文档
```

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送分支 (`git push origin feature/amazing-feature`)
5. 提交 Pull Request

---

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。

---

<div align="center">

**如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！**

</div>
