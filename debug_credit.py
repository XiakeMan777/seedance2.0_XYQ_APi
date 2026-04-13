import asyncio
import sys
sys.path.insert(0, '/app')

import xiaoyunque_v3 as x
import importlib
importlib.reload(x)

async def test():
    cookie_path = '/app/cookies/1000-1-0.json'
    cookies = x.load_cookies(cookie_path)
    print('Cookies count:', len(cookies))
    print('Cookie names:', list(cookies.keys())[:3])

    ctx = await x.browser_session.get_context(cookie_path)
    page = await ctx.new_page()
    try:
        await asyncio.wait_for(page.goto('https://xyq.jianying.com/home', wait_until='domcontentloaded'), timeout=30)
        print('Page loaded OK')
    except asyncio.TimeoutError:
        print('Page load TIMEOUT')
    await page.wait_for_timeout(5000)

    # Test commerce API directly
    resp = await x.api_get(page, '/commerce/v1/benefits/user_credit')
    print('Commerce API resp:', resp[:500])

    credits = await x.get_credits_info(page)
    print('Final credits:', credits)

    await page.close()

asyncio.run(test())