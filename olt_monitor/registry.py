"""Device registry - the list of OLTs and routers, stored in MySQL.

The **web UI is authoritative**: adding, editing, disabling or deleting a device
writes to the ``devices`` table and (when the poller runs in the same process)
triggers a live reload, so no restart is needed.

``config/config.yaml`` is only a *seed*: the first time the registry is read it
is filled from the config file's ``olt:`` / ``olts:`` / ``router:`` / ``routers:``
blocks.  After that the database wins and the config file is no longer used for
the device list (it still provides app/polling/alerts settings).

Only a small, safe subset of each device is editable from the web (host, port,
credentials, enable/disable, poll interval).  Firmware-specific things such as
the optical OID tables live in ``config/optical_oids.yaml`` and the tunables
(``classify``, ``oids``, ``mac_table``) stay in ``config/config.yaml``.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import secrets, vendors
from .config import (
    apply_optical_file,
    normalise_olt,
    normalise_router,
    raw_device_entries,
)
from .db import execute, init_db, query, utcnow_iso
from .logging_setup import get_logger

log = get_logger("registry")

KINDS = ("olt", "router")

# The web pages ask for the device list several times per request (page body,
# sidebar counts, optical check, per-page helpers).  Normalising four OLTs plus
# four routers each time costs a few hundred milliseconds, so the built list is
# cached for a moment and thrown away on every registry write.
_CACHE_TTL = 5.0
_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()


def invalidate_cache() -> None:
    """Drop the cached device lists (called after any registry write)."""
    with _CACHE_LOCK:
        _CACHE.clear()

# editable field -> type.  Anything not listed here is not accepted from the web.
FIELDS: dict[str, dict[str, str]] = {
    "olt": {
        # connection
        "host": "str",
        "snmp_port": "int",
        "snmp_version": "str",
        "community": "str",
        "timeout_seconds": "float",
        "retries": "int",
        "interval_seconds": "int",
        # organisation
        "branch": "str",
        "site": "str",
        "rack": "str",
        "display_order": "int",
        # identity / vendor
        "brand": "str",
        "firmware": "str",
        "model": "str",
        "pon_type": "str",
        "max_onus_per_pon": "int",
        # optional Telnet CLI
        "telnet_enabled": "bool",
        "telnet_port": "int",
        "telnet_username": "str",
        "telnet_password": "str",
        "telnet_enable_password": "str",
        "telnet_profile": "str",
        "telnet_interval_seconds": "int",
        "telnet_timeout_seconds": "float",
        "telnet_detail_max": "int",
        "pon_count": "int",
        # optional vendor HTTP API (C-Data "OLT Web Management System")
        "cdata_api_enabled": "bool",
        "cdata_api_port": "int",
        "cdata_api_tls": "bool",
        "cdata_api_verify_tls": "bool",
        "cdata_api_username": "str",
        "cdata_api_password": "str",
        "cdata_api_interval_seconds": "int",
        "cdata_api_timeout_seconds": "float",
        "cdata_api_optical": "bool",
        "cdata_api_rates": "bool",
        "cdata_api_mac": "bool",
    },
    "router": {
        "host": "str",
        "api_port": "int",
        "username": "str",
        "password": "str",
        "timeout_seconds": "float",
        "uplink_interfaces": "list",
        "use_tls": "bool",
        "verify_tls": "bool",
        "sfp_monitor": "bool",
        "monitor_traffic": "bool",
        "pppoe": "bool",
        "dhcp_leases": "bool",
        "interval_seconds": "int",
    },
}

# stored encrypted with data/.secret_key, never sent back to the browser
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "olt": (
        "community",
        "telnet_password",
        "telnet_enable_password",
        "cdata_api_password",
    ),
    "router": ("password",),
}

# the subset the editor form deals with (in order)
FORM_FIELDS: dict[str, tuple[str, ...]] = {
    "olt": (
        "branch", "site", "rack", "display_order",
        "brand", "firmware", "model", "pon_type", "max_onus_per_pon",
        "host", "snmp_port", "snmp_version", "community",
        "timeout_seconds", "retries", "interval_seconds",
        "telnet_enabled", "telnet_port", "telnet_username",
        "telnet_password", "telnet_enable_password", "telnet_profile",
        "telnet_interval_seconds", "telnet_timeout_seconds", "telnet_detail_max",
        "pon_count",
        "cdata_api_enabled", "cdata_api_port", "cdata_api_tls",
        "cdata_api_username", "cdata_api_password",
        "cdata_api_interval_seconds", "cdata_api_timeout_seconds",
        "cdata_api_optical", "cdata_api_rates", "cdata_api_mac",
    ),
    "router": (
        "host", "api_port", "username", "password", "timeout_seconds",
        "uplink_interfaces", "use_tls", "verify_tls", "sfp_monitor",
        "monitor_traffic", "pppoe", "dhcp_leases", "interval_seconds",
    ),
}

# fields that must never end up in the JSON blob (they live in their own column)
NOT_IN_CONFIG = ("id", "kind", "name", "enabled", "position", "display_order")


class RegistryError(ValueError):
    """A user-visible problem (duplicate name, bad value, unknown device)."""


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------
def _loads(text: Any) -> dict[str, Any]:
    try:
        data = json.loads(text or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce(kind: str, field: str, value: Any) -> Any:
    """Cast and validate one field coming from the web form."""
    ftype = FIELDS.get(kind, {}).get(field)
    if ftype is None:
        raise RegistryError(f"{field!r} is not a known {kind} setting")
    if value is None:
        return None
    if ftype == "str":
        return str(value).strip()
    if ftype == "int":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise RegistryError(f"{field} must be a whole number") from None
    if ftype == "float":
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            raise RegistryError(f"{field} must be a number") from None
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise RegistryError(f"{field} must be true or false")
    if ftype == "list":
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [p for p in re.split(r"[,\s]+", str(value)) if p]
    return value


def _clean_config(
    kind: str,
    data: dict[str, Any],
    *,
    db_path: str | Path,
    keep_secret: bool = False,
) -> dict[str, Any]:
    """Validate a form payload into the JSON blob we store.

    Secrets are sealed here, so no plain-text community/password ever reaches
    the database.  An empty secret on *edit* means "leave the stored one alone"; an
    empty secret on *create* means "use the built-in default".
    """
    out: dict[str, Any] = {}
    known = FIELDS.get(kind, {})
    secret_fields = SECRET_FIELDS.get(kind, ())
    other_known = FIELDS.get("router" if kind == "olt" else "olt", {})
    other_kind = "router" if kind == "olt" else "olt"

    for field, value in (data or {}).items():
        if field in NOT_IN_CONFIG:
            continue
        if field not in known:
            if field in other_known:
                # One modal holds both editors.  A browser tab that was left
                # open across an upgrade (or any client that reuses one body
                # for both kinds) can still post the other kind's keys - drop
                # them instead of failing the whole save with a confusing
                # "'snmp_version' is not a known router setting".
                log.debug("ignoring %s-only field %r on a %s", other_kind, field, kind)
                continue
            raise RegistryError(f"{field!r} is not a known {kind} setting")
        if field in secret_fields:
            if value in (None, ""):
                continue  # keep the old value (edit) / apply the default (create)
            out[field] = secrets.seal(str(value).strip(), db_path)
            continue
        out[field] = _coerce(kind, field, value)

    # ---- value checks that a type cannot express ---------------------
    if "brand" in out and str(out["brand"]).lower() not in vendors.BRAND_KEYS:
        raise RegistryError(
            "brand must be one of: " + ", ".join(vendors.BRAND_KEYS)
        )
    if "brand" in out:
        out["brand"] = str(out["brand"]).lower()
    if "pon_type" in out and str(out["pon_type"]).lower() not in ("", *vendors.PON_TYPES):
        raise RegistryError("pon_type must be epon or gpon")
    if "pon_type" in out:
        out["pon_type"] = str(out["pon_type"]).lower()
    if "telnet_profile" in out and str(out["telnet_profile"]) not in ("", *vendors.telnet_profile_names()):
        raise RegistryError(
            "telnet_profile must be one of: " + ", ".join(vendors.telnet_profile_names())
        )

    for field in ("snmp_port", "telnet_port", "api_port", "cdata_api_port"):
        if field in out and out[field] is not None and not (1 <= int(out[field]) <= 65535):
            raise RegistryError(f"{field} must be between 1 and 65535")
    if "retries" in out and not (0 <= int(out["retries"]) <= 10):
        raise RegistryError("retries must be between 0 and 10")
    for field in ("interval_seconds", "telnet_interval_seconds", "cdata_api_interval_seconds"):
        if field in out and out[field] is not None and int(out[field]) < 0:
            raise RegistryError(f"{field} cannot be negative")
    if "max_onus_per_pon" in out and out["max_onus_per_pon"] is not None:
        if not (1 <= int(out["max_onus_per_pon"]) <= 1024):
            raise RegistryError("max_onus_per_pon must be between 1 and 1024")

    # A C-Data OLT publishes no ONU table over SNMP.  If the operator filled
    # in the HTTP API credentials, turn the API on instead of silently adding
    # a device that can never show an ONU.  An explicit checkbox value always
    # wins (including an explicit "off").
    if (
        kind == "olt"
        and out.get("brand") == "cdata"
        and "cdata_api_enabled" not in out
        and out.get("cdata_api_username")
        and out.get("cdata_api_password")
    ):
        out["cdata_api_enabled"] = True

    return out


def _row_to_device(row: Any, index: int, db_path: str | Path) -> dict[str, Any]:
    """DB row -> fully normalised device dict (ready for a collector)."""
    stored = _loads(row["config"])
    stored.pop("enabled", None)
    entry = dict(stored)
    entry["name"] = row["name"]
    entry["enabled"] = bool(row["enabled"])
    normalise = normalise_olt if row["kind"] == "olt" else normalise_router
    device = normalise(entry, index)
    for field in SECRET_FIELDS.get(str(row["kind"]), ()):
        if field in device:
            device[field] = secrets.reveal(device.get(field), db_path)
    device["id"] = row["id"]
    device["kind"] = row["kind"]
    device["position"] = row["position"]
    device["display_order"] = row["position"]
    return device


def _one(db_path: str | Path, device_id: int) -> dict[str, Any]:
    rows = query(db_path, "SELECT * FROM devices WHERE id = ?", [int(device_id)])
    if not rows:
        raise RegistryError(f"device {device_id} does not exist")
    return rows[0]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _check_name(db_path: str | Path, kind: str, name: str, exclude_id: int | None = None) -> None:
    for row in query(db_path, "SELECT id FROM devices WHERE kind = ? AND name = ?", [kind, name]):
        if exclude_id is None or int(row["id"]) != int(exclude_id):
            raise RegistryError(f"a {kind} named {name!r} already exists")


def _check_unique_host(
    db_path: str | Path, cfg: dict, kind: str, host: str, exclude_id: int | None = None
) -> None:
    """Refuse a second device of the same kind pointing at the same address.

    Adding the same OLT twice doubles every SNMP walk and creates two sets of
    ONU rows, which makes the ONU/PON pages and the user matching look broken.
    """
    needle = str(host or "").strip().lower()
    if not needle:
        return
    for device in list_devices(db_path, cfg, kind):
        if int(device.get("id") or 0) == int(exclude_id or 0):
            continue
        if str(device.get("host") or "").strip().lower() == needle:
            label = "OLT" if kind == "olt" else "router"
            article = "an" if kind == "olt" else "a"
            raise RegistryError(
                f"{article} {label} with host {host} already exists as "
                f"{device.get('name')!r} - edit that entry instead of adding the "
                "same device twice"
            )


def _next_position(db_path: str | Path, kind: str) -> int:
    rows = query(db_path, "SELECT COALESCE(MAX(`position`), -1) + 1 AS pos FROM devices WHERE kind = ?", [kind])
    return int(rows[0]["pos"]) if rows else 0


# ---------------------------------------------------------------------------
#  seeding
# ---------------------------------------------------------------------------
_SEAL_CHECKED: set[str] = set()


def seal_plaintext_secrets(db_path: str | Path, *, force: bool = False) -> int:
    """Encrypt any secret that is still stored in the clear.

    A registry created before 2.1 (or seeded from an old ``config.yaml``) has a
    plain-text community / password in its JSON blob.  Reads still work - the
    envelope passes plain text through - but this upgrades them in place so the
    database file no longer contains the secret.  Runs once per process.
    """
    key = str(db_path)
    if not force and key in _SEAL_CHECKED:
        return 0
    _SEAL_CHECKED.add(key)

    changed = 0
    for row in query(db_path, "SELECT id, kind, config FROM devices"):
        config = _loads(row["config"])
        touched = False
        for field in SECRET_FIELDS.get(str(row["kind"]), ()):
            value = config.get(field)
            if value and not secrets.is_sealed(value):
                config[field] = secrets.seal(str(value), db_path)
                touched = True
        if touched:
            execute(
                db_path,
                "UPDATE devices SET config = ?, updated_at = ? WHERE id = ?",
                [json.dumps(config), utcnow_iso(), int(row["id"])],
            )
            changed += 1
    if changed:
        invalidate_cache()
    return changed


def ensure_seeded(db_path: str | Path, cfg: dict) -> int:
    """Fill the registry from config.yaml the first time it is used.

    Returns the number of devices inserted (0 when the registry already exists).
    Creates the schema when needed, so it is safe to call before the poller has
    started (the web UI can be opened on a brand-new database).
    """
    init_db(db_path)
    rows = query(db_path, "SELECT COUNT(*) AS n FROM devices")
    if rows and int(rows[0]["n"]):
        # a registry written before 2.1 keeps its community/passwords in the
        # clear - upgrade them the first time we touch the database
        seal_plaintext_secrets(db_path)
        return 0

    entries = raw_device_entries(cfg)
    if not entries:
        return 0

    stamp = utcnow_iso()
    positions: dict[str, int] = {"olt": 0, "router": 0}
    inserted = 0
    for kind, entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name:
            name = f"{'OLT' if kind == 'olt' else 'MikroTik'}-{positions[kind] + 1}"
        if query(db_path, "SELECT id FROM devices WHERE kind = ? AND name = ?", [kind, name]):
            continue
        enabled = 1 if entry.get("enabled", True) else 0
        payload = {
            k: v for k, v in entry.items()
            if k in FIELDS.get(kind, {}) and v is not None
        }
        for field in SECRET_FIELDS.get(kind, ()):
            if payload.get(field):
                payload[field] = secrets.seal(payload[field], db_path)
        try:
            execute(
                db_path,
                "INSERT INTO devices (kind, name, enabled, `position`, config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [kind, name, enabled, positions[kind], json.dumps(payload), stamp, stamp],
            )
        except Exception:  # pragma: no cover - unique clash race
            continue
        positions[kind] += 1
        inserted += 1
    if inserted:
        invalidate_cache()
    return inserted


# ---------------------------------------------------------------------------
#  reads
# ---------------------------------------------------------------------------
def device_lists(db_path: str | Path, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Return ``(olts, routers)`` as normalised device dicts, from the registry.

    Seeds the registry from ``config.yaml`` on first use, then merges the optical
    OID file (``config/optical_oids.yaml``) over every OLT.
    """
    ensure_seeded(db_path, cfg)
    olts = list_devices(db_path, cfg, "olt")
    routers = list_devices(db_path, cfg, "router")
    apply_optical_file(olts)
    return olts, routers


