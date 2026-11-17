# opencap program board

Status, assignments and next priorities for opencap. Maintained by the technical lead on every pass; the issue [#1](https://github.com/sharmaniraj009/opencap/issues/1) and `docs/BOARD.md` carry the same text. How to work with it: `CONTRIBUTING.md` → "Engineering protocol".

**Last updated:** 2026-11-17 (pass 1)

## Status summary

- `main` holds the Python reference (Modbus TCP, AB Logix/Micro800, AB PCCC, MQTT + store-and-forward, admin API, KEPServerEX import) plus [#4](https://github.com/sharmaniraj009/opencap/issues/4) (Apache-2.0 licence, CI on 3.12/3.13, container/systemd/Windows packaging, `mqtt.topic_prefix`), merged after review.
- Architecture of record: `docs/architecture/` (requirements, high-level design, deep dive, scale and reliability, trade-offs, roadmap and migration, SCADA matrix, OSS/licence table) and ADRs 0001–0014 in `docs/adr/`. Landing with [#59](https://github.com/sharmaniraj009/opencap/issues/59).
- Milestone **M1** (Python parity for the reference project) in progress; **M2** (Rust engine core) starts with the `rust-engine` PR. Milestones: `docs/architecture/06-roadmap-and-migration.md`.
- First wave filed: 56 issues ([#2](https://github.com/sharmaniraj009/opencap/issues/2)–[#58](https://github.com/sharmaniraj009/opencap/issues/58)); 29 `ready`, 27 `blocked` on in-flight PRs or on each other.

## Engineer sessions

| Session | Current work | PR | Suggested next (`ready`, highest priority first) | After its own PR merges |
|---|---|---|---|---|
| `platform` | licence, CI, docs, packaging, topic prefix | [#4](https://github.com/sharmaniraj009/opencap/issues/4) merged | [#6](https://github.com/sharmaniraj009/opencap/issues/6) licence gate (P1), [#38](https://github.com/sharmaniraj009/opencap/issues/38) CI follow-ups, [#35](https://github.com/sharmaniraj009/opencap/issues/35) Windows CI, [#34](https://github.com/sharmaniraj009/opencap/issues/34) release pipeline, [#9](https://github.com/sharmaniraj009/opencap/issues/9) docs site | — |
| `s7-melsec` | Siemens S7 + Mitsubishi MC drivers (Python) | not opened yet | [#5](https://github.com/sharmaniraj009/opencap/issues/5) AB conformance vectors (P1), [#11](https://github.com/sharmaniraj009/opencap/issues/11) Logix browse, [#12](https://github.com/sharmaniraj009/opencap/issues/12) simulator driver, [#13](https://github.com/sharmaniraj009/opencap/issues/13) FINS simulator | [#44](https://github.com/sharmaniraj009/opencap/issues/44) S7 port, [#45](https://github.com/sharmaniraj009/opencap/issues/45) MC port |
| `opcua` | OPC UA client driver + OPC UA server (Python) | not opened yet | [#17](https://github.com/sharmaniraj009/opencap/issues/17) SCADA matrix + recipes, [#18](https://github.com/sharmaniraj009/opencap/issues/18) RFC 0004 OPC DA bridge | [#28](https://github.com/sharmaniraj009/opencap/issues/28) server hardening (P1), [#29](https://github.com/sharmaniraj009/opencap/issues/29) L1 suite (P1), [#30](https://github.com/sharmaniraj009/opencap/issues/30) quality module, [#46](https://github.com/sharmaniraj009/opencap/issues/46) client port, [#50](https://github.com/sharmaniraj009/opencap/issues/50) Rust server |
| `serial` | Modbus RTU + AB DF1 (Python) | not opened yet | [#14](https://github.com/sharmaniraj009/opencap/issues/14) DNP3 spike, [#10](https://github.com/sharmaniraj009/opencap/issues/10) KEPServerEX CSV tags, [#16](https://github.com/sharmaniraj009/opencap/issues/16) IEC 104 spike | [#47](https://github.com/sharmaniraj009/opencap/issues/47) RTU port, [#53](https://github.com/sharmaniraj009/opencap/issues/53) DF1 port |
| `web-ui` | web UI + config REST API | not opened yet | [#26](https://github.com/sharmaniraj009/opencap/issues/26) UI screen spec, [#27](https://github.com/sharmaniraj009/opencap/issues/27) Tauri shell | [#31](https://github.com/sharmaniraj009/opencap/issues/31) Quick Client (P1), [#32](https://github.com/sharmaniraj009/opencap/issues/32) OpenAPI contract (P1), [#33](https://github.com/sharmaniraj009/opencap/issues/33) import wizard, [#37](https://github.com/sharmaniraj009/opencap/issues/37) project file v2 |
| `rust-engine` | RFC 0001, `engine/` workspace, Modbus + MQTT + API parity | not opened yet | [#3](https://github.com/sharmaniraj009/opencap/issues/3) Modbus/payload vectors (P1), [#2](https://github.com/sharmaniraj009/opencap/issues/2) config schema (P1), [#7](https://github.com/sharmaniraj009/opencap/issues/7) benchmark harness | [#39](https://github.com/sharmaniraj009/opencap/issues/39)–[#43](https://github.com/sharmaniraj009/opencap/issues/43), [#49](https://github.com/sharmaniraj009/opencap/issues/49)–[#52](https://github.com/sharmaniraj009/opencap/issues/52) |

The protocol decides, not this table: take the highest-priority `ready` issue in your area, else any `ready` issue.

**Request to `rust-engine`:** if your PR is large, land RFC 0001 + the workspace + the Rust `Driver` trait first as a small PR. 15 issues wait on it directly and 5 more indirectly ([#39](https://github.com/sharmaniraj009/opencap/issues/39)–[#58](https://github.com/sharmaniraj009/opencap/issues/58)), and the other sessions can start ports in parallel as soon as it merges.

## Open PRs

| PR | Area | State |
|---|---|---|
| [#59](https://github.com/sharmaniraj009/opencap/issues/59) | docs | lead's PR: architecture, ADRs, merged CONTRIBUTING.md, ROADMAP.md reconciled, board mirror; merging when CI is green |

## Ready queue by area (priority order)

| Area | Ready issues |
|---|---|
| core | [#2](https://github.com/sharmaniraj009/opencap/issues/2) P1 config JSON Schema · [#3](https://github.com/sharmaniraj009/opencap/issues/3) P1 Modbus/scaling/payload vectors · [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 AB vectors · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 benchmark · [#10](https://github.com/sharmaniraj009/opencap/issues/10) P2 KEPServerEX CSV · [#19](https://github.com/sharmaniraj009/opencap/issues/19) P2 RFC 0002 write safety · [#21](https://github.com/sharmaniraj009/opencap/issues/21) P2 RFC 0005 security · [#22](https://github.com/sharmaniraj009/opencap/issues/22) P2 system tags spec · [#23](https://github.com/sharmaniraj009/opencap/issues/23) P2 derived tags spec · [#20](https://github.com/sharmaniraj009/opencap/issues/20) P3 RFC 0003 redundancy · [#24](https://github.com/sharmaniraj009/opencap/issues/24) P3 alarms spec · [#25](https://github.com/sharmaniraj009/opencap/issues/25) P3 data logger spec |
| driver | [#5](https://github.com/sharmaniraj009/opencap/issues/5) P1 · [#11](https://github.com/sharmaniraj009/opencap/issues/11) P2 Logix browse · [#12](https://github.com/sharmaniraj009/opencap/issues/12) P2 simulator driver · [#13](https://github.com/sharmaniraj009/opencap/issues/13) P2 FINS simulator · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 DNP3 spike · [#15](https://github.com/sharmaniraj009/opencap/issues/15) P3 BACnet spike · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 IEC 104 spike |
| north | [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 Mosquitto restart test · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 · [#36](https://github.com/sharmaniraj009/opencap/issues/36) P2 IoT Gateway JSON · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 |
| infra | [#6](https://github.com/sharmaniraj009/opencap/issues/6) P1 licence gate · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 · [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 · [#9](https://github.com/sharmaniraj009/opencap/issues/9) P2 docs site · [#34](https://github.com/sharmaniraj009/opencap/issues/34) P2 release pipeline · [#35](https://github.com/sharmaniraj009/opencap/issues/35) P2 Windows CI · [#38](https://github.com/sharmaniraj009/opencap/issues/38) P2 CI follow-ups |
| scada | [#17](https://github.com/sharmaniraj009/opencap/issues/17) P2 matrix + recipes · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 RFC 0004 OPC DA |
| docs | [#9](https://github.com/sharmaniraj009/opencap/issues/9) · [#17](https://github.com/sharmaniraj009/opencap/issues/17) · [#18](https://github.com/sharmaniraj009/opencap/issues/18) · [#19](https://github.com/sharmaniraj009/opencap/issues/19) · [#20](https://github.com/sharmaniraj009/opencap/issues/20) · [#21](https://github.com/sharmaniraj009/opencap/issues/21) · [#22](https://github.com/sharmaniraj009/opencap/issues/22) · [#24](https://github.com/sharmaniraj009/opencap/issues/24) · [#25](https://github.com/sharmaniraj009/opencap/issues/25) · [#26](https://github.com/sharmaniraj009/opencap/issues/26) |
| ui | [#26](https://github.com/sharmaniraj009/opencap/issues/26) P2 screen spec |
| desktop | [#27](https://github.com/sharmaniraj009/opencap/issues/27) P2 Tauri shell |
| engine-rust | none yet: all wait for the `rust-engine` PR |

## Blocked (and on what)

| Waiting on | Issues |
|---|---|
| `opcua` PR | [#28](https://github.com/sharmaniraj009/opencap/issues/28) P1, [#29](https://github.com/sharmaniraj009/opencap/issues/29) P1, [#30](https://github.com/sharmaniraj009/opencap/issues/30) P2; with Rust deps: [#46](https://github.com/sharmaniraj009/opencap/issues/46), [#50](https://github.com/sharmaniraj009/opencap/issues/50) |
| `web-ui` PR | [#31](https://github.com/sharmaniraj009/opencap/issues/31) P1, [#32](https://github.com/sharmaniraj009/opencap/issues/32) P1, [#33](https://github.com/sharmaniraj009/opencap/issues/33) P2, [#37](https://github.com/sharmaniraj009/opencap/issues/37) P2 (also [#2](https://github.com/sharmaniraj009/opencap/issues/2)) |
| `rust-engine` PR | [#39](https://github.com/sharmaniraj009/opencap/issues/39) P1 (+[#2](https://github.com/sharmaniraj009/opencap/issues/2)), [#40](https://github.com/sharmaniraj009/opencap/issues/40) P1 (+[#3](https://github.com/sharmaniraj009/opencap/issues/3)), [#41](https://github.com/sharmaniraj009/opencap/issues/41) P1, [#42](https://github.com/sharmaniraj009/opencap/issues/42) P1, [#43](https://github.com/sharmaniraj009/opencap/issues/43) P1 (+[#5](https://github.com/sharmaniraj009/opencap/issues/5)), [#44](https://github.com/sharmaniraj009/opencap/issues/44) P2, [#45](https://github.com/sharmaniraj009/opencap/issues/45) P2, [#46](https://github.com/sharmaniraj009/opencap/issues/46) P2, [#47](https://github.com/sharmaniraj009/opencap/issues/47) P2, [#49](https://github.com/sharmaniraj009/opencap/issues/49) P2, [#50](https://github.com/sharmaniraj009/opencap/issues/50) P1, [#51](https://github.com/sharmaniraj009/opencap/issues/51) P1, [#52](https://github.com/sharmaniraj009/opencap/issues/52) P2, [#54](https://github.com/sharmaniraj009/opencap/issues/54) P2 (+[#13](https://github.com/sharmaniraj009/opencap/issues/13)), [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 |
| `s7-melsec` PR | [#44](https://github.com/sharmaniraj009/opencap/issues/44), [#45](https://github.com/sharmaniraj009/opencap/issues/45) |
| `serial` PR | [#47](https://github.com/sharmaniraj009/opencap/issues/47), [#53](https://github.com/sharmaniraj009/opencap/issues/53) |
| other issues | [#48](https://github.com/sharmaniraj009/opencap/issues/48) ← [#43](https://github.com/sharmaniraj009/opencap/issues/43) · [#53](https://github.com/sharmaniraj009/opencap/issues/53) ← [#47](https://github.com/sharmaniraj009/opencap/issues/47), [#48](https://github.com/sharmaniraj009/opencap/issues/48) · [#55](https://github.com/sharmaniraj009/opencap/issues/55) ← [#23](https://github.com/sharmaniraj009/opencap/issues/23), [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#56](https://github.com/sharmaniraj009/opencap/issues/56) ← [#22](https://github.com/sharmaniraj009/opencap/issues/22), [#41](https://github.com/sharmaniraj009/opencap/issues/41) · [#58](https://github.com/sharmaniraj009/opencap/issues/58) ← [#27](https://github.com/sharmaniraj009/opencap/issues/27) |

## Merge order (to minimise conflicts)

1. Architecture PR (docs only, new directories).
2. Driver PRs (`s7-melsec`, `serial`): new driver files; hotspot one-liners in `drivers/__init__.py`, `models.py`, `kep_import.py`.
3. `opcua` (touches `runtime.py`, `models.py`), then `web-ui` (touches `api/`, `runtime.py`, the loader). Each rebased on the previous.
4. `rust-engine` (new `engine/`, possibly test fixtures) whenever it is ready, since it barely overlaps.
PRs touching the same hotspot are merged one at a time; the later one is asked to rebase.

## Decisions for the owner

The lead continues with everything else meanwhile. Reply on this issue.

1. **Pin this issue.** The lead's tools cannot pin issues or set label colours. (One click: "Pin issue" in the sidebar.)
2. **Licence:** `LICENSE` is Apache-2.0 (merged in [#4](https://github.com/sharmaniraj009/opencap/issues/4), matching `pyproject.toml`). Confirm. If you want a different licence, it has to change before outside contributions arrive.
3. **Name:** "opencap": check that the name is free where you want to publish (PyPI, GHCR, a domain, trademarks). KEPServerEX is PTC's trademark, so we use it only to describe compatibility (constraint C-7).
4. **Writes to real plants:** stays forbidden (ADR-0008). RFC 0002 ([#19](https://github.com/sharmaniraj009/opencap/issues/19)) will put the options in front of you; nothing changes until you approve.
5. **Copyleft plugins:** may GPL stacks (lib60870 for IEC 104, libiec61850 for IEC 61850) be shipped as separately distributed, out-of-process optional plugins? Recommendation: yes, never in the default build (ADR-0007). The alternative is native Rust implementations: slower to build, no licence question.
6. **OPC DA:** may we redistribute OPC Foundation components (Core Components proxy/stub, OpcEnum), or do you have OPC Foundation membership? RFC 0004 ([#18](https://github.com/sharmaniraj009/opencap/issues/18)) will ask the precise question. Recommendation until then: a native DA server that relies on the Core Components already installed on SCADA hosts.
7. **Paid services** (none bought without you): a Windows code-signing certificate (MSI/desktop app SmartScreen), OPC Foundation certification (L3), vendor SCADA licences/trials for manual L2 tests, a PyPI account/organisation if you want PyPI releases ([#34](https://github.com/sharmaniraj009/opencap/issues/34) publishes to GHCR and GitHub Releases only).
8. **Priorities for M5:** which KEPServerEX plug-ins do your sites use today (Advanced Tags, Alarms & Events, DataLogger, IoT Gateway, Redundancy, Scheduler, Local Historian)? This orders the M5 work.
9. **Telemetry:** none, ever. That is the design. Confirm.
10. **Site work:** commissioning and the Rust cut-over shadow runs (M4) need someone on site. Nothing connects to plant from CI or cloud sessions.

## Backlog (next waves, not yet filed)

- Drivers (Rust): EtherNet/IP generic CIP explicit (after [#43](https://github.com/sharmaniraj009/opencap/issues/43)), Beckhoff ADS, GE SRTP, SNMP, MQTT client, ODBC client, OPC DA client; DNP3 master/outstation, IEC 104 client/server, BACnet/IP (after spikes [#14](https://github.com/sharmaniraj009/opencap/issues/14)–[#16](https://github.com/sharmaniraj009/opencap/issues/16)).
- Processing: alarms implementation (after [#24](https://github.com/sharmaniraj009/opencap/issues/24)), data logger (after [#25](https://github.com/sharmaniraj009/opencap/issues/25)), redundancy (after [#20](https://github.com/sharmaniraj009/opencap/issues/20)), users/roles (after [#21](https://github.com/sharmaniraj009/opencap/issues/21)).
- Core: Rust KEPServerEX importer; re-import three-way merge (ADR-0013); client-driven scan rate; per-channel concurrency; inter-request delay; media-level redundancy; dynamic tags; scheduler.
- Diagnostics: comm frame capture, event log, `/health/live` + `/health/ready`, Prometheus alert rules.
- North: REST client/server agents; OPC DA server bridge implementation (after [#18](https://github.com/sharmaniraj009/opencap/issues/18) + owner decision); OPC AE.
