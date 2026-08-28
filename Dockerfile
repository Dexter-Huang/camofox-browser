# 源码固定从 GEO 维护的 fork 拉取；不要在 Dockerfile 中引用未审查的第三方浮动分支。
# 本地开发/验收默认使用已构建的本机基础镜像，不在 Docker build 中隐式拉取远程镜像。
# 需要从零构建时，显式传入 CAMOUFOX_RUNTIME_IMAGE 和 CAMOFOX_BROWSER_BASE_IMAGE。
ARG CAMOUFOX_RUNTIME_IMAGE=geo-camofox-browser:jo-local
ARG CAMOFOX_BROWSER_BASE_IMAGE=geo-camofox-browser-base:jo-local

# 仅复用已验证的 Firefox/Camoufox 二进制层；服务代码来自当前构建上下文中的
# Dexter-Huang/camofox-browser fork。二进制来源不改变服务 API 的源码归属。
FROM ${CAMOUFOX_RUNTIME_IMAGE} AS camoufox-runtime

FROM camoufox-runtime AS source
WORKDIR /src
COPY . ./

# 预镜像复用已验证的浏览器运行时，并固化 fork 的锁文件依赖与插件依赖。
# 这样服务源码改动不会触发浏览器、系统包或 npm 依赖的重复安装。
FROM camoufox-runtime AS camofox-browser-base
USER root
WORKDIR /app
COPY --from=source /src/package.json /src/package-lock.json ./
COPY --from=source /src/scripts/ ./scripts/
COPY --from=source /src/plugins/ ./plugins/
COPY --from=source /src/bin/yt-dlp ./bin/yt-dlp
# 插件依赖脚本只依据 /app/camofox.config.json 决定安装集合。必须在执行脚本前
# 复制当前 fork 的配置，否则 vnc 等新启用插件会因读取到基础镜像旧配置而漏装依赖。
COPY --from=source /src/camofox.config.json ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3 curl \
    && CAMOFOX_SKIP_DOWNLOAD=1 npm ci --omit=dev \
    && sh scripts/install-plugin-deps.sh \
    && chmod 755 /usr/local/bin/yt-dlp \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*
USER node

# 发布基础镜像时构建到 ``camofox-browser-base`` target；应用镜像可改用已发布的同版本基础镜像。
FROM ${CAMOFOX_BROWSER_BASE_IMAGE} AS camofox-browser

# GHCR 使用这些 OCI 标签追溯最终应用镜像到 GEO fork 的固定 revision。
ARG CAMOFOX_BROWSER_REPOSITORY=https://github.com/Dexter-Huang/camofox-browser.git
ARG CAMOFOX_BROWSER_REV=e5a36f5cd0332fde6597de474329a308a53a0716
LABEL org.opencontainers.image.source="${CAMOFOX_BROWSER_REPOSITORY}" \
      org.opencontainers.image.revision="${CAMOFOX_BROWSER_REV}" \
      org.opencontainers.image.title="geo-camofox-browser"

WORKDIR /app

# 最终服务以 node 用户启动，而 camoufox-js 只会从该用户的缓存目录读取浏览器。
# 预构建运行时镜像中的 /root 缓存不会自动继承到这里；显式使用构建上下文内
# 已校验的发行包，避免首次创建会话时再从网络下载浏览器。
USER root
COPY --from=source --chown=node:node /src/bin/camoufox-135.0.1-beta.24-lin.x86_64.zip /tmp/camoufox.zip
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends unzip; \
    mkdir -p /home/node/.cache/camoufox; \
    # Windows 打包的发行包可能令 unzip 返回警告退出码，随后以二进制存在性作完整性校验。
    (unzip -q /tmp/camoufox.zip -d /home/node/.cache/camoufox || true); \
    test -f /home/node/.cache/camoufox/camoufox-bin; \
    echo '{"version":"135.0.1","release":"beta.24"}' > /home/node/.cache/camoufox/version.json; \
    chmod -R 755 /home/node/.cache/camoufox; \
    chown -R node:node /home/node/.cache/camoufox; \
    rm /tmp/camoufox.zip; \
    apt-get purge -y --auto-remove unzip; \
    rm -rf /var/lib/apt/lists/*
USER node

COPY --from=source /src/server.js ./
COPY --from=source /src/camofox.config.json ./
COPY --from=source /src/lib/ ./lib/
# 插件源代码属于快速迭代层；依赖仍固定在预镜像，避免普通 REST 协议修复触发
# Firefox、系统包与 npm 依赖的完整重装。
COPY --from=source /src/plugins/ ./plugins/
# lib/cookies.js is a compatibility re-export from ../mcp/lib/cookies.mjs, so mcp/
# must ship even though the MCP server itself is not run here. Without it the
# persistence plugin dies at load with ERR_MODULE_NOT_FOUND, the server starts
# anyway, /health keeps reporting ok, and no profile is ever written -- i.e. the
# container silently loses the durable-profile feature it exists to provide.
COPY --from=source /src/mcp/ ./mcp/

ENV NODE_ENV=production
ENV CAMOFOX_PORT=9377

EXPOSE 9377

CMD ["sh", "-c", "node --max-old-space-size=${MAX_OLD_SPACE_SIZE:-128} server.js"]
