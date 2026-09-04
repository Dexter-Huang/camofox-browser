# 贡献指南

本项目只维护 Python Camofox sidecar。请使用 Python 3.12 和仓库已有的
`requirements-dev.txt`，不要引入 Node.js、npm、MCP 或 OpenClaw 依赖。

## 本地验证

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest tests -q
```

Docker 构建需要维护者预置的浏览器归档包：

```bash
docker build -t geo-camofox-browser:local .
```

提交前确认镜像没有 Node/npm，服务仍通过 Uvicorn 暴露 protocol v3，并且不把
归档包、profile、凭据或运行日志加入 Git。
