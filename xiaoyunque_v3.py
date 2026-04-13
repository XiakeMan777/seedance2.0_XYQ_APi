# -*- coding: utf-8 -*-
"""
小云雀 (XiaoYunque) v3.0 - 优化版核心引擎
优化内容：
- 异步架构（asyncio）
- 浏览器会话复用（单例浏览器 + 多 Context + 空闲超时）
- 指数退避轮询
- 多策略视频 URL 提取
- 结构化错误码
- 多层配置系统
- 多账号 Token 管理
"""

import asyncio
import json
import time
import uuid
import os
import mimetypes
import base64
import re
import html as _html
import argparse
import urllib.request
import urllib.error
import traceback
import random
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from enum import Enum

from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

# ==================== 配置系统 ====================

@dataclass
class Config:
    """多层配置：环境变量 > 默认值"""
    cookies_dir: str = os.environ.get('COOKIES_DIR', 'cookies')
    output_dir: str = os.environ.get('OUTPUT_DIR', 'downloads')
    max_image_size: int = int(os.environ.get('MAX_IMAGE_SIZE', 20 * 1024 * 1024))
    page_load_timeout: int = int(os.environ.get('PAGE_LOAD_TIMEOUT', 30))
    api_timeout: int = int(os.environ.get('API_TIMEOUT', 60))
    upload_timeout: int = int(os.environ.get('UPLOAD_TIMEOUT', 120))
    download_timeout: int = int(os.environ.get('DOWNLOAD_TIMEOUT', 600))
    browser_idle_timeout: int = int(os.environ.get('BROWSER_IDLE_TIMEOUT', 600))
    app_id: str = os.environ.get('APP_ID', '795647')
    headless: bool = os.environ.get('HEADLESS', 'true').lower() == 'true'
    # v4: API 调用最小间隔
    min_api_interval: float = float(os.environ.get('MIN_API_INTERVAL', '3.0'))

config = Config()

# ==================== 每账号 API 限速 ====================

class PerAccountRateLimiter:
    """按账号的 API 请求限速器 - 无锁版本，适用于多线程 async 环境"""

    def __init__(self, min_interval: float = 3.0):
        self._last_request_time: dict = {}
        self._min_interval = min_interval

    async def wait_if_needed(self, cookie_name: str):
        # 读取上次请求时间（Python dict 操作在单操作级别是原子的）
        last_time = self._last_request_time.get(cookie_name, 0)
        elapsed = time.time() - last_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        # 写回时间戳（多线程并发时可能有少量竞争，但不影响正确性）
        self._last_request_time[cookie_name] = time.time()

    def record_request(self, cookie_name: str):
        self._last_request_time[cookie_name] = time.time()

rate_limiter = PerAccountRateLimiter(min_interval=config.min_api_interval)

# ==================== 错误码体系 ====================

class ErrorCode(Enum):
    SUCCESS = (0, '成功', 200)
    TOKEN_EXPIRED = (-2002, 'Token已失效', 401)
    INSUFFICIENT_CREDITS = (-2009, '积分不足', 402)
    PARAMS_INVALID = (-2000, '请求参数非法', 400)
    REQUEST_FAILED = (-2001, '请求失败', 500)
    FILE_INVALID = (-2003, '文件URL非法', 400)
    FILE_TOO_LARGE = (-2004, '文件超出大小限制', 400)
    CONTENT_FILTERED = (-2006, '内容审核未通过', 400)
    VIDEO_FAILED = (-2008, '视频生成失败', 500)
    BROWSER_ERROR = (-2010, '浏览器异常', 500)
    TIMEOUT = (-2011, '操作超时', 504)
    RATE_LIMITED = (-2012, '操作过于频繁', 429)

class APIException(Exception):
    def __init__(self, code: ErrorCode, detail: str = ''):
        self.code = code
        self.detail = detail
        self.message = f'{code.value[1]}: {detail}' if detail else code.value[1]
        super().__init__(self.message)

