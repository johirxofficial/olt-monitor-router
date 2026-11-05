# olt-monitor-router — complete Linux installation guide

This guide takes a **brand-new Ubuntu/Debian server** to a running, secured
installation with its own MySQL database, super-admin account and systemd
service. Everything below is copy-paste friendly.

---

## 1. What you need

| Item | Minimum | Notes |
|---|---|---|
| OS | Ubuntu 22.04 / Debian 12 | any modern Linux with systemd works |
| Python | 3.10+ | `python3 --version` |
| MySQL | 8.0+ (MariaDB 10.6+) | local or remote |
| RAM | 1 GB | 2 GB is comfortable for several OLTs |
| Disk | 2 GB + history | history grows ~1–2 GB per million rows |
| Network | SNMP (UDP 161) to OLTs, RouterOS API (TCP 8728/1122) to routers | the web port is configurable (default 5002) |

The server needs outbound access only if you want the CDN-hosted Bootstrap /
Chart.js assets; all app logic is offline.

---

## 2. Quick install (recommended)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip mysql-server unzip

# copy the bundle to the server, then:
unzip olt-monitor-router-*.zip -d ~/olt-monitor-router
cd ~/olt-monitor-router

./install.sh
```

`install.sh` does, in order:

1. checks Python ≥ 3.10;
2. creates `.venv` and installs `requirements.txt`;
3. copies `config/config.example.yaml` → `config/config.yaml` (only if missing);
4. creates `.env` from `.env.example` (only if missing) and stops so you can fill it in;
5. creates the MySQL database, app user and tables (asks for a MySQL admin password);
6. creates the `<database>_selftest` companion database used by `tools/selftest.py`;
7. installs and enables the **systemd --user** unit `olt-monitor-router`.

Run it twice: the first run creates `.env`, the second finishes the install.

### Fill in `.env`

```dotenv
# MySQL (created by tools/setup_mysql.py)
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=olt_monitor
DB_USER=olt_monitor
DB_PASSWORD=change-me

# web
APP_PORT=5002

# first device seed (optional - you can also add devices in the browser)
OLT_HOST=192.0.2.10
OLT_COMMUNITY=your-community
ROUTER_HOST=198.51.100.1
ROUTER_API_PORT=8728
ROUTER_USER=admin
ROUTER_PASSWORD=
```

`.env` is written with mode `0600` and is never included in a bundle or a backup
zip. Treat it like a password file.

### First-run web setup

Open **`http://<server-ip>:5002/setup`**. The wizard asks for:

* **site name / tagline** (shown in the navbar, browser title and About page);
* **web port** (written to `.env` as `APP_PORT`, takes effect after restart);
* **super-admin username + password**;
* optional **MySQL remote connection** fields (host, port, database, user,
  password) with a **Test connection** button. Leave them untouched to keep the
  database from `.env`.

When setup finishes you are logged in as **super admin**. The wizard disappears
forever after the first account exists (to re-run it, delete all rows from the
`users` table deliberately — see §9).

---

## 3. Manual installation (no `install.sh`)

```bash
cd ~/olt-monitor-router
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml
cp .env.example .env && nano .env

# 1) create database + user + schema (local root, or a remote admin)
.venv/bin/python tools/setup_mysql.py
#    non-interactive:
.venv/bin/python tools/setup_mysql.py --admin-user root --admin-password '...'

# 2) verify devices (SNMP / RouterOS), may take a minute per OLT
.venv/bin/python run.py --check

# 3) foreground smoke test
.venv/bin/python run.py
```

Then open `http://<server-ip>:5002/setup`.

### Using a remote MySQL server

On the MySQL host (as an admin):

```sql
CREATE DATABASE olt_monitor CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE olt_monitor_selftest CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'olt_monitor'@'10.%' IDENTIFIED BY 'a-long-password';
GRANT ALL PRIVILEGES ON olt_monitor.*          TO 'olt_monitor'@'10.%';
GRANT ALL PRIVILEGES ON olt_monitor_selftest.* TO 'olt_monitor'@'10.%';
FLUSH PRIVILEGES;
```

Set `DB_HOST` / `DB_USER` / `DB_PASSWORD` in `.env` (or later on the Settings
page). The app never needs global MySQL privileges. `bind-address` and the
firewall must allow TCP 3306 from the monitor server.

---

## 4. Run it as a service

### Per-user (no root, installed by `install.sh`)

```bash
systemctl --user status olt-monitor-router
systemctl --user restart olt-monitor-router
journalctl --user -u olt-monitor-router -f
```

Keep it running after logout (already enabled on this machine):

```bash
sudo loginctl enable-linger $USER
```

### System-wide

```bash
sudo cp deploy/olt-monitor-router.service /etc/systemd/system/
sudo nano /etc/systemd/system/olt-monitor-router.service   # edit paths + User
sudo systemctl daemon-reload
sudo systemctl enable --now olt-monitor-router
```

### Without systemd

```bash
./start-bg.sh     # writes data/app.pid, logs to logs/console.{out,err}
./status.sh
./stop.sh
```

`start-bg.sh` automatically uses the systemd unit when it exists, so you never
end up with two copies.

---

## 5. After installation

1. **Add your OLTs and routers** on the **Devices** page. The config file's
   `olt:` / `router:` blocks are only the initial seed.
2. Open **Settings → Site** and set the site name, tagline and About text.
3. Create accounts on **Settings → Users**:
   * `viewer` — read-only;
   * `manager` — can add/edit devices, poll now;
   * `superadmin` — everything.
4. Run the OLT auto-detector once per OLT for optical dBm:
   ```bash
   .venv/bin/python tools/discover_olt.py --olt "OLT name" --autoconfig
   ```
