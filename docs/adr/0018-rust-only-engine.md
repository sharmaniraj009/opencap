# 0018. Rust is the only engine; Python is frozen and will be deleted; OSS drivers first

- Status: Accepted
- Date: 2026-11-27
- Deciders: owner (direction), technical lead
- Supersedes: the "Python reference" role in ADR-0002; the "native Rust only" approach of ADR-0015, 0016 and 0017
  (those drivers now follow the OSS-first rule below).

## Context
ADR-0002 made Rust the production engine and kept Python as the reference. Two engines cost double:
every feature is built twice, decision numbers collide, and Python tops out near 25k tag updates/s per
core. The product has to scale, so the owner decided on one engine.

## Decision
1. **Rust is the engine.** New features go into `engine/` only. **No new Python code** (no features, no
   drivers, no simulators). Python bug fixes are not accepted either, unless a release blocker.
2. **Python is deleted when v0.1 is done** (see `docs/plan/v0.1-rust.md`, "Deleting Python"). Until then
   `src/opencap/` and the Python test harness (`tests/`) stay in the tree, untouched, only so the Rust
   engine can still be checked against them.
3. **Drivers: open source first.** For each protocol, in this order:
   a. evaluate an existing open-source library (Rust crate, or a C/C++ library bound through FFI);
   b. test it against a simulator or a real device: connect, read every supported data type, error and
      timeout paths, reconnect, a soak run;
   c. if it passes and the licence passes the ADR-0007 gate, use it;
   d. if it fails the tests or the licence, write our own and record why in `docs/decisions.md`.
   Drivers that already exist natively in `engine/drivers/` stay unless an OSS library clearly beats them
   in a test (field-proven C libraries have more real-hardware mileage than our simulator-tested code,
   so a comparison test is worth running for Allen-Bradley and Siemens).
4. **FFI to C/C++ is allowed.** A wrapper crate exposes a read-only safe API. Bindings are generated with an
   allow-list of read functions only, and a CI test fails if the wrapper declares or calls a write
   function (ADR-0008 is unchanged: no write path may be reachable from the engine).
5. **Licences:** the ADR-0007 gate applies to C/C++ libraries too. Static linking of LGPL or GPL code is
   not allowed. MPL-2.0, Apache-2.0, MIT and BSD are fine. LGPL is allowed only with dynamic linking
   and a documented relink path.

## Consequences
- ADR-0002's Python reference is retired; the conformance vectors in `tests/conformance/` stay valid
  (they are JSON data) and become the specification, run by Rust only.
- Python simulators in `tests/helpers/` stay as test tools until replaced by OSS simulators or Rust
  ones. Nobody writes new Python simulators.
- Issues that only concern Python code are closed as "not planned".