def list_devices(db_path: str | Path, cfg: dict, kind: str | None = None) -> list[dict]:
    """All devices of one kind (or both), fully normalised."""
    key = (str(Path(db_path).resolve()), str(kind or "*"))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return copy.deepcopy(hit[1])

    ensure_seeded(db_path, cfg)
    if kind in KINDS:
        sql = "SELECT * FROM devices WHERE kind = ? ORDER BY `position`, name"
        rows = query(db_path, sql, [kind])
    else:
        rows = query(db_path, "SELECT * FROM devices ORDER BY kind, `position`, name")
    devices = [_row_to_device(row, i, db_path) for i, row in enumerate(rows)]
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), devices)
    return copy.deepcopy(devices)


def get_device(db_path: str | Path, device_id: int) -> dict[str, Any]:
    """One normalised device by id (raises ``RegistryError`` if missing)."""
    row = _one(db_path, device_id)
    return _row_to_device(row, 0, db_path)


def form_view(device: dict[str, Any]) -> dict[str, Any]:
    """The subset shown in the editor, with secrets masked."""
    kind = str(device.get("kind") or "olt")
    view: dict[str, Any] = {
        "id": device.get("id"),
        "kind": kind,
        "name": device.get("name"),
        "enabled": bool(device.get("enabled", True)),
        "host": device.get("host"),
        "display_order": device.get("position", 0),
        "brand_label": device.get("brand_label") or "",
        "pon_type": device.get("pon_type") or "",
        "configured": bool(str(device.get("host") or "").strip()),
    }
    secret_fields = SECRET_FIELDS.get(kind, ())
    for field in FORM_FIELDS.get(kind, ()):
        if field in secret_fields:
            view[f"{field}_set"] = bool(device.get(field))
            continue
        value = device.get(field)
        if field == "uplink_interfaces":
            value = ", ".join(str(v) for v in (value or []))
        view[field] = value
    if kind == "router":
        view["password_set"] = bool(device.get("password"))
    return view


