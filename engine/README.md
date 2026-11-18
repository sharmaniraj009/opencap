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
reported as "not implemented yet in the Rust engine" and its devices are skipped, never
polled. The engine is **read-only** toward PLCs: the driver trait has no write method.

## Build and run

```bash
cd engine
cargo build --release
./target/release/opencap-engine check  --config ../config -v           # block plan, cycle-time estimate
./target/release/opencap-engine read   "Plant A/PLC1" --config ../config  # one read, no MQTT
./target/release/opencap-engine identity "Plant A/PLC1" --config ../config
./target/release/opencap-engine run    --config ../config [--site NAME]   # poll, publish, serve the API
```

`SIGTERM`/`SIGINT` stop cleanly (the link publishes `offline`, the outbox stays on disk);
`SIGHUP` or `POST /reload` re-reads the config and restarts only the channels that changed.
Logs are JSON lines on stdout. Secrets come from the same environment variables as Python
(`OPENCAP_MQTT_PASSWORD`, `OPENCAP_API_TOKEN`, and the env vars named in `api.tokens`).
The API applies the same roles as Python: a `viewer` token may only read, an `admin` token may
also `POST /reload` and `read-once`; with no token the API is read-only and answers only
requests to `localhost` (D-062).

## Crates

| Crate | Contents |
|---|---|
| `opencap-core` | config models + loader (same schema and layering as `src/opencap/config/`), value model, cache, health, scheduler, driver trait, registry, channel worker, metrics, Python-compatible formatting |
| `opencap-drivers` | `modbus_tcp` (address parser, block planner, decoder, tokio-modbus I/O) and the driver factory |
| `opencap-north` | SubDevice JSON payload, SQLite outbox, MQTT link (rumqttc), publisher |
| `opencap-api` | axum admin API: same routes, auth and status codes as `src/opencap/api/server.py` |
| `opencap-engine` | the `opencap-engine` binary: `run`, `check`, `read`, `identity` |

## Tests

```bash
cd engine && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test
cargo deny check licenses bans sources            # licence gate (ADR-0007), needs cargo-deny
```

Conformance against the Python reference (from the repository root, after `cargo build`):

```bash
.venv/bin/pytest tests/unit/test_payload_golden.py tests/integration/test_rust_engine_cli.py \
                 tests/integration/test_rust_engine_outbox.py tests/integration/test_rust_engine_api.py
OPENCAP_ENGINE=rust .venv/bin/pytest -m acceptance tests/integration/test_phase1_acceptance.py -s
```

`OPENCAP_ENGINE_BIN` points the tests at a specific binary (default: `target/release`, then
`target/debug`). The payload golden file in `crates/opencap-north/tests/golden/` is generated
by the Python engine (`python -m tests.helpers.payload_golden`).
