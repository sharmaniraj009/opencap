# DNP3 spike: library choice, simulator plan, master and outstation design

Spike for [#14](https://github.com/sharmaniraj009/opencap/issues/14), dated 2026-11-25. The
decision it supports is [ADR-0015](../adr/0015-dnp3-native-rust-subset.md).

opencap needs DNP3 (IEEE 1815) in two places:

- **as a master driver**, polling RTUs and IEDs (FR-1, KEPServerEX "DNP3 Client");
- **as an outstation northbound**, serving the tag store to utility SCADA such as Geo SCADA and
  VTScada ([`scada-compatibility.md`](../architecture/scada-compatibility.md)).

**Recommendation:** implement the read subset natively in Rust: one codec crate, a master driver
and an outstation north plugin. Test against **opendnp3**, via the maintained `yadnp3` fork
(Apache-2.0), and against **dnp3py** (MIT) in CI. Do not use Step Function I/O's `dnp3` crate in
any role, not even as a test peer. No GPL component is needed, so no owner decision is required.

A throwaway prototype under [`spikes/dnp3/`](../../spikes/dnp3/) proves that the subset is
small. It is a pure-Python master, about 400 lines, and it reads static data, events with device
timestamps and flags from both independent outstations (§7).

## 1. Licence verification (ADR-0007)

Every licence was read from the project's licence file at the tag or commit named, on 2026-11-18.
Versions and dates come from the git tags, crates.io and PyPI.

| Candidate | Licence (pinned source) | Latest | Status | ADR-0007 class |
|---|---|---|---|---|
| **opendnp3** (C++; Java and .NET bindings) | Apache-2.0 ([LICENSE@3.1.2](https://github.com/dnp3/opendnp3/blob/3.1.2/LICENSE)) | 3.1.2, 2022-04-22 | **Archived 2022-09-01** with an end-of-life notice. It passed automated Subset Level 2 conformance ([report](https://dnp3.github.io/conformance/report.html)) | **Test-only** (through yadnp3). Allowed as a design reference. Not adopted: EOL C++ with no C ABI |
| **yadnp3** (opendnp3 fork, pybind11 bindings; import name `opendnp3`) | Apache-2.0 ([LICENSE@v3.2.1.5](https://github.com/f0rw4rd/opendnp3/blob/v3.2.1.5/LICENSE); wheel metadata `License-Expression: Apache-2.0`) | 3.2.1.5, 2026-11-01 | Active and self-described as "experimental". Wheels for cp312 manylinux_2_28 x86_64 and win_amd64. The wheel bundles OpenSSL 1.1.1k (EOL; only the TLS transport uses it) | **Test-only** (CI peer: master and outstation) |
| **dnp3py** (pure Python; import name `dnp3`) | MIT ([LICENSE@v0.6.0](https://github.com/craigpnnl/dnp3py/blob/v0.6.0/LICENSE)) | 0.6.0, 2026-09-29 | Active (Beta). Master and outstation; targets the Subset Level 2 object set; `py3-none-any` wheel | **Test-only** (second, unrelated CI peer) |
| **Step Function I/O `dnp3`** (Rust, plus C/C++/.NET/Java bindings) | **Custom non-commercial / non-production licence** ([LICENSE.txt@1.8.0](https://github.com/stepfunc/dnp3/blob/1.8.0/LICENSE.txt); crates.io shows "non-standard" for every version, and the same kind of licence ships at [0.9.0](https://github.com/stepfunc/dnp3/blob/0.9.0/LICENSE.txt) and [1.7.0](https://github.com/stepfunc/dnp3/blob/1.7.0/LICENSE.txt)) | 1.8.0, 2026-10-29 | Active; claims Subset Level 3 | **Avoid** in every role. §2.6 of the 1.8.0 licence also forbids using it "as a reference, oracle, test peer, or source of expected results" and forbids deriving behaviour or design from it. **Engineers must not read its source for opencap work** |
| rusty-dnp3 (`-master`, `-outstation`, `-serde`) | Apache-2.0 in the repo ([LICENSE@492db97](https://github.com/gazzasaur/rusty-dnp3/blob/492db97e02b63e4e73c95154d37b2ce69a7e0757/LICENSE)); crates.io says "MIT OR Apache-2.0", which does not match | 0.0.1, 2026-07-10 | 9 commits, early stage | Permissive but immature: not adopted |
| industrial-protocols-dnp3 | MIT ([LICENSE@v2.0.0](https://github.com/erikwang2013/industrial-protocols-rust/blob/v2.0.0/LICENSE)) | 2.0.0, 2026-11-07 | One crate of a 38-protocol set; 10 commits | Not adopted (immature) |
| tpt-protocol-dnp3 | MIT OR Apache-2.0 ([LICENSE-MIT@v0.1.0](https://github.com/tpt-solutions/tpt-protocol/blob/v0.1.0/LICENSE-MIT)) | 0.1.0, 2026-11-10 | Parser only | Not adopted |
| dnp3-python (VOLTTRON), pydnp3 (Kisensum) | Apache-2.0 ([7d0d314](https://github.com/VOLTTRON/dnp3-python/blob/7d0d314cc533d39096fc8f0c39e2e36c7fd35315/LICENSE), [0.1](https://github.com/Kisensum/pydnp3/blob/0.1/LICENSE)) | 2024 / 2018 | No cp312 wheels; dormant | Not usable |
| go-dnp3 (otfabric), cB-Sigma/dnp3 (Go) | MIT ([v1.15.0](https://github.com/otfabric/go-dnp3/blob/v1.15.0/LICENSE)), Apache-2.0 ([v0.3.2](https://github.com/cB-Sigma/dnp3/blob/v0.3.2/LICENSE)) | 2026-11 | Both about a month old | Possible third interop peer later |
| jdnp3 (Java) | Apache-2.0 ([LICENSE.txt@0.12.11](https://github.com/gazzasaur/jdnp3/blob/0.12.11/LICENSE.txt)) | 0.12.11, 2026-02 | Outstation and a web simulator | Not needed |
| nfm-dnp3, dnp3-outstation (PyPI) | PyPI says MIT, but **no licence file** exists in the repo or wheel | 2026 | Small subsets | **Avoid** (unknown licence) |
| dnp3protocol / FreyrSCADA, Triangle MicroWorks, DNP3bean | proprietary or commercial binaries | — | — | **Avoid** |
| dnp3time, casket (Rust) | AGPL-3.0-only; "non-standard" | — | security tooling, not stacks | **Avoid** |

There is no free public copy of the conformance procedures: the DNP Users Group "IED
Certification Procedures" are members-only, and IEEE 1815-2012, whose subset definitions are in
clause 14.4 / Table 14-3, is a paid standard. Subset Level 2 is the target; §3 lists it by
object, so engineers work from the object list, not from a copied text.

## 2. Options

| Option | What it takes | Effort (PRs) | Risks |
|---|---|---|---|
| **A. Native Rust read subset** (recommended) | a codec crate (link, transport, application, the §3 objects), a master driver and an outstation north plugin, all on tokio | **5** (§8, follow-ups 1, 3, 4, 5, 6) plus the harness (2) | Subset Level 2 needs care with multi-fragment responses, confirm and unsolicited state, and event ordering. It is mitigated by testing against two independent peers and capturing vectors from opendnp3 |
| B. FFI to opendnp3 | a C shim over the C++ API (opendnp3 3.x has no C ABI), a cmake build from `build.rs`, Windows and cross builds, the ASIO threading model bridged to tokio | 4, plus ongoing maintenance of an **end-of-life** C++ code base whose security fixes would fall to us | Apache-2.0 allows static linking, so licence is not the problem; maintenance is. A crash in C++ takes the engine down (ADR-0004 §3 says to isolate crash-prone C code) |
| C. Isolated plugin (opendnp3 or yadnp3 behind `opencap.plugin.v1`) | the Python driver host (#49) running yadnp3, or a C++ plugin process | 2 once #49 exists | It adds a process and a Python runtime, and it rests on an "experimental" fork with a bundled EOL OpenSSL. The outstation north would need a north-plugin protocol, which does not exist yet. Acceptable only as a stop-gap |
| D. Adopt a young permissive Rust crate | rusty-dnp3, industrial-protocols-dnp3 | 3 | Pre-alpha, with one author each and no conformance evidence. We would end up maintaining a fork |
| E. Step Function I/O `dnp3` | — | — | Excluded by licence (non-commercial, and no use as a test peer) |

The prototype (§7) supports option A. A master that reads static data and events, confirms
events and maps flags fits in about 400 lines of Python. Most of the Rust cost is in hardening
against the two peers and in the outstation's event buffer, not in framing.

## 3. Needed subset

### 3.1 Master (driver)

**Transports:** TCP client first; serial and UDP in a later PR (KEPServerEX ships a DNP3 Client
for Ethernet and one for serial lines). Link layer: unconfirmed user data, link status (keep-alive), and no link-layer
confirms by default.

**Polls:**
- integrity poll: class 1, 2, 3 then class 0, in one READ (`3C02 06 3C03 06 3C04 06 3C01 06`), at
  start-up, on reconnect, when IIN1.7 DEVICE_RESTART or IIN2.3 EVENT_BUFFER_OVERFLOW is set, and
  at the device's integrity rate;
- event polls: class 1/2/3 at the scan rate;
- optional explicit-variation reads, such as `g30v5` for floats. opendnp3's default static
  analog variation is the 32-bit integer `g30v1`, so a master that wants decimals must ask for
  them (§7).

**Unsolicited responses:** the driver confirms them (application CONFIRM with the UNS bit) and
feeds their points into the push path (§4). By default it does not send ENABLE_UNSOLICITED; see
§6 Q1.

**Objects (read):**

| Data | Static | Event |
|---|---|---|
| binary input | g1v1 (packed), g1v2 | g2v1, g2v2, g2v3 (with CTO g51v1/v2) |
| double-bit binary | g3v2 | g4v1, g4v2, g4v3 |
| binary output status | g10v2 | g11v1, g11v2 |
| counter | g20v1, g20v2, g20v5, g20v6 | g22v1, g22v2, g22v5, g22v6 |
| frozen counter | g21v1, g21v2, g21v5, g21v6, g21v9, g21v10 | g23v1, g23v2, g23v5, g23v6 |
| analog input | g30v1–g30v6 | g32v1–g32v8 |
| analog output status | g40v1–g40v4 | g42v1–g42v8 |
| other | g50v4 (time and interval), g110/g111 (octet strings), g80v1 (IIN) | |

Qualifiers 0x00, 0x01, 0x06, 0x07, 0x08, 0x17 and 0x28; multi-fragment responses
(FIR/FIN/CON/SEQ).

**Time sync:** off. The driver sends no WRITE g50 and no DELAY_MEASURE (§5.2 covers the
consequences).

**Read-only framing:** the driver can frame only these application function codes:
- `0x00` CONFIRM;
- `0x01` READ;
- `0x14`/`0x15` ENABLE/DISABLE_UNSOLICITED, only if §6 Q1 is approved.

WRITE, SELECT, OPERATE, DIRECT_OPERATE(_NR), the FREEZE family, COLD/WARM_RESTART,
INITIALIZE_DATA, START/STOP_APPL, SAVE_CONFIG, ASSIGN_CLASS and the file functions cannot be
framed. A unit test pins this (the prototype's `FC_ALLOWED`). A consequence is that the driver
does not clear IIN1.7 DEVICE_RESTART, because clearing it takes a WRITE of g80v1 index 7; see
§6 Q2.

### 3.2 Outstation (north)

**Transport:** a TCP server, one master per listener. A new connection replaces the old one,
as opendnp3's `CloseExisting` accept mode does. Optionally a second listener later.

**Static data from the tag store:** each mapped tag gets a point type and index (config: point
type, index, class 0/1/2/3, deadband, static and event variation). Defaults: g1v2, g30v5,
g20v1, g10v2, g40v3; events g2v2, g32v7, g22v5 with time.

**Events:** generated from the change-event bus (ADR-0005). An event is generated on a value
change beyond the deadband or on a flag change, with `source_ts` as the event time. The event
buffer is bounded per class. On overflow the outstation sets IIN2.3 EVENT_BUFFER_OVERFLOW and
the oldest events are dropped.

**Class assignment** comes from the config (no ASSIGN_CLASS from the master). Unsolicited
reporting is off by default and on by config, with confirm timeout and retries.

**Read-only:**
- CROB (g12) and analog output (g41) requests with SELECT, OPERATE or DIRECT_OPERATE are
  answered per object with status `NOT_SUPPORTED` (4), and nothing reaches the tag store or a
  PLC.
- WRITE (other than g80v1 index 7, which clears our own restart IIN), FREEZE, COLD/WARM_RESTART
  and file services are answered with IIN2.0 NO_FUNC_CODE_SUPPORT.
- A test pins this against both peer masters.

**IIN:** the outstation keeps IIN bits for its own state:
- IIN1.7 is set at start-up;
- IIN1.4 NEED_TIME stays clear, because opencap uses its own clock;
- IIN1.1–1.3 report class events available;
- IIN2.3 reports an event buffer overflow.

## 4. Fit with the driver contract

- The master is a **hybrid push driver**
  ([03 §3.3](../architecture/03-deep-dive.md#33-driver-contract)):
  - integrity polls are ordinary `read()`s;
  - event polls and unsolicited responses deliver batches through the push path.
  - Until the Rust `subscribe(blocks, sink)` lands (#46), the driver keeps the latest event per
    point, like the Python OPC UA client. That coalesces events between two reads.
  - SOE (sequence of events) fidelity therefore needs the subscribe path, and the follow-up
    master issue depends on it.
- **Planning:** a tag's address is a point (type + index); a "block" is one poll class. `plan()`
  groups tags per device. There is no address-range merging: DNP3 returns the whole class.
- **Errors:**
  - timeouts and TCP failures raise `CommError`/`ConnectionLost`;
  - an object the codec cannot parse makes the fragment undecodable, so it is a `CommError`
    with the object named, logged once;
  - a point the outstation never reports gives `bad_config` after an integrity poll;
  - IIN2.1 OBJECT_UNKNOWN or IIN2.2 PARAMETER_ERROR on an explicit read gives `BlockConfigError`.

## 5. Quality and timestamps (ADR-0006)

### 5.1 DNP3 flags → opencap quality (master)

Flag bits:
- bits 0–4 are common to all point types: ONLINE, RESTART, COMM_LOST, REMOTE_FORCED,
  LOCAL_FORCED;
- bit 5 is CHATTER_FILTER (binary), OVER_RANGE (analog) or ROLLOVER (counter);
- bit 6 is REFERENCE_ERR (analog) or DISCONTINUITY (counter).

| Flags | opencap quality | Why |
|---|---|---|
| COMM_LOST | `bad_comm` | the outstation lost its own source; the value is the last one it had |
| RESTART (not updated since the outstation restarted) | `stale` | the "never read" case: OPC UA `Bad_WaitingForInitialData` |
| ONLINE clear (point off-line) | `stale` | the outstation says the value is not current |
| analog OVER_RANGE or REFERENCE_ERR | `bad_decode` | the number is not a trustworthy measurement |
| REMOTE/LOCAL_FORCED, CHATTER_FILTER, counter ROLLOVER/DISCONTINUITY | `good` | the value is the current one; the flag is reported, not hidden (§6 Q3) |
| ONLINE only | `good` | |

Priority goes top to bottom (COMM_LOST wins over RESTART). The prototype's `quality_of()` is
this table, pinned by `test_flag_mapping`.

### 5.2 Timestamps (master)

- **Events with absolute time** (g2v2, g4v2, g11v2, g22v5/6, g23v5/6, g32v3/4/7/8, g42v3/4/7/8):
  `source_ts` = the device time, a 48-bit millisecond count since 1970-01-01 UTC.
- **Events with relative time** (g2v3, g4v3): `source_ts` = CTO (g51v1 synchronised, g51v2
  unsynchronised) + offset.
- **Static objects and events without time:** `source_ts` = reply arrival (ADR-0006).
- **Unsynchronised device clocks:** because the master does not time-sync, the device's clock
  may be wrong. A device setting `device_time: auto | always | never`, default `auto`, uses the
  device time unless the outstation reports IIN1.4 NEED_TIME or the CTO is g51v2. In those cases
  it uses the arrival time and counts `device_time_rejected` in diagnostics.

### 5.3 opencap quality → DNP3 flags (outstation)

| opencap | Flags sent | Value sent |
|---|---|---|
| `good` | ONLINE | current |
| `bad_comm` | COMM_LOST (ONLINE clear) | last value |
| `stale` with a value | COMM_LOST (ONLINE clear) | last value |
| `stale`, never read | RESTART | 0 |
| `bad_config` | ONLINE clear | 0 |
| `bad_decode` | analog: ONLINE + REFERENCE_ERR; binary and counter: ONLINE clear | last value or 0 |

This mapping is lossy in the round trip. A value that arrives at an opencap master with
COMM_LOST becomes `bad_comm`, then leaves an opencap outstation as COMM_LOST again, but a
forced flag is lost. §6 Q3 proposes keeping the raw flags.

## 6. Questions for the lead

1. **ENABLE_UNSOLICITED from the master.** Masters commonly enable unsolicited reporting at
   start-up; check KEPServerEX's DNP3 Client default in follow-up 6. Sending function codes 0x14/0x15 changes which classes the outstation reports on its
   own; it changes no process data. Proposal: allow it behind a device setting
   (`unsolicited: false` by default) and list it in ADR-0008's notes as non-process traffic.
2. **IIN1.7 clear.** Clearing an outstation's restart indication is a WRITE (g80v1 index 7).
   Proposal: do not send it (ADR-0008), and treat IIN1.7 as "do an integrity poll" each time it
   is seen (rate-limited to one per integrity period). Some outstations keep sending events with
   the bit set; that is harmless.
3. **Flag pass-through.** ADR-0006's "Revisit when" names DNP3 flags. Proposal: a per-value
   `device_flags` attribute in the tag store (u16, protocol-tagged). The DNP3 outstation and IEC
   104 server would emit it unchanged when the source is DNP3 or IEC 104, and an OPC UA server
   could map it to a sub-status. This changes ADR-0006, so it is not in this spike's follow-ups.
4. **Second listener / multiple masters** on the outstation: needed by any site? Not in the
   first PR.

## 7. Prototype and test peers

`spikes/dnp3/proto_master.py` is a throwaway, never imported by `src/`. It contains:

- CRC-16/DNP;
- link frames with 16-byte CRC blocks;
- transport segmentation and reassembly;
- application READ and CONFIRM, including multi-fragment responses;
- the §3.1 object parser;
- the §5.1 flag mapping.

It was written from the frame layouts above. No other implementation's source was used, and
`stepfunc/dnp3` was never opened.

`spikes/dnp3/test_proto_master.py` runs it against two independent outstations:

```bash
pip install yadnp3==3.2.1.5 dnp3py==0.6.0 pytest pytest-asyncio
pytest spikes/dnp3 -q          # 7 passed (2026-11-25, Python 3.12)
```

| Test | Peer | Proves |
|---|---|---|
| `test_crc_check_value_and_frames` | — | CRC check value 0xEA82; integrity-poll APDU, transport and link bytes |
| `test_read_only_function_codes` | — | only READ and CONFIRM can be framed |
| `test_flag_mapping` | — | §5.1 |
| `test_integrity_poll_values_flags_and_quality` | opendnp3 | binary, analog and counter values; ONLINE, COMM_LOST and OVER_RANGE → quality; RESTART on never-updated points; IIN1.7; no command handler call; only READ/CONFIRM on the wire |
| `test_explicit_variation_read_gets_floats` | opendnp3 | the default g30v1 truncates 12.5; reading g30v5 returns 12.5 |
| `test_event_poll_timestamps_and_confirm` | opendnp3 | time-tagged events (g2v2, g32v7) with exact device timestamps; CON → CONFIRM; a confirmed buffer is empty on the next poll |
| `test_integrity_poll_against_dnp3py` | dnp3py | the same reads against an unrelated implementation |

The test is not collected by CI (`testpaths = ["tests"]`) because a spike is not product code.
The follow-ups move the peers into the `dev` extra.

### 7.1 Simulator plan (ADR-0004 §5, ADR-0014)

`tests/helpers/dnp3_sim.py` (follow-up 2) is an asyncio TCP outstation. Its parts:

- **Application layer and point database:** dnp3py's `Outstation.process_request()`, a pure
  function from request bytes to response fragments, under MIT.
- **Link and transport front end:** written by us, so that every fault switch lives in our code:
  - `mute`;
  - `drop_connection`;
  - `corrupt_crc`;
  - `split_frames` (a frame across TCP writes);
  - `delay`;
  - `unsolicited_burst`;
  - `restart()` (same port, IIN1.7 set again);
  - `event_overflow`;
  - per-point flags and timestamps.
- **Counters:** `requests`, `function_counts`, `control_attempts` (every SELECT, OPERATE or
  DIRECT_OPERATE, answered NOT_SUPPORTED), `writes`.

It is validated, unmodified, by **opendnp3's master** (yadnp3) and by **dnp3py's master**:
integrity, event and unsolicited flows, faults, and both peers reading the same values. The
outstation north plugin is accepted against the same two masters (SCADA matrix L1).

Wire **conformance vectors** (`tests/conformance/dnp3/`) are captured from opendnp3. A generator
script drives the yadnp3 outstation with known values and records its response fragments. Each
vector pairs the bytes with the inputs, so the expected decode comes from the inputs, not from
our parser. These are the decode vectors for the Rust codec.

## 8. Follow-up issues (one PR each)

| # | Title | Depends on | Acceptance (summary) |
|---|---|---|---|
| 1 | `engine-rust: DNP3 codec crate (link, transport, application, read-subset objects)` | — | `engine/crates/opencap-dnp3` (no I/O): CRC, frames, segments, fragments, the §3.1 objects and qualifiers; the framing API cannot produce a non-allowed function code (unit test); decode vectors (follow-up 2b) pass; fuzz target for the parser |
| 2 | `tests: DNP3 outstation simulator validated by opendnp3 and dnp3py masters` | — | `tests/helpers/dnp3_sim.py` as in §7.1; `yadnp3` and `dnp3py` in the `dev` extra and `[test_only]` licence list; `tests/unit/test_dnp3_sim.py` (both masters, every fault switch) |
| 2b | `tests: DNP3 decode vectors captured from opendnp3` | 2 | `tests/conformance/dnp3/*.json` + generator; one vector per object/variation/qualifier in §3.1, plus multi-fragment, CON and UNS cases |
| 3 | `engine-rust: DNP3 master driver over TCP (integrity and event polls, unsolicited, flags → quality)` | 1, 2, #41, #46 (subscribe path) | §3.1, §4, §5.1, §5.2; `OPENCAP_ENGINE=rust` simulator suite (mute, restart, overflow, unsolicited, flags); `docs/driver_params.md`; read-only test (simulator `control_attempts == 0`, `writes == 0`) |
| 4 | `north: DNP3 outstation over TCP from the tag store, read-only` | 1, #41 | §3.2 and §5.3; point-map config in both engines' contract; accepted by the opendnp3 and dnp3py masters (static, events with time, unsolicited, overflow); controls answered NOT_SUPPORTED and pinned by a test |
| 5 | `engine-rust: DNP3 master over serial and UDP` | 3, #47 (serial) | the same suite through the serial transport (PTY bridge) and UDP |
| 6 | `config: KEPServerEX "DNP3 Client" import mapping` | 3 | address syntax from PTC's DNP3 Client manual (verify; not covered by this spike), device and channel settings → `params`, import vectors |

Questions Q1–Q3 (§6) go to the lead, and Q3 needs an ADR-0006 amendment if accepted.
