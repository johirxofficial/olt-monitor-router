# olt-monitor-router

**EPON / GPON OLT (SNMP + Telnet) + MikroTik RouterOS (API) monitoring — এক জায়গায় সব ডেটা।**

Polls one or more **EPON/GPON OLTs** over **SNMP v1/v2c** (VSOL, BDCOM, LITEX,
C-Data, PHOTON, ECOM, AVIES, HSGQ, HUAWEI, TBS, BTPON, DBC, BDPON and generic
EPON/GPON) and one or more **MikroTik RouterOS** routers over the **RouterOS API**, joins them together,
and shows — per PPPoE user — **uptime + optical power (dBm) + traffic**, next to
the state of every PON port, uplink and ONU. When SNMP cannot describe a device
the poller **falls back to the read-only Telnet CLI automatically**, so a
firmware with no ONU MIB still produces a complete snapshot. Each OLT also gets
a **Telnet CLI collector** (CPU, RAM, board temperature, SFP and per-ONU
diagnostics), a **health score** and an **encrypted-at-rest** configuration.

The same UI gives you a **one-box quick search** (username, IP, MAC, ONU serial,
model, VLAN or PON), a **device-vendor lookup** for every router/ONU MAC (bundled
offline IEEE OUI database), **per-account permission switches**, and a
**one-click distribution ZIP** from the Developer page.

> 🇧🇩 **সংক্ষেপে:** OLT আর MikroTik — দুটোকেই পোল করে, ইউজার অনুযায়ী **dBm + uptime +
> ট্রাফিক** আর OLT-এর PON/uplink/ONU অবস্থা এক ড্যাশবোর্ডে দেখায়।
> একাধিক OLT আর একাধিক রাউটারও একসাথে চলবে — আর **নতুন OLT/রাউটার ওয়েব পেজ থেকেই**
> (Devices পেজ) যোগ, এডিট, টেস্ট আর ডিলিট করা যায়, সার্ভিস রিস্টার্ট ছাড়াই।
> OLT-এর ব্র্যান্ড (VSOL / BDCOM / LITEX / C-Data / PHOTON / ECOM / AVIES / HSGQ /
> HUAWEI / TBS / BTPON / DBC / BDPON / Generic EPON-GPON) আর PON ধরন (EPON / GPON) বেছে নেওয়া যায়, দরকার হলে
> অটো-ডিটেক্ট হয় — আর SNMP community / Telnet পাসওয়ার্ড ডেটাবেজে **এনক্রিপ্ট করে**
> রাখা হয়। **SNMP-তে data না পেলে Telnet fallback** নিজে থেকেই চলে।
> যেকোনো ইউজার/MAC/IP/ONU search করলে **quick search** এক পেজে সব তথ্য দেখায়, আর
> প্রতিটি MAC-এর **device company** (Huawei / Cisco / Netis …) দেখা যায়।

---

## What's new in 3.5.2

**A "Generic GPON" device now uses its real vendor OID profile**

* **`Choyani_DBC_GPON` gets dBm, subscriber and ONU data.** It is a VSOL
  V1600G1B (`sysDescr=V1600G1B`, enterprise `1.3.6.1.4.1.37950`) but had been
  saved as **Generic GPON**, so the collector walked the BDCOM OID tree and
  every optical/inventory column came back empty. Generic GPON / Generic EPON /
  Auto are placeholders, not vendors: the collector now refines them from
  `sysDescr`, `sysObjectID` and interface names on every poll and loads that
  vendor's ONU and forwarding tables. All 301 ONUs now show Rx/Tx dBm,
  temperature, voltage, distance, ONU MAC, and 261 subscribers are linked with
  PPPoE uptime and dBm in both the ONU and Users lists.
* **`sysObjectID` is now a first-class fingerprint.** A white-label firmware
  whose `sysDescr` is only a model string still resolves through its enterprise
  number (VSOL `37950`, BDCOM `3320`, HSGQ/AVIES `50224`, Huawei `2011`).
* **A deliberate vendor choice is still respected.** Only `auto`, `generic` and
  `gpon` are refined; selecting a real brand (VSOL, BDCOM, C-Data, …) is never
  overridden by detection.
* **The first subscriber-MAC snapshot is taken immediately** on a newly
  repaired VSOL GPON, instead of waiting for its stagger phase, so the Users
  page links subscribers on the first poll. Tx/voltage follow on the next poll
  and the static serial/model columns over the next few polls.

### From 3.5.1

**Every configured OLT now returns usable data**

* **BDCOM / VSOL EPON "auto-configured" ONUs are counted as online.** The
  active-onu and onu-information tables report a registered ONU as
  `auto-configured`; that state was not mapped to `oper_status`, so a
  Telnet-only EPON showed `0/N ONU up` even while every ONU was passing
  traffic. `deregistered` / `config-failed` are now explicitly offline.
* **The Test button falls back instead of failing.** If SNMP is blocked but the
  read-only Telnet CLI (or the C-Data HTTP API) answers, the device is reported
  as reachable with the SNMP error shown alongside the working transport.
* **The brand list is complete and honest.** HUAWEI, TBS, HSGQ and
  Generic GPON are first-class entries; the old hidden `gpon_soho` key is
  migrated to `gpon`, and the UI no longer says "best effort" or "SOHO".
* **A failed poll keeps the last good snapshot** (`stale`, `poll_error`,
  `last_good_at`), so an unreachable OLT does not lose its brand, ONU totals or
  history.
* **Slower polls no longer busy-loop.** A VSOL GPON that takes longer than its
  interval gets half the poll duration as a recovery gap, and the per-vendor
  optical/MAC staggering keeps a 500-ONU chassis inside the 10-minute cycle.

### From 3.5.0

**Config-only backup (new)**

* **Settings → Backup → Download site config** now produces a small, portable
  JSON file instead of a database dump. It contains the complete site setup:
  every OLT/router with its credentials, all editable site settings, and every
  web user with role, permission flags and password hash.
* **No monitoring data is exported.** ONU inventory, optical dBm, PPPoE
  sessions, router signal readings, forwarding tables, alerts and all history
  stay on the server, so the file stays a few KB even on a large network.
* **Import restores configuration, not measurements.** Device secrets are
  re-encrypted with the target server's `data/.secret_key`; collected tables
  are cleared and the normal 10-minute polling refills ONU/PPPoE/dBm data.
* Import is validated (format version, devices, duplicate hosts, users, at
  least one active super-admin) and the admin performing it keeps their
  session. The raw SQL dump remains available under **Advanced** for disaster
  recovery.

**AVIES / HSGQ ONU data**

* The `AV-OLT-XE08-L3` (HSGQ enterprise `1.3.6.1.4.1.50224`) naming style is now
  detected and classified: `ONU01/01` = PON 1 / ONU 1, `PON01: Area-A` is a
  PON port, and `GE01/Uplink` / `XGE01` are uplinks.
* Its ONU table is read without discovery: Rx/Tx dBm, temperature, voltage,
  bias current, distance, ONU MAC, model and vendor are attached to every ONU,
  so subscriber correlation and the ONU page have real data.

### From 3.4.1

The 3.4.1 release fixed the EPON/GPON data-integrity bugs and gave the UI a
new look with a light/dark theme.

**OLT data correctness**

* **GPON OLTs no longer show fake EPON ports.** Port names that carry a
  description (`GPON0/1 Area-A,Road-1,Market`, `GPON01ONU61 GPON0/1:61_7141`,
  `GE0/3 Uplink`) are classified by their leading port token, so an all-GPON
  chassis renders exactly `GPON0/1 … GPON0/8` with capacity 128 — never four
  invented `EPON0/*` rows.
* Synthetic PON rows inherit the device's real `pon_type`, name and ONU
  capacity instead of a hard-coded EPON/64 default.
* The VSOL GPON subscriber MAC column and its `PON:ONU` port column are now
  walked in **one interleaved GETBULK snapshot**. The table reuses row numbers;
  reading the columns separately could attach one ONU's MAC to another ONU.
* An exact subscriber-MAC → ONU link from that snapshot always wins over a
  bridge-forwarding-table guess. On GPON, the old "bridge port number equals
  ifIndex" fallback is disabled (on a VSOL V1600G1B it pointed at the wrong
  ONU).
* Correlation now tries an **exact ONU MAC before the FDB**, which is the
  correct order for GPON ONUs that bridge the customer router transparently.
* After a successful poll the scheduler removes interfaces that moved between
  the port and ONU table, so a re-classified ONU cannot linger as a duplicate
  port row.
* `subscriber-01@isp` consistently resolves to **PON 3 / ONU 18** (ifIndex 189,
  MAC `88:BD:09:42:E6:2E`).

**New UI**

* Modern design tokens, gradient brand mark, glass/sticky navigation, pill
  nav links, hover-lift cards, sticky table headers and redesigned badges,
  forms, alerts and pagination.
* **Light/dark theme toggle** in the navbar, remembered in `localStorage` and
  applied before first paint (no flash), defaulting to the OS preference.
* Responsive layout fixes for small screens, reduced-motion support, and a
  fixed-height chart wrapper.
* **The Add/Edit Device popup is readable and editable in both themes.** Its
  markup now renders as a direct child of `<body>` (`{% block modals %}`), and
  the page entrance animation no longer retains a CSS `transform`. A retained
  transform turned `<main>` into a stacking context, which put the modal *under*
  the page backdrop (dark, translucent, impossible to type into).

**Automatic polling, manual refresh and 24-hour retention**

* **Every OLT and router is polled every 10 minutes** (`600 s`), including slow
  GPON chassis. The Devices page also has a **Poll all now** button that queues
  an immediate full refresh of every enabled device.
* Scheduled and manual polls share one **active-device set**, so a device that
  is already being walked is never walked a second time concurrently; the button
  reports how many devices started and how many were already running.
* A live indicator on the Devices page shows `polling N device(s)` and the page
  reloads when the refresh finishes.
* **History retention is 24 hours.** A dedicated maintenance thread prunes rows
  older than `database.keep_history_days` (default `1`) once at startup and then
  every `prune_interval_seconds` (default `300`), so the history tables stay
  small and the trend queries stay fast.
* **One database.** The forwarding-table cache that used to be a JSON file is
  now stored in MySQL (`olt_mac_cache`, one row per OLT). The old
  `data/fdb_<olt>.json` file is only read once as an upgrade fallback.

**Web performance**

* `/users` and `/onus` use **server-side pagination (200 rows/page)** with a
  shared pager, filter-preserving sort links and true totals from `COUNT(*)`.
