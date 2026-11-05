#!/usr/bin/env python3
"""olt-monitor-router entry point.

    python run.py                 # start polling + web dashboard
    python run.py --once          # poll every device once, store, then exit
    python run.py --check         # only test SNMP / RouterOS connectivity
    python run.py --no-poll       # web UI only (uses data already in the DB)
    python run.py --port 9000     # change the web port

The dashboard is then available at http://localhost:8000
"""

from __future__ import annotations

import argparse
import sys

MIN_PYTHON = (3, 10)


def check_python_version() -> None:
    """FastAPI evaluates the route annotations at runtime, and this project uses
    PEP 604 unions (``int | None``), which need Python 3.10 or newer."""
    if sys.version_info < MIN_PYTHON:
        current = ".".join(str(p) for p in sys.version_info[:3])
        required = ".".join(str(p) for p in MIN_PYTHON)
        print(
            f"This project needs Python {required} or newer (found {current}).\n"
            "Install a current Python from https://www.python.org/downloads/ and\n"
            "re-create the virtual environment.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def ensure_streams() -> None:
    """Give ``stdout``/``stderr`` a sink when there is no console.

    ``pythonw.exe`` - used by the Windows auto-start task so the monitor has no
    console window - leaves both at ``None``.  ``print()`` survives that, but
    uvicorn's logging handlers do not, so point them at ``os.devnull`` instead.
    """
    import os

    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


from olt_monitor import __version__  # noqa: E402
from olt_monitor import db as db_mod  # noqa: E402
from olt_monitor.config import db_path as resolve_db_path  # noqa: E402
from olt_monitor.config import dig, load_config  # noqa: E402
from olt_monitor.logging_setup import get_logger, setup_logging  # noqa: E402


def port_in_use(host: str, port: int) -> bool:
    """True when something is already listening on the dashboard port.

    Used to make a second start (``start.bat`` clicked twice, or an auto-start
    task while a copy is already running) exit quietly instead of crashing with
    an ``address already in use`` traceback.
    """
    import socket

    probes = ["127.0.0.1"] if host in ("0.0.0.0", "::", "") else [host]
    for probe in probes:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                if sock.connect_ex((probe, port)) == 0:
                    return True
            except OSError:
                continue
    return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="olt-monitor-router",
        description="Monitor a VSOL EPON OLT over SNMP and MikroTik PPPoE users over the API.",
    )
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    parser.add_argument("--host", default=None, help="web bind address (overrides config)")
    parser.add_argument("--port", type=int, default=None, help="web port (overrides config)")
    parser.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--only",
        choices=["all", "olt", "router"],
        default="all",
        help="with --once: which device(s) to poll",
    )
    parser.add_argument("--check", action="store_true", help="test device connectivity and exit")
    parser.add_argument("--no-poll", action="store_true", help="serve the web UI without polling")
    parser.add_argument("--version", action="version", version=f"olt-monitor-router {__version__}")
    return parser.parse_args(argv)


def check_devices(cfg: dict) -> int:
    """Try each device once and print a friendly report. Returns an exit code."""
    from olt_monitor import registry
    from olt_monitor.collectors import OltCollector, RouterCollector
    from olt_monitor.db import init_db

    db_file = resolve_db_path(cfg)
    init_db(db_file)
    registry.ensure_seeded(db_file, cfg)

    failures = 0
    olts, routers = registry.device_lists(db_file, cfg)
    print(f"Configured devices: {len(olts)} OLT(s), {len(routers)} router(s)\n")

    for device in olts:
        olt = OltCollector(cfg, device)
        if not olt.enabled:
            print(f"[OLT ] {olt.name}: disabled in config")
            continue
        print(
            f"[OLT ] {olt.name}: {olt.host} SNMP "
            f"v{olt.cfg.get('snmp_version', '2c')} "
            f"community={'set' if olt.cfg.get('community') else 'MISSING'} ..."
        )
        snapshot = olt.collect()
        if snapshot.ok:
            info = snapshot.info
            print(f"[OLT ] OK  {info.get('sys_descr')} | name={info.get('sys_name')} | "
                  f"uptime={info.get('sys_uptime_text')}")
            print(f"[OLT ]     {len(snapshot.ports)} ports, {len(snapshot.onus)} ONUs, "
                  f"{len(snapshot.mac_map)} learned MACs")
            if info.get("optical_note"):
                print(f"[OLT ]     note: {info['optical_note']}")
            if info.get("mac_table_note"):
                print(f"[OLT ]     note: {info['mac_table_note']}")
        else:
            failures += 1
            print(f"[OLT ] FAILED: {snapshot.error}")
        olt.close()

    for device in routers:
        router = RouterCollector(cfg, device)
        if not router.enabled:
            print(f"[RTR ] {router.name}: disabled in config")
            continue
        print(
            f"[RTR ] {router.name}: {router.host} API port={router.cfg.get('api_port')} "
            f"user={router.username!r} ..."
        )
        snapshot = router.collect()
        if snapshot.ok:
            info = snapshot.info
            print(f"[RTR ] OK  {info.get('identity')} | RouterOS {info.get('version')} | "
                  f"uptime={info.get('uptime_text')}")
            with_counters = sum(
                1 for user in snapshot.users
                if user.get("bytes_in") is not None or user.get("bytes_out") is not None
            )
            print(f"[RTR ]     {len(snapshot.users)} PPPoE users "
                  f"({with_counters} with byte counters), {len(snapshot.interfaces)} interfaces")
        else:
            failures += 1
            print(f"[RTR ] FAILED: {snapshot.error}")
        router.close()

    init_db(resolve_db_path(cfg))
    print(f"[DB  ] ready at {db_mod.describe()}")
    if failures:
        print(f"\n{failures} device(s) failed. Fix the settings above and run again.")
    else:
        print("\nAll good - start the monitor with:  python3 run.py")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ensure_streams()
    check_python_version()
    args = parse_args(argv)
    cfg = load_config(args.config)
    db_mod.configure(cfg)

    if args.log_level:
        cfg.setdefault("app", {})["log_level"] = args.log_level

    setup_logging(dig(cfg, "app.log_level", "INFO"))
    log = get_logger("main")
    log.info("olt-monitor-router %s starting", __version__)

    if args.check:
        return check_devices(cfg)

    if args.once:
        from olt_monitor.scheduler import run_once

        run_once(cfg, args.only)
        print(f"Poll complete. Database: {db_mod.describe()}")
        return 0

    host = args.host or dig(cfg, "app.host", "0.0.0.0")
    port = args.port or int(dig(cfg, "app.port", 8000))

    if port_in_use(host, port):
        print(
            f"[!] {host}:{port} is already in use - another copy is probably running.\n"
            f"    Stop it first (./stop.sh) or start this one with --port <other>.",
            file=sys.stderr,
        )
        return 0

    monitor = None
    if not args.no_poll:
        from olt_monitor.scheduler import Monitor

        monitor = Monitor(cfg)
        monitor.start()
    else:
        from olt_monitor.db import init_db

        init_db(resolve_db_path(cfg))

    from olt_monitor.web.app import create_app

    app = create_app(cfg, monitor)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed.\n"
            "  python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    print(f"\n  Dashboard:  http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
    print(f"  API:        http://{host if host != '0.0.0.0' else 'localhost'}:{port}/api/health\n")

    try:
        uvicorn.run(app, host=host, port=port, log_level=dig(cfg, "app.log_level", "info").lower())
    except KeyboardInterrupt:
        pass
    finally:
        if monitor is not None:
            monitor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
