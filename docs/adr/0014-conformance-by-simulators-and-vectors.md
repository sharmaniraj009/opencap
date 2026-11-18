# 0014. Behaviour is proven by simulators and shared conformance vectors

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead (rule set by the owner: nothing touches real plant from CI or cloud)

## Context
CI and cloud sessions can never reach a PLC. The Python reference already proves each
protocol against simulators that real client libraries accept unmodified (pymodbus server,
`cip_sim.py`, `pccc_sim.py`, mutation-checked encoders, D-014/D-024/D-026). Two engines must
behave identically.

## Decision
1. **Simulators**: every protocol has a simulator in `tests/helpers/`, validated by an
   independent client library (or the protocol's OSS reference peer), with fault switches
   (mute, exception/status codes, short replies, restart on the same port).
2. **Conformance vectors**: language-neutral JSON files in `tests/conformance/<area>/`
   for pure functions (address parsing, block planning, decoding, scaling, payload building,
   quality mapping, config validation, derived-tag expressions). Generated from the Python
   reference, checked by a Python test, consumed by Rust tests.
3. **Engine-agnostic acceptance suites**: the phase acceptance tests drive an engine only
   through its config directory, API and the wire, selected by `OPENCAP_ENGINE=python|rust`.
4. **North conformance** with OSS clients (SCADA matrix L1).
5. Real-device findings are recorded in `docs/decisions.md` and then reproduced in a simulator
   before the fix merges.

## Consequences
Simulator fidelity is the limit of what CI proves; commissioning on site (owner-run) remains
the final check, guided by the README's commissioning steps.

## Alternatives considered
Hardware-in-the-loop CI (impossible by rule); recorded packet captures from sites (privacy
risk in a public repo).

## Revisit when
Never for the rule; the tooling evolves.
