# 0015. DNP3: native Rust master and outstation for the read subset; opendnp3 and dnp3py as test peers

- Status: Proposed
- Date: 2026-11-25
- Deciders: technical lead

## Context
opencap needs DNP3 twice:

- as a master driver (FR-1, KEPServerEX "DNP3 Client");
- as an outstation northbound for utility SCADA.

The DNP3 spike ([`docs/spikes/dnp3.md`](../spikes/dnp3.md), #14) checked every candidate's
licence at a pinned tag:

- **opendnp3** (Apache-2.0) is the reference C++ stack. It was archived in 2022 and has no C ABI.
- **Step Function I/O `dnp3`** is non-commercial in every version, and its 1.8.0 licence also
  forbids using it as a test peer or as a reference.
- The **permissive Rust crates** are pre-alpha.
- **Two Python peers** are permissive: yadnp3 (an Apache-2.0 opendnp3 fork with cp312 wheels)
  and dnp3py (MIT, pure Python). Both have a master and an outstation.

A 385-line prototype master read static data, time-tagged events and flags from both peers.

## Decision
1. Implement DNP3 natively in Rust:
   - a codec crate with no I/O;
   - a master driver crate;
   - an outstation north plugin.

   They cover the read subset in spike §3: integrity and event polls, unsolicited responses, and
   the binary, double-bit, counter, frozen counter, analog and output-status objects.
2. **Read-only by construction** (ADR-0008):
   - The master's framing API can produce only CONFIRM and READ. Adding ENABLE/DISABLE_UNSOLICITED
     waits on the lead's answer to spike §6 Q1.
   - The outstation answers every control (CROB, analog output) with `NOT_SUPPORTED` and answers
     other writing function codes with IIN2.0.
   - Tests against both peers pin this.
3. **Quality mapping:** spike §5 (flags ↔ the five qualities; device event time → `source_ts`,
   with `device_time: auto` falling back to arrival time on unsynchronised clocks) is the
   mapping table ADR-0006 asks for.
4. **Test peers:** yadnp3 (opendnp3) and dnp3py, test-only and installed by the `dev` extra.
   - The Python harness simulator wraps dnp3py's application layer behind a link/transport front
     end of our own that carries the fault switches.
   - Decode vectors are captured from opendnp3.
   - Step Function I/O `dnp3` is **not used in any role**, and its source is not read for opencap
     work.
5. No GPL component is involved, so this needs no owner decision.

## Consequences
- 5 PRs for the product plus 2–3 for the harness (spike §8). Subset Level 2 behaviour has to
  be earned against two independent peers, not inherited from a mature stack.
- One code base for master and outstation, the same on every platform the engine builds for, with
  no C++ toolchain.
- SOE fidelity (every event, not only the latest) needs the Rust `subscribe` path (#46); until
  then events between two reads are coalesced.

## Alternatives considered
- **FFI to opendnp3:** licence-compatible, but it means maintaining an end-of-life C++ code base
  and writing a C shim, and a crash would take the engine down.
- **opendnp3 or yadnp3 as an out-of-process plugin:** quicker to start, but it adds a process and
  a Python runtime and rests on an "experimental" fork. It stays an option for a stop-gap.
- **Step Function I/O `dnp3`:** excluded by its licence.
- **A young permissive Rust crate:** immature; we would end up owning a fork.

## Revisit when
- A permissively licensed Rust DNP3 stack reaches Subset Level 2 conformance with an active
  maintainer team.
- Write support is approved (RFC 0002): DNP3 controls then need their own design.
