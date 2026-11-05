#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  One-time installer for olt-monitor-router on Ubuntu / Debian (Linux).
#
#      ./install.sh                    install + enable as a systemd --user service
#      ./install.sh --no-service       install only (start with ./start.sh)
#      ./install.sh --migrate          also import an old data/olt_monitor.db
#      ./install.sh --mysql-admin-pass PASSWORD
#
#  It is safe to run again: it upgrades the venv/dependencies, re-checks the
#  schema and restarts the service.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

WITH_SERVICE=1
MIGRATE=0
ADMIN_PASS="${MYSQL_ADMIN_PASSWORD:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-service) WITH_SERVICE=0 ;;
    --migrate) MIGRATE=1 ;;
    --mysql-admin-pass) ADMIN_PASS="${2:-}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

PY=${PY:-python3}
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "[!] Python 3.10+ is required (found $("$PY" -V 2>&1))." >&2
  echo "    sudo apt install python3 python3-venv python3-pip" >&2
  exit 1
fi

# ---- system packages (only when missing; may ask for sudo) ----------------
need_apt=0
command -v mysql >/dev/null 2>&1 || need_apt=1
"$PY" -c 'import venv' >/dev/null 2>&1 || need_apt=1
if [ "$need_apt" = "1" ] && command -v apt-get >/dev/null 2>&1; then
  echo "[*] installing missing system packages (python3-venv, mysql-server)..."
  sudo apt-get update -qq || true
  sudo apt-get install -y python3 python3-venv python3-pip mysql-server || true
fi

# ---- python environment ---------------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
  echo "[*] creating .venv"
  "$PY" -m venv .venv
fi
echo "[*] installing Python dependencies"
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

mkdir -p logs data

# ---- config + .env --------------------------------------------------------
if [ ! -f "config/config.yaml" ] && [ -f "config/config.example.yaml" ]; then
  cp "config/config.example.yaml" "config/config.yaml"
  echo "[i] config/config.yaml created from the template"
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[!] .env created.  Fill in the real ROUTER_*/OLT_* values, then run:"
  echo "      ./install.sh"
  exit 0
fi

# ---- MySQL database + schema ---------------------------------------------
if ! ./.venv/bin/python - <<'PY' >/dev/null 2>&1
from olt_monitor.config import load_config
from olt_monitor import db
db.configure(load_config())
db.init_db()
PY
then
  echo "[*] MySQL database is not ready - running the one-time setup"
  if [ -n "$ADMIN_PASS" ]; then
    ./.venv/bin/python tools/setup_mysql.py --admin-password "$ADMIN_PASS"
  else
    ./.venv/bin/python tools/setup_mysql.py
  fi
else
  ./.venv/bin/python - <<'PY'
from olt_monitor.config import load_config
from olt_monitor import db
db.configure(load_config())
db.init_db()
print("[ok] MySQL schema ready at " + db.describe())
PY
  # tools/selftest.py needs its own empty database; create it when the app
  # user is allowed to (setup_mysql.py grants it by default)
  ./.venv/bin/python - <<'PY' || echo "[i] self-test DB not ready - run tools/setup_mysql.py once"
from olt_monitor.config import load_config
from olt_monitor import db
cfg = load_config()
db.configure({"database": {**cfg["database"], "name": cfg["database"]["name"] + "_selftest"}})
db.init_db()
print("[ok] self-test schema ready at " + db.describe())
PY
fi

# ---- optional import of an old SQLite database ----------------------------
if [ "$MIGRATE" = "1" ] && [ -f "data/olt_monitor.db" ]; then
  echo "[*] importing data/olt_monitor.db (current + history tables)"
  ./.venv/bin/python tools/migrate_sqlite_to_mysql.py --all
fi

# ---- systemd service ------------------------------------------------------
if [ "$WITH_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/olt-monitor-router.service" <<UNIT
[Unit]
Description=OLT + Router Monitor (MySQL)
After=network-online.target mysql.service
Wants=network-online.target mysql.service

[Service]
Type=simple
WorkingDirectory=$PWD
EnvironmentFile=-$PWD/.env
ExecStart=$PWD/.venv/bin/python $PWD/run.py
Restart=always
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now olt-monitor-router.service
  echo "[ok] systemd --user service enabled (starts at login)"
  echo "     systemctl --user status olt-monitor-router"
  # keep it running after logout when the system allows it (no sudo needed
  # for your own user on Ubuntu)
  timeout 8 loginctl enable-linger "$USER" >/dev/null 2>&1 || \
    echo "[i] to run after logout:  sudo loginctl enable-linger $USER"
else
  echo "[i] service not installed.  Start it with:  ./start-bg.sh"
fi

# stop the foreground copy from earlier smoke tests, if any
if [ -f "data/app.pid" ] && kill -0 "$(cat data/app.pid 2>/dev/null)" 2>/dev/null; then
  kill "$(cat data/app.pid)" 2>/dev/null || true
  rm -f data/app.pid
fi

PORT=$(./.venv/bin/python -c 'from olt_monitor.config import load_config, dig; print(dig(load_config(), "app.port", 5002))')
echo
echo "Done.  Dashboard:  http://localhost:$PORT"
echo "        status:    ./status.sh"
echo "        stop:      ./stop.sh     start: ./start-bg.sh"
