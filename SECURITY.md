# Security policy

## Supported versions

Security fixes are provided for the latest released `3.x` line.

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability.

Use GitHub's private vulnerability reporting on
<https://github.com/johirxofficial/olt-monitor-router/security/advisories/new>
or contact the maintainer through the GitHub profile
[@johirxofficial](https://github.com/johirxofficial).

Include:

* the affected version and component (web UI, SNMP/Telnet collector, database,
  bundle builder, …);
* a minimal reproduction or proof of concept;
* the impact you believe it has;
* any suggested fix, if you have one.

You will get an acknowledgement as soon as possible. Please give the maintainer
a reasonable window to ship a fix before publishing details.

## Handling secrets

The application stores device communities/passwords encrypted with
`data/.secret_key` and signs session cookies with `data/.session_key`.
Neither key, nor `.env`, nor the MySQL password, is included in the source
bundle. A site-config export is the exception: it deliberately decrypts device
credentials so it can be restored on another server. Treat a site-config JSON
file as a secret: it contains device credentials and web-user password hashes.

If a key or export leaks:

1. rotate every device credential and web-user password;
2. replace the MySQL password in `.env`;
3. delete `data/.secret_key` and restart so a new key is generated (device
   secrets stored under the old key must then be re-entered or restored from a
   trusted config export);
4. review the audit log at **Developer → Audit log** and the server logs.

## Deployment hardening

* Put the web UI behind a firewall/VPN; do not expose it directly to the
  internet without TLS and network-level access control.
* Run as an unprivileged service user; keep `.env`, `data/`, `logs/` and
  `backups/` at mode `0600`/`0700`.
* Use a dedicated MySQL account limited to this application's database.
* Keep the device SNMP community read-only and separate from management
  credentials where the hardware allows it.
* Take regular config-only backups (**Settings → Backup**) and keep them
  private.
