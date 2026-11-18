# Conformance vectors

Language-neutral test vectors for the pure functions both engines must agree on
([ADR-0014](../../docs/adr/0014-conformance-by-simulators-and-vectors.md)). They are generated
from the Python reference, checked by a Python test, and consumed by the Rust engine's tests.
A vector is one input and the exact output the Python reference produces for it.

```
tests/conformance/
  README.md            this file: layout, format, regeneration, how an engine consumes them
  vectors.py           Python loader + value codec (the format, nothing protocol-specific)
  reference.py         Python reference adapter per area: input -> output
  modbus/plan/*.json   Modbus block planning (D-005, D-006)
  modbus/decode/*.json Modbus register/bit decoding (D-006)
  scaling/*.json       tag scaling (linear, sqrt, clamps, negate)
  payload/*.json       SubDevice JSON payload bytes (D-017)
  ab_logix/plan/*.json   Allen-Bradley Logix/Micro800 read groups and packing (D-008, D-025)
  ab_logix/decode/*.json Logix values coerced to the configured dtype
  ab_pccc/plan/*.json    PCCC (SLC/MicroLogix) address parsing and file blocks (D-008, D-027)
  ab_pccc/decode/*.json  PCCC words, bits, floats, strings and T/C sub-elements
  config/              config model fixtures: an older, separate format, see config/compare.py
```

Generator: [`scripts/gen_conformance.py`](../../scripts/gen_conformance.py) (Allen-Bradley inputs
in [`scripts/gen_conformance_ab.py`](../../scripts/gen_conformance_ab.py)).
Python checks: [`tests/unit/test_conformance_vectors.py`](../unit/test_conformance_vectors.py) (every
vector, every area) and [`tests/unit/test_conformance_ab.py`](../unit/test_conformance_ab.py).

## File format

Every `*.json` file under an area directory has this shape (format 1):

```json
{
 "format": 1,
 "area": "modbus/plan",
 "description": "Block merging by function code, max_gap and overlaps (D-006).",
 "generated_by": "scripts/gen_conformance.py",
 "vectors": [
  {"id":"contiguous","description":"Adjacent float32, float32, uint16 merge ...","input":{...},"expected":{...}},
  ...
 ]
}
```

- `area` equals the file's directory relative to `tests/conformance/`.
- `id` is unique in its file and matches `[a-z0-9][a-z0-9_.-]*`. A vector's global key is
  `<file without .json>/<id>`, e.g. `modbus/plan/merge_and_gaps/contiguous`: use it in test names
  and failure messages, together with the `description`.
- One vector per line, so a behaviour change shows up as a one-line diff per affected vector.
- A loader must refuse a `format` it does not know rather than guess.

### Values

Tag values (decoded values, scaling inputs and outputs, payload snapshot values) use a tagged
encoding that keeps the exact type and bits. Plain JSON cannot tell `1` from `1.0`, has no NaN
or infinity, and many parsers lose integers above 2^53 or the last bit of a double.

| encoding                              | meaning                                                      |
|---------------------------------------|--------------------------------------------------------------|
| `{"t":"none"}`                        | no value                                                     |
| `{"t":"bool","v":true}`               | boolean                                                      |
| `{"t":"int","v":"-32768"}`            | integer as decimal text, any size (uint64 and int64 included)|
| `{"t":"float","v":"0.1"}`             | IEEE-754 double as Python `repr()`: the shortest text that parses back to the same double; also `"nan"`, `"inf"`, `"-inf"`, `"-0.0"`, `"1e+16"` |
| `{"t":"str","v":"text"}`              | string                                                       |
| `{"t":"list","v":[...]}`              | array tag; nested lists for 2-D arrays                       |
| `{"t":"decode_error","v":"reason"}`   | a per-tag decode failure (Python `DecodeError`)              |

The payload golden file `engine/crates/opencap-north/tests/golden/payload_cases.json` uses the
same encoding (without `decode_error`).

Numbers in configuration objects (`params`, `scale`, `cfg`) are plain JSON numbers. The
generator only writes ones that every JSON parser reads back exactly (at most 15 significant
digits, decimal exponent within ±22); anything that needs exact bits is a tagged value.

