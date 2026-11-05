#!/usr/bin/env python3
"""Build the deployable source bundle (CLI wrapper).

    python3 tools/build_bundle.py                       # -> /tmp/olt-monitor-router-v3.zip
    python3 tools/build_bundle.py --out /tmp/bundle.zip

The actual file selection lives in :mod:`olt_monitor.bundle` so the command
line and the **Developer → Download source bundle** button always produce the
same tree.  Everything that belongs to *this* installation is excluded:

* ``.env`` / ``.env.bak``                    - real credentials
* ``config/config.yaml``                     - this site's device seeds
* ``config/optical_oids.yaml``               - OIDs discovered on these OLTs
* ``data/``                                  - encryption + session keys, caches
* ``logs/`` / ``backups/``                   - local files
* ``.venv`` / ``__pycache__`` / ``*.pyc``

Shell scripts are stored with LF line endings and the executable bit, so the
zip is safe to build on Windows and unpack on the Linux server.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from olt_monitor import bundle  # noqa: E402

LOCAL_STATE = (".env", ".env.bak", "config/config.yaml", "config/optical_oids.yaml")
LOCAL_DIRS = ("data/", "logs/", "backups/", ".venv/", "venv/", "__pycache__/")
DEFAULT_OUT = Path(os.environ.get("TEMP", "/tmp")) / "olt-monitor-router-v3.zip"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="zip to write")
    args = parser.parse_args()

    out = args.out.expanduser()
    info = bundle.build_source_zip(out)
    print(f"wrote {info['path']}")
    print(
        f"  {info['files']} files, {info['bytes'] / 1024:.0f} KB zipped, "
        f"version {info['version']}"
    )

    with zipfile.ZipFile(info["path"]) as archive:
        names = archive.namelist()

    leaked = [
        name
        for name in names
        if name in LOCAL_STATE
        or any(name.startswith(prefix) for prefix in LOCAL_DIRS)
        or name.endswith((".pyc", ".pyo", ".log"))
    ]
    if leaked:
        info["path"].unlink(missing_ok=True)
        print("REFUSING: the bundle contains local state:", leaked, file=sys.stderr)
        return 1

    required = (
        "BUNDLE-INFO.txt",
        "docs/INSTALL.md",
        "docs/DEVELOPER.md",
        "docs/USERS.md",
        "config/config.example.yaml",
        "olt_monitor/data/oui.json.gz",
        "olt_monitor/web/app.py",
        "olt_monitor/web/static/css/style.css",
    )
    missing = [name for name in required if name not in names]
    if missing:
        print("REFUSING: the bundle is missing:", missing, file=sys.stderr)
        return 1

    print("  includes: code, templates, static, docs, scripts, config template")
    print("  excludes: .env, config/config.yaml, optical_oids.yaml, data/, logs/, backups/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
