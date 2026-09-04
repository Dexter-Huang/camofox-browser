"""GEO RPA protocol 的 Python/Camoufox 实现。

此服务是现有 ``backend.app.rpa.camofox_browser`` 的受限 HTTP adapter。它不提供
通用 evaluate、浏览器调试端口或任意网络请求能力；所有操作都必须归属到一个已知
``userId`` 的 tab。账号状态沿用 Node sidecar 的 StorageState 文件布局，方便 PoC
复用同一个 Docker volume 而不迁移 Chromium 数据。服务始终声明 v3；部署显式启用
``ENABLE_WINDOW_PUBLISHER=1`` 后，窗口路由才会验证并发布 X11 单窗口。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from camoufox import DefaultAddons
from camoufox.async_api import AsyncCamoufox
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from playwright.async_api import Browser, BrowserContext, Locator, Page, Response as PwResponse

from app.window_vnc import (
    WindowPublisher,
    read_x11_window_tree,
    top_level_window_ids,
    wait_for_new_x11_window_id,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 3
LOCATOR_TIMEOUT_MS = 3_000
SUBMISSION_CLICK_TIMEOUT_MS = 12_000
MAX_SELECTOR_LENGTH = 1_024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_CAPTURES_PER_TAB = 2
MAX_MANUAL_TEXT_LENGTH = 10_000
MAX_COORDINATE = 10_000
MAX_WHEEL_DELTA = 10_000
SESSION_CLOSE_TIMEOUT_SECONDS = 15
TAB_CREATE_TIMEOUT_SECONDS = 95
VNC_DEFAULT_RESOLUTION = "1920x1080x24"
WINDOW_PUBLISHER_WS_BASE_PORT = 6082
WINDOW_PUBLISHER_RFB_OFFSET = 180
MANUAL_WINDOW_RFB_PORT = 5901
MANUAL_WINDOW_WS_PORT = 6081
MAX_TABS_PER_SESSION = int(os.getenv("MAX_TABS_PER_SESSION", "3"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", os.getenv("RPA_BROWSER_CAPACITY", "30")))
MAX_TABS_GLOBAL = int(os.getenv("MAX_TABS_GLOBAL", os.getenv("RPA_BROWSER_CAPACITY", "30")))
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_MS", "300000")) / 1_000
TAB_INACTIVITY_SECONDS = int(os.getenv("TAB_INACTIVITY_MS", "300000")) / 1_000
ALLOWED_HOST_SUFFIXES = (
    "doubao.com",
    "kimi.com",
    "qianwen.com",
    "yuanbao.tencent.com",
    "deepseek.com",
    "yiyan.baidu.com",
    "bot.sannysoft.com",
    "abrahamjuliot.github.io",
    "browserleaks.com",
    "bot.incolumitas.com",
)
MANUAL_KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown",
    "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Insert",
    "a", "c", "v", "x", "y", "z", "A", "C", "V", "X", "Y", "Z",
}
MODIFIERS = {"Alt", "Control", "Meta", "Shift"}
CONTROLLED_RPA_ERROR_CODES = {"network_capture_active", "submission_not_dispatched"}


class ProtocolError(Exception):
    """HTTP handler 可安全返回给 Python adapter 的受控失败。"""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(message)


def env_flag(name: str, default: bool = False) -> bool:
    """只接受部署配置中的显式布尔值，避免拼写错误意外开放可见桌面。"""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes"}


def bounded_port(name: str, default: int) -> int:
    """VNC 状态端点不因部署变量拼写错误返回 500。"""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if 1 <= value <= 65_535 else default


@dataclass
class Capture:
    capture_id: str
    spec: dict[str, Any]
    state: str = "waiting"
    armed: bool = False
    generation: int = 0
    status: int | None = None
    content_type: str = ""
    body: bytes = b""


@dataclass
class TabState:
    tab_id: str
    page: Page
    session: SessionState
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    captures: dict[str, Capture] = field(default_factory=dict)
    last_access: float = field(default_factory=time.monotonic)


@dataclass
class SessionState:
    user_id: str
    context: BrowserContext
    tabs: dict[str, TabState] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_access: float = field(default_factory=time.monotonic)


@dataclass
class ManagedWindow:
    """服务内部维护的原生 Firefox 窗口。

    ``window_id`` 只用于容器内 x11vnc 绑定，不能进入 HTTP 响应、异常正文或日志。
    """

    handle: str
    user_id: str
    tab: TabState
    window_id: str
    kind: str
    state: str = "window_ready"
    publisher: WindowPublisher | None = None
    websocket_port: int | None = None


class BrowserService:
    """隐藏 Camoufox 生命周期、账号隔离、StorageState 与受限网络采集的深模块。"""

    def __init__(self) -> None:
        self.browser: Browser | None = None
        self._camoufox: AsyncCamoufox | None = None
        self.sessions: dict[str, SessionState] = {}
        self.sessions_lock = asyncio.Lock()
        # 同一账号的所有关闭入口共用一个任务。公开索引先删除，新的 session_for
        # 必须等待该任务完成，避免旧 Firefox Context 在后台继续写入同一个 profile。
        self.closing_sessions: dict[str, asyncio.Task[None]] = {}
        self._browser_restart_lock = asyncio.Lock()
        self.browser_ready = False
        self.background_tasks: list[asyncio.Task[None]] = []
        self.profile_dir = Path(os.getenv("CAMOFOX_PROFILE_DIR", "/home/node/.camofox/profiles"))
        self.vnc_enabled = env_flag("ENABLE_VNC")
        # 整桌面 VNC 仅供旧调试用途。v3 只在独立窗口发布器可用时对后端声明。
        self.window_publisher_enabled = env_flag("ENABLE_WINDOW_PUBLISHER")
        self.x11_display = os.getenv("CAMOFOX_VNC_DISPLAY", ":99")
        self.persistence_ready = False
        self.windows: dict[str, ManagedWindow] = {}
        self._window_handles_by_tab: dict[tuple[str, str], str] = {}
        self._window_lock = asyncio.Lock()
        self._next_window_ws_port = bounded_port(
            "WINDOW_PUBLISHER_WS_BASE_PORT", WINDOW_PUBLISHER_WS_BASE_PORT
        )

    async def start(self) -> None:
        # A v3 sidecar must provide account-scoped native windows. Advertising a
        # ready browser without the publisher would let the backend accept an
        # incomplete deployment, so keep readiness false until it is explicit.
        if not self.window_publisher_enabled:
            logger.error("Window publisher must be enabled for GEO RPA protocol v3")
            return
        # Dockerfile has already installed the pinned official Camoufox release
        # into this service account's package-manager cache.  Excluding the
        # default UBO addon ensures startup never reaches the network.
        self._camoufox = AsyncCamoufox(
            # 全桌面调试和 v3 窗口发布均复用 entrypoint 创建的同一 Xvfb；
            # 普通自动化实例继续以 headless 模式运行，避免额外 X11 开销。
            headless=not (self.vnc_enabled or self.window_publisher_enabled),
            exclude_addons=list(DefaultAddons),
        )
        try:
            launched = await self._camoufox.__aenter__()
            if not hasattr(launched, "new_context"):
                raise RuntimeError("Camoufox did not return a Browser")
            self.browser = launched
            # 预热只证明浏览器可以创建上下文，不会加载第三方页面或写入账号状态。
            context = await launched.new_context()
            await context.close()
            self.persistence_ready = await self._check_profile_storage()
            self.browser_ready = True
            if not self.background_tasks:
                self.background_tasks.append(asyncio.create_task(self._idle_reaper()))
        except Exception:
            logger.exception("Camoufox prewarm failed")

    async def close(self) -> None:
        for task in self.background_tasks:
            task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_tasks.clear()
        for user_id in list(self.sessions):
            await self.close_session(user_id)
        if self._camoufox is not None:
            with suppress(Exception):
                await self._camoufox.__aexit__(None, None, None)
        self.browser = None
        self.browser_ready = False
        self.persistence_ready = False

    async def _check_profile_storage(self) -> bool:
        """探测 profile volume 可写性，不接触任何账号的 StorageState 文件。"""
        probe = self.profile_dir / f".write-check-{uuid4().hex}"
        try:
            await asyncio.to_thread(self.profile_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(probe.write_bytes, b"")
            await asyncio.to_thread(probe.unlink)
            return True
        except OSError:
            with suppress(OSError):
                await asyncio.to_thread(probe.unlink)
            logger.warning("Profile storage is not writable")
            return False

    @property
    def protocol_version(self) -> int:
        """Python sidecar 始终实现并声明 GEO RPA protocol v3。"""
        return PROTOCOL_VERSION

    async def _idle_reaper(self) -> None:
        """按旧 sidecar 的会话和 Tab 闲置策略回收浏览器内存。

        仅回收没有正在执行操作的 Tab；Context 先从公开索引摘除，再等待
        Tab 锁完成，避免清理线程把已失效的页面交给并发的 RPA 或人工输入。
        """
        while True:
            await asyncio.sleep(min(max(min(SESSION_TIMEOUT_SECONDS, TAB_INACTIVITY_SECONDS) / 4, 5), 30))
            now = time.monotonic()
            for user_id, session in list(self.sessions.items()):
                if now - session.last_access >= SESSION_TIMEOUT_SECONDS:
                    if not any(tab.lock.locked() for tab in session.tabs.values()):
                        await self.close_session(user_id)
                    continue

                for tab in list(session.tabs.values()):
                    if tab.lock.locked() or now - tab.last_access < TAB_INACTIVITY_SECONDS:
                        continue
                    await self.close_tab(tab)

                if not session.tabs:
                    await self.close_session(user_id)

    def _state_path(self, user_id: str) -> Path:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        return self.profile_dir / digest / "storage-state.json"

    async def _load_state(self, user_id: str) -> dict[str, Any] | None:
        path = self._state_path(user_id)
        try:
            raw = await asyncio.to_thread(path.read_text, "utf-8")
            state = json.loads(raw)
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                raise ValueError("StorageState cookies are invalid")
            if "origins" in state and not isinstance(state["origins"], list):
                raise ValueError("StorageState origins are invalid")
            return state
        except FileNotFoundError:
            return None
        except Exception:
            quarantine = path.with_name(f"storage-state.invalid-{int(time.time())}.json")
            with suppress(Exception):
                await asyncio.to_thread(path.replace, quarantine)
            logger.warning("Invalid StorageState isolated")
            return None

    async def _save_state(self, session: SessionState) -> None:
        path = self._state_path(session.user_id)
        try:
            state = await session.context.storage_state(indexed_db=False)
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            await asyncio.to_thread(temporary.write_text, payload, "utf-8")
            await asyncio.to_thread(temporary.replace, path)
        except Exception:
            # 登录态内容绝不写入日志，checkpoint 失败也不能阻止 Context 正常关闭。
            logger.exception("StorageState checkpoint failed")

    async def session_for(self, user_id: str) -> SessionState:
        if not user_id or len(user_id) > 256:
            raise ProtocolError(400, "userId is required")
        while True:
            async with self.sessions_lock:
                existing = self.sessions.get(user_id)
                closing = self.closing_sessions.get(user_id)
                if closing is not None and closing.done():
                    # The close task normally removes itself in finally. Retire a
                    # completed barrier here as well so a cancelled shutdown task
                    # cannot make a later session request spin indefinitely.
                    self.closing_sessions.pop(user_id, None)
                    closing = None
                if existing is not None:
                    existing.last_access = time.monotonic()
                    return existing
            if closing is not None:
                await asyncio.shield(closing)
                continue
            async with self.sessions_lock:
                # Re-check after acquiring the creation lock: a concurrent close may
                # have just installed its barrier while this request was waiting.
                if user_id in self.closing_sessions:
                    continue
                existing = self.sessions.get(user_id)
                if existing is not None:
                    existing.last_access = time.monotonic()
                    return existing
                if self.browser is None or not self.browser_ready:
                    raise ProtocolError(503, "Camoufox Browser is not ready")
                if len(self.sessions) >= MAX_SESSIONS:
                    raise ProtocolError(429, "Maximum sessions reached")
                state = await self._load_state(user_id)
                try:
                    context = await self.browser.new_context(storage_state=state) if state else await self.browser.new_context()
                except Exception:
                    if state is None:
                        raise ProtocolError(503, "Camoufox Browser could not create a session") from None
                    path = self._state_path(user_id)
                    quarantine = path.with_name(f"storage-state.invalid-{int(time.time())}.json")
                    with suppress(Exception):
                        await asyncio.to_thread(path.replace, quarantine)
                    context = await self.browser.new_context()
                session = SessionState(user_id=user_id, context=context)
                self.sessions[user_id] = session
                return session

    async def close_session(self, user_id: str) -> None:
        async with self.sessions_lock:
            closing = self.closing_sessions.get(user_id)
            if closing is None:
                session = self.sessions.pop(user_id, None)
                if session is None:
                    return
                closing = asyncio.create_task(self._finalize_session_close(session))
                self.closing_sessions[user_id] = closing
        await asyncio.shield(closing)

    async def _finalize_session_close(self, session: SessionState) -> None:
        """完成单个 Context 的关闭屏障，并在关闭无法确认时恢复浏览器进程。"""
        try:
            async with session.lock:
                await self._close_windows_for_user(session.user_id)
                for tab in list(session.tabs.values()):
                    await self.close_tab(tab)
                await self._save_state(session)
                try:
                    await asyncio.wait_for(session.context.close(), SESSION_CLOSE_TIMEOUT_SECONDS)
                except Exception:
                    logger.warning("Camoufox session close timed out; restarting browser")
                    await self._restart_browser()
        finally:
            async with self.sessions_lock:
                self.closing_sessions.pop(session.user_id, None)

    async def _restart_browser(self) -> None:
        """隔离无法关闭的 Context，重建单一浏览器进程后才允许新的账号会话。"""
        async with self._browser_restart_lock:
            # A browser restart invalidates every Context in the old process. Remove
            # their public indexes before terminating it, so future requests cannot
            # use stale pages or write a profile while recovery is in progress.
            async with self.sessions_lock:
                stale_sessions = list(self.sessions.values())
                self.sessions.clear()
                self.browser_ready = False
                self.persistence_ready = False
            for stale in stale_sessions:
                await self._close_windows_for_user(stale.user_id)
            if self._camoufox is not None:
                with suppress(Exception):
                    await self._camoufox.__aexit__(None, None, None)
            self._camoufox = None
            self.browser = None
            await self.start()
        async with session.lock:
            await self._close_windows_for_user(user_id)
            for tab in list(session.tabs.values()):
                await self.close_tab(tab)
            await self._save_state(session)
            with suppress(Exception):
                await session.context.close()

    async def create_tab(self, user_id: str, url: str) -> TabState:
        ensure_allowed_url(url)
        task = asyncio.create_task(self._create_tab(user_id, url))
        try:
            return await asyncio.wait_for(asyncio.shield(task), TAB_CREATE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), SESSION_CLOSE_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("Camoufox tab creation did not cancel; restarting browser")
                await self._restart_browser()
            await self.close_session(user_id)
            raise ProtocolError(504, "Tab creation timed out") from exc

    async def _create_tab(self, user_id: str, url: str) -> TabState:
        session = await self.session_for(user_id)
        page: Page | None = None
        async with session.lock:
            if self.sessions.get(user_id) is not session:
                raise ProtocolError(409, "Session is closing")
            if len(session.tabs) >= MAX_TABS_PER_SESSION or self.tab_count >= MAX_TABS_GLOBAL:
                raise ProtocolError(429, "Maximum tabs reached")
            try:
                page = await session.context.new_page()
                if self.sessions.get(user_id) is not session:
                    await page.close()
                    raise ProtocolError(409, "Session is closing")
                tab = self._register_page(session, page)
                await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                if self.sessions.get(user_id) is not session:
                    await self.close_tab(tab)
                    raise ProtocolError(409, "Session is closing")
                return tab
            except ProtocolError:
                raise
            except Exception:
                if page is not None and not page.is_closed():
                    with suppress(Exception):
                        await page.close()
                raise ProtocolError(502, "Tab navigation failed") from None

    def _register_page(self, session: SessionState, page: Page) -> TabState:
        """将 Context 内新建的受控页面纳入同一账号的 Tab 索引。"""
        tab = TabState(tab_id=uuid4().hex, page=page, session=session)
        session.tabs[tab.tab_id] = tab
        page.on("response", lambda response: asyncio.create_task(self._on_response(tab, response)))
        return tab

    async def promote_tab_to_window(self, user_id: str, tab_id: str, *, kind: str) -> ManagedWindow:
        """把尚未执行的账号 Tab 转为独立 Firefox popup，并绑定其原生 X11 窗口。"""
        if kind not in {"manual", "task"}:
            raise ProtocolError(400, "Window type is invalid")
        if not self.window_publisher_enabled:
            raise ProtocolError(409, "X11 window publisher is unavailable")
        tab = await self.owned_tab(user_id, tab_id)
        async with self._window_lock:
            if kind == "manual" and any(window.kind == "manual" for window in self.windows.values()):
                raise ProtocolError(409, "A manual window already exists")
            async with tab.session.lock:
                async with tab.lock:
                    if tab.session.tabs.get(tab_id) is not tab:
                        raise ProtocolError(404, "Tab not found")
                    popup: Page | None = None
                    try:
                        existing_window_ids = set(
                            top_level_window_ids(await read_x11_window_tree(self.x11_display))
                        )
                        features = (
                            "popup=yes,width=1920,height=1009"
                            if kind == "manual"
                            else "popup=yes,width=1440,height=900"
                        )
                        async with tab.page.expect_popup(timeout=5_000) as popup_event:
                            opened = await tab.page.evaluate(
                                """features => {
                                    const popup = window.open('about:blank', '_blank', features);
                                    return Boolean(popup);
                                }""",
                                features,
                            )
                        if opened is not True:
                            raise RuntimeError("Firefox blocked the controlled popup")
                        popup = await popup_event.value
                        title = f"GEO_RPA_WINDOW_{uuid4().hex}"
                        await popup.evaluate("title => { document.title = title; }", title)
                        await popup.goto(tab.page.url, wait_until="commit", timeout=90_000)
                        window_id = await wait_for_new_x11_window_id(
                            self.x11_display, existing_window_ids
                        )
                    except Exception as exc:
                        if popup is not None:
                            with suppress(Exception):
                                await popup.close()
                        raise ProtocolError(409, "Unable to create the controlled X11 window") from exc

                    if len(tab.session.tabs) >= MAX_TABS_PER_SESSION:
                        with suppress(Exception):
                            await popup.close()
                        raise ProtocolError(429, "Maximum tabs reached")
                    target_tab = self._register_page(tab.session, popup)
                    handle = uuid4().hex
                    window = ManagedWindow(
                        handle=handle,
                        user_id=user_id,
                        tab=target_tab,
                        window_id=window_id,
                        kind=kind,
                    )
                    self.windows[handle] = window
                    self._window_handles_by_tab[(user_id, target_tab.tab_id)] = handle
                    popup.on(
                        "close",
                        lambda: asyncio.create_task(self._on_window_page_closed(handle)),
                    )
                    # 新 popup 已登记后才撤销源 Tab，确保后端永远拿到有效 targetId。
                    tab.session.tabs.pop(tab.tab_id, None)
                    tab.captures.clear()
                    with suppress(Exception):
                        await tab.page.close()
                    return window

    async def _on_window_page_closed(self, handle: str) -> None:
        """Firefox 主动关闭 popup 时回收发布器和账号内 Tab 索引。"""
        async with self._window_lock:
            window = self.windows.pop(handle, None)
            if window is None:
                return
            self._window_handles_by_tab.pop((window.user_id, window.tab.tab_id), None)
            if window.tab.session.tabs.get(window.tab.tab_id) is window.tab:
                window.tab.session.tabs.pop(window.tab.tab_id, None)
        if window.publisher is not None:
            await window.publisher.stop()

    async def _close_windows_for_user(self, user_id: str) -> None:
        handles = [
            handle for handle, window in self.windows.items() if window.user_id == user_id
        ]
        for handle in handles:
            await self.close_managed_window(handle, user_id)

    async def close_managed_window(self, handle: str, user_id: str) -> bool:
        """幂等结束窗口租约，先撤销 publisher 再关闭该账号 popup。"""
        async with self._window_lock:
            window = self.windows.get(handle)
            if window is None or window.user_id != user_id:
                return False
            self.windows.pop(handle, None)
            self._window_handles_by_tab.pop((user_id, window.tab.tab_id), None)
            if window.tab.session.tabs.get(window.tab.tab_id) is window.tab:
                window.tab.session.tabs.pop(window.tab.tab_id, None)
        if window.publisher is not None:
            await window.publisher.stop()
        if not window.tab.page.is_closed():
            with suppress(Exception):
                await window.tab.page.close()
        return True

    def _next_task_window_ports(self) -> tuple[int, int]:
        """为观察器分配容器内唯一端口对；端口永不返回给浏览器前端。"""
        websocket_port = self._next_window_ws_port
        rfb_port = websocket_port - WINDOW_PUBLISHER_RFB_OFFSET
        if not 1024 <= rfb_port <= 65535 or websocket_port > 65535:
            raise ProtocolError(503, "Window publisher port range is exhausted")
        self._next_window_ws_port += 1
        return rfb_port, websocket_port

    async def publish_window(self, handle: str, user_id: str) -> ManagedWindow:
        """启动或复用指定窗口的私有 RFB/WebSocket 发布器。"""
        async with self._window_lock:
            window = self.windows.get(handle)
            if window is None or window.user_id != user_id or window.tab.page.is_closed():
                raise ProtocolError(404, "Window not found")
            if window.publisher is not None and window.publisher.running:
                return window
            if window.publisher is not None:
                await window.publisher.stop()
                window.publisher = None
                window.websocket_port = None
            if window.kind == "manual":
                rfb_port = bounded_port("MANUAL_WINDOW_RFB_PORT", MANUAL_WINDOW_RFB_PORT)
                websocket_port = bounded_port("MANUAL_WINDOW_WS_PORT", MANUAL_WINDOW_WS_PORT)
            else:
                rfb_port, websocket_port = self._next_task_window_ports()
            publisher = WindowPublisher(
                display=self.x11_display,
                window_id=window.window_id,
                rfb_port=rfb_port,
                websocket_port=websocket_port,
            )
            try:
                await publisher.start()
            except Exception as exc:
                window.state = "failed"
                raise ProtocolError(409, "Window publisher could not start") from exc
            window.publisher = publisher
            window.websocket_port = websocket_port
            window.state = "published"
            return window

    async def unpublish_window(self, handle: str, user_id: str) -> bool:
        """停止观察器传输，不关闭仍由 RPA 使用的任务窗口。"""
        async with self._window_lock:
            window = self.windows.get(handle)
            if window is None or window.user_id != user_id:
                return False
            publisher = window.publisher
            window.publisher = None
            window.websocket_port = None
            if window.state != "failed":
                window.state = "window_ready"
        if publisher is not None:
            await publisher.stop()
        return True

    @property
    def tab_count(self) -> int:
        return sum(len(session.tabs) for session in self.sessions.values())

    async def owned_tab(self, user_id: str, tab_id: str) -> TabState:
        session = self.sessions.get(user_id)
        tab = session.tabs.get(tab_id) if session else None
        if tab is None:
            raise ProtocolError(404, "Tab not found")
        session.last_access = time.monotonic()
        tab.last_access = session.last_access
        return tab

    async def close_tab(self, tab: TabState) -> None:
        handle = self._window_handles_by_tab.get((tab.session.user_id, tab.tab_id))
        if handle is not None:
            await self.close_managed_window(handle, tab.session.user_id)
            return
        async with tab.lock:
            # 删除 API 与闲置回收可能竞争同一个 Tab；只有仍属于该 session
            # 的对象才可关闭，保证迟到的清理不会影响随后新建的 Tab。
            if tab.session.tabs.get(tab.tab_id) is not tab:
                return
            tab.session.tabs.pop(tab.tab_id, None)
            tab.captures.clear()
            with suppress(Exception):
                await tab.page.close()

    async def _on_response(self, tab: TabState, response: PwResponse) -> None:
        for capture in list(tab.captures.values()):
            if capture.state != "waiting" or not capture.armed or not response_matches(response, capture.spec):
                continue
            capture.armed = False
            capture.state = "streaming"
            capture.generation += 1
            generation = capture.generation
            capture.status = response.status
            capture.content_type = response.headers.get("content-type", "")
            try:
                body = await response.body()
                if len(body) > capture.spec["maxBytes"]:
                    raise ValueError("response body exceeded byte limit")
                if capture.generation == generation:
                    capture.body = body
                    capture.state = "complete"
            except Exception:
                if capture.generation == generation:
                    capture.state = "failed"


service = BrowserService()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await service.start()
    try:
        yield
    finally:
        await service.close()


app = FastAPI(title="GEO Python Camoufox Browser", lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)


@app.exception_handler(ProtocolError)
async def protocol_error_response(_: Request, error: ProtocolError) -> JSONResponse:
    """将所有受控输入错误稳定地映射为 adapter 已识别的错误包络。"""
    payload: dict[str, str] = {"error": error.message}
    if error.code:
        payload["code"] = error.code
    return JSONResponse(status_code=error.status_code, content=payload)


@app.exception_handler(HTTPException)
async def http_exception_response(_: Request, error: HTTPException) -> JSONResponse:
    """保持 Node sidecar 的顶层 ``error``/``code`` 错误体，不能泄露 FastAPI 的 ``detail`` 包装。"""
    if isinstance(error.detail, dict) and isinstance(error.detail.get("error"), str):
        return JSONResponse(status_code=error.status_code, content=error.detail)
    return JSONResponse(status_code=error.status_code, content={"error": str(error.detail)})


@app.exception_handler(RequestValidationError)
async def validation_error_response(_: Request, __: RequestValidationError) -> JSONResponse:
    """FastAPI 默认 422 的 ``detail`` 结构不属于 GEO RPA v3 协议。"""
    return JSONResponse(status_code=400, content={"error": "Request parameters are invalid"})


def safe_error(error: Exception) -> str:
    """Keep Playwright, URL, account, and X11 details out of the HTTP protocol."""
    del error
    return "Browser operation failed"


def error_response(error: ProtocolError) -> HTTPException:
    detail: dict[str, str] = {"error": error.message}
    if error.code in CONTROLLED_RPA_ERROR_CODES:
        detail["code"] = error.code
    return HTTPException(error.status_code, detail=detail)


async def request_object(request: Request) -> dict[str, Any]:
    """读取 JSON object，拒绝数组或标量，避免未校验的 ``.get`` 触发 500。"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(400, "Request JSON is invalid") from exc
    if not isinstance(body, dict):
        raise ProtocolError(400, "Request JSON object is required")
    return body


