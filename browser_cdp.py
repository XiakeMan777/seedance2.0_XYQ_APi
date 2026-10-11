# -*- coding: utf-8 -*-
"""轻量 CDP 客户端：连接本机已运行、已登录的 Chrome，复用其登录态。

为什么不用 Playwright 的 connect_over_cdp：
Playwright 连接时会调用 Browser.setDownloadBehavior，而本机浏览器运行时暴露的
CDP 端点会返回 "Browser context management is not supported."，导致连接直接失败。
本模块只使用 CDP 中确实可用的 Target.* / Runtime.evaluate / Page.navigate，
用 websocket-client 直连，行为可控、失败信息明确。

对外暴露一组与 Playwright 兼容的最小接口（.pages / .new_page() / .evaluate() /
.goto() / .wait_for_timeout() / .close() / .url），使上层业务代码无需改动即可复用。

安全边界：
- 只连接 config.cdp_url 指定的端点，绝不启动新浏览器；
- 只创建/关闭自己新建的标签页，绝不关闭用户已有的标签页；
- 不读取、不导出任何 Cookie；登录态来自浏览器自身。
"""

import asyncio
import itertools
import json
import threading
import time
import urllib.request
from typing import Dict, List, Optional

import websocket


class CdpError(Exception):
    """CDP 交互失败（连接、命令或页面执行异常）。"""


class CdpConnection:
    """单个 CDP WebSocket 连接，线程安全。"""

    def __init__(self, http_endpoint: str, default_timeout: float = 30.0):
        self.http_endpoint = http_endpoint.rstrip('/')
        self.default_timeout = default_timeout
        self._ws: Optional[websocket.WebSocket] = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def _ensure_connected(self):
        if self._ws is not None:
            return
        try:
            with urllib.request.urlopen(self.http_endpoint + '/json/version', timeout=10) as resp:
                version = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            raise CdpError(f'无法访问 CDP 端点 {self.http_endpoint}/json/version：{e}')
        ws_url = version.get('webSocketDebuggerUrl')
        if not ws_url:
            raise CdpError(f'CDP 端点未返回 webSocketDebuggerUrl：{version}')
        try:
            # suppress_origin：该端点会以 403 拒绝携带 Origin 头的 WebSocket 连接
            self._ws = websocket.create_connection(ws_url, timeout=self.default_timeout,
                                                   suppress_origin=True)
        except Exception as e:
            raise CdpError(f'无法建立 CDP WebSocket 连接 {ws_url}：{e}')

    def call(self, method: str, params: Optional[dict] = None,
             session_id: Optional[str] = None, timeout: Optional[float] = None) -> dict:
        """发送一条 CDP 命令并等待其响应；同一连接内串行执行。"""
        timeout = timeout or self.default_timeout
        with self._lock:
            self._ensure_connected()
            msg_id = next(self._ids)
            payload = {'id': msg_id, 'method': method}
            if params:
                payload['params'] = params
            if session_id:
                payload['sessionId'] = session_id
            try:
                self._ws.settimeout(timeout)
                self._ws.send(json.dumps(payload))
            except Exception as e:
                self.reset()
                raise CdpError(f'发送 CDP 命令失败 {method}：{e}')

            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise CdpError(f'CDP 命令超时 {method}（{timeout}s）')
                try:
                    self._ws.settimeout(remaining)
                    raw = self._ws.recv()
                except Exception as e:
                    self.reset()
                    raise CdpError(f'读取 CDP 响应失败 {method}：{e}')
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get('id') != msg_id:
                    continue  # 事件或其它响应，忽略
                if 'error' in data:
                    err = data['error']
                    raise CdpError(f"CDP 命令失败 {method}：{err.get('message')}")
                return data.get('result', {})

    async def acall(self, method: str, params: Optional[dict] = None,
                    session_id: Optional[str] = None, timeout: Optional[float] = None) -> dict:
        return await asyncio.to_thread(self.call, method, params, session_id, timeout)

    def reset(self):
        with self._lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None

    def close(self):
        self.reset()


def _is_function_source(expr: str) -> bool:
    """判断 Playwright 风格的 evaluate 源码是否为函数表达式。"""
    s = expr.lstrip()
    return s.startswith('(') or s.startswith('async ') or s.startswith('function')


