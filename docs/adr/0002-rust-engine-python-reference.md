# 0002. Rust production engine; Python as reference and conformance harness

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead (direction set by the owner: "main engine in Rust or C++")

## Context
The Python implementation works (Modbus TCP, AB Logix/Micro800, PCCC, MQTT with outbox,
admin API; ~985 tests) and more Python drivers are in flight. The owner wants the main
engine in Rust or C++, desktop apps for Linux and Windows, and scale to thousands of devices
per node on small edge boxes. Python's per-tag overhead and the GIL cap one process at an
estimated ~25k tag updates/s per core (04 §4.1, to be measured); memory per tag is high.

## Decision
1. **Rust**, not C++: memory safety for network-facing protocol parsers, Cargo for
   reproducible cross-builds (x86_64, aarch64, Windows MSVC), a mature async runtime, Tauri
   for the desktop app, `cargo-deny` for licence policy.
2. The Rust engine lives in `engine/` (Cargo workspace, RFC 0001 from the `rust-engine`
   session). It reads the same configuration ([ADR-0003](0003-shared-config-schema.md)),
   serves the same API, publishes the same payloads.
3. The **Python code is the reference**: its behaviour is the specification, captured as
   conformance vectors and acceptance suites ([ADR-0014](0014-conformance-by-simulators-and-vectors.md)).
4. **Migration is a strangler** ([06-roadmap-and-migration.md](../architecture/06-roadmap-and-migration.md)):
   in-flight Python drivers finish in Python; once the Rust driver trait is on `main`, new
   drivers, north plugins and runtime features are Rust-first; Python drivers are ported
   with their simulator suites as acceptance tests; unported drivers can run under the
   Rust engine through the Python driver host ([ADR-0004](0004-driver-contract-and-plugin-model.md)).
5. **Cut-over** per driver family after acceptance suites, vectors and a 7-day shadow run
   pass; the Python engine is retired one release later. Simulators and acceptance suites
   stay in Python permanently.

## Consequences
- For a while some behaviour exists twice; the vectors and the engine-agnostic acceptance
  suites keep the two equal.
- Python receives only bug fixes and site-critical features after the Rust trait lands.
- Engineers must be able to work in both languages; most new work is Rust.

## Alternatives considered
- **Stay in Python** (optimise, multiprocess per channel): cheapest now, but no single
  binary, weak on 1 GB boxes, awkward Windows service and desktop packaging.
- **C++**: mature industrial libraries (open62541, opendnp3), but memory-unsafe parsers,
  heavier build tooling, harder to keep a small AI team productive.
- **Big-bang rewrite**: loses the working reference and its tests during the rewrite.

## Revisit when
The Rust engine misses the NFR targets at M2 by a wide margin, or the team finds the
dual-language period more expensive than porting everything at once.