* The ONU page no longer builds the full dashboard to render its summary; the
  filter + aggregate SQL is shared by the list and the counters.
* `GZipMiddleware` compresses HTML responses larger than 1 KB.
* Page auto-refresh is now **120 s** (data changes every 10 minutes), and the
  registry cache, registry counts and index coverage were tightened.
* Measured on the live 9 300-user / 4 700-ONU dataset: `/users` ~11.4 s → ~2.2 s,
  `/onus` ~7.5 s → ~1.3 s, `/pons` ~3.7 s → ~1.8 s, `/ports` ~6.5 s → ~1.9 s,
  `/devices` ~5–13 s → ~1.5 s.

**Regression tests**

`tools/selftest.py` now covers descriptive GPON port names, suffix-safe
`pon_number()`, MAC-before-FDB precedence, GPON synthetic-PON naming and
capacity, and a full **site-config export → import roundtrip** (devices,
secrets, settings, users, and clearing of collected data). A clean run prints
`FAILURES: 0`.

---

## 1. Verified device support

Built and verified end-to-end against a reference deployment:

| Property | Value |
|---|---|
| OLT model (`sysDescr`) | **V1600D** (VSOL EPON OLT) |
| OLT host / community | `192.0.2.10` / **`your-community`** |
| SNMP | **v1 and v2c work**, v2c with GETBULK; SNMPv3 is *not* enabled on the device |
| OLT enterprise MIB root | `1.3.6.1.4.1.37950` |
| OLT interfaces | 16 × `GE0/1..GE0/16`, 8 × `EPON0/1..EPON0/8`, ~386 × `EPONxxONUy`, `vlan*` |
| RouterOS | **7.23.8** |
| Router host / API port | `198.51.100.1` / **1122** |
| PPPoE users | ~1100 online sessions, all with live byte counters |

**A live poll of this hardware gives:** 411 OLT interfaces → 25 ports + 386 ONUs,
369-entry bridge MAC table, 386/386 ONUs with temperature/distance/MAC,
286 with live Rx/Tx dBm, and **300 of ~1100 PPPoE users linked to their ONU**.

> 1100 users vs 386 ONUs is expected: the router is an aggregation device that
> also serves other OLTs, so most of its sessions legitimately belong to a
> different OLT and stay "not linked" on this dashboard.

### 1.1 Other OLT brands (multi-vendor profiles)

The collector is vendor-neutral: a **VendorProfile** holds the interface-name
patterns, the ONU identity key and the Telnet command set, so a new brand is a
table entry rather than a code change. Choose the brand and the PON architecture
on the Devices page — or leave them on **Auto-detect** and the next poll guesses
from `sysDescr` and the interface names.

| Brand | PON | ONU identity | Notes |
|---|---|---|---|
| **VSOL** (V1600D) | EPON | MAC | ✅ verified on live hardware |
| **C-Data** (FD1304E-B1) | EPON *or* GPON | MAC / serial | ✅ verified on live hardware (HTTP API + Telnet) |
| **BDCOM** | EPON *or* GPON | MAC / serial | ✅ verified on live P3608B (SNMP EPON); GPON uses the CLI/OID profile |
| **AVIES** | EPON | MAC | ✅ verified on a live AV-OLT-XE08 (vendor OID tree + CLI) |
| **HSGQ** | EPON *or* GPON | MAC / serial | ✅ verified EPON; GPON uses the standard GPON rules |
| **HUAWEI** | GPON *or* EPON | serial / MAC | MA/EA/SmartAX; `gpon-onu_1/2/3:4` names |
| **TBS** | EPON *or* GPON | MAC / serial | VSOL-style `EPON0/1:7` names |
| **ECOM** | EPON | MAC | `ecom_epon` config-mode CLI |
| **LITEX** | EPON *or* GPON | MAC / serial | ECOM-style `show ont ...` CLI |
| **DBC** | EPON | MAC | BDCOM EPON command set |
| **BDPON** | EPON | MAC | Telnet `epon_ponport` |
| **BTPON** | EPON | MAC | Telnet `epon_ponport` |
| **PHOTON** | EPON | MAC | ✅ verified on live 4PON EPON-OLT (Telnet DDM) |
| **Generic GPON** | GPON | serial | any GPON OLT with standard MIB-II |
| **Generic EPON** | EPON | MAC | any EPON OLT with standard MIB-II |

VSOL, C-Data, BDCOM EPON, AVIES/HSGQ and PHOTON are verified against real
hardware.  The other profiles use the same standard MIB-II tables and the
shared EPON/GPON interface rules, so choosing the closest brand never disables
collection; switch to **Generic EPON/GPON** when the make is not listed.
Switching **EPON ⇄ GPON** also switches ONU identity between MAC and serial,
and the ONU capacity default between 64 and 128 per PON.

Each brand maps to one of the read-only Telnet profiles (`epon_vsol`,
`bdcom_epon`, `cdata_epon`, `ecom_epon`, `epon_ponport`, `gpon_generic`,
`gpon_exec`, `bdcom_gpon`, `vsol_gpon`). The collector picks the profile from
**brand + PON type** automatically; an old install whose device still carries
the historical default is upgraded on the next poll without touching the form.

> **Telnet fallback is automatic.** When the SNMP walk fails, returns no
> interface table, or exposes PON ports but no ONU inventory (PHOTON / BDPON /
> BTPON firmware), the poller builds the snapshot from the CLI instead of
> storing an "unreachable" row. The OLT needs **Telnet credentials + the
> *collect over Telnet* switch** on the Devices page — that is the only
> prerequisite; no separate mode or restart is required.

