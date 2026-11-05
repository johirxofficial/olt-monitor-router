#!/usr/bin/env python3
"""Copy an old SQLite database (``data/olt_monitor.db``) into MySQL.

    python3 tools/migrate_sqlite_to_mysql.py                 # current + small tables
    python3 tools/migrate_sqlite_to_mysql.py --all           # + history tables
    python3 tools/migrate_sqlite_to_mysql.py --file old.db --all
    python3 tools/migrate_sqlite_to_mysql.py --truncate      # empty target first

The source file is opened read-only.  Rows are copied in batches, so a few
million history rows take a minute or two instead of exhausting memory.
Secrets in ``devices.config`` stay encrypted: the same ``data/.secret_key`` is
used after migration.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from olt_monitor import db
from olt_monitor.config import db_path as resolve_db_path
from olt_monitor.config import load_config

BATCH = 1000

#: order matters only for readability; there are no foreign keys
CURRENT_TABLES = (
    "meta",
    "devices",
    "device_current",
    "olt_port_current",
    "olt_onu_current",
    "olt_health_current",
    "router_iface_current",
    "pppoe_user_current",
    "alerts_current",
)
HISTORY_TABLES = (
    "olt_onu_history",
    "pppoe_user_history",
    "port_history",
    "router_history",
    "olt_health_history",
)


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _mysql_columns(table: str) -> list[str]:
    rows = db.query(
        None,
        "SELECT COLUMN_NAME AS name, EXTRA AS extra FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        [table],
    )
    return [row["name"] for row in rows if "GENERATED" not in str(row.get("extra") or "").upper()]


def _copy_table(
    src: sqlite3.Connection,
    table: str,
    *,
    truncate: bool,
    dry_run: bool,
) -> int:
    try:
        sqlite_cols = _sqlite_columns(src, table)
    except sqlite3.Error:
        return 0
    mysql_cols = _mysql_columns(table)
    if not mysql_cols:
        print(f"  - {table}: no matching MySQL table, skipped")
        return 0

    cols = [col for col in sqlite_cols if col in mysql_cols]
    if not cols:
        print(f"  - {table}: no shared columns, skipped")
        return 0

    total = int(src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    if dry_run:
        print(f"  - {table}: would copy {total} row(s) ({len(cols)} columns)")
        return total

    conn = db.connect()
    if truncate:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM `{table}`")
        conn.commit()

    insert = (
        f"INSERT INTO `{table}` ({', '.join('`' + c + '`' for c in cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})"
        f" ON DUPLICATE KEY UPDATE "
        + ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols)
    )

    started = time.monotonic()
    copied = 0
    cursor = src.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"')
    while True:
        rows = cursor.fetchmany(BATCH)
        if not rows:
            break
        data: list[list[Any]] = []
        for row in rows:
            values: list[Any] = []
            for value in row:
                if isinstance(value, bool):
                    values.append(int(value))
                else:
                    values.append(value)
            data.append(values)
        with conn.cursor() as cur:
            cur.executemany(insert, data)
        conn.commit()
        copied += len(data)
        if total:
            pct = copied * 100 // total
            print(f"\r  - {table}: {copied}/{total} ({pct}%)", end="", flush=True)
    elapsed = time.monotonic() - started
    print(f"\r  - {table}: {copied} row(s) in {elapsed:.1f}s" + " " * 12)
    return copied


def migrate(path: Path, *, history: bool = False, truncate: bool = False, dry_run: bool = False) -> int:
    if not path.exists():
        print(f"[!] {path} does not exist", file=sys.stderr)
        return 2

    db.init_db()
    uri = f"file:{path}?mode=ro&immutable=1"
    src = sqlite3.connect(uri, uri=True)
    src.row_factory = None

    tables = list(CURRENT_TABLES) + (list(HISTORY_TABLES) if history else [])
    print(f"[*] copying {path} -> {db.describe()}")
    print(f"[*] tables: {', '.join(tables)}")
    total = 0
    try:
        for table in tables:
            total += _copy_table(src, table, truncate=truncate, dry_run=dry_run)
    finally:
        src.close()

    print(f"[ok] {total} row(s) {'would be ' if dry_run else ''}copied")
    return 0


def main() -> int:
    cfg = load_config()
    default_file = resolve_db_path(cfg)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=str(default_file), help="SQLite file to import")
    parser.add_argument("--all", action="store_true", help="also copy history tables (large)")
    parser.add_argument("--truncate", action="store_true", help="empty each target table first")
    parser.add_argument("--dry-run", action="store_true", help="only print what would be copied")
    args = parser.parse_args()

    return migrate(
        Path(args.file).expanduser(),
        history=args.all,
        truncate=args.truncate,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
