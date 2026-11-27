# Vendored libplctag

- Upstream: https://github.com/libplctag/libplctag, tag v2.7.2 (commit 7712ed7), licence MPL-2.0 OR LGPL-2+
  (`LICENSE-1.MPL`, `LICENSE-2.LGPL`). opencap uses it under MPL-2.0, linked statically (ADR-0018, D-121).
- Copied: `src/libplctag`, `src/platform`, `src/utils`, `src/tools/ab_server`. CMake files are left out; the
  `cc` crate builds the sources (`engine/crates/opencap-plctag-sys/build.rs`, `engine/crates/opencap-ab-server/build.rs`).
- **Only change to the sources:** in the comments of `src/libplctag/protocols/ab/cip.c` (lines 224-225) and
  `src/libplctag/protocols/omron/cip.c` (lines 220-221), the example address `10.206.10.14` is written as
  `192.0.2.14` (the documentation range), because the repository's private-address check
  (`tests/unit/test_no_private_data.py`) scans every tracked file. No code changed.
- To update: copy the new tag over these directories and repeat the comment edit.
