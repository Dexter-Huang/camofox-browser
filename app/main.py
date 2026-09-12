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
from typing import Any, Literal, TypedDict, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

from camoufox import DefaultAddons
from camoufox.async_api import AsyncCamoufox
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from playwright.async_api import Browser, BrowserContext, Locator, Page, Response as PwResponse

from app.provider_automation import (
    Citation,
    NetworkAnswerListener,
    PLATFORM_EXECUTION_TIMEOUT_SECONDS,
    NETWORK_FIRST_RESPONSE_TIMEOUT_SECONDS,
    answer_dom_baseline,
    ProviderAutomationError,
    ProviderName,
    conversation_url,
    prepare as prepare_provider,
    rule_for,
    submit as submit_provider,
    wait_result,
)
from app.session_probe import probe_browser_context
from app.window_vnc import (
    WindowPublisher,
    read_x11_window_tree,
    top_level_window_ids,
    wait_for_new_x11_window_id,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 4
LOCATOR_TIMEOUT_MS = 3_000
SUBMISSION_CLICK_TIMEOUT_MS = 12_000
MAX_SELECTOR_LENGTH = 1_024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_CAPTURES_PER_TAB = 2
MAX_MANUAL_TEXT_LENGTH = 10_000
MAX_COORDINATE = 10_000
MAX_WHEEL_DELTA = 10_000
SESSION_CLOSE_TIMEOUT_SECONDS = 15
STORAGE_STATE_TIMEOUT_SECONDS = 12
STORAGE_STATE_MAX_BYTES = 2 * 1024 * 1024
TAB_CLOSE_TIMEOUT_SECONDS = 8
TAB_CREATE_TIMEOUT_SECONDS = 95
BROWSER_WATCHDOG_INTERVAL_SECONDS = max(
    1.0, float(os.getenv("BROWSER_WATCHDOG_INTERVAL_SECONDS", "5"))
)
VNC_DEFAULT_RESOLUTION = "1920x1080x24"
WINDOW_PUBLISHER_RFB_BASE_PORT = 5902
WINDOW_PUBLISHER_WS_BASE_PORT = 6082
# ``goto(..., wait_until="commit")`` 只说明导航已被 Firefox 接收，尚不能保证 Xvfb
# 已合成首帧。人工 VNC 直接附着该原生窗口，过早启动 x11vnc 会把短暂的未绘制区域
# 传给用户。此等待只用于人工窗口，不让任务窗口的发布时序发生变化。
MANUAL_WINDOW_PAINT_READY_TIMEOUT_MS = 5_000
MANUAL_WINDOW_PAINT_SETTLE_SECONDS = 0.35
# 单 Firefox/Xvfb 部署中，人工用户不需要 100fps 的 VNC。x11vnc 的 -wait/-defer
# 使用毫秒，这里限制为约 25fps，降低长期人工认证对后台 Tab 绘制和编码的抢占。
MANUAL_WINDOW_VNC_CAPTURE_WAIT_MS = 40
MANUAL_WINDOW_VNC_CAPTURE_DEFER_MS = 40
# 创建 Context、导航、生成原生 popup 和发现 X11 window id 都会在短时间内占用
# 单 Firefox/Xvfb 的主线程与合成资源。这里只限制初始化并发；窗口发布完成后立即
# 释放槽位，不限制后续同时在线和操作的人工 VNC 数量。
MANUAL_WINDOW_CREATE_CONCURRENCY = max(
    1, min(8, int(os.getenv("MANUAL_WINDOW_CREATE_CONCURRENCY", "2")))
)
# 人工窗口包含独立 Firefox popup、x11vnc 与 WebSocket bridge；该限制必须和
# GEO API 的 RPA_MANUAL_SESSION_MAX_CONCURRENT 使用同一部署值。
# 0 表示不设置人工窗口硬上限；正整数时必须与 GEO API 的部署配置一致。
MAX_MANUAL_WINDOWS = int(os.getenv("MAX_MANUAL_WINDOWS", "0"))
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
CONTROLLED_RPA_ERROR_CODES = {
    "provider_error",
    "login_required",
    "verification_required",
    "page_unavailable",
    "answer_timeout",
    "execution_not_found",
    "network_capture_active",
    "submission_not_dispatched",
    # 提交控件被平台对话框遮挡时，调用方必须保留账号会话等待人工处理，不能把
    # Playwright 的页面细节泄露给应用层，更不能将一次未派发的提交自动重放。
    "manual_intervention_required",
}


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


def vnc_window_geometry() -> tuple[int, int, int]:
    """返回单个发布窗口的受控宽、高和色深。"""
    parts = os.getenv("VNC_RESOLUTION", VNC_DEFAULT_RESOLUTION).split("x")
    try:
        width, height, depth = (int(part) for part in parts)
    except ValueError:
        return 1920, 1080, 24
    if (
        not 800 <= width <= 3840
        or not 600 <= height <= 2160
        or depth not in {16, 24, 32}
    ):
        return 1920, 1080, 24
    return width, height, depth


def window_slot_coordinates(slot_index: int) -> tuple[int, int]:
    """把窗口放到共享 Xvfb 的独立槽位，避免顶层窗口相互遮挡。"""
    width, height, _ = vnc_window_geometry()
    try:
        configured_columns = int(os.getenv("WINDOW_PUBLISHER_GRID_COLUMNS", "5"))
    except ValueError:
        configured_columns = 5
    columns = max(1, min(16, configured_columns))
    slot = max(0, slot_index)
    return (slot % columns) * width, (slot // columns) * height


def manual_window_features(slot_index: int = 0) -> str:
    """按 Xvfb 画布生成受限的人工 popup 几何。

    降低 ``VNC_RESOLUTION`` 可减少活跃人工窗口的像素编码量；非法值始终回退到
    已验收的 1920x1080，避免 popup 与 Xvfb 尺寸失配而暴露灰色未绘制区域。
    """
    width, height, _ = vnc_window_geometry()
    left, top = window_slot_coordinates(slot_index)
    # Firefox 的原生窗口 chrome 占用约 71 像素；窗口内容仍由 Xvfb 分辨率决定。
    # 显式关闭地址栏、工具栏和菜单栏，避免 VNC 画面暴露浏览器导航 chrome。
    # 仍为 Firefox 原生标题栏预留约 71 像素，防止 popup 超出 Xvfb 画布导致底部裁剪。
    return (
        f"popup=yes,width={width},height={max(1, height - 71)},left={left},top={top},"
        "location=no,toolbar=no,menubar=no,status=no,personalbar=no,"
        "scrollbars=yes,resizable=yes"
    )


def task_window_features(slot_index: int) -> str:
    """任务观察窗口与人工窗口共用槽位网格，防止互相覆盖。"""
    cell_width, cell_height, _ = vnc_window_geometry()
    left, top = window_slot_coordinates(slot_index)
    return (
        f"popup=yes,width={min(1440, cell_width)},"
        f"height={min(900, max(1, cell_height - 71))},left={left},top={top}"
    )


async def wait_for_manual_window_paint(page: Page) -> None:
    """等待人工 popup 的首帧进入 X11 合成队列。

    人工窗口在导航提交后即可获得 X11 window id，但此时 ``x11vnc`` 若立即连接，
    首个 framebuffer 常为短暂的黑色或未绘制区域。这里不等待 ``load`` 或网络空闲，
    以免登录页的长连接、广告或流式资源阻塞人工认证；只等待 DOM 就绪、连续两帧
    ``requestAnimationFrame``，再给 Xvfb 一个很短的合成稳定时间。

    页面重定向、关闭或站点异常不会阻断人工会话创建。调用方会在发布前再次确认
    页面仍然存在，失败页面则走既有的窗口未找到语义。
    """
    try:
        await page.wait_for_load_state(
            "domcontentloaded", timeout=MANUAL_WINDOW_PAINT_READY_TIMEOUT_MS
        )
        await page.evaluate(
            """() => new Promise(resolve => requestAnimationFrame(
                () => requestAnimationFrame(resolve)
            ))"""
        )
    except Exception as exc:
        logger.info(
            "CAMOFOX_MANUAL_WINDOW_PAINT stage=best_effort error_type=%s",
            type(exc).__name__,
        )
    if not page.is_closed():
        await asyncio.sleep(MANUAL_WINDOW_PAINT_SETTLE_SECONDS)


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
    slot_index: int = 0
    state: str = "window_ready"
    publisher: WindowPublisher | None = None
    rfb_port: int | None = None
    websocket_port: int | None = None


ExecutionState = Literal["prepared", "submitting", "generating", "completed", "failed", "cancelled"]


class AutomationResult(TypedDict, total=False):
    """平台自动化可以安全返回给 SaaS 的标准结果。"""

    citations: list[Citation]
    conversationUrl: str


class ExecutionResult(TypedDict):
    answerMarkdown: str
    result: AutomationResult


class ExecutionError(TypedDict):
    code: str
    message: str


def lower_camel_case(field_name: str) -> str:
    """将 Python 内部 snake_case 字段稳定映射为协议使用的 lowerCamelCase。"""
    first, *rest = field_name.split("_")
    return first + "".join(part.capitalize() for part in rest)


class V4Request(BaseModel):
    """v4 仅接受声明过的任务级字段，拒绝任何浏览器细节字段。"""

    # 统一在模型级生成协议别名，避免旧版 FastAPI 在字段级 alias 元数据上产生
    # Pydantic 告警。Python 内部仍只使用清晰的 snake_case 名称。
    model_config = ConfigDict(
        alias_generator=lower_camel_case,
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class ExecutionCreateRequest(V4Request):
    """创建执行请求；字段均由后端调度生成，不能携带 URL 或 Selector。"""

    execution_id: str = Field(min_length=1, max_length=256)
    provider: ProviderName
    profile_key: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1, max_length=20_000)
    debug: bool = False


class AccountRequest(V4Request):
    """账号级高层操作的最小参数。"""

    provider: ProviderName
    profile_key: str = Field(min_length=1, max_length=256)


class ProfileKeyRequest(V4Request):
    """人工会话 checkpoint 和关闭操作只需隔离配置档键。"""

    profile_key: str = Field(min_length=1, max_length=256)

class AccountSessionHydrateRequest(V4Request):
    """用 GEO 持有的 Cookie + LocalStorage 快照创建账号 Context。"""

    profile_key: str = Field(min_length=1, max_length=256)
    storage_state: dict[str, Any] = Field(default_factory=dict)


# 路由在运行时自行解析 request，避免 FastAPI 0.115 与新 Pydantic 的字段别名告警；
# 同时显式复用模型 schema，保证运维仍可在 OpenAPI 中看到固定的 provider 枚举与字段边界。
EXECUTION_CREATE_OPENAPI_SCHEMA = ExecutionCreateRequest.model_json_schema(
    by_alias=True
)


def profile_log_id(user_id: str) -> str:
    """返回可跨日志关联、但不泄露账号标识的短哈希。"""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]


class KeepalivePayload(TypedDict):
    status: Literal["ok", "login_required", "verification_required", "uncertain"]


class ManualSessionPayload(TypedDict):
    sessionHandle: str
    state: str


class BooleanPayload(TypedDict):
    ok: bool


class AccountSessionCheckpointPayload(TypedDict, total=False):
    ok: bool
    storageState: dict[str, Any]


V4RequestModel = TypeVar("V4RequestModel", bound=V4Request)


class ExecutionPayload(TypedDict, total=False):
    executionId: str
    state: ExecutionState
    result: ExecutionResult
    error: ExecutionError
    failureScreenshotBase64: str


@dataclass
class Execution:
    """不落库的单次任务执行；进程重启即丢失，后端必须保守收敛。"""

    execution_id: str
    provider: ProviderName
    profile_key: str
    query: str
    tab: TabState
    state: ExecutionState = "prepared"
    result: ExecutionResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    failure_screenshot: bytes | None = None
    task: asyncio.Task[None] | None = None


class BrowserService:
    """隐藏 Camoufox 生命周期、账号隔离、StorageState 与受限网络采集的深模块。"""

    def __init__(self) -> None:
        self.browser: Browser | None = None
        self._camoufox: AsyncCamoufox | None = None
        self.sessions: dict[str, SessionState] = {}
        self.sessions_lock = asyncio.Lock()
        # 同一账号的所有关闭入口共用一个任务。公开索引先删除，新的 session_for
        # 必须等待该任务完成，避免旧 Firefox Context 在后台继续被复用。
        self.closing_sessions: dict[str, asyncio.Task[None]] = {}
        self._browser_restart_lock = asyncio.Lock()
        self._browser_restart_task: asyncio.Task[None] | None = None
        self._browser_watchdog_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self.browser_ready = False
        self.background_tasks: list[asyncio.Task[None]] = []
        self.vnc_enabled = env_flag("ENABLE_VNC")
        # 整桌面 VNC 仅供旧调试用途。v3 只在独立窗口发布器可用时对后端声明。
        self.window_publisher_enabled = env_flag("ENABLE_WINDOW_PUBLISHER")
        self.x11_display = os.getenv("CAMOFOX_VNC_DISPLAY", ":99")
        self.persistence_ready = False
        self.windows: dict[str, ManagedWindow] = {}
        self._window_handles_by_tab: dict[tuple[str, str], str] = {}
        self._window_lock = asyncio.Lock()
        self._manual_window_create_semaphore = asyncio.Semaphore(
            MANUAL_WINDOW_CREATE_CONCURRENCY
        )
        self._next_window_rfb_port = bounded_port(
            "WINDOW_PUBLISHER_RFB_BASE_PORT", WINDOW_PUBLISHER_RFB_BASE_PORT
        )
        self._next_window_ws_port = bounded_port(
            "WINDOW_PUBLISHER_WS_BASE_PORT", WINDOW_PUBLISHER_WS_BASE_PORT
        )
        self.executions: dict[str, Execution] = {}
        self._execution_lock = asyncio.Lock()

    def _next_window_slot(self) -> int:
        """返回当前 Xvfb 网格中最小的空闲槽位。"""
        used_slots = {window.slot_index for window in self.windows.values()}
        for slot_index in range(max(0, MAX_TABS_GLOBAL)):
            if slot_index not in used_slots:
                return slot_index
        raise ProtocolError(429, "Window publisher capacity is exhausted")

    async def start(self) -> None:
        self._shutting_down = False
        # A v4 sidecar must provide account-scoped native windows. Advertising a
        # ready browser without the publisher would let the backend accept an
        # incomplete deployment, so keep readiness false until it is explicit.
        if not self.window_publisher_enabled:
            logger.error("Window publisher must be enabled for GEO RPA protocol v4")
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
            # 登录快照由 GEO MySQL 持有；sidecar 就绪只表示可以创建内存 Context。
            self.persistence_ready = True
            self.browser_ready = True
            if not self.background_tasks:
                self.background_tasks.append(asyncio.create_task(self._idle_reaper()))
            if self._browser_watchdog_task is None or self._browser_watchdog_task.done():
                self._browser_watchdog_task = asyncio.create_task(
                    self._browser_watchdog(), name="camoufox-browser-watchdog"
                )
        except Exception:
            self.browser_ready = False
            logger.exception("Camoufox prewarm failed")

    async def close(self) -> None:
        self._shutting_down = True
        if self._browser_watchdog_task is not None:
            self._browser_watchdog_task.cancel()
            await asyncio.gather(self._browser_watchdog_task, return_exceptions=True)
            self._browser_watchdog_task = None
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

    @property
    def protocol_version(self) -> int:
        """Python sidecar 始终实现并声明 GEO RPA protocol v4。"""
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

                # 不能因为本轮保活或任务刚好关闭了最后一个 Tab，就立即关闭账号
                # Context。后端可能正在为同一账号排队下一项保活/任务；立刻摘除
                # session 会让已通过 session_for() 的并发请求在创建 Tab 时收到
                # ``Session is closing``。空 Context 仍会在 SESSION_TIMEOUT_SECONDS
                # 到期后统一回收，既保留账号隔离，也避免这一短暂竞争窗口。

    async def _browser_watchdog(self) -> None:
        """检测 Camoufox 子进程被 OOM 等原因杀死后，自动重建浏览器。

        Docker 的 ``restart: unless-stopped`` 只能处理容器主进程退出；
        Camoufox 被 cgroup OOM Killer 杀死时，Uvicorn 仍可能存活，导致
        ``/health`` 返回 200 但所有 ``new_context`` 请求持续失败。因此这里
        由 sidecar 主动观察 Playwright 连接状态并触发一次受控重启。
        """
        while not self._shutting_down:
            await asyncio.sleep(BROWSER_WATCHDOG_INTERVAL_SECONDS)
            browser = self.browser
            if (
                self._shutting_down
                or not self.browser_ready
                or browser is None
                or browser.is_connected()
            ):
                continue
            logger.error(
                "CAMOFOX_BROWSER_WATCHDOG browser_disconnected active_sessions=%s active_tabs=%s; restarting",
                len(self.sessions),
                self.tab_count,
            )
            restart_task = self._request_browser_restart()
            await asyncio.shield(restart_task)

    def _request_browser_restart(self) -> asyncio.Task[None]:
        """合并并发的浏览器恢复请求，避免 OOM 后重复启动多个实例。"""
        task = self._browser_restart_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._restart_browser(), name="camoufox-browser-restart"
            )
            self._browser_restart_task = task
        return task

    def _normalized_storage_state(self, value: object) -> dict[str, Any] | None:
        """Return a Playwright-ready snapshot, or None for an empty/logged-out state."""
        if not isinstance(value, dict):
            return None
        cookies = value.get("cookies")
        if cookies is None:
            return None
        if not isinstance(cookies, list):
            return None
        origins = value.get("origins")
        if origins is not None and not isinstance(origins, list):
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        if len(encoded.encode("utf-8")) > STORAGE_STATE_MAX_BYTES:
            return None
        return value

    async def _create_browser_context(
        self, storage_state: dict[str, Any] | None
    ) -> BrowserContext:
        if self.browser is None or not self.browser_ready:
            raise ProtocolError(503, "Camoufox Browser is not ready")
        try:
            if storage_state is None:
                return await self.browser.new_context()
            return await self.browser.new_context(storage_state=storage_state)
        except Exception as exc:
            browser = self.browser
            disconnected = browser is None or not browser.is_connected()
            target_closed = (
                "target" in str(exc).lower()
                and "closed" in str(exc).lower()
            )
            if disconnected or target_closed:
                self.browser_ready = False
                self.persistence_ready = False
                self._request_browser_restart()
                raise ProtocolError(
                    503, "Camoufox Browser could not create a session"
                ) from None
            if storage_state is None:
                raise ProtocolError(503, "Camoufox Browser could not create a session") from None
            try:
                return await self.browser.new_context()
            except Exception:
                raise ProtocolError(503, "Camoufox Browser could not create a session") from None

    async def _export_storage_state(self, session: SessionState) -> dict[str, Any] | None:
        """Export Cookie + LocalStorage only; never write a local profile file."""
        started_at = time.monotonic()
        log_id = profile_log_id(session.user_id)
        logger.info("CAMOFOX_CHECKPOINT stage=started profile=%s", log_id)
        try:
            state = await asyncio.wait_for(
                session.context.storage_state(indexed_db=False),
                timeout=STORAGE_STATE_TIMEOUT_SECONDS,
            )
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                logger.warning(
                    "CAMOFOX_CHECKPOINT stage=invalid profile=%s elapsed_ms=%s",
                    log_id,
                    int((time.monotonic() - started_at) * 1000),
                )
                return None
            logger.info(
                "CAMOFOX_CHECKPOINT stage=completed profile=%s elapsed_ms=%s",
                log_id,
                int((time.monotonic() - started_at) * 1000),
            )
            return state
        except TimeoutError:
            logger.warning(
                "CAMOFOX_CHECKPOINT stage=timed_out profile=%s timeout_seconds=%s elapsed_ms=%s",
                log_id,
                STORAGE_STATE_TIMEOUT_SECONDS,
                int((time.monotonic() - started_at) * 1000),
            )
            return None
        except Exception:
            logger.exception(
                "CAMOFOX_CHECKPOINT stage=failed profile=%s elapsed_ms=%s",
                log_id,
                int((time.monotonic() - started_at) * 1000),
            )
            return None

    async def hydrate_account_session(
        self, profile_key: str, storage_state: object
    ) -> None:
        """仅在账号尚无内存 Context 时应用 GEO 快照；已有会话忽略入站快照。"""
        await self._session_for(
            profile_key,
            storage_state=self._normalized_storage_state(storage_state),
            apply_storage_state=True,
        )

    async def checkpoint_account_session(self, profile_key: str) -> dict[str, Any]:
        """Return the live snapshot so GEO can persist it; 404 if no Context exists."""
        session = self.sessions.get(profile_key)
        if session is None:
            raise ProtocolError(404, "Account session not found")
        session.last_access = time.monotonic()
        state = await self._export_storage_state(session)
        if state is None:
            raise ProtocolError(503, "Camofox Browser could not persist the authenticated session")
        return state

    async def session_for(self, user_id: str) -> SessionState:
        return await self._session_for(user_id, storage_state=None, apply_storage_state=False)

    async def _session_for(
        self,
        user_id: str,
        *,
        storage_state: dict[str, Any] | None,
        apply_storage_state: bool,
    ) -> SessionState:
        if not user_id or len(user_id) > 256:
            raise ProtocolError(400, "userId is required")
        while True:
            async with self.sessions_lock:
                existing = self.sessions.get(user_id)
                closing = self.closing_sessions.get(user_id)
                if closing is not None and closing.done():
                    self.closing_sessions.pop(user_id, None)
                    closing = None
                if existing is not None:
                    existing.last_access = time.monotonic()
                    return existing
            if closing is not None:
                await asyncio.shield(closing)
                continue
            async with self.sessions_lock:
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
                # Hydrate applies GEO snapshot only when creating a Context.
                # Later tab/execution calls reuse the live session and ignore inbound state.
                context = await self._create_browser_context(
                    storage_state if apply_storage_state else None
                )
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
        started_at = time.monotonic()
        log_id = profile_log_id(session.user_id)
        logger.info("CAMOFOX_SESSION_CLOSE stage=started profile=%s", log_id)
        try:
            async with session.lock:
                await self._close_windows_for_user(session.user_id)
                for tab in list(session.tabs.values()):
                    await self.close_tab(tab)
                try:
                    await asyncio.wait_for(session.context.close(), SESSION_CLOSE_TIMEOUT_SECONDS)
                except Exception:
                    logger.warning(
                        "CAMOFOX_SESSION_CLOSE stage=context_close_failed profile=%s elapsed_ms=%s; restarting browser",
                        log_id,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    await self._restart_browser()
                else:
                    logger.info(
                        "CAMOFOX_SESSION_CLOSE stage=completed profile=%s elapsed_ms=%s",
                        log_id,
                        int((time.monotonic() - started_at) * 1000),
                    )
        finally:
            async with self.sessions_lock:
                self.closing_sessions.pop(session.user_id, None)

    async def _restart_browser(self) -> None:
        """隔离无法关闭的 Context，重建单一浏览器进程后才允许新的账号会话。"""
        try:
            async with self._browser_restart_lock:
                # A browser restart invalidates every Context in the old process. Remove
                # their public indexes before terminating it, so future requests cannot
                # reuse stale pages while recovery is in progress.
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
        finally:
            if self._browser_restart_task is asyncio.current_task():
                self._browser_restart_task = None

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
            manual_window_count = sum(
                window.kind == "manual" for window in self.windows.values()
            )
            if (
                kind == "manual"
                and MAX_MANUAL_WINDOWS > 0
                and manual_window_count >= MAX_MANUAL_WINDOWS
            ):
                raise ProtocolError(429, "Manual window capacity is exhausted")
            async with tab.session.lock:
                async with tab.lock:
                    if tab.session.tabs.get(tab_id) is not tab:
                        raise ProtocolError(404, "Tab not found")
                    popup: Page | None = None
                    window_id_task: asyncio.Task[str] | None = None
                    slot_index = self._next_window_slot()
                    create_stage = "inspect_windows"
                    try:
                        existing_window_ids = set(
                            top_level_window_ids(await read_x11_window_tree(self.x11_display))
                        )
                        create_stage = "open_popup"
                        features = (
                            manual_window_features(slot_index)
                            if kind == "manual"
                            else task_window_features(slot_index)
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
                        create_stage = "set_popup_title"
                        title = f"GEO_RPA_WINDOW_{uuid4().hex}"
                        await popup.evaluate("title => { document.title = title; }", title)
                        # 原生 popup 映射到 X11 后即可并行查找窗口；导航无需等待该结果。
                        window_id_task = asyncio.create_task(
                            wait_for_new_x11_window_id(self.x11_display, existing_window_ids)
                        )
                        create_stage = "navigate_popup"
                        await popup.goto(tab.page.url, wait_until="commit", timeout=90_000)
                        create_stage = "discover_window"
                        window_id = await window_id_task
                    except Exception as exc:
                        if window_id_task is not None:
                            if not window_id_task.done():
                                window_id_task.cancel()
                            with suppress(asyncio.CancelledError, Exception):
                                await window_id_task
                        if popup is not None:
                            with suppress(Exception):
                                await popup.close()
                        logger.warning(
                            "CAMOFOX_MANUAL_WINDOW_CREATE stage=failed profile=%s "
                            "create_stage=%s error_type=%s",
                            profile_log_id(user_id),
                            create_stage,
                            type(exc).__name__,
                        )
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
                        slot_index=slot_index,
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

    async def close_all_manual_windows(self) -> int:
        """回收无 API 所有者的人工窗口，供应用重启后的受控恢复使用。"""
        windows = [
            (handle, window.user_id)
            for handle, window in self.windows.items()
            if window.kind == "manual"
        ]
        for handle, user_id in windows:
            await self.close_managed_window(handle, user_id)
        return len(windows)

    async def close_managed_window(self, handle: str, user_id: str) -> bool:
        """幂等结束窗口租约，先撤销 publisher 再关闭该账号 popup。"""
        started_at = time.monotonic()
        log_id = profile_log_id(user_id)
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
        logger.info(
            "CAMOFOX_MANUAL_WINDOW_CLOSE stage=started profile=%s handle=%s",
            log_id,
            handle,
        )
        if window.tab.page.is_closed():
            logger.info(
                "CAMOFOX_MANUAL_WINDOW_CLOSE stage=already_closed profile=%s handle=%s elapsed_ms=%s",
                log_id,
                handle,
                int((time.monotonic() - started_at) * 1000),
            )
            return True
        try:
            await asyncio.wait_for(
                window.tab.page.close(), timeout=TAB_CLOSE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # 即使 popup 的 Juggler RPC 卡住，后续 Context close 仍会尝试回收；
            # 删除 Tab 的 HTTP 调用不能因此无限等待。
            logger.warning(
                "CAMOFOX_MANUAL_WINDOW_CLOSE stage=timed_out profile=%s handle=%s "
                "timeout_seconds=%s elapsed_ms=%s",
                log_id,
                handle,
                TAB_CLOSE_TIMEOUT_SECONDS,
                int((time.monotonic() - started_at) * 1000),
            )
        except Exception:
            logger.warning(
                "CAMOFOX_MANUAL_WINDOW_CLOSE stage=failed profile=%s handle=%s elapsed_ms=%s",
                log_id,
                handle,
                int((time.monotonic() - started_at) * 1000),
                exc_info=True,
            )
        else:
            logger.info(
                "CAMOFOX_MANUAL_WINDOW_CLOSE stage=completed profile=%s handle=%s elapsed_ms=%s",
                log_id,
                handle,
                int((time.monotonic() - started_at) * 1000),
            )
        return True

    def _next_window_ports(
        self, *, websocket_required: bool
    ) -> tuple[int, int | None]:
        """为单个账号窗口分配容器内唯一端口对。

        人工认证与任务观察共用这一分配器，保证不同账号绝不会复用同一 RFB 或
        WebSocket 上游；端口只返回给应用后端代理，绝不进入浏览器前端。
        """
        rfb_port = self._next_window_rfb_port
        websocket_port = self._next_window_ws_port if websocket_required else None
        if (
            not 1024 <= rfb_port < WINDOW_PUBLISHER_WS_BASE_PORT
            or (websocket_port is not None and websocket_port > 65535)
        ):
            raise ProtocolError(503, "Window publisher port range is exhausted")
        self._next_window_rfb_port += 1
        if websocket_port is not None:
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
                window.rfb_port = None
                window.websocket_port = None
            if window.kind == "manual":
                # VNC 发布器会立即读取该 X11 窗口；先完成轻量首帧门槛，避免客户
                # 首次连入时把 Firefox/Xvfb 尚未合成的黑色 framebuffer 误认为掉线。
                await wait_for_manual_window_paint(window.tab.page)
                if window.tab.page.is_closed():
                    raise ProtocolError(404, "Window not found")
            rfb_port, websocket_port = self._next_window_ports(
                websocket_required=window.kind == "task"
            )
            publisher = WindowPublisher(
                display=self.x11_display,
                window_id=window.window_id,
                rfb_port=rfb_port,
                websocket_port=websocket_port,
                expose_rfb_to_docker_network=window.kind == "manual",
                capture_wait_ms=(
                    MANUAL_WINDOW_VNC_CAPTURE_WAIT_MS
                    if window.kind == "manual"
                    else 10
                ),
                capture_defer_ms=(
                    MANUAL_WINDOW_VNC_CAPTURE_DEFER_MS
                    if window.kind == "manual"
                    else 10
                ),
            )
            try:
                await publisher.start()
            except Exception as exc:
                window.state = "failed"
                logger.warning(
                    "CAMOFOX_WINDOW_PUBLISH stage=failed profile=%s error_type=%s",
                    profile_log_id(user_id),
                    type(exc).__name__,
                )
                raise ProtocolError(409, "Window publisher could not start") from exc
            window.publisher = publisher
            window.rfb_port = rfb_port
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
            window.rfb_port = None
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
            started_at = time.monotonic()
            log_id = profile_log_id(tab.session.user_id)
            logger.info(
                "CAMOFOX_TAB_CLOSE stage=started profile=%s tab_id=%s", log_id, tab.tab_id
            )
            try:
                await asyncio.wait_for(tab.page.close(), timeout=TAB_CLOSE_TIMEOUT_SECONDS)
            except TimeoutError:
                # 后续 session close 会继续关闭 Context，必要时重建浏览器；单个过期
                # Tab 不能无限阻塞 HTTP 取消路径。
                logger.warning(
                    "CAMOFOX_TAB_CLOSE stage=timed_out profile=%s tab_id=%s timeout_seconds=%s elapsed_ms=%s",
                    log_id,
                    tab.tab_id,
                    TAB_CLOSE_TIMEOUT_SECONDS,
                    int((time.monotonic() - started_at) * 1000),
                )
            except Exception:
                logger.warning(
                    "CAMOFOX_TAB_CLOSE stage=failed profile=%s tab_id=%s elapsed_ms=%s",
                    log_id,
                    tab.tab_id,
                    int((time.monotonic() - started_at) * 1000),
                    exc_info=True,
                )
            else:
                logger.info(
                    "CAMOFOX_TAB_CLOSE stage=completed profile=%s tab_id=%s elapsed_ms=%s",
                    log_id,
                    tab.tab_id,
                    int((time.monotonic() - started_at) * 1000),
                )

    async def create_execution(
        self, execution_id: str, provider: ProviderName, profile_key: str, query: str
    ) -> Execution:
        """创建并填写任务页；未调用 submit 前不会向第三方平台派发问题。"""
        if not execution_id or len(execution_id) > 256 or not profile_key or len(profile_key) > 256:
            raise ProtocolError(400, "executionId and profileKey are required")
        try:
            rule = rule_for(provider)
        except ProviderAutomationError as exc:
            raise ProtocolError(400, "Unsupported RPA provider", exc.code) from exc
        # executionId 由后端任务尝试生成。这里先建立内存登记再进行任何页面动作，
        # 使客户端在创建请求超时后可安全重试，并避免并发请求各自打开一张任务页。
        async with self._execution_lock:
            existing = self.executions.get(execution_id)
            if existing is not None:
                if existing.provider != provider or existing.profile_key != profile_key or existing.query != query:
                    raise ProtocolError(409, "executionId is already bound to another request")
                return existing
            tab = await self.create_tab(profile_key, rule.entry_url)
            execution = Execution(execution_id, provider, profile_key, query, tab)
            self.executions[execution_id] = execution
        try:
            # prepare 仅写入输入框，不触发提交。后端须在下一阶段将 promptSubmitted
            # 落库；这把浏览器副作用与 SaaS 持久化事务明确切开。
            async with tab.lock:
                await prepare_provider(tab.page, rule, query)
            return execution
        except ProviderAutomationError as exc:
            execution.state, execution.error_code, execution.error_message = "failed", exc.code, str(exc)
            with suppress(Exception):
                execution.failure_screenshot = await tab.page.screenshot(type="png")
            await self.close_tab(tab)
            raise ProtocolError(409, "Provider execution could not be prepared", exc.code) from exc
        except Exception as exc:
            # 仅记录平台标识和堆栈，绝不把 query、profileKey、Cookie 或页面正文写入日志。
            # 未分类异常仍对后端收敛成稳定错误码，运维则可据此修复 Camofox 内的平台规则。
            logger.exception("Provider execution preparation failed: provider=%s", provider.value)
            execution.state, execution.error_code, execution.error_message = "failed", "page_unavailable", "Provider page is unavailable"
            with suppress(Exception):
                execution.failure_screenshot = await tab.page.screenshot(type="png")
            await self.close_tab(tab)
            raise ProtocolError(503, "Provider execution could not be prepared") from exc

    async def submit_execution(self, execution_id: str) -> Execution:
        """幂等开始执行。重复请求只返回同一执行状态，绝不再次点击提交。"""
        execution = self.executions.get(execution_id)
        if execution is None:
            raise ProtocolError(404, "Execution not found", "execution_not_found")
        if execution.state == "prepared":
            # 仅 prepared 可创建后台协程；submitting/generating 的重复 submit 返回同一
            # 对象，completed/failed/cancelled 也不会被重新激活。
            execution.state = "submitting"
            execution.task = asyncio.create_task(self._run_execution(execution), name=f"rpa-{execution_id}")
        return execution

    async def _run_execution(self, execution: Execution) -> None:
        rule = rule_for(execution.provider)
        listener: NetworkAnswerListener | None = None
        # 一个 execution 只有一个总截止时间；网络监听、提交确认和 DOM 兜底共享该预算。
        execution_deadline = time.monotonic() + PLATFORM_EXECUTION_TIMEOUT_SECONDS
        try:
            async with execution.tab.lock:
                # 网络监听器在提交前绑定到 execution 独占页面。它只能命中
                # ProviderRule 中的静态聊天接口，不读取 DOM 回答，避免网站导航、
                # 工作台、思考步骤或历史会话文字污染 Markdown。
                listener = NetworkAnswerListener(execution.tab.page, rule)
                await listener.arm()
                # DOM 只作为网络流无终态时的兜底基线。它不参与正常网络结果的
                # Markdown 或引用合并，避免将工作台、导航和搜索状态写入结果。
                baseline_count = await answer_dom_baseline(execution.tab.page, rule)
                pacing = rule.interaction_pacing
                retry_window = pacing.submission_retry_window_seconds
                retry_deadline = min(
                    execution_deadline,
                    time.monotonic() + retry_window,
                )
                while True:
                    # 只有尚未观察到受限聊天请求时才会再次点击，绝不重发已确认的问题。
                    listener.reset_submission_observed()
                    await submit_provider(execution.tab.page, rule)
                    if retry_window <= 0:
                        break
                    remaining_retry = retry_deadline - time.monotonic()
                    if remaining_retry <= 0:
                        raise ProviderAutomationError(
                            "submission_not_dispatched",
                            "Provider chat request was not observed after submission",
                        )
                    try:
                        await listener.wait_submission_observed(
                            min(pacing.submission_retry_interval_seconds, remaining_retry)
                        )
                        break
                    except ProviderAutomationError as submission_error:
                        if submission_error.code != "submission_not_dispatched":
                            raise
                        if time.monotonic() >= retry_deadline:
                            raise
                execution.state = "generating"
                try:
                    remaining_execution = execution_deadline - time.monotonic()
                    network_answer = await listener.wait_result(
                        min(
                            NETWORK_FIRST_RESPONSE_TIMEOUT_SECONDS,
                            remaining_execution,
                        )
                    )
                    answer_markdown = network_answer.markdown
                    citations = network_answer.citations
                except ProviderAutomationError as network_error:
                    if network_error.code not in {"answer_timeout", "answer_incomplete"}:
                        raise
                    # 平台 SSE 长连接未关闭或协议短暂改版时，才使用同一 execution
                    # 新增的回答卡片。绝不因兜底重新提交问题或读取整页文本。
                    remaining_execution = execution_deadline - time.monotonic()
                    answer_markdown, citations = await wait_result(
                        execution.tab.page,
                        rule,
                        timeout_seconds=remaining_execution,
                        baseline_count=baseline_count,
                        submitted_query=execution.query,
                    )
                result: AutomationResult = {"citations": citations}
                confirmed = conversation_url(rule, execution.tab.page.url)
                if confirmed is not None:
                    result["conversationUrl"] = confirmed
                execution.result = {"answerMarkdown": answer_markdown, "result": result}
                execution.state = "completed"
        except asyncio.CancelledError:
            execution.state = "cancelled"
            raise
        except ProviderAutomationError as exc:
            execution.state, execution.error_code, execution.error_message = "failed", exc.code, str(exc)
            with suppress(Exception):
                execution.failure_screenshot = await execution.tab.page.screenshot(type="png")
        except Exception:
            execution.state, execution.error_code, execution.error_message = "failed", "page_unavailable", "Provider execution failed"
            with suppress(Exception):
                execution.failure_screenshot = await execution.tab.page.screenshot(type="png")
        finally:
            if listener is not None:
                await listener.close()
            # 无论成功、取消还是失败都立即 checkpoint 并关闭任务 Tab。StorageState 留在
            # sidecar 的受限卷内，执行结果和失败证据则由后端拉取后按其保留期持久化。
            # sidecar 重启会丢失 execution registry；后端看到 execution_not_found 时必须
            # 把已提交任务收敛为不确定状态，绝不可重新提交。
            await self.close_tab(execution.tab)

    async def capture_execution_preview(self, execution_id: str) -> bytes:
        """返回执行中的完整 PNG，不允许按账号、Tab 或页面规则读取。

        预览严格绑定后端生成的 ``executionId``，仅在任务准备、提交或生成期间可用。
        任务终态后会关闭 Tab，因此此接口不是历史页面重放通道，也不会把平台页面内容
        写入 sidecar 的持久化卷。
        """
        execution = self.executions.get(execution_id)
        if execution is None:
            raise ProtocolError(404, "Execution not found", "execution_not_found")
        if execution.state not in {"prepared", "submitting", "generating"}:
            raise ProtocolError(
                409, "Execution preview is unavailable", "execution_preview_unavailable"
            )
        try:
            # 执行协程在等待网络流结束时保持页面静止；这里仅触发 Playwright 的只读
            # 截图，不修改 DOM、网络 API 或用户会话。不能等待 tab 锁，否则长流会使
            # 前端预览始终阻塞到任务结束。
            return await execution.tab.page.screenshot(type="png", full_page=True)
        except Exception as exc:
            raise ProtocolError(
                409,
                "Execution page is not ready for preview",
                "execution_preview_pending",
            ) from exc

    def execution_payload(self, execution: Execution) -> ExecutionPayload:
        payload: ExecutionPayload = {"executionId": execution.execution_id, "state": execution.state}
        if execution.result is not None:
            payload["result"] = execution.result
        if execution.error_code is not None:
            payload["error"] = {"code": execution.error_code, "message": execution.error_message or "Provider execution failed"}
        if execution.failure_screenshot is not None:
            payload["failureScreenshotBase64"] = base64.b64encode(execution.failure_screenshot).decode("ascii")
        return payload

    async def cancel_execution(self, execution_id: str) -> Execution:
        execution = self.executions.get(execution_id)
        if execution is None:
            raise ProtocolError(404, "Execution not found", "execution_not_found")
        if execution.task is not None and not execution.task.done():
            execution.task.cancel()
            await asyncio.gather(execution.task, return_exceptions=True)
        elif execution.state == "prepared":
            execution.state = "cancelled"
            await self.close_tab(execution.tab)
        return execution

    async def account_keepalive(
        self, provider: ProviderName, profile_key: str
    ) -> Literal["ok", "login_required", "verification_required", "uncertain"]:
        """只用已 hydrate 的 Context 打平台用户接口，不打开聊天页。

        平台 URL 与业务码判定留在 sidecar。调用方仍然只传 provider 与 profileKey。
        豆包等首页加载很慢，页面保活还会把游客输入框误判为在线，因此不再回退开页。
        没有 Context 或接口无法判定时返回 uncertain，由调度侧决定是否重试。
        """
        try:
            rule_for(provider)
        except ProviderAutomationError as exc:
            raise ProtocolError(400, "Unsupported RPA provider", exc.code) from exc
        session = self.sessions.get(profile_key)
        if session is None:
            return "uncertain"
        session.last_access = time.monotonic()
        return await probe_browser_context(provider.value, session.context)

    async def create_manual_session(
        self, provider: ProviderName, profile_key: str
    ) -> ManagedWindow:
        """按平台规则排队创建认证窗口，并返回不透明人工会话句柄。

        信号量只覆盖窗口初始化阶段。发布完成的窗口不会继续占用创建槽位，因此
        在线人工 VNC 数量仍由 ``MAX_MANUAL_WINDOWS`` 独立控制。
        """
        try:
            rule = rule_for(provider)
        except ProviderAutomationError as exc:
            raise ProtocolError(400, "Unsupported RPA provider", exc.code) from exc
        async with self._manual_window_create_semaphore:
            tab = await self.create_tab(profile_key, rule.entry_url)
            try:
                window = await self.promote_tab_to_window(
                    profile_key, tab.tab_id, kind="manual"
                )
                return await self.publish_window(window.handle, profile_key)
            except Exception:
                await self.close_tab(tab)
                raise

    async def checkpoint_manual_session(self, handle: str, profile_key: str) -> bool:
        window = self.windows.get(handle)
        if window is None or window.user_id != profile_key or window.kind != "manual":
            raise ProtocolError(404, "Manual session not found")
        return await self._export_storage_state(window.tab.session) is not None

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
                # Kimi 的会话健康 RPC 只需要受保护接口的 HTTP 状态。maxBytes=0
                # 是明确的“状态专用”语义：不读取、缓存或回传该响应正文，避免把
                # 订阅信息等页面数据越过最小化采集边界。
                if capture.spec["maxBytes"] == 0:
                    if capture.generation == generation:
                        capture.body = b""
                        capture.state = "complete"
                    continue
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


async def parse_v4_request(
    request: Request, model: type[V4RequestModel]
) -> V4RequestModel:
    """在协议边界解析 v4 JSON，并由 Pydantic 拒绝未声明的浏览器控制字段。"""
    raw = await request_object(request)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # 校验详情可能回显请求字段或值；对外只返回稳定的受控错误文本。
        raise ProtocolError(400, "Request parameters are invalid") from exc


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
    # 0 是状态专用 capture：允许确认登录态，但禁止读取响应正文。正值才是
    # 可回传正文的字节上限；负数、布尔值和过大的值保持拒绝。
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 0 <= maximum <= MAX_CAPTURE_BYTES:
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
    try:
        await selected.click(timeout=SUBMISSION_CLICK_TIMEOUT_MS, no_wait_after=True)
    except Exception as exc:
        # 豆包会不定期显示全屏平台对话框。此时发送按钮虽仍满足可见/可用条件，
        # 但浏览器明确证明点击被弹窗树拦截，提交未派发。该状态需要人工确认，
        # 不能将页面内部元素、标题或堆栈传出，也不能由自动恢复再次点击。
        if "intercepts pointer events" in str(exc):
            raise ProtocolError(
                409,
                "A platform dialog is blocking submission and needs manual handling",
                "manual_intervention_required",
            ) from exc
        raise


@app.get("/health")
async def health() -> dict[str, Any]:
    running = service.browser is not None and service.browser.is_connected()
    ready = service.browser_ready and running
    return {
        "ok": True,
        "engine": "camoufox",
        "geoRpaProtocolVersion": service.protocol_version,
        "browserConnected": running,
        "browserRunning": running,
        "browserReady": ready,
        "persistenceReady": service.persistence_ready,
        "activeTabs": service.tab_count,
        "activeSessions": len(service.sessions),
        "activeWindows": len(service.windows),
        "consecutiveFailures": 0,
        "vncEnabled": service.vnc_enabled,
        "windowPublisherEnabled": service.window_publisher_enabled,
        "capabilities": ["task_executions", "account_keepalive", "manual_windows"],
    }


@app.post(
    "/rpa/executions",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": EXECUTION_CREATE_OPENAPI_SCHEMA}
            },
        }
    },
)
async def create_execution(request: Request) -> ExecutionPayload:
    """创建平台任务并填写问题；不接受 URL、selector 或网络捕获声明。"""
    try:
        body = await parse_v4_request(request, ExecutionCreateRequest)
        # 请求体刻意没有 url、selector、headers、cookie 或脚本字段。平台变化只能通过
        # Camofox 镜像中的受审查 Python 规则修复，不能在生产后端临时拼装浏览器操作。
        execution = await service.create_execution(
            body.execution_id,
            body.provider,
            body.profile_key,
            body.query,
        )
        return service.execution_payload(execution)
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/executions/{execution_id}/submit", status_code=202)
async def submit_execution(execution_id: str) -> ExecutionPayload:
    """幂等地开始已经准备好的任务，不允许调用方指定提交元素。"""
    try:
        return service.execution_payload(await service.submit_execution(execution_id))
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/rpa/executions/{execution_id}")
async def execution_status(execution_id: str) -> ExecutionPayload:
    execution = service.executions.get(execution_id)
    if execution is None:
        raise error_response(ProtocolError(404, "Execution not found", "execution_not_found"))
    return service.execution_payload(execution)


