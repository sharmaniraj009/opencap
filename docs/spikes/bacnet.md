# BACnet/IP spike: library and simulator choice, object model mapping

This is the spike for [#15](https://github.com/sharmaniraj009/opencap/issues/15) (2026-11-25).
It backs [ADR-0016](../adr/0016-bacnet-native-rust-client.md).

BACnet/IP is a tier-2 KEPServerEX driver for building automation. opencap needs it as a
**client driver**: a read-only BACnet client that discovers devices, reads properties in
batches and subscribes to changes of value (COV).

**Recommendation:**

- Implement the read subset natively in Rust: a codec crate and a driver crate.
- Build the test simulator on **bacpypes3**, a pure-Python BACnet stack whose pinned pyproject
  declares MIT. The lead should note that no licence text ships with it (§1).
- Use **rusty-bacnet** (MIT) as a second, independent peer, not as a dependency.
- Do not link bacnet-stack. Its core is GPL with a linking exception, and its client cannot
  reassemble segmented replies.

A throwaway prototype under [`spikes/bacnet/`](../../spikes/bacnet/) proves the subset fits
in about 500 lines of Python. It covers Who-Is/I-Am, ReadProperty, ReadPropertyMultiple,
segmented responses, COV, foreign-device registration through a BBMD, `browse()` and the
quality mapping, all against a bacpypes3 device (§7).

## 1. Licence verification (ADR-0007)

Each licence below was read from the licence file or declaration at the stated tag or commit
on 2026-11-18. The bacpypes3 entry was re-checked on 2026-11-25.

| Candidate | Licence (pinned source) | Latest | Status / scope | ADR-0007 class |
|---|---|---|---|---|
| **bacnet-stack** (C, Steve Karg) | **Mixed** ([license/readme.txt@1.7.0](https://github.com/bacnet-stack/bacnet-stack/blob/bacnet-stack-1.7.0/license/readme.txt), [GCC-exception-2.0@1.7.0](https://github.com/bacnet-stack/bacnet-stack/blob/bacnet-stack-1.7.0/license/GCC-exception-2.0)). SPDX headers in `src/bacnet` at that tag: 400 MIT, 91 `GPL-2.0-or-later WITH GCC-exception-2.0` (including `rp.c`, `rpm.c`, `cov.c`, `whois.c`, `npdu.c`, `bacdcode.c`, `bvlc.c`, `tsm.c`, `h_bbmd.c`, `address.c`), 11 Apache-2.0, 8 BSD, 6 CC-PDDC, and **2 plain `GPL-2.0-or-later`** with no exception (`src/bacnet/basic/ucix/ucix.[ch]`) | 1.7.0, 2026-11-04 | Active; Who-Is, RP, RPM, COV, BBMD/FD; many ports. **The client cannot reassemble segmented replies**: `tsm.c` says "not coded for segmentation", and the 1.5.0 CHANGELOG says there is "no support for segmentation in the TSM or APDU handlers" | **Avoid** in the default build. The linking exception lets the code be linked, but changes to those files stay GPL, the two `ucix` files must be excluded, and the segmentation gap is a functional blocker. Its demo apps may be used as a test-only tool |
| **rusty-bacnet** (`bacnet-client` and related crates; Python wheels) | MIT ([LICENSE@v0.15.0](https://github.com/jscott3201/rusty-bacnet/blob/v0.15.0/LICENSE)) | 0.15.0, 2026-10-29 | Very active, but one author, first release 2026-03, pre-1.0, MSRV 1.95 (the engine's is 1.85). Covers Who-Is, RP, RPM, WP, COV, segmentation, FD, BBMD, IPv6, SC and MS/TP, plus a server object DB; about 12k downloads | **Test-only** (second peer). A dependency later if it matures (§2 B) |
| rustbac (`rustbac-client`) | MIT OR Apache-2.0 ([LICENSE-MIT@v0.4.1](https://github.com/rbhans/rust-bac/blob/v0.4.1/LICENSE-MIT)) | 0.4.1, 2026-05-12 | No commits since 2026-05; claims segmentation both ways, COV and FD | Not adopted (stalled) |
| bacnet-rs | MIT OR Apache-2.0 ([LICENSE-MIT@0.3.2](https://github.com/bacnet-rs/bacnet-rs/blob/0.3.2/LICENSE-MIT)) | 0.3.2, 2026-09-02 | 91k downloads but "not yet production-ready"; **segmentation not implemented** | Not adopted |
| embedded-bacnet, bacnet-emb, mabi-bacnet | Apache-2.0; MIT/Apache; Apache-2.0 (crates.io metadata only) | 2024–2026 | codecs, `no_std` crates, a simulator | Not needed |
| bacnet, bacnet_parse (Rust) | MPL-2.0 | 2018/2019 | dead | Not used |
| **bacpypes3** (Python) | **MIT declared** (`license = "MIT"` in [pyproject.toml@v0.1.0](https://github.com/JoelBender/BACpypes3/blob/v0.1.0/pyproject.toml); wheel metadata `License-Expression: MIT`). **No licence file** at the tag, in the wheel or in the sdist, and the trove classifier says "BSD License" | 0.1.0, 2026-11-17 | Active ("Pre-Alpha" classifier); pure Python. Client: Who-Is, RP, RPM, COV, segmentation, foreign/BBMD/NAT. Local objects for every subset type | **Test-only.** It is not shipped or vendored, so the missing MIT notice imposes nothing on us, and the licence gate reads the declared MIT. **Lead to confirm** this is enough, or ask upstream for a licence file before the simulator PR (§6 Q1) |
| bacpypes 0.18/0.19 | MIT ([LICENSE@v0.18.6](https://github.com/JoelBender/bacpypes/blob/v0.18.6/LICENSE)) | 0.19.0, 2025-01 | **Fails to import on Python 3.12** (`asyncore` was removed) | Not usable |
| BAC0 | LGPL-3.0 ([LICENSE@v2026.07.25](https://github.com/ChristianTremblay/BAC0/blob/v2026.07.25/LICENSE)) | 2026.7.25 | Wrapper over bacpypes3 | Not needed (adds nothing over bacpypes3) |
| BACnet4J (Java) | GPL-3.0 or commercial ([license.txt@v7.0.0-alpha.6](https://github.com/MangoAutomation/BACnet4J/blob/v7.0.0-alpha.6/license.txt)) | 2026-10 | | **Avoid** |
| gobacnet, NubeDev/bacnet (Go) | GPL-2.0 | 2023/2024 | | **Avoid** |
| @bacnet-js/client (Node) | MIT ([LICENSE@v3.3.2](https://github.com/bacnet-js/client/blob/v3.3.2/LICENSE)) | 3.3.2 | Who-Is, RPM, SubscribeCOV | Possible third peer; not needed |
| YABE (C#), VTS | MIT per SourceForge; "Public Domain" per SourceForge (neither file verified) | | Windows GUI test tools | Manual commissioning aids only |

## 2. Options

| Option | Effort (PRs) | For | Against |
|---|---|---|---|
| **A. Native Rust read subset** (recommended) | **4** (§8, follow-ups 1, 3, 4, 5) plus the simulator (2) | One small codebase, no dependency risk, the same model as FINS, S7 and MC. The prototype shows the encodings are compact | We own segmentation and the COV renewal logic. Against two peers, that is manageable |
| B. Adopt rusty-bacnet | 3 | Most complete permissive stack | One maintainer, pre-1.0, MSRV 1.95 (an engine-wide bump), and churn. Revisit when it reaches 1.0 with more maintainers (ADR-0016) |
| C. bacnet-stack through FFI | 4, plus a C build on every target | Mature server side | GPL+exception terms on modified files, two GPL-only files to exclude, **no client segmentation**, and C code in-process |
| D. rustbac / bacnet-rs | 3 | Permissive | Stalled, or missing segmentation |

## 3. Needed subset

**Transport (BACnet/IP, Annex J)**
- UDP 47808 by default.
- BVLC Original-Unicast-NPDU (0x0A) and Original-Broadcast-NPDU (0x0B), Forwarded-NPDU (0x04)
  on receive, and BVLC-Result (0x00).
- **Foreign-device registration** (0x05, re-registered at TTL/2) and Distribute-Broadcast-To-Network
  (0x09), for devices on another subnet behind a BBMD.
- NPDU with DNET/SNET parsing for routed devices. The driver addresses a routed device by
  network number + MAC through the router's address.

**Services (all client side)**

| Service | Use | Notes |
|---|---|---|
| Who-Is / I-Am (unconfirmed) | discovery; binds device instance → address, max APDU, segmentation support | broadcast, or unicast to a known address; I-Am may come unicast or broadcast |
| ReadProperty (12) | single reads; array index for `ObjectList[0]` (its length) and elements | |
| ReadPropertyMultiple (14) | **the batching read**: one request per device per scan, split by the device's max APDU | per-property errors come back inside the ACK, and only that tag is `bad_config` |
| SubscribeCOV (5), unconfirmed notifications (2) | `COV`-modified tags and push mode: a subscription per object with a lifetime, renewed at lifetime/2, re-subscribed after a reconnect or device restart | confirmed notifications (1) need a SimpleACK; later |
| Segmentation | segmented ComplexACK reassembly with SegmentACK (window ≥ 1); SA bit and max segments in every request | needed for `ObjectList` and large RPMs. The client does not send segmented *requests*; RPMs are split instead |

**Objects:** Analog Input/Output/Value, Binary Input/Output/Value and Multi-state
Input/Output/Value, with these properties:

- `PresentValue`;
- `StatusFlags`;
- `Reliability`, read only when the FAULT flag is set, or always if configured;
- `OutOfService`;
- `Units`, `ObjectName` and `Description` for browsing and metadata;
- `StateText` and `NumberOfStates` for multi-state objects;
- `PriorityArray` (read-only access);
- `EventState`.

**Value mapping:**

| BACnet | opencap dtype |
|---|---|
| REAL | `float32` |
| Double | `float64` |
| Unsigned | `uint32` |
| Signed | `int32` |
| Enumerated (binary objects) | `bool` |
| Enumerated (other) | `uint32` |
| Character string | `string` |
| Bit string | `uint32` bitmask (StatusFlags) |

The bit string mask puts bit 0 (IN_ALARM) at bit 0.

**Read-only:** the driver can send only **ReadProperty, ReadPropertyMultiple and
SubscribeCOV**. The prototype pins this with `CONFIRMED_ALLOWED`. It cannot send:

- WriteProperty or WritePropertyMultiple;
- DeviceCommunicationControl or ReinitializeDevice;
- Create/DeleteObject or AddListElement;
- any confirmed COV acknowledgement other than SimpleACK.

The `PRIORITY` and `RELINQUISH` address modifiers exist only for writes, so the address parser
refuses them.

## 4. Addresses and automatic tag generation

### 4.1 Syntax, compatible with KEPServerEX

PTC's *BACnet/IP Driver* manual (v1.125) writes a tag as
`<ObjectType>.<Instance>.<Property>[<index>] [<modifier>]`. Examples:

- `AnalogValue.100.PresentValue`
- `AnalogValue.100.OutOfService COV`
- `AnalogValue.100.PriorityArray [4]`
- `AnalogValue.100.EventEnable.ToFault`

Object type and property names are CamelCase. The instance is 0–4194303. An array index is
1-based. A modifier follows a single space.

The opencap parser (the prototype's `parse_address`):

- **accepts** the KEPServerEX form for the subset's 9 object types plus `Device`;
- accepts the `COV` modifier;
- accepts **numeric** forms (`2.100.85`) for vendor-proprietary types and properties, as an
  opencap extension;
- **refuses** `PRIORITY` and `RELINQUISH`, because they are write-only (ADR-0008).

The structured form (`.EventEnable.ToFault`, bit-string members) is follow-up work. The
manual's exact spelling has been seen for `AnalogValue`, `AnalogInput` and `TrendLog` only, so
the other names (`MultiStateInput`, …) **must be checked against the PTC manual** in the
import-mapping follow-up.

### 4.2 `browse()`

1. Who-Is → I-Am gives the device address and capabilities.
2. ReadProperty `Device.<inst>.ObjectList`, segmented when needed. If the device cannot
   segment, read `ObjectList[0]` for the length, then each index.
3. RPM `ObjectName`, `Units` and `Description` (plus `StateText` for multi-state objects) for
   the subset objects, split by max APDU.
4. Emit one tag per object, `<ObjectType>.<inst>.PresentValue`, named after `ObjectName` and
   typed by object type. Group the tags by object type, or by object name (KEPServerEX's "Use
   Object Names for Group Names" option). `StatusFlags` tags are optional.

The prototype's `browse()` does steps 2–4, and its test checks that every generated address
parses back.

## 5. Quality (ADR-0006)

`StatusFlags` (IN_ALARM, FAULT, OVERRIDDEN, OUT_OF_SERVICE) and `Reliability` map to opencap
qualities. The first matching row wins:

| Condition | opencap quality | Why |
|---|---|---|
| Reliability = communication-failure (12) | `bad_comm` | the device has lost its own source |
| Reliability = configuration-error (10) | `bad_config` | |
| FAULT set, or any other Reliability ≠ no-fault-detected (no-sensor, over-range, under-range, open-loop, shorted-loop, unreliable-other, process-error, …) | `bad_decode` | the number is not a trustworthy measurement (the same use as the DNP3 spike's over-range) |
| OUT_OF_SERVICE | `stale` | the present value is disconnected from the field |
| IN_ALARM and/or OVERRIDDEN only | `good` | the value is current. The alarm and override are reported, not hidden (§6 Q2) |
| no flags | `good` | |

The driver reads `StatusFlags` together with `PresentValue` in the same RPM, at no extra cost,
and `COV` notifications carry both. `Reliability` is read only when FAULT is set. A device
that does not answer at all is `bad_comm` through the usual worker path.

**Timestamps:** BACnet present values carry no time, so `source_ts` is the arrival time of
the reply or notification (ADR-0006).

## 6. Questions for the lead

1. **bacpypes3's missing licence file.** MIT is declared in its pinned pyproject and in the
   wheel metadata, but no licence text is shipped. We use it test-only, never vendored or
   shipped. Is the declaration enough under ADR-0007, or should we ask upstream for a licence
   file (and pin the release that adds one) before the simulator PR?
2. **Alarm and override pass-through.** This is the same question as the DNP3 spike's Q3: keep
   raw device flags (here `StatusFlags`, a u4) as a per-value attribute, so a north protocol
   that can express them does not lose them.
3. **COV as the default for `browse()`-generated tags?** KEPServerEX makes `COV` a per-tag
   modifier. Proposal: polling by default, and `COV` on request.

## 7. Prototype and test peer

`spikes/bacnet/proto_client.py` is a throwaway, never imported by `src/`. It contains:

- BVLC and NPDU;
- BACnet tag encoding and decoding;
- Who-Is/I-Am, RP, RPM, SubscribeCOV and the COV notification parser;
- segmented-ACK reassembly with SegmentACK;
- foreign-device registration and distribute-broadcast;
- the address parser, `browse()` and the §5 mapping.

It was written from the ASHRAE 135 encodings. No other implementation's source was used.

`spikes/bacnet/sim_device.py` builds a bacpypes3 device on `127.0.0.1:<ephemeral>`. The device
has every subset object type (AI, AO, AV, BI, BO, BV, MI, MO, MV), 60 extra analog values to
force segmentation, and a write counter that refuses WriteProperty/WPM. It can optionally run
as a BBMD.

```bash
pip install bacpypes3==0.1.0 pytest pytest-asyncio
pytest spikes/bacnet -q        # 11 passed, 3 runs in a row (2026-11-25, Python 3.12)
```

| Test | Proves |
|---|---|
| `test_encodings` | RP request bytes; confirmed-request header; only RP, RPM and SubscribeCOV can be framed |
| `test_addresses_and_quality` | KEPServerEX syntax, numeric form, write modifiers refused; §5 |
| `test_who_is_and_read_property` | unicast Who-Is → I-Am; REAL, enumerated and unsigned values; unknown object → error |
| `test_status_flags_and_reliability_map_to_quality` | fault + no-sensor → `bad_decode`; out of service → `stale` |
| `test_read_property_multiple_batches_and_per_property_errors` | one RPM for 9 objects × 2 properties; an unknown property fails alone, inside the ACK |
| `test_segmented_response_is_reassembled` | a 72-entry `ObjectList` with 50-byte APDUs, segments acknowledged |
| `test_cov_subscription_delivers_changes` | subscription, initial notification, change notification with value and flags |
| `test_browse_builds_kepserverex_addresses` | 70 tags named after `ObjectName`; every address parses back |
| `test_foreign_device_registration_and_distribute_broadcast` | registration with a bacpypes3 BBMD (BVLC-Result 0), then a distributed Who-Is finds the device |
| `test_bacpypes3_client_reads_the_same_device` | **the device script validated by bacpypes3's own client**, independently of the prototype |
| `test_device_refuses_and_counts_writes` | the simulator side of read-only: a write sent by bacpypes3 is refused and counted, and the value is unchanged |

### 7.1 Simulator and CI plan

`tests/helpers/bacnet_sim.py` is follow-up 2. It grows `sim_device.py` with:

- several devices;
- per-object fault switches: `fault` with a reliability code, `out_of_service`, and an
  `unknown_property` error;
- `mute`, which keeps the socket open and drops replies;
- `restart()` on the same port, which drops COV subscriptions;
- counters for RP, RPM, SubscribeCOV, writes and SegmentACKs;
- a max APDU and segmentation-support setting, to cover devices that cannot segment.

It is validated by bacpypes3's client, unmodified, and by a second, unrelated client. That is
rusty-bacnet (MIT; its Python wheels install on 3.12) if its client API covers RP, RPM and COV;
follow-up 2 confirms this. Otherwise it is @bacnet-js/client (MIT).

**CI networking:**

- Everything runs on `127.0.0.1` with ephemeral UDP ports and **unicast** Who-Is, which every
  device answers (bacpypes3 answers a unicast Who-Is with a unicast I-Am).
- True broadcast is not needed to prove the protocol, and loopback broadcast behaves
  differently from one OS to another.
- The cross-subnet path is covered by foreign-device registration with a BBMD, as the
  prototype shows.
- A single host can still test the "devices on 47808 on separate IPs" case by binding devices
  to 127.0.0.2, 127.0.0.3, and so on, on Linux, with a test marked Linux-only.

## 8. Follow-up issues (one PR each)

| # | Title | Depends on | Acceptance (summary) |
|---|---|---|---|
| 1 | `engine-rust: BACnet/IP codec crate (BVLC, NPDU, APDU, tags, the §3 services, segmentation)` | — | `engine/crates/opencap-bacnet`, no I/O. The framing API cannot produce a non-allowed service (unit test). Encode/decode vectors (follow-up 2b) pass. A fuzz target covers the tag parser |
| 2 | `tests: BACnet/IP device simulator validated by bacpypes3 and a second client` | — | `tests/helpers/bacnet_sim.py` as described in §7.1. bacpypes3 is in the `dev` extra and the `[test_only]` licence list, after §6 Q1 is answered. `tests/unit/test_bacnet_sim.py` exercises every fault switch with both clients |
| 2b | `tests: BACnet encode/decode vectors captured from bacpypes3` | 2 | `tests/conformance/bacnet/`: RP/RPM/COV requests and ACKs, segmented ACKs, errors, and every application tag type. The expected values come from the simulator's inputs |
| 3 | `engine-rust: BACnet/IP driver: discovery, RPM batching, status → quality, browse()` | 1, 2, #41 | §3 and §4 with a planner that splits RPMs by max APDU. Per-property errors give `bad_config`. Covers `browse()`, routed devices (DNET), the `OPENCAP_ENGINE=rust` simulator suite and `docs/driver_params.md`. Read-only test: simulator `write_attempts == 0` |
| 4 | `engine-rust: BACnet COV subscriptions (push path)` | 3, #46 (subscribe path) | `COV` tags: subscribe, renew at lifetime/2, re-subscribe after a restart, fall back to polling when SubscribeCOV is refused, and staleness counted from the last notification |
| 5 | `engine-rust: BACnet foreign-device registration and BBMD use` | 3 | Registration and re-registration at TTL/2, distribute-broadcast discovery, and a registration-refused path (BVLC-Result NAK) |
| 6 | `config: KEPServerEX "BACnet/IP" import mapping` | 3 | Object type and property names checked against PTC's manual (§4.1), device ID → address and instance, `COV` modifier, structured properties, import vectors |
