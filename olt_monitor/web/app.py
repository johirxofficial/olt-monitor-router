"""FastAPI application: HTML dashboard plus a JSON API.

Everything is read from MySQL, so the web process and the polling process can
even run separately (they only share the database server).

Two kinds of data live here:

* **collected** rows (``olt_*``, ``router_*``, ``pppoe_user_*``) written by the
  pollers - shown on the dashboard, PON/ONU/user pages;
* the **device registry** (``devices`` table) - fully managed from the web
  (``/devices``).  Adding/editing/deleting a device calls ``Monitor.reload()``
  when the poller runs in this process, so no restart is needed.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from .. import __version__, auth, bundle as bundle_mod, health, oui as oui_mod, registry, site_settings, siteconfig, sqladmin, vendors
from ..config import ROOT as PROJECT_ROOT
from ..config import db_path as resolve_db_path
from ..config import dig
from ..db import configure as configure_db
from ..db import describe as describe_db
from ..db import init_db, query
from ..logging_setup import get_logger
from ..snmp import SnmpClient, SnmpError, format_bps, format_bytes, format_dbm, format_uptime

log = get_logger("web")

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

STATE_COLOURS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

OPTICAL_KEYS = ("rx_power", "tx_power", "olt_rx", "temperature", "distance", "mac")


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------
def to_local(value: str | None, tz_name: str = "UTC") -> str:
    """Convert a stored UTC ISO string to the configured timezone.

    Windows has no tz database unless the ``tzdata`` package is installed, so a
    failed ``ZoneInfo`` lookup falls back to the *OS* timezone instead of UTC.
    Falling back to UTC showed every "last poll" six hours in the past, which
    looks exactly like a device that stopped being polled.
    """
    if not value:
        return "n/a"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        parsed = parsed.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        parsed = parsed.astimezone(local)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def days_hours(seconds: Any) -> str:
    return format_uptime(seconds)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def safe_sort(column: str, allowed: set[str], default: str) -> str:
    return column if column in allowed else default


def _device_filter(sql: str, params: list[Any], column: str, value: str | None) -> str:
    if value:
        sql += f" AND {column} = ?"
        params.append(value)
    return sql


def _pon_number(if_name: Any) -> int | None:
    """``EPON0/7`` -> ``7``, ``epon 0/1/1`` -> ``11`` (any vendor prefix).

    Thin wrapper over :func:`olt_monitor.vendors.pon_number` so the web layer
    and the collectors can never disagree about which port a number means.
    """
    return vendors.pon_number(if_name)


def _as_int(value: Any, default: int | None = None) -> int | None:
    """Tolerant int for query strings: ``""``/``None``/junk -> ``default``.

    HTML forms submit every field, so an untouched ``<select>`` arrives as an
    empty string - which FastAPI would otherwise reject with a 422.
    """
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _limit(value: Any, default: int, maximum: int) -> int:
    """Clamp a requested row limit instead of failing the request."""
    number = _as_int(value, default)
    if number is None:
        number = default
    return max(1, min(int(number), int(maximum)))


# ---------------------------------------------------------------------------
#  data access :: devices
# ---------------------------------------------------------------------------
def _devices(db_path: str | Path, cfg: dict) -> tuple[list[dict], list[dict]]:
    return registry.device_lists(db_path, cfg)


def _device_rows(db_path: str | Path, cfg: dict) -> dict[str, Any]:
    """Raw ``device_current`` rows split by kind, plus the configured names.

    No derived metric is computed here - see :func:`_olt_counters` and
    :func:`_health_fields`.
    """
    tz = dig(cfg, "app.timezone", "Asia/Dhaka")
    olts, routers = _devices(db_path, cfg)

    rows: list[dict[str, Any]] = []
    for row in query(db_path, "SELECT * FROM device_current ORDER BY kind, name"):
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (ValueError, TypeError):
            payload = {}
        row = dict(row)
        row["payload"] = payload
        row["updated_local"] = to_local(row.get("updated_at"), tz)
        rows.append(row)

    olt_rows = [r for r in rows if r.get("kind") == "olt"]
    router_rows = [r for r in rows if r.get("kind") == "router"]

    seen = {r.get("name") for r in olt_rows}
    for device in olts:
        if device["name"] not in seen:
            olt_rows.append(
                {
                    "key": f"olt:{device['name']}",
                    "kind": "olt",
                    "name": device["name"],
                    "host": device.get("host"),
                    "ok": None,
                    "payload": {},
                    "error": None,
                    "updated_local": "n/a",
                    "pending": True,
                }
            )
    seen = {r.get("name") for r in router_rows}
    for device in routers:
        if device["name"] not in seen:
            router_rows.append(
                {
                    "key": f"router:{device['name']}",
                    "kind": "router",
                    "name": device["name"],
                    "host": device.get("host"),
                    "ok": None,
                    "payload": {},
                    "error": None,
                    "updated_local": "n/a",
                    "pending": True,
                }
            )

    # mark disabled devices (they are configured but not polled)
    disabled_olts = {d["name"] for d in olts if not d.get("enabled", True)}
    disabled_routers = {d["name"] for d in routers if not d.get("enabled", True)}
    for row in olt_rows:
        row["disabled"] = row.get("name") in disabled_olts
    for row in router_rows:
        row["disabled"] = row.get("name") in disabled_routers

    return {
        "devices": rows,
        "olts": olt_rows,
        "routers": router_rows,
        "registry_olts": olts,
        "registry_routers": routers,
        "olt_names": [d["name"] for d in olts],
        "router_names": [d["name"] for d in routers],
    }


def _olt_counters(db_path: str | Path, cfg: dict) -> dict[str, Any]:
    """Per-OLT rolling numbers: health row, ONU/PON counts, linked users."""
    weak_limit = float(dig(cfg, "alerts.onu_rx_power_low_dbm", -27.0))

    health_rows: dict[str, dict[str, Any]] = {
        str(row.get("olt_name")): row
        for row in query(db_path, "SELECT * FROM olt_health_current")
    }

    onu_stats: dict[str, dict[str, Any]] = {}
    for row in query(
        db_path,
        """
        SELECT olt_name,
               COUNT(*)                                           AS total,
               SUM(CASE WHEN oper_status = 1 THEN 1 ELSE 0 END)    AS up,
               SUM(CASE WHEN oper_status = 2 THEN 1 ELSE 0 END)    AS down,
               SUM(CASE WHEN rx_power_dbm < ? AND oper_status = 1 THEN 1 ELSE 0 END) AS weak,
               AVG(rx_power_dbm)                                  AS rx_avg
          FROM olt_onu_current
         GROUP BY olt_name
        """,
        [weak_limit],
    ):
        onu_stats[str(row["olt_name"])] = row

    pon_stats: dict[str, dict[str, Any]] = {
        str(row["olt_name"]): row
        for row in query(
            db_path,
            """
            SELECT olt_name,
                   COUNT(*)                                         AS total,
                   SUM(CASE WHEN oper_status = 1 THEN 1 ELSE 0 END)  AS up
              FROM olt_port_current WHERE kind = 'pon'
             GROUP BY olt_name
            """,
        )
    }

    # A user is linked either to one ONU (exact MAC/name match) or, on
    # firmware that hides the subscriber FDB, to the PON that carries the ONU.
    # Count both - otherwise an OLT that only has PON-level links (Bashar
    # Photon) reports 0 users on the overview even though the Users page lists
    # dozens behind it.
    user_stats: dict[str, int] = {
        str(row["olt_name"]): int(row["users"] or 0)
        for row in query(
            db_path,
            """
            SELECT olt_name, COUNT(*) AS users FROM (
                SELECT o.olt_name AS olt_name, u.router_name AS router_name,
                       u.name AS name
                  FROM pppoe_user_current u
                  JOIN olt_onu_current o
                    ON o.olt_name = u.onu_olt_name AND o.ifindex = u.onu_ifindex
                 WHERE u.onu_ifindex IS NOT NULL
                UNION
                SELECT u.onu_olt_name, u.router_name, u.name
                  FROM pppoe_user_current u
                 WHERE u.onu_ifindex IS NULL AND u.onu_pon IS NOT NULL
            ) linked
            GROUP BY olt_name
            """,
        )
    }

    return {
        "health": health_rows,
        "onus": onu_stats,
        "pons": pon_stats,
        "users": user_stats,
        "weak_dbm": weak_limit,
    }


def _health_fields(row: dict[str, Any], counters: dict[str, Any]) -> dict[str, Any]:
    """The health score and metrics for one raw OLT row.

    Both the dashboard and the OLT page show a score, so it is computed in one
    place and both pages agree down to the reason list.
    """
    name = str(row.get("name"))
    payload = row.get("payload") or {}
    h = (counters.get("health") or {}).get(name) or {}
    onus = (counters.get("onus") or {}).get(name) or {}
    pons = (counters.get("pons") or {}).get(name) or {}

    enabled = not row.get("disabled")
    online = bool(row.get("ok")) and enabled
    onus_total = int(onus.get("total") or 0)
    onus_up = int(onus.get("up") or 0)
    pons_total = int(pons.get("total") or 0)
    pons_up = int(pons.get("up") or 0)

    if enabled:
        score, reasons = health.score(
            online=online,
            error=row.get("error"),
            cpu_percent=h.get("cpu_percent"),
            memory_percent=h.get("memory_percent"),
            temperature_c=h.get("temperature_c"),
            onus_up=onus_up,
            onus_total=onus_total,
            pons_up=pons_up,
            pons_total=pons_total,
            has_metrics=bool(h.get("ok")),
        )
        grade, label = health.grade(score), health.label(score)
    else:
        # A device the operator switched off is not "critical": showing it as
        # unreachable sent people looking for a network fault that does not exist.
        score = 0
        reasons = ["polling is paused - press Enable on the Devices page"]
        grade, label = "secondary", "Paused"

    uptime_seconds = h.get("uptime_seconds")
    if uptime_seconds is None and payload.get("sys_uptime_ticks") is not None:
        try:
            uptime_seconds = int(payload["sys_uptime_ticks"]) // 100
        except (TypeError, ValueError):
            uptime_seconds = None

    return {
        "enabled": enabled,
        "online": online,
        "ok": row.get("ok"),
        "pending": bool(row.get("pending")),
        "health": score,
        "health_grade": grade,
        "health_label": label,
        "health_reasons": reasons,
        "cpu_percent": h.get("cpu_percent"),
        "memory_percent": h.get("memory_percent"),
        "memory_total_mb": h.get("memory_total_mb"),
        "memory_free_mb": h.get("memory_free_mb"),
        "temperature_c": h.get("temperature_c"),
        "snmp_rtt_ms": h.get("snmp_rtt_ms"),
        "telnet_ok": h.get("telnet_ok"),
        "telnet_ms": h.get("telnet_ms"),
        "telnet_error": h.get("telnet_error"),
        "uptime_seconds": uptime_seconds,
        "uptime_text": payload.get("sys_uptime_text")
        or (format_uptime(uptime_seconds) if uptime_seconds is not None else ""),
        "onus_total": onus_total,
        "onus_up": onus_up,
        "onus_down": int(onus.get("down") or 0),
        "onus_weak": int(onus.get("weak") or 0),
        "rx_avg": onus.get("rx_avg"),
        "pons_total": pons_total,
        "pons_up": pons_up,
        "users": int((counters.get("users") or {}).get(name, 0)),
        "branch": payload.get("branch"),
    }


def device_overview(db_path: str | Path, cfg: dict) -> dict[str, Any]:
    """``device_current`` rows split by kind, with health merged onto each OLT."""
    base = _device_rows(db_path, cfg)
    counters = _olt_counters(db_path, cfg)
    for row in base["olts"]:
        row.update(_health_fields(row, counters))
    base["weak_dbm"] = counters["weak_dbm"]
    return base


def olt_overview(db_path: str | Path, cfg: dict) -> dict[str, Any]:
    """Per-OLT health cards for the OLT Overview page.

    Merges four sources: the registry (branch/brand/site), ``device_current``
    (online + uptime), ``olt_health_current`` (CPU/RAM/temp/RTT) and the ONU/PON
    tables (counts).
    """
    base = _device_rows(db_path, cfg)
    counters = _olt_counters(db_path, cfg)
    devices = {d["name"]: d for d in base["registry_olts"]}

    cards: list[dict[str, Any]] = []
    for row in base["olts"]:
        name = str(row.get("name"))
        device = devices.get(name) or {}
        payload = row.get("payload") or {}
        detected = payload.get("detected_brand") or {}

        card = {
            "name": name,
            "id": device.get("id"),
            "host": device.get("host") or row.get("host"),
            "branch": device.get("branch") or "",
            "site": device.get("site") or "",
            "rack": device.get("rack") or "",
            "brand": detected.get("brand") or device.get("brand") or "auto",
            "brand_label": detected.get("label") or device.get("brand_label")
            or vendors.brand_label(device.get("brand")),
            "brand_verified": bool(detected.get("verified")),
            "pon_type": device.get("pon_type") or detected.get("pon_type") or "epon",
            "firmware": device.get("firmware") or payload.get("firmware") or "",
            "model": device.get("model") or payload.get("model") or "",
            "display_order": device.get("display_order", device.get("position", 0)),
            "error": row.get("error"),
            "last_poll": row.get("updated_local"),
            "updated_at": row.get("updated_at"),
            "pon_type_warning": payload.get("pon_type_warning"),
            "optical_note": payload.get("optical_note"),
            "telnet_note": payload.get("telnet_note"),
            "mac_table_note": payload.get("mac_table_note"),
            "cdata_note": payload.get("cdata_note"),
            "cdata_error": payload.get("cdata_error"),
        }
        card.update(_health_fields(row, counters))
        cards.append(card)

    cards.sort(key=lambda c: (int(c.get("display_order") or 0), str(c["name"]).lower()))
    totals = {
        "olts": len(cards),
        "online": sum(1 for c in cards if c["online"]),
        "onus": sum(c["onus_total"] for c in cards),
        "onus_up": sum(c["onus_up"] for c in cards),
        "users": sum(c["users"] for c in cards),
        "critical": sum(1 for c in cards if c["health_grade"] in ("danger", "orange")),
    }
    return {"olts": cards, "totals": totals}


def device_payload(db_path: str | Path, kind: str, name: str) -> dict[str, Any]:
    """The JSON payload a collector stored for a device ({} when not polled)."""
    rows = query(
        db_path, "SELECT payload FROM device_current WHERE kind = ? AND name = ?", [kind, name]
    )
    if not rows:
        return {}
    try:
        data = json.loads(rows[0].get("payload") or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
#  data access :: collected rows
# ---------------------------------------------------------------------------
def dashboard_data(db_path: str | Path, cfg: dict) -> dict[str, Any]:
    overview = device_overview(db_path, cfg)

    totals = query(
        db_path,
        """
        SELECT
            COUNT(*)                                            AS onu_total,
            SUM(CASE WHEN oper_status = 1 THEN 1 ELSE 0 END)     AS onu_up,
            SUM(CASE WHEN oper_status = 2 THEN 1 ELSE 0 END)     AS onu_down,
            SUM(CASE WHEN rx_power_dbm IS NOT NULL THEN 1 ELSE 0 END) AS onu_with_power,
            MIN(rx_power_dbm)                                   AS rx_min,
            MAX(rx_power_dbm)                                   AS rx_max,
            AVG(rx_power_dbm)                                   AS rx_avg
            FROM olt_onu_current
        """,
    )[0]

    ports = query(
        db_path,
        """
        SELECT COUNT(*) AS ports_total,
               SUM(CASE WHEN kind='uplink' AND oper_status=1 THEN 1 ELSE 0 END) AS uplinks_up,
               SUM(CASE WHEN kind='uplink' THEN 1 ELSE 0 END)                   AS uplinks_total,
               SUM(CASE WHEN kind='pon' AND oper_status=1 THEN 1 ELSE 0 END)    AS pons_up,
               SUM(CASE WHEN kind='pon' THEN 1 ELSE 0 END)                      AS pons_total
        FROM olt_port_current
        """,
    )[0]

    users = query(
        db_path,
        """
        SELECT COUNT(*) AS online,
               SUM(CASE WHEN onu_ifindex IS NOT NULL OR onu_pon IS NOT NULL
                        THEN 1 ELSE 0 END) AS matched,
               SUM(CASE WHEN rx_power_dbm IS NOT NULL AND rx_power_dbm <= ? THEN 1 ELSE 0 END) AS weak,
               SUM(COALESCE(bytes_in,0) + COALESCE(bytes_out,0))        AS traffic_bytes
        FROM pppoe_user_current
        """,
        [float(dig(cfg, "alerts.onu_rx_power_low_dbm", -27.0))],
    )[0]

    alert_rows = query(db_path, "SELECT * FROM alerts_current WHERE active=1 LIMIT 1000")
    alert_rows.sort(key=lambda r: (SEVERITY_ORDER.get(str(r.get("severity")), 9), r.get("ref") or ""))
    alert_counts = {"critical": 0, "warning": 0, "info": 0}
    for row in alert_rows:
        alert_counts[str(row.get("severity"))] = alert_counts.get(str(row.get("severity")), 0) + 1

    pon_summary = query(
        db_path,
        """
        SELECT o.olt_name,
               o.pon,
               COUNT(*)                                              AS onus,
               SUM(CASE WHEN o.oper_status=1 THEN 1 ELSE 0 END)       AS up,
               SUM(CASE WHEN o.oper_status=2 THEN 1 ELSE 0 END)       AS down,
               AVG(o.rx_power_dbm)                                    AS rx_avg
        FROM olt_onu_current o
        WHERE o.pon IS NOT NULL
        GROUP BY o.olt_name, o.pon
        ORDER BY o.olt_name, o.pon
        """,
    )

    uplinks = query(
        db_path,
        """
        SELECT olt_name, ifindex, if_name, oper_status, in_bps, out_bps, in_errors, out_errors
        FROM olt_port_current WHERE kind='uplink' ORDER BY olt_name, ifindex
        """,
    )

    pon_numbers = sorted(
        {int(r["pon"]) for r in query(db_path, "SELECT DISTINCT pon FROM olt_onu_current WHERE pon IS NOT NULL")}
    )

    return {
        "totals": totals,
        "ports": ports,
        "users": users,
        "alerts": alert_rows[:200],
        "alert_counts": alert_counts,
        "alert_total": len(alert_rows),
        "pon_summary": pon_summary,
        "pon_numbers": pon_numbers or list(range(0, 17)),
        "uplinks": uplinks,
        "devices": overview["devices"],
        "olts": overview["olts"],
        "routers": overview["routers"],
        "olt_names": overview["olt_names"],
        "router_names": overview["router_names"],
        "olt_info": (overview["olts"][0]["payload"] if overview["olts"] else {}),
        "router_info": (overview["routers"][0]["payload"] if overview["routers"] else {}),
    }


def _onu_filters(
    olt: str | None,
    pon: int | None,
    status: str | None,
    search: str | None,
    only_power: bool,
    weak_dbm: float,
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause for the ONU list and its counters."""
    where = " WHERE 1=1"
    params: list[Any] = []
    if olt:
        where += " AND olt_name = ?"
        params.append(olt)
    if pon is not None:
        where += " AND pon = ?"
        params.append(pon)
    if status == "up":
        where += " AND oper_status = 1"
    elif status == "down":
        where += " AND oper_status = 2"
    elif status == "weak":
        where += " AND oper_status = 1 AND rx_power_dbm IS NOT NULL AND rx_power_dbm < ?"
        params.append(float(weak_dbm))
    elif status == "pppoe":
        # Exact links (MAC/name) plus the PON-level links some firmware only
        # exposes.  Without the second half the "has PPPoE" filter is empty on
        # PHOTON / BDPON / BTPON even though subscribers are linked to the PON.
        where += (
            " AND (ifindex IN (SELECT onu_ifindex FROM pppoe_user_current"
            "                   WHERE onu_olt_name = olt_onu_current.olt_name"
            "                     AND onu_ifindex IS NOT NULL)"
            "      OR pon IN (SELECT onu_pon FROM pppoe_user_current"
            "                  WHERE onu_olt_name = olt_onu_current.olt_name"
            "                    AND onu_ifindex IS NULL AND onu_pon IS NOT NULL))"
        )
    if only_power:
        where += " AND rx_power_dbm IS NOT NULL"
    if search:
        where += (
            " AND (if_name LIKE ? OR pppoe_user LIKE ? OR mac LIKE ? OR if_descr LIKE ?"
            "      OR serial LIKE ? OR model LIKE ? OR vlan LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like] * 7)
    return where, params


def list_onus(
    db_path: str | Path,
    olt: str | None = None,
    pon: int | None = None,
    status: str | None = None,
    search: str | None = None,
    only_power: bool = False,
    sort: str = "if_name",
    direction: str = "asc",
    limit: int = 200,
    offset: int = 0,
    weak_dbm: float = -27.0,
) -> list[dict[str, Any]]:
    allowed = {
        "if_name", "olt_name", "pon", "onu_id", "oper_status", "rx_power_dbm",
        "distance_m", "pppoe_user", "linked_user", "linked_iface", "temperature_c",
        "serial", "model", "voltage_v", "bias_current_ma", "last_down_reason",
    }
    sort = safe_sort(sort, allowed, "if_name")
    direction = "DESC" if str(direction).lower() == "desc" else "ASC"

    # ``linked_user`` is the authoritative PPPoE name (a join), because the
    # denormalised column is empty for a few seconds between an OLT poll and the
    # next correlation pass.
    #
    # Firmware such as PHOTON / BDPON / BTPON only names the PON in its
    # forwarding table, so those subscribers cannot be pinned to one ONU.  They
    # are aggregated per PON here and shown on every ONU of that PON, which is
    # far more useful than the "-" the page used to print.
    where, params = _onu_filters(olt, pon, status, search, only_power, weak_dbm)
    sql = (
        "SELECT olt_onu_current.*,"
        "       (SELECT u.name FROM pppoe_user_current u"
        "           WHERE u.onu_olt_name = olt_onu_current.olt_name"
        "             AND u.onu_ifindex = olt_onu_current.ifindex"
        "           ORDER BY u.name LIMIT 1) AS linked_user,"
        "       (SELECT u.access_iface FROM pppoe_user_current u"
        "           WHERE u.onu_olt_name = olt_onu_current.olt_name"
        "             AND u.onu_ifindex = olt_onu_current.ifindex"
        "           ORDER BY u.name LIMIT 1) AS linked_iface,"
        "       ponl.users AS pon_user_count,"
        "       ponl.iface AS pon_iface"
        "  FROM olt_onu_current"
        "  LEFT JOIN ("
        "      SELECT onu_olt_name, onu_pon, COUNT(*) AS users,"
        "             GROUP_CONCAT(DISTINCT access_iface ORDER BY access_iface"
        "                          SEPARATOR ', ') AS iface"
        "        FROM pppoe_user_current"
        "       WHERE onu_ifindex IS NULL AND onu_pon IS NOT NULL"
        "       GROUP BY onu_olt_name, onu_pon"
        "  ) ponl ON ponl.onu_olt_name = olt_onu_current.olt_name"
        "        AND ponl.onu_pon = olt_onu_current.pon"
        + where
    )
    sql += f" ORDER BY olt_name, {sort} {direction} LIMIT ? OFFSET ?"
    params.extend([int(limit), max(0, int(offset))])
    rows = query(db_path, sql, params)

    # The PON-level rows only carry a count.  Attach the first names of each
    # PON so the ONU list shows *who* is behind it, not just "53 users" - the
    # firmware cannot say which ONU, but the names are still useful on the row.
    groups = {
        (str(r.get("olt_name")), int(r.get("pon") or 0))
        for r in rows
        if r.get("pon_user_count")
    }
    if groups:
        by_pon: dict[tuple[str, int], list[str]] = {}
        for user in query(
            db_path,
            "SELECT onu_olt_name, onu_pon, name FROM pppoe_user_current"
            " WHERE onu_ifindex IS NULL AND onu_pon IS NOT NULL"
            " ORDER BY name",
        ):
            try:
                key = (str(user["onu_olt_name"]), int(user["onu_pon"]))
            except (TypeError, ValueError):
                continue
            if key in groups:
                by_pon.setdefault(key, []).append(str(user["name"]))
        for row in rows:
            try:
                key = (str(row.get("olt_name")), int(row.get("pon") or 0))
            except (TypeError, ValueError):
                key = ("", 0)
            listed = by_pon.get(key) or []
            row["pon_user_names"] = listed[:2]
            row["pon_user_more"] = max(0, len(listed) - 2)
    return rows


def onu_summary(
    db_path: str | Path,
    olt: str | None = None,
    pon: int | None = None,
    status: str | None = None,
    search: str | None = None,
    only_power: bool = False,
    weak_dbm: float = -27.0,
) -> dict[str, int]:
    """Aggregate counters for the *whole* filtered ONU set, not just one page."""
    where, params = _onu_filters(olt, pon, status, search, only_power, weak_dbm)
    sql = (
        "SELECT COUNT(*) AS total,"
        "       SUM(CASE WHEN oper_status = 1 THEN 1 ELSE 0 END) AS up,"
        "       SUM(CASE WHEN oper_status = 2 THEN 1 ELSE 0 END) AS down,"
        "       SUM(CASE WHEN oper_status = 1 AND rx_power_dbm IS NOT NULL"
        "                 AND rx_power_dbm < ? THEN 1 ELSE 0 END) AS weak,"
        "       SUM(CASE WHEN rx_power_dbm IS NULL THEN 1 ELSE 0 END) AS no_power,"
        "       SUM(CASE WHEN pppoe_user IS NOT NULL THEN 1 ELSE 0 END) AS pppoe"
        "  FROM olt_onu_current"
        + where
    )
    row = query(db_path, sql, [float(weak_dbm), *params])[0]
    return {key: int(row.get(key) or 0) for key in ("total", "up", "down", "weak", "no_power", "pppoe")}


def get_onu(db_path: str | Path, olt_name: str, ifindex: int) -> dict[str, Any] | None:
    rows = query(
        db_path,
        "SELECT * FROM olt_onu_current WHERE olt_name = ? AND ifindex = ?",
        [olt_name, int(ifindex)],
    )
    return rows[0] if rows else None


def _attach_onu_info(db_path: str | Path, rows: list[dict[str, Any]]) -> None:
    """Add ``onu_mac`` / ``onu_vendor`` / ``onu_power`` to user rows in one query.

    The user table only stores the ONU ifindex, so the shop-facing lists need a
    small join to show what device the customer actually has.
    """
    keys: set[tuple[str, int]] = set()
    for row in rows:
        if row.get("onu_ifindex") is None or not row.get("onu_olt_name"):
            continue
        try:
            keys.add((str(row["onu_olt_name"]), int(row["onu_ifindex"])))
        except (TypeError, ValueError):
            continue
    if not keys:
        return
    names = sorted({name for name, _ in keys})
    placeholders = ", ".join("?" for _ in names)
    found = query(
        db_path,
        f"SELECT olt_name, ifindex, if_name, mac, vendor, model, serial,"
        f"       oper_status, rx_power_dbm"
        f"  FROM olt_onu_current WHERE olt_name IN ({placeholders})",
        names,
    )
    by_key = {
        (str(onu.get("olt_name")), int(onu.get("ifindex") or 0)): onu for onu in found
    }
    for row in rows:
        try:
            onu = by_key.get((str(row.get("onu_olt_name")), int(row.get("onu_ifindex"))))
        except (TypeError, ValueError):
            onu = None
        if not onu:
            continue
        row["onu_mac"] = onu.get("mac")
        row["onu_vendor"] = oui_mod.vendor_for(
            onu.get("mac"), None, onu.get("vendor"), onu.get("model")
        )
        row["onu_state"] = onu.get("oper_status")
        if row.get("rx_power_dbm") is None:
            row["rx_power_dbm"] = onu.get("rx_power_dbm")


def _user_filters(
    router: str | None,
    search: str | None,
    matched: str | None,
    optical: str | None,
) -> tuple[str, list[Any]]:
    where = " WHERE 1=1"
    params: list[Any] = []
    where = _device_filter(where, params, "router_name", router)
    if search:
        where += (
            " AND (name LIKE ? OR address LIKE ? OR caller_id LIKE ? OR onu_name LIKE ?"
            "      OR access_iface LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like])
    if matched == "yes":
        where += " AND (onu_ifindex IS NOT NULL OR onu_pon IS NOT NULL)"
    elif matched == "no":
        where += " AND onu_ifindex IS NULL AND onu_pon IS NULL"
    if optical == "yes":
        where += " AND rx_power_dbm IS NOT NULL"
    elif optical == "no":
        where += " AND rx_power_dbm IS NULL"
    return where, params


def list_users(
    db_path: str | Path,
    router: str | None = None,
    search: str | None = None,
    matched: str | None = None,
    optical: str | None = None,
    sort: str = "name",
    direction: str = "asc",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    allowed = {
        "name", "address", "uptime_seconds", "bytes_in", "bytes_out",
        "rx_power_dbm", "onu_name", "match_method", "router_name", "onu_olt_name",
        "access_iface", "olt_vlan",
    }
    sort = safe_sort(sort, allowed, "name")
    direction = "DESC" if str(direction).lower() == "desc" else "ASC"

    where, params = _user_filters(router, search, matched, optical)
    sql = "SELECT * FROM pppoe_user_current" + where
    sql += f" ORDER BY router_name, {sort} {direction} LIMIT ? OFFSET ?"
    params.extend([int(limit), max(0, int(offset))])
    rows = query(db_path, sql, params)
    _attach_onu_info(db_path, rows)
    return rows


def count_users(
    db_path: str | Path,
    router: str | None = None,
    search: str | None = None,
    matched: str | None = None,
    optical: str | None = None,
) -> int:
    where, params = _user_filters(router, search, matched, optical)
    row = query(db_path, "SELECT COUNT(*) AS n FROM pppoe_user_current" + where, params)[0]
    return int(row.get("n") or 0)


def get_user(db_path: str | Path, router_name: str, name: str) -> dict[str, Any] | None:
    rows = query(
        db_path,
        "SELECT * FROM pppoe_user_current WHERE router_name = ? AND name = ?",
        [router_name, name],
    )
    return rows[0] if rows else None


def users_on_onu(db_path: str | Path, olt_name: str, ifindex: int) -> list[dict[str, Any]]:
    return query(
        db_path,
        "SELECT * FROM pppoe_user_current WHERE onu_olt_name = ? AND onu_ifindex = ? ORDER BY name",
        [olt_name, int(ifindex)],
    )


def users_on_pon(db_path: str | Path, olt_name: str, pon: int) -> list[dict[str, Any]]:
    return query(
        db_path,
        """
        SELECT u.* FROM pppoe_user_current u
        LEFT JOIN olt_onu_current o
          ON o.olt_name = u.onu_olt_name AND o.ifindex = u.onu_ifindex
        WHERE u.onu_olt_name = ?
          AND (o.pon = ? OR (u.onu_ifindex IS NULL AND u.onu_pon = ?))
        ORDER BY u.name
        """,
        [olt_name, int(pon), int(pon)],
    )


def quick_search(
    db_path: str | Path,
    term: str,
    *,
    limit: int = 60,
) -> dict[str, Any]:
    """Unified search across PPPoE users, ONUs and PONs.

    The same box accepts a username, an IP address, a MAC in any notation, an
    ONU name (``pon1:23``), a serial number, a model, or a PON name.  Each hit
    is enriched with the manufacturer of its MAC, so the result already answers
    "whose device is this?" without a second lookup.
    """
    text = (term or "").strip()
    hex_only = re.sub(r"[^0-9A-F]", "", text.upper())
    mac = None
    if len(hex_only) == 12:
        mac = ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    like = f"%{text}%"
    hex_like = f"%{hex_only}%" if len(hex_only) >= 4 else None

    user_sql = (
        "SELECT * FROM pppoe_user_current WHERE ("
        "name LIKE ? OR address LIKE ? OR caller_id LIKE ?"
        " OR onu_name LIKE ? OR access_iface LIKE ?"
        + (" OR REPLACE(caller_id, ':', '') LIKE ?" if hex_like else "")
        + ") ORDER BY (name = ?) DESC, (address = ?) DESC, name LIMIT ?"
    )
    user_params: list[Any] = [like, like, like, like, like]
    if hex_like:
        user_params.append(hex_like)
    user_params.extend([text, text, int(limit)])
    users = query(db_path, user_sql, user_params)

    onu_sql = (
        "SELECT * FROM olt_onu_current WHERE ("
        "if_name LIKE ? OR mac LIKE ? OR serial LIKE ? OR model LIKE ?"
        " OR vendor LIKE ? OR pppoe_user LIKE ?"
        + (" OR REPLACE(mac, ':', '') LIKE ?" if hex_like else "")
        + ") ORDER BY (if_name = ?) DESC, olt_name, pon, onu_id LIMIT ?"
    )
    onu_params: list[Any] = [like, like, like, like, like, like]
    if hex_like:
        onu_params.append(hex_like)
    onu_params.extend([text, int(limit)])
    onus = query(db_path, onu_sql, onu_params)

    ports: list[dict[str, Any]] = []
    if re.match(r"^(?:epon|gpon)?\s*\d+$|^pon", text, re.IGNORECASE):
        ports = query(
            db_path,
            "SELECT * FROM olt_port_current WHERE kind = 'pon' AND (if_name LIKE ?"
            " OR if_descr LIKE ?) ORDER BY olt_name, ifindex LIMIT ?",
            [like, like, 20],
        )

    for user in users:
        user["caller_vendor"] = oui_mod.vendor_for(user.get("caller_id"))
        user["onu_vendor"] = None
        if user.get("onu_olt_name") and user.get("onu_ifindex") is not None:
            onu = get_onu(db_path, str(user["onu_olt_name"]), int(user["onu_ifindex"]))
            if onu:
                user["onu"] = onu
                user["onu_vendor"] = oui_mod.vendor_for(
                    onu.get("mac"), None, onu.get("vendor"), onu.get("model")
                )
    for onu in onus:
        onu["mac_vendor"] = oui_mod.vendor_for(
            onu.get("mac"), None, onu.get("vendor"), onu.get("model")
        )
        if onu.get("pon") is not None:
            onu["pon_users"] = users_on_pon(
                db_path, str(onu.get("olt_name")), int(onu["pon"])
            )
    for port in ports:
        port["pon_users"] = users_on_pon(
            db_path, str(port.get("olt_name")), int(port.get("pon") or 0)
        )
        for user in port["pon_users"]:
            user["caller_vendor"] = oui_mod.vendor_for(user.get("caller_id"))

    return {
        "term": text,
        "mac": mac,
        "users": users,
        "onus": onus,
        "ports": ports,
        "total": len(users) + len(onus) + len(ports),
    }


def pon_rows(db_path: str | Path, cfg: dict, olt: str | None = None) -> list[dict[str, Any]]:
    """One row per PON port, enriched with the ONU/user counts behind it."""
    sql = "SELECT * FROM olt_port_current WHERE kind = 'pon'"
    params: list[Any] = []
    sql = _device_filter(sql, params, "olt_name", olt)
    ports = query(db_path, sql + " ORDER BY olt_name, ifindex", params)

    agg_sql = """
        SELECT olt_name, pon,
               COUNT(*)                                              AS onus,
               SUM(CASE WHEN oper_status = 1 THEN 1 ELSE 0 END)        AS up,
               SUM(CASE WHEN oper_status = 2 THEN 1 ELSE 0 END)        AS down,
               SUM(CASE WHEN oper_status = 1 AND rx_power_dbm IS NOT NULL
                         AND rx_power_dbm < ? THEN 1 ELSE 0 END)       AS weak,
               AVG(rx_power_dbm)                                       AS rx_avg,
               MIN(rx_power_dbm)                                       AS rx_min,
               MAX(rx_power_dbm)                                       AS rx_max,
               SUM(COALESCE(in_bps, 0))                                AS onu_in_bps,
               SUM(COALESCE(out_bps, 0))                               AS onu_out_bps
        FROM olt_onu_current WHERE pon IS NOT NULL
        GROUP BY olt_name, pon
    """
    weak_limit = float(dig(cfg, "alerts.onu_rx_power_low_dbm", -27.0))
    agg = {(r["olt_name"], int(r["pon"])): r for r in query(db_path, agg_sql, [weak_limit])}

    # Linked users come from the authoritative side (pppoe_user_current joined to
    # its ONU) rather than the denormalised olt_onu_current.pppoe_user column,
    # which is briefly empty between an OLT poll and the next correlation run.
    user_sql = """
        SELECT olt_name, pon, COUNT(*) AS users FROM (
            SELECT u.onu_olt_name AS olt_name, o.pon AS pon,
                   u.router_name AS router_name, u.name AS name
            FROM pppoe_user_current u
            JOIN olt_onu_current o
              ON o.olt_name = u.onu_olt_name AND o.ifindex = u.onu_ifindex
            WHERE u.onu_ifindex IS NOT NULL
            UNION
            SELECT u.onu_olt_name, u.onu_pon, u.router_name, u.name
            FROM pppoe_user_current u
            WHERE u.onu_ifindex IS NULL AND u.onu_pon IS NOT NULL
        ) linked
        WHERE pon IS NOT NULL
        GROUP BY olt_name, pon
    """
    for row in query(db_path, user_sql):
        info = agg.get((row["olt_name"], int(row["pon"])))
        if info is not None:
            info["users"] = row["users"]

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for port in ports:
        pon = _pon_number(port.get("if_name"))
        info = agg.get((port["olt_name"], pon), {}) if pon is not None else {}
        if pon is not None:
            seen.add((port["olt_name"], pon))
        rows.append(_pon_row(port, pon, info))

    # How a real port was named is the best guide to the OLT's architecture.
    # Fall back to the device registry for an OLT whose port table is missing.
    inferred: dict[str, str] = {}
    for port in ports:
        ptype = str(port.get("pon_type") or "").lower()
        if ptype in ("epon", "gpon"):
            inferred.setdefault(str(port.get("olt_name")), ptype)
    try:
        for device in registry.list_devices(db_path, cfg, "olt"):
            ptype = str(device.get("pon_type") or "").lower()
            if ptype in ("epon", "gpon"):
                inferred[str(device.get("name"))] = ptype
    except Exception:  # registry must never break a page
        pass

    # a PON that carries ONUs but has no port row (e.g. port table not walked yet)
    for (olt_name, pon), info in sorted(agg.items()):
        if olt is not None and olt_name != olt:
            continue
        if (olt_name, pon) in seen:
            continue
        ptype = inferred.get(olt_name, "epon")
        # Never paint an all-GPON OLT as EPON: the synthetic row uses the same
        # architecture and capacity as the real ports of that device.
        max_onus = int(
            vendors.pon_defaults(ptype).get("default_max_onus") or 64
        )
        prefix = "GPON" if ptype == "gpon" else "EPON"
        rows.append(
            _pon_row(
                {
                    "olt_name": olt_name,
                    "if_name": f"{prefix}0/{pon}",
                    "oper_status": None,
                    "pon_type": ptype,
                    "max_onus": max_onus,
                },
                pon,
                info,
            )
        )

    rows.sort(key=lambda r: (str(r["olt_name"]), r["pon"] if r["pon"] is not None else -1))
    return rows


def _pon_row(port: dict[str, Any], pon: int | None, info: dict[str, Any]) -> dict[str, Any]:
    onus = int(info.get("onus") or 0)
    up = int(info.get("up") or 0)
    pon_type = str(port.get("pon_type") or "").lower()
    default_max = int(vendors.pon_defaults(pon_type or "epon").get("default_max_onus") or 64)
    try:
        max_onus = int(port.get("max_onus") or 0) or default_max
    except (TypeError, ValueError):
        max_onus = default_max
    # Never render an impossible "120 / 64" capacity: some EPON firmware allows
    # more ONUs per PON than the nominal split, and a stale config value must
    # not win over the population the device is actually carrying.
    if onus > max_onus:
        max_onus = 128 if onus <= 128 else ((onus + 63) // 64) * 64
    oper = port.get("oper_status")
    return {
        "olt_name": port.get("olt_name"),
        "ifindex": port.get("ifindex"),
        "if_name": port.get("if_name"),
        "oper_status": oper,
        "status": "up" if oper == 1 else ("down" if oper == 2 else "unknown"),
        "pon_type": port.get("pon_type"),
        "max_onus": max_onus,
        "in_bps": port.get("in_bps"),
        "out_bps": port.get("out_bps"),
        "pon": pon,
        "onus": onus,
        "up": up,
        "down": int(info.get("down") or 0),
        "weak": int(info.get("weak") or 0),
        "users": int(info.get("users") or 0),
        "up_pct": round(up / onus * 100) if onus else None,
        "util_pct": min(100, round(onus / max_onus * 100)) if max_onus else None,
        "rx_avg": info.get("rx_avg"),
        "rx_min": info.get("rx_min"),
        "rx_max": info.get("rx_max"),
        "onu_in_bps": info.get("onu_in_bps"),
        "onu_out_bps": info.get("onu_out_bps"),
        "sfp_rx_power_dbm": port.get("sfp_rx_power_dbm"),
        "sfp_tx_power_dbm": port.get("sfp_tx_power_dbm"),
        "sfp_temperature_c": port.get("sfp_temperature_c"),
    }


def find_pon(db_path: str | Path, cfg: dict, olt_name: str, pon: int) -> dict[str, Any] | None:
    for row in pon_rows(db_path, cfg, olt=olt_name):
        if row["pon"] == int(pon):
            return row
    return None


def list_ports(db_path: str | Path, kind: str | None = None, olt: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM olt_port_current WHERE 1=1"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql = _device_filter(sql, params, "olt_name", olt)
    sql += " ORDER BY olt_name, ifindex"
    return query(db_path, sql, params)


def list_router_ifaces(db_path: str | Path, router: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM router_iface_current WHERE 1=1"
    params: list[Any] = []
    sql = _device_filter(sql, params, "router_name", router)
    sql += " ORDER BY router_name, name"
    return query(db_path, sql, params)


def list_alerts(db_path: str | Path) -> list[dict[str, Any]]:
    rows = query(db_path, "SELECT * FROM alerts_current WHERE active=1")
    rows.sort(key=lambda r: (SEVERITY_ORDER.get(str(r.get("severity")), 9), r.get("ref") or ""))
    return rows


# ---------------------------------------------------------------------------
#  application
# ---------------------------------------------------------------------------
def _optical_configured(db_path: str | Path, cfg: dict) -> bool:
    """True when at least one OLT has any optical/identity OID configured."""
    olts, _ = _devices(db_path, cfg)
    for device in olts:
        tables = device.get("onu_tables") or {}
        for key in OPTICAL_KEYS:
            spec = tables.get(key)
            if isinstance(spec, dict) and spec.get("oid"):
                return True
    return False


# ---------------------------------------------------------------------------
#  authentication helpers (used by the middleware below)
# ---------------------------------------------------------------------------
PUBLIC_PATHS = {
    "/login",
    "/setup",
    "/about",
    "/api/health",
    "/api/setup/test-db",  # only answers while no account exists
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static/",)
#: fine-grained area guard; the first matching prefix wins, so SQL endpoints
#: are listed before the generic /settings one
PERMISSION_PATHS = (
    ("/settings/export-config", "perm_sql"),
    ("/settings/import-config", "perm_sql"),
    ("/settings/export", "perm_sql"),
    ("/settings/import", "perm_sql"),
    ("/settings/backups", "perm_sql"),
    ("/api/sql", "perm_sql"),
    ("/settings", "perm_settings"),
    ("/api/admin", "perm_settings"),
    ("/admin", "perm_users"),
    ("/developer", "perm_developer"),
    ("/tools", "perm_tools"),
    ("/api/snmp", "perm_tools"),
)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()
LOGIN_MAX_FAILS = 8
LOGIN_WINDOW = 300.0


def _login_key(ip: str, username: str) -> str:
    return f"{ip}|{username.strip().lower()}"


def _login_blocked(ip: str, username: str) -> int:
    """Seconds left before another attempt is allowed (0 = allowed)."""
    key = _login_key(ip, username)
    now = time.monotonic()
    with _LOGIN_LOCK:
        recent = [t for t in _LOGIN_FAILS.get(key, []) if now - t < LOGIN_WINDOW]
        _LOGIN_FAILS[key] = recent
        if len(recent) >= LOGIN_MAX_FAILS:
            return int(LOGIN_WINDOW - (now - recent[0])) + 1
    return 0


def _login_failed(ip: str, username: str) -> None:
    key = _login_key(ip, username)
    with _LOGIN_LOCK:
        _LOGIN_FAILS.setdefault(key, []).append(time.monotonic())


def _login_ok(ip: str, username: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(_login_key(ip, username), None)


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "?")


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    # backslashes are normalised to "/" by some browsers, which would turn
    # "/\\evil.example" into a protocol-relative redirect
    if "\\" in value or any(ord(ch) < 0x20 for ch in value):
        return "/"
    return value


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _deny(request: Request, status: int, message: str) -> Response:
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status)
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>{status}</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        </head><body class="p-5"><h1>{status}</h1><p class="lead">{message}</p>
        <p><a href="/">Dashboard</a> · <a href="/about">About</a></p></body></html>""",
        status_code=status,
    )


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _required_permission(path: str) -> str | None:
    """The permission that guards *path* (``None`` for normal monitoring pages)."""
    for prefix, permission in PERMISSION_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return permission
    return None


def _permission_label(key: str) -> str:
    for item in auth.PERMISSIONS:
        if item["key"] == key:
            return item["label"]
    return key


def _strip_slash(path: str) -> str:
    return path.rstrip("/") or "/"


def create_app(cfg: dict, monitor: Any = None) -> FastAPI:
    app = FastAPI(title=dig(cfg, "app.title", "OLT + Router Monitor"), version=__version__)
    configure_db(cfg)
    db_path = resolve_db_path(cfg)
    db_label = describe_db()
    tz_name = dig(cfg, "app.timezone", "Asia/Dhaka")
    # the web layer owns users/settings tables, so make sure they exist even
    # when the app is started with --no-poll
    init_db(db_path)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["bytes"] = format_bytes
    templates.env.filters["bps"] = format_bps
    templates.env.filters["uptime"] = format_uptime
    templates.env.filters["dbm"] = format_dbm
    templates.env.filters["local"] = lambda value: to_local(value, tz_name)
    templates.env.filters["state"] = lambda value: STATE_COLOURS.get(value, "unknown")
    templates.env.filters["oui"] = lambda value: oui_mod.lookup(value) or "-"
    templates.env.filters["urlencode"] = lambda value: quote(str(value or ""))
    templates.env.globals["oui_vendor"] = oui_mod.vendor_for

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # HTML tables can still be a few hundred KB even after pagination; gzip
    # makes them several times smaller over a WAN link at almost no CPU cost.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ---- authentication / role gate -----------------------------------
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = _strip_slash(request.url.path)

        # cross-site form protection: any unsafe request must come from the
        # same host (cookies are SameSite=Lax as a second line of defence)
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                try:
                    origin_host = urlsplit(origin).netloc
                except Exception:
                    origin_host = ""
                if origin_host and origin_host != request.headers.get("host"):
                    return _deny(request, 403, "cross-origin request refused")

        token = request.cookies.get(auth.SESSION_COOKIE)
        user = auth.user_from_token(db_path, token)
        request.state.user = user
        setup_needed = auth.setup_required(db_path)

        if setup_needed:
            if path == "/setup" or _is_public(path):
                response = await call_next(request)
                return _secure(response)
            if _wants_json(request):
                return JSONResponse(
                    {"ok": False, "setup_required": True, "error": "first-run setup is required"},
                    status_code=503,
                )
            return RedirectResponse("/setup", status_code=303)

        if path == "/setup":
            return RedirectResponse("/" if user else "/login", status_code=303)

        if _is_public(path):
            if path == "/login" and user:
                return RedirectResponse("/", status_code=303)
            return _secure(await call_next(request))

        if user is None:
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": "authentication required"}, status_code=401)
            target = request.url.path
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

        is_admin = auth.is_admin(user)
        required = _required_permission(path)
        if required and not auth.has_permission(user, required):
            return _deny(
                request,
                403,
                "your account does not have permission for this area "
                f"({_permission_label(required)})",
            )
        if request.method in UNSAFE_METHODS and path not in ("/logout", "/account"):
            if path.startswith(("/api/registry", "/api/olts", "/api/poll")):
                if not auth.has_permission(user, "perm_devices"):
                    return _deny(request, 403, "your account cannot change devices")
            elif user.get("role") == "viewer":
                return _deny(request, 403, "your account is read-only (viewer)")

        response = await call_next(request)
        return _secure(response)

    def _secure(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Device changes are applied on a daemon thread: a reload closes the old
    # sockets and waits for the in-flight polls, which takes a few seconds on a
    # busy network - the browser must not sit on a spinner for that long.
    _reload_guard = threading.Lock()
    _reload_state = {"busy": False, "again": False}

    def _reload_worker() -> None:
        while True:
            if monitor is not None:
                try:
                    monitor.reload()
                except Exception:
                    log.exception("device reload failed")
            with _reload_guard:
                if not _reload_state["again"]:
                    _reload_state["busy"] = False
                    return
                _reload_state["again"] = False

    def reload_monitor() -> None:
        """Apply a device change to the running poller (no restart)."""
        if monitor is None:
            return
        with _reload_guard:
            if _reload_state["busy"]:
                _reload_state["again"] = True
                return
            _reload_state["busy"] = True
        threading.Thread(target=_reload_worker, name="device-reload", daemon=True).start()

    def site_context() -> dict[str, Any]:
        return site_settings.get_all(db_path)

    def render_public(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
        site = site_context()
        base = {
            "request": request,
            "app_title": site.get("site_name") or dig(cfg, "app.title", "OLT Monitor Router"),
            "tagline": site.get("tagline") or "",
            "service_name": site.get("site_name") or "OLT Monitor Router",
            "version": __version__,
            "site": site,
            "user": getattr(request.state, "user", None),
            "can_manage": False,
            "is_admin": False,
            "permissions": auth.permissions_of(None),
            "permission_meta": auth.PERMISSIONS,
            "setup_required": auth.setup_required(db_path),
        }
        base.update(context)
        return templates.TemplateResponse(request=request, name=template, context=base)

    def render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
        olts, routers = _devices(db_path, cfg)
        site = site_context()
        user = getattr(request.state, "user", None)
        try:
            refresh = int(site.get("refresh_seconds") or dig(cfg, "app.refresh_seconds", 30))
        except (TypeError, ValueError):
            refresh = dig(cfg, "app.refresh_seconds", 30)
        base = {
            "request": request,
            "app_title": site.get("site_name") or dig(cfg, "app.title", "OLT Monitor Router"),
            "tagline": site.get("tagline") or "",
            "site": site,
            "version": __version__,
            "refresh_seconds": max(5, refresh),
            "active": "",
            "cfg": cfg,
            "user": user,
            "can_manage": auth.can_manage(user),
            "is_admin": auth.is_admin(user),
            "permissions": auth.permissions_of(user),
            "permission_meta": auth.PERMISSIONS,
            "olt_names": [d["name"] for d in olts],
            "router_names": [d["name"] for d in routers],
            "rx_low": dig(cfg, "alerts.onu_rx_power_low_dbm", -27.0),
            "rx_critical": dig(cfg, "alerts.onu_rx_power_critical_dbm", -30.0),
            "rx_high": dig(cfg, "alerts.onu_rx_power_high_dbm", -3.0),
        }
        base.update(context)
        return templates.TemplateResponse(request=request, name=template, context=base)

    # ---- authentication / first-run setup ------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def page_login(request: Request, next: str = "") -> HTMLResponse:
        if auth.setup_required(db_path):
            return RedirectResponse("/setup", status_code=303)
        return render_public(
            request,
            "login.html",
            {"next": _safe_next(next), "error": None, "username": "", "active": ""},
        )

    @app.post("/login", response_class=HTMLResponse)
    def do_login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
    ) -> Response:
        ip = _client_ip(request)
        wait = _login_blocked(ip, username)
        if wait:
            return render_public(
                request,
                "login.html",
                {
                    "next": _safe_next(next),
                    "username": username,
                    "error": f"too many failed attempts - try again in {wait} seconds",
                    "active": "",
                },
            )
        user = auth.authenticate(db_path, username, password)
        if not user:
            _login_failed(ip, username)
            auth.audit(db_path, username, "auth.login-failed", "invalid credentials", ip)
            return render_public(
                request,
                "login.html",
                {
                    "next": _safe_next(next),
                    "username": username,
                    "error": "invalid username or password",
                    "active": "",
                },
            )
        _login_ok(ip, username)
        auth.audit(db_path, user["username"], "auth.login", f"role={user['role']}", ip)
        token = auth.create_session(db_path, int(user["id"]))
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response = RedirectResponse(_safe_next(next), status_code=303)
        response.set_cookie(
            auth.SESSION_COOKIE,
            token,
            max_age=auth.SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )
        return response

    @app.post("/logout")
    def do_logout(request: Request) -> Response:
        user = getattr(request.state, "user", None)
        if user:
            auth.bump_session_epoch(db_path, int(user["id"]))
            auth.audit(db_path, user["username"], "auth.logout", "", _client_ip(request))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @app.get("/about", response_class=HTMLResponse)
    def page_about(request: Request) -> HTMLResponse:
        return render_public(
            request,
            "about.html",
            {
                "active": "about",
                "roles": [
                    {"name": auth.ROLE_LABELS[r], "key": r, "hint": auth.ROLE_HINTS[r]}
                    for r in auth.ROLES
                ],
            },
        )

    @app.get("/account", response_class=HTMLResponse)
    def page_account(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        return render(
            request,
            "account.html",
            {
                "active": "account",
                "msg": msg,
                "err": err,
                "my_sessions": None,
            },
        )

    @app.post("/account")
    def save_account(
        request: Request,
        display_name: str = Form(""),
        email: str = Form(""),
        current_password: str = Form(""),
        new_password: str = Form(""),
        new_password2: str = Form(""),
    ) -> Response:
        user = getattr(request.state, "user", None)
        if not user:
            return RedirectResponse("/login", status_code=303)
        password_changed = False
        try:
            auth.update_user(db_path, int(user["id"]), display_name=display_name, email=email)
            if new_password:
                raw = auth.get_user_raw(db_path, int(user["id"])) or {}
                if not auth.verify_password(current_password, str(raw.get("password_hash") or "")):
                    raise ValueError("current password is wrong")
                auth.validate_password(new_password, new_password2)
                auth.set_password(db_path, int(user["id"]), new_password)
                password_changed = True
            auth.audit(
                db_path,
                user["username"],
                "account.update",
                "password changed" if new_password else "profile changed",
                _client_ip(request),
            )
        except ValueError as exc:
            return RedirectResponse(f"/account?err={quote(str(exc))}", status_code=303)
        response = RedirectResponse("/account?msg=Account+updated", status_code=303)
        if password_changed:
            # the new password bumped the session epoch - issue a fresh cookie
            token = auth.create_session(db_path, int(user["id"]))
            secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie(
                auth.SESSION_COOKIE,
                token,
                max_age=auth.SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=secure,
                path="/",
            )
        return response

    # ---- first-run wizard ---------------------------------------------
    def _setup_values() -> dict[str, str]:
        current = site_settings.current_db_target(cfg)
        site = site_settings.get_all(db_path)
        return {
            "site_name": site.get("site_name") or "",
            "tagline": site.get("tagline") or "",
            "web_port": str(dig(cfg, "app.port", 5002)),
            "db_host": current["host"],
            "db_port": str(current["port"]),
            "db_name": current["name"],
            "db_user": current["user"],
            "admin_user": "admin",
            "timezone": site.get("timezone") or dig(cfg, "app.timezone", "Asia/Dhaka"),
        }

    @app.get("/setup", response_class=HTMLResponse)
    def page_setup(request: Request) -> HTMLResponse:
        if not auth.setup_required(db_path):
            return RedirectResponse("/login", status_code=303)
        return render_public(
            request,
            "setup.html",
            {"values": _setup_values(), "error": None, "active": ""},
        )

    @app.post("/api/setup/test-db")
    def api_setup_test_db(payload: dict = Body(...)) -> dict[str, Any]:
        if not auth.setup_required(db_path):
            return {"ok": False, "error": "setup is already complete"}
        current = dict(cfg.get("database") or {})
        password = str(payload.get("password") or "") or str(current.get("password") or "")
        try:
            info = site_settings.test_mysql(
                str(payload.get("host") or ""),
                int(payload.get("port") or 3306),
                str(payload.get("name") or ""),
                str(payload.get("user") or ""),
                password,
            )
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **info}

    @app.post("/setup", response_class=HTMLResponse)
    def do_setup(
        request: Request,
        site_name: str = Form(""),
        tagline: str = Form(""),
        admin_user: str = Form(""),
        admin_password: str = Form(""),
        admin_password2: str = Form(""),
        web_port: int = Form(5002),
        timezone_name: str = Form("Asia/Dhaka"),
        db_host: str = Form(""),
        db_port: int = Form(3306),
        db_name: str = Form(""),
        db_user: str = Form(""),
        db_password: str = Form(""),
    ) -> Response:
        if not auth.setup_required(db_path):
            return RedirectResponse("/login", status_code=303)

        values = {
            "site_name": site_name,
            "tagline": tagline,
            "web_port": str(web_port),
            "db_host": db_host,
            "db_port": str(db_port),
            "db_name": db_name,
            "db_user": db_user,
            "admin_user": admin_user,
            "timezone": timezone_name,
        }

        def fail(message: str) -> HTMLResponse:
            return render_public(
                request,
                "setup.html",
                {"values": values, "error": message, "active": ""},
            )

        try:
            username = auth.validate_username(admin_user)
            auth.validate_password(admin_password, admin_password2)
            if not site_name.strip():
                raise ValueError("site name is required")
            site_settings.save_web_port(web_port)
        except ValueError as exc:
            return fail(str(exc))

        current_db = dict(cfg.get("database") or {})
        if not db_password:
            db_password = str(current_db.get("password") or "")
        target_changed = any(
            str(new) != str(old)
            for new, old in (
                (db_host, current_db.get("host")),
                (str(db_port), str(current_db.get("port"))),
                (db_name, current_db.get("name")),
                (db_user, current_db.get("user")),
                (db_password, current_db.get("password")),
            )
        )

        old_db = dict(current_db)
        if target_changed:
            try:
                site_settings.test_mysql(db_host, db_port, db_name, db_user, db_password)
            except ValueError as exc:
                return fail(f"database: {exc}")
            new_cfg = dict(cfg)
            new_cfg["database"] = {
                **current_db,
                "host": db_host,
                "port": db_port,
                "name": db_name,
                "user": db_user,
                "password": db_password,
            }
            configure_db(new_cfg)
            try:
                init_db(db_path)
            except Exception as exc:  # noqa: BLE001
                configure_db({"database": old_db})
                return fail(f"cannot initialise the database: {exc}")
            cfg["database"] = new_cfg["database"]

        try:
            auth.create_user(
                db_path,
                username,
                admin_password,
                "superadmin",
                display_name="Site administrator",
            )
        except ValueError as exc:
            if target_changed:
                # roll the .env back when we already switched the live target
                site_settings.write_env(
                    {
                        "DB_HOST": str(old_db.get("host") or ""),
                        "DB_PORT": str(old_db.get("port") or 3306),
                        "DB_NAME": str(old_db.get("name") or ""),
                        "DB_USER": str(old_db.get("user") or ""),
                        "DB_PASSWORD": str(old_db.get("password") or ""),
                    }
                )
                configure_db({"database": old_db})
            return fail(str(exc))

        updates = {
            "DB_HOST": db_host,
            "DB_PORT": str(db_port),
            "DB_NAME": db_name,
            "DB_USER": db_user,
            "DB_PASSWORD": db_password,
            "APP_PORT": str(web_port),
        }
        try:
            site_settings.write_env(updates)
        except OSError as exc:
            return fail(f"settings created, but .env could not be written: {exc}")

        site_settings.set_many(
            db_path,
            {
                "site_name": site_name.strip(),
                "tagline": tagline.strip(),
                "web_port": str(web_port),
                "timezone": timezone_name,
            },
        )
        site_settings.invalidate()
        auth.audit(db_path, username, "setup.complete", "first super admin created", _client_ip(request))

        user = auth.get_user_by_username(db_path, username)
        token = auth.create_session(db_path, int(user["id"])) if user else None
        response = RedirectResponse("/", status_code=303)
        if token:
            secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie(
                auth.SESSION_COOKIE,
                token,
                max_age=auth.SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=secure,
                path="/",
            )
        return response

    # ---- pages ---------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request) -> HTMLResponse:
        return render(request, "dashboard.html", {"active": "dashboard", "data": dashboard_data(db_path, cfg)})

    @app.get("/search", response_class=HTMLResponse)
    def page_search(request: Request, q: str = "") -> Response:
        """Quick search: username, IP, MAC, ONU name/serial or PON."""
        term = (q or "").strip()
        if not term:
            return RedirectResponse("/", status_code=303)
        result = quick_search(db_path, term)
        # An exact username or MAC hit with nothing else to disambiguate goes
        # straight to the full user page - that is the fastest path for NOC.
        if result["total"] == 1 and len(result["users"]) == 1:
            user = result["users"][0]
            return RedirectResponse(
                f"/user?router={quote(str(user['router_name']))}&name={quote(str(user['name']))}",
                status_code=303,
            )
        return render(
            request,
            "search.html",
            {
                "active": "search",
                "q": term,
                "result": result,
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/olts", response_class=HTMLResponse)
    def page_olts(request: Request) -> HTMLResponse:
        return render(
            request,
            "olts.html",
            {
                "active": "olts",
                "overview": olt_overview(db_path, cfg),
                "status": getattr(monitor, "status", {}) if monitor else {},
            },
        )

    @app.get("/devices", response_class=HTMLResponse)
    def page_devices(request: Request) -> HTMLResponse:
        return render(
            request,
            "devices.html",
            {
                "active": "devices",
                "registry": registry.public_devices(db_path, cfg),
                "counts": registry.counts(db_path, cfg),
                "status": getattr(monitor, "status", {}) if monitor else {},
                "db_path": db_label,
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/pons", response_class=HTMLResponse)
    def page_pons(request: Request, olt: str | None = None) -> HTMLResponse:
        return render(
            request,
            "pons.html",
            {
                "active": "pons",
                "olt": olt or "",
                "rows": pon_rows(db_path, cfg, olt=olt),
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/pon", response_class=HTMLResponse)
    def page_pon(request: Request, olt: str, pon: int) -> HTMLResponse:
        detail = find_pon(db_path, cfg, olt, pon)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"no PON {pon} on {olt}")
        return render(
            request,
            "pon_detail.html",
            {
                "active": "pons",
                "olt": olt,
                "pon": pon,
                "detail": detail,
                "onus": list_onus(db_path, olt=olt, pon=pon, sort="onu_id"),
                "users": users_on_pon(db_path, olt, pon),
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/onu", response_class=HTMLResponse)
    def page_onu(request: Request, olt: str, ifindex: int) -> HTMLResponse:
        onu = get_onu(db_path, olt, ifindex)
        if onu is None:
            raise HTTPException(status_code=404, detail=f"no ONU ifindex={ifindex} on {olt}")
        pon = onu.get("pon")
        pon_info = find_pon(db_path, cfg, olt, int(pon)) if pon is not None else None
        pon_users = users_on_pon(db_path, olt, int(pon)) if pon is not None else []
        return render(
            request,
            "onu_detail.html",
            {
                "active": "onus",
                "olt": olt,
                "onu": onu,
                "pon_info": pon_info,
                "onu_vendor": oui_mod.vendor_for(
                    onu.get("mac"), None, onu.get("vendor"), onu.get("model")
                ),
                "users": users_on_onu(db_path, olt, ifindex),
                "pon_users": [
                    row for row in pon_users if row.get("onu_ifindex") is None
                ],
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/onus", response_class=HTMLResponse)
    def page_onus(
        request: Request,
        olt: str | None = None,
        pon: str | None = None,
        status: str | None = None,
        q: str | None = None,
        power: str | None = None,
        sort: str = "if_name",
        dir: str = "asc",
        page: int = 1,
    ) -> HTMLResponse:
        # The filter form always submits every field, so an untouched <select>
        # arrives as an empty string - parse it tolerantly instead of returning 422.
        pon_number = _as_int(pon)
        power_on = _as_int(power, 0) == 1
        weak_dbm = float(dig(cfg, "alerts.onu_rx_power_low_dbm", -27.0))
        per_page = 200
        summary = onu_summary(
            db_path, olt=olt or None, pon=pon_number, status=status or None,
            search=q or None, only_power=power_on, weak_dbm=weak_dbm,
        )
        pages = max(1, (summary["total"] + per_page - 1) // per_page)
        page = min(max(1, int(page or 1)), pages)
        rows = list_onus(
            db_path, olt=olt or None, pon=pon_number, status=status or None,
            search=q or None, only_power=power_on, sort=sort, direction=dir,
            weak_dbm=weak_dbm, limit=per_page, offset=(page - 1) * per_page,
        )
        summary["pon_level"] = 0
        summary["first_row"] = (page - 1) * per_page + 1 if rows else 0
        summary["last_row"] = (page - 1) * per_page + len(rows)
        summary["page"] = page
        summary["pages"] = pages
        summary["per_page"] = per_page
        filters = {
            key: value for key, value in request.query_params.items()
            if key not in ("page", "sort", "dir") and value
        }
        # Subscribers the OLT only associates with a PON.  Counted separately
        # (not summed over the ONU rows, which would multiply them by the number
        # of ONUs sharing that PON).
        where = ["onu_ifindex IS NULL", "onu_pon IS NOT NULL"]
        params: list[Any] = []
        if olt:
            where.append("onu_olt_name = ?")
            params.append(olt)
        if pon_number is not None:
            where.append("onu_pon = ?")
            params.append(pon_number)
        summary["pon_level"] = int(
            query(
                db_path,
                "SELECT COUNT(*) AS n FROM pppoe_user_current WHERE "
                + " AND ".join(where),
                params,
            )[0]["n"]
            or 0
        )
        return render(
            request,
            "onus.html",
            {
                "active": "onus",
                "rows": rows,
                "summary": summary,
                "total": summary["total"],
                "page": page,
                "pages": pages,
                "first_row": summary["first_row"],
                "last_row": summary["last_row"],
                "olt": olt or "",
                "pon": pon_number,
                "pon_numbers": sorted(
                    {
                        int(r["pon"])
                        for r in query(
                            db_path,
                            "SELECT DISTINCT pon FROM olt_onu_current WHERE pon IS NOT NULL",
                        )
                    }
                ) or list(range(0, 17)),
                "status": status or "",
                "q": q or "",
                "power": 1 if power_on else 0,
                "sort": sort,
                "dir": dir,
                "weak_dbm": weak_dbm,
                "filter_qs": urlencode(filters),
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/users", response_class=HTMLResponse)
    def page_users(
        request: Request,
        router: str | None = None,
        q: str | None = None,
        matched: str | None = None,
        optical: str | None = None,
        sort: str = "name",
        dir: str = "asc",
        page: int = 1,
    ) -> HTMLResponse:
        per_page = 200
        total = count_users(
            db_path, router=router, search=q, matched=matched, optical=optical
        )
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, int(page or 1)), pages)
        rows = list_users(
            db_path, router=router, search=q, matched=matched, optical=optical,
            sort=sort, direction=dir, limit=per_page, offset=(page - 1) * per_page,
        )
        matched_count = sum(
            1 for row in rows
            if row.get("onu_ifindex") is not None or row.get("onu_pon") is not None
        )
        power_count = sum(1 for row in rows if row.get("rx_power_dbm") is not None)
        filters = {
            key: value for key, value in request.query_params.items()
            if key not in ("page", "sort", "dir") and value
        }
        return render(
            request,
            "users.html",
            {
                "active": "users",
                "rows": rows,
                "router": router or "",
                "q": q or "",
                "matched": matched or "",
                "optical": optical or "",
                "sort": sort,
                "dir": dir,
                "matched_count": matched_count,
                "power_count": power_count,
                "total": total,
                "page": page,
                "pages": pages,
                "per_page": per_page,
                "first_row": (page - 1) * per_page + 1 if rows else 0,
                "last_row": (page - 1) * per_page + len(rows),
                "filter_qs": urlencode(filters),
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/user", response_class=HTMLResponse)
    def page_user(request: Request, router: str, name: str) -> HTMLResponse:
        user = get_user(db_path, router, name)
        if user is None:
            raise HTTPException(status_code=404, detail=f"no user {name!r} on {router}")
        onu = None
        pon_info = None
        if user.get("onu_olt_name") and user.get("onu_ifindex") is not None:
            onu = get_onu(db_path, str(user["onu_olt_name"]), int(user["onu_ifindex"]))
        if user.get("onu_olt_name") and user.get("onu_pon") is not None:
            pon_info = find_pon(db_path, cfg, str(user["onu_olt_name"]), int(user["onu_pon"]))
        return render(
            request,
            "user_detail.html",
            {
                "active": "users",
                "router": router,
                "user": user,
                "onu": onu,
                "pon": pon_info,
                "caller_vendor": oui_mod.vendor_for(user.get("caller_id")),
                "onu_vendor": oui_mod.vendor_for(
                    (onu or {}).get("mac"),
                    None,
                    (onu or {}).get("vendor"),
                    (onu or {}).get("model"),
                ),
                "optical_configured": _optical_configured(db_path, cfg),
            },
        )

    @app.get("/ports", response_class=HTMLResponse)
    def page_ports(request: Request, olt: str | None = None) -> HTMLResponse:
        data = dashboard_data(db_path, cfg)
        return render(
            request,
            "ports.html",
            {
                "active": "ports",
                "olt": olt or "",
                "ports": list_ports(db_path, olt=olt),
                "router_ifaces": list_router_ifaces(db_path),
                "data": data,
            },
        )

    @app.get("/routers", response_class=HTMLResponse)
    def page_routers(request: Request) -> HTMLResponse:
        overview = device_overview(db_path, cfg)
        return render(
            request,
            "routers.html",
            {
                "active": "routers",
                "routers": overview["routers"],
                "ifaces": list_router_ifaces(db_path),
                "status": getattr(monitor, "status", {}) if monitor else {},
            },
        )

    @app.get("/router", response_class=HTMLResponse)
    def page_router(request: Request, name: str) -> HTMLResponse:
        info = device_payload(db_path, "router", name)
        if not info:
            raise HTTPException(status_code=404, detail=f"no router data for {name!r} yet")
        return render(
            request,
            "router_detail.html",
            {
                "active": "routers",
                "name": name,
                "info": info,
                "ifaces": list_router_ifaces(db_path, router=name),
                "users": list_users(db_path, router=name, limit=20000),
                "status": (getattr(monitor, "status", {}) or {}).get("routers", {}).get(name, {}),
            },
        )

    @app.get("/alerts", response_class=HTMLResponse)
    def page_alerts(request: Request) -> HTMLResponse:
        return render(request, "alerts.html", {"active": "alerts", "rows": list_alerts(db_path)})

    # ---- super-admin: settings ----------------------------------------
    def _settings_context(request: Request, msg: str = "", err: str = "") -> dict[str, Any]:
        users = auth.list_users(db_path)
        return {
            "active": "settings",
            "site": site_settings.get_all(db_path),
            "target": site_settings.current_db_target(cfg),
            "env_file": str(site_settings.env_path()),
            "web_port": dig(cfg, "app.port", 5002),
            "backups": sqladmin.list_backups(10),
            "free_mb": sqladmin.free_space_mb(),
            "users_count": len(users),
            "audit": auth.recent_audit(db_path, 15),
            "msg": msg,
            "err": err,
            "optical_configured": _optical_configured(db_path, cfg),
            "db_path": db_label,
            "status": getattr(monitor, "status", {}) if monitor else {},
        }

    @app.get("/settings", response_class=HTMLResponse)
    def page_settings(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        return render(request, "settings.html", _settings_context(request, msg, err))

    @app.post("/settings/site")
    def save_site_settings(
        request: Request,
        site_name: str = Form(""),
        tagline: str = Form(""),
        about_text: str = Form(""),
        footer_text: str = Form(""),
        support_email: str = Form(""),
        refresh_seconds: int = Form(30),
    ) -> Response:
        user = request.state.user
        try:
            if not site_name.strip():
                raise ValueError("site name cannot be empty")
            if not (5 <= int(refresh_seconds) <= 3600):
                raise ValueError("refresh interval must be between 5 and 3600 seconds")
            site_settings.set_many(
                db_path,
                {
                    "site_name": site_name.strip(),
                    "tagline": tagline.strip(),
                    "about_text": about_text.strip(),
                    "footer_text": footer_text.strip(),
                    "support_email": support_email.strip(),
                    "refresh_seconds": str(refresh_seconds),
                },
            )
            auth.audit(db_path, user["username"], "settings.site", "site settings updated", _client_ip(request))
        except ValueError as exc:
            return RedirectResponse(f"/settings?err={quote(str(exc))}", status_code=303)
        return RedirectResponse("/settings?msg=Site+settings+saved", status_code=303)

    @app.post("/settings/port")
    def save_port(request: Request, web_port: int = Form(5002)) -> Response:
        user = request.state.user
        try:
            site_settings.save_web_port(web_port)
            site_settings.set_many(db_path, {"web_port": str(web_port)})
            auth.audit(db_path, user["username"], "settings.port", f"APP_PORT={web_port}", _client_ip(request))
        except ValueError as exc:
            return RedirectResponse(f"/settings?err={quote(str(exc))}", status_code=303)
        return RedirectResponse(
            "/settings?msg=Port+saved+-+restart+the+service+to+apply+it", status_code=303
        )

    @app.post("/api/admin/db/test")
    def api_admin_db_test(payload: dict = Body(...)) -> dict[str, Any]:
        current = dict(cfg.get("database") or {})
        password = str(payload.get("password") or "") or str(current.get("password") or "")
        try:
            info = site_settings.test_mysql(
                str(payload.get("host") or ""),
                int(payload.get("port") or 3306),
                str(payload.get("name") or ""),
                str(payload.get("user") or ""),
                password,
            )
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **info}

    @app.post("/settings/database")
    def save_database(
        request: Request,
        db_host: str = Form(""),
        db_port: int = Form(3306),
        db_name: str = Form(""),
        db_user: str = Form(""),
        db_password: str = Form(""),
    ) -> Response:
        user = request.state.user
        current = dict(cfg.get("database") or {})
        password = db_password or str(current.get("password") or "")
        try:
            site_settings.save_db_target(
                {"host": db_host, "port": db_port, "name": db_name, "user": db_user},
                password,
            )
            auth.audit(
                db_path,
                user["username"],
                "settings.database",
                f"target {db_host}:{db_port}/{db_name} (restart required)",
                _client_ip(request),
            )
        except ValueError as exc:
            return RedirectResponse(f"/settings?err={quote(str(exc))}", status_code=303)
        return RedirectResponse(
            "/settings?msg=Database+target+saved+-+restart+the+service+to+apply+it",
            status_code=303,
        )

    @app.get("/settings/export-config")
    def export_site_config(request: Request) -> Response:
        """Download the full site configuration (no collected measurements)."""
        user = request.state.user
        payload = siteconfig.export_config(db_path)
        body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        detail = (
            f"{len(payload['devices'])} devices, {len(payload['users'])} users, "
            f"{len(payload['site_settings'])} settings"
        )
        auth.audit(
            db_path, user["username"], "config.export", detail, _client_ip(request)
        )
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{siteconfig.suggested_filename()}"'
                ),
                "Cache-Control": "no-store",
            },
        )

    @app.post("/settings/import-config")
    def import_site_config(
        request: Request,
        file: UploadFile = File(...),
        confirm: str = Form(""),
    ) -> Response:
        """Restore a config-only export and let polling refill the data."""
        user = request.state.user
        if confirm.strip().upper() != "IMPORT":
            return RedirectResponse("/settings?err=Type+IMPORT+to+confirm", status_code=303)
        try:
            raw = file.file.read(siteconfig.MAX_UPLOAD_BYTES + 1)
            payload = siteconfig.load_upload(raw)
            info = siteconfig.import_config(db_path, payload)
        except ValueError as exc:
            auth.audit(
                db_path,
                user["username"],
                "config.import-failed",
                f"{file.filename}: {str(exc)[:300]}",
                _client_ip(request),
            )
            return RedirectResponse(f"/settings?err={quote(str(exc))}", status_code=303)

        reload_monitor()
        auth.audit(
            db_path,
            user["username"],
            "config.import",
            f"{file.filename}: {info['devices']} devices, {info['users']} users, "
            f"{info['tables_cleared']} data tables cleared",
            _client_ip(request),
        )

        # Users were replaced, so every old cookie is invalid.  Re-issue a
        # session for the admin who performed the import (when they survived
        # in the imported user list).
        actor = auth.get_user_by_username(db_path, user["username"])
        if not actor or not actor.get("active"):
            return RedirectResponse("/login", status_code=303)

        token = auth.create_session(db_path, int(actor["id"]))
        target = (
            f"/settings?msg={quote('Site configuration imported - polling will refill the data')}"
            if auth.has_permission(actor, "perm_settings")
            else "/"
        )
        response = RedirectResponse(target, status_code=303)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(
            auth.SESSION_COOKIE,
            token,
            max_age=auth.SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=secure,
        )
        return response

    @app.get("/settings/export")
    def export_sql(request: Request, history: int = 0) -> FileResponse:
        user = request.state.user
        info = sqladmin.make_backup(include_history=bool(_as_int(history, 0)), compress=True)
        path = Path(info["path"])
        auth.audit(
            db_path,
            user["username"],
            "sql.export",
            f"{info['filename']} ({info['bytes']} bytes)",
            _client_ip(request),
        )
        return FileResponse(path, filename=path.name, media_type="application/zip")

    @app.get("/settings/backups/{name}")
    def download_backup(request: Request, name: str) -> FileResponse:
        safe = Path(name).name
        path = sqladmin.BACKUP_DIR / safe
        if not path.is_file():
            raise HTTPException(status_code=404, detail="backup not found")
        auth.audit(db_path, request.state.user["username"], "sql.download", safe, _client_ip(request))
        return FileResponse(path, filename=safe, media_type="application/zip")

    @app.post("/settings/import")
    def import_sql(
        request: Request,
        file: UploadFile = File(...),
        confirm: str = Form(""),
    ) -> Response:
        user = request.state.user
        if confirm.strip().upper() != "IMPORT":
            return RedirectResponse("/settings?err=Type+IMPORT+to+confirm", status_code=303)
        import os

        suffix = Path(file.filename or "upload.sql").suffix or ".sql"
        fd, tmp_name = tempfile.mkstemp(prefix="olt-import-", suffix=suffix)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with tmp_path.open("wb") as handle:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            info = sqladmin.import_sql(tmp_path)
            site_settings.invalidate()
            registry.invalidate_cache()
            reload_monitor()
            auth.audit(
                db_path,
                user["username"],
                "sql.import",
                f"{file.filename}: {info['statements']} statements, {info['rows']} rows",
                _client_ip(request),
            )
            message = f"Import finished: {info['statements']} statements in {info['seconds']}s"
            return RedirectResponse(f"/settings?msg={quote(message)}", status_code=303)
        except ValueError as exc:
            auth.audit(db_path, user["username"], "sql.import-failed", str(exc)[:300], _client_ip(request))
            return RedirectResponse(f"/settings?err={quote(str(exc))}", status_code=303)
        finally:
            tmp_path.unlink(missing_ok=True)

    # ---- super-admin: web users ---------------------------------------
    @app.get("/admin/users", response_class=HTMLResponse)
    def page_users(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        return render(
            request,
            "admin_users.html",
            {
                "active": "admin-users",
                "users": auth.list_users(db_path),
                "roles": [
                    {"key": r, "label": auth.ROLE_LABELS[r], "hint": auth.ROLE_HINTS[r]}
                    for r in auth.ROLES
                ],
                "permissions": auth.PERMISSIONS,
                "permission_keys": auth.PERMISSION_KEYS,
                "msg": msg,
                "err": err,
            },
        )

    @app.post("/admin/users/create")
    def create_web_user(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        password2: str = Form(""),
        role: str = Form("viewer"),
        display_name: str = Form(""),
        email: str = Form(""),
        permissions_set: str = Form(""),
        perm_devices: str = Form(""),
        perm_tools: str = Form(""),
        perm_settings: str = Form(""),
        perm_users: str = Form(""),
        perm_developer: str = Form(""),
        perm_sql: str = Form(""),
    ) -> Response:
        actor = request.state.user
        flags = None
        if permissions_set:
            flags = {
                "perm_devices": bool(perm_devices),
                "perm_tools": bool(perm_tools),
                "perm_settings": bool(perm_settings),
                "perm_users": bool(perm_users),
                "perm_developer": bool(perm_developer),
                "perm_sql": bool(perm_sql),
            }
        try:
            if password != password2:
                raise ValueError("the two passwords do not match")
            user = auth.create_user(
                db_path,
                username,
                password,
                role,
                display_name=display_name,
                email=email,
                permissions=flags,
            )
            auth.audit(
                db_path,
                actor["username"],
                "user.create",
                f"{user['username']} as {user['role']}",
                _client_ip(request),
            )
        except ValueError as exc:
            return RedirectResponse(f"/admin/users?err={quote(str(exc))}", status_code=303)
        return RedirectResponse("/admin/users?msg=User+created", status_code=303)

    @app.post("/admin/users/{user_id}/permissions")
    def change_user_permissions(
        request: Request,
        user_id: int,
        perm_devices: str = Form(""),
        perm_tools: str = Form(""),
        perm_settings: str = Form(""),
        perm_users: str = Form(""),
        perm_developer: str = Form(""),
        perm_sql: str = Form(""),
    ) -> Response:
        actor = request.state.user
        target = auth.get_user(db_path, user_id)
        if not target:
            return RedirectResponse("/admin/users?err=User+not+found", status_code=303)
        flags = {
            "perm_devices": bool(perm_devices),
            "perm_tools": bool(perm_tools),
            "perm_settings": bool(perm_settings),
            "perm_users": bool(perm_users),
            "perm_developer": bool(perm_developer),
            "perm_sql": bool(perm_sql),
        }
        # A super admin is unrestricted by design, so the flags would be
        # misleading on that account; keep the role itself as the control.
        if target.get("role") == "superadmin":
            return RedirectResponse(
                "/admin/users?err=Super+admin+already+has+every+permission",
                status_code=303,
            )
        auth.update_user(db_path, user_id, **flags)
        auth.audit(
            db_path,
            actor["username"],
            "user.permissions",
            f"{target['username']}: "
            + ", ".join(key for key, value in flags.items() if value),
            _client_ip(request),
        )
        return RedirectResponse("/admin/users?msg=Permissions+updated", status_code=303)

    @app.post("/admin/users/{user_id}/role")
    def change_user_role(request: Request, user_id: int, role: str = Form("viewer")) -> Response:
        actor = request.state.user
        try:
            target = auth.get_user(db_path, user_id)
            if not target:
                raise ValueError("user not found")
            if target["id"] == actor["id"] and role != "superadmin":
                raise ValueError("you cannot remove your own super-admin role")
            auth.update_user(db_path, user_id, role=role)
            auth.audit(
                db_path,
                actor["username"],
                "user.role",
                f"{target['username']} -> {role}",
                _client_ip(request),
            )
        except ValueError as exc:
            return RedirectResponse(f"/admin/users?err={quote(str(exc))}", status_code=303)
        return RedirectResponse("/admin/users?msg=Role+updated", status_code=303)

    @app.post("/admin/users/{user_id}/password")
    def reset_user_password(
        request: Request,
        user_id: int,
        password: str = Form(""),
        password2: str = Form(""),
    ) -> Response:
        actor = request.state.user
        try:
            auth.validate_password(password, password2)
            target = auth.get_user(db_path, user_id)
            if not target:
                raise ValueError("user not found")
            auth.set_password(db_path, user_id, password)
            auth.audit(
                db_path,
                actor["username"],
                "user.password",
                f"password reset for {target['username']}",
                _client_ip(request),
            )
        except ValueError as exc:
            return RedirectResponse(f"/admin/users?err={quote(str(exc))}", status_code=303)
        return RedirectResponse("/admin/users?msg=Password+reset", status_code=303)

    @app.post("/admin/users/{user_id}/toggle")
    def toggle_user(request: Request, user_id: int) -> Response:
        actor = request.state.user
        target = auth.get_user(db_path, user_id)
        if not target:
            return RedirectResponse("/admin/users?err=User+not+found", status_code=303)
        if target["id"] == actor["id"]:
            return RedirectResponse("/admin/users?err=You+cannot+disable+yourself", status_code=303)
        auth.update_user(db_path, user_id, active=not target["active"])
        auth.audit(
            db_path,
            actor["username"],
            "user.toggle",
            f"{target['username']} -> {'active' if not target['active'] else 'disabled'}",
            _client_ip(request),
        )
        return RedirectResponse("/admin/users?msg=User+updated", status_code=303)

    @app.post("/admin/users/{user_id}/delete")
    def remove_user(request: Request, user_id: int) -> Response:
        actor = request.state.user
        target = auth.get_user(db_path, user_id)
        if not target:
            return RedirectResponse("/admin/users?err=User+not+found", status_code=303)
        if target["id"] == actor["id"]:
            return RedirectResponse("/admin/users?err=You+cannot+delete+yourself", status_code=303)
        auth.delete_user(db_path, user_id)
        auth.audit(
            db_path,
            actor["username"],
            "user.delete",
            target["username"],
            _client_ip(request),
        )
        return RedirectResponse("/admin/users?msg=User+deleted", status_code=303)

    # ---- super-admin: developer ---------------------------------------
    def _file_allowed(relative: str) -> bool:
        return bundle_mod._wanted(relative) and not relative.startswith(".")

    @app.get("/developer", response_class=HTMLResponse)
    def page_developer(request: Request) -> HTMLResponse:
        files = bundle_mod.collect()
        total = sum(path.stat().st_size for path, _ in files)
        return render(
            request,
            "developer.html",
            {
                "active": "developer",
                "file_count": len(files),
                "source_bytes": total,
                "audit": auth.recent_audit(db_path, 60),
                "python_version": __import__("sys").version.split()[0],
                "project_root": str(PROJECT_ROOT),
                "env_file": str(site_settings.env_path()),
                "backups": sqladmin.list_backups(8),
                "free_mb": sqladmin.free_space_mb(),
                "target": site_settings.current_db_target(cfg),
                "user_count": auth.count_users(db_path),
            },
        )

    @app.get("/developer/bundle.zip")
    def download_bundle(request: Request) -> FileResponse:
        tmp_dir = PROJECT_ROOT / "backups" / "tmp"
        dest = bundle_mod.temp_bundle(tmp_dir)
        info = bundle_mod.build_source_zip(dest)
        auth.audit(
            db_path,
            request.state.user["username"],
            "developer.bundle",
            f"{dest.name} ({info['files']} files, {info['bytes']} bytes)",
            _client_ip(request),
        )
        return FileResponse(
            dest,
            filename=dest.name,
            media_type="application/zip",
            background=BackgroundTask(lambda: dest.unlink(missing_ok=True)),
        )

    @app.get("/developer/files", response_class=HTMLResponse)
    def page_developer_files(request: Request, q: str = "") -> HTMLResponse:
        needle = q.strip().lower()
        files = []
        for path, relative in bundle_mod.collect():
            if needle and needle not in relative.lower():
                continue
            files.append({"path": relative, "size": path.stat().st_size})
        files.sort(key=lambda row: row["path"])
        return render(
            request,
            "developer_files.html",
            {"active": "developer", "files": files, "q": q, "count": len(files)},
        )

    @app.get("/developer/file")
    def view_developer_file(request: Request, path: str = "", download: int = 0) -> Response:
        relative = (path or "").replace("\\", "/").lstrip("/")
        if not relative or ".." in Path(relative).parts or not _file_allowed(relative):
            raise HTTPException(status_code=403, detail="this file is not readable from the web")
        target = (PROJECT_ROOT / relative).resolve()
        root = PROJECT_ROOT.resolve()
        if root not in target.parents:
            raise HTTPException(status_code=403, detail="outside the project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        if download:
            return FileResponse(target, filename=target.name)
        raw = target.read_bytes()
        if b"\x00" in raw or len(raw) > 512 * 1024:
            return FileResponse(target, filename=target.name)
        return render(
            request,
            "developer_file.html",
            {
                "active": "developer",
                "file_path": relative,
                "content": raw.decode("utf-8", errors="replace"),
                "size": len(raw),
            },
        )

    # ---- JSON API ------------------------------------------------------
    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        olts, routers = _devices(db_path, cfg)
        return {
            "ok": True,
            "version": __version__,
            "database": describe_db(),
            "devices": {
                "olts": [d["name"] for d in olts],
                "routers": [d["name"] for d in routers],
            },
            "status": getattr(monitor, "status", {}) if monitor else {},
        }

    @app.get("/api/summary")
    def api_summary() -> Any:
        return _jsonable(dashboard_data(db_path, cfg))

    @app.get("/api/devices")
    def api_devices() -> Any:
        return _jsonable(device_overview(db_path, cfg))

    # -- device registry -------------------------------------------------
    @app.get("/api/registry")
    def api_registry() -> Any:
        return _jsonable(registry.public_devices(db_path, cfg))

    @app.post("/api/registry/{kind}")
    def api_registry_create(kind: str, payload: dict[str, Any] = Body(...)) -> Any:
        try:
            device = registry.create_device(db_path, cfg, kind, payload)
        except registry.RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        reload_monitor()
        return {"ok": True, "device": _jsonable(registry.form_view(device))}

    @app.put("/api/registry/{device_id}")
    def api_registry_update(
        device_id: int, payload: dict[str, Any] = Body(...)
    ) -> Any:
        try:
            device = registry.update_device(db_path, cfg, device_id, payload)
        except registry.RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        reload_monitor()
        return {"ok": True, "device": _jsonable(registry.form_view(device))}

    @app.delete("/api/registry/{device_id}")
    def api_registry_delete(device_id: int) -> Any:
        try:
            removed = registry.delete_device(db_path, cfg, device_id)
        except registry.RegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        reload_monitor()
        return {"ok": True, "removed": removed}

    @app.post("/api/registry/{device_id}/toggle")
    def api_registry_toggle(device_id: int, payload: dict[str, Any] = Body(default={})) -> Any:
        try:
            device = registry.get_device(db_path, device_id)
            enabled = payload.get("enabled")
            if enabled is None:
                enabled = not bool(device.get("enabled"))
            device = registry.set_enabled(db_path, cfg, device_id, bool(enabled))
        except registry.RegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        reload_monitor()
        return {"ok": True, "device": _jsonable(registry.form_view(device))}

    @app.post("/api/registry/{device_id}/test")
    def api_registry_test(device_id: int) -> Any:
        try:
            device = registry.get_device(db_path, device_id)
        except registry.RegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        kind = device.get("kind")
        if kind == "olt":
            from ..collectors import OltCollector

            collector = OltCollector(cfg, device)
            try:
                ok, message, details = collector.probe()
            finally:
                collector.close()

            # A working OLT whose SNMP is blocked (or simply disabled) still
            # returns its data through the CLI / HTTP API.  Do not report the
            # device as broken when one of those transports answers.
            if not ok and device.get("telnet_enabled"):
                from .. import telnet as telnet_mod

                telnet_ok, telnet_message, telnet_details = telnet_mod.probe(device)
                if telnet_ok:
                    merged = dict(telnet_details or {})
                    merged["snmp_error"] = message
                    details = merged
                    ok = True
                    message = f"SNMP failed ({message}); Telnet OK: {telnet_message}"
            if not ok and device.get("cdata_api_enabled"):
                from .. import cdata as cdata_mod

                api_ok, api_message, api_details = cdata_mod.probe(device)
                if api_ok:
                    merged = dict(api_details or {})
                    merged["snmp_error"] = message
                    details = merged
                    ok = True
                    message = f"SNMP failed ({message}); OLT API OK: {api_message}"
        else:
            from ..collectors import RouterCollector

            collector = RouterCollector(cfg, device)
            try:
                ok, message, details = collector.ping()
            finally:
                collector.close()
        return {"ok": ok, "message": message, "details": _jsonable(details)}

    # -- collected data --------------------------------------------------
    @app.get("/api/olts")
    def api_olts() -> Any:
        return _jsonable(olt_overview(db_path, cfg))

    @app.post("/api/olts/{name}/poll")
    def api_olt_poll(name: str) -> Any:
        if monitor is None:
            raise HTTPException(
                status_code=503,
                detail="the poller is not running in this process - start the app with run.py",
            )
        outcome = monitor.poll_now("olt", name)
        if outcome == "missing":
            raise HTTPException(status_code=404, detail=f"no enabled OLT named {name!r}")
        message = (
            f"{name}: already being polled"
            if outcome == "running"
            else f"{name}: poll started in the background"
        )
        return {"ok": True, "status": outcome, "message": message}

    @app.post("/api/poll-all")
    def api_poll_all(kind: str = "all") -> Any:
        """Queue an immediate full refresh of every enabled device.

        The scheduled cycle already runs every 10 minutes; this button is for
        "I need fresh data right now" and never starts a second concurrent walk
        of a device that is still polling.
        """
        if monitor is None:
            raise HTTPException(
                status_code=503,
                detail="the poller is not running in this process - start the app with run.py",
            )
        if kind not in ("all", "olt", "router"):
            raise HTTPException(status_code=400, detail="kind must be all, olt or router")
        result = monitor.poll_all(kind)
        started = len(result.get("started") or [])
        running = len(result.get("running") or [])
        if started:
            message = f"{started} device poll(s) started"
            if running:
                message += f", {running} already running"
        elif running:
            message = f"{running} device poll(s) are already running"
        else:
            message = "no enabled device to poll"
        return {"ok": True, "message": message, "result": result, "kind": kind}

    @app.get("/api/poll-status")
    def api_poll_status() -> Any:
        if monitor is None:
            return {"active": [], "count": 0, "running": False}
        state = monitor.polling_status()
        state["running"] = bool(state.get("count"))
        return state

    @app.post("/api/registry/{device_id}/test-telnet")
    def api_registry_test_telnet(device_id: int) -> Any:
        try:
            device = registry.get_device(db_path, device_id)
        except registry.RegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if device.get("kind") != "olt":
            raise HTTPException(status_code=400, detail="Telnet is only supported for OLTs")
        from .. import telnet as telnet_mod

        ok, message, details = telnet_mod.probe(device)
        return {"ok": ok, "message": message, "details": _jsonable(details)}

    @app.post("/api/registry/{device_id}/test-api")
    def api_registry_test_api(device_id: int) -> Any:
        try:
            device = registry.get_device(db_path, device_id)
        except registry.RegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if device.get("kind") != "olt":
            raise HTTPException(status_code=400, detail="the HTTP API is only used for OLTs")
        from .. import cdata as cdata_mod

        ok, message, details = cdata_mod.probe(device)
        return {"ok": ok, "message": message, "details": _jsonable(details)}

    @app.get("/api/health/{olt}")
    def api_olt_health(olt: str, hours: str | None = None) -> Any:
        window = _limit(hours, 24, 720)
        return _jsonable(
            query(
                db_path,
                "SELECT ts, cpu_percent, memory_percent, temperature_c, uptime_seconds,"
                " snmp_rtt_ms FROM olt_health_history WHERE olt_name = ?"
                " ORDER BY ts DESC LIMIT ?",
                [olt, window * 60],
            )
        )

    @app.get("/api/onus")
    def api_onus(
        olt: str | None = None,
        pon: str | None = None,
        status: str | None = None,
        q: str | None = None,
        power: str | None = None,
        sort: str = "if_name",
        dir: str = "asc",
        limit: str | None = None,
    ) -> Any:
        return _jsonable(list_onus(
            db_path, olt=olt, pon=_as_int(pon), status=status, search=q,
            only_power=_as_int(power, 0) == 1, sort=sort, direction=dir,
            limit=_limit(limit, 2000, 20000),
            weak_dbm=float(dig(cfg, "alerts.onu_rx_power_low_dbm", -27.0)),
        ))

    @app.get("/api/onus/{olt}/{ifindex}")
    def api_onu(olt: str, ifindex: int) -> Any:
        onu = get_onu(db_path, olt, ifindex)
        if onu is None:
            raise HTTPException(status_code=404, detail="ONU not found")
        return _jsonable({"onu": onu, "users": users_on_onu(db_path, olt, ifindex)})

    @app.get("/api/pons")
    def api_pons(olt: str | None = None) -> Any:
        return _jsonable(pon_rows(db_path, cfg, olt=olt))

    @app.get("/api/users")
    def api_users(
        router: str | None = None,
        q: str | None = None,
        matched: str | None = None,
        optical: str | None = None,
        sort: str = "name",
        dir: str = "asc",
        limit: str | None = None,
    ) -> Any:
        return _jsonable(list_users(db_path, router=router, search=q, matched=matched,
                                    optical=optical, sort=sort, direction=dir,
                                    limit=_limit(limit, 5000, 50000)))

    @app.get("/api/user/{router}/{name}")
    def api_user(router: str, name: str) -> Any:
        user = get_user(db_path, router, name)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        return _jsonable(user)

    @app.get("/api/ports")
    def api_ports(kind: str | None = None, olt: str | None = None) -> Any:
        return _jsonable(list_ports(db_path, kind=kind, olt=olt))

    @app.get("/api/router-interfaces")
    def api_router_interfaces(router: str | None = None) -> Any:
        return _jsonable(list_router_ifaces(db_path, router=router))

    @app.get("/api/alerts")
    def api_alerts() -> Any:
        return _jsonable(list_alerts(db_path))

    @app.get("/api/onu-power")
    def api_onu_power(olt: str | None = None) -> Any:
        """ONU rx power values, for the distribution chart."""
        sql = "SELECT olt_name, if_name, rx_power_dbm FROM olt_onu_current WHERE rx_power_dbm IS NOT NULL"
        params: list[Any] = []
        sql = _device_filter(sql, params, "olt_name", olt)
        return _jsonable(query(db_path, sql + " ORDER BY olt_name, if_name", params))

    @app.get("/api/trend/onu/{ifindex}")
    def api_trend_onu(ifindex: int, olt: str | None = None, hours: str | None = None) -> Any:
        window = _limit(hours, 24, 720)
        sql = ("SELECT ts, olt_name, rx_power_dbm, tx_power_dbm, olt_rx_power_dbm, oper_status "
               "FROM olt_onu_history WHERE ifindex=?")
        params: list[Any] = [ifindex]
        sql = _device_filter(sql, params, "olt_name", olt)
        params.append(window * 12)
        return _jsonable(query(db_path, sql + " ORDER BY ts DESC LIMIT ?", params))

    @app.get("/api/trend/port/{ifindex}")
    def api_trend_port(ifindex: int, olt: str | None = None, hours: str | None = None) -> Any:
        window = _limit(hours, 24, 720)
        sql = "SELECT ts, olt_name, in_bps, out_bps, oper_status FROM port_history WHERE ifindex=?"
        params: list[Any] = [ifindex]
        sql = _device_filter(sql, params, "olt_name", olt)
        params.append(window * 60)
        return _jsonable(query(db_path, sql + " ORDER BY ts DESC LIMIT ?", params))

    @app.get("/api/trend/pppoe")
    def api_trend_pppoe(router: str | None = None, hours: str | None = None) -> Any:
        window = _limit(hours, 24, 720)
        sql = "SELECT ts, router_name, pppoe_online, cpu_load, free_memory_percent FROM router_history WHERE 1=1"
        params: list[Any] = []
        sql = _device_filter(sql, params, "router_name", router)
        params.append(window * 120)
        return _jsonable(query(db_path, sql + " ORDER BY ts DESC LIMIT ?", params))

    @app.get("/api/snmp")
    def api_snmp(oid: str, walk: str | None = None, olt: str | None = None) -> Any:
        """Live SNMP query against a configured OLT - very handy for MIB hunting."""
        do_walk = _as_int(walk, 0) == 1
        olts = {device["name"]: device for device in _devices(db_path, cfg)[0]}
        device = olts.get(olt) if olt else next(iter(olts.values()), None)
        if not device:
            raise HTTPException(status_code=400, detail="no OLT configured")
        if not device.get("host"):
            raise HTTPException(status_code=400, detail="OLT host is not configured")
        client = SnmpClient(
            host=str(device.get("host")),
            community=str(device.get("community") or "public"),
            version=device.get("snmp_version", "2c"),
            port=int(device.get("snmp_port", 161)),
            timeout=float(device.get("timeout_seconds", 4)),
            retries=int(device.get("retries", 2)),
        )
        try:
            if do_walk:
                binds = client.walk(oid, max_rows=2000)
            else:
                binds = client.get([oid])
        except SnmpError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        finally:
            client.close()
        return _jsonable(
            [
                {
                    "oid": bind.oid,
                    "tag": f"0x{bind.tag:02X}",
                    "value": bind.value,
                    "exception": bind.is_exception,
                }
                for bind in binds
            ]
        )

    @app.get("/tools", response_class=HTMLResponse)
    def page_tools(request: Request, oid: str = "", walk: str | None = None, olt: str | None = None) -> HTMLResponse:
        walk_on = _as_int(walk, 0) == 1
        result: list[dict[str, Any]] = []
        error: str | None = None
        if oid:
            try:
                result = api_snmp(oid, "1" if walk_on else "0", olt)
            except HTTPException as exc:
                error = str(exc.detail)
        return render(
            request,
            "tools.html",
            {"active": "tools", "oid": oid, "walk": 1 if walk_on else 0, "olt": olt or "",
             "result": result, "error": error,
             "optical_configured": _optical_configured(db_path, cfg)},
        )

    return app
