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
WINDOW_PUBLISHER_GRID_COLUMNS="${WINDOW_PUBLISHER_GRID_COLUMNS:-5}"
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

# x11vnc -id 只能可靠读取未被其他顶层窗口遮挡的像素。窗口发布模式因此使用
# 扩展的单屏 Xvfb 网格；Firefox 仍只有一个进程，每个发布窗口只占一个槽位。
XVFB_RESOLUTION="$VNC_RESOLUTION"
if window_publisher_enabled; then
  IFS=x read -r window_width window_height window_depth <<<"$VNC_RESOLUTION"
  case "$window_width:$window_height:$window_depth:$WINDOW_PUBLISHER_GRID_COLUMNS:${MAX_TABS_GLOBAL:-30}" in
    *[!0-9:]*|*::* )
      echo "Invalid window publisher grid configuration" >&2
      exit 1
      ;;
  esac
  if (( window_width < 800 || window_width > 3840 \
      || window_height < 600 || window_height > 2160 \
      || (window_depth != 16 && window_depth != 24 && window_depth != 32) )); then
    window_width=1920
    window_height=1080
    window_depth=24
    VNC_RESOLUTION="1920x1080x24"
  fi
  grid_capacity="${MAX_TABS_GLOBAL:-30}"
  grid_columns="$WINDOW_PUBLISHER_GRID_COLUMNS"
  (( grid_capacity >= 1 )) || grid_capacity=1
  (( grid_columns >= 1 )) || grid_columns=1
  (( grid_columns <= 16 )) || grid_columns=16
  (( grid_columns <= grid_capacity )) || grid_columns="$grid_capacity"
  grid_rows=$(( (grid_capacity + grid_columns - 1) / grid_columns ))
  xvfb_width=$(( window_width * grid_columns ))
  xvfb_height=$(( window_height * grid_rows ))
  if (( xvfb_width > 32767 || xvfb_height > 32767 )); then
    echo "Window publisher grid exceeds X11 coordinate limits" >&2
    exit 1
  fi
  XVFB_RESOLUTION="${xvfb_width}x${xvfb_height}x${window_depth}"
fi

Xvfb "$DISPLAY" -screen 0 "$XVFB_RESOLUTION" -ac -nolisten tcp &
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
