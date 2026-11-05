#!/usr/bin/env python3
"""Discover which OIDs your OLT actually exposes.

The standard MIB-2 tables (system, interfaces) are already wired up in
``config/config.yaml``.  The *optical* values (ONU rx/tx power, distance,
temperature, MAC) live in the vendor's private MIB, which differs between
firmware versions - this tool finds them for you.

Examples
--------
    # find the optical columns and write them to config/optical_oids.yaml
    python3 tools/discover_olt.py --autoconfig

    # does the OLT publish a subscriber MAC table? (needed to link users)
    python3 tools/discover_olt.py --mac-table

    # everything, saved to a file for sharing
    python3 tools/discover_olt.py --presets --columns 1.3.6.1.4.1.37950 --out olt_walk.txt

    # a single value
    python3 tools/discover_olt.py --get 1.3.6.1.2.1.1.1.0

Run it from the project root so the config is picked up.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from olt_monitor import registry  # noqa: E402
from olt_monitor.config import ROOT, db_path as resolve_db_path, dig, load_config  # noqa: E402
from olt_monitor.db import init_db  # noqa: E402
from olt_monitor.snmp import SnmpClient, SnmpError, format_ticks, to_mac  # noqa: E402

PRESETS: list[tuple[str, str]] = [
    ("system group", "1.3.6.1.2.1.1"),
    ("interfaces (ifDescr)", "1.3.6.1.2.1.2.2.1.2"),
    ("interfaces (ifName)", "1.3.6.1.2.1.31.1.1.1.1"),
    ("interfaces (ifAlias)", "1.3.6.1.2.1.31.1.1.1.18"),
    ("ifOperStatus", "1.3.6.1.2.1.2.2.1.8"),
    ("vendor enterprise", "1.3.6.1.4.1.37950"),
]

# standard bridge tables used to turn a subscriber MAC into an ONU
FDB_TABLES: list[tuple[str, str]] = [
    ("bridge port -> ifIndex   (dot1dBasePortIfIndex)", "1.3.6.1.2.1.17.1.4.1.2"),
    ("FDB MAC address          (dot1dTpFdbAddress)", "1.3.6.1.2.1.17.4.3.1.1"),
    ("FDB bridge port          (dot1dTpFdbPort)", "1.3.6.1.2.1.17.4.3.1.2"),
    ("Q-BRIDGE FDB port        (dot1qTpFdbPort)", "1.3.6.1.2.1.17.7.1.2.2.1.2"),
    ("Q-BRIDGE FDB status      (dot1qTpFdbStatus)", "1.3.6.1.2.1.17.7.1.2.2.1.3"),
]

_SCALE_HINTS = (1.0, 0.1, 0.01, 0.001)
_POWER_FIELDS = ("rx_power", "tx_power", "olt_rx", "temperature", "distance", "mac")

OPTICAL_FILE = ROOT / "config" / "optical_oids.yaml"


class Tee:
    """Write to stdout and (optionally) a file at the same time."""

    def __init__(self, path: str | None) -> None:
        self.handle = Path(path).open("w", encoding="utf-8") if path else None

    def __call__(self, *parts: Any) -> None:
        line = " ".join(str(p) for p in parts)
        print(line)
        if self.handle:
            self.handle.write(line + "\n")

    def close(self) -> None:
        if self.handle:
            self.handle.close()


def looks_like_power(raw: int, scale: float) -> bool:
    """Heuristic: does raw*scale look like an optical power in dBm?"""
    value = raw * scale
    if abs(value) > 1e9:
        return False
    if value > 2**31:
        value -= 2**32
    return -45.0 <= value <= 10.0


def signed(value: int) -> int:
    if isinstance(value, int) and value > 2**31:
        return value - 2**32
    return value


# ---------------------------------------------------------------------------
#  column analysis (used by --autoconfig)
# ---------------------------------------------------------------------------
_DBM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*dBm", re.IGNORECASE)


def _is_row_counter(values: list[int]) -> bool:
    """A column that merely counts rows (0,1,2,.. n-1) is never real data.

    On the V1600D, ``...5.12.1.12.1.1`` is exactly that: the ONU row number,
    and 0.01 x (0..385) looks deceptively like a Tx power.
    """
    if len(values) < 8:
        return False
    unique = sorted(set(values))
    contiguous = unique == list(range(unique[0], unique[0] + len(unique)))
    return contiguous and len(unique) == len(values)


def _text_dbm(values: list[Any]) -> list[float]:
    """Pull the dBm figure out of text columns like '1.92 mW (2.84 dBm)'."""
    found: list[float] = []
    for value in values:
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", errors="ignore")
        if isinstance(value, str):
            match = _DBM_RE.search(value)
            if match:
                found.append(float(match.group(1)))
    return found


def _is_mac_text(value: Any) -> bool:
    text = str(value).strip().upper()
    if len(text) not in (12, 17):
        return False
    hex_only = "".join(ch for ch in text if ch in "0123456789ABCDEF")
    bare = text.replace(":", "").replace("-", "").replace(".", "")
    return len(hex_only) == 12 and len(bare) == 12


def _index_shape(indexes: list[str], ifindexes: set[int]) -> str:
    """How a table's row index maps onto the interface table."""
    if not indexes:
        return "unrelated"
    sample = indexes[:400]

    # a table indexed 0,1,2,..n-1 is counting its own rows - interface indexes
    # never start at 0, so this is unambiguous
    if all(i.isdigit() for i in sample) and len(set(sample)) > 1:
        nums = sorted({int(i) for i in sample})
        if nums[0] == 0 and nums == list(range(len(nums))):
            return "row"

    half = max(3, len(sample) // 2)
    if sum(1 for i in sample if i.isdigit() and int(i) in ifindexes) >= half:
        return "ifindex"
    if sum(
        1 for i in sample
        if i.rsplit(".", 1)[-1].isdigit() and int(i.rsplit(".", 1)[-1]) in ifindexes
    ) >= half:
        return "ifindex_tail"
    pairs = [i.split(".") for i in sample]
    if sum(1 for p in pairs if len(p) == 2 and p[0].isdigit() and p[1].isdigit()) >= half:
        return "pon_onu"
    if all(i.isdigit() for i in sample):
        return "row"
    return "unrelated"


def _shape_is_direct(shape: str) -> bool:
    """True when the collector can resolve this index without extra config."""
    return shape in ("ifindex", "ifindex_tail", "pon_onu")


def _is_varied(values: list[Any]) -> bool:
    """True when a column takes many different values (i.e. per-ONU data)."""
    if len(values) < 8:
        return False
    return len(set(values)) >= max(5, len(values) // 4)


def _merge_per_pon(
    columns: dict[str, dict[str, Any]], pon_ids: set[int] | None = None
) -> dict[str, dict[str, Any]]:
    """Fold tables published one-PON-at-a-time back into a single table.

    ``...5.12.2.1.8.1.6.<pon>.<row>`` gets split by the naive "strip the last
    sub-id" grouping into one pseudo-column per PON.  Those siblings all have a
    row index that is a dense ONU-id space, and the row counts differ - a very
    specific signature, so merging them back is safe.

    When the device told us which PON ids exist, only those are accepted as
    siblings, which keeps unrelated tables from being folded together.
    """
    parents: dict[str, list[str]] = {}
    for head in columns:
        parent, _, leaf = head.rpartition(".")
        if leaf.isdigit():
            parents.setdefault(parent, []).append(head)

    merged: dict[str, dict[str, Any]] = {}
    for parent, children in parents.items():
        if not 2 <= len(children) <= 64:
            continue
        counts: set[int] = set()
        ok = True
        for head in children:
            indexes = columns[head]["indexes"]
            if not indexes or not all(i.isdigit() for i in indexes):
                ok = False
                break
            nums = sorted(int(i) for i in indexes)
            # ONU ids are 1..n but rarely contiguous - deleted ONUs leave holes,
            # so tolerate gaps instead of demanding an exact 1..n run
            if len(set(nums)) != len(nums) or nums[0] < 1 or nums[-1] > len(nums) * 2 + 8:
                ok = False
                break
            counts.add(len(nums))
        if not ok or len(counts) < 2:
            continue

        leaves = {int(head.rpartition(".")[2]) for head in children}
        if pon_ids:
            if not leaves <= pon_ids or len(leaves) < max(2, len(pon_ids) // 2):
                continue
        elif leaves != set(range(1, len(children) + 1)):
            continue

        values: list[Any] = []
        indexes: list[str] = []
        for head in sorted(children, key=lambda h: int(h.rpartition(".")[2])):
            leaf = head.rpartition(".")[2]
            for value, index in zip(columns[head]["values"], columns[head]["indexes"]):
                values.append(value)
                indexes.append(f"{leaf}.{index}")
        merged[parent] = {"values": values, "indexes": indexes}

    for parent, children in parents.items():
        if parent in merged:
            for child in children:
                columns.pop(child, None)
    columns.update(merged)
    return columns


def _find_index_map(
    columns: dict[str, dict[str, Any]],
    integer_cols: dict[str, list[int]],
    row_heads: list[str],
) -> dict[str, str]:
    """Find the two columns that turn a table's row number into (PON, ONU id).

    On the V1600D the ONU inventory (``...5.12.1.12.1``) is indexed by a plain
    row number, and columns ``.2`` / ``.3`` hold the PON and the ONU id.
    """
    best: tuple | None = None
    for row_head in row_heads:
        wanted = set(columns[row_head]["indexes"])
        if len(wanted) < 8:
            continue
        # a (pon, onu) column pair must live in the very same table as the
        # target, i.e. cover exactly the same row indexes under the same parent
        row_parent = row_head.rpartition(".")[0]
        shared = []
        for head, nums in integer_cols.items():
            if head == row_head or len(nums) < 8:
                continue
            if head.rpartition(".")[0] != row_parent:
                continue
            if set(columns[head]["indexes"]) == wanted:
                shared.append(head)
        if not shared:
            continue

        stats = []
        for head in shared:
            values = [signed(v) for v in integer_cols[head]]
            if not values or min(values) < 1:
                continue
            distinct = len(set(values))
            if distinct < 2 or len(values) < distinct * 1.5:
                # too unique to be a PON or an ONU id - it is another row counter
                continue
            stats.append((head, min(values), max(values), distinct, len(values)))
        if not stats:
            continue

        # PON: a handful of small, dense values (1..8 typically)
        pons = [s for s in stats if 2 <= s[3] <= 32 and s[2] <= 32]
        # ONU id: near-consecutive ids, repeated across rows
        onus = [s for s in stats if s[3] > 1 and s[2] <= 4096 and s[2] <= s[3] * 2 + 8]

        for pon in pons:
            for onu in onus:
                if onu[0] == pon[0] or onu[2] < pon[2] or onu[3] <= pon[3]:
                    continue
                # prefer the biggest table; ties go to the widest ONU id space
                score = (len(wanted), onu[3], -pon[2])
                if best is None or score > best[0]:
                    best = (score, pon[0], onu[0], row_head.rpartition(".")[0])

    if best is None:
        return {}
    return {"pon_oid": best[1], "onu_oid": best[2], "table": best[3]}


def _column_candidates(nums: list[int]) -> dict[str, tuple[float, float, float, float]]:
    """Best (scale, median, min, max) per optical field for one integer column."""
    out: dict[str, tuple[float, float, float]] = {}
    if len(nums) < 8:
        return out

    # ---- rx / olt rx: negative dBm, typically -8..-30 ----
    best = None
    for scale in _SCALE_HINTS:
        vals = [n * scale for n in nums]
        if min(vals) < -60.0 or max(vals) > 8.0:
            continue
        median = statistics.median(vals)
        if not -35.0 <= median <= -5.0:
            continue
        spread = max(vals) - min(vals)
        if spread > 45.0:
            continue
        score = 100.0 - abs(median + 18.0) - spread / 4.0
        if best is None or score > best[0]:
            best = (score, scale, median, min(vals), max(vals))
    if best is not None:
        out["rx"] = (best[1], best[2], best[3], best[4])

    # ---- tx: the ONU's own transmit power, usually slightly positive ----
    best = None
    for scale in _SCALE_HINTS:
        vals = [n * scale for n in nums]
        if min(vals) < -8.0 or max(vals) > 8.0:
            continue
        median = statistics.median(vals)
        if not 0.0 <= median <= 6.0:
            continue
        best = (median, scale, median, min(vals), max(vals))
        break
    if best is not None:
        out["tx"] = (best[1], best[2], best[3], best[4])

    # ---- temperature ----
    best = None
    for scale in (1.0, 0.1, 0.01):
        vals = [n * scale for n in nums]
        if min(vals) < -20.0 or max(vals) > 120.0:
            continue
        median = statistics.median(vals)
        if 10.0 <= median <= 80.0 and (max(vals) - min(vals)) < 100.0:
            best = (scale, median, min(vals), max(vals))
            break
    if best is not None:
        out["temperature"] = best

    # ---- distance (metres): a real drop is at least a few tens of metres ----
    best = None
    for scale in (1.0, 0.1):
        vals = [n * scale for n in nums]
        if min(vals) < 0.0 or max(vals) > 60000.0:
            continue
        median = statistics.median(vals)
        if median < 20.0 or max(vals) < 50.0:
            continue
        best = (scale, median, min(vals), max(vals))
        break
    if best is not None:
        out["distance"] = best

    return out


def analyse_columns(
    columns: dict[str, dict[str, Any]],
    ifindexes: set[int],
    onu_names: set[str],
) -> dict[str, Any]:
    """Turn a column dump into a best guess for each optical field."""
    integer_cols: dict[str, list[int]] = {}
    string_cols: dict[str, list[Any]] = {}
    text_power: dict[str, list[float]] = {}

    for head, info in columns.items():
        values = info["values"]
        if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            integer_cols[head] = [signed(v) for v in values]
        else:
            string_cols[head] = values
            dbm = _text_dbm(values)
            if dbm and len(dbm) >= max(5, len(values) * 6 // 10):
                text_power[head] = dbm

    shapes = {head: _index_shape(columns[head]["indexes"], ifindexes) for head in columns}

    # A table indexed by a plain row number is only usable when two other
    # columns turn that row number into (PON, ONU id) - see _find_index_map.
    row_heads = sorted({head for head, shape in shapes.items() if shape == "row"})[:40]
    index_map = _find_index_map(columns, integer_cols, row_heads) if row_heads else {}
    can_map_rows = bool(index_map)
    # the columns that make up the mapping are ids, never measurements
    mapped_heads = {index_map[k] for k in ("pon_oid", "onu_oid") if k in index_map}
    # ...and the table they live in is where the rest of the ONU data lives, so
    # anything from a *parallel* inventory table loses to it
    family = index_map.get("table")

    def family_bonus(head: str) -> float:
        return 200.0 if family and head.rpartition(".")[0] == family else 0.0

    # a column that contains the ONU interface names is the join key
    key_oid = None
    best_key = 0
    for head, values in string_cols.items():
        hits = sum(1 for v in values if str(v).strip().upper() in onu_names)
        if hits > best_key and hits >= max(5, len(onu_names) // 5):
            best_key, key_oid = hits, head

    scored: dict[str, list[tuple[float, str, float, tuple]]] = {
        "rx": [], "tx": [], "temperature": [], "distance": [],
    }

    for head, nums in integer_cols.items():
        if head in mapped_heads or _is_row_counter(nums):
            continue
        candidates = _column_candidates(nums)
        bonus = (25.0 if _shape_is_direct(shapes[head]) else 0.0) + family_bonus(head)
        for field, (scale, median, low, high) in candidates.items():
            # optical power differs on every ONU - a column with only a handful
            # of distinct values is a PON number or a status flag, not a dBm
            if field in ("rx", "tx") and not _is_varied(nums):
                continue
            scored[field].append((len(nums) + bonus, head, scale, (median, low, high)))

    # Text columns that spell out the dBm figure ("0.04 mW (-14.31 dBm)") are
    # the strongest possible evidence, so they beat any guessed integer scale.
    for head, dbm in text_power.items():
        if not _is_varied(dbm):
            continue
        median = statistics.median(dbm)
        entry = (len(dbm) + 1000.0 + family_bonus(head), head, 1.0,
                 (median, min(dbm), max(dbm)))
        scored["rx" if median < -1.0 else "tx"].append(entry)

    for field in scored:
        scored[field].sort(key=lambda item: item[0], reverse=True)

    chosen: dict[str, Any] = {}
    if key_oid:
        chosen["key_oid"] = key_oid

    used: set[str] = set(mapped_heads)

    def take(field: str, target: str) -> bool:
        for _score, head, scale, stats_ in scored[field]:
            if head in used:
                continue
            if shapes[head] == "row" and not can_map_rows:
                continue
            used.add(head)
            chosen[target] = {"oid": head, "scale": scale}
            chosen.setdefault("_stats", {})[target] = stats_
            chosen.setdefault("_shapes", {})[target] = shapes[head]
            return True
        return False

    take("rx", "rx_power")
    take("tx", "tx_power")

    # a second, equally strong negative column is usually the OLT-side rx power
    if chosen.get("rx_power") and scored["rx"]:
        best = scored["rx"][0][0]
        for score, head, scale, stats_ in scored["rx"]:
            if head in used or score < best * 0.7:
                continue
            if shapes[head] == "row" and not can_map_rows:
                continue
            used.add(head)
            chosen["olt_rx"] = {"oid": head, "scale": scale}
            chosen.setdefault("_stats", {})["olt_rx"] = stats_
            chosen.setdefault("_shapes", {})["olt_rx"] = shapes[head]
            break

    take("temperature", "temperature")
    take("distance", "distance")

    # MAC columns: 6 raw bytes, or text like "00:d3:9e:8b:30:1a"
    mac_heads: list[str] = []
    for head, values in string_cols.items():
        if len(values) < 8:
            continue
        if shapes[head] == "row" and not can_map_rows:
            continue
        macs = sum(1 for v in values if isinstance(v, (bytes, bytearray)) and len(v) == 6)
        macs += sum(1 for v in values if _is_mac_text(v))
        if macs >= max(5, len(values) * 8 // 10):
            mac_heads.append(head)
    if mac_heads:
        # again, the table that supplied the mapping wins over a parallel one
        mac_heads.sort(key=lambda h: (0 if family and h.rpartition(".")[0] == family else 1))
        head = mac_heads[0]
        chosen["mac"] = {"oid": head, "scale": 1.0}
        chosen.setdefault("_shapes", {})["mac"] = shapes[head]

    # tables indexed by a row number need the (pon, onu) mapping emitted too
    if index_map and any(chosen.get("_shapes", {}).get(f) == "row" for f in _POWER_FIELDS):
        chosen["index_map"] = {
            "pon_oid": index_map.get("pon_oid"),
            "onu_oid": index_map.get("onu_oid"),
        }

    return chosen


# ---------------------------------------------------------------------------
#  commands
# ---------------------------------------------------------------------------
def cmd_get(client: SnmpClient, oid: str, out: Tee) -> int:
    binds = client.get([oid])
    for bind in binds:
        out(f"{bind.oid}  tag=0x{bind.tag:02X}  value={bind.value!r}")
    return 0


def cmd_walk(client: SnmpClient, base: str, out: Tee, limit: int) -> list[dict[str, Any]]:
    binds = client.walk(base, max_rows=limit)
    out(f"--- walk {base}: {len(binds)} varbinds")
    for bind in binds:
        out(f"  {bind.oid:<60} = {bind.value}")
    return [{"oid": b.oid, "tag": b.tag, "value": b.value} for b in binds]


def collect_columns(client: SnmpClient, base: str, limit: int) -> dict[str, dict[str, Any]]:
    binds = client.walk(base, max_rows=limit)
    columns: dict[str, dict[str, Any]] = {}
    for bind in binds:
        head, _, tail = bind.oid.rpartition(".")
        column = columns.setdefault(head, {"values": [], "indexes": []})
        column["values"].append(bind.value)
        column["indexes"].append(tail)
    return columns


def cmd_columns(client: SnmpClient, base: str, out: Tee, limit: int) -> None:
    binds = client.walk(base, max_rows=limit)
    if not binds:
        out(f"--- {base}: nothing returned")
        return

    columns: dict[str, list[Any]] = {}
    for bind in binds:
        head, _, _tail = bind.oid.rpartition(".")
        columns.setdefault(head, []).append(bind.value)

    out(f"--- {base}: {len(binds)} varbinds in {len(columns)} columns")
    out("")
    for head in sorted(columns):
        values = columns[head]
        numbers = [v for v in values if isinstance(v, int)]
        sample = values[0]
        line = f"  {head:<52} n={len(values):<5}"
        if numbers and len(numbers) == len(values):
            low, high = min(numbers), max(numbers)
            line += f" int  min={signed(low):<12} max={signed(high):<12}"
            hints = []
            for scale in _SCALE_HINTS:
                if looks_like_power(signed(low), scale) and looks_like_power(signed(high), scale):
                    hints.append(f"x{scale}")
            if hints:
                best = hints[-1] if len(hints) > 1 else hints[0]
                scaled = signed(low) * float(best[1:])
                line += f"  <== possible dBm (scale {best[1:]}, e.g. {scaled:.2f})"
        else:
            text = str(sample)
            line += f" str  sample={text[:38]!r}"
            mac = to_mac(sample)
            if mac and len(str(sample)) in (6, 12, 17):
                line += f"  <== possible MAC ({mac})"
        out(line)

    out("")
    out("How to read this:")
    out("  * a column whose values are all integers and in the -45..+10 range is")
    out("    most likely an optical power.  If the device reports dBm x100 (-2150)")
    out("    put  rx_power: { oid: <column oid>, scale: 0.01 }")
    out("  * or just run  --autoconfig  and let this tool write the file for you")


def cmd_mac_table(client: SnmpClient, out: Tee, limit: int) -> dict[str, int]:
    """Probe the standard bridge tables; report how many rows each returns."""
    out("--- bridge / forwarding tables (used to link a user to an ONU)")
    out("")
    counts: dict[str, int] = {}
    for label, oid in FDB_TABLES:
        try:
            binds = client.walk(oid, max_rows=limit)
        except SnmpError as exc:
            out(f"  {label:<46} {oid}  ERROR {exc}")
            counts[oid] = 0
            continue
        counts[oid] = len(binds)
        out(f"  {label:<46} {oid}  n={len(binds)}")
        for bind in binds[:5]:
            tail = bind.oid.split(".")
            value: Any = bind.value
            if len(tail) >= 6 and label.startswith("Q-BRIDGE"):
                mac = ":".join(f"{int(x):02X}" for x in tail[-6:])
                out(f"      {bind.oid:<58} = {value}   (index MAC {mac})")
            else:
                out(f"      {bind.oid:<58} = {to_mac(value) if isinstance(value, (bytes, bytearray)) else value}")
        if len(binds) > 5:
            out(f"      ... and {len(binds) - 5} more")
    out("")
    if counts.get("1.3.6.1.2.1.17.4.3.1.1"):
        out("  RESULT: the OLT publishes a bridge MAC table - user <-> ONU linking")
        out("          will work out of the box (match method 'fdb').")
    else:
        out("  RESULT: no standard MAC table found.  Users can still be linked by")
        out("          ONU MAC or by name, but per-user dBm needs the FDB.  Send")
        out("          this output to your support contact for a vendor OID.")
    return counts


def cmd_autoconfig(
    client: SnmpClient, device: dict, out: Tee, limit: int, dry_run: bool, root: str, force: bool = False
) -> int:
    onu_regex = re.compile(str(dig(device, "classify.onu_regex", r"^EPON(\d+)ONU(\d+)$")))

    # The interface list is needed to recognise ifIndex-shaped tables, so fetch
    # it first - and retry once, because a slow agent that has just answered a
    # big walk sometimes drops the next request.
    if_name_oid = str(dig(device, "oids.if_name", "1.3.6.1.2.1.31.1.1.1.1"))
    names: dict[str, Any] = {}
    for _attempt in (1, 2):
        names = client.walk_map(if_name_oid)
        if names:
            break
        out("    (interface list came back empty, retrying)")

    out(f"--- autoconfig: walking {root} (this can take a few minutes)")
    binds = client.walk(root, max_rows=limit)
    truncated = len(binds) >= limit
    out(f"    {len(binds)} varbinds")
    if not binds:
        out("    nothing returned - is the vendor root correct?")
        return 1
    if truncated:
        out(f"    !! hit the {limit}-varbind limit - the walk did NOT cover the whole")
        out("       subtree, so anything below it is invisible to this analysis.")
        if not force:
            out("")
            out("    Refusing to write a half-analysed result.  Either raise the limit:")
            out(f"        --limit {limit * 4}")
            out("    or narrow the walk to the part of the tree that holds the optics,")
            out("    for example:")
            out("        --root 1.3.6.1.4.1.37950.1.1.5   (or ...5.12)")
            out("    Use --force to write it anyway.")
            return 1

    # ONU interface names + interface indexes
    onu_names: set[str] = set()
    onu_ifindexes: set[int] = set()
    all_ifindexes: set[int] = set()
    pon_ids: set[int] = set()
    for index, value in names.items():
        if not index.isdigit():
            continue
        all_ifindexes.add(int(index))
        text = str(value)
        match = onu_regex.match(text)
        if match and match.groups():
            pon_ids.add(int(match.group(1)))
            onu_names.add(text.strip().upper())
            onu_ifindexes.add(int(index))
    if names and not onu_names:
        onu_names = {str(v).strip().upper() for v in names.values()}
        onu_ifindexes = set(all_ifindexes)

    out(f"    {len(onu_names)} ONU interfaces, {len(all_ifindexes)} interfaces in total")
    if pon_ids:
        out(f"    PON ids seen in the ONU names: {sorted(pon_ids)}")

    columns: dict[str, dict[str, Any]] = {}
    for bind in binds:
        head, _, tail = bind.oid.rpartition(".")
        column = columns.setdefault(head, {"values": [], "indexes": []})
        column["values"].append(bind.value)
        column["indexes"].append(tail)

    # firmwares that publish "<table>.<pon>.<row>" get split into one pseudo
    # column per PON by the grouping above - put those back together
    columns = _merge_per_pon(columns, pon_ids)

    chosen = analyse_columns(columns, onu_ifindexes or all_ifindexes, onu_names)

    shapes = chosen.pop("_shapes", {})
    stats = chosen.pop("_stats", {})
    key_oid = chosen.get("key_oid")

    out("")
    out("--- result")
    if key_oid:
        out(f"  key_oid     = {key_oid}   (column containing the ONU name)")
    for field in _POWER_FIELDS:
        spec = chosen.get(field)
        if not isinstance(spec, dict) or not spec.get("oid"):
            out(f"  {field:<11} = not found")
            continue
        line = f"  {field:<11} = {spec['oid']}   scale={spec['scale']}"
        if field in shapes:
            line += f"   index={shapes[field]}"
        if field in stats:
            median, low, high = stats[field]
            line += f"   median={median:.2f} range={low:.2f}..{high:.2f}"
        out(line)
    mapping = chosen.get("index_map")
    if isinstance(mapping, dict):
        out(f"  index_map    = pon {mapping.get('pon_oid')}")
        out(f"                 onu {mapping.get('onu_oid')}")

    if not any(isinstance(chosen.get(f), dict) and chosen[f].get("oid") for f in _POWER_FIELDS):
        out("")
        out("  Could not identify any optical column.  Run --columns on the vendor")
        out("  root and send the output, or set olt.onu_tables by hand.")
        return 1

    payload: dict[str, Any] = {}
    if key_oid:
        payload["key_oid"] = key_oid
    if isinstance(mapping, dict):
        payload["index_map"] = mapping
    for field in _POWER_FIELDS:
        spec = chosen.get(field)
        if isinstance(spec, dict) and spec.get("oid"):
            payload[field] = {"oid": spec["oid"], "scale": spec["scale"]}
        else:
            payload[field] = {"oid": None, "scale": 1.0}

    name = device.get("name") or "OLT-1"
    document = {name: payload}
    if dry_run:
        out("")
        out("--- not written (--dry-run).  This is what would go to config/optical_oids.yaml:")
        out(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        return 0

    existing: dict[str, Any] = {}
    if OPTICAL_FILE.exists():
        try:
            loaded = yaml.safe_load(OPTICAL_FILE.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    # Merge field by field: a value that is already configured keeps winning,
    # so re-running this tool can never break a working setup.  --force
    # replaces the block wholesale.
    block = existing.get(name)
    if not isinstance(block, dict) or force:
        block = {}
    for key, value in payload.items():
        if key == "index_map":
            current_map = block.get("index_map")
            if not isinstance(current_map, dict) or not current_map.get("pon_oid"):
                block["index_map"] = value
            continue
        current = block.get(key)
        if isinstance(current, dict) and current.get("oid") and key != "key_oid":
            continue
        block[key] = value
    existing[name] = block

    OPTICAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPTICAL_FILE.write_text(
        "# Auto-generated by tools/discover_olt.py --autoconfig\n"
        + yaml.safe_dump(existing, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    out("")
    out(f"--- written to {OPTICAL_FILE}")
    out("    Restart the monitor to apply it:  sudo systemctl restart olt-monitor")
    return 0


# ---------------------------------------------------------------------------
#  main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover OLT OIDs over SNMP.")
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    parser.add_argument("--olt", default=None, help="which OLT (by name) to query")
    parser.add_argument("--get", metavar="OID", help="read a single OID")
    parser.add_argument("--walk", metavar="OID", help="walk a subtree and print everything")
    parser.add_argument("--columns", metavar="OID", help="walk a subtree and group it into columns")
    parser.add_argument("--presets", action="store_true", help="walk the standard + vendor subtrees")
    parser.add_argument("--mac-table", action="store_true", help="probe the bridge MAC tables")
    parser.add_argument("--autoconfig", action="store_true", help="detect the optical OIDs and write config/optical_oids.yaml")
    parser.add_argument("--dry-run", action="store_true", help="with --autoconfig: print the result instead of writing")
    parser.add_argument("--force", action="store_true", help="with --autoconfig: overwrite already-configured fields")
    parser.add_argument("--root", default="1.3.6.1.4.1.37950", help="vendor root for --autoconfig")
    parser.add_argument("--out", metavar="FILE", help="also write the output to FILE")
    parser.add_argument("--limit", type=int, default=60000, help="max varbinds per walk")
    args = parser.parse_args(argv)

    if not any([args.get, args.walk, args.columns, args.presets, args.mac_table, args.autoconfig]):
        args.presets = True

    cfg = load_config(args.config)
    db_file = resolve_db_path(cfg)
    init_db(db_file)
    registry.ensure_seeded(db_file, cfg)
    olts, _routers = registry.device_lists(db_file, cfg)
    if not olts:
        print("no OLT is configured - add one on the web Devices page or in config/config.yaml",
              file=sys.stderr)
        return 2
    if args.olt:
        match = [d for d in olts if d.get("name") == args.olt]
        if not match:
            print(f"no OLT named {args.olt!r} (have: {[d.get('name') for d in olts]})", file=sys.stderr)
            return 2
        device = match[0]
    else:
        device = olts[0]

    host = str(device.get("host") or "")
    if not host:
        print("OLT host is not set in config/config.yaml", file=sys.stderr)
        return 2

    out = Tee(args.out)
    out("=" * 78)
    out(f" OLT discovery - {device.get('name')}  {host}:{device.get('snmp_port', 161)}"
        f"  SNMP v{device.get('snmp_version', '2c')}")
    out(f" community={device.get('community')!r}   config={dig(cfg, '_config_path')}")
    out("=" * 78)
    out("")

    client = SnmpClient(
        host=host,
        community=str(device.get("community") or "public"),
        version=device.get("snmp_version", "2c"),
        port=int(device.get("snmp_port", 161)),
        timeout=float(device.get("timeout_seconds", 4)),
        retries=int(device.get("retries", 2)),
        max_repetitions=int(dig(cfg, "polling.snmp_max_repetitions", 25)),
    )

    try:
        try:
            descr = client.get_value("1.3.6.1.2.1.1.1.0")
            uptime = client.get_value("1.3.6.1.2.1.1.3.0")
            out(f"sysDescr  = {descr}")
            out(f"sysUpTime = {format_ticks(uptime if isinstance(uptime, int) else None)}")
            out("")
        except SnmpError as exc:
            print(f"\nSNMP did not answer: {exc}", file=sys.stderr)
            return 1

        if args.get:
            cmd_get(client, args.get, out)
            out("")

        if args.walk:
            cmd_walk(client, args.walk, out, args.limit)
            out("")

        if args.columns:
            cmd_columns(client, args.columns, out, args.limit)
            out("")

        if args.presets:
            for label, oid in PRESETS:
                try:
                    binds = client.walk(oid, max_rows=args.limit)
                except SnmpError as exc:
                    out(f"--- {label} ({oid}) failed: {exc}")
                    continue
                out(f"--- {label} ({oid}): {len(binds)} varbinds")
                if len(binds) <= 30:
                    for bind in binds:
                        out(f"  {bind.oid:<60} = {bind.value}")
                else:
                    out(f"  ... {len(binds)} rows, showing the first 5")
                    for bind in binds[:5]:
                        out(f"  {bind.oid:<60} = {bind.value}")
                out("")
                if oid == "1.3.6.1.4.1.37950":
                    cmd_columns(client, oid, out, args.limit)

        if args.mac_table:
            cmd_mac_table(client, out, args.limit)
            out("")

        if args.autoconfig:
            rc = cmd_autoconfig(client, device, out, args.limit, args.dry_run, args.root, args.force)
            out("")
            if rc:
                return rc
    finally:
        client.close()
        out.close()

    if args.out:
        print(f"\nSaved to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
