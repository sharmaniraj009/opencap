# 0008. Read-only toward PLCs until a write-safety RFC is approved

- Status: Accepted
- Date: 2026-11-17
- Deciders: owner (hard rule), technical lead

## Context
opencap polls production plants. A write path is the only way opencap could move an
actuator. KEPServerEX supports writes (OPC client writes, link tags, IoT Gateway writes);
some users will expect it.

## Decision
- No driver implements writes; drivers guard against write services (done for Modbus, AB
  Logix, PCCC: write services are refused before they reach the wire, with tests).
- North servers expose tags with read-only access levels (OPC UA `AccessLevel =
  CurrentRead`; Modbus server answers write function codes with exception 01; DNP3
  outstation rejects control operations; Sparkplug DCMD/NCMD only for `Node Control/Rebirth`).
- Link tags and any "write-back" feature are out of scope.
- Changing opencap's own state (config, alarm acknowledgement) is not a PLC write and is
  allowed through the authenticated API with audit.
- Writes may be designed only through `docs/rfc/0002-write-safety.md` (board issue). The RFC
  must cover: off by default per node, per tag and per user; audit log; rate limits; value
  range checks; interlocks; simulation-only testing; failure behaviour. It takes effect only
  when merged **and** approved by the owner on the board issue.

## Consequences
Sites that write setpoints through KEPServerEX keep that path outside opencap for now.

## Alternatives considered
Writes behind a config flag (too easy to enable by accident on a live plant).

## Revisit when
The owner approves the write-safety RFC.
