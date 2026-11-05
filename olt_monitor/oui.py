"""MAC address -> device manufacturer ("company name").

The dashboard shows who made the ONU and the customer's router, which makes it
obvious whether a subscriber is behind a Huawei ONT or a cheap CPE that may be
the cause of a bad optical reading.

Three data sources are consulted, in this order:

1. ``data/oui.csv`` / ``data/oui.txt`` / ``data/oui.json`` if the operator
   placed one next to the application (the IEEE registry or Wireshark's
   ``manuf`` file both work).  Updating that file is enough to refresh the
   names - no code change and no restart.
2. The bundled database (``olt_monitor/data/oui.json.gz``), a compact copy of
   the IEEE MA-L/MA-M/MA-S assignments.  It is loaded lazily and only once.
3. A device's own reported vendor/model string, when the OLT exposes one.

A MAC that is not in any database returns ``None`` and the UI shows "-" rather
than a guessed name.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import threading
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("oui")

_BUNDLED = Path(__file__).resolve().parent / "data" / "oui.json.gz"

_HEX = re.compile(r"[^0-9A-F]")

_lock = threading.Lock()
_cache: dict[str, str] | None = None
_external_stamp: tuple[str, float] | None = None


def normalise_mac(value: Any) -> str | None:
    """Return the 12 upper-case hex digits of a MAC, or ``None``."""
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)) and len(value) == 6:
        return "".join(f"{b:02X}" for b in value)
    text = str(value).strip().upper()
    hex_only = _HEX.sub("", text)
    if len(hex_only) != 12:
        return None
    return hex_only


def _clean(name: Any) -> str | None:
    text = " ".join(str(name or "").split())
    if not text:
        return None
    # The IEEE file appends an address after the company name; external files
    # may still contain it, but the bundled one keeps only the first line.
    return text[:120]


def _load_bundled() -> dict[str, str]:
    if not _BUNDLED.is_file():
        return {}
    try:
        with gzip.open(_BUNDLED, "rt", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:  # corrupted/missing package data
        log.warning("could not read bundled OUI database: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for prefix, vendor in data.items():
        key = _HEX.sub("", str(prefix).upper())
        name = _clean(vendor)
        if len(key) in (6, 7, 9) and name:
            out.setdefault(key, name)
    return out


def _read_external(path: Path) -> dict[str, str]:
    """Read an IEEE ``oui.csv``, a Wireshark ``manuf`` file or a JSON map."""
    try:
        if path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(raw, dict):
                return {}
            return {
                _HEX.sub("", str(prefix).upper()): _clean(vendor) or ""
                for prefix, vendor in raw.items()
                if _clean(vendor)
            }
        if path.suffix.lower() == ".txt":
            out: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                key = _HEX.sub("", parts[0].upper())
                name = _clean(parts[2] if len(parts) > 2 and parts[2].strip() else parts[1])
                if len(key) in (6, 7, 9) and name:
                    out.setdefault(key, name)
            return out
        out = {}
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.reader(handle)
            for row in reader:
                if len(row) < 3:
                    continue
                key = _HEX.sub("", str(row[1]).upper())
                if len(key) not in (6, 7, 9):
                    continue  # header / malformed row
                name = _clean(row[2])
                if name:
                    out[key] = name
        return out
    except OSError as exc:
        log.warning("could not read OUI file %s: %s", path, exc)
        return {}


def _external_files(data_dir: Path | None) -> list[Path]:
    if data_dir is None:
        try:
            from .config import ROOT

            data_dir = Path(ROOT) / "data"
        except Exception:  # pragma: no cover - config import must not break UI
            return []
    return [
        candidate
        for candidate in (
            data_dir / "oui.csv",
            data_dir / "oui.txt",
            data_dir / "oui.json",
        )
        if candidate.is_file()
    ]


def _database(data_dir: Path | None = None) -> dict[str, str]:
    global _cache, _external_stamp
    files = _external_files(data_dir)
    stamp = (
        (str(files[0]), files[0].stat().st_mtime) if files else None
    )
    with _lock:
        if _cache is not None and stamp == _external_stamp:
            return _cache
        merged = _load_bundled()
        for path in files:
            merged.update(_read_external(path))
        _cache = merged
        _external_stamp = stamp
        return merged


def lookup(mac: Any, data_dir: Path | None = None) -> str | None:
    """Manufacturer name for *mac*, or ``None`` when the OUI is unknown."""
    key = normalise_mac(mac)
    if not key:
        return None
    database = _database(data_dir)
    # MA-S (36 bit) and MA-M (28 bit) assignments are more specific than MA-L.
    for length in (9, 7, 6):
        name = database.get(key[:length])
        if name:
            return name
    return None


#: ONU model/vendor strings that already name the maker
_KNOWN_WORDS = (
    "HUAWEI", "ZTE", "FIBERHOME", "FIBER HOME", "NOKIA", "ALCATEL", "CALIX",
    "RAISECOM", "BDCOM", "VSOL", "CDATA", "C-DATA", "ECOM", "PHOTON", "DASAN",
    "H3C", "RUIJIE", "ZYXEL", "SAGEMCOM", "SERCOMM", "ASKEY", "HITRON",
    "TENDA", "TPLINK", "TP-LINK", "MIKROTIK", "UBIQUITI", "MIMOSA", "CAMBIUM",
)


def from_reported(*values: Any) -> str | None:
    """Pick a maker name out of an OLT's own ``vendor``/``model`` columns."""
    for value in values:
        text = " ".join(str(value or "").split())
        if not text:
            continue
        upper = text.upper()
        for word in _KNOWN_WORDS:
            if word in upper:
                return word.replace("TPLINK", "TP-Link").replace("FIBER HOME", "FiberHome")
        # A vendor column that is not a hardware description is still useful.
        lowered = text.lower()
        if not any(
            token in lowered
            for token in ("epon", "gpon", "ont", "onu", "ftth", "sfu", "hgu", "110f")
        ) and len(text) >= 3:
            return text[:60]
    return None


def vendor_for(
    mac: Any,
    data_dir: Path | None = None,
    *reported: Any,
) -> str | None:
    """Best available maker name: OUI database first, then the OLT's report."""
    return lookup(mac, data_dir) or from_reported(*reported)


def reload() -> None:
    """Forget the cached database (used after an OUI file is replaced)."""
    global _cache, _external_stamp
    with _lock:
        _cache = None
        _external_stamp = None
