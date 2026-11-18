# Simulator driver conformance vectors

`vectors.json` pins the `simulator` driver's address grammar and value functions (#12) for both engines
([ADR-0014](../../../docs/adr/0014-conformance-by-simulators-and-vectors.md)). The formulas are in
[`docs/driver_params.md`](../../../docs/driver_params.md) (section `simulator`).
The Python engine runs them in `tests/unit/test_simulator.py`; the Rust port must pass the same file.

- `valid[]`: `{address, dtype, at_ms, expected, params?, reads?, tolerance?, note?}`. Parse `address` for a tag of
  `dtype` with `params` (the device's `params`, default `{}`), compute the value with the clock at `at_ms`
  (milliseconds since 1970-01-01T00:00:00Z) and, for `COUNTER`, the 1-based read count `reads` (default 1).
  The result must equal `expected` exactly and have its JSON type (integer, float, boolean, string); with
  `tolerance`, a float within that absolute difference. float32 values are given as the exact double of the float32.
- `invalid[]`: `{address, dtype, error, params?}`. Parsing must fail. `error` is a substring of the Python
  engine's message; another engine may match on the reason instead.

All data is synthetic. The Python side generated the RANDOM and off-axis SINE values; the rest were computed by hand
and checked against the implementation.
