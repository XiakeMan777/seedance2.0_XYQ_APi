# -*- coding: utf-8 -*-
"""只读账号 / 积分检查（不会提交生成任务，不会消耗积分）。

用法：
    ./.venv/bin/python debug_credit.py

外部浏览器模式（默认）：复用本机已登录的小云雀 Chrome，读取登录态与剩余积分。
Cookie 文件模式：需要设置 BROWSER_CDP_URL='' 且 ALLOW_BROWSER_LAUNCH=true，并放入 cookies/*.json。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xiaoyunque_v3 import (  # noqa: E402
    acquire_page,
    api_post,
    browser_session,
    config,
    get_token_files,
    read_account_badge,
    using_live_browser,
)


async def check_live() -> dict:
    ctx = await browser_session.get_context(None)
    page, created = await acquire_page(ctx)
    try:
        try:
            await asyncio.wait_for(
                page.goto('https://xyq.jianying.com/home', wait_until='domcontentloaded'),
                timeout=config.page_load_timeout,
            )
        except asyncio.TimeoutError:
            print('[WARN] 页面加载超时，继续尝试读取')
        await page.wait_for_timeout(4000)

        resp = json.loads(await api_post(page, '/api/web/v1/workspace/get_user_workspace', {}))
        if str(resp.get('ret')) != '0':
            return {'logged_in': False, 'error': f"登录态无效 (ret={resp.get('ret')}, {resp.get('errmsg') or resp})"}
        data = resp.get('data') or {}
        badge = await read_account_badge(page)
        return {
            'logged_in': True,
            'credits': badge.get('credits'),
            'membership': badge.get('membership'),
            'workspace_id': data.get('workspace_id'),
        }
    finally:
        if created:
            await page.close()


async def main():
    print(f'浏览器模式: {"外部浏览器（本机已登录 Chrome）" if using_live_browser() else "Cookie 文件"}')
    if not using_live_browser():
        print(f'Cookie 文件: {get_token_files() or "（无）"}')
        return 1

    try:
        result = await check_live()
    finally:
        await browser_session.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('logged_in') else 2


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