@app.get("/rpa/executions/{execution_id}/preview")
async def execution_preview(execution_id: str) -> Response:
    """读取执行中页面的单张 PNG，供已鉴权的后端任务观察接口转发。"""
    try:
        image = await service.capture_execution_preview(execution_id)
    except ProtocolError as exc:
        raise error_response(exc) from exc
    return Response(
        image,
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.delete("/rpa/executions/{execution_id}")
async def cancel_execution(execution_id: str) -> ExecutionPayload:
    try:
        return service.execution_payload(await service.cancel_execution(execution_id))
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/accounts/session")
async def hydrate_account_session(request: Request) -> BooleanPayload:
    """Create or reuse an account Context from the GEO-owned login snapshot."""
    try:
        body = await parse_v4_request(request, AccountSessionHydrateRequest)
        await service.hydrate_account_session(body.profile_key, body.storage_state)
        return {"ok": True}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/accounts/session/checkpoint")
async def checkpoint_account_session(request: Request) -> AccountSessionCheckpointPayload:
    """Export Cookie + LocalStorage for GEO to persist; never log the snapshot."""
    try:
        body = await parse_v4_request(request, ProfileKeyRequest)
        state = await service.checkpoint_account_session(body.profile_key)
        return {"ok": True, "storageState": state}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/accounts/keepalive")
async def account_keepalive(request: Request) -> KeepalivePayload:
    """执行平台无关的账号保活，页面规则完全由 sidecar 决定。"""
    try:
        body = await parse_v4_request(request, AccountRequest)
        return {
            "status": await service.account_keepalive(
                body.provider,
                body.profile_key,
            )
        }
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.post("/rpa/manual-sessions")
async def create_manual_session(request: Request) -> ManualSessionPayload:
    """按 provider 创建人工认证窗口；调用方不能指定认证地址或 tab。"""
    try:
        body = await parse_v4_request(request, AccountRequest)
        window = await service.create_manual_session(
            body.provider,
            body.profile_key,
        )
        return {"sessionHandle": window.handle, "state": window.state}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.get("/rpa/manual-sessions/{handle}")
async def manual_session_status(handle: str, profileKey: str) -> dict[str, str]:
    window = service.windows.get(handle)
    if window is None or window.user_id != profileKey or window.kind != "manual" or window.tab.page.is_closed():
        raise error_response(ProtocolError(404, "Manual session not found"))
    return {"state": window.state}


@app.post("/rpa/manual-sessions/{handle}/checkpoint")
async def checkpoint_manual_session(handle: str, request: Request) -> BooleanPayload:
    try:
        body = await parse_v4_request(request, ProfileKeyRequest)
        persisted = await service.checkpoint_manual_session(
            handle, body.profile_key
        )
        if not persisted:
            raise ProtocolError(503, "Camofox Browser could not persist the authenticated session")
        return {"ok": True}
    except ProtocolError as exc:
        raise error_response(exc) from exc


@app.delete("/rpa/manual-sessions/{handle}")
async def delete_manual_session(handle: str, request: Request) -> BooleanPayload:
    try:
        body = await parse_v4_request(request, ProfileKeyRequest)
        return {"ok": await service.close_managed_window(handle, body.profile_key)}
    except ProtocolError as exc:
        raise error_response(exc) from exc


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


@app.post("/rpa/manual-windows/{handle}/vnc")
async def open_manual_window_vnc(handle: str, request: Request) -> dict[str, int | str]:
    """按需发布一个人工窗口；端口只交给 GEO 后端的内网代理。"""
    body = await request_object(request)
    try:
        user_id = require_string(body.get("userId"), "userId", 256)
        window = service.windows.get(handle)
        if window is None or window.user_id != user_id or window.kind != "manual":
            raise ProtocolError(404, "Manual window not found")
        window = await service.publish_window(handle, user_id)
        if window.rfb_port is None:
            raise ProtocolError(409, "Manual VNC is unavailable")
        return {"state": window.state, "rfbPort": window.rfb_port}
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


@app.delete("/rpa/manual-windows")
async def delete_orphaned_manual_windows() -> dict[str, int]:
    """关闭所有人工窗口；该内部端点只供单一 GEO API 实例恢复重启遗留资源。"""
    return {"closed": await service.close_all_manual_windows()}


@app.post("/rpa/tabs/{tab_id}/checkpoint")
async def checkpoint(tab_id: str, request: Request) -> dict[str, bool]:
    """在人工认证完成时先持久化当前账号 Context，随后才允许调用方关闭窗口。"""
    body = await request_object(request)
    try:
        async with tab_operation(require_string(body.get("userId"), "userId", 256), tab_id) as tab:
            # 旧 tab checkpoint 仅验证当前 Context 可导出快照；GEO 必须走账号级 checkpoint 才会写库。
            # 导出失败时明确返回 503，不能仅因页面仍显示已登录而报成功。
            if await service._export_storage_state(tab.session) is None:
                raise ProtocolError(503, "Camofox Browser could not persist the authenticated session")
        return {"ok": True}
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


@app.get("/tabs/{tab_id}/current-url")
@app.get("/rpa/tabs/{tab_id}/current-url")
async def current_url(tab_id: str, userId: str) -> dict[str, str]:
    """返回账号归属 Tab 的自然地址，不接受导航、脚本或任意页面读取参数。"""
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
            target = body.get("target")
            locator = target_locator(tab, target)
            count = await locator.count()
            soft = {"visible", "enabled", "editable", "has_value_property", "content_editable"}
            if operation == "count":
                return {"ok": True, "result": count}
            if count == 0 and operation in soft:
                return {"ok": True, "result": False}
            if (
                count == 0
                and operation == "inner_text"
                and isinstance(target, dict)
                and target.get("selector") == "body"
            ):
                # task window 刚创建时 Firefox 可能尚未建立 document.body。它不是
                # locator 协议错误，也不表示账号 tab 已关闭；返回空文本让调用方继续
                # 按自身的页面就绪条件轮询。其他 selector 仍保留严格的缺失语义。
                return {"ok": True, "result": ""}
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
@app.post("/rpa/tabs/{tab_id}/locator-clear")
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
                elif action == "locator-clear":
                    atomic = body.get("atomic", False)
                    if not isinstance(atomic, bool):
                        raise ProtocolError(400, "locator clear atomic is invalid")
                    # Playwright 的 Locator.fill 空值同时支持 input、textarea 和
                    # contenteditable，并由浏览器派发站点框架可识别的编辑事件。清空
                    # 必须保持在声明的 Locator 内，不能退化为页面全局快捷键。
                    await locator.fill("", timeout=LOCATOR_TIMEOUT_MS)
                elif action == "locator-input":
                    text = require_string(body.get("text"), "locator input text", 4_096)
                    atomic = body.get("atomic", False)
                    native_contenteditable_events = body.get("nativeContenteditableEvents", False)
                    if not isinstance(atomic, bool):
                        raise ProtocolError(400, "locator input atomic is invalid")
                    if not isinstance(native_contenteditable_events, bool):
                        raise ProtocolError(400, "locator input nativeContenteditableEvents is invalid")
                    if native_contenteditable_events:
                        # 该路径只服务于已经由工作流声明的 contenteditable。使用
                        # Playwright 的 Locator 键盘事件，不注入 JavaScript，也不伪造
                        # isTrusted；这样 React 能接到与用户编辑一致的输入事件链。
                        if not await locator.evaluate("node => node.isContentEditable"):
                            raise ProtocolError(400, "native contenteditable input requires a contenteditable locator")
                        await locator.focus(timeout=LOCATOR_TIMEOUT_MS)
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
