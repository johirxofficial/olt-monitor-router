"""OLT health telemetry: SNMP system metrics and the health score.

Two separate jobs live here:

* :func:`collect` reads uptime / CPU / memory / temperature over SNMP, using the
  vendor-neutral HOST-RESOURCES-MIB wherever the firmware implements it and an
  optional vendor OID map from the device config when it does not.
* :func:`score` turns every number the UI knows (online state, health metrics,
  PON state, ONU state) into one 0-100 health score plus the reasons behind it.

Nothing here raises: a metric that is not available is simply ``None``, and the
UI prints ``-`` instead of inventing a value.
"""

from __future__ import annotations

import time
from typing import Any

from . import vendors
from .logging_setup import get_logger
from .snmp import SnmpClient

log = get_logger("health")

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"

# how long a negative HOST-RESOURCES probe is trusted before it is retried
HR_RETRY_SECONDS = 3600.0

_MB = 1024 * 1024


def _ticks_to_seconds(value: Any) -> int | None:
    """TimeTicks (1/100 s) or a plain seconds counter -> seconds."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number // 100 if number > 100_000_000 else number


def _ram_usage(client: SnmpClient) -> tuple[float | None, float | None, float | None]:
    """``(used_percent, total_mb, free_mb)`` from the HOST-RESOURCES storage table."""
    oids = vendors.STANDARD_HEALTH_OIDS
    try:
        descriptions = client.walk_map(oids["hr_storage_descr"], max_rows=400)
        units = client.walk_map(oids["hr_storage_units"], max_rows=400)
        sizes = client.walk_map(oids["hr_storage_size"], max_rows=400)
        used = client.walk_map(oids["hr_storage_used"], max_rows=400)
    except Exception:  # pragma: no cover - the walk already swallows SNMP errors
        return None, None, None

    best: tuple[int, float, float, float] | None = None
    for index, description in descriptions.items():
        text = str(description or "").lower()
        if "ram" not in text and "memory" not in text and "dram" not in text:
            continue
        try:
            unit = int(units.get(index) or 0)
            size = int(sizes.get(index) or 0)
            in_use = int(used.get(index) or 0)
        except (TypeError, ValueError):
            continue
        if unit <= 0 or size <= 0:
            continue
        total_bytes = unit * size
        if best is None or total_bytes > best[1]:
            best = (index, total_bytes, unit * in_use, unit)
    if best is None:
        return None, None, None

    _index, total_bytes, used_bytes, _unit = best
    total_mb = round(total_bytes / _MB, 1)
    free_mb = round(max(0.0, (total_bytes - used_bytes) / _MB), 1)
    percent = round(used_bytes / total_bytes * 100, 1) if total_bytes else None
    return percent, total_mb, free_mb


def _cpu_load(client: SnmpClient, override: str | None = None) -> float | None:
    oid = override or vendors.STANDARD_HEALTH_OIDS["hr_processor_load"]
    try:
        values = client.walk_map(oid, max_rows=256)
    except Exception:  # pragma: no cover
        return None
    numbers: list[float] = []
    for value in values.values():
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            continue
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 1)


def collect(
    client: SnmpClient,
    device: dict[str, Any],
    info: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    prober: SnmpClient | None = None,
) -> dict[str, Any]:
    """Read the health metrics that this firmware exposes over SNMP.

    ``info`` is the collector's already-fetched scalar block (uptime + the round
    trip time of the request that brought it), ``state`` is a per-collector dict
    used to remember whether HOST-RESOURCES-MIB exists at all - probing an OLT
    that does not implement it costs seconds of SNMP timeouts, so the negative
    answer is cached (and retried once an hour) instead of paid on every poll.
    """
    result: dict[str, Any] = {
        "ok": False,
        "error": None,
        "uptime_seconds": None,
        "cpu_percent": None,
        "memory_percent": None,
        "memory_total_mb": None,
        "memory_free_mb": None,
        "temperature_c": None,
        "snmp_rtt_ms": None,
        "source": "snmp",
    }
    info = info or {}
    state = state if state is not None else {}

    result["snmp_rtt_ms"] = info.get("snmp_rtt_ms")
    result["uptime_seconds"] = info.get("uptime_seconds")

    oids = device.get("health_oids") or {}

    # ---- vendor OIDs (one GET each, so always safe to try) -------------
    if oids.get("cpu") or oids.get("cpu_scalar"):
        result["cpu_percent"] = _cpu_load(client, str(oids.get("cpu") or "") or None)
        if result["cpu_percent"] is None and oids.get("cpu_scalar"):
            try:
                value = client.get_value(str(oids["cpu_scalar"]))
                result["cpu_percent"] = float(value) if value is not None else None
            except (TypeError, ValueError):
                pass

    if oids.get("memory_percent"):
        try:
            value = client.get_value(str(oids["memory_percent"]))
            result["memory_percent"] = float(value) if value is not None else None
        except (TypeError, ValueError):
            pass

    if oids.get("temperature"):
        try:
            value = client.get_value(str(oids["temperature"]))
            if value is not None:
                temperature = float(value)
                # some firmware reports hundredths of a degree
                if temperature > 300:
                    temperature = temperature / 100
                result["temperature_c"] = round(temperature, 1)
        except (TypeError, ValueError):
            pass

    # ---- RFC 2790 HOST-RESOURCES-MIB (only if the device has it) -------
    supported = state.get("hr_supported")
    fresh = time.monotonic() - float(state.get("hr_checked_at") or 0) < HR_RETRY_SECONDS
    if not (supported is False and fresh):
        if supported is None or supported is False:
            probe = prober or client
            try:
                supported = probe.get_next(
                    vendors.STANDARD_HEALTH_OIDS["hr_processor_load"]
                ) is not None
            except Exception:  # pragma: no cover - get_next already swallows SNMP errors
                supported = False
            state["hr_supported"] = supported
            state["hr_checked_at"] = time.monotonic()
            if not supported:
                log.debug("%s: HOST-RESOURCES-MIB not implemented", device.get("name"))

        if supported:
            if result["cpu_percent"] is None:
                result["cpu_percent"] = _cpu_load(client)
            percent, total, free = _ram_usage(client)
            if result["memory_percent"] is None:
                result["memory_percent"] = percent
            result["memory_total_mb"] = total
            result["memory_free_mb"] = free
            if result["uptime_seconds"] is None:
                try:
                    ticks = client.get_value(vendors.STANDARD_HEALTH_OIDS["hr_system_uptime"])
                except Exception:
                    ticks = None
                result["uptime_seconds"] = _ticks_to_seconds(ticks)

    result["ok"] = any(
        result[field] is not None
        for field in ("uptime_seconds", "cpu_percent", "memory_percent", "temperature_c")
    )
    return result


# ---------------------------------------------------------------------------
#  health score
# ---------------------------------------------------------------------------
def score(
    *,
    online: bool,
    error: str | None = None,
    cpu_percent: float | None = None,
    memory_percent: float | None = None,
    temperature_c: float | None = None,
    onus_up: int = 0,
    onus_total: int = 0,
    pons_up: int = 0,
    pons_total: int = 0,
    has_metrics: bool = True,
) -> tuple[int, list[str]]:
    """A 0-100 health score and the reasons that lowered it.

    Offline is 0.  Otherwise start at 100 and subtract for heat, load, memory
    pressure, dead PON ports and offline ONUs.  The floor is 1 so an online OLT
    never looks identical to an unreachable one.
    """
    if not online:
        return 0, [error or "OLT is not responding"]

    reasons: list[str] = []
    value = 100.0

    if cpu_percent is not None:
        if cpu_percent >= 90:
            value -= 20
            reasons.append(f"CPU {cpu_percent:.0f}%")
        elif cpu_percent >= 80:
            value -= 10
            reasons.append(f"CPU {cpu_percent:.0f}%")

    if memory_percent is not None:
        if memory_percent >= 90:
            value -= 15
            reasons.append(f"memory {memory_percent:.0f}%")
        elif memory_percent >= 80:
            value -= 7
            reasons.append(f"memory {memory_percent:.0f}%")

    if temperature_c is not None:
        if temperature_c >= 70:
            value -= 20
            reasons.append(f"temperature {temperature_c:.0f} °C")
        elif temperature_c >= 60:
            value -= 10
            reasons.append(f"temperature {temperature_c:.0f} °C")

    if pons_total:
        down = max(0, pons_total - pons_up)
        value -= min(30, down * 4)
        if down:
            reasons.append(f"{down}/{pons_total} PON down")

    if onus_total:
        down = max(0, onus_total - onus_up)
        ratio = down / onus_total
        # 10% of ONUs offline should already be visible in the score
        value -= min(30, ratio * 100)
        if ratio >= 0.02:
            reasons.append(f"{down}/{onus_total} ONU down")

    if not has_metrics:
        value -= 5
        reasons.append("no health metrics available")

    return max(1, min(100, int(round(value)))), reasons


def grade(value: int) -> str:
    """Bootstrap colour name for a health score."""
    if value >= 90:
        return "success"
    if value >= 70:
        return "warning"
    if value >= 40:
        return "orange"
    return "danger"


def label(value: int) -> str:
    if value >= 95:
        return "Excellent"
    if value >= 70:
        return "Good"
    if value >= 40:
        return "Degraded"
    return "Critical"
