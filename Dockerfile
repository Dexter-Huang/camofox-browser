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
# 插件依赖脚本只依据 /app/camofox.config.json 决定安装集合。必须在执行脚本前
# 复制当前 fork 的配置，否则 vnc 等新启用插件会因读取到基础镜像旧配置而漏装依赖。
COPY --from=source /src/camofox.config.json ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3 curl \
    && CAMOFOX_SKIP_DOWNLOAD=1 npm ci --omit=dev \
    && sh scripts/install-plugin-deps.sh \
    && curl -fL -o /usr/local/bin/yt-dlp "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" \
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
