# RFC 0001: the Rust engine

- Status: Proposed (with the `rust-engine` PR that adds `engine/`)
- Date: 2026-11-17
- Author: `rust-engine` session
- Decides within: [ADR-0002](../adr/0002-rust-engine-python-reference.md) (Rust engine, Python reference),
  [ADR-0003](../adr/0003-shared-config-schema.md) (shared config), [ADR-0004](../adr/0004-driver-contract-and-plugin-model.md)
  (driver contract), [ADR-0005](../adr/0005-north-plugins-tag-store-event-bus.md) (tag store, event bus),
  [ADR-0007](../adr/0007-oss-licence-policy.md) (licences), [ADR-0011](../adr/0011-store-and-forward-outbox-per-agent.md)
  (outbox), [ADR-0014](../adr/0014-conformance-by-simulators-and-vectors.md) (conformance)
- Target contract it implements and refines: [`docs/architecture/03-deep-dive.md`](../architecture/03-deep-dive.md)

## Summary

opencap replaces KEPServerEX with open-source software. The production engine moves from
Python to Rust; the Python code stays the reference implementation, and its simulators and
acceptance suites are the specification. This RFC describes the Rust engine's
architecture, what we reuse and what we build (with licences), how SCADA systems connect,
how the desktop apps are packaged, and the plan to reach KEPServerEX feature parity.

The PR that carries this RFC also delivers the first vertical slice in `engine/`:

- a Cargo workspace (`opencap-core`, `opencap-drivers`, `opencap-north`, `opencap-api`,
  `opencap-engine`);
- the Rust `Driver` trait, the channel worker with the Python fault rules, the cache, health,
  scheduler and metrics;
- the Modbus TCP driver (same address parser, block planner and word-order decoder as
  `src/opencap/drivers/modbus_tcp.py`) on tokio-modbus;
- the SubDevice JSON payload, **byte-for-byte identical** to Python's, the SQLite outbox
  (same file format), the MQTT link on rumqttc (LWT, status topics, `mqtt.topic_prefix`);
- the admin API on axum (same routes, auth and status codes);
- the `opencap-engine` binary (`run`, `check`, `read`, `identity`);
- conformance: the **Phase 1 acceptance suite passes against the Rust binary** with the same
  simulators, broker and assertions (`OPENCAP_ENGINE=rust`), plus golden payloads,
  `check -v` and `read` output parity and outbox interchange in both directions.

## 1. Goals and non-goals

