# Contributing to olt-monitor-router

Thanks for taking the time to improve the project. This guide keeps changes
easy to review and safe for an appliance that talks to live network hardware.

## Before you start

* Read [`README.md`](README.md) for the architecture and supported vendors.
* Search the existing issues and pull requests before opening a new one.
* Never commit real credentials, customer names, public IP addresses, SNMP
  communities, database dumps or captured traffic. Use documentation ranges
  (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and placeholders such as
  `your-community` in examples.
* For a security problem, **do not open a public issue** — follow
  [`SECURITY.md`](SECURITY.md).

## Development setup

```bash
git clone https://github.com/johirxofficial/olt-monitor-router.git
cd olt-monitor-router
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # edit DB_* and device values
.venv/bin/python tools/setup_mysql.py
.venv/bin/python run.py       # http://localhost:5002
```

MySQL 8 is the only supported database. The project must never touch another
application's schema: keep the database name configurable and default to
`olt_monitor`.

## Tests

Run the offline regression suite before every commit:

```bash
.venv/bin/python tools/selftest.py
```

It creates and clears a separate `<DB_NAME>_selftest` database, so it never
modifies live monitoring data. A healthy run ends with:

```text
FAILURES: 0
```

Add a focused test when you change a vendor pattern, an OID profile, the
classifier, the optical resolver or the site-config export/import.

## Code style

* Python 3.10+, standard library first; no new dependency without a reason.
* Keep credential handling inside `olt_monitor/secrets.py` and never log a
  decrypted community or password.
* Keep SQL in `olt_monitor/db.py` helpers and use parameter placeholders.
* Match the existing layout, naming and comment style. Small, reviewable
  commits with a clear message are preferred.
* New vendor support should be data-driven (a profile in
  `olt_monitor/vendors.py` / `olt_monitor/config.py`) rather than a special
  case scattered through the collector.

## Pull requests

1. Fork the repository and create a topic branch.
2. Describe **what** changed, **why**, and the hardware/firmware you tested on
   (mask hosts and communities).
3. Include the `FAILURES: 0` test line in the PR description.
4. Do not force-push after review has started; add follow-up commits instead.

## License

By contributing you agree that your contribution is licensed under the MIT
License in [`LICENSE`](LICENSE).