@asynccontextmanager
async def tab_operation(user_id: str, tab_id: str) -> AsyncIterator[TabState]:
    """按账号锁、Tab 锁的固定顺序执行一次 RPA 操作。

    BrowserContext 会共享账号的 Cookie 与 StorageState，因此同一账号的多个
    tab 也必须串行。重新确认归属可拦住在 ``owned_tab`` 与取得锁之间被闲置
    回收或显式关闭的页面。
    """
    tab = await service.owned_tab(user_id, tab_id)
    async with tab.session.lock:
        async with tab.lock:
            if tab.session.tabs.get(tab_id) is not tab:
                raise ProtocolError(404, "Tab not found")
            tab.session.last_access = time.monotonic()
            tab.last_access = tab.session.last_access
            yield tab


def require_string(value: Any, name: str, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError(400, f"{name} is required")
    return value


def ensure_allowed_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES):
        raise ProtocolError(400, "URL is not in the GEO RPA provider allowlist")


def target_locator(tab: TabState, target: Any) -> Locator:
    if not isinstance(target, dict):
        raise ProtocolError(400, "locator target is required")
    selector = target.get("selector")
    text = target.get("text")
    valid_selector = isinstance(selector, str) and bool(selector.strip()) and len(selector) <= MAX_SELECTOR_LENGTH
    valid_text = isinstance(text, str) and bool(text) and len(text) <= 4_096
    if valid_selector == valid_text:
        raise ProtocolError(400, "locator target requires exactly one selector or text")
    index = target.get("index", 0)
    if not isinstance(index, int) or isinstance(index, bool) or index < -1:
        raise ProtocolError(400, "locator index is invalid")
    locator = tab.page.locator(selector) if valid_selector else tab.page.get_by_text(text, exact=True)
    return locator.last if index == -1 else locator.nth(index)


