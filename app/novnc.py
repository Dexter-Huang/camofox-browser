"""专用人工浏览器的 noVNC 静态站点及受限 RFB 字节桥接。

这个进程不加载 Camoufox、不会创建 BrowserContext，也不监听外部网络。它只在
``ENABLE_VNC=1`` 时由 entrypoint 启动，并且仅把本机 noVNC WebSocket 转发到
同一容器中 x11vnc 的回环 RFB 端口。常规自动化 sidecar 不启用此进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

NOVNC_DIR = Path("/app/novnc")
MAX_RFB_FRAME_BYTES = 2 * 1024 * 1024

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def vnc_port() -> int:
    """读取受限 VNC 端口；无效部署值退回容器私有默认端口。"""
    try:
        value = int(os.getenv("RFB_TARGET_PORT", os.getenv("VNC_PORT", "5900")))
    except ValueError:
        return 5900
    return value if 1 <= value <= 65_535 else 5900


@app.websocket("/")
@app.websocket("/websockify")
async def websockify(websocket: WebSocket) -> None:
    """在 noVNC 浏览器客户端和容器回环 x11vnc 之间转发二进制 RFB 字节。"""
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", vnc_port())
    except OSError:
        await websocket.close(code=1011)
        return

    await websocket.accept()

    async def rfb_to_client() -> None:
        while data := await reader.read(64 * 1024):
            await websocket.send_bytes(data)

    async def client_to_rfb() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                # noVNC 使用二进制帧；文本帧不进入 RFB，避免意外命令通道。
                continue
            if len(data) > MAX_RFB_FRAME_BYTES:
                await websocket.close(code=1009)
                return
            writer.write(data)
            await writer.drain()

    rfb_task = asyncio.create_task(rfb_to_client())
    client_task = asyncio.create_task(client_to_rfb())
    try:
        await asyncio.wait({rfb_task, client_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in (rfb_task, client_task):
            task.cancel()
        await asyncio.gather(rfb_task, client_task, return_exceptions=True)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


@app.get("/")
async def index() -> FileResponse:
    """保持访问 ``:6080/`` 时跳转前的 noVNC 默认入口兼容性。"""
    return FileResponse(NOVNC_DIR / "vnc.html")


app.mount("/", StaticFiles(directory=NOVNC_DIR, html=True), name="novnc")
