"""OLT collector - walks a VSOL V1600D (or any EPON OLT) over SNMP.

Every poll produces:

* ``ports``   - uplink (GE), PON (EPON0/x) and VLAN interfaces
* ``onus``    - every ONU interface, with optical values when configured
* ``mac_map`` - subscriber MAC -> ONU interface index, read from the bridge
                forwarding table (FDB).  This is what lets a PPPoE user be
                matched to the ONU that serves them: the PPPoE ``caller-id`` is
                the customer's router MAC, which the OLT has learned behind the
                ONU's port.

Network counters are converted to bits/second using the previous poll kept in
memory, so the first poll after start-up reports no rate.

The FDB walk is comparatively expensive (thousands of rows), so it runs only
every ``olt.mac_table.every_n_polls`` polls and the result is cached in memory
between runs.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import cdata as cdata_mod
from .. import config as config_mod
from .. import health as health_mod
from .. import telnet as telnet_mod
from .. import vendors
from ..db import utcnow_iso
from ..logging_setup import get_logger
from ..snmp import SnmpClient, SnmpError, SnmpProtocolError, format_ticks, to_mac

log = get_logger("collector.olt")

UPLINK = "uplink"
PON = "pon"
ONU = "onu"
VLAN = "vlan"
OTHER = "other"

_2_31 = 2**31
_2_32 = 2**32

# column definitions for the optional optical table
_OPTICAL_FIELDS = {
    "rx_power_dbm": "rx_power",
    "tx_power_dbm": "tx_power",
    "olt_rx_power_dbm": "olt_rx",
    "temperature_c": "temperature",
    "voltage_v": "voltage",
    "bias_current_ma": "bias",
    "distance_m": "distance",
}

#: text-valued inventory columns (serial/model/vendor/VLAN) that share the
#: same row index/port mapping as the numeric optical columns
_TEXT_FIELDS = {
    "serial": "serial",
    "model": "model",
    "vendor": "vendor",
    "vlan": "vlan",
}

# standard bridge tables (RFC 4188 BRIDGE-MIB / RFC 4363 Q-BRIDGE-MIB)
_DOT1D_BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
_DOT1D_FDB_ADDRESS = "1.3.6.1.2.1.17.4.3.1.1"
_DOT1D_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
_DOT1Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"


@dataclass
class OltSnapshot:
    name: str
    ok: bool
    info: dict[str, Any] = field(default_factory=dict)
    ports: list[dict[str, Any]] = field(default_factory=list)
    onus: list[dict[str, Any]] = field(default_factory=list)
    mac_map: dict[str, int] = field(default_factory=dict)
    #: MAC -> service VLAN, when the FDB exposes it (C-Data API, dot1q, BDCOM CLI)
    mac_vlans: dict[str, int] = field(default_factory=dict)
    #: MAC -> PON number when the firmware only learns the subscriber at the
    #: PON level (Photon/BDPON ``show mac-address-table`` prints ``pon1``)
    mac_pons: dict[str, int] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class OltCollector:
    def __init__(self, cfg: dict, device: dict | None = None) -> None:
        olt = dict(device if device is not None else (cfg.get("olt") or {}))
        self.cfg = olt
        self.enabled = bool(olt.get("enabled", True))
        self.name = str(olt.get("name") or "OLT-1")
        self.host = str(olt.get("host") or "")
        self.oids: dict[str, str] = dict(olt.get("oids") or {})
        self.onu_tables: dict[str, Any] = dict(olt.get("onu_tables") or {})
        self.mac_table: dict[str, Any] = dict(olt.get("mac_table") or {})
        self.max_repetitions = int((cfg.get("polling") or {}).get("snmp_max_repetitions", 25))
        bulk_cfg = olt.get("snmp_use_bulk")
        self.use_snmp_bulk: bool | None = None if bulk_cfg is None else bool(bulk_cfg)
        # GPON optical tables may use GETNEXT even when the interface tables use
        # GETBULK: the VSOL agent returns one page of a crowded multipart
        # GETBULK and then stalls until its timeout.
        self.use_snmp_bulk_optical: bool | None = None
        #: multi-root GETBULK is fast on BDCOM but overloads the VSOL V1600G1B
        #: agent, which then stops answering even a single GET for 10+ seconds
        self.use_batch_walk = True
        self._optical_poll = 0
        self._optical_cache: dict[str, dict[int, Any]] = {}
        #: True when the cache was seeded from a recent database snapshot.
        #: The first poll then keeps the stored live values instead of walking
        #: them again, which makes a restart feel instant.
        self._optical_warm = False
        #: Optional JSON file that survives restarts with the last FDB, so a
        #: slow VSOL FDB does not have to be re-walked after every restart.
        self._fdb_cache_path: Path | None = None

        classify = dict(olt.get("classify") or {})
        if not classify:
            # a device dict that did not come through the registry (tools, tests)
            # still gets the right patterns for its brand / PON type
            classify = vendors.classify_for(olt.get("brand"), olt.get("pon_type"))
        # a profile may give one regex or a list; the first that matches wins
        self.re_uplink = vendors.compile_patterns(classify.get("uplink_regex"))
        self.re_pon = vendors.compile_patterns(classify.get("pon_regex"))
        self.re_onu = vendors.compile_patterns(classify.get("onu_regex"))
        self.re_vlan = vendors.compile_patterns(classify.get("vlan_regex"))
        if not self.re_uplink:
            self.re_uplink = vendors.compile_patterns(r"^GE\d*/\d+$")
        if not self.re_pon:
            self.re_pon = vendors.compile_patterns(r"^EPON\d*/\d+$")
        if not self.re_onu:
            self.re_onu = vendors.compile_patterns(r"^EPON(\d+)ONU(\d+)$")
        if not self.re_vlan:
            self.re_vlan = vendors.compile_patterns(r"^vlan\d+$")

        self.brand = str(olt.get("brand") or "auto")
        self.pon_type = str(
            olt.get("pon_type") or vendors.profile(olt.get("brand")).get("pon_type") or "epon"
        ).lower()
        self.max_onus = int(
            olt.get("max_onus_per_pon")
            or vendors.pon_defaults(self.pon_type).get("default_max_onus")
            or 64
        )

        self._last: dict[int, tuple[float, int | None, int | None]] = {}
        self._client: SnmpClient | None = None
        self._ifindexes: set[int] = set()

        self._mac_map: dict[str, int] = {}
        self._mac_vlans: dict[str, int] = {}
        #: Exact subscriber MAC -> ONU ifIndex learned from a vendor table that
        #: reports "PON:ONU" in the same row as the MAC (VSOL GPON).  This is
        #: more accurate than the bridge FDB, whose port numbers are not
        #: ifIndexes on those boxes.
        self._vendor_mac_map: dict[str, int] = {}
        #: subscriber MAC -> PON number for firmware whose FDB only says
        #: "pon1" instead of "pon1:23" (Photon / BDPON / BTPON)
        self._mac_pons: dict[str, int] = {}
        self._mac_polls = 0

        # MACs learned over a vendor API are kept separately so an SNMP or CLI
        # FDB (when it exists) always wins on conflicts, while the API still
        # fills in ONUs that SNMP never exposes (C-Data).
        self._api_mac_map: dict[str, int] = {}
        self._api_mac_vlans: dict[str, int] = {}

        # Telnet runs on its own (slower) cadence and its result is reused
        # between polls, so the UI keeps showing CPU/RAM between CLI sessions.
        self._telnet_last = 0.0
        self._telnet_payload: dict[str, Any] = {}

        # The vendor HTTP API (C-Data) is the only source of the ONU table on
        # some firmware; it is slow, so it also runs on its own cadence.
        self._api: cdata_mod.CDataApi | None = None
        self._api_last = 0.0
        self._api_payload: cdata_mod.CDataResult | None = None

        # health state: remembers that this firmware has no HOST-RESOURCES-MIB
        self._health_state: dict[str, Any] = {}
        self._health_probe: SnmpClient | None = None

        self.stats = {"polls": 0, "failures": 0, "last_duration_ms": 0, "mac_entries": 0}

    # -- helpers ---------------------------------------------------------
    def _oid(self, key: str) -> str | None:
        value = self.oids.get(key)
        return str(value) if value else None

    def client(self) -> SnmpClient:
        if self._client is None:
            self._client = SnmpClient(
                host=self.host,
                community=str(self.cfg.get("community") or "public"),
                version=self.cfg.get("snmp_version", "2c"),
                port=int(self.cfg.get("snmp_port", 161)),
                timeout=float(self.cfg.get("timeout_seconds", 4)),
                retries=int(self.cfg.get("retries", 2)),
                max_repetitions=self.max_repetitions,
            )
        return self._client

    def api(self) -> cdata_mod.CDataApi:
        """The device's HTTP API client (C-Data "OLT Web Management System")."""
        if self._api is None:
            self._api = cdata_mod.CDataApi(
                host=self.host,
                username=str(self.cfg.get("cdata_api_username") or ""),
                password=str(self.cfg.get("cdata_api_password") or ""),
                port=int(self.cfg.get("cdata_api_port") or 80),
                use_tls=bool(self.cfg.get("cdata_api_tls")),
                verify_tls=bool(self.cfg.get("cdata_api_verify_tls", True)),
                timeout=float(self.cfg.get("cdata_api_timeout_seconds") or 12),
            )
        return self._api

    def health_prober(self) -> SnmpClient:
        """A short-timeout client used only to test HOST-RESOURCES-MIB support.

        The four storage walks behind that MIB are cheap on a device that
        implements it and *seconds of timeouts* on one that does not, so the
        probe gets its own 2 s / no-retry socket.
        """
        if self._health_probe is None:
            try:
                timeout = float(self.cfg.get("timeout_seconds", 4))
            except (TypeError, ValueError):
                timeout = 4.0
            self._health_probe = SnmpClient(
                host=self.host,
                community=str(self.cfg.get("community") or "public"),
                version=self.cfg.get("snmp_version", "2c"),
                port=int(self.cfg.get("snmp_port", 161)),
                timeout=max(1.0, min(2.0, timeout)),
                retries=0,
            )
        return self._health_probe

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._health_probe is not None:
            self._health_probe.close()
            self._health_probe = None

    def probe(self) -> tuple[bool, str, dict[str, Any]]:
        """Cheap connectivity test (three SNMP GETs) for the Devices page.

        Returns ``(ok, message, details)``.  Unlike :meth:`collect` this does not
        walk the interface or optical tables, so it answers in well under a
        second and is safe to run from a web button.
        """
        if not self.host:
            return False, "host is not configured", {}
        oids = [
            oid
            for oid in (
                self._oid("sys_descr"),
                self._oid("sys_name"),
                self._oid("sys_object_id"),
                self._oid("if_number"),
            )
            if oid
        ]
        if not oids:
            return False, "no SNMP OIDs configured for this OLT", {}
        try:
            values = self.client().get_map(oids)
        except (SnmpError, SnmpProtocolError) as exc:
            self.close()
            return False, str(exc), {}
        except Exception as exc:  # pragma: no cover - defensive
            self.close()
            return False, str(exc), {}

        descr = values.get(self._oid("sys_descr") or "")
        if not descr:
            return (
                False,
                "no reply - wrong community, wrong host, or SNMP is blocked",
                {},
            )
        details = {
            "sys_descr": str(descr),
            "sys_name": values.get(self._oid("sys_name") or ""),
            "sys_object_id": values.get(self._oid("sys_object_id") or ""),
            "if_number": _as_int(values.get(self._oid("if_number") or "")),
            "community_set": bool(self.cfg.get("community")),
            "snmp_version": str(self.cfg.get("snmp_version", "2c")),
            "detected": vendors.detect(
                str(descr),
                None,
                self.cfg.get("brand"),
                self.cfg.get("pon_type"),
                values.get(self._oid("sys_object_id") or ""),
            ),
        }
        return True, str(descr), details

    def classify(self, name: str) -> tuple[str, int | None, int | None]:
        """Return ``(kind, pon, onu_id)`` for an interface name.

        The V1600D pads interface names with a trailing space (``"GE0/1 "``),
        so the name is stripped first - otherwise the PON and uplink regexes
        would never match and every port would be classified as ``other``.

        The patterns come from the vendor profile.  ``EPON01ONU8`` and
        ``EPON0/1:8`` (BDCOM) both use named groups ``pon`` / ``onu``; a legacy
        unnamed pattern falls back to groups 1 and 2.
        """
        name = str(name or "").strip()

        # A few VSOL / ECOM firmware releases append a human description to the
        # interface name:
        #     GPON0/1 Area-A,Road-1,Market
        #     GPON01ONU61 GPON0/1:61_7141
        #     GE0/3 Uplink
        # Matching only the full string classified those ports as "other", so
        # the four GPON ports vanished and the UI synthesised fake EPON rows.
        # Try the complete name first, then the leading token (the real port).
        candidates = [name]
        head = re.split(r"[\s,]", name, maxsplit=1)[0]
        if head and head != name:
            candidates.append(head)

        for candidate in candidates:
            for pattern in self.re_onu:
                match = pattern.match(candidate)
                if not match:
                    continue
                groups = match.groupdict()
                try:
                    raw_pon = groups.get("pon") or (match.group(1) if match.groups() else None)
                    raw_onu = groups.get("onu") or (match.group(2) if len(match.groups()) > 1 else None)
                    return ONU, vendors.pon_number(raw_pon), int(raw_onu)
                except (IndexError, TypeError, ValueError):
                    return ONU, None, None

            if any(pattern.match(candidate) for pattern in self.re_pon):
                return PON, None, None
            if any(pattern.match(candidate) for pattern in self.re_uplink):
                return UPLINK, None, None
            if any(pattern.match(candidate) for pattern in self.re_vlan):
                return VLAN, None, None
        return OTHER, None, None

    def _rates(self, ifindex: int, now: float, in_oct: int | None, out_oct: int | None):
        """Convert octet counters to bits/second using the previous poll."""
        previous = self._last.get(ifindex)
        self._last[ifindex] = (now, in_oct, out_oct)
        if not previous:
            return None, None
        prev_ts, prev_in, prev_out = previous
        elapsed = now - prev_ts
        if elapsed <= 0.2:
            return None, None

        def rate(cur: int | None, prev: int | None) -> float | None:
            if cur is None or prev is None or cur < prev:
                return None  # counter reset / wrap -> skip this round
            return (cur - prev) * 8.0 / elapsed

        return rate(in_oct, prev_in), rate(out_oct, prev_out)

    @staticmethod
    def _signed(value: Any) -> Any:
        """Some agents pack negative values into unsigned counters."""
        if isinstance(value, int) and value > _2_31:
            return value - _2_32
        return value

    # -- main ------------------------------------------------------------
    def collect(self) -> OltSnapshot:
        started = time.monotonic()
        if not self.enabled:
            return OltSnapshot(self.name, False, error="OLT polling disabled in config")
        if not self.host:
            return OltSnapshot(self.name, False, error="OLT host is not configured")

        client = self.client()
        try:
            info = self._collect_info(client)
            names = self._walk_map(client, self._oid("if_name"))
            descrs = self._walk_map(client, self._oid("if_descr"))
            if not names and not descrs:
                # Fail fast: walking the remaining six interface columns on a
                # device that answers nothing costs several more timeouts and
                # only delays the "unreachable" state in the UI.
                self._last.clear()
                message = "no interface table returned (wrong community or SNMP blocked?)"
                self.stats["failures"] += 1
                log.warning("OLT %s: %s", self.name, message)
                fallback = self._telnet_only_snapshot(message)
                if fallback is not None:
                    return fallback
                return OltSnapshot(self.name, False, error=message)
            oper = self._walk_map(client, self._oid("if_oper_status"))
            admin = self._walk_map(client, self._oid("if_admin_status"))
            in_oct = self._walk_map(client, self._oid("if_hc_in_octets"))
            out_oct = self._walk_map(client, self._oid("if_hc_out_octets"))
            in_err = self._walk_map(client, self._oid("if_in_errors"))
            out_err = self._walk_map(client, self._oid("if_out_errors"))
        except SnmpError as exc:
            self.stats["failures"] += 1
            log.warning("OLT %s unreachable: %s", self.name, exc)
            fallback = self._telnet_only_snapshot(str(exc))
            if fallback is not None:
                return fallback
            return OltSnapshot(self.name, False, error=str(exc))

        now = time.monotonic()
        stamp = utcnow_iso()
        ports: list[dict[str, Any]] = []
        onus: list[dict[str, Any]] = []
        all_indexes = sorted(
            set(names) | set(descrs), key=lambda k: int(k) if k.isdigit() else 0
        )
        self._ifindexes = {int(i) for i in all_indexes if i.isdigit()}

        # ---- vendor detection (before classification / optical walks) ----
        # A device added as "auto" must still get the right ONU/fdb tables.
        # Detect from sysDescr + interface names first, then swap in the
        # verified profile for the detected brand/PON family.
        try:
            detected = vendors.detect(
                info.get("sys_descr"),
                [names.get(i) or descrs.get(i) for i in all_indexes],
                self.cfg.get("brand"),
                self.cfg.get("pon_type"),
                info.get("sys_object_id"),
            )
        except Exception:  # pragma: no cover - detection must never break a poll
            detected = {"brand": "generic", "pon_type": self.pon_type,
                        "label": "Generic", "verified": False, "reason": "detection failed"}
        detected_changed = (
            detected["brand"] != self.brand or detected["pon_type"] != self.pon_type
        )
        if detected_changed:
            self.brand = detected["brand"]
            self.pon_type = detected["pon_type"]
            classify_detected = vendors.classify_for(self.brand, self.pon_type)
            self.re_uplink = vendors.compile_patterns(classify_detected.get("uplink_regex"))
            self.re_pon = vendors.compile_patterns(classify_detected.get("pon_regex"))
            self.re_onu = vendors.compile_patterns(classify_detected.get("onu_regex"))
            self.max_onus = int(
                vendors.pon_defaults(self.pon_type).get("default_max_onus") or 64
            )
            configured_brand = str(self.cfg.get("brand") or "auto").lower()
            if configured_brand in ("", "auto", "generic", "gpon", "gpon_soho"):
                profile = config_mod.ONU_TABLE_PROFILES.get(
                    (self.brand, self.pon_type)
                )
                if profile:
                    self.onu_tables = config_mod._merge_tables(
                        config_mod.DEFAULT_ONU_TABLES, profile
                    )
                mac_profile = config_mod.MAC_TABLE_PROFILES.get(
                    (self.brand, self.pon_type)
                )
                if mac_profile:
                    self.mac_table = {
                        **config_mod.DEFAULT_MAC_TABLE,
                        **mac_profile,
                    }
        # The V1600G1B SNMP agent takes ~2.5 s for 10 GETBULK repetitions and
        # can pause for many seconds between responses.  A 4 s timeout with 25
        # repetitions returns one page and then stalls; 15 s / 10 repetitions
        # is the combination that completes the whole table.
        if self.brand == "vsol" and self.pon_type == "gpon":
            client.timeout = max(float(client.timeout or 0), 15.0)
            client.max_repetitions = min(int(client.max_repetitions or 10), 10)
            self.use_batch_walk = False
        if self.brand == "bdcom" and self.pon_type == "epon":
            # The P3608B agent answers a multi-root GETBULK with only the
            # first page and then stalls.  Separate single-column walks are
            # slower but complete; each ONU optical table is ~400 rows.
            self.use_batch_walk = False

        for index in all_indexes:
            ifindex = int(index) if index.isdigit() else 0
            if_name = str(names.get(index) or "").strip() or str(descrs.get(index) or "").strip()
            if_descr = str(descrs.get(index) or "").strip() or if_name
            kind, pon_id, onu_id = self.classify(if_name)
            hc_in = _as_int(in_oct.get(index))
            hc_out = _as_int(out_oct.get(index))
            in_bps, out_bps = self._rates(ifindex, now, hc_in, hc_out)

            row = {
                "olt_name": self.name,
                "ifindex": ifindex,
                "if_name": if_name,
                "if_descr": if_descr,
                "kind": kind,
                "pon_type": self.pon_type if kind == PON else None,
                "max_onus": self.max_onus if kind == PON else None,
                "admin_status": _as_int(admin.get(index)),
                "oper_status": _as_int(oper.get(index)),
                "hc_in_octets": hc_in,
                "hc_out_octets": hc_out,
                "in_errors": _as_int(in_err.get(index)),
                "out_errors": _as_int(out_err.get(index)),
                "in_bps": in_bps,
                "out_bps": out_bps,
                "sfp_rx_power_dbm": None,
                "sfp_tx_power_dbm": None,
                "sfp_temperature_c": None,
                "updated_at": stamp,
            }
            if kind == ONU:
                # olt_onu_current has its own column set: drop the port-only keys
                for key in ("kind", "in_errors", "out_errors", "pon_type", "max_onus",
                            "sfp_rx_power_dbm", "sfp_tx_power_dbm", "sfp_temperature_c"):
                    row.pop(key, None)
                row.update(
                    {
                        "pon": pon_id,
                        "onu_id": onu_id,
                        "mac": None,
                        "serial": None,
                        "model": None,
                        "vendor": None,
                        "sw_version": None,
                        "onu_type": None,
                        "vlan": None,
                        "rx_power_dbm": None,
                        "tx_power_dbm": None,
                        "olt_rx_power_dbm": None,
                        "temperature_c": None,
                        "voltage_v": None,
                        "bias_current_ma": None,
                        "distance_m": None,
                        "last_down_reason": None,
                        "last_down_at": None,
                        "pppoe_user": None,
                    }
                )
                onus.append(row)
            else:
                ports.append(row)

        note = None
        if onus and self._optical_configured():
            try:
                note = self._apply_optical(client, onus)
            except (SnmpError, SnmpProtocolError) as exc:
                note = f"optical tables failed: {exc}"
                log.warning("OLT %s: %s", self.name, note)
        elif onus:
            note = (
                "optical/ONU tables are not configured yet - run "
                "tools/discover_olt.py --autoconfig (see Settings)"
            )

        try:
            self._apply_pon_optical(client, ports)
        except (SnmpError, SnmpProtocolError) as exc:
            log.warning("OLT %s: PON SFP walk failed: %s", self.name, exc)

        # ---- vendor detection result (computed before classification) ----
        info["detected_brand"] = detected
        info["brand"] = detected["brand"]
        info["brand_label"] = detected["label"]
        info["pon_type"] = detected["pon_type"]
        info["pon_speed"] = vendors.pon_defaults(detected["pon_type"]).get("speed")
        # a firmware whose naming disagrees with the stored PON type would
        # classify every ONU as "other"; trust what the interfaces show
        if detected["pon_type"] != self.pon_type:
            log.warning(
                "OLT %s: configured as %s but the interfaces look like %s - "
                "change the PON type on the Devices page",
                self.name, self.pon_type, detected["pon_type"],
            )
            info["pon_type_warning"] = (
                f"configured as {self.pon_type.upper()} but detected "
                f"{detected['pon_type'].upper()} - fix the PON type"
            )

        # ---- health telemetry (SNMP) ------------------------------------
        try:
            health_data = health_mod.collect(
                client, self.cfg, info, self._health_state, self.health_prober()
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("OLT %s: health collection failed: %s", self.name, exc)
            health_data = {"ok": False, "error": str(exc)}

        # ---- vendor HTTP API (C-Data: SNMP has no ONU table) -------------
        api_result = self._maybe_api()
        if api_result is not None:
            if api_result.ok:
                self._merge_api(onus, ports, api_result)
            elif api_result.error:
                info["cdata_error"] = api_result.error
            if api_result.note:
                info["cdata_note"] = api_result.note
            for src, dst in (
                ("model", "model"),
                ("firmware", "firmware"),
                ("hardware", "cdata_hardware"),
                ("serial", "serial"),
                ("device_type", "cdata_device_type"),
                ("sys_name", "cdata_sys_name"),
            ):
                if api_result.info.get(src):
                    info[dst] = api_result.info[src]
            if api_result.ok:
                info["cdata_api"] = True
                # the API itself is the C-Data fingerprint: their firmware
                # answers it and nobody else does
                if str(self.cfg.get("brand") or "auto").lower() in ("", "auto", "generic"):
                    detected = {
                        "brand": "cdata",
                        "pon_type": self.pon_type,
                        "label": vendors.brand_label("cdata"),
                        "verified": True,
                        "reason": "C-Data HTTP API answered",
                    }
                    info["detected_brand"] = detected
                    info["brand"] = detected["brand"]
                    info["brand_label"] = detected["label"]
                for field in ("cpu_percent", "memory_percent", "temperature_c",
                              "uptime_seconds"):
                    if api_result.health.get(field) is not None:
                        health_data[field] = api_result.health[field]
                if any(
                    api_result.health.get(f) is not None
                    for f in ("cpu_percent", "memory_percent", "temperature_c")
                ):
                    health_data["ok"] = True
                    health_data["api"] = True
                    health_data["source"] = "cdata-api"

        # ---- subscriber MAC table (FDB) -> lets us join users to ONUs
        mac_note = self._refresh_mac_table(client, onus)

        # ---- Telnet CLI telemetry (own cadence) -------------------------
        telnet_note = self._maybe_telnet(onus, ports)
        if telnet_note:
            info["telnet_note"] = telnet_note

        if note:
            info["optical_note"] = note
        if mac_note:
            info["mac_table_note"] = mac_note

        # Merge the vendor-API FDB with the SNMP/CLI one.  SNMP/CLI wins on a
        # conflict, but the API entries stay for ONUs SNMP cannot see.
        mac_map = dict(self._api_mac_map)
        mac_map.update(self._mac_map)
        # A vendor table that prints MAC and PON:ONU in the same row is the
        # most trustworthy source; let it override any bridge-port guess.
        mac_map.update(self._vendor_mac_map)
        mac_vlans = dict(self._api_mac_vlans)
        mac_vlans.update(self._mac_vlans)
        mac_pons = dict(self._mac_pons)
        if self.mac_table.get("enabled", True) or mac_map:
            info["mac_entries"] = len(mac_map)

        # A GPON ONU often has no VLAN column of its own in SNMP.  Derive it
        # from the service VLANs of the subscriber MACs learned behind it, so
        # the ONU page shows the VLAN that the router actually uses.
        if mac_map and mac_vlans:
            vlan_counts: dict[int, dict[int, int]] = {}
            for mac, vlan in mac_vlans.items():
                ifindex = mac_map.get(mac)
                if ifindex is None or not vlan:
                    continue
                bucket = vlan_counts.setdefault(int(ifindex), {})
                bucket[int(vlan)] = bucket.get(int(vlan), 0) + 1
            for onu in onus:
                if onu.get("vlan"):
                    continue
                bucket = vlan_counts.get(int(onu.get("ifindex") or 0))
                if bucket:
                    onu["vlan"] = str(max(bucket.items(), key=lambda kv: kv[1])[0])

        # merge the CLI health values over the SNMP ones (Telnet is the only
        # source of CPU/RAM/temperature on most EPON firmware)
        telnet_health = (self._telnet_payload or {}).get("health") or {}
        for field in ("cpu_percent", "memory_percent", "memory_total_mb", "memory_free_mb", "temperature_c"):
            if telnet_health.get(field) is not None:
                health_data[field] = telnet_health[field]
        if telnet_health:
            health_data["ok"] = True
            health_data["source"] = "snmp+telnet" if health_data.get("uptime_seconds") else "telnet"

        telnet_payload = self._telnet_payload or {}
        health_data["telnet_ok"] = bool(telnet_payload.get("ok")) if telnet_payload else None
        health_data["telnet_ms"] = telnet_payload.get("duration_ms")
        health_data["telnet_error"] = telnet_payload.get("error")
        health_data["telnet_commands"] = telnet_payload.get("commands")

        self.stats["polls"] += 1
        self.stats["mac_entries"] = len(mac_map)
        self.stats["last_duration_ms"] = int((time.monotonic() - started) * 1000)
        log.info(
            "OLT %s: %d interfaces (%d ports, %d ONUs, %d MACs) in %d ms "
            "[%s/%s, health=%s, telnet=%s]",
            self.name, len(all_indexes), len(ports), len(onus),
            len(mac_map), self.stats["last_duration_ms"],
            detected["brand"], detected["pon_type"],
            "yes" if health_data.get("ok") else "no",
            "yes" if telnet_payload.get("ok") else ("off" if not self.cfg.get("telnet_enabled") else "no"),
        )
        return OltSnapshot(
            self.name, True, info=info, ports=ports, onus=onus,
            mac_map=mac_map, mac_vlans=mac_vlans, mac_pons=mac_pons,
            health=health_data,
        )

    # -- vendor HTTP API (C-Data: their ONU table is not in SNMP at all) ---
    def _maybe_api(self) -> cdata_mod.CDataResult | None:
        """Poll the vendor HTTP API when it is enabled and due.

        The ONU optical table makes the OLT ask every ONU over OAM, which takes
        tens of seconds, so this runs on its own cadence and the last result is
        merged into every SNMP poll in between.
        """
        if not self.cfg.get("cdata_api_enabled"):
            return None
        if not str(self.cfg.get("cdata_api_username") or "").strip():
            return None
        try:
            interval = int(self.cfg.get("cdata_api_interval_seconds") or 300)
        except (TypeError, ValueError):
            interval = 300

        now = time.monotonic()
        if self._api_payload is not None and (now - self._api_last) < max(30, interval):
            return self._api_payload

        try:
            result = cdata_mod.collect(
                self.api(),
                self.name,
                optical=bool(self.cfg.get("cdata_api_optical", True)),
                rates=bool(self.cfg.get("cdata_api_rates", True)),
                mac=bool(self.cfg.get("cdata_api_mac", True)),
            )
        except Exception as exc:  # pragma: no cover - never fail the SNMP poll
            log.warning("OLT %s: C-Data API collection crashed: %s", self.name, exc)
            result = cdata_mod.CDataResult(ok=False, error=f"API collection crashed: {exc}")
        self._api_last = now
        self._api_payload = result
        if result.ok:
            log.info(
                "OLT %s: C-Data API: %d ONUs, %d PONs, %d call(s) in %d ms",
                self.name, len(result.onus), len(result.pons),
                self._api.calls if self._api else 0,
                self._api.duration_ms if self._api else 0,
            )
        else:
            log.info("OLT %s: C-Data API: %s", self.name, result.error)
        return result

    def _merge_api(
        self,
        onus: list[dict[str, Any]],
        ports: list[dict[str, Any]],
        result: cdata_mod.CDataResult,
    ) -> None:
        """Attach API values to the SNMP rows, adding ONUs SNMP never showed."""
        if not result.ok:
            return

        # ---- subscriber FDB from the vendor API (C-Data) -------------------
        if result.mac_map:
            self._api_mac_map = dict(result.mac_map)
        if result.mac_vlans:
            self._api_mac_vlans = dict(result.mac_vlans)

        # ---- PON ports: OLT-side SFP DDM (and a row if SNMP had none) ------
        port_by_pon: dict[int, dict[str, Any]] = {}
        for row in ports:
            if row.get("kind") != PON:
                continue
            pon = vendors.pon_number(row.get("if_name"))
            if pon is not None:
                port_by_pon[pon] = row

        for info in result.pons:
            pon = info.get("pon")
            if pon is None:
                continue
            target = port_by_pon.get(int(pon))
            if target is None:
                target = _blank_like(ports)
                target.update(
                    {
                        "olt_name": self.name,
                        "ifindex": info.get("index"),
                        "if_name": f"epon {info.get('pon_id')}",
                        "if_descr": f"epon {info.get('pon_id')}",
                        "kind": PON,
                        "pon_type": self.pon_type,
                        "max_onus": self.max_onus,
                    }
                )
                ports.append(target)
                port_by_pon[int(pon)] = target
            for field in ("sfp_tx_power_dbm", "sfp_rx_power_dbm", "sfp_temperature_c"):
                value = info.get(field)
                if value is not None:
                    target[field] = value

        # ---- ONUs ----------------------------------------------------------
        existing: dict[tuple[int, int], dict[str, Any]] = {}
        for row in onus:
            if row.get("pon") is not None and row.get("onu_id") is not None:
                existing[(int(row["pon"]), int(row["onu_id"]))] = row

        merged_fields = (
            "oper_status", "admin_status", "mac", "model", "vendor", "sw_version",
            "rx_power_dbm", "tx_power_dbm", "olt_rx_power_dbm", "temperature_c",
            "voltage_v", "bias_current_ma", "distance_m", "last_down_reason",
            "last_down_at", "in_bps", "out_bps",
        )
        for row in result.onus:
            if row.get("pon") is None or row.get("onu_id") is None:
                continue
            key = (int(row["pon"]), int(row["onu_id"]))
            target = existing.get(key)
            if target is None:
                onus.append(dict(row))
                existing[key] = onus[-1]
                continue
            for field in merged_fields:
                if row.get(field) is not None:
                    target[field] = row[field]
            if row.get("if_descr") and not target.get("if_descr"):
                target["if_descr"] = row["if_descr"]

    # -- Telnet CLI ------------------------------------------------------
    def _maybe_telnet(
        self,
        onus: list[dict[str, Any]],
        ports: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> str | None:
        """Run the CLI collector when it is enabled and due, then merge it in."""
        if not self.cfg.get("telnet_enabled"):
            return None
        try:
            interval = int(self.cfg.get("telnet_interval_seconds") or 300)
        except (TypeError, ValueError):
            interval = 300
        now = time.monotonic()
        due = force or (now - self._telnet_last) >= max(30, interval) or not self._telnet_payload
        if not due and self._telnet_payload:
            self._merge_telnet(onus, ports, self._telnet_payload)
            return None
        if not due:
            return None

        pons = {int(row["pon"]) for row in onus if row.get("pon") is not None}
        # Include every PON port SNMP discovered, not only the ones that have
        # ONUs: BDPON / BTPON / PHOTON need their SFP DDM even on an empty PON.
        for row in ports:
            if row.get("kind") != PON:
                continue
            match = re.search(r"(\d+)\s*$", str(row.get("if_name") or ""))
            if match:
                pons.add(int(match.group(1)))
        if not pons:
            # SNMP is down or exposes no PON ports (first poll of a Photon box):
            # probe the configured number of PONs so the CLI can still inventory
            # the whole device.
            try:
                count = int(self.cfg.get("pon_count") or 8)
            except (TypeError, ValueError):
                count = 8
            pons = set(range(1, max(1, min(count, 64)) + 1))
        pons = sorted(pons)
        detail = [
            (int(row["pon"]), int(row["onu_id"]))
            for row in onus
            if row.get("pon") is not None and row.get("onu_id") is not None
            and not row.get("serial") and not row.get("model")
        ]
        # The brand may have been auto-detected on this poll (e.g. PHOTON's
        # "EPON-OLT Series" sysDescr).  Use the family profile for its
        # brand + PON type, while still honouring an explicit override.
        device = dict(self.cfg)
        device["brand"] = self.brand
        device["pon_type"] = self.pon_type
        device["telnet_profile"] = vendors.telnet_profile_for(
            self.brand,
            self.pon_type,
            self.cfg.get("telnet_profile"),
        )
        try:
            payload = telnet_mod.collect(device, pons, detail)
        except Exception as exc:  # pragma: no cover - never fail the SNMP poll
            log.warning("OLT %s: Telnet collection crashed: %s", self.name, exc)
            return f"Telnet collection crashed: {exc}"
        self._telnet_last = now
        if payload.get("identity"):
            self._telnet_payload = payload
        else:
            self._telnet_payload = {**(self._telnet_payload or {}), **payload}
        self._merge_telnet(onus, ports, self._telnet_payload)

        if not payload.get("ok"):
            log.info("OLT %s: Telnet: %s", self.name, payload.get("error"))
            return payload.get("error")
        log.info(
            "OLT %s: Telnet %d command(s) in %d ms (%d ONUs, %d ports, health=%s)",
            self.name, payload.get("commands") or 0, payload.get("duration_ms") or 0,
            len(payload.get("onus") or []), len(payload.get("ports") or []),
            bool(payload.get("health")),
        )
        return None

    def _merge_telnet(
        self,
        onus: list[dict[str, Any]],
        ports: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        """Attach CLI readings to the SNMP rows (SNMP always wins on conflicts)."""
        port_by_pon: dict[int, dict[str, Any]] = {}
        for row in ports:
            if row.get("kind") == PON:
                match = re.search(r"(\d+)\s*$", str(row.get("if_name") or ""))
                if match:
                    port_by_pon[int(match.group(1))] = row

        port_by_name = {
            str(row.get("if_name") or "").strip().lower(): row
            for row in ports if row.get("if_name")
        }

        for reading in payload.get("ports") or []:
            pon = reading.get("pon")
            target = port_by_pon.get(int(pon)) if isinstance(pon, int) else None
            if target is None and reading.get("port"):
                # uplink SFP readings arrive by interface name, not by PON
                target = port_by_name.get(str(reading["port"]).strip().lower())
            if target is None:
                continue
            for src, dst in (
                ("temperature_c", "sfp_temperature_c"),
                ("tx_power_dbm", "sfp_tx_power_dbm"),
                ("rx_power_dbm", "sfp_rx_power_dbm"),
            ):
                value = _dbm(reading.get(src)) if src != "temperature_c" else reading.get(src)
                if value is not None:
                    target[dst] = value

        by_pon_onu: dict[tuple[int, int], dict[str, Any]] = {}
        for row in onus:
            if row.get("pon") is not None and row.get("onu_id") is not None:
                by_pon_onu[(int(row["pon"]), int(row["onu_id"]))] = row

        for reading in payload.get("onus") or []:
            pon, onu_id = reading.get("pon"), reading.get("onu_id")
            if pon is None or onu_id is None:
                continue
            target = by_pon_onu.get((int(pon), int(onu_id)))
            if target is None:
                # CLI-only inventory (BDPON / BTPON / PHOTON): SNMP has no ONU
                # interface for it, so synthesise a stable row.
                target = _blank_onu_row(self.name, int(pon), int(onu_id), utcnow_iso())
                onus.append(target)
                by_pon_onu[(int(pon), int(onu_id))] = target

            # CLI run state only fills a status SNMP did not supply
            state = reading.get("state")
            if state not in (None, "") and target.get("oper_status") is None:
                lowered = str(state).strip().lower()
                # "auto-configured" is how BDCOM/VSOL EPON report an ONU that
                # is registered and forwarding - it is an *online* state, not
                # a missing one.  Without it every Telnet-only EPON showed
                # 0/N ONU up even though the active-onu table listed them all.
                if lowered in (
                    "online", "up", "active", "working", "normal", "registered",
                    "auto-configured", "auto configured", "operational", "ok",
                ):
                    target["oper_status"] = 1
                elif lowered in (
                    "offline", "down", "inactive", "los", "dying-gasp", "failed",
                    "deregistered", "config-failed", "deactive", "disabled",
                ):
                    target["oper_status"] = 2

            # optical values: only fill what SNMP did not provide
            for field in ("rx_power_dbm", "tx_power_dbm"):
                value = _dbm(reading.get(field))
                if value is not None and target.get(field) is None:
                    target[field] = value
            if reading.get("temperature_c") is not None and target.get("temperature_c") is None:
                target["temperature_c"] = float(reading["temperature_c"])
            if reading.get("distance_m") is not None and target.get("distance_m") is None:
                target["distance_m"] = float(reading["distance_m"])
            for field in ("voltage_v", "bias_current_ma"):
                if reading.get(field) is not None:
                    target[field] = float(reading[field])
            for field in ("mac", "serial", "model", "vendor", "sw_version", "vlan",
                          "onu_type"):
                value = reading.get(field)
                if value not in (None, "") and target.get(field) in (None, ""):
                    target[field] = str(value)
            if reading.get("model") and not target.get("onu_type"):
                target["onu_type"] = str(reading["model"])

            # a down reason only makes sense while the ONU is down
            if _oper_down(target):
                if reading.get("last_down_reason"):
                    target["last_down_reason"] = str(reading["last_down_reason"])
                if reading.get("last_down_at"):
                    target["last_down_at"] = str(reading["last_down_at"])

        # CLI forwarding table ("show mac address-table"): fill any MAC that
        # SNMP's bridge MIB did not expose, so PPPoE caller-id matching works.
        cli_macs = payload.get("macs") or {}
        if cli_macs:
            by_name = {
                str(row.get("if_name") or "").strip().upper(): int(row["ifindex"])
                for row in onus if row.get("ifindex")
            }
            cli_vlans = payload.get("mac_vlans") or {}
            for mac, port in cli_macs.items():
                port_text = str(port or "").strip()
                ifindex = by_name.get(port_text.upper())
                if ifindex and mac:
                    key = str(mac)
                    self._mac_map.setdefault(key, ifindex)
                    vlan = (cli_vlans.get(mac) or {}).get("vlan")
                    if vlan:
                        self._mac_vlans.setdefault(key, int(vlan))
                    continue
                # Photon / BDPON / BTPON print only the PON ("pon1"), not the
                # ONU.  C-Data's "show mac all" prints "epon0/1/4".  Keep that
                # weaker association so the subscriber can still be shown
                # under the right OLT and PON.
                if re.match(r"^(?:EPON|GPON|PON)\s*\d", port_text, re.IGNORECASE) and mac:
                    pon = vendors.pon_number(port_text)
                    if pon:
                        self._mac_pons.setdefault(str(mac), int(pon))
                        vlan = (cli_vlans.get(mac) or {}).get("vlan")
                        if vlan:
                            self._mac_vlans.setdefault(str(mac), int(vlan))

    def _synthetic_pon_row(self, pon: int, stamp: str) -> dict[str, Any]:
        """A PON port row for a Telnet-only snapshot (no SNMP interface table)."""
        return {
            "olt_name": self.name,
            "ifindex": cdata_mod.SYNTHETIC_IFINDEX_BASE // 100 + int(pon),
            "if_name": f"pon{int(pon)}",
            "if_descr": f"PON {int(pon)}",
            "kind": PON,
            "pon_type": self.pon_type,
            "max_onus": self.max_onus,
            "admin_status": 1,
            "oper_status": 1,
            "hc_in_octets": None,
            "hc_out_octets": None,
            "in_errors": None,
            "out_errors": None,
            "in_bps": None,
            "out_bps": None,
            "sfp_rx_power_dbm": None,
            "sfp_tx_power_dbm": None,
            "sfp_temperature_c": None,
            "updated_at": stamp,
        }

    def _telnet_only_snapshot(self, reason: str) -> OltSnapshot | None:
        """Poll through the CLI alone when SNMP cannot describe the device.

        Some firmware runs with SNMP disabled, behind a filtering ACL, or with
        an agent too slow to answer a table walk.  The Telnet collector can
        discover the ONUs, their optics and the forwarding table on its own, so
        instead of storing an "unreachable" snapshot the poller falls back to
        it and returns a normal, fully usable snapshot.  ``info["snmp_error"]``
        records why the fallback ran, so the UI can show it.
        """
        if not self.cfg.get("telnet_enabled"):
            return None
        started = time.monotonic()
        onus: list[dict[str, Any]] = []
        ports: list[dict[str, Any]] = []
        try:
            self._maybe_telnet(onus, ports)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("OLT %s: Telnet fallback crashed: %s", self.name, exc)
            return None
        payload = self._telnet_payload or {}
        if not payload.get("ok"):
            return None

        stamp = utcnow_iso()
        pons: set[int] = set()
        for reading in list(payload.get("onus") or []) + list(payload.get("ports") or []):
            pon = reading.get("pon")
            if isinstance(pon, int) or str(pon or "").isdigit():
                pons.add(int(pon))
        for pon in sorted(pons):
            ports.append(self._synthetic_pon_row(pon, stamp))
        # The SFP readings could not be attached before the port rows existed.
        self._merge_telnet(onus, ports, payload)

        mac_map = dict(self._mac_map)
        mac_vlans = dict(self._mac_vlans)
        mac_pons = dict(self._mac_pons)
        health = dict(payload.get("health") or {})
        if health:
            health["ok"] = True
            health["source"] = "telnet"
        health["telnet_ok"] = True
        health["telnet_ms"] = payload.get("duration_ms")
        health["telnet_commands"] = payload.get("commands")
        health["telnet_error"] = payload.get("error")
        health.setdefault("ok", False)

        identity = payload.get("identity") or {}
        info: dict[str, Any] = {
            "snmp_ok": False,
            "snmp_error": reason,
            "telnet_only": True,
            "brand": self.brand,
            "brand_label": vendors.brand_label(self.brand),
            "pon_type": self.pon_type,
            "pon_speed": vendors.pon_defaults(self.pon_type).get("speed"),
            "mac_entries": len(mac_map),
            "telnet_note": "SNMP unavailable - values collected over Telnet",
        }
        for src, dst in (
            ("sys_name", "sys_name"),
            ("model", "model"),
            ("firmware", "firmware"),
            ("serial", "serial"),
            ("vendor", "vendor"),
            ("sys_descr", "sys_descr"),
            ("uptime_seconds", "uptime_seconds"),
        ):
            if identity.get(src) not in (None, ""):
                info[dst] = identity[src]

        self.stats["polls"] += 1
        self.stats["mac_entries"] = len(mac_map)
        self.stats["last_duration_ms"] = int((time.monotonic() - started) * 1000)
        log.info(
            "OLT %s: Telnet-only snapshot (%d ports, %d ONUs, %d MACs) in %d ms "
            "(SNMP: %s)",
            self.name, len(ports), len(onus), len(mac_map),
            self.stats["last_duration_ms"], reason,
        )
        return OltSnapshot(
            self.name, True, info=info, ports=ports, onus=onus,
            mac_map=mac_map, mac_vlans=mac_vlans, mac_pons=mac_pons,
            health=health,
        )

    def _collect_info(self, client: SnmpClient) -> dict[str, Any]:
        scalars = [
            self._oid("sys_descr"),
            self._oid("sys_name"),
            self._oid("sys_uptime"),
            self._oid("sys_location"),
            self._oid("sys_contact"),
            self._oid("if_number"),
        ]
        oids = [oid for oid in scalars if oid]
        started = time.monotonic()
        values = client.get_map(oids) if oids else {}
        rtt_ms = int((time.monotonic() - started) * 1000)
        ticks = _as_int(values.get(self._oid("sys_uptime") or ""))
        return {
            "sys_descr": values.get(self._oid("sys_descr") or ""),
            "sys_name": values.get(self._oid("sys_name") or ""),
            "sys_location": values.get(self._oid("sys_location") or ""),
            "sys_contact": values.get(self._oid("sys_contact") or ""),
            "sys_uptime_ticks": ticks,
            "sys_uptime_text": format_ticks(ticks),
            "uptime_seconds": (ticks // 100) if ticks is not None else None,
            "snmp_rtt_ms": rtt_ms,
            "if_number": _as_int(values.get(self._oid("if_number") or "")),
            # never store the community itself - the Devices page only needs to
            # know that one is configured
            "community_set": bool(self.cfg.get("community")),
            "snmp_version": str(self.cfg.get("snmp_version", "2c")),
            "host": self.host,
        }

    def _walk_map(
        self,
        client: SnmpClient,
        oid: str | None,
        use_bulk: bool | None = None,
        *,
        _inherit: bool = True,
    ) -> dict[str, Any]:
        if not oid:
            return {}
        bulk = self.use_snmp_bulk if _inherit else use_bulk
        try:
            if bulk is None:
                return client.walk_map(oid)
            return client.walk_map(oid, use_bulk=bulk)
        except (SnmpError, SnmpProtocolError) as exc:
            log.warning("OLT %s: walk %s failed: %s", self.name, oid, exc)
            return {}

    def warm_optical_cache(
        self, rows: list[dict[str, Any]], fresh_seconds: float = 1800
    ) -> None:
        """Seed the per-field cache from the previous database snapshot.

        A restart should not re-walk every slow GPON inventory table just
        because the in-memory cache is empty.  Values are keyed by ONU
        ifindex, exactly like :meth:`_apply_optical` stores them.
        """
        fields = (
            "rx_power_dbm", "tx_power_dbm", "olt_rx_power_dbm",
            "temperature_c", "voltage_v", "bias_current_ma", "distance_m",
            "serial", "model", "vendor", "vlan", "mac",
        )
        newest: datetime | None = None
        for row in rows:
            try:
                ifindex = int(row.get("ifindex"))
            except (TypeError, ValueError):
                continue
            for field in fields:
                value = row.get(field)
                if value is not None:
                    if field.endswith("_power_dbm") and not _plausible_power(value):
                        continue
                    self._optical_cache.setdefault(field, {})[ifindex] = value
            raw_updated = row.get("updated_at")
            if raw_updated:
                try:
                    stamp = datetime.fromisoformat(
                        str(raw_updated).replace("Z", "+00:00")
                    )
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    if newest is None or stamp > newest:
                        newest = stamp
                except ValueError:
                    pass
        if fresh_seconds and newest is not None and self._optical_cache:
            age = (datetime.now(timezone.utc) - newest).total_seconds()
            self._optical_warm = age <= float(fresh_seconds)

    def set_fdb_cache_path(self, path: str | Path) -> None:
        self._fdb_cache_path = Path(path)

    def import_fdb_cache(self, data: dict[str, Any]) -> bool:
        """Apply a saved MAC/VLAN/PON snapshot (from MySQL or the old file)."""
        try:
            mac = {
                str(key): int(value)
                for key, value in (data.get("mac") or {}).items()
            }
            vlans = {
                str(key): int(value)
                for key, value in (data.get("vlans") or {}).items()
            }
            pons = {
                str(key): int(value)
                for key, value in (data.get("pons") or {}).items()
            }
            vendor_mac = {
                str(key): int(value)
                for key, value in (data.get("vendor_mac") or {}).items()
            }
        except (ValueError, TypeError, AttributeError):
            return False
        if not mac:
            return False
        self._mac_map = mac
        self._mac_vlans = vlans
        self._mac_pons = pons
        # The MAC+port vendor snapshot is the authoritative subscriber -> ONU
        # map.  Keep it across a restart so the first correlation after a
        # service restart does not fall back to a stale bridge guess.
        self._vendor_mac_map = vendor_mac
        # skip the first scheduled FDB walk; the cached table is used for it
        self._mac_polls = 1
        return True

    def export_fdb_cache(self) -> dict[str, Any]:
        """Serialise the current forwarding-table cache for MySQL storage."""
        return {
            "mac": self._mac_map,
            "vlans": self._mac_vlans,
            "pons": self._mac_pons,
            "vendor_mac": self._vendor_mac_map,
        }

    def load_fdb_cache(self) -> bool:
        """Load the FDB saved by the previous run, if it is still available."""
        path = self._fdb_cache_path
        if path is None or not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not read FDB cache %s: %s", path, exc)
            return False
        if not self.import_fdb_cache(data):
            return False
        log.info(
            "OLT %s: loaded %d cached FDB entries from %s",
            self.name, len(self._mac_map), path,
        )
        return True

    def _save_fdb_cache(self) -> None:
        path = self._fdb_cache_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps({
                    "mac": self._mac_map,
                    "vlans": self._mac_vlans,
                    "pons": self._mac_pons,
                    "vendor_mac": self._vendor_mac_map,
                }),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            log.warning("could not save FDB cache %s: %s", path, exc)

    # -- subscriber MAC table (FDB) --------------------------------------
    def _refresh_mac_table(self, client: SnmpClient, onus: list[dict[str, Any]]) -> str | None:
        """Walk the FDB every N polls; keep the previous result in between."""
        if not self.mac_table.get("enabled", True):
            return None

        every = max(1, int(self.mac_table.get("every_n_polls", 5)))
        self._mac_polls += 1
        if (self._mac_map or self._api_mac_map) and self._mac_polls % every != 1:
            return None

        try:
            found, found_vlans = self._collect_mac_table(client, onus)
        except (SnmpError, SnmpProtocolError) as exc:
            return f"MAC table walk failed: {exc}"

        if found:
            # Never let the slower bridge/vendor FDB walk downgrade a link from
            # the exact MAC+port snapshot.
            found.update(self._vendor_mac_map)
            self._mac_map = found
            self._mac_vlans = found_vlans
            self._save_fdb_cache()
            return None
        if self._mac_map or self._api_mac_map:
            return None  # keep the last good / API table

        # nothing found: seed the ONU MACs so at least the ONU's own MAC is known
        for onu in onus:
            mac = _mac_of(onu.get("mac"))
            if mac and onu.get("ifindex"):
                self._mac_map.setdefault(mac, int(onu["ifindex"]))

        return (
            "no bridge forwarding table found (standard BRIDGE-MIB and Q-BRIDGE-MIB "
            "both empty) - user to ONU linking needs olt.mac_table.vendor_oid"
        )

    def _collect_mac_table(
        self, client: SnmpClient, onus: list[dict[str, Any]] | None = None
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Return ``({MAC: ifindex}, {MAC: VLAN})`` from the bridge FDB.

        Standard BRIDGE-MIB on a GPON OLT often only exposes the uplink ports,
        not the ONU behind which a subscriber MAC was learned.  Vendors that
        publish a separate "port" column (``PON3:ONU27``) are handled through
        ``mac_table.vendor_port_oid``.
        """
        sources = [str(s).lower() for s in (self.mac_table.get("sources") or ["dot1d", "dot1q"])]
        by_pon_onu: dict[tuple[int, int], int] = {}
        for row in onus or []:
            if row.get("pon") is None or row.get("onu_id") is None:
                continue
            try:
                by_pon_onu[(int(row["pon"]), int(row["onu_id"]))] = int(row["ifindex"])
            except (TypeError, ValueError, KeyError):
                continue

        # bridge port -> ifIndex
        base_port_ifindex: dict[str, int] = {}
        for index, value in self._walk_map(client, _DOT1D_BASE_PORT_IFINDEX).items():
            ifindex = _as_int(value)
            if ifindex is not None:
                base_port_ifindex[index] = ifindex

        mac_map: dict[str, int] = {}
        vlan_map: dict[str, int] = {}

        # Read the raw forwarding table first, then resolve every bridge port
        # in one pass.  On a GPON OLT the bridge-port number is *not* the ONU
        # ifIndex (VSOL: ONU 3/18 is bridge port 210), so the old "assume the
        # bridge port is an ifIndex" fallback silently linked users to a
        # different, existing ONU.  That produced the fake per-ONU attribution
        # this release fixes.
        raw_entries: list[tuple[str, int, int | None]] = []
        if "dot1d" in sources:
            addresses = self._walk_map(client, _DOT1D_FDB_ADDRESS)
            ports = self._walk_map(client, _DOT1D_FDB_PORT)
            for index, raw in addresses.items():
                mac = _mac_of(raw)
                port = _as_int(ports.get(index))
                if mac and port is not None:
                    raw_entries.append((mac, port, None))

        if "dot1q" in sources:
            for index, bridge_port in self._walk_map(client, _DOT1Q_FDB_PORT).items():
                parts = index.split(".")
                if len(parts) < 7:
                    continue
                try:
                    mac = ":".join(f"{int(a):02X}" for a in parts[-6:])
                except ValueError:
                    continue
                port = _as_int(bridge_port)
                if port is None:
                    continue
                # dot1qTpFdbPort is indexed by dot1qVlanIndex.MAC (6 octets);
                # the first component is the service VLAN of the entry.
                vlan = _as_int(parts[0])
                raw_entries.append((mac, port, vlan if vlan and 0 < vlan < 4095 else None))

        # Learn GPON bridge ports from the ONUs' own MACs.  If the OLT learned
        # an ONU's hardware MAC on bridge port 210, every subscriber MAC on
        # that same bridge port is behind that ONU - no guessing required.
        onu_macs: dict[str, int] = {}
        for row in onus or []:
            mac = _mac_of(row.get("mac"))
            try:
                ifindex = int(row.get("ifindex"))
            except (TypeError, ValueError):
                continue
            if mac:
                onu_macs.setdefault(mac, ifindex)

        learned_ports: dict[str, int] = {}
        for mac, port, _vlan in raw_entries:
            target = onu_macs.get(mac)
            if target is not None:
                learned_ports.setdefault(str(port), target)

        def resolve(mac: str, bridge_port: int) -> int | None:
            # An exact ONU hardware MAC is the strongest possible evidence.
            direct = onu_macs.get(mac)
            if direct is not None:
                return direct
            mapped = base_port_ifindex.get(str(bridge_port))
            if mapped:
                return mapped
            learned = learned_ports.get(str(bridge_port))
            if learned:
                return learned
            # EPON gear usually numbers an ONU bridge port exactly like its
            # ifIndex.  Restrict the identity fallback to EPON: on GPON it is
            # known to point at the wrong ONU.
            if self.pon_type != "gpon" and bridge_port in self._ifindexes:
                return bridge_port
            return None

        for mac, port, vlan in raw_entries:
            ifindex = resolve(mac, port)
            if not ifindex:
                continue
            mac_map.setdefault(mac, ifindex)
            if vlan:
                vlan_map.setdefault(mac, vlan)

        vendor_oid = self.mac_table.get("vendor_oid")
        if vendor_oid and ("vendor" in sources or not mac_map):
            port_oid = self.mac_table.get("vendor_port_oid")
            vlan_oid = self.mac_table.get("vendor_vlan_oid")
            vendor_oids = [
                str(oid) for oid in (vendor_oid, port_oid, vlan_oid) if oid
            ]
            vendor_maps: dict[str, dict[str, Any]] = {}
            batched: dict[str, list[Any]] = {}
            if (
                len(vendor_oids) > 1
                and self.use_batch_walk
                and getattr(client, "version_code", 1) == 1
                and hasattr(client, "walk_many")
            ):
                try:
                    batched = client.walk_many(
                        vendor_oids,
                        max_rows=10_000,
                        max_repetitions=min(
                            10, int(getattr(client, "max_repetitions", 10) or 10)
                        ),
                    )
                except SnmpError:
                    batched = {}
            for oid in vendor_oids:
                rows = batched.get(oid) or []
                if rows:
                    vendor_maps[oid] = {
                        vb.oid[len(oid) + 1:]: vb.value
                        for vb in rows
                        if not vb.is_exception
                    }
                else:
                    vendor_maps[oid] = self._walk_map(client, oid)
            port_values = vendor_maps.get(str(port_oid), {}) if port_oid else {}
            vlan_values = vendor_maps.get(str(vlan_oid), {}) if vlan_oid else {}
            port_regex = self.mac_table.get("vendor_port_regex")
            pattern = re.compile(str(port_regex), re.IGNORECASE) if port_regex else None

            for index, raw in vendor_maps.get(str(vendor_oid), {}).items():
                mac = _mac_of(raw)
                if not mac:
                    continue
                target: int | None = None
                port_text = str(port_values.get(index) or "").strip()
                if pattern and port_text:
                    match = pattern.search(port_text)
                    if match:
                        try:
                            target = by_pon_onu.get(
                                (int(match.group(1)), int(match.group(2)))
                            )
                        except (TypeError, ValueError, IndexError):
                            target = None
                    if target is None and ("PON" in port_text.upper()):
                        # a PON port with no matching ONU row is not a
                        # subscriber port - never fall back to the row id
                        continue
                if target is None:
                    candidate = _as_int(index.split(".")[-1])
                    if candidate is not None and candidate in self._ifindexes:
                        target = candidate
                if target is None:
                    continue
                mac_map.setdefault(mac, target)
                vlan = _as_int(vlan_values.get(index))
                if vlan and 0 < vlan < 4095:
                    vlan_map.setdefault(mac, vlan)

        return mac_map, vlan_map

    # -- optical tables --------------------------------------------------
    def _apply_pon_optical(
        self, client: SnmpClient, ports: list[dict[str, Any]]
    ) -> None:
        """PON-port SFP DDM, indexed by the PON port's ifIndex.

        This is separate from ``_apply_optical`` (which is per ONU).  Some
        vendors publish only transmit power and module temperature, so the
        profile decides which columns exist.
        """
        profile = config_mod.PON_OPTICAL_PROFILES.get((self.brand, self.pon_type))
        if not profile:
            return
        port_by_index = {
            int(row["ifindex"]): row
            for row in ports
            if row.get("ifindex") is not None
        }
        for field, spec in profile.items():
            scale = _as_float(spec.get("scale"), 1.0) or 1.0
            oid = str(spec.get("oid") or "")
            if not oid:
                continue
            for table_index, raw in self._walk_map(client, oid).items():
                try:
                    ifindex = int(table_index.split(".")[-1])
                except ValueError:
                    continue
                target = port_by_index.get(ifindex)
                if target is None:
                    continue
                value = _optical_number(raw, scale)
                if value is None:
                    continue
                # 0 means "module absent" on BDCOM, not a real reading
                if value == 0:
                    continue
                if field == "sfp_temperature_c" and value <= -0.5:
                    continue
                target[field] = round(value, 3)

    def _optical_configured(self) -> bool:
        tables = self.onu_tables or {}
        for key in (
            "rx_power", "tx_power", "olt_rx", "temperature", "voltage",
            "bias", "distance", "mac", "serial", "model", "vendor", "vlan",
        ):
            spec = tables.get(key)
            if isinstance(spec, dict) and spec.get("oid"):
                return True
        return False

    def _resolve_target(
        self,
        table_index: str,
        index_map: dict[str, int],
        by_index: dict[int, dict[str, Any]],
        by_pon_onu: dict[tuple[int, int], int],
    ) -> int | None:
        """Work out which ONU a row of the optical table belongs to.

        Different tables on the same OLT index themselves differently, so every
        known scheme is tried:
          * ``key_oid``  - the row value is the ONU interface name
          * ``index_map``- the row index maps to (pon, onu) through other columns
          * plain index  - the row index is the interface index
          * (pon, onu)   - the last two sub-identifiers are PON and ONU id
          * trailing id  - the last sub-identifier is the interface index
        """
        if table_index in index_map:
            return index_map[table_index]

        # AVIES / HSGQ optical table uses ``onuRow.pon.onu`` while the
        # inventory table that names the ONU is indexed by ``onuRow`` alone.
        # The trailing components are 0.0 for a real ONU (65535.65535 for the
        # PON-level pseudo rows), so the first component is the shared key.
        parts = table_index.split(".")
        if (
            len(parts) >= 2
            and index_map
            and parts[0] in index_map
            and all(part in ("0", "65535") for part in parts[1:])
        ):
            return index_map[parts[0]]

        # index -> (pon, onu) -> ifindex  (e.g. the ONU inventory table)
        if len(parts) >= 2 and parts[-2].isdigit() and parts[-1].isdigit():
            target = by_pon_onu.get((int(parts[-2]), int(parts[-1])))
            if target is not None:
                return target

        if table_index.isdigit():
            candidate = int(table_index)
            if candidate in by_index:
                return candidate

        tail = parts[-1]
        if tail.isdigit() and int(tail) in by_index:
            return int(tail)
        return None

    def _apply_optical(self, client: SnmpClient, onus: list[dict[str, Any]]) -> str | None:
        """Walk the configured optical columns and attach them to each ONU."""
        by_index = {row["ifindex"]: row for row in onus}
        by_name = {str(row["if_name"]).strip().upper(): row["ifindex"] for row in onus}
        by_pon_onu: dict[tuple[int, int], int] = {}
        for row in onus:
            if row.get("pon") is not None and row.get("onu_id") is not None:
                by_pon_onu[(int(row["pon"]), int(row["onu_id"]))] = int(row["ifindex"])

        # the optical table's own index -> ONU interface index
        index_map: dict[str, int] = {}
        key_oid = self.onu_tables.get("key_oid")
        if key_oid:
            for table_index, value in self._walk_map(client, str(key_oid)).items():
                target = by_name.get(str(value).strip().upper())
                if target is not None:
                    index_map[table_index] = target

        # a table whose index is a row number: map it to (pon, onu) first
        index_map_cfg = self.onu_tables.get("index_map") or {}
        pon_oid = index_map_cfg.get("pon_oid") if isinstance(index_map_cfg, dict) else None
        onu_oid = index_map_cfg.get("onu_oid") if isinstance(index_map_cfg, dict) else None
        if pon_oid and onu_oid:
            pon_by_row = self._walk_map(client, str(pon_oid))
            onu_by_row = self._walk_map(client, str(onu_oid))
            for row_index, pon in pon_by_row.items():
                pon_id = _as_int(pon)
                onu_id = _as_int(onu_by_row.get(row_index))
                if pon_id is None or onu_id is None:
                    continue
                target = by_pon_onu.get((pon_id, onu_id))
                if target is not None:
                    index_map[row_index] = target

        resolved_seen = 0
        filled = 0
        self._optical_poll += 1
        warm_grace = self._optical_warm
        self._optical_warm = False
        port_maps: dict[str, dict[str, int]] = {}

        def resolve_row(table_index: str, spec: dict[str, Any]) -> int | None:
            """Resolve a vendor row, including a separate PON:ONU port column."""
            port_oid = spec.get("port_oid")
            if port_oid:
                key = str(port_oid)
                mapping = port_maps.get(key)
                if mapping is None:
                    mapping = {}
                    raw_ports = values.get(key)
                    if raw_ports is None:
                        raw_ports = self._walk_map(
                            client, key, use_bulk=self.use_snmp_bulk_optical,
                            _inherit=False,
                        )
                    for row_index, raw_port in raw_ports.items():
                        target = _port_target(raw_port, by_pon_onu)
                        if target is not None:
                            mapping[row_index] = target
                    port_maps[key] = mapping
                target = mapping.get(table_index)
                if target is not None:
                    return target
            return self._resolve_target(table_index, index_map, by_index, by_pon_onu)

        specs: list[tuple[str, str, dict[str, Any]]] = []
        for field_name, spec_key in _OPTICAL_FIELDS.items():
            spec = self.onu_tables.get(spec_key)
            if isinstance(spec, dict) and spec.get("oid"):
                specs.append(("number", field_name, spec))
        for field_name, spec_key in _TEXT_FIELDS.items():
            spec = self.onu_tables.get(spec_key)
            if isinstance(spec, dict) and spec.get("oid"):
                specs.append(("text", field_name, spec))
        mac_spec = self.onu_tables.get("mac")
        if isinstance(mac_spec, dict) and mac_spec.get("oid"):
            specs.append(("mac", "mac", mac_spec))

        # Which columns are due on this poll?  A field is always due while its
        # cache is empty, so a freshly added OLT fills up on its first poll;
        # afterwards the static columns are staggered to keep polls short.
        slow_optical = self.brand == "vsol" and self.pon_type == "gpon"
        due_specs: list[tuple[str, str, dict[str, Any]]] = []
        for kind, field_name, spec in specs:
            every = max(1, int(spec.get("every_n_polls") or 1))
            phase = max(0, int(spec.get("phase") or 0))
            cache = self._optical_cache.setdefault(field_name, {})
            on_phase = (self._optical_poll - phase) % every == 0
            if slow_optical and every > 1:
                # Walking every column on the first poll of a V1600G1B takes
                # ~23 minutes, longer than its own poll interval.  Honour the
                # stagger from the very first poll instead; the remaining
                # columns fill over the next few polls.
                due = on_phase
            else:
                due = not cache or every <= 1 or on_phase
            # A vendor MAC+port table is the exact subscriber -> ONU map.  When
            # it is not persisted yet (first run after an upgrade), take one
            # snapshot immediately instead of waiting for the stagger phase.
            if (
                kind == "mac"
                and spec.get("port_oid")
                and not self._vendor_mac_map
            ):
                due = True
            if warm_grace and cache and every <= 1:
                due = False  # recent values from the previous run are shown
            if due:
                due_specs.append((kind, field_name, spec))
                continue
            for target, value in cache.items():
                if target in by_index:
                    if field_name.endswith("_power_dbm") and not _plausible_power(value):
                        continue
                    by_index[target][field_name] = value
                    filled += 1

        # One GETBULK request can advance several table columns at once.  The
        # VSOL agent returns ~8 rows per column for every response, so 8
        # columns complete in roughly the time one column used to take.
        oid_order: list[str] = []
        for _, _, spec in due_specs:
            for key in ("oid", "port_oid"):
                oid = str(spec.get(key) or "")
                if oid and oid not in oid_order:
                    oid_order.append(oid)

        values: dict[str, dict[str, Any]] = {}

        # A paired table (MAC + the PON:ONU port column, VLAN + the same port
        # column) is a *dynamic* vendor table: its row numbers are reused as
        # ONUs register and drop.  Walking the MAC column and the port column
        # in two separate requests can therefore read two different snapshots,
        # attaching one ONU's MAC to another ONU.  Fetch a pair in a single
        # interleaved GETBULK stream so both columns describe the same instant.
        pairs: list[tuple[str, str]] = []
        for _, _, spec in due_specs:
            oid = str(spec.get("oid") or "")
            port_oid = str(spec.get("port_oid") or "")
            if oid and port_oid and (oid, port_oid) not in pairs:
                pairs.append((oid, port_oid))
        for oid, port_oid in pairs:
            if oid in values and port_oid in values:
                continue
            result: dict[str, list[Any]] = {}
            if getattr(client, "version_code", 1) == 1 and hasattr(client, "walk_many"):
                try:
                    result = client.walk_many(
                        [oid, port_oid],
                        max_rows=max(2000, len(onus) * 4),
                        max_repetitions=min(
                            10, int(getattr(client, "max_repetitions", 10) or 10)
                        ),
                    )
                except (SnmpError, SnmpProtocolError):
                    result = {}
            for key in (oid, port_oid):
                if key in values:
                    continue
                rows = result.get(key) or []
                if rows:
                    values[key] = {
                        vb.oid[len(key) + 1:]: vb.value
                        for vb in rows
                        if not vb.is_exception
                    }

        for offset in range(0, len(oid_order), 8):
            chunk = oid_order[offset:offset + 8]
            batched: dict[str, list[Any]] = {}
            if (
                len(chunk) > 1
                and self.use_batch_walk
                and getattr(client, "version_code", 1) == 1
                and hasattr(client, "walk_many")
            ):
                try:
                    batched = client.walk_many(
                        chunk,
                        max_rows=max(1000, len(onus) * 4),
                        max_repetitions=min(
                            10, int(getattr(client, "max_repetitions", 10) or 10)
                        ),
                    )
                except SnmpError:
                    batched = {}
            for oid in chunk:
                if oid in values:
                    continue
                rows = batched.get(oid) or []
                if rows:
                    values[oid] = {
                        vb.oid[len(oid) + 1:]: vb.value
                        for vb in rows
                        if not vb.is_exception
                    }
                else:
                    values[oid] = self._walk_map(
                        client, oid, use_bulk=self.use_snmp_bulk_optical,
                        _inherit=False,
                    )

        mac_specs = [
            spec for kind, _, spec in due_specs
            if kind == "mac" and spec.get("port_oid")
        ]
        if mac_specs and any(values.get(str(spec["oid"])) for spec in mac_specs):
            # rebuilt below from this poll's consistent MAC+port snapshot
            self._vendor_mac_map = {}

        for kind, field_name, spec in due_specs:
            cache = self._optical_cache.setdefault(field_name, {})
            old = dict(cache)
            seen: set[int] = set()
            scale = _as_float(spec.get("scale"), 1.0) or 1.0
            oid = str(spec["oid"])
            for table_index, raw_value in values.get(oid, {}).items():
                target = resolve_row(table_index, spec)
                if target is None:
                    continue
                resolved_seen += 1
                row = by_index[target]
                if kind == "mac":
                    mac = _mac_of(raw_value)
                    if mac:
                        # Keep every MAC -> ONU link from this exact snapshot;
                        # the forwarding table is often wrong for these boxes.
                        if spec.get("port_oid"):
                            self._vendor_mac_map.setdefault(mac, target)
                        if not row.get("mac"):
                            row["mac"] = mac
                        cache.setdefault(target, mac)
                        seen.add(target)
                        filled += 1
                    continue
                if kind == "number":
                    value = _optical_number(raw_value, scale)
                    if value is None:
                        continue
                    # -1 A/C is this vendor's "no reading" placeholder, and a
                    # 0 reading on a down / unsupported ONU means the same
                    # thing.  Keep the last good value instead of showing 0.
                    if value == 0 and field_name in (
                        "rx_power_dbm", "tx_power_dbm", "temperature_c",
                        "voltage_v", "bias_current_ma",
                    ):
                        continue
                    if field_name == "temperature_c" and value <= -0.5:
                        continue
                    if field_name.endswith("_power_dbm") and not _plausible_power(value):
                        # BDCOM reports INT32_MIN for an SFP with no reading;
                        # multiplied by 0.1 this becomes -214748364.8 dBm and
                        # would raise a permanent critical alert.
                        cache.pop(target, None)
                        old.pop(target, None)
                        continue
                    if field_name == "distance_m" and value <= 0 and _as_int(row.get("oper_status")) != 1:
                        continue
                    value = round(value, 3)
                    row[field_name] = value
                    cache[target] = value
                    seen.add(target)
                    filled += 1
                    continue
                if field_name == "vlan":
                    number = _optical_number(raw_value, scale)
                    if number is None or number <= 0:
                        continue
                    text = str(int(number))
                else:
                    text = _text_of(raw_value)
                    if text is None:
                        continue
                row[field_name] = text
                cache[target] = text
                seen.add(target)
                filled += 1

            # keep the last good reading for an ONU whose row was missing in
            # this walk (device busy / table page lost)
            for target, value in old.items():
                if target not in seen and target in by_index:
                    if field_name.endswith("_power_dbm") and not _plausible_power(value):
                        continue
                    by_index[target][field_name] = value
                    filled += 1

        if mac_specs and self._vendor_mac_map:
            # Fold the authoritative snapshot into the FDB cache so it is used
            # by correlation and survives the next restart.
            self._mac_map.update(self._vendor_mac_map)
            self._save_fdb_cache()

        if due_specs and not index_map and not resolved_seen:
            return (
                "optical OIDs are set but no rows matched - the table index probably "
                "does not equal the interface index; set olt.onu_tables.key_oid"
            )
        return None


_DBM_IN_TEXT = re.compile(r"(-?\d+(?:\.\d+)?)\s*dBm", re.IGNORECASE)
_NUMBER_IN_TEXT = re.compile(r"(-?\d+(?:\.\d+)?)")


def _plausible_power(value: Any) -> bool:
    """Reject vendor sentinels such as INT32_MIN / 65535 from power tables."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return -70.0 <= number <= 30.0


def _optical_number(raw: Any, scale: float) -> float | None:
    """Convert an SNMP value to a number in dBm / °C / metres.

    Some OLTs return the power as text (``"0.04 mW (-14.31 dBm)"``), others as
    an integer in hundredths of a dBm (``-1431`` with ``scale: 0.01``).
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) * scale
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="ignore")
    text = str(raw)
    match = _DBM_IN_TEXT.search(text)
    if match:
        return float(match.group(1))
    match = _NUMBER_IN_TEXT.search(text)
    if match:
        return float(match.group(1)) * scale
    return None


def _text_of(value: Any) -> str | None:
    """Trim an SNMP string/number for use as inventory text.

    BDCOM pads several OCTET STRING columns with NUL bytes; leaving them in
    makes the model/vendor look empty or unsearchable in the web UI.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="ignore")
    text = str(value).replace("\x00", "").strip()
    return text or None


_PORT_RE = re.compile(
    r"(?:PON\s*)?(\d+)\s*[:/]\s*(?:ONU\s*)?(\d+)$", re.IGNORECASE
)


def _port_target(
    raw_port: Any, by_pon_onu: dict[tuple[int, int], int]
) -> int | None:
    """``PON3:ONU27`` / ``6:72`` -> the ONU interface index."""
    text = _text_of(raw_port)
    if not text or "GE" in text.upper() or "CPU" in text.upper():
        return None
    match = _PORT_RE.search(text)
    if not match:
        return None
    try:
        return by_pon_onu.get((int(match.group(1)), int(match.group(2))))
    except (TypeError, ValueError):
        return None


def _mac_of(value: Any) -> str | None:
    """MAC from an SNMP value, treating a bare 6-byte string as binary."""
    if isinstance(value, (bytes, bytearray)) and len(value) == 6:
        return ":".join(f"{b:02X}" for b in value)
    return to_mac(value)


def _dbm(value: Any) -> float | None:
    """Normalise an optical reading to dBm.

    Some CLIs print linear mW (``0.04``) instead of dBm (``-14.0``).  A received
    power below 1 mW is the common case, so anything in ``(0, 1)`` is converted;
    values already in the -40..+10 dBm range are left alone.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if 0 < number < 1:
        import math

        return round(10 * math.log10(number), 2)
    return round(number, 3)


def _oper_down(row: dict[str, Any]) -> bool:
    status = row.get("oper_status")
    return status is not None and int(status) != 1


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _blank_like(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """An empty row carrying the same columns as the rows already built.

    ``db.upsert_many`` takes its column list from the first row, so every row
    handed to it must have the same keys - a synthesised PON row therefore
    starts as a blank copy of a real one.
    """
    template = dict(rows[0]) if rows else {}
    return {key: None for key in template}


def _blank_onu_row(olt_name: str, pon: int, onu_id: int, stamp: str) -> dict[str, Any]:
    """A complete ``olt_onu_current`` row for a CLI-only ONU.

    BDPON / BTPON / PHOTON expose no ONU interfaces over SNMP, so their ONUs
    are discovered through Telnet.  Every column the regular SNMP path writes
    must be present here too: ``db.upsert_many`` uses the first row's keys.
    """
    return {
        "olt_name": olt_name,
        "ifindex": cdata_mod.onu_ifindex(pon, onu_id),
        "if_name": f"pon{pon}:{onu_id}",
        "if_descr": f"EPON ONU {pon}/{onu_id}",
        "pon": pon,
        "onu_id": onu_id,
        "admin_status": 1,
        "oper_status": None,
        "mac": None,
        "serial": None,
        "model": None,
        "vendor": None,
        "sw_version": None,
        "onu_type": None,
        "vlan": None,
        "rx_power_dbm": None,
        "tx_power_dbm": None,
        "olt_rx_power_dbm": None,
        "temperature_c": None,
        "voltage_v": None,
        "bias_current_ma": None,
        "distance_m": None,
        "hc_in_octets": None,
        "hc_out_octets": None,
        "in_bps": None,
        "out_bps": None,
        "last_down_reason": None,
        "last_down_at": None,
        "pppoe_user": None,
        "updated_at": stamp,
    }


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
