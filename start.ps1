param(
    [int]$Port = 9377,
    [switch]$EnableVnc
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:CAMOFOX_PORT = $Port.ToString()
$env:CAMOFOX_BIND_HOST = "127.0.0.1"
$env:ENABLE_VNC = if ($EnableVnc) { "1" } else { "0" }

if (Test-Path ".venv/Scripts/python.exe") {
    & ".venv/Scripts/python.exe" -m uvicorn app.main:app --host $env:CAMOFOX_BIND_HOST --port $Port
} else {
    & uvicorn app.main:app --host $env:CAMOFOX_BIND_HOST --port $Port
}