def log(msg):
    t = time.strftime('%H:%M:%S')
    print(f'[{t}] {msg}', flush=True)

# ==================== 模型映射 ====================

MODELS = {
    'fast': 'seedance2.0_fast_direct',
    '2.0': 'seedance2.0_direct',
}

MODEL_LABELS = {
    'seedance2.0_fast_direct': 'Seedance 2.0 Fast (5积分/秒)',
    'seedance2.0_direct': 'Seedance 2.0 (8积分/秒)',
}

MODEL_CREDITS_PER_SEC = {
    'fast': 5,
    '2.0': 8,
}

# ==================== Token 管理 ====================

def parse_tokens(authorization: str) -> List[str]:
    """解析 Authorization header，支持逗号分隔多 token"""
    if not authorization:
        return []
    token_str = authorization.replace('Bearer ', '').replace('bearer ', '')
    return [t.strip() for t in token_str.split(',') if t.strip()]

def get_token_files() -> List[str]:
    """获取 cookies 目录下的所有 JSON 文件"""
    if not os.path.exists(config.cookies_dir):
        os.makedirs(config.cookies_dir, exist_ok=True)
        return []
    return sorted([f for f in os.listdir(config.cookies_dir) if f.endswith('.json')])

def load_cookies(path: str) -> list:
    if not os.path.exists(path):
        raise FileNotFoundError(f'Cookies文件不存在: {path}')
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    cleaned = []
    for c in raw:
        clean = {}
        for k in ['name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure']:
            if k == 'expires':
                v = c.get('expirationDate') or c.get('expires')
                if v is not None:
                    clean['expires'] = v
            elif k in c and c[k] is not None:
                clean[k] = c[k]
        cleaned.append(clean)
    if not cleaned:
        raise ValueError('Cookies文件为空或格式错误')
    return cleaned

def select_cookie_file() -> Optional[str]:
    """随机选择一个 cookie 文件"""
    files = get_token_files()
    if not files:
        return None
    return os.path.join(config.cookies_dir, random.choice(files))

# ==================== 浏览器会话管理 ====================

