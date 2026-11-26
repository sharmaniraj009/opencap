# Third-party protobuf schemas (vendored, unmodified)

| File | Source | Licence |
|---|---|---|
| `sparkplug_b.proto` | Eclipse Tahu [v1.0.19](https://github.com/eclipse-tahu/tahu/blob/v1.0.19/sparkplug_b/sparkplug_b.proto), `sparkplug_b/sparkplug_b.proto`. SHA-256 `4432c5c483b7fb9732d0594c98a2e97dca5e517e39c5374a8b918d837f0b4a19` | EPL-2.0 (header in the file; full text in [`LICENSE-EPL-2.0.txt`](LICENSE-EPL-2.0.txt), from the SPDX licence list) |

Rules (ADR-0007, weak copyleft at file level):

- **Never edit these files.** A change to an EPL-2.0 file must be published under EPL-2.0. To move to a newer Tahu
  release, replace the file with the upstream copy, update the table above, and rerun
  `OPENCAP_BLESS=1 cargo test -p opencap-north-sparkplug --test vectors descriptor` and
  `python -m tests.helpers.sparkplug_ref`.
- `engine/north/sparkplug/build.rs` generates the Rust types from `sparkplug_b.proto` at build time with prost and
  protox, so no generated code is committed. `tests/conformance/sparkplug/sparkplug_b.desc` is the compiled schema
  (no comments) that the Python reference loads; a Rust test keeps it identical to the build's.
- The binary carries code generated from this file; `NOTICE` names it and where its source is.
