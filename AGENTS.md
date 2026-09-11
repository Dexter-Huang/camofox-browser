# Camofox Browser Agent Guide

本子模块是 GEO RPA protocol v4 的 Python sidecar，使用 FastAPI/Uvicorn 和
`camoufox==0.5.5`、`playwright==1.59.0`。Node.js、npm、MCP、OpenClaw 插件和
旧 JavaScript 服务已经废弃，不得恢复或新增运行时依赖。

## 运行与构建

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest tests -q
docker build -t geo-camofox-browser:local .
docker compose --env-file .env up -d --no-build camofox-browser
```

镜像必须使用 `python:3.12-slim-bookworm`，只从 `bin/` 中预置的固定
`camoufox-152.0.4-beta.29-lin.x86_64.zip` 安装浏览器，并校验 SHA-256。
镜像运行时不下载浏览器、不包含 Node.js，入口为 `uvicorn app.main:app`。

## 运行边界

- 对外只提供 `app/main.py` 中 GEO RPA protocol v4 所需的受限 HTTP 路由。
- 不提供任意 JavaScript 执行、任意 URL/cookie/header、通用调试协议或 MCP 入口。
- 账号登录快照由 GEO `storageState` 持有；sidecar 仅在内存 Context 中使用 Cookie + LocalStorage，不得写入本地 profile volume，也不得导入完整 Firefox profile 或 Sandbox/Chromium cookie。
- `ENABLE_WINDOW_PUBLISHER=true` 时才发布账号级 X11 窗口；共享桌面不能作为账号观察器暴露。
- 关闭会话必须等待 teardown barrier，超时必须清理 Context 索引并重启受控浏览器。

## 文件边界

- `app/`：FastAPI 路由、Camoufox 生命周期、noVNC 和窗口发布实现。
- `docker-compose.yaml`：浏览器服务器独立部署。
- `docker-entrypoint.sh`：可选 Xvfb/x11vnc/noVNC 进程树和 Uvicorn 启动。
- `tests/*.py`：Python 协议、预热和 VNC 检查。
- `bin/`：由维护者预置并审核的浏览器归档包，不提交到 Git。

修改协议或安全边界时，必须同步更新 `README.md`、主仓库部署文档和 Python 测试。
