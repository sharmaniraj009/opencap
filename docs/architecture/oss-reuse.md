# Build vs reuse (OSS) with licence check

Policy: [ADR-0007](../adr/0007-oss-licence-policy.md). Decisions: **Adopt** (use in the
product), **Evaluate** (candidate, decided by the named issue with a benchmark or conformance
run), **Test-only** (dev/CI tool, never shipped), **Isolated** (only as a separately shipped
out-of-process plugin, owner approval needed), **Avoid**, **Build** (no suitable library).

The **Verified** column is `yes` only when the licence was checked against the project's
own `LICENSE` file at a pinned version. Every `no` must be verified by the PR that first
adds the dependency (CI licence gate, board issue). RFC 0001 (`rust-engine`) carries its own
table; the lead merges it into this file when that PR lands.

## Python reference (shipped dependencies)

| Purpose | Library | Licence | Verified | Decision |
|---|---|---|---|---|
| Config models | pydantic | MIT | no | Adopt (in use) |
| CLI | typer | MIT | no | Adopt (in use) |
| YAML | PyYAML | MIT | no | Adopt (in use) |
| Logging | structlog | MIT or Apache-2.0 | no | Adopt (in use) |
| Modbus | pymodbus | BSD-3-Clause | no | Adopt (in use) |
| Allen-Bradley CIP/PCCC | pycomm3 | MIT | no | Adopt (in use) |
| Siemens S7 | python-snap7 (wrapper) + snap7 (bundled C library) | MIT + LGPL-3.0 | no | Adopt: snap7 is dynamically loaded, unmodified, replaceable (LGPL OK) |
| Mitsubishi MC | pymcprotocol | MIT (*verify*) | no | Adopt if verified |
| OPC UA | asyncua | LGPL-3.0 | no | Adopt: imported unmodified, separately installed (LGPL OK); not vendored |
| MQTT | aiomqtt + paho-mqtt | BSD-3-Clause; EPL-2.0 or EDL-1.0 | no | Adopt (EDL-1.0 = BSD-3) |
| API | FastAPI, uvicorn | MIT; BSD-3-Clause | no | Adopt (in use) |
| YAML edits that keep comments (config API) | ruamel.yaml | MIT ([PyPI 0.19.1](https://pypi.org/project/ruamel.yaml/0.19.1/), `LICENSE` in the wheel) | yes (0.19.1) | Adopt (`north` extra; D-034) |
| Web UI framework, vendored in `src/opencap/api/ui/vendor/` | Alpine.js 3.17.5, CSP build | MIT ([LICENSE.md](https://github.com/alpinejs/alpine/blob/v3.17.5/LICENSE.md)) | yes (3.17.5) | Adopt (vendored, listed in `NOTICE`; D-037) |
| Metrics | prometheus-client | Apache-2.0 | no | Adopt (in use) |
| Test broker | amqtt | MIT | no | Test-only |

## Rust engine

| Purpose | Library | Licence | Verified | Decision |
|---|---|---|---|---|
| Async runtime | tokio | MIT | no | Adopt |
| HTTP API, WebSocket | axum (+ tower, hyper) | MIT | no | Adopt |
| Serialisation | serde, serde_json, serde_yaml successor | MIT or Apache-2.0 | no | Adopt |
| Schema → types | typify | Apache-2.0 | no | Evaluate (config issue) |
| Logging | tracing, tracing-subscriber | MIT | no | Adopt |
| Metrics | prometheus-client | Apache-2.0 | no | Adopt |
| SQLite outbox | rusqlite (bundled SQLite) | MIT (SQLite: public domain) | no | Adopt |
| gRPC plugin protocol | tonic, prost | MIT; Apache-2.0 | no | Adopt |
| Modbus TCP/RTU client and server | tokio-modbus | MIT or Apache-2.0 | no | Adopt |
| Serial ports | tokio-serial / serialport | MIT; MPL-2.0 | no | Adopt (MPL-2.0 is file-level copyleft: OK unmodified) |
| MQTT | rumqttc | Apache-2.0 | no | Adopt |
| Sparkplug B payload | `sparkplug_b.proto` from the Eclipse Sparkplug specification + prost | EPL-2.0 (the .proto) | no | Adopt: keep the .proto file unmodified with its notice; generated code is ours (*verify* with the Sparkplug issue) |
| OPC UA client + server | async-opcua (successor of locka99/opcua) | MPL-2.0 | no | Evaluate against the memory/throughput gate (04 §4.1) |
| OPC UA (alternative) | open62541 via FFI | MPL-2.0 | no | Evaluate as fallback |
| EtherNet/IP, Logix, PCCC | rseip | MIT (*verify*) | no | Evaluate (Logix port issue) |
| EtherNet/IP, Logix, PCCC (alternative) | libplctag via FFI | MPL-2.0 or LGPL-2.0+ | no | Evaluate (MPL-2.0 permits static linking) |
| Siemens S7comm | — | — | — | Build: native read subset (COTP, S7 setup, read var); snap7 server as Test-only peer |
| Mitsubishi MC 3E/4E | — | — | — | Build: native (simple framing) |
| Omron FINS | — | — | — | Build: native |
| Beckhoff ADS | ads | MIT or Apache-2.0 (*verify*) | no | Evaluate |
| DNP3 | opendnp3 (C++) | Apache-2.0 (*verify*; upstream archived) | no | Evaluate in the DNP3 spike |
| DNP3 | stepfunc `dnp3` (Rust) | non-commercial licence (*verify*) | no | Avoid unless verified OSS |
| IEC 60870-5-104 | lib60870-C | GPL-3.0 or commercial | no | Isolated, or Build natively (IEC 104 spike) |
| IEC 61850 MMS | libiec61850 | GPL-3.0 or commercial | no | Isolated only |
| BACnet/IP | bacnet-stack | GPL-2.0 with linking exception (*verify*) | no | Evaluate in the BACnet spike |
| SNMP | csnmp / snmp2 crates | *verify* | no | Evaluate |
| Expression engine | own grammar (Pratt parser) or rhai | — ; MIT or Apache-2.0 | no | Decide in the derived-tags spec; **Avoid evalexpr** until its current licence is verified (reported to have moved to AGPL) |
| Password hashing | argon2 | MIT or Apache-2.0 | no | Adopt (security RFC) |
| Windows service | windows-service | MIT or Apache-2.0 | no | Adopt |
| Windows COM (OPC DA bridge) | windows (windows-rs) | MIT or Apache-2.0 | no | Adopt |
| OPC DA proxy/stub, OpcEnum | OPC Foundation Core Components | OPC Foundation redistributable licence | no | **Owner decision** (OPC DA RFC) |
| Desktop shell | Tauri 2 | MIT or Apache-2.0 | no | Adopt |
| Licence gate | cargo-deny | MIT or Apache-2.0 | no | Test-only |

## Test and CI tools (never shipped)

| Purpose | Tool | Licence | Decision |
|---|---|---|---|
| OPC UA client (Ignition's stack) | Eclipse Milo | EPL-2.0 | Test-only |
| OPC UA client/server | open62541 tools | MPL-2.0 | Test-only |
| Sparkplug conformance | Eclipse Sparkplug TCK, Eclipse Tahu | EPL-2.0 | Test-only |
| MQTT broker with persistence | Eclipse Mosquitto | EPL-2.0 or EDL-1.0 | Test-only (and recommended in production) |
| BACnet device simulator | bacpypes3 | MIT (*verify*) | Test-only |
| ADS test server | pyads (testserver) | MIT (*verify*) | Test-only |
| SNMP agent simulator | snmpsim | BSD-2-Clause (*verify*) | Test-only |
| IEC 104 test peer | c104 (Python, wraps lib60870) | GPL-3.0 (*verify*) | Test-only: installed in CI, never vendored or shipped |
| S7 test server | python-snap7 server | MIT + LGPL-3.0 | Test-only |
| Docs site | mkdocs-material | MIT | Test-only (build tool) |
| Python licence report | pip-licenses | MIT | Test-only |
| MSI build | WiX Toolset | MS-RL (*verify*) | Build tool only; output is not encumbered (*verify*) |
