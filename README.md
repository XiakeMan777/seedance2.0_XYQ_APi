# 小云雀 (XiaoYunque) - AI视频生成自动化

通过 Playwright 注入 cookies，自动调用小云雀平台(xyq.jianying.com) API 生成 AI 视频。

## 原理

页面的 JS 安全 SDK 自动给所有 API 请求加 `msToken` + `a_bogus` 签名，
直接用 `page.evaluate(() => fetch(...))` 走页面的签名通道。

## 安装

```bash
pip install playwright
playwright install chromium
```

## 准备

1. 在浏览器登录 `xyq.jianying.com`
2. 用 EditThisCookie 等插件导出 cookies 保存为 `cookies.json`

## 用法

```bash
# 查看配额
py xiaoyunque.py --dry-run --ref-images 2.png

# 最简用法（需要至少1张参考图）
py xiaoyunque.py --prompt "一个美女在海边跳舞" --ref-images 2.png

# 指定时长和比例
py xiaoyunque.py --prompt "夕阳下跑步的女孩" --ref-images 2.png --duration 5 --ratio 9:16

# 用 Seedance 2.0 模型 + 多张参考图
py xiaoyunque.py --prompt "参考图片中的女孩在海边" --model 2.0 --ref-images 1.png 2.png

# 竖屏15秒
py xiaoyunque.py --prompt "城市夜景延时摄影" --ref-images city.png --ratio 9:16 --duration 15
```

## 参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--prompt` | ✅ | - | 视频描述提示词 |
| `--ref-images` | ✅ | - | 参考图片路径(至少1张, 最大20MB) |
| `--duration` | - | 10 | 视频时长: 5/10/15 秒 |
| `--ratio` | - | 16:9 | 比例: 16:9(横屏) / 9:16(竖屏) / 1:1(方屏) |
| `--model` | - | fast | 模型: fast / 2.0 / 1.5 |
| `--cookies` | - | cookies.json | Cookies 文件路径 |
| `--output` | - | . | 输出目录 |
| `--dry-run` | - | - | 只查配额不提交 |

## 模型

| 代号 | 全名 | 积分消耗 |
|------|------|----------|
| fast | Seedance 2.0 Fast | 3积分/秒 |
| 2.0 | Seedance 2.0 | 5积分/秒 |
| 1.5 | Seedance 1.5 | - |

## 状态码

| state | 含义 |
|-------|------|
| 1 | 排队中 |
| 2 | 处理中（生成中） |
| 3 | 视频就绪 |
| 4 | 失败 |

## Web API 服务器

### 启动 Web API 服务器

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务器（默认端口 6033）
python app.py

# 或使用启动脚本
chmod +x start.sh
./start.sh
```

### Docker 部署

```bash
# 构建镜像
docker build -t xiaoyunque-api .

# 运行容器
docker run -d \
  -p 6033:6033 \
  -v $(pwd)/cookies.json:/app/cookies.json:ro \
  -v $(pwd)/uploads:/app/uploads \
  -v $(pwd)/downloads:/app/downloads \
  --name xiaoyunque \
  xiaoyunque-api

# 使用 docker-compose（推荐）
docker-compose up xiaoyunque
```

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `GET /api/health` | GET | 健康检查 |
| `POST /api/generate-video` | POST | 生成视频 |
| `GET /api/task/<task_id>` | GET | 获取任务状态 |
| `GET /api/video/<task_id>` | GET | 下载生成的视频 |
| `GET /api/tasks` | GET | 列出所有任务 |
| `POST /api/cleanup` | POST | 清理旧任务 |

### 生成视频 API 请求示例

```bash
curl -X POST http://localhost:6033/api/generate-video \
  -F "prompt=一个美女在海边跳舞" \
  -F "duration=10" \
  -F "ratio=16:9" \
  -F "model=seedance-2.0" \
  -F "files=@image1.png" \
  -F "files=@image2.png"
```

或使用 base64 图片：

```bash
curl -X POST http://localhost:6033/api/generate-video \
  -d "prompt=一个美女在海边跳舞" \
  -d "duration=10" \
  -d "ratio=16:9" \
  -d "model=seedance-2.0" \
  -d "images=data:image/png;base64,iVBORw0KGgo..."
```

### 响应格式

成功响应：
```json
{
  "status": "success",
  "task_id": "uuid-string",
  "message": "视频生成任务已提交",
  "task_status": "pending"
}
```

### 测试 API

```bash
# 运行测试脚本
python test_api.py
```

## 注意事项

- Cookies 有效期通常 1-7 天，过期需重新导出
- 积分不足时 API 返回 error code 11001
- 图片最大 20MB，超过会报错
- 视频生成耗时约 2-5 分钟，脚本每 30 秒轮询
- 轮询上限 40 次（约 20 分钟）
- 提示词和图片都会经过安全审核
- 需要网络能访问 xyq.jianying.com
- Web API 服务器需要有效的 cookies.json 文件