**Goals**
1. One engine binary per node (Linux x86_64/arm64, Windows x64) that meets the NFRs of
   [01-requirements.md §1.2](../architecture/01-requirements.md#12-non-functional-requirements)
   (NFR-1 2,000 devices / 250k tags per node; NFR-2 10k tags at 1 s in ≤ 64 MB RSS on a 1 GB
   ARM64 box).
2. **Interchangeable with the Python engine** on a node during the migration: same config
   files, same API, same payload bytes, same outbox file, same MQTT topics.
3. The same behaviour rules as Python (`docs/decisions.md` D-001…D-031), proven by the same
   simulators and suites, not by re-reading code.
4. Read-only toward PLCs ([ADR-0008](../adr/0008-read-only-by-default.md)): no write path in the type system.

**Non-goals (for this RFC)**
- Writes to PLCs (RFC 0002, owner decision).
- Replacing the Python simulators or acceptance suites (they stay Python permanently).
- A new config format (project file v2 is [ADR-0013](../adr/0013-project-file-v2.md); the
  engine reads today's four files and will read v2 when it lands).

## 2. Architecture

### 2.1 Process model

```
                         opencap-engine (one process, tokio multi-thread runtime)
 ┌───────────────────────────────────────────────────────────────────────────────────────┐
 │ config loader ──► Registry (DevicePlan per device: rate groups → Plan{blocks,invalid}) │
 │                                                                                        │
 │ ChannelWorker "Plant A"            ChannelWorker "Plant B"         (one per channel)   │
 │  ├─ wire lock (FIFO, 1 request on the wire per channel)                                │
 │  ├─ device task PLC1 ─┐ Driver (connect/read/close, one attempt)                        │
 │  ├─ device task PLC2 ─┘   ↳ retries, reconnect, backoff, demotion, probe, scaling       │
 │  └─ housekeeping (stale at 3 × rate, device.stats)                                     │
 │            │ cache.update(batch)                     ▲ read_once (API, CLI)             │
 │            ▼                                         │                                 │
 │       ValueCache (snapshot, version) ──► Publisher (interval / on_change / status)     │
 │                                              │ publish() = append to the outbox        │
 │                                              ▼                                         │
 │                    StoreAndForward (SQLite WAL) ──► MqttLink (rumqttc, windowed PUBACK)│
 │                                                                                        │
 │  admin API (axum): /health /metrics /channels /devices/{id}[/values|/read-once] /reload│
 │  signals: SIGTERM/SIGINT stop, SIGHUP reload (POST /reload on Windows)                 │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

Every arrow above is the Python design ([02-high-level-design.md](../architecture/02-high-level-design.md),
`docs/phase1_contracts.md`) translated, not redesigned. Where Rust needs a different
mechanism for the same rule, §2.9 lists it.

### 2.2 Crates

| Crate | Python counterpart | Contents |
|---|---|---|
| `opencap-core` | `config/`, `core/`, `drivers/base.py`, `metrics.py` | serde config models and loader, `Value`/`Quality`/`TagValue`, `ValueCache`, `DeviceHealth`/`Backoff`, `DeviceSchedule`, `Driver`/`Block`/`Plan`/`DriverError`, `Registry`/`DevicePlan`, `ChannelWorker`, Prometheus metrics, Python-compatible formatting (`pyfmt`) |
| `opencap-drivers` | `drivers/modbus_tcp.py`, `drivers/__init__.py` | `modbus_tcp` (params, address, plan, decode, tokio-modbus I/O) and the driver factory; every other Python driver name is "not implemented yet in the Rust engine" |
| `opencap-north` | `north/` | `payload` (SubDevice JSON), `saf` (outbox), `mqtt` (link), `publisher` |
| `opencap-api` | `api/server.py` | axum router, bearer auth, the `EngineView` trait the engine implements |
| `opencap-engine` | `__main__.py`, `core/runtime.py`, `core/check.py` | the binary, the runtime (`Poller`), `check` |

[ADR-0004](../adr/0004-driver-contract-and-plugin-model.md) §2 says drivers are crates in
`engine/drivers/<name>` behind Cargo features. This slice keeps one `opencap-drivers` crate
(the crate list this work was given) with one module per driver; it splits into per-driver
crates when the second driver lands (#43), so heavy dependencies (OPC UA, TLS stacks) stay
optional. The lead reconciles the two layouts.

### 2.3 Runtime and concurrency

- **tokio multi-thread runtime.** Channels are independent tasks, so a dead PLC on one
  channel never delays another (D-015), and the work spreads over all cores (Python: one
  event loop).
- **One request on the wire per channel.** Each device has its own task; every `connect`,
  `read` and `close` takes the channel's wire lock (`tokio::sync::Mutex`, which is FIFO), so
  devices interleave request by request exactly as in Python. Tested with a concurrency
  probe (`crates/opencap-core/tests/worker.rs`: max 1 in flight per channel, 2 across two
  channels).
- **Safety net** on every driver call: `timeout_ms × 1.25 + 0.1 s` (`× block.wire_requests()`
  for reads). When it fires, the future is dropped, the call counts as a CommError and the
  connection is closed (the driver's state is unknown).
- **Status never waits on the wire.** The API and the publisher read a per-device status
  snapshot (state, last_good, errors, cycle_ms, last_error, connected) that the device task
  refreshes after every connect, drop and cycle. Only `read-once` waits for I/O, as intended.
  This matters for the conformance adapter, which polls `/devices/{id}` while the simulators
  share its event loop.
- **Blocking libraries** (future FFI drivers, e.g. libplctag) get one dedicated OS thread per
  channel with a job queue (the Python channel executor, D-015), not `spawn_blocking`.
- **Panics** in a device step are caught at the task boundary (`catch_unwind`), logged as
  `device.loop_error`, and the loop resumes after 1 s; an `on_cycle` callback that panics is
  logged and ignored.

### 2.4 Channel, device and rate-group model, and the fault rules

Ported rule by rule from `src/opencap/core/channel_worker.py`, `health.py` and
`scheduler.py` (D-015):

| Rule | Implementation |
|---|---|
| Rate groups = enabled tags grouped by `max(tag.scan_ms or device.scan_ms, min_scan_ms)`, fastest first | `Device::rate_groups()` (note Python's `or`: `scan_ms: 0` means "device rate") |
| Start jitter U(0, 5 % of rate), one-sided; ties go to the faster rate | `DeviceSchedule` |
| Overrun: missed slots dropped, next due `max(ended, started + rate)`; counted | `next_due_after()`, `poll_overrun_total` |
| Cycle outcome good / failed / neutral | `Runner::cycle()` |
| `offline` → `ok`; `demoted` after `demote_after` failed cycles (connection closed); one probe after `demote_for_ms`; the probe stops at its first failed block | `DeviceHealth`, `poll_step()` |
| CommError: retry up to `retries`; a socket closed by `close_on_timeout` is reopened at once inside the same budget | `read_block()` |
| ConnectionLost on a stale socket: reconnect and repeat the same attempt; right after a fresh connect: end the cycle, remaining blocks `bad_comm`, nothing sent | `read_block()`, `cycle()` |
| Connect backoff 1 s → 60 s, reset on success; no request while backing off | `Backoff` |
| BlockConfigError: tags `bad_config`, block never read again until reload | `disable_block()` |
| Plan-invalid tags `bad_config` once, at registration | `ensure_registered()` |
| Scaling on good numbers (element-wise on arrays), scaling error → `bad_decode` | `postprocess()`, `scale_value()` |
| Stale after 3 × effective rate, from a per-channel tick (0.02 … 1 s) | `housekeeping()` |
| `read_once`: works on disabled devices, ignores demotion, one connect attempt even in backoff, feeds health | `ChannelWorker::read_once()` |
| Driver without I/O or that cannot be built: device not polled, reason in `last_error` | `Unsupported`, `not_polled_reason()` |
| Periodic `device.stats` INFO line (D-023) | `housekeeping()` |

### 2.5 Driver trait

`crates/opencap-core/src/driver.rs` mirrors `drivers/base.py` and the target contract of
[03 §3.3](../architecture/03-deep-dive.md#33-driver-contract):

```rust
pub trait Block: Debug + Send + Sync + 'static {
    fn tags(&self) -> &[String];              // sorted names served by this wire request
    fn describe(&self) -> String;             // `check -v`, e.g. "FC03 44..75 (32 regs)"
    fn wire_requests(&self) -> usize { 1 }
    fn as_any(&self) -> &dyn Any;             // the driver downcasts to its own block type
}
pub struct Plan { pub blocks: Vec<Arc<dyn Block>>, pub invalid: BTreeMap<String, String> }

#[async_trait]
pub trait Driver: Send {
    fn name(&self) -> &'static str;
    fn plan(&self, tags: &[&Tag]) -> Plan;                     // PURE: no I/O
    fn connected(&self) -> bool { false }
    fn reports_connection(&self) -> bool { false }              // Python: the class overrides `connected`
    fn connect_timeout(&self) -> Option<Duration> { None }      // e.g. a Logix tag-list upload
    async fn connect(&mut self) -> Result<(), DriverError>;     // ONE attempt
    async fn close(&mut self);                                  // idempotent, never fails
    async fn read(&mut self, block: &dyn Block) -> Result<HashMap<String, TagValue>, DriverError>; // ONE attempt
    async fn identity(&mut self) -> Map<String, Json> { Map::new() }
}
pub enum DriverError { Comm(String), ConnectionLost(String), BlockConfig(String), NotImplemented(String), Other(String) }
pub type DriverFactory = Arc<dyn Fn(&Device) -> Result<Box<dyn Driver>, String> + Send + Sync>;
```

- **No write method.** A driver cannot be asked to write. Modbus sends only FC01/02/03/04
  and FC43/14.
- Per-tag outcomes are `TagValue`s (`good` with the raw value, or `bad_decode`), exactly the
  Python contract. 03 §3.3's `Reading` enum and `browse()` are compatible additions for the
  next drivers: `browse` arrives with the first driver that can browse (Logix, #43; OPC UA
  client, #46), push drivers add `subscribe` (#46).
- `DriverError` carries the Python class names (`CommError`, `ConnectionLost`,
  `BlockConfigError`, `NotImplementedError`) so `last_error` texts read the same in both
  engines.

### 2.6 Cache

`ValueCache` keeps the Python semantics (contract §2): register pre-fills enabled tags as
`(None, stale, None)`; a good value replaces value, quality and `source_ts`; any other
quality keeps the last value and last good timestamp; `version()` bumps on every change;
snapshots are copies in tag order. One short-held mutex replaces "one event loop, no locks".

[ADR-0005](../adr/0005-north-plugins-tag-store-event-bus.md)'s tag store (one slot per device,
one writer, bounded broadcast of `ChangeBatch` with conflation) replaces it in #41 without
changing any rule above; the publisher then subscribes instead of being woken by `on_cycle`.
The second timestamp (`server_ts`, [ADR-0006](../adr/0006-quality-and-timestamps.md)) arrives
with the store. Timestamps are truncated to microseconds, Python's precision, so the two
engines report identical `last_good` and `source_ts` strings.

### 2.7 North side

- **Payload** (`opencap-north::payload`): same keys, order and rules as
  `north/payload.py` (D-017). Byte identity needs CPython's float rules, implemented in
  `opencap-core::pyfmt`: `round(x, n)` (exact decimal rounding, ties to even; Rust's exact
  `{:.n}` formatting has the same semantics, including negative `n`), `float.__repr__`
  (shortest round-trip digits, exponent form outside `1e-4 ≤ |x| < 1e16`, two-digit
  exponents), and `json.dumps(..., ensure_ascii=False)` string escaping. `DateTime` uses
  chrono-tz (IANA database compiled in, so no host tz data is needed).
- **Outbox** (`saf`): same SQLite schema (`outbox(id AUTOINCREMENT, topic, payload, qos,
  retain, size, created)`), WAL, `synchronous=NORMAL`, oldest-first trim at `max_bytes`,
  delete after acknowledgement. A file written by one engine is drained by the other
  (tested both ways).
- **MQTT link** (`mqtt`): outbox-always (D-016). Each connection gets a new rumqttc client
  and event loop (clean session: in-flight state is never reused). Messages go out in
  windows of 32 in id order; the PUBACK of each packet id is matched to its outbox row
  through the order of rumqttc's `Outgoing::Publish(pkid)` events; a row is deleted only
  after its PUBACK. LWT `<prefix>/poller/<instance>/status` = `offline` (retained), `online`
  after each connect, `offline` on a clean stop. Backoff 1 s → 30 s.
- **Publisher**: interval mode (first message one interval after start, missed slots
  skipped), on_change mode (deadband on the rounded, last published value; heartbeat),
  retained status topics at start, on state change and every 60 s; no data until the first
  good read.

### 2.8 Configuration

The engine reads the same four files with the same layering as `config/loader.py`
(D-012): devices.json → overrides (fields, `params` merge, per-tag fields) → publish
settings (devices.json → `devices_map.yaml` `defaults` → `overrides.yaml` `publish` → the
device's map entry) → default topic with `mqtt.topic_prefix` (D-028). Models are hand-written
serde types with `deny_unknown_fields` (= pydantic `extra="forbid"`) and the same defaults
and validators (unique tag names, unique device ids, topic prefix rules). Warnings
(unknown ids, stubs, unmapped devices, shared topics/SubDeviceIds) have the **same text**,
which the `check` parity test compares. When the JSON Schema of
[ADR-0003](../adr/0003-shared-config-schema.md) lands (#2), #39 replaces the hand-written types
(or adds a drift test) and runs the shared config fixtures.

### 2.9 Where Rust differs from Python on purpose

These are recorded in `docs/decisions.md` (D-032) as well.

| Topic | Python | Rust | Why |
|---|---|---|---|
| Unknown `driver` value in devices.json | pydantic rejects the whole file | loads; the device gets "unknown driver" (not polled) | the Python `DriverName` list grows in parallel PRs; a new name must not stop the Rust engine from loading a node's config |
| Invalid driver `params` | `ValueError` at plan time (process stops) | plan error for that device (reported by `check`, device not polled) | one bad device must not take down a node |
| Value coercion | pydantic lax mode (`"5"` → 5, `2000.0` → 2000) | serde: exact JSON/YAML types | generated files use exact types; lax inputs are hand-edit mistakes |
| YAML dialect | PyYAML (YAML 1.1: `yes`/`no`/`on`/`off` are booleans) | serde_yaml_ng (YAML 1.2: they are strings) | write `true`/`false`; the shipped templates do |
| Peer close of an idle Modbus socket | pymodbus flags it, reopen before the read | seen on the next request (I/O error → ConnectionLost → reopen, same attempt) | same outcome, one failed write on a dead socket |
| `POST /devices/{id}/read-once` on an unusable device | uncaught RuntimeError → 500 | 500 with `{"detail": …}` | same status, readable body |
| `/metrics` | prometheus_client (also `_created` samples) | `prometheus` crate, same names, labels and buckets | `_created` samples are not used |
| Log lines | structlog JSON (`event` key) | tracing JSON (`message` key), same event names | consumers key on the event name |
| Test-only `OPENCAP_PUBLISH_TAP` | — | appends every message handed to the link to a file | the conformance adapter's equivalent of the Python spy on `link.publish` |

## 3. Build vs reuse

Decision words as in [oss-reuse.md](../architecture/oss-reuse.md): **Adopt**, **Evaluate**
(decided by the named issue with a benchmark or conformance run), **Test-only**,
**Isolated** (separately shipped out-of-process plugin, owner approval), **Avoid**,
**Build**. Licence rules: [ADR-0007](../adr/0007-oss-licence-policy.md).

"Checked" says how the licence was checked: **lock** = the crate's declared licence at the
pinned version in `engine/Cargo.lock`, enforced by `cargo deny check licenses` in CI;
**manifest** = the crate's declared licence at the version shown (`cargo info`); **page** =
the project's own site or repository page (the PR that adopts it links the `LICENSE` file at
the pinned version); **verify** = not checked yet. The lead merges this table into
oss-reuse.md.

### 3.1 Engine foundations (in this PR)

| Purpose | Library | Licence (checked) | Maturity | Decision |
|---|---|---|---|---|
| Async runtime | tokio 1.54 | MIT (lock) | the de-facto Rust async runtime | Adopt |
| Async trait objects | async-trait 0.1 | MIT OR Apache-2.0 (lock) | ubiquitous | Adopt |
| Modbus TCP client (and server, later) | tokio-modbus 0.17 | MIT OR Apache-2.0 (lock) | maintained by slowtec, used in production; client and `tcp-server` feature | Adopt |
| MQTT 3.1.1/5 client | rumqttc 0.25 | Apache-2.0 (lock) | widely used (bytebeam); explicit event loop gives the packet ids we need | Adopt |
| TLS for MQTT | rustls 0.23 + ring 0.17 | Apache-2.0 / ISC (lock) | standard Rust TLS; ring avoids a cmake/NASM toolchain | Adopt |
| SQLite outbox | rusqlite 0.41, bundled SQLite | MIT; SQLite public domain (lock) | mature | Adopt |
| HTTP API | axum 0.8 (hyper, tower) | MIT (lock) | tokio project | Adopt |
| Metrics | prometheus 0.14 | Apache-2.0 (lock) | mature (TiKV); classic 0.0.4 text format, same exposition as Python | Adopt (oss-reuse lists prometheus-client; either works, this one matches Python's format) |
| Config | serde, serde_json, serde_yaml_ng 0.10 | MIT OR Apache-2.0; MIT (lock) | serde_yaml_ng is the maintained fork of the archived serde_yaml | Adopt |
| Time zones | chrono 0.4 + chrono-tz 0.10 | MIT OR Apache-2.0 (lock) | IANA database compiled in | Adopt |
| CLI | clap 4 | MIT OR Apache-2.0 (lock) | standard | Adopt |
| Logging | tracing, tracing-subscriber (JSON) | MIT (lock) | standard | Adopt |
| Slug (topic levels) | unicode-normalization | MIT OR Apache-2.0 (lock) | standard | Adopt |
| Error context in the binary | anyhow 1.0.104 | MIT OR Apache-2.0 ([LICENSE-MIT](https://docs.rs/crate/anyhow/1.0.104/source/LICENSE-MIT)) | ubiquitous | Adopt |
| Typed errors in libraries | thiserror 2.0.21 | MIT OR Apache-2.0 ([LICENSE-MIT](https://docs.rs/crate/thiserror/2.0.21/source/LICENSE-MIT)) | ubiquitous | Adopt |
| Publish-tap and payload encoding | base64 0.22.1 | MIT OR Apache-2.0 ([LICENSE-MIT](https://docs.rs/crate/base64/0.22.1/source/LICENSE-MIT)) | ubiquitous | Adopt |
| Stream and future combinators (`catch_unwind`) | futures-util 0.3.34 | MIT OR Apache-2.0 ([LICENSE-MIT](https://docs.rs/crate/futures-util/0.3.34/source/LICENSE-MIT)) | ubiquitous | Adopt |
| Ordered maps (config key order) | indexmap 2.14.2 | Apache-2.0 OR MIT ([LICENSE-MIT](https://docs.rs/crate/indexmap/2.14.2/source/LICENSE-MIT)) | ubiquitous | Adopt |
| Start jitter | rand 0.9.5 | MIT OR Apache-2.0 ([LICENSE-MIT](https://docs.rs/crate/rand/0.9.5/source/LICENSE-MIT)) | ubiquitous | Adopt |
| Address and slug parsing | regex 1.13.1 | MIT OR Apache-2.0 ([LICENSE-MIT](https://docs.rs/crate/regex/1.13.1/source/LICENSE-MIT)) | ubiquitous | Adopt |
| axum service utilities | tower 0.5.3 | MIT ([LICENSE](https://docs.rs/crate/tower/0.5.3/source/LICENSE)) | ubiquitous | Adopt |
| Licence gate | cargo-deny 0.21 | MIT OR Apache-2.0 (manifest) | standard | Test-only (CI) |

`cargo deny check licenses` over the whole dependency graph passes with the allow-list
MIT, Apache-2.0 (incl. LLVM exception), BSD-2/3, ISC, Zlib, Unicode-3.0 (`engine/deny.toml`);
dual-licensed crates resolve to their permissive option. Every direct dependency of the
workspace is listed in the table above; each licence link points at the licence file inside the
published crate at the version pinned in `engine/Cargo.lock` (checked against the crate sources).

### 3.2 Southbound drivers (FR-1)

| KEPServerEX driver | Candidates (licence, checked) | Maturity | Decision |
|---|---|---|---|
| Modbus TCP/IP Ethernet | tokio-modbus (MIT OR Apache-2.0, lock) | see above | **Adopt — done in this PR**: our planner/decoder, tokio-modbus framing |
| Modbus Serial (RTU/ASCII), RTU over TCP | tokio-modbus `rtu` + tokio-serial 5.5 (MIT, manifest) | maintained | Adopt (#47); ASCII framing: Build (small) |
| ControlLogix/CompactLogix, Micro800 (CIP symbolic) | libplctag via `plctag` 0.4 bindings (libplctag MPL-2.0 or LGPL-2.0+, page; bindings MIT, manifest); rust-ethernet-ip 1.2 (MIT, manifest); rseip (MIT, verify) | libplctag: mature C library, widely used; rust-ethernet-ip: young (1.x), CompactLogix/ControlLogix per its description; rseip: small | **Evaluate in #43**, criteria: control of Multiple Service Packet batching and the probe (D-025), tag-list upload once per instance, read-only guard, passes `cip_sim.py` unmodified. Default if no library gives that control: **Build** a native CIP read subset (the Python simulator already pins the wire format); libplctag (MPL-2.0 permits static linking) as fallback |
| ControlLogix model 23 (SLC/MicroLogix/PLC-5 PCCC) | libplctag (as above) | — | Evaluate with #43/#48; Build on the shared CIP layer otherwise (file-directory check, D-027) |
| DF1 serial | — | — | Build (#53) |
| Siemens TCP/IP Ethernet (S7comm) | snap7 (LGPL-3.0, page; C library, upstream inactive, verify); rust-snap7 (MulanPSL-2.0, manifest); `s7` crate (no licence in manifest) | — | **Build** a native read subset (COTP, S7 setup, read var; #44). snap7 cannot be linked statically (ADR-0007); MulanPSL is not on the allow-list; no licence = never. python-snap7's server stays Test-only |
| Mitsubishi Ethernet (MC 3E/4E) | — | — | Build (#45) |
| OPC UA Client | async-opcua 0.19 (MPL-2.0, manifest); open62541 1.x via the `open62541` 0.12 crate (MPL-2.0, manifest) | async-opcua: active successor of locka99/opcua, client + server; open62541: mature C stack, wide use | Evaluate (#46, shared with the server gate #50) |
| Omron FINS | — | — | Build (#54) |
| BACnet/IP | bacnet-stack (GPL-2.0 with linking exception, verify) | mature C | Evaluate in the BACnet spike (#15); Build a read subset otherwise |
| DNP3 master | opendnp3 (Apache-2.0, page; **archived 2022-09-01**, C++); stepfunc `dnp3` 1.8 and `rodbus` 1.6 (non-commercial, non-production licence agreement: page for dnp3, `LICENSE.txt` for rodbus) | opendnp3: mature but unmaintained; stepfunc: maintained but not OSS | Evaluate opendnp3 via FFI in the DNP3 spike (#14); **Avoid** stepfunc dnp3/rodbus (not open source, ADR-0007); Build a native level-2 subset if the spike rejects opendnp3 |
| IEC 60870-5-104 | lib60870-C (GPL-3.0 or commercial, page) | mature | Isolated, or Build natively (IEC 104 spike #16) |
| EtherNet/IP generic (CIP explicit) | — | — | Build on the shared CIP layer |
| Beckhoff TwinCAT ADS | `ads` crate (MIT OR Apache-2.0, verify) | small | Evaluate |
| GE Ethernet (SRTP) | — | — | Build, on demand |
| SNMP | csnmp / snmp2 (verify) | small | Evaluate |
| MQTT Client (topics as tags) | rumqttc (Apache-2.0, lock) | — | Adopt |
| OPC DA Client (Windows) | windows-rs COM (MIT OR Apache-2.0, verify); `opc_da` 0.3 (MIT, manifest) | opc_da: young | Evaluate with the OPC DA RFC (#18) |
| ODBC Client | odbc-api (MIT, verify) | maintained | Evaluate |
| Simulator, Memory Based | — | — | Build (M2; demos, UI and desktop tests) |
| IEC 61850 MMS | libiec61850 (GPL-3.0 or commercial, page) | mature | Isolated only |
| Fanuc Focas | closed vendor library | — | Out (cannot be redistributed) |

**Apache PLC4X** (Apache-2.0, page) describes many of these protocols in its `mspec` DSL and
generates Java, Go, C and Python code; there is no maintained Rust runtime. Decision:
**reference** — native drivers use PLC4X's mspec files (and its test frames) as a
cross-check of field layouts; we do not embed a JVM or Go runtime.

### 3.3 Northbound servers and agents (FR-7, FR-8)

| Feature | Candidates (licence, checked) | Maturity | Decision |
|---|---|---|---|
| MQTT agents, SubDevice JSON | rumqttc + own payload (Apache-2.0, lock) | — | **Adopt — done in this PR** |
| KEPServerEX IoT Gateway JSON, flat JSON, templates | own encoders | — | Build (#36 format) |
| Store-and-forward per agent | rusqlite (MIT, lock) | — | **Adopt — done** (one file per durable agent, ADR-0011) |
| MQTT Sparkplug B edge node | `srad` 0.7 (MIT OR Apache-2.0, manifest); `sparkplug-rs` 0.5 (EPL-2.0, manifest); prost + `sparkplug_b.proto` from the Eclipse Sparkplug spec (EPL-2.0 file, verify) | srad: young, full edge-node model; sparkplug-rs: small | Evaluate srad first (permissive) in #51; fallback Build on prost with the unmodified `.proto` (allowed under ADR-0007). Eclipse Tahu / Sparkplug TCK Test-only |
| OPC UA server | async-opcua (MPL-2.0, manifest); open62541 (MPL-2.0, manifest) | see 3.2 | **Evaluate in #50** against the gate: 100k-node address space within the NFR memory budget, subscriptions at target rate, Eclipse Milo (Ignition's stack) and open62541 clients pass L1. KEPServerEX-compatible NodeIds (ADR-0009) |
| Modbus TCP server (read-only register map) | tokio-modbus `tcp-server` (MIT OR Apache-2.0, lock) | — | Adopt (#52); write function codes answered with exception 01 |
| DNP3 outstation | as DNP3 master | — | same decision as 3.2 (#14) |
| IEC 104 server | as IEC 104 | — | same decision as 3.2 (#16) |
| REST server (read values) | axum (MIT, lock) | — | Adopt (the admin API already serves `/devices/{id}/values`) |
| REST client (HTTP push) | hyper client (MIT, lock) or reqwest (MIT OR Apache-2.0, verify) | mature | Adopt when the agent is built |
| OPC DA 2.05a/3.0 server (bridge) | windows-rs (MIT OR Apache-2.0, verify); OPC Foundation Core Components (OPC Foundation redistributable licence) | — | Build a separate Windows bridge process (RFC 0004, #18); **owner decision** on redistributing Core Components |
| OPC AE | as OPC DA | — | after OPC DA |
| SuiteLink, FastDDE/DDE, iFIX native, ThingWorx native | — | proprietary | Not offered (ADR-0009) |

### 3.4 KEPServerEX plug-ins and platform features (FR-4 … FR-13)

| KEPServerEX feature | Candidates (licence, checked) | Decision |
|---|---|---|
| Advanced Tags (derived) | own expression grammar (Pratt parser); `rhai` (MIT OR Apache-2.0, verify) | Decide in the derived-tags spec (#23), implement in Rust (#55); Avoid `evalexpr` until its licence is verified (oss-reuse) |
| Alarms & Events | — | Build (spec #24) on the tag store's event bus |
| DataLogger | `sqlx` (MIT OR Apache-2.0, verify) for PostgreSQL/MySQL; `tiberius` (MIT OR Apache-2.0, verify) for SQL Server; rusqlite (lock) for the local historian; InfluxDB line protocol over HTTP (own) | Adopt per sink after the data-logger spec (#25) |
| Local Historian | rusqlite (lock) | Build on SQLite with retention; revisit with OPC HDA |
| Configuration API | axum (lock); OpenAPI via `utoipa` (MIT OR Apache-2.0, verify) | Adopt axum; Evaluate utoipa with the OpenAPI contract issue (#32) |
| Redundancy Master | — | Build (RFC 0003, #20; ADR-0012) |
| Media Level Redundancy | — | Build in the core (alternate host list per device) |
| Scheduler | `croner` (MIT, verify) | Evaluate (low priority) |
| Security Policies, User Manager | `argon2` (MIT OR Apache-2.0, verify) | Build roles and audit (security RFC #21); Adopt argon2 |
| Diagnostics: system tags, event log, comm capture | tracing, prometheus (lock) | Build (#56) |
| Quick Client | the web UI over the API | Build (web-ui PR, #31) |
| KEPServerEX project import | Python importer (reference) | Keep in Python until a Rust importer has parity (06 §6.3) |
| Windows service | `windows-service` (MIT OR Apache-2.0, verify) | Adopt for the engine (#57) |
| Desktop shell | Tauri 2.9 (Apache-2.0 OR MIT, manifest) | Adopt (ADR-0010, §5) |
| EFM Exporter, OPC .NET (Xi), OPC HDA | — | Out of scope (01 §1.1) |

### 3.5 Reusing an existing OSS gateway instead of building one

| Project | Licence (checked) | What it is | Why not the core | Use |
|---|---|---|---|---|
| EMQ Neuron | core and Modbus/MQTT/eKuiper plugins LGPL-3.0, other drivers commercial (page) | C industrial gateway | the open edition has Modbus and MQTT only; the drivers we need are commercial; LGPL cannot be linked into our binary | Avoid |
| Apache PLC4X | Apache-2.0 (page) | protocol libraries for Java/Go/C/Python | no Rust runtime; JVM/Go footprint on 1 GB boxes | Reference (mspec), see 3.2 |
| EdgeX Foundry | Apache-2.0 (verify) | Go microservices platform | a dozen services where we need one binary; no KEPServerEX semantics (block planning, demotion, import) | Avoid; integrate over MQTT/REST |
| Node-RED | Apache-2.0 (verify) | flow-based integration tool | not a deterministic polling engine | Integration target (MQTT, REST, OPC UA) |
| Telegraf | MIT (verify) | metrics agent with Modbus/OPC UA inputs | no per-device health, demotion or SCADA servers | Avoid |
| ThingsBoard IoT Gateway | Apache-2.0 (verify) | Python connectors | same performance envelope as our Python engine | Avoid |
| Eclipse Kura | EPL-2.0 (verify) | Java/OSGi gateway framework | JVM footprint, OSGi complexity | Avoid |
| OpenPLC | GPL-3.0 (verify) | soft PLC runtime | a PLC, not a gateway; GPL | Avoid (possible test peer) |
| FUXA | MIT (verify) | web SCADA/HMI | a consumer of OPC UA/MQTT, not a server | Integration target |

Conclusion: no existing gateway gives KEPServerEX semantics under a licence and footprint we
can ship, so opencap builds the engine and reuses **libraries** (3.1–3.4), not a gateway.

## 4. SCADA connectivity

Strategy: [ADR-0009](../adr/0009-scada-connectivity-strategy.md) (OPC UA first,
KEPServerEX-compatible naming); the living matrix with its *verify* marks is
[scada-compatibility.md](../architecture/scada-compatibility.md). This section fixes what
the Rust engine must provide for each server.

### 4.1 North servers in the Rust engine

| Server | Who needs it | Engine work | Acceptance (L1 in CI, [NFR-15](../architecture/01-requirements.md#12-non-functional-requirements)) | Issue |
|---|---|---|---|---|
| **OPC UA server** | Ignition, WinCC V7/V8/Unified/OA, FactoryTalk (via Linx Gateway/Optix), AVEVA (via OI Gateway), iFIX, CIMPLICITY, Citect, zenon, VTScada, PI, cloud gateways | address space from the registry (`ns=<idx>;s=Channel.Device.Tag`, optional ns=2), Read/Browse/TranslateBrowsePaths, subscriptions with data-change filters and deadband, ADR-0006 status codes, security policies and user tokens, ServiceLevel for redundancy | Eclipse Milo, open62541 and asyncua clients browse 100k nodes, subscribe, and read the same values as the SubDevice payload | #50 |
| **MQTT Sparkplug B** | Ignition (MQTT Engine module), cloud | edge node per channel or node, birth/death, aliases, rebirth, primary-host awareness, outbox with BIRTH before drain | Eclipse Tahu host + Sparkplug TCK pass | #51 |
| **Modbus TCP server** | any SCADA (universal fallback), Geo SCADA, Citect, VTScada | register map over the store (configurable addresses and types, same encoder as the driver in reverse), read function codes only | pymodbus client reads every mapped tag with the right word order; writes refused (exception 01) and counted | #52 |
| **DNP3 outstation** | utilities, Geo SCADA, VTScada | point map (binary/analog inputs, classes, events from the event bus) | OSS DNP3 master (opendnp3 tools or the spike's choice) integrity poll and event poll | after #14 |
| **IEC 60870-5-104 server** | energy SCADA, zenon | as DNP3 | OSS IEC 104 master test peer (Test-only) | after #16 |
| **REST** | Node-RED, IT systems, Grafana | read-only values/health (exists), later push agent | HTTP client tests | — |
| **OPC DA bridge** | FactoryTalk View SE/ME, older WinCC, iFIX, AVEVA via OI Gateway/FSGateway | separate Windows process implementing an OPC DA 2.05a/3.0 server on top of the engine's OPC UA server or API; item ids = tag paths | an OPC DA test client on Windows CI reads every item | RFC 0004 (#18) |

### 4.2 Compatibility matrix

✓ primary path, ○ alternative, ✗ not possible, — not applicable. *verify* rows are
confirmed by the SCADA research issue (#17) before they are documented to users.

| SCADA / consumer | OPC UA | OPC DA (bridge) | Sparkplug B | Modbus TCP server | DNP3 / IEC 104 | MQTT JSON / REST | Notes |
|---|---|---|---|---|---|---|---|
| Inductive Automation Ignition | ✓ | — | ○ (MQTT Engine) | ○ | ○ | ○ | Milo is Ignition's OPC UA stack: our L1 client |
| Siemens WinCC V7/V8 | ✓ (*verify* version) | ○ | — | ○ | — | — | |
| Siemens WinCC Unified / OA | ✓ | — | — | ○ | — | ○ (OA) | |
| Rockwell FactoryTalk View SE/ME | ○ (Linx Gateway / Optix, *verify*) | ✓ | — | ○ | — | — | the DA bridge matters most here |
| AVEVA InTouch / System Platform | ✓ (via OI Gateway, *verify*) | ○ (OI Gateway / FSGateway) | — | ○ | — | — | SuiteLink ✗: tags repointed to an OI Gateway topic |
| GE Proficy iFIX | ✓ (*verify* version) | ○ | — | ○ | — | — | iFIX native IGS interface ✗ |
| GE CIMPLICITY | ✓ (*verify*) | ○ | — | ○ | — | — | |
| AVEVA Plant SCADA (Citect) | ✓ (*verify*) | ○ | — | ○ | — | — | |
| Schneider Geo SCADA (ClearSCADA) | ○ | ○ | — | ○ | ✓ (DNP3) | — | utilities prefer DNP3 |
| Trihedral VTScada | ✓ | — | — | ○ | ○ (DNP3) | — | |
| COPA-DATA zenon | ✓ | — | — | ○ | ○ (IEC 104) | — | |
| AVEVA PI | ✓ (PI Adapter for OPC UA) | ○ (PI Interface for OPC DA) | — | — | — | ○ (PI Adapter for MQTT) | |
| AWS IoT SiteWise, Azure IoT | ✓ | — | ○ | — | — | ○ | |
| Node-RED, Grafana, IT systems | ○ | — | ○ | ○ | — | ✓ | |

Order of work follows the number of rows a server unlocks: OPC UA → Sparkplug B →
Modbus TCP server → DNP3 / IEC 104 → OPC DA bridge (Windows-only, R5 in §8) ([scada-compatibility.md](../architecture/scada-compatibility.md)).

## 5. Desktop apps (Linux, Windows)

[ADR-0010](../adr/0010-one-ui-web-and-tauri.md): one static web UI served by the engine under
`/ui/`, wrapped by a Tauri 2 shell (`desktop/`, #27, #58).

```
 ┌─────────────── node (Linux or Windows) ───────────────┐      ┌──── engineer's PC ────┐
 │ system service: opencap-engine run                    │      │ opencap desktop app   │
 │   systemd unit / Windows service (SCM, #57)           │◄─────┤ (Tauri 2: web UI +    │
 │   config: /etc/opencap or %ProgramData%\opencap       │ HTTPS│  connection profiles, │
 │   data:   /var/lib/opencap or %ProgramData%\…\data    │  or  │  tokens in the OS     │
 │   API on 127.0.0.1:8080 (token from a 0600 file, *planned*; today an env var, D-019)       │ VPN  │  keychain)            │
 │ opencap desktop app (same machine) ── loopback ──────►│      └───────────────────────┘
 └───────────────────────────────────────────────────────┘
```

- **The engine is always a service, the app is always a client.** Polling must survive a
  logged-out desktop session and a closed window, so the engine never runs inside the app
  by default. The app talks only to the public API (REST + WebSocket), locally over
  loopback or to remote nodes (KEPServerEX "remote configuration").
- **Sidecar mode** for single-user installs and demos: the app can start a bundled engine
  binary as a Tauri sidecar with a private config directory (portable mode), and stop it on
  exit.
- **Service integration**: Linux: a systemd unit (hardened like `deploy/opencap.service`,
  `ExecReload` = SIGHUP). Windows: a native service through the `windows-service` crate
  (Service Control Manager stop → the same clean stop as SIGTERM; reload = `POST /reload`),
  replacing WinSW for the Rust engine (#57).
- **Installers**: Tauri's bundler builds deb and AppImage (Linux) and MSI (Windows, WebView2
  bootstrapped if missing). The deb/MSI install the engine service, the app, a default
  config, and generate the API token at install time (readable by root/Administrators and
  the service account; the app asks the OS for elevation once to copy it into the keychain).
  Code signing needs a certificate (owner decision, board item 7).
- **Security defaults**: API bound to loopback; remote access only with a token and TLS
  (reverse proxy or built-in, security RFC #21). The app stores tokens in the OS keychain,
  never in plain files.

Acceptance (M4): on clean Ubuntu LTS and Windows 11 VMs, the installer sets up the service,
the app connects, shows live values of a simulator device and survives a service restart;
uninstall removes the service but keeps config and data.

## 6. Conformance and testing

| Level | What | Where |
|---|---|---|
| Unit (Rust) | ported Python unit tests: Modbus address (15), planner (18, including the determinism and coverage property tests), decoder (24), payload, outbox, cache, health, scheduler, loader warnings, API routes and auth, channel-worker fault rules (16, fake driver) | `engine/crates/*/src`, `engine/crates/*/tests` |
| Golden payloads | 229 payload cases (random devices, all dtypes, arrays, qualities, time zones, rounding ties, repr edge cases) + status bodies, generated by Python, compared byte for byte; mutation-checked (changing the exponent threshold or the `\u` hex case fails it) | `engine/crates/opencap-north/tests/payload_golden.rs`, `tests/helpers/payload_golden.py`, `tests/unit/test_payload_golden.py` |
| CLI parity | `check -v` (every block of every device, warnings, rate groups, invalid-tag reasons, load) and `read` against live simulators: identical stdout and exit codes | `tests/integration/test_rust_engine_cli.py` |
| Outbox interchange | a Python-written outbox drains from the Rust engine in order; a Rust-written outbox is read by Python | `tests/integration/test_rust_engine_outbox.py` |
| API and reload | bearer auth on every protected route, open `/health` and `/metrics`, 404 texts, refusal of a public bind without a token, `POST /reload` and SIGHUP restart only the changed channels, an invalid config keeps the old one | `tests/integration/test_rust_engine_api.py` |
| Acceptance | `tests/integration/test_phase1_acceptance.py` with `--engine=rust` / `OPENCAP_ENGINE=rust`: 30 simulated Modbus devices (1,590 tags), publish interval, payload values, kill-one demotion and resume, API, network outage with no loss | the existing suite, through `tests/helpers/engine.py` |

The adapter (`tests/helpers/engine.py`) gives the test the same `Poller` surface for both
engines: it starts the binary with the test's config directory, answers `device_detail`,
`device_values`, `health`, `link.connected` and `saf.count()` from the API, and builds
`sent` from the publish tap. The test itself changed by a few lines (the Python spy moved
into the Python adapter).

CI (`.github/workflows/rust-engine.yml`): `cargo fmt --check`, `cargo build`, `cargo clippy
-D warnings`, `cargo test`, `cargo deny check licenses bans sources`; then the conformance
job builds the binary and runs the golden, CLI, outbox and Phase 1 acceptance tests against
it, plus the API/reload tests; a Windows build-and-test job is informational until #35.

Not yet covered, and where it goes: conformance **vectors** for Modbus planning/decoding,
scaling and payloads under `tests/conformance/` (#3, then #40 replaces the engine-local
golden file with them); config fixtures (#2, #39); every other acceptance suite with
`OPENCAP_ENGINE=rust` (#42); TLS to a real broker; the benchmark harness (#7).

## 7. KEPServerEX feature parity checklist

Status: ✅ done, 🚧 in progress, 📋 planned. Python = reference engine, Rust = this engine.

| KEPServerEX feature | opencap equivalent | Python | Rust | Milestone | Acceptance criteria |
|---|---|---|---|---|---|
| Modbus TCP/IP Ethernet | `modbus_tcp` | ✅ | ✅ (this PR) | M2 | Phase 1 suite green on both engines; vectors (#40) |
| Allen-Bradley ControlLogix / Micro800 / model 23 | `ab_logix`, `ab_pccc` | ✅ | 📋 #43, #48 | M3 | Phase 2 acceptance and the AB simulator suites (`test_ab_logix_sim.py`, `test_ab_pccc_sim.py`) green with `OPENCAP_ENGINE=rust`; Micro800 batching probe identical (D-025); writes refused |
| Siemens, Mitsubishi, OPC UA client, Modbus serial, DF1 | `s7`, `melsec_mc`, `opcua_client`, `modbus_rtu`, `ab_df1` | 🚧 | 📋 #44–#47, #53 | M3 | each driver's Python simulator suite green on Rust |
| FINS, BACnet, DNP3, IEC 104, ADS, SNMP, MQTT/ODBC/OPC DA clients | Rust-first drivers | 📋 | 📋 | M5 | simulator + vectors + read-only guard (ADR-0004 §5) |
| **Advanced Tags** (derived) | derived tags on virtual devices | 📋 | 📋 #55 | M5 | `docs/spec/derived-tags.md` vectors pass; DAG check rejects cycles at load; values in OPC UA, MQTT and the API |
| **Alarms & Events** | alarm subscribers on the event bus | 📋 | 📋 | M5 | spec #24 vectors (limits, deadband, delay, ack, return); OPC UA A&C and MQTT alarm topic; history query |
| **DataLogger** / Local Historian | logger agents with their own outbox | 📋 | 📋 | M5 | spec #25: every sink round-trips rows; no loss across a sink outage up to the outbox cap; retention enforced |
| **IoT Gateway** | MQTT agents (SubDevice JSON ✅, IoT Gateway JSON, flat JSON), Sparkplug B, REST client/server | ✅ MQTT | ✅ MQTT (this PR); 📋 Sparkplug #51 | M2–M3 | byte-identical payloads (done for SubDevice JSON); KEPServerEX IoT Gateway JSON vectors; Sparkplug TCK |
| **Configuration API** | REST + WebSocket, optimistic concurrency, audit | 🚧 web-ui | 📋 | M3 | OpenAPI contract (#32) drift-tested; both engines pass the same API tests; `412` on stale revisions |
| **Redundancy** (Redundancy Master, Media Level) | warm-standby pair, alternate hosts | 📋 | 📋 | M5 | RFC 0003: failover ≤ 15 s (NFR-7), never two pollers, witness prevents split brain |
| **Diagnostics / system tags** | `_System`, `_Statistics`, event log, frame capture, metrics ✅, `/health` ✅, `check` ✅ | partial | partial (metrics, health, check, identity: this PR); 📋 #56 | M5 | spec #22 vectors; every device's state, cycle time and errors visible as tags and metrics (NFR-13) |
| **User Manager / Security Policies** | users, roles, tokens, per-tag policies, audit | single token ✅ | single token ✅ | M5 | security RFC #21; role tests for every API route; audit entries for every config change |
| OPC UA server | `opcua` north | 🚧 | 📋 #50 | M3 | L1 with Milo, open62541, asyncua on a 100k-node space |
| OPC DA server | Windows bridge | 📋 | 📋 | M5 | RFC 0004; DA test client reads all items |
| Quick Client | web UI live values and demand read | 📋 #31 | API ✅ (`/values`, `/read-once`) | M3 | UI shows live values from either engine |
| Project import (KEPServerEX JSON) | `import-kep` | ✅ | uses Python's output | M4+ | Rust importer only after parity with the Python one |
| Scheduler | scan calendars | 📋 | 📋 | M5 | low priority |
| SuiteLink, DDE, iFIX native, ThingWorx native | — | ✗ | ✗ | — | documented OPC UA / DA recipes instead |

## 8. Phased plan

| Phase | Scope | Acceptance criteria | Issues |
|---|---|---|---|
| **R0 — this PR** (M2 start) | RFC; workspace; driver trait; worker with the Python fault rules; cache; Modbus TCP; payload; outbox; MQTT link; publisher; admin API; `run`/`check`/`read`/`identity`; CI | Phase 1 acceptance green with `OPENCAP_ENGINE=rust` (30 devices, interval, kill-one demotion and resume, API, outage with no loss, also with a 60 s outage); 229 golden payloads identical; `check -v` and `read` identical; outbox interchange both ways; API auth and reload on the binary; fmt, clippy `-D warnings`, tests and `cargo deny` green | — |
| **R1** (M2 complete) | config from the shared schema; conformance-vector harness; tag store + event bus; quality module; simulator/memory driver; benchmark | config fixtures pass (#39); 100 % of vectors (#40); store benchmark recorded and NFR-2 met on ARM64 (10k tags @ 1 s, ≤ 10 % of a core, ≤ 64 MB RSS) (#41, #7); dual-engine CI for every Python-only suite that applies (#42) | #39, #40, #41, #42, #7 |
| **R2** (M3a) | plugin protocol + Python driver host; AB Logix/Micro800, PCCC, Modbus RTU, DF1 ports | Phase 2 acceptance and the AB simulator suites green on Rust; any unported Python driver runs under the Rust engine through the driver host with its own suite green (#49) | #43, #48, #47, #53, #49 |
| **R3** (M3b) | S7, MC, OPC UA client ports; OPC UA server; Sparkplug B; Modbus TCP server | each port's simulator suite green; SCADA L1 green for OPC UA, Sparkplug, Modbus (NFR-15) | #44, #45, #46, #50, #51, #52 |
| **R4** (M4 cut-over) | Rust engine as default; services; Tauri app; installers | per driver family: suites + vectors green and a 7-day shadow run (Rust and Python side by side against simulators) with identical payloads apart from timestamps; installers pass §5's acceptance on clean VMs | #57, #27, #58, #34 |
| **R5** (M5) | derived tags, alarms, logger, redundancy, users/roles, system tags, Rust-first drivers, DNP3/IEC 104 servers, OPC DA bridge | each feature: spec + vectors + implementation + L1 tests (06 §6.1) | #55, #56, #54, … |

## 9. Risks

| Risk | Mitigation |
|---|---|
| Two engines drift | vectors, golden files and engine-agnostic suites in CI on every PR that touches either engine; Python fixes go to Rust in the same release |
| rumqttc's packet-id model changes | the PUBACK matching is isolated in `MqttLink::send`; the acceptance outage test fails on any loss |
| tokio-modbus behaviour on malformed replies differs from pymodbus | decoder is ours and pure; protocol errors map to CommError; the Modbus simulator's fault switches go into the dual-engine suite (#42) |
| Float formatting edge cases | `pyfmt` is tested against CPython and the golden file covers ties, exponents and extremes |
| Big PR | crates map 1:1 to Python modules; this RFC lists every deliberate difference; suggested review order: `opencap-core::driver`, `worker`, `drivers::modbus_tcp`, `north::payload`/`mqtt`, then the tests |

## 10. Open questions for the lead

1. Crate layout: keep `opencap-drivers` with one module per driver until #43, or split now
   into `engine/drivers/<name>` (ADR-0004 §2)?
2. Metrics crate: `prometheus` (this PR, Python-identical exposition) or `prometheus-client`
   (oss-reuse.md)?
3. The engine-local golden payload file stays until #3/#40 provide `tests/conformance/payload/`;
   then it is deleted. Agreed?
