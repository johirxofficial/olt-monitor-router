#!/usr/bin/env bash
# Stop the background instance started by ./start-bg.sh (or a systemd unit).
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE="data/app.pid"
stopped=0

if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$PID" 2>/dev/null || true
    echo "[ok] stopped pid $PID"
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

# also stop a copy started by systemd --user, when that unit exists
if command -v systemctl >/dev/null 2>&1 && systemctl --user list-unit-files 2>/dev/null | grep -q '^olt-monitor-router.service'; then
  if systemctl --user is-active --quiet olt-monitor-router.service; then
    systemctl --user stop olt-monitor-router.service
    echo "[ok] stopped systemd --user unit olt-monitor-router.service"
    stopped=1
  fi
fi

if [ "$stopped" = "0" ]; then
  echo "[i] nothing was running"
fi
