#!/usr/bin/env bash
# Python Camoufox sidecar 的可选可见桌面。浏览器自动化 HTTP 服务始终由
# Uvicorn 承载。默认只启动 Xvfb 供 headed Firefox 使用，不再启动 x11vnc/noVNC。

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

xvfb_enabled() {
  enabled_flag "${ENABLE_XVFB:-0}"
}

if ! vnc_enabled && ! window_publisher_enabled && ! xvfb_enabled; then
  exec "$@"
fi

DISPLAY="${CAMOFOX_VNC_DISPLAY:-:99}"
VNC_RESOLUTION="${VNC_RESOLUTION:-1920x1080x24}"
WINDOW_PUBLISHER_GRID_COLUMNS="${WINDOW_PUBLISHER_GRID_COLUMNS:-5}"

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for pid in "${APP_PID:-}" "${XVFB_PID:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  wait "${APP_PID:-}" "${XVFB_PID:-}" 2>/dev/null || true
  exit "$status"
}

trap cleanup EXIT
trap 'exit 0' INT TERM

# 窗口发布模式仍使用扩展网格，避免遗留代码把多个顶层窗口叠在同一坐标。
# 默认 headed 自动化只需要单屏 Xvfb，不再为 VNC 预留多窗口画布。
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

export DISPLAY
"$@" &
APP_PID=$!
wait "$APP_PID"