async def visible_locator(tab: TabState, target: Any) -> Locator:
    locator = target_locator(tab, target)
    if await locator.count() == 0:
        raise ProtocolError(400, "locator not found")
    return locator


def normalize_spec(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProtocolError(400, "capture spec is required")
    method = raw.get("method", "").upper() if isinstance(raw.get("method"), str) else ""
    host = raw.get("host", "").strip().lower() if isinstance(raw.get("host"), str) else ""
    path = raw.get("path", "").strip() if isinstance(raw.get("path"), str) else ""
    prefix = raw.get("pathPrefix", "").strip() if isinstance(raw.get("pathPrefix"), str) else ""
    maximum = raw.get("maxBytes", MAX_CAPTURE_BYTES)
    if not method.isalpha() or not host or not path.startswith("/") or (prefix and not prefix.startswith("/")):
        raise ProtocolError(400, "capture spec method, host, and path are required")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 0 < maximum <= MAX_CAPTURE_BYTES:
        raise ProtocolError(400, "capture spec maxBytes is invalid")
    return {"method": method, "host": host, "path": path.rstrip("/") or "/", "pathPrefix": prefix or None, "maxBytes": maximum, "activateOnSubmit": raw.get("activateOnSubmit") is True}


def response_matches(response: PwResponse, spec: dict[str, Any]) -> bool:
    parsed = urlsplit(response.url)
    if response.request.method.upper() != spec["method"] or parsed.hostname is None or parsed.hostname.lower() != spec["host"]:
        return False
    path = parsed.path.rstrip("/") or "/"
    return path.startswith(spec["pathPrefix"]) if spec["pathPrefix"] else path == spec["path"]


def arm_submit_captures(tab: TabState) -> None:
    for capture in tab.captures.values():
        if capture.state == "waiting" and capture.spec["activateOnSubmit"]:
            capture.armed = True


async def submit_near(tab: TabState, target: Any) -> None:
    prompt = await visible_locator(tab, target)
    prompt_box = await prompt.bounding_box()
    if prompt_box is None:
        raise ProtocolError(400, "input locator has no visible bounds")
    controls = tab.page.locator("button, [role='button']")
    selected: Locator | None = None
    rightmost = float("-inf")
    for index in range(min(await controls.count(), 128)):
        candidate = controls.nth(index)
        if not await candidate.is_visible(timeout=LOCATOR_TIMEOUT_MS) or not await candidate.is_enabled(timeout=LOCATOR_TIMEOUT_MS):
            continue
        box = await candidate.bounding_box()
        if box is None:
            continue
        adjacent = box["y"] + box["height"] >= prompt_box["y"] - 64 and box["y"] <= prompt_box["y"] + prompt_box["height"] + 64
        right = box["x"] + box["width"]
        if adjacent and box["x"] >= prompt_box["x"] + prompt_box["width"] / 2 and right > rightmost:
            selected, rightmost = candidate, right
    if selected is None:
        raise ProtocolError(400, "input-adjacent submit control not found")
    arm_submit_captures(tab)
    await selected.click(timeout=SUBMISSION_CLICK_TIMEOUT_MS, no_wait_after=True)


@app.get("/health")
async def health() -> dict[str, Any]:
    running = service.browser is not None and service.browser.is_connected()
    return {
        "ok": True,
        "engine": "camoufox",
        "geoRpaProtocolVersion": service.protocol_version,
        "browserConnected": running,
        "browserRunning": running,
        "browserReady": service.browser_ready,
        "persistenceReady": service.persistence_ready,
        "activeTabs": service.tab_count,
        "activeSessions": len(service.sessions),
        "activeWindows": len(service.windows),
        "consecutiveFailures": 0,
        "vncEnabled": service.vnc_enabled,
        "windowPublisherEnabled": service.window_publisher_enabled,
    }


@app.get("/vnc/status")
async def vnc_status() -> dict[str, Any]:
    """仅返回 VNC 传输状态，不返回密码、会话或浏览器页面数据。"""
    return {
        "enabled": service.vnc_enabled,
        "running": service.vnc_enabled and service.browser_ready,
        "vncPort": bounded_port("VNC_PORT", 5900),
        "novncPort": bounded_port("NOVNC_PORT", 6080),
        "path": "/vnc.html",
    }


@app.post("/tabs")
async def create_tab(request: Request) -> dict[str, str]:
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        require_string(body.get("sessionKey") or body.get("listItemId"), "sessionKey", 256)
        tab = await service.create_tab(user_id, require_string(body.get("url"), "url"))
        return {"tabId": tab.tab_id, "url": tab.page.url}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/tabs")
async def list_tabs(userId: str) -> dict[str, Any]:
    session = service.sessions.get(userId)
    return {"tabs": [] if session is None else [{"tabId": tab.tab_id, "url": tab.page.url} for tab in session.tabs.values()]}


@app.delete("/tabs/{tab_id}")
async def delete_tab(tab_id: str, request: Request) -> dict[str, bool]:
    body = await request_object(request) if request.headers.get("content-type", "").startswith("application/json") else {}
    user_id = request.query_params.get("userId") or body.get("userId")
    try:
        tab = await service.owned_tab(require_string(user_id, "userId", 256), tab_id)
        async with tab.session.lock:
            await service.close_tab(tab)
        return {"ok": True}
    except ProtocolError as exc:
        # Node close is idempotent; retain that behaviour for stale cleanup paths.
        if exc.status_code == 404:
            return {"ok": True}
        raise error_response(exc) from exc


@app.delete("/sessions/{user_id}")
async def delete_session(user_id: str) -> dict[str, bool]:
    await service.close_session(user_id)
    return {"ok": True}


@app.post("/rpa/tabs/{tab_id}/manual-window")
async def create_manual_window(tab_id: str, request: Request) -> dict[str, str]:
    """创建唯一的人工作业窗口，并立即发布其固定内部 VNC 通道。"""
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        window = await service.promote_tab_to_window(user_id, tab_id, kind="manual")
        try:
            window = await service.publish_window(window.handle, user_id)
        except ProtocolError:
            await service.close_managed_window(window.handle, user_id)
            raise
        return {"handle": window.handle, "state": window.state, "targetId": window.tab.tab_id}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/rpa/manual-windows/{handle}")
async def manual_window_status(handle: str, userId: str) -> dict[str, str]:
    window = service.windows.get(handle)
    if window is None or window.user_id != userId or window.kind != "manual" or window.tab.page.is_closed():
        raise error_response(ProtocolError(404, "Manual window not found"))
    return {"state": window.state}


@app.delete("/rpa/manual-windows/{handle}")
async def delete_manual_window(handle: str, request: Request) -> Response:
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        window = service.windows.get(handle)
        if window is None or window.user_id != user_id or window.kind != "manual":
            raise ProtocolError(404, "Manual window not found")
        await service.close_managed_window(handle, user_id)
        return Response(status_code=204)
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/tabs/{tab_id}/task-window")
async def create_task_window(tab_id: str, request: Request) -> dict[str, str]:
    """在 provider 执行前把目标 Tab 转为可按窗口观察的原生 popup。"""
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        window = await service.promote_tab_to_window(user_id, tab_id, kind="task")
        return {"handle": window.handle, "state": window.state, "targetId": window.tab.tab_id}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/task-windows/{handle}/observer")
async def open_task_observer(handle: str, request: Request) -> dict[str, int | str]:
    """按需发布任务窗口；端口仅返回给后端的容器内代理。"""
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        window = service.windows.get(handle)
        if window is None or window.user_id != user_id or window.kind != "task":
            raise ProtocolError(404, "Task window not found")
        window = await service.publish_window(handle, user_id)
        if window.websocket_port is None:
            raise ProtocolError(409, "Task observer is unavailable")
        return {"state": window.state, "websocketPort": window.websocket_port}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.delete("/rpa/task-windows/{handle}/observer")
async def close_task_observer(handle: str, request: Request) -> Response:
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        window = service.windows.get(handle)
        if window is None or window.user_id != user_id or window.kind != "task":
            raise ProtocolError(404, "Task window not found")
        await service.unpublish_window(handle, user_id)
        return Response(status_code=204)
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/tabs/{tab_id}/navigate")
async def navigate(tab_id: str, request: Request) -> dict[str, str]:
    body = await request_object(request)
    try:
        url = require_string(body.get("url"), "url")
        ensure_allowed_url(url)
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            await tab.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        return {"url": tab.page.url}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/tabs/{tab_id}/{action}")
async def tab_action(tab_id: str, action: str, request: Request) -> dict[str, bool]:
    body = await request_object(request)
    try:
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            if action == "type":
                locator = await visible_locator(tab, {"selector": require_string(body.get("selector"), "selector", MAX_SELECTOR_LENGTH)})
                await locator.fill(require_string(body.get("text"), "text", MAX_MANUAL_TEXT_LENGTH), timeout=10_000)
            elif action == "press":
                key = require_string(body.get("key"), "key", 64)
                if key not in {"Enter", "End"}:
                    raise ProtocolError(400, "key is not allowed")
                await tab.page.keyboard.press(key)
            else:
                raise ProtocolError(404, "Tab action not found")
        return {"ok": True}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/tabs/{tab_id}/screenshot")
async def screenshot(tab_id: str, userId: str, fullPage: bool = False) -> Response:
    try:
        async with tab_operation(userId, tab_id) as tab:
            image = await tab.page.screenshot(full_page=fullPage, type="png")
        return Response(image, media_type="image/png")
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/tabs/{tab_id}/snapshot")
async def snapshot(tab_id: str, userId: str) -> dict[str, str]:
    try:
        async with tab_operation(userId, tab_id) as tab:
            body = tab.page.locator("body")
            try:
                value = await body.aria_snapshot(timeout=5_000)
            except Exception:
                value = await body.inner_text(timeout=5_000)
        return {"snapshot": value}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/tabs/{tab_id}/network-captures")
async def create_capture(tab_id: str, request: Request) -> dict[str, str]:
    body = await request_object(request)
    try:
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            if len(tab.captures) >= MAX_CAPTURES_PER_TAB:
                raise ProtocolError(429, "Too many response captures")
            spec = normalize_spec(body.get("spec"))
            capture = Capture(capture_id=uuid4().hex, spec=spec, armed=not spec["activateOnSubmit"])
            tab.captures[capture.capture_id] = capture
            return {"captureId": capture.capture_id}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/rpa/tabs/{tab_id}/network-captures/{capture_id}")
async def capture_status(tab_id: str, capture_id: str, userId: str) -> Response:
    try:
        async with tab_operation(userId, tab_id) as tab:
            capture = tab.captures.get(capture_id)
            if capture is None:
                raise ProtocolError(404, "Network capture not found")
            if capture.state in {"waiting", "streaming"}:
                return Response(json.dumps({"state": capture.state}), status_code=202, media_type="application/json")
            if capture.state == "failed":
                return Response(json.dumps({"state": "failed"}), media_type="application/json")
            return Response(json.dumps({"state": "complete", "status": capture.status, "contentType": capture.content_type, "bodyBase64": base64.b64encode(capture.body).decode("ascii")}), media_type="application/json")
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/tabs/{tab_id}/network-captures/{capture_id}/reset")
async def reset_capture(tab_id: str, capture_id: str, request: Request) -> dict[str, bool]:
    body = await request_object(request)
    try:
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            capture = tab.captures.get(capture_id)
            if capture is None:
                raise ProtocolError(404, "Network capture not found")
            if capture.state not in {"complete", "failed"}:
                raise ProtocolError(409, "Network capture is still active", "network_capture_active")
            capture.state, capture.status, capture.content_type, capture.body = "waiting", None, "", b""
            capture.armed = not capture.spec["activateOnSubmit"]
            capture.generation += 1
            return {"ok": True}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.delete("/rpa/tabs/{tab_id}/network-captures/{capture_id}")
async def delete_capture(tab_id: str, capture_id: str, request: Request) -> dict[str, bool]:
    body = await request_object(request)
    try:
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            if tab.captures.pop(capture_id, None) is None:
                raise ProtocolError(404, "Network capture not found")
            return {"ok": True}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/rpa/tabs/{tab_id}/current-url")
async def current_url(tab_id: str, userId: str) -> dict[str, str]:
    try:
        async with tab_operation(userId, tab_id) as tab:
            return {"url": tab.page.url}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/rpa/tabs/{tab_id}/viewport")
async def viewport(tab_id: str, userId: str) -> dict[str, int | bool]:
    try:
        async with tab_operation(userId, tab_id) as tab:
            size = tab.page.viewport_size
            if size is None:
                box = await tab.page.locator("html").bounding_box()
                if box is None:
                    raise ProtocolError(409, "Tab viewport is unavailable")
                size = {"width": round(box["width"]), "height": round(box["height"])}
            return {"ok": True, "width": size["width"], "height": size["height"]}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/tabs/{tab_id}/manual-input")
async def manual_input(tab_id: str, request: Request) -> dict[str, bool]:
    body = await request_object(request)
    try:
        event = body.get("event")
        if not isinstance(event, dict):
            raise ProtocolError(400, "manual input event is required")
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            await dispatch_manual_input(tab.page, event)
        return {"ok": True}
    except ProtocolError as exc:
        raise error_response(exc) from exc


async def dispatch_manual_input(page: Page, event: dict[str, Any]) -> None:
    kind = event.get("kind")
    if kind == "text":
        text = event.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= MAX_MANUAL_TEXT_LENGTH:
            raise ProtocolError(400, "manual text is invalid")
        await page.keyboard.insert_text(text)
        return
    if kind == "key":
        key, modifiers = event.get("key"), event.get("modifiers", [])
        if not isinstance(key, str) or (key not in MANUAL_KEYS and key not in MODIFIERS) or not isinstance(modifiers, list) or any(item not in MODIFIERS for item in modifiers):
            raise ProtocolError(400, "manual key is invalid")
        if key not in MODIFIERS:
            await page.keyboard.press("+".join([*modifiers, key]))
        return
    if kind != "mouse":
        raise ProtocolError(400, "manual input kind is invalid")
    event_type, x, y = event.get("type"), event.get("x"), event.get("y")
    if event_type not in {"move", "down", "up", "wheel"} or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= MAX_COORDINATE for value in (x, y)):
        raise ProtocolError(400, "manual mouse coordinates are invalid")
    await page.mouse.move(float(x), float(y), steps=1)
    if event_type == "wheel":
        dx, dy = event.get("delta_x"), event.get("delta_y")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) <= MAX_WHEEL_DELTA for value in (dx, dy)):
            raise ProtocolError(400, "manual wheel delta is invalid")
        await page.mouse.wheel(float(dx), float(dy))
    elif event_type == "down":
        await page.mouse.down(button=event.get("button") if event.get("button") in {"left", "middle", "right"} else "left")
    elif event_type == "up":
        await page.mouse.up(button=event.get("button") if event.get("button") in {"left", "middle", "right"} else "left")


