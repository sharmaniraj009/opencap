# Build vs reuse (OSS) with licence check

Policy: [ADR-0007](../adr/0007-oss-licence-policy.md). Decisions: **Adopt** (use in the
product), **Evaluate** (candidate, decided by the named issue with a benchmark or conformance
run), **Test-only** (dev/CI tool, never shipped), **Isolated** (only as a separately shipped
out-of-process plugin, owner approval needed), **Avoid**, **Build** (no suitable library).

The **Verified** column is `yes` only when the licence was checked against the project's
own `LICENSE` file at a pinned version. Every `no` must be verified by the PR that first
adds the dependency (CI licence gate, board issue). The per-feature build-vs-reuse study
(every KEPServerEX driver and plug-in, existing OSS gateways) is RFC 0001 §3; this file is the
policy-level summary, reconciled with it after #64.

## Python reference (shipped dependencies)

| Purpose | Library | Licence | Verified | Decision |
|---|---|---|---|---|
| Config models | pydantic | MIT | no | Adopt (in use) |
| CLI | typer | MIT | no | Adopt (in use) |
| YAML | PyYAML | MIT | no | Adopt (in use) |
| Logging | structlog | MIT or Apache-2.0 | no | Adopt (in use) |
| Modbus | pymodbus | BSD-3-Clause | no | Adopt (in use) |
| Allen-Bradley CIP/PCCC | pycomm3 | MIT | no | Adopt (in use) |
| Siemens S7 | python-snap7 ≥ 3.5 | MIT (3.x is pure Python: `py3-none-any` wheel, no bundled LGPL snap7 library) | yes (3.5.0 metadata + LICENSE) | Adopt (in use, #95) |
| Mitsubishi MC | pymcprotocol 0.3 | MIT | yes (0.3.0 metadata + LICENSE) | Adopt (in use, #95; driver overrides its socket I/O) |
| OPC UA client and server | asyncua 2.1 (pinned `>=2.1,<2.2`: the driver touches internals) | LGPL-3.0-or-later | no | Adopt (#96): imported unmodified, separately installed (LGPL OK); not vendored; ADR-0007 exception in `licenses/python-allow.toml` |
| MQTT | aiomqtt + paho-mqtt | BSD-3-Clause; EPL-2.0 or EDL-1.0 | no | Adopt (EDL-1.0 = BSD-3) |
| API | FastAPI, uvicorn | MIT; BSD-3-Clause | no | Adopt (in use) |
| WebSocket server for uvicorn (`/api/v1/ws`, Quick Client) | websockets | BSD-3-Clause ([`LICENSE` at 15.0.1](https://github.com/python-websockets/websockets/blob/15.0.1/LICENSE)) | yes (15.0.1) | Adopt (`north` extra; D-067) |
| Serial ports (modbus_rtu, ab_df1) | pyserial 3.5 | BSD-3-Clause (SPDX header in `serial/__init__.py`; LICENSE.txt at tag v3.5) | yes | Adopt (optional `serial` extra, #62) |
| YAML edits that keep comments (config API) | ruamel.yaml | MIT ([PyPI 0.19.1](https://pypi.org/project/ruamel.yaml/0.19.1/), `LICENSE` in the wheel) | yes (0.19.1) | Adopt (`north` extra; D-041) |
| Web UI framework, vendored in `src/opencap/api/ui/vendor/` | Alpine.js 3.17.5, CSP build | MIT ([LICENSE.md](https://github.com/alpinejs/alpine/blob/v3.17.5/LICENSE.md)) | yes (3.17.5) | Adopt (vendored, listed in `NOTICE`; D-044) |
| Metrics | prometheus-client | Apache-2.0 | no | Adopt (in use) |
| Test broker | amqtt | MIT | no | Test-only |
| Sparkplug conformance vectors (`tests/helpers/sparkplug_ref.py`) | protobuf (Google's Python runtime) | BSD-3-Clause | yes (7.36 metadata) | Test-only (`dev` extra, #51) |

## Rust engine

| Purpose | Library | Licence | Verified | Decision |
|---|---|---|---|---|
| Async runtime | tokio | MIT | no | Adopt |
| Cancellation token (north plugins) | tokio-util 0.7 (`default-features = false`) | MIT | yes (cargo-deny) | Adopt (#41; already in the tree through rumqttc and tokio-modbus) |
| Benchmarks (dev-dependency only) | criterion 0.7 (`default-features = false`) | MIT or Apache-2.0; its tree is MIT/Apache-2.0, `ciborium` Apache-2.0, `walkdir`/`same-file`/`winapi-util` Unlicense or MIT | yes (cargo-deny) | Adopt (#41; 0.8 needs Rust 1.86, the workspace declares 1.85) |
| HTTP API, WebSocket | axum (+ tower, hyper) | MIT | no | Adopt |
| Serialisation | serde, serde_json, serde_yaml successor | MIT or Apache-2.0 | no | Adopt |
| Schema → types | typify | Apache-2.0 | no | Not used: #64 hand-writes serde types with `deny_unknown_fields`; #39 adds the schema drift check |
| Logging | tracing, tracing-subscriber | MIT | no | Adopt |
| Metrics | prometheus (rust-prometheus) 0.14 | Apache-2.0 | no | Adopt (#64; exposition identical to Python's, RFC 0001 §10 Q2) |
| Config YAML | serde_yaml_ng | MIT | no | Adopt (#64; YAML 1.2, see D-032) |
| TLS | rustls + ring | Apache-2.0 / ISC-style | no | Adopt (#64; allowed by `engine/deny.toml`) |
| CLI, time zones | clap; chrono-tz | MIT or Apache-2.0 | no | Adopt (#64) |
| SQLite outbox | rusqlite (bundled SQLite) | MIT (SQLite: public domain) | no | Adopt |
| gRPC plugin protocol | tonic 0.14.5 (pinned: later 0.14 releases need Rust 1.88), prost 0.14, tonic-prost; protox 0.10 to compile the `.proto` at build time (no `protoc`) | MIT; Apache-2.0; MIT; MIT or Apache-2.0 | yes (cargo-deny) | Adopt (#49) |
| gRPC plugin protocol, Python host | grpcio, protobuf; grpcio-tools for the stub drift test (dev) | Apache-2.0; BSD-3-Clause; Apache-2.0 | yes (licence gate) | Adopt (#49; `plugin` extra) |
| Modbus TCP/RTU client and server | tokio-modbus | MIT or Apache-2.0 | no | Adopt |
| Serial ports | tokio-serial / serialport | MIT; MPL-2.0 | no | Adopt (MPL-2.0 is file-level copyleft: OK unmodified) |
| MQTT | rumqttc | Apache-2.0 | no | Adopt |
| Sparkplug B payload | `sparkplug_b.proto` (Eclipse Tahu v1.0.19, vendored unmodified in `proto/third_party/`) + prost 0.14 | EPL-2.0 (the .proto, file-level: [header](https://github.com/eclipse-tahu/tahu/blob/v1.0.19/sparkplug_b/sparkplug_b.proto)); prost Apache-2.0 | yes (v1.0.19 file + SHA-256) | Adopt (#51, `engine/north/sparkplug`, listed in `NOTICE`). The `srad` crates (MIT OR Apache-2.0) were evaluated and not used: pre-1.0 (0.4 → 0.7 in eight months), and `srad-types` ships prost code generated from the EPL proto under MIT/Apache with no EPL notice (D-090) |
| Protobuf code generation (build time only) | prost-build 0.14 + protox 0.10 (pure-Rust protobuf compiler: no system `protoc`) | Apache-2.0; MIT OR Apache-2.0 | yes (crates.io metadata; `cargo deny` passes) | Adopt (#51, build-dependencies of `opencap-north-sparkplug`) |
| OPC UA client + server | async-opcua 0.19 (successor of locka99/opcua) | MPL-2.0 | yes (cargo info) | Adopt for the server (D-123, #50) and the client (D-124, #46): passes the memory gate, 1.87 KB per node at 250k |
| OPC UA (alternative) | open62541 via FFI | MPL-2.0 | no | Not needed: a usable Rust crate exists (ADR-0018 order, D-123) |
| EtherNet/IP, Logix, PCCC | native CIP client (RFC 0001 default) vs rseip / rust-ethernet-ip | — ; MIT (*verify*) | no | **Decided in #43: native CIP client, no new dependency** (D-076) |
| EtherNet/IP, Logix, PCCC (alternative) | libplctag via FFI | MPL-2.0 or LGPL-2.0+ | no | Evaluate again under ADR-0018 (OSS first; D-076 superseded). v2.7.2 has no serial DF1: DF1 is native (D-120) |
| Allen-Bradley DF1 (serial) | none found (libplctag has no DF1) | — | — | Build: native full-duplex link layer on opencap-serial (D-120) |
| Siemens S7comm | — (rust-snap7 is MulanPSL-2.0: Avoid) | — | — | Build: native read subset (COTP, S7 setup, read var); snap7 server as Test-only peer |
| Mitsubishi MC 3E/4E | — | — | — | Build: native (simple framing) |
| Omron FINS | — | — | — | Build: native (#54); conformance harness `tests/helpers/fins_sim.py` + `docs/protocols/omron-fins.md` (#13) |
| Beckhoff ADS | ads | MIT or Apache-2.0 (*verify*) | no | Evaluate |
| DNP3 | opendnp3 (C++) | Apache-2.0 ([3.1.2](https://github.com/dnp3/opendnp3/blob/3.1.2/LICENSE); archived 2022) | yes (#14) | Not adopted (EOL C++, no C ABI); design reference allowed. Build: native Rust read subset ([ADR-0015](../adr/0015-dnp3-native-rust-subset.md)) |
| DNP3 | stepfunc `dnp3` (Rust) | custom non-commercial licence ([1.8.0](https://github.com/stepfunc/dnp3/blob/1.8.0/LICENSE.txt); never OSS in any version) | yes (#14) | **Avoid** in every role, including as a test peer (its §2.6); do not read its source |
| IEC 60870-5-104 | lib60870-C | GPL-3.0 or commercial ([v2.4.1](https://github.com/mz-automation/lib60870/blob/v2.4.1/COPYING)) | yes (#16) | Not used in any shipped form. Build: native Rust client and server ([ADR-0017](../adr/0017-iec104-native-rust-subset.md)) |
| IEC 60870-5-104 | igs-iec104 | Apache-2.0 ([c586279](https://github.com/aklira/igs-iec104/blob/c586279f0634ecebb411079c23e316f63af892c0/LICENSE)) | yes (#16) | Test-only peer (igs104-server/-client); revisit as a dependency at 1.0 |
| IEC 61850 MMS | libiec61850 | GPL-3.0 or commercial | no | Isolated only |
| BACnet/IP | bacnet-stack | mixed: core `GPL-2.0-or-later WITH GCC-exception-2.0`, 2 plain GPL-2.0 files, rest MIT/BSD/Apache ([1.7.0](https://github.com/bacnet-stack/bacnet-stack/blob/bacnet-stack-1.7.0/license/readme.txt)) | yes (#15) | **Avoid** in the default build (no client segmentation either); demo apps Test-only. Build: native Rust read subset ([ADR-0016](../adr/0016-bacnet-native-rust-client.md)) |
| BACnet/IP | rusty-bacnet | MIT ([v0.15.0](https://github.com/jscott3201/rusty-bacnet/blob/v0.15.0/LICENSE)) | yes (#15) | Test-only peer; revisit as a dependency at 1.0 (single maintainer, MSRV 1.95) |
| SNMP | csnmp / snmp2 crates | *verify* | no | Evaluate |
| Expression engine | own grammar (Pratt parser) or rhai | — ; MIT or Apache-2.0 | no | Decide in the derived-tags spec; **Avoid evalexpr** until its current licence is verified (reported to have moved to AGPL) |
| Password hashing | argon2 | MIT or Apache-2.0 | no | Adopt (security RFC) |
| Windows service | windows-service | MIT or Apache-2.0 | no | Adopt |
| Windows COM (OPC DA bridge) | windows (windows-rs) | MIT or Apache-2.0 | no | Adopt |
| OPC DA proxy/stub, OpcEnum | OPC Foundation Core Components | OPC Foundation redistributable licence | no | **Owner decision** (OPC DA RFC) |
| Desktop shell | Tauri 2.12.6 (+ tauri-build 2.7.5) | MIT or Apache-2.0; pulls in MPL-2.0 `cssparser`, `cssparser-macros`, `selectors`, `dtoa-short`, `option-ext` (unmodified; allowed per crate in `desktop/deny.toml`, ADR-0007) | yes (cargo-deny, `desktop/deny.toml`, #90) | Adopt (#87) |
| Desktop keychain | keyring 3.6.3 | MIT or Apache-2.0 | yes (cargo-deny, `desktop/deny.toml`, #90) | Adopt (#87; Secret Service, Credential Manager, Keychain) |
| Desktop HTTP checks | ureq 2.12.1 (+ webpki-roots, CDLA-Permissive-2.0, allowed for that crate only) | MIT or Apache-2.0 | yes (cargo-deny, `desktop/deny.toml`, #90) | Adopt (#87) |
| Licence gate | cargo-deny | MIT or Apache-2.0 | no | Test-only |

## Test and CI tools (never shipped)

| Purpose | Tool | Licence | Decision |
|---|---|---|---|
| OPC UA client (Ignition's stack), L1 SCADA suite | Eclipse Milo 0.6.16 (`sdk-client`, from Maven Central; [LICENSE](https://github.com/eclipse-milo/milo/blob/v0.6.16/LICENSE)) | EPL-2.0 | Test-only: built in CI from `tests/scada/milo-client/pom.xml` (#29) |
| Milo's runtime dependencies (L1 suite) | Netty 4.1, Guava, digitalpetri strict-machine / netty-channel-fsm; BouncyCastle 1.78 (bcprov, bcpkix, bcutil); JAXB runtime 2.3 / Jakarta XML Bind / Activation; slf4j-api + slf4j-simple 1.7.36 | Apache-2.0; Bouncy Castle Licence (MIT-style); EDL-1.0 (BSD-3-Clause); MIT | Test-only (Maven scope `runtime` of the Milo client, #29) |
| OPC UA client, L1 SCADA suite | open62541 v1.4.22 (commit `a9d7e2f2`, built with OpenSSL; [LICENSE](https://github.com/open62541/open62541/blob/v1.4.22/LICENSE)) | MPL-2.0 (OpenSSL 3: Apache-2.0) | Test-only: built in CI by `tests/scada/open62541-client/build.sh` (#29) |
| OPC UA client, L1 SCADA suite | asyncua 2.1 (the `opcua` extra) | LGPL-3.0-or-later | Test-only use of a dependency already adopted above (#29) |
| Sparkplug conformance | Eclipse Sparkplug TCK, Eclipse Tahu | EPL-2.0 | Test-only |
| MQTT broker with persistence | Eclipse Mosquitto | EPL-2.0 or EDL-1.0 | Test-only (and recommended in production) |
| BACnet device simulator | bacpypes3 0.1.0 | MIT declared in [pyproject@v0.1.0](https://github.com/JoelBender/BACpypes3/blob/v0.1.0/pyproject.toml); **no licence file shipped** (classifier says BSD) | Test-only; the lead accepted the declared MIT in its pyproject metadata for a test-only dependency (#15, spike §6 Q1) |
| ADS test server | pyads (testserver) | MIT (*verify*) | Test-only |
| SNMP agent simulator | snmpsim | BSD-2-Clause (*verify*) | Test-only |
| IEC 104 test peer | c104 2.2.1 (Python, wraps lib60870) | GPL-3.0 ([v2.2.1](https://github.com/Fraunhofer-FIT-DIEN/iec104-python/blob/v2.2.1/LICENSE), verified #16) | Test-only: installed by a separate CI job, never in the `dev` extra, never vendored or shipped |
| S7 test server | python-snap7 server | MIT | Test-only |
| Omron FINS client (validates `fins_sim`) | fins-driver 0.3.1 | MIT ([LICENSE at v0.3.1](https://github.com/indrarudianto/fins-driver/blob/v0.3.1/LICENSE), verified) | Test-only (FINS/UDP only: its "tcp" mode lacks the FINS/TCP header) |
| DNP3 master/outstation peer (opendnp3) | yadnp3 3.2.1.5 | Apache-2.0 ([v3.2.1.5](https://github.com/f0rw4rd/opendnp3/blob/v3.2.1.5/LICENSE), verified #14) | Test-only (spike #14; `dev` extra in the DNP3 simulator issue) |
| DNP3 master/outstation peer (pure Python) | dnp3py 0.6.0 | MIT ([v0.6.0](https://github.com/craigpnnl/dnp3py/blob/v0.6.0/LICENSE), verified #14) | Test-only (same) |
| Docs site | mkdocs-material | MIT | Test-only (build tool) |
| Python licence report | pip-licenses | MIT | Test-only |
| MSI build | WiX Toolset | MS-RL (*verify*) | Build tool only; output is not encumbered (*verify*) |
