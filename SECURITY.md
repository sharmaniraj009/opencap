# Security policy

opencap connects to industrial control systems. Please treat security problems with that
in mind.

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.** Report it privately through GitHub:
open the repository's **Security** tab and choose **Report a vulnerability** (a private
security advisory). Include:

- the opencap version or commit, and how it was deployed (Docker, systemd, Windows service);
- what an attacker can do, and the preconditions (network position, a token, config);
- steps to reproduce, using simulators and documentation addresses (`192.0.2.x`), never a
  real plant.

If the **Report a vulnerability** button is missing, open an issue that says only that you
have a security report and asks for a private contact. Put no details in it.

We aim to acknowledge a report within 5 working days, and to agree a fix and a disclosure
date with you. Fixes land on the default branch; only the latest version is supported.

## Never post real site data

Not in issues, pull requests, discussions or logs, not even in a "private" repository fork:

- real PLC, gateway, broker or VPN **IP addresses and hostnames**;
- **KEPServerEX project exports** (`project.json`) or files generated from them
  (`devices.json`, `devices_map.yaml`, `reports/`). They contain addresses, plant
  structure, and sometimes credentials;
- **credentials**: MQTT passwords, API tokens, OPC UA users and certificates;
- site, customer or plant names, and screenshots that show them.

Redact before you share: replace addresses with `192.0.2.x` and names with invented ones
(`Plant A/PLC1`). If you posted something by mistake, delete it, rotate any credential it
contained, and tell the maintainers so the history can be cleaned.

## Security model

- **Read-only by design.** opencap never writes to a PLC. `Driver.write()` raises. The
  Allen-Bradley drivers refuse every write and set-attribute service at the protocol level.
  The simulators reject writes, and tests assert none are sent. A path that writes to a PLC
  is a vulnerability: report it.
- **Secrets stay out of files.** Config files hold only the *names* of environment
  variables (`mqtt.password_env`, `api.token_env`, OPC UA `username_env` / `password_env`).
  The KEPServerEX importer never copies credentials from a project.
- **Admin API.** `/health` and `/metrics` are unauthenticated, for monitoring. Every other
  route needs a bearer token (constant-time compare). Without a token, the API refuses to
  bind anything but a loopback address. The API has no TLS of its own: bind it to localhost
  or a VPN interface, or put it behind a TLS reverse proxy.
- **MQTT.** Use TLS (`mqtt.tls: true`) and broker authentication on any network you do not
  fully control. `isSecure` in the payload reflects `mqtt.tls`.
- **Least privilege.** The container image runs as a non-root user. The systemd unit runs
  as a dedicated user with a hardened sandbox. Neither needs inbound access except the
  admin API port.
- **Network placement.** Run opencap on the OT side of the firewall or VPN. PLC protocols
  (Modbus, EtherNet/IP, S7) have no authentication. opencap should be the only system
  that reaches the PLCs, and only the broker connection should leave the OT network.
