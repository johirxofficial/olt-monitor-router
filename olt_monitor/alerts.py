"""Alert evaluation.

Runs after every poll cycle, reads the ``*_current`` tables and upserts one row
per ongoing problem into ``alerts_current``.  Problems that disappear are
marked ``active=0`` instead of being deleted, so recent history stays visible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import execute, query, upsert_many, utcnow_iso
from .logging_setup import get_logger

log = get_logger("alerts")

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"


def _thresholds(cfg: dict) -> dict[str, Any]:
    alerts = dict(cfg.get("alerts") or {})
    return {
        "enabled": bool(alerts.get("enabled", True)),
        "rx_low": float(alerts.get("onu_rx_power_low_dbm", -27.0)),
        "rx_critical": float(alerts.get("onu_rx_power_critical_dbm", -30.0)),
        "rx_high": float(alerts.get("onu_rx_power_high_dbm", -3.0)),
        "traffic_high_mbps": float(alerts.get("port_traffic_high_mbps", 900.0)),
        "cpu_high": float(alerts.get("router_cpu_high_percent", 85.0)),
        "memory_low": float(alerts.get("router_free_memory_low_percent", 10.0)),
        "uplink_down": bool(alerts.get("alert_uplink_down", False)),
        "onu_down": bool(alerts.get("alert_onu_down", True)),
        "subscriber_onu_down": bool(alerts.get("alert_subscriber_onu_down", True)),
        "device_down": bool(alerts.get("alert_device_down", True)),
    }


def evaluate(cfg: dict, db_path: str | Path, status: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Rebuild the active alert set; returns the list of active alerts."""
    limits = _thresholds(cfg)
    if not limits["enabled"]:
        return []

    stamp = utcnow_iso()
    found: list[dict[str, Any]] = []

    def add(
        severity: str,
        scope: str,
        ref: str,
        message: str,
        pppoe_user: str | None = None,
    ) -> None:
        found.append(
            {
                "ts": stamp,
                "severity": severity,
                "scope": scope,
                "ref": ref,
                "message": message,
                "pppoe_user": pppoe_user,
                "active": 1,
            }
        )

    # ---- device reachability -------------------------------------------
    if limits["device_down"]:
        for row in query(db_path, "SELECT * FROM device_current"):
            if row.get("ok"):
                continue
            kind = str(row.get("kind") or "device")
            name = row.get("name") or row.get("key") or "device"
            label = "OLT" if kind == "olt" else "router" if kind == "router" else kind
            add(
                CRITICAL,
                "device",
                str(row.get("key")),
                f"{label} {name} is unreachable: {row.get('error') or 'unknown error'}",
            )

    # ---- ports ---------------------------------------------------------
    ports = query(
        db_path,
        "SELECT * FROM olt_port_current WHERE kind IN ('pon','uplink')",
    )
    for port in ports:
        name = port.get("if_name") or f"if{port.get('ifindex')}"
        oper = port.get("oper_status")
        if oper == 2:  # down
            if port.get("kind") == "pon":
                add(CRITICAL, "port", f"{port['olt_name']}/{name}", f"PON port {name} is DOWN")
            elif limits["uplink_down"]:
                add(WARNING, "port", f"{port['olt_name']}/{name}", f"Uplink {name} is DOWN")

        total_bps = (port.get("in_bps") or 0) + (port.get("out_bps") or 0)
        mbps = total_bps / 1_000_000.0
        if port.get("kind") == "uplink" and mbps >= limits["traffic_high_mbps"]:
            add(
                WARNING,
                "port",
                f"{port['olt_name']}/{name}/traffic",
                f"Uplink {name} is at {mbps:.0f} Mbps (threshold "
                f"{limits['traffic_high_mbps']:.0f} Mbps)",
            )

    # ---- ONU optical power / state -------------------------------------
    onus = query(db_path, "SELECT * FROM olt_onu_current")
    weak = 0
    for onu in onus:
        name = onu.get("if_name") or f"if{onu.get('ifindex')}"
        ref = f"{onu['olt_name']}/{name}"
        user = onu.get("pppoe_user")
        rx = onu.get("rx_power_dbm")
        if rx is not None:
            if rx <= limits["rx_critical"]:
                add(
                    CRITICAL, "onu_power", ref,
                    f"{name} optical power {rx:.2f} dBm (critical)",
                    pppoe_user=user,
                )
            elif rx <= limits["rx_low"]:
                add(
                    WARNING, "onu_power", ref,
                    f"{name} optical power {rx:.2f} dBm (weak)",
                    pppoe_user=user,
                )
            elif rx >= limits["rx_high"]:
                add(
                    WARNING, "onu_power", ref,
                    f"{name} optical power {rx:.2f} dBm (too strong)",
                    pppoe_user=user,
                )
            if rx <= limits["rx_low"]:
                weak += 1

        if onu.get("oper_status") == 2:
            if user and limits["subscriber_onu_down"]:
                add(
                    CRITICAL, "onu_down", ref,
                    f"ONU {name} is DOWN (user {user} affected)",
                    pppoe_user=user,
                )
            elif limits["onu_down"]:
                add("info", "onu_down", ref, f"ONU {name} is DOWN", pppoe_user=user)

    # ---- router --------------------------------------------------------
    routers = query(db_path, "SELECT * FROM device_current WHERE kind='router'")
    for row in routers:
        try:
            info = json.loads(row.get("payload") or "{}")
        except (ValueError, TypeError):
            info = {}
        if not info:
            continue
        name = row.get("name") or "router"
        cpu = info.get("cpu_load")
        free = info.get("free_memory_percent")
        if isinstance(cpu, (int, float)) and cpu >= limits["cpu_high"]:
            add(WARNING, "router_cpu", name, f"{name} CPU load {cpu:.0f}%")
        if isinstance(free, (int, float)) and free <= limits["memory_low"]:
            add(WARNING, "router_memory", name, f"{name} free memory {free:.1f}%")
        for key, sfp in info.items():
            if not key.startswith("sfp_"):
                continue
            rx = (sfp or {}).get("rx_power_dbm")
            if isinstance(rx, (int, float)) and rx <= limits["rx_critical"]:
                add(
                    CRITICAL,
                    "router_sfp",
                    f"{name}/{key[4:]}",
                    f"{name} uplink SFP on {key[4:]} receives {rx:.2f} dBm",
                )

    # ---- persist -------------------------------------------------------
    execute(db_path, "UPDATE alerts_current SET active=0 WHERE active=1")
    # one multi-row statement instead of one commit per alert (932 alerts took
    # ~20 s on MySQL); the unique key on the message hash still deduplicates
    upsert_many(db_path, "alerts_current", ["scope", "ref", "message"], found)
    log.info("alerts: %d active", len(found))
    return found


def active_alerts(db_path: str | Path, limit: int = 500) -> list[dict[str, Any]]:
    return query(
        db_path,
        "SELECT * FROM alerts_current WHERE active=1 "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, ts DESC "
        "LIMIT ?",
        [limit],
    )
