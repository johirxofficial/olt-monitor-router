"""MikroTik RouterOS collector.

Reads, over the RouterOS API:

* ``/ppp/active/print``            -> every online PPPoE user + uptime + counters
* ``/interface/print``             -> interface state and octet counters
* ``/interface/monitor-traffic``   -> live rx/tx bit rate for the uplinks
* ``/interface/ethernet/monitor``  -> SFP optical power in dBm (when supported)
* ``/ip/dhcp-server/lease``, ``/ip/arp`` -> IP -> MAC mapping used to link a
  PPPoE user to the ONU that belongs to the same subscriber
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..db import utcnow_iso
from ..logging_setup import get_logger
from ..mikrotik import RouterOSApi, RouterOSError, parse_int, parse_uptime
from ..mikrotik.api import parse_bool, parse_float
from ..snmp import format_uptime

log = get_logger("collector.router")


@dataclass
class RouterSnapshot:
    name: str
    ok: bool
    info: dict[str, Any] = field(default_factory=dict)
    users: list[dict[str, Any]] = field(default_factory=list)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    pppoe_servers: list[dict[str, Any]] = field(default_factory=list)
    mac_by_ip: dict[str, str] = field(default_factory=dict)
    error: str | None = None


class RouterCollector:
    def __init__(self, cfg: dict, device: dict | None = None) -> None:
        router = dict(device if device is not None else (cfg.get("router") or {}))
        self.cfg = router
        self.enabled = bool(router.get("enabled", True))
        self.name = str(router.get("name") or "MikroTik")
        self.host = str(router.get("host") or "")
        self.username = str(router.get("username") or "")
        self.password = str(router.get("password") or "")
        self.uplinks = [str(x) for x in (router.get("uplink_interfaces") or [])]
        self.sfp_monitor = bool(router.get("sfp_monitor", True))
        self.monitor_traffic = bool(router.get("monitor_traffic", True))
        self.want_pppoe = bool(router.get("pppoe", True))
        self.want_leases = bool(router.get("dhcp_leases", True))

        self._api: RouterOSApi | None = None
        self._last_counters: dict[str, tuple[float, int | None, int | None]] = {}
        self.stats = {"polls": 0, "failures": 0, "last_duration_ms": 0}

    # -- connection ------------------------------------------------------
    def _connect(self) -> RouterOSApi:
        # Keep a local reference: a device-list reload calls close() from
        # another thread and clears self._api.  Returning self._api could then
        # hand back None and crash the poll with "NoneType has no attribute".
        api = self._api
        if api is None:
            api = RouterOSApi(
                host=self.host,
                username=self.username,
                password=self.password,
                port=int(self.cfg.get("api_port") or 0) or None,
                use_tls=bool(self.cfg.get("use_tls", False)),
                verify_tls=bool(self.cfg.get("verify_tls", False)),
                timeout=float(self.cfg.get("timeout_seconds", 10)),
            )
            self._api = api
        api.ensure_connected()
        return api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None

    def ping(self) -> tuple[bool, str | None, dict[str, Any]]:
        """Try to connect, log in and read the identity.

        Returns ``(ok, error, details)``.  Used by the Devices page "Test"
        button, so it stays deliberately cheap.
        """
        try:
            api = self._connect()
            identity = api.identity()
            try:
                resource = api.resource()
            except RouterOSError:
                resource = {}
            details = {
                "identity": identity.get("name"),
                "version": resource.get("version"),
                "board_name": resource.get("board-name"),
                "architecture": resource.get("architecture-name"),
                "uptime": resource.get("uptime"),
                "host": self.host,
                "api_port": self.cfg.get("api_port"),
                "username": self.username,
            }
            return True, None, details
        except RouterOSError as exc:
            self.close()
            return False, str(exc), {}

    # -- main ------------------------------------------------------------
    def collect(self) -> RouterSnapshot:
        started = time.monotonic()
        if not self.enabled:
            return RouterSnapshot(self.name, False, error="router polling disabled in config")
        if not self.host:
            return RouterSnapshot(self.name, False, error="router.host is not configured")

        stamp = utcnow_iso()
        try:
            api = self._connect()
            identity = api.identity()
            resource = api.resource()
            interface_rows = api.interfaces()
            try:
                vlan_rows = api.vlans()
            except RouterOSError:
                vlan_rows = []
            try:
                server_rows = api.pppoe_servers()
            except RouterOSError:
                server_rows = []
        except RouterOSError as exc:
            self.stats["failures"] += 1
            self.close()
            log.warning("Router %s unreachable: %s", self.name, exc)
            return RouterSnapshot(self.name, False, error=str(exc))

        # ---- resource summary
        total_memory = parse_int(resource.get("total-memory")) or 0
        free_memory = parse_int(resource.get("free-memory")) or 0
        free_percent = (
            round(free_memory * 100.0 / total_memory, 1) if total_memory else None
        )
        uptime_seconds = parse_uptime(resource.get("uptime"))
        info: dict[str, Any] = {
            "identity": identity.get("name"),
            "version": resource.get("version"),
            "board_name": resource.get("board-name"),
            "architecture": resource.get("architecture-name"),
            "cpu_load": parse_float(resource.get("cpu-load")),
            "cpu_count": parse_int(resource.get("cpu-count")),
            "total_memory": total_memory,
            "free_memory": free_memory,
            "free_memory_percent": free_percent,
            "uptime_seconds": uptime_seconds,
            "uptime_text": format_uptime(uptime_seconds),
            "api_host": self.host,
            "api_port": self.cfg.get("api_port"),
        }

        # ---- interfaces
        vlan_by_iface: dict[str, int] = {}
        for row in vlan_rows:
            name = str(row.get("name") or "").strip()
            vlan_id = parse_int(row.get("vlan-id"))
            if name and vlan_id:
                vlan_by_iface[name] = vlan_id

        now = time.monotonic()
        interfaces: list[dict[str, Any]] = []
        for row in interface_rows:
            name = row.get("name")
            if not name:
                continue
            rx_byte = parse_int(row.get("rx-byte"))
            tx_byte = parse_int(row.get("tx-byte"))
            rx_bps, tx_bps = self._delta_rates(str(name), now, rx_byte, tx_byte)
            interfaces.append(
                {
                    "router_name": self.name,
                    "name": str(name),
                    "iface_type": row.get("type"),
                    "vlan_id": vlan_by_iface.get(str(name)),
                    "running": _as_int(parse_bool(row.get("running"))),
                    "disabled": _as_int(parse_bool(row.get("disabled"))),
                    "rx_byte": rx_byte,
                    "tx_byte": tx_byte,
                    "rx_bps": rx_bps,
                    "tx_bps": tx_bps,
                    "sfp_rx_power_dbm": None,
                    "sfp_tx_power_dbm": None,
                    "sfp_temperature_c": None,
                    "updated_at": stamp,
                }
            )

        # PPPoE servers: service-name -> bound interface.  A session does not
        # report its server, but this list lets the dashboard name the VLAN /
        # physical interface a subscriber arrives on.
        pppoe_servers: list[dict[str, Any]] = []
        for row in server_rows:
            iface = str(row.get("interface") or "").strip()
            service = str(row.get("service-name") or "").strip()
            if not iface:
                continue
            pppoe_servers.append(
                {
                    "service_name": service or None,
                    "interface": iface,
                    "profile": row.get("default-profile") or None,
                    "disabled": parse_bool(row.get("disabled")),
                    "vlan_id": vlan_by_iface.get(iface),
                }
            )
        info["pppoe_servers"] = pppoe_servers
        info["vlans"] = [
            {"name": name, "vlan_id": vlan_id}
            for name, vlan_id in sorted(vlan_by_iface.items(), key=lambda item: item[1])
        ]

        index_by_name = {row["name"]: row for row in interfaces}

        # ---- live traffic + SFP optics for the configured uplinks
        for uplink in self.uplinks:
            row = index_by_name.get(uplink)
            if row is None:
                continue
            if self.monitor_traffic:
                traffic = api.monitor_traffic(uplink)
                if traffic:
                    row["rx_bps"] = parse_float(traffic.get("rx-bps"), row["rx_bps"])
                    row["tx_bps"] = parse_float(traffic.get("tx-bps"), row["tx_bps"])
            if self.sfp_monitor:
                sfp = api.ethernet_monitor(uplink)
                if sfp:
                    row["sfp_rx_power_dbm"] = parse_float(sfp.get("sfp-rx-power"))
                    row["sfp_tx_power_dbm"] = parse_float(sfp.get("sfp-tx-power"))
                    row["sfp_temperature_c"] = parse_float(sfp.get("sfp-temperature"))
                    info[f"sfp_{uplink}"] = {
                        "vendor": sfp.get("sfp-vendor-name"),
                        "part": sfp.get("sfp-vendor-part-number"),
                        "type": sfp.get("sfp-type") or sfp.get("sfp-connector-type"),
                        "rx_power_dbm": row["sfp_rx_power_dbm"],
                        "tx_power_dbm": row["sfp_tx_power_dbm"],
                        "temperature_c": row["sfp_temperature_c"],
                    }

        # ---- PPPoE sessions
        users: list[dict[str, Any]] = []
        if self.want_pppoe:
            try:
                sessions = api.ppp_active()
            except RouterOSError as exc:
                log.warning("Router %s: /ppp/active/print failed: %s", self.name, exc)
                sessions = []
            for row in sessions:
                uptime = parse_uptime(row.get("uptime"))
                username = str(row.get("name") or "")
                bytes_in = parse_int(row.get("bytes-in"))
                bytes_out = parse_int(row.get("bytes-out"))
                # The dynamic interface is named <pppoe-username>; that is the
                # stable per-user interface on RouterOS 7.  The physical/VLAN
                # interface the session arrived on is filled in after
                # correlation (``access_iface``).
                session_iface = (
                    f"<pppoe-{username}>"
                    if username
                    else (row.get("<pppoe>") or row.get("interface") or row.get("service"))
                )
                # RouterOS 7 does not always report per-session byte counters.
                # The dynamic <pppoe-username> interface carries the same
                # information, so fall back to its octet counters.
                if bytes_in is None and bytes_out is None and username:
                    iface_name = f"<pppoe-{username}>"
                    iface_row = index_by_name.get(iface_name)
                    if iface_row is not None:
                        bytes_in = iface_row.get("rx_byte")
                        bytes_out = iface_row.get("tx_byte")
                users.append(
                    {
                        "router_name": self.name,
                        "name": username,
                        "service": row.get("service"),
                        "caller_id": (row.get("caller-id") or "").strip() or None,
                        "address": (row.get("address") or "").strip() or None,
                        "uptime_seconds": uptime,
                        "uptime_text": format_uptime(uptime),
                        "bytes_in": bytes_in,
                        "bytes_out": bytes_out,
                        "session_id": row.get("session-id"),
                        "iface": session_iface,
                        "olt_vlan": None,
                        "access_iface": None,
                        "access_source": None,
                        "onu_ifindex": None,
                        "onu_name": None,
                        "onu_olt_name": None,
                        "onu_pon": None,
                        "match_method": None,
                        "rx_power_dbm": None,
                        "tx_power_dbm": None,
                        "olt_rx_power_dbm": None,
                        "distance_m": None,
                        "updated_at": stamp,
                    }
                )

        # ---- IP -> MAC map (used to link a user to an ONU)
        mac_by_ip: dict[str, str] = {}
        if self.want_leases:
            for lease in api.dhcp_leases():
                ip = (lease.get("address") or "").strip()
                mac = (lease.get("mac-address") or "").strip()
                if ip and mac:
                    mac_by_ip[ip] = mac.upper()
        for entry in api.arp():
            ip = (entry.get("address") or "").strip()
            mac = (entry.get("mac-address") or "").strip()
            if ip and mac:
                mac_by_ip.setdefault(ip, mac.upper())

        self.stats["polls"] += 1
        self.stats["last_duration_ms"] = int((time.monotonic() - started) * 1000)
        log.info(
            "Router %s: %d interfaces, %d PPPoE users in %d ms",
            self.name, len(interfaces), len(users), self.stats["last_duration_ms"],
        )
        return RouterSnapshot(
            self.name, True, info=info, users=users, interfaces=interfaces,
            pppoe_servers=pppoe_servers, mac_by_ip=mac_by_ip,
        )

    def _delta_rates(
        self, name: str, now: float, rx_byte: int | None, tx_byte: int | None
    ) -> tuple[float | None, float | None]:
        previous = self._last_counters.get(name)
        self._last_counters[name] = (now, rx_byte, tx_byte)
        if not previous:
            return None, None
        prev_ts, prev_rx, prev_tx = previous
        elapsed = now - prev_ts
        if elapsed <= 0.2:
            return None, None

        def rate(cur: int | None, prev: int | None) -> float | None:
            if cur is None or prev is None or cur < prev:
                return None
            return (cur - prev) * 8.0 / elapsed

        return rate(rx_byte, prev_rx), rate(tx_byte, prev_tx)


def _as_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(value)