### Comparing

An engine compares its result with `expected` structurally: object keys unordered, arrays in
order, strings and numbers exact, encoded values by their text (so `-0.0` differs from `0.0`,
an int never equals a float, and `"nan"` equals `"nan"`). Every field of `expected` is part of
the contract, error and reason texts included: they are user-visible (plan report, UI, logs),
and the Rust port already mirrors them. If an engine cannot match a vector, that is a bug in
one engine; fix it, in Python too (separate issue) if Python is the wrong one. Never skip a
vector silently.

## Areas

Each area defines the `input` an adapter receives and the `expected` it must produce.
`reference.py` is the Python adapter for each and the executable definition of these rules.

### `modbus/plan`

Planning of a `modbus_tcp` device (`modbus_rtu` shares the same planner).

- `input`: `{"params": {...}, "tags": [{"name", "address", "dtype", "enabled"?}]}`.
  `params` is the device's `params` mapping exactly as it would appear in `devices.json`
  (unknown keys ignored, `null` = default, `0`/`1` accepted for booleans). Tags are config
  `Tag` objects; `enabled` defaults to `true`.
- `expected`, when the params are accepted:
  `{"blocks": [{"fc": 3, "start": 44, "count": 2, "tags": ["FLOW_1"]}], "invalid": {"TAG": "reason"}}`.
  `blocks` are in plan order (by function code, then start address); `start` is the protocol
  address on the wire, `count` registers (FC03/04) or bits (FC01/02); `tags` is the sorted list
  of names the block serves. `invalid` maps each tag that cannot be read to its reason.
  Disabled tags appear in neither.
- `expected`, when the params are refused (the driver cannot be built): `{"error": "reason"}`.

### `modbus/decode`

Decoding of one block reply.

- `input`: `{"params": {...}, "tags": [...], "reply": {"registers": [u16, ...]}}` or
  `... "reply": {"bits": [bool, ...]}}`. The tags always plan to exactly one block with no invalid
  tag: plan them as above, then decode that block's reply. Registers are unsigned 16-bit
  numbers in wire order (high byte first); bit replies may be padded to a multiple of 8, as
  client libraries return them. Replies may be shorter or longer than the block.
- `expected`: `{"values": {"TAG": <value>, ...}}` with one entry per tag of the block (aliases
  included): the decoded value, or a `decode_error` with its reason. No scaling is applied.

### `scaling`

`Scale` applied to one raw value, as the channel worker does after decoding.

- `input`: `{"scale": {"type", "raw_lo", "raw_hi", "eng_lo", "eng_hi", "clamp_lo", "clamp_hi",
  "negate", "units"}, "raw": <value>}`. Every scale field is given explicitly.
- `expected`: `{"value": <value>}`, or `{"error": "reason"}` when scaling fails (the worker
  then gives the tag quality `bad_decode`). Numbers are scaled; arrays element by element;
  bool, string and none pass through untouched (an array fails as a whole if one element fails).

### `payload`

The SubDevice JSON message for one device, byte for byte.

- `input`: `{"firm_version", "device", "values", "cfg", "is_secure", "now"}`.
  `device` is a config `Device` (fields at their default are omitted, as in `devices.json`);
  `values` maps tag name to `{"value": <value>, "quality": "good|bad_comm|bad_config|bad_decode|stale",
  "source_ts": "<ISO-8601 with offset>" | null}` (the cache snapshot); `cfg` is the full
  `PayloadCfg`; `now` (ISO-8601) is the time used when no tag is good.
- `expected`: `{"bytes": "..."}`, the UTF-8 text of the encoded payload. The message contains
  `"FirmVersion":"<firm_version>"`; an engine with another release number replaces that one
  field before comparing, as the payload golden test does.

### `ab_logix/plan`

Planning of an `ab_logix` device (ControlLogix/CompactLogix and Micro800, symbolic tags).

- `input`: `{"params": {...}, "tags": [...]}` as for Modbus. Params that matter: `micro800`
  (truth value), `max_batch` (names per read, default 40; `0`/`null` = default),
  `connection_size` (default 4000 B, 500 B on Micro800), plus `batching`, `slot` and `path`,
  which are only validated.
