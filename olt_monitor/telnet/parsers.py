"""Parsers for OLT CLI output.

The commands differ per vendor, but the *shape* of the output does not: every
OLT prints a fixed-width table with a header, or ``key: value`` lines.  Rather
than one parser per firmware, this module has three generic primitives and a
thin layer per metric:

* :func:`parse_table` - a fixed-width table -> list of dicts (header driven,
  with a positional fallback when a cell is empty)
* :func:`parse_key_values` - ``Label: value`` lines -> dict
* :func:`find_column` / :func:`cell` - case-insensitive column helpers

Everything is tolerant: unrecognised output yields ``None`` / ``{}`` instead of
raising, so a firmware quirk can never stop the poller.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

# ---------------------------------------------------------------------------
#  generic table parsing
# ---------------------------------------------------------------------------
_SEPARATOR = re.compile(r"^[\s\-=+_|]+$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _split_cells(line: str) -> list[tuple[int, str]]:
    """Split on 2+ spaces, keeping each cell's start column."""
    cells: list[tuple[int, str]] = []
    position = 0
    for match in re.finditer(r"\s{2,}", line):
        cells.append((position, line[position:match.start()]))
        position = match.end()
    cells.append((position, line[position:]))
    return [(start, text.strip()) for start, text in cells]


def _looks_like_header(line: str) -> bool:
    text = line.strip()
    if len(text) < 6 or not re.search(r"[A-Za-z]", text):
        return False
    if _SEPARATOR.match(text):
        return False
    cells = [c for _, c in _split_cells(text) if c]
    return len(cells) >= 2


def parse_table(text: str, *, min_columns: int = 2) -> tuple[list[str], list[dict[str, str]]]:
    """Parse a fixed-width CLI table into ``(headers, rows)``.

    The header is the first line that splits into at least *min_columns* cells
    and contains letters; a line of dashes right below it is skipped as a
    separator.  Data rows are aligned to the header by character position, so a
    row with an empty cell still lines up.
    """
    lines = [line.rstrip() for line in str(text or "").replace("\r", "").split("\n")]
    if not lines:
        return [], []

    header_index = -1
    for index, line in enumerate(lines):
        if not _looks_like_header(line):
            continue
        cells = [c for _, c in _split_cells(line) if c]
        if len(cells) >= max(2, min_columns):
            header_index = index
            break
    if header_index < 0:
        return [], []

    header_line = lines[header_index]
    header_cells = _split_cells(header_line)
    headers = [name for _, name in header_cells if name]
    if len(headers) < max(2, min_columns):
        return [], []
    starts = [start for start, name in header_cells if name]

    # a row is data when it has content, a number, and is not a separator/echo
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 1:]:
        if not line.strip() or _SEPARATOR.match(line.strip()):
            continue
        if line.lstrip().startswith(("---", "===")):
            continue
        if not _NUMBER.search(line):
            continue

        parts = _split_cells(line)
        values = [text for _, text in parts]
        # fast path: same number of cells -> direct mapping
        if len(values) == len(headers):
            rows.append(dict(zip(headers, values)))
            continue
        # positional fallback: slice the raw line at header midpoints
        row: dict[str, str] = {}
        for i, name in enumerate(headers):
            left = 0 if i == 0 else (starts[i - 1] + starts[i]) // 2
            right = len(line) if i + 1 >= len(starts) else (starts[i] + starts[i + 1]) // 2
            row[name] = line[left:right].strip()
        if any(row.values()):
            rows.append(row)
    return headers, rows


def find_column(headers: Iterable[str], *needles: str) -> str | None:
    """First header containing any of *needles* (case-insensitive)."""
    lowered = [str(n).lower() for n in needles]
    for header in headers:
        text = str(header).lower()
        if all(n in text for n in lowered):
            return header
    return None


def _column(headers: Iterable[str], *needles: str) -> str | None:
    """Header that contains *any* of the needles."""
    for header in headers:
        text = str(header).lower()
        for needle in needles:
            if needle in text:
                return header
    return None


