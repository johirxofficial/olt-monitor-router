"""Polling engine.

One worker thread per configured device (any number of OLTs and routers).  Each
thread polls its device on its own interval, stores the result, then runs the
shared post-processing step:

    poll -> store current -> correlate ONU <-> PPPoE user -> evaluate alerts

Correlation is global: every ONU from every OLT is matched against every user
from every router, so a user is found no matter which pair of devices they sit
between.  A unique index on ``alerts_current`` keeps one row per ongoing
problem.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import alerts as alerts_mod
from . import registry
from .collectors import OltCollector, RouterCollector
from .collectors.olt import _plausible_power
from .config import db_path as resolve_db_path
from .db import configure as configure_db
from .correlate import build_fdb, build_fdb_pons, build_fdb_vlans, correlate
from .db import (
    execute,
    executemany,
    insert_many,
    init_db,
    prune_history,
    query,
    upsert,
    upsert_many,
    utcnow_iso,
)
from .logging_setup import get_logger

log = get_logger("scheduler")


# Words that appear in almost every PPPoE server name but say nothing about
# which OLT / customer group it serves.  They are ignored when matching a
# server to an OLT or username.
_SERVER_STOPWORDS = {
    "olt", "vsol", "bdcom", "cdata", "raisecom", "huawei", "zte", "epon",
    "gpon", "pppoe", "server", "router", "uplink", "vlan", "litex", "isp",
    "broadband", "internet", "net", "network", "default",
}


def _name_tokens(value: Any) -> set[str]:
    tokens = set()
    for token in re.split(r"[^a-z0-9]+", str(value or "").lower()):
        if len(token) >= 3 and not token.isdigit() and token not in _SERVER_STOPWORDS:
            tokens.add(token)
    return tokens


def _pon_from_onu_name(value: Any) -> int | None:
    """PON number from an ONU interface name (GPON01ONU72, EPON0/3:8, ...)."""
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in (
        r"(?:GPON|EPON)\s*0*(\d+)\s*ONU\s*\d+",
        r"(?:GPON|EPON)\s*\d+\s*/\s*0*(\d+)\s*[:/]",
        r"\bPON\s*[_-]?\s*0*(\d+)\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _onu_is_down(value: Any) -> bool:
    """True when an SNMP ``ifOperStatus`` / ONU state means "down"."""
    try:
        return int(value) == 2
    except (TypeError, ValueError):
        return False


def mark_last_down(
    rows: list[dict[str, Any]],
    previous: dict[int, dict[str, Any]],
    stamp: str,
) -> None:
    """Record when each ONU was first seen going down.

    Most SNMP OLTs do not expose a per-ONU "last down" time, so the ONU and
    Users pages were blank for them while CLI-driven EPON boxes showed a time.
    The poller can observe the up -> down transition itself, which gives every
    vendor a real timestamp.  A row that is already down keeps its original
    timestamp (``previous`` / the carried value); only a transition is stamped.
    """
    for row in rows:
        try:
            old = previous.get(int(row.get("ifindex"))) or {}
        except (TypeError, ValueError):
            old = {}
        if _onu_is_down(row.get("oper_status")) and not _onu_is_down(old.get("oper_status")):
            row["last_down_at"] = stamp


def _access_interface(
    user: dict[str, Any],
    snapshot: Any,
    olt_vlan: int | None,
) -> tuple[str | None, str | None]:
    """Best name for the MikroTik interface a PPPoE user arrives on.

    Order of confidence:

    1. ``vlan``  - the OLT forwarding table knows the service VLAN and the
       router has a VLAN interface with the same ``vlan-id`` (for example
       ``ISP_CORE_1365``).  This is exact.
    2. ``server`` - the PPPoE server service-name / bound interface contains
       the OLT name or the start of the username (``ISP_CORE_OLT`` ->
       ``ISP_CORE_1365``).
    3. ``pppoe`` - the per-user dynamic interface ``<pppoe-username>``.
    """
    interfaces = list(getattr(snapshot, "interfaces", None) or [])
    servers = list(getattr(snapshot, "pppoe_servers", None) or [])
    olt_tokens = _name_tokens(user.get("onu_olt_name"))

    # 1. exact service VLAN -> VLAN interface on the router
    if olt_vlan:
        candidates = [row for row in interfaces if row.get("vlan_id") == olt_vlan]
        if candidates:
            chosen = candidates[0]
            if olt_tokens:
                preferred = next(
                    (
                        row for row in candidates
                        if olt_tokens & _name_tokens(row.get("name"))
                    ),
                    None,
                )
                chosen = preferred or chosen
            return str(chosen.get("name") or ""), "vlan"

    # 1b. inner (QinQ) service VLAN -> the router's PON interface.  Some GPON
    # OLTs put an inner VLAN in the FDB (291, 361, ...) while the router has
    # one interface per PON (Town_OLT_PON_1 = outer VLAN 131).  Match the PON
    # number from the ONU name, but only when the OLT name also matches the
    # interface name, so a generic "PON_1" can never steal another OLT's user.
    pon = _pon_from_onu_name(user.get("onu_name"))
    if pon is not None and olt_tokens:
        pon_pattern = re.compile(rf"PON[_-]?0*{pon}\b", re.IGNORECASE)
        preferred = [
            row for row in interfaces
            if pon_pattern.search(str(row.get("name") or ""))
            and (olt_tokens & _name_tokens(row.get("name")))
        ]
        if preferred:
            # ``VSOL_GPON_TOWN`` also tokenises as just {"town"}, which also
            # matches another provider's ``TOWN-AVEIS_PON_1``.  Prefer the
            # explicit ``..._OLT_PON_N`` interface, then the name with the
            # fewest foreign words, so a generic PON name cannot steal it.
            preferred.sort(
                key=lambda row: (
                    "olt" not in str(row.get("name") or "").lower(),
                    len(_name_tokens(row.get("name")) - olt_tokens),
                    len(str(row.get("name") or "")),
                )
            )
            return str(preferred[0].get("name") or ""), "pon"

    # 2. OLT name or username prefix -> PPPoE server interface
    username = str(user.get("name") or "").lower()
    for server in servers:
        iface = str(server.get("interface") or "").strip()
        if not iface:
            continue
        tokens = _name_tokens(server.get("service_name")) | _name_tokens(iface)
        if olt_tokens and (olt_tokens & tokens):
            return iface, "server"
        if username and any(username.startswith(token) for token in tokens):
            return iface, "server"

    # 3. the always-present dynamic PPPoE interface
    return (str(user.get("iface") or "").strip() or None), "pppoe"


def _attach_access_interfaces(
    users: list[dict[str, Any]],
    router_snapshots: list[Any],
) -> dict[str, int]:
    """Fill ``access_iface`` / ``access_source`` for every user in place."""
    by_router = {str(snapshot.name): snapshot for snapshot in router_snapshots}
    counts: dict[str, int] = {}
    for user in users:
        snapshot = by_router.get(str(user.get("router_name")))
        if snapshot is None:
            continue
        iface, source = _access_interface(user, snapshot, user.get("olt_vlan"))
        user["access_iface"] = iface
        user["access_source"] = source
        if source:
            counts[source] = counts.get(source, 0) + 1
    return counts


class Monitor:
    """Owns the collectors, the polling threads and the DB writes."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        configure_db(cfg)
        self.db_path: Path = resolve_db_path(cfg)

        self.olt_interval = max(5, int((cfg.get("polling") or {}).get("olt_interval_seconds", 60)))
        self.router_interval = max(5, int((cfg.get("polling") or {}).get("router_interval_seconds", 30)))
        self.history_every = max(1, int((cfg.get("database") or {}).get("history_every_n_polls", 5)))

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        # Correlation writes a few thousand rows; it must not hold _lock (the web
        # pages read status through it) and must not run twice at the same time
        # when several pollers finish together.  _refresh_pending coalesces those
        # overlapping requests into one extra pass.
        self._refresh_lock = threading.Lock()
        self._refresh_pending = False
        # reload() rebuilds the collectors; only one reload may run at a time.
        self._reload_lock = threading.Lock()
        # Which device pollers are currently walking their device.  The
        # scheduled loop and the manual "poll all" button share this set, so a
        # slow OLT can never be walked twice at the same time.
        self._active_lock = threading.Lock()
        self._active: set[str] = set()
        self._maintenance_thread: threading.Thread | None = None
        self._poll_count = 0
        self._running = False

        self.olts: list[OltCollector] = []
        self.routers: list[RouterCollector] = []

        self._olt_snapshots: dict[str, Any] = {}
        self._router_snapshots: dict[str, Any] = {}

        self.status: dict[str, Any] = {
            "started_at": None,
            "olts": {},
            "routers": {},
            "correlation": {},
            "alerts": {"critical": 0, "warning": 0, "info": 0, "total": 0},
        }

    # -- lifecycle -------------------------------------------------------
    def init(self) -> None:
        """Prepare the database and load the device list from the registry."""
        init_db(self.db_path)
        registry.ensure_seeded(self.db_path, self.cfg)
        self.build()
        self._drop_removed_devices()

    def build(self) -> None:
        """(Re)create the collectors from the device registry."""
        olts, routers = registry.device_lists(self.db_path, self.cfg)
        self.olts = [OltCollector(self.cfg, device) for device in olts]
        self.routers = [RouterCollector(self.cfg, device) for device in routers]

        # Seed slow optical/inventory fields from the previous snapshot so a
        # restart does not spend 20 minutes re-walking a VSOL GPON table.
        for collector in self.olts:
            try:
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", collector.name)
                collector.set_fdb_cache_path(
                    Path(self.db_path).parent / f"fdb_{safe_name}.json"
                )
                loaded = False
                # MySQL is the single source of truth.  The JSON file is only
                # a compatibility fallback for an installation upgrading from
                # an older release.
                cached = query(
                    self.db_path,
                    "SELECT payload FROM olt_mac_cache WHERE olt_name = ?",
                    [collector.name],
                )
                if cached:
                    try:
                        loaded = collector.import_fdb_cache(
                            json.loads(cached[0].get("payload") or "{}")
                        )
                    except (ValueError, TypeError):
                        loaded = False
                if not loaded:
                    collector.load_fdb_cache()
                previous = query(
                    self.db_path,
                    "SELECT ifindex, rx_power_dbm, tx_power_dbm, olt_rx_power_dbm,"
                    "       temperature_c, voltage_v, bias_current_ma, distance_m,"
                    "       serial, model, vendor, vlan, mac, updated_at"
                    "  FROM olt_onu_current WHERE olt_name = ?",
                    [collector.name],
                )
                collector.warm_optical_cache(previous)
            except Exception as exc:  # pragma: no cover - never block startup
                log.warning("warm optical cache for %s failed: %s", collector.name, exc)

        olt_names = {collector.name for collector in self.olts}
        router_names = {collector.name for collector in self.routers}
        with self._lock:
            # keep the last snapshot of devices that are still around, so
            # correlation does not go blank until the next poll
            self._olt_snapshots = {
                name: snap for name, snap in self._olt_snapshots.items() if name in olt_names
            }
            self._router_snapshots = {
                name: snap for name, snap in self._router_snapshots.items() if name in router_names
            }
            self.status["olts"] = {
                collector.name: self.status["olts"].get(
                    collector.name, self._empty_olt_status(collector)
                )
                for collector in self.olts
            }
            self.status["routers"] = {
                collector.name: self.status["routers"].get(
                    collector.name, self._empty_router_status(collector)
                )
                for collector in self.routers
            }

    def start(self) -> None:
        self.init()
        self.status["started_at"] = utcnow_iso()
        self._running = True
        from .db import describe

        log.info("database: %s", describe())
        log.info("devices: %d OLT(s), %d router(s)", len(self.olts), len(self.routers))
        self._spawn_all()
        self._start_maintenance()

    def _spawn_all(self) -> None:
        for collector in self.olts:
            if not collector.enabled:
                log.warning("OLT %s is disabled", collector.name)
                continue
            self._spawn(
                self.poll_olt, collector,
                self._interval(collector, self.olt_interval), f"olt:{collector.name}",
            )
        for collector in self.routers:
            if not collector.enabled:
                log.warning("router %s is disabled", collector.name)
                continue
            self._spawn(
                self.poll_router, collector,
                self._interval(collector, self.router_interval), f"router:{collector.name}",
            )

    @staticmethod
    def _interval(collector: Any, default: int) -> int:
        """Per-device ``interval_seconds`` overrides the global poll interval."""
        try:
            value = int((collector.cfg or {}).get("interval_seconds") or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value >= 5 else default

    def reload(self) -> None:
        """Re-read the device registry and restart the pollers.

        Called by the web UI after a device is added, edited, disabled or
        deleted, so changes take effect without restarting the process.
        """
        with self._reload_lock:
            log.info("reloading device list")
            # Close the old sockets *before* waiting: an SNMP walk or a router
            # API dump on a busy device can run for a minute, and a closed
            # socket makes the in-flight poll fail within a second instead of
            # stalling the web request that triggered the reload.
            self._stop.set()
            for collector in self.olts + self.routers:
                try:
                    collector.close()
                except Exception:  # pragma: no cover - best effort
                    pass
            # Reload runs on its own thread now, so it can afford to wait for the
            # old pollers properly.  Starting a second walk of the same OLT while
            # the previous one is still running makes a weak OLT time out and
            # report half its ONUs as down.
            for thread in self._threads:
                thread.join(timeout=15)
            self._threads.clear()
            self._stop = threading.Event()
            self.build()
            self._drop_removed_devices()
            if self._running:
                self._spawn_all()
            log.info(
                "reload complete: %d OLT(s), %d router(s)", len(self.olts), len(self.routers)
            )

    def _spawn(self, target: Any, collector: Any, interval: int, label: str) -> None:
        thread = threading.Thread(
            target=self._loop, args=(target, collector, interval, label, self._stop),
            name=f"poll-{label}", daemon=True,
        )
        thread.start()
        self._threads.append(thread)
        log.info("%s poller started (every %ds)", label, interval)

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()
        self.close()
        log.info("monitor stopped")

    def close(self) -> None:
        """Release network resources without touching the worker threads."""
        for collector in self.olts + self.routers:
            try:
                collector.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def _loop(self, target: Any, collector: Any, interval: int, label: str, stop: threading.Event) -> None:
        # the stop event is bound at spawn time so a reload() that installs a
        # fresh event cannot resurrect an old thread
        while not stop.is_set():
            started = time.monotonic()
            with self._active_lock:
                self._active.add(label)
            try:
                target(collector, stop)
            except Exception:  # never let a worker die
                log.exception("%s poller crashed", label)
            finally:
                with self._active_lock:
                    self._active.discard(label)
            elapsed = time.monotonic() - started
            if elapsed > interval:
                log.warning(
                    "%s poll took %.0fs, longer than its %ds interval - raising "
                    "interval_seconds on the Devices page (or the per-vendor "
                    "stagger) would stop continuous polling",
                    label, elapsed, interval,
                )
            # Never busy-loop a device: keep a small idle gap even when a poll
            # overruns its interval.  If a poll is slower than the interval
            # (a VSOL GPON with hundreds of ONUs), give the agent at least half
            # the poll duration to recover instead of starting the next walk
            # five seconds later.
            wait = interval - elapsed
            if wait < 5.0:
                wait = max(5.0, min(float(interval), elapsed * 0.5))
            stop.wait(wait)

    # -- retention / maintenance ----------------------------------------
    def _start_maintenance(self) -> None:
        """Start the 24-hour history cleaner once per process."""
        if self._maintenance_thread is not None and self._maintenance_thread.is_alive():
            return
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="history-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def _maintenance_loop(self) -> None:
        interval = 300
        try:
            interval = max(60, int((self.cfg.get("database") or {}).get(
                "prune_interval_seconds", 300)))
        except (TypeError, ValueError):
            pass
        # Run once at startup so an installation that was offline for days
        # drops the stale rows immediately, then keep sweeping every few
        # minutes.
        self._housekeeping()
        while not self._stop.wait(interval):
            self._housekeeping()

    def _housekeeping(self) -> None:
        """Delete history rows older than the configured retention window.

        Retention is 24 hours by default.  This runs on its own thread every
        few minutes, so it does not depend on how often a device finishes a
        poll and it keeps the history tables small (which is also what keeps
        the trend queries and the web UI fast).
        """
        try:
            keep = int((self.cfg.get("database") or {}).get("keep_history_days", 1))
        except (TypeError, ValueError):
            keep = 1
        try:
            removed = prune_history(self.db_path, keep)
            if removed:
                log.info("retention: pruned %d history rows older than %d day(s)", removed, keep)
        except Exception:
            log.exception("history pruning failed")

    def _still_current(self, collector: Any, stop: threading.Event | None, label: str) -> bool:
        """Refuse to store a poll whose device was deleted or renamed meanwhile.

        An SNMP walk takes about 90 s on the real OLT, so a reload() can easily
        land in the middle of one.  The old thread would then write its rows back
        under the *old* name and the device we just removed would reappear.
        """
        if stop is not None and stop.is_set():
            log.info("%s poll discarded: the device list was reloaded while it ran", label)
            return False
        with self._lock:
            names = {c.name for c in self.olts} | {c.name for c in self.routers}
        if collector.name not in names:
            log.info("%s poll discarded: the device is no longer registered", label)
            return False
        return True

    # -- polling ---------------------------------------------------------
    def poll_olt(self, collector: OltCollector, stop: threading.Event | None = None) -> None:
        snapshot = collector.collect()
        if not self._still_current(collector, stop, f"OLT {collector.name}"):
            return
        self.store_olt(collector, snapshot)
        self.refresh()

    def poll_router(self, collector: RouterCollector, stop: threading.Event | None = None) -> None:
        snapshot = collector.collect()
        if not self._still_current(collector, stop, f"Router {collector.name}"):
            return
        self.store_router(collector, snapshot)
        self.refresh()

    def poll_now(self, kind: str, name: str) -> str:
        """Trigger an immediate poll of one device from the web UI.

        The poll runs on its own thread (an OLT walk can take minutes), so the
        HTTP request returns immediately and the page picks the new data up on
        its next refresh.  Returns one of:

        ``started``  - a new background poll was queued
        ``running``  - this device is already being polled, nothing was queued
        ``missing``  - the device is unknown or disabled
        """
        with self._lock:
            pool = self.olts if kind == "olt" else self.routers
            collector = next((c for c in pool if c.name == name), None)
        if collector is None or not collector.enabled:
            return "missing"

        label = f"{kind}:{name}"
        with self._active_lock:
            if label in self._active:
                return "running"
            # reserve the slot now: the thread may take a moment to start
            self._active.add(label)

        target = self.poll_olt if kind == "olt" else self.poll_router

        def run() -> None:
            try:
                target(collector, None)
            except Exception:  # pragma: no cover - the loop would normally catch it
                log.exception("manual poll of %s %s failed", kind, name)
            finally:
                with self._active_lock:
                    self._active.discard(label)

        threading.Thread(
            target=run, name=f"manual-{kind}:{name}", daemon=True
        ).start()
        log.info("manual poll requested for %s %s", kind, name)
        return "started"

    def poll_all(self, kind: str = "all") -> dict[str, list[str]]:
        """Queue every enabled device for an immediate poll.

        Returns the device names grouped by outcome so the web UI can explain
        exactly what happened.  Devices already being polled are skipped, so
        pressing the button twice (or while the 10-minute cycle is running)
        can never create a second concurrent walk.
        """
        result: dict[str, list[str]] = {"started": [], "running": [], "missing": []}
        collectors: list[tuple[str, Any]] = []
        if kind in ("all", "olt"):
            collectors.extend(("olt", c) for c in self.olts)
        if kind in ("all", "router"):
            collectors.extend(("router", c) for c in self.routers)
        for item_kind, collector in collectors:
            if not collector.enabled:
                result["missing"].append(collector.name)
                continue
            outcome = self.poll_now(item_kind, collector.name)
            result[outcome].append(collector.name)
        return result

    def polling_status(self) -> dict[str, Any]:
        """Small JSON-friendly snapshot for the Devices page poll indicator."""
        with self._active_lock:
            active = sorted(self._active)
        return {"active": active, "count": len(active)}

    # -- status helpers --------------------------------------------------
    @staticmethod
    def _empty_olt_status(collector: OltCollector) -> dict[str, Any]:
        return {
            "name": collector.name,
            "host": collector.host,
            "enabled": collector.enabled,
            "ok": None,
            "error": None,
            "last_run": None,
            "duration_ms": None,
            "ports": 0,
            "onus": 0,
            "mac_entries": 0,
            "health": {},
        }

    @staticmethod
    def _empty_router_status(collector: RouterCollector) -> dict[str, Any]:
        return {
            "name": collector.name,
            "host": collector.host,
            "enabled": collector.enabled,
            "ok": None,
            "error": None,
            "last_run": None,
            "duration_ms": None,
            "users": 0,
            "interfaces": 0,
        }

    # -- storage ---------------------------------------------------------
    def store_olt(self, collector: OltCollector, snapshot: Any) -> None:
        stamp = utcnow_iso()
        with self._lock:
            # A failed poll must not wipe the last good snapshot: an OLT whose
            # SNMP community is wrong would otherwise lose its brand, ONU
            # count and every other detail the UI reads from this payload, and
            # look like a brand-new device with no data at all.
            previous = query(
                self.db_path,
                "SELECT payload, updated_at, ok FROM device_current WHERE `key` = ?",
                [f"olt:{snapshot.name}"],
            )
            previous = previous[0] if previous else None
            info = dict(snapshot.info or {})
            if not snapshot.ok:
                previous_info: dict[str, Any] = {}
                if previous and previous.get("payload"):
                    try:
                        loaded = json.loads(previous["payload"])
                        if isinstance(loaded, dict):
                            previous_info = loaded
                    except (ValueError, TypeError):
                        previous_info = {}
                merged = dict(previous_info)
                merged.update(info)
                info = merged
                last_good = previous_info.get("last_good_at")
                if not last_good and previous and previous.get("ok"):
                    last_good = previous.get("updated_at")
                if not last_good:
                    # The payload may predate last_good_at (or be empty after a
                    # very first failed poll).  The collected ONU/port rows still
                    # record when this OLT last returned data.
                    stamps = [
                        row.get("m")
                        for row in query(
                            self.db_path,
                            "SELECT MAX(updated_at) AS m FROM olt_onu_current WHERE olt_name = ?"
                            " UNION ALL"
                            " SELECT MAX(updated_at) AS m FROM olt_port_current WHERE olt_name = ?",
                            [snapshot.name, snapshot.name],
                        )
                        if row.get("m")
                    ]
                    if stamps:
                        last_good = max(stamps)
                if last_good:
                    info["last_good_at"] = last_good
                if snapshot.error:
                    info["poll_error"] = snapshot.error
                info["stale"] = True

            upsert(
                self.db_path,
                "device_current",
                ["key"],
                {
                    "key": f"olt:{snapshot.name}",
                    "kind": "olt",
                    "name": snapshot.name,
                    "ok": 1 if snapshot.ok else 0,
                    "payload": json.dumps(info),
                    "error": snapshot.error,
                    "updated_at": stamp,
                },
            )
            entry = {
                "name": snapshot.name,
                "host": collector.host,
                "enabled": collector.enabled,
                "ok": snapshot.ok,
                "error": snapshot.error,
                "last_run": stamp,
                "duration_ms": collector.stats.get("last_duration_ms"),
                "ports": len(snapshot.ports),
                "onus": len(snapshot.onus),
                "mac_entries": len(snapshot.mac_map or {}),
                "health": getattr(snapshot, "health", {}) or {},
                "info": info,
            }
            if not snapshot.ok:
                # Keep showing how many ports/ONUs were last known instead of
                # reporting "fail - 0 ONUs" for a device that merely went
                # unreachable.  last_good stays pinned to the last good poll.
                previous_status = self.status["olts"].get(snapshot.name) or {}
                for key in ("ports", "onus", "mac_entries"):
                    if not entry[key] and previous_status.get(key):
                        entry[key] = previous_status[key]
                entry["stale"] = True
                entry["last_good"] = (
                    info.get("last_good_at") or previous_status.get("last_good")
                )
            else:
                entry["stale"] = False
                entry["last_good"] = stamp
            self.status["olts"][snapshot.name] = entry
            self._store_health(snapshot, stamp)
            if not snapshot.ok:
                return

            upsert_many(self.db_path, "olt_port_current", ["olt_name", "ifindex"], snapshot.ports)
            # The OLT knows nothing about PPPoE.  Keep the last subscriber name
            # already stored for this ONU so a user who dropped because the ONU
            # went down still appears in the alert and on the ONU page.
            # Slow vendors (VSOL GPON) refresh serial/model/VLAN/MAC on a
            # staggered schedule, so a poll may legitimately return NULL for a
            # field.  Carry the last good value forward instead of blanking the
            # page between refreshes.
            carried_onu = (
                "pppoe_user", "mac", "serial", "model", "vendor", "sw_version",
                "onu_type", "vlan", "rx_power_dbm", "tx_power_dbm",
                "olt_rx_power_dbm", "temperature_c", "voltage_v",
                "bias_current_ma", "distance_m", "last_down_reason",
                "last_down_at",
            )
            previous_rows = {
                row["ifindex"]: row
                for row in query(
                    self.db_path,
                    "SELECT ifindex, oper_status, " + ", ".join(carried_onu)
                    + " FROM olt_onu_current WHERE olt_name = ?",
                    [snapshot.name],
                )
            }
            for row in snapshot.onus:
                old = previous_rows.get(row.get("ifindex"))
                if not old:
                    continue
                for field in carried_onu:
                    if row.get(field) is None:
                        old_value = old.get(field)
                        if field.endswith("_power_dbm") and not _plausible_power(old_value):
                            # Never carry a vendor sentinel (INT32_MIN) forward:
                            # the collector deliberately left this reading NULL.
                            continue
                        row[field] = old_value
            # An ONU that just went down gets a timestamp, so the "Last down"
            # column is populated on every vendor (not only CLI EPON).
            mark_last_down(snapshot.onus, previous_rows, stamp)
            upsert_many(self.db_path, "olt_onu_current", ["olt_name", "ifindex"], snapshot.onus)

            # A re-classification can move an interface between tables (an ONU
            # whose name carries a description used to land in the port table).
            # Upsert does not delete the old row, so remove anything this poll
            # no longer sees - but only when the poll actually returned rows,
            # never after a partial/failed walk.
            for table, rows in (
                ("olt_port_current", snapshot.ports),
                ("olt_onu_current", snapshot.onus),
            ):
                keys = [int(r["ifindex"]) for r in rows if r.get("ifindex") is not None]
                if not keys:
                    continue
                placeholders = ", ".join("?" for _ in keys)
                execute(
                    self.db_path,
                    f"DELETE FROM {table} WHERE olt_name = ?"
                    f" AND ifindex NOT IN ({placeholders})",
                    [snapshot.name, *keys],
                )
            self._olt_snapshots[snapshot.name] = snapshot

            if self._tick():
                insert_many(
                    self.db_path,
                    "port_history",
                    [
                        {
                            "ts": stamp,
                            "olt_name": row["olt_name"],
                            "ifindex": row["ifindex"],
                            "if_name": row["if_name"],
                            "kind": row["kind"],
                            "oper_status": row["oper_status"],
                            "in_bps": row["in_bps"],
                            "out_bps": row["out_bps"],
                        }
                        for row in snapshot.ports
                    ],
                )
                insert_many(
                    self.db_path,
                    "olt_onu_history",
                    [
                        {
                            "ts": stamp,
                            "olt_name": row["olt_name"],
                            "ifindex": row["ifindex"],
                            "if_name": row["if_name"],
                            "oper_status": row["oper_status"],
                            "rx_power_dbm": row["rx_power_dbm"],
                            "tx_power_dbm": row["tx_power_dbm"],
                            "olt_rx_power_dbm": row["olt_rx_power_dbm"],
                            "distance_m": row["distance_m"],
                        }
                        for row in snapshot.onus
                    ],
                )
        if snapshot.ok:
            self._persist_fdb_cache(collector)
            log.info(
                "OLT %s stored: %d ports, %d ONUs, %d MACs",
                snapshot.name, len(snapshot.ports), len(snapshot.onus), len(snapshot.mac_map or {}),
            )
        else:
            log.warning("OLT %s poll failed: %s", snapshot.name, snapshot.error)

    def _persist_fdb_cache(self, collector: OltCollector) -> None:
        """Store the forwarding-table snapshot in MySQL (single database)."""
        try:
            payload = json.dumps(collector.export_fdb_cache(), separators=(",", ":"))
        except Exception:  # pragma: no cover - defensive
            return
        try:
            upsert(
                self.db_path,
                "olt_mac_cache",
                ["olt_name"],
                {
                    "olt_name": collector.name,
                    "payload": payload,
                    "updated_at": utcnow_iso(),
                },
            )
        except Exception:
            log.exception("could not persist MAC cache for %s", collector.name)

    def _store_health(self, snapshot: Any, stamp: str) -> None:
        """Persist the OLT health row (also when the poll failed - the error counts)."""
        health = dict(getattr(snapshot, "health", {}) or {})
        row = {
            "olt_name": snapshot.name,
            "ok": 1 if health.get("ok") and snapshot.ok else 0,
            "uptime_seconds": health.get("uptime_seconds"),
            "cpu_percent": health.get("cpu_percent"),
            "memory_percent": health.get("memory_percent"),
            "memory_total_mb": health.get("memory_total_mb"),
            "memory_free_mb": health.get("memory_free_mb"),
            "temperature_c": health.get("temperature_c"),
            "snmp_rtt_ms": health.get("snmp_rtt_ms"),
            "telnet_ok": None if health.get("telnet_ok") is None else (1 if health.get("telnet_ok") else 0),
            "telnet_ms": health.get("telnet_ms"),
            "telnet_error": health.get("telnet_error"),
            "error": snapshot.error,
            "updated_at": stamp,
        }
        upsert(self.db_path, "olt_health_current", ["olt_name"], row)
        if self._tick():
            insert_many(
                self.db_path,
                "olt_health_history",
                [
                    {
                        "ts": stamp,
                        "olt_name": snapshot.name,
                        "uptime_seconds": row["uptime_seconds"],
                        "cpu_percent": row["cpu_percent"],
                        "memory_percent": row["memory_percent"],
                        "temperature_c": row["temperature_c"],
                        "snmp_rtt_ms": row["snmp_rtt_ms"],
                    }
                ],
            )

    def store_router(self, collector: RouterCollector, snapshot: Any) -> None:
        stamp = utcnow_iso()
        with self._lock:
            upsert(
                self.db_path,
                "device_current",
                ["key"],
                {
                    "key": f"router:{snapshot.name}",
                    "kind": "router",
                    "name": snapshot.name,
                    "ok": 1 if snapshot.ok else 0,
                    "payload": json.dumps(snapshot.info),
                    "error": snapshot.error,
                    "updated_at": stamp,
                },
            )
            self.status["routers"][snapshot.name] = {
                "name": snapshot.name,
                "host": collector.host,
                "enabled": collector.enabled,
                "ok": snapshot.ok,
                "error": snapshot.error,
                "last_run": stamp,
                "duration_ms": collector.stats.get("last_duration_ms"),
                "users": len(snapshot.users),
                "interfaces": len(snapshot.interfaces),
            }
            if not snapshot.ok:
                return

            upsert_many(
                self.db_path, "router_iface_current", ["router_name", "name"], snapshot.interfaces
            )
            # The router collector knows nothing about ONUs or optical power,
            # so its fresh rows carry NULL in those columns.  Keep the values
            # from the previous correlation pass until the pass that follows
            # this store overwrites (or clears) them; otherwise every router
            # poll makes every user flash "not linked" for a few seconds.
            carried_fields = (
                "onu_ifindex", "onu_name", "onu_olt_name", "onu_pon", "match_method",
                "rx_power_dbm", "tx_power_dbm", "olt_rx_power_dbm", "distance_m",
            )
            access_fields = ("olt_vlan", "access_iface", "access_source", "onu_pon")
            stored = query(
                self.db_path,
                "SELECT name, onu_ifindex, onu_name, onu_olt_name, onu_pon, match_method,"
                "       rx_power_dbm, tx_power_dbm, olt_rx_power_dbm, distance_m,"
                "       olt_vlan, access_iface, access_source"
                "  FROM pppoe_user_current WHERE router_name = ?",
                [snapshot.name],
            )
            previous = {row["name"]: row for row in stored}
            for row in snapshot.users:
                old = previous.get(str(row.get("name")))
                if old is None:
                    continue
                # Access interface/VLAN are computed for every user, linked or
                # not, so they can always be carried across a router poll.
                for field in access_fields:
                    row[field] = old.get(field)
                if old.get("onu_ifindex") is None and old.get("onu_pon") is None:
                    continue
                for field in carried_fields:
                    row[field] = old.get(field)

            upsert_many(
                self.db_path, "pppoe_user_current", ["router_name", "name"], snapshot.users
            )
            # ``pppoe_user_current`` means *currently online*.  A user who has
            # logged out must disappear from the Users page immediately, not
            # linger with a stale uptime / interface until the next OLT poll.
            current_names = {str(row.get("name")) for row in snapshot.users if row.get("name")}
            if current_names:
                stale = [row["name"] for row in stored if row["name"] not in current_names]
                for start in range(0, len(stale), 400):
                    chunk = stale[start:start + 400]
                    placeholders = ", ".join("?" for _ in chunk)
                    execute(
                        self.db_path,
                        f"DELETE FROM pppoe_user_current WHERE router_name = ? AND name IN ({placeholders})",
                        [snapshot.name, *chunk],
                    )
            self._router_snapshots[snapshot.name] = snapshot

            if self._tick():
                insert_many(
                    self.db_path,
                    "pppoe_user_history",
                    [
                        {
                            "ts": stamp,
                            "router_name": row["router_name"],
                            "name": row["name"],
                            "address": row["address"],
                            "uptime_seconds": row["uptime_seconds"],
                            "bytes_in": row["bytes_in"],
                            "bytes_out": row["bytes_out"],
                            "rx_power_dbm": row["rx_power_dbm"],
                        }
                        for row in snapshot.users
                    ],
                )
                info = snapshot.info or {}
                insert_many(
                    self.db_path,
                    "router_history",
                    [
                        {
                            "ts": stamp,
                            "router_name": snapshot.name,
                            "cpu_load": info.get("cpu_load"),
                            "free_memory_percent": info.get("free_memory_percent"),
                            "uptime_seconds": info.get("uptime_seconds"),
                            "pppoe_online": len(snapshot.users),
                        }
                    ],
                )
        if snapshot.ok:
            log.info(
                "Router %s stored: %d interfaces, %d users",
                snapshot.name, len(snapshot.interfaces), len(snapshot.users),
            )
        else:
            log.warning("Router %s poll failed: %s", snapshot.name, snapshot.error)

    # -- shared post-processing -----------------------------------------
    def refresh(self) -> None:
        """Correlate users with ONUs, persist the result and evaluate alerts.

        Every poller calls this when its poll finishes, so on a real network
        (thousands of users against ~1200 ONUs) several calls land together.
        One pass runs at a time and overlapping requests are coalesced into a
        single follow-up pass instead of queueing up.
        """
        if not self._refresh_lock.acquire(blocking=False):
            self._refresh_pending = True
            return
        try:
            while True:
                self._refresh_pending = False
                self._refresh_once()
                if not self._refresh_pending:
                    break
        finally:
            self._refresh_lock.release()

    def _refresh_once(self) -> None:
        with self._lock:
            olt_snapshots = list(self._olt_snapshots.values())
            router_snapshots = list(self._router_snapshots.values())

        onus: list[dict[str, Any]] = []
        for snapshot in olt_snapshots:
            onus.extend(snapshot.onus)
        users: list[dict[str, Any]] = []
        mac_by_ip: dict[str, str] = {}
        for snapshot in router_snapshots:
            users.extend(snapshot.users)
            for ip, mac in (snapshot.mac_by_ip or {}).items():
                mac_by_ip.setdefault(ip, mac)

        if users and onus:
            # An OLT poll can take minutes, so right after a restart - or while one
            # OLT is unreachable - some OLTs have no snapshot yet.  For those, keep
            # the ONU link that is already in the database instead of blanking it,
            # otherwise every user would lose their dBm until the next full cycle.
            known_olts = {snapshot.name for snapshot in olt_snapshots}
            configured_olts = {collector.name for collector in self.olts}
            carried: dict[tuple[Any, Any], dict[str, Any]] = {}
            old_links: dict[tuple[Any, Any], tuple[Any, Any]] = {}
            for row in query(
                self.db_path,
                "SELECT router_name, name, onu_olt_name, onu_ifindex, onu_pon, onu_name,"
                "       match_method, rx_power_dbm, tx_power_dbm, olt_rx_power_dbm,"
                "       distance_m, olt_vlan, access_iface, access_source"
                "  FROM pppoe_user_current"
                " WHERE onu_ifindex IS NOT NULL OR onu_pon IS NOT NULL",
            ):
                key = (row.get("router_name"), row.get("name"))
                if row.get("onu_ifindex") is not None:
                    old_links[key] = (row.get("onu_olt_name"), row.get("onu_ifindex"))
                olt_name = row.get("onu_olt_name")
                if olt_name in configured_olts and olt_name not in known_olts:
                    carried[key] = row

            fdb = build_fdb(olt_snapshots)
            fdb_vlans = build_fdb_vlans(olt_snapshots)
            fdb_pons = build_fdb_pons(olt_snapshots)
            counts = correlate(
                self.cfg, onus, users, mac_by_ip, fdb, fdb_vlans, fdb_pons
            )
            counts["fdb_size"] = len(fdb)
            counts["fdb_vlans"] = len(fdb_vlans)
            counts["fdb_pons"] = len(fdb_pons)
            counts["onus"] = len(onus)
            counts["users"] = len(users)

            restored = 0
            for user in users:
                if user.get("onu_ifindex") is not None or user.get("onu_pon") is not None:
                    continue
                old = carried.get((user.get("router_name"), user.get("name")))
                if not old:
                    continue
                user["onu_olt_name"] = old.get("onu_olt_name")
                user["onu_ifindex"] = old.get("onu_ifindex")
                user["onu_pon"] = old.get("onu_pon")
                user["onu_name"] = old.get("onu_name")
                user["match_method"] = old.get("match_method") or "cached"
                user["rx_power_dbm"] = old.get("rx_power_dbm")
                user["tx_power_dbm"] = old.get("tx_power_dbm")
                user["olt_rx_power_dbm"] = old.get("olt_rx_power_dbm")
                user["distance_m"] = old.get("distance_m")
                user["olt_vlan"] = old.get("olt_vlan")
                user["access_iface"] = old.get("access_iface")
                user["access_source"] = old.get("access_source")
                restored += 1
            if restored:
                counts["cached"] = restored
                counts["unmatched"] = max(0, counts["unmatched"] - restored)

            access_counts = _attach_access_interfaces(users, router_snapshots)
            if access_counts:
                counts["access"] = access_counts

            upsert_many(
                self.db_path, "pppoe_user_current", ["router_name", "name"], users
            )
            pairs = [
                (user["name"], user["onu_olt_name"], user["onu_ifindex"])
                for user in users
                if user.get("onu_ifindex") is not None and user.get("onu_olt_name")
            ]
            # Move/clear handling: if an online user is now on a different ONU
            # (or no ONU at all), the old ONU must not keep that name.  ONUs
            # whose subscriber merely went offline are left alone on purpose -
            # that last name is what makes a DOWN alert say who is affected.
            new_targets = {(olt, ifindex) for _, olt, ifindex in pairs}
            moved: list[tuple[Any, Any]] = []
            for user in users:
                key = (user.get("router_name"), user.get("name"))
                old = old_links.get(key)
                if not old:
                    continue
                if old == (user.get("onu_olt_name"), user.get("onu_ifindex")):
                    continue
                if old in new_targets:
                    continue
                moved.append(old)
            if moved:
                executemany(
                    self.db_path,
                    "UPDATE olt_onu_current SET pppoe_user=NULL WHERE olt_name=? AND ifindex=?",
                    moved,
                )
            # One statement, one transaction.  A per-pair execute() cost a commit
            # each (~540 fsyncs) and that is what made /devices take ~40 s: the
            # web pages read status through _lock while this loop held it.
            executemany(
                self.db_path,
                "UPDATE olt_onu_current SET pppoe_user=? WHERE olt_name=? AND ifindex=?",
                pairs,
            )
            # Some GPON agents report the ONU's UNI port state, not the PON
            # link state, so a subscriber can be passing traffic while
            # ifOperStatus says "down".  An active PPPoE session is stronger
            # evidence than that column; mark those ONUs up to avoid false
            # DOWN alerts.  A truly dead ONU loses its session immediately.
            if pairs:
                executemany(
                    self.db_path,
                    "UPDATE olt_onu_current SET oper_status=1"
                    " WHERE olt_name=? AND ifindex=? AND oper_status<>1",
                    [(olt, ifindex) for _, olt, ifindex in pairs],
                )
            with self._lock:
                self.status["correlation"] = counts

        elif users:
            # No ONU inventory yet (e.g. an OLT is still on its first poll),
            # but the router interface can still be named from the PPPoE server
            # list and the username prefix.
            _attach_access_interfaces(users, router_snapshots)
            upsert_many(
                self.db_path, "pppoe_user_current", ["router_name", "name"], users
            )

        try:
            found = alerts_mod.evaluate(self.cfg, self.db_path, self.status)
        except Exception:
            log.exception("alert evaluation failed")
            return

        summary = {"critical": 0, "warning": 0, "info": 0, "total": len(found)}
        for alert in found:
            summary[alert["severity"]] = summary.get(alert["severity"], 0) + 1
        self.status["alerts"] = summary

    def _tick(self) -> bool:
        self._poll_count += 1
        return self._poll_count % self.history_every == 0

    def _drop_removed_devices(self) -> None:
        """Delete rows belonging to devices that are no longer configured."""
        with self._lock:
            olt_names = [collector.name for collector in self.olts]
            router_names = [collector.name for collector in self.routers]
            for table, column, names in (
                ("olt_port_current", "olt_name", olt_names),
                ("olt_onu_current", "olt_name", olt_names),
                ("olt_health_current", "olt_name", olt_names),
                ("olt_health_history", "olt_name", olt_names),
                ("olt_mac_cache", "olt_name", olt_names),
                ("router_iface_current", "router_name", router_names),
                ("pppoe_user_current", "router_name", router_names),
            ):
                if names:
                    placeholders = ", ".join("?" for _ in names)
                    execute(
                        self.db_path,
                        f"DELETE FROM {table} WHERE {column} NOT IN ({placeholders})",
                        names,
                    )
                else:
                    execute(self.db_path, f"DELETE FROM {table}")

            keys = [f"olt:{name}" for name in olt_names] + [
                f"router:{name}" for name in router_names
            ]
            if keys:
                placeholders = ", ".join("?" for _ in keys)
                execute(
                    self.db_path,
                    f"DELETE FROM device_current WHERE `key` NOT IN ({placeholders})",
                    keys,
                )
            else:
                execute(self.db_path, "DELETE FROM device_current")


def run_once(cfg: dict, which: str = "all") -> None:
    """Poll once, store, correlate and exit (used by CLI ``--once``)."""
    monitor = Monitor(cfg)
    monitor.init()
    if which in ("all", "olt"):
        for collector in monitor.olts:
            if collector.enabled:
                monitor.poll_olt(collector)
    if which in ("all", "router"):
        for collector in monitor.routers:
            if collector.enabled:
                monitor.poll_router(collector)
    monitor.refresh()
    monitor.close()
