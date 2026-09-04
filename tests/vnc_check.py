"""校验 VNC 容器的 sidecar、noVNC 页面和 WebSocket 桥接都可用。"""

import asyncio
import json
import urllib.request

from websockets.asyncio.client import connect


def get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        return json.load(response)


async def main() -> None:
    health = get_json("http://127.0.0.1:9377/health")
    assert health["browserReady"] is True
    assert health["vncEnabled"] is True

    status = get_json("http://127.0.0.1:9377/vnc/status")
    assert status["enabled"] is True
    assert status["running"] is True
    assert status["vncPort"] == 5900
    assert status["novncPort"] == 6080
    assert status["path"] == "/vnc.html"

    with urllib.request.urlopen("http://127.0.0.1:6080/vnc.html", timeout=5) as response:  # noqa: S310
        assert response.status == 200
        assert "text/html" in response.headers["content-type"]

    async with connect("ws://127.0.0.1:6080/websockify", open_timeout=5) as websocket:
        banner = await asyncio.wait_for(websocket.recv(), timeout=5)
        assert isinstance(banner, bytes)
        assert banner.startswith(b"RFB ")

    print("VNC validation passed")


if __name__ == "__main__":
    asyncio.run(main())
