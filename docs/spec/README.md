# Language-neutral specifications

Behaviour that both engines must implement identically is specified here, with conformance
vectors in `tests/conformance/<area>/` ([ADR-0014](../adr/0014-conformance-by-simulators-and-vectors.md)).
Each spec arrives through a board issue.

| Spec | Status |
|---|---|
| `payload-subdevice-json.md` | today in the `src/opencap/north/payload.py` docstring and D-017 |
| [`payload-iotgw-json.md`](payload-iotgw-json.md) | v1 (#36); vectors in `tests/conformance/payload/iotgw/`; Python done, Rust follows |
| [`payload-sparkplug.md`](payload-sparkplug.md) | draft v1 (#51); vectors in `tests/conformance/sparkplug/`; Rust `engine/north/sparkplug` |
| [`system-tags.md`](system-tags.md) | draft v1 (#22); vectors in `tests/conformance/system_tags/` (generated; hand-written cases in `scripts/conformance_system_tags/`); Python follow-ups and Rust #56 implement it |
| [`ui-kepserver-parity.md`](ui-kepserver-parity.md) | audit and build order for the KEPServerEX-style desktop experience (D-140) |
| `derived-tags.md` | board issue |
| `alarms.md` | board issue |
| `data-logger.md` | board issue |
