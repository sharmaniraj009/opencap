# 13 iFIX

> Source: KEPServerEX V6 server manual (© PTC), pages 221–232, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
GE's iFIX can use KEPServerEX through its native "IGS" driver interface. GE ships an OEM build of
KEPServerEX under that name. Engineers create process database (PDB) blocks in iFIX Database
Manager with the driver acronym `IGS`, and with an I/O address made of the server's channel,
device and tag names. That address is not case-sensitive, and several blocks may share one address
(manual p. 221–223). Bits are reached through digital-register block offsets or bit tags in the
server. Array elements are reached through analog/digital-register offsets, and a whole array
through a text block (manual p. 223–224). For each block, iFIX can apply signal conditioning: BCD
(3 or 4 digits), 8- to 15-bit binary, with or without out-of-range alarming, and two clamped ranges
(6400–32000 and 0–32000). Each has a read algorithm that scales the raw value to the block's
engineering range, and a write algorithm for the reverse (manual p. 224–231). Finally, the server
keeps a per-project iFIX item file (`default_FIX.ini`) and starts scanning those items before iFIX
asks for them, so that blocks read only once at start (outputs, alarm values) get an initial value
(manual p. 231–232).

## opencap today
- **The iFIX native (IGS) interface is not offered** (FR-8, ADR-0009 point 5: closed protocol).
  iFIX is meant to reach opencap through the **OPC UA server**. In the compatibility matrix, the
  iFIX OPC UA client is marked *verify* for version support. The alternative is OPC DA through the
  planned Windows bridge (RFC 0004, #18). See `docs/architecture/scada-compatibility.md` and RFC
  0001 §4.2. IGS sites are named there as a primary audience.
- The OPC UA server is in flight (#61 → takeover #74). Hardening with KEPServerEX-compatible NodeIds
  `ns=<idx>;s=Channel.Device.Tag` and an optional namespace index 2 is #28, and the L1 client suite
  is #29. Per-SCADA recipes (`docs/scada/<product>.md`) are #17.
- Tag model (`src/opencap/config/models.py`): bit tags through driver address syntax (Modbus
  `4xxxxx.b`, PCCC bit addresses, Logix `Tag.n`; program-scoped Logix names keep their full
  `Program:X.Y` path), 1-D arrays, and linear/square-root `scale` with raw and engineering ranges,
  clamps, negate and units. BCD/LBCD tags are imported disabled, because there is no decoder yet
  (D-010, FR-2).
- Polling does not depend on clients: every enabled tag is scanned from start, and reads are served
  from the cache (D-015, 03 §3.5). Quality before the first good read follows ADR-0006 (OPC UA
  `Bad_WaitingForInitialData`).
- Read-only: no output-block writes (ADR-0008).

## Features

### IFIX-01 iFIX native (IGS) driver interface (manual p. 221–223)
- **What:** iFIX talks to the server through its own driver interface, selected with the acronym
  `IGS` and listed in the iFIX SCADA configuration.
- **Decision:** Out of scope: a proprietary, undocumented interface (ADR-0009, FR-8). iFIX connects
  through OPC UA (IFIX-02) or the OPC DA bridge (#18).
- **Status:** Out of scope

### IFIX-02 Connection recipe for iFIX over OPC UA (manual p. 221–223)
- **What:** The steps an iFIX engineer follows to point database blocks at the server.
- **Decision:** Adapt: a recipe page for iFIX's OPC UA client driver. It covers endpoint, security
  policy, user token, how `Channel.Device.Tag` maps to the driver's item address, what changes when
  migrating from IGS, and known limits.
- **Status:** Planned (#17)
- **Requirements:**
  - `docs/scada/ifix.md` gives the iFIX version range verified, the endpoint and security
    settings, the address format with examples, and how bits, arrays and signal conditioning map
    (IFIX-05 … IFIX-09).
  - The recipe names the OPC DA bridge route as the fallback for iFIX versions without OPC UA
    (#18).
  - The *verify* mark for iFIX in `scada-compatibility.md` is resolved (kept or removed, with the
    version).
- **Acceptance:** An L2 test, owner-run (scada-compatibility.md "Test levels"): an iFIX node reads
  a simulator device's tags through opencap, recorded in the recipe. L1 OPC UA tests (#29) cover
  the server side in CI.

### IFIX-03 KEPServerEX-compatible item paths (manual p. 223)
- **What:** A block's I/O address is `Channel.Device.Tag`, case-insensitive, and many blocks may
  use the same address. Blocks added later are validated against the server's configuration.
- **Decision:** Adapt: the OPC UA NodeIds keep the KEPServerEX path (ADR-0009 point 2), so a
  migrated block changes only its driver and server, not its tag path. OPC UA string NodeIds are
  case-sensitive, so case is corrected at migration time (IFIX-04), not at runtime.
- **Status:** Planned (#28)
- **Requirements:**
  - Every tag is reachable as `ns=<idx>;s=Channel.Device[.Group…].Tag`. A namespace-index-2 option
    exists for SCADA projects that hard-code the index (#28).
  - Any number of monitored items or reads may target the same node, and the device is polled once
    whatever the number of subscribers.
  - An address that is not a configured tag returns `Bad_NodeIdUnknown`. No tag is created on demand
    unless dynamic tags are enabled (plan 06, FR-2, off by default).
- **Acceptance:** An L1 test (#29): two subscriptions on one node with one poll on the simulator,
  an unknown node rejected, and NodeIds equal to the KEPServerEX-style path for every tag of a
  fixture project.

### IFIX-04 PDB address migration helper (manual p. 221–223)
- **What:** Moving an iFIX database from IGS addresses to another driver means editing every
  block's driver and I/O address.
- **Decision:** Defer: write a converter once the iFIX OPC UA driver's address format is confirmed
  by #17.
- **Status:** Not started
- **Requirements:**
  - `opencap scada ifix-convert <pdb.csv> [--config DIR]` reads an iFIX Database Manager CSV
    export. It rewrites blocks whose driver is `IGS` to the iFIX OPC UA driver and its address
    syntax, and corrects letter case against opencap's tag list.
  - It writes a report of blocks whose address matches no opencap tag, bit or array forms it
    rewrote, and blocks it left unchanged. It never edits the input file.
- **Acceptance:** Unit tests with a synthetic PDB export (no site data): every row converted or
  reported, and case corrected. The owner imports the result into a test iFIX node (L2).

### IFIX-05 Bit addressing (manual p. 223–224)
- **What:** Bits of an integer are read through digital-register block offsets, or by defining a
  bit tag in the server. An integer address on a digital block uses only its least significant
  bit.
- **Decision:** Adapt: define the bit as a `bool` tag with the driver's bit syntax. iFIX-side
  offsets on DR blocks then act on the integer that the OPC UA client driver reads. The recipe
  (IFIX-02) documents both ways.
- **Status:** Done
- **Requirements:** Bool tags at bit addresses exist for every driver whose protocol has
  word-sized data: Modbus `4xxxxx.b`/`3xxxxx.b`, PCCC bit addresses and Logix `Tag.n`. They read
  through the host word's block, with no extra request.
- **Acceptance:** `tests/unit/test_modbus_address.py`, `test_pccc_address.py`,
  `test_logix_plan.py`, and the simulator suites for each driver.

### IFIX-06 Array addressing (manual p. 224)
- **What:** Elements of an array tag are read through register-block offsets, and the whole array
  as text through a text block.
- **Decision:** Adapt: 1-D array tags are served as OPC UA array variables (`ValueRank` 1,
  `ArrayDimensions`), and single elements are read with `IndexRange`. Where a SCADA cannot use
  arrays, define element tags instead.
- **Status:** Planned (#28)
- **Requirements:**
  - Array tags appear with the right `DataType`, `ValueRank` and `ArrayDimensions`.
  - `Read` and subscriptions honour `IndexRange` for single elements and ranges.
  - String rendering of a whole array is left to the client.
- **Acceptance:** An L1 test (#29) reads a whole array and single elements of a simulator array
  tag, and they equal the MQTT payload's array.

### IFIX-07 Read-side signal conditioning: binary and clamp presets (manual p. 224–231)
- **What:** Per-block conversions from raw 8- to 15-bit values, or from the 6400–32000 and
  0–32000 ranges, to the block's engineering range. The "BN" forms ignore the bits above the
  stated width.
- **Decision:** Adapt: express each preset as an opencap `scale` (linear, raw and engineering
  ranges, clamps), plus a new optional `scale.raw_mask` for the bit-width forms. Do the
  conditioning **either** in opencap **or** in iFIX, never both (the recipe says which). This
  manual says on p. 222 that the IGS driver does not use the signal-conditioning field, yet lists
  the options on p. 224–231, so the recipe states plainly what applies over OPC UA.
- **Status:** Not started
- **Requirements:**
  - `scale.raw_mask` (unsigned int, optional) is ANDed with the raw integer before scaling. It goes
    into the Pydantic model, the JSON Schema, the Rust serde model and a conformance fixture
    (ADR-0003).
  - `docs/scada/ifix.md` lists, for each preset (8BN, 12BN, 13BN, 14BN, 15BN, 20P, TNON), the
    `scale` block that reproduces its read algorithm.
  - The scaling vectors include every preset at its range ends, at mid-range and with high bits
    set.
- **Acceptance:** Conformance vectors in `tests/conformance/scaling/` (#3), passing in both
  engines.

### IFIX-08 BCD signal conditioning (manual p. 225–226)
- **What:** 3- and 4-digit BCD values are decoded and scaled. A nibble above 9 is reported as out
  of range.
- **Decision:** Adapt: implement the planned `bcd`/`lbcd` data types (FR-2) in the drivers, then
  scale with a linear `scale` from 0–999 or 0–9999. An invalid digit makes the tag `bad_decode`
  (ADR-0006).
- **Status:** Not started
- **Requirements:**
  - `dtype: bcd` (16-bit, 4 digits) and `lbcd` (32-bit, 8 digits) decode in every word-based driver.
    The importer then imports BCD tags enabled instead of disabled (D-010).
  - Any nibble greater than 9 gives `bad_decode` with the raw value in the device's last error.
- **Acceptance:** Decoder vectors (valid, each invalid nibble, maximum value) in both engines. A
  Modbus simulator test with a BCD register.

### IFIX-09 Out-of-range alarm variants (manual p. 226–230)
- **What:** The "AL" variants scale like their "BN" twins but also flag raw values outside the
  input range as an alarm.
- **Decision:** Defer to the alarms feature (FR-5, spec #24): a limit alarm on the raw range. No
  new quality is added for it (ADR-0006).
- **Status:** Not started
- **Requirements:** These are set in spec #24: a raw-range limit condition per tag, exposed through
  OPC UA Alarms & Conditions and the MQTT alarm topic.
- **Acceptance:** Spec #24 vectors.

### IFIX-10 Write side of signal conditioning and output blocks (manual p. 225–232)
- **What:** Each conditioning option has a write algorithm. Output blocks (AO, DO) write values to
  the device.
- **Decision:** Out of scope: opencap is read-only towards PLCs (ADR-0008). Writes wait for RFC 0002
  (#19) and the owner's approval.
- **Status:** Out of scope

### IFIX-11 Initial values at SCADA start (manual p. 231–232)
- **What:** The server keeps a list of the items iFIX will use (`default_FIX.ini`, rebuilt by
  re-importing the PDB) and scans them before iFIX asks, so one-shot reads at startup get a value.
- **Decision:** Adapt: no per-client item file is needed. opencap scans every configured, enabled
  tag from the moment the channel starts, whether or not a client is subscribed. Every read is
  served from the cache.
- **Status:** Done
- **Requirements:**
  - Polling starts at engine start for every enabled device, independent of north clients.
  - North reads return the cached value. Before the first good read, the value is `stale` with no
    value, mapped per ADR-0006 (OPC UA `Bad_WaitingForInitialData`).
- **Acceptance:** `tests/unit/test_cache.py` (pre-filled `stale` entries) and the Phase 1 acceptance
  suite (values present after the first cycle, both engines). OPC UA mapping vectors (#30).

### IFIX-12 Readiness signal so SCADA start can wait (manual p. 232)
- **What:** The manual suggests delaying iFIX's scan start until the server has read initial
  values.
- **Decision:** Adapt: expose readiness, so an operator, script or service dependency can wait for
  it.
- **Status:** Not started
- **Requirements:**
  - `GET /health/ready` (board backlog: `/health/live` + `/health/ready`) answers 200 once config
    is loaded, north servers are started, and every enabled device has finished one cycle or is
    `offline`/`demoted`. Otherwise it answers 503, with the devices still waiting.
  - `/health` shows `initial_scan_complete` (bool) and how long it took.
  - The OPC UA server's `ServerStatus.State` stays `Running`, so clients can connect before
    readiness.
- **Acceptance:** Integration test with one fast and one muted simulator device: 503 before the
  first cycle, 200 after the muted device is marked offline. Both engines.

## Open questions
- **iFIX version:** which iFIX versions do the owner's sites run, and does their OPC UA client
  driver support the needed security policies and arrays? (#17 verify)
- **Where conditioning happens (IFIX-07):** should migrated sites move signal conditioning into
  opencap's `scale` (one place for every consumer, MQTT included), or keep it in iFIX's OPC UA
  driver (no change in iFIX)?
- **Case handling (IFIX-03/04):** fixing case at migration time is proposed. Is a case-insensitive
  NodeId lookup on the opencap side ever acceptable? It is not standard OPC UA and would cost
  memory on 250k-node spaces.
- **Converter (IFIX-04):** is a PDB converter worth building, or does the owner's iFIX tooling
  already re-point drivers in bulk?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| IFIX-01 | iFIX native (IGS) driver interface | Out of scope | Out of scope | ADR-0009 | — |
| IFIX-02 | Connection recipe for iFIX over OPC UA | Adapt | Planned (#17) | #17, #74 | M3 |
| IFIX-03 | KEPServerEX-compatible item paths | Adapt | Planned (#28) | #28, #29 | M3 |
| IFIX-04 | PDB address migration helper | Defer | Not started | after #17 | — |
| IFIX-05 | Bit addressing through bool tags | Adapt | Done | — | M1 |
| IFIX-06 | Array addressing over OPC UA | Adapt | Planned (#28) | #28, #29 | M3 |
| IFIX-07 | Read-side signal conditioning presets | Adapt | Not started | #3 | — |
| IFIX-08 | BCD signal conditioning | Adapt | Planned (#115) | #115 | — |
| IFIX-09 | Out-of-range alarm variants | Defer | Not started | #24 | M5 |
| IFIX-10 | Write side of conditioning, output blocks | Out of scope | Out of scope | ADR-0008, #19 | — |
| IFIX-11 | Initial values at SCADA start | Adapt | Done | D-015 | M1 |
| IFIX-12 | Readiness signal so SCADA start can wait | Adapt | Planned (#123) | #123 | — |
