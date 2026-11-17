# Roadmap: replacing KEPServerEX

The goal: everything a typical KEPServerEX installation does for data collection, in an
open-source server you can read, test and run anywhere. Each item has an **acceptance
criterion**: the item is done when that criterion is met by tests in CI (simulators, not
real plants), plus the listed field check where one is needed.

Status: ✅ done · 🚧 in progress (open pull request) · 📋 next · 💡 later

The engineering plan behind this page (milestones M1–M5, the Python → Rust migration, the
architecture and its decision records) is in
[docs/architecture/](docs/architecture/06-roadmap-and-migration.md); every item below is
broken into one-PR issues on the [program board](https://github.com/sharmaniraj009/opencap/issues/1)
([mirror](docs/BOARD.md)).

Design rules that hold for every item: drivers stay behind the `Driver` interface; tests
come first for parsers and planners; protocol tests run against simulators that the real
client library talks to unmodified; deviations are recorded in
[docs/decisions.md](docs/decisions.md); no real site data in the repository.

## Done

| Item | Status | Acceptance criterion |
|---|---|---|
| Core polling engine | ✅ | One request on the wire per channel; retries, reconnect backoff (1 s → 60 s), auto-demotion and one probe, stale marking at 3 × rate, scaling. 30 simulated devices poll at production timing with no unexpected stale tags. |
| `opencap check` | ✅ | Block plan, `bad_config` tags and estimated cycle time for every device, without opening a socket and without the protocol library installed. |
| Modbus TCP driver | ✅ | KEPServerEX planning semantics (block size, gaps, word/byte/bit order, strings, zero-based addressing); round trips against the Modbus simulator; exception codes map to `bad_config` / `bad_comm`. |
| Allen-Bradley Logix / Micro800 driver | ✅ | Symbolic reads against the CIP simulator through stock pycomm3; tag-list upload cached across reconnects; Micro800 batching probed and used when accepted (11 vs 402 requests per pass in simulation). |
| Allen-Bradley PCCC driver (SLC / MicroLogix) | ✅ | Data-table reads against the PCCC simulator through stock pycomm3; file-directory check rejects blocks for missing or short files before sending. |
| MQTT with store-and-forward | ✅ | SQLite outbox, QoS 1, in-order drain, delete only after PUBACK; a broker outage loses no message and keeps order (acceptance test with a cut network, up to 10 minutes). |
| KEPServerEX project import | ✅ | `opencap import-kep` turns a `project.json` export into `devices.json`, `devices_map.yaml` stubs and review reports. It never copies credentials and never overwrites hand edits. |
| Admin API and metrics | ✅ | `/health`, `/metrics` open; devices, values, read-once and reload behind a bearer token; refuses a non-loopback bind without a token; Prometheus metrics for requests, cycles, states and outbox. |
| Config reload | ✅ | SIGHUP or `POST /reload` restarts only the channels whose config changed; a bad config keeps the old one running. |
| Project infrastructure | ✅ | Apache-2.0 license, CI on Python 3.12 and 3.13, container image, systemd unit, Windows service definition, configurable MQTT topic prefix. |

## In progress (parallel pull requests)

| Item | Status | Acceptance criterion |
|---|---|---|
| Siemens S7 driver (`s7`) | 🚧 | S7-300/400/1200/1500 DB, M, I, Q addresses planned into PDU-sized reads; round trips against a simulator that stock python-snap7 talks to; KEPServerEX Siemens TCP/IP Ethernet devices import and poll. |
| Mitsubishi MC driver (`melsec_mc`) | 🚧 | 3E-frame batch reads of word and bit devices (D, M, X, Y, ...) against a simulator that the client library talks to unmodified; KEPServerEX Mitsubishi Ethernet devices import and poll. |
| OPC UA client driver (`opcua_client`) | 🚧 | Reads (and subscriptions where configured) from an asyncua test server with security policies None and Basic256Sha256; credentials from environment variables only. |
| OPC UA server (north side) | 🚧 | SCADA/HMI clients browse `channel/device/tag` and read live values with quality and source timestamps; read-only address space; authenticated, encrypted endpoints. |
| Modbus RTU (serial) driver | 🚧 | Same planning as Modbus TCP over a serial line; round trips against a simulated serial slave (pty pair); one request at a time per port. |
| Allen-Bradley DF1 (serial) driver | 🚧 | SLC/MicroLogix data-table reads over DF1 full duplex against a simulator; the devices the importer excludes today as "DF1 serial" are imported and polled. |
| Web UI and config API | 🚧 | Browse channels, devices and live values; edit `overrides.yaml` and `devices_map.yaml` through a validated API that writes the files atomically; changes go live through reload; same token as the admin API. |

## Rust engine, desktop apps and SCADA connectivity

The production engine moves to Rust ([ADR-0002](docs/adr/0002-rust-engine-python-reference.md)).
The Python implementation stays as the reference and as the simulator and acceptance-test
harness. Both engines read the same config and serve the same API.

| Item | Status | Acceptance criterion |
|---|---|---|
| Rust engine core (`engine/`) | 🚧 | Reads the shared config schema; Modbus TCP, MQTT with outbox and the admin API pass the Python Phase 1 acceptance suite against the Rust binary; conformance vectors pass in both engines. |
| Rust ports of every Python driver | 📋 | Each port passes its Python simulator suite and conformance vectors with `OPENCAP_ENGINE=rust`. Until a driver is ported, the Python driver runs under the Rust engine through the plugin host. |
| Rust engine as default (cut-over) | 📋 | All acceptance suites pass on Rust, and a 7-day shadow run shows payloads identical to Python's apart from timestamps. Python stays installable for one more release. |
| Desktop apps (Linux, Windows) | 📋 | A Tauri app that shows the web UI, manages connections to local and remote nodes, and bundles the engine as a sidecar; MSI, deb and AppImage built in CI. |
| OPC UA server hardening | 📋 | KEPServerEX-compatible NodeIds (`ns=2;s=Channel.Device.Tag` option), security policies and users, limits, 100k-node scale; level-L1 compatibility suite green with Eclipse Milo (Ignition's stack), open62541 and asyncua clients. |
| Modbus TCP server (north) | 📋 | Tags served as a configurable register map; write function codes answered with exception 01; tested with a Modbus client. |
| DNP3 outstation and IEC 60870-5-104 server (north) | 📋 | Utility SCADA (Geo SCADA, VTScada, zenon) reads tags with quality and time stamps; control requests rejected. |
| OPC DA server bridge (Windows, north) | 📋 | FactoryTalk View, iFIX, older WinCC and AVEVA (through OI Gateway) browse, read and subscribe to opencap tags over OPC DA 2.05a/3.0; read-only. Design and licence questions first (RFC 0004). |
| SCADA compatibility matrix | 📋 | [docs/architecture/scada-compatibility.md](docs/architecture/scada-compatibility.md) verified per product, with a connection recipe for each. |

## Next

| Item | Status | Acceptance criterion |
|---|---|---|
| Derived / expression tags (Advanced Tags equivalent) | 📋 | Tags computed from other tags (arithmetic, min/max/average, totalizers, bit extraction, conditional), evaluated after each cycle in a sandboxed expression language (no `eval`); quality propagates (any bad input → bad output); imported from KEPServerEX Advanced Tags where possible. |
| Alarms | 📋 | Limit (HI/HIHI/LO/LOLO with deadband and delay), digital and rate-of-change alarms per tag; active/cleared/acknowledged state survives restart; alarm events published to MQTT (retained state topic plus event topic) and shown in the API. |
| Data logger (SQL / CSV) | 📋 | Log selected tags on interval or change to PostgreSQL, SQLite or CSV (rotated files), with store-and-forward when the database is down; no row lost across a database outage in the acceptance test. |
| Sparkplug B | 📋 | Sparkplug B 3.0 edge node: NBIRTH/DBIRTH with metric aliases, DDATA on change, NDEATH as LWT, rebirth on request; passes the Eclipse Tahu compatibility checks; runs alongside or instead of the SubDevice JSON payload. |
| Write support: design and safety model | 📋 | A reviewed design document **before any code**. Writes stay off by default and absent from the default install. The design must cover: per-tag write permission (explicit allow-list, never inherited), value range and type checks, an append-only audit log (who, what, old/new value, when, result), two-step confirmation (prepare + confirm with a short timeout), a role separate from the read token, rate limits, and a global kill switch. Until it is accepted, opencap stays read-only and every driver refuses write services. |
| Redundancy / failover | 📋 | Two opencap instances, active and warm standby ([ADR-0012](docs/adr/0012-redundancy-warm-standby.md)): only the active one polls PLCs and publishes; the standby takes over within a configured time (for example 10 s) when the active one dies, without duplicate publishing beyond QoS 1 duplicates; split-brain prevented (broker-based lease or quorum); tested with killed processes and cut networks. |
| Omron FINS driver | 📋 | FINS/TCP and FINS/UDP reads of DM, CIO, W, H areas against a simulator; KEPServerEX Omron FINS Ethernet devices import. |
| BACnet/IP driver | 📋 | Read present-value (and status flags) of analog/binary/multistate objects via ReadPropertyMultiple; device discovery with Who-Is; COV subscription optional; tested against a BACnet simulator. |
| DNP3 driver | 📋 | DNP3 outstation polling over TCP (class 0/1/2/3 polls, event buffers, time sync off by default) against a simulated outstation; quality flags map to opencap quality. |
| IEC 60870-5-104 driver | 📋 | Client with general interrogation and spontaneous updates (M_ME_*, M_SP_*, M_DP_*), quality descriptors mapped, against a simulated server; still read-only (no commands). |
| EtherNet/IP generic (CIP explicit / assembly) driver | 📋 | Read assembly instances and generic CIP attributes from non-Logix EtherNet/IP devices (drives, I/O blocks), against the CIP simulator. |
| OPC DA client driver (Windows, south) | 📋 | Reads classic OPC DA servers through COM and feeds opencap as a driver, so DA-only data sources can migrate; documented 32/64-bit and DCOM setup. Built on the COM layer of the OPC DA server bridge; no GPL code (OpenOPC is GPL-2.0, so [ADR-0007](docs/adr/0007-oss-licence-policy.md) excludes it). |
| Installers: Windows service / MSI | 📋 | An MSI that installs Python, opencap and the WinSW service, creates `C:\ProgramData\opencap`, and upgrades in place without losing the outbox; signed binaries. |
| Installers: Linux deb / rpm | 📋 | deb and rpm packages that create the `opencap` user, install the systemd unit and config templates, and upgrade without losing config or outbox; built and install-tested in CI. |
| Documentation site | 📋 | Published docs (configuration, drivers, migration, deployment, API) built from the repository in CI, versioned per release. |

## Later

| Item | Status | Acceptance criterion |
|---|---|---|
| Releases on PyPI and a container registry | 💡 | Tagged releases publish a wheel to PyPI and a multi-arch image (amd64, arm64) from CI, with a changelog. |
| Config UI import wizard | 💡 | Upload a KEPServerEX export in the web UI, review the import report, and confirm devices without editing YAML. |
| More KEPServerEX import coverage | 💡 | Advanced Tags, alarms and IoT Gateway agent settings are imported into their opencap equivalents once those exist. |
