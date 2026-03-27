新增了前端页面，cookies轮换，账号积分查询，调试模式！


# 小云雀 (XiaoYunque) - AI视频生成自动化平台


通过 Playwright 注入 cookies，自动调用剪映网页版 (xyq.jianying.com) API 生成 AI 视频。

---

## 📋 目录

- [✨ 特性](#✨-特性)
- [🔧 技术架构](#🔧-技术架构)
- [📦 安装部署](#📦-安装部署)
- [🚀 快速开始](#🚀-快速开始)
- [🎨 Web 界面使用](#🎨-web-界面使用)
- [🌐 Web API 参考](#🌐-web-api-参考)
- [📁 项目结构](#📁-项目结构)
- [⚠️ 注意事项](#⚠️-注意事项)

---

## ✨ 特性

### 核心功能

- 🚀 **自动化视频生成** - 无需浏览器操作，通过 API 自动生成 AI 视频
- 🎨 **现代化 Web 界面** - 提供美观、易用的可视化操作界面
- 🎯 **多模型支持** - 支持 Seedance 2.0 Fast、Seedance 2.0 等多种模型
- ⚙️ **灵活配置** - 支持自定义视频时长、比例、提示词等参数

### 任务管理

- 📊 **任务状态追踪** - 实时显示任务进度（等待中/生成中/已完成/失败）
- 🔄 **多任务并发** - 支持最多 3 个任务同时运行
- 💾 **断点续跑** - 基于 SQLite 持久化存储，服务重启后可恢复
- 📈 **进度估算** - 基于时间算法的进度百分比显示

### Cookie 管理

- 🔐 **多账号支持** - 支持上传和管理多个 Cookie 账号
- 🔄 **自动切换** - 积分不足时自动切换到下一个 Cookie 账号
- 🔍 **积分查询** - 一键查询所有账号的剩余积分
- 📤 **多种上传方式** - 支持文件上传和粘贴 JSON 上传

### 调试功能

- 🔧 **调试模式** - 可开关的调试模式，跳过 AI 调用直接返回示例视频
- 📝 **模式说明** - 内置调试模式功能说明弹窗
- ✅ **快速验证** - 无需消耗积分即可测试整个工作流

### 部署方式

- 🐳 **Docker 支持** - 提供 Docker 和 Docker Compose 部署方案
- 🌐 **API 服务** - 提供完整的 REST API，支持远程调用和集成

---

## 🔧 技术架构

### 核心原理

剪映网页版的 JS 安全 SDK 会自动给所有 API 请求添加 `msToken` + `a_bogus` 签名。小云雀通过 Playwright 的 `page.evaluate(() => fetch(...))` 利用页面的签名通道，绕过签名算法的实现。

### 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.12 + Flask + SQLite |
| 自动化 | Playwright (Chromium) |
| 前端 | HTML5 + CSS3 + JavaScript (原生) |
| 容器化 | Docker + Docker Compose |

### 核心流程

```
积分检查 → 上传图片 → 安全审核 → 提交任务 → 轮询结果 → 下载视频
```

| 步骤 | 说明 |
|------|------|
| 1. 积分检查 | 检查账号是否有足够积分生成视频 |
| 2. 上传图片 | 将参考图片上传到剪映 CDN |
| 3. 安全审核 | 文本和图片内容安全检查 |
| 4. 提交任务 | 提交视频生成请求 |
| 5. 轮询结果 | 每 30 秒轮询任务状态（最多 40 次） |
| 6. 下载视频 | 视频生成完成后下载到本地 |

---

## 📦 安装部署

### 环境要求

- Python 3.12+
- Playwright (Chromium 浏览器)
- 有效的剪映 Cookie

### 方式一：直接安装

```bash
# 克隆仓库
git clone <repository-url>
cd xiaoyunque

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium
```

### 方式二：Docker 部署

```bash
# 使用 docker-compose 启动
docker-compose up -d xiaoyunque

# 查看日志
docker-compose logs -f xiaoyunque
```

### 方式三：直接运行

```bash
# 启动服务器（默认端口 8033）
python app.py

# 或使用启动脚本
chmod +x start.sh
./start.sh
```

---

## 🚀 快速开始

### 1. 准备 Cookies

1. 在浏览器登录 [xyq.jianying.com](https://xyq.jianying.com)
2. 使用 EditThisCookie 或其他 Cookie 管理插件导出 cookies
3. 将导出的 JSON 文件保存到 `cookies/` 目录下

### 2. 启动服务

```bash
python app.py
```

### 3. 访问 Web 界面

打开浏览器访问：**http://localhost:8033**

---

## 🎨 Web 界面使用

### 界面布局

```
┌─────────────────────────────────────────────────────────────────┐
│  🎬 小云雀 - 视频生成管理                    [调试模式] [检查服务] │
├─────────────────────────────────────────────────────────────────┤
│  📊 统计概览                                                       │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐        │
│  │ 等待中 │ │ 生成中 │ │ 已完成 │ │  失败  │ │Cookies │        │
│  │   0    │ │   0    │ │   0    │ │   0    │ │   1    │        │
│  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘        │
├─────────────────────────────────────────────────────────────────┤
│  [创建任务]  [任务列表]  [Cookie 管理]                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  📊 积分消耗参考                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Seedance 2.0 Fast          5 积分/秒 (生成速度更快)          │ │
│  │ Seedance 2.0 满血版        8 积分/秒 (生成质量更高)          │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  📝 提示词                                                        │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ 描述视频内容，如：阳光下的海边，一个女孩在跳舞...               │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ⚙️ 参数设置                                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ [5秒 ▼]  [横屏 16:9 ▼]  [Seedance 2.0 Fast ▼]              │ │
│  │ 预计消耗: 50 积分 (5积分/秒 × 10秒)                          │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  📤 参考图片（至少上传1张）                                         │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                                                                   │ │
│  │                    点击或拖拽图片到此处上传                        │ │
│  │                                                                   │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  [====================== 创建视频生成任务 =======================] │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

### 创建任务

1. **填写提示词** - 描述想要生成的视频内容
2. **选择时长** - 5秒 / 10秒 / 15秒
3. **选择比例** - 横屏 16:9 / 竖屏 9:16
4. **选择模型** - Seedance 2.0 Fast / Seedance 2.0 满血版
5. **上传参考图片** - 至少上传 1 张图片（支持拖拽上传）
6. **点击创建** - 提交任务

### 任务列表

- 查看所有任务的状态和进度
- 播放/下载完成的视频
- 重试失败的任务
- 删除不需要的任务
- 按状态筛选任务

### Cookie 管理

- **上传 Cookie** - 支持文件上传或粘贴 JSON
- **测试积分** - 测试单个账号的剩余积分
- **一键查询** - 批量查询所有账号积分
- **删除账号** - 移除不需要的 Cookie

### 调试模式

页面右上角有调试模式开关，适合以下场景：

- 🚀 前端界面功能测试
- 🚀 视频生成流程调试
- 🚀 在没有积分/Cookie 时演示
- 🚀 开发阶段快速验证

**注意**：调试模式开启后，所有新建任务会跳过 AI 生成，直接返回本地示例视频，不消耗积分。

---

## 🌐 Web API 参考

### 基础信息

- **Base URL**: `http://localhost:8033`
- **Content-Type**: `application/json` (部分接口使用 `multipart/form-data`)

### API 端点

#### 健康检查

```
GET /api/health
```

响应示例：
```json
{
  "status": "healthy",
  "service": "xiaoyunque-v2.1",
  "version": "2.1.0",
  "max_workers": 3,
  "running_tasks": 0,
  "cookies_count": 1,
  "debug_mode": true
}
```

#### 调试模式

```
GET /api/debug-mode
```

响应示例：
```json
{
  "status": "success",
  "debug_mode": true
}
```

```
POST /api/debug-mode
Content-Type: application/json

{"enabled": false}
```

响应示例：
```json
{
  "status": "success",
  "debug_mode": false,
  "message": "调试模式已关闭"
}
```

#### 生成视频

```
POST /api/generate-video
Content-Type: multipart/form-data
```

**请求参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| prompt | string | ✅ | 视频描述提示词 |
| duration | integer | ✅ | 视频时长：5 / 10 / 15 |
| ratio | string | ✅ | 视频比例：16:9 / 9:16 |
| model | string | ✅ | 模型：seedance-2.0-fast / seedance-2.0 |
| files | file | ✅ | 参考图片文件（至少1张） |

**请求示例**：
```bash
curl -X POST http://localhost:8033/api/generate-video \
  -F "prompt=一个美女在海边跳舞" \
  -F "duration=10" \
  -F "ratio=16:9" \
  -F "model=seedance-2.0-fast" \
  -F "files=@image1.png" \
  -F "files=@image2.png"
```

**响应示例**：
```json
{
  "status": "success",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "视频生成任务已提交 (预计需要 50 积分)",
  "required_credits": 50,
  "running_tasks": 1
}
```

#### 查询任务状态

```
GET /api/task/<task_id>
```

**响应示例**：
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "prompt": "一个美女在海边跳舞",
  "duration": 10,
  "ratio": "16:9",
  "model": "seedance-2.0-fast",
  "status": "running",
  "progress": 45,
  "video_path": null,
  "created_at": "2026-03-27T10:30:00",
  "started_at": "2026-03-27T10:30:05",
  "completed_at": null,
  "error_message": null
}
```

**任务状态值**：
| 状态 | 说明 |
|------|------|
| pending | 等待中 |
| running | 生成中 |
| success | 已完成 |
| failed | 失败 |

#### 下载视频

```
GET /api/video/<task_id>
```

**响应**：返回 MP4 文件流

#### 列出所有任务

```
GET /api/tasks?limit=100&offset=0&status=
```

**查询参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| limit | integer | 返回数量限制（默认 100） |
| offset | integer | 偏移量（默认 0） |
| status | string | 按状态筛选：pending / running / success / failed |

#### 重试任务

```
POST /api/task/<task_id>/retry
```

#### 删除任务

```
DELETE /api/task/<task_id>
```

#### 清空所有任务

```
POST /api/tasks/clear
```

#### 获取统计数据

```
GET /api/stats
```

**响应示例**：
```json
{
  "status": "success",
  "stats": {
    "pending": 1,
    "running": 1,
    "success": 5,
    "failed": 2
  },
  "total": 9,
  "running": 1,
  "cookies_count": 2
}
```

### Cookie 管理 API

#### 获取 Cookie 列表

```
GET /api/cookies
```

**响应示例**：
```json
{
  "status": "success",
  "cookies": [
    {
      "id": 1,
      "name": "账号1",
      "filename": "cookies.json",
      "path": "/path/to/cookies.json",
      "size": 1234,
      "credits": 500,
      "last_used": "2026-03-27T10:00:00",
      "status": "active"
    }
  ],
  "count": 1
}
```

#### 上传 Cookie

```
POST /api/cookies
Content-Type: multipart/form-data
```

**参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| file | file | Cookie JSON 文件 |
| name | string | Cookie 名称（可选） |

或：

```
POST /api/cookies
Content-Type: application/json

{
  "name": "账号1",
  "content": [{"name": "cookie_name", "value": "cookie_value", ...}]
}
```

#### 测试单个 Cookie

```
POST /api/cookies/<cookie_name>/test
```

#### 一键检查所有积分

```
POST /api/cookies/check-all
```

#### 删除 Cookie

```
DELETE /api/cookies/<cookie_name>
```

---

## 📁 项目结构

```
xiaoyunque/
├── xiaoyunque.py              # 核心视频生成逻辑（CLI 工具）
├── app.py                     # Flask Web API 服务器 + Web 界面
├── static/
│   └── index.html            # Web 可视化界面
├── cookies/                   # Cookie 存储目录
│   └── *.json                # Cookie 文件
├── data/                      # SQLite 数据库存储
│   └── xiaoyunque_tasks.db   # 任务数据库
├── uploads/                   # 上传文件临时存储
├── downloads/                 # 生成视频存储目录
├── requirements.txt          # Python 依赖
├── Dockerfile                 # Docker 镜像构建文件
├── docker-compose.yml         # Docker Compose 配置
├── start.sh                  # 启动脚本
└── README.md                  # 项目文档
```

---

## ⚠️ 注意事项

| 项目 | 说明 |
|------|------|
| **Cookie 有效期** | 通常 1-7 天，过期需重新导出 |
| **积分限制** | 积分不足时自动切换到下一个 Cookie |
| **文件大小** | 图片最大 50MB |
| **生成时间** | 视频生成耗时约 2-5 分钟 |
| **轮询限制** | 轮询上限 40 次（约 20 分钟） |
| **内容审核** | 提示词和图片都会经过安全审核 |
| **网络要求** | 需要能访问 xyq.jianying.com |
| **并发限制** | 最多 3 个任务同时运行 |

### 积分消耗

| 模型 | 消耗 |
|------|------|
| Seedance 2.0 Fast | 5 积分/秒 |
| Seedance 2.0 满血版 | 8 积分/秒 |

---

## 📝 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件。