def public_devices(db_path: str | Path, cfg: dict) -> dict[str, list[dict[str, Any]]]:
    """Everything the Devices page needs, secrets removed."""
    return {
        "olts": [form_view(d) for d in list_devices(db_path, cfg, "olt")],
        "routers": [form_view(d) for d in list_devices(db_path, cfg, "router")],
        "defaults": {
            "olt": {
                "snmp_port": 161,
                "snmp_version": "2c",
                "timeout_seconds": 4,
                "retries": 2,
                "telnet_port": 23,
                "telnet_interval_seconds": 300,
                "telnet_timeout_seconds": 8,
                "telnet_detail_max": 8,
                "cdata_api_port": 80,
                "cdata_api_tls": False,
                "cdata_api_interval_seconds": 300,
                "cdata_api_timeout_seconds": 12,
                "cdata_api_optical": True,
                "cdata_api_rates": True,
                "cdata_api_mac": True,
                "brand": "auto",
                "pon_type": "",
                "display_order": 0,
            },
            "router": {"api_port": 8728, "timeout_seconds": 10},
        },
        "brands": [
            {"key": key, "label": prof["label"], "pon_type": prof["pon_type"],
             "verified": bool(prof.get("verified"))}
            for key, prof in vendors.PROFILES.items()
        ],
        "pon_types": list(vendors.PON_TYPES),
        "telnet_profiles": [
            {"key": key, "label": value["label"]}
            for key, value in vendors.TELNET_PROFILES.items()
        ],
    }


