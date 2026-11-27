# opencap Rust engine (`engine/`)

The production engine in Rust ([ADR-0002](../docs/adr/0002-rust-engine-python-reference.md)),
designed in [RFC 0001](../docs/rfc/0001-rust-engine.md). It reads the **same config
directory** as the Python `opencap` (`poller.yaml`, `devices.json`, `devices_map.yaml`,
`overrides.yaml`), serves the **same admin API**, publishes **byte-identical** SubDevice JSON
payloads and uses the **same outbox file format**, so the two engines are interchangeable on
a node. The Python code in `src/` stays the reference implementation; its simulators and
acceptance suites are the specification this engine is tested against.

Status: first vertical slice (milestone M2). Modbus TCP, MQTT with store-and-forward, the
admin API and `check`/`read` pass the Python Phase 1 acceptance suite. Every other driver is
reported as "not implemented yet in the Rust engine" (or "not compiled in" when its crate
exists but its Cargo feature is off) and its devices are skipped, never polled. Modbus RTU (serial
line and RTU over TCP) passes the Python RTU simulator suite (`tests/integration/test_rust_engine_rtu.py`). Siemens S7 passes the Python S7 simulator checks (`tests/integration/test_rust_engine_s7.py`). Mitsubishi MELSEC MC 3E passes the Python MC simulator checks (`tests/integration/test_rust_engine_melsec.py`). The engine is **read-only** toward PLCs: the driver trait has no write method.

## Build and run

```bash
cd engine
cargo build --release
./target/release/opencap-engine check  --config ../config -v           # block plan, cycle-time estimate
./target/release/opencap-engine read   "Plant A/PLC1" --config ../config  # one read, no MQTT
./target/release/opencap-engine identity "Plant A/PLC1" --config ../config
./target/release/opencap-engine run    --config ../config [--site NAME]   # poll, publish, serve the API
./target/release/opencap-engine drivers                                    # drivers this build implements (JSON)
```

`SIGTERM`/`SIGINT` stop cleanly (the link publishes `offline`, the outbox stays on disk);
`SIGHUP` or `POST /reload` re-reads the config and restarts only the channels that changed.
`/health/ready` (200 once every polled device has finished one cycle, else 503 with the devices still
waiting), `/health/live` and `/api/v1/about` are open; `/api/v1/support` and `opencap-engine info` give the
support summary, safe to paste into a public issue (D-075, D-093). Logs are JSON lines on stdout. Secrets come from the same environment variables as Python
(`OPENCAP_MQTT_PASSWORD`, `OPENCAP_API_TOKEN`, and the env vars named in `api.tokens`).
The API applies the same roles as Python: a `viewer` token may only read, an `admin` token may
also `POST /reload` and `read-once`; with no token the API is read-only and answers only
requests to `localhost` (D-062).

## Crates