@app.post("/rpa/tabs/{tab_id}/locator-read")
async def locator_read(tab_id: str, request: Request) -> dict[str, Any]:
    body = await request_object(request)
    try:
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            operation = body.get("operation")
            locator = target_locator(tab, body.get("target"))
            count = await locator.count()
            soft = {"visible", "enabled", "editable", "has_value_property", "content_editable"}
            if operation == "count":
                return {"ok": True, "result": count}
            if count == 0 and operation in soft:
                return {"ok": True, "result": False}
            locator = await visible_locator(tab, body.get("target"))
            if operation == "visible": result = await locator.is_visible(timeout=LOCATOR_TIMEOUT_MS)
            elif operation == "enabled": result = await locator.is_enabled(timeout=LOCATOR_TIMEOUT_MS)
            elif operation == "editable": result = await locator.is_editable(timeout=LOCATOR_TIMEOUT_MS)
            elif operation == "inner_text": result = await locator.inner_text(timeout=LOCATOR_TIMEOUT_MS)
            elif operation == "input_value": result = await locator.input_value(timeout=LOCATOR_TIMEOUT_MS)
            elif operation == "has_value_property": result = await locator.evaluate("node => 'value' in node")
            elif operation == "content_editable": result = await locator.evaluate("node => node.isContentEditable")
            elif operation == "generation_stop_visible":
                # 仅检查输入框右侧相邻的停止/取消控件，不读取正文，也不执行调用方脚本。
                prompt_box = await locator.bounding_box()
                if prompt_box is None:
                    result = False
                else:
                    controls = tab.page.locator("button, [role='button']")
                    result = False
                    for index in range(min(await controls.count(), 128)):
                        control = controls.nth(index)
                        if not await control.is_visible(timeout=LOCATOR_TIMEOUT_MS):
                            continue
                        box = await control.bounding_box()
                        if box is None or box["x"] < prompt_box["x"] + prompt_box["width"] / 2:
                            continue
                        if box["y"] + box["height"] < prompt_box["y"] or box["y"] > prompt_box["y"] + prompt_box["height"]:
                            continue
                        semantics = " ".join(
                            str(value or "")
                            for value in await control.evaluate(
                                "node => [node.getAttribute('aria-label'), node.getAttribute('title'), node.getAttribute('data-testid'), node.className]"
                            )
                        ).lower()
                        if any(marker in semantics for marker in ("stop", "cancel", "abort", "pause", "停止", "终止", "取消")):
                            result = True
                            break
            elif operation == "scroll_into_view": await locator.scroll_into_view_if_needed(timeout=LOCATOR_TIMEOUT_MS); result = True
            elif operation == "answer_markdown":
                result = await locator.evaluate("""node => ({
                    markdown: (node.innerText || '').trim(),
                    citations: [...node.querySelectorAll('a[href]')].map(link => ({url: new URL(link.getAttribute('href'), document.baseURI).href, title: (link.innerText || link.getAttribute('title') || '').trim()})).filter((value, index, values) => /^https?:/.test(value.url) && values.findIndex(item => item.url === value.url) === index)
                })""")
            else:
                raise ProtocolError(400, "locator operation is not allowed")
            return {"ok": True, "result": result}
    except ProtocolError as exc:
        raise error_response(exc) from exc
    except Exception as exc:
        raise HTTPException(400, {"error": safe_error(exc)}) from exc


