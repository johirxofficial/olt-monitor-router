#!/usr/bin/env bash
# Update an existing olt-monitor-router install from a v3 bundle.  Runs ON the
# server (the Windows box scp's the zip here and calls this script over ssh).
#
#   ./deploy_remote.sh --bundle ~/olt-monitor-router-v3.zip
#   ./deploy_remote.sh --bundle ~/bundle.zip --dir ~/olt-monitor-router --service olt-monitor-router
#   ./deploy_remote.sh --bundle ~/bundle.zip --dry-run
#
# What it guarantees
#   * the whole install is tarred into ./backups/ first (rollback = untar);
#   * the server's own state is NEVER overwritten:
#       .env, data/ (.secret_key + forwarding-table cache), logs/,
#       config/config.yaml, config/optical_oids.yaml
#     they are moved aside, the bundle is unpacked, and they are moved back;
#   * the Python venv is reused; only requirements.txt is re-installed;
#   * MySQL is untouched by this script.  db.py applies its schema migrations
#     (ALTER TABLE ... ADD COLUMN) on the next start, in place and without data
#     loss.  Take a dump first if you want a rollback point:
#         mysqldump --single-transaction -u <DB_USER> -p <DB_NAME> > backup.sql
#   * the service is restarted with `systemctl --user` when that unit exists,
#     otherwise with sudo, otherwise it tells you the command to run.
#
# It needs `unzip` (or python3 -m zipfile).  Nothing else.

set -euo pipefail

BUNDLE=""
DIR="$HOME/olt-monitor-router"
SERVICE="olt-monitor-router"
PORT="5002"
DRY_RUN=0
KEEP=(".env" "data" "logs" "config/config.yaml" "config/optical_oids.yaml")

while [ $# -gt 0 ]; do
  case "$1" in
    --bundle)  BUNDLE="$2"; shift 2 ;;
    --dir)     DIR="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --port)    PORT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$BUNDLE" ] || die "usage: $0 --bundle <zip>"
BUNDLE="$(readlink -f "$BUNDLE")"
[ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
[ -d "$DIR" ] || die "$DIR is not a directory (is the service installed elsewhere?)"

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUPS="$DIR/backups"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/olt-bundle-XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

say "bundle : $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
say "install: $DIR"
say "service: $SERVICE"

# ---------------------------------------------------------------- unpack ---
if command -v unzip >/dev/null 2>&1; then
  unzip -q "$BUNDLE" -d "$STAGE"
elif command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e "$BUNDLE" "$STAGE"
else
  die "neither unzip nor python3 is available"
fi
[ -f "$STAGE/run.py" ] || die "the bundle does not look like olt-monitor-router (no run.py)"

NEW_FILES="$(find "$STAGE" -type f | wc -l | tr -d ' ')"
say "$NEW_FILES files in the bundle"
if find "$STAGE" \( -name '.env' -o -name 'olt_monitor.db' -o -name '.secret_key' \) | grep -q .; then
  die "the bundle contains local state (.env / database / key) - refusing"
fi

# ---------------------------------------------------------------- backup ---
mkdir -p "$BACKUPS"
BACKUP="$BACKUPS/olt-monitor-router-$STAMP.tar.gz"
say "backing up -> $BACKUP"
tar -czf "$BACKUP" -C "$(dirname "$DIR")" "$(basename "$DIR")" \
  --exclude='backups' --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc'
du -h "$BACKUP" | cut -f1 | xargs -I{} echo "    backup size: {}"

if [ "$DRY_RUN" = "1" ]; then
  warn "dry run: nothing was copied.  Files that would change:"
  ( cd "$STAGE" && find . -type f | sed 's|^\./||' | sort ) | \
    while read -r f; do [ -f "$DIR/$f" ] || echo "    + $f"; done | head -40
  warn "(plus any file whose content differs)"
  exit 0
fi

# ------------------------------------------------------- preserve + swap ---
PRESERVE="$(mktemp -d "${TMPDIR:-/tmp}/olt-keep-XXXXXX")"
for rel in "${KEEP[@]}"; do
  if [ -e "$DIR/$rel" ]; then
    mkdir -p "$PRESERVE/$(dirname "$rel")"
    cp -a "$DIR/$rel" "$PRESERVE/$rel"
    say "kept: $rel"
  fi
done
say "preserved state -> $PRESERVE"

# copy the code over, then put the server's own files back
( cd "$STAGE" && tar -cf - . ) | ( cd "$DIR" && tar -xf - )
for rel in "${KEEP[@]}"; do
  if [ -e "$PRESERVE/$rel" ]; then
    rm -rf "$DIR/$rel"
    mkdir -p "$DIR/$(dirname "$rel")"
    cp -a "$PRESERVE/$rel" "$DIR/$rel"
  fi
done
rm -rf "$PRESERVE"
say "code updated"

# shell scripts must stay executable with LF endings
find "$DIR" -maxdepth 1 -name '*.sh' -exec sed -i 's/\r$//' {} + 2>/dev/null || true
chmod +x "$DIR"/*.sh 2>/dev/null || true
chmod +x "$DIR/tools/deploy_remote.sh" 2>/dev/null || true

# ------------------------------------------------------------- venv/deps ---
PY=""
for candidate in "$DIR/.venv/bin/python" "$HOME/.venv/bin/python" "$DIR/venv/bin/python"; do
  [ -x "$candidate" ] && PY="$candidate" && break
done
if [ -n "$PY" ]; then
  say "python: $PY ($("$PY" -V 2>&1))"
  say "installing requirements"
  "$PY" -m pip install --disable-pip-version-check -q -r "$DIR/requirements.txt"
  say "byte-compiling"
  "$PY" -m compileall -q "$DIR/olt_monitor" "$DIR/tools" && echo "    compile OK"
else
  warn "no venv found (.venv / ~/.venv) - install the deps by hand before restarting"
fi

# -------------------------------------------------------------- restart ----
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user list-unit-files 2>/dev/null | grep -q "^$SERVICE.service"; then
    say "restarting $SERVICE (systemd --user)"
    systemctl --user restart "$SERVICE"
    sleep 3
    systemctl --user --no-pager -l status "$SERVICE" | head -12 || true
  elif sudo -n true 2>/dev/null; then
    say "restarting $SERVICE (system)"
    sudo -n systemctl restart "$SERVICE"
    sleep 3
    sudo -n systemctl --no-pager -l status "$SERVICE" | head -12 || true
  else
    warn "no user unit and cannot sudo without a password - restart it yourself:"
    echo "        systemctl --user restart $SERVICE   # or ./stop.sh && ./start-bg.sh"
  fi
else
  warn "systemd not found - restart the process yourself (./stop.sh && ./start-bg.sh)"
fi

# --------------------------------------------------------------- verify ----
say "verifying http://127.0.0.1:$PORT"
for url in "/api/health" "/api/olts" "/olts" "/pons"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "http://127.0.0.1:$PORT$url" || echo 000)"
  printf '    %-14s %s\n' "$url" "$code"
done
command -v curl >/dev/null 2>&1 && curl -s --max-time 20 "http://127.0.0.1:$PORT/api/health" | head -c 300 && echo

say "done.  rollback:  tar -xzf $BACKUP -C $(dirname "$DIR")"
