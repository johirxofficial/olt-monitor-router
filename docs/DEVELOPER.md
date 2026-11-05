# Developer guide — olt-monitor-router

Everything a developer needs to understand, extend and safely update this
project. Written for the source bundle on the **Developer** page.

* Owner / maintainer: **Johirxofficial**
* Built with assistance from an AI agent
* Stack: Python 3.10+, FastAPI + Jinja2, MySQL 8, PyMySQL, SNMP (stdlib socket),
  MikroTik RouterOS API, systemd

---

## 1. Runtime picture

```
OLT #1 ── SNMP ──┐
OLT #2 ── SNMP ──┼──► collector threads ──► scheduler ──► MySQL ──► FastAPI ──► browser
Router #1 ─ API ─┤        (per device)         │            ▲
Router #2 ─ API ─┘                             └─ alerts ───┘
                                                     │
                                     registry (devices table) ◄── web UI
```

* **One polling thread per device.** A slow OLT never blocks a router.
* **MySQL is the only store.** `*_current` tables are upserted every poll;
  `*_history` tables are append-only (sampled by `history_every_n_polls`).
* **The web UI runs in the same process as the pollers** by default. Device
  changes call `Monitor.reload()` on a background thread — no restart.
* **Authentication is enforced by middleware** in `olt_monitor/web/app.py`;
  individual routes do not repeat the check.

---

## 2. File map

```
olt-monitor-router/
├── run.py                     entry point: args, DB configure, monitor, uvicorn
├── install.sh                 one-time Linux setup + systemd --user unit
├── start.sh / start-bg.sh     foreground / background launcher
├── stop.sh / status.sh        lifecycle + health
├── requirements.txt           fastapi, uvicorn, jinja2, pyyaml, dotenv, python-multipart, pymysql, cryptography
├── config/
│   ├── config.example.yaml    shipped template (no site secrets)
│   ├── config.yaml            THIS install (never ship)
│   └── optical_oids.yaml      auto-discovered OIDs for THIS install's OLTs
├── deploy/
│   └── olt-monitor-router.service   system-wide systemd unit template
├── docs/
│   ├── INSTALL.md             full new-server guide
│   ├── DEVELOPER.md           this file
│   └── USERS.md               roles + daily usage
├── olt_monitor/
│   ├── config.py              YAML/.env loader, defaults, device seeds, OID merge
│   ├── db.py                  MySQL layer: schema, migrations, query/upsert, prune
│   ├── auth.py                users, roles, scrypt hashes, signed session cookie
│   ├── site_settings.py       site name/tagline/about, .env editor, MySQL test
│   ├── siteconfig.py          config-only JSON export/import (devices, users, settings)
│   ├── sqladmin.py            SQL dump / import, backup listing
│   ├── bundle.py              source-bundle builder (Developer page)
│   ├── registry.py            devices table (OLT/router CRUD, seeding, cache)
│   ├── scheduler.py           Monitor: threads, history sampling, correlation, reload
│   ├── alerts.py              thresholds → alerts_current
│   ├── correlate.py           PPPoE user ↔ ONU matching
│   ├── secrets.py             ChaCha20+HMAC encryption for device passwords
│   ├── health.py              OLT health score
│   ├── vendors.py             brand/pon detection, name classifiers, PON numbering
│   ├── cdata.py               C-Data OLT HTTP API client
│   ├── logging_setup.py       rotating log file
│   ├── collectors/
│   │   ├── olt.py             SNMP OLT collector (ports, ONUs, optical, FDB)
│   │   ├── router.py          RouterOS collector (PPPoE, interfaces, SFP)
│   │   └── telnet*.py         optional CLI telemetry
│   ├── web/
│   │   ├── app.py             FastAPI app, auth middleware, all page/API routes
│   │   ├── templates/         Jinja2 pages (base.html is the shell)
│   │   └── static/            CSS/JS
│   └── snmp.py                minimal SNMP v1/v2c client
└── tools/
    ├── setup_mysql.py         create DB/user/schema (+ selftest DB)
    ├── migrate_sqlite_to_mysql.py   import an old data/olt_monitor.db
    ├── discover_olt.py        walk a vendor MIB, write config/optical_oids.yaml
    ├── selftest.py            offline tests (uses <DB>_selftest)
    ├── reset_admin.py         password recovery for a locked-out admin
    ├── build_bundle.py        CLI wrapper around olt_monitor/bundle.py
    ├── deploy_remote.sh       update a server from a bundle zip
    ├── bootstrap_optical.py   resumable optical warm-start
    └── ...                    diagnostics
```

