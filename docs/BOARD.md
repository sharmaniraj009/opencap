# opencap program board

Status, assignments and priorities for opencap. This file and issue #1 carry the same text.

**Last updated:** 2026-11-28, v0.1 push (Rust only). Main at `4756f37` (#274).

## Decisions in force
- **Rust is the only engine** (ADR-0018). No new Python. Python is deleted when v0.1 is done (`docs/plan/v0.1-rust.md`).
- **Drivers: open source first.** An open-source Rust crate, then a C/C++ library through FFI, then our own code, with the reason recorded in `docs/decisions.md`. Read-only toward PLCs always (ADR-0008).
- **Workers:** two at a time (core, drivers), on the Sonnet model.

## Merged on `main` (this push)
- **Drivers:** Siemens S7 with batch reads, Mitsubishi MELSEC, Allen-Bradley Logix, PCCC over EtherNet/IP (libplctag, read-only binding with a CI guard), native DF1 serial, Omron FINS, Modbus TCP and RTU, the simulator driver, and the OPC UA client (polled reads).
- **North side:** MQTT with store-and-forward, Sparkplug B, IoT Gateway JSON, the read-only OPC UA server (with the owner-approved RUSTSEC-2023-0071 ignore, D-127).
- **Core:** system tags (both halves), project v2 loading, config validation (timezone and DST), config and API events, one log format with reason codes, readiness / about / support endpoints, the web UI served by the engine.
- **Packaging and docs:** Rust-only container and release workflow, a Linux binary artifact on release runs, ADR-0018, the v0.1 plan, the user guide (`docs/user-guide.md`), `docs/run-linux.md`.
- **Tests:** Windows wall-clock hardening, and the OPC UA certificate flake fixed at its root cause.

## In flight
- **#280:** admin API read routes (start of #140). Core worker.
- **Python-era PRs #244, #249, #251:** open, contradict the Rust-only rule. Recommended: close.

## Not done for v0.1
1. Admin API parity (#140, #132). The web UI needs it; some panels fail until it lands.
2. Real-hardware test with a Phoenix Contact PLC (waiting on the owner's port probe).
3. Deleting Python, after replacements exist for the Python test simulators and suites.
4. Release setup: `v0.1.0` tag, GHCR, PyPI, code signing.
5. OPC UA follow-ups: client subscriptions; server X.509 user tokens, `_System` folder, array tags.
6. Other follow-ups: Rust serial rules (#256), outbox robustness (#142), scan modes (#111), derived tags (#55), Modbus TCP server (#52), Driver `browse()` (#173).

## Waiting on the owner
- Phoenix probe: `nc -zv PLC_IP 502`, `4840`, `44818`, and `uname -m` on the test machine.
- Switch on GitHub Pages (Settings, Pages, Source: GitHub Actions).
- Close or keep the Python PRs #244, #249, #251.
- The write-safety question (RFC 0002, options A to D). Nothing can write to a PLC until answered.
- Release decisions: `v0.1.0` tag, GHCR visibility, PyPI, code signing, Windows service account (LocalService or LocalSystem), and the three questions on #16.

## Parked for v0.2
DNP3, BACnet and IEC 60870-5-104 (issues #180 to #215). Same open-source-first rule.

## Housekeeping
- 71 issues open. Likely finished or mostly finished, to verify and close: #123 (both halves merged), #46, #48, #50 (core merged, follow-ups remain).
- Decision-log numbers: core uses D-100 to D-119, drivers D-120 to D-139. Check `docs/decisions.md` on `main` before adding one.

## Risks
- No run against a real PLC yet. Everything is simulator-tested.
- Every PR touches `docs/decisions.md`, so merges conflict one after another.
- Two workers running all day use a lot of the shared usage limit.
