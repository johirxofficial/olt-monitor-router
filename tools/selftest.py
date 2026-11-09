"""Offline self-test for the auto-detection heuristics, the optical parser and
the device registry.

Runs **without any device**: it feeds the discovery code a synthetic column dump
that reproduces the awkward parts of the real VSOL V1600D tree (optical power as
text, ONU ids with holes, a PON column that looks like Tx power, row counters
that look like dBm), then checks that the collector resolves every ONU, that
interface names are classified correctly, and that the MySQL device registry
plus ``Monitor.reload()`` behave.

The registry part uses a **separate** MySQL database (``<DB_NAME>_selftest`` by
default) that is wiped before the run, so the live ``olt_monitor`` data is never
touched.  Create it once with ``tools/setup_mysql.py`` (it is created by
default).  Override the name with ``OLT_TEST_DB`` if needed.

    python3 tools/selftest.py

Exit code 0 means everything matched.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discover_olt as D  # noqa: E402
from olt_monitor.collectors.olt import OltCollector  # noqa: E402

ROOT = "1.3.6.1.4.1.37950"
PER_PON = [26, 60, 47, 65, 67, 31, 48, 42]
assert sum(PER_PON) == 386

random.seed(7)

# ONU ids are NOT contiguous - the real OLT has deleted ONUs leaving holes
ONU_IDS: dict[int, list[int]] = {}
for _pon, _count in enumerate(PER_PON, start=1):
    _span = int(_count * 1.15) + 3
    ONU_IDS[_pon] = sorted(random.sample(range(1, _span + 1), _count))

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}")
    if not ok:
        failures += 1


class _FakeCli:
    """A tiny stand-in for an OLT's Telnet CLI.

    Speaks just enough of the protocol to exercise :class:`TelnetSession`:
    a login banner, a prompt, command output, and one ``--More--`` page that must
    be completed by sending a bare space.
    """

    def __init__(self, restricted: bool = False, photon: bool = False,
                 cdata: bool = False) -> None:
        import socket
        import threading

        self.restricted = restricted
        self.photon = photon
        self.cdata = cdata
        self.username = ""
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = int(self._server.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            stream = conn.makefile("rwb")

            def send(text: str) -> None:
                conn.sendall(text.encode("utf-8", errors="ignore"))

            if self.cdata:
                # FD13xx prints the banner but no login prompt until it gets a
                # newline, and its enable step takes no password.
                send("EPON OLT Integrated Operating System\r\n"
                     "Device Model : AC-FD1304E-B1-PDA0\r\n")
                stream.readline()  # the client's bare-newline nudge
                send("User name: ")
                self.username = stream.readline().decode(errors="ignore").strip()
                send("Password: ")
                stream.readline()
                send("CDATA> ")
                if stream.readline().decode(errors="ignore").strip().lower() == "enable":
                    send("CDATA# ")
            else:
                send("Username: ")
                self.username = stream.readline().decode(errors="ignore").strip()
                send("Password: ")
                stream.readline()
                send("VSOL# ")
            while True:
                raw = stream.readline()
                if not raw:
                    return
                command = raw.decode(errors="ignore").strip()
                if not command:
                    continue
                if self.restricted and command != "exit":
                    # an account that may log in but not run anything useful
                    send("\r\n% Unknown command.\r\nVSOL# ")
                    continue
                if command.startswith("show version"):
                    send("VSOL V1600D EPON OLT\r\nVersion 1.2.3\r\nVSOL# ")
                elif self.photon and command == "show cpu":
                    send("cpu0: 7%\r\nVSOL# ")
                elif self.photon and command == "show memory":
                    send("MemTotal:         223528 kB\r\nMemFree:          141356 kB\r\n"
                         "Memory usage: 22.15%\r\nVSOL# ")
                elif self.photon and command == "show pon-port ddm-info pon1":
                    send(" Laser bias current : 13.600 mA\r\n"
                         " Laser output power : 8.1780 mW / 9.13 dBm\r\n"
                         " Module temperature : 26.35 degrees C\r\n"
                         " Module voltage     : 3.2924 V\r\nVSOL# ")
                elif self.photon and command == "show pon-port ddm-info pon2":
                    send("  Error: The optic module of port is absence!\r\nVSOL# ")
                elif self.photon and command == "show mac-address-table":
                    send("VLAN  MAC-ADDRESS     TYPE     PORT\r\n"
                         "----  --------------  -------  ----\r\n"
                         "1     70a5.6a2e.fb4f  dynamic  pon1\r\nVSOL# ")
                elif self.photon and command == "show ont info pon1 all":
                    send("PORT   ONT MAC            Control  Run     Config  Match    Desc\r\n"
                         "       ID                 flag     state   state   state\r\n"
                         "====== === ============== ======== ======= ======= ======== ======\r\n"
                         "pon1   1   70A5.6A2E.FB4F active   online  success match    \r\n"
                         "pon1   2   702E.2207.4463 active   offline initial initial  \r\nVSOL# ")
                elif self.photon and command == "show ont optical-info pon1 all":
                    send("ONT ONT              Voltage  Tx optical  Rx optical   Laser bias   Temperature\r\n"
                         "ID  MAC              (V)      power(dBm)  power(dBm)   current(mA)  (C)\r\n"
                         "=== ================ ======== ==========  ==========   ===========  ==========\r\n"
                         "1   70A5.6A2E.FB4F  3.31      1.44        -15.35       11.80        30.84\r\n"
                         "2   702E.2207.4463\r\nVSOL# ")
                elif self.photon and command == "show ont basic-info pon1 all":
                    send("ONT MAC            Type    Distance  Up          Extended\r\n"
                         "ID                         (m)       Time        Model   \r\n"
                         "=== ============== ======= ======== =========== ========\r\n"
                         "1   70A5.6A2E.FB4F SFU     2631     00:14:51:13  110F\r\n"
                         "2   702E.2207.4463 --\r\nVSOL# ")
                elif self.photon and command == "show ont version pon1 all":
                    send("ONT MAC            Vendor   Model      Hardware  Software\r\n"
                         "ID                 ID       ID         version   version\r\n"
                         "=== ============== ======== ========== ======== ==========\r\n"
                         "1   70A5.6A2E.FB4F CDT      0x31313046 F331.1A  V1.2.5\r\nVSOL# ")
                elif self.photon and command.startswith("show ont ") and command.endswith(" pon2 all"):
                    send("  Error: There is no ONT available.\r\nVSOL# ")
                elif self.cdata and command == "show cpu":
                    send("CPU usage: 31%\r\nCDATA# ")
                elif self.cdata and command == "show memory":
                    send("Memory usage: 13%\r\nCDATA# ")
                elif self.cdata and command == "show temperature":
                    send("Slot  current\r\n0     44.50\r\n1     42.25\r\nCDATA# ")
                elif self.cdata and command == "show version":
                    send("Software version V3.4.57\r\nCDATA# ")
                elif self.cdata and command == "show mac all":
                    send(" MAC                   VLAN    Port           MAC-Type\r\n"
                         " 08:40:F3:EF:0F:57     1365    epon0/1/4      dynamic\r\n"
                         " 74:4D:28:5C:78:EB     1365    xge0/0/4       dynamic\r\nCDATA# ")
                elif self.cdata and command == "show ont info all":
                    send("F/S P  ONT    MAC                  Control  Run     Config    Match\n"
                         "  0/1 1  1      70:2E:22:01:96:5D    Active   Online  success   match\r\n"
                         "  0/1 2  7      00:D3:9E:71:53:8A    Active   Offline initial   match\r\n"
                         "CDATA# ")
                elif self.cdata and command in ("config", "exit"):
                    send("CDATA# ")
                elif command == "show epon active-onu":
                    send("ONU-1 up\r\n--More--")
                    stream.read(1)  # the client answers paging with a space
                    send("ONU-2 up\r\nVSOL# ")
                elif command == "exit":
                    send("\r\n")
                    return
                else:
                    send("\rVSOL# ")


def build_columns() -> dict:
    columns: dict[str, dict] = {}

    def add(head: str, index: str, value) -> None:
        col = columns.setdefault(head, {"values": [], "indexes": []})
        col["values"].append(value)
        col["indexes"].append(index)

    row = 0
    for pon, count in enumerate(PER_PON, start=1):
        for onu in ONU_IDS[pon]:
            # ---- optical text table:  <table>.<pon>.<onu> ----
            # hundreds of distinct readings, exactly like the real device
            add(f"{ROOT}.1.1.5.12.2.1.8.1.6.{pon}", str(onu),
                f"1.92 mW ({1 + (row % 260) / 100:.2f} dBm)")
            add(f"{ROOT}.1.1.5.12.2.1.8.1.7.{pon}", str(onu),
                f"0.04 mW (-{8 + (row % 241) / 10:.1f} dBm)")
            # ---- inventory table:     <table>.<column>.<row> ----
            add(f"{ROOT}.1.1.5.12.1.12.1.1", str(row), row)               # row counter (decoy)
            add(f"{ROOT}.1.1.5.12.1.12.1.2", str(row), pon)               # PON
            add(f"{ROOT}.1.1.5.12.1.12.1.3", str(row), onu)               # ONU id
            add(f"{ROOT}.1.1.5.12.1.12.1.4", str(row), random.randint(0, 62))     # temperature
            add(f"{ROOT}.1.1.5.12.1.12.1.6", str(row),
                f"00:d3:{pon:02x}:{onu % 256:02x}:8b:{row % 256:02x}")            # MAC text
            add(f"{ROOT}.1.1.5.12.1.12.1.13", str(row), random.randint(5, 3080))  # distance
            # ---- parallel inventory tables, column-major and indexed <pon>.<onu>.
            # These are the ones that used to win: their "ONU id" column looks like
            # a temperature and their distance-like columns look like power.
            add(f"{ROOT}.1.1.5.12.1.9.1.5", str(row),
                f"aa:bb:{pon:02x}:{onu % 256:02x}:cc:{row % 256:02x}")            # other MAC
            add(f"{ROOT}.1.1.5.12.1.25.1.1.{pon}", str(onu), pon)                 # PON number
            add(f"{ROOT}.1.1.5.12.1.25.1.2.{pon}", str(onu), onu)                 # ONU id
            add(f"{ROOT}.1.1.5.12.1.25.1.12.{pon}", str(onu), random.randint(0, 3080))
            add(f"{ROOT}.1.1.5.12.1.25.1.17.{pon}", str(onu), random.randint(0, 4892))
            row += 1

    # ---- a second, smaller inventory: the decoy that fooled the first version.
    # It really does have one row per entry, so its "temperature" column is
    # nothing but a 1..367 counter.
    for index in range(367):
        add(f"{ROOT}.1.1.5.10.3.2.1.1", str(index), index + 1)
    return columns


ifindexes = set(range(1, 412))
onu_names = {
    f"EPON{pon:02d}ONU{onu}"
    for pon, count in enumerate(PER_PON, 1)
    for onu in ONU_IDS[pon]
}

# ---------------------------------------------------------------------------
print("== _merge_per_pon (tables published one PON at a time, with holes) ==")
columns = build_columns()
print(f"  raw pseudo-columns: {len(columns)}")
columns = D._merge_per_pon(columns, set(range(1, len(PER_PON) + 1)))
print(f"  after merge:        {len(columns)}")

rx_head = f"{ROOT}.1.1.5.12.2.1.8.1.7"
tx_head = f"{ROOT}.1.1.5.12.2.1.8.1.6"
check("rx table merged back into one column", rx_head in columns)
check("tx table merged back into one column", tx_head in columns)
check("rx index is now <pon>.<onu>",
      rx_head in columns and all(i.count(".") == 1 for i in columns[rx_head]["indexes"]))
check("the inventory table was NOT merged away",
      f"{ROOT}.1.1.5.12.1.12.1.4" in columns and f"{ROOT}.1.1.5.12.1.12.1.13" in columns)
check("the parallel table merged too (it really is per-PON)",
      f"{ROOT}.1.1.5.12.1.25.1.2" in columns)

# ---------------------------------------------------------------------------
print()
print("== analyse_columns ==")
chosen = D.analyse_columns(columns, ifindexes, onu_names)
chosen.pop("_shapes", None)
chosen.pop("_stats", None)
for field in ("rx_power", "tx_power", "olt_rx", "temperature", "distance", "mac"):
    print(f"  {field:<11} = {(chosen.get(field) or {}).get('oid')}")
print(f"  index_map   = {chosen.get('index_map')}")

check("rx_power = ...5.12.2.1.8.1.7", (chosen.get("rx_power") or {}).get("oid") == rx_head)
check("tx_power = ...5.12.2.1.8.1.6", (chosen.get("tx_power") or {}).get("oid") == tx_head)
check("temperature = ...5.12.1.12.1.4",
      (chosen.get("temperature") or {}).get("oid") == f"{ROOT}.1.1.5.12.1.12.1.4")
check("distance = ...5.12.1.12.1.13",
      (chosen.get("distance") or {}).get("oid") == f"{ROOT}.1.1.5.12.1.12.1.13")
check("mac = ...5.12.1.12.1.6",
      (chosen.get("mac") or {}).get("oid") == f"{ROOT}.1.1.5.12.1.12.1.6")
check("index_map.pon_oid = ...5.12.1.12.1.2",
      (chosen.get("index_map") or {}).get("pon_oid") == f"{ROOT}.1.1.5.12.1.12.1.2")
check("index_map.onu_oid = ...5.12.1.12.1.3",
      (chosen.get("index_map") or {}).get("onu_oid") == f"{ROOT}.1.1.5.12.1.12.1.3")
check("the row-counter decoy was rejected",
      (chosen.get("rx_power") or {}).get("oid") != f"{ROOT}.1.1.5.12.1.12.1.1"
      and (chosen.get("temperature") or {}).get("oid") != f"{ROOT}.1.1.5.10.3.2.1.1")

# ---------------------------------------------------------------------------
print()
print("== the OLT that never answered the interface list ==")
blind = D.analyse_columns(columns, set(), set())
blind.pop("_shapes", None)
blind.pop("_stats", None)
check("still finds rx/tx without an interface list",
      (blind.get("rx_power") or {}).get("oid") == rx_head
      and (blind.get("tx_power") or {}).get("oid") == tx_head)

# ---------------------------------------------------------------------------
print()
print("== collector optical parsing (live config values) ==")

FAKE_ROWS: list[tuple[str, object]] = []
for head, info in build_columns().items():
    for index, value in zip(info["indexes"], info["values"]):
        FAKE_ROWS.append((f"{head}.{index}", value))


class FakeClient:
    """Mimics SnmpClient.walk_map: returns suffix -> value for a subtree."""

    def walk_map(self, oid):
        base = oid.rstrip(".") + "."
        return {
            full[len(base):]: value
            for full, value in FAKE_ROWS
            if full.startswith(base)
        }


device = {
    "name": "OLT-1",
    "host": "10.0.0.1",
    "community": "x",
    "onu_tables": {
        "key_oid": None,
        "index_map": {
            "pon_oid": f"{ROOT}.1.1.5.12.1.12.1.2",
            "onu_oid": f"{ROOT}.1.1.5.12.1.12.1.3",
        },
        "rx_power": {"oid": f"{ROOT}.1.1.5.12.2.1.8.1.7", "scale": 1.0},
        "tx_power": {"oid": f"{ROOT}.1.1.5.12.2.1.8.1.6", "scale": 1.0},
        "olt_rx": {"oid": None, "scale": 1.0},
        "temperature": {"oid": f"{ROOT}.1.1.5.12.1.12.1.4", "scale": 1.0},
        "distance": {"oid": f"{ROOT}.1.1.5.12.1.12.1.13", "scale": 1.0},
        "mac": {"oid": f"{ROOT}.1.1.5.12.1.12.1.6", "scale": 1.0},
    },
}

onus = []
ifindex = 26
for pon, count in enumerate(PER_PON, start=1):
    for onu in ONU_IDS[pon]:
        onus.append({
            "ifindex": ifindex,
            "if_name": f"EPON{pon:02d}ONU{onu}",
            "pon": pon,
            "onu_id": onu,
        })
        ifindex += 1

collector = OltCollector({"olt": {}}, device)
warning = collector._apply_optical(FakeClient(), onus)
print(f"  warning:    {warning}")
print(f"  rx filled:  {sum(1 for o in onus if o.get('rx_power_dbm') is not None)}/{len(onus)}")
print(f"  first row:  {onus[0]}")

check("no warning returned", warning is None)
check("every ONU got an rx reading",
      sum(1 for o in onus if o.get("rx_power_dbm") is not None) == len(onus))
check("rx parsed out of the dBm text", onus[0]["rx_power_dbm"] == -8.0)
check("tx parsed out of the dBm text", onus[0]["tx_power_dbm"] == 1.0)
check("temperature came from the row-indexed table",
      isinstance(onus[0].get("temperature_c"), (int, float)))
check("distance came from the row-indexed table",
      isinstance(onus[0].get("distance_m"), (int, float)))
check("mac came from the row-indexed table",
      isinstance(onus[0].get("mac"), str) and onus[0]["mac"].count(":") == 5)

print()
print("== interface classification (the V1600D pads names with a trailing space) ==")

classifier = OltCollector({}, device)
check("'GE0/1 '      -> uplink", classifier.classify("GE0/1 ") == ("uplink", None, None))
check("'GE0/1'       -> uplink", classifier.classify("GE0/1") == ("uplink", None, None))
check("'EPON0/7 '    -> pon", classifier.classify("EPON0/7 ") == ("pon", None, None))
check("'EPON01ONU8 ' -> onu 1/8", classifier.classify("EPON01ONU8 ") == ("onu", 1, 8))
check("'vlan100 '    -> vlan", classifier.classify("vlan100 ") == ("vlan", None, None))
check("''            -> other", classifier.classify("") == ("other", None, None))
# VSOL / ECOM append a description after the real port name.  These used to
# classify as "other", which made the PONs page synthesise fake EPON ports on
# an all-GPON chassis.
check("'GPON0/1 Area-A,...' -> GPON PON",
      classifier.classify("GPON0/1 Area-A,Road-1,Market") == ("pon", None, None))
check("'GPON0/3 B.Nath,...'   -> GPON PON",
      classifier.classify("GPON0/3 B.Nath,R-colony") == ("pon", None, None))
check("'GPON01ONU61 GPON0/1:61_7141' -> ONU 1/61",
      classifier.classify("GPON01ONU61 GPON0/1:61_7141") == ("onu", 1, 61))
check("'GE0/3 Uplink'        -> uplink",
      classifier.classify("GE0/3 Uplink") == ("uplink", None, None))

print()
print("== AVIES / HSGQ AV-OLT-XE08 naming + optical table ==")
from olt_monitor import config as config_mod  # noqa: E402
from olt_monitor import vendors as vendors_mod  # noqa: E402

avies = OltCollector({}, {"name": "AVIES", "host": "0.0.0.0", "brand": "auto", "pon_type": ""})
check("'ONU01/01' -> ONU 1/1", avies.classify("ONU01/01") == ("onu", 1, 1))
check("'ONU08/64' -> ONU 8/64", avies.classify("ONU08/64") == ("onu", 8, 64))
check("'PON01: Area-A' -> PON", avies.classify("PON01: Area-A") == ("pon", None, None))
check("'PON02:Area-B'  -> PON", avies.classify("PON02:Area-B") == ("pon", None, None))
check("'GE01/Uplink'        -> uplink", avies.classify("GE01/Uplink") == ("uplink", None, None))
check("'XGE01'              -> uplink", avies.classify("XGE01") == ("uplink", None, None))
check("PON number ignores the description",
      vendors_mod.pon_number("PON02:Area-B") == 2)
detected = vendors_mod.detect("AV-OLT-XE08-L3", ["PON01: x", "ONU01/01", "GE01/Uplink"])
check("'AV-OLT' sysDescr is detected as AVIES/EPON",
      detected["brand"] == "avies" and detected["pon_type"] == "epon")
profile = config_mod.ONU_TABLE_PROFILES[("avies", "epon")]
check("the AVIES profile maps RX/TX/bias/voltage/temperature",
      all(profile.get(key, {}).get("oid") for key in
          ("rx_power", "tx_power", "bias", "voltage", "temperature")))
check("the AVIES profile maps the ONU MAC and inventory name",
      profile.get("key_oid", "").endswith(".3.3.2.1.2")
      and profile["mac"]["oid"].endswith(".3.3.2.1.7"))
check("the HSGQ optical values use hundredths",
      profile["rx_power"]["scale"] == 0.01
      and profile["temperature"]["scale"] == 0.01)
check("a composite HSGQ optical index resolves to the inventory row",
      avies._resolve_target("16777473.0.0", {"16777473": 9001}, {9001: {}}, {}) == 9001)
check("a PON-level HSGQ pseudo row does not resolve",
      avies._resolve_target("16777472.65535.65535", {"16777473": 9001}, {9001: {}}, {}) is None)

print()
print("== device registry + live reload (throw-away database) ==")

import os  # noqa: E402
import tempfile  # noqa: E402

from olt_monitor import db as db_mod  # noqa: E402
from olt_monitor import registry  # noqa: E402
from olt_monitor.config import load_config  # noqa: E402
from olt_monitor.scheduler import Monitor  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="olt-selftest-"))

# ---- isolated MySQL database (never the live one) -------------------------
_live = load_config()
TEST_DB = os.environ.get("OLT_TEST_DB") or f"{_live.get('database', {}).get('name', 'olt_monitor')}_selftest"
test_db_cfg = {"database": {**(_live.get("database") or {}), "name": TEST_DB}}
db_mod.close()
db_mod.configure(test_db_cfg)
try:
    db_mod.init_db()
except Exception as exc:  # noqa: BLE001
    print(f"\n[!] cannot use the self-test database {TEST_DB!r}: {exc}", file=sys.stderr)
    print("    Run this once (it creates and grants the self-test DB):", file=sys.stderr)
    print("        .venv/bin/python tools/setup_mysql.py", file=sys.stderr)
    raise SystemExit(2) from exc

if not db_mod.describe().endswith("/" + TEST_DB):
    print(f"[!] refusing to run against {db_mod.describe()}", file=sys.stderr)
    raise SystemExit(2)

# start from an empty database every time
for _table in db_mod.TABLES:
    db_mod.execute(None, f"DELETE FROM `{_table}`")
print(f"[i] self-test database: {db_mod.describe()} (cleared)")

seed_cfg = {
    "database": {**test_db_cfg["database"], "path": str(tmp / "selftest.db")},
    "polling": {"olt_interval_seconds": 60, "router_interval_seconds": 30},
    "olt": {"name": "OLT-A", "host": "10.0.0.1", "community": "public"},
    "router": {"name": "RTR-A", "host": "10.0.0.2", "api_port": 1122, "password": "pw"},
}
db_file = seed_cfg["database"]["path"]

inserted = registry.ensure_seeded(db_file, seed_cfg)
check("config.yaml seeds the registry once", inserted == 2)
check("a second call seeds nothing", registry.ensure_seeded(db_file, seed_cfg) == 0)

olts, routers = registry.device_lists(db_file, seed_cfg)
check("seeded OLT keeps its name and community",
      [d["name"] for d in olts] == ["OLT-A"] and olts[0]["community"] == "public")
check("seeded router keeps its api_port", [d["name"] for d in routers] == ["RTR-A"])
check("seeded router keeps its password", routers[0].get("password") == "pw")
check("secrets are not exposed to the browser",
      "password" not in registry.form_view(routers[0])
      and registry.form_view(routers[0])["password_set"] is True)

created = registry.create_device(db_file, seed_cfg, "olt", {"name": "OLT-B", "host": "10.0.0.9"})
check("a new OLT is stored with defaults", created["snmp_port"] == 161 and created["name"] == "OLT-B")

try:
    registry.create_device(db_file, seed_cfg, "olt", {"name": "OLT-B", "host": "10.0.0.9"})
    duplicate_rejected = False
except registry.RegistryError:
    duplicate_rejected = True
check("a duplicate name is refused", duplicate_rejected)

try:
    registry.create_device(db_file, seed_cfg, "olt", {"name": "OLT-C", "host": "1.1.1.1", "snmp_port": "abc"})
    bad_value_rejected = False
except registry.RegistryError:
    bad_value_rejected = True
check("a non-numeric port is refused", bad_value_rejected)

registry.update_device(db_file, seed_cfg, created["id"], {"community": "Other", "interval_seconds": 300})
registry.update_device(db_file, seed_cfg, created["id"], {"snmp_port": 1161})
again = registry.get_device(db_file, created["id"])
check("an edit updates a field and keeps the rest",
      again["community"] == "Other" and again["snmp_port"] == 1161 and again["interval_seconds"] == 300)

# a secret left empty on edit must keep the stored value (routers have a password)
import json  # noqa: E402

from olt_monitor.db import execute as _db_execute  # noqa: E402
from olt_monitor.db import query as _db_query  # noqa: E402

rtr = registry.create_device(
    db_file, seed_cfg, "router",
    {"name": "RTR-P", "host": "10.0.0.11", "username": "u", "password": "secret1"},
)
registry.update_device(db_file, seed_cfg, rtr["id"], {"password": "", "username": "u2"})
kept = registry.get_device(db_file, rtr["id"])
check("an empty password on edit keeps the stored one", kept["password"] == "secret1")
check("the password is encrypted at rest",
      "secret1" not in json.dumps(_db_query(db_file, "SELECT config FROM devices")))
registry.delete_device(db_file, seed_cfg, rtr["id"])

registry.set_enabled(db_file, seed_cfg, created["id"], False)
check("toggle disables a device", registry.get_device(db_file, created["id"])["enabled"] is False)

# deleting a device must not leave its history/MAC cache behind for a future
# device that reuses the same name
_db_execute(
    db_file,
    "REPLACE INTO olt_mac_cache (olt_name, payload, updated_at) VALUES (?, ?, ?)",
    [created["name"], "{}", "2026-11-07T10:00:00+00:00"],
)
_db_execute(
    db_file,
    "INSERT INTO olt_onu_history (ts, olt_name, ifindex, if_name) VALUES (?, ?, ?, ?)",
    ["2026-11-07T10:00:00+00:00", created["name"], 1, "EPON01ONU1"],
)

# --- the poller picks changes up without a restart --------------------------
monitor = Monitor(seed_cfg)
monitor.init()
check("the poller builds one collector per registered OLT",
      [c.name for c in monitor.olts] == ["OLT-A", "OLT-B"])
check("a new OLT defaults to 600 s and an explicit per-device value wins",
      monitor._interval(monitor.olts[1], 60) == 300
      and monitor._interval(monitor.olts[0], 60) == 600)
check("a disabled device is not polled (no worker thread)",
      monitor.olts[1].enabled is False and monitor.olts[0].enabled is True)

registry.delete_device(db_file, seed_cfg, created["id"])
check("deleting an OLT also removes its history and MAC cache",
      not _db_query(db_file, "SELECT 1 FROM olt_onu_history WHERE olt_name = ?", [created["name"]])
      and not _db_query(db_file, "SELECT 1 FROM olt_mac_cache WHERE olt_name = ?", [created["name"]]))
monitor.reload()
check("reload() drops a deleted device without a restart",
      [c.name for c in monitor.olts] == ["OLT-A"] and not monitor._threads)

registry.create_device(db_file, seed_cfg, "router", {"name": "RTR-B", "host": "10.0.0.10", "username": "x"})
monitor.reload()
check("reload() adds a router without a restart",
      [c.name for c in monitor.routers] == ["RTR-A", "RTR-B"])
monitor.stop()

print()
print("== correlation survives a restart (an OLT that has not polled yet) ==")

from types import SimpleNamespace  # noqa: E402

from olt_monitor.db import execute as db_execute  # noqa: E402
from olt_monitor.db import query as db_query  # noqa: E402

registry.create_device(db_file, seed_cfg, "olt", {"name": "OLT-B", "host": "10.0.0.3"})
monitor2 = Monitor(seed_cfg)
monitor2.init()
check("both OLTs are known to the poller",
      [c.name for c in monitor2.olts] == ["OLT-A", "OLT-B"])

db_execute(
    db_file,
    "REPLACE INTO pppoe_user_current"
    " (router_name, name, onu_olt_name, onu_ifindex, onu_name, match_method, rx_power_dbm)"
    " VALUES ('RTR-A', 'cust1', 'OLT-B', 7, 'EPON01ONU7', 'fdb', -21.5)",
)

onu_a = {"olt_name": "OLT-A", "ifindex": 26, "if_name": "EPON01ONU1", "mac": None, "rx_power_dbm": -14.0}
user_1 = {"router_name": "RTR-A", "name": "cust1", "caller_id": None, "address": "10.0.0.5"}

# only OLT-A has answered so far - OLT-B is still mid-poll
monitor2._olt_snapshots = {"OLT-A": SimpleNamespace(name="OLT-A", onus=[onu_a], mac_map={})}
monitor2._router_snapshots = {"RTR-A": SimpleNamespace(name="RTR-A", users=[user_1], mac_by_ip={})}
monitor2.refresh()

row = db_query(db_file, "SELECT onu_olt_name, onu_ifindex, rx_power_dbm FROM pppoe_user_current WHERE name='cust1'")[0]
check("a user keeps their ONU while their OLT has not polled yet",
      row["onu_olt_name"] == "OLT-B" and row["onu_ifindex"] == 7)
check("and keeps their dBm", row["rx_power_dbm"] == -21.5)

# now OLT-B HAS polled, but this user is not in its forwarding table -> clear
monitor2._olt_snapshots["OLT-B"] = SimpleNamespace(name="OLT-B", onus=[onu_a], mac_map={})
monitor2.refresh()
row = db_query(db_file, "SELECT onu_ifindex FROM pppoe_user_current WHERE name='cust1'")[0]
check("once that OLT has polled, a stale link is dropped", row["onu_ifindex"] is None)
monitor2.stop()

print()
print("== a poll that finishes after a reload must not write stale rows ==")

import threading  # noqa: E402


class FakeOlt:
    name = "OLT-OLD"
    enabled = True
    host = "10.0.0.99"
    stats: dict = {}

    def collect(self):
        return SimpleNamespace(
            name=self.name, ok=True, error=None, info={}, ports=[], onus=[],
            mac_map={}, mac_entries=0, duration_ms=1,
        )


monitor3 = Monitor(seed_cfg)
monitor3.init()
stale = FakeOlt()

fired = threading.Event()
fired.set()
monitor3.poll_olt(stale, fired)
check("an in-flight poll is dropped once the list was reloaded",
      monitor3._still_current(stale, fired, "x") is False)

monitor3.poll_olt(stale, threading.Event())
check("a poll for a device that is no longer registered is dropped",
      monitor3._still_current(stale, threading.Event(), "x") is False)
check("nothing was stored for the discarded device",
      not db_query(db_file, "SELECT 1 FROM device_current WHERE `key`='olt:OLT-OLD'")
      and not db_query(db_file, "SELECT 1 FROM olt_onu_current WHERE olt_name='OLT-OLD'"))

served = FakeOlt()
served.name = "OLT-A"
monitor3.poll_olt(served, threading.Event())
check("a poll for a registered device is stored",
      bool(db_query(db_file, "SELECT 1 FROM device_current WHERE `key`='olt:OLT-A'")))

# a later poll fails (wrong community / device offline): the last good payload
# must stay available and the UI must be told the data is stale
db_execute(
    db_file,
    "UPDATE device_current SET ok=1, error=NULL, payload=?, updated_at=?"
    " WHERE `key`='olt:OLT-A'",
    [
        json.dumps({
            "brand": "vsol",
            "brand_label": "VSOL",
            "last_good_at": "2026-11-07T10:00:00+00:00",
        }),
        "2026-11-07T10:00:00+00:00",
    ],
)
monitor3.status["olts"]["OLT-A"] = {
    "ports": 5, "onus": 25, "mac_entries": 12,
    "last_good": "2026-11-07T10:00:00+00:00",
}


class FailingOlt(FakeOlt):
    def collect(self):
        return SimpleNamespace(
            name="OLT-A", ok=False, error="no interface table returned",
            info={}, ports=[], onus=[], mac_map={}, health={},
        )


monitor3.store_olt(FailingOlt(), FailingOlt().collect())
failed_row = db_query(
    db_file,
    "SELECT payload, ok, error FROM device_current WHERE `key`='olt:OLT-A'",
)[0]
failed_payload = json.loads(failed_row["payload"])
failed_status = monitor3.status["olts"]["OLT-A"]
check("a failed poll keeps the last good payload instead of blanking it",
      failed_row["ok"] == 0
      and failed_row["error"] == "no interface table returned"
      and failed_payload.get("brand") == "vsol"
      and failed_payload.get("stale") is True
      and failed_payload.get("poll_error") == "no interface table returned")
check("a failed poll keeps the last known counts and last-good time",
      failed_status["onus"] == 25 and failed_status["ports"] == 5
      and failed_status["stale"] is True
      and failed_status["last_good"] == "2026-11-07T10:00:00+00:00")

monitor3.stop()

print()
print("== secrets at rest (ChaCha20 + HMAC envelope) ==")

from olt_monitor import secrets as secrets_mod  # noqa: E402

sealed = secrets_mod.seal("public", db_file)
check("a secret is sealed with the enc:v1: prefix",
      sealed.startswith("enc:v1:") and "public" not in sealed)
check("a sealed secret round-trips", secrets_mod.reveal(sealed, db_file) == "public")
check("plain text passes through (legacy config.yaml)", secrets_mod.reveal("public", db_file) == "public")
check("an empty value stays empty", secrets_mod.reveal("", db_file) == "")
check("a value sealed with another key does not open",
      secrets_mod.reveal(sealed, Path(tempfile.mkdtemp(prefix="olt-selftest-key-")) / "other.db") == "")
check("sealing twice produces different ciphertext", secrets_mod.seal("public", db_file) != sealed)

print()
print("== vendor detection and PON architecture ==")

from olt_monitor import vendors as vendors_mod  # noqa: E402

check("VSOL is recognised from sysDescr",
      vendors_mod.detect("VSOL V1600D EPON OLT", [])["brand"] == "vsol")
check("BDCOM GPON is recognised",
      vendors_mod.detect("BDCOM GPON OLT", [])["pon_type"] == "gpon")
check("an interface list can carry the answer",
      vendors_mod.detect("unknown device", ["GPON0/1", "GPON0/1:1"])["pon_type"] == "gpon")
manual = vendors_mod.detect("whatever", [], "btpon")
check("a manual brand always wins",
      manual["brand"] == "btpon" and manual["pon_type"] == "epon" and manual["reason"] == "selected manually")
check("PHOTON is recognised from its EPON-OLT sysDescr",
      vendors_mod.detect(
          "Internetwork Operating System Software EPON-OLT Series Software, 1.1.3",
          ["pon1", "pon2", "ge1", "ge2"],
      )["brand"] == "photon")
check("flat pon1/ge1 names identify the BDPON/BTPON/PHOTON family",
      vendors_mod.detect("unknown", ["pon1", "pon2", "ge1"])["brand"] == "photon")
check("the BDPON/BTPON/PHOTON Telnet profile reads CPU, RAM and per-PON DDM",
      vendors_mod.PROFILES["bdpon"]["telnet_profile"] == "epon_ponport"
      and vendors_mod.PROFILES["btpon"]["telnet_profile"] == "epon_ponport"
      and vendors_mod.PROFILES["photon"]["telnet_profile"] == "epon_ponport"
      and vendors_mod.telnet_profile("epon_ponport")["commands"]["ports"]["cmd"]
      == "show pon-port ddm-info pon{pon}")
check("the VSOL profile is flagged as verified", vendors_mod.profile("vsol")["verified"] is True)
check("HUAWEI is recognised from sysDescr",
      vendors_mod.detect("Huawei Technologies MA5800 GPON", [])["brand"] == "huawei")
check("TBS and HSGQ are first-class brands",
      vendors_mod.detect("TBS EPON OLT", [])["brand"] == "tbs"
      and vendors_mod.detect("HSGQ OLT", [])["brand"] == "hsgq")
check("the generic GPON profile has no SOHO label",
      vendors_mod.detect("unknown GPON OLT", [])["brand"] == "gpon"
      and "SOHO" not in vendors_mod.brand_label("gpon"))
check("the legacy gpon_soho key resolves to generic GPON",
      vendors_mod.profile("gpon_soho")["pon_type"] == "gpon"
      and vendors_mod.brand_label("gpon_soho") == "Generic GPON")
# "Generic GPON" is a placeholder, not a real vendor: the collector must still
# recognise the hardware and pick that vendor's ONU/optical tables.  Choyani_DBC_GPON
# (sysDescr V1600G1B) was saved as Generic GPON and therefore walked the BDCOM
# OIDs, leaving every ONU without dBm / serial / user.
_generic_vsol = vendors_mod.detect(
    "V1600G1B", ["GPON01ONU1", "GPON0/1"], "gpon", "gpon",
    "1.3.6.1.4.1.37950.1.1.5.10.14.1")
from olt_monitor.config import ONU_TABLE_PROFILES as _ONU_TABLE_PROFILES  # noqa: E402

check("a Generic GPON device is refined to its real vendor (VSOL)",
      _generic_vsol["brand"] == "vsol" and _generic_vsol["pon_type"] == "gpon")
check("the refined Generic GPON uses the VSOL optical OID tree",
      _ONU_TABLE_PROFILES[(_generic_vsol["brand"], _generic_vsol["pon_type"])]["rx_power"]["oid"]
      == "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7")
check("sysObjectID alone is enough to refine a white-label Generic GPON",
      vendors_mod.detect("V1600G1B", None, "gpon", "gpon",
                         "1.3.6.1.4.1.37950.1.1.5.10.14.1")["brand"] == "vsol")
check("a real manual brand is never overridden by detection",
      vendors_mod.detect("V1600G1B", ["GPON0/1"], "bdcom", "gpon",
                         "1.3.6.1.4.1.37950.1.1.5.10.14.1")["brand"] == "bdcom")
check("BDCOM hardware saved as Generic GPON refines to BDCOM",
      vendors_mod.detect("BDCOM(tm) GP3600-08B Software", ["GPON0/1", "GPON0/1:1"],
                         "gpon", "gpon", "1.3.6.1.4.1.3320.1.405.0")["brand"] == "bdcom")
_huawei = OltCollector({}, {"name": "HW", "host": "0.0.0.0", "brand": "huawei", "pon_type": "gpon"})
check("Huawei gpon-onu_1/2/3:4 is classified as an ONU",
      _huawei.classify("gpon-onu_1/2/3:4")[0] == "onu")
check("an unknown brand falls back to generic", vendors_mod.profile("nope")["pon_type"] == "epon")
check("the collector classifies GPON names too",
      OltCollector({}, {"name": "G", "host": "0.0.0.0", "brand": "cdata"}).classify("GPON0/1:5") == ("onu", 1, 5))
check("and GPON ports", OltCollector({}, {"name": "G", "host": "0.0.0.0", "brand": "cdata"}).classify("GPON0/3") == ("pon", None, None))

from olt_monitor.collectors.olt import _plausible_power  # noqa: E402
from olt_monitor.config import ONU_TABLE_PROFILES, PON_OPTICAL_PROFILES  # noqa: E402

bdcom_epon = ONU_TABLE_PROFILES[("bdcom", "epon")]
check("a BDCOM EPON needs no manual OID block for ONU RX/TX",
      bdcom_epon["rx_power"]["oid"].endswith(".3320.101.10.5.1.5")
      and bdcom_epon["tx_power"]["oid"].endswith(".3320.101.10.5.1.6"))
check("a BDCOM EPON gets vendor/model/MAC from the built-in profile",
      all(bdcom_epon[k].get("oid") for k in ("vendor", "model", "mac")))
check("a BDCOM EPON gets PON SFP TX and temperature",
      {"sfp_tx_power_dbm", "sfp_temperature_c"} <= set(PON_OPTICAL_PROFILES[("bdcom", "epon")]))
check("an INT32_MIN optical sentinel is rejected, a real dBm is kept",
      not _plausible_power(-214748364.8) and _plausible_power(-14.6))

print()
print("== Telnet safety (read-only guard) ==")

from olt_monitor.telnet import client as telnet_client  # noqa: E402

for good in ("show cpu", "show epon inactive-onu", "display version", "show interface gigaethernet0/7"):
    check(f"allowed: {good!r}", telnet_client.is_read_only(good) is True)
for bad in ("reboot", "write memory", "show run | include x", "show cpu; reboot",
            "clear counters", "no vlan 100", "show cpu && save", ""):
    check(f"refused: {bad!r}", telnet_client.is_read_only(bad) is False)

print()
print("== Telnet CLI parsers (real command output shapes) ==")

from olt_monitor.telnet import parsers as tp  # noqa: E402

check("CPU from 'show cpu'", tp.parse_cpu(
    "CPU utilization for five seconds: 12%/0%; one minute: 9%; five minutes: 8%") == 12.0)
mem = tp.parse_memory("Total memory: 256 MB\nUsed memory: 110 MB\nFree memory: 146 MB")
check("memory from 'show memory'",
      mem.get("used_percent") == 42.96875 or round(mem.get("used_percent") or 0) == 43)
check("board temperature", tp.parse_temperature("Board temperature: 42 C") == 42.0)

ports = tp.parse_port_optics(
    "Port       Temp(C)  Voltage(V)  Bias(mA)  TXPower(dBm)  RXPower(dBm)\n"
    "EPON0/1    42       3.29        12.4      -3.12         -18.32\n"
    "EPON0/2    41       3.30        12.1      -3.20         -19.10\n"
)
check("per-port SFP optics (2 rows, PON numbers read from the name)",
      len(ports) == 2 and ports[0]["pon"] == 1 and ports[0]["rx_power_dbm"] == -18.32)

# BDPON / BTPON / PHOTON: "show cpu", "show memory" and a key/value DDM block
check("flat CPU output ('cpu0: 7%')",
      tp.parse_cpu("cpu0: 7%") == 7.0)
linux_mem = tp.parse_memory(
    "MemTotal:         223528 kB\n"
    "MemFree:          141356 kB\n"
    "Buffers:               0 kB\n"
    "Memory usage: 22.15%\n"
)
check("Linux-style 'MemTotal/MemFree kB' memory output",
      linux_mem["used_percent"] == 22.15 and linux_mem["free_mb"] == 138.0
      and linux_mem["total_mb"] == 218.3)
photon_ddm = tp.parse_port_optics(
    " Vendor                                    : OEM            \n"
    " Product name                              : EPON-OLT-PX20++\n"
    " Laser bias current                        : 13.600 mA\n"
    " Laser output power                        : 8.1780 mW / 9.13 dBm\n"
    " Module temperature                        : 26.35 degrees C / 79.43 degrees F\n"
    " Module voltage                            : 3.2924 V\n",
    pon=1,
)
check("PHOTON per-PON DDM key/value block (dBm chosen over mW)",
      len(photon_ddm) == 1 and photon_ddm[0]["pon"] == 1
      and photon_ddm[0]["tx_power_dbm"] == 9.13
      and photon_ddm[0]["temperature_c"] == 26.35
      and photon_ddm[0]["voltage_v"] == 3.2924
      and photon_ddm[0]["bias_current_ma"] == 13.6)
check("an absent PON module does not invent readings",
      tp.parse_port_optics("  Error: The optic module of port is absence!", pon=4) == [])

optics = tp.parse_onu_optics(
    "ONU-ID  Temp(C)  Voltage(V)  Bias(mA)  TxPower(dBm)  RxPower(dBm)  Distance(m)\n"
    "1       45       3.28        12.5      2.15          -18.32        1930\n"
    "2       44       3.30        11.9      2.10          -20.75        826\n"
)
check("per-ONU optics", optics[2]["rx_power_dbm"] == -20.75 and optics[1]["distance_m"] == 1930.0)

photon_info_text = (
    "PORT   ONT MAC            Control  Run     Config  Match    Desc\n"
    "       ID                 flag     state   state   state\n"
    "====== === ============== ======== ======= ======= ======== ======\n"
    "pon1   1   70A5.6A2E.FB4F active   online  success match    \n"
    "pon1   2   702E.2207.4463 active   offline initial initial  \n"
)
photon_info = tp.parse_onu_inventory(photon_info_text)
check("PHOTON 'show ont info' maps the split two-line header",
      photon_info[1]["state"] == "online" and photon_info[1]["admin_status"] == 1
      and photon_info[2]["state"] == "offline"
      and photon_info[1]["mac"] == "70:A5:6A:2E:FB:4F")

photon_optics = tp.parse_onu_optics(
    "ONT ONT              Voltage  Tx optical  Rx optical   Laser bias   Temperature\n"
    "ID  MAC              (V)      power(dBm)  power(dBm)   current(mA)  (C)\n"
    "=== ================ ======== ==========  ==========   ===========  ==========\n"
    "1   70A5.6A2E.FB4F  3.31      1.44        -15.35       11.80        30.84\n"
    "2   702E.2207.4463\n"
)
check("PHOTON ONT optical table (dBm, V, mA, C)",
      photon_optics[1]["rx_power_dbm"] == -15.35
      and photon_optics[1]["tx_power_dbm"] == 1.44
      and photon_optics[1]["voltage_v"] == 3.31
      and photon_optics[1]["bias_current_ma"] == 11.8
      and photon_optics[1]["temperature_c"] == 30.84
      and photon_optics[2]["rx_power_dbm"] is None)

photon_basic = tp.parse_photon_onu_basic(
    "ONT MAC            Type    Distance  Up          Extended\n"
    "ID                         (m)       Time        Model   \n"
    "=== ============== ======= ======== =========== ========\n"
    "1   70A5.6A2E.FB4F SFU     2631     00:14:51:13  110F\n"
    "2   702E.2207.4463 --\n"
)
check("PHOTON ONT basic-info (type, distance, model)",
      photon_basic[1]["onu_type"] == "SFU" and photon_basic[1]["distance_m"] == 2631.0
      and photon_basic[1]["model"] == "110F"
      and photon_basic[2]["state"] == "offline")

photon_version = tp.parse_photon_onu_version(
    "ONT MAC            Vendor   Model      Hardware  Software\n"
    "ID                 ID       ID         version   version\n"
    "=== ============== ======== ========== ======== ==========\n"
    "1   70A5.6A2E.FB4F CDT      0x31313046 F331.1A  V1.2.5\n"
)
check("PHOTON ONT version (vendor, decoded model, software)",
      photon_version[1]["vendor"] == "CDT" and photon_version[1]["model"] == "110F"
      and photon_version[1]["sw_version"] == "V1.2.5")

photon_merge = OltCollector({}, {
    "name": "P", "host": "0.0.0.0", "brand": "photon", "pon_type": "epon",
    "telnet_profile": "epon_ponport",
})
photon_rows: list[dict] = []
photon_merge._merge_telnet(
    photon_rows,
    [],
    {"onus": [{
        "pon": 1, "onu_id": 1, "mac": "70:A5:6A:2E:FB:4F", "state": "online",
        "onu_type": "SFU", "model": "110F", "vendor": "CDT", "sw_version": "V1.2.5",
        "distance_m": 2631.0, "rx_power_dbm": -15.35, "tx_power_dbm": 1.44,
        "voltage_v": 3.31, "bias_current_ma": 11.8, "temperature_c": 30.84,
    }]},
)
check("a CLI-only ONU gets a stable synthetic ifindex and a full row",
      len(photon_rows) == 1
      and photon_rows[0]["ifindex"] == 700004097
      and photon_rows[0]["if_name"] == "pon1:1"
      and photon_rows[0]["oper_status"] == 1
      and photon_rows[0]["onu_type"] == "SFU"
      and photon_rows[0]["vendor"] == "CDT"
      and photon_rows[0]["distance_m"] == 2631.0)

# BDCOM / VSOL EPON report a registered ONU as "auto-configured"; it must be
# counted as online (a Telnet-only OLT otherwise shows 0/N up).
bdcom_merge = OltCollector({}, {
    "name": "B", "host": "0.0.0.0", "brand": "bdcom", "pon_type": "epon",
    "telnet_profile": "epon_vsol",
})
bdcom_rows: list[dict] = []
bdcom_merge._merge_telnet(
    bdcom_rows,
    [],
    {"onus": [
        {"pon": 1, "onu_id": 1, "mac": "80:76:30:61:AE:C0",
         "state": "auto-configured"},
        {"pon": 1, "onu_id": 2, "mac": "F4:B8:C1:3A:B6:1F",
         "state": "deregistered"},
    ]},
)
check("BDCOM 'auto-configured' is an online ONU",
      bdcom_rows[0]["oper_status"] == 1
      and bdcom_rows[1]["oper_status"] == 2)

# ---- C-Data FD13xx (EPON) ------------------------------------------------
cdata_rows = tp.parse_cdata_inventory(
    "F/S P  ONT    MAC                  Control  Run     Config    Match     Last\n"
    "         ID                          flag     state   state     state     down-cause\n"
    "----------------------------------------------------------------------------\n"
    "  0/1 1  1      70:2E:22:01:96:5D    Active   Online  success   match     dying-gasp\n"
    "  0/1 2  7      00:D3:9E:71:53:8A    Active   Offline initial   match     LOS\n"
    "  0/2 4  12     B0:7C:10:15:23:EC    Active   Online  success   match     dying-gasp\n"
)
check("C-Data's global ONT table keeps the PON for every ONU",
      len(cdata_rows) == 3
      and cdata_rows[0]["pon"] == 11 and cdata_rows[0]["onu_id"] == 1
      and cdata_rows[1]["pon"] == 12 and cdata_rows[1]["onu_id"] == 7
      and cdata_rows[2]["pon"] == 24 and cdata_rows[2]["onu_id"] == 12)
check("C-Data ONU state and MAC are read",
      cdata_rows[0]["mac"] == "70:2E:22:01:96:5D"
      and cdata_rows[0]["state"] == "Online"
      and cdata_rows[1]["state"] == "Offline"
      and "LOS" in (cdata_rows[1].get("last_down_reason") or ""))
check("C-Data has a dedicated Telnet profile",
      vendors_mod.telnet_profile_for("cdata", "epon", "epon_vsol") == "cdata_epon"
      and vendors_mod.telnet_profile("cdata_epon")["commands"]["macs"]["cmd"] == "show mac all"
      and vendors_mod.telnet_profile("cdata_epon")["commands"]["onu_table"]["cmd"]
      == "show ont info all")

cdata_merge = OltCollector({}, {
    "name": "C", "host": "0.0.0.0", "brand": "cdata", "pon_type": "epon",
    "telnet_profile": "cdata_epon",
})
cdata_onus = [{
    "olt_name": "C", "ifindex": 700004145, "if_name": "epon 0/1/4:1",
    "pon": 14, "onu_id": 1, "oper_status": 1, "mac": "70:2E:22:01:96:5D",
}]
cdata_merge._merge_telnet(
    cdata_onus, [],
    {"macs": {"08:40:F3:EF:0F:57": "epon0/1/4"},
     "mac_vlans": {"08:40:F3:EF:0F:57": {"vlan": 1365}}},
)
check("C-Data's 'show mac all' port becomes a PON-level subscriber link",
      cdata_merge._mac_pons.get("08:40:F3:EF:0F:57") == 14
      and cdata_merge._mac_vlans.get("08:40:F3:EF:0F:57") == 1365)

inactive = tp.parse_inactive_onus(
    "ONU-ID   Last Down Cause   Last Down Time\n"
    "1        LOS               2024-01-01 12:00:00\n"
    "2        Power Off         2024-01-01 13:00:00\n"
)
check("inactive ONUs keep their reason",
      "LOS" in (inactive[0].get("reason") or "")
      and "power" in (inactive[1].get("reason") or "").lower())
check("a down reason is normalised to a friendly label",
      tp.normalise_down_reason("Dying Gasp") is not None
      and tp.normalise_down_reason("Power Off") != tp.normalise_down_reason("LOS"))

# ---- BDCOM EPON (P3608B 10.1.0F): real output shapes --------------------
check("BDCOM temperature uses the current reading, not the 75C threshold",
      tp.parse_temperature(
          " upper temperature threshold is 75 degree\n"
          " low temperature threshold is -15 degree\n"
          " current temperature is 50 degree\n Status:Normal") == 50.0)

bdcom_opt = tp.parse_onu_optics(
    "IntfName     Temp(degree) Volt(V) Bias(mA) TxPow(dBm) RxPow(dBm)\n"
    "------------ ------------ ------- -------- ---------- ----------\n"
    "epon0/1:1    49.4         3.4     14.2     2.4        -21.8\n"
    "epon0/1:3    --           --      --       --         --\n"
    "          epon0/1:22   41.9         3.3     17.3     2.3        -21.7\n"
)
check("BDCOM optical table takes the ONU id from epon0/1:22",
      bdcom_opt.get(22, {}).get("rx_power_dbm") == -21.7
      and 3 not in bdcom_opt and bdcom_opt[1]["voltage_v"] == 3.4)

bdcom_info = tp.parse_onu_inventory(
    "IntfName         VendorID ModelID    MAC Address    Description\n"
    "    BindType Status           Dereg Reason\n"
    "EPON0/3:1        GPTF     PTF2       0018.93a4.4ab0 N/A\n"
    "    static   deregistered     power-off\n"
)
check("BDCOM onu-information survives its split two-line rows",
      bdcom_info.get(1, {}).get("mac") == "00:18:93:A4:4A:B0"
      and bdcom_info[1]["state"] == "deregistered" and bdcom_info[1]["reason"])

bdcom_down = tp.parse_inactive_onus(
    "Interface EPON0/3 has bound 2 inactive ONUs:\n"
    "IntfName         MAC Address    Status           LastRegTime         LastDeregTi\n"
    "me       LastDeregReason   Absenttime\n"
    "EPON0/3:23       a4dc.be95.1f0b deregistered     1970-06-29 02:34:35 1970-06-29\n"
    "15:56:36 power-off         0.07:50:18\n"
)
check("BDCOM inactive ONU keeps its PON, id and wrapped reason",
      bool(bdcom_down) and bdcom_down[0]["pon"] == 3 and bdcom_down[0]["onu_id"] == 23
      and "Power" in (bdcom_down[0].get("reason") or ""))

bdcom_macs = tp.parse_mac_table(
    "Vlan    Mac Address       Type       Ports\n"
    "200\t4c49.689d.feb3\t  DYNAMIC    tg0/1\n"
    "193\t0018.93a4.6ff9\t  DYNAMIC    epon0/1:10\n"
)
check("BDCOM MAC table maps a subscriber MAC to its ONU port",
      bdcom_macs.get("00:18:93:A4:6F:F9") == "epon0/1:10"
      and bdcom_macs.get("4C:49:68:9D:FE:B3") == "tg0/1")

bdcom_uplink = tp.parse_interface_details(
    "TGigaEthernet0/1 is up, line protocol is up\n"
    "DDM info:\n"
    "    TX power:-2.40 dBM, RX power:-3.69 dBM\n"
    "    SFP temperature:45.00 C,supply voltage :3.29V,Bias Current.:5.73mA\n"
    "  Real time input rate 7%, 737860765 bits/sec, 76678 packets/sec\n"
    "  Real time output rate 0%, 68462580 bits/sec, 27336 packets/sec\n"
)
check("BDCOM uplink SFP DDM and traffic are read",
      bool(bdcom_uplink) and bdcom_uplink[0]["port"] == "TGigaEthernet0/1"
      and bdcom_uplink[0]["rx_power_dbm"] == -3.69
      and bdcom_uplink[0]["temperature_c"] == 45.0
      and bdcom_uplink[0]["in_bps"] == 737860765.0)

check("BDCOM has its own verified Telnet profile",
      vendors_mod.telnet_profile("bdcom_epon")["label"] == "BDCOM EPON"
      and vendors_mod.PROFILES["bdcom"]["telnet_profile"] == "bdcom_epon")

detail = tp.parse_gpon_onu_detail(
    "GPON0/1:3\nModel: HG8245\nSN: 48575443ABCDEF\nState: up\nVLAN: 111\n"
    "Last down cause: LOS\nDistance: 1930\n"
)
check("GPON per-ONU detail (serial / model / VLAN / last down)",
      detail.get("serial") and detail.get("model") == "HG8245"
      and str(detail.get("vlan")) == "111" and detail.get("last_down_reason"))

print()
print("== Telnet session against a fake CLI (login + paging) ==")

fake = _FakeCli()
try:
    with telnet_client.TelnetSession("127.0.0.1", fake.port, "admin", "pw", timeout=5) as session:
        version = session.run("show version")
        active = session.run("show epon active-onu")
    check("login succeeds and the prompt/echo are stripped",
          "V1600D" in version and "#" not in version)
    check("a paged answer is completed by sending a space",
          "ONU-1" in active and "ONU-2" in active and "More" not in active)
    check("the fake server saw the configured username", fake.username == "admin")
except Exception as exc:
    check(f"the fake Telnet session works ({exc})", False)

# an account that logs in but has no privileges must not look like success
from olt_monitor.telnet import collector as telnet_collector  # noqa: E402

restricted = _FakeCli(restricted=True)
try:
    out = telnet_collector.collect(
        {
            "name": "R", "host": "127.0.0.1", "brand": "vsol", "pon_type": "epon",
            "telnet_enabled": True, "telnet_port": restricted.port,
            "telnet_username": "support", "telnet_password": "pw",
            "telnet_timeout_seconds": 5, "telnet_detail_max": 0,
        },
        pon_numbers=[1],
    )
    check("a restricted CLI account is reported as a failure", out["ok"] is False)
    check("and the reason names the missing privilege",
          "privileged" in (out["error"] or "").lower())
    check("and no health/optical data is invented",
          not out["health"] and not out["onus"] and not out["ports"])
except Exception as exc:
    check(f"the restricted-account path works ({exc})", False)

# BDPON / BTPON / PHOTON: cpu + memory + one DDM command per PON
photon_cli = _FakeCli(photon=True)
try:
    out = telnet_collector.collect(
        {
            "name": "P", "host": "127.0.0.1", "brand": "photon", "pon_type": "epon",
            "telnet_enabled": True, "telnet_port": photon_cli.port,
            "telnet_username": "admin", "telnet_password": "admin",
            "telnet_profile": "epon_ponport",
            "telnet_timeout_seconds": 5, "telnet_detail_max": 0,
        },
        pon_numbers=[1, 2],
    )
    check("per-PON DDM expansion runs for every requested PON",
          out["health"].get("cpu_percent") == 7.0
          and out["health"].get("memory_percent") == 22.15
          and len(out["ports"]) == 1 and out["ports"][0]["pon"] == 1
          and out["ports"][0]["tx_power_dbm"] == 9.13
          and out["commands"] == 14)
    check("PHOTON ONT inventory is built from the CLI tables",
          len(out["onus"]) == 2
          and out["onus"][0]["pon"] == 1 and out["onus"][0]["onu_id"] == 1
          and out["onus"][0]["mac"] == "70:A5:6A:2E:FB:4F"
          and out["onus"][0]["state"] == "online"
          and out["onus"][0]["onu_type"] == "SFU"
          and out["onus"][0]["distance_m"] == 2631.0
          and out["onus"][0]["model"] == "110F"
          and out["onus"][0]["vendor"] == "CDT"
          and out["onus"][0]["sw_version"] == "V1.2.5"
          and out["onus"][0]["rx_power_dbm"] == -15.35
          and out["onus"][0]["tx_power_dbm"] == 1.44
          and out["onus"][0]["voltage_v"] == 3.31
          and out["onus"][0]["bias_current_ma"] == 11.8
          and out["onus"][0]["temperature_c"] == 30.84)
    check("a down PHOTON ONT stays in the inventory with its MAC",
          out["onus"][1]["onu_id"] == 2 and out["onus"][1]["state"] == "offline"
          and out["onus"][1]["mac"] == "70:2E:22:07:44:63")
except Exception as exc:
    check(f"the per-PON DDM path works ({exc})", False)

# C-Data FD13xx: banner-only login, empty enable step, global ONT table
cdata_cli = _FakeCli(cdata=True)
try:
    out = telnet_collector.collect(
        {
            "name": "C", "host": "127.0.0.1", "brand": "cdata", "pon_type": "epon",
            "telnet_enabled": True, "telnet_port": cdata_cli.port,
            "telnet_username": "root", "telnet_password": "admin",
            "telnet_profile": "epon_vsol",
            "telnet_timeout_seconds": 5, "telnet_detail_max": 0,
        },
        pon_numbers=[1],
    )
    check("C-Data login nudges the silent banner and enters enable mode",
          out["ok"] and cdata_cli.username == "root")
    check("C-Data health comes from show cpu/memory/temperature",
          out["health"].get("cpu_percent") == 31.0
          and out["health"].get("memory_percent") == 13.0
          and out["health"].get("temperature_c") == 44.5)
    check("C-Data's global ONT table is used as the ONU inventory",
          len(out["onus"]) == 2
          and out["onus"][0]["pon"] == 11 and out["onus"][0]["onu_id"] == 1
          and out["onus"][1]["pon"] == 12 and out["onus"][1]["onu_id"] == 7)
    check("C-Data's forwarding table is read through 'show mac all'",
          out["macs"].get("08:40:F3:EF:0F:57") == "epon0/1/4")
except Exception as exc:
    check(f"the C-Data CLI path works ({exc})", False)

print()
print("== health score ==")

from olt_monitor import health as health_mod  # noqa: E402

check("an offline OLT scores 0", health_mod.score(online=False, error="timeout")[0] == 0)
perfect, _ = health_mod.score(online=True, has_metrics=False)
check("an online OLT with no metrics still scores well", perfect >= 90)
low, reasons = health_mod.score(
    online=True, cpu_percent=95, memory_percent=92, temperature_c=75,
    onus_up=200, onus_total=400, pons_up=6, pons_total=8,
)
check("heat / load / dead ports / dead ONUs all lower the score", low < 40 and len(reasons) >= 4)
check("the grade matches the score",
      health_mod.grade(95) == "success" and health_mod.grade(10) == "danger"
      and health_mod.label(75) == "Good")

print()
print("== registry: the new OLT fields ==")

olt_new = registry.create_device(
    db_file, seed_cfg, "olt",
    {
        "name": "OLT-N", "host": "10.0.0.20", "branch": "Sample Branch",
        "site": "POP-2", "rack": "A", "display_order": 3,
        "brand": "cdata", "pon_type": "gpon", "firmware": "1.0.1",
        "community": "SecretCommunity", "max_onus_per_pon": 128,
        "telnet_enabled": True, "telnet_username": "admin",
        "telnet_password": "telnetpw", "telnet_enable_password": "enablepw",
        "telnet_profile": "gpon_generic", "telnet_interval_seconds": 600,
    },
)
check("the branch / site / vendor survive a round-trip",
      olt_new["branch"] == "Sample Branch" and olt_new["brand"] == "cdata"
      and olt_new["pon_type"] == "gpon" and olt_new["max_onus_per_pon"] == 128)
check("display_order becomes the table position", olt_new["display_order"] == 3)
check("Telnet settings survive", olt_new["telnet_enabled"] is True
      and olt_new["telnet_username"] == "admin" and olt_new["telnet_password"] == "telnetpw"
      and olt_new["telnet_profile"] == "gpon_generic")
check("the Telnet profile drives the classifier",
      OltCollector({}, olt_new).classify("GPON0/2:9") == ("onu", 2, 9))
check("secrets are never handed to the browser",
      "community" not in registry.form_view(olt_new)
      and "telnet_password" not in registry.form_view(olt_new)
      and registry.form_view(olt_new)["community_set"] is True
      and registry.form_view(olt_new)["telnet_password_set"] is True)
stored = json.dumps(_db_query(db_file, "SELECT config FROM devices WHERE name='OLT-N'"))
check("community and Telnet passwords are encrypted at rest",
      "SecretCommunity" not in stored and "telnetpw" not in stored and "enablepw" not in stored)

for bad, label in ((("brand", "nokia"), "an unknown brand"), (("pon_type", "xgspon"), "an unknown PON type"),
                   (("telnet_profile", "nope"), "an unknown Telnet profile"), (("snmp_port", 99999), "an impossible port")):
    try:
        registry.update_device(db_file, seed_cfg, olt_new["id"], {bad[0]: bad[1]})
        ok = False
    except registry.RegistryError:
        ok = True
    check(f"{label} is refused", ok)

registry.update_device(db_file, seed_cfg, olt_new["id"], {"community": "", "telnet_password": ""})
check("an empty secret on edit keeps the stored one",
      registry.get_device(db_file, olt_new["id"])["community"] == "SecretCommunity")
check("an OLT list is sorted by display_order",
      [d["name"] for d in registry.list_devices(db_file, seed_cfg, "olt")][-1] == "OLT-N")
registry.delete_device(db_file, seed_cfg, olt_new["id"])

print()
print("== C-Data HTTP API (the ONU table SNMP never shows) ==")

import re  # noqa: E402

from olt_monitor import cdata as cdata_mod  # noqa: E402
from olt_monitor import vendors  # noqa: E402

# --- port numbering: EPON0/7 stays 7, epon 0/1/1 folds to 11 ---------------
check("a two-level PON name keeps its number", vendors.pon_number("EPON0/7") == 7)
check("a frame/slot/port name folds to a unique number",
      vendors.pon_number("epon 0/1/1") == 11 and vendors.pon_number("epon 0/2/1") == 21
      and vendors.pon_number("epon 0/2/4") == 24)
check("a descriptive port name still yields its PON number",
      vendors.pon_number("GPON0/1 Area-A,Road-1,Market") == 1
      and vendors.pon_number("GPON0/3 B.Nath,R-colony") == 3)
check("both PON name styles are classified as PON ports",
      any(re.match(p, "EPON0/7") for p in vendors.EPON_CLASSIFY["pon_regex"])
      and any(re.match(p, "epon 0/1/3") for p in vendors.EPON_CLASSIFY["pon_regex"]))

# --- the C-Data fingerprint wins over the generic "EPON" guess -------------
found = vendors.detect("SamplePublic", ["ge 0/0/1", "xge 0/0/4", "epon 0/1/1"])
check("C-Data is detected from its interface names",
      found["brand"] == "cdata" and found["pon_type"] == "epon")

# --- interface classification on that CLI ----------------------------------
cdata_olt = {"name": "C-Data", "host": "10.0.0.30", "brand": "cdata",
             "pon_type": "epon", "cdata_api_enabled": True,
             "cdata_api_username": "admin", "cdata_api_password": "admin"}
cdata_collector = OltCollector({}, cdata_olt)
check("an epon 0/1/3 port classifies as a PON port",
      cdata_collector.classify("epon 0/1/3") == ("pon", None, None))
check("an epon 0/1/3:7 interface classifies as ONU 7 of that PON",
      cdata_collector.classify("epon 0/1/3:7") == ("onu", 13, 7))
check("ge 0/0/1 classifies as an uplink",
      cdata_collector.classify("ge 0/0/1")[0] == "uplink")

# --- paging: the CGI cuts a reply at 40 KiB, so the page size must shrink ---
page_api = cdata_mod.CDataApi("127.0.0.1", "admin", "admin")
page_calls = {"n": 0}
FIXTURE = [
    {"PonId": "0/1/1", "OnuId": n, "Mac": f"00:11:22:33:44:{n:02X}",
     "RunningState": 1, "LastDownCause": 0, "ControlFlag": 1, "EquipId": "V601",
     "OnuVendor": "HWTC", "SwVersion": "V1"}
    for n in range(1, 121)
]
def _page_call(module, params=None, body=None, timeout=None, with_token=True):
    page_calls["n"] += 1
    if module != "onu_list_get":
        raise cdata_mod.CDataError(f"unexpected module {module}")
    size = int(params["PageSize"])
    if size > 100:  # what the device does on a big page
        raise cdata_mod.CDataError("onu_list_get: reply was truncated (40960 bytes)")
    start = (int(params["PageNumber"]) - 1) * size
    return {"PageTotal": len(FIXTURE), "list": FIXTURE[start:start + size]}
page_api._call = _page_call
paged = page_api.onus()
check("a truncated reply shrinks the page and still returns every ONU",
      len(paged) == len(FIXTURE) and page_calls["n"] > 2)

# --- one API ONU -> one olt_onu_current row --------------------------------
api_row = cdata_mod._onu_row(
    "C-Data",
    {"PonId": "0/1/1", "OnuId": 3, "Mac": "AA:BB:CC:DD:EE:03", "RunningState": 1,
     "LastDownCause": 1, "ControlFlag": 1, "EquipId": " D401 ", "OnuVendor": "VSOL",
     "SwVersion": "V6.0", "LastDownTime": 1760000000, "OnuName": "PON 0/1/1 ONU 3"},
    {"RxPower": -21.4, "TxPower": 2.1, "OltRxPower": -19.0, "Temperature": 44.5,
     "Voltage": 3.24, "BiasCurrent": 15.4, "Range": 1493},
    {"in_bps": 1000.0, "out_bps": 200.0},
)
check("the ONU row carries its port, id and a stable synthetic ifindex",
      api_row["pon"] == 11 and api_row["onu_id"] == 3
      and api_row["ifindex"] == cdata_mod.onu_ifindex(11, 3)
      and api_row["if_name"] == "epon 0/1/1:3")
check("optical power, distance and DDM values are mapped",
      api_row["rx_power_dbm"] == -21.4 and api_row["tx_power_dbm"] == 2.1
      and api_row["olt_rx_power_dbm"] == -19.0 and api_row["distance_m"] == 1493
      and api_row["voltage_v"] == 3.24 and api_row["bias_current_ma"] == 15.4)
check("the vendor / model / firmware of the ONU are kept",
      api_row["vendor"] == "VSOL" and api_row["model"] == "D401"
      and api_row["sw_version"] == "V6.0")
check("an online ONU does not claim a down cause",
      api_row["oper_status"] == 1 and api_row["last_down_reason"] is None)
check("traffic rates are attached", api_row["in_bps"] == 1000.0)

down_row = cdata_mod._onu_row(
    "C-Data",
    {"PonId": "0/2/4", "OnuId": 58, "RunningState": 0, "LastDownCause": 2,
     "ControlFlag": 1, "LastDownTime": 1760000000},
    None,
    None,
)
check("an offline ONU keeps its last-down cause and timestamp",
      down_row["oper_status"] == 2 and down_row["last_down_reason"] == "dying-gasp"
      and down_row["last_down_at"].startswith("2025-10-09")
      and down_row["rx_power_dbm"] is None and down_row["distance_m"] is None)

# --- collect(): device info, health and PON DDM ----------------------------
class _FakeApi:
    calls = 1
    duration_ms = 1
    def login(self, force=False): return "tok"
    def device_info(self):
        return {"SysName": "Sample_OLT_Name", "Model": "AC-FD1304E-B1-PDA0",
                "Firmware": "V3.4.57_260324", "Hardware": "V1.1", "SnNum": "AF2802",
                "DevType": "EPON OLT", "Uptime": 692340000}
    def device_health(self): return {"CpuUsage": 29.48, "RamUsage": 13.0, "Temp": 45.0}
    def port_status(self):
        return [{"Name": "epon 0/1/1", "Index": 17825793, "Status": 2},
                {"Name": "epon 0/2/1", "Index": 34603009, "Status": 2}]
    def onus(self): return FIXTURE[:4]
    def optical(self, pon_id): return [{"OnuId": 1, "RxPower": -10.78, "TxPower": 2.05,
                                       "Range": 1493, "Temperature": 44.2,
                                       "Voltage": 3.24, "BiasCurrent": 15.4}]
    def onu_rates(self, index): return {"1": {"in_bps": 11.0, "out_bps": 22.0}}
    def mac_table(self):
        return (
            {"74:FE:CE:04:E1:35": 700045057},
            {"74:FE:CE:04:E1:35": 1365},
        )
    def pon_summary(self, index): return {"Total": 4, "Register": 4, "Online": 4}
    def ddm(self, index): return {"Status": True, "TxPower": 7.79,
                                  "RxPower": -30.97, "Temp": 51.22}

collected = cdata_mod.collect(_FakeApi(), "C-Data")
check("collect() returns the whole inventory", collected.ok and len(collected.onus) == 4)
check("CPU / RAM / temperature / uptime come from the API",
      collected.health["cpu_percent"] == 29.48 and collected.health["memory_percent"] == 13.0
      and collected.health["temperature_c"] == 45.0
      and collected.health["uptime_seconds"] == 692340
      and collected.health["source"] == "cdata-api")
check("the model and firmware are reported",
      collected.info["model"] == "AC-FD1304E-B1-PDA0"
      and collected.info["firmware"] == "V3.4.57_260324")
check("each PON carries its OLT-side SFP DDM",
      collected.pons[0]["pon"] == 11 and collected.pons[0]["sfp_tx_power_dbm"] == 7.79
      and collected.pons[0]["sfp_rx_power_dbm"] == -30.97)
check("optical power and rates reach the ONU rows",
      collected.onus[0]["rx_power_dbm"] == -10.78 and collected.onus[0]["in_bps"] == 11.0)
check("the per-PON MAC table links caller-id to an ONU and its VLAN",
      collected.mac_map.get("74:FE:CE:04:E1:35") == 700045057
      and collected.mac_vlans.get("74:FE:CE:04:E1:35") == 1365)

# --- merging into an SNMP poll that produced no ONUs at all ----------------
port_row = {
    "olt_name": "C-Data", "ifindex": 17825793, "if_name": "epon 0/1/1",
    "if_descr": "epon 0/1/1", "kind": "pon", "pon_type": "epon", "max_onus": 64,
    "admin_status": 1, "oper_status": 1, "hc_in_octets": None, "hc_out_octets": None,
    "in_errors": 0, "out_errors": 0, "in_bps": None, "out_bps": None,
    "sfp_rx_power_dbm": None, "sfp_tx_power_dbm": None, "sfp_temperature_c": None,
}
merge_ports = [dict(port_row)]
merge_onus: list = []
cdata_collector._merge_api(merge_onus, merge_ports, collected)
check("an OLT whose SNMP has no ONU table still gets its ONUs",
      len(merge_onus) == 4 and merge_onus[0]["if_name"] == "epon 0/1/1:1")
check("the SNMP PON row is enriched with the SFP readings",
      merge_ports[0]["sfp_tx_power_dbm"] == 7.79)
check("a PON that SNMP missed is added with the same columns",
      len(merge_ports) == 2 and set(merge_ports[1]) == set(merge_ports[0])
      and merge_ports[1]["if_name"] == "epon 0/2/1")

snmp_onu = {"pon": 11, "onu_id": 1, "ifindex": 1234, "if_name": "EPON0/1:1",
            "oper_status": 2, "rx_power_dbm": None, "tx_power_dbm": None,
            "last_down_reason": "LOS", "model": None, "mac": None}
merge_onus_existing = [dict(snmp_onu)]
cdata_collector._merge_api(merge_onus_existing, [], collected)
first_onu = [r for r in merge_onus_existing
             if r.get("pon") == 11 and r.get("onu_id") == 1]
check("a matching SNMP ONU is updated, not duplicated",
      len(first_onu) == 1
      and first_onu[0]["ifindex"] == 1234
      and first_onu[0]["if_name"] == "EPON0/1:1"
      and first_onu[0]["oper_status"] == 1
      and first_onu[0]["rx_power_dbm"] == -10.78)

# --- a bad password must never hammer the OLT (it locks after two) ----------
lock_api = cdata_mod.CDataApi("127.0.0.1", "admin", "wrong")
attempts = {"n": 0}
def _bad_login(module, params=None, body=None, timeout=None, with_token=True):
    attempts["n"] += 1
    raise cdata_mod.CDataError("sys_login: The user name or password is invalid")
lock_api._call = _bad_login
for _ in range(4):
    try:
        lock_api.login()
    except cdata_mod.CDataError:
        pass
check("a failed login is retried only once (no account lock-out)", attempts["n"] == 1)

print()
print("== GPON ONU tables (BDCOM GP3600 / VSOL V1600G / C-Data) ==")
from olt_monitor.telnet import parsers  # noqa: E402

gpon_table = """
GP3600-08#show gpon onu-information interface GPON0/1
OnuIndex     Model      MAC                SN                State
GPON0/1:1    HG8245H    00:1A:2B:3C:4D:5E  48575443AABBCCDD  online
GPON0/1:2    F660       00:1A:2B:3C:4D:5F  48575443AABBCCDE  offline
"""
inventory = parsers.parse_onu_inventory(gpon_table)
check("a GPON table keeps both ONUs", sorted(inventory) == [1, 2])
check("the ONU model comes from the Model column", inventory[1].get("model") == "HG8245H")
check("the interface name sets the PON", inventory[1].get("pon") == 1)
check("the serial number is kept", inventory[1].get("serial") == "48575443AABBCCDD")

gpon_free = (
    "GPON0/2:7   ZTEG  F660  00:1a:2b:3c:4d:5f  "
    "ZTEGC0FFEE01  working  1240m"
)
free = parsers.parse_onu_inventory(gpon_free, default_pon=2)
check("a free-form GPON line is parsed", 7 in free)
check("its serial / state / distance are read",
      free[7].get("serial") == "ZTEGC0FFEE01"
      and free[7].get("state") == "working"
      and free[7].get("distance_m") == 1240.0)

gpon_inactive = """
ONU-ID  S/N               Last down cause   Last down time
1       48575443AABBCCDD  LOS               2026-10-20 10:11:12
"""
inactive = parsers.parse_inactive_onus(gpon_inactive, default_pon=4)
check("a per-PON GPON inactive table uses the caller's PON",
      inactive and inactive[0].get("pon") == 4)
check("the down cause is normalised",
      inactive and "LOS" in str(inactive[0].get("reason")))
check("the down time is kept",
      inactive and inactive[0].get("at") == "2026-10-20 10:11:12")

gpon_macs = """
PON   MAC-ADDRESS         VLAN
PON1  00:1a:2b:3c:4d:5e   100
"""
check("a GPON MAC table without a TYPE column is still read",
      parsers.parse_mac_table(gpon_macs) == {"00:1A:2B:3C:4D:5E": "PON1"})
check("its VLAN is kept",
      parsers.parse_mac_table_vlans(gpon_macs)["00:1A:2B:3C:4D:5E"]["vlan"] == 100)

print()
print("== device vendor lookup (bundled OUI database) ==")
from olt_monitor import oui  # noqa: E402

check("the bundled OUI database is loaded", len(oui._database()) > 40000)
check("a known OUI resolves to its company",
      "CISCO" in str(oui.lookup("00:00:0C:11:22:33")).upper())
check("an unknown OUI returns nothing", oui.lookup("02:00:00:00:00:01") is None)
check("an ONU's own vendor string is a fallback",
      oui.vendor_for("02:00:00:00:00:01", None, None, "Huawei HG8245H") == "HUAWEI")
check("the OUI wins over a generic model string",
      "HUAWEI" in str(oui.vendor_for("A4:DC:BE:00:00:01", None, None, "HG8245H")).upper())

print()
print("== PON-level correlation (Photon / BDPON / BTPON firmware) ==")
from olt_monitor.correlate import correlate  # noqa: E402

pon_onus = [
    {"olt_name": "PHOTON", "ifindex": 1, "if_name": "pon1:1", "pon": 1, "onu_id": 1,
     "mac": "AA:00:00:00:00:01"},
    {"olt_name": "PHOTON", "ifindex": 2, "if_name": "pon1:2", "pon": 1, "onu_id": 2,
     "mac": "AA:00:00:00:00:02"},
    {"olt_name": "PHOTON", "ifindex": 3, "if_name": "pon2:1", "pon": 2, "onu_id": 1,
     "mac": "AA:00:00:00:00:03"},
]
pon_users = [
    {"router_name": "R", "name": "u-shared", "caller_id": "00:11:22:33:44:55"},
    {"router_name": "R", "name": "u-single", "caller_id": "00:11:22:33:44:66"},
    {"router_name": "R", "name": "u-unknown", "caller_id": "00:11:22:33:44:77"},
]
pon_counts = correlate(
    {}, pon_onus, pon_users, {}, {}, {},
    {
        "00:11:22:33:44:55": ("PHOTON", 1),
        "00:11:22:33:44:66": ("PHOTON", 2),
        "00:11:22:33:44:77": ("PHOTON", 9),
    },
)
check("a MAC on a multi-ONU PON is linked at PON level",
      pon_users[0].get("match_method") == "pon"
      and pon_users[0].get("onu_pon") == 1
      and pon_users[0].get("onu_ifindex") is None)
check("a MAC on a single-ONU PON is promoted to that ONU",
      pon_users[1].get("match_method") == "pon"
      and pon_users[1].get("onu_ifindex") == 3
      and pon_users[1].get("onu_pon") == 2)
check("a MAC outside the FDB stays unmatched",
      pon_users[2].get("match_method") is None)
check("the PON hits are counted", pon_counts.get("pon") == 2)
check("correlation leaves the ONU name readable",
      pon_users[0].get("onu_name") == "pon1")

# A bridge FDB also learns every MAC on the uplink.  Those entries must not win
# over the OLT that really has the subscriber behind an ONU: OLT_5 had 166 exact
# ONU matches but only 3 linked users because an earlier snapshot's uplink
# entry claimed the MAC first.
from types import SimpleNamespace as _NS  # noqa: E402
from olt_monitor.correlate import build_fdb as _build_fdb  # noqa: E402

_uplink_only = _NS(name="UPSTREAM", mac_map={"00:AA:00:00:00:01": 9},
                   onus=[{"ifindex": 10}, {"ifindex": 11}])
_real_onu = _NS(name="OLT_5", mac_map={"00:AA:00:00:00:01": 45},
                onus=[{"ifindex": 45}, {"ifindex": 46}])
_missing = _NS(name="OLT_5", mac_map={"00:AA:00:00:00:02": 9},
               onus=[{"ifindex": 45}])
_fdb = _build_fdb([_uplink_only, _real_onu, _missing])
check("an uplink FDB entry cannot mask another OLT's real ONU match",
      _fdb.get("00:AA:00:00:00:01") == ("OLT_5", 45))
check("an FDB entry learned only on the uplink is ignored",
      "00:AA:00:00:00:02" not in _fdb)

# An exact ONU MAC must beat a stale / wrong forwarding-table entry.  GPON
# ONUs bridge the customer router, so the caller-id is the ONU MAC; the FDB
# bridge-port mapping is the less trustworthy source on that firmware.
prio_onus = [
    {"olt_name": "G", "ifindex": 10, "if_name": "GPON03ONU10", "pon": 3, "onu_id": 10,
     "mac": "88:BD:09:00:00:10"},
    {"olt_name": "G", "ifindex": 18, "if_name": "GPON03ONU18", "pon": 3, "onu_id": 18,
     "mac": "02:00:00:00:00:01"},
]
prio_users = [{"router_name": "R", "name": "sample-user", "caller_id": "02:00:00:00:00:01"}]
correlate({}, prio_onus, prio_users, {}, {"02:00:00:00:00:01": ("G", 10)}, {}, None)
check("an exact ONU MAC wins over an FDB bridge-port guess",
      prio_users[0].get("match_method") == "mac"
      and prio_users[0].get("onu_ifindex") == 18
      and prio_users[0].get("onu_pon") == 3)

print()
print("== ONU last-down time (every vendor) ==")
from olt_monitor.scheduler import mark_last_down as _mark_last_down  # noqa: E402

_stamp = "2026-11-09T00:00:00+00:00"
_last_down_rows = [
    {"ifindex": 1, "oper_status": 2},                      # up -> down
    {"ifindex": 2, "oper_status": 1},                      # still up
    {"ifindex": 3, "oper_status": 2,
     "last_down_at": "2026-11-01T00:00:00+00:00"},         # still down
    {"ifindex": 4, "oper_status": 2},                      # new ONU, down
]
_mark_last_down(_last_down_rows, {
    1: {"oper_status": 1},
    2: {"oper_status": 2, "last_down_at": "2026-11-01T00:00:00+00:00"},
    3: {"oper_status": 2, "last_down_at": "2026-11-01T00:00:00+00:00"},
}, _stamp)
check("an ONU that just went down gets a last-down timestamp",
      _last_down_rows[0].get("last_down_at") == _stamp)
check("an ONU that stayed down keeps its original timestamp",
      _last_down_rows[2].get("last_down_at") == "2026-11-01T00:00:00+00:00")
check("an ONU that is up is not stamped",
      "last_down_at" not in _last_down_rows[1])
check("a new ONU already down is stamped on its first poll",
      _last_down_rows[3].get("last_down_at") == _stamp)

print()
print("== per-account permissions ==")
from olt_monitor import auth  # noqa: E402

check("a super admin bypasses the flags",
      auth.has_permission({"role": "superadmin"}, "perm_developer"))
check("a manager without the flag cannot open settings",
      not auth.has_permission({"role": "manager", "perm_devices": 1}, "perm_settings"))
check("a viewer keeps the read-only SNMP tools",
      auth.has_permission({"role": "viewer", "perm_tools": 1}, "perm_tools"))
created = auth.create_user(
    None, "perm-selftest", "Password-123", "manager",
    permissions={"perm_devices": 1, "perm_settings": 1},
)
check("flags are stored when a user is created",
      created.get("perm_settings") is True and created.get("perm_users") is False)
updated = auth.update_user(None, int(created["id"]), perm_users=True, perm_settings=False)
check("flags can be switched on and off",
      updated.get("perm_users") is True and updated.get("perm_settings") is False)

print()
print("== quick search (one box for user / MAC / IP) ==")
from olt_monitor.web import app as webapp  # noqa: E402

db_mod.upsert(
    None, "pppoe_user_current", ["router_name", "name"],
    {"router_name": "RTR-A", "name": "search-target", "address": "10.9.8.7",
     "caller_id": "00:00:0C:AA:BB:CC", "onu_pon": None},
)
found = webapp.quick_search(None, "search-target")
check("the quick search finds a user by name",
      found["total"] >= 1 and found["users"][0]["name"] == "search-target")
check("the hit carries the router vendor",
      "CISCO" in str(found["users"][0].get("caller_vendor")).upper())
check("an IP search finds the same user",
      any(row["name"] == "search-target" for row in webapp.quick_search(None, "10.9.8.7")["users"]))
check("a MAC search finds the same user",
      any(row["name"] == "search-target" for row in webapp.quick_search(None, "0000.0caa.bbcc")["users"]))

# ---- PON-level links are visible on the ONU list (Photon / BDPON / BTPON) --
db_mod.upsert(
    None, "olt_onu_current", ["olt_name", "ifindex"],
    {"olt_name": "OLT-P", "ifindex": 700004097, "if_name": "pon1:1",
     "if_descr": "EPON ONU 1/1", "pon": 1, "onu_id": 1, "oper_status": 1,
     "mac": "70:A5:6A:2E:FB:4F"},
)
db_mod.upsert(
    None, "pppoe_user_current", ["router_name", "name"],
    {"router_name": "RTR-A", "name": "pon-level-target", "address": "10.9.8.8",
     "caller_id": "00:00:0C:AA:BB:CD", "onu_olt_name": "OLT-P", "onu_pon": 1,
     "onu_ifindex": None, "access_iface": "Rony_Cable_1380",
     "match_method": "pon"},
)
pon_rows = webapp.list_onus(None, olt="OLT-P")
check("the ONU list carries the PON-level user count and MikroTik iface",
      len(pon_rows) == 1
      and pon_rows[0].get("pon_user_count") == 1
      and pon_rows[0].get("pon_iface") == "Rony_Cable_1380"
      and pon_rows[0].get("pon_user_names") == ["pon-level-target"]
      and not pon_rows[0].get("linked_user"))
check("the 'has PPPoE' filter also matches PON-level links",
      len(webapp.list_onus(None, olt="OLT-P", status="pppoe")) == 1)

# ---- a synthetic PON row on a GPON OLT must not be labelled EPON ----------
db_mod.upsert(
    None, "olt_port_current", ["olt_name", "ifindex"],
    {"olt_name": "OLT-G", "ifindex": 9001, "if_name": "GPON0/5", "kind": "pon",
     "pon_type": "gpon", "max_onus": 128, "oper_status": 1},
)
db_mod.upsert(
    None, "olt_onu_current", ["olt_name", "ifindex"],
    {"olt_name": "OLT-G", "ifindex": 9002, "if_name": "GPON09ONU1", "pon": 9,
     "onu_id": 1, "oper_status": 1},
)
gpon_pons = {row["pon"]: row for row in webapp.pon_rows(None, {}, olt="OLT-G")}
check("a GPON OLT's synthetic PON keeps the GPON name and capacity",
      gpon_pons[9]["if_name"] == "GPON0/9"
      and gpon_pons[9]["pon_type"] == "gpon"
      and gpon_pons[9]["max_onus"] == 128)
check("real GPON ports are not relabelled EPON",
      gpon_pons[5]["if_name"] == "GPON0/5" and gpon_pons[5]["max_onus"] == 128)

over_capacity = webapp._pon_row(
    {"olt_name": "OLT-X", "if_name": "EPON0/1", "pon_type": "epon", "max_onus": 64},
    1, {"onus": 70, "up": 60},
)
check("a PON carrying more ONUs than the nominal split scales its capacity",
      over_capacity["max_onus"] == 128 and over_capacity["util_pct"] == 55)

# ---- config-only backup: export / import keeps setup, clears measurements --
print()
print("== config-only site backup ==")
from olt_monitor import site_settings, siteconfig  # noqa: E402

site_settings.set_many(db_file, {
    "site_name": "Selftest Site",
    "tagline": "config backup",
    "refresh_seconds": "45",
})
try:
    auth.create_user(db_file, "cfg-admin", "Password-123", "superadmin")
except ValueError:
    pass

db_mod.upsert(
    None, "olt_onu_current", ["olt_name", "ifindex"],
    {"olt_name": "OLT-A", "ifindex": 4242, "if_name": "EPON0/1:99",
     "rx_power_dbm": -21.5},
)
db_mod.upsert(
    None, "pppoe_user_current", ["router_name", "name"],
    {"router_name": "RTR-A", "name": "backup-cleared", "address": "10.9.9.9"},
)
db_mod.upsert(
    None, "olt_mac_cache", ["olt_name"],
    {"olt_name": "OLT-A", "payload": "{}", "updated_at": db_mod.utcnow_iso()},
)

device_count = len(registry.list_devices(db_file, seed_cfg))
user_count = len(auth.list_users(db_file))
payload = siteconfig.export_config(db_file)
serialised = json.dumps(payload)
check("the export marks monitoring data as excluded",
      payload["contains"]["onu_data"] is False
      and payload["contains"]["history"] is False
      and "rx_power_dbm" not in serialised)
check("the export keeps every device and web user",
      len(payload["devices"]) == device_count and len(payload["users"]) == user_count)
check("device secrets are portable plain text in the export",
      any(d["name"] == "OLT-A" and d["config"].get("community") == "public"
          for d in payload["devices"]))
check("the export carries the site settings",
      payload["site_settings"].get("site_name") == "Selftest Site"
      and payload["site_settings"].get("refresh_seconds") == "45")

# wipe everything the import should replace, then restore from JSON text
db_mod.execute(None, "DELETE FROM devices")
db_mod.execute(None, "DELETE FROM users")
site_settings.set_many(db_file, {"site_name": "Changed", "refresh_seconds": "30"})
info = siteconfig.import_config(db_file, json.loads(serialised))
check("import reports the restored device and user counts",
      info["devices"] == device_count and info["users"] == user_count)
check("import restores the devices",
      len(registry.list_devices(db_file, seed_cfg)) == device_count)
restored_olt = next(d for d in registry.list_devices(db_file, seed_cfg, "olt") if d["name"] == "OLT-A")
check("import re-encrypts device secrets with the local key",
      restored_olt.get("community") == "public")
stored = db_query(None, "SELECT config FROM devices WHERE name = 'OLT-A'")
check("the restored secret is sealed in MySQL",
      stored and "enc:v1:" in str(stored[0]["config"]))
check("import restores site settings",
      site_settings.get_all(db_file).get("site_name") == "Selftest Site"
      and site_settings.get_all(db_file).get("refresh_seconds") == "45")
restored_users = auth.list_users(db_file)
check("import restores roles and password hashes",
      any(u["username"] == "cfg-admin" and u["role"] == "superadmin" for u in restored_users)
      and auth.authenticate(db_file, "cfg-admin", "Password-123") is not None)
check("import clears collected ONU / PPPoE / forwarding data",
      not db_query(None, "SELECT 1 FROM olt_onu_current")
      and not db_query(None, "SELECT 1 FROM pppoe_user_current")
      and not db_query(None, "SELECT 1 FROM olt_mac_cache"))

# ---- 24-hour retention: old history is deleted, fresh history is kept ------
from datetime import datetime, timedelta, timezone  # noqa: E402

now = datetime.now(timezone.utc).replace(microsecond=0)
old_ts = (now - timedelta(hours=25)).isoformat()
new_ts = (now - timedelta(hours=1)).isoformat()
db_mod.execute(
    None,
    "INSERT INTO olt_onu_history (ts, olt_name, ifindex, if_name) VALUES (?, ?, ?, ?)",
    [old_ts, "OLT-R", 1, "old-onu"],
)
db_mod.execute(
    None,
    "INSERT INTO olt_onu_history (ts, olt_name, ifindex, if_name) VALUES (?, ?, ?, ?)",
    [new_ts, "OLT-R", 2, "fresh-onu"],
)
removed = db_mod.prune_history(None, 1)
left = db_mod.query(None, "SELECT if_name FROM olt_onu_history ORDER BY ifindex")
check("retention deletes history older than 24 hours",
      removed >= 1 and [row["if_name"] for row in left] == ["fresh-onu"])

print()
print("FAILURES:", failures)
raise SystemExit(1 if failures else 0)
