"""Fill slow GPON optical/inventory columns in small, resumable steps.

Some GPON SNMP agents (notably the VSOL V1600G1B) stop answering when a poll
asks for many table columns at once.  This tool walks one small group of
columns at a time and writes each group to ``olt_onu_current`` immediately, so
the web UI fills in progressively instead of waiting for one huge poll.

Usage::

    py -3.12 tools/bootstrap_optical.py --olt OLT-Core --brand vsol --pon gpon
    py -3.12 tools/bootstrap_optical.py --olt "SERVER GPON" --brand bdcom --pon gpon

The normal scheduler does the same work automatically after the first poll; the
tool exists for a one-off warm start and for diagnosing a new OLT.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from olt_monitor import registry
from olt_monitor.collectors.olt import OltCollector
from olt_monitor.config import (
    DEFAULT_MAC_TABLE,
    DEFAULT_ONU_TABLES,
    MAC_TABLE_PROFILES,
    ONU_TABLE_PROFILES,
    db_path as resolve_db_path,
    load_config,
)
from olt_monitor.db import configure as configure_db
from olt_monitor.db import executemany, query, utcnow_iso
from olt_monitor.snmp import SnmpClient

GROUPS = [
    ("live optical", ("rx_power", "tx_power", "temperature"),
     ("rx_power_dbm", "tx_power_dbm", "temperature_c")),
    ("inventory", ("serial", "model"), ("serial", "model")),
    ("ONU MAC / VLAN", ("mac", "vlan"), ("mac", "vlan")),
    ("static optical", ("voltage", "bias", "distance"),
     ("voltage_v", "bias_current_ma", "distance_m")),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--olt", required=True)
    parser.add_argument("--brand", default="")
    parser.add_argument("--pon", default="")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--fdb-only", action="store_true")
    parser.add_argument("--skip-fdb", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    configure_db(cfg)
    dbp = resolve_db_path(cfg)
    device = next(
        (d for d in registry.list_devices(dbp, cfg, "olt") if d["name"] == args.olt),
        None,
    )
    if device is None:
        print(f"no OLT named {args.olt!r}", file=sys.stderr)
        return 2
    brand = (args.brand or device.get("brand") or "auto").lower()
    pon = (args.pon or device.get("pon_type") or "gpon").lower()
    profile = ONU_TABLE_PROFILES.get((brand, pon))
    if not profile:
        print(f"no built-in GPON profile for {brand}/{pon}", file=sys.stderr)
        return 2

    onus = query(
        dbp,
        "SELECT ifindex, pon, onu_id, if_name, oper_status"
        "  FROM olt_onu_current WHERE olt_name = ?"
        " ORDER BY pon, onu_id",
        [args.olt],
    )
    if not onus:
        print("no ONUs stored yet - poll the OLT once before bootstrapping", file=sys.stderr)
        return 2

    collector = OltCollector(cfg, device)
    collector.brand = brand
    collector.pon_type = pon
    collector.use_batch_walk = not (brand == "vsol" and pon == "gpon")
    collector._ifindexes = {int(row["ifindex"]) for row in onus}
    collector._fdb_cache_path = (
        Path(dbp).parent / f"fdb_{re.sub(r'[^A-Za-z0-9_.-]+', '_', args.olt)}.json"
    )
    collector.mac_table = {
        **DEFAULT_MAC_TABLE,
        **MAC_TABLE_PROFILES.get((brand, pon), {}),
    }
    client = SnmpClient(
        device["host"],
        device["community"],
        version=device.get("snmp_version", "2c"),
        port=int(device.get("snmp_port") or 161),
        timeout=args.timeout,
        retries=1,
        max_repetitions=10,
    )

    print(f"{args.olt}: {len(onus)} ONUs, profile {brand}/{pon}", flush=True)
    if not args.fdb_only:
        for title, keys, columns in GROUPS:
            collector.onu_tables = {
                **DEFAULT_ONU_TABLES,
                **{key: profile[key] for key in keys if key in profile},
            }
            collector._optical_cache = {}
            collector._optical_poll = 0
            started = time.time()
            note = collector._apply_optical(client, onus)
            elapsed = round(time.time() - started, 1)
            stamp = utcnow_iso()
            for column in columns:
                values = [
                    (row.get(column), stamp, args.olt, row["ifindex"])
                    for row in onus
                    if row.get(column) is not None
                ]
                if values:
                    executemany(
                        dbp,
                        f"UPDATE olt_onu_current SET {column} = ?, updated_at = ?"
                        " WHERE olt_name = ? AND ifindex = ?",
                        values,
                    )
            counts = {
                column: sum(1 for row in onus if row.get(column) is not None)
                for column in columns
            }
            print(
                f"{time.strftime('%H:%M:%S')} {title}: {elapsed}s {counts}"
                + (f" note={note}" if note else ""),
                flush=True,
            )

    if not args.skip_fdb:
        started = time.time()
        note = collector._refresh_mac_table(client, onus)
        elapsed = round(time.time() - started, 1)
        print(
            f"{time.strftime('%H:%M:%S')} subscriber FDB: {elapsed}s "
            f"{len(collector._mac_map)} MACs / {len(collector._mac_vlans)} VLANs"
            + (f" note={note}" if note else ""),
            flush=True,
        )

    client.close()
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
