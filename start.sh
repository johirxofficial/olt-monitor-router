#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  olt-monitor-router - Linux / macOS launcher
#
#      ./start.sh              start dashboard + polling (foreground)
#      ./start.sh --check      test device connectivity and exit
#      ./start.sh --no-poll    dashboard only, data already in MySQL
#      ./start.sh --port 9000  override the web port
#
#  Everything is stored in MySQL.  The first time, run ./install.sh (or
#  tools/setup_mysql.py) to create the database.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-python3}

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[!] .env created - fill in ROUTER_* / OLT_* / DB_* and run again."
  exit 0
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[*] creating virtual environment..."
  "$PY" -m venv .venv
fi

# install / refresh dependencies only when something is missing
if ! ./.venv/bin/python -c "import fastapi, uvicorn, jinja2, yaml, dotenv, pymysql" >/dev/null 2>&1; then
  echo "[*] installing dependencies..."
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null
  ./.venv/bin/python -m pip install -r requirements.txt
fi

mkdir -p logs data

# friendly check instead of a traceback when the database was never created
if ! ./.venv/bin/python - <<'PY' >/dev/null 2>&1
from olt_monitor.config import load_config
from olt_monitor import db
db.configure(load_config())
db.init_db()
PY
then
  echo "[!] MySQL is not ready.  Run the one-time setup:"
  echo "      ./.venv/bin/python tools/setup_mysql.py"
  exit 1
fi

echo "[*] database: $(./.venv/bin/python -c 'from olt_monitor.config import load_config; from olt_monitor import db; db.configure(load_config()); print(db.describe())')"
exec ./.venv/bin/python run.py "$@"
