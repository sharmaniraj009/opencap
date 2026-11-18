# opencap program board

Status, assignments and next priorities for opencap. Maintained by the technical lead on every pass; the issue [#1](https://github.com/sharmaniraj009/opencap/issues/1) and `docs/BOARD.md` carry the same text. How to work with it: `CONTRIBUTING.md` → "Engineering protocol".

**Last updated:** 2026-11-18 00:40 UTC, pass 3

## Status summary

- **Merged since pass 2:**
  - [#72](https://github.com/sharmaniraj009/opencap/issues/72) (CI licence gate for Python and Rust, closes [#6](https://github.com/sharmaniraj009/opencap/issues/6));
  - [#71](https://github.com/sharmaniraj009/opencap/issues/71) (config JSON Schema + conformance fixtures, closes [#2](https://github.com/sharmaniraj009/opencap/issues/2)).
- **Merged earlier:**
  - [#4](https://github.com/sharmaniraj009/opencap/issues/4) (licence, CI, packaging);
  - [#59](https://github.com/sharmaniraj009/opencap/issues/59) (architecture);
  - [#64](https://github.com/sharmaniraj009/opencap/issues/64) (Rust engine core);
  - [#62](https://github.com/sharmaniraj009/opencap/issues/62) (serial drivers);
  - [#69](https://github.com/sharmaniraj009/opencap/issues/69) (de-flake, closes [#65](https://github.com/sharmaniraj009/opencap/issues/65));
  - [#70](https://github.com/sharmaniraj009/opencap/issues/70) (architecture reconciled with RFC 0001).
- **Stalled:** [#60](https://github.com/sharmaniraj009/opencap/issues/60) (S7 + MELSEC), [#61](https://github.com/sharmaniraj009/opencap/issues/61) (OPC UA) and [#63](https://github.com/sharmaniraj009/opencap/issues/63) (web UI) have had no answer to their reviews for several passes. Each is now a **P1 takeover issue**: [#73](https://github.com/sharmaniraj009/opencap/issues/73), [#74](https://github.com/sharmaniraj009/opencap/issues/74), [#75](https://github.com/sharmaniraj009/opencap/issues/75). Any engineer can finish one from the PR head (CONTRIBUTING → "Stalled PRs").
- **Milestones:** M1 (Python parity) needs [#73](https://github.com/sharmaniraj009/opencap/issues/73), [#74](https://github.com/sharmaniraj009/opencap/issues/74) and [#75](https://github.com/sharmaniraj009/opencap/issues/75). M2 (Rust core) is under way: the config schema is in, so [#39](https://github.com/sharmaniraj009/opencap/issues/39) is now ready.
- **CI:** tests on 3.12/3.13 with every extra, wheel + image, Rust (fmt, clippy, test, deny, conformance, Windows), licence gates. Nightly acceptance.

## Engineer sessions

| Session | Done | Now |
|---|---|---|
| `engineer-1` | [#69](https://github.com/sharmaniraj009/opencap/issues/69) (flaky test) | — |
| `engineer-3` | [#71](https://github.com/sharmaniraj009/opencap/issues/71) (config schema) | — |
| `engineer-4` | [#72](https://github.com/sharmaniraj009/opencap/issues/72) (licence gate) | — |
| `s7-melsec`, `opcua`, `web-ui` (original sessions) | — | silent; their PRs moved to takeover issues [#73](https://github.com/sharmaniraj009/opencap/issues/73), [#74](https://github.com/sharmaniraj009/opencap/issues/74), [#75](https://github.com/sharmaniraj009/opencap/issues/75) |
| `platform`, `serial`, `rust-engine` (original sessions) | [#4](https://github.com/sharmaniraj009/opencap/issues/4), [#62](https://github.com/sharmaniraj009/opencap/issues/62), [#64](https://github.com/sharmaniraj009/opencap/issues/64) | — |

**Next for anyone (P1, `ready`):**
- [#73](https://github.com/sharmaniraj009/opencap/issues/73) finish S7 + MELSEC;
- [#74](https://github.com/sharmaniraj009/opencap/issues/74) finish OPC UA;
- [#75](https://github.com/sharmaniraj009/opencap/issues/75) finish the web UI;
- [#3](https://github.com/sharmaniraj009/opencap/issues/3) Modbus/payload vectors;
- [#5](https://github.com/sharmaniraj009/opencap/issues/5) AB vectors;
- [#39](https://github.com/sharmaniraj009/opencap/issues/39) Rust config from the schema;
- [#41](https://github.com/sharmaniraj009/opencap/issues/41) tag store + bus;
- [#42](https://github.com/sharmaniraj009/opencap/issues/42) dual-engine acceptance;
- [#66](https://github.com/sharmaniraj009/opencap/issues/66) Rust review follow-ups;
- [#67](https://github.com/sharmaniraj009/opencap/issues/67) Rust driver crates.

Take the highest priority in your area, else any `ready` issue.

## Decision-log numbers (`docs/decisions.md`)

Reserved for the stalled PRs: [#63](https://github.com/sharmaniraj009/opencap/issues/63)/[#75](https://github.com/sharmaniraj009/opencap/issues/75) D-039–D-044, [#61](https://github.com/sharmaniraj009/opencap/issues/61)/[#74](https://github.com/sharmaniraj009/opencap/issues/74) D-045–D-047, [#60](https://github.com/sharmaniraj009/opencap/issues/60)/[#73](https://github.com/sharmaniraj009/opencap/issues/73) D-048–D-058. Used: D-059 ([#72](https://github.com/sharmaniraj009/opencap/issues/72)), D-060 ([#71](https://github.com/sharmaniraj009/opencap/issues/71)). **Next free: D-062.**

## Open PRs

| PR | Area | State |
|---|---|---|
| [#60](https://github.com/sharmaniraj009/opencap/issues/60) | driver | stalled; finish via [#73](https://github.com/sharmaniraj009/opencap/issues/73) |
| [#61](https://github.com/sharmaniraj009/opencap/issues/61) | driver, north | stalled; finish via [#74](https://github.com/sharmaniraj009/opencap/issues/74) |
| [#63](https://github.com/sharmaniraj009/opencap/issues/63) | ui | stalled; finish via [#75](https://github.com/sharmaniraj009/opencap/issues/75) |
| lead PR (pass 3) | docs | board + "Stalled PRs" rule in CONTRIBUTING (this PR) |

## Ready queue by area (priority order)

| Area | Ready issues |
|---|---|
| core | [#3](https://github.com/sharmaniraj009/opencap/issues/3) P1 Modbus/scaling/payload vectors · [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 AB vectors · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 benchmark · [#10](https://github.com/sharmaniraj009/opencap/issues/10) P2 KEPServerEX CSV · [#19](https://github.com/sharmaniraj009/opencap/issues/19) P2 RFC 0002 write safety · [#21](https://github.com/sharmaniraj009/opencap/issues/21) P2 RFC 0005 security · [#22](https://github.com/sharmaniraj009/opencap/issues/22) P2 system tags spec · [#23](https://github.com/sharmaniraj009/opencap/issues/23) P2 derived tags spec · [#20](https://github.com/sharmaniraj009/opencap/issues/20) P3 RFC 0003 redundancy · [#24](https://github.com/sharmaniraj009/opencap/issues/24) P3 alarms spec · [#25](https://github.com/sharmaniraj009/opencap/issues/25) P3 data logger spec |
| engine-rust | [#39](https://github.com/sharmaniraj009/opencap/issues/39) P1 config from schema · [#41](https://github.com/sharmaniraj009/opencap/issues/41) P1 tag store + bus · [#42](https://github.com/sharmaniraj009/opencap/issues/42) P1 dual-engine acceptance · [#66](https://github.com/sharmaniraj009/opencap/issues/66) P1 review follow-ups · [#67](https://github.com/sharmaniraj009/opencap/issues/67) P1 driver crates · [#47](https://github.com/sharmaniraj009/opencap/issues/47) P2 Modbus RTU port · [#49](https://github.com/sharmaniraj009/opencap/issues/49) P2 plugin host · [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 services |
| driver | [#73](https://github.com/sharmaniraj009/opencap/issues/73) P1 finish S7/MELSEC · [#74](https://github.com/sharmaniraj009/opencap/issues/74) P1 finish OPC UA · [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 · [#67](https://github.com/sharmaniraj009/opencap/issues/67) P1 · [#11](https://github.com/sharmaniraj009/opencap/issues/11) P2 Logix browse · [#12](https://github.com/sharmaniraj009/opencap/issues/12) P2 simulator driver · [#13](https://github.com/sharmaniraj009/opencap/issues/13) P2 FINS simulator · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 DNP3 spike · [#47](https://github.com/sharmaniraj009/opencap/issues/47) P2 · [#68](https://github.com/sharmaniraj009/opencap/issues/68) P2 serial follow-ups · [#15](https://github.com/sharmaniraj009/opencap/issues/15) P3 BACnet spike · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 IEC 104 spike |
| north | [#74](https://github.com/sharmaniraj009/opencap/issues/74) P1 · [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 Mosquitto restart test · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 · [#36](https://github.com/sharmaniraj009/opencap/issues/36) P2 IoT Gateway JSON · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 |
| infra | [#42](https://github.com/sharmaniraj009/opencap/issues/42) P1 · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 · [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 · [#9](https://github.com/sharmaniraj009/opencap/issues/9) P2 docs site · [#34](https://github.com/sharmaniraj009/opencap/issues/34) P2 release pipeline · [#35](https://github.com/sharmaniraj009/opencap/issues/35) P2 Windows CI · [#38](https://github.com/sharmaniraj009/opencap/issues/38) P2 CI follow-ups · [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 |
| scada | [#74](https://github.com/sharmaniraj009/opencap/issues/74) P1 · [#17](https://github.com/sharmaniraj009/opencap/issues/17) P2 matrix + recipes · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 RFC 0004 OPC DA |
| docs | [#9](https://github.com/sharmaniraj009/opencap/issues/9) · [#17](https://github.com/sharmaniraj009/opencap/issues/17) · [#18](https://github.com/sharmaniraj009/opencap/issues/18) · [#19](https://github.com/sharmaniraj009/opencap/issues/19) · [#20](https://github.com/sharmaniraj009/opencap/issues/20) · [#21](https://github.com/sharmaniraj009/opencap/issues/21) · [#22](https://github.com/sharmaniraj009/opencap/issues/22) · [#24](https://github.com/sharmaniraj009/opencap/issues/24) · [#25](https://github.com/sharmaniraj009/opencap/issues/25) · [#26](https://github.com/sharmaniraj009/opencap/issues/26) |
| ui | [#75](https://github.com/sharmaniraj009/opencap/issues/75) P1 finish web UI · [#26](https://github.com/sharmaniraj009/opencap/issues/26) P2 screen spec |
| desktop | [#27](https://github.com/sharmaniraj009/opencap/issues/27) P2 Tauri shell |

## Blocked (and on what)

| Waiting on | Issues |
|---|---|
| OPC UA lands ([#61](https://github.com/sharmaniraj009/opencap/issues/61) via [#74](https://github.com/sharmaniraj009/opencap/issues/74)) | [#28](https://github.com/sharmaniraj009/opencap/issues/28) P1, [#29](https://github.com/sharmaniraj009/opencap/issues/29) P1, [#30](https://github.com/sharmaniraj009/opencap/issues/30) P2; with Rust deps: [#46](https://github.com/sharmaniraj009/opencap/issues/46) (+[#41](https://github.com/sharmaniraj009/opencap/issues/41)), [#50](https://github.com/sharmaniraj009/opencap/issues/50) (+[#41](https://github.com/sharmaniraj009/opencap/issues/41), [#29](https://github.com/sharmaniraj009/opencap/issues/29)) |
| web UI lands ([#63](https://github.com/sharmaniraj009/opencap/issues/63) via [#75](https://github.com/sharmaniraj009/opencap/issues/75)) | [#31](https://github.com/sharmaniraj009/opencap/issues/31) P1, [#32](https://github.com/sharmaniraj009/opencap/issues/32) P1, [#33](https://github.com/sharmaniraj009/opencap/issues/33) P2, [#37](https://github.com/sharmaniraj009/opencap/issues/37) P2 |
| S7/MELSEC lands ([#60](https://github.com/sharmaniraj009/opencap/issues/60) via [#73](https://github.com/sharmaniraj009/opencap/issues/73)) | [#44](https://github.com/sharmaniraj009/opencap/issues/44) P2, [#45](https://github.com/sharmaniraj009/opencap/issues/45) P2 |
| ready issues | [#40](https://github.com/sharmaniraj009/opencap/issues/40) ← [#3](https://github.com/sharmaniraj009/opencap/issues/3) · [#43](https://github.com/sharmaniraj009/opencap/issues/43) ← [#5](https://github.com/sharmaniraj009/opencap/issues/5) · [#48](https://github.com/sharmaniraj009/opencap/issues/48) ← [#43](https://github.com/sharmaniraj009/opencap/issues/43) · [#51](https://github.com/sharmaniraj009/opencap/issues/51) ← [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#52](https://github.com/sharmaniraj009/opencap/issues/52) ← [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#53](https://github.com/sharmaniraj009/opencap/issues/53) ← [#47](https://github.com/sharmaniraj009/opencap/issues/47), [#48](https://github.com/sharmaniraj009/opencap/issues/48) · [#54](https://github.com/sharmaniraj009/opencap/issues/54) ← [#13](https://github.com/sharmaniraj009/opencap/issues/13) · [#55](https://github.com/sharmaniraj009/opencap/issues/55) ← [#23](https://github.com/sharmaniraj009/opencap/issues/23), [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#56](https://github.com/sharmaniraj009/opencap/issues/56) ← [#22](https://github.com/sharmaniraj009/opencap/issues/22), [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#58](https://github.com/sharmaniraj009/opencap/issues/58) ← [#27](https://github.com/sharmaniraj009/opencap/issues/27) |

## Merge order (to minimise conflicts)

1. Whichever of [#73](https://github.com/sharmaniraj009/opencap/issues/73), [#74](https://github.com/sharmaniraj009/opencap/issues/74), [#75](https://github.com/sharmaniraj009/opencap/issues/75) lands first. They touch disjoint code. [#74](https://github.com/sharmaniraj009/opencap/issues/74) and [#75](https://github.com/sharmaniraj009/opencap/issues/75) share `runtime.py`, `channel_worker.py` and `models.py` (auto-merge cleanly) and `pyproject.toml` (adjacent lines: the later one rebases).
2. Rust issues: [#67](https://github.com/sharmaniraj009/opencap/issues/67) (crate layout) before the driver ports.

**Timing tests to watch** (tight wall-clock bounds found by [#69](https://github.com/sharmaniraj009/opencap/issues/69); loosen if they flake): `tests/integration/test_publisher.py:301` (150 ms), `tests/integration/test_modbus_io.py:264` (250 ms).

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
10. **Site work:** commissioning and the Rust cut-over shadow runs need someone on site. Each driver PR lists its "still to do on real hardware" checks: [#62](https://github.com/sharmaniraj009/opencap/issues/62) (DF1, RTU), [#64](https://github.com/sharmaniraj009/opencap/issues/64) (MQTT TLS), [#60](https://github.com/sharmaniraj009/opencap/issues/60) (S7, MELSEC), [#61](https://github.com/sharmaniraj009/opencap/issues/61) (OPC UA servers, a SCADA tag path).

## Backlog (next waves, not yet filed)

- Drivers (Rust): EtherNet/IP generic CIP explicit (after [#43](https://github.com/sharmaniraj009/opencap/issues/43)), Beckhoff ADS, GE SRTP, SNMP, MQTT client, ODBC client, OPC DA client; DNP3, IEC 104, BACnet/IP (after spikes [#14](https://github.com/sharmaniraj009/opencap/issues/14)–[#16](https://github.com/sharmaniraj009/opencap/issues/16)).
- Processing: alarms (after [#24](https://github.com/sharmaniraj009/opencap/issues/24)), data logger (after [#25](https://github.com/sharmaniraj009/opencap/issues/25)), redundancy (after [#20](https://github.com/sharmaniraj009/opencap/issues/20)), users/roles (after [#21](https://github.com/sharmaniraj009/opencap/issues/21)).
- Core: Rust KEPServerEX importer; re-import three-way merge (ADR-0013); client-driven scan rate; per-channel concurrency; inter-request delay; media-level redundancy; dynamic tags; scheduler.
- Diagnostics: comm frame capture, event log, `/health/live` + `/health/ready`, Prometheus alert rules.
- North: REST client/server agents; OPC DA server bridge (after [#18](https://github.com/sharmaniraj009/opencap/issues/18) + owner decision); OPC AE.
