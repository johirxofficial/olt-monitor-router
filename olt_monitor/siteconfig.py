"""Portable, config-only site export / import.

The Settings backup is deliberately **not** a database dump.  It contains only
the configuration a fresh installation needs:

* every OLT / router, including its credentials (decrypted for transport so the
  file can be restored on another machine that has a different
  ``data/.secret_key``);
* the editable site settings (name, tagline, refresh interval, timezone, ...);
* web users, roles, permissions and password hashes.

Collected measurements - ONU optical levels, PPPoE sessions, router dBm,
forwarding tables, alerts and all history - are **never** exported.  Importing
a file clears those tables and the normal 10-minute polling fills them again
for the restored devices.

The JSON is versioned so a future release can migrate an older file instead of
silently mis-reading it.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, auth, db, registry, secrets, site_settings
from .logging_setup import get_logger

log = get_logger("siteconfig")

FORMAT = "olt-monitor-router/site-config"
SCHEMA_VERSION = 1
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

#: tables that hold collected measurements / derived state and must be emptied
#: when a different site configuration is imported
COLLECTED_TABLES: tuple[str, ...] = (
    "device_current",
    "olt_mac_cache",
    "olt_port_current",
    "olt_onu_current",
    "olt_health_current",
    "router_iface_current",
    "pppoe_user_current",
    "alerts_current",
    "olt_onu_history",
    "pppoe_user_history",
    "port_history",
    "router_history",
    "olt_health_history",
)

#: user columns that travel with the configuration
USER_FIELDS = (
    "username",
    "password_hash",
    "role",
    "display_name",
    "email",
    "active",
    *auth.PERMISSION_KEYS,
)

class SiteConfigError(ValueError):
    """A user-visible problem with an upload or a stored configuration."""


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------
def _loads(text: Any) -> dict[str, Any]:
    try:
        data = json.loads(text or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, field: str, *, low: int | None = None, high: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SiteConfigError(f"{field} must be a whole number") from None
    if low is not None and number < low:
        raise SiteConfigError(f"{field} must be at least {low}")
    if high is not None and number > high:
        raise SiteConfigError(f"{field} must be at most {high}")
    return number


def _as_float(value: Any, field: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise SiteConfigError(f"{field} must be a number") from None


def _as_text(value: Any, field: str, *, limit: int = 65535) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > limit:
        raise SiteConfigError(f"{field} is too long (max {limit} characters)")
    return text


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def suggested_filename() -> str:
    return f"site-config-{_stamp()}.json"


# ---------------------------------------------------------------------------
#  export
# ---------------------------------------------------------------------------
def export_config(path: str | Path) -> dict[str, Any]:
    """Build the config-only payload (device secrets are decrypted)."""
    devices: list[dict[str, Any]] = []
    for row in db.query(
        path,
        "SELECT kind, name, enabled, `position`, config FROM devices "
        "ORDER BY kind, `position`, name",
    ):
        kind = str(row.get("kind") or "")
        if kind not in registry.KINDS:
            continue
        config = _loads(row.get("config"))
        for field in registry.SECRET_FIELDS.get(kind, ()):
            if config.get(field):
                config[field] = secrets.reveal(config[field], path)
        devices.append(
            {
                "kind": kind,
                "name": str(row.get("name") or ""),
                "enabled": bool(int(row.get("enabled") or 0)),
                "position": int(row.get("position") or 0),
                "config": config,
            }
        )

    users: list[dict[str, Any]] = []
    for row in db.query(path, "SELECT * FROM users ORDER BY username"):
        user: dict[str, Any] = {}
        for field in USER_FIELDS:
            if field == "active":
                user[field] = bool(int(row.get(field) or 0))
            elif field in auth.PERMISSION_KEYS:
                user[field] = bool(int(row.get(field) or 0))
            else:
                user[field] = "" if row.get(field) is None else str(row[field])
        users.append(user)

    settings = site_settings.get_all(path)
    return {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "app_version": __version__,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "contains": {
            "devices": True,
            "device_credentials": True,
            "site_settings": True,
            "web_users": True,
            "onu_data": False,
            "optical_levels": False,
            "pppoe_sessions": False,
            "router_dbm": False,
            "history": False,
        },
        "devices": devices,
        "site_settings": settings,
        "users": users,
    }


def export_json(path: str | Path) -> str:
    return json.dumps(export_config(path), indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
#  validation
# ---------------------------------------------------------------------------
def _clean_device_config(kind: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SiteConfigError("each device needs a 'config' object")
    known = registry.FIELDS.get(kind, {})
    secret_fields = registry.SECRET_FIELDS.get(kind, ())
    out: dict[str, Any] = {}
    for field, value in data.items():
        ftype = known.get(field)
        if ftype is None:
            # future / unknown fields are ignored rather than injected
            continue
        if value is None:
            out[field] = None
            continue
        if ftype == "str":
            text = _as_text(value, field)
            if field in secret_fields and secrets.is_sealed(text):
                raise SiteConfigError(
                    f"{field} is already encrypted - export the config from this app "
                    "instead of importing a database value"
                )
            out[field] = text
        elif ftype == "int":
            out[field] = _as_int(value, field)
        elif ftype == "float":
            out[field] = _as_float(value, field)
        elif ftype == "bool":
            out[field] = _as_bool(value)
        elif ftype == "list":
            if isinstance(value, (list, tuple)):
                out[field] = [str(item).strip() for item in value if str(item).strip()]
            else:
                out[field] = [p for p in re.split(r"[,\s]+", str(value)) if p]
    return out


def _normalise(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SiteConfigError("the file is not a site configuration")
    if str(payload.get("format") or "") != FORMAT:
        raise SiteConfigError(
            "this is not an olt-monitor-router site configuration file"
        )
    try:
        version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        raise SiteConfigError("invalid schema version") from None
    if version < 1 or version > SCHEMA_VERSION:
        raise SiteConfigError(
            f"unsupported configuration version {version} "
            f"(this installation supports up to {SCHEMA_VERSION})"
        )

    raw_devices = payload.get("devices")
    if not isinstance(raw_devices, list) or not raw_devices:
        raise SiteConfigError("the configuration contains no devices")

    devices: list[dict[str, Any]] = []
    seen_names: set[tuple[str, str]] = set()
    seen_hosts: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_devices, 1):
        if not isinstance(raw, dict):
            raise SiteConfigError(f"device #{index} is not an object")
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in registry.KINDS:
            raise SiteConfigError(f"device #{index} has an unknown kind")
        name = _as_text(raw.get("name"), f"device #{index} name", limit=191)
        if not name:
            raise SiteConfigError(f"device #{index} has no name")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise SiteConfigError(
                f"device name {name!r} contains control characters"
            )
        key = (kind, name.lower())
        if key in seen_names:
            raise SiteConfigError(f"duplicate device {kind}:{name}")
        seen_names.add(key)

        config = _clean_device_config(kind, raw.get("config"))
        host = str(config.get("host") or "").strip().lower()
        if not host:
            raise SiteConfigError(f"device {name!r} has no host address")
        host_key = (kind, host)
        if host_key in seen_hosts:
            raise SiteConfigError(
                f"two {kind} devices point at the same host {host!r}"
            )
        seen_hosts.add(host_key)

        devices.append(
            {
                "kind": kind,
                "name": name,
                "enabled": _as_bool(raw.get("enabled", True)),
                "position": _as_int(raw.get("position", index - 1), "position"),
                "config": config,
            }
        )

    raw_settings = payload.get("site_settings") or {}
    if not isinstance(raw_settings, dict):
        raise SiteConfigError("site_settings must be an object")
    settings: dict[str, str] = {}
    for key, value in raw_settings.items():
        if key not in site_settings.DEFAULTS:
            continue
        settings[key] = _as_text(value, f"setting {key}", limit=200_000)
    if not settings.get("site_name"):
        settings["site_name"] = site_settings.DEFAULTS["site_name"]
    if "refresh_seconds" in settings:
        settings["refresh_seconds"] = str(
            _as_int(settings["refresh_seconds"], "refresh_seconds", low=5, high=3600)
        )
    if "web_port" in settings:
        settings["web_port"] = str(
            _as_int(settings["web_port"], "web_port", low=1, high=65535)
        )

    raw_users = payload.get("users")
    if not isinstance(raw_users, list) or not raw_users:
        raise SiteConfigError("the configuration contains no web users")
    users: list[dict[str, Any]] = []
    seen_users: set[str] = set()
    active_admins = 0
    for index, raw in enumerate(raw_users, 1):
        if not isinstance(raw, dict):
            raise SiteConfigError(f"web user #{index} is not an object")
        try:
            username = auth.validate_username(str(raw.get("username") or ""))
        except ValueError as exc:
            raise SiteConfigError(f"web user #{index}: {exc}") from None
        if username in seen_users:
            raise SiteConfigError(f"duplicate web user {username!r}")
        seen_users.add(username)

        role = str(raw.get("role") or "viewer").strip().lower()
        if role not in auth.ROLES:
            raise SiteConfigError(f"web user {username!r} has an unknown role")
        password_hash = str(raw.get("password_hash") or "").strip()
        parts = password_hash.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            raise SiteConfigError(
                f"web user {username!r} has an invalid password hash"
            )
        active = _as_bool(raw.get("active", True))
        if role == "superadmin" and active:
            active_admins += 1
        user = {
            "username": username,
            "password_hash": password_hash,
            "role": role,
            "display_name": _as_text(
                raw.get("display_name") or username, "display_name", limit=191
            ),
            "email": _as_text(raw.get("email"), "email", limit=191),
            "active": active,
        }
        for key in auth.PERMISSION_KEYS:
            user[key] = _as_bool(raw.get(key))
        users.append(user)

    if not active_admins:
        raise SiteConfigError(
            "the configuration must contain at least one active super-admin"
        )

    return {
        "schema_version": version,
        "devices": devices,
        "site_settings": settings,
        "users": users,
    }


# ---------------------------------------------------------------------------
#  import
# ---------------------------------------------------------------------------
def _clear_collected(path: str | Path) -> int:
    """Empty every measurement table.  TRUNCATE is instant, DELETE is a fallback."""
    removed = 0
    for table in COLLECTED_TABLES:
        try:
            db.execute(path, f"TRUNCATE TABLE `{table}`")
            removed += 1
        except Exception:
            # some MySQL users may not hold DROP (needed by TRUNCATE); deleting
            # still leaves the system correct, just slower on very large tables
            try:
                db.execute(path, f"DELETE FROM `{table}`")
                removed += 1
            except Exception:
                log.warning("could not clear collected table %s", table, exc_info=True)
    return removed


def _replace_config(path: str | Path, normalised: dict[str, Any]) -> None:
    """Replace devices, site settings and users in one transaction."""
    stamp = db.utcnow_iso()
    conn = db.connect(path)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM devices")
            for device in normalised["devices"]:
                config = dict(device["config"])
                for field in registry.SECRET_FIELDS.get(device["kind"], ()):
                    value = config.get(field)
                    if value:
                        config[field] = secrets.seal(value, path)
                cur.execute(
                    "INSERT INTO devices (kind, name, enabled, `position`, config,"
                    " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        device["kind"],
                        device["name"],
                        1 if device["enabled"] else 0,
                        int(device["position"]),
                        json.dumps(config, ensure_ascii=False),
                        stamp,
                        stamp,
                    ],
                )

            cur.execute("DELETE FROM site_settings")
            for key, value in normalised["site_settings"].items():
                cur.execute(
                    "INSERT INTO site_settings (`key`, `value`) VALUES (%s, %s)",
                    [key, value],
                )

            # A restored configuration must never reuse a session cookie from
            # before the import.  A fresh, monotonically larger epoch is
            # written for every user; the route then issues a new cookie for
            # the admin performing the import.  Nanoseconds keep two imports
            # in the same second distinct.
            epoch = int(time.time_ns())
            cur.execute("DELETE FROM users")
            columns = ["username", "password_hash", "role", "display_name", "email",
                       "active", "session_epoch", "created_at", "updated_at",
                       *auth.PERMISSION_KEYS]
            placeholders = ", ".join("%s" for _ in columns)
            for user in normalised["users"]:
                values: list[Any] = [
                    user["username"],
                    user["password_hash"],
                    user["role"],
                    user["display_name"],
                    user["email"],
                    1 if user["active"] else 0,
                    epoch,
                    stamp,
                    stamp,
                ]
                values.extend(1 if user[key] else 0 for key in auth.PERMISSION_KEYS)
                cur.execute(
                    f"INSERT INTO users ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def _clear_legacy_fdb(path: str | Path) -> None:
    """Old installs kept a JSON forwarding cache next to the key; drop it."""
    data_dir = Path(path).parent
    try:
        for item in data_dir.glob("fdb_*.json"):
            item.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove legacy forwarding caches", exc_info=True)


def import_config(path: str | Path, payload: Any) -> dict[str, Any]:
    """Validate *payload* and replace the site configuration.

    Returns a small summary for the UI / audit log.  Collected data is cleared
    first; polling fills it in again for the restored devices.
    """
    normalised = _normalise(payload)
    db.init_db(path)

    cleared = _clear_collected(path)
    _replace_config(path, normalised)
    _clear_legacy_fdb(path)

    registry.invalidate_cache()
    site_settings.invalidate()

    log.info(
        "site config imported: %s devices, %s users, %s tables cleared",
        len(normalised["devices"]),
        len(normalised["users"]),
        cleared,
    )
    return {
        "devices": len(normalised["devices"]),
        "olts": sum(1 for d in normalised["devices"] if d["kind"] == "olt"),
        "routers": sum(1 for d in normalised["devices"] if d["kind"] == "router"),
        "users": len(normalised["users"]),
        "settings": len(normalised["site_settings"]),
        "tables_cleared": cleared,
    }


def load_upload(raw: bytes) -> dict[str, Any]:
    """Parse an uploaded JSON file with a friendly error message."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise SiteConfigError(
            f"file is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
        )
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise SiteConfigError("the file is not UTF-8 text") from None
    except ValueError as exc:
        raise SiteConfigError(f"invalid JSON: {exc}") from None