class BrowserSession:
    """单例浏览器 + 多 Context 会话管理"""
    
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.contexts: Dict[str, BrowserContext] = {}
        self.context_last_used: Dict[str, float] = {}
        self._launching = False
    
    async def ensure_browser(self):
        """懒启动浏览器，防止并发重复启动"""
        if self.browser and self.browser.is_connected():
            return

        # 如果正在启动，等待完成
        if self._launching:
            for _ in range(300):
                await asyncio.sleep(0.1)
                if self.browser and self.browser.is_connected():
                    return
                if not self._launching:
                    break
            if self.browser and self.browser.is_connected():
                return

        self._launching = True
        try:
            if not self.playwright:
                log('[*] 启动 Playwright...')
                self.playwright = await async_playwright().start()
            if not self.browser or not self.browser.is_connected():
                log('[*] 启动 Chromium...')
                self.browser = await self.playwright.chromium.launch(
                    headless=config.headless,
                    args=['--no-sandbox', '--disable-web-security']
                )
                log('[Browser] Chromium 已启动')
        except Exception as e:
            log(f'[ERROR] 浏览器启动失败: {e}')
            raise
        finally:
            self._launching = False

    async def get_context(self, cookie_file: str) -> BrowserContext:
        """获取或创建 Context，复用已存在的会话"""
        await self.ensure_browser()
        
        if cookie_file in self.contexts:
            ctx = self.contexts[cookie_file]
            try:
                # 健康检查：带超时保护，防止 new_page 卡死
                test_page = await asyncio.wait_for(ctx.new_page(), timeout=10)
                try:
                    await asyncio.wait_for(test_page.close(), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass  # close 失败不影响后续
                self.context_last_used[cookie_file] = time.time()
                return ctx
            except (asyncio.TimeoutError, Exception) as e:
                log(f'[Browser] Context 健康检查失败，重建: {os.path.basename(cookie_file)} ({e})')
                try:
                    await ctx.close()
                except Exception:
                    pass
                del self.contexts[cookie_file]
                del self.context_last_used[cookie_file]
        
        cookies = load_cookies(cookie_file)
        ctx = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        await ctx.add_cookies(cookies)
        
        # 优化：屏蔽非必要资源加载
        await ctx.route('**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot,css}', 
                       lambda route: route.abort())
        
        self.contexts[cookie_file] = ctx
        self.context_last_used[cookie_file] = time.time()
        log(f'[Browser] 创建新 Context: {os.path.basename(cookie_file)}')
        return ctx
    
    async def cleanup_idle(self):
        """清理空闲超时的 Context"""
        now = time.time()
        to_remove = []
        for key, last_used in self.context_last_used.items():
            if now - last_used > config.browser_idle_timeout:
                to_remove.append(key)
        
        for key in to_remove:
            try:
                await self.contexts[key].close()
                log(f'[Browser] 清理空闲 Context: {key}')
            except Exception:
                pass
            del self.contexts[key]
            del self.context_last_used[key]
    
    async def close(self):
        """关闭所有资源"""
        for ctx in self.contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        self.contexts.clear()
        self.context_last_used.clear()
        
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

browser_session = BrowserSession()

# ==================== API 调用 ====================

async def api_post(page: Page, url: str, body_dict: dict, timeout: int = None, cookie_name: str = None, skip_rate_limit: bool = False) -> str:
    timeout = timeout or config.api_timeout
    if cookie_name and not skip_rate_limit:
        await rate_limiter.wait_if_needed(cookie_name)
    body_json = json.dumps(body_dict, ensure_ascii=False)
    body_safe = body_json.replace("\\", "\\\\").replace("'", "\\'")
    js = f'''async () => {{
        try {{
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), {timeout * 1000});
            const r = await fetch("{url}", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: '{body_safe}',
                signal: ctrl.signal
            }});
            clearTimeout(timer);
            return await r.text();
        }} catch(e) {{
            return JSON.stringify({{error: e.toString()}});
        }}
    }}'''
    result = await asyncio.wait_for(page.evaluate(js), timeout=timeout + 10)
    if cookie_name and not skip_rate_limit:
        rate_limiter.record_request(cookie_name)
    return result

# ==================== 积分检查 ====================

async def check_credits(page: Page) -> Optional[int]:
    try:
        ui_credit = await asyncio.wait_for(page.evaluate('''() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while(walker.nextNode()) {
                const text = walker.currentNode.textContent.trim();
                if (/^\\d+$/.test(text)) {
                    const num = parseInt(text);
                    if (num > 0 && num < 100000) {
                        const rect = walker.currentNode.parentElement?.getBoundingClientRect();
                        if (rect && rect.x > 1200 && rect.y > 0 && rect.y < 100 && rect.width > 0 && rect.height > 0) {
                            return num;
                        }
                    }
                }
            }
            return null;
        }'''), timeout=30)
        return ui_credit
    except asyncio.TimeoutError:
        log('[WARN] check_credits 超时 (30s)')
        return None

async def get_credits_info(page: Page) -> Optional[int]:
    try:
        resp = await api_post(page, '/api/web/v1/workspace/get_user_workspace', {})
        data = json.loads(resp)
        if str(data.get('ret')) == '0':
            credits = data.get('data', {}).get('remain_credit', 0)
            if credits > 0:
                return credits
    except Exception:
        log('[WARN] API 积分查询失败，回退到 DOM 解析')
    return await check_credits(page)

# ==================== 安全审核 ====================

async def security_check_text(page: Page, text: str, cookie_name: str = None):
    resp = json.loads(await api_post(page, '/api/web/v1/security/check', {
        'scene': 'pippit_video_part_user_input_text',
        'text_list': [text],
    }, cookie_name=cookie_name))
    if str(resp.get('ret')) != '0':
        return False, f'API error: {resp}'
    hit_list = resp.get('data', {}).get('text_hit_list', [])
    passed = not any(hit_list) if hit_list else True
    detail = resp.get('data', {}).get('text_hit_detail_list', [])
    return passed, detail

async def security_check_images(page: Page, image_urls: list, cookie_name: str = None):
    resp = json.loads(await api_post(page, '/api/web/v1/security/check', {
        'scene': 'pippit_seedance2_0_user_input_image',
        'image_list': [{'resource_type': 2, 'resource': url} for url in image_urls],
    }, cookie_name=cookie_name))
    if str(resp.get('ret')) != '0':
        return False, f'API error: {resp}'
    hit_list = resp.get('data', {}).get('image_hit_list', [])
    passed = not any(hit_list) if hit_list else True
    return passed, hit_list

# ==================== 图片上传 ====================

async def upload_image(page: Page, file_path: str, workspace_id: str, cookie_name: str = None) -> dict:
    fname = os.path.basename(file_path)
    mime = mimetypes.guess_type(file_path)[0] or 'image/png'
    file_size = os.path.getsize(file_path)

    if file_size > config.max_image_size:
        raise APIException(ErrorCode.FILE_TOO_LARGE, f'{file_size / 1024 / 1024:.1f}MB')

    # 一次性读取文件并编码 base64
    # 注意：不能分块编码后拼接，因为每个 chunk 独立 base64 编码会破坏 3→4 字节对齐，
    # 导致 JS atob() 在拼接边界处报 InvalidCharacterError
    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()

    up_js = f'''async () => {{
        try {{
            const bytes = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
            const fd = new FormData();
            fd.append("file", new Blob([bytes],{{type:"{mime}"}}), "{fname}");
            fd.append("asset_type", "2");
            const r = await fetch("/api/web/v1/common/upload_file", {{method:"POST", body:fd}});
            return await r.text();
        }} catch(e) {{
            return JSON.stringify({{error: e.toString()}});
        }}
    }}'''

    up = json.loads(await asyncio.wait_for(page.evaluate(up_js), timeout=config.upload_timeout))

    if str(up.get('ret')) != '0':
        raise APIException(ErrorCode.REQUEST_FAILED, f'upload failed: {up}')

    cdn_url = (up.get('data', {}).get('url', '')
               or up.get('data', {}).get('download_url', '')
               or up.get('url', ''))
    if not cdn_url:
        raise APIException(ErrorCode.REQUEST_FAILED, f'no CDN url')

    asset_id = str(up['data'].get('asset_id', ''))
    dl_url = up['data'].get('download_url', '') or cdn_url

    for attempt in range(5):
        await asyncio.sleep(2)
        info = json.loads(await api_post(page, '/api/web/v1/common/mget_asset_info', {
            'workspace_id': workspace_id,
            'asset_ids': [asset_id],
            'uid': '0',
            'need_transcode': True,
        }, cookie_name=cookie_name))
        if str(info.get('ret')) == '0' and info.get('data'):
            asset_data = info['data'][0] if info['data'] else {}
            log(f'  资产就绪 ({attempt+1}): {asset_data.get("width","?")}x{asset_data.get("height","?")}')
            dl_url = asset_data.get('download_url', '') or dl_url
            break
        log(f'  资产处理中 ({attempt+1})...')

    return {
        'asset_id': asset_id,
        'url': dl_url,
        'name': fname,
    }

# ==================== 任务提交 ====================

async def submit_task(page: Page, prompt: str, images: list, duration: int, 
                     ratio: str, model: str, workspace_id: str, cookie_name: str = None) -> str:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    param = {
        'prompt': prompt,
        'images': images,
        'duration_sec': duration,
        'ratio': ratio,
        'model': model,
        'language': 'zh',
        'imitation_videos': [],
        'videos': [],
        'audios': [],
    }

    payload = {
        'message': {
            'message_id': '',
            'role': 'user',
            'thread_id': thread_id,
            'run_id': run_id,
            'created_at': int(time.time() * 1000),
            'content': [{
                'type': 'data',
                'sub_type': 'biz/x_data_direct_tool_call_req',
                'data': json.dumps({
                    'param': json.dumps(param, ensure_ascii=False),
                    'tool_name': 'biz/x_tool_name_video_part',
                }),
                'hidden': False,
                'is_thought': False,
            }],
        },
        'user_info': {
            'consumer_uid': '0',
            'workspace_id': workspace_id,
            'app_id': config.app_id,
        },
        'agent_name': 'pippit_video_part_agent',
        'entrance_from': 'web',
    }

    resp = json.loads(await api_post(page, '/api/biz/v1/agent/submit_run', payload, cookie_name=cookie_name))
    ret_code = str(resp.get('ret'))
    if ret_code != '0':
        err_msg = resp.get('errmsg', '')
        fail_reason = ''
        data = resp.get('data')
        if data and isinstance(data, dict):
            run = data.get('run', {})
            if run and isinstance(run, dict):
                fail_reason = run.get('fail_reason', '')
        # v4: 限流时直接报错，不自动重试
        if ret_code == '99999':
            raise APIException(ErrorCode.RATE_LIMITED, f'操作过于频繁 (err={err_msg})')
        raise APIException(ErrorCode.VIDEO_FAILED, f'ret={resp.get("ret")} err={err_msg} fail_reason={fail_reason}')

    return resp['data']['run']['thread_id']

# ==================== 多策略视频 URL 提取 ====================

def extract_video_url(data: dict) -> Optional[str]:
    """多策略降级提取视频 URL"""
    if not data:
        return None
    
    # 策略 1: 结构化字段
    for path in [
        lambda d: d.get('video', {}).get('transcoded_video', {}).get('origin', {}).get('video_url', ''),
        lambda d: d.get('video', {}).get('play_url', ''),
        lambda d: d.get('video', {}).get('download_url', ''),
        lambda d: d.get('data', {}).get('video_url', ''),
        lambda d: d.get('url', ''),
    ]:
        url = path(data)
        if url and 'http' in url and '.mp4' in url:
            return _html.unescape(url)
    
    # 策略 2: 正则提取
    json_str = json.dumps(data, ensure_ascii=False)
    patterns = [
        r'https?://[^\s"\\]+\.mp4[^\s"\\]*',
        r'https?://[^\s"\\]+\.mp4\?[^\s"\\]+',
        r'https?://[^\s"\\]+video[^\s"\\]*\.mp4[^\s"\\]*',
    ]
    for pattern in patterns:
        urls = re.findall(pattern, json_str)
        if urls:
            return _html.unescape(urls[0])
    
    return None

# ==================== 固定间隔轮询 ====================

POLL_INTERVAL = 60  # 固定 1 分钟轮询一次
DEFAULT_TASK_TIMEOUT = 1800  # 默认任务超时 30 分钟

async def poll_result(page: Page, thread_id: str,
                     max_rounds: int = 0, cookie_name: str = None,
                     timeout: int = None) -> Optional[str]:
    """
    固定间隔轮询，每 60 秒查询一次，直到视频生成完成或超时。
    
    轮询次数 = timeout / POLL_INTERVAL（默认 1 分钟一次）
    超时时间：参数 > TASK_TIMEOUT > 默认 1800s
    """
    # 超时时间：显式参数 > 运行时配置(app_v3) > 默认 1800s
    if timeout is None:
        try:
            from app_v3 import get_task_timeout
            timeout = get_task_timeout()
        except ImportError:
            timeout = DEFAULT_TASK_TIMEOUT
    # 轮询次数 = 超时秒数 / 轮询间隔（向上取整 +1 确保最后还能查一次）
    max_rounds = timeout // POLL_INTERVAL + 1
    start_time = time.time()
    
    log(f'  开始轮询: 超时={timeout}s, 间隔={POLL_INTERVAL}s, 最多{max_rounds}次')
    
    for i in range(max_rounds):
        await asyncio.sleep(POLL_INTERVAL)
        elapsed = int(time.time() - start_time)

        try:
            detail_text = await api_post(page, '/api/biz/v1/agent/get_thread', {
                'scopes': ['run_list.entry_list'],
                'thread_id': thread_id,
            }, skip_rate_limit=True)
        except Exception as e:
            log(f'  poll#{i+1} API请求异常 ({elapsed}s/{timeout}s): {e}')
            continue

        try:
            detail = json.loads(detail_text)
        except json.JSONDecodeError:
            log(f'  poll#{i+1} 非JSON响应 ({elapsed}s/{timeout}s)')
            continue

        if detail.get('ret') != '0':
            log(f'  poll#{i+1} API错误: {detail.get("errmsg","")} ({elapsed}s/{timeout}s)')
            continue

        thread_data = detail.get('data', {}).get('thread', {})
        run_list = thread_data.get('run_list', [])
        if not run_list:
            log(f'  poll#{i+1} 无run记录 ({elapsed}s/{timeout}s)')
            continue

        state = run_list[0].get('state', -1)
        entry_list = []
        for run_item in run_list:
            entry_list.extend(run_item.get('entry_list', []))

        # 多策略 URL 提取
        mp4_url = None
        search_targets = [json.dumps(entry, ensure_ascii=False) for entry in entry_list]
        search_targets.append(json.dumps(thread_data, ensure_ascii=False))
        
        for target in search_targets:
            try:
                parsed_target = json.loads(target)
            except (json.JSONDecodeError, TypeError):
                continue
            url = extract_video_url(parsed_target)
            if url:
                mp4_url = url
                break

        if state == 2:
            est = '?'
            if run_list:
                est = run_list[0].get('RunQueueInfo', {}).get(
                    'run_state_for_generation_stage', {}).get('estimated_time_seconds', '?')
            log(f'  poll#{i+1} 生成中... 预计{est}秒 ({elapsed}s/{timeout}s)')
            continue

        if state == 3:
            if mp4_url:
                log(f'  poll#{i+1} 视频就绪! (耗时{elapsed}s)')
                return mp4_url
            log(f'  poll#{i+1} state=3 但无mp4，继续等待... ({elapsed}s/{timeout}s)')
            continue

        if state == 4:
            fail_reason = run_list[0].get('fail_reason', {})
            log(f'  poll#{i+1} 生成失败: {fail_reason}')
            return None

        if state == 1:
            log(f'  poll#{i+1} 排队中... ({elapsed}s/{timeout}s)')
            continue

        log(f'  poll#{i+1} 未知状态: {state}')
        return None

    log(f'[ERROR] 轮询超时 ({timeout}s/{max_rounds}次)')
    return None

# ==================== 视频下载 ====================

def download_video(mp4_url: str, output_path: str, timeout: int = None) -> bool:
    timeout = timeout or config.download_timeout
    mp4_url = _html.unescape(mp4_url)
    try:
        req = urllib.request.Request(mp4_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(output_path, 'wb') as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 100000
    except Exception as e:
        log(f'  下载异常: {e}')
        return False

# ==================== 核心执行流程 ====================

async def run_with_cookie(prompt: str, duration: int, ratio: str, model: str,
                         ref_images: list, output_dir: str, cookie_file: str) -> Optional[str]:
    cookie_name = os.path.basename(cookie_file).replace('.json', '')
    log(f'[*] 使用 Cookie: {os.path.basename(cookie_file)}')
    log(f'[*] 小云雀 - {prompt[:30]}... | {duration}s | {ratio} | {MODEL_LABELS.get(model, model)}')

    ctx = None
    page = None
    try:
        ctx = await browser_session.get_context(cookie_file)
        page = await ctx.new_page()
        
        try:
            await asyncio.wait_for(
                page.goto('https://xyq.jianying.com/home', wait_until='domcontentloaded'),
                timeout=config.page_load_timeout
            )
        except asyncio.TimeoutError:
            log('[WARN] 页面加载超时，继续...')
        await page.wait_for_timeout(5000)

        ws_resp = json.loads(await api_post(page, '/api/web/v1/workspace/get_user_workspace', {}, cookie_name=cookie_name))
        if str(ws_resp.get('ret')) == '0':
            workspace_id = ws_resp['data']['workspace_id']
            log(f'[*] workspace_id: {workspace_id}')
        else:
            log('[ERROR] 获取workspace失败，cookies可能已过期')
            raise APIException(ErrorCode.TOKEN_EXPIRED, '获取workspace失败')

        credits = await get_credits_info(page)
        log(f'[*] 当前积分: {credits}')

        required_credits = MODEL_CREDITS_PER_SEC.get(model, 5) * duration
        if credits is not None and credits < required_credits:
            log(f'[*] 积分不足 ({credits} < {required_credits})')
            raise APIException(ErrorCode.INSUFFICIENT_CREDITS, 
                             f'{credits} < {required_credits}')

        if ref_images:
            log(f'[*] 上传 {len(ref_images)} 张图片...')
            images = []
            for i, img_path in enumerate(ref_images):
                log(f'  [{i+1}] {os.path.basename(img_path)} ({os.path.getsize(img_path) / 1024:.0f}KB)...')
                asset = await upload_image(page, img_path, workspace_id, cookie_name=cookie_name)
                images.append(asset)
                log(f'  [{i+1}] OK: {asset["asset_id"]}')
            img_urls = [img['url'] for img in images]
        else:
            images = []
            img_urls = []

        log('[*] 安全审核...')
        text_ok, text_detail = await security_check_text(page, prompt, cookie_name=cookie_name)
        log(f'  文字: {"通过" if text_ok else "拒绝"} {text_detail}')
        if not text_ok:
            raise APIException(ErrorCode.CONTENT_FILTERED, '文字审核未通过')

        if img_urls:
            img_ok, img_detail = await security_check_images(page, img_urls, cookie_name=cookie_name)
            log(f'  图片: {"通过" if img_ok else "拒绝"} {img_detail}')
            if not img_ok:
                raise APIException(ErrorCode.CONTENT_FILTERED, '图片审核未通过')

        log('[*] 提交任务...')
        thread_id = await submit_task(page, prompt, images, duration, ratio, model, workspace_id, cookie_name=cookie_name)
        log(f'  thread_id: {thread_id}')

        log(f'[*] 轮询结果...')
        mp4_url = await poll_result(page, thread_id, cookie_name=cookie_name)

        if mp4_url:
            ts = time.strftime('%Y%m%d_%H%M%S')
            safe_name = ''.join(c for c in prompt[:15] if c.isalnum() or c in '_ ') or 'video'
            out_path = os.path.join(output_dir, f'{safe_name}_{duration}s_{ts}.mp4')
            log(f'[*] 下载: {out_path}')
            if download_video(mp4_url, out_path):
                size_mb = os.path.getsize(out_path) / 1048576
                log(f'[DONE] {out_path} ({size_mb:.1f}MB)')
                return out_path
            else:
                raise APIException(ErrorCode.VIDEO_FAILED, '下载失败')
        else:
            raise APIException(ErrorCode.VIDEO_FAILED, '未获取到视频URL')

    except APIException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise APIException(ErrorCode.BROWSER_ERROR, str(e))
    finally:
        if page:
            try:
                await asyncio.wait_for(page.close(), timeout=10)
            except (asyncio.TimeoutError, Exception):
                log('[WARN] page.close() 超时，强制跳过')
                pass

async def run(prompt: str, duration: int = 10, ratio: str = '16:9', 
             model: str = 'fast', ref_images: list = None, 
             output_dir: str = None, cookie_file: str = None) -> Optional[str]:
    """主执行函数，支持多账号自动切换"""
    model_key = MODELS.get(model, model)
    ratio = ratio if ratio != '1:1' else '16:9'
    output_dir = output_dir or config.output_dir
    os.makedirs(output_dir, exist_ok=True)

    if cookie_file:
        cookie_files = [cookie_file]
    else:
        cookie_files = get_token_files()
        if not cookie_files:
            raise APIException(ErrorCode.PARAMS_INVALID, '没有找到Cookie文件')

    last_error = None
    for idx, fname in enumerate(cookie_files):
        fpath = os.path.join(config.cookies_dir, fname) if not os.path.dirname(fname) else fname
        try:
            result = await run_with_cookie(
                prompt=prompt,
                duration=duration,
                ratio=ratio,
                model=model_key,
                ref_images=ref_images or [],
                output_dir=output_dir,
                cookie_file=fpath
            )
            if result:
                return result
        except APIException as e:
            if e.code == ErrorCode.INSUFFICIENT_CREDITS:
                log(f'[*] Cookie #{idx+1} 积分不足，尝试下一个...')
                last_error = e
                continue
            elif e.code == ErrorCode.RATE_LIMITED:
                log(f'[*] Cookie #{idx+1} 限流: {e.message}，尝试下一个...')
                last_error = e
                continue
            else:
                log(f'[*] Cookie #{idx+1} 执行失败: {e.message}')
                last_error = e
                continue
        except Exception as e:
            log(f'[*] Cookie #{idx+1} 执行失败: {e}')
            last_error = e
            continue

    raise last_error or APIException(ErrorCode.VIDEO_FAILED, '所有 Cookie 都执行失败')

# ==================== CLI 入口 ====================

def main():
    parser = argparse.ArgumentParser(description='小云雀 v3.0 - AI视频生成自动化')
    parser.add_argument('--prompt', required=True, help='视频描述提示词')
    parser.add_argument('--ref-images', nargs='+', help='参考图片路径')
    parser.add_argument('--duration', type=int, default=10, choices=[5, 10, 15], help='视频时长秒数')
    parser.add_argument('--ratio', default='16:9', choices=['16:9', '9:16', '1:1'], help='视频比例')
    parser.add_argument('--model', default='fast', choices=['fast', '2.0'], help='模型: fast / 2.0')
    parser.add_argument('--cookies', default=config.cookies_dir, help='Cookies目录')
    parser.add_argument('--output', default=config.output_dir, help='视频输出目录')
    parser.add_argument('--cookie-file', default=None, help='指定使用的Cookie文件')
    args = parser.parse_args()

    if args.ref_images:
        for img in args.ref_images:
            if not os.path.exists(img):
                parser.error(f'图片不存在: {img}')
            if os.path.getsize(img) > config.max_image_size:
                parser.error(f'图片过大: {img}')

    config.cookies_dir = args.cookies
    config.output_dir = args.output

    try:
        result = asyncio.run(run(
            prompt=args.prompt,
            duration=args.duration,
            ratio=args.ratio,
            model=args.model,
            ref_images=args.ref_images,
            output_dir=args.output,
            cookie_file=args.cookie_file
        ))
        if result:
            print(f'\n[DONE] 视频已保存: {result}')
    except APIException as e:
        print(f'\n[ERROR] {e.message}')
        exit(1)
    except Exception as e:
        print(f'\n[FATAL] {e}')
        exit(1)

if __name__ == '__main__':
    main()
