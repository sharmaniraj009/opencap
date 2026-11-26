# 0016. BACnet/IP: native Rust read-only client; bacpypes3 simulator and a second independent peer

- Status: Proposed
- Date: 2026-11-25
- Deciders: technical lead

## Context
BACnet/IP is a tier-2 KEPServerEX driver used in building automation. The BACnet spike
([`docs/spikes/bacnet.md`](../spikes/bacnet.md), #15) checked each candidate's licence at a
pinned tag:

- **bacnet-stack** has a GPL-with-linking-exception core and two plain-GPL files. Its client
  cannot reassemble segmented replies.
- **The permissive Rust crates** are either pre-1.0 with a single author and an MSRV above the
  engine's (rusty-bacnet), stalled (rustbac), or without segmentation (bacnet-rs).
- **bacpypes3** is pure Python. It declares MIT in its pinned pyproject but ships no licence
  text. Its local objects can act as a complete test device.

A ~520-line prototype client covered the whole read subset against a bacpypes3 device:

- Who-Is/I-Am, RP and RPM;
- segmented responses;
- COV;
- foreign-device registration through a BBMD;
- `browse()`.

## Decision
1. Implement BACnet/IP natively in Rust as a codec crate (no I/O) and a driver crate, covering
   the read subset in spike §3. RPM is the batching read, split by the device's max APDU.
   Segmented ComplexACKs are reassembled. COV runs on the push path, and foreign-device
   registration covers other subnets.
2. **Read-only by construction** (ADR-0008). The framing API can send only ReadProperty,
   ReadPropertyMultiple and SubscribeCOV. The address parser refuses the write-only
   `PRIORITY` and `RELINQUISH` modifiers.
3. **Addresses** follow KEPServerEX: `<ObjectType>.<Instance>.<Property>[index] [COV]`. A
   numeric form is added for proprietary types and properties.
4. **Quality.** Spike §5 maps `StatusFlags` and `Reliability` to the five qualities; this is the
   mapping table ADR-0006 asks for. `source_ts` is the arrival time.
5. **Test peers.** The simulator is built on bacpypes3 (test-only; the lead confirms that the
   declared MIT is enough, spike §6 Q1). It is validated by bacpypes3's client and a second
   unrelated client (rusty-bacnet or @bacnet-js/client, both MIT). bacnet-stack is not linked;
   its demo tools may be used test-only.

## Consequences
- The product needs 4 PRs, plus 2 for the harness (spike §8).
- The engine keeps its MSRV and adds no C toolchain.
- We own segmentation and COV renewal. Both are tested against two peers.
- KEPServerEX's object type and property spellings beyond AnalogInput and AnalogValue still
  have to be checked against PTC's manual (import-mapping follow-up).

## Alternatives considered
- **rusty-bacnet as a dependency:** the most complete permissive option, but it is pre-1.0,
  has one maintainer and needs MSRV 1.95. Revisit at 1.0.
- **bacnet-stack through FFI:** copyleft terms on modified files, the two plain-GPL files, no
  client segmentation, and C code in the engine process.
- **rustbac / bacnet-rs:** stalled, or lacking segmentation.

## Revisit when
- rusty-bacnet (or another permissive stack) reaches 1.0 with more than one maintainer and
  passes the simulator suite.
- BACnet/SC or MS/TP is requested. Both are out of this subset.
