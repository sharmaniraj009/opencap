# 5. Trade-offs and what to revisit

Each row names the choice, what we give up, and the ADR that records it.

| # | Choice | We gain | We give up | ADR |
|---|---|---|---|---|
| T-1 | Rust production engine; Python stays as reference and test harness | Memory and CPU headroom on edge boxes, one static binary per platform, a natural fit for Tauri and Windows services, memory-safe protocol parsing | A migration period in which some behaviour exists twice; Rust is slower to write than Python | [0002](../adr/0002-rust-engine-python-reference.md) |
| T-2 | JSON Schema generated from the Pydantic models is the cross-language config contract | One source of truth today, machine-checked parity, UI forms from the schema | Python owns the schema until cut-over; a Rust-first schema change needs a Python model change first | [0003](../adr/0003-shared-config-schema.md) |
| T-3 | Compiled-in Rust drivers + out-of-process gRPC plugins; no dynamic Rust plugins | Safety (no unstable ABI), simple builds, crash and licence isolation where needed | Third parties cannot drop in a `.so`; out-of-process plugins cost a hop (~50–100 µs per request, negligible against a 60 ms RTT) | [0004](../adr/0004-driver-contract-and-plugin-model.md) |
| T-4 | One request on the wire per channel by default | KEPServerEX-identical load on PLCs, predictable behaviour on weak devices | Throughput on gateways with many devices; opt-in concurrency later | [0004](../adr/0004-driver-contract-and-plugin-model.md), D-015 |
| T-5 | Bounded event bus with conflation; durable agents use outboxes | The scan loop never waits for north; bounded memory | A starved consumer can skip intermediate values (counted, logged) | [0005](../adr/0005-north-plugins-tag-store-event-bus.md) |
| T-6 | Five qualities, mapped per north protocol | Simple core, one mapping table | Fewer distinctions than OPC UA's status codes (e.g. sensor failure vs device failure) | [0006](../adr/0006-quality-and-timestamps.md) |
| T-7 | Permissive licences only in the default build; copyleft only out of process and with owner approval | Anyone can ship and embed opencap | Some mature C stacks (lib60870, libiec61850) are not in the default build | [0007](../adr/0007-oss-licence-policy.md) |
| T-8 | Read-only toward PLCs | No path by which opencap can move a valve | KEPServerEX users who write setpoints or use link tags cannot migrate those functions yet | [0008](../adr/0008-read-only-by-default.md) |
| T-9 | OPC UA first for SCADA; KEPServerEX-compatible NodeIds | Almost every modern SCADA connects with no custom work; SCADA tag references survive the migration | Proprietary links (SuiteLink, iFIX NIO) are never offered; older SCADA needs the OPC DA bridge | [0009](../adr/0009-scada-connectivity-strategy.md) |
| T-10 | One web UI, wrapped by Tauri for desktop | One codebase for browser and desktop | Not a native-look desktop app; Windows needs WebView2 (present on Windows 10/11) | [0010](../adr/0010-one-ui-web-and-tauri.md) |
| T-11 | Outbox-always (every message written to SQLite before publishing) per durable agent | Ordering, no loss across outages and restarts | One SQLite write per message; QoS 1 duplicates at reconnect | [0011](../adr/0011-store-and-forward-outbox-per-agent.md), D-016 |
| T-12 | Warm standby, standby never polls | No extra load on PLCs with few connections | Failover takes seconds, not milliseconds; standby values are cold at takeover | [0012](../adr/0012-redundancy-warm-standby.md) |
| T-13 | Project as files with revisions and optimistic concurrency, not a database | Diffable, git-friendly, no DB server on edge boxes | Concurrent editors conflict (412) instead of merging | [0013](../adr/0013-project-file-v2.md) |
| T-14 | Protocol behaviour proven by simulators and shared vectors, never by real plant | Safe, repeatable CI; engines provably equal on pure functions | Simulator fidelity limits; real-device quirks found on site (recorded in D-NNN, then simulated) | [0014](../adr/0014-conformance-by-simulators-and-vectors.md) |
| T-15 | Native Rust protocol implementations where the read subset is small (S7comm, MC, FINS); FFI or out-of-process for big stacks | Memory safety, easy cross-compiling, no copyleft | Re-implementing protocol corner cases that mature C libraries already handle | [0007](../adr/0007-oss-licence-policy.md), [oss-reuse.md](oss-reuse.md) |

## What to revisit as the system grows

| Trigger | Revisit |
|---|---|
| > 1M tags per node, or lock contention visible in profiles | Shard the tag store per channel and per core; lock-free snapshots (`arc-swap`) |
| > 2,000 msg/s per agent sustained | Replace the SQLite outbox with a segmented append-only log |
| The chosen OPC UA stack misses the memory/throughput gate | Switch stack (async-opcua ↔ open62541 via FFI) behind the north plugin trait |
| Third parties want to ship drivers without forking | A WASM component model for drivers (sandboxed, stable ABI) next to gRPC |
| Many engineers edit one project at the same time | A config store with per-object revisions and merge, or project-in-git with server-side merges |
| Sites need more than active/standby | N+1 node groups with channel re-assignment; only then consider consensus (Raft) |
| Customers require writes | Write-safety RFC (ADR-0008) → supervised write path, per-tag permission, audit, interlocks |
| Python reference falls behind the Rust engine | Retire Python as an engine; keep simulators and the acceptance harness |
| KEPServerEX import is no longer the main entry path | Move the importer to Rust and drop the overrides/devices_map layering (ADR-0013) |
