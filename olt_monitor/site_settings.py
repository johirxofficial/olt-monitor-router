"""Site-wide settings and the small pieces of ``.env`` the web UI may edit.

Kept separate from ``olt_monitor/config.py`` on purpose:

* ``config.py`` loads YAML + environment at process start (polling thresholds,
  device seeds, MySQL credentials);
* this module owns the **runtime editable** site settings (site name, tagline,
  about text, refresh interval, web port) stored in the ``site_settings`` table,
  and writes the few environment variables that the next start must see
  (MySQL connection + ``APP_PORT``).

Only a super-admin can reach these functions through the web layer.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pymysql

from . import db

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

DEFAULTS: dict[str, str] = {
    "site_name": "OLT Monitor Router",
    "tagline": "EPON / GPON OLT + MikroTik monitoring, in one place",
    "about_text": "",
    "footer_text": "olt-monitor-router",
    "support_email": "",
    "refresh_seconds": "30",
    "web_port": "5002",
    "timezone": "Asia/Dhaka",
}

#: keys the settings form may write (everything else is ignored)
EDITABLE = tuple(DEFAULTS)

_CACHE: tuple[float, dict[str, str]] | None = None
_CACHE_TTL = 3.0


def get_all(path: str | Path | None) -> dict[str, str]:
    """Defaults merged with the rows in ``site_settings`` (cached ~3 s)."""
    global _CACHE
    import time

    now = time.monotonic()
    if _CACHE and now - _CACHE[0] < _CACHE_TTL:
        return dict(_CACHE[1])

    values = dict(DEFAULTS)
    try:
        for row in db.query(path, "SELECT `key`, `value` FROM site_settings"):
            key = str(row.get("key") or "")
            if key:
                values[key] = "" if row.get("value") is None else str(row["value"])
    except Exception:
        # before the first schema migration the table may not exist yet
        pass
    _CACHE = (now, values)
    return dict(values)


def get(path: str | Path | None, key: str, default: str | None = None) -> str:
    values = get_all(path)
    return values.get(key, DEFAULTS.get(key, default or ""))


def set_many(path: str | Path | None, values: dict[str, Any]) -> None:
    global _CACHE
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        db.execute(
            path,
            "INSERT INTO site_settings (`key`, `value`) VALUES (?, ?)"
            " ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)",
            [key, "" if value is None else str(value)],
        )
    _CACHE = None


def invalidate() -> None:
    global _CACHE
    _CACHE = None


# ---------------------------------------------------------------------------
#  .env editing (MySQL target, web port)
# ---------------------------------------------------------------------------
_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        match = _ENV_KEY.match(line)
        if not match:
            continue
        key = match.group(1)
        raw = line.split("=", 1)[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        values[key] = raw
    return values


def _quote_env(value: str) -> str:
    if not value:
        return ""
    if re.search(r"[\s#\"'\\]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def write_env(updates: dict[str, str]) -> Path:
    """Merge *updates* into ``.env`` (atomic, 0600) and keep a ``.bak`` copy."""
    existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = existing.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _ENV_KEY.match(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            out.append(f"{key}={_quote_env(str(updates[key]))}")
            seen.add(key)
        else:
            out.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- updated from the web settings page ---")
        out.extend(f"{key}={_quote_env(str(updates[key]))}" for key in missing)

    if ENV_FILE.exists():
        shutil.copy2(ENV_FILE, ENV_FILE.with_name(ENV_FILE.name + ".bak"))
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", dir=str(ENV_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out).rstrip() + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, ENV_FILE)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return ENV_FILE


# ---------------------------------------------------------------------------
#  MySQL target
# ---------------------------------------------------------------------------
def current_db_target(cfg: dict) -> dict[str, Any]:
    database = dict(cfg.get("database") or {})
    return {
        "host": str(database.get("host") or "127.0.0.1"),
        "port": int(database.get("port") or 3306),
        "name": str(database.get("name") or "olt_monitor"),
        "user": str(database.get("user") or "olt_monitor"),
    }


def test_mysql(host: str, port: int, name: str, user: str, password: str) -> dict[str, Any]:
    """Open a throw-away connection and report what is there.

    Raises :class:`ValueError` with a short, human-readable reason when the
    target cannot be used.  Never logs or returns the password.
    """
    if not host or not name or not user:
        raise ValueError("host, database and user are required")
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be a number") from exc
    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=name,
            charset="utf8mb4",
            connect_timeout=8,
            read_timeout=15,
            autocommit=True,
        )
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else 0
        if code == 1045:
            raise ValueError("access denied - wrong user or password") from exc
        if code == 1049:
            raise ValueError(f"database {name!r} does not exist on {host}") from exc
        if code == 2003:
            raise ValueError(f"cannot reach MySQL at {host}:{port}") from exc
        raise ValueError(str(exc.args[-1] if exc.args else exc)) from exc

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS version, DATABASE() AS db")
            row = cur.fetchone() or {}
            version = str(row[0] if isinstance(row, tuple) else row.get("version") or "?")
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = DATABASE()"
            )
            tables = int((cur.fetchone() or [0])[0])
        return {"version": version, "database": name, "tables": tables}
    finally:
        conn.close()


def save_db_target(target: dict[str, Any], password: str) -> Path:
    """Validate the target, then write it to ``.env`` (restart required)."""
    info = test_mysql(
        str(target.get("host") or ""),
        int(target.get("port") or 3306),
        str(target.get("name") or ""),
        str(target.get("user") or ""),
        password,
    )
    if info["tables"] == 0:
        # a brand-new database is fine, but there will be nothing to show
        pass
    return write_env(
        {
            "DB_HOST": str(target.get("host") or ""),
            "DB_PORT": str(int(target.get("port") or 3306)),
            "DB_NAME": str(target.get("name") or ""),
            "DB_USER": str(target.get("user") or ""),
            "DB_PASSWORD": password,
        }
    )


def save_web_port(port: int) -> Path:
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be a number") from exc
    if not (1024 <= port <= 65535):
        raise ValueError("port must be between 1024 and 65535")
    return write_env({"APP_PORT": str(port)})


def env_path() -> Path:
    return ENV_FILE
