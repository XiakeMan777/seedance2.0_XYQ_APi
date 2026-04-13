# -*- coding: utf-8 -*-
"""
小云雀 (XiaoYunque) - AI视频生成自动化工具
通过 Playwright 注入 cookies 到小云雀平台(xyq.jianying.com)，自动化完成：
  积分检查 → 上传图片 → 安全审核 → 提交任务 → 轮询结果 → 下载视频

状态码:
  run.state: 1=排队, 2=处理中, 3=视频就绪, 4=失败
  ret: 0=成功, 非0=失败
  error 11001: 配额/积分不足
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

from playwright.async_api import async_playwright

DEFAULT_COOKIES_DIR = 'cookies'
DEFAULT_OUTPUT = '.'
APP_ID = '795647'

MAX_IMAGE_SIZE = 20 * 1024 * 1024
POLL_INTERVAL = 30
POLL_MAX_ROUNDS = 40
PAGE_LOAD_TIMEOUT = 30
API_TIMEOUT = 60
UPLOAD_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 600

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

def log(msg):
    t = time.strftime('%H:%M:%S')
    print(f'[{t}] {msg}', flush=True)


def load_cookies(path):
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


def get_cookies_files():
    if not os.path.exists(DEFAULT_COOKIES_DIR):
        os.makedirs(DEFAULT_COOKIES_DIR, exist_ok=True)
        return []
    return [f for f in os.listdir(DEFAULT_COOKIES_DIR) if f.endswith('.json')]


async def api_get(page, path, timeout=None):
    timeout = timeout or API_TIMEOUT
    js = f'''async () => {{
        try {{
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), {timeout * 1000});
            const r = await fetch("{path}", {{signal: ctrl.signal}});
            clearTimeout(timer);
            return await r.text();
        }} catch(e) {{
            return JSON.stringify({{error: e.toString()}});
        }}
    }}'''
    return await page.evaluate(js)


async def api_post(page, url, body_dict, timeout=None):
    timeout = timeout or API_TIMEOUT
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
    return await page.evaluate(js)


async def check_credits(page):
    ui_credit = await page.evaluate('''() => {
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
    }''')
    return ui_credit


async def get_credits_info(page):
    try:
        resp = await api_post(page, '/api/web/v1/workspace/get_user_workspace', {})
        data = json.loads(resp)
        if str(data.get('ret')) == '0':
            credits = data.get('data', {}).get('remain_credit', 0)
            if credits > 0:
                return credits
    except:
        pass
    return await check_credits(page)


async def security_check_text(page, text):
    resp = json.loads(await api_post(page, '/api/web/v1/security/check', {
        'scene': 'pippit_video_part_user_input_text',
        'text_list': [text],
    }))
    if str(resp.get('ret')) != '0':
        return False, f'API error: {resp}'
    hit_list = resp.get('data', {}).get('text_hit_list', [])
    passed = not any(hit_list) if hit_list else True
    detail = resp.get('data', {}).get('text_hit_detail_list', [])
    return passed, detail


async def security_check_images(page, image_urls):
    resp = json.loads(await api_post(page, '/api/web/v1/security/check', {
        'scene': 'pippit_seedance2_0_user_input_image',
        'image_list': [{'resource_type': 2, 'resource': url} for url in image_urls],
    }))
    if str(resp.get('ret')) != '0':
        return False, f'API error: {resp}'
    hit_list = resp.get('data', {}).get('image_hit_list', [])
    passed = not any(hit_list) if hit_list else True
    return passed, hit_list


async def upload_image(page, file_path, workspace_id):
    fname = os.path.basename(file_path)
    mime = mimetypes.guess_type(file_path)[0] or 'image/png'
    file_size = os.path.getsize(file_path)

    if file_size > MAX_IMAGE_SIZE:
        raise ValueError(f'图片过大: {file_size / 1024 / 1024:.1f}MB (最大{MAX_IMAGE_SIZE / 1024 / 1024}MB)')

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

    up = json.loads(await asyncio.wait_for(page.evaluate(up_js), timeout=UPLOAD_TIMEOUT))

    if str(up.get('ret')) != '0':
        raise Exception(f'upload failed: {up}')

    cdn_url = (up.get('data', {}).get('url', '')
               or up.get('data', {}).get('download_url', '')
               or up.get('url', ''))
    if not cdn_url:
        raise Exception(f'no CDN url in response: {json.dumps(up, ensure_ascii=False)[:200]}')

    asset_id = str(up['data'].get('asset_id', ''))
    dl_url = up['data'].get('download_url', '') or cdn_url

    for attempt in range(5):
        await page.wait_for_timeout(2000)
        info = json.loads(await api_post(page, '/api/web/v1/common/mget_asset_info', {
            'workspace_id': workspace_id,
            'asset_ids': [asset_id],
            'uid': '0',
            'need_transcode': True,
        }))
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


async def submit_task(page, prompt, images, duration, ratio, model, workspace_id):
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
            'app_id': APP_ID,
        },
        'agent_name': 'pippit_video_part_agent',
        'entrance_from': 'web',
    }

    resp = json.loads(await api_post(page, '/api/biz/v1/agent/submit_run', payload))
    if str(resp.get('ret')) != '0':
        err_msg = resp.get('errmsg', '')
        fail = resp.get('data', {}).get('run', {}).get('fail_reason', {})
        raise Exception(f'submit failed: ret={resp.get("ret")} err={err_msg} fail={fail}')

    return resp['data']['run']['thread_id']


async def poll_result(page, thread_id, max_rounds=POLL_MAX_ROUNDS, interval=POLL_INTERVAL):
    for i in range(max_rounds):
        await page.wait_for_timeout(interval * 1000)

        detail_text = await api_post(page, '/api/biz/v1/agent/get_thread', {
            'scopes': ['run_list.entry_list'],
            'thread_id': thread_id,
        })

        try:
            detail = json.loads(detail_text)
        except json.JSONDecodeError:
            log(f'  poll#{i+1} 非JSON响应: {detail_text[:100]}')
            continue

        if detail.get('ret') != '0':
            log(f'  poll#{i+1} API错误: {detail.get("errmsg","")}')
            continue

        thread_data = detail.get('data', {}).get('thread', {})
        run_list = thread_data.get('run_list', [])
        if not run_list:
            log(f'  poll#{i+1} 无run记录')
            continue

        state = run_list[0].get('state', -1)
        entry_list = []
        for run_item in run_list:
            entry_list.extend(run_item.get('entry_list', []))

        mp4_url = None
        search_targets = [json.dumps(entry, ensure_ascii=False) for entry in entry_list]
        search_targets.append(json.dumps(thread_data, ensure_ascii=False))

        for target in search_targets:
            if '.mp4' in target and 'http' in target:
                urls = re.findall(r'https?://[^\s"\\]+\.mp4[^\s"\\]*', target)
                if urls:
                    mp4_url = urls[0]
                    break

        if state == 2:
            est = '?'
            if run_list:
                est = run_list[0].get('RunQueueInfo', {}).get(
                    'run_state_for_generation_stage', {}).get('estimated_time_seconds', '?')
            log(f'  poll#{i+1} 生成中... 预计{est}秒')
            continue

        if state == 3:
            if mp4_url:
                log(f'  poll#{i+1} 视频就绪!')
                return mp4_url
            log(f'  poll#{i+1} state=3 但无mp4，继续等待...')
            continue

        if state == 4:
            fail_reason = run_list[0].get('fail_reason', {})
            log(f'  poll#{i+1} 生成失败: {fail_reason}')
            return None

        if state == 1:
            log(f'  poll#{i+1} 排队中...')
            continue

        log(f'  poll#{i+1} 未知状态: {state}')
        return None

    log('[ERROR] 轮询超时')
    return None


def download_video(mp4_url, output_path, timeout=DOWNLOAD_TIMEOUT):
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


async def run_with_cookie(prompt, duration, ratio, model, ref_images, output_dir, cookie_index, cookies_file):
    log(f'[*] 使用 Cookie #{cookie_index + 1}: {os.path.basename(cookies_file)}')
    log(f'[*] 小云雀 - {prompt[:30]}... | {duration}s | {ratio} | {MODEL_LABELS.get(model, model)}')

    p = await async_playwright().start()
    b = await p.chromium.launch(headless=True, args=['--no-sandbox'])

    try:
        ctx = await b.new_context(viewport={'width': 1920, 'height': 1080})
        cookies = load_cookies(cookies_file)
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        log(f'[*] 加载 {len(cookies)} cookies')

        try:
            await asyncio.wait_for(
                page.goto('https://xyq.jianying.com/home', wait_until='domcontentloaded'),
                timeout=PAGE_LOAD_TIMEOUT
            )
        except asyncio.TimeoutError:
            log('[WARN] 页面加载超时，继续...')
        await page.wait_for_timeout(5000)

        ws_resp = json.loads(await api_post(page, '/api/web/v1/workspace/get_user_workspace', {}))
        if str(ws_resp.get('ret')) == '0':
            workspace_id = ws_resp['data']['workspace_id']
            log(f'[*] workspace_id: {workspace_id}')
        else:
            log('[ERROR] 获取workspace失败，cookies可能已过期')
            return None

        for sel in ['text=X', '[class*=close]']:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click(timeout=2000)
                    log(f'[*] 关闭弹窗')
                    await page.wait_for_timeout(1000)
                    break
            except:
                continue

        credits = await get_credits_info(page)
        log(f'[*] 当前积分: {credits}')

        required_credits = MODEL_CREDITS_PER_SEC.get(model, 5) * duration
        if credits is not None and credits < required_credits:
            log(f'[*] 积分不足 ({credits} < {required_credits})，需要换用其他Cookie')
            await b.close()
            await p.stop()
            return 'INSUFFICIENT_CREDITS'

        if ref_images:
            log(f'[*] 上传 {len(ref_images)} 张图片...')
            images = []
            for i, img_path in enumerate(ref_images):
                log(f'  [{i+1}] {os.path.basename(img_path)} ({os.path.getsize(img_path) / 1024:.0f}KB)...')
                asset = await upload_image(page, img_path, workspace_id)
                images.append(asset)
                log(f'  [{i+1}] OK: {asset["asset_id"]}')

            img_urls = [img['url'] for img in images]
        else:
            images = []
            img_urls = []

        log('[*] 安全审核...')
        text_ok, text_detail = await security_check_text(page, prompt)
        log(f'  文字: {"通过" if text_ok else "拒绝"} {text_detail}')
        if not text_ok:
            log('[ERROR] 文字安全审核未通过')
            await b.close()
            await p.stop()
            return None

        if img_urls:
            img_ok, img_detail = await security_check_images(page, img_urls)
            log(f'  图片: {"通过" if img_ok else "拒绝"} {img_detail}')
            if not img_ok:
                log('[ERROR] 图片安全审核未通过')
                await b.close()
                await p.stop()
                return None

        log('[*] 提交任务...')
        thread_id = await submit_task(page, prompt, images, duration, ratio, model, workspace_id)
        log(f'  thread_id: {thread_id}')

        log(f'[*] 轮询结果 (每{POLL_INTERVAL}秒)...')
        mp4_url = await poll_result(page, thread_id)

        if mp4_url:
            ts = time.strftime('%Y%m%d_%H%M%S')
            safe_name = ''.join(c for c in prompt[:15] if c.isalnum() or c in '_ ') or 'video'
            out_path = os.path.join(output_dir, f'{safe_name}_{duration}s_{ts}.mp4')
            log(f'[*] 下载: {out_path}')
            if download_video(mp4_url, out_path):
                size_mb = os.path.getsize(out_path) / 1048576
                log(f'[DONE] {out_path} ({size_mb:.1f}MB)')
                await b.close()
                await p.stop()
                return out_path
            else:
                log('[ERROR] 下载失败')
        else:
            log('[ERROR] 未获取到视频URL')

        await b.close()
        await p.stop()
        return None

    except Exception as e:
        traceback.print_exc()
        log(f'[FATAL] {e}')
        try:
            await b.close()
            await p.stop()
        except:
            pass
        return None


async def run(args):
    model = MODELS.get(args.model, args.model)
    ratio = args.ratio if args.ratio != '1:1' else '16:9'

    cookies_files = []
    if args.cookie_index is not None:
        cookie_files_all = get_cookies_files()
        if not cookie_files_all:
            cookie_files_all = [os.path.join(DEFAULT_COOKIES_DIR, 'cookies.json')]
        if args.cookie_index < len(cookie_files_all):
            cookies_files = [cookie_files_all[args.cookie_index]]
        else:
            cookies_files = cookie_files_all
    else:
        cookies_files = get_cookies_files()
        if not cookies_files:
            cookies_files = [os.path.join(DEFAULT_COOKIES_DIR, 'cookies.json')]

    for idx, cookies_file in enumerate(cookies_files):
        cookies_path = os.path.join(DEFAULT_COOKIES_DIR, cookies_file) if not os.path.dirname(cookies_file) else cookies_file
        result = await run_with_cookie(
            prompt=args.prompt,
            duration=args.duration,
            ratio=ratio,
            model=model,
            ref_images=args.ref_images,
            output_dir=args.output,
            cookie_index=idx,
            cookies_file=cookies_path
        )

        if result == 'INSUFFICIENT_CREDITS':
            log(f'[*] Cookie #{idx + 1} 积分不足，尝试下一个...')
            continue
        elif result:
            return result
        else:
            log(f'[*] Cookie #{idx + 1} 执行失败，尝试下一个...')
            continue

    log('[ERROR] 所有 Cookie 都执行失败')
    return None


def main():
    parser = argparse.ArgumentParser(
        description='小云雀 - AI视频生成自动化',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  %(prog)s --prompt "夕阳下跑步的女孩" --ref-images 2.png
  %(prog)s --prompt "海边跳舞" --model fast --ref-images 1.png 2.png --duration 5
  %(prog)s --prompt "城市夜景" --ratio 9:16 --duration 15 --ref-images city.png
'''
    )
    parser.add_argument('--prompt', required=True, help='视频描述提示词')
    parser.add_argument('--ref-images', nargs='+', help='参考图片路径')
    parser.add_argument('--duration', type=int, default=10, choices=[5, 10, 15], help='视频时长秒数 (默认10)')
    parser.add_argument('--ratio', default='16:9', choices=['16:9', '9:16', '1:1'], help='视频比例 (默认16:9)')
    parser.add_argument('--model', default='fast', choices=['fast', '2.0'], help='模型: fast / 2.0 (默认fast)')
    parser.add_argument('--cookies', default=DEFAULT_COOKIES_DIR, help=f'Cookies目录 (默认{DEFAULT_COOKIES_DIR})')
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help=f'视频输出目录 (默认{DEFAULT_OUTPUT})')
    parser.add_argument('--cookie-index', type=int, default=None, help='指定使用的Cookie索引')
    parser.add_argument('--dry-run', action='store_true', help='仅查询配额，不提交任务')
    args = parser.parse_args()

    if args.ref_images:
        for img in args.ref_images:
            if not os.path.exists(img):
                parser.error(f'图片不存在: {img}')
            if os.path.getsize(img) > MAX_IMAGE_SIZE:
                parser.error(f'图片过大: {img} ({os.path.getsize(img) / 1024 / 1024:.1f}MB, 最大20MB)')

    asyncio.run(run(args))


def main_wrapper(args):
    """包装函数，接受 argparse.Namespace 参数并执行"""
    if args.ref_images:
        for img in args.ref_images:
            if not os.path.exists(img):
                raise FileNotFoundError(f'图片不存在: {img}')
            if os.path.getsize(img) > MAX_IMAGE_SIZE:
                raise ValueError(f'图片过大: {img} ({os.path.getsize(img) / 1024 / 1024:.1f}MB, 最大20MB)')
    
    return asyncio.run(run(args))


if __name__ == '__main__':
    main()