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

## 🔧 本分支改动：新增「连接本机已登录 Chrome」的可选模式

本分支以**可选增量**方式，新增「复用用户本人已登录浏览器」的接入模式；
**上游原有的多账号 Cookie 轮换等能力全部保留**，可随时切回：

- **只连接现有 Chrome**：通过 CDP 连接本机已运行、已登录的小云雀浏览器，复用其登录态；
  **不会**新启动 Chromium，也**不会**读取 / 导出任何 Cookie。
- **外部模式是单账号**：外部浏览器模式下只使用浏览器里当前登录的这一个账号，
  不参与多 Cookie 轮换、也不会自动切换到别的账号。
  ⚠️ 这只是默认模式的取舍，**上游的多账号轮换功能并未删除**：见下文
  「旧版 Cookie 文件模式」，切回去即与上游行为一致（积分不足 / 限流时自动切换下一个账号）。
- **失败明确**：连不上指定 CDP 端点时直接报错，不会偷偷另起浏览器。
- **修复视频地址解析**：小云雀的视频直链不含 .mp4 后缀，且藏在任务产物
  `artifact.content[].data` 的嵌套 JSON 字符串里；原实现的正则与字段查找都命中不了，
  会出现「state=3 已完成但取不到 mp4」而一直等到超时。现已按显式 video 字段解析。
- **不打扰用户**：不注入 Cookie、不改写 UA、不拦截资源、不关闭用户已打开的标签页。

### ⏱️ 已知问题与规避：默认 `TASK_TIMEOUT=1200` 对排队场景偏短

实测一例 5 秒 Seedance 生成：上游普通队列显示预计等待约 4.4 小时，
本地默认 `TASK_TIMEOUT=1200`（20 分钟）先到点，本地任务被标记为
`failed（任务执行超时）`，但**上游仍在正常排队，并没有失败**。

- **本地超时 ≠ 生成失败**：本地超时只停止本地轮询，不会取消上游任务。
- 上游任务不受影响，可凭 `thread_id` 继续只读查询
  （`/api/biz/v1/agent/get_thread`），出片后照常下载。
- 排队久的场景请在 `.env` 里调大 `TASK_TIMEOUT`（例如 21600 = 6 小时）。
  **本分支不修改默认值，也不删除上游行为。**
- 会员账号若在自己的界面提供「加速 / VIP 通道」，是否使用由用户本人决定；
  不要为了绕过超时自动重提，也不要切换到其它账号。

### 为什么不用 Playwright 连接

上游使用 Playwright 注入 Cookie 的方案。本机浏览器运行时暴露的 CDP 端点会拒绝
`Browser.setDownloadBehavior`（返回 `Browser context management is not supported.`），
导致 Playwright `connect_over_cdp` 直接失败。因此改用 `browser_cdp.py`：只调用确实可用的
`Target.*` / `Runtime.evaluate` / `Page.navigate`，对外接口与 Playwright 的最小用法保持兼容。

### 环境要求

- macOS，Python 3.11+（已在 3.12 验证）
- 一个**已启动并已登录小云雀**的 Chrome（默认 CDP 端点 `http://127.0.0.1:18800`）

### 安装与启动

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 前台启动（Ctrl-C 停止）
./start.sh
```

前台启动会占用当前终端；需要后台常驻请用下面的服务脚本或 launchd。

### 后台常驻（两种方式，二选一）

```bash
# 方式 A：PID 文件管理（当前登录会话内有效）
./scripts/xyq-service.sh start|stop|restart|status|logs

# 方式 B：launchd 用户级代理（登录后自动常驻，推荐）
sed "s|__PROJECT_DIR__|$(pwd)|g" deploy/com.leng.xyq-seedance.plist \
  > ~/Library/LaunchAgents/com.leng.xyq-seedance.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.leng.xyq-seedance.plist

# 停止 / 卸载
launchctl bootout gui/$(id -u)/com.leng.xyq-seedance
```

启动日志：`logs/xyq-seedance.log`（launchd 方式另见 `logs/xyq-seedance.launchd.log`）。

### 访问入口

- 管理后台：<http://127.0.0.1:8033>
- 账号状态（只读）：<http://127.0.0.1:8033/status>

服务**默认只监听 127.0.0.1**（无鉴权，切勿直接暴露公网）。手机远程访问需要自行配置
反向代理与鉴权，本仓库不提供、也不猜测公网地址。

### 只读检查登录与积分

```bash
./.venv/bin/python debug_credit.py                    # 命令行
curl -s http://127.0.0.1:8033/api/account/status      # 接口
```

两点说明：

- 小云雀 web 端 `workspace` 接口**已不再返回** `remain_credit`，且 `commerce` 积分接口需要
  `msToken` / `a_bogus` 反爬签名。因此积分改为从页面上的积分徽标只读读取；
  读不到时返回 `null`，**不会**把「字段不存在」当成 0（那会导致误判「积分不足」而无法生成）。
- 会员（如「基础会员」）**不等于**无限生成，实际额度以页面显示为准。

### 环境变量

见 `.env.example`。关键项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址，保持回环 |
| `PORT` | `8033` | 端口 |
| `MAX_WORKERS` | `1` | 并发任务数 |
| `BROWSER_CDP_URL` | `http://127.0.0.1:18800` | 现有 Chrome 的 CDP 端点 |
| `ALLOW_BROWSER_LAUNCH` | `false` | 是否允许自启独立 Chromium（默认禁止） |
| `TASK_TIMEOUT` | `1200` | 单任务最大执行时间（秒）。高峰期 Seedance 排队可达数小时，建议调到 7200-10800 |

### 配置文件 .env（自动加载）

项目根目录的 `.env` 会被 `start.sh` 自动读取（最简单的 KEY=VALUE 赋值，不执行文件内容；外部已设置的同名环境变量优先）。`.env` 已在 `.gitignore` 中，不会提交。

```bash
cp .env.example .env
# 高峰期排队较长时：
# TASK_TIMEOUT=10800
```

> 提示：本地 `TASK_TIMEOUT` 默认 1200 秒（20 分钟）。Seedance 高峰期普通队列等待可能远超 20 分钟，此时本地任务会被标记为 failed，但上游任务仍在排队、并非生成失败；调大 `TASK_TIMEOUT` 可避免误判。也可 `POST /api/settings {"task_timeout": 10800}` 在线调整（重启后以 `.env` 为准）。

### 旧版 Cookie 文件模式（可选）

只有在**没有**现有登录浏览器、明确需要独立部署时才使用：

```bash
export BROWSER_CDP_URL=''
export ALLOW_BROWSER_LAUNCH=true
# 再把导出的 cookies/*.json 放入 cookies/ 目录
```

该模式保留上游的多账号轮换逻辑，需要额外执行 `playwright install chromium`。

### 安全与隐私

- 服务不读取、不上传、不导出任何 Cookie；登录态始终留在浏览器内。
- 全部网络请求只发往 `xyq.jianying.com`，代码中没有其它第三方外发地址。
- `.gitignore` 已忽略 `cookies/`、`data/`、`logs/`、`run/`、`.env`，真实凭证不要提交。

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
