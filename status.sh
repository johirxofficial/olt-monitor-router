#!/usr/bin/env bash
# Show whether the monitor is running, the MySQL target and the last log lines.
set -uo pipefail
cd "$(dirname "$0")"

PID_FILE="data/app.pid"
PORT=$(.venv/bin/python - <<'PY' 2>/dev/null || echo 5002
from olt_monitor.config import load_config
from olt_monitor.config import dig
print(dig(load_config(), "app.port", 5002))
PY
)

echo "== process =="
running=0
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
  echo "  running (pid $(cat "$PID_FILE"), background)"
  running=1
elif pgrep -af "python.*run\.py" | grep -v grep | head -3 | grep -q .; then
  pgrep -af "python.*run\.py" | grep -v grep | head -3 | sed 's/^/  /'
  running=1
else
  echo "  not running"
fi

echo "== systemd --user =="
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user is-active olt-monitor-router.service 2>/dev/null | sed 's/^/  /'
fi

echo "== web =="
if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | head -c 600; then
  echo
else
  echo "  no answer on http://127.0.0.1:$PORT"
fi

echo "== database =="
./.venv/bin/python - <<'PY' 2>/dev/null || echo "  (could not read database settings)"
from olt_monitor.config import load_config
from olt_monitor import db
cfg = load_config()
db.configure(cfg)
print("  " + db.describe())
rows = db.query(None, "SELECT COUNT(*) AS n FROM devices")
print(f"  devices: {rows[0]['n']}")
PY

echo "== last log lines =="
tail -8 logs/olt_monitor.log 2>/dev/null | sed 's/^/  /' || echo "  (no log yet)"
