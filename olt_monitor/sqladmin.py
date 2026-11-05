"""SQL export / import used by the admin Settings page.

The exporter produces a plain, portable MySQL dump (``DROP TABLE`` +
``CREATE TABLE`` + batched ``INSERT``), so it can be restored with this page or
with the usual command line tools:

    mysql -u olt_monitor -p olt_monitor < olt_monitor-2026-....sql

History tables are optional - they are by far the largest part of the database
(millions of rows), so the default export only contains the current state plus
users, settings and the device registry.

The importer executes a dump statement by statement.  It deliberately does not
try to support stored procedures / ``DELIMITER``; the app never creates those.
"""

from __future__ import annotations

import os
import re
import time
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pymysql
from pymysql.cursors import SSCursor

from . import db

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "backups"

HISTORY_TABLES: tuple[str, ...] = (
    "olt_onu_history",
    "pppoe_user_history",
    "port_history",
    "router_history",
    "olt_health_history",
)
CORE_TABLES: tuple[str, ...] = tuple(t for t in db.TABLES if t not in HISTORY_TABLES)

BATCH_ROWS = 200
_INSERT_HEADER = "INSERT INTO `{table}` ({columns}) VALUES\n"
_MAX_IMPORT_BYTES = 1024 * 1024 * 1024  # 1 GiB


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def backup_path(include_history: bool) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "full" if include_history else "current"
    return BACKUP_DIR / f"olt_monitor-{suffix}-{_stamp()}.sql"


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        return repr(float(value)) if isinstance(value, float) else str(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    text = str(value)
    text = (
        text.replace("\\", "\\\\")
        .replace("\0", "\\0")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x1a", "\\Z")
        .replace("'", "\\'")
    )
    return "'" + text + "'"


def _create_sql(conn: pymysql.connections.Connection, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(f"SHOW CREATE TABLE `{table}`")
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"table {table} vanished while exporting")
    if isinstance(row, dict):
        return str(row.get("Create Table") or "")
    return str(row[1])


def dump_sql(dest: Path, *, include_history: bool = False) -> dict[str, Any]:
    """Write a portable dump to *dest* and return ``{path, bytes, rows, tables}``."""
    tables = list(CORE_TABLES) + (list(HISTORY_TABLES) if include_history else [])
    dest.parent.mkdir(parents=True, exist_ok=True)
    settings = db.settings()
    written_rows = 0
    started = time.monotonic()

    with dest.open("w", encoding="utf-8", newline="\n") as out:
        out.write("-- olt-monitor-router SQL dump\n")
        out.write(f"-- generated : {db.utcnow_iso()}\n")
        out.write(f"-- database  : {settings.get('database')} on {settings.get('host')}:{settings.get('port')}\n")
        out.write(f"-- history   : {'included' if include_history else 'excluded'}\n")
        out.write("-- restore   : mysql -u <user> -p <database> < this-file.sql\n\n")
        out.write("SET NAMES utf8mb4;\n")
        out.write("SET FOREIGN_KEY_CHECKS = 0;\n\n")

        for table in tables:
            conn = db._new_connection()
            try:
                create_sql = _create_sql(conn, table)
                out.write(f"-- ------------------------------------------------------------------ {table}\n")
                out.write(f"DROP TABLE IF EXISTS `{table}`;\n")
                out.write(create_sql.rstrip().rstrip(";") + ";\n\n")

                stream = conn.cursor(SSCursor)
                stream.execute(f"SELECT * FROM `{table}`")
                columns = [item[0] for item in stream.description or []]
                if not columns:
                    continue
                columns_sql = ", ".join(f"`{name}`" for name in columns)
                first = True
                while True:
                    rows = stream.fetchmany(BATCH_ROWS)
                    if not rows:
                        break
                    values = ",\n".join(
                        "(" + ", ".join(_literal(value) for value in row) + ")"
                        for row in rows
                    )
                    if first:
                        out.write(_INSERT_HEADER.format(table=table, columns=columns_sql))
                        first = False
                    else:
                        out.write(",\n")
                    out.write(values)
                    written_rows += len(rows)
                if not first:
                    out.write(";\n")
                out.write("\n")
                stream.close()
            finally:
                conn.close()

        out.write("SET FOREIGN_KEY_CHECKS = 1;\n")
        out.write(f"-- {written_rows} row(s), {len(tables)} table(s)\n")

    return {
        "path": dest,
        "bytes": dest.stat().st_size,
        "rows": written_rows,
        "tables": len(tables),
        "include_history": include_history,
        "seconds": round(time.monotonic() - started, 1),
    }


def _zip_dump(dump: Path) -> Path:
    """A ``.sql`` can be large; the download is zipped to save bandwidth."""
    zipped = dump.with_suffix(dump.suffix + ".zip")
    with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(dump, arcname=dump.name)
    dump.unlink(missing_ok=True)
    return zipped


def make_backup(*, include_history: bool = False, compress: bool = True) -> dict[str, Any]:
    info = dump_sql(backup_path(include_history), include_history=include_history)
    path = Path(info["path"])
    if compress:
        info["path"] = _zip_dump(path)
        info["bytes"] = info["path"].stat().st_size
        info["compressed"] = True
    info["filename"] = Path(info["path"]).name
    return info


# ---------------------------------------------------------------------------
#  import
# ---------------------------------------------------------------------------
def iter_statements(path: Path) -> Iterator[str]:
    """Yield SQL statements from a dump without loading the whole file.

    Understands single/double-quoted strings, backtick identifiers, ``--`` and
    ``#`` line comments and ``/* ... */`` blocks.
    """
    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    previous = ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            for char in chunk:
                if line_comment:
                    if char == "\n":
                        line_comment = False
                        buffer.append(char)
                    previous = char
                    continue
                if block_comment:
                    if previous == "*" and char == "/":
                        block_comment = False
                    previous = char
                    continue
                if quote:
                    buffer.append(char)
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        quote = None
                    previous = char
                    continue

                if char == "-" and previous == "-":
                    # '--' starts a comment; drop the '-' already buffered
                    line_comment = True
                    if buffer and buffer[-1] == "-":
                        buffer.pop()
                    previous = char
                    continue
                if char == "#":
                    line_comment = True
                    previous = char
                    continue
                if char == "/" and previous == "*":
                    block_comment = True
                    if buffer and buffer[-1] == "/":
                        buffer.pop()
                    previous = char
                    continue
                if char in ("'", '"', "`"):
                    quote = char
                    escaped = False
                    buffer.append(char)
                    previous = char
                    continue
                if char == ";":
                    statement = "".join(buffer).strip()
                    buffer.clear()
                    if statement:
                        yield statement
                    previous = char
                    continue
                buffer.append(char)
                previous = char

    tail = "".join(buffer).strip()
    if tail:
        yield tail


def _snippet(statement: str) -> str:
    compact = re.sub(r"\s+", " ", statement).strip()
    return compact[:180] + ("..." if len(compact) > 180 else "")


def import_sql(source: Path, *, max_bytes: int = _MAX_IMPORT_BYTES) -> dict[str, Any]:
    """Execute every statement of *source*.  Returns rows/statements counters."""
    size = source.stat().st_size
    if size == 0:
        raise ValueError("the uploaded file is empty")
    if size > max_bytes:
        raise ValueError(f"file is larger than {max_bytes // (1024 * 1024)} MB")

    head = source.read_text(encoding="utf-8", errors="replace")[:4000]
    if re.search(r"(?im)^\s*DELIMITER\s+", head):
        raise ValueError("DELIMITER / stored procedures are not supported by the importer")

    statements = 0
    rowcount = 0
    started = time.monotonic()
    conn = db._new_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            conn.commit()
            for statement in iter_statements(source):
                if not statement or statement.startswith("SET FOREIGN_KEY_CHECKS"):
                    continue
                try:
                    cur.execute(statement)
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    raise ValueError(
                        f"statement {statements + 1} failed: {exc}\n{_snippet(statement)}"
                    ) from exc
                statements += 1
                rowcount += max(0, int(cur.rowcount or 0))
                if statements % 200 == 0:
                    conn.commit()
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    try:
        from . import registry

        registry.invalidate_cache()
    except Exception:
        pass
    return {
        "statements": statements,
        "rows": rowcount,
        "seconds": round(time.monotonic() - started, 1),
        "bytes": size,
    }


def free_space_mb() -> int:
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        return shutil_disk_free(BACKUP_DIR)
    except OSError:
        return 0


def shutil_disk_free(path: Path) -> int:
    import shutil

    return int(shutil.disk_usage(path).free / (1024 * 1024))


def list_backups(limit: int = 20) -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    files = sorted(
        (p for p in BACKUP_DIR.glob("olt_monitor-*.sql*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    return [
        {
            "name": p.name,
            "bytes": p.stat().st_size,
            "when": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        }
        for p in files
    ]
