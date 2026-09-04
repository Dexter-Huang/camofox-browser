"""在镜像内验证预置 Camoufox 可实际启动，不启动 HTTP 监听器。"""

from __future__ import annotations

import asyncio

from app.main import BrowserService


async def main() -> None:
    service = BrowserService()
    await service.start()
    try:
        if service.browser is None or not service.browser_ready:
            raise RuntimeError("Camoufox prewarm did not complete")
        print("camoufox-prewarm-ok")
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
