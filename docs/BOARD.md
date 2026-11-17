# opencap program board

Status, assignments and next priorities for opencap. Maintained by the technical lead on every pass; the issue [#1](https://github.com/sharmaniraj009/opencap/issues/1) and `docs/BOARD.md` carry the same text. How to work with it: `CONTRIBUTING.md` → "Engineering protocol".

**Last updated:** 2026-11-17, pass 2

## Status summary

- **Merged:**
  - [#4](https://github.com/sharmaniraj009/opencap/issues/4) (licence, CI, packaging, topic prefix);
  - [#59](https://github.com/sharmaniraj009/opencap/issues/59) (architecture, ADRs, protocol);
  - [#64](https://github.com/sharmaniraj009/opencap/issues/64) (**Rust engine core**: RFC 0001; `engine/` with Modbus TCP, MQTT + outbox, API; Phase 1 acceptance passes on both engines);
  - [#62](https://github.com/sharmaniraj009/opencap/issues/62) (**serial**: Modbus RTU + Allen-Bradley DF1).
- **In review, changes requested:** [#60](https://github.com/sharmaniraj009/opencap/issues/60) (S7 + MELSEC), [#61](https://github.com/sharmaniraj009/opencap/issues/61) (OPC UA client + server), [#63](https://github.com/sharmaniraj009/opencap/issues/63) (web UI + config API). Details are in each PR's lead review.
- **Milestones:** M1 (Python parity) is close; S7/MC, OPC UA and the web UI are in review. M2 (Rust core) has started: 15 Rust issues unblocked by [#64](https://github.com/sharmaniraj009/opencap/issues/64).
- **CI:** from this pass on, CI installs `.[dev,all]`, so every driver's tests run without PRs editing the install lines.
- **New this pass:**
  - [#65](https://github.com/sharmaniraj009/opencap/issues/65) flaky publisher test (P1);
  - [#66](https://github.com/sharmaniraj009/opencap/issues/66) Rust follow-ups from the #64 review (P1);
  - [#67](https://github.com/sharmaniraj009/opencap/issues/67) one crate per Rust driver (P1, before the ports);
  - [#68](https://github.com/sharmaniraj009/opencap/issues/68) serial follow-ups (P2).

## Engineer sessions

| Session | Done | Now | Suggested next (`ready`, highest priority first) |
|---|---|---|---|
| `platform` | [#4](https://github.com/sharmaniraj009/opencap/issues/4) | — | [#6](https://github.com/sharmaniraj009/opencap/issues/6) licence gate (P1), [#65](https://github.com/sharmaniraj009/opencap/issues/65) flaky test (P1), [#38](https://github.com/sharmaniraj009/opencap/issues/38) CI follow-ups, [#35](https://github.com/sharmaniraj009/opencap/issues/35) Windows CI, [#34](https://github.com/sharmaniraj009/opencap/issues/34) release pipeline, [#9](https://github.com/sharmaniraj009/opencap/issues/9) docs site |
| `s7-melsec` | — | [#60](https://github.com/sharmaniraj009/opencap/issues/60): rebase, renumber to **D-048–D-058**, `importorskip` in the acceptance test | then [#5](https://github.com/sharmaniraj009/opencap/issues/5) AB vectors (P1); after merge [#44](https://github.com/sharmaniraj009/opencap/issues/44) S7 port, [#45](https://github.com/sharmaniraj009/opencap/issues/45) MC port |
| `opcua` | — | [#61](https://github.com/sharmaniraj009/opencap/issues/61): rebase, renumber to **D-045–D-047**, security defaults, client write guard | after merge [#28](https://github.com/sharmaniraj009/opencap/issues/28) server hardening (P1), [#29](https://github.com/sharmaniraj009/opencap/issues/29) L1 suite (P1), [#30](https://github.com/sharmaniraj009/opencap/issues/30) quality module |
| `serial` | [#62](https://github.com/sharmaniraj009/opencap/issues/62) | — | [#68](https://github.com/sharmaniraj009/opencap/issues/68) serial follow-ups (P2), [#47](https://github.com/sharmaniraj009/opencap/issues/47) Rust RTU port (P2), [#14](https://github.com/sharmaniraj009/opencap/issues/14) DNP3 spike (P2) |
| `web-ui` | — | [#63](https://github.com/sharmaniraj009/opencap/issues/63): 4 blocking fixes (auth before body + size cap, DNS rebinding, read-only config dirs, multi-file atomicity), renumber to **D-039–D-044** | after merge [#31](https://github.com/sharmaniraj009/opencap/issues/31) Quick Client (P1), [#32](https://github.com/sharmaniraj009/opencap/issues/32) OpenAPI (P1), [#33](https://github.com/sharmaniraj009/opencap/issues/33) import wizard, [#37](https://github.com/sharmaniraj009/opencap/issues/37) project v2 |
| `rust-engine` | [#64](https://github.com/sharmaniraj009/opencap/issues/64) | — | [#66](https://github.com/sharmaniraj009/opencap/issues/66) review follow-ups (P1), [#67](https://github.com/sharmaniraj009/opencap/issues/67) driver crates (P1), [#41](https://github.com/sharmaniraj009/opencap/issues/41) tag store + bus (P1), [#42](https://github.com/sharmaniraj009/opencap/issues/42) dual-engine acceptance (P1), [#49](https://github.com/sharmaniraj009/opencap/issues/49) plugin host |

The protocol decides, not this table: take the highest-priority `ready` issue in your area, else any `ready` issue. Sessions with a PR in review: fix the review first.

## Decision-log numbers (`docs/decisions.md`)

Ranges are pre-assigned so merge order does not matter: [#64](https://github.com/sharmaniraj009/opencap/issues/64) D-032 ✅ · [#62](https://github.com/sharmaniraj009/opencap/issues/62) D-033–D-038 ✅ · [#63](https://github.com/sharmaniraj009/opencap/issues/63) D-039–D-044 · [#61](https://github.com/sharmaniraj009/opencap/issues/61) D-045–D-047 · [#60](https://github.com/sharmaniraj009/opencap/issues/60) D-048–D-058. **Next free: D-059.** Any new PR takes the next free number when it rebases, as before.

## Open PRs

| PR | Area | State |
|---|---|---|
| [#60](https://github.com/sharmaniraj009/opencap/issues/60) | driver | changes requested (mechanical): rebase, renumber, import guard |
| [#61](https://github.com/sharmaniraj009/opencap/issues/61) | driver, north | changes requested: rebase, renumber, security defaults, client write guard |
| [#63](https://github.com/sharmaniraj009/opencap/issues/63) | ui | changes requested: 4 blocking defects |
| lead PR (pass 2) | docs, infra | CI `.[dev,all]`, architecture reconciled with RFC 0001, board |

## Ready queue by area (priority order)

| Area | Ready issues |
|---|---|
| core | [#2](https://github.com/sharmaniraj009/opencap/issues/2) P1 config JSON Schema · [#3](https://github.com/sharmaniraj009/opencap/issues/3) P1 Modbus/scaling/payload vectors · [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 AB vectors · [#65](https://github.com/sharmaniraj009/opencap/issues/65) P1 flaky test · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 benchmark · [#10](https://github.com/sharmaniraj009/opencap/issues/10) P2 KEPServerEX CSV · [#19](https://github.com/sharmaniraj009/opencap/issues/19) P2 RFC 0002 write safety · [#21](https://github.com/sharmaniraj009/opencap/issues/21) P2 RFC 0005 security · [#22](https://github.com/sharmaniraj009/opencap/issues/22) P2 system tags spec · [#23](https://github.com/sharmaniraj009/opencap/issues/23) P2 derived tags spec · [#20](https://github.com/sharmaniraj009/opencap/issues/20) P3 RFC 0003 redundancy · [#24](https://github.com/sharmaniraj009/opencap/issues/24) P3 alarms spec · [#25](https://github.com/sharmaniraj009/opencap/issues/25) P3 data logger spec |
| engine-rust | [#66](https://github.com/sharmaniraj009/opencap/issues/66) P1 review follow-ups · [#67](https://github.com/sharmaniraj009/opencap/issues/67) P1 driver crates · [#41](https://github.com/sharmaniraj009/opencap/issues/41) P1 tag store + bus · [#42](https://github.com/sharmaniraj009/opencap/issues/42) P1 dual-engine acceptance · [#47](https://github.com/sharmaniraj009/opencap/issues/47) P2 Modbus RTU port · [#49](https://github.com/sharmaniraj009/opencap/issues/49) P2 plugin host · [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 services |
| driver | [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 · [#67](https://github.com/sharmaniraj009/opencap/issues/67) P1 · [#11](https://github.com/sharmaniraj009/opencap/issues/11) P2 Logix browse · [#12](https://github.com/sharmaniraj009/opencap/issues/12) P2 simulator driver · [#13](https://github.com/sharmaniraj009/opencap/issues/13) P2 FINS simulator · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 DNP3 spike · [#47](https://github.com/sharmaniraj009/opencap/issues/47) P2 · [#68](https://github.com/sharmaniraj009/opencap/issues/68) P2 serial follow-ups · [#15](https://github.com/sharmaniraj009/opencap/issues/15) P3 BACnet spike · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 IEC 104 spike |
| north | [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 Mosquitto restart test · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 · [#36](https://github.com/sharmaniraj009/opencap/issues/36) P2 IoT Gateway JSON · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 |
| infra | [#6](https://github.com/sharmaniraj009/opencap/issues/6) P1 licence gate · [#42](https://github.com/sharmaniraj009/opencap/issues/42) P1 · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 · [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 · [#9](https://github.com/sharmaniraj009/opencap/issues/9) P2 docs site · [#34](https://github.com/sharmaniraj009/opencap/issues/34) P2 release pipeline · [#35](https://github.com/sharmaniraj009/opencap/issues/35) P2 Windows CI · [#38](https://github.com/sharmaniraj009/opencap/issues/38) P2 CI follow-ups · [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 |
| scada | [#17](https://github.com/sharmaniraj009/opencap/issues/17) P2 matrix + recipes · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 RFC 0004 OPC DA |
| docs | [#9](https://github.com/sharmaniraj009/opencap/issues/9) · [#17](https://github.com/sharmaniraj009/opencap/issues/17) · [#18](https://github.com/sharmaniraj009/opencap/issues/18) · [#19](https://github.com/sharmaniraj009/opencap/issues/19) · [#20](https://github.com/sharmaniraj009/opencap/issues/20) · [#21](https://github.com/sharmaniraj009/opencap/issues/21) · [#22](https://github.com/sharmaniraj009/opencap/issues/22) · [#24](https://github.com/sharmaniraj009/opencap/issues/24) · [#25](https://github.com/sharmaniraj009/opencap/issues/25) · [#26](https://github.com/sharmaniraj009/opencap/issues/26) |
| ui | [#26](https://github.com/sharmaniraj009/opencap/issues/26) P2 screen spec |
| desktop | [#27](https://github.com/sharmaniraj009/opencap/issues/27) P2 Tauri shell |

## Blocked (and on what)

| Waiting on | Issues |
|---|---|
| [#61](https://github.com/sharmaniraj009/opencap/issues/61) (`opcua`) | [#28](https://github.com/sharmaniraj009/opencap/issues/28) P1, [#29](https://github.com/sharmaniraj009/opencap/issues/29) P1, [#30](https://github.com/sharmaniraj009/opencap/issues/30) P2; with Rust deps: [#46](https://github.com/sharmaniraj009/opencap/issues/46) (+[#41](https://github.com/sharmaniraj009/opencap/issues/41)), [#50](https://github.com/sharmaniraj009/opencap/issues/50) (+[#41](https://github.com/sharmaniraj009/opencap/issues/41), [#29](https://github.com/sharmaniraj009/opencap/issues/29)) |
| [#63](https://github.com/sharmaniraj009/opencap/issues/63) (`web-ui`) | [#31](https://github.com/sharmaniraj009/opencap/issues/31) P1, [#32](https://github.com/sharmaniraj009/opencap/issues/32) P1, [#33](https://github.com/sharmaniraj009/opencap/issues/33) P2, [#37](https://github.com/sharmaniraj009/opencap/issues/37) P2 (+[#2](https://github.com/sharmaniraj009/opencap/issues/2)) |
| [#60](https://github.com/sharmaniraj009/opencap/issues/60) (`s7-melsec`) | [#44](https://github.com/sharmaniraj009/opencap/issues/44) P2, [#45](https://github.com/sharmaniraj009/opencap/issues/45) P2 |
| ready issues | [#39](https://github.com/sharmaniraj009/opencap/issues/39) ← [#2](https://github.com/sharmaniraj009/opencap/issues/2) · [#40](https://github.com/sharmaniraj009/opencap/issues/40) ← [#3](https://github.com/sharmaniraj009/opencap/issues/3) · [#43](https://github.com/sharmaniraj009/opencap/issues/43) ← [#5](https://github.com/sharmaniraj009/opencap/issues/5) · [#48](https://github.com/sharmaniraj009/opencap/issues/48) ← [#43](https://github.com/sharmaniraj009/opencap/issues/43) · [#51](https://github.com/sharmaniraj009/opencap/issues/51) ← [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#52](https://github.com/sharmaniraj009/opencap/issues/52) ← [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#53](https://github.com/sharmaniraj009/opencap/issues/53) ← [#47](https://github.com/sharmaniraj009/opencap/issues/47), [#48](https://github.com/sharmaniraj009/opencap/issues/48) · [#54](https://github.com/sharmaniraj009/opencap/issues/54) ← [#13](https://github.com/sharmaniraj009/opencap/issues/13) · [#55](https://github.com/sharmaniraj009/opencap/issues/55) ← [#23](https://github.com/sharmaniraj009/opencap/issues/23), [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#56](https://github.com/sharmaniraj009/opencap/issues/56) ← [#22](https://github.com/sharmaniraj009/opencap/issues/22), [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#58](https://github.com/sharmaniraj009/opencap/issues/58) ← [#27](https://github.com/sharmaniraj009/opencap/issues/27) |

## Merge order (to minimise conflicts)

1. The lead PR for this pass (CI install line, docs).
2. [#60](https://github.com/sharmaniraj009/opencap/issues/60), [#61](https://github.com/sharmaniraj009/opencap/issues/61), [#63](https://github.com/sharmaniraj009/opencap/issues/63) in whatever order their fixes land. They touch disjoint code; [#61](https://github.com/sharmaniraj009/opencap/issues/61) and [#63](https://github.com/sharmaniraj009/opencap/issues/63) both touch `runtime.py`, `channel_worker.py` and `models.py` (auto-merge cleanly) and `pyproject.toml` (adjacent lines: the later one rebases).
3. Rust issues: [#67](https://github.com/sharmaniraj009/opencap/issues/67) first (crate layout), then the ports.

## Decisions for the owner

No new decisions this pass. The list stands. The lead continues with everything else meanwhile; reply on this issue.

1. **Pin this issue.** The lead's tools cannot pin issues or set label colours.
2. **Licence:** `LICENSE` is Apache-2.0 ([#4](https://github.com/sharmaniraj009/opencap/issues/4)). Confirm.
3. **Name:** "opencap": check availability (PyPI, GHCR, domain, trademarks). KEPServerEX is PTC's trademark, used only to describe compatibility.
4. **Writes to real plants:** stays forbidden (ADR-0008). RFC 0002 ([#19](https://github.com/sharmaniraj009/opencap/issues/19)) will put the options to you.
5. **Copyleft plugins:** may GPL stacks (lib60870, libiec61850) ship as separate, optional, out-of-process plugins? Recommendation: yes, never in the default build.
6. **OPC DA:** OPC Foundation Core Components redistribution or membership (RFC 0004, [#18](https://github.com/sharmaniraj009/opencap/issues/18)).
7. **Paid services:** Windows code signing, OPC Foundation certification, vendor SCADA licences for manual tests, a PyPI account.
8. **Priorities for M5:** which KEPServerEX plug-ins your sites use (Advanced Tags, Alarms & Events, DataLogger, IoT Gateway, Redundancy, Scheduler, Local Historian).
9. **Telemetry:** none, ever. Confirm.
10. **Site work:** commissioning and the Rust cut-over shadow runs need someone on site. Each merged driver PR lists its "still to do on real hardware" checks: [#62](https://github.com/sharmaniraj009/opencap/issues/62) (DF1, RTU), [#64](https://github.com/sharmaniraj009/opencap/issues/64) (MQTT TLS), [#60](https://github.com/sharmaniraj009/opencap/issues/60) (S7, MELSEC), [#61](https://github.com/sharmaniraj009/opencap/issues/61) (OPC UA servers, a SCADA tag path).

## Backlog (next waves, not yet filed)

- Drivers (Rust): EtherNet/IP generic CIP explicit (after [#43](https://github.com/sharmaniraj009/opencap/issues/43)), Beckhoff ADS, GE SRTP, SNMP, MQTT client, ODBC client, OPC DA client; DNP3, IEC 104, BACnet/IP (after spikes [#14](https://github.com/sharmaniraj009/opencap/issues/14)–[#16](https://github.com/sharmaniraj009/opencap/issues/16)).
- Processing: alarms (after [#24](https://github.com/sharmaniraj009/opencap/issues/24)), data logger (after [#25](https://github.com/sharmaniraj009/opencap/issues/25)), redundancy (after [#20](https://github.com/sharmaniraj009/opencap/issues/20)), users/roles (after [#21](https://github.com/sharmaniraj009/opencap/issues/21)).
- Core: Rust KEPServerEX importer; re-import three-way merge (ADR-0013); client-driven scan rate; per-channel concurrency; inter-request delay; media-level redundancy; dynamic tags; scheduler.
- Diagnostics: comm frame capture, event log, `/health/live` + `/health/ready`, Prometheus alert rules.
- North: REST client/server agents; OPC DA server bridge (after [#18](https://github.com/sharmaniraj009/opencap/issues/18) + owner decision); OPC AE.