# ---------------------------------------------------------------------------
#  writes
# ---------------------------------------------------------------------------
def create_device(db_path: str | Path, cfg: dict, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise RegistryError(f"kind must be one of {KINDS}")
    ensure_seeded(db_path, cfg)

    name = str((data or {}).get("name") or "").strip()
    if not name:
        raise RegistryError("a name is required")
    _check_name(db_path, kind, name)

    payload = _clean_config(kind, data, db_path=db_path)
    host = str(payload.get("host") or "").strip()
    if not host:
        raise RegistryError("host is required")
    _check_unique_host(db_path, cfg, kind, host)

    position = data.get("display_order")
    if position in (None, ""):
        position = _next_position(db_path, kind)
    else:
        try:
            position = int(position)
        except (TypeError, ValueError):
            raise RegistryError("display_order must be a whole number") from None

    stamp = utcnow_iso()
    device_id = execute(
        db_path,
        "INSERT INTO devices (kind, name, enabled, `position`, config, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            kind, name, 1 if data.get("enabled", True) else 0,
            position, json.dumps(payload), stamp, stamp,
        ],
    )
    invalidate_cache()
    return get_device(db_path, device_id)


def update_device(db_path: str | Path, cfg: dict, device_id: int, data: dict[str, Any]) -> dict[str, Any]:
    row = _one(db_path, device_id)
    kind = str(row["kind"])

    stored = _loads(row["config"])
    stored.update(_clean_config(kind, data, db_path=db_path, keep_secret=True))

    name = str(row["name"])
    if data.get("name") is not None:
        new_name = str(data.get("name") or "").strip()
        if not new_name:
            raise RegistryError("a name is required")
        if new_name != name:
            _check_name(db_path, kind, new_name, exclude_id=int(device_id))
            name = new_name

    enabled = int(row["enabled"])
    if data.get("enabled") is not None:
        enabled = 1 if _as_bool(data.get("enabled")) else 0

    position = int(row["position"])
    if data.get("display_order") not in (None, ""):
        try:
            position = int(data.get("display_order"))
        except (TypeError, ValueError):
            raise RegistryError("display_order must be a whole number") from None

    if not str(stored.get("host") or "").strip():
        raise RegistryError("host is required")
    _check_unique_host(db_path, cfg, kind, str(stored.get("host") or ""), exclude_id=int(device_id))

    execute(
        db_path,
        "UPDATE devices SET name = ?, enabled = ?, `position` = ?, config = ?, updated_at = ? WHERE id = ?",
        [name, enabled, position, json.dumps(stored), utcnow_iso(), int(device_id)],
    )
    invalidate_cache()
    return get_device(db_path, device_id)


