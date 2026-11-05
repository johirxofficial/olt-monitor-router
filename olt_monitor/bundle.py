"""Build a complete source bundle for another server (Developer page).

The zip is what a new Linux server needs: code, templates, docs, shell scripts,
the systemd unit template and ``config/config.example.yaml``.  It never contains
this installation's own state:

* ``.env`` / ``.env.bak``            - real credentials
* ``config/config.yaml``            - this site's device seeds
* ``config/optical_oids.yaml``      - OIDs discovered on *these* OLTs
* ``data/``                          - encryption/session keys + FDB caches
* ``logs/`` / ``backups/``          - local files
* ``.venv`` / ``__pycache__`` / ``*.pyc``

A fresh install therefore starts clean and runs ``./install.sh`` (or
``tools/setup_mysql.py``) and the first-run web setup.
"""

from __future__ import annotations

import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parents[1]

TOP_FILES = (
    "run.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    ".env.example",
    ".gitignore",
    "install.sh",
    "start.sh",
    "start-bg.sh",
    "stop.sh",
    "status.sh",
    "start.bat",
    "start-bg.bat",
    "stop.bat",
    "status.bat",
    "autostart.bat",
)

TOP_DIRS = ("olt_monitor", "tools", "config", "deploy", "docs")

SKIP_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    ".idea",
    ".vscode",
}
#: only the top-level state directories are excluded; ``olt_monitor/data``
#: ships the bundled OUI database and must stay in the archive
TOP_STATE_DIRS = {"data", "logs", "backups"}
SKIP_NAMES = {
    ".env",
    ".env.bak",
    "config/config.yaml",
    "config/optical_oids.yaml",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".pyd", ".log", ".zip")


def _wanted(relative: str) -> bool:
    if relative in SKIP_NAMES:
        return False
    parts = relative.split("/")
    if parts[0] in TOP_STATE_DIRS:
        return False
    if any(part in SKIP_DIRS for part in parts):
        return False
    if relative.endswith(SKIP_SUFFIXES):
        return False
    return True


def collect() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for name in TOP_FILES:
        path = ROOT / name
        if path.is_file():
            files.append((path, name))
    for name in TOP_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if _wanted(relative):
                files.append((path, relative))
    return files


def build_source_zip(dest: Path) -> dict:
    """Write the bundle to *dest* and return ``{path, files, bytes, version}``."""
    files = collect()
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    readme = (
        "olt-monitor-router - source bundle\n"
        f"version : {__version__}\n"
        f"built   : {generated}\n\n"
        "Quick start on a fresh Ubuntu/Debian server:\n"
        "  1. unzip this bundle into a new directory (e.g. ~/olt-monitor-router)\n"
        "  2. cd into that directory && ./install.sh\n"
        "  3. open http://<server-ip>:5002/setup and create the super-admin\n\n"
        "Full guide: docs/INSTALL.md\n"
        "Developer guide: docs/DEVELOPER.md\n"
        "Do not copy an old .env or data/ directory over a new install.\n"
    )

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        info = zipfile.ZipInfo("BUNDLE-INFO.txt", date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(info, readme)
        for path, arcname in files:
            data = path.read_bytes()
            if arcname.endswith((".sh", ".bat")):
                data = data.replace(b"\r\n", b"\n")
            entry = zipfile.ZipInfo(arcname, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if arcname.endswith(".sh") else 0o644
            entry.external_attr = mode << 16
            archive.writestr(entry, data)
            total += len(data)

    return {
        "path": dest,
        "files": len(files) + 1,
        "bytes": dest.stat().st_size,
        "version": __version__,
    }


def temp_bundle(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return directory / f"olt-monitor-router-{__version__}-{stamp}.zip"
