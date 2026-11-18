# opencap program board

Status, assignments and next priorities for opencap. Maintained by the technical lead on every pass; the issue [#1](https://github.com/sharmaniraj009/opencap/issues/1) and `docs/BOARD.md` carry the same text. How to work with it: `CONTRIBUTING.md` → "Engineering protocol".

**Last updated:** 2026-11-18 10:55 UTC, pass 11 (lead-2)

## The push: resolve every open issue

The owner asked to resolve every issue that was open when the push started ([#3](https://github.com/sharmaniraj009/opencap/issues/3)–[#90](https://github.com/sharmaniraj009/opencap/issues/90), 57 issues; [#1](https://github.com/sharmaniraj009/opencap/issues/1) is this board). Ten area sessions work their queues in order, one PR per issue; `lead-2` reviews, merges, relabels and keeps this board. The previous lead session has handed over: it no longer merges or edits the board, and may leave comment reviews.

- **Progress:** 3 of 57 closed ([#3](https://github.com/sharmaniraj009/opencap/issues/3), [#38](https://github.com/sharmaniraj009/opencap/issues/38), [#67](https://github.com/sharmaniraj009/opencap/issues/67)). 4 PRs in review. 10 issues claimed.
- **Done when:** each of the 57 is closed by a merged PR, or closed with a written reason. Follow-up issues filed during the push ([#102](https://github.com/sharmaniraj009/opencap/issues/102) onward) are not part of that set; sessions take them only after their own queue.

## Status summary

- **Merged in pass 11:**
  - [#100](https://github.com/sharmaniraj009/opencap/issues/100) (**one crate per Rust driver**, `engine/drivers/<name>` behind `driver-<name>` features, closes [#67](https://github.com/sharmaniraj009/opencap/issues/67)); RFC 0001 §2.2/§10 Q1 and `02-high-level-design.md` updated to match;
  - [#101](https://github.com/sharmaniraj009/opencap/issues/101) (**CI push builds only on `main`; `acceptance-pr` on PRs that touch the engine**, closes [#38](https://github.com/sharmaniraj009/opencap/issues/38));
  - [#114](https://github.com/sharmaniraj009/opencap/issues/114) (**464 Modbus/scaling/payload conformance vectors**, format 1 in `tests/conformance/`, closes [#3](https://github.com/sharmaniraj009/opencap/issues/3));
  - [#99](https://github.com/sharmaniraj009/opencap/issues/99) (board pass 10, the previous lead's last).
- **Unblocked:** [#40](https://github.com/sharmaniraj009/opencap/issues/40) (Rust conformance harness, P1) → `ready`; [#115](https://github.com/sharmaniraj009/opencap/issues/115) (BCD/Date/8-bit types) → `ready`. [#44](https://github.com/sharmaniraj009/opencap/issues/44), [#45](https://github.com/sharmaniraj009/opencap/issues/45), [#47](https://github.com/sharmaniraj009/opencap/issues/47) stay `ready` now that [#67](https://github.com/sharmaniraj009/opencap/issues/67) is on `main`.
- **Follow-ups filed:** every unchecked row of `plan/checklist.md` now has an issue: 33 new issues [#103](https://github.com/sharmaniraj009/opencap/issues/103)–[#145](https://github.com/sharmaniraj009/opencap/issues/145) (one PR each, P2/P3; IF-09 mapped to [#28](https://github.com/sharmaniraj009/opencap/issues/28)). The plan's event-log rows (ADM-11, DIAG-03, LOG-R9–R11) now point at [#136](https://github.com/sharmaniraj009/opencap/issues/136) and DIAG-11 at [#138](https://github.com/sharmaniraj009/opencap/issues/138) instead of [#56](https://github.com/sharmaniraj009/opencap/issues/56), which covers system tags only. Sessions also filed [#102](https://github.com/sharmaniraj009/opencap/issues/102) (desktop advisory), [#120](https://github.com/sharmaniraj009/opencap/issues/120)/#122/#125/#127/#128/#130 (system-tag implementation, from [#22](https://github.com/sharmaniraj009/opencap/issues/22)) and [#124](https://github.com/sharmaniraj009/opencap/issues/124) (Rust simulator port, from [#12](https://github.com/sharmaniraj009/opencap/issues/12)).
- **CI:** green on `main`. PR branches now run CI once (pull request only). `acceptance-pr` runs on PRs touching `src/opencap/`, `tests/helpers/`, `tests/integration/`, `tests/conftest.py` or `pyproject.toml`.

## Engineer sessions

| Session | Queue (in order) | Now | Done this push |
|---|---|---|---|
| `rust-core` | [#41](https://github.com/sharmaniraj009/opencap/issues/41) [#49](https://github.com/sharmaniraj009/opencap/issues/49) [#57](https://github.com/sharmaniraj009/opencap/issues/57) [#56](https://github.com/sharmaniraj009/opencap/issues/56) [#55](https://github.com/sharmaniraj009/opencap/issues/55) | [#41](https://github.com/sharmaniraj009/opencap/issues/41) tag store + bus | — |
| `rust-drivers` | [#67](https://github.com/sharmaniraj009/opencap/issues/67) [#47](https://github.com/sharmaniraj009/opencap/issues/47) [#44](https://github.com/sharmaniraj009/opencap/issues/44) [#45](https://github.com/sharmaniraj009/opencap/issues/45) [#43](https://github.com/sharmaniraj009/opencap/issues/43) [#48](https://github.com/sharmaniraj009/opencap/issues/48) [#53](https://github.com/sharmaniraj009/opencap/issues/53) [#54](https://github.com/sharmaniraj009/opencap/issues/54) | [#47](https://github.com/sharmaniraj009/opencap/issues/47) Modbus RTU port | [#67](https://github.com/sharmaniraj009/opencap/issues/67) |
| `rust-north` | [#42](https://github.com/sharmaniraj009/opencap/issues/42) [#51](https://github.com/sharmaniraj009/opencap/issues/51) [#52](https://github.com/sharmaniraj009/opencap/issues/52) [#50](https://github.com/sharmaniraj009/opencap/issues/50) [#46](https://github.com/sharmaniraj009/opencap/issues/46) | PR [#116](https://github.com/sharmaniraj009/opencap/issues/116) ([#42](https://github.com/sharmaniraj009/opencap/issues/42)); preparing [#51](https://github.com/sharmaniraj009/opencap/issues/51) codec | — |
| `conformance` | [#3](https://github.com/sharmaniraj009/opencap/issues/3) [#5](https://github.com/sharmaniraj009/opencap/issues/5) [#30](https://github.com/sharmaniraj009/opencap/issues/30) [#7](https://github.com/sharmaniraj009/opencap/issues/7) | [#5](https://github.com/sharmaniraj009/opencap/issues/5) AB vectors | [#3](https://github.com/sharmaniraj009/opencap/issues/3) |
| `opcua-scada` | [#29](https://github.com/sharmaniraj009/opencap/issues/29) [#28](https://github.com/sharmaniraj009/opencap/issues/28) [#17](https://github.com/sharmaniraj009/opencap/issues/17) [#8](https://github.com/sharmaniraj009/opencap/issues/8) | [#29](https://github.com/sharmaniraj009/opencap/issues/29) L1 SCADA suite | — |
| `ui` | [#31](https://github.com/sharmaniraj009/opencap/issues/31) [#89](https://github.com/sharmaniraj009/opencap/issues/89) [#33](https://github.com/sharmaniraj009/opencap/issues/33) [#26](https://github.com/sharmaniraj009/opencap/issues/26) | [#31](https://github.com/sharmaniraj009/opencap/issues/31) Quick Client | — |
| `py-features` | [#12](https://github.com/sharmaniraj009/opencap/issues/12) [#10](https://github.com/sharmaniraj009/opencap/issues/10) [#36](https://github.com/sharmaniraj009/opencap/issues/36) [#11](https://github.com/sharmaniraj009/opencap/issues/11) [#37](https://github.com/sharmaniraj009/opencap/issues/37) [#68](https://github.com/sharmaniraj009/opencap/issues/68) | PR [#131](https://github.com/sharmaniraj009/opencap/issues/131) ([#12](https://github.com/sharmaniraj009/opencap/issues/12)); [#10](https://github.com/sharmaniraj009/opencap/issues/10) CSV import/export | — |
| `infra` | [#38](https://github.com/sharmaniraj009/opencap/issues/38) [#79](https://github.com/sharmaniraj009/opencap/issues/79) [#90](https://github.com/sharmaniraj009/opencap/issues/90) [#35](https://github.com/sharmaniraj009/opencap/issues/35) [#34](https://github.com/sharmaniraj009/opencap/issues/34) [#9](https://github.com/sharmaniraj009/opencap/issues/9) [#58](https://github.com/sharmaniraj009/opencap/issues/58) | PR [#147](https://github.com/sharmaniraj009/opencap/issues/147) ([#79](https://github.com/sharmaniraj009/opencap/issues/79)); [#90](https://github.com/sharmaniraj009/opencap/issues/90) desktop licence gate | [#38](https://github.com/sharmaniraj009/opencap/issues/38) |
| `specs` | [#22](https://github.com/sharmaniraj009/opencap/issues/22) [#23](https://github.com/sharmaniraj009/opencap/issues/23) [#19](https://github.com/sharmaniraj009/opencap/issues/19) [#21](https://github.com/sharmaniraj009/opencap/issues/21) [#24](https://github.com/sharmaniraj009/opencap/issues/24) [#25](https://github.com/sharmaniraj009/opencap/issues/25) [#20](https://github.com/sharmaniraj009/opencap/issues/20) [#18](https://github.com/sharmaniraj009/opencap/issues/18) | PR [#146](https://github.com/sharmaniraj009/opencap/issues/146) ([#22](https://github.com/sharmaniraj009/opencap/issues/22)); [#23](https://github.com/sharmaniraj009/opencap/issues/23) derived tags spec | — |
| `spikes` | [#13](https://github.com/sharmaniraj009/opencap/issues/13) [#14](https://github.com/sharmaniraj009/opencap/issues/14) [#15](https://github.com/sharmaniraj009/opencap/issues/15) [#16](https://github.com/sharmaniraj009/opencap/issues/16) | [#13](https://github.com/sharmaniraj009/opencap/issues/13) FINS simulator | — |

**Not in any queue:** [#40](https://github.com/sharmaniraj009/opencap/issues/40) (P1, Rust conformance harness, now `ready`) and [#80](https://github.com/sharmaniraj009/opencap/issues/80) (P3, replace chrono). The first engine-rust session with a free slot takes [#40](https://github.com/sharmaniraj009/opencap/issues/40) (`rust-drivers` after [#47](https://github.com/sharmaniraj009/opencap/issues/47) is the natural owner, since every port reuses the harness); [#80](https://github.com/sharmaniraj009/opencap/issues/80) goes to `rust-core` or `infra`, whichever frees first. If neither is claimed by pass 13, the lead takes them.

## Open PRs

| PR | Issue | Session | State |
|---|---|---|---|
| [#116](https://github.com/sharmaniraj009/opencap/issues/116) | [#42](https://github.com/sharmaniraj009/opencap/issues/42) dual-engine acceptance | `rust-north` | changes requested: Phase 3 step 2 waits on a fixed sleep; wait on the condition instead |
| [#131](https://github.com/sharmaniraj009/opencap/issues/131) | [#12](https://github.com/sharmaniraj009/opencap/issues/12) simulator driver | `py-features` | changes requested: `no_gc` on the 50-device cadence check; cross-link the two vector READMEs |
| [#146](https://github.com/sharmaniraj009/opencap/issues/146) | [#22](https://github.com/sharmaniraj009/opencap/issues/22) system-tags spec | `specs` | approved; merges when CI is green |
| [#147](https://github.com/sharmaniraj009/opencap/issues/147) | [#79](https://github.com/sharmaniraj009/opencap/issues/79) pin actions by SHA | `infra` | in review (after [#116](https://github.com/sharmaniraj009/opencap/issues/116): both edit `.github/workflows/`) |

## Decision-log numbers (`docs/decisions.md`)

D-066 is claimed by both [#116](https://github.com/sharmaniraj009/opencap/issues/116) and [#131](https://github.com/sharmaniraj009/opencap/issues/131): the lead renumbers whichever merges second to **D-067** at merge time. **Next free: D-068.** Before adding an entry, check `docs/decisions.md` on `main` and this line.

## Merge order (the owner's, plus conflict notes)

| First | Then | State |
|---|---|---|
| [#67](https://github.com/sharmaniraj009/opencap/issues/67) | [#44](https://github.com/sharmaniraj009/opencap/issues/44), [#45](https://github.com/sharmaniraj009/opencap/issues/45), [#47](https://github.com/sharmaniraj009/opencap/issues/47) | [#67](https://github.com/sharmaniraj009/opencap/issues/67) merged |
| [#30](https://github.com/sharmaniraj009/opencap/issues/30) | [#28](https://github.com/sharmaniraj009/opencap/issues/28) | — |
| [#3](https://github.com/sharmaniraj009/opencap/issues/3) | [#40](https://github.com/sharmaniraj009/opencap/issues/40) | [#3](https://github.com/sharmaniraj009/opencap/issues/3) merged; [#40](https://github.com/sharmaniraj009/opencap/issues/40) `ready` |
| [#5](https://github.com/sharmaniraj009/opencap/issues/5) | [#43](https://github.com/sharmaniraj009/opencap/issues/43), then [#43](https://github.com/sharmaniraj009/opencap/issues/43) before [#48](https://github.com/sharmaniraj009/opencap/issues/48) | — |
| [#41](https://github.com/sharmaniraj009/opencap/issues/41) | [#46](https://github.com/sharmaniraj009/opencap/issues/46), [#50](https://github.com/sharmaniraj009/opencap/issues/50), [#51](https://github.com/sharmaniraj009/opencap/issues/51), [#52](https://github.com/sharmaniraj009/opencap/issues/52) | — ([#51](https://github.com/sharmaniraj009/opencap/issues/51): a "Part of [#51](https://github.com/sharmaniraj009/opencap/issues/51)" codec-only PR may land earlier) |
| [#22](https://github.com/sharmaniraj009/opencap/issues/22) | [#56](https://github.com/sharmaniraj009/opencap/issues/56) | [#146](https://github.com/sharmaniraj009/opencap/issues/146) in review ([#56](https://github.com/sharmaniraj009/opencap/issues/56) also waits on [#41](https://github.com/sharmaniraj009/opencap/issues/41)) |
| [#23](https://github.com/sharmaniraj009/opencap/issues/23) | [#55](https://github.com/sharmaniraj009/opencap/issues/55) | — ([#55](https://github.com/sharmaniraj009/opencap/issues/55) also waits on [#41](https://github.com/sharmaniraj009/opencap/issues/41)) |
| [#13](https://github.com/sharmaniraj009/opencap/issues/13) | [#54](https://github.com/sharmaniraj009/opencap/issues/54) | — |
| [#29](https://github.com/sharmaniraj009/opencap/issues/29) | [#50](https://github.com/sharmaniraj009/opencap/issues/50) | — |
| [#47](https://github.com/sharmaniraj009/opencap/issues/47) and [#48](https://github.com/sharmaniraj009/opencap/issues/48) | [#53](https://github.com/sharmaniraj009/opencap/issues/53) | — |

Conflict notes: [#116](https://github.com/sharmaniraj009/opencap/issues/116) and [#79](https://github.com/sharmaniraj009/opencap/issues/79) both edit `.github/workflows/` (one at a time); [#41](https://github.com/sharmaniraj009/opencap/issues/41) and the driver ports both touch `opencap-core` (the second rebases).

## Blocked (and on what)

| Waiting on | Original issues | Follow-ups |
|---|---|---|
| [#41](https://github.com/sharmaniraj009/opencap/issues/41) tag store + bus | [#46](https://github.com/sharmaniraj009/opencap/issues/46), [#50](https://github.com/sharmaniraj009/opencap/issues/50) (+[#29](https://github.com/sharmaniraj009/opencap/issues/29)), [#51](https://github.com/sharmaniraj009/opencap/issues/51) (runtime), [#52](https://github.com/sharmaniraj009/opencap/issues/52), [#55](https://github.com/sharmaniraj009/opencap/issues/55) (+[#23](https://github.com/sharmaniraj009/opencap/issues/23)), [#56](https://github.com/sharmaniraj009/opencap/issues/56) (+[#22](https://github.com/sharmaniraj009/opencap/issues/22)) | — |
| [#5](https://github.com/sharmaniraj009/opencap/issues/5) AB vectors | [#43](https://github.com/sharmaniraj009/opencap/issues/43) → [#48](https://github.com/sharmaniraj009/opencap/issues/48) → [#53](https://github.com/sharmaniraj009/opencap/issues/53) (+[#47](https://github.com/sharmaniraj009/opencap/issues/47)) | — |
| [#13](https://github.com/sharmaniraj009/opencap/issues/13) FINS simulator | [#54](https://github.com/sharmaniraj009/opencap/issues/54) | — |
| [#12](https://github.com/sharmaniraj009/opencap/issues/12) simulator driver | — | [#124](https://github.com/sharmaniraj009/opencap/issues/124) |
| [#22](https://github.com/sharmaniraj009/opencap/issues/22) system-tags spec / [#120](https://github.com/sharmaniraj009/opencap/issues/120) | — | [#122](https://github.com/sharmaniraj009/opencap/issues/122), [#125](https://github.com/sharmaniraj009/opencap/issues/125), [#127](https://github.com/sharmaniraj009/opencap/issues/127) |
| [#28](https://github.com/sharmaniraj009/opencap/issues/28) OPC UA hardening | — | [#118](https://github.com/sharmaniraj009/opencap/issues/118), [#119](https://github.com/sharmaniraj009/opencap/issues/119), [#137](https://github.com/sharmaniraj009/opencap/issues/137) (+[#136](https://github.com/sharmaniraj009/opencap/issues/136)) |
| [#37](https://github.com/sharmaniraj009/opencap/issues/37) project file v2 | — | [#132](https://github.com/sharmaniraj009/opencap/issues/132) |
| [#42](https://github.com/sharmaniraj009/opencap/issues/42), [#89](https://github.com/sharmaniraj009/opencap/issues/89) | — | [#140](https://github.com/sharmaniraj009/opencap/issues/140) → [#141](https://github.com/sharmaniraj009/opencap/issues/141) |
| [#129](https://github.com/sharmaniraj009/opencap/issues/129) | — | [#139](https://github.com/sharmaniraj009/opencap/issues/139) |
| [#18](https://github.com/sharmaniraj009/opencap/issues/18), [#24](https://github.com/sharmaniraj009/opencap/issues/24) | — | [#145](https://github.com/sharmaniraj009/opencap/issues/145) |
| Tauri leaving gtk-rs 0.18 | — | [#102](https://github.com/sharmaniraj009/opencap/issues/102) |

## Ready, unclaimed (original issues, by area)

| Area | Issues |
|---|---|
| engine-rust | [#40](https://github.com/sharmaniraj009/opencap/issues/40) P1 · [#44](https://github.com/sharmaniraj009/opencap/issues/44) P2 · [#45](https://github.com/sharmaniraj009/opencap/issues/45) P2 · [#49](https://github.com/sharmaniraj009/opencap/issues/49) P2 · [#57](https://github.com/sharmaniraj009/opencap/issues/57) P2 · [#80](https://github.com/sharmaniraj009/opencap/issues/80) P3 |
| core / north / driver | [#30](https://github.com/sharmaniraj009/opencap/issues/30) P2 · [#7](https://github.com/sharmaniraj009/opencap/issues/7) P2 · [#8](https://github.com/sharmaniraj009/opencap/issues/8) P2 · [#11](https://github.com/sharmaniraj009/opencap/issues/11) P2 · [#14](https://github.com/sharmaniraj009/opencap/issues/14) P2 · [#36](https://github.com/sharmaniraj009/opencap/issues/36) P2 · [#37](https://github.com/sharmaniraj009/opencap/issues/37) P2 · [#68](https://github.com/sharmaniraj009/opencap/issues/68) P2 · [#15](https://github.com/sharmaniraj009/opencap/issues/15) P3 · [#16](https://github.com/sharmaniraj009/opencap/issues/16) P3 |
| scada / docs | [#28](https://github.com/sharmaniraj009/opencap/issues/28) P1 · [#17](https://github.com/sharmaniraj009/opencap/issues/17) P2 · [#18](https://github.com/sharmaniraj009/opencap/issues/18) P2 · [#19](https://github.com/sharmaniraj009/opencap/issues/19) P2 · [#21](https://github.com/sharmaniraj009/opencap/issues/21) P2 · [#26](https://github.com/sharmaniraj009/opencap/issues/26) P2 · [#20](https://github.com/sharmaniraj009/opencap/issues/20) P3 · [#24](https://github.com/sharmaniraj009/opencap/issues/24) P3 · [#25](https://github.com/sharmaniraj009/opencap/issues/25) P3 |
| ui / desktop / infra | [#89](https://github.com/sharmaniraj009/opencap/issues/89) P2 · [#33](https://github.com/sharmaniraj009/opencap/issues/33) P2 · [#90](https://github.com/sharmaniraj009/opencap/issues/90) P2 · [#35](https://github.com/sharmaniraj009/opencap/issues/35) P2 · [#34](https://github.com/sharmaniraj009/opencap/issues/34) P2 · [#9](https://github.com/sharmaniraj009/opencap/issues/9) P2 · [#58](https://github.com/sharmaniraj009/opencap/issues/58) P3 |

Follow-ups [#103](https://github.com/sharmaniraj009/opencap/issues/103)–[#145](https://github.com/sharmaniraj009/opencap/issues/145) and the system-tag issues are `ready` or `blocked` as labelled; take them after your own queue.

## Decisions for the owner

Three new questions this pass (12–15). The lead continues with everything else meanwhile; reply on this issue.

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
11. **Require CI on `main`** (GitHub → Settings → Branches → a rule for `main`: require the `ci`, `rust-engine` and `licenses` checks). It is M1's last exit criterion, and the lead's tools cannot change repository settings.
12. **New: delete leftover branches.** 17 remote branches belong to merged PRs (repository housekeeping; the leads leave deletion to you).
13. **New: modems** ([#109](https://github.com/sharmaniraj009/opencap/issues/109)): does any site still dial PLCs over a modem? If not, the lead closes the modem/phonebook/auto-dial rows as out of scope.
14. **New: KEPServerEX Configuration API** ([#141](https://github.com/sharmaniraj009/opencap/issues/141)): does any site run scripts against KEPServerEX's REST configuration API? That decides whether opencap offers a compatibility subset.
15. **New: OPC AE** ([#145](https://github.com/sharmaniraj009/opencap/issues/145)): is OPC Alarms & Events (classic) needed at all, or is OPC UA A&C enough?

## Backlog (next waves, not yet filed)

- Drivers (Rust): EtherNet/IP generic CIP explicit (after [#43](https://github.com/sharmaniraj009/opencap/issues/43)), Beckhoff ADS, GE SRTP, SNMP, MQTT client, ODBC client, OPC DA client; DNP3, IEC 104, BACnet/IP (after spikes [#14](https://github.com/sharmaniraj009/opencap/issues/14)–[#16](https://github.com/sharmaniraj009/opencap/issues/16)).
- Processing: alarms (after [#24](https://github.com/sharmaniraj009/opencap/issues/24)), data logger (after [#25](https://github.com/sharmaniraj009/opencap/issues/25)), redundancy (after [#20](https://github.com/sharmaniraj009/opencap/issues/20)), users/roles (after [#21](https://github.com/sharmaniraj009/opencap/issues/21)).
- Core: Rust KEPServerEX importer; re-import three-way merge (ADR-0013); per-channel concurrency; dynamic tags ([#118](https://github.com/sharmaniraj009/opencap/issues/118)); scheduler.
- North: REST client/server agents; OPC DA server bridge (after [#18](https://github.com/sharmaniraj009/opencap/issues/18) + owner decision).
