# opencap architecture

opencap is an open-source replacement for everything KEPServerEX offers: southbound drivers
for PLCs and field protocols, a tag model with scan control, derived tags, alarms, a data
logger, an IoT gateway (MQTT, Sparkplug B, REST), servers for SCADA systems (OPC UA,
OPC DA, Modbus, DNP3, IEC 104), a configuration API, redundancy, diagnostics, users and
security, and import of existing KEPServerEX projects.

This directory is the architecture of record. It is owned by the technical lead and kept
current: a PR that changes a contract described here updates the document in the same PR,
or the lead does it at merge time.

## How this architecture is written

Every part follows the same system-design method:

1. **Requirements**: functional (feature parity with KEPServerEX), non-functional (scale,
   latency, availability, durability, resources), constraints (licences, team, existing code).
2. **High-level design**: components, data flow, API contracts, storage.
3. **Deep dive**: data model, APIs, caching, queues and events, error and retry handling.
4. **Scale and reliability**: load estimates, scaling, failover and redundancy, monitoring.
5. **Trade-offs**: explicit, each major decision recorded as an ADR in [`docs/adr/`](../adr/),
   with what we would revisit as the system grows.

## Documents

| Document | Content |
|---|---|
| [01-requirements.md](01-requirements.md) | KEPServerEX feature map, non-functional targets, constraints |
| [02-high-level-design.md](02-high-level-design.md) | Components, data flow, contracts, storage, deployment views |
| [03-deep-dive.md](03-deep-dive.md) | Data model, tag store and events, scheduler, driver and north contracts, config API, errors |
| [04-scale-reliability.md](04-scale-reliability.md) | Load estimates, scaling, redundancy, failure modes, monitoring |
| [05-tradeoffs.md](05-tradeoffs.md) | Trade-offs and what to revisit |
| [06-roadmap-and-migration.md](06-roadmap-and-migration.md) | Milestones, Python → Rust migration path, cut-over criteria |
| [scada-compatibility.md](scada-compatibility.md) | SCADA compatibility matrix and test levels |
| [oss-reuse.md](oss-reuse.md) | Build-vs-reuse table with licences |
| [`docs/adr/`](../adr/) | Architecture decision records |
| [`docs/decisions.md`](../decisions.md) | Implementation decisions and protocol findings (D-NNN), per phase |
| [`docs/BOARD.md`](../BOARD.md) | Program board mirror (status, assignments, next priorities) |

`docs/decisions.md` records *implementation* findings (how a library behaves, why a
planner splits a block). ADRs record *architecture* decisions (what the system is made of
and why). When an implementation finding changes the architecture, it becomes an ADR.

## Ground rules that every design here respects

- **Read-only toward PLCs** until a merged write-safety RFC says otherwise
  ([ADR-0008](../adr/0008-read-only-by-default.md)).
- **Nothing connects to real plant equipment from CI or cloud sessions.** Every protocol is
  proven against simulators and conformance vectors ([ADR-0014](../adr/0014-conformance-by-simulators-and-vectors.md)).
- **Public repository:** no real site names, real IPs (use 192.0.2.x), credentials or
  KEPServerEX exports. `tests/unit/test_no_private_data.py` must pass.
- **Reuse OSS where licences allow** ([ADR-0007](../adr/0007-oss-licence-policy.md)), build only
  what is missing.
