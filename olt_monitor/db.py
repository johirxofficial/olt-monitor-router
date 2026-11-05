"""MySQL storage layer.

Every piece of state lives in MySQL: the device registry, the current
snapshot of every OLT/router, the alert list and all history tables.

Two kinds of tables:

* ``*_current``  - one row per object, upserted on every poll (fast for the UI)
* ``*_history``  - append-only rows used for trends / graphs

No ORM: only :mod:`pymysql` (pure Python) is used.

The public helpers keep the historical signature ``fn(db_path, sql, params)``
so the rest of the code did not have to change.  ``db_path`` is ignored for
the connection - MySQL settings come from the ``database:`` block in
``config.yaml`` / the environment (see :func:`configure`) - but it still points
at the local data directory used for the encryption key and the per-OLT
forwarding-table cache.

SQL is written with ``?`` placeholders throughout the project; they are
translated to MySQL's ``%s`` here, in one place.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import pymysql
from pymysql.cursors import DictCursor

_LOCK = threading.RLock()
_CONFIG: dict[str, Any] | None = None
_CONFIG_LOCK = threading.Lock()

#: how long a web request / poll statement may wait for a lock
LOCK_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
#  schema
# ---------------------------------------------------------------------------
TABLES: tuple[str, ...] = (
    "meta",
    "devices",
    "device_current",
    "olt_mac_cache",
    "olt_port_current",
    "olt_onu_current",
    "olt_health_current",
    "router_iface_current",
    "pppoe_user_current",
    "alerts_current",
    "olt_onu_history",
    "pppoe_user_history",
    "port_history",
    "router_history",
    "olt_health_history",
    "users",
    "site_settings",
    "audit_log",
)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        `key`   VARCHAR(191) NOT NULL,
        `value` LONGTEXT NULL,
        PRIMARY KEY (`key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        id         BIGINT NOT NULL AUTO_INCREMENT,
        kind       VARCHAR(16)  NOT NULL,
        name       VARCHAR(191) NOT NULL,
        enabled    TINYINT      NOT NULL DEFAULT 1,
        `position` INT          NOT NULL DEFAULT 0,
        config     LONGTEXT     NOT NULL,
        created_at VARCHAR(40)  NULL,
        updated_at VARCHAR(40)  NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uk_devices_kind_name (kind, name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS device_current (
        `key`      VARCHAR(191) NOT NULL,
        kind       VARCHAR(16)  NULL,
        name       VARCHAR(191) NULL,
        ok         TINYINT      NULL,
        payload    LONGTEXT     NULL,
        error      TEXT         NULL,
        updated_at VARCHAR(40)  NULL,
        PRIMARY KEY (`key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS olt_mac_cache (
        olt_name   VARCHAR(191) NOT NULL,
        payload    LONGTEXT     NOT NULL,
        updated_at VARCHAR(40)  NULL,
        PRIMARY KEY (olt_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS olt_port_current (
        olt_name          VARCHAR(191) NOT NULL,
        ifindex           BIGINT       NOT NULL,
        if_name           VARCHAR(191) NULL,
        if_descr          VARCHAR(512) NULL,
        kind              VARCHAR(32)  NULL,
        pon_type          VARCHAR(32)  NULL,
        max_onus          INT          NULL,
        admin_status      INT          NULL,
        oper_status       INT          NULL,
        hc_in_octets      BIGINT       NULL,
        hc_out_octets     BIGINT       NULL,
        in_errors         BIGINT       NULL,
        out_errors        BIGINT       NULL,
        in_bps            DOUBLE       NULL,
        out_bps           DOUBLE       NULL,
        sfp_rx_power_dbm  DOUBLE       NULL,
        sfp_tx_power_dbm  DOUBLE       NULL,
        sfp_temperature_c DOUBLE       NULL,
        updated_at        VARCHAR(40)  NULL,
        PRIMARY KEY (olt_name, ifindex),
        KEY idx_port_kind (kind)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS olt_onu_current (
        olt_name         VARCHAR(191) NOT NULL,
        ifindex          BIGINT       NOT NULL,
        if_name          VARCHAR(191) NULL,
        if_descr         VARCHAR(512) NULL,
        pon              INT          NULL,
        onu_id           INT          NULL,
        admin_status     INT          NULL,
        oper_status      INT          NULL,
        mac              VARCHAR(64)  NULL,
        serial           VARCHAR(128) NULL,
        model            VARCHAR(191) NULL,
        vendor           VARCHAR(128) NULL,
        sw_version       VARCHAR(128) NULL,
        onu_type         VARCHAR(128) NULL,
        vlan             VARCHAR(128) NULL,
        rx_power_dbm     DOUBLE       NULL,
        tx_power_dbm     DOUBLE       NULL,
        olt_rx_power_dbm DOUBLE       NULL,
        temperature_c    DOUBLE       NULL,
        voltage_v        DOUBLE       NULL,
        bias_current_ma  DOUBLE       NULL,
        distance_m       DOUBLE       NULL,
        last_down_reason VARCHAR(255) NULL,
        last_down_at     VARCHAR(40)  NULL,
        hc_in_octets     BIGINT       NULL,
        hc_out_octets    BIGINT       NULL,
        in_bps           DOUBLE       NULL,
        out_bps          DOUBLE       NULL,
        pppoe_user       VARCHAR(191) NULL,
        updated_at       VARCHAR(40)  NULL,
        PRIMARY KEY (olt_name, ifindex),
        KEY idx_onu_mac (mac),
        KEY idx_onu_pon (olt_name, pon),
        KEY idx_onu_user (pppoe_user)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS olt_health_current (
        olt_name        VARCHAR(191) NOT NULL,
        ok              TINYINT      NULL,
        uptime_seconds  BIGINT       NULL,
        cpu_percent     DOUBLE       NULL,
        memory_percent  DOUBLE       NULL,
        memory_total_mb DOUBLE       NULL,
        memory_free_mb  DOUBLE       NULL,
        temperature_c   DOUBLE       NULL,
        snmp_rtt_ms     DOUBLE       NULL,
        telnet_ok       TINYINT      NULL,
        telnet_ms       INT          NULL,
        telnet_error    TEXT         NULL,
        error           TEXT         NULL,
        updated_at      VARCHAR(40)  NULL,
        PRIMARY KEY (olt_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS router_iface_current (
        router_name       VARCHAR(191) NOT NULL,
        name              VARCHAR(191) NOT NULL,
        iface_type        VARCHAR(64)  NULL,
        vlan_id           INT          NULL,
        running           TINYINT      NULL,
        disabled          TINYINT      NULL,
        rx_byte           BIGINT       NULL,
        tx_byte           BIGINT       NULL,
        rx_bps            DOUBLE       NULL,
        tx_bps            DOUBLE       NULL,
        sfp_rx_power_dbm  DOUBLE       NULL,
        sfp_tx_power_dbm  DOUBLE       NULL,
        sfp_temperature_c DOUBLE       NULL,
        updated_at        VARCHAR(40)  NULL,
        PRIMARY KEY (router_name, name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS pppoe_user_current (
        router_name      VARCHAR(191) NOT NULL,
        name             VARCHAR(191) NOT NULL,
        service          VARCHAR(191) NULL,
        caller_id        VARCHAR(64)  NULL,
        address          VARCHAR(64)  NULL,
        uptime_seconds   BIGINT       NULL,
        uptime_text      VARCHAR(64)  NULL,
        bytes_in         BIGINT       NULL,
        bytes_out        BIGINT       NULL,
        session_id       VARCHAR(128) NULL,
        iface            VARCHAR(191) NULL,
        olt_vlan         INT          NULL,
        access_iface     VARCHAR(191) NULL,
        access_source    VARCHAR(32)  NULL,
        onu_ifindex      BIGINT       NULL,
        onu_name         VARCHAR(191) NULL,
        onu_olt_name     VARCHAR(191) NULL,
        onu_pon          INT          NULL,
        match_method     VARCHAR(32)  NULL,
        rx_power_dbm     DOUBLE       NULL,
        tx_power_dbm     DOUBLE       NULL,
        olt_rx_power_dbm DOUBLE       NULL,
        distance_m       DOUBLE       NULL,
        updated_at       VARCHAR(40)  NULL,
        PRIMARY KEY (router_name, name),
        KEY idx_user_onu (onu_olt_name, onu_ifindex),
        KEY idx_user_vlan (olt_vlan)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS alerts_current (
        id         BIGINT NOT NULL AUTO_INCREMENT,
        ts         VARCHAR(40)  NULL,
        severity   VARCHAR(16)  NULL,
        scope      VARCHAR(64)  NULL,
        ref        VARCHAR(255) NULL,
        message    TEXT         NULL,
        pppoe_user VARCHAR(191) NULL,
        active     TINYINT      NOT NULL DEFAULT 1,
        key_hash   CHAR(64) GENERATED ALWAYS AS (
                       SHA2(CONCAT_WS(CHAR(31), scope, ref, message), 256)
                   ) STORED,
        PRIMARY KEY (id),
        UNIQUE KEY uk_alerts_key (key_hash),
        KEY idx_alerts_ts (ts),
        KEY idx_alerts_active (active, severity)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS olt_onu_history (
        id             BIGINT NOT NULL AUTO_INCREMENT,
        ts             VARCHAR(40)  NULL,
        olt_name       VARCHAR(191) NULL,
        ifindex        BIGINT       NULL,
        if_name        VARCHAR(191) NULL,
        oper_status    INT          NULL,
        rx_power_dbm   DOUBLE       NULL,
        tx_power_dbm   DOUBLE       NULL,
        olt_rx_power_dbm DOUBLE     NULL,
        distance_m     DOUBLE       NULL,
        PRIMARY KEY (id),
        KEY idx_onu_hist (olt_name, ifindex, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS pppoe_user_history (
        id             BIGINT NOT NULL AUTO_INCREMENT,
        ts             VARCHAR(40)  NULL,
        router_name    VARCHAR(191) NULL,
        name           VARCHAR(191) NULL,
        address        VARCHAR(64)  NULL,
        uptime_seconds BIGINT       NULL,
        bytes_in       BIGINT       NULL,
        bytes_out      BIGINT       NULL,
        rx_power_dbm   DOUBLE       NULL,
        PRIMARY KEY (id),
        KEY idx_user_hist (router_name, name, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS port_history (
        id          BIGINT NOT NULL AUTO_INCREMENT,
        ts          VARCHAR(40)  NULL,
        olt_name    VARCHAR(191) NULL,
        ifindex     BIGINT       NULL,
        if_name     VARCHAR(191) NULL,
        kind        VARCHAR(32)  NULL,
        oper_status INT          NULL,
        in_bps      DOUBLE       NULL,
        out_bps     DOUBLE       NULL,
        PRIMARY KEY (id),
        KEY idx_port_hist (olt_name, ifindex, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS router_history (
        id                  BIGINT NOT NULL AUTO_INCREMENT,
        ts                  VARCHAR(40)  NULL,
        router_name         VARCHAR(191) NULL,
        cpu_load            DOUBLE       NULL,
        free_memory_percent DOUBLE       NULL,
        uptime_seconds      BIGINT       NULL,
        pppoe_online        INT          NULL,
        PRIMARY KEY (id),
        KEY idx_router_hist (router_name, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS olt_health_history (
        id             BIGINT NOT NULL AUTO_INCREMENT,
        ts             VARCHAR(40)  NULL,
        olt_name       VARCHAR(191) NULL,
        uptime_seconds BIGINT       NULL,
        cpu_percent    DOUBLE       NULL,
        memory_percent DOUBLE       NULL,
        temperature_c  DOUBLE       NULL,
        snmp_rtt_ms    DOUBLE       NULL,
        PRIMARY KEY (id),
        KEY idx_health_hist (olt_name, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ---- web users / roles ------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS users (
        id            BIGINT NOT NULL AUTO_INCREMENT,
        username      VARCHAR(64)  NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role          VARCHAR(16)  NOT NULL DEFAULT 'viewer',
        display_name  VARCHAR(191) NULL,
        email         VARCHAR(191) NULL,
        active        TINYINT      NOT NULL DEFAULT 1,
        session_epoch BIGINT       NOT NULL DEFAULT 0,
        perm_devices  TINYINT      NOT NULL DEFAULT 0,
        perm_tools    TINYINT      NOT NULL DEFAULT 1,
        perm_settings TINYINT      NOT NULL DEFAULT 0,
        perm_users    TINYINT      NOT NULL DEFAULT 0,
        perm_developer TINYINT     NOT NULL DEFAULT 0,
        perm_sql      TINYINT      NOT NULL DEFAULT 0,
        created_at    VARCHAR(40)  NULL,
        updated_at    VARCHAR(40)  NULL,
        last_login_at VARCHAR(40)  NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uk_users_username (username),
        KEY idx_users_role (role)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ---- site-wide settings (name, tagline, about, port, ...) -------------
    """
    CREATE TABLE IF NOT EXISTS site_settings (
        `key`   VARCHAR(191) NOT NULL,
        `value` LONGTEXT NULL,
        PRIMARY KEY (`key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ---- audit trail of privileged actions --------------------------------
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id       BIGINT NOT NULL AUTO_INCREMENT,
        ts       VARCHAR(40)  NULL,
        username VARCHAR(64)  NULL,
        action   VARCHAR(64)  NULL,
        detail   TEXT         NULL,
        ip       VARCHAR(64)  NULL,
        PRIMARY KEY (id),
        KEY idx_audit_ts (ts),
        KEY idx_audit_user (username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)

# Columns added after a database already existed.  ``init_db`` only adds the
# ones that are missing, so this doubles as the upgrade path.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("olt_onu_current", "pppoe_user", "VARCHAR(191) NULL"),
    ("olt_onu_current", "serial", "VARCHAR(128) NULL"),
    ("olt_onu_current", "model", "VARCHAR(191) NULL"),
    ("olt_onu_current", "vendor", "VARCHAR(128) NULL"),
    ("olt_onu_current", "sw_version", "VARCHAR(128) NULL"),
    ("olt_onu_current", "onu_type", "VARCHAR(128) NULL"),
    ("olt_onu_current", "vlan", "VARCHAR(128) NULL"),
    ("olt_onu_current", "voltage_v", "DOUBLE NULL"),
    ("olt_onu_current", "bias_current_ma", "DOUBLE NULL"),
    ("olt_onu_current", "last_down_reason", "VARCHAR(255) NULL"),
    ("olt_onu_current", "last_down_at", "VARCHAR(40) NULL"),
    ("olt_port_current", "pon_type", "VARCHAR(32) NULL"),
    ("olt_port_current", "max_onus", "INT NULL"),
    ("olt_port_current", "sfp_rx_power_dbm", "DOUBLE NULL"),
    ("olt_port_current", "sfp_tx_power_dbm", "DOUBLE NULL"),
    ("olt_port_current", "sfp_temperature_c", "DOUBLE NULL"),
    ("pppoe_user_current", "olt_vlan", "INT NULL"),
    ("pppoe_user_current", "access_iface", "VARCHAR(191) NULL"),
    ("pppoe_user_current", "access_source", "VARCHAR(32) NULL"),
    ("pppoe_user_current", "onu_olt_name", "VARCHAR(191) NULL"),
    ("pppoe_user_current", "onu_pon", "INT NULL"),
    ("pppoe_user_current", "match_method", "VARCHAR(32) NULL"),
    ("router_iface_current", "vlan_id", "INT NULL"),
    ("alerts_current", "pppoe_user", "VARCHAR(191) NULL"),
    # web accounts: bumping this column invalidates every issued session cookie
    ("users", "session_epoch", "BIGINT NOT NULL DEFAULT 0"),
    # fine-grained extra permissions (super admin bypasses all of them)
    ("users", "perm_devices", "TINYINT NOT NULL DEFAULT 0"),
    ("users", "perm_tools", "TINYINT NOT NULL DEFAULT 1"),
    ("users", "perm_settings", "TINYINT NOT NULL DEFAULT 0"),
    ("users", "perm_users", "TINYINT NOT NULL DEFAULT 0"),
    ("users", "perm_developer", "TINYINT NOT NULL DEFAULT 0"),
    ("users", "perm_sql", "TINYINT NOT NULL DEFAULT 0"),
]

#: Non-unique indexes added after the first schema release.  Every row is
#: ``(table, index_name, column list)``; ``init_db`` skips the ones that exist.
EXTRA_INDEXES: list[tuple[str, str, str]] = [
    # prune_history deletes with ``WHERE ts < cutoff`` on every table
    ("olt_onu_history", "idx_onu_hist_ts", "ts"),
    ("pppoe_user_history", "idx_user_hist_ts", "ts"),
    ("port_history", "idx_port_hist_ts", "ts"),
    ("router_history", "idx_router_hist_ts", "ts"),
    ("olt_health_history", "idx_health_hist_ts", "ts"),
    ("alerts_current", "idx_alerts_active_ts", "active, ts"),
    # PON-level subscriber aggregation on PHOTON / BDPON / BTPON
    ("pppoe_user_current", "idx_user_pon", "onu_olt_name, onu_pon"),
]


# ---------------------------------------------------------------------------
#  configuration
# ---------------------------------------------------------------------------
def _settings_from_cfg(cfg: dict) -> dict[str, Any]:
    database = dict(cfg.get("database") or {})
    return {
        "host": str(database.get("host") or "127.0.0.1"),
        "port": int(database.get("port") or 3306),
        "database": str(database.get("name") or "olt_monitor"),
        "user": str(database.get("user") or "olt_monitor"),
        "password": str(database.get("password") or ""),
        "charset": str(database.get("charset") or "utf8mb4"),
    }


def configure(cfg: dict | None = None) -> dict[str, Any]:
    """Point the storage layer at the MySQL server described by *cfg*.

    Called by ``run.py`` / the scheduler / the web app at start-up.  When it is
    never called (CLI tools, scripts), the settings are loaded lazily from
    ``config/config.yaml`` so every entry point behaves the same.
    """
    global _CONFIG
    if cfg is None:
        from .config import load_config

        cfg = load_config()
    settings = _settings_from_cfg(cfg)
    with _CONFIG_LOCK:
        changed = _CONFIG is not None and _CONFIG != settings
        _CONFIG = settings
    if changed:
        # A process that switches target (e.g. tools/setup_mysql.py, the
        # self-test) must not keep using a connection to the previous server
        # or database.
        close()
    return settings


def settings() -> dict[str, Any]:
    """Current MySQL connection settings (loaded from config on first use)."""
    if _CONFIG is None:
        configure()
    return dict(_CONFIG or {})


def describe() -> str:
    """Human readable target, safe to show in the UI (never the password)."""
    cfg = settings()
    return f"mysql://{cfg.get('user')}@{cfg.get('host')}:{cfg.get('port')}/{cfg.get('database')}"


# ---------------------------------------------------------------------------
#  connections
# ---------------------------------------------------------------------------
_TLS = threading.local()


def _new_connection() -> pymysql.connections.Connection:
    cfg = settings()
    try:
        return pymysql.connect(
            host=cfg["host"],
            port=int(cfg["port"]),
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset=cfg.get("charset") or "utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=10,
            read_timeout=300,
            write_timeout=300,
            local_infile=False,
        )
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else 0
        if code == 1049:  # unknown database
            raise RuntimeError(
                f"MySQL database {cfg['database']!r} does not exist. "
                "Run:  python3 tools/setup_mysql.py"
            ) from exc
        if code in (1044, 1045, 2003):  # no access / bad password / not running
            raise RuntimeError(
                f"cannot connect to MySQL at {cfg['host']}:{cfg['port']} as "
                f"{cfg['user']!r}: {exc.args[-1]}. Check the database: block in "
                "config/config.yaml (or .env DB_*) and make sure MySQL is running."
            ) from exc
        raise


def connect(path: str | Path | None = None) -> pymysql.connections.Connection:
    """A MySQL connection for the current thread (reused, reconnected on demand).

    The ``path`` argument is accepted for backwards compatibility with the old
    SQLite API and is ignored.
    """
    conn = getattr(_TLS, "conn", None)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _TLS.conn = None
    conn = _new_connection()
    _TLS.conn = conn
    return conn


def close() -> None:
    """Close this thread's connection (used by tests / shutdown)."""
    conn = getattr(_TLS, "conn", None)
    _TLS.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _placeholder_sql(sql: str) -> str:
    """Turn ``?`` placeholders into ``%s`` without touching quoted text."""
    if "?" not in sql:
        return sql
    out: list[str] = []
    quote: str | None = None
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote is None:
            if char in ("'", '"', "`"):
                quote = char
                out.append(char)
            elif char == "?":
                out.append("%s")
            else:
                out.append(char)
        else:
            if char == "\\":  # backslash escape inside a string
                out.append(char)
                if index + 1 < length:
                    index += 1
                    out.append(sql[index])
            elif char == quote:
                if index + 1 < length and sql[index + 1] == quote:
                    out.append(char)
                    index += 1
                    out.append(char)
                else:
                    quote = None
                    out.append(char)
            else:
                out.append(char)
        index += 1
    return "".join(out)


def _clean_value(value: Any) -> Any:
    """Make a database value safe for the rest of the app.

    MySQL returns ``Decimal`` for ``SUM()``/``AVG()`` over integer columns.
    The templates and the health score arithmetic expect plain int/float, so
    convert here once instead of at every call site.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _dict_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return {key: _clean_value(value) for key, value in row.items()}


def _quoted(column: str) -> str:
    return "`" + str(column).replace("`", "``") + "`"


def _row_list(row: Sequence[Any]) -> list[Any]:
    return list(row)


# ---------------------------------------------------------------------------
#  public helpers
# ---------------------------------------------------------------------------
def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db(path: str | Path | None = None) -> None:
    """Create the schema (and any missing columns) on the configured database."""
    with _LOCK:
        conn = connect(path)
        try:
            with conn.cursor() as cur:
                for statement in SCHEMA_STATEMENTS:
                    cur.execute(statement)
                # add columns introduced after the table was created
                for table, column, sql_type in MIGRATIONS:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
                        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
                        "   AND COLUMN_NAME = %s",
                        (table, column),
                    )
                    row = _dict_row(cur.fetchone())
                    if row and int(row.get("n") or 0) == 0:
                        cur.execute(
                            f"ALTER TABLE {_quoted(table)} ADD COLUMN {_quoted(column)} {sql_type}"
                        )
                # Extra indexes for the 24-hour retention sweep and the
                # PON-level user lookups.  Created once, then skipped.
                for table, index_name, columns in EXTRA_INDEXES:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.STATISTICS"
                        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
                        "   AND INDEX_NAME = %s",
                        (table, index_name),
                    )
                    row = _dict_row(cur.fetchone())
                    if row and int(row.get("n") or 0) == 0:
                        cur.execute(
                            f"ALTER TABLE {_quoted(table)} ADD INDEX"
                            f" {_quoted(index_name)} ({columns})"
                        )
                # One-time defaults for accounts created before fine-grained
                # permissions existed: a manager keeps managing devices, while
                # every administrative area stays super-admin only.
                cur.execute(
                    "SELECT `value` FROM meta WHERE `key` = 'permissions_v1'"
                )
                if cur.fetchone() is None:
                    cur.execute(
                        "UPDATE `users` SET perm_devices = 1 WHERE role = 'manager'"
                    )
                    cur.execute(
                        "INSERT INTO meta (`key`, `value`) VALUES ('permissions_v1', '1')"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(path: str | Path | None, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """Run a SELECT and return a list of dicts.

    Each thread has its own connection, so a reader never waits behind a
    writer's transaction.
    """
    conn = connect(path)
    try:
        with conn.cursor() as cur:
            cur.execute(_placeholder_sql(sql), tuple(params) if params else None)
            rows = [_dict_row(row) for row in cur.fetchall()]
        # End the read transaction.  Without this, MySQL's REPEATABLE READ
        # keeps the first snapshot for the whole connection and a long-lived
        # web thread would never see new polls.
        conn.commit()
        return rows
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def execute(path: str | Path | None, sql: str, params: Sequence[Any] = ()) -> int:
    """Run a single INSERT/UPDATE/DELETE; returns lastrowid."""
    with _LOCK:
        conn = connect(path)
        try:
            with conn.cursor() as cur:
                cur.execute(_placeholder_sql(sql), tuple(params) if params else None)
                last_id = int(cur.lastrowid or 0)
            conn.commit()
            return last_id
        except Exception:
            conn.rollback()
            raise


def executemany(path: str | Path | None, sql: str, rows: Iterable[Sequence[Any]]) -> None:
    with _LOCK:
        data = [_row_list(row) for row in rows]
        if not data:
            return
        conn = connect(path)
        try:
            with conn.cursor() as cur:
                cur.executemany(_placeholder_sql(sql), data)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _upsert_sql(table: str, columns: list[str], pk: Sequence[str]) -> str:
    placeholders = ", ".join(["%s"] * len(columns))
    # ``pk`` identifies the row, everything else is refreshed on a duplicate
    updates = [
        f"{_quoted(col)} = VALUES({_quoted(col)})"
        for col in columns
        if col not in pk
    ]
    sql = (
        f"INSERT INTO {_quoted(table)} ({', '.join(_quoted(c) for c in columns)}) "
        f"VALUES ({placeholders})"
    )
    if updates:
        sql += " ON DUPLICATE KEY UPDATE " + ", ".join(updates)
    else:
        sql += f" ON DUPLICATE KEY UPDATE {_quoted(pk[0])} = {_quoted(pk[0])}"
    return sql


def upsert(path: str | Path | None, table: str, pk: Sequence[str], row: dict) -> None:
    """INSERT ... ON DUPLICATE KEY UPDATE, keyed by *pk*."""
    cols = list(row.keys())
    execute(path, _upsert_sql(table, cols, pk), [row[c] for c in cols])


def upsert_many(path: str | Path | None, table: str, pk: Sequence[str], rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    executemany(
        path,
        _upsert_sql(table, cols, pk),
        [[r.get(c) for c in cols] for r in rows],
    )


def insert_many(path: str | Path | None, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join("%s" for _ in cols)
    sql = (
        f"INSERT INTO {_quoted(table)} ({', '.join(_quoted(c) for c in cols)}) "
        f"VALUES ({placeholders})"
    )
    executemany(path, sql, [[r.get(c) for c in cols] for r in rows])


def set_meta(path: str | Path | None, key: str, value: str) -> None:
    execute(
        path,
        "INSERT INTO meta (`key`, `value`) VALUES (?, ?) "
        "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)",
        [key, value],
    )


def get_meta(path: str | Path | None, key: str, default: str | None = None) -> str | None:
    rows = query(path, "SELECT `value` FROM meta WHERE `key` = ?", [key])
    return rows[0]["value"] if rows else default


def bump_poll_counter(path: str | Path | None, key: str) -> int:
    """Increment an integer counter in ``meta`` and return the new value."""
    raw = get_meta(path, key, "0") or "0"
    try:
        value = int(raw) + 1
    except ValueError:
        value = 1
    set_meta(path, key, str(value))
    return value


def prune_history(path: str | Path | None, keep_days: int) -> int:
    """Delete history rows older than *keep_days*; returns rows removed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, keep_days))).replace(
        microsecond=0
    ).isoformat()
    removed = 0
    for table in (
        "olt_onu_history",
        "pppoe_user_history",
        "port_history",
        "router_history",
        "olt_health_history",
    ):
        with _LOCK:
            conn = connect(path)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {_quoted(table)} WHERE ts < %s", (cutoff,))
                    removed += int(cur.rowcount or 0)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # keep the alerts table from growing forever
    execute(
        path,
        "DELETE FROM alerts_current WHERE active = 0 AND ts < ?",
        [cutoff],
    )
    return removed
