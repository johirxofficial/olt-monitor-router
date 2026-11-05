#!/usr/bin/env bash
# Start olt-monitor-router in the background.
#
# If the systemd --user unit exists this delegates to it, so there is never a
# systemd copy *and* a plain nohup copy fighting over the same port.  Without
# systemd (or when arguments are passed, e.g. --port 5003) it falls back to a
# nohup process tracked in data/app.pid with logs in logs/console.{out,err}.
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE="data/app.pid"
LOG_OUT="logs/console.out"
LOG_ERR="logs/console.err"
UNIT="olt-monitor-router.service"
mkdir -p logs data

PORT=$(.venv/bin/python - <<'PY' 2>/dev/null || echo 5002
from olt_monitor.config import load_config
from olt_monitor.config import dig
print(dig(load_config(), "app.port", 5002))
PY
)

# a stale pid file from a killed process
if [ -f "$PID_FILE" ] && ! kill -0 "$(cat "$PID_FILE" 2>/dev/null || echo 0)" 2>/dev/null; then
  rm -f "$PID_FILE"
fi

have_unit=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user list-unit-files 2>/dev/null | grep -q "^${UNIT}"; then
  have_unit=1
fi

if [ "$have_unit" = "1" ] && [ "$#" -eq 0 ]; then
  # stop a copy that was started by an older ./start-bg.sh, otherwise both
  # would try to bind the same port
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "[i] stopping the plain background copy (pid $OLD_PID) and handing over to systemd"
      kill "$OLD_PID" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$OLD_PID" 2>/dev/null || break
        sleep 0.5
      done
      kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi

  if systemctl --user is-active --quiet "$UNIT"; then
    echo "[i] already running under systemd (${UNIT}) on http://localhost:$PORT"
    exit 0
  fi

  systemctl --user start "$UNIT"
  for _ in $(seq 1 30); do
    if systemctl --user is-active --quiet "$UNIT"; then
      echo "[ok] started systemd --user unit ${UNIT} on http://localhost:$PORT"
      echo "     status: systemctl --user status ${UNIT}"
      echo "     logs:   journalctl --user -u ${UNIT} -f"
      exit 0
    fi
    sleep 0.5
  done
  echo "[!] systemd did not report the unit as active - last log lines:"
  journalctl --user -u "$UNIT" -n 20 --no-pager 2>/dev/null || true
  exit 1
fi

# ---- plain nohup fallback -------------------------------------------------
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "[i] already running (pid $(cat "$PID_FILE"))"
  exit 0
fi

if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  exec 3>&- 3<&-
  echo "[!] port $PORT is already in use - another copy is running."
  echo "    Stop it with ./stop.sh, or start with ./start-bg.sh --port <other>."
  exit 1
fi

nohup ./.venv/bin/python run.py "$@" >"$LOG_OUT" 2>"$LOG_ERR" &
echo $! > "$PID_FILE"
sleep 2

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "[ok] started (pid $(cat "$PID_FILE")) on http://localhost:$PORT"
  echo "     logs: $LOG_OUT / $LOG_ERR"
else
  echo "[!] failed to start - last lines:"
  tail -20 "$LOG_ERR" || true
  rm -f "$PID_FILE"
  exit 1
fi