class CdpPage:
    """Playwright Page 的最小兼容实现（仅覆盖本项目的用法）。"""

    def __init__(self, connection: CdpConnection, target_id: str,
                 session_id: str, url: str = '', owned: bool = True):
        self._conn = connection
        self.target_id = target_id
        self.session_id = session_id
        self._url = url or ''
        self.owned = owned

    @property
    def url(self) -> str:
        return self._url

    async def evaluate(self, expression: str, timeout: float = 120.0):
        wrapped = f'({expression})()' if _is_function_source(expression) else expression
        result = await self._conn.acall('Runtime.evaluate', {
            'expression': wrapped,
            'awaitPromise': True,
            'returnByValue': True,
            'userGesture': True,
        }, session_id=self.session_id, timeout=timeout)

        if result.get('exceptionDetails'):
            detail = result['exceptionDetails']
            desc = (detail.get('exception') or {}).get('description') or detail.get('text')
            raise CdpError(f'页面执行异常：{desc}')

        remote = result.get('result', {})
        if remote.get('subtype') == 'error':
            raise CdpError(f"页面执行异常：{remote.get('description')}")
        return remote.get('value')

    async def goto(self, url: str, wait_until: str = 'domcontentloaded',
                   timeout: float = 30.0) -> None:
        try:
            await self._conn.acall('Page.enable', session_id=self.session_id)
        except CdpError:
            pass
        await self._conn.acall('Page.navigate', {'url': url}, session_id=self.session_id)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = await self.evaluate('document.readyState', timeout=15)
            except CdpError:
                await asyncio.sleep(0.3)
                continue
            if state in ('interactive', 'complete'):
                break
            await asyncio.sleep(0.3)
        self._url = url

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000.0)

    async def close(self) -> None:
        if not self.owned:
            return
        try:
            await self._conn.acall('Target.closeTarget', {'targetId': self.target_id})
        except CdpError:
            pass


class CdpContext:
    """对已连接 Chrome 的默认上下文的薄封装（不创建新 Context，不注入 Cookie）。"""

    def __init__(self, connection: CdpConnection):
        self._conn = connection
        self._pages: List[CdpPage] = []

    async def _list_page_targets(self) -> List[dict]:
        result = await self._conn.acall('Target.getTargets')
        return [t for t in result.get('targetInfos', []) if t.get('type') == 'page']

    async def _attach(self, target_id: str, url: str, owned: bool) -> CdpPage:
        result = await self._conn.acall('Target.attachToTarget',
                                        {'targetId': target_id, 'flatten': True})
        session_id = result.get('sessionId')
        if not session_id:
            raise CdpError(f'无法附加到标签页 {target_id}')
        return CdpPage(self._conn, target_id, session_id, url=url, owned=owned)

    async def pages(self) -> List[CdpPage]:
        """当前所有可用的标签页（用户已打开的标签一并返回）。"""
        targets = await self._list_page_targets()
        pages = []
        for target in targets:
            pages.append(CdpPage(self._conn, target['targetId'], '', url=target.get('url', ''),
                                 owned=False))
        self._pages = pages
        return pages

    async def page_for_url(self, needle: str) -> Optional[CdpPage]:
        """复用已打开且 URL 匹配的标签页，避免堆叠窗口。"""
        for target in await self._list_page_targets():
            if needle in (target.get('url') or ''):
                return await self._attach(target['targetId'], target.get('url', ''), owned=False)
        return None

    async def new_page(self) -> CdpPage:
        result = await self._conn.acall('Target.createTarget', {'url': 'about:blank'})
        target_id = result.get('targetId')
        if not target_id:
            raise CdpError('无法新建标签页')
        return await self._attach(target_id, 'about:blank', owned=True)


class CdpBrowser:
    """外部浏览器模式的会话入口（连接用户本机已登录的 Chrome）。"""

    def __init__(self, http_endpoint: str):
        self.connection = CdpConnection(http_endpoint)
        self._context = CdpContext(self.connection)

    async def probe(self) -> dict:
        """确认端点可用；返回浏览器版本信息。失败时抛出明确的 CdpError。"""
        version = await asyncio.to_thread(self.connection.call, 'Browser.getVersion')
        return version or {}

    def default_context(self) -> CdpContext:
        return self._context

    def is_connected(self) -> bool:
        try:
            self.connection.call('Browser.getVersion', timeout=5)
            return True
        except CdpError:
            return False

    def close(self):
        self.connection.close()