def number(value: Any) -> float | None:
    """First number in a cell (``"-18.32 dBm"`` -> ``-18.32``)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value))
    return float(match.group()) if match else None


def percentage(value: Any) -> float | None:
    """``"43%"`` -> 43.0 ; ``"0/5"`` -> 0.0 (the first number)."""
    return number(value)


def _first_row_value(rows: list[dict[str, str]], *needles: str) -> str | None:
    for row in rows:
        for key, value in row.items():
            if any(needle in key.lower() for needle in needles) and str(value).strip():
                return str(value).strip()
    return None


# ---------------------------------------------------------------------------
#  key: value output
# ---------------------------------------------------------------------------
_KV = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _/()\-.\[\]]{1,48}?)\s*[:=]\s*(.+?)\s*$")


def parse_key_values(text: str) -> dict[str, str]:
    """``Label: value`` lines -> ``{"label": "value"}`` (labels lower-cased)."""
    out: dict[str, str] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _KV.match(line)
        if not match:
            continue
        label = match.group(1).strip().lower()
        value = match.group(2).strip()
        if value and not value.startswith("----"):
            out.setdefault(label, value)
    return out


def kv_value(values: dict[str, str], *needles: str) -> str | None:
    for key, value in values.items():
        if any(needle in key for needle in needles) and value:
            return value
    return None


# ---------------------------------------------------------------------------
#  metrics
# ---------------------------------------------------------------------------
def parse_cpu(text: str) -> float | None:
    """CPU load in percent from ``show cpu``-style output."""
    lines = str(text or "").splitlines()
    # prefer a line that mentions CPU *and* a percentage
    for line in lines:
        lowered = line.lower()
        if "cpu" not in lowered:
            continue
        values = re.findall(r"(\d+(?:\.\d+)?)\s*%", line)
        if values:
            return float(values[0])
    # then any percentage at all
    values = re.findall(r"(\d+(?:\.\d+)?)\s*%", str(text or ""))
    if values:
        return float(values[0])
    # last resort: a "CPU" row in a table
    headers, rows = parse_table(text)
    if rows:
        for row in rows:
            joined = " ".join(str(v) for v in row.values()).lower()
            if "cpu" in joined:
                found = number(row.get(headers[-1], ""))
                if found is not None:
                    return found
    return None


def parse_memory(text: str) -> dict[str, float | None]:
    """Memory usage: percent plus total/used/free in MB when printed."""
    raw = str(text or "")
    out: dict[str, float | None] = {
        "used_percent": None,
        "total_mb": None,
        "used_mb": None,
        "free_mb": None,
    }

    lowered = raw.lower()
    for line in raw.splitlines():
        low = line.lower()
        if "mem" in low or "ram" in low:
            values = re.findall(r"(\d+(?:\.\d+)?)\s*%", line)
            if values:
                out["used_percent"] = float(values[0])
                break

    def grab(labels: tuple[str, ...]) -> float | None:
        pattern = r"(?:" + "|".join(labels) + r")\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(GB|MB|KB|G|M|K)?"
        match = re.search(pattern, lowered, re.IGNORECASE)
        if not match:
            return None
        value = float(match.group(1))
        unit = (match.group(2) or "mb").lower()
        if unit.startswith("g"):
            value *= 1024
        elif unit.startswith("k"):
            value /= 1024
        return round(value, 1)

    out["total_mb"] = grab(("total memory", "total", "memory total", "dram total"))
    out["used_mb"] = grab(("used memory", "used", "memory used", "dram used"))
    out["free_mb"] = grab(("free memory", "free", "memory free", "dram free"))

    # "Total memory:242MB Free memory:124MB Utilization:49%" (BDPON / BTPON /
    # PHOTON and many C-Data clones) has no "used" line at all.
    if out["used_mb"] is None and out["total_mb"] and out["free_mb"]:
        out["used_mb"] = round(out["total_mb"] - out["free_mb"], 1)
    if out["free_mb"] is None and out["total_mb"] and out["used_mb"]:
        out["free_mb"] = round(out["total_mb"] - out["used_mb"], 1)
    if out["used_percent"] is None and out["total_mb"] and out["used_mb"]:
        out["used_percent"] = round(out["used_mb"] / out["total_mb"] * 100, 1)
    return out


def parse_temperature(text: str) -> float | None:
    """Board temperature in degrees C.

    BDCOM prints the alarm thresholds before the reading::

        upper temperature threshold is 75 degree
        low temperature threshold is -15 degree
        current temperature is 50 degree

    so the "current" line is preferred - otherwise the upper threshold (75)
    would be reported as the board temperature.
    """
    raw = str(text or "")
    # C-Data prints a per-slot table:
    #     Slot  current
    #     0     44.50
    #     1     44.50
    # There is no "temperature" word on the rows, so handle the header first.
    lines = raw.replace("\r", "").split("\n")
    for index, line in enumerate(lines):
        low = line.lower()
        if "slot" in low and "current" in low:
            readings: list[float] = []
            for row in lines[index + 1:]:
                match = re.match(r"^\s*\d+\s+(-?\d+(?:\.\d+)?)\s*$", row)
                if match:
                    readings.append(float(match.group(1)))
                elif row.strip("- ") == "":
                    continue
                else:
                    break
            if readings:
                return max(readings)
    for pattern in (
        r"(?:current|now|board|system|inlet|outlet)\s+temperature[^0-9\n-]*(-?\d+(?:\.\d+)?)",
        r"temperature\s*(?:is|:|=)\s*(-?\d+(?:\.\d+)?)",
    ):
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            return float(match.group(1))

    for line in raw.splitlines():
        low = line.lower()
        if "temp" not in low:
            continue
        if any(word in low for word in ("threshold", "limit", "alarm", "warning",
                                        "high", "low", "max", "min")):
            continue
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:degree|deg\b|C\b)", low)
        if match:
            return float(match.group(1))
        values = re.findall(r"(-?\d+(?:\.\d+)?)", line)
        if values:
            return float(values[0])
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*C\b", raw)
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------------------
#  PON port SFP
# ---------------------------------------------------------------------------
def _ddm_power(value: Any) -> float | None:
    """Optical power from a DDM cell.

    BDPON / BTPON / PHOTON print both units::

        Laser output power : 8.1780 mW / 9.13 dBm

    The dBm value is the one the UI wants; only fall back to mW (converted to
    dBm) when the firmware omits it.
    """
    text = str(value or "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*mW", text, re.IGNORECASE)
    if match:
        milliwatts = float(match.group(1))
        if milliwatts > 0:
            return round(10 * math.log10(milliwatts), 2)
        return None
    return number(value)


def _ddm_values(text: str) -> dict[str, float | None]:
    """Read a key/value DDM block (labels differ slightly per firmware)."""
    values = parse_key_values(text)
    out: dict[str, float | None] = {
        "temperature_c": None,
        "voltage_v": None,
        "bias_current_ma": None,
        "tx_power_dbm": None,
        "rx_power_dbm": None,
    }

    for key, raw in values.items():
        low = key.lower()
        if out["temperature_c"] is None and "temp" in low:
            out["temperature_c"] = number(raw)
        if out["voltage_v"] is None and "volt" in low:
            out["voltage_v"] = number(raw)
        if out["bias_current_ma"] is None and "bias" in low:
            out["bias_current_ma"] = number(raw)
        if out["tx_power_dbm"] is None and (
            "output power" in low
            or "tx power" in low
            or "transmit power" in low
            or ("laser" in low and "power" in low and "bias" not in low)
        ):
            out["tx_power_dbm"] = _ddm_power(raw)
        if out["rx_power_dbm"] is None and (
            "rx power" in low
            or "receive" in low
            or "receiver" in low
            or "recv" in low
        ):
            out["rx_power_dbm"] = _ddm_power(raw)

    # Some firmware uses a bare table ("Module temperature 26.35 degrees C")
    # without a colon; parse the line itself when the key/value pass missed it.
    if any(value is None for value in out.values()):
        for line in str(text or "").replace("\r", "").split("\n"):
            low = line.lower()
            if out["temperature_c"] is None and "temp" in low:
                out["temperature_c"] = number(line)
            if out["voltage_v"] is None and "volt" in low:
                out["voltage_v"] = number(line)
            if out["bias_current_ma"] is None and "bias" in low:
                out["bias_current_ma"] = number(line)
            if out["tx_power_dbm"] is None and (
                "output power" in low or "tx power" in low or "transmit power" in low
            ):
                out["tx_power_dbm"] = _ddm_power(line)
            if out["rx_power_dbm"] is None and (
                "rx power" in low or "receive" in low or "recv" in low
            ):
                out["rx_power_dbm"] = _ddm_power(line)
    return out


def parse_port_optics(text: str, pon: int | None = None) -> list[dict[str, Any]]:
    """Per-port SFP readings from a PON diagnostics command.

    Handles both fixed-width tables (``show epon optical-transceiver-diagnosis``)
    and the key/value DDM block printed by BDPON / BTPON / PHOTON::

        show pon-port ddm-info pon1
        Laser bias current    : 13.600 mA
        Laser output power    : 8.1780 mW / 9.13 dBm
        Module temperature    : 26.35 degrees C
        Module voltage        : 3.2924 V

    *pon* is the PON number that was requested.  It is used when the firmware
    does not print the port name in the answer (the PHOTON block above does
    not), which is exactly why the collector passes it in.
    """
    out: list[dict[str, Any]] = []
    headers, rows = parse_table(text)
    if rows:
        port_col = _column(headers, "port", "interface", "epon", "gpon", "pon")
        temp_col = _column(headers, "temp")
        volt_col = _column(headers, "volt")
        bias_col = _column(headers, "bias", "current")
        tx_col = _column(headers, "tx", "transmit", "output")
        rx_col = _column(headers, "rx", "receive", "recv")

        for row in rows:
            port = ""
            if port_col and port_col in row:
                port = str(row.get(port_col) or "").strip()
            if not port:
                for value in row.values():
                    if re.search(r"(?:EPON|GPON|PON)\s*\d+(?:/\d+)*", str(value), re.IGNORECASE):
                        port = str(value).strip()
                        break
            if not port and pon is not None:
                port = f"pon{pon}"
            if not port:
                continue
            match = re.search(r"(\d+)\s*$", port)
            out.append(
                {
                    "port": port,
                    "pon": int(match.group(1)) if match else pon,
                    "temperature_c": number(row.get(temp_col)) if temp_col else None,
                    "voltage_v": number(row.get(volt_col)) if volt_col else None,
                    "bias_current_ma": number(row.get(bias_col)) if bias_col else None,
                    "tx_power_dbm": _ddm_power(row.get(tx_col)) if tx_col else None,
                    "rx_power_dbm": _ddm_power(row.get(rx_col)) if rx_col else None,
                }
            )
        out = [row for row in out if any(row.get(k) is not None for k in (
            "temperature_c", "voltage_v", "bias_current_ma", "tx_power_dbm", "rx_power_dbm"))]
    if out:
        return out

    values = _ddm_values(text)
    if not any(value is not None for value in values.values()):
        return []

    if pon is None:
        match = re.search(r"\b(?:pon|gpon|epon)\s*[-_]?\s*(\d+)\b", str(text or ""), re.IGNORECASE)
        if match:
            pon = int(match.group(1))
    values.update({"port": f"pon{pon}" if pon is not None else None, "pon": pon})
    return [values]


# ---------------------------------------------------------------------------
#  ONU optical telemetry (EPON and GPON share the table shape)
# ---------------------------------------------------------------------------
_ONU_ID_HEADERS = ("onu-id", "ont-id", "onu", "ont", "id", "onu-idx", "ont-idx")

#: a CLI column that carries the whole ONU interface name, e.g. ``epon0/1:22``
_IFACE_HEADERS = ("intf", "interface", "port", "onu name", "ont name")

#: ``EPON0/1:22`` / ``epon0/1:22`` / ``GPON0/1:5`` -> the ONU id
_IFNAME_ONU = re.compile(r"(?:EPON|GPON)\s*\d*\s*/\s*\d+\s*:\s*(\d+)", re.IGNORECASE)

#: a MAC in Cisco/dotted, colon or hyphen form
_MAC_TEXT = re.compile(
    r"((?:[0-9a-f]{4}\.){2}[0-9a-f]{4}|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2})",
    re.IGNORECASE,
)


def _ifname_onu(value: Any) -> int | None:
    """ONU id inside an interface name (``epon0/1:22`` -> 22)."""
    match = _IFNAME_ONU.search(str(value or ""))
    return int(match.group(1)) if match else None


def _ifname_pon(value: Any) -> int | None:
    """PON number inside an interface name (``EPON0/3:7`` -> 3)."""
    text = str(value or "")
    match = re.search(r"(?:EPON|GPON)\s*\d*\s*/\s*(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _mac_text(value: Any) -> str | None:
    """Normalise a MAC printed by a CLI to ``AA:BB:CC:DD:EE:FF``."""
    match = _MAC_TEXT.search(str(value or ""))
    if not match:
        return None
    digits = "".join(ch for ch in match.group(1).upper() if ch in "0123456789ABCDEF")
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _onu_id(headers: list[str], row: dict[str, str]) -> int | None:
    # 1) a column holding the full interface name (BDCOM's "IntfName")
    for header in headers:
        low = header.lower()
        if any(hint in low for hint in _IFACE_HEADERS):
            found = _ifname_onu(row.get(header))
            if found is not None:
                return found

    # 2) an explicit ONU/ONT id column
    for header in headers:
        low = header.lower().replace(" ", "-")
        if low in _ONU_ID_HEADERS:
            found = number(row.get(header))
            if found is not None and found == int(found):
                return int(found)
    col = _column(headers, "onu", "ont")
    if col is not None:
        # ``OnuIndex``/``ONU`` may hold the full interface name (``GPON0/1:5``):
        # take the id after the colon, never the 0 inside ``GPON0``.
        found = _ifname_onu(row.get(col))
        if found is not None:
            return found
        found = number(row.get(col))
        if found is not None and found == int(found):
            return int(found)

    # 3) a cell that is nothing but an integer (never the "0" inside "epon0/1")
    for header in headers:
        text = str(row.get(header) or "").strip()
        if re.fullmatch(r"\d+", text):
            return int(text)
    return None


_BDCOM_OPTICS_ROW = re.compile(
    r"(EPON\d*/\d+:\d+)\s+"
    r"(-?\d+(?:\.\d+)?|--)\s+(-?\d+(?:\.\d+)?|--)\s+"
    r"(-?\d+(?:\.\d+)?|--)\s+(-?\d+(?:\.\d+)?|--)\s+"
    r"(-?\d+(?:\.\d+)?|--)",
    re.IGNORECASE,
)


def _opt_number(value: str) -> float | None:
    return None if value.strip() == "--" else number(value)


def parse_bdcom_optics(text: str) -> dict[int, dict[str, Any]]:
    """``show epon onu-ctc-optical-transceiver-diagnosis`` on BDCOM.

    The header only separates ``IntfName`` with spaces - the remaining columns
    are single-space separated - so this table needs its own row regex.
    """
    out: dict[int, dict[str, Any]] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _BDCOM_OPTICS_ROW.search(line)
        if not match:
            continue
        onu_id = _ifname_onu(match.group(1))
        if onu_id is None:
            continue
        values = {
            "temperature_c": _opt_number(match.group(2)),
            "voltage_v": _opt_number(match.group(3)),
            "bias_current_ma": _opt_number(match.group(4)),
            "tx_power_dbm": _opt_number(match.group(5)),
            "rx_power_dbm": _opt_number(match.group(6)),
            "distance_m": None,
        }
        if any(v is not None for v in values.values()):
            out[onu_id] = values
    return out


def parse_onu_optics(text: str) -> dict[int, dict[str, Any]]:
    """ONU optical telemetry -> ``{onu_id: {...}}``.

    Handles both EPON (``ONU-ID ... RxPower(dBm)``) and GPON
    (``ONT-ID ... RxPower(dBm)``) column layouts.
    """
    low = str(text or "").lower()
    if "intfname" in low and "rxpow" in low:
        return parse_bdcom_optics(text)
    if "tx optical" in low and "rx optical" in low and "laser bias" in low:
        return parse_photon_onu_optical(text)

    headers, rows = parse_table(text)
    if not rows:
        # a one-ONU "key: value" response is also possible
        values = parse_key_values(text)
        if values:
            single = {
                "rx_power_dbm": number(kv_value(values, "rx")),
                "tx_power_dbm": number(kv_value(values, "tx")),
                "temperature_c": number(kv_value(values, "temp")),
                "voltage_v": number(kv_value(values, "volt")),
                "bias_current_ma": number(kv_value(values, "bias")),
                "distance_m": number(kv_value(values, "distance")),
            }
            if any(v is not None for v in single.values()):
                return {0: single}
        return {}

    temp_col = _column(headers, "temp")
    volt_col = _column(headers, "volt")
    bias_col = _column(headers, "bias", "current")
    tx_col = _column(headers, "tx", "transmit")
    rx_col = _column(headers, "rx", "receive", "recv")
    dist_col = _column(headers, "distance", "dist")

    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        onu_id = _onu_id(headers, row)
        if onu_id is None:
            continue
        values = {
            "rx_power_dbm": number(row.get(rx_col)) if rx_col else None,
            "tx_power_dbm": number(row.get(tx_col)) if tx_col else None,
            "temperature_c": number(row.get(temp_col)) if temp_col else None,
            "voltage_v": number(row.get(volt_col)) if volt_col else None,
            "bias_current_ma": number(row.get(bias_col)) if bias_col else None,
            "distance_m": number(row.get(dist_col)) if dist_col else None,
        }
        # a column that resolved to itself (header == value) is noise
        if any(v is not None for v in values.values()):
            out[onu_id] = values
    return out


# ---------------------------------------------------------------------------
#  ONU inventory / state / last down cause
# ---------------------------------------------------------------------------
_DOWN_LABELS = {
    "power off": "Power off (dying gasp)",
    "dying gasp": "Power off (dying gasp)",
    "power": "Power off (dying gasp)",
    "los": "Fiber / LOS (loss of signal)",
    "loss of signal": "Fiber / LOS (loss of signal)",
    "fiber": "Fiber / LOS (loss of signal)",
    "dyinggasp": "Power off (dying gasp)",
    "lob": "Laser / LOS",
    "lofi": "Fiber / LOS (loss of signal)",
    "deactive": "Deactivated by OLT",
    "admin": "Administratively down",
    "reboot": "ONU rebooting",
    "reset": "ONU reset",
}


def normalise_down_reason(raw: Any) -> str | None:
    """Map a vendor's terse cause to a human label (``LOS`` -> *Fiber / LOS*)."""
    text = str(raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for marker, label in _DOWN_LABELS.items():
        if marker in lowered:
            return label
    return text[:80]


def _is_bdcom_table(text: str) -> bool:
    """True for BDCOM's wrapped EPON tables.

    BDCOM wraps its 80-column tables onto a second header line and splits every
    row into two physical lines (``BindType Status DeregReason`` lives below the
    interface/MAC part), so the generic fixed-width parser cannot read them.
    """
    low = str(text or "").lower()
    return "lastderegreason" in low or "bindtype" in low or (
        "intfname" in low and "vendorid" in low
    )


_DOWN_TOKEN = re.compile(
    r"\b(power[- ]off|wire[- ]down|dying[- ]gasp|los|lofi|lob|reboot|deactive|"
    r"fiber|admin)\b",
    re.IGNORECASE,
)

_BDCOM_IFACE_ROW = re.compile(
    r"(EPON\d*/\d+:\d+)\s+((?:[0-9a-f]{4}\.){2}[0-9a-f]{4})",
    re.IGNORECASE,
)

_BDCOM_INFO_ROW = re.compile(
    r"(EPON\d*/\d+:\d+)\s+(\S+)\s+(\S+)\s+((?:[0-9a-f]{4}\.){2}[0-9a-f]{4})(.*)$",
    re.IGNORECASE,
)

_BDCOM_ACTIVE_ROW = re.compile(
    r"(EPON\d*/\d+:\d+)\s+((?:[0-9a-f]{4}\.){2}[0-9a-f]{4})\s+"
    r"(\S+)\s+(\S+)\s+(\d+)\s+(\d+)",
    re.IGNORECASE,
)


def _bdcom_reason(text: str) -> str | None:
    match = _DOWN_TOKEN.search(str(text or ""))
    return normalise_down_reason(match.group(1)) if match else None


def parse_bdcom_inactive(text: str) -> list[dict[str, Any]]:
    """``show epon inactive-onu`` on BDCOM -> ``[{pon, onu_id, reason, mac}]``."""
    lines = str(text or "").replace("\r", "").split("\n")
    out: list[dict[str, Any]] = []
    seen: set[tuple[int | None, int]] = set()
    for index, line in enumerate(lines):
        match = _BDCOM_IFACE_ROW.search(line)
        if not match:
            continue
        pon = _ifname_pon(match.group(1))
        onu_id = _ifname_onu(match.group(1))
        if onu_id is None:
            continue
        if (pon, onu_id) in seen:
            continue
        seen.add((pon, onu_id))
        # the reason and the dates wrap onto the following line
        hay = line + " " + (lines[index + 1] if index + 1 < len(lines) else "")
        out.append(
            {
                "pon": pon,
                "onu_id": onu_id,
                "reason": _bdcom_reason(hay),
                "at": None,
                "mac": _mac_text(match.group(2)),
            }
        )
    return out


def parse_bdcom_inventory(text: str) -> dict[int, dict[str, Any]]:
    """``show epon onu-information interface EPON0/N`` on BDCOM."""
    lines = str(text or "").replace("\r", "").split("\n")
    out: dict[int, dict[str, Any]] = {}
    for index, line in enumerate(lines):
        match = _BDCOM_INFO_ROW.search(line)
        if not match:
            continue
        onu_id = _ifname_onu(match.group(1))
        if onu_id is None:
            continue
        hay = (line + " " + (lines[index + 1] if index + 1 < len(lines) else "")).lower()
        state = None
        for token in ("deregistered", "config-failed", "auto-configured",
                      "online", "offline"):
            if token in hay:
                state = token
                break
        out[onu_id] = {
            "if_name": match.group(1),
            "mac": _mac_text(match.group(4)),
            "vendor": match.group(2),
            "model": match.group(3),
            "state": state,
            "reason": _bdcom_reason(hay),
        }
    return out


def parse_bdcom_active(text: str) -> list[dict[str, Any]]:
    """``show epon active-onu`` on BDCOM -> inventory rows incl. distance."""
    out: list[dict[str, Any]] = []
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _BDCOM_ACTIVE_ROW.search(line)
        if not match:
            continue
        onu_id = _ifname_onu(match.group(1))
        if onu_id is None:
            continue
        distance = number(match.group(5))
        out.append(
            {
                "if_name": match.group(1),
                "pon": _ifname_pon(match.group(1)),
                "onu_id": onu_id,
                "mac": _mac_text(match.group(2)),
                "state": match.group(3),
                "distance_m": distance,
            }
        )
    return out


# ---------------------------------------------------------------------------
#  GPON ONU tables (BDCOM GP3600 / VSOL V1600G / C-Data)
# ---------------------------------------------------------------------------
_GPON_IFACE = re.compile(r"(GPON\s*\d*\s*/\s*\d+\s*:\s*\d+)", re.IGNORECASE)
_GPON_SERIAL = re.compile(
    r"\b(?:SN|S/N|SERIAL)\s*[:=]?\s*([0-9A-Za-z]{8,24})\b", re.IGNORECASE
)
_GPON_HEX_SN = re.compile(r"\b([0-9A-F]{16}|[A-Z]{4}[0-9A-F]{8,12})\b")
_GPON_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
_GPON_DISTANCE = re.compile(r"(\d+(?:\.\d+)?)\s*(km|m)\b", re.IGNORECASE)
_GPON_STATES = (
    "online", "offline", "active", "inactive", "working", "standby",
    "deregistered", "config-failed", "auto-configured", "up", "down",
)


def _is_gpon_text(text: str) -> bool:
    """True for the ``GPON0/x`` family (BDCOM GP3600, VSOL V1600G, C-Data GPON)."""
    low = str(text or "").lower()
    return "gpon" in low


def _gpon_state(line: str) -> str | None:
    low = str(line or "").lower()
    for token in _GPON_STATES:
        if re.search(rf"\b{re.escape(token)}\b", low):
            return token
    return None


def _gpon_serial(line: str) -> str | None:
    match = _GPON_SERIAL.search(str(line or ""))
    if match:
        return match.group(1).upper()
    match = _GPON_HEX_SN.search(str(line or ""))
    return match.group(1).upper() if match else None


def _gpon_distance(line: str) -> float | None:
    match = _GPON_DISTANCE.search(str(line or ""))
    if not match:
        return None
    value = number(match.group(1))
    if value is None:
        return None
    return round(value * 1000, 1) if match.group(2).lower() == "km" else value


def _gpon_row_from_line(line: str, default_pon: int | None) -> dict[str, Any] | None:
    """One free-form GPON row (no usable header) -> a partial ONU dict."""
    iface = _GPON_IFACE.search(line)
    onu_id = _ifname_onu(iface.group(1)) if iface else None
    if onu_id is None:
        if default_pon is None:
            return None
        # a table without interface names: first standalone integer is the id
        match = re.match(r"\s*(\d+)\b", line)
        if not match:
            return None
        onu_id = int(match.group(1))
    mac = _mac_text(line)
    serial = _gpon_serial(line)
    if mac is None and serial is None:
        return None
    return {
        "pon": _ifname_pon(iface.group(1)) if iface else default_pon,
        "onu_id": onu_id,
        "mac": mac,
        "serial": serial,
        "state": _gpon_state(line),
        "distance_m": _gpon_distance(line),
        "reason": _bdcom_reason(line),
    }


def parse_gpon_inventory(
    text: str, default_pon: int | None = None
) -> dict[int, dict[str, Any]]:
    """GPON ONU inventory -> ``{onu_id: {mac, serial, model, state, ...}}``.

    Handles both shapes the GPON firmwares print:

    * a header table (``ONU-ID / SN / MAC / State`` columns), and
    * one free-form line per ONU (``GPON0/1:4  HWTC  HG8245H  00:1A:..  online``).
    """
    headers, rows = parse_table(text)
    out: dict[int, dict[str, Any]] = {}

    # Most GPON firmwares print the interface name on every row.  Scan those
    # lines first: it is exact, whereas a fixed-width table that mixes
    # `GPON0/1:1` with numbers is easy to mis-align.
    for line in str(text or "").replace("\r", "").split("\n"):
        values = _gpon_row_from_line(line, default_pon)
        if values is None:
            continue
        out[int(values.pop("onu_id"))] = {
            key: value for key, value in values.items() if value not in (None, "")
        }

    if rows:
        mac_col = _column(headers, "mac")
        sn_col = _column(headers, "serial", "sn")
        state_col = _column(headers, "state", "status", "run")
        model_col = _column(headers, "model", "equip", "type")
        vendor_col = _column(headers, "vendor", "manufacturer")
        dist_col = _column(headers, "distance", "dist", "range")
        cause_col = _column(headers, "cause", "reason", "dereg")
        iface_col = _column(headers, "interface", "intf", "port", "onu", "ont")
        for row in rows:
            onu_id = _onu_id(headers, row)
            if onu_id is None:
                continue
            iface = row.get(iface_col) if iface_col else None
            values: dict[str, Any] = {
                "pon": _ifname_pon(iface) or default_pon,
                "mac": _mac_text(row.get(mac_col)) if mac_col else None,
                "serial": ((row.get(sn_col) or "").strip() or None) if sn_col else None,
                "model": ((row.get(model_col) or "").strip() or None) if model_col else None,
                "vendor": ((row.get(vendor_col) or "").strip() or None) if vendor_col else None,
                "state": ((row.get(state_col) or "").strip() or None) if state_col else None,
                "distance_m": number(row.get(dist_col)) if dist_col else None,
                "reason": normalise_down_reason(row.get(cause_col)) if cause_col else None,
            }
            if not values["serial"]:
                values["serial"] = _gpon_serial(" ".join(str(v or "") for v in row.values()))
            entry = out.setdefault(onu_id, {})
            for key, value in values.items():
                if value not in (None, "") and entry.get(key) in (None, ""):
                    entry[key] = value
        return out

    return out


def parse_gpon_inactive(
    text: str, default_pon: int | None = None
) -> list[dict[str, Any]]:
    """``show gpon inactive-onu`` -> one row per ONU with its down reason/time."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int | None, int]] = set()

    headers, rows = parse_table(text)
    if rows:
        iface_col = _column(headers, "interface", "intf", "port", "onu", "ont")
        cause_col = _column(headers, "cause", "reason", "dereg")
        time_col = _column(headers, "time", "date", "since")
        mac_col = _column(headers, "mac")
        sn_col = _column(headers, "serial", "sn")
        for row in rows:
            onu_id = _onu_id(headers, row)
            if onu_id is None:
                continue
            pon = (_ifname_pon(row.get(iface_col)) or default_pon) if iface_col else default_pon
            if (pon, onu_id) in seen:
                continue
            seen.add((pon, onu_id))
            blob = " ".join(str(value or "") for value in row.values())
            found_time = _GPON_TIME.search(blob)
            out.append(
                {
                    "pon": pon,
                    "onu_id": onu_id,
                    "reason": (
                        normalise_down_reason(row.get(cause_col))
                        if cause_col else _bdcom_reason(blob)
                    ),
                    "at": (
                        found_time.group() if found_time
                        else ((row.get(time_col) or None) if time_col else None)
                    ),
                    "mac": _mac_text(row.get(mac_col)) if mac_col else _mac_text(blob),
                    "serial": (
                        ((row.get(sn_col) or "").strip() or _gpon_serial(blob))
                        if sn_col else _gpon_serial(blob)
                    ),
                }
            )
        if out:
            return out

    for line in str(text or "").replace("\r", "").split("\n"):
        values = _gpon_row_from_line(line, default_pon)
        if values is None:
            continue
        pon, onu_id = values.get("pon"), values.get("onu_id")
        if (pon, onu_id) in seen:
            continue
        seen.add((pon, onu_id))
        found_time = _GPON_TIME.search(line)
        out.append(
            {
                "pon": pon,
                "onu_id": onu_id,
                "reason": values.get("reason"),
                "at": found_time.group() if found_time else None,
                "mac": values.get("mac"),
                "serial": values.get("serial"),
            }
        )
    return out


def parse_inactive_onus(text: str, default_pon: int | None = None) -> list[dict[str, Any]]:
    """``show ... inactive-onu`` -> ``[{pon, onu_id, reason, at, mac}]``.

    A list (not a dict) because the same ONU id exists on every PON and the
    table usually names the port (``EPON0/5:12``) - keying by id alone would put
    a reason on the wrong ONU.  *default_pon* is used when the firmware omits
    the interface name from a per-PON command's output.
    """
    if _is_bdcom_table(text):
        return parse_bdcom_inactive(text)
    if _is_gpon_text(text) or default_pon is not None:
        # A per-PON command's output usually omits the "GPON" prefix; the
        # caller's PON number is then the only way to place the row.
        return parse_gpon_inactive(text, default_pon=default_pon)

    headers, rows = parse_table(text)
    if not rows:
        return []

    cause_col = _column(headers, "cause", "reason")
    time_col = _column(headers, "time", "since", "date")
    mac_col = _column(headers, "mac", "serial", "sn")
    name_col = _column(headers, "onu", "ont", "port", "interface")

    out: list[dict[str, Any]] = []
    for row in rows:
        onu_id = _onu_id(headers, row)
        if onu_id is None:
            continue
        pon = None
        if name_col:
            pon = _ifname_pon(row.get(name_col))
            if pon is None:
                match = re.search(r"(?:EPON|GPON)\s*\d*/(\d+)", str(row.get(name_col) or ""), re.IGNORECASE)
                if match:
                    pon = int(match.group(1))
        out.append(
            {
                "pon": pon,
                "onu_id": onu_id,
                "reason": normalise_down_reason(row.get(cause_col)) if cause_col else None,
                "at": (row.get(time_col) or None) if time_col else None,
                "mac": _mac_text(row.get(mac_col)) if mac_col else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
#  PHOTON / BDPON / BTPON EPON ONT tables
# ---------------------------------------------------------------------------
_PHOTON_MAC = r"(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}"
_PHOTON_INFO_ROW = re.compile(rf"^\s*\S+\s+(\d+)\s+({_PHOTON_MAC})\s+(.*)$")
_PHOTON_OPTICAL_ROW = re.compile(
    rf"^\s*(\d+)\s+({_PHOTON_MAC})"
    rf"(?:\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"
    rf"\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?))?\s*$"
)
_PHOTON_BASIC_ROW = re.compile(rf"^\s*(\d+)\s+({_PHOTON_MAC})\s*(.*)$")
_PHOTON_VERSION_ROW = re.compile(
    rf"^\s*(\d+)\s+({_PHOTON_MAC})\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)


def _hex_text(token: Any) -> str | None:
    """``0x31313046`` -> ``110F`` when the hex bytes are printable ASCII."""
    text = str(token or "").strip()
    if not text.lower().startswith("0x"):
        return None
    try:
        raw = bytes.fromhex(text[2:])
    except ValueError:
        return None
    decoded = raw.decode("ascii", "ignore").strip("\x00 ")
    if decoded and all(32 <= ord(char) < 127 for char in decoded):
        return decoded
    return None


def parse_photon_onu_info(text: str) -> dict[int, dict[str, Any]]:
    """``show ont info ponN all`` -> ONU state/MAC for PHOTON EPON-OLT."""
    out: dict[int, dict[str, Any]] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _PHOTON_INFO_ROW.match(line)
        if not match:
            continue
        onu_id = int(match.group(1))
        mac = _mac_text(match.group(2))
        fields = match.group(3).split()
        if not mac or not fields:
            continue
        control = fields[0] if len(fields) > 0 else None
        state = fields[1] if len(fields) > 1 else None
        out[onu_id] = {
            "mac": mac,
            "state": state,
            "admin_status": 1 if str(control or "").lower() in ("active", "enable", "enabled") else None,
            "control": control,
            "config": fields[2] if len(fields) > 2 else None,
            "match": fields[3] if len(fields) > 3 else None,
            "desc": " ".join(fields[4:]) or None,
        }
    return out


def parse_photon_onu_optical(text: str) -> dict[int, dict[str, Any]]:
    """``show ont optical-info ponN all`` -> voltage/TX/RX/bias/temperature."""
    out: dict[int, dict[str, Any]] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _PHOTON_OPTICAL_ROW.match(line)
        if not match:
            continue
        onu_id = int(match.group(1))
        mac = _mac_text(match.group(2))
        if not mac:
            continue
        values: dict[str, Any] = {
            "mac": mac,
            "voltage_v": None,
            "tx_power_dbm": None,
            "rx_power_dbm": None,
            "bias_current_ma": None,
            "temperature_c": None,
        }
        if match.group(3) is not None:
            values.update(
                {
                    "voltage_v": number(match.group(3)),
                    "tx_power_dbm": number(match.group(4)),
                    "rx_power_dbm": number(match.group(5)),
                    "bias_current_ma": number(match.group(6)),
                    "temperature_c": number(match.group(7)),
                }
            )
        out[onu_id] = values
    return out


def parse_photon_onu_basic(text: str) -> dict[int, dict[str, Any]]:
    """``show ont basic-info ponN all`` -> type, distance, uptime, model."""
    out: dict[int, dict[str, Any]] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _PHOTON_BASIC_ROW.match(line)
        if not match:
            continue
        onu_id = int(match.group(1))
        mac = _mac_text(match.group(2))
        fields = match.group(3).split()
        if not mac:
            continue
        values: dict[str, Any] = {"mac": mac}
        if fields and fields[0] != "--":
            values["onu_type"] = fields[0]
            if len(fields) > 1 and fields[1] != "--":
                values["distance_m"] = number(fields[1])
            if len(fields) > 2 and fields[2] != "--":
                values["up_time"] = fields[2]
            if len(fields) > 3:
                values["model"] = " ".join(fields[3:])
        elif fields:
            # an unregistered/offline ONT prints only the MAC and ``--``
            values["state"] = "offline"
        out[onu_id] = values
    return out


def parse_photon_onu_version(text: str) -> dict[int, dict[str, Any]]:
    """``show ont version ponN all`` -> vendor, model and software version."""
    out: dict[int, dict[str, Any]] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _PHOTON_VERSION_ROW.match(line)
        if not match:
            continue
        onu_id = int(match.group(1))
        mac = _mac_text(match.group(2))
        if not mac:
            continue
        model = _hex_text(match.group(4)) or match.group(4)
        out[onu_id] = {
            "mac": mac,
            "vendor": match.group(3),
            "model": model,
            "hw_version": match.group(5),
            "sw_version": match.group(6),
        }
    return out


def parse_cdata_inventory(text: str) -> list[dict[str, Any]]:
    """``show ont info all`` on C-Data -> one row per ONU (PON included).

    The table is::

        F/S P  ONT    MAC                  Control  Run     Config   Match   Last
                 ID                          flag     state   state    state   down-cause
          0/1 1  1      70:2E:22:01:96:5D    Active   Online  success  match   dying-gasp

    Unlike the per-PON PHOTON table this listing covers every PON at once, so a
    list (not an ``{onu_id: ...}`` map) is required - ONU id 1 exists on every
    PON and would otherwise overwrite itself.
    """
    out: list[dict[str, Any]] = []
    row_re = re.compile(
        r"^\s*(\d+)\s*/\s*(\d+)\s+(\d+)\s+(\d+)\s+"
        r"([0-9A-Fa-f:.\-]{12,17})\s+"
        r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$"
    )
    for line in str(text or "").replace("\r", "").split("\n"):
        match = row_re.match(line)
        if not match:
            continue
        frame, slot, port, onu_id = (int(match.group(i)) for i in range(1, 5))
        mac = _mac_text(match.group(5))
        if not mac:
            continue
        control = match.group(6)
        run_state = match.group(7)
        cause = match.group(10)
        pon = slot * 10 + port if frame == 0 else frame * 100 + slot * 10 + port
        values: dict[str, Any] = {
            "pon": pon,
            "onu_id": onu_id,
            "mac": mac,
            "state": run_state,
            "admin_status": (
                1 if str(control).lower() in ("active", "enable", "enabled") else 2
            ),
        }
        if cause and cause.strip() not in ("-", "--"):
            values["last_down_reason"] = normalise_down_reason(cause)
        out.append(values)
    return out


def parse_onu_inventory(
    text: str, default_pon: int | None = None
) -> dict[int, dict[str, Any]]:
    """``show ... onu-information`` -> ``{onu_id: {mac, model, state, distance, reason}}``."""
    low = str(text or "").lower()
    if (
        "control" in low
        and "run" in low
        and "config" in low
        and "match" in low
        and "ont" in low
    ):
        return parse_photon_onu_info(text)
    if _is_bdcom_table(text):
        return parse_bdcom_inventory(text)
    if _is_gpon_text(text):
        gpon = parse_gpon_inventory(text, default_pon=default_pon)
        if gpon:
            return gpon

    headers, rows = parse_table(text)
    if not rows:
        return {}

    mac_col = _column(headers, "mac")
    model_col = _column(headers, "model", "type", "equipment")
    state_col = _column(headers, "state", "status")
    dist_col = _column(headers, "distance", "dist")
    cause_col = _column(headers, "cause", "reason")
    serial_col = _column(headers, "serial", "sn")

    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        onu_id = _onu_id(headers, row)
        if onu_id is None:
            continue
        out[onu_id] = {
            "mac": _mac_text(row.get(mac_col)) if mac_col else None,
            "serial": (row.get(serial_col) or None) if serial_col else None,
            "model": (row.get(model_col) or None) if model_col else None,
            "state": (row.get(state_col) or None) if state_col else None,
            "distance_m": number(row.get(dist_col)) if dist_col else None,
            "reason": normalise_down_reason(row.get(cause_col)) if cause_col else None,
        }
    return out


# ---------------------------------------------------------------------------
#  BDCOM uplink ports and subscriber MAC table
# ---------------------------------------------------------------------------
_INTERFACE_HEADER = re.compile(
    r"(?m)^\s*([A-Za-z][\w\-]*\d+/\d+)\s+is\s+(.+)$",
)


def parse_interface_details(text: str) -> list[dict[str, Any]]:
    """``show interface <name>`` -> status, traffic and SFP DDM per port.

    Handles the BDCOM layout::

        TGigaEthernet0/1 is up, line protocol is up
            TX power:-2.40 dBM, RX power:-3.69 dBM
            SFP temperature:45.00 C,supply voltage :3.29V,Bias Current.:5.73mA
            Real time input rate 7%, 737860765 bits/sec, 76678 packets/sec
    """
    raw = str(text or "").replace("\r", "")
    matches = list(_INTERFACE_HEADER.finditer(raw))
    out: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        name = match.group(1)
        header = match.group(2).strip()
        block = raw[match.start(): matches[index + 1].start() if index + 1 < len(matches) else len(raw)]

        def grab(pattern: str) -> float | None:
            found = re.search(pattern, block, re.IGNORECASE)
            return number(found.group(1)) if found else None

        status = 1 if header.lower().startswith("up") else 2
        out.append(
            {
                "port": name,
                "pon": None,
                "oper_status": status,
                "rx_power_dbm": grab(r"RX\s*power\s*:\s*(-?\d+(?:\.\d+)?)"),
                "tx_power_dbm": grab(r"TX\s*power\s*:\s*(-?\d+(?:\.\d+)?)"),
                "temperature_c": grab(r"SFP\s+temperature\s*:\s*(-?\d+(?:\.\d+)?)"),
                "voltage_v": grab(r"supply\s+voltage\s*:\s*(-?\d+(?:\.\d+)?)"),
                "bias_current_ma": grab(r"Bias\s+Current\.?\s*:\s*(-?\d+(?:\.\d+)?)"),
                "in_bps": grab(r"Real\s+time\s+input\s+rate[^\n]*?(\d+)\s*bits/sec"),
                "out_bps": grab(r"Real\s+time\s+output\s+rate[^\n]*?(\d+)\s*bits/sec"),
            }
        )
    return out


_MAC_TYPES = frozenset({"DYNAMIC", "STATIC", "SECURE", "LEARNED"})
_MAC_NOISE = frozenset(
    {"VLAN", "MAC", "PORT", "ADDRESS", "MAC-ADDRESS", "TYPE", "INTERFACE", "PON", "GPON", "EPON"}
)


def _port_token(text: str) -> str | None:
    """First port-looking token in *text* (``PON1``, ``gpon0/1:5``, ``ge0/2``)."""
    for token in re.findall(r"[A-Za-z][\w/\-.:]*|\d+", str(text or ""))[:8]:
        if token.isdigit():
            continue
        upper = token.upper()
        if upper in _MAC_TYPES or upper in _MAC_NOISE:
            continue
        if re.search(r"\d", token):
            return token
    return None


def _mac_and_port(line: str) -> tuple[str | None, str | None]:
    """MAC + port from either ``MAC ... PORT`` or ``PORT ... MAC`` layouts."""
    match = _MAC_TEXT.search(str(line or ""))
    if not match:
        return None, None
    mac = _mac_text(match.group(1))
    port = _port_token(line[match.end():])
    if port is None:
        # VSOL prints PON/VLAN in front of the MAC address
        for token in reversed(re.findall(r"[A-Za-z][\w/\-.:]*", line[: match.start()])):
            upper = token.upper()
            if upper in _MAC_TYPES or upper in _MAC_NOISE:
                continue
            if re.search(r"\d", token):
                port = token
                break
    return mac, port


def parse_mac_table(text: str) -> dict[str, str]:
    """``show mac address-table`` -> ``{AA:BB:CC:DD:EE:FF: port}``.

    Only rows with a *port* are useful (``tg0/1``, ``epon0/1:22``); the caller
    maps the port name back to an interface index.  The type column is optional
    because several GPON firmwares print only ``MAC  PORT  VLAN``.
    """
    out: dict[str, str] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        mac, port = _mac_and_port(line)
        if mac and port:
            out.setdefault(mac, port)
    return out


def parse_mac_table_vlans(text: str) -> dict[str, dict[str, Any]]:
    """``show mac address-table`` -> ``{MAC: {"port": ..., "vlan": ...}}``.

    BDCOM (and several other CLI-first EPON vendors) print a VLAN column in
    front of the MAC.  Keeping it lets the dashboard map a subscriber to the
    MikroTik VLAN interface (``ISP_CORE_1365``) they arrive on.
    """
    out: dict[str, dict[str, Any]] = {}
    for line in str(text or "").replace("\r", "").split("\n"):
        mac, port = _mac_and_port(line)
        if not mac or not port:
            continue
        match = _MAC_TEXT.search(line)
        vlan: int | None = None
        if match:
            candidates = re.findall(r"\b(\d{1,4})\b", line[: match.start()])[-1:]
            candidates += re.findall(r"\b(\d{1,4})\b", line[match.end():])[:2]
            for candidate in candidates:
                value = int(candidate)
                if 0 < value < 4095:
                    vlan = value
                    break
        out.setdefault(mac, {"port": port, "vlan": vlan})
    return out


def parse_identity(text: str) -> dict[str, str]:
    """Best-effort vendor/model/serial from ``show version``-style output."""
    values = parse_key_values(text)
    out: dict[str, str] = {}
    for key, target in (
        (("model", "device type", "product"), "model"),
        (("version", "software", "firmware"), "version"),
        (("serial", "sn"), "serial"),
        (("vendor", "manufacturer", "company"), "vendor"),
    ):
        found = kv_value(values, *key)
        if found:
            out[target] = found
    if not out:
        first = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
        if first:
            out["first_line"] = first[:160]
    return out


def parse_gpon_onu_detail(text: str) -> dict[str, Any]:
    """``show ont info ... all`` -> the fields we care about."""
    values = parse_key_values(text)
    if not values:
        return {}
    out: dict[str, Any] = {}
    mapping = {
        "serial": ("serial", "sn", "s/n", "gpon sn"),
        "model": ("model", "equipment id", "ont type", "type", "device type"),
        "mac": ("mac",),
        "state": ("state", "run state", "status", "oper state"),
        "vlan": ("vlan", "c-vlan", "svlan", "service vlan"),
        "distance_m": ("distance", "optical distance"),
        "last_down_reason": ("last down cause", "down cause", "last down reason"),
        "temperature_c": ("temperature", "temp"),
        "voltage_v": ("voltage", "volt"),
        "bias_current_ma": ("bias", "current"),
        "rx_power_dbm": ("rx power", "rxpower", "rx optical"),
        "tx_power_dbm": ("tx power", "txpower", "tx optical"),
    }
    for field, labels in mapping.items():
        found = kv_value(values, *labels)
        if found is None:
            continue
        if field in ("distance_m", "temperature_c", "voltage_v", "bias_current_ma",
                     "rx_power_dbm", "tx_power_dbm"):
            out[field] = number(found)
        elif field == "last_down_reason":
            out[field] = normalise_down_reason(found)
        else:
            out[field] = found
    return out