| Crate | Contents |
|---|---|
| `opencap-core` | config models + loader (same schema and layering as `src/opencap/config/`), value model, tag store and change-event bus (ADR-0005), health, scheduler, driver trait, registry, channel worker, metrics, Python-compatible formatting |
| `opencap-drivers` | the driver registry: one static table (`DRIVERS`), one Cargo feature per driver crate, the driver factory |
| `opencap-serial` | serial lines: line settings, endpoints (`transport`), shared lines, local echo, TCP and local serial streams (tokio-serial) |
| `drivers/modbus_tcp` (`opencap-driver-modbus-tcp`) | Modbus TCP: address parser, block planner, decoder, tokio-modbus I/O |
| `drivers/modbus_rtu` (`opencap-driver-modbus-rtu`) | Modbus RTU on a serial line or RTU over TCP: the Modbus TCP planner/decoder, tokio-modbus RTU framing |
| `drivers/ab_logix` (`opencap-driver-ab-logix`) | Allen-Bradley Logix / Micro800 symbolic tags: native EtherNet/IP + CIP client (read services only), tag-list upload and cache, Multiple Service Packet packing and the Micro800 batching probe |
| `drivers/melsec_mc` (`opencap-driver-melsec-mc`) | Mitsubishi MELSEC MC protocol, 3E binary frame over TCP or UDP: Kepware Q Series address parser, planner, decoder, native read-only client (batch read in word units, read CPU model); whole-frame reads |
| `drivers/s7` (`opencap-driver-s7`) | Siemens S7-300/400/1200/1500 over ISO-on-TCP: Kepware address parser, planner, decoder, native S7comm read subset (COTP connect with rack/slot TSAPs, setup communication, read var, SZL read for identity); blocks split to the negotiated PDU |
| `drivers/ab_pccc` (`opencap-driver-ab-pccc`) | Allen-Bradley PCCC data tables (SLC 500, MicroLogix, PLC-5): address parser, planner, decoder and STS tables shared by the EtherNet/IP and DF1 drivers; no I/O |
| `drivers/ab_df1` (`opencap-driver-ab-df1`) | Allen-Bradley DF1 full duplex over a serial line or serial-over-TCP gateway (D-120): DLE framing with BCC or CRC-16, ACK/NAK/ENQ, duplicate detection, PCCC typed logical reads (read commands only) |
| `drivers/omron_fins` (`opencap-driver-omron-fins`) | Omron CS1/CJ1/CJ2, Rust-first: KEPServerEX address parser (the `omron_fins/addresses` vectors), planner (0101 reads, single elements pooled into 0104), decoder (BCD, word order, strings), FINS/UDP with SID matching and FINS/TCP with the node handshake; reads only |
| `opencap-plugin` | out-of-process drivers (#49): the `opencap.plugin.v1` gRPC client (compiled from `proto/` with protox, no `protoc`), the plugin process supervisor, `PluginDriver`, and the registry that routes `plugin: python` devices to the Python driver host |
| `opencap-north` | north plugin contract (`NorthPlugin`, `NorthContext`), SubDevice JSON payload, SQLite outbox, MQTT link (rumqttc), publisher (the `mqtt` plugin) |
| `north/sparkplug` (`opencap-north-sparkplug`) | Sparkplug B 3.0 edge node (`node`) and payload codec (#51, `docs/spec/payload-sparkplug.md`): types generated at build time from the vendored `proto/third_party/sparkplug_b.proto` (prost + protox, no `protoc`), topics, datatypes and packed arrays, ADR-0006 quality, birth/data/death payloads, aliases, seq/bdSeq, NCMD/DCMD classification (only Rebirth), STATE |
| `north/opcua` (`opencap-north-opcua`) | OPC UA server on async-opcua (#50, D-123): the tag store as a read-only, KEPServerEX-style address space (`ns=2;s=<channel>.<device>.<tag>`), security policies, users, fail-closed client certificates, `rejected/` cap; write, call, history-update and node-management services refused |
| `opencap-api` | axum admin API: same routes, auth and status codes as `src/opencap/api/server.py` |
| `opencap-conformance` | test harness for the shared vectors in `tests/conformance` (ADR-0014): vector loader, value codec, structural diff, `run_area`; the adapters for Modbus, scaling and payload are in its `tests/` |
| `opencap-engine` | the `opencap-engine` binary: `run`, `check`, `read`, `identity`, `drivers` |

## Drivers and Cargo features

Each driver is its own crate in `drivers/<name>`, compiled in by the feature `driver-<name>`
of `opencap-engine` (`default` = every driver that exists). A build without a driver still
knows its name: `check` reports `driver '<name>' is not compiled in` and the device is never
polled. A Modbus-only build: `cargo build -p opencap-engine --no-default-features --features driver-modbus-tcp`.

### How to add a driver crate

1. `drivers/<name>/Cargo.toml`: copy `drivers/modbus_tcp/Cargo.toml`, set `name = "opencap-driver-<name>"`;
   depend on `opencap-core` and your protocol crate only (licence per ADR-0007). `drivers/*` is already a workspace member.
2. Implement `opencap_core::driver::Driver` (pure `plan()`, one-attempt `read()`, no write path).
3. `crates/opencap-drivers/Cargo.toml`: `opencap-driver-<name> = { path = "../../drivers/<name>", optional = true }`,
   feature `driver-<name> = ["dep:opencap-driver-<name>"]`, and add it to `default`.
4. `crates/opencap-drivers/src/lib.rs`: one `DriverEntry` in `DRIVERS` (`#[cfg(feature = ...)]` on `create`).
5. `crates/opencap-engine/Cargo.toml`: `driver-<name> = ["opencap-drivers/driver-<name>"]`, add it to `default`.
6. Tests: unit tests in `drivers/<name>/tests/`; conformance vectors in `../tests/conformance/<driver>/`, run by adding an adapter to `crates/opencap-conformance/tests/` (`run_area("<area>", |input| ...)`);
   the simulator in `../tests/helpers/<proto>_sim.py`, driven against the binary by `../tests/integration/`.
7. `cargo test -p opencap-drivers` (with and without `--no-default-features`) checks the table against the features.

## Tests

```bash
cd engine && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test
cargo deny check licenses bans sources            # licence gate (ADR-0007), needs cargo-deny
```

Benchmarks of the tag store and event bus (numbers in
[`04-scale-reliability.md`](../docs/architecture/04-scale-reliability.md) §4.1):

```bash
cargo bench -p opencap-core --bench store         # Criterion micro-benchmarks
cargo bench -p opencap-core --bench store_load    # 250k tags, 8 writers, 4 subscribers (one slow), p99
```

Conformance vectors (Modbus plan and decode, scaling, payload; every failing vector is listed with its key and description):

```bash
cargo test -p opencap-conformance
```

Conformance against the Python reference (from the repository root, after `cargo build`):

```bash
.venv/bin/pytest tests/unit/test_payload_golden.py tests/integration/test_rust_engine_cli.py \
                 tests/integration/test_rust_engine_outbox.py tests/integration/test_rust_engine_api.py \
                 tests/integration/test_engine_harness.py tests/integration/test_rust_engine_rtu.py
OPENCAP_ENGINE=rust .venv/bin/pytest -m acceptance -ra     # every acceptance suite, Rust engine
```

`OPENCAP_ENGINE_BIN` points the tests at a specific binary (default: `target/release`, then
`target/debug`, with a warning that it may be stale). With `OPENCAP_ENGINE=rust`, a suite whose
drivers (`@pytest.mark.drivers(...)`) this build does not implement is skipped as "driver not
ported", and the run ends with a "Rust port gap" table (also written to the GitHub job summary
by the `acceptance-rust` CI job, D-069). The payload golden file in `crates/opencap-north/tests/golden/` is generated
by the Python engine (`python -m tests.helpers.payload_golden`).