def set_enabled(db_path: str | Path, cfg: dict, device_id: int, enabled: bool) -> dict[str, Any]:
    ensure_seeded(db_path, cfg)
    _one(db_path, device_id)
    execute(
        db_path,
        "UPDATE devices SET enabled = ?, updated_at = ? WHERE id = ?",
        [1 if enabled else 0, utcnow_iso(), int(device_id)],
    )
    invalidate_cache()
    return get_device(db_path, device_id)


def delete_device(db_path: str | Path, cfg: dict, device_id: int) -> dict[str, Any]:
    """Delete a device and everything that was collected for it."""
    ensure_seeded(db_path, cfg)
    row = _one(db_path, device_id)
    kind, name = str(row["kind"]), str(row["name"])

    execute(db_path, "DELETE FROM devices WHERE id = ?", [int(device_id)])

    if kind == "olt":
        for table in ("olt_port_current", "olt_onu_current", "olt_health_current", "olt_health_history"):
            execute(db_path, f"DELETE FROM {table} WHERE olt_name = ?", [name])
        execute(
            db_path,
            "UPDATE pppoe_user_current SET onu_olt_name = NULL, onu_ifindex = NULL,"
            " onu_name = NULL, match_method = NULL, rx_power_dbm = NULL,"
            " tx_power_dbm = NULL, olt_rx_power_dbm = NULL, distance_m = NULL"
            " WHERE onu_olt_name = ?",
            [name],
        )
    else:
        for table in ("router_iface_current", "pppoe_user_current"):
            execute(db_path, f"DELETE FROM {table} WHERE router_name = ?", [name])
    execute(db_path, "DELETE FROM device_current WHERE `key` = ?", [f"{kind}:{name}"])
    invalidate_cache()
    return {"id": int(device_id), "kind": kind, "name": name}


def counts(db_path: str | Path, cfg: dict) -> dict[str, Any]:
    """Small summary used by the Devices page header."""
    ensure_seeded(db_path, cfg)
    rows = query(
        db_path,
        "SELECT kind, COUNT(*) AS total, SUM(enabled) AS enabled FROM devices GROUP BY kind",
    )
    out: dict[str, Any] = {"olt": {"total": 0, "enabled": 0}, "router": {"total": 0, "enabled": 0}}
    for row in rows:
        out[str(row["kind"])] = {"total": int(row["total"] or 0), "enabled": int(row["enabled"] or 0)}
    return out