### Where state lives

| State | Location |
|---|---|
| measurements, devices, alerts, users, settings | MySQL |
| device password encryption key | `data/.secret_key` (0600) |
| session signing key | `data/.session_key` (0600) |
| per-OLT forwarding-table cache | `data/fdb_<OLT>.json` |
| SQL backups | `backups/` |
| credentials | `.env` (0600) |

Never put a key or `.env` in git or in a bundle.

---

## 3. Database layer (`olt_monitor/db.py`)

* `TABLES`, `SCHEMA_STATEMENTS`, `MIGRATIONS`.
* Public helpers keep the old signature `fn(db_path, sql, params)`; `db_path`
  only anchors local files, the connection comes from `db.configure(cfg)`.
* `?` placeholders are translated to MySQL `%s` inside `db.query/execute/...`.
* Writes use `INSERT ... ON DUPLICATE KEY UPDATE` (`db.upsert`, `db.upsert_many`).
* `query()` commits after reading — do not remove that: MySQL REPEATABLE READ
  would otherwise pin an old snapshot to the connection.

### Adding a column / table

1. Add the column to the `CREATE TABLE` in `SCHEMA_STATEMENTS`.
2. Add `(table, column, "SQL TYPE")` to `MIGRATIONS` so existing installs get it.
3. Add the table name to `TABLES` (used by dump/import and the self-test).
4. Update the collector that writes it and the page/API that reads it.
5. Add a row to `docs` if it is user-visible.

There are no foreign keys on purpose: a deleted device's history stays useful.

---

## 4. Authentication and roles

`olt_monitor/auth.py`

* `viewer` — read every monitoring page.
* `manager` — viewer + device CRUD, poll now.
* `superadmin` — everything, including this Developer page, users, SQL and
  database settings.

Sessions are stateless HMAC-signed cookies (`olt_session`, 7 days). The user row
is re-read on every request, so role changes/deactivation apply immediately.

`web/app.py` middleware decides:

| Condition | Result |
|---|---|
| public path (`/login`, `/setup`, `/about`, `/static`, `/api/health`) | pass |
| no valid cookie | redirect to `/login?next=…` (API → 401 JSON) |
| no users at all | redirect to `/setup` |
| viewer + POST/PUT/PATCH/DELETE | 403 |
| non-admin on `/settings`, `/users`, `/developer`, `/api/sql` | 403 |
| cross-origin unsafe request | 403 |

Add new privileged pages under those prefixes, or extend `ADMIN_PREFIXES`.

### Password hashing

`hash_password()` → `scrypt$N$r$p$salt$hash`. `verify_password()` is constant
time. Never log or return a hash — `auth.list_users()` strips it.

### Audit trail

`auth.audit(path, username, action, detail, ip)` writes to `audit_log`. Call it
from every state-changing admin action (the existing routes are a template).

---

## 5. Adding a new OLT brand/firmware

Most new OLTs work without touching Python:

1. Add the device on the **Devices** page, choose the brand and PON type.
2. Run `tools/discover_olt.py --olt "<name>" --autoconfig`; it writes
   `config/optical_oids.yaml`.
3. If the interface names differ, use the per-device **classify regexes**
   (`uplink_regex`, `pon_regex`, `onu_regex`, `vlan_regex`).

If the vendor needs a real profile:

| Change | File |
|---|---|
| brand fingerprint (sysDescr/interface names) | `olt_monitor/vendors.py` → `detect()` + brand tables |
| interface name → port/ONU mapping | `vendors.py` → `classify_interface` / regex tables |
| default ONU optical OID groups | `olt_monitor/config.py` → `ONU_TABLE_PROFILES` |
| MAC/forwarding table OIDs | `olt_monitor/config.py` → `MAC_TABLE_PROFILES` |
| SNMP walking quirks, batching | `olt_monitor/collectors/olt.py` |
| Telnet CLI commands/parsers | `olt_monitor/collectors/telnet*.py` |
| HTTP-API-only vendors | `olt_monitor/cdata.py` pattern (new client module) |
| brand picker label | `registry.py` → `BRANDS` / `form_view` |
| new database columns | `db.py` schema + migrations |

