"""X11 单窗口发布器。

此模块只接收服务内部生成的 X11 window id，并启动一个绑定该窗口的
``x11vnc`` 与 WebSocket-to-RFB bridge。HTTP 路由只能持有不透明 handle，
不得向调用方返回 window id、RFB 端口或子进程信息。
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
from contextlib import suppress
from dataclasses import dataclass

WINDOW_DISCOVERY_ATTEMPTS = 20
WINDOW_DISCOVERY_DELAY_SECONDS = 0.15
PUBLISHER_READINESS_ATTEMPTS = 20
PUBLISHER_READINESS_DELAY_SECONDS = 0.15
_DISPLAY_PATTERN = re.compile(r"^:[0-9]+$")
_WINDOW_ID_PATTERN = re.compile(r"^0x[0-9a-f]+$", re.IGNORECASE)


def valid_display(display: str) -> bool:
    """只接受容器内 Xvfb 使用的 ``:N`` display 标识。"""
    return bool(_DISPLAY_PATTERN.fullmatch(display))


def valid_window_id(window_id: str) -> bool:
    """只接受 ``xwininfo`` 返回的十六进制窗口标识，禁止命令注入。"""
    return bool(_WINDOW_ID_PATTERN.fullmatch(window_id))


async def read_x11_window_tree(display: str) -> str:
    """无 shell 地读取 X11 根窗口树，用于识别刚创建的原生 popup。"""
    if not valid_display(display):
        raise RuntimeError("X11 display is unavailable")
    process = await asyncio.create_subprocess_exec(
        "xwininfo",
        "-display",
        display,
        "-root",
        "-tree",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("X11 window inspection timed out") from exc
    if process.returncode != 0:
        raise RuntimeError("X11 window inspection failed")
    return stdout.decode("utf-8", "replace")


def top_level_window_ids(window_tree: str) -> list[str]:
    """返回根窗口的直接子节点，避免把 popup 的子控件误认成目标窗口。"""
    candidates: list[tuple[int, str]] = []
    for line in window_tree.splitlines():
        match = re.match(r"^(\s+)(0x[0-9a-f]+)\s", line, re.IGNORECASE)
        if match:
            candidates.append((len(match.group(1)), match.group(2)))
    if not candidates:
        return []
    root_indent = min(indent for indent, _ in candidates)
    return [window_id for indent, window_id in candidates if indent == root_indent]


async def wait_for_new_x11_window_id(display: str, existing_window_ids: set[str]) -> str:
    """等待 Firefox 为固定 popup 请求创建一个新的顶层 X11 窗口。"""
    last_error: Exception | None = None
    for attempt in range(WINDOW_DISCOVERY_ATTEMPTS):
        try:
            for window_id in top_level_window_ids(await read_x11_window_tree(display)):
                if window_id not in existing_window_ids:
                    return window_id
        except Exception as exc:  # Xvfb 退出时统一映射为受控失败。
            last_error = exc
        if attempt + 1 < WINDOW_DISCOVERY_ATTEMPTS:
            await asyncio.sleep(WINDOW_DISCOVERY_DELAY_SECONDS)
    if last_error is not None:
        raise RuntimeError("X11 window discovery failed") from last_error
    raise RuntimeError("Firefox popup did not create an X11 window")


def assert_tcp_port_available(port: int) -> None:
    """拒绝复用陈旧发布器端口，防止新租约连到上一窗口。"""
    if not 1024 <= port <= 65535:
        raise RuntimeError("Window publisher port is invalid")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError("Window publisher port is already in use") from exc


async def wait_for_tcp_port(port: int) -> None:
    """等待 bridge 开始监听，避免把尚未启动的地址交给后端代理。"""
    for attempt in range(PUBLISHER_READINESS_ATTEMPTS):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.25
            )
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, TimeoutError):
            if attempt + 1 < PUBLISHER_READINESS_ATTEMPTS:
                await asyncio.sleep(PUBLISHER_READINESS_DELAY_SECONDS)
    raise RuntimeError("Window publisher did not listen")


async def wait_for_rfb_greeting(port: int) -> None:
    """确认 x11vnc 已绑定目标窗口且能够发出 RFB 握手。"""
    for attempt in range(PUBLISHER_READINESS_ATTEMPTS):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.5
            )
            try:
                greeting = await asyncio.wait_for(reader.readexactly(4), timeout=0.5)
            finally:
                writer.close()
                await writer.wait_closed()
            if greeting == b"RFB ":
                return
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        if attempt + 1 < PUBLISHER_READINESS_ATTEMPTS:
            await asyncio.sleep(PUBLISHER_READINESS_DELAY_SECONDS)
    raise RuntimeError("Window publisher did not emit an RFB greeting")


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    """终止发布子进程；x11vnc 忽略 SIGTERM 时继续强制回收。"""
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
        return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=1)


@dataclass
class WindowPublisher:
    """一个 X11 窗口对应的一对私有 RFB/WebSocket 进程。"""

    display: str
    window_id: str
    rfb_port: int
    websocket_port: int
    _x11vnc: asyncio.subprocess.Process | None = None
    _bridge: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return bool(
            self._x11vnc
            and self._bridge
            and self._x11vnc.returncode is None
            and self._bridge.returncode is None
        )

    async def start(self) -> None:
        """启动发布器并仅在 RFB 与 WebSocket 都就绪后返回。"""
        if not valid_display(self.display) or not valid_window_id(self.window_id):
            raise RuntimeError("Window publisher target is invalid")
        assert_tcp_port_available(self.rfb_port)
        assert_tcp_port_available(self.websocket_port)
        self._x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc",
            "-display",
            self.display,
            "-id",
            self.window_id,
            "-localhost",
            "-nopw",
            "-forever",
            "-shared",
            "-rfbport",
            str(self.rfb_port),
            "-noxdamage",
            "-wait",
            "10",
            "-defer",
            "10",
            "-wait_ui",
            "1",
            "-setdefer",
            "-1",
            "-quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await wait_for_tcp_port(self.rfb_port)
            await wait_for_rfb_greeting(self.rfb_port)
            environment = os.environ | {"RFB_TARGET_PORT": str(self.rfb_port)}
            self._bridge = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "uvicorn",
                "app.novnc:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(self.websocket_port),
                env=environment,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await wait_for_tcp_port(self.websocket_port)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """幂等撤销发布器，不关闭仍可继续执行的浏览器窗口。"""
        await stop_process(self._bridge)
        await stop_process(self._x11vnc)
        self._bridge = None
        self._x11vnc = None
