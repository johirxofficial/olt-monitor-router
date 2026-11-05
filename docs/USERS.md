# Users, roles and daily use

Every page except **About** and the **login** screen requires an account. The
first-run wizard creates the first **super admin**.

---

## 1. Roles and extra permissions

A role decides the **baseline**. Each non-super-admin account also has six
independent switches that can add (or remove) access without changing the role.
Super admins always have every permission and cannot be locked out.

| Capability | Viewer | Manager | Super admin | Permission flag |
|---|:---:|:---:|:---:|---|
| Dashboard, OLTs, PONs, ONUs, Users, Routers, Ports, Alerts, Search | ✅ | ✅ | ✅ | — |
| SNMP Tools (live probes) | ✅ | ✅ | ✅ | `perm_tools` |
| Add / edit / disable / delete OLTs and routers | — | ✅ | ✅ | `perm_devices` |
| Poll a device now, test SNMP / Telnet / HTTP API | — | ✅ | ✅ | `perm_devices` |
| Site settings (name, tagline, About, refresh, port) | — | — | ✅ | `perm_settings` |
| User management (create, role, permissions, reset password, disable) | — | — | ✅ | `perm_users` |
| Site config / SQL export & import, remote MySQL settings | — | — | ✅ | `perm_sql` |
| Developer page (source bundle, file browser, audit log) | — | — | ✅ | `perm_developer` |
| About page | ✅ | ✅ | ✅ | — |

Defaults: **viewer** = `perm_tools`; **manager** = `perm_devices` + `perm_tools`;
**super admin** = all. Every switch is enforced in the navigation, the route and
the central middleware — hiding a button is never the only protection.

**Viewer** is the safe account for NOC screens and customers. Give a trusted
viewer `perm_devices` only if they should manage hardware; the monitoring pages
themselves stay read-only.

### Device vendor lookup and quick search

The navbar and dashboard search box accepts a username, remote IP, MAC (any
notation), ONU interface, serial, model, VLAN or PON number. Results are grouped
into users / ONUs / PONs and every row shows the **device company** resolved from
the bundled offline IEEE OUI database (Huawei, Cisco, Netis, TP-Link, …), so a
support call can identify the customer's router immediately. Unknown or
randomized MACs show `-`.

---

## 2. Sign in / out

* `http://<server>:<port>/login`
* After 8 failed attempts from the same IP + username, login is paused for
  5 minutes.
* The account menu (top right) → **Account** to change your own password,
  **About**, **Log out**.
* Sessions last 7 days and are invalidated immediately when the account is
  disabled or deleted.

---

## 3. Account menu

| Item | Who | What |
|---|---|---|
| Account | everyone | change display name / email / own password |
| About | everyone (also public) | project, developer, feature list |
| Settings | super admin | site, web port, MySQL, backup, users |
| Developer | super admin | download bundle, inspect source, audit log |
| Log out | everyone | clears the session cookie |

---

## 4. Super admin: creating users

**Settings → Web users → New user**

* username (3–32 chars, letters/digits/`.`/`-`/`_`);
* password (min 8 chars) — use a password manager;
* role: viewer / manager / superadmin;
* display name and optional email;
* the six **extra permission** switches (pre-filled from the role; the role
  dropdown updates them live).

Press **Permissions** on an existing row to change that account's switches later.
You can change a role, reset a password, deactivate or delete an account at any
time. The system refuses to leave **zero active super admins** and will not let
you remove your own last administrative access.

---

## 5. Super admin: site settings

**Settings → Site**

* site name (navbar + browser title);
* tagline (login and About page);
* About text (optional extra paragraphs);
* support email and footer text;
* refresh interval (dashboard auto-refresh);
* web port — saved to `.env` as `APP_PORT`, **restart required**:
  `systemctl --user restart olt-monitor-router`.

**Settings → Database**

* shows the active MySQL target (never the password);
* remote connection form with **Test connection**;
* saving writes `.env` atomically (`.env.bak` kept) and asks for a restart.

**Settings → Backup**

* **Download site config** — a small JSON file with devices + credentials, site
  settings and web users. It deliberately contains **no** ONU, PPPoE, dBm,
  forwarding-table or history data.
* **Import site config** — type `IMPORT` to confirm. Replaces the configuration,
  re-encrypts secrets with this server's key, clears collected data and lets
  polling refill it.
* **Advanced** — full SQL dump (current state or all history), SQL import and
  the list of recent backups in `backups/`, for disaster recovery.

---

## 6. Manager: devices and polling

* **Devices → Add OLT / Add router** — enter host, SNMP community or RouterOS
  credentials, optional branch/site/rack and per-device interval.
* Edit / disable / delete takes effect immediately (the poller reloads).
* **Devices → Test** checks SNMP, Telnet and (for C-Data) the HTTP API.
* **Tools** probes OIDs live and can poll one device now.

Device secrets are encrypted with `data/.secret_key`; the UI only shows whether
a secret is set, never the value.

---

## 7. Viewer: monitoring

The viewer sees the same data as a manager but every action button is hidden or
rejected with HTTP 403. Useful for a wall display: open the dashboard, log in as
a viewer, leave it running.

---

## 8. Alerts

**Alerts** shows active issues (critical / warning / info). They are recomputed
every poll from the thresholds in `config/config.yaml` (`alerts:` block):
ONU optical power, dead PONs, OLT CPU/RAM/temperature, router health, and
custom per-device rules.

---

## 9. About page

`/about` is public by design: project description, feature list, technology,
version and credits (**Johirxofficial**, assisted by an AI agent). The About text
from Settings is appended. No monitoring data is shown there.