Checklist for a new vendor:

- [ ] `vendors.detect()` recognises the `sysDescr` without a configured brand.
- [ ] PON/uplink/ONU names classify correctly (add cases to `tools/selftest.py`).
- [ ] ONU optical values are scaled correctly (`scale`, e.g. 0.01).
- [ ] The MAC table maps PPPoE caller-id → ONU (or document the limitation).
- [ ] Health metrics (CPU/RAM/temp) are either read or reported as `n/a` cleanly.
- [ ] `tools/selftest.py` exits 0 against the self-test database.

---

## 6. Adding a page or API endpoint

1. Data helpers live in `web/app.py` (`pon_rows`, `list_onus`, …) or the module
   that owns the concern. Keep SQL in these helpers, not in templates.
2. Add the route inside `create_app()`; use `render(request, "page.html", {...})`
   for HTML so the site name, user, role flags and device counts are injected.
3. Template: extend `base.html`, set `{% block content %}` and `{% set active = "..." %}`.
4. Nav link: `templates/base.html` (wrap manager/admin links in
   `{% if can_manage %}` / `{% if is_admin %}`).
5. JSON API: `@app.get("/api/...")` returning plain dicts (Decimal is converted).
6. Unsafe endpoints are automatically protected by the role middleware.

---

## 7. Configuration, settings and `.env`

* `config/config.yaml` — polling intervals, alerts, device seed, OIDs.
  The loader falls back to `config/config.example.yaml` when the live file is
  missing (fresh bundle).
* `site_settings` table — site name, tagline, About text, refresh seconds,
  web port, timezone. Edited on **Settings**.
* `.env` — MySQL credentials + `APP_PORT` + initial device secrets.
  Written atomically by `site_settings.write_env()` with a `.env.bak` copy.
* Changing the port / database writes `.env` and needs a restart; the UI says so.

---

## 8. Build, run, test

```bash
# development server with auto-reload is intentionally not used: the poller
# runs in-process. Use the normal launcher:
./start.sh --no-poll              # UI only, data already in MySQL

# tests
.venv/bin/python tools/selftest.py        # offline, uses <DB>_selftest

# compile check
.venv/bin/python -m compileall olt_monitor tools run.py
```

The self-test wipes only the `<DB>_selftest` database and refuses to run against
the live database name.

---

## 9. Releasing a bundle

**Developer → Download source bundle** calls `olt_monitor/bundle.py`:

* includes `olt_monitor/`, `tools/`, `config/config.example.yaml`, `deploy/`,
  `docs/`, all shell scripts and the README;
* excludes `.env`, `config/config.yaml`, `config/optical_oids.yaml`, `data/`,
  `logs/`, `backups/`, `.venv`, `__pycache__`, `*.pyc`;
* stores `.sh` files with LF + executable bit (safe to build on Windows).

CLI equivalent:

```bash
.venv/bin/python tools/build_bundle.py --out /tmp/olt-monitor-router.zip
```

Bundle contents are printed and checked for leaked local state.

---

## 10. Updating any file on a server

1. Keep the old tree: `tar -czf backup.tgz olt-monitor-router`.
2. Replace only the files you changed (or use `tools/deploy_remote.sh`).
3. **Never overwrite** `.env`, `config/config.yaml`, `config/optical_oids.yaml`
   or `data/` — they belong to that installation.
4. Run `./install.sh` (reinstalls deps, applies schema migrations, restarts).
5. Verify: `./status.sh`, `/api/health`, and the pages you touched.
6. Roll back by restoring the tarball if needed.

`tools/deploy_remote.sh` automates steps 1–4 from a bundle zip and restarts the
user unit when it exists.

---

## 11. Coding conventions

* Python 3.10+, `from __future__ import annotations`, type hints on public
  functions.
* Keep the MySQL layer in `db.py`; no raw SQL in templates.
* One concern per module; collectors never import the web app.
* Log with `get_logger("area")`; never log secrets.
* SQL identifiers that are reserved words are backticked (`key`, `position`).
* Templates escape by default. Use `|safe` only for admin-authored About text.
* Every schema change is idempotent and safe to run on an existing database.
* New privileged action → add an `auth.audit()` call.