5. Open **Settings → Backup** and take a first **site config** export (JSON).
   It contains devices, credentials, site settings and web users only; polling
   refills ONU/PPPoE/dBm data after an import.

---

## 6. Security checklist

* Change the web port from 5002 if the server is reachable from the internet.
* Put the dashboard behind a VPN or firewall; the login is strong but there is
  no rate-limiting at the network edge.
* `.env`, `data/.secret_key` and `data/.session_key` must be readable only by the
  service user. `install.sh` and the web settings page set `0600`.
* Use a dedicated MySQL user limited to the two `olt_monitor*` databases.
* Take a regular **site config export** (Settings → Backup). It is small,
  portable and restores devices/credentials/settings/users without monitoring
  data. A SQL dump is the additional disaster-recovery option; without its
  matching `data/.secret_key`, the device passwords in that dump are unreadable.
* Delete unused accounts; deactivating a user takes effect on their next click.

---

## 7. Updating an existing installation

```bash
cd ~/olt-monitor-router
./stop.sh

# keep your own: .env, config/config.yaml, config/optical_oids.yaml, data/
unzip -o new-bundle.zip -d /tmp/olt-new
rsync -a --exclude '.env' --exclude 'config/config.yaml' \
      --exclude 'config/optical_oids.yaml' --exclude 'data/' --exclude 'logs/' \
      /tmp/olt-new/ ./

./install.sh          # upgrades the venv, re-checks the schema, restarts
```

`db.init_db()` applies `ALTER TABLE ... ADD COLUMN` migrations automatically.
Take a **site config** export (or a SQL dump) first if you want a rollback point.

A bundle for a new server is available on **Developer → Download source
bundle**; it excludes all local state.

---

## 8. Backup and restore

**Recommended — config-only site backup (from the UI):** Settings → Backup →
*Download site config (.json)*. The file contains every OLT/router with its
credentials, all site settings and all web users (roles, permissions, password
hashes). It contains **no** ONU inventory, optical dBm, PPPoE sessions, router
signal readings, forwarding tables or history, so it stays small.

Store it privately — device credentials and password hashes are inside. The
server-specific `.env` (MySQL credentials, listening port) is not included.

**Restore the config:** Settings → Backup → *Import site config*, choose the
JSON file and type `IMPORT`. The import:

* validates the file (format version, devices, duplicate hosts, at least one
  active super-admin);
* replaces devices, site settings and web users;
* re-encrypts device secrets with **this** server's `data/.secret_key`;
* clears the collected measurement tables, which polling refills within the
  next 10-minute cycle.

The admin performing the import keeps their session when the account exists in
the file.

**Full database dump (advanced / disaster recovery):**

```bash
mysqldump --single-transaction --routines --triggers \
  -h "$DB_HOST" -u "$DB_USER" -p "$DB_NAME" | gzip > olt_monitor.sql.gz
cp data/.secret_key data/.session_key .env ~/olt-monitor-keys/
```

**Restore the dump:**

```bash
gunzip -c olt_monitor.sql.gz | mysql -h "$DB_HOST" -u "$DB_USER" -p "$DB_NAME"
# or: Settings → Backup → Advanced → Import SQL file
```

A SQL dump needs its matching `data/.secret_key`; a config-only export does not.
The SQL importer requires you to type `IMPORT`, runs in a transaction per batch
and reports the failing statement if anything is wrong. It does not support
`DELIMITER`/stored procedures.

---

## 9. Reset / disaster recovery

| Situation | What to do |
|---|---|
| Lost the super-admin password | with DB access: `.venv/bin/python tools/reset_admin.py --user admin` (adds a new password interactively) |
| Lost `data/.secret_key` | device passwords cannot be decrypted; restore a config-only JSON export (it re-encrypts), or retype them on the Devices page |
| Lost `data/.session_key` | everyone is logged out once; a new key is generated |
| Want the setup wizard again | `mysql ... -e "DELETE FROM users"` and restart, then open `/setup` |
| Database gone | create an empty DB, run `tools/setup_mysql.py`, restore the dump |

`tools/reset_admin.py` is included in `tools/`.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `MySQL database 'olt_monitor' does not exist` | run `tools/setup_mysql.py` |
| `Access denied for user ... (1045)` | wrong `DB_*`; re-run setup or fix `.env` |
| Setup page keeps appearing | no active super-admin: check `SELECT username, role, active FROM users;` |
| Login says "too many attempts" | wait 5 minutes; the limiter is per IP + username |
| `port 5002 is already in use` | `./stop.sh`, then `ss -ltnp \| grep 5002` |
| Service stops after logout | `sudo loginctl enable-linger $USER` |
| Blank page / 403 on POST | your proxy strips the `Origin` header or serves a different host — use the same hostname |
| `pymysql` / `python-multipart` missing | run through `.venv/bin/python` or re-run `./install.sh` (it installs `requirements.txt`) |
| OLT dBm shows `n/a` | run `tools/discover_olt.py --autoconfig` for that OLT |
| SQL import "statement N failed" | the dump is partial or from another schema; restore the matching version |

Logs: `logs/olt_monitor.log`, `journalctl --user -u olt-monitor-router`.

---

## 11. Uninstall

```bash
./stop.sh
systemctl --user disable olt-monitor-router 2>/dev/null || true
rm -rf ~/.config/systemd/user/olt-monitor-router.service
# optional: drop the databases (this deletes all history)
mysql -u root -p -e "DROP DATABASE olt_monitor; DROP DATABASE olt_monitor_selftest;"
# then remove the project directory
```

Keep a SQL export and `data/.secret_key` before deleting anything.