@app.post("/rpa/tabs/{tab_id}/locator-screenshot")
async def locator_screenshot(tab_id: str, request: Request) -> Response:
    """截取工作流声明的 CSS Locator 当前边界，不滚动页面或执行任意脚本。"""
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        target = body.get("target")
        if not isinstance(target, dict) or not isinstance(target.get("selector"), str) or not target["selector"].strip() or target.get("text") is not None:
            raise ProtocolError(400, "locator screenshot requires a CSS selector target")
        async with tab_operation(user_id, tab_id) as tab:
            locator = await visible_locator(tab, target)
            clip = await locator.bounding_box(timeout=LOCATOR_TIMEOUT_MS)
            if not clip or clip["width"] <= 0 or clip["height"] <= 0:
                raise ProtocolError(400, "answer locator has no visible bounds")
            image = await tab.page.screenshot(type="png", clip=clip, timeout=LOCATOR_TIMEOUT_MS)
        return Response(image, media_type="image/png")
    except ProtocolError as exc:
        raise error_response(exc) from exc
    except Exception as exc:
        raise HTTPException(400, {"error": safe_error(exc)}) from exc


@app.post("/rpa/tabs/{tab_id}/locator-focus")
@app.post("/rpa/tabs/{tab_id}/locator-key")
@app.post("/rpa/tabs/{tab_id}/locator-input")
@app.post("/rpa/tabs/{tab_id}/locator-click")
@app.post("/rpa/tabs/{tab_id}/locator-submit")
async def locator_action(tab_id: str, request: Request) -> dict[str, bool]:
    body = await request_object(request)
    action = request.url.path.rsplit("/", 1)[-1]
    try:
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            if action == "locator-submit":
                await submit_near(tab, body.get("target"))
            else:
                locator = await visible_locator(tab, body.get("target"))
                if action == "locator-focus": await locator.focus(timeout=LOCATOR_TIMEOUT_MS)
                elif action == "locator-key":
                    if body.get("key") != "Enter": raise ProtocolError(400, "locator key is not allowed")
                    await locator.press("Enter", timeout=LOCATOR_TIMEOUT_MS)
                elif action == "locator-input":
                    text = require_string(body.get("text"), "locator input text", 4_096)
                    await locator.press_sequentially(text, delay=0, timeout=LOCATOR_TIMEOUT_MS)
                elif action == "locator-click":
                    try:
                        await locator.click(timeout=SUBMISSION_CLICK_TIMEOUT_MS, no_wait_after=True)
                    except Exception as exc:
                        code = "submission_not_dispatched" if "intercepts pointer events" in str(exc) else None
                        raise ProtocolError(400, safe_error(exc), code) from exc
        return {"ok": True}
    except ProtocolError as exc:
        raise error_response(exc) from exc
