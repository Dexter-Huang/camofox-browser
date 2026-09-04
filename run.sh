#!/usr/bin/env bash
set -Eeuo pipefail

PORT="9377"
while getopts "p:" opt; do
  case "$opt" in
    p) PORT="$OPTARG" ;;
    *) echo "Usage: $0 [-p port]" >&2; exit 1 ;;
  esac
done

export CAMOFOX_PORT="$PORT"
exec uvicorn app.main:app --host "${CAMOFOX_BIND_HOST:-127.0.0.1}" --port "$PORT"
