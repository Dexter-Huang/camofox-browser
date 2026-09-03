param(
    [int]$Port = 9377,
    [switch]$EnableVnc
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 本地服务默认只绑定回环地址。Docker 部署另行通过 Compose 公开容器内端口，
# 避免调试时误把本机控制接口暴露到局域网。
$env:CAMOFOX_PORT = $Port.ToString()
$env:CAMOFOX_BIND_HOST = "127.0.0.1"
$env:ENABLE_VNC = if ($EnableVnc) { "1" } else { "0" }

$env:PYTHON = (uv python find 3.11).Trim()
$env:npm_config_python = $env:PYTHON
& $env:PYTHON --version

npm ci
npm start
