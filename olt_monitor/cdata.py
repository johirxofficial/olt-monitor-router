"""C-Data OLT HTTP API client ("OLT Web Management System" firmware).

Some OLTs - the C-Data EPON/GPON family (``AC-FD1304E-B1``, ``FD11xx``, ...) -
publish almost nothing over SNMP: MIB-2 system + interfaces and a four-variable
private subtree, with **no ONU table at all**.  Everything the network operator
needs (ONU inventory, optical power, distance, last-down cause, CPU/RAM/board
temperature) lives in the JSON API that the OLT's own web UI calls::

    POST /cgi-bin/h.cgi?module=sys_login   {"Usrname": ..., "Password": md5hex}
    GET  /cgi-bin/h.cgi?module=<module>    header ``token: <token>``

Every reply is ``{"code": 0, "data": ...}``; a non-zero ``code`` carries a
human readable ``description``.

This module wraps the handful of modules the collector needs, hides two
firmware quirks (a hard 40 960-byte cap on a reply, and a last-down-cause enum)
and normalises everything into the same row shapes the SNMP collector produces
(``olt_onu_current`` / ``olt_port_current`` columns), so the web UI does not
care where the numbers came from.

Nothing here writes to the OLT: every call is a read (the module names used all
end in ``_get`` / ``_info`` / ``_status``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .logging_setup import get_logger
from .vendors import pon_number

log = get_logger("cdata")

#: the CGI caps a reply body at 40 KiB (longer ones are cut mid-JSON), so ONU
#: lists are paged.  A page size of 40 rows stays well under the cap; when a
#: reply still comes back truncated the page size is halved and retried.
DEFAULT_PAGE_SIZE = 40
MIN_PAGE_SIZE = 4

#: the device locks the account after two bad passwords, so a failed login is
#: cached and not retried for a while (the poller would otherwise lock it out)
LOGIN_RETRY_SECONDS = 300

#: ONUs that exist in the API but have no SNMP interface get a synthetic
#: ifindex (stable across polls) so links, history and PPPoE correlation work.
SYNTHETIC_IFINDEX_BASE = 700_000_000
SYNTHETIC_IFINDEX_STRIDE = 4096

#: enum -> the wording the rest of the app already uses for CLI last-down
LAST_DOWN_CAUSES: dict[int, str] = {0: "", 1: "LOS", 2: "dying-gasp"}

#: modules this client is allowed to call - a typo can never hit a write module
READ_MODULES = frozenset(
    {
        "sys_login",
        "sys_logout",
        "sys_dev_info",
        "sys_dev_mng",
        "sys_dev_status",
        "sys_version_info",
        "sys_onu_brief",
        "sys_ddm_info",
        "onu_list_get",
        "ont_optical_list_get",
        "ont_optical_get",
        "onu_stats_rate_get",
        "port_onu_auth_statitics",
        "mac_address_get",
        "epon_mac_address_get",
        "sys_alarm_active",
    }
)


class CDataError(RuntimeError):
    """Anything that stopped a C-Data API call (network, auth, bad reply)."""


# ---------------------------------------------------------------------------
#  small helpers
# ---------------------------------------------------------------------------
def onu_ifindex(pon: int, onu_id: int) -> int:
    """A stable synthetic ifindex for an ONU that has no SNMP interface."""
    return SYNTHETIC_IFINDEX_BASE + int(pon) * SYNTHETIC_IFINDEX_STRIDE + int(onu_id)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or math.isinf(number):  # NaN / inf
        return None
    return number


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    return text or None


def _iso(epoch: Any) -> str | None:
    """Epoch seconds from the OLT -> ISO-8601 UTC (empty/zero -> ``None``)."""
    seconds = _as_int(epoch)
    if not seconds or seconds <= 0:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  the API client
# ---------------------------------------------------------------------------
class CDataApi:
    """Minimal client for the OLT's ``cgi-bin/h.cgi`` JSON API.

    The instance keeps the session token and the failed-login time, so it is
    meant to live for the whole lifetime of a collector (one per OLT).
    """

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        *,
        port: int = 80,
        use_tls: bool = False,
        verify_tls: bool = True,
        timeout: float = 12.0,
    ) -> None:
        self.host = str(host or "").strip()
        self.username = str(username or "")
        self.password = str(password or "")
        self.port = int(port or (443 if use_tls else 80))
        self.use_tls = bool(use_tls)
        self.verify_tls = bool(verify_tls)
        self.timeout = float(timeout or 12.0)
        self._token: str | None = None
        self._login_failed_at = 0.0
        self._login_error: str | None = None
        self.calls = 0
        self.duration_ms = 0

    # -- plumbing --------------------------------------------------------
    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_tls else "http"
        default = 443 if self.use_tls else 80
        host = self.host
        if self.port and self.port != default:
            return f"{scheme}://{host}:{self.port}"
        return f"{scheme}://{host}"

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.use_tls:
            return None
        if self.verify_tls:
            return ssl.create_default_context()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _call(
        self,
        module: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        with_token: bool = True,
    ) -> Any:
        """POST/GET one module and return its ``data`` (raises :class:`CDataError`)."""
        if module not in READ_MODULES:
            raise CDataError(f"{module!r} is not an allow-listed read module")
        if not self.host:
            raise CDataError("host is not configured")

        url = f"{self.base_url}/cgi-bin/h.cgi?module={urllib.parse.quote(module)}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "&" + urllib.parse.urlencode(clean)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=payload, method="POST" if payload is not None else "GET"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "olt-monitor-router")
        if with_token and self._token:
            request.add_header("token", self._token)

        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request, timeout=float(timeout or self.timeout), context=self._ssl_context()
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise CDataError(f"{module}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise CDataError(f"{module}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise CDataError(f"{module}: {type(exc).__name__}: {exc}") from exc
        finally:
            self.calls += 1
            self.duration_ms = int((time.monotonic() - started) * 1000)

        text = raw.decode("utf-8", "replace")
        try:
            reply = json.loads(text)
        except ValueError:
            # the device cuts a reply at 40 KiB - the caller shops smaller pages
            raise CDataError(f"{module}: reply was truncated ({len(raw)} bytes)") from None
        if not isinstance(reply, dict):
            raise CDataError(f"{module}: unexpected reply type {type(reply).__name__}")

        code = reply.get("code")
        if code not in (0, None):
            description = str(reply.get("description") or "").strip()
            if code == 2 and with_token:
                # session expired: drop the token so the next call logs in again
                self._token = None
            raise CDataError(f"{module}: {description or f'code {code}'}")
        return reply.get("data")

    # -- session ---------------------------------------------------------
    def login(self, *, force: bool = False) -> str:
        """Log in if needed and return the session token."""
        if self._token and not force:
            return self._token
        if not self.username:
            raise CDataError("no API username configured")
        if (
            not force
            and self._login_error
            and (time.monotonic() - self._login_failed_at) < LOGIN_RETRY_SECONDS
        ):
            raise CDataError(f"login disabled after a failure ({self._login_error})")

        body = {
            "Usrname": self.username,
            "Password": hashlib.md5(self.password.encode("utf-8")).hexdigest(),
        }
        try:
            data = self._call("sys_login", body=body, with_token=False)
        except CDataError as exc:
            # remember the failure: the OLT locks the account after two attempts
            self._login_failed_at = time.monotonic()
            self._login_error = str(exc)
            raise
        token = str((data or {}).get("token") or "") if isinstance(data, dict) else ""
        if not token:
            self._login_failed_at = time.monotonic()
            self._login_error = "no token in the reply"
            raise CDataError("login returned no token")
        self._token = token
        self._login_error = None
        return token

    def logout(self) -> None:
        if not self._token:
            return
        try:
            self._call("sys_logout", with_token=True)
        except CDataError:
            pass
        self._token = None

    # -- paging ----------------------------------------------------------
    def _page_all(self, module: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Read every row of a paged module, shrinking the page if truncated."""
        rows: list[dict] = []
        page = 1
        size = DEFAULT_PAGE_SIZE
        guard = 0
        while True:
            guard += 1
            if guard > 400:
                break
            query = dict(params or {})
            query.update({"PageNumber": page, "PageSize": size})
            try:
                data = self._call(module, params=query)
            except CDataError as exc:
                if "truncated" in str(exc) and size > MIN_PAGE_SIZE:
                    size = max(MIN_PAGE_SIZE, size // 2)
                    continue
                raise
            if not isinstance(data, dict):
                break
            items = data.get("list")
            if not isinstance(items, list):
                break
            rows.extend(item for item in items if isinstance(item, dict))
            total = _as_int(data.get("PageTotal")) or 0
            if not items or len(items) < size or (total and len(rows) >= total):
                break
            page += 1
        return rows

    # -- read helpers ----------------------------------------------------
    def device_info(self) -> dict[str, Any]:
        data = self._call("sys_dev_info")
        return data if isinstance(data, dict) else {}

    def device_health(self) -> dict[str, Any]:
        data = self._call("sys_dev_mng")
        return data if isinstance(data, dict) else {}

    def port_status(self) -> list[dict[str, Any]]:
        """Every physical port with its API index (``epon 0/1/1`` -> 17825793)."""
        data = self._call("sys_dev_status")
        out: list[dict[str, Any]] = []
        if isinstance(data, dict):
            for group in ("geFiber", "geCopper", "xge", "gpon", "epon"):
                entries = data.get(group)
                if isinstance(entries, list):
                    out.extend(e for e in entries if isinstance(e, dict))
        return out

    def onus(self) -> list[dict[str, Any]]:
        """The full ONU inventory (``onu_list_get``), every PON, every ONU."""
        return self._page_all("onu_list_get")

    def optical(self, pon_id: str) -> list[dict[str, Any]]:
        """Per-ONU optical readings for one PON (slow: the OLT asks each ONU)."""
        return self._page_all("ont_optical_list_get", {"PonId": pon_id})

    def pon_summary(self, index: int) -> dict[str, Any]:
        data = self._call("sys_onu_brief", params={"Index": int(index)})
        return data if isinstance(data, dict) else {}

    def ddm(self, index: int) -> dict[str, Any]:
        """OLT-side SFP DDM (tx/rx/temp) of one PON port."""
        data = self._call("sys_ddm_info", params={"Index": int(index)})
        return data if isinstance(data, dict) else {}

    def onu_rates(self, index: int) -> dict[str, dict[str, float]]:
        """Live per-ONU Up/Down rate of one PON -> ``{onu_id: {"in_bps",...}}``."""
        data = self._call("onu_stats_rate_get", params={"PortIndex": int(index)})
        out: dict[str, dict[str, float]] = {}
        entries = (data or {}).get("OnuRateInfo") if isinstance(data, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            onu_id = str(entry.get("onuId") or "").strip()
            kind = str(entry.get("type") or "").lower()
            value = _as_float(entry.get("value"))
            if not onu_id or value is None:
                continue
            bucket = out.setdefault(onu_id, {})
            # the web UI prints kbit/s; our DB columns are bits per second
            if kind == "up":
                bucket["out_bps"] = round(value * 1000.0, 2)
            elif kind == "down":
                bucket["in_bps"] = round(value * 1000.0, 2)
        return out

    def mac_table(self) -> tuple[dict[str, int], dict[str, int]]:
        """Subscriber MAC -> (ONU ifindex, service VLAN) from the per-PON FDB.

        The C-Data ONU list carries the ONU's *own* MAC, but a PPPoE session's
        ``caller-id`` is the customer router behind the ONU.  ``epon_mac_
        address_get`` is the forwarding table the OLT's own web UI shows and
        returns ``OnuId`` for each learned MAC, which is exactly the missing
        join key.  It needs the PON API index (the same value as
        ``sys_dev_status`` -> ``Index``), so both indexes are walked.

        Returns ``(mac_to_ifindex, mac_to_vlan)``.  The VLAN is the service
        VLAN of the MAC entry and lets the dashboard name the MikroTik VLAN
        interface the session arrived on.
        """
        macs: dict[str, int] = {}
        vlans: dict[str, int] = {}

        try:
            ports = self.port_status()
        except CDataError:
            return macs, vlans

        for port in ports:
            name = _clean(port.get("Name")) or ""
            lowered = name.lower()
            if "epon" not in lowered and "gpon" not in lowered:
                continue
            index = _as_int(port.get("Index"))
            if index is None:
                continue

            page = 1
            page_size = 200
            seen = 0
            while page <= 200:
                try:
                    data = self._call(
                        "epon_mac_address_get",
                        params={
                            "PortIndex": index,
                            "PageNumber": page,
                            "PageSize": page_size,
                        },
                    )
                except CDataError as exc:
                    # the CGI cuts replies at 40 KiB; shrink the page and retry
                    if "truncated" in str(exc) and page_size > 20:
                        page_size = max(20, page_size // 2)
                        continue
                    log.info("C-Data MAC table for %s failed: %s", name, exc)
                    break

                rows = (data or {}).get("MacArray") if isinstance(data, dict) else None
                if not isinstance(rows, list) or not rows:
                    break
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    mac = _clean(row.get("MacAddress"))
                    onu_id = _as_int(row.get("OnuId"))
                    if not mac or onu_id is None:
                        continue
                    port_name = _clean(row.get("PortIndex")) or name
                    pon = pon_number(port_name) or pon_number(name)
                    if pon is None:
                        continue
                    mac = mac.upper().replace("-", ":")
                    macs.setdefault(mac, onu_ifindex(pon, onu_id))
                    vlan = _as_int(row.get("Vlan"))
                    if vlan is None:
                        vlan = _as_int(row.get("VlanId"))
                    if vlan and 0 < vlan < 4095:
                        vlans.setdefault(mac, vlan)

                seen += len(rows)
                total = _as_int(data.get("TotalNum")) if isinstance(data, dict) else None
                if len(rows) < page_size or (total is not None and seen >= total):
                    break
                page += 1

        return macs, vlans


# ---------------------------------------------------------------------------
#  mapping: API JSON -> the rows the rest of the app already understands
# ---------------------------------------------------------------------------
@dataclass
class CDataResult:
    """Everything one C-Data API poll produced."""

    ok: bool = False
    error: str | None = None
    note: str | None = None
    info: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    onus: list[dict[str, Any]] = field(default_factory=list)
    pons: list[dict[str, Any]] = field(default_factory=list)
    mac_map: dict[str, int] = field(default_factory=dict)
    mac_vlans: dict[str, int] = field(default_factory=dict)
    optical_ok: bool = False


def _onu_row(
    olt_name: str,
    raw: dict[str, Any],
    optical: dict[str, Any] | None,
    rates: dict[str, float] | None,
) -> dict[str, Any] | None:
    """One ``onu_list_get`` entry (+ optical/rate) -> an ``olt_onu_current`` row."""
    pon_id = _clean(raw.get("PonId"))
    onu_id = _as_int(raw.get("OnuId"))
    if not pon_id or onu_id is None:
        return None
    pon = pon_number(pon_id)
    if pon is None:
        return None

    running = _as_int(raw.get("RunningState"))
    online = running == 1
    cause = _as_int(raw.get("LastDownCause"))
    reason = LAST_DOWN_CAUSES.get(cause or 0) or None

    row: dict[str, Any] = {
        "olt_name": olt_name,
        "ifindex": onu_ifindex(pon, onu_id),
        "if_name": f"epon {pon_id}:{onu_id}",
        "if_descr": _clean(raw.get("OnuDesc")) or _clean(raw.get("OnuName")),
        "pon": pon,
        "onu_id": onu_id,
        "admin_status": 1 if (_as_int(raw.get("ControlFlag")) or 0) == 1 else 2,
        "oper_status": 1 if online else 2,
        "mac": _clean(raw.get("Mac")),
        "serial": None,
        "model": _clean(raw.get("EquipId")),
        "vendor": _clean(raw.get("OnuVendor")),
        "sw_version": _clean(raw.get("SwVersion")),
        "onu_type": None,
        "vlan": None,
        "rx_power_dbm": None,
        "tx_power_dbm": None,
        "olt_rx_power_dbm": None,
        "temperature_c": None,
        "voltage_v": None,
        "bias_current_ma": None,
        "distance_m": None,
        # a down cause only means something while the ONU is down
        "last_down_reason": None if online else reason,
        "last_down_at": None if online else _iso(raw.get("LastDownTime")),
        "hc_in_octets": None,
        "hc_out_octets": None,
        "in_bps": None,
        "out_bps": None,
        "pppoe_user": None,
    }

    if optical:
        row["rx_power_dbm"] = _as_float(optical.get("RxPower"))
        row["tx_power_dbm"] = _as_float(optical.get("TxPower"))
        row["olt_rx_power_dbm"] = _as_float(optical.get("OltRxPower"))
        row["temperature_c"] = _as_float(optical.get("Temperature"))
        row["voltage_v"] = _as_float(optical.get("Voltage"))
        row["bias_current_ma"] = _as_float(optical.get("BiasCurrent"))
        distance = _as_float(optical.get("Range"))
        row["distance_m"] = distance if distance and distance > 0 else None
        if not row["mac"]:
            row["mac"] = _clean(optical.get("Mac"))

    if rates:
        row["in_bps"] = rates.get("in_bps")
        row["out_bps"] = rates.get("out_bps")

    return row


def collect(
    api: CDataApi,
    olt_name: str,
    *,
    optical: bool = True,
    rates: bool = True,
    mac: bool = True,
) -> CDataResult:
    """Poll one C-Data OLT over its HTTP API.

    Never raises for a single failing sub-call: a missing optical table or a
    refused DDM read only leaves those columns empty, the inventory still comes
    through.  A failed *login* is the one hard error (it is reported in
    ``result.error``) because there is nothing to show without it.
    """
    result = CDataResult()
    started = time.monotonic()

    try:
        api.login()
    except CDataError as exc:
        result.error = str(exc)
        return result

    try:
        info = api.device_info()
    except CDataError as exc:
        result.error = f"device info failed: {exc}"
        return result

    uptime_ms = _as_int(info.get("Uptime")) or 0
    result.info = {
        "cdata_api": True,
        "sys_name": _clean(info.get("SysName")),
        "model": _clean(info.get("Model")),
        "firmware": _clean(info.get("Firmware")),
        "hardware": _clean(info.get("Hardware")),
        "serial": _clean(info.get("SnNum")),
        "device_type": _clean(info.get("DevType")),
        "mac": _clean(info.get("DevMacAddr")),
        "uptime_seconds": (uptime_ms // 1000) if uptime_ms > 0 else None,
    }

    # ---- health: the API is the only source of CPU/RAM/temperature ---------
    try:
        management = api.device_health()
    except CDataError as exc:
        management = {}
        result.note = f"device health failed: {exc}"
    temperature = management.get("Temp")
    if isinstance(temperature, dict):
        temperature = temperature.get("Temperature")
    result.health = {
        "ok": True,
        "source": "cdata-api",
        "uptime_seconds": result.info.get("uptime_seconds"),
        "cpu_percent": _as_float(management.get("CpuUsage")),
        "memory_percent": _as_float(management.get("RamUsage")),
        "temperature_c": _as_float(temperature),
    }

    # ---- port index per PON (needed by the per-port modules) --------------
    index_by_pon_id: dict[str, int] = {}
    port_status: dict[str, int | None] = {}
    try:
        for port in api.port_status():
            name = _clean(port.get("Name")) or ""
            if "epon" not in name.lower() and "gpon" not in name.lower():
                continue
            match = re.search(r"(\d+(?:/\d+)+)\s*$", name)
            if not match:
                continue
            pon_id = match.group(1)
            index = _as_int(port.get("Index"))
            if index is not None:
                index_by_pon_id[pon_id] = index
            port_status[pon_id] = _as_int(port.get("Status"))
    except CDataError as exc:
        result.note = f"port list failed: {exc}"

    # ---- ONU inventory ----------------------------------------------------
    try:
        raw_onus = api.onus()
    except CDataError as exc:
        result.error = f"ONU list failed: {exc}"
        return result

    pon_ids: list[str] = []
    for entry in raw_onus:
        pon_id = _clean(entry.get("PonId"))
        if pon_id and pon_id not in pon_ids:
            pon_ids.append(pon_id)
    for pon_id in index_by_pon_id:
        if pon_id not in pon_ids:
            pon_ids.append(pon_id)
    pon_ids.sort()

    # ---- per-PON: optical, SFP DDM, live rates ----------------------------
    optics: dict[tuple[str, int], dict[str, Any]] = {}
    rate_by_pon: dict[str, dict[str, dict[str, float]]] = {}
    optics_failed: list[str] = []
    if optical:
        for pon_id in pon_ids:
            try:
                for entry in api.optical(pon_id):
                    onu_id = _as_int(entry.get("OnuId"))
                    if onu_id is not None:
                        optics[(pon_id, onu_id)] = entry
            except CDataError as exc:
                optics_failed.append(pon_id)
                log.info("C-Data %s: optical for PON %s failed: %s", olt_name, pon_id, exc)
    result.optical_ok = bool(optics) or not optical

    for pon_id in pon_ids:
        index = index_by_pon_id.get(pon_id)
        pon = pon_number(pon_id)
        if pon is None or index is None:
            continue
        if rates:
            try:
                rate_by_pon[pon_id] = api.onu_rates(index)
            except CDataError as exc:
                log.info("C-Data %s: rates for PON %s failed: %s", olt_name, pon_id, exc)
        try:
            summary = api.pon_summary(index)
        except CDataError:
            summary = {}
        try:
            ddm = api.ddm(index)
        except CDataError:
            ddm = {}
        ddm_ok = bool(ddm.get("Status"))
        result.pons.append(
            {
                "pon": pon,
                "pon_id": pon_id,
                "index": index,
                "raw_status": _as_int(port_status.get(pon_id)),
                "onus_total": _as_int(summary.get("Total")),
                "onus_registered": _as_int(summary.get("Register")),
                "onus_online": _as_int(summary.get("Online")),
                "sfp_tx_power_dbm": _as_float(ddm.get("TxPower")) if ddm_ok else None,
                "sfp_rx_power_dbm": _as_float(ddm.get("RxPower")) if ddm_ok else None,
                "sfp_temperature_c": _as_float(ddm.get("Temp")) if ddm_ok else None,
            }
        )

    # ---- ONU rows ---------------------------------------------------------
    for entry in raw_onus:
        pon_id = _clean(entry.get("PonId"))
        onu_id = _as_int(entry.get("OnuId"))
        if not pon_id or onu_id is None:
            continue
        row = _onu_row(
            olt_name,
            entry,
            optics.get((pon_id, onu_id)),
            (rate_by_pon.get(pon_id) or {}).get(str(onu_id)),
        )
        if row is not None:
            result.onus.append(row)

    # ---- subscriber forwarding table --------------------------------------
    # This is what links a PPPoE caller-id to an ONU.  It is comparatively
    # slow (one request per PON), so it shares the API cadence with optical.
    if mac:
        mac_table = getattr(api, "mac_table", None)
        if callable(mac_table):
            try:
                result.mac_map, result.mac_vlans = mac_table()
            except CDataError as exc:
                result.note = (
                    (result.note + "; " if result.note else "")
                    + f"MAC table failed: {exc}"
                )

    if optics_failed:
        result.note = (
            (result.note + "; " if result.note else "")
            + "ONU optical power unavailable on PON " + ", ".join(optics_failed)
        )
    log.info(
        "C-Data %s: api %d calls in %.1fs -> %d ONUs, %d PONs, %d MACs",
        olt_name, api.calls, time.monotonic() - started, len(result.onus),
        len(result.pons), len(result.mac_map),
    )
    result.ok = True
    return result


def probe(device: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Connectivity test behind the "Test API" button on the Devices page.

    Logs in, reads the device identity and the port list, and reports the ONU
    count per PON without touching optical power (that part is slow).  Returns
    ``(ok, message, details)`` exactly like the Telnet probe, so the same
    JavaScript can show either result.
    """
    host = str(device.get("host") or "").strip()
    if not host:
        return False, "host is not set", {}

    api = CDataApi(
        host=host,
        username=str(device.get("cdata_api_username") or ""),
        password=str(device.get("cdata_api_password") or ""),
        port=int(device.get("cdata_api_port") or 80),
        use_tls=bool(device.get("cdata_api_tls")),
        verify_tls=bool(device.get("cdata_api_verify_tls", True)),
        timeout=float(device.get("cdata_api_timeout_seconds") or 12),
    )

    started = time.monotonic()
    try:
        api.login(force=True)
        info = api.device_info()
        try:
            onus = api.onus()
        except CDataError:
            onus = []
    except CDataError as exc:
        return (
            False,
            f"{exc}  (careful: the OLT locks the account after two bad passwords)",
            {},
        )
    finally:
        api.logout()

    model = _clean(info.get("Model")) or "?"
    firmware = _clean(info.get("Firmware")) or "?"
    device_type = _clean(info.get("DevType")) or "OLT"
    sys_name = _clean(info.get("SysName")) or ""
    pon_ids = sorted({str(o.get("PonId")) for o in onus if o.get("PonId")})

    message = (
        f"{device_type} {model}, firmware {firmware}"
        + (f", hostname {sys_name}" if sys_name else "")
        + f" - {len(onus)} ONU(s) on {len(pon_ids)} PON port(s)"
        + f" [{time.monotonic() - started:.1f}s]"
    )
    details = {
        "model": model,
        "firmware": firmware,
        "hardware": _clean(info.get("Hardware")),
        "serial": _clean(info.get("SnNum")),
        "hostname": sys_name,
        "device_type": device_type,
        "mac": _clean(info.get("DevMacAddr")),
        "onus": len(onus),
        "pons": pon_ids,
    }
    return True, message, details
