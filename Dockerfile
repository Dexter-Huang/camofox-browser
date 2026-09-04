# 构建上下文是当前子模块根目录。浏览器归档由受控发布流程放入 bin/，运行时绝不下载浏览器。
FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PYTHONPATH=/app \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/

# Firefox/Camoufox 的动态库和字体。服务不包含 Node 业务运行时；Playwright Python 自带的
# driver 仅作为控制通道使用，浏览器可执行文件来自下方固定的本地发行包。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates unzip \
        libasound2 libdbus-glib-1-2 libegl1 libgbm1 libgl1-mesa-dri \
        libgtk-3-0 libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
        libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
        fonts-liberation fonts-noto-color-emoji fontconfig \
        xvfb x11vnc novnc python3-websockify x11-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN uv venv --python /usr/local/bin/python /app/.venv \
    && uv pip install --python /app/.venv/bin/python -r requirements.txt

# 固定的官方 Camoufox 归档由操作者预先放入 bin/。COPY 层位于应用源码
# 之前，业务代码或测试变动会复用该解压后的浏览器层，不会重复下载。
# 不匹配的归档立即失败，绝不退回到启动或构建时在线下载。
COPY bin/camoufox-152.0.4-beta.29-lin.x86_64.zip /tmp/camoufox.zip
ENV XDG_CACHE_HOME=/home/node/.cache
RUN echo "1bea4b55a51c88e82dc7d426d9c75093d942d2afc8c911cb8fc78ebf723d686c  /tmp/camoufox.zip" | sha256sum -c - \
    && mkdir -p /home/node/.cache/camoufox/browsers/official/152.0.4-beta.29 \
    && unzip -q /tmp/camoufox.zip -d /home/node/.cache/camoufox/browsers/official/152.0.4-beta.29 \
    && test -f /home/node/.cache/camoufox/browsers/official/152.0.4-beta.29/camoufox-bin \
    && printf '{"version":"152.0.4","build":"beta.29","prerelease":true}\n' > /home/node/.cache/camoufox/browsers/official/152.0.4-beta.29/version.json \
    && printf '{"active_version":"browsers/official/152.0.4-beta.29"}\n' > /home/node/.cache/camoufox/config.json \
    && touch /home/node/.cache/camoufox/.0.5_FLAG \
    && chmod -R 755 /home/node/.cache/camoufox \
    && rm /tmp/camoufox.zip

# Debian 的 novnc 包仅用于提供静态网页，却会带入 Node.js 和一组运行期不需要
# 的 Python 依赖。复制静态资源后立刻卸载这些包；6080 的 WebSocket-to-RFB
# 桥接由 app.novnc 中的 Python ASGI 进程承担，最终镜像不保留 Node 运行时。
RUN mkdir -p /app/novnc \
    && cp -a /usr/share/novnc/. /app/novnc/ \
    && apt-get purge -y --auto-remove novnc python3-websockify nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && ! command -v node \
    && ! command -v nodejs \
    && ! command -v websockify

RUN useradd --create-home --uid 1000 node \
    && mkdir -p /home/node/.camofox/profiles \
    && chown -R node:node /home/node /app
USER node

COPY --chown=node:node app ./app
COPY --chown=node:node docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

ENV CAMOFOX_PROFILE_DIR=/home/node/.camofox/profiles \
    CAMOFOX_PORT=9377
EXPOSE 9377
EXPOSE 5900 6080
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9377"]
