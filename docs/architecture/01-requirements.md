# 1. Requirements

Status legend: **done** (on `main`), **flight** (an engineer PR is open or being written),
**planned** (milestone in [06-roadmap-and-migration.md](06-roadmap-and-migration.md)),
**out** (out of scope, with the reason). Issue numbers live on the [board](../BOARD.md).

## 1.1 Functional requirements (KEPServerEX feature map)

### FR-1 Southbound drivers

Tiers order the work. Tier 1 is what the reference project (a real KEPServerEX export,
never committed) uses; tier 2 is what most KEPServerEX installations use; tier 3 is on demand.

| Tier | Protocol | KEPServerEX driver | opencap status |
|---|---|---|---|
| 1 | Modbus TCP | Modbus TCP/IP Ethernet | done (Python) |
| 1 | Modbus RTU/ASCII serial, RTU over TCP | Modbus Serial | done (Python, #62) |
| 1 | Allen-Bradley Logix / Micro800 (CIP symbolic) | ControlLogix Ethernet, Micro800 Ethernet | done (Python) |
| 1 | Allen-Bradley SLC/MicroLogix/PLC-5 (PCCC over EtherNet/IP) | ControlLogix Ethernet (model 23) | done (Python) |
| 1 | Allen-Bradley DF1 serial | DF1 | done (Python, #62) |
| 1 | Siemens S7-300/400/1200/1500 (S7comm) | Siemens TCP/IP Ethernet | done (Python, #95; field check to do) |
| 1 | Mitsubishi MC protocol (3E/4E) | Mitsubishi Ethernet | done (Python, #95; field check to do) |
| 1 | OPC UA client | OPC UA Client | done (Python, #96; field check to do) |
| 2 | Omron FINS (UDP/TCP) | Omron FINS Ethernet | planned M5 |
| 2 | BACnet/IP | BACnet/IP | planned M5 |
| 2 | DNP3 master (TCP/serial) | DNP Client Ethernet/Serial | planned M5 |
| 2 | IEC 60870-5-104 client | IEC 60870-5-104 Master | planned M5 |
| 2 | EtherNet/IP generic (CIP explicit class/instance/attribute) | EtherNet/IP Gateway, CIP | planned M5 |
| 2 | Beckhoff TwinCAT ADS | Beckhoff TwinCAT | planned M5 |
| 2 | GE SRTP (Ethernet) | GE Ethernet | planned M5 |
| 2 | SNMP v1/v2c/v3 | SNMP | planned M5 |
| 2 | MQTT client (subscribe topics as tags) | MQTT Client | planned M5 |
| 2 | OPC DA client (Windows) | OPC DA Client | planned M5, needs the Windows COM work of FR-8 |
| 2 | ODBC client (SQL rows as tags) | ODBC Client | planned M5 |
| 2 | Simulator and memory drivers | Simulator, Memory Based | planned M2 (needed for demos, UI and desktop tests) |
| 3 | IEC 61850 MMS client | IEC 61850 MMS Client | on demand (licence: see oss-reuse) |
| 3 | Omron Host Link, Yokogawa, Honeywell, Toshiba, … | various | on demand |
| 3 | MTConnect (HTTP/XML) | MTConnect | on demand |
| 3 | User-configurable protocol (scripted frames) | U-CON | on demand |
| out | Fanuc FOCAS | Fanuc Focas | closed vendor library, cannot be redistributed |

Every driver: `plan()` is pure (no I/O); one attempt per `read()`; the error taxonomy of
[03-deep-dive.md §3.4](03-deep-dive.md#34-error-and-retry-handling); identity where the
protocol offers it; **browse** (automatic tag generation) where the protocol offers it.

### FR-2 Tag model
- Hierarchy: channel → device → tag groups (any depth) → tag. Names cannot contain `.`
  (as in KEPServerEX), so the dotted path `Channel.Device.Group.Tag` is unambiguous.
- Data types: bool, int8…int64, uint8…uint64, float32, float64, string, BCD/LBCD, date,
  char/byte (aliases), one- and two-dimensional arrays. Today: bool, int16…uint64,
  float32/64, string, 1-D arrays (done); BCD/LBCD/Date are imported disabled (planned).
- Scaling: linear and square root, raw/engineering ranges, clamping, negate, units (done).
- Client access: read-only (the only value accepted until ADR-0008 is superseded).
- Description, engineering units, EU range for OPC UA `EURange`.
- Automatic tag generation: from the device (Logix tag list, OPC UA browse, BACnet object
  list, …), from KEPServerEX CSV tag files, and from the KEPServerEX project import (done).
- Dynamic tags (a client addresses `Channel.Device.<address>` without a configured tag):
  planned M5, off by default (it lets clients create load on PLCs).

### FR-3 Scan model
- Per-tag scan rate, "respect tag-specified scan rate", "request data no faster than"
  (`min_scan_ms`), "request all data at scan rate" (done).
- Demand poll (`read_once`, done) and "do not scan, demand poll only" (planned).
- Client-driven scan: the fastest rate any north client asks for, floored by `min_scan_ms`
  (planned with the OPC UA server; KEPServerEX default behaviour).
- Initial updates from cache; device reads from OPC clients served from cache unless the
  device allows demand reads (see 03 §3.5).
- One request on the wire per channel by default (KEPServerEX semantics, done); optional
  per-channel concurrency for protocols that support it (planned).
- Timeouts, retries, connect timeout, inter-request delay, auto-demotion, stale marking,
  backoff (done except inter-request delay).
- Scheduler plug-in equivalent (enable scanning by calendar): planned M5, low priority.

### FR-4 Derived (advanced) tags
Expression tags, average, min/max, bit mask, totaliser/cumulative, complex (struct/array
assembly), timer/counter tags. Link tags (tag-to-tag copy) are **writes**: out until ADR-0008
is superseded. Specified language-neutrally first (`docs/spec/derived-tags.md`), implemented
in the Rust engine.

### FR-5 Alarms and events
Limit (HiHi/Hi/Lo/LoLo), deviation, rate of change, digital/discrete conditions; severity,
deadband, delay; states active/acknowledged/returned; acknowledge through the API (an
opencap state change, not a PLC write); event history. Exposed through OPC UA Alarms &
Conditions and an MQTT alarm topic; OPC AE later through the Windows bridge.

### FR-6 Data logger and local historian
Sinks: SQLite (local historian), PostgreSQL/TimescaleDB, Microsoft SQL Server, MySQL
(through native drivers or ODBC), InfluxDB line protocol, CSV files. Wide and narrow table
formats, triggers (interval, on change, condition), store-and-forward when the sink is down,
retention. Local historian: query API for trends in the UI.

### FR-7 IoT gateway
- MQTT agents (several per node): SubDevice JSON (done), KEPServerEX IoT Gateway JSON
  (compatibility for sites migrating from KEPServerEX's IoT Gateway), flat JSON, templates.
- Sparkplug B (edge node, birth/death, aliases, rebirth, primary host awareness).
- REST client (HTTP push) and REST server (read values over HTTP).
- Store-and-forward for every agent (done for MQTT, D-016).

### FR-8 Servers for SCADA
| Server | Status |
|---|---|
| OPC UA server (security policies, users, certificates, subscriptions, browse) | done (Python, #96: one user, secure defaults); hardening #28, L1 suite #29, Rust #50 |
| MQTT / Sparkplug B (see FR-7) | MQTT done, Sparkplug planned M3 |
| Modbus TCP server (register map over the tag store, read function codes only) | planned M3 |
| OPC DA 2.05a/3.0 server (Windows, COM) | planned M5, RFC + owner decision on OPC Foundation components |
| DNP3 outstation | planned M5 |
| IEC 60870-5-104 server | planned M5 |
| OPC AE server (Windows) | planned after OPC DA |
| SuiteLink, FastDDE, DDE, iFIX native interface | out: proprietary or obsolete; AVEVA and iFIX reach opencap through OPC UA or OPC DA (see [scada-compatibility.md](scada-compatibility.md)) |
| ThingWorx native interface | out: proprietary PTC protocol; MQTT/REST instead |

### FR-9 Configuration
- Project import from KEPServerEX JSON (done) and KEPServerEX CSV tag files (planned).
  The binary `.opf` format is not read: KEPServerEX saves projects as JSON.
- Project export, validation (`opencap check`, done), hot reload of changed channels only
  (done), config revisions with rollback (planned).
- Configuration REST API: CRUD for channels, devices, tags, north agents and plug-ins, with
  optimistic concurrency (done for channels, devices, tags, map and overrides, #63; contract in
  03 §3.6 and `api/openapi.json`, #88; north agents and plug-ins planned).
- Web UI (done, #63), desktop app for Linux and Windows (shell done, #87; installers with a
  bundled engine planned M4, #58), Quick Client
  equivalent (live values, demand read), CLI (done).

### FR-10 Diagnostics
System tags (`_System`, per-device `_Statistics`/`_Error`/`_State`), per-device
communication diagnostics (frame capture on demand, rate limited), event log, Prometheus
metrics (done), health endpoint (done), device identity (done), block plan and cycle-time
estimate (`opencap check`, done).

### FR-11 Users and security
Users, groups and roles (viewer, operator, engineer, admin); API tokens (single token done);
per-tag access policies (Security Policies plug-in equivalent); OPC UA security policies,
user tokens and certificate trust management; TLS for MQTT (done); audit log of every
configuration change; secrets only from environment variables or secret files (done).

### FR-12 Redundancy
Active/standby node pair with automatic failover (Redundancy Master equivalent), config
sync between the pair, no dual polling of PLCs. Media-level redundancy: alternate IP/route
per device (KEPServerEX Media Level Redundancy plug-in). See [ADR-0012](../adr/0012-redundancy-warm-standby.md).

### FR-13 Platform and packaging
Linux x86_64/arm64 and Windows x64; systemd unit and Windows service; Docker image;
installers (MSI, deb, AppImage); desktop apps for Linux and Windows (Tauri); offline install;
no telemetry, ever, unless the owner decides otherwise.

### Out of scope
KEPServerEX licensing/activation (we have none), EFM exporter (oil and gas flow measurement;
revisit on demand), OPC .NET (Xi), OPC HDA (revisit with the local historian), writes
(ADR-0008).

## 1.2 Non-functional requirements

Numbers are targets for the Rust engine unless a row says otherwise. The Python reference is
measured by the benchmark issue and its numbers are recorded here once known.

| Id | Requirement | Target |
|---|---|---|
| NFR-1 | Scale per node | 2,000 devices, 250,000 tags, 250,000 tag updates/s aggregate |
| NFR-2 | Edge scale | 10,000 tags at 1 s on a 4-core ARM64 box with 1 GB RAM: ≤ 10 % of one core, ≤ 64 MB RSS |
| NFR-3 | Internal latency | reply arrival → north publish (on-change) ≤ 20 ms p99 at target load |
| NFR-4 | Scan accuracy | start jitter ≤ 5 % of rate (done), overruns counted (done) |
| NFR-5 | Startup | 250,000-tag project loaded and polling in ≤ 10 s |
| NFR-6 | Availability, single node | crash → restart by supervisor ≤ 5 s, polling again ≤ 10 s; reload of one channel never interrupts the others (done) |
| NFR-7 | Availability, pair | failover ≤ 15 s, no dual polling |
| NFR-8 | Durability | zero message loss across broker or WAN outage up to the outbox cap; outbox survives process kill; power loss may lose at most the last ~1 s (SQLite WAL, `synchronous=NORMAL`) |
| NFR-9 | Resource ceiling at target scale | ≤ 1 GB RSS, ≤ 4 cores |
| NFR-10 | Safety | no code path emits a write service to a PLC (guards and tests per driver, done for Modbus/AB/PCCC) |
| NFR-11 | Security | no open port unless configured; API authentication; no secret in config, logs or payloads |
| NFR-12 | Portability | runs without root (default ports ≥ 1024: OPC UA 4840, Modbus server 5020) |
| NFR-13 | Observability | every device's state, cycle time, errors and every north agent's backlog visible in metrics, system tags and the API |
| NFR-14 | Maintainability | every protocol behaviour covered by a simulator test; pure functions covered by conformance vectors shared by both engines |
| NFR-15 | Compatibility | SCADA matrix level L1 (automated, OSS clients) green in CI for every north server |

## 1.3 Constraints

| Id | Constraint | Consequence |
|---|---|---|
| C-1 | Open source, Apache-2.0 (`LICENSE`, `pyproject.toml`) | Dependencies follow [ADR-0007](../adr/0007-oss-licence-policy.md) |
| C-2 | Small team of AI engineer sessions that cannot message each other | All coordination on GitHub; small PRs; issues carry their own acceptance criteria and test plans; file overlap avoided by design ([CONTRIBUTING.md](../../CONTRIBUTING.md)) |
| C-3 | A working Python reference (~985 tests) exists | It stays green; it is the executable spec and the conformance harness for the Rust engine ([ADR-0002](../adr/0002-rust-engine-python-reference.md)) |
| C-4 | Public repository | Privacy rules, `test_no_private_data.py` |
| C-5 | No PLCs, no SCADA licences, no plant access from CI or cloud sessions | Simulators and OSS clients only; vendor-product tests are manual and owner-run |
| C-6 | Sites reach PLCs over IPsec VPN, ~60 ms RTT, small edge boxes | Wire-latency-bound polling; small memory footprint; store-and-forward |
| C-7 | KEPServerEX is a PTC trademark | Use the name only to describe compatibility ("imports KEPServerEX projects"), never as part of a product name |
