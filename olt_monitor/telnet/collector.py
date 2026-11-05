"""Run a vendor's read-only CLI commands and normalise the output.

:mod:`olt_monitor.telnet.client` knows how to talk to a CLI;
:mod:`olt_monitor.telnet.parsers` knows how to read its tables.  This module
ties the two together for one device:

1. log in once,
2. run the static ``show`` commands (CPU, memory, temperature, SFP, inactive
   ONUs),
3. run the per-PON optical command for every PON that has ONUs,
4. optionally fetch detailed inventory for a handful of ONUs per cycle (some
   firmware only has a per-ONU ``show ont info ...`` command).

The result is a plain dict that the OLT collector merges into its snapshot.  A
CLI that is down, slow or unsupported never fails the poll: the error is
returned in ``error`` and the SNMP data is still stored.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from .. import vendors
from . import parsers
from .client import TelnetError, TelnetSession

DEFAULT_TIMEOUT = 8.0
MAX_PONS = 24           # a chassis with more PONs still gets the first 24
DEFAULT_DETAIL_MAX = 8  # per-ONU ``show ont info`` calls per cycle

# the firmware's way of saying "no such command here".  A restricted account
# answers *every* command this way, which is the difference between "the CLI
# has nothing to say" and "this account may not say it".
_REJECTED = re.compile(
    r"%\s*(unknown command|there is no matched command|command incomplete|invalid input)",
    re.IGNORECASE,
)


def _specs(commands: dict[str, Any], key: str) -> list[tuple[str, str]]:
    """Every command for one profile key (a key may need several commands)."""
    entry = commands.get(key)
    if not entry:
        return []
    if isinstance(entry, str):
        return [(entry, "exec")]
    mode = str(entry.get("mode") or "exec").lower()
    command = entry.get("cmd")
    if isinstance(command, (list, tuple)):
        return [(str(item).strip(), mode) for item in command if str(item).strip()]
    command = str(command or "").strip()
    return [(command, mode)] if command else []


def _spec(commands: dict[str, Any], key: str) -> tuple[str, str] | None:
    specs = _specs(commands, key)
    return specs[0] if specs else None


def _profile_for(device: dict[str, Any]) -> dict[str, Any]:
    name = vendors.telnet_profile_for(
        device.get("brand"),
        device.get("pon_type"),
        device.get("telnet_profile"),
    )
    return vendors.telnet_profile(name)


def collect(
    device: dict[str, Any],
    pon_numbers: Iterable[int] | None = None,
    detail_onus: Iterable[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Collect CLI telemetry for *device*.

    Returns::

        {
          "ok": bool, "error": str|None, "commands": int, "duration_ms": int,
          "health": {"cpu_percent", "memory_percent", "memory_total_mb",
                     "memory_free_mb", "temperature_c"},
          "ports": [{"pon", "temperature_c", "voltage_v", "bias_current_ma",
                     "tx_power_dbm", "rx_power_dbm"}],
          "onus":  [{"pon", "onu_id", "rx_power_dbm", "tx_power_dbm",
                     "temperature_c", "voltage_v", "bias_current_ma",
                     "distance_m", "serial", "model", "vlan", "state",
                     "last_down_reason"}],
          "identity": {...},
          "macs": {"AA:BB:CC:DD:EE:FF": "epon0/1:22"},
        }
    """
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": False,
        "error": None,
        "commands": 0,
        "duration_ms": 0,
        "health": {},
        "ports": [],
        "onus": [],
        "identity": {},
        "macs": {},
        "mac_vlans": {},
    }

    host = str(device.get("host") or "").strip()
    if not host:
        result["error"] = "no host configured"
        return result
    if not device.get("telnet_enabled"):
        result["error"] = "Telnet collection is disabled for this device"
        return result

    timeout = float(device.get("telnet_timeout_seconds") or DEFAULT_TIMEOUT)
    profile = _profile_for(device)
    commands = profile.get("commands") or {}

    pons = sorted({int(p) for p in (pon_numbers or []) if str(p).isdigit() or isinstance(p, int)})
    pons = [p for p in pons if p > 0][:MAX_PONS]

    # A per-PON command such as ``show pon-port ddm-info pon{pon}`` still has
    # to run when SNMP has not discovered any ONU/PON yet (BDPON / BTPON /
    # PHOTON expose almost no PON data over SNMP).  Probe the usual 8 ports;
    # a chassis with more ports will be covered once its PON list is known.
    per_pon_ports = [spec for spec in _specs(commands, "ports") if "{pon}" in spec[0]]
    if per_pon_ports and not pons:
        pons = list(range(1, 9))

    raw: dict[str, str] = {}
    errors: list[str] = []
    inactive_rows: list[dict[str, Any]] = []

    try:
        with TelnetSession(
            host=host,
            port=int(device.get("telnet_port") or 23),
            username=str(device.get("telnet_username") or ""),
            password=str(device.get("telnet_password") or ""),
            enable_password=str(device.get("telnet_enable_password") or ""),
            timeout=timeout,
        ) as session:
            # ---- static commands ---------------------------------------
            for key in ("cpu", "memory", "temperature", "ports", "uplinks",
                        "identity", "active_onu", "inactive_onu", "macs",
                        "onu_table"):
                for command, mode in _specs(commands, key):
                    if "{" in command:  # per-PON / per-ONU template, handled below
                        continue
                    try:
                        text = session.run(command, mode=mode, timeout=timeout * 2)
                        result["commands"] += 1
                        if _REJECTED.search(text):
                            errors.append(
                                f"{key}: the CLI rejected '{command}' - the account "
                                "probably has no privileged access"
                            )
                            continue
                        raw[key] = (raw.get(key, "") + "\n" + text).strip()
                    except TelnetError as exc:
                        errors.append(f"{key}: {exc}")

            # ---- per-PON SFP DDM (BDPON / BTPON / PHOTON) ---------------
            for pon in pons:
                for spec in list(per_pon_ports):
                    command, mode = spec[0].replace("{pon}", str(pon)), spec[1]
                    try:
                        text = session.run(command, mode=mode, timeout=timeout * 2)
                        result["commands"] += 1
                        if _REJECTED.search(text):
                            errors.append(
                                f"ports PON{pon}: the CLI rejected '{command}' - "
                                "the account probably has no privileged access"
                            )
                            per_pon_ports.remove(spec)
                            continue
                        result["ports"].extend(parsers.parse_port_optics(text, pon=pon))
                        # ``show interface gpon0/<pon>`` is an interface detail
                        # block, not a DDM table: parse both shapes.
                        for reading in parsers.parse_interface_details(text):
                            if reading.get("pon") is None:
                                reading["pon"] = pon
                            result["ports"].append(reading)
                    except TelnetError as exc:
                        errors.append(f"ports PON{pon}: {exc}")
                        per_pon_ports.remove(spec)

            # a vendor that has no explicit identity command: ``show version``
            if "identity" not in raw:
                try:
                    text = session.run("show version", timeout=timeout * 2)
                    result["commands"] += 1
                    if not _REJECTED.search(text):
                        raw["identity"] = text
                except TelnetError:
                    pass

            # ---- per-PON optical ---------------------------------------
            optical_spec = _spec(commands, "onu_optical")
            info_spec = _spec(commands, "onu_info")
            # ``onu_detail`` is the per-ONU inventory command.  Older profiles
            # put it under ``onu_info``; keep reading both.
            detail_spec = _spec(commands, "onu_detail")
            if detail_spec is None and info_spec and "{onu}" in info_spec[0]:
                detail_spec = info_spec
            for pon in pons:
                if optical_spec and "{pon}" in optical_spec[0]:
                    command = optical_spec[0].replace("{pon}", str(pon))
                    try:
                        text = session.run(command, mode=optical_spec[1], timeout=timeout * 2)
                        result["commands"] += 1
                        if _REJECTED.search(text):
                            errors.append(
                                f"optical PON{pon}: the CLI rejected '{command}' - "
                                "the account probably has no privileged access"
                            )
                            optical_spec = None
                        else:
                            for onu_id, values in parsers.parse_onu_optics(text).items():
                                entry = {"pon": pon, "onu_id": onu_id}
                                entry.update(values)
                                result["onus"].append(entry)
                    except TelnetError as exc:
                        # a firmware that rejects the command will reject it for
                        # every PON - stop early instead of paying N timeouts
                        errors.append(f"optical PON{pon}: {exc}")
                        optical_spec = None

                if info_spec and "{pon}" in info_spec[0] and "{onu}" not in info_spec[0]:
                    command = info_spec[0].replace("{pon}", str(pon))
                    try:
                        text = session.run(command, mode=info_spec[1], timeout=timeout * 2)
                        result["commands"] += 1
                        if _REJECTED.search(text):
                            errors.append(
                                f"inventory PON{pon}: the CLI rejected '{command}' - "
                                "the account probably has no privileged access"
                            )
                            info_spec = None
                        else:
                            for onu_id, values in parsers.parse_onu_inventory(text, default_pon=pon).items():
                                values = dict(values)
                                reason = values.pop("reason", None)
                                if reason:
                                    values["last_down_reason"] = reason
                                entry = {"pon": pon, "onu_id": onu_id}
                                entry.update({k: v for k, v in values.items() if v is not None})
                                result["onus"].append(entry)
                    except TelnetError as exc:
                        errors.append(f"inventory PON{pon}: {exc}")
                        info_spec = None

            # ---- per-PON inactive table (BDCOM GPON / VSOL GPON) --------
            per_pon_inactive = [
                spec for spec in _specs(commands, "inactive_onu") if "{pon}" in spec[0]
            ]
            for pon in pons:
                for spec in list(per_pon_inactive):
                    command = spec[0].replace("{pon}", str(pon))
                    try:
                        text = session.run(command, mode=spec[1], timeout=timeout * 2)
                        result["commands"] += 1
                        if _REJECTED.search(text):
                            errors.append(
                                f"inactive PON{pon}: the CLI rejected '{command}' - "
                                "the account probably has no privileged access"
                            )
                            per_pon_inactive.remove(spec)
                            continue
                        inactive_rows.extend(
                            parsers.parse_inactive_onus(text, default_pon=pon)
                        )
                    except TelnetError as exc:
                        errors.append(f"inactive PON{pon}: {exc}")
                        per_pon_inactive.remove(spec)

            # ---- per-PON ONT inventory extras (PHOTON / BDPON / BTPON) --
            # These firmwares expose no ONU table over SNMP, so the CLI is the
            # only source of ONU identity, distance and vendor information.
            basic_spec = _spec(commands, "onu_basic")
            version_spec = _spec(commands, "onu_version")
            for pon in pons:
                if basic_spec and "{pon}" in basic_spec[0]:
                    command = basic_spec[0].replace("{pon}", str(pon))
                    try:
                        text = session.run(command, mode=basic_spec[1], timeout=timeout * 2)
                        result["commands"] += 1
                        if _REJECTED.search(text):
                            errors.append(
                                f"basic-info PON{pon}: the CLI rejected '{command}' - "
                                "the account probably has no privileged access"
                            )
                            basic_spec = None
                        else:
                            for onu_id, values in parsers.parse_photon_onu_basic(text).items():
                                entry = {"pon": pon, "onu_id": onu_id}
                                entry.update({k: v for k, v in values.items() if v is not None})
                                result["onus"].append(entry)
                    except TelnetError as exc:
                        errors.append(f"basic-info PON{pon}: {exc}")
                        basic_spec = None

                if version_spec and "{pon}" in version_spec[0]:
                    command = version_spec[0].replace("{pon}", str(pon))
                    try:
                        text = session.run(command, mode=version_spec[1], timeout=timeout * 2)
                        result["commands"] += 1
                        if _REJECTED.search(text):
                            errors.append(
                                f"version PON{pon}: the CLI rejected '{command}' - "
                                "the account probably has no privileged access"
                            )
                            version_spec = None
                        else:
                            for onu_id, values in parsers.parse_photon_onu_version(text).items():
                                entry = {"pon": pon, "onu_id": onu_id}
                                entry.update({k: v for k, v in values.items() if v is not None})
                                result["onus"].append(entry)
                    except TelnetError as exc:
                        errors.append(f"version PON{pon}: {exc}")
                        version_spec = None

            # ---- per-ONU detail (only for firmware that needs it) ------
            if detail_spec and "{onu}" in detail_spec[0]:
                limit = int(device.get("telnet_detail_max") or DEFAULT_DETAIL_MAX)
                for pon, onu_id in list(detail_onus or [])[: max(0, limit)]:
                    command = detail_spec[0].replace("{pon}", str(pon)).replace("{onu}", str(onu_id))
                    try:
                        text = session.run(command, mode=detail_spec[1], timeout=timeout * 2)
                        result["commands"] += 1
                    except TelnetError as exc:
                        errors.append(f"detail {pon}:{onu_id}: {exc}")
                        continue
                    if _REJECTED.search(text):
                        errors.append(f"detail {pon}:{onu_id}: command rejected")
                        detail_spec = None
                        break
                    values = parsers.parse_gpon_onu_detail(text)
                    if values:
                        entry = {"pon": pon, "onu_id": onu_id}
                        entry.update(values)
                        result["onus"].append(entry)

    except TelnetError as exc:
        result["error"] = str(exc)
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result["error"] = f"Telnet failed: {exc}"
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    # ------------------------------------------------------------------
    #  merge the parsed pieces
    # ------------------------------------------------------------------
    if raw.get("cpu"):
        cpu = parsers.parse_cpu(raw["cpu"])
        if cpu is not None:
            result["health"]["cpu_percent"] = cpu

    if raw.get("memory"):
        memory = parsers.parse_memory(raw["memory"])
        if memory.get("used_percent") is not None:
            result["health"]["memory_percent"] = memory["used_percent"]
        if memory.get("free_mb") is not None:
            result["health"]["memory_free_mb"] = memory["free_mb"]
        if memory.get("total_mb") is not None:
            result["health"]["memory_total_mb"] = memory["total_mb"]

    if raw.get("temperature"):
        temperature = parsers.parse_temperature(raw["temperature"])
        if temperature is not None:
            result["health"]["temperature_c"] = temperature

    if raw.get("ports"):
        result["ports"].extend(parsers.parse_port_optics(raw["ports"]))

    # "show interface ..." adds the uplink's status, traffic and SFP DDM
    if raw.get("uplinks"):
        result["ports"].extend(parsers.parse_interface_details(raw["uplinks"]))

    if raw.get("identity"):
        result["identity"] = parsers.parse_identity(raw["identity"])

    # "show epon active-onu" carries every ONU's MAC and fibre distance
    if raw.get("active_onu"):
        result["onus"].extend(parsers.parse_bdcom_active(raw["active_onu"]))

    # C-Data's global ONT table ("show ont info all") has F/S + P on every row,
    # so the PON is part of the parsed row instead of the command's argument.
    if raw.get("onu_table"):
        result["onus"].extend(parsers.parse_cdata_inventory(raw["onu_table"]))

    # "show mac address-table" gives the subscriber MAC -> ONU port mapping
    if raw.get("macs"):
        result["macs"] = parsers.parse_mac_table(raw["macs"])
        result["mac_vlans"] = parsers.parse_mac_table_vlans(raw["macs"])

    # a down ONU's reason is usually only in the inactive-onu table
    inactive_all: list[dict[str, Any]] = list(inactive_rows)
    if raw.get("inactive_onu"):
        inactive_all.extend(parsers.parse_inactive_onus(raw["inactive_onu"]))
    for row in inactive_all:
        if not row.get("reason"):
            continue
        match = next(
            (
                onu for onu in result["onus"]
                if onu.get("onu_id") == row.get("onu_id")
                and (row.get("pon") is None or onu.get("pon") == row.get("pon"))
            ),
            None,
        )
        if match is None:
            match = {
                "pon": row.get("pon"),
                "onu_id": row.get("onu_id"),
            }
            result["onus"].append(match)
        match["last_down_reason"] = row["reason"]
        if row.get("at"):
            match["last_down_at"] = str(row["at"])

    # de-duplicate by (pon, onu_id), later entries winning per field
    merged: dict[tuple[Any, Any], dict[str, Any]] = {}
    for onu in result["onus"]:
        key = (onu.get("pon"), onu.get("onu_id"))
        target = merged.setdefault(key, {"pon": onu.get("pon"), "onu_id": onu.get("onu_id")})
        for field, value in onu.items():
            if value is not None and value != "":
                target[field] = value
    result["onus"] = list(merged.values())

    useful = bool(
        result["health"] or result["onus"] or result["ports"] or result["identity"]
    )
    if not useful and result["commands"]:
        # every command went through but nothing could be read: the usual cause
        # is an account without privileged access
        errors.append(
            f"the CLI answered {result['commands']} command(s) but none produced "
            "readable output - the account may lack privileged access"
        )
    result["ok"] = useful
    if errors and not result["error"]:
        result["error"] = "; ".join(errors[:3]) + ("" if len(errors) <= 3 else f" (+{len(errors) - 3} more)")
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def probe(device: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """The *Test Telnet* button: connect, log in, read ``show version``."""
    from .client import probe as client_probe

    return client_probe(
        str(device.get("host") or ""),
        int(device.get("telnet_port") or 23),
        str(device.get("telnet_username") or ""),
        str(device.get("telnet_password") or ""),
        str(device.get("telnet_enable_password") or ""),
        float(device.get("telnet_timeout_seconds") or DEFAULT_TIMEOUT),
    )
