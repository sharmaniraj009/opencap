# 0004. Driver contract; compiled-in Rust drivers plus out-of-process gRPC plugins

- Status: Accepted (amended 2026-11-17 after RFC 0001, see the end)
- Date: 2026-11-17
- Deciders: technical lead

## Context
The Python `Driver` ABC (`src/opencap/drivers/base.py`) has proven itself on three
protocol families: a pure `plan()`, one-attempt `connect()`/`read()`, a small error
taxonomy, and the worker (not the driver) owning retries, backoff, demotion and scaling.
The Rust engine needs the same contract. Some protocol stacks are not Rust, are copyleft,
or are too crash-prone to run in the engine process; during migration the Rust engine should
be able to use the existing Python drivers.

## Decision
1. The Rust `Driver` trait mirrors the Python ABC exactly in semantics
   ([03 §3.3](../architecture/03-deep-dive.md#33-driver-contract)): `plan` (pure),
   `connected`, `connect_timeout`, `connect`, `close`, `read`, `identity`, `browse`;
   errors `Comm`, `ConnectionLost`, `BlockConfig`, per-tag `Decode`/`BadConfig`/`BadComm`.
   **No write method.** Push-style drivers add `subscribe`.
2. Rust drivers are crates in `engine/drivers/<name>`, compiled in behind Cargo features,
   registered in a static table. No dynamic loading of Rust libraries.
3. **Out-of-process plugins** speak `opencap.plugin.v1` (gRPC over a Unix socket, named pipe
   or loopback TCP; `.proto` files in `proto/opencap/plugin/v1/`). The engine spawns,
   supervises and restarts them. Uses: the **Python driver host** (runs any existing Python
   driver), copyleft stacks shipped as separate packages ([ADR-0007](0007-oss-licence-policy.md)),
   vendor or C libraries that may crash.
4. Concurrency default is **one request on the wire per channel** (D-015); a channel may
   opt into `max_inflight > 1` for protocols with transaction ids, per driver capability.
5. A new driver is accepted when it has: a simulator in the Python harness validated by an
   independent client library, conformance vectors for `plan()` and decode, read-only guards
   with a test that write services are refused or never built, `docs/driver_params.md`
   entries, and KEPServerEX import mapping where KEPServerEX has the driver.

## Consequences
- The core never changes for a new protocol (as Phases 2 and 3 showed in Python).
- Out-of-process calls cost ~50–100 µs, negligible against field RTTs.
- Third parties cannot hot-load native drivers; they use the plugin protocol.

## Alternatives considered
- Dynamic Rust plugins (`.so`/`.dll` via `abi_stable`): fragile, unsafe across compiler versions.
- C ABI plugins: unsafe, every plugin must re-implement framing and error mapping.
- WASM components: promising sandbox and stable ABI, but no socket story mature enough yet.

## Revisit when
WASM component networking matures, or third-party driver demand appears (05 "revisit").

## Amendment (2026-11-17, after RFC 0001 / #64)
- The trait as built (`opencap-core::driver`) is the contract; `docs/architecture/03-deep-dive.md`
  §3.3 shows it. Differences from the first sketch are deliberate and accepted: `name()` and
  `reports_connection()` mirror Python, `read()` takes `&dyn Block` and returns a map of
  `TagValue` with per-tag qualities, `identity()` returns `{}` instead of failing, errors add
  `NotImplemented`/`Other`. `browse()` arrives with #43.
- Crate layout: `engine/crates/opencap-{core,drivers,north,api,engine}`; each driver gets its own
  crate `engine/drivers/<name>` behind a Cargo feature (#67).
- Push drivers: the Python OPC UA client (#61) samples a notification buffer per rate period;
  the Rust `subscribe()` path comes with #46.
