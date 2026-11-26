# 0017. IEC 60870-5-104: native Rust client and server; lib60870 only as a GPL test peer

- Status: Proposed
- Date: 2026-11-25
- Deciders: technical lead (and the owner, if c104 in CI is judged to fall under decision 5)

## Context
opencap needs IEC 104 in two roles:

- as a **client** driver for RTUs and substations;
- as a **server** for energy SCADA.

The IEC 104 spike ([`docs/spikes/iec104.md`](../spikes/iec104.md), #16) checked every
candidate's licence at a pinned tag:

- **lib60870-C** is mature but GPL-3.0 or commercial, and so are its .NET, Java, Python (c104)
  and Rust-binding wrappers. ADR-0007 allows it only as a separately distributed plugin with the
  owner's approval.
- **The permissive Rust crates** (igs-iec104, iec104, fegrid-iec60870) are all pre-1.0, have one
  author each, and are weeks old.
- **No permissive Python stack** exists.

A ~330-line prototype client covered the client subset against both lib60870 (through c104) and
igs-iec104:

- STARTDT/STOPDT/TESTFR;
- general and counter interrogation;
- spontaneous ASDUs and CP56Time2a;
- the k/w windows;
- the quality mapping.

## Decision
1. Implement IEC 104 natively in Rust: a codec crate (no I/O), a client driver and a server
   north plugin, for the subset in spike §3. Redundancy groups and TLS follow in their own PR.
2. **Read-only by construction** (ADR-0008).
   - The client can frame only general interrogation and counter interrogation with FRZ = 0
     (and the read command, if it is added). It never sends a command, set point, clock sync,
     reset process or parameter load.
   - The server answers every command with a negative confirmation.
3. **Quality:** spike §5 maps IV/NT/SB/BL/OV to and from the five qualities; this is the mapping
   table ADR-0006 asks for. A device `time_zone` setting converts CP56Time2a to `source_ts`.
4. **Test peers:**
   - our own Python server simulator, validated by the c104 client and the igs104 client;
   - c104 (GPL-3.0) is test-only and is installed by a separate CI job, never in the `dev` extra
     and never vendored;
   - igs-iec104 (Apache-2.0) is built once in CI.
5. lib60870 is not shipped in any form, so this decision needs no owner approval for a copyleft
   plugin.

## Consequences
- The product needs 5 PRs, plus 2 for the harness (spike §8).
- A standard energy protocol ships in the default build with no extra package.
- We own the APCI state machine and redundancy. Both are tested against two independent stacks.
- The KEPServerEX "IEC 60870-5-104 Client" address syntax is still to be checked (import
  follow-up).

## Alternatives considered
- **lib60870-C as an isolated plugin:** needs owner decision 5 and a second package for users,
  and the server side needs a north-plugin protocol that does not exist yet.
- **igs-iec104 or iec104 as a dependency:** permissive, but too young. Revisit them at 1.0.
- **Linking lib60870 into the engine:** GPL in the default build, so excluded.

## Revisit when
- A permissive Rust IEC 104 stack reaches 1.0 with more than one maintainer and passes the
  simulator suite.
- IEC 60870-5-101 (serial) is requested: the codec crate is meant to be reused for it.
