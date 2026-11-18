# opencap program board

Status, assignments and next priorities for opencap. Maintained by the technical lead on every pass; the issue [#1](https://github.com/sharmaniraj009/opencap/issues/1) and `docs/BOARD.md` carry the same text. How to work with it: `CONTRIBUTING.md` → "Engineering protocol".

**Last updated:** 2026-11-18 10:30 UTC, pass 10

## Status summary

- **Pass 10:** no PRs. Ten engineer sessions claimed ten issues (table below). Pass 9 merged everything that was mergeable.
- **Merged in pass 9:**
  - [#93](https://github.com/sharmaniraj009/opencap/issues/93) (**KEPServerEX V6 feature-parity plan**, `plan/`: 297 features with decisions, requirements and a generated checklist; the `web-ui` session's branch had no PR);
  - [#95](https://github.com/sharmaniraj009/opencap/issues/95) (**Siemens S7 and Mitsubishi MC drivers**, supersedes [#60](https://github.com/sharmaniraj009/opencap/issues/60), closes [#73](https://github.com/sharmaniraj009/opencap/issues/73));
  - [#96](https://github.com/sharmaniraj009/opencap/issues/96) (**OPC UA client driver and OPC UA server for SCADA**, supersedes [#61](https://github.com/sharmaniraj009/opencap/issues/61), closes [#74](https://github.com/sharmaniraj009/opencap/issues/74): anonymous off and plaintext refused beyond loopback by default, client write guard, `opcua_server` in both engines' config contract).
  - [#94](https://github.com/sharmaniraj009/opencap/issues/94) (Rust worker one-request-on-the-wire flake, closes [#78](https://github.com/sharmaniraj009/opencap/issues/78): a test artefact, the probe now uses a drop guard; D-065);
  - [#97](https://github.com/sharmaniraj009/opencap/issues/97) (no automatic GC pause inside wall-clock-bounded tests: asyncua's cyclic garbage made full collections stall the event loop 300–400 ms; fixed the CI flakes seen on [#94](https://github.com/sharmaniraj009/opencap/issues/94) and [#96](https://github.com/sharmaniraj009/opencap/issues/96)).
  - The lead finished [#60](https://github.com/sharmaniraj009/opencap/issues/60) and [#61](https://github.com/sharmaniraj009/opencap/issues/61) under the stalled-PR rule: the original commits are kept and credited, and the review fixes went on top.
- **Merged in pass 8:** [#87](https://github.com/sharmaniraj009/opencap/issues/87) (Tauri desktop shell, closes [#27](https://github.com/sharmaniraj009/opencap/issues/27)).
- **Merged in pass 7:** [#86](https://github.com/sharmaniraj009/opencap/issues/86) (Rust config conformance, closes [#39](https://github.com/sharmaniraj009/opencap/issues/39)); [#88](https://github.com/sharmaniraj009/opencap/issues/88) (OpenAPI contract, closes [#32](https://github.com/sharmaniraj009/opencap/issues/32)).
- **Merged earlier:** [#84](https://github.com/sharmaniraj009/opencap/issues/84), [#63](https://github.com/sharmaniraj009/opencap/issues/63), [#77](https://github.com/sharmaniraj009/opencap/issues/77), [#72](https://github.com/sharmaniraj009/opencap/issues/72), [#71](https://github.com/sharmaniraj009/opencap/issues/71), [#70](https://github.com/sharmaniraj009/opencap/issues/70), [#69](https://github.com/sharmaniraj009/opencap/issues/69), [#64](https://github.com/sharmaniraj009/opencap/issues/64), [#62](https://github.com/sharmaniraj009/opencap/issues/62), [#59](https://github.com/sharmaniraj009/opencap/issues/59), [#4](https://github.com/sharmaniraj009/opencap/issues/4).
- **Open PRs:** none. **Branches:** every remote branch except the lead's is either fully merged or belongs to a merged PR.
- **Milestones:**
  - **M1 (Python parity) is code-complete.** All its drivers, the OPC UA server, the web UI and the config API are on `main`. Two owner actions remain: the real-hardware checks (Decisions 10) and making CI required on `main` (Decisions 11).
  - **M2 (Rust core):** core, API parity, config conformance and the API contract are done. Left: [#41](https://github.com/sharmaniraj009/opencap/issues/41) tag store + bus, [#42](https://github.com/sharmaniraj009/opencap/issues/42) dual-engine acceptance, [#67](https://github.com/sharmaniraj009/opencap/issues/67) driver crates.
- **CI:** tests on 3.12/3.13 with every extra, wheel + image, Rust (fmt, clippy, test, deny, RustSec advisories, conformance, Windows), licence gates, desktop bundles. Nightly acceptance. A new unit test keeps `plan/checklist.md` in sync with `plan/features/`.
- **New in pass 9:**
  - unblocked [#28](https://github.com/sharmaniraj009/opencap/issues/28) P1 (OPC UA server hardening), [#29](https://github.com/sharmaniraj009/opencap/issues/29) P1 (L1 SCADA compatibility suite), [#30](https://github.com/sharmaniraj009/opencap/issues/30) P2 (quality module), [#44](https://github.com/sharmaniraj009/opencap/issues/44) P2 (Rust S7 port) and [#45](https://github.com/sharmaniraj009/opencap/issues/45) P2 (Rust MC port);
  - timing-bound tests now use the `no_gc` fixture ([#97](https://github.com/sharmaniraj009/opencap/issues/97), CONTRIBUTING → "Opening a PR").

## Engineer sessions

| Session | Done | Now |
|---|---|---|
| `engineer-1` | [#69](https://github.com/sharmaniraj009/opencap/issues/69) (flaky test) | — |
| `engineer-3` | [#71](https://github.com/sharmaniraj009/opencap/issues/71) (config schema) | — |
| `engineer-4` | [#72](https://github.com/sharmaniraj009/opencap/issues/72) (licence gate) | — |
| `engineer-5` | [#77](https://github.com/sharmaniraj009/opencap/issues/77) (Rust follow-ups) | — |
| `engineer-7` | [#88](https://github.com/sharmaniraj009/opencap/issues/88) (OpenAPI contract) | — |
| `web-ui` (original session) | [#63](https://github.com/sharmaniraj009/opencap/issues/63) (via [#75](https://github.com/sharmaniraj009/opencap/issues/75)), [#84](https://github.com/sharmaniraj009/opencap/issues/84), [#86](https://github.com/sharmaniraj009/opencap/issues/86), [#87](https://github.com/sharmaniraj009/opencap/issues/87), [#93](https://github.com/sharmaniraj009/opencap/issues/93) (plan) | — |
| `s7-melsec`, `opcua` (original sessions) | [#60](https://github.com/sharmaniraj009/opencap/issues/60) → [#95](https://github.com/sharmaniraj009/opencap/issues/95), [#61](https://github.com/sharmaniraj009/opencap/issues/61) → [#96](https://github.com/sharmaniraj009/opencap/issues/96) (finished by the lead) | — |
| `platform`, `serial`, `rust-engine` (original sessions) | [#4](https://github.com/sharmaniraj009/opencap/issues/4), [#62](https://github.com/sharmaniraj009/opencap/issues/62), [#64](https://github.com/sharmaniraj009/opencap/issues/64) | — |
| `engineer-8` | [#94](https://github.com/sharmaniraj009/opencap/issues/94) (worker flake, [#78](https://github.com/sharmaniraj009/opencap/issues/78)) | — |
| `rust-core` | — | [#41](https://github.com/sharmaniraj009/opencap/issues/41) tag store + change-event bus |
| `rust-north` | — | [#42](https://github.com/sharmaniraj009/opencap/issues/42) engine-agnostic acceptance, dual-engine CI |
| `rust-drivers` | — | [#67](https://github.com/sharmaniraj009/opencap/issues/67) one crate per driver |
| `conformance` | — | [#3](https://github.com/sharmaniraj009/opencap/issues/3) Modbus/scaling/payload vectors |
| `opcua-scada` | — | [#29](https://github.com/sharmaniraj009/opencap/issues/29) L1 SCADA compatibility suite |
| `ui` | — | [#31](https://github.com/sharmaniraj009/opencap/issues/31) Quick Client |
| `infra` | — | [#38](https://github.com/sharmaniraj009/opencap/issues/38) CI follow-ups |
| `specs` | — | [#22](https://github.com/sharmaniraj009/opencap/issues/22) system and diagnostic tags spec |
| `spikes` | — | [#13](https://github.com/sharmaniraj009/opencap/issues/13) Omron FINS simulator |
| `py-features` | — | [#12](https://github.com/sharmaniraj009/opencap/issues/12) simulator driver |

**Next for anyone (`ready`, highest first):**
- P1: [#28](https://github.com/sharmaniraj009/opencap/issues/28) OPC UA server hardening (PKI folders, X.509 and several users, limits, EURange, advertised URL, 100k-node scale); [#5](https://github.com/sharmaniraj009/opencap/issues/5) AB vectors.
- P2: [#30](https://github.com/sharmaniraj009/opencap/issues/30) quality module; [#44](https://github.com/sharmaniraj009/opencap/issues/44) / [#45](https://github.com/sharmaniraj009/opencap/issues/45) Rust S7 / MC ports (after [#67](https://github.com/sharmaniraj009/opencap/issues/67)); [#47](https://github.com/sharmaniraj009/opencap/issues/47) Rust Modbus RTU port (after [#67](https://github.com/sharmaniraj009/opencap/issues/67)); [#89](https://github.com/sharmaniraj009/opencap/issues/89) OpenAPI request bodies; [#90](https://github.com/sharmaniraj009/opencap/issues/90) desktop licence gate; [#7](https://github.com/sharmaniraj009/opencap/issues/7) benchmark; [#19](https://github.com/sharmaniraj009/opencap/issues/19) / [#21](https://github.com/sharmaniraj009/opencap/issues/21) / [#23](https://github.com/sharmaniraj009/opencap/issues/23) RFCs and specs.

Take the highest priority in your area, else any `ready` issue. `plan/checklist.md` lists every KEPServerEX feature; ask the lead to file an issue for any unchecked row that has none.

## Decision-log numbers (`docs/decisions.md`)

Used: D-039–D-044 ([#63](https://github.com/sharmaniraj009/opencap/issues/63)), D-045–D-047 ([#96](https://github.com/sharmaniraj009/opencap/issues/96)), D-048–D-058 ([#95](https://github.com/sharmaniraj009/opencap/issues/95)), D-059 ([#72](https://github.com/sharmaniraj009/opencap/issues/72)), D-060 ([#71](https://github.com/sharmaniraj009/opencap/issues/71)), D-061 ([#77](https://github.com/sharmaniraj009/opencap/issues/77)), D-062 ([#84](https://github.com/sharmaniraj009/opencap/issues/84)), D-063 ([#86](https://github.com/sharmaniraj009/opencap/issues/86)), D-064 ([#88](https://github.com/sharmaniraj009/opencap/issues/88)), D-065 ([#94](https://github.com/sharmaniraj009/opencap/issues/94)). **Next free: D-066.** Before adding an entry, check `docs/decisions.md` on `main`. If two PRs claim the same number, the lead renumbers the later one at merge.

## Open PRs

None. In progress (claimed): [#3](https://github.com/sharmaniraj009/opencap/issues/3), [#12](https://github.com/sharmaniraj009/opencap/issues/12), [#13](https://github.com/sharmaniraj009/opencap/issues/13), [#22](https://github.com/sharmaniraj009/opencap/issues/22), [#29](https://github.com/sharmaniraj009/opencap/issues/29), [#31](https://github.com/sharmaniraj009/opencap/issues/31), [#38](https://github.com/sharmaniraj009/opencap/issues/38), [#41](https://github.com/sharmaniraj009/opencap/issues/41), [#42](https://github.com/sharmaniraj009/opencap/issues/42), [#67](https://github.com/sharmaniraj009/opencap/issues/67).

## Ready queue by area (priority order)

| Area | Ready issues |
|---|---|
| core | [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 AB vectors · [#30](https://github.com/sharmaniraj009/opencap/issues/30) P2 quality module · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 benchmark · [#89](https://github.com/sharmaniraj009/opencap/issues/89) P2 OpenAPI request bodies · [#37](https://github.com/sharmaniraj009/opencap/issues/37) P2 project file v2 · [#10](https://github.com/sharmaniraj009/opencap/issues/10) P2 KEPServerEX CSV · [#19](https://github.com/sharmaniraj009/opencap/issues/19) P2 RFC 0002 write safety · [#21](https://github.com/sharmaniraj009/opencap/issues/21) P2 RFC 0005 security · [#23](https://github.com/sharmaniraj009/opencap/issues/23) P2 derived tags spec · [#20](https://github.com/sharmaniraj009/opencap/issues/20) P3 RFC 0003 redundancy · [#24](https://github.com/sharmaniraj009/opencap/issues/24) P3 alarms spec · [#25](https://github.com/sharmaniraj009/opencap/issues/25) P3 data logger spec |
| engine-rust | [#44](https://github.com/sharmaniraj009/opencap/issues/44) P2 S7 port · [#45](https://github.com/sharmaniraj009/opencap/issues/45) P2 MC port · [#47](https://github.com/sharmaniraj009/opencap/issues/47) P2 Modbus RTU port · [#49](https://github.com/sharmaniraj009/opencap/issues/49) P2 plugin host · [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 services · [#80](https://github.com/sharmaniraj009/opencap/issues/80) P3 replace chrono |
| driver | [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 · [#44](https://github.com/sharmaniraj009/opencap/issues/44) P2 · [#45](https://github.com/sharmaniraj009/opencap/issues/45) P2 · [#11](https://github.com/sharmaniraj009/opencap/issues/11) P2 Logix browse · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 DNP3 spike · [#47](https://github.com/sharmaniraj009/opencap/issues/47) P2 · [#68](https://github.com/sharmaniraj009/opencap/issues/68) P2 serial follow-ups · [#15](https://github.com/sharmaniraj009/opencap/issues/15) P3 BACnet spike · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 IEC 104 spike |
| north | [#28](https://github.com/sharmaniraj009/opencap/issues/28) P1 OPC UA hardening · [#30](https://github.com/sharmaniraj009/opencap/issues/30) P2 · [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 Mosquitto restart test · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 · [#36](https://github.com/sharmaniraj009/opencap/issues/36) P2 IoT Gateway JSON · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 |
| infra | [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 · [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 · [#9](https://github.com/sharmaniraj009/opencap/issues/9) P2 docs site · [#34](https://github.com/sharmaniraj009/opencap/issues/34) P2 release pipeline · [#35](https://github.com/sharmaniraj009/opencap/issues/35) P2 Windows CI · [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 · [#90](https://github.com/sharmaniraj009/opencap/issues/90) P2 · [#58](https://github.com/sharmaniraj009/opencap/issues/58) P3 · [#79](https://github.com/sharmaniraj009/opencap/issues/79) P3 pin actions by SHA |
| scada | [#28](https://github.com/sharmaniraj009/opencap/issues/28) P1 · [#17](https://github.com/sharmaniraj009/opencap/issues/17) P2 matrix + recipes · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 RFC 0004 OPC DA |
| docs | [#9](https://github.com/sharmaniraj009/opencap/issues/9) · [#17](https://github.com/sharmaniraj009/opencap/issues/17) · [#18](https://github.com/sharmaniraj009/opencap/issues/18) · [#19](https://github.com/sharmaniraj009/opencap/issues/19) · [#20](https://github.com/sharmaniraj009/opencap/issues/20) · [#21](https://github.com/sharmaniraj009/opencap/issues/21) · [#24](https://github.com/sharmaniraj009/opencap/issues/24) · [#25](https://github.com/sharmaniraj009/opencap/issues/25) · [#26](https://github.com/sharmaniraj009/opencap/issues/26) |
| ui | [#89](https://github.com/sharmaniraj009/opencap/issues/89) P2 OpenAPI request bodies · [#33](https://github.com/sharmaniraj009/opencap/issues/33) P2 import wizard · [#26](https://github.com/sharmaniraj009/opencap/issues/26) P2 screen spec |
| desktop | [#90](https://github.com/sharmaniraj009/opencap/issues/90) P2 licence gate for `desktop/` · [#58](https://github.com/sharmaniraj009/opencap/issues/58) P3 installers with the engine as a sidecar |

## Blocked (and on what)

| Waiting on | Issues |
|---|---|
| [#41](https://github.com/sharmaniraj009/opencap/issues/41) tag store + bus | [#46](https://github.com/sharmaniraj009/opencap/issues/46) P2 Rust OPC UA client · [#50](https://github.com/sharmaniraj009/opencap/issues/50) P1 Rust OPC UA server (+[#29](https://github.com/sharmaniraj009/opencap/issues/29)) · [#51](https://github.com/sharmaniraj009/opencap/issues/51) P1 Sparkplug B · [#52](https://github.com/sharmaniraj009/opencap/issues/52) P2 Modbus TCP server · [#55](https://github.com/sharmaniraj009/opencap/issues/55) (+[#23](https://github.com/sharmaniraj009/opencap/issues/23)) · [#56](https://github.com/sharmaniraj009/opencap/issues/56) (+[#22](https://github.com/sharmaniraj009/opencap/issues/22)) |
| ready issues | [#40](https://github.com/sharmaniraj009/opencap/issues/40) ← [#3](https://github.com/sharmaniraj009/opencap/issues/3) · [#43](https://github.com/sharmaniraj009/opencap/issues/43) ← [#5](https://github.com/sharmaniraj009/opencap/issues/5) · [#48](https://github.com/sharmaniraj009/opencap/issues/48) ← [#43](https://github.com/sharmaniraj009/opencap/issues/43) · [#53](https://github.com/sharmaniraj009/opencap/issues/53) ← [#47](https://github.com/sharmaniraj009/opencap/issues/47), [#48](https://github.com/sharmaniraj009/opencap/issues/48) · [#54](https://github.com/sharmaniraj009/opencap/issues/54) ← [#13](https://github.com/sharmaniraj009/opencap/issues/13) |

## Merge order (to minimise conflicts)

1. Rust: [#67](https://github.com/sharmaniraj009/opencap/issues/67) (crate layout, `rust-drivers`) before the driver ports ([#44](https://github.com/sharmaniraj009/opencap/issues/44), [#45](https://github.com/sharmaniraj009/opencap/issues/45), [#47](https://github.com/sharmaniraj009/opencap/issues/47)). [#41](https://github.com/sharmaniraj009/opencap/issues/41) and [#67](https://github.com/sharmaniraj009/opencap/issues/67) both touch `opencap-core`: whichever lands second rebases.
2. [#42](https://github.com/sharmaniraj009/opencap/issues/42) (`rust-north`) and [#38](https://github.com/sharmaniraj009/opencap/issues/38) (`infra`) both edit `.github/workflows/`: one at a time.
3. OPC UA: [#30](https://github.com/sharmaniraj009/opencap/issues/30) (quality module) before [#28](https://github.com/sharmaniraj009/opencap/issues/28) if both are open, since #28 changes the server's status mapping.

**Timing tests:** tests with wall-clock bounds use the `no_gc` fixture ([#97](https://github.com/sharmaniraj009/opencap/issues/97)) and wait on conditions rather than fixed sleeps. If one still flakes, file it with the CI log.

## Decisions for the owner

No new decisions this pass. The lead continues with everything else meanwhile; reply on this issue.

1. **Pin this issue.** The lead's tools cannot pin issues or set label colours.
2. **Licence:** `LICENSE` is Apache-2.0 ([#4](https://github.com/sharmaniraj009/opencap/issues/4)). Confirm.
3. **Name:** "opencap": check availability (PyPI, GHCR, domain, trademarks). KEPServerEX is PTC's trademark, used only to describe compatibility.
4. **Writes to real plants:** stays forbidden (ADR-0008). RFC 0002 ([#19](https://github.com/sharmaniraj009/opencap/issues/19)) will put the options to you.
5. **Copyleft plugins:** may GPL stacks (lib60870, libiec61850) ship as separate, optional, out-of-process plugins? Recommendation: yes, never in the default build.
6. **OPC DA:** OPC Foundation Core Components redistribution or membership (RFC 0004, [#18](https://github.com/sharmaniraj009/opencap/issues/18)).
7. **Paid services:** Windows code signing, OPC Foundation certification, vendor SCADA licences for manual tests, a PyPI account.
8. **Priorities for M5:** which KEPServerEX plug-ins your sites use (Advanced Tags, Alarms & Events, DataLogger, IoT Gateway, Redundancy, Scheduler, Local Historian).
9. **Telemetry:** none, ever. Confirm.
10. **Site work:** commissioning and the Rust cut-over shadow runs need someone on site. The checks per driver: [#62](https://github.com/sharmaniraj009/opencap/issues/62) (DF1, RTU), [#64](https://github.com/sharmaniraj009/opencap/issues/64) (MQTT TLS), S7 and MELSEC in `docs/deployment.md` → "Commissioning a Siemens S7 or Mitsubishi PLC" ([#95](https://github.com/sharmaniraj009/opencap/issues/95)), OPC UA servers and one SCADA client on the opencap server ([#96](https://github.com/sharmaniraj009/opencap/issues/96)).
11. **New: require CI on `main`** (GitHub → Settings → Branches → a rule for `main`: require the `ci`, `rust-engine` and `licenses` checks). It is M1's last exit criterion, and the lead's tools cannot change repository settings.

## Backlog (next waves, not yet filed)

- Drivers (Rust): EtherNet/IP generic CIP explicit (after [#43](https://github.com/sharmaniraj009/opencap/issues/43)), Beckhoff ADS, GE SRTP, SNMP, MQTT client, ODBC client, OPC DA client; DNP3, IEC 104, BACnet/IP (after spikes [#14](https://github.com/sharmaniraj009/opencap/issues/14)–[#16](https://github.com/sharmaniraj009/opencap/issues/16)).
- Processing: alarms (after [#24](https://github.com/sharmaniraj009/opencap/issues/24)), data logger (after [#25](https://github.com/sharmaniraj009/opencap/issues/25)), redundancy (after [#20](https://github.com/sharmaniraj009/opencap/issues/20)), users/roles (after [#21](https://github.com/sharmaniraj009/opencap/issues/21)).
- Core: Rust KEPServerEX importer; re-import three-way merge (ADR-0013); client-driven scan rate; per-channel concurrency; inter-request delay; media-level redundancy; dynamic tags; scheduler.
- Diagnostics: comm frame capture, event log (`plan/features/15-event-log-messages.md`), `/health/live` + `/health/ready`, Prometheus alert rules.
- North: REST client/server agents; OPC DA server bridge (after [#18](https://github.com/sharmaniraj009/opencap/issues/18) + owner decision); OPC AE.
