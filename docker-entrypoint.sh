#!/usr/bin/env bash
# Python Camoufox sidecar 的可选可见桌面。浏览器自动化 HTTP 服务始终由
# Uvicorn 承载；仅在 ENABLE_VNC=true 时才创建 Xvfb/noVNC 进程树。

set -Eeuo pipefail

enabled_flag() {
  case "${1:-0}" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

vnc_enabled() {
  enabled_flag "${ENABLE_VNC:-0}"
}

window_publisher_enabled() {
  enabled_flag "${ENABLE_WINDOW_PUBLISHER:-0}"
}

if ! vnc_enabled && ! window_publisher_enabled; then
  exec "$@"
fi

DISPLAY="${CAMOFOX_VNC_DISPLAY:-:99}"
VNC_RESOLUTION="${VNC_RESOLUTION:-1920x1080x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_BIND="${VNC_BIND:-127.0.0.1}"
VNC_PASSWORD_FILE=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for pid in "${APP_PID:-}" "${NOVNC_PID:-}" "${X11VNC_PID:-}" "${XVFB_PID:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  wait "${APP_PID:-}" "${NOVNC_PID:-}" "${X11VNC_PID:-}" "${XVFB_PID:-}" 2>/dev/null || true
  [[ -z "$VNC_PASSWORD_FILE" ]] || rm -f "$VNC_PASSWORD_FILE"
  exit "$status"
}

trap cleanup EXIT
trap 'exit 0' INT TERM

Xvfb "$DISPLAY" -screen 0 "$VNC_RESOLUTION" -ac -nolisten tcp &
XVFB_PID=$!
for _ in $(seq 1 50); do
  xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
  sleep 0.1
done
xdpyinfo -display "$DISPLAY" >/dev/null 2>&1

if vnc_enabled; then
X11VNC_ARGS=(-display "$DISPLAY" -forever -shared -localhost -rfbport "$VNC_PORT" -noxdamage -quiet)
if enabled_flag "${VIEW_ONLY:-0}"; then
  # 专用人工浏览器可按部署需要只读；共享自动化 sidecar 不得暴露该桌面。
  X11VNC_ARGS+=(-viewonly)
fi
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  VNC_PASSWORD_FILE=/tmp/camoufox-vnc.passwd
  x11vnc -storepasswd "$VNC_PASSWORD" "$VNC_PASSWORD_FILE" >/dev/null
  X11VNC_ARGS+=(-rfbauth "$VNC_PASSWORD_FILE")
else
  X11VNC_ARGS+=(-nopw)
fi
x11vnc "${X11VNC_ARGS[@]}" &
X11VNC_PID=$!

# 6080 只运行静态 noVNC 页面和到容器回环 x11vnc 的受限字节桥接；不启动
# 第二个 Camoufox 进程，也不引入 Node/websockify 运行时。
uvicorn app.novnc:app --host "$VNC_BIND" --port "$NOVNC_PORT" &
NOVNC_PID=$!
fi

export DISPLAY
"$@" &
APP_PID=$!
wait "$APP_PID"
