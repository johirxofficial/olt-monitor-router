#!/usr/bin/env python3
"""Create the MySQL database, application user and tables for olt-monitor-router.

Run this once after cloning / after installing MySQL:

    python3 tools/setup_mysql.py

The target connection (database name, app user, password) is read from
``config/config.yaml`` + ``.env``.  The *admin* account used to create the
database defaults to ``root``; its password is asked interactively or can be
given with ``MYSQL_ADMIN_PASSWORD`` / ``--admin-password``.

Nothing else on the MySQL server is touched: the script only creates/updates
the database named in the config (plus ``<name>_selftest``, used by
``tools/selftest.py``), and only grants that user access to those two.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymysql

from olt_monitor import db
from olt_monitor.config import dig, load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-user", default=os.environ.get("MYSQL_ADMIN_USER", "root"))
    parser.add_argument("--admin-password", default=os.environ.get("MYSQL_ADMIN_PASSWORD"))
    parser.add_argument("--admin-host", default=os.environ.get("MYSQL_ADMIN_HOST", "127.0.0.1"))
    parser.add_argument("--admin-port", type=int, default=int(os.environ.get("MYSQL_ADMIN_PORT", "3306")))
    parser.add_argument("--migrate", metavar="SQLITE_FILE", default=None,
                        help="after setup, copy an old data/olt_monitor.db into MySQL")
    parser.add_argument("--migrate-all", action="store_true",
                        help="when migrating, also copy the (large) history tables")
    parser.add_argument("--skip-create-user", action="store_true",
                        help="database + tables only, use the current MySQL user")
    parser.add_argument("--no-test-db", action="store_true",
                        help="do not create the <name>_selftest database")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    target = db._settings_from_cfg(cfg)
    database = target["database"]
    app_user = target["user"]
    app_password = target["password"]

    if not app_password:
        print("[!] DB password is empty. Set DB_PASSWORD in .env first.", file=sys.stderr)
        return 2

    password = args.admin_password
    if password is None:
        password = getpass.getpass(f"MySQL admin password for {args.admin_user}@{args.admin_host}: ")

    print(f"[*] connecting to MySQL {args.admin_host}:{args.admin_port} as {args.admin_user}")
    try:
        admin = pymysql.connect(
            host=args.admin_host,
            port=args.admin_port,
            user=args.admin_user,
            password=password,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
        )
    except pymysql.err.OperationalError as exc:
        print(f"[!] cannot connect as MySQL admin: {exc}", file=sys.stderr)
        print("    Pass --admin-password (or set MYSQL_ADMIN_PASSWORD).", file=sys.stderr)
        return 2

    test_database = None if args.no_test_db else f"{database}_selftest"
    databases = [database] + ([test_database] if test_database else [])

    try:
        with admin.cursor() as cur:
            for name in databases:
                print(f"[*] creating database {name!r} (if missing)")
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )

            if not args.skip_create_user:
                print(f"[*] creating user {app_user!r} for local connections")
                for host in ("localhost", "127.0.0.1"):
                    cur.execute(
                        "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                        (app_user, host, app_password),
                    )
                    cur.execute(
                        "ALTER USER %s@%s IDENTIFIED BY %s",
                        (app_user, host, app_password),
                    )
                    for name in databases:
                        cur.execute(
                            f"GRANT ALL PRIVILEGES ON `{name}`.* TO %s@%s",
                            (app_user, host),
                        )
                cur.execute("FLUSH PRIVILEGES")
    finally:
        admin.close()

    # Now use exactly the same code path the application uses.
    db.configure(cfg)
    db.init_db()
    print(f"[ok] schema ready in {db.describe()}")
    tables = db.query(
        None,
        "SELECT TABLE_NAME AS name FROM information_schema.TABLES"
        " WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME",
    )
    print(f"[ok] {len(tables)} table(s): " + ", ".join(row["name"] for row in tables))

    if test_database:
        test_cfg = {"database": {**cfg.get("database", {}), "name": test_database}}
        db.configure(test_cfg)
        db.init_db()
        print(f"[ok] self-test schema ready in {db.describe()}")

    if args.migrate:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from migrate_sqlite_to_mysql import migrate

        migrate(Path(args.migrate), history=args.migrate_all)

    print("\nDone.  Start the monitor with:  ./start.sh   (or python3 run.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