> **C-Data EPON OLTs are best read through their HTTP API.** Their SNMP agent
> answers MIB-2 and nothing else: no ONU table, no bridge FDB, no ARP, no
> ENTITY. The ONU inventory, optical power, distance and last-down cause live in
> the JSON API of the device's own web UI, so the collector reads that instead —
> see [§3.2](#32-olt-http-api-c-data-read-only). If the API is disabled or its
> password is unavailable, the verified `cdata_epon` **Telnet profile** still
> builds the ONU inventory (`show ont info all`) and the subscriber MAC table
> (`show mac all`); that table names the PON but not the ONU, so its links are
> PON-level.

---

## 2. Architecture

```
                 ┌──────────────────── olt-monitor-router ────────────────────┐
   OLT #1 ─SNMP──┤ OltCollector ─┐                                           │
   OLT #2 ─SNMP──┤   (1 thread   │      ┌──────────┐    ┌────────┐   ┌─────────┐
     ...         │    per OLT)   ├─────►│correlate │───►│ MySQL  │──►│ FastAPI │
                 │               │      │ user↔ONU │    │        │   │ + Jinja │
 Router #1 ─API─┤ RouterCollector┘      └──────────┘    └────────┘   └─────────┘
 Router #2 ─API─┤   (1 thread per router)      ▲            ▲
     ...         │                              │            │
                 └─────────────────────── alerts.py ─────────┘ (thresholds)
```

* One polling thread **per device** — a slow OLT never blocks a router.
* The web app only *reads* MySQL, so it can run as its own process (or a second one).
* Correlation is global: every PPPoE user is matched against every OLT.

**Why two sources?** A MikroTik PPPoE session knows nothing about optics — the dBm
belongs to the ONU. `correlate.py` joins them so the Users page can show both.

---

## 3. Features

* **Devices page (the whole system is managed from the browser)** – add, edit,
  test, enable/disable and delete **OLTs and routers** from the web UI. Devices
  live in MySQL, so a change is applied **live** (only that device's poller is
  restarted, the service keeps running) and `config.yaml` is only a first-run seed.
* **PONs page** – one row per PON port across all OLTs: state, ONU up/down, linked
  users, average and worst rx dBm, PON and aggregate ONU throughput.
* **PON drill-down** – click a PON → every ONU behind it → click an ONU → its full
  optical/metrics page with the PPPoE users on it.
* **ONU detail page** – admin/oper state, ONU MAC, rx/tx/OLT-side rx dBm,
  temperature, distance, traffic, the linked user, and a 24 h optical chart.
* **User detail page** – the PPPoE session (MAC/caller-id, remote IP, uptime,
  session id, interface, in/out bytes) **plus** the ONU behind it, its dBm and the
  strategy that made the match.
* **Dashboard** – OLT & router health, ONU up/down counters, PON summary, uplink
  throughput, optical power distribution chart, latest alerts, per-device filter.
* **OLT Overview page (`/olts`)** – one card per OLT with a **health score gauge**,
  online/last-poll, uptime, CPU, RAM, board temperature, SNMP round-trip time,
  PON/ONU/subscriber totals and a **Poll now** button; plus a multi-OLT table
  (branch, brand/firmware, SNMP address, status, ONU ratio, polling state).
* **Multi-vendor OLT support** – BDCOM, C-Data, VSOL, ECOM, HUAWEI, HSGQ, TBS,
  BTPON, generic EPON and generic GPON, auto-detected or chosen per device.
  EPON (1.25 G, MAC-based) or GPON (2.5 G, serial-based). See §1.1.
* **Telnet CLI telemetry (optional, read-only)** – CPU load, RAM usage, board
  temperature, SFP diagnostics and the per-ONU *last down cause* on firmwares that
  do not expose them over SNMP. Only `show`/`display` commands are ever sent.
* **OLT HTTP API (optional, read-only, C-Data)** – C-Data EPON OLTs have no ONU
  table over SNMP at all; their own JSON API is read instead for the ONU list,
  optical power, distance, last-down cause plus CPU/RAM/temperature.
  See §3.2.
* **Secrets encrypted at rest** – SNMP community and Telnet passwords are sealed
  with a ChaCha20 + HMAC-SHA256 envelope; the key never leaves the server.
* **ONUs page** – summary bar (online/offline/weak-rx/PPPoE), filters by
  OLT / PON / state / weak rx / has PPPoE, search over ONU, user, MAC, serial,
  model and VLAN; serial, model, VLAN, voltage, bias current, fibre distance,
  colour-coded rx/tx and the last-down reason with its timestamp.
* **ONUs page** – filter by OLT / PON / state / search, sortable, optical dBm,
  temperature, distance and MAC per ONU, click through to its PPPoE user.
* **Users page** – every online PPPoE user with IP, MAC, **uptime**, in/out bytes,
  and the **dBm of their ONU**, plus *which* matching strategy linked them.
* **Routers page** – identity, RouterOS version, board, CPU, memory, uptime and
  PPPoE count per router, plus a per-interface table with SFP optical readings.
* **Ports page** – uplink + PON + VLAN interfaces with live bps, octet and error counters.
* **Alerts page** – weak/hot ONU power, down PON ports, down ONUs that affect a
  subscriber, saturated uplinks, high CPU, low router memory, unreachable device.
* **SNMP Tools page** – run a live `GET`/`WALK` against any OLT from the browser.
* **Automatic polling every 10 minutes + manual full refresh** – every enabled
  OLT and router is polled on a `600 s` cycle, and **Poll all now** on the
  Devices page pulls a complete refresh on demand. Polls share an active-device
  guard, so a slow device is never walked twice at once.
* **History** – ONU optical, port throughput, user count and router resources on
  a configurable cadence, exposed as `/api/trend/...`. **Only the last 24 hours
  are kept**; older rows are pruned every few minutes by a maintenance thread.
* **One MySQL database** – devices, current snapshots, alerts, history *and* the
  forwarding-table cache (`olt_mac_cache`) live in the same schema.
* **Dependency-light** – SNMP and the RouterOS API protocol are implemented on the
  standard library. Only **5 pip packages**, all for the web layer.

### 3.1 OLT health score

Every OLT gets a 0–100 score, shown as a gauge on `/olts` and as a badge on the
dashboard, computed in `olt_monitor/health.py` from whatever is actually known
about the device (a missing metric is never counted as a fault):

| Signal | Effect |
|---|---|
| unreachable / disabled | score 0 and the reason is listed |
| CPU ≥ 80 % / ≥ 90 % | −10 / −20 |
| RAM ≥ 80 % / ≥ 90 % | −7 / −15 |
| board temperature ≥ 60 / ≥ 70 °C | −10 / −20 |
| PON ports down | −4 each, capped at −30 |
| ONUs offline | −100 × the down ratio, capped at −30 |
| no health metric could be read at all | −5 |

If every metric is unknown the OLT still scores 95 — a missing reading is never
reported as a fault. The floor is 1, so an unreachable OLT (0) always looks
different from a healthy one. `grade()` maps the score to a Bootstrap colour
(`success ≥ 90`, `warning ≥ 70`, `orange ≥ 40`, `danger` below), `label()` to a
word (*Excellent ≥ 95*, *Good ≥ 70*, *Degraded ≥ 40*, else *Critical*), and the
reasons are listed on the OLT card and in its tooltip.

### 3.2 OLT HTTP API (C-Data, read-only)

A C-Data EPON OLT (FD11xx/FD13xx, verified on an `AC-FD1304E-B1`) publishes
nothing useful over SNMP: MIB-2 system + interfaces, and **no ONU table, no
bridge FDB, no ARP, no ENTITY** — so the ONU pages stay empty no matter how the
community is configured. Everything is in the JSON API behind the device's own
web UI (`http://<olt>/cgi-bin/h.cgi?module=…`), which the collector calls
instead:

| What | Module | Result |
|---|---|---|
| login | `sys_login` (password = MD5 hex) | session `token` header |
| identity | `sys_dev_info` | model, firmware, serial, MAC, uptime |
| CPU / RAM / board temp / fans | `sys_dev_mng` | `CpuUsage`, `RamUsage`, `Temp` |
| ONU inventory | `onu_list_get` | PonId, OnuId, MAC, state, last-down cause/time, model, vendor, firmware |
| ONU optical | `ont_optical_list_get` | Rx/Tx dBm, OLT-side Rx, distance (**m**), temperature, voltage, bias |
| OLT-side SFP DDM | `sys_ddm_info` | Tx/Rx dBm and temperature per PON port |
| live traffic | `onu_stats_rate_get` | up/down rate per ONU |
| port state | `sys_dev_status` | `ge` / `xge` / `epon` index + state |

Enable it per OLT on the **Devices** page (*OLT HTTP API*): HTTP port (80),
username, password, cadence and two checkboxes — *read ONU optical power* (slow:
the OLT asks every ONU over OAM, ~50–95 s for 322 ONUs) and *read live ONU
rates*. Press **API** in the OLT table to test it; the answer names the model,
firmware and ONU count.

The same firmware also answers Telnet. The `cdata_epon` profile logs in even
though the banner prints no prompt until it receives a newline, and it sends
`enable` without a password (as this firmware expects). It reads CPU, RAM,
temperature, the global ONT inventory (`show ont info all`) and the forwarding
table (`show mac all`). `show mac all` prints `epon0/1/4` — the PON, not the ONU
— so when the API is off those subscribers are linked at PON level instead of
per ONU.

Safety and behaviour:

* only read modules are allow-listed in `olt_monitor/cdata.py` — a typo cannot
  reach a `_set` / `_add` / `_delete` module, and nothing is ever written;
* the API result **overrides** SNMP for the ONU table (SNMP has none here) but
  the uplink/PON port rows still come from SNMP, so up/down state and counters
  stay consistent;
* optical data is slow, so it runs on `cdata_api_interval_seconds` (default
  300 s) and the last result is merged into every poll in between;
* the API caps a reply at 40 KiB (longer replies are cut mid-JSON), so ONU
  lists are paged and the page size is halved if a reply still comes back
  truncated;
* **the OLT locks an account after two bad passwords**, so a failed login is
  cached for 5 minutes instead of being retried on every poll; a wrong password
  also never turns into a lock-out loop;
* ONUs that SNMP has no interface for get a stable synthetic ifindex
  (`700 000 000 + pon × 4096 + onu`), so ONU links, history rows and the
  PPPoE ↔ ONU join work exactly as they do for a real SNMP interface.

### 3.3 Telnet CLI telemetry

CPU load, RAM usage, board temperature and the SFP/ONU *last down cause* are only
available over the CLI on many OLTs. Enable Telnet per device on the Devices page
(username, password, optional enable password, port, and how often to run it) and
the collector logs in, runs a fixed list of **read-only** commands and merges the
result into the same ONU/port rows that SNMP fills.

Safety is deliberate and enforced in code:

* only commands starting with `show` / `display` are ever sent;
* a deny-list rejects anything that could change the device (`write`, `save`,
  `reboot`, `reset`, `delete`, `clear`, `no`, `configure`, shell escapes, `|` pipes
  that could turn a `show` into a write);
* `--More--` is answered with a space rather than by changing the terminal length;
* the CLI command set comes from the selected **Telnet profile** (VSOL EPON,
  BDCOM EPON, GPON generic, ...), so a different firmware is a profile, not a
  code change;
* SNMP always wins: a Telnet reading never overwrites a value SNMP reported.

> **Privileged access is required.** On a VSOL V1600D the factory `support` user
> logs into user mode (`Hostname>`) where the only commands are `show
> channel-group summary`, `show history`, `show ip rip` and `show monitor session`
> — no EPON, ONU, CPU or optical commands at all. Use an account that can reach
> privileged mode (`Hostname#`), and fill the **enable password** box if the
> account needs `enable` first. When an account is too restricted the collector
> now says so instead of reporting a silent success: the reason appears as
> *"the CLI rejected 'show cpu' - the account probably has no privileged
> access"* on the OLT's card and in `telnet_error`.

Telnet runs on its own cadence (`telnet_interval_seconds`, default 300 s) *inside*
the OLT poll, and its last result is kept between polls so the UI never blanks.
Per-ONU detail commands are only run for ONUs still missing their serial/model,
capped at `telnet_detail_max` per cycle.

#### BDCOM EPON (P3608B, verified on 10.1.0F)

Choose the **BDCOM EPON** Telnet profile (the BDCOM brand selects it
automatically). Every command is read-only:

| What | Command |
|---|---|
| CPU | `show cpu` |
| Board temperature | `show temperature` |
| PON SFP | `show epon optical-transceiver-diagnosis` |
| Uplink state, traffic and DDM | `show interface gigaethernet0/7`, `show interface tgigaethernet0/1` |
| ONU inventory | `show epon active-onu`, `show epon inactive-onu` |
| Per-PON ONU optics and inventory | `show epon onu-ctc-optical-transceiver-diagnosis interface EPON0/N`, `show epon onu-information interface EPON0/N` (N = 1..8) |
| Subscriber MAC table | `show mac address-table` |

BDCOM wraps its 80-column tables, so a row can be split over two physical
lines; the BDCOM parsers stitch them back together. `--` cells (an ONU that is
offline) are stored as "n/a", never as 0 dBm. `show memory` is not supported on
this firmware, so RAM stays empty. The uplink interface names in the profile
are `gigaethernet0/7` and `tgigaethernet0/1`; edit the profile if your chassis
uses different ports.

#### BDPON / BTPON / PHOTON EPON (verified on PHOTON 1.1.3)

Choose the **BDPON**, **BTPON** or **PHOTON** brand (all three select the
`epon_ponport` Telnet profile automatically; Auto-detect recognises PHOTON from
its `EPON-OLT Series` `sysDescr` and flat `pon1`, `ge1` interface names). Every
command is read-only:

| What | Command |
|---|---|
| CPU | `show cpu` — flat output such as `cpu0: 7%` |
| RAM | `show memory` — Linux `MemTotal` / `MemFree` plus `Memory usage: 22.15%` |
| PON SFP DDM | `show pon-port ddm-info pon{pon}` — one per PON |

The collector expands `{pon}` for every PON it knows about (SNMP PON ports plus
the ONU list) and probes PON 1–8 when the device exposes no PON inventory at
all. Each answer is a key/value block:

```
 Laser bias current     : 13.600 mA
 Laser output power     : 8.1780 mW / 9.13 dBm
 Module temperature     : 26.35 degrees C / 79.43 degrees F
 Module voltage         : 3.2924 V
```

The **dBm** value is stored for TX power (mW is only converted when the
firmware omits dBm); temperature, voltage and bias are stored as printed. An
empty PON answers `Error: The optic module of port is absence!` and produces no
invented row. PHOTON firmware prints no RX power for the PON module, so
`rx_power` stays blank there; `show temperature` is not a valid command on this
firmware and is not sent.

> PHOTON login was verified with `admin` / `admin`. The factory `root` and
> `support` accounts rejected the provided passwords, so use the privileged
> account that reaches `Hostname#` on your firmware.

### 3.4 Secrets encrypted at rest

The SNMP community, the router password, the Telnet passwords and the OLT API
password are encrypted
before they are written to MySQL, with a small dependency-free envelope
(ChaCha20 + HMAC-SHA256, key in `data/.secret_key`, mode 0600). Sealed values are
stored as `enc:v1:<nonce>:<ciphertext>:<tag>`; a plain-text value still reads
through so an existing `config.yaml` keeps working, and any plain-text secret is
upgraded in place the first time the registry is opened. Secret fields are never
sent to the browser — the form only shows *"set"* / *"keep"*.

### 3.5 Quick search — one box for everything

The navbar and the dashboard both have a search box. It accepts any of:

| You type | It finds |
|---|---|
| `rahim`, `rahim@isp` | a PPPoE username (exact hits first) |
| `10.10.1.5` | the session by remote IP |
| `AA:BB:CC:DD:EE:FF`, `aabb.ccdd.eeff`, `aabbccddeeff` | router MAC, ONU MAC, or anything in the FDB |
| `pon1:23`, `EPON0/1:23`, `GPON0/1:5` | the ONU interface |
| `48575443AABBCCDD` | a GPON/EPON serial number |
| `HG8245H`, `Huawei` | the ONU model / vendor string |
| `100`, `VLAN100` | a service VLAN or a PON port |

The result page (`/search?q=…`) groups **users**, **ONUs** and **PONs**. Every
user row carries the MAC vendor of the caller and of the ONU, the optical
reading, the matching strategy and the router; every ONU row links to its detail
page and lists the PON-level subscribers. A single exact username hit redirects
straight to the user detail page, so the NOC flow is: type the name → press
Enter → see everything.

### 3.6 Device vendor lookup (offline OUI database)

A PPPoE `caller-id` (the customer's router) and an ONU MAC are resolved to the
manufacturer name (`Huawei`, `Cisco`, `Netis`, `TP-Link`, …) with a bundled
IEEE OUI database: `olt_monitor/data/oui.json.gz` (~54,000 MA-L/MA-M/MA-S
prefixes, loaded lazily, no network access at runtime). The lookup order is:

1. the exact MAC prefix in the bundled database;
2. an optional local override you can drop into `data/oui.csv`,
   `data/oui.txt` or `data/oui.json` (same install only — never shipped);
3. the vendor/model string the ONU itself reported over SNMP/Telnet.

An unknown or locally-administered MAC (second bit of the first octet set) shows
`-` rather than a wrong name. The vendor badge appears on the Users, User detail,
ONU detail, PON detail and search pages.

---

## 4. Quick start (Ubuntu / Debian + MySQL)

**Requirements:** Python **3.10+** and **MySQL 8** (MariaDB 10.6+ also works).
Nothing needs compiling; the MySQL driver (`pymysql`) is pure Python.

```bash
cd ~/olt-monitor-router

# 1. one-time: python venv + dependencies + database + service
./install.sh

#    ...or step by step:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env          # DB_* + ROUTER_* + OLT_*
.venv/bin/python tools/setup_mysql.py      # creates the DB, user and tables
.venv/bin/python run.py --check            # test every configured device
.venv/bin/python run.py                    # foreground (Ctrl+C stops it)
```

Dashboard → **http://localhost:5002**. On the very first start the web UI sends
you to **`/setup`** to create the super-admin and choose the site name, web port and
MySQL target.

### Web login, first-run setup and roles

**Every monitoring page requires a login.** Only `/about` and `/login` are public,
plus `/api/health` for uptime probes. The first visit to `/setup` creates the first
**super admin** and stores the site settings:

* site name + tagline (navbar, title, About page);
* web port → written to `.env` as `APP_PORT` (restart required);
* super-admin username + password (min 8 characters, scrypt hashed);
* optional remote **MySQL** host / port / database / user / password, with a
  **Test connection** button. Leave blank to keep the current database.

The wizard disappears once an account exists. A session is an HMAC-signed
`olt_session` cookie (7 days, `HttpOnly`, `SameSite=Lax`); the user row is re-read
on every request, so disabling an account or changing its role applies on the next
click. Logging out and changing a password invalidate old cookies.

| Role | Baseline access |
|---|---|
| **viewer** | read every monitoring page + the read-only SNMP Tools page |
| **manager** | viewer + add / edit / disable / delete devices, test SNMP/Telnet/API, poll now |
| **superadmin** | everything; bypasses every permission flag (cannot be locked out) |

Every account also has **six independent permission switches**, so a role is only
the starting point. Open **Settings → Web users** and press **Permissions** on a
row (or set them while creating the account):

| Flag | Grants |
|---|---|
| `perm_devices` | add / edit / test / enable / delete OLTs and routers, poll now |
| `perm_tools` | run read-only SNMP queries from the Tools page |
| `perm_settings` | site settings and the database target |
| `perm_users` | create / edit / disable / delete web accounts |
| `perm_developer` | open the Developer page and build the distribution ZIP |
| `perm_sql` | download / import the site config and SQL dumps |

A flag only adds capability; monitoring pages stay read-only without
`perm_devices`. Defaults: viewer = `perm_tools`, manager = `perm_devices +
perm_tools`, super admin = all (and cannot be reduced). The last active super
admin can never be disabled or demoted. The same flags are enforced **in the
nav bar, the routes and the middleware**, not just by hiding buttons.

Create accounts at **Settings → Web users** (`/admin/users`). The system refuses to
leave zero active super admins. Forgot the password? On the server:

```bash
.venv/bin/python tools/reset_admin.py --user admin
```

Other admin building blocks:

* **Settings → Site** — site name, tagline, About text, footer, support email,
  refresh interval.
* **Settings → Database** — active MySQL target (password never shown) + remote
  connection form with **Test connection**; writes `.env` atomically (`.env.bak`).
* **Settings → Backup** — **Download site config** (devices + credentials, site
  settings and web users only — no ONU/PPPoE/dBm/history data) and import it on
  this or another installation. An optional **Advanced** section still offers a
  full SQL dump (current state or all history), SQL import and the list of
  backups in `backups/`.
* **Developer** (`/developer`) — download a complete, ready-to-deploy source ZIP,
  browse every shipped file, read the deployment/add-OLT/update guides and the
  audit log. The ZIP never contains `.env`, `config/config.yaml`,
  `config/optical_oids.yaml`, `data/`, `logs/` or `backups/`.
* **About** (`/about`) — public project/feature/credits page (developed by
  **Johirxofficial**, assisted by an AI agent).

Security defaults: passwords use stdlib `scrypt`, login is rate-limited (8 failures
per IP+user → 5-minute pause), unsafe requests are rejected cross-origin, pages are
`no-store`, and admin areas are enforced centrally by middleware.

### Linux helper scripts

| file | what it does |
| --- | --- |
| `./install.sh` | venv + dependencies + MySQL setup + **systemd --user** service (auto-start at login) |
| `./install.sh --migrate` | additionally imports an old `data/olt_monitor.db` into MySQL |
| `./start.sh` | run in this terminal (Ctrl+C stops it); passes `--check`, `--no-poll`, `--port` through |
| `./start-bg.sh` | start in the background — delegates to `systemctl --user start` when the unit exists, otherwise `nohup` + `logs/console.{out,err}` |
| `./stop.sh` | stop the background process **or** the systemd --user unit |
| `./status.sh` | process, `systemctl --user` state, `/api/health`, MySQL target, last log lines |
| `systemctl --user status olt-monitor-router` | service state / live log (`journalctl --user -u olt-monitor-router -f`) |

`install.sh` writes `~/.config/systemd/user/olt-monitor-router.service`; it runs
without root and starts at login. To keep it running after logout:

```bash
sudo loginctl enable-linger $USER
```

A system-wide unit template is also in `deploy/olt-monitor-router.service`.

### Database (MySQL)

Everything is stored in MySQL: the device registry, current OLT/router/ONU/user
snapshots, alerts, the forwarding-table cache (`olt_mac_cache`) and every history
table used by the graphs. The schema lives in `olt_monitor/db.py` and is
created/upgraded automatically by `init_db()`.

History is retained for **24 hours** (`database.keep_history_days: 1`). A
`history-maintenance` thread sweeps once at startup and then every
`database.prune_interval_seconds` (default 300 s), deleting older rows and
inactive alerts. The `ts` columns are indexed specifically for that delete.

`tools/setup_mysql.py` creates **only** the database named in `config.yaml`
(default `olt_monitor`) plus its `<name>_selftest` companion, an application user
with privileges on those two databases only, and the tables. It never touches
other databases on the server — `ISP-MikroTik-Billing` / `mikrotik_billing` and
the `mikrotik-api` database are completely separate.

`tools/selftest.py` runs against `<name>_selftest`, wipes it first, and refuses
to run if it is somehow pointed at the live database.

Old SQLite data is not lost — import it once:

```bash
# small tables only (devices, current snapshots, alerts)
.venv/bin/python tools/migrate_sqlite_to_mysql.py

# everything, including history (a few million rows; ~1-6 minutes)
.venv/bin/python tools/migrate_sqlite_to_mysql.py --all
```

The import is idempotent (`ON DUPLICATE KEY UPDATE`), so it can be re-run.
`data/.secret_key` stays with the install: device secrets are copied already
encrypted and keep working.

### Windows

The `.bat` wrappers are still in the tree, but MySQL must be installed and
`DB_*` set in `.env` — there is no SQLite fallback any more. On Linux/macOS always
use the `.sh` scripts.

### `.env`

Secrets never live in `config.yaml` — it references them as `${VAR}`.

```ini
# --- MikroTik RouterOS API ---
ROUTER_HOST=198.51.100.1
ROUTER_API_PORT=1122
ROUTER_USER=monitor
ROUTER_PASSWORD=yourpassword

# --- OLT SNMP ---
OLT_HOST=192.0.2.10
OLT_COMMUNITY=your-community
```

### MikroTik side (must be done once on the router)

```routeros
/ip service enable api
/user add name=monitor group=read password=strong-password
```

A user in the **`read`** group is enough — the monitor never writes anything.
The API listens on **8728** by default; set `ROUTER_API_PORT` (or `api_port:`
in `config.yaml`) if your router uses another port.

---

## 5. One device or many — and managing them from the web

**The browser is the source of truth.** Open **Devices** and use *Add OLT* /
*Add router*; each row has **Test**, **Edit**, **Enable/Disable** and **Delete**.
A change is written to the `devices` table and the affected poller is restarted
immediately, so nothing else is interrupted and there is no need to restart the
service or edit a file.

Every enabled device is polled automatically on a **10-minute (`600 s`)** cycle.
The **Poll all now** button queues an immediate full refresh of every device; a
device that is already being polled is skipped, so pressing it during a cycle
can never start two concurrent walks. While it runs, the page shows
`polling N device(s)` and reloads when the refresh has finished. History from
those polls is kept for **24 hours**.

Everything below is therefore only the **first-run seed**: on a brand-new database
the registry is filled once from `config.yaml`, and after that the file no longer
decides which devices exist (it still provides app, polling, alert and OID settings).

The singular form is what ships by default and keeps working untouched:

```yaml
olt:
  host: "${OLT_HOST:192.0.2.10}"
  community: "${OLT_COMMUNITY:your-community}"
router:
  host: "${ROUTER_HOST:198.51.100.1}"
  api_port: "${ROUTER_API_PORT:8728}"
```

To seed several devices at once, use the plural keys. Every entry is merged over
the singular block and the built-in defaults, so you only list what differs — and
each device needs a unique `name`, because that name ties every database row to
its device.

```yaml
olts:
  - name: OLT-Core
    host: 192.0.2.10
    community: your-community
  - name: OLT-Branch-2
    host: 192.0.2.11
    community: your-community

routers:
  - name: MikroTik-Core
    host: 198.51.100.1
    api_port: 8728
    username: monitor
    password: "${ROUTER_PASSWORD}"
  - name: MikroTik-Branch
    host: 10.0.0.10
    api_port: 8728
```

`python run.py --check` reports each registered device separately, and every page
gains an OLT / router filter. Set `enabled: false` on an entry (or use the
*Disable* button) to keep it configured but idle.

### 5.1 What the Devices page can change

| Field | OLT | Router |
|---|---|---|
| `name`, `host`, `enabled` | ✅ | ✅ |
| `interval_seconds` (per device, overrides the global interval) | ✅ | ✅ |
| `timeout_seconds` | ✅ | ✅ |
| `branch`, `site`, `rack`, `display_order` | ✅ | – |
| `snmp_port`, `snmp_version`, `community`, `retries` | ✅ | – |
| `brand`, `pon_type`, `firmware`, `model`, `max_onus_per_pon` | ✅ | – |
| `telnet_enabled`, `telnet_port`, `telnet_username`, `telnet_password`, `telnet_enable_password`, `telnet_profile`, `telnet_interval_seconds`, `telnet_timeout_seconds`, `telnet_detail_max` | ✅ | – |
| `cdata_api_enabled`, `cdata_api_port`, `cdata_api_tls`, `cdata_api_username`, `cdata_api_password`, `cdata_api_interval_seconds`, `cdata_api_timeout_seconds`, `cdata_api_optical`, `cdata_api_rates`, `cdata_api_mac` | ✅ | – |
| `api_port`, `username`, `password` | – | ✅ |
| `uplink_interfaces` (for throughput/SFP) | – | ✅ |
| `use_tls`, `verify_tls`, `sfp_monitor`, `monitor_traffic`, `pppoe`, `dhcp_leases` | – | ✅ |

**Secret fields** (`community`, `password`, `telnet_password`,
`telnet_enable_password`, `cdata_api_password`) are **never sent back to the
browser** — the form shows
*"set"* and leaving the box empty on an edit keeps the stored value. They are
stored **encrypted** (see §3.4). `display_order` is the position in the OLT table
and on the Overview page. Firmware-specific optical OIDs stay in
`config/optical_oids.yaml`, and the tunables (`classify`, `oids`, `mac_table`) stay
in `config/config.yaml` — those are deliberately not editable from a web form.

### 5.2 Grouping: PON → ONU → user

```
PONs                     /pons      one row per PON of every OLT
 └─ PON detail           /pon?olt=OLT-1&pon=2
     ├─ ONUs on the PON  (with dBm, state, distance, linked user)
     └─ PPPoE users behind that PON
 └─ ONU detail           /onu?olt=OLT-1&ifindex=26
     ├─ optical + temperature + distance + traffic
     ├─ matched PPPoE session(s)
     └─ 24 h optical chart
 └─ User detail          /user?router=MikroTik&name=SomeUser
     ├─ MAC (caller-id), remote IP, uptime, session id, in/out bytes
     └─ the ONU behind the user + its dBm
```

---

## 6. Per-user optical power (dBm) — how it works

Two independent mechanisms are involved.

### 6.1 The ONU optical tables

The ONU optical values live in the vendor's private MIB. On the V1600D they are:

| Value | OID (before the index) | Raw format |
|---|---|---|
| ONU **Rx** power | `1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.7.<pon>.<onu>` | text: `"0.04 mW (-14.27 dBm)"` |
| ONU **Tx** power | `1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.6.<pon>.<onu>` | text: `"1.92 mW (2.84 dBm)"` |
| temperature | `1.3.6.1.4.1.37950.1.1.5.12.1.12.1.4.<row>` | int °C |
| distance | `1.3.6.1.4.1.37950.1.1.5.12.1.12.1.13.<row>` | int metres |
| MAC | `1.3.6.1.4.1.37950.1.1.5.12.1.12.1.6.<row>` | `"00:d3:9e:8b:30:1a"` |

**These are shipped, verified, in `config/optical_oids.yaml`**, which is merged over
`olt.onu_tables` at load time. So on this OLT **dBm works out of the box** —
no manual OID hunting.

Note the two different index shapes, which is the whole reason this used to be hard:

* the power table is indexed `<pon>.<onu>`;
* the inventory table (temperature/distance/MAC) is indexed by a **plain row number**,
  and it holds the PON and ONU id in *other columns* (`.2` and `.3`).

`optical_oids.yaml` therefore also carries an `index_map`:

```yaml
_default:
  index_map:
    pon_oid: '1.3.6.1.4.1.37950.1.1.5.12.1.12.1.2'
    onu_oid: '1.3.6.1.4.1.37950.1.1.5.12.1.12.1.3'
  rx_power:    { oid: '1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.7', scale: 1.0 }
  tx_power:    { oid: '1.3.6.1.4.1.37950.1.1.5.12.2.1.8.1.6', scale: 1.0 }
  temperature: { oid: '1.3.6.1.4.1.37950.1.1.5.12.1.12.1.4',  scale: 1.0 }
  distance:    { oid: '1.3.6.1.4.1.37950.1.1.5.12.1.12.1.13', scale: 1.0 }
  mac:         { oid: '1.3.6.1.4.1.37950.1.1.5.12.1.12.1.6',  scale: 1.0 }
```

For each row the collector tries, in order:

1. `key_oid` — the row value *is* the ONU interface name;
2. `index_map` — the row number maps to (PON, ONU id);
3. the index itself is the interface index;
4. the last two sub-identifiers are (PON, ONU id);
5. the last sub-identifier is the interface index.

Values may be integers (`scale` = `0.01` when the device reports dBm × 100) **or
text** — when the text contains a `dBm` figure, that figure is used as-is.

#### Built-in BDCOM P3608B EPON (no OID discovery)

The BDCOM P3608B EPON OLTs (`OLT_1`..`OLT_3` here) have a restricted `support`
CLI account, and Telnet is disabled on two of them. Their telemetry is therefore
read entirely over SNMP. The collector ships this profile as
`("bdcom", "epon")`:

| Value | OID (before the ONU ifIndex) | Scale |
|---|---|---|
| ONU Rx power | `1.3.6.1.4.1.3320.101.10.5.1.5` | `0.1` dBm |
| ONU Tx power | `1.3.6.1.4.1.3320.101.10.5.1.6` | `0.1` dBm |
| temperature | `1.3.6.1.4.1.3320.101.10.5.1.2` | `1/256` C |
| voltage | `1.3.6.1.4.1.3320.101.10.5.1.3` | `0.0001` V |
| bias current | `1.3.6.1.4.1.3320.101.10.5.1.4` | `0.002` mA |
| ONU MAC | `1.3.6.1.4.1.3320.101.10.1.1.3` | bytes |
| model | `1.3.6.1.4.1.3320.101.10.1.1.2` | text |
| vendor | `1.3.6.1.4.1.3320.101.10.1.1.1` | text |

PON SFP TX and temperature use `.3320.101.107.1.3` (`0.1`) and
`.3320.101.107.1.7` (`0.001`). The OLT-side table `.3320.101.108.1.3` is not
polled: on this firmware it can take 80+ seconds and return zero rows.

The profile is chosen automatically when an OLT is added as `auto` (detected
from `sysDescr`) or explicitly as `bdcom` + `epon`. No per-device `onu_tables`
block, OID update, or code edit is needed. EPON ONUs have no GPON serial number;
their stable identity is the MAC shown in the ONU list. A vendor sentinel such
as `INT32_MIN` in a power column is discarded rather than displayed as
`-214748364.8 dBm`.

### 6.2 A different OLT or firmware

Run the auto-detector. It walks the vendor tree, analyses every column, and writes a
per-device block into `config/optical_oids.yaml`:

```bash
python3 tools/discover_olt.py --autoconfig --dry-run   # show, do not write
python3 tools/discover_olt.py --autoconfig             # write it
python3 tools/discover_olt.py --autoconfig --olt OLT-Branch-2
```

It understands both index shapes (it derives the `index_map` itself), reads dBm out
of text columns, merges tables that are published one-PON-at-a-time, and rejects
"row counter" columns that look deceptively like a dBm reading. It is **additive**:
a field that is already configured is never overwritten unless you pass `--force`.

The vendor tree on this OLT is larger than 60 000 varbinds, and a truncated walk is
worse than useless — it "finds" the wrong columns. So if the walk hits `--limit`,
the tool **refuses to write** and tells you to either raise the limit or narrow the
walk:

```bash
python3 tools/discover_olt.py --root 1.3.6.1.4.1.37950.1.1.5.12 --autoconfig --dry-run
python3 tools/discover_olt.py --autoconfig --limit 300000
```

If detection still fails, look at the raw data and fill the block in by hand:

```bash
python3 tools/discover_olt.py --columns 1.3.6.1.4.1.37950 --out olt_walk.txt
python3 tools/discover_olt.py --get 1.3.6.1.2.1.1.1.0
```

There is also an offline self-test of the whole detection + parsing path — it needs
no OLT and no database:

```bash
python3 tools/selftest.py        # exit code 0 = everything matched
```

### 6.3 Per-user dBm — how a user gets an ONU's reading

Measurement only happens on the ONU. The join is done by `correlate.py` (next
section); once a user is linked, the Users page shows that ONU's dBm, temperature
and distance directly.

**Note on poll cost:** a full OLT poll is expensive (~80 s here — the optical walks
plus the bridge MAC table). Every device therefore runs on a **600 s (10 minute)**
cycle, and the MAC-table walk (the most expensive part) still runs only every
`olt.mac_table.every_n_polls` polls (default 5), cached in between. For fresh
data on demand use **Devices → Poll all now**, which shares the active-device
guard with the scheduled cycle (no duplicate concurrent walks).

---

## 7. How user ↔ ONU matching works

Tried in order; each can be disabled in `config.yaml` → `correlate`:

| # | Strategy | What it compares | Config key |
|---|---|---|---|
| 1 | **`mac`** | PPPoE `caller-id` MAC **==** the ONU's own MAC (exact) | `match_by_mac` |
| 2 | **`fdb`** | the PPPoE `caller-id` MAC looked up in the OLT subscriber/bridge forwarding table | `match_by_mac_table` |
| 3 | `arp` | user IP → ARP/DHCP MAC → FDB / ONU MAC | `match_by_arp` |
| 4 | `template` | e.g. `onu_name_template: "pppoe-{user}"` | `onu_name_template` |
| 5 | `contains` | the username appears inside the ONU interface name | `match_by_name_contains` |

**Why both `mac` and `fdb` are needed:** a MikroTik PPPoE `caller-id` is the
**customer's router MAC**. On GPON the ONU usually bridges that router
transparently, so the `caller-id` is also the MAC the ONU reports — an exact
match. On EPON the `caller-id` is normally the customer router while the ONU has
its own MAC, so only the forwarding table can tell which ONU the router sits
behind. The collector therefore tries the exact ONU MAC first, then the FDB.

For a VSOL GPON OLT the collector does not guess from the bridge port. It walks
the vendor table's **MAC column and `PON:ONU` port column in one interleaved
GETBULK snapshot**, resolves each `PON:ONU` to that ONU's ifIndex, and merges
that exact map **last**, so it overrides any bridge-FDB guess. The snapshot is
also cached (`data/fdb_<olt>.json`) and survives a restart. GPON bridge-port →
ifIndex identity fallback is deliberately disabled; EPON keeps it because that
numbering is valid there.

On this network the combination took the match rate from **0 to 300+ users**
and removed the wrong-ONU attributions caused by bridge-port guessing.

Configure it under `olt`:

```yaml
olt:
  mac_table:
    enabled: true
    every_n_polls: 5                       # walk frequency (it is expensive)
    sources: ["dot1d", "dot1q"]
    vendor_oid: null                       # optional vendor MAC->port table
```

Probe what your OLT exposes:

```bash
python3 tools/discover_olt.py --mac-table
```

The Users page shows **which** strategy matched each session, so the reliability of
the data is always visible.

### 7.1 PON-level fallback (Photon / BDPON / BTPON firmware)

Some EPON firmware prints only the PON in its forwarding table:

```
OLT-4PON# show mac-address-table
MAC-ADDRESS        PORT
a4:dc:be:11:22:33  pon1
00:18:93:aa:bb:cc  pon2
```

There is no per-ONU port to resolve, so a strict ONU mapping would leave every
subscriber "not linked". The monitor therefore keeps a second, honest tier:

* a MAC whose FDB port is `ponN` is linked to **that PON** (`pppoe_user_current.onu_pon`, `match_method = 'pon'`);
* if that PON has **exactly one known ONU**, the link is **promoted to that ONU**
  (the common single-customer-PON case);
* if the PON has several ONUs, the user stays PON-level — the dashboard shows a
  **PON** badge and the user detail page shows the PON card instead of inventing
  an ONU;
* an FDB entry for a PON with **no known ONU** is deliberately left unmatched.

PON-level links are counted everywhere an ONU link is counted (dashboard
"matched" total, PON subscriber counts, ONU detail "sessions linked to this
PON", quick search). This is why a Photon OLT with 136 ONUs can show hundreds of
linked subscribers even though only one ONU MAC ever matched directly.

The ONU list makes that visible instead of printing a dash: every ONU of a
PON with PON-level subscribers shows **`ponN · N users`** in the *PPPoE user*
column and the PON's MikroTik interface (for example `Rony_Cable_C-ghat_1380`)
in the *MikroTik iface* column. Clicking the count opens the PON, which lists
each subscriber with its IP, caller MAC, vendor, optical power and MikroTik
interface. The **has PPPoE** filter matches PON-level links too, and a blue
banner on the ONUs page states how many subscribers are linked this way.

Configure the number of PONs to probe when SNMP exposes none on the Devices form
(`pon_count`); the collector otherwise derives it from the discovered ports.

---

## 8. Running modes

| Command | What it does |
|---|---|
| `python run.py` | poll every device + serve the dashboard |
| `python run.py --once` | poll once, store, exit (cron / Task Scheduler) |
| `python run.py --once --only olt` | poll only the OLTs (`router` for the routers) |
| `python run.py --check` | connectivity test with a readable per-device report |
| `python run.py --no-poll` | web UI only, read-only against an existing database |
| `python run.py --port 9000` | different web port |
| `python run.py --log-level DEBUG` | verbose logging (`logs/olt_monitor.log`) |

---

## 9. Pages & API

| Page | URL | Notes |
|---|---|---|
| Dashboard | `/` | summary cards, quick-search box, PON table, uplinks, alerts, power chart |
| **Search** | `/search?q=rahim` | one box for username / IP / MAC / serial / model / VLAN / PON |
| **OLT Overview** | `/olts` | health gauge + CPU/RAM/temp/RTT per OLT, **Poll now**, multi-OLT table |
| **Devices** | `/devices` | **add / edit / test / enable / delete OLTs and routers** |
| PONs | `/pons?olt=OLT-Core` | PON state, ONU capacity bar, live Mbps, SFP TX/RX/temp, subscribers |
| PON detail | `/pon?olt=OLT-Core&pon=3` | ONUs on the PON + the users behind it |
| ONU detail | `/onu?olt=OLT-Core&ifindex=33` | serial/model, VLAN, optical, temp/volt/bias, distance, last down, 24 h chart |
| User detail | `/user?router=MikroTik&name=SomeUser` | MAC, remote IP, uptime, bytes + the ONU behind the user |
| ONUs | `/onus?olt=OLT-Core&pon=3&status=weak&q=ali` | summary bar, filters (`up`/`down`/`weak`/`pppoe`), search, sort |
| Users | `/users?matched=yes&router=MikroTik` | PPPoE users with dBm + uptime + caller/ONU vendor |
| Routers | `/routers` | per-router health, CPU, memory, PPPoE count |
| Router detail | `/router?name=MikroTik` | interfaces, SFP optics, PPPoE, DHCP leases |
| Ports | `/ports` | uplink, PON, VLAN, counters |
| Alerts | `/alerts` | active alerts by severity |
| SNMP Tools | `/tools?olt=OLT-Core&oid=1.3.6.1.4.1.37950&walk=1` | live SNMP against a chosen OLT |
| **Settings** | `/settings` | site, web port, MySQL, SQL backup/import, runtime status |
| **Web users** | `/admin/users` | accounts + roles + **per-account permission switches** + password resets |
| **Developer** | `/developer` | source bundle, file browser, guides, audit log |
| **About** | `/about` | public project page (no login) |
| Login / setup | `/login`, `/setup` | sign in / first-run wizard |
| Account | `/account` | own display name, email, password |

JSON API — handy for Grafana / Zabbix / Home Assistant / a mobile app:

```
/api/health        /api/summary      /api/devices      /api/alerts
/api/olts          /api/onus         /api/users        /api/ports
/api/health/OLT-Core  (per-OLT health row)                /api/router-interfaces
/api/onu-power     /api/snmp?oid=...&walk=1
/api/pons          /api/onus/{olt}/{ifindex}          /api/user/{router}/{name}
/api/trend/onu/{ifindex}   /api/trend/port/{ifindex}   /api/trend/pppoe
/api/poll-status   (which devices are being polled right now)
```

`/onus` and `/users` return at most 200 rows per page and accept `page=N`; the
JSON endpoints still accept an explicit `limit`.

Managing devices over HTTP (what the Devices page calls):

```
GET    /api/registry                 -> { olts: [...], routers: [...] }
POST   /api/registry/olt             -> add an OLT      (JSON body = its fields)
POST   /api/registry/router          -> add a router
PUT    /api/registry/{id}            -> edit one field or all of them
POST   /api/registry/{id}/toggle     -> {"enabled": false} or toggle
POST   /api/registry/{id}/test       -> live SNMP / API connectivity probe
POST   /api/registry/{id}/test-telnet-> live Telnet CLI probe (login + `show version`)
POST   /api/registry/{id}/test-api   -> live C-Data HTTP API probe (login + identity + ONU count)
POST   /api/olts/{name}/poll         -> poll that OLT right now
POST   /api/poll-all?kind=all|olt|router -> full refresh of every enabled device
GET    /api/poll-status              -> {"active": ["olt:...","router:..."], "count": N}
DELETE /api/registry/{id}            -> remove it and everything collected for it
```

Every one of those applies **immediately** — the affected poller is restarted in the
background and the page never goes down.

Authentication is enforced for every route by middleware in
`olt_monitor/web/app.py`:

* no session cookie → `303` to `/login?next=…` (JSON API → `401`);
* viewer + any `POST`/`PUT`/`PATCH`/`DELETE` → `403`;
* non-admin on `/settings`, `/admin`, `/developer`, `/api/admin`, `/api/sql` → `403`;
* cross-origin unsafe request → `403`;
* no accounts yet → everything redirects to `/setup`, except `/about`.

Admin-only helpers:

```
POST /api/admin/db/test        -> test a MySQL target (super admin)
POST /api/setup/test-db        -> same, available only during first-run setup
GET  /settings/export-config   -> config-only site JSON (devices, settings, users)
POST /settings/import-config   -> multipart JSON + confirm=IMPORT
GET  /settings/export?history=0|1
POST /settings/import          -> multipart SQL/file + confirm=IMPORT
GET  /developer/bundle.zip     -> the full source bundle
```

---

## 10. Deploying on the Ubuntu server

The reference deployment: web UI on **port 5002**, MikroTik API on **8728**,
everything stored in **MySQL 8**.

```bash
cd ~/olt-monitor-router

# 1. dependencies + virtual environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. credentials (DB_* + ROUTER_* + OLT_*); .env is git-ignored
cp .env.example .env && nano .env

# 3. one-time database + user + schema (needs a MySQL admin account)
.venv/bin/python tools/setup_mysql.py
#    or non-interactive:
#    .venv/bin/python tools/setup_mysql.py --admin-password '...'

# 4. verify every configured device
.venv/bin/python run.py --check

# 5. start in the foreground once (Ctrl+C stops it)
.venv/bin/python run.py
```

The web port comes from `.env` (`APP_PORT`, default **5002**); change it later in
**Settings → Site** and restart the service.

Open **http://&lt;server-ip&gt;:5002/setup** once to create the super admin, then add
every OLT and router on **/devices** — after the first run you never need to edit a
file to add a device.

### Run it as a service

With `.env` filled in, `./install.sh` does the venv, the MySQL setup (steps 1 and
3) and installs a **systemd --user** unit, so it needs no `sudo`:

```bash
./install.sh                       # venv + MySQL + service, enabled at login
systemctl --user status olt-monitor-router
journalctl --user -u olt-monitor-router -f      # live log
systemctl --user restart olt-monitor-router     # after a config change
```

The unit is written to `~/.config/systemd/user/olt-monitor-router.service` and
starts at login. To keep it running after you log out:

```bash
sudo loginctl enable-linger $USER
```

A system-wide unit template is in `deploy/olt-monitor-router.service`
(`/etc/systemd/system/`) if the server is managed with root units.

### Backups

**Recommended — config-only site backup.** Open **Settings → Backup** and press
**Download site config (.json)**. The file contains the full site setup:

* every OLT/router with its credentials (decrypted for portability);
* all editable site settings;
* every web user with role, permission flags and password hash.

It contains **no** ONU inventory, optical dBm, PPPoE sessions, router signal
readings, forwarding tables or history. Keep the file private — it includes
device passwords and user password hashes. On import, device secrets are
re-encrypted with the target server's `data/.secret_key`, collected tables are
cleared, and the normal 10-minute polling refills the data. This is the easiest
way to copy a complete setup to a new server. Server-specific `.env` values
(MySQL password, listening port) are not in the file.

**Full database dump — advanced / disaster recovery.** A raw dump also contains
live monitoring rows and needs the matching keys:

```bash
mysqldump --single-transaction -u olt_monitor -p olt_monitor > olt_monitor.sql
cp data/.secret_key ~/backups/olt-monitor.secret_key
cp .env ~/backups/olt-monitor.env
```

`data/.secret_key` decrypts the communities/passwords stored in the database,
and `.env` holds the MySQL password. Re-import with:

```bash
mysql -u olt_monitor -p olt_monitor < olt_monitor.sql
```

The same dump can be restored from **Settings → Backup → Advanced → Import SQL**.
A config-only export does not need the old `.secret_key`; a SQL dump does.

`data/olt_monitor.db` (the old SQLite file) is **not** used at runtime; it is only
the import source for `tools/migrate_sqlite_to_mysql.py`.

### Distribution: build the ZIP, upload to GitHub, install elsewhere

The **Developer** page builds the same archive as the CLI, so you can ship the
project without ever exposing this site's credentials:

```bash
# from the project directory
.venv/bin/python tools/build_bundle.py --out /tmp/olt-monitor-router.zip
sha256sum /tmp/olt-monitor-router.zip
```

or click **Developer → Download source bundle** (`/developer/bundle.zip`).

**Always included:** `run.py`, `requirements.txt`, `README.md`, `docs/`,
`install.sh` + the other scripts, `config/config.example.yaml`, the whole
`olt_monitor/` package (including the offline vendor database
`olt_monitor/data/oui.json.gz`), `tools/`, `deploy/`.

**Never included (by design):**

| Excluded | Why |
|---|---|
| `.env`, `.env.bak` | real MySQL / router / SNMP passwords |
| `config/config.yaml` | this site's device seeds and OIDs |
| `config/optical_oids.yaml` | OIDs discovered on *these* OLTs |
| top-level `data/`, `logs/`, `backups/` | encryption/session keys, legacy FDB cache, local logs and backups |
| `.venv/`, `__pycache__/`, `*.pyc`, `*.zip` | rebuildable / local state |

The builder refuses to finish if a forbidden path leaks in or a required file
(the OUI database, templates, docs) is missing, and prints the file count,
compressed size and version.

Upload the ZIP plus this `README.md` to GitHub (Releases is the simplest place
for the ZIP; the README renders on the repository home page):

```bash
git init
git add README.md docs/ olt_monitor/ tools/ config/config.example.yaml \
        run.py requirements.txt *.sh .env.example .gitignore deploy/
git commit -m "olt-monitor-router v3.5.1"
git branch -M main
git remote add origin git@github.com:<you>/olt-monitor-router.git
git push -u origin main
```

Then, on the new server:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip mysql-server unzip
unzip olt-monitor-router.zip -d ~/olt-monitor-router
cd ~/olt-monitor-router
./install.sh                 # run twice: first creates .env, second finishes
nano .env                    # DB_* (+ optional first OLT/router seed)
.venv/bin/python tools/setup_mysql.py
./start.sh                   # or ./install.sh for the systemd --user unit
```

Open `http://<server-ip>:5002/setup`, create the first super admin, then add
every device on **/devices**. A fresh install deliberately starts with **no**
inherited devices or secrets.

### ⚠️ Updating an existing install

The config loader, the database schema and the config file all changed together.
**Copy the whole project over your existing directory** — do not copy a single file:

* **keep** your own `.env` and your own `config/config.yaml` edits;
* `olt_monitor/config.py` (new) must land *before* a new `config.yaml` is used —
  the old loader does not understand `${VAR:default}`;
* `config/optical_oids.yaml` is new and required for dBm;
* on first start `db.py` runs its `MIGRATIONS` against MySQL, adding columns such
  as `pppoe_user_current.onu_olt_name` and the supporting indexes — the existing
  database is upgraded in place, not lost (no SQLite file is created any more);
* the new `devices` table is created and **seeded once from `config.yaml`**, so an
  old `olt:` / `router:` (or `olts:` / `routers:`) block keeps working exactly as
  before — nothing to migrate by hand;
* `olt_onu_current` / `olt_port_current` gain the vendor columns (`serial`, `model`,
  `vlan`, `voltage_v`, `bias_current_ma`, `last_down_reason`, `pon_type`,
  `max_onus`, `sfp_*`) and the new `olt_health_current` / `olt_health_history`
  tables are created — again, in place;
* the **C-Data HTTP API** release adds `olt_onu_current.vendor` and
  `olt_onu_current.sw_version`, plus nine `cdata_api_*` device fields (all
  optional, off by default) — no action needed on an existing database;
* the **PON-level / permissions** release adds `pppoe_user_current.onu_pon`, the
  six `users.perm_*` flags (one-time `permissions_v1` migration defaults them for
  every existing account) and `olt_onu_current.vendor`; `init_db()` applies them
  in place on first start;
* **3.5.2 is a vendor-profile fix release** — Generic GPON / Generic EPON /
  Auto devices are refined to their real vendor from `sysDescr`, `sysObjectID`
  and interface names, so a VSOL chassis saved as Generic GPON gets its
  optical/inventory/subscriber tables (`Choyani_DBC_GPON` now shows dBm, users
  and uptime). No database schema change is required.
* **3.5.1 is a collection fix release** — BDCOM/VSOL `auto-configured` ONUs
  count as online, the Test button falls back to Telnet/HTTP API, the brand
  list includes HUAWEI/TBS/HSGQ/Generic GPON, and a failed poll preserves the
  last good payload. No database schema change is required.
* **3.5.0 is a backup/UI release** — the Settings backup is now a **config-only
  JSON export** (devices + credentials, site settings, web users) with no
  monitoring rows. Import replaces that configuration, clears the collected
  tables and lets polling refill them. The SQL dump moved under **Advanced**.
  No database schema change is required; `init_db()` is unchanged.
* **3.4.1 is a code/UI release** — `init_db()` only adds the new
  `olt_mac_cache` table and a few indexes, in place. The forwarding-table cache
  moves from `data/fdb_<olt>.json` into MySQL; the old file is read once as a
  fallback and then no longer needed. The first full poll may take one extra
  MAC-table walk while that cache is rebuilt.
* **Polling defaults changed to 600 s** for every device. Existing rows keep
  whatever `interval_seconds` they already store; set the field on the Devices
  page (or run `UPDATE devices SET config=JSON_SET(config,'$.interval_seconds',600);`)
  if you want an old install on the same 10-minute cycle.
* **History retention is now 24 hours** (`keep_history_days: 1`). If your
  `config.yaml` still says `30`, it wins — change it to `1` to activate the
  cleanup. The first startup sweep deletes everything older than the window.
* `data/.secret_key` is created on first write. **Back it up with a SQL dump**:
  without it the sealed communities and passwords in that dump cannot be
  decrypted. A config-only export is independent of the key - it decrypts on
  export and re-encrypts on import.

Then restart and confirm:

```bash
systemctl --user restart olt-monitor-router     # or: ./stop.sh && ./start-bg.sh
curl -s localhost:5002/api/health
curl -s localhost:5002/api/registry
```

The `health` reply shows the MySQL target (never the password) in `"database"`.


---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `MySQL database 'olt_monitor' does not exist` | run the one-time setup: `.venv/bin/python tools/setup_mysql.py` (add `--admin-password` for a non-interactive run) |
| `cannot connect to MySQL ... (1045)` | wrong `DB_USER`/`DB_PASSWORD` in `.env`; re-run `tools/setup_mysql.py` to reset the app user's password |
| `ModuleNotFoundError: No module named 'pymysql'` | you are running the system `python3`, not the venv — use `./start.sh`, `./.venv/bin/python ...`, or re-run `./install.sh` |
| Service stopped after logout | `sudo loginctl enable-linger $USER` (the unit is per-user) |
| `port 5002 is already in use` | an old copy is still running: `./stop.sh`, or `ss -ltnp \| grep 5002` |
| `no SNMP response ... after N attempt(s)` | wrong community, or the OLT's SNMP ACL blocks this server's IP. Test with `tools/discover_olt.py --get 1.3.6.1.2.1.1.1.0` (a wrong community is silently dropped — no ICMP error) |
| OLT works but ONU dBm shows `n/a` | `config/optical_oids.yaml` missing, or the OIDs are wrong for the firmware — see §6.2 |
| ONU dBm worked, then a re-run broke it | do **not** use `--autoconfig --force`; plain `--autoconfig` never overwrites a configured field, and it refuses to write at all if the walk was truncated |
| `autoconfig` says "hit the N-varbind limit" | the vendor tree is bigger than the walk — narrow it (`--root 1.3.6.1.4.1.37950.1.1.5.12`) or raise `--limit` |
| `cannot connect to RouterOS` | `/ip service enable api`; check `ROUTER_HOST` and the configured RouterOS API port |
| `login rejected` | wrong password, or the user lacks the `api` policy |
| Users list empty but PPPoE is up | `/ppp/active/print` needs the `read` policy — check the log |
| Users page shows no In/Out | the router needs `read`; the collector already falls back from `.proplist` to `detail` to the `<pppoe-USER>` interface counters |
| `0 linked to an ONU` | the OLT has no bridge MAC table — check `tools/discover_olt.py --mac-table`, then set `olt.mac_table.vendor_oid` or use `onu_name_template` |
| Photon / BDPON / BTPON links users to **PON** instead of an ONU | expected: the CLI prints only `ponN` in the MAC table. With one ONU on that PON it is promoted automatically; with several it stays PON-level, so the ONU list shows `ponN · N users` and the PON's MikroTik interface on every ONU of that PON. See §7.1 |
| Many users "not linked" | expected when one router serves several OLTs — see §1 |
| Device vendor shows `-` | the MAC is unknown in the bundled OUI database or locally administered (e.g. a randomized phone MAC). Add `data/oui.csv` to override |
| Quick search finds nothing | the search reads the **current** snapshots — run a poll first (`/olts → Poll now`) and check the term is not too broad |
| Dashboard loads but everything is `n/a` | no poll has completed yet — run `python run.py --once` and watch the console |
| Charts do not render | the browser needs internet for the Chart.js / Bootstrap CDNs (or self-host them in `static/`) |
| CPU / RAM / board temp stay `n/a` | the OLT does not implement HOST-RESOURCES-MIB — enable **Telnet CLI** for that OLT (Devices → Edit → Telnet) |
| Telnet test fails with `login failed` | wrong username/password, or the CLI needs an **enable** password (fill the enable box) |
| Telnet logs in but CPU/RAM/temp stay `n/a` | the account is not privileged — VSOL's `support` user only has `show channel-group summary` / `show history` / `show ip rip`; use an admin account (`Hostname#`) |
| Telnet test fails with `timed out` | port 23 blocked, or `telnet_port` is not 23 on that firmware |
| A CLI command was refused | the read-only guard only allows `show` / `display`; anything that could change the device is dropped and logged |
| Brand shows *Auto-detect* | that is the state before the first poll — it is filled from `sysDescr` after one poll |
| C-Data OLT: SNMP is fine but **no ONUs / no dBm** | expected — its SNMP has no ONU table. Enable *OLT HTTP API* on the Devices page, then press **API** to test (`192.0.2.162`, user `admin`). With no API, enable Telnet (a `root`/admin account): the `cdata_epon` profile reads the ONT and MAC tables |
| C-Data: ONUs appear but PPPoE users stay unlinked | `show mac all` only names the PON, so the user page shows `ponN` instead of an ONU. Enable the HTTP API for per-ONU links, or accept the PON-level link (the user is still shown under the right OLT/PON) |
| C-Data OLT API says `login disabled after a failure` | the password was refused once, and the OLT locks an account after two tries. Fix the password on the Devices page; the next poll retries after 5 minutes (or press **API** to test now) |
| C-Data OLT: ONUs appear but `Ethernet`/uplink ports are `other` | the brand was `auto` at poll time — set `C-Data` + `EPON` and poll again (the `epon 0/1/1` port style is then classified as PON / uplink) |
| C-Data OLT: optical is slower than the poll interval | lower `cdata_api_interval_seconds` pressure by unticking *read ONU optical power* (a poll then takes ~1 s) or raise `interval_seconds` |
| OLT health is 0 but SNMP works | the device is **disabled** on the Devices page (a disabled OLT is not polled) |
| Community / password can no longer be decrypted | `data/.secret_key` was lost or restored from a different host — retype the secret on the Devices page |

Logs: `logs/olt_monitor.log` (rotating, 5 MB × 5).

---

## 12. Tuning knobs

In `config/config.yaml`:

* `polling.olt_interval_seconds` — **600 s (10 minutes)** by default for every
  OLT; a full poll is ~80 s here, so 600 s leaves ample headroom
* `polling.router_interval_seconds`, `polling.snmp_max_repetitions` (GETBULK chunk)
* `app.refresh_seconds` — page auto-refresh (default `120`; data changes every 10 min)
* `olt.mac_table.every_n_polls` — how often the forwarding table is re-read
* `database.keep_history_days` — **`1` = keep only the last 24 hours**
* `database.prune_interval_seconds` — how often the retention sweep runs (default `300`)
* `database.history_every_n_polls` — store 1 history row every N polls (default 5)
* `alerts.*` — every threshold used by the Alerts page
* `olt.classify.*` — the regexes that tell uplink / PON / ONU / VLAN apart
* `router.uplink_interfaces` — which interfaces get live bps + SFP readings

Per device you can also set `interval_seconds` on the Devices page: it overrides the
global interval for that one OLT or router (useful when a second OLT is much slower).
A newly added OLT defaults to **600 s** (`DEFAULT_OLT`); leaving the interval field
blank no longer means "use the global value".
For an OLT that also has Telnet enabled: `telnet_interval_seconds` (default 300 —
the CLI poll runs *inside* the OLT poll), `telnet_timeout_seconds` and
`telnet_detail_max` (how many per-ONU `show` commands may run per cycle).

For a C-Data OLT read over its HTTP API: `cdata_api_interval_seconds` (default
300 — how often the slow ONU/optical read runs), `cdata_api_timeout_seconds`
(12), and the three switches `cdata_api_optical` / `cdata_api_rates` /
`cdata_api_mac`. Turning optical off makes an API poll ~1 s instead of ~1 min,
which is the right setting for an OLT with 300+ ONUs on a metered uplink.
`cdata_api_mac` reads the per-PON forwarding table (`epon_mac_address_get`);
that table is what links a PPPoE `caller-id` to the ONU and also supplies the
service VLAN used to name the MikroTik VLAN interface on the Users page.

---

## 13. Project layout

```
olt-monitor-router/
├── run.py                     entry point (CLI + server)
├── requirements.txt           web layer + MySQL driver
├── .env.example               copy to .env
├── install.sh                 one-time Linux + MySQL setup, systemd service
├── start.sh / start-bg.sh     run in foreground / background (Linux)
├── stop.sh / status.sh        stop / inspect the running monitor (Linux)
├── start-bg.bat               run hidden in the background (Windows)
├── stop.bat / status.bat      stop / inspect the running monitor (Windows)
├── autostart.bat              start hidden at every logon (Windows)
├── config/
│   ├── config.example.yaml    shipped template (installer copies it on a new host)
│   ├── config.yaml            all settings, OIDs, thresholds (seeds the devices)
│   └── optical_oids.yaml      per-OLT optical OIDs (verified + auto-generated)
├── docs/
│   ├── INSTALL.md             full new-server installation guide
│   ├── DEVELOPER.md           file map, add-OLT and safe-update guide
│   └── USERS.md               roles + daily usage
├── olt_monitor/
│   ├── config.py              YAML + ${ENV:default} loading, device normalisation
│   ├── logging_setup.py       console + rotating file log
│   ├── db.py                  MySQL schema, migrations, connection helpers
│   ├── auth.py                users, roles, permission flags, scrypt passwords, sessions, audit
│   ├── site_settings.py       site name/tagline/About, .env editor, MySQL test
│   ├── sqladmin.py            SQL export / import / backup listing
│   ├── bundle.py              source-bundle builder (Developer page)
│   ├── registry.py            device registry (add/edit/delete OLTs & routers)
│   ├── secrets.py             ChaCha20 + HMAC envelope (encrypt at rest)
│   ├── oui.py                 offline IEEE OUI → vendor lookup
│   ├── vendors.py             per-brand profiles: patterns, ONU key, CLI commands
│   ├── cdata.py               C-Data OLT HTTP API client (ONU/optical/DMM/health)
│   ├── health.py              HOST-RESOURCES/vendor health reads + 0-100 score
│   ├── correlate.py           user ↔ ONU / PON matching (FDB / MAC / ARP / name)
│   ├── alerts.py              threshold evaluation
│   ├── scheduler.py           per-device threads, storage, global correlation
│   ├── data/oui.json.gz       bundled IEEE OUI database (ships in the ZIP)
│   ├── snmp/                  BER codec + v1/v2c client (stdlib only)
│   ├── mikrotik/api.py        RouterOS API protocol (stdlib only)
│   ├── telnet/
│   │   ├── client.py          read-only Telnet session (login, paging, safety)
│   │   ├── parsers.py         CLI text → CPU/RAM/temp/optics/down-reason
│   │   └── collector.py       runs the profile's commands for one OLT
│   ├── collectors/
│   │   ├── olt.py             interfaces, ONUs, optics, forwarding table, health
│   │   └── router.py          users, interfaces, SFP, traffic
│   └── web/
│       ├── app.py             FastAPI routes (pages + JSON API)
│       ├── templates/         Jinja2 + Bootstrap 5
│       └── static/            css + js (Chart.js, device manager, OLT overview)
├── tools/
│   ├── setup_mysql.py         create DB + app user + schema (+ selftest DB)
│   ├── migrate_sqlite_to_mysql.py  one-time import of an old SQLite file
│   ├── discover_olt.py        OID discovery, column analysis, autoconfig
│   ├── reset_admin.py         password recovery from the shell
│   ├── build_bundle.py        CLI wrapper around olt_monitor/bundle.py
│   ├── deploy_remote.sh       update a server from a bundle zip
│   └── selftest.py            offline self-test (uses <DB>_selftest)
├── deploy/olt-monitor-router.service  system-wide systemd unit (optional)
├── data/.secret_key           device-password key (0600) - back it up
├── data/.session_key          web-session signing key (0600)
├── backups/                   SQL exports/downloads (not in a bundle)
└── logs/olt_monitor.log       created at first run
```

---

## 14. Extending it

* **More devices** — §5; the schema is already keyed by `olt_name` / `router_name`.
* **Push to a NMS** — every table is plain MySQL; or poll the JSON API from
  Telegraf/Zabbix.
* **Traps** — `alerts.py` is deliberately isolated; hook a Telegram / e-mail /
  webhook sender in there.

---

## Contributing and license

* Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request.
* Security issues: follow [`SECURITY.md`](SECURITY.md) and report privately.
* Community standards: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
* Licensed under the **MIT License** — see [`LICENSE`](LICENSE).

---

## Notes

* The SNMP and RouterOS protocol code is written against the observable behaviour of
  the real hardware listed in §1. If a future firmware changes something, the raw
  bytes are logged with `--log-level DEBUG`.
* This tool is **read-only** towards both devices: it only issues SNMP
  GET/GETNEXT/GETBULK and RouterOS `print`/`monitor` commands.