- `expected`: `{"blocks": [...], "invalid": {...}}`, or `{"error": "reason"}` when the params are
  refused (when the driver is built or the plan made). Each block is one `plc.read(*names)`:
  - `reads`: the distinct names in read order (sorted by code point), each with `data_size`
    (estimated reply bytes: dtype size, a `.n` bit's host as at least 4 B, the largest per name)
    and `path_size` (request path bytes);
  - `items`: `{"tag", "index", "bit"}`, the read each tag takes its value from and its `.n` bit,
    ordered by read then tag name;
  - `tags`, `micro800`, `connection_size`;
  - `wire_requests`: the estimated CIP round trips: one request per name on Micro800 or for a
    single name, otherwise Multiple Service Packets packed greedily by response size against
    the connection size, and fragmented reads for an entry larger than the connection.

### `ab_logix/decode`

- `input`: `{"params", "tags", "values"}`. The tags plan to exactly one block; `values` is a
  tagged value: the list `plc.read(*names)` returned, aligned with the block's `reads` (a
  `none` element: no value for that name), or `none` for no reply at all.
- `expected`: `{"values": {"TAG": <value>}}`, each tag coerced to its dtype (integers wrap to
  the dtype's width, `.n` bits taken from the integer) or a `decode_error`.

### `ab_pccc/plan`

Planning of an `ab_pccc` device (SLC 500, MicroLogix, PLC-5 data tables; `ab_df1` shares it).

- `input`: `{"params": {...}, "tags": [...]}`. Params: `max_elements` (100), `max_gap` (4),
  `request_size` (232 B, clamped to the 255-byte PCCC limit), plus `file_check` and `path`,
  which are only validated.
- `expected`: `{"blocks": [...], "invalid": {...}}` or `{"error": "reason"}`. Each block is one
  PCCC read: `address` (the read string, `N7:0{3}`), `file_type`, `file_number`, `slot` (I/O
  only, else null), `field` (T/C sub-element, else null), `start` (first element, or word in
  the slot), `count`, `elem_size`, `tags`, and `items` `{"tag", "offset", "bit"}` ordered by
  offset, bit (none first) and tag name.

### `ab_pccc/decode`

- `input`: `{"params", "tags", "value"}`. The tags plan to exactly one block; `value` is the
  tagged `.value` pycomm3 returned for its address: a scalar for one element, a list for `{n}`,
  or `none`. N/B/S/I/O words come back signed.
- `expected`: `{"values": {"TAG": <value>}}`: words masked or sign-extended to the dtype, bits
  taken from the whole word, T/C bits already extracted (any non-zero is true), or a
  `decode_error`.

## Regenerating

```bash
python scripts/gen_conformance.py            # rewrite the files from the current Python code
python scripts/gen_conformance.py --check    # exit 1 if a file is stale, missing or extra
```

The inputs live in the generator (directed cases, one behaviour each, plus seeded random
mixes); every `expected` is computed by `reference.py`. `tests/unit/test_conformance_vectors.py`
fails when the files and the Python code disagree, and also breaks the reference on purpose
(a flipped word order, a wrong bit gap, ...) to prove the vectors catch it. After a deliberate
behaviour change, regenerate, review the diff vector by vector, and name in the PR the engine
issue that ports the change. Never edit a vector file by hand.

Inputs use invented names and documentation addresses only (`Plant A/PLC1`, `192.0.2.x`).

## Consuming the vectors from another engine

1. Walk `tests/conformance/<area>/*.json` for each area you support, in sorted file order.
2. Parse each file, refuse an unknown `format`, check `area` and unique ids (`vectors.py` is
   the reference loader, ~100 lines).
3. Run your adapter for the area on `input`, compare with `expected` as described above.
4. Collect every failure and report its key, `description` and a diff, then fail once at the
   end, so one run shows the whole picture.

The Rust harness is engine work: Modbus, scaling and payload in #40; Allen-Bradley in #43.
Keep the loader generic (vector loader + per-area adapters) so later areas reuse it.
