# 6. Roadmap and Python → Rust migration

[`ROADMAP.md`](../../ROADMAP.md) is the public summary; this file is the engineering
plan behind it. The lead keeps the two consistent.

## 6.1 Milestones

| Milestone | Scope | Exit criteria |
|---|---|---|
| **M1 Python parity for the reference project** | S7, Mitsubishi MC, OPC UA client, Modbus RTU and DF1 drivers; OPC UA server v1; web UI + config API v1; CI, licence file, Docker/systemd/Windows service | All in-flight engineer PRs merged; every driver green against its simulator; CI required on `main` |
| **M2 Rust engine core** (core landed in #64: Phase 1 acceptance passes on Rust) | Rust workspace, driver trait, config from the shared schema, tag store + event bus, Modbus TCP, MQTT + outbox, admin API | Python Phase 1 acceptance suite passes against the Rust binary (`OPENCAP_ENGINE=rust`); conformance vectors pass in both engines; benchmark recorded |
| **M3 Rust driver and SCADA parity** | Rust ports of AB Logix, PCCC, S7, MC, OPC UA client, RTU, DF1; OPC UA server, Sparkplug B, Modbus TCP server in Rust; plugin protocol + Python driver host | Every Python acceptance suite passes against Rust; SCADA matrix L1 green for OPC UA, Sparkplug, Modbus |
| **M4 Cut-over, desktop, installers** | Rust is the default engine; Tauri desktop apps (Linux, Windows); MSI, deb, AppImage; docs site | 7-day shadow run per driver family in staging (simulators) and owner-run site shadow tests; installers signed (owner decision on code-signing) |
| **M5 KEPServerEX plug-in parity** | Derived tags, alarms, data logger, redundancy, users/roles, system tags, OPC DA bridge, DNP3 master/outstation, IEC 104, BACnet/IP, Omron FINS, EtherNet/IP generic, ADS, SNMP, MQTT client | Each feature: spec + conformance vectors + implementation + L1 tests |

Work on M5 specs, simulators and RFCs starts now in parallel: they need no engine code.

## 6.2 Migration path (strangler, [ADR-0002](../adr/0002-rust-engine-python-reference.md))

1. **Now (M1).** Python is the production engine. In-flight Python drivers finish in Python.
2. **Contract first.** The config JSON Schema and the conformance vectors (planner,
   decoder, scaling, payload) are exported from Python and committed. They are the
   definition of "same behaviour".
3. **Rust core (M2).** The Rust engine reads the same config, passes the same vectors, and
   the Python acceptance suites run against its binary over the network (simulators and
   test broker unchanged). The suites become engine-agnostic: they talk to the engine only
   through its config directory, its API and the wire.
4. **Rust-first from the merge of the Rust driver trait.** New protocol drivers, new north
   plugins and new runtime features (derived tags, alarms, logger, redundancy) are written
   in Rust. Python receives bug fixes and only the features the production sites need
   before cut-over. Every new protocol still gets its simulator in the Python test harness.
5. **Ports (M3).** Each Python driver is ported with its simulator suite and vectors as the
   acceptance test. Until a port lands, the Rust engine can run the Python driver through the
   plugin host, so the Rust engine can go to production before every port is done.
6. **Cut-over (M4).** The Rust engine becomes the default when, for every driver family a
   site uses, the acceptance suite passes, the vectors pass, and a 7-day shadow run (Rust and
   Python side by side against simulators in staging; on site only when the owner runs it)
   shows identical payloads apart from timestamps.
7. **Retirement.** The Python engine stays installable for one release after cut-over, then
   is retired as an engine. The Python package remains the importer reference (until the
   Rust importer has parity) and the simulator/acceptance harness, permanently.

## 6.3 What runs where during the migration

| Function | M1 | M2 | M3 | M4+ |
|---|---|---|---|---|
| Production engine | Python | Python | Python (Rust in staging) | Rust |
| Drivers Modbus TCP | Python | Python + Rust | Rust | Rust |
| Drivers AB, S7, MC, OPC UA, serial | Python | Python | Rust (or Python via plugin host) | Rust |
| New drivers (FINS, BACnet, DNP3, …) | specs/sims | specs/sims | Rust | Rust |
| North MQTT | Python | Python + Rust | Rust | Rust |
| OPC UA server | Python | Python | Rust | Rust |
| Sparkplug, Modbus server, DNP3 outstation | — | — | Rust | Rust |
| Simulators, acceptance suites | Python | Python | Python | Python |
| KEPServerEX importer | Python | Python | Python | Rust (Python as reference) |
| Web UI | served by Python | served by Python | served by either | served by Rust, in Tauri |
