# 06 Tags, tag groups and the alias map

> Source: KEPServerEX V6 server manual (© PTC), pages 95–107, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
A tag points at one address in a device. Static (user-defined) tags live in the server project and
can be browsed; dynamic tags are created on the fly when a client names a device address directly,
optionally with a data-type or rate suffix. A static tag has a name, description, driver address,
data type (from Boolean through 64-bit integers, floats, strings, BCD and dates), client access
(read only or read/write) and a scan rate used when the device honours tag rates. Scaling turns raw
values into engineering values (linear or square root, raw and scaled ranges, scaled data type,
clamps, negation). A multiple-tag generator builds many tags from an address template with static
text, numeric ranges and text sequences, with a preview and a naming scheme. Tag groups nest under a
device to organise the browse tree. The alias map gives short names to long `Channel.Device[.Group]`
paths, mainly for legacy and DDE-style clients, with an optional scan-rate override
(manual p. 95–107). The event log (p. 107–108) is in plan 12.

## opencap today
- **Model:** `Tag(name, address, dtype, scan_ms, scale, enabled, description)` and `Scale(type, raw_lo,
  raw_hi, eng_lo, eng_hi, clamp_lo, clamp_hi, negate, units)` in `src/opencap/config/models.py`; tag
  names are unique per device; groups are kept as dot-joined names (`Group.Sub.TAG`), flattened by the
  importer (`_iter_tags` in `src/opencap/config/kep_import.py`, README "How KEPServerEX settings map").
- **Types:** `bool`, `int16`…`uint64`, `float32`, `float64`, `string`, 1-D arrays through the address
  syntax; Char → `int16`, Byte → `uint16` (warning); BCD, LBCD and Date imported disabled; Default takes
  the device default (D-010, FR-2).
- **Scaling:** Kepware formulas in `Scale.apply`, element-wise on arrays, scaling error → `bad_decode`
  (D-015); the scaled data type is dropped at import (D-010). Both engines (#64).
- **Access:** no write path at all (ADR-0008); every tag is read-only by construction.
- **API/UI:** `GET/POST /api/v1/devices/{id}/tags` (POST takes one tag or a list),
  `GET/PUT/PATCH/DELETE …/tags/{tag}`; an address the driver cannot plan is refused with 422; tag
  table in the config editor; live values on the device page (#63). Tag fixes that must survive a
  re-import go in `overrides.yaml` `devices.<id>.tags`.
- Not present: dynamic tags, multiple-tag generator, explicit group objects, alias map, BCD/LBCD/Date
  decoders, scaled data type.

## Features

### TAG-01 Tag object, name and online edits (manual p. 95–96, 98)
- **What:** A tag has a name (up to 256 characters) that is unique inside its device or group branch
  and forms part of the browse path; tags are added, edited and removed while the server runs, and a
  client keeps its old reference until it re-acquires the item.
- **Decision:** Adapt — the tag name carries its group path (`Group.Sub.TAG`); edits go through the
  audited config API and take effect on reload of that channel only.
- **Status:** Done
- **Requirements:**
  - Duplicate names in one device are rejected by the model and by `POST …/tags` (422).
  - `POST /api/v1/reload` (or `?apply=true` on the write) restarts only the changed channel.
  - Renaming a tag moves its `overrides.yaml` tag override along (or answers 409 on a clash).
- **Acceptance:** `tests/unit/test_models.py`, `tests/unit/test_config_store.py`,
  `tests/unit/test_config_api.py`, `tests/integration/test_runtime_reload.py`.

### TAG-02 Address (manual p. 97)
- **What:** The tag's location in the device, in the driver's own syntax, checked by the driver when
  entered.
- **Decision:** Adopt — native KEPServerEX address syntax is kept per driver; the driver's pure
  `plan()` is the validator.
- **Status:** Done
- **Requirements:**
  - Addresses are documented per driver in the README "Addresses and data types" and
    `docs/driver_params.md`.
  - A tag whose address does not plan is refused by the API (422 with the field) and shown as
    `bad_config` by `opencap check` without any network traffic.
- **Acceptance:** address and planner unit tests per driver (`test_modbus_address.py`,
  `test_pccc_address.py`, `test_logix_plan.py`); planner conformance vectors (#3, #5) for Rust.

### TAG-03 Core data types (manual p. 97, 103)
- **What:** The data type says how the device data is interpreted (one or two registers, signed or
  unsigned, float, string); "Default" lets the driver choose.
- **Decision:** Adapt — opencap names (`int16`, `uint32`, `float32`, …); Default resolves to the
  device's default type at import; Char/Byte widen to 16 bits with a warning.
- **Status:** Done
- **Requirements:**
  - Every `DType` decodes identically in both engines; the payload golden file covers all of them.
  - The importer reports each widened or unsupported type in `reports/import_summary.json`.
- **Acceptance:** `tests/unit/test_modbus_decode.py`, `tests/unit/test_kep_import.py`, Rust payload
  golden test (`engine/crates/opencap-north/tests/payload_golden.rs`).

### TAG-04 BCD, LBCD, Date and 8-bit types (manual p. 97)
- **What:** Packed BCD in two or four bytes, an 8-byte OLE date, and signed/unsigned 8-bit integers.
- **Decision:** Adopt — FR-2 lists them as planned; tags of these types are imported disabled today.
- **Status:** Not started
- **Requirements:**
  - New `DType` values `int8`, `uint8`, `bcd16`, `bcd32`, `date` (additive schema change); decoders in
    each driver that supports them; invalid BCD nibbles → `bad_decode`.
  - The importer imports such tags enabled once the driver supports the type, and stops widening
    Char/Byte.
- **Acceptance:** decode conformance vectors (valid and invalid BCD, date epoch edges) for both
  engines; Modbus simulator round trip.

### TAG-05 Client access: read only (manual p. 97)
- **What:** Each tag is either read only or read/write for clients; the choice also changes how the
  tag appears to OPC UA clients.
- **Decision:** Adapt — read only is the only value (FR-2, ADR-0008). The planned `access` tag field
  (03 §3.1) will accept only `read_only`; OPC UA nodes get `AccessLevel = CurrentRead` (plan 02).
- **Status:** Done
- **Requirements:**
  - No driver has a write method (ADR-0004); `modbus_tcp`, `modbus_rtu`, `ab_logix`, `ab_pccc` and
    `ab_df1` refuse write services before they reach the wire (NFR-10).
  - `opencap import-kep` imports Read/Write tags as ordinary (read-only) tags; the CSV importer (#10)
    does the same with a warning.
- **Acceptance:** read-only guard tests per driver; Rust read-only test (#77).

### TAG-06 Client access: read/write (manual p. 97)
- **What:** Clients may change the tag's value in the device.
- **Decision:** Out of scope — PLC writes (ADR-0008); only RFC 0002 write safety (#19) may introduce it.
- **Status:** Out of scope

### TAG-07 Per-tag scan rate (manual p. 97–98)
- **What:** An update interval per tag (10 ms to about 27.8 h, default 100 ms), used when the device
  honours tag-specified rates, to read slow data less often.
- **Decision:** Adapt — `scan_ms` per tag (absent = device `scan_ms`), floored by the device's
  `min_scan_ms`; tags with the same effective rate share a rate group and are read together.
- **Status:** Done
- **Requirements:**
  - Effective rate `max(tag.scan_ms or device.scan_ms, min_scan_ms)`; a tag becomes `stale` after 3 ×
    that rate without a good read.
  - Overrides can change a tag's rate (`overrides.yaml` `tags.<name>.scan_ms`).
- **Acceptance:** `tests/unit/test_scheduler.py`, `tests/unit/test_cache.py`; Phase 1 acceptance on
  both engines.

### TAG-08 Description (manual p. 97)
- **What:** A text of up to 255 characters per tag, offered to clients as an item property.
- **Decision:** Adopt
- **Status:** Done
- **Requirements:**
  - `Tag.description` is imported, editable through the API and shown in the config editor.
  - Follow-up in plan 02: the OPC UA server sets each variable's `Description` from it (03 §3.5,
    #74, #50); property-style access is SYS-10 in plan 07.
- **Acceptance:** importer and API unit tests.

### TAG-09 Multiple tag generation (manual p. 98–101)
- **What:** A tool that builds many tags from one template: static text, numeric ranges in decimal,
  octal or hex with an increment (or one sized by the data type, so values do not overlap), text
  sequences, a shared data type and rate, a preview marking invalid tags, a name prefix with a
  counter, optional grouping and renumbering.
- **Decision:** Adapt — a pure generator function behind the CLI and the config API; the UI dialog is
  plan 14. Every generated address is checked by the driver's `plan()` before anything is saved.
- **Status:** Not started
- **Requirements:**
  - `generate_tags(template) -> (tags, invalid)` where the template has `name` (prefix + `{n}` counter
    with a start value), `address` parts (`text`, `range{from,to,step,base}`, `sequence[...]`), `dtype`,
    `scan_ms`, optional `group`; `step: auto` advances by the type's size in the driver's address units.
  - `opencap tags generate <device-id> template.yaml [--preview]` prints the tags and the invalid ones;
    `POST /api/v1/devices/{id}/tags:generate?dry_run=true` returns the same preview; with
    `dry_run=false` it adds the valid tags (audited).
- **Acceptance:** conformance vectors for the generator (ranges in each base, auto step for 32/64-bit
  types, sequences, naming) run by both engines; API test with dry run and apply.

### TAG-10 Scaling (manual p. 101–102)
- **What:** Linear or square-root scaling from a raw range to a scaled range, with optional clamping at
  either end and negation; the server publishes ranges as item properties.
- **Decision:** Adopt — same formulas and defaults (`Scale`), applied to good numeric values only.
- **Status:** Done
- **Requirements:**
  - `raw_hi == raw_lo` is reported at import and every read of that tag is `bad_decode`.
  - `scale.units` is imported and kept; follow-up in plan 02: units and the scaled range feed OPC UA
    `EngineeringUnits` and `EURange` (03 §3.5).
- **Acceptance:** scaling unit tests; scaling conformance vectors (#3) for both engines.

### TAG-11 Scaled data type (manual p. 102)
- **What:** The data type of the value after scaling (default Double), independent of the raw type.
- **Decision:** Adopt — today scaled values are always floats and the imported type is dropped (D-010).
- **Status:** Not started
- **Requirements:**
  - Optional `scale.dtype` (any numeric `DType`, default `float64`); the scaled value is converted with
    round-half-to-even and range checks (out of range → `bad_decode`).
  - The importer keeps the KEPServerEX scaled type instead of dropping it.
- **Acceptance:** scaling vectors with integer targets for both engines; payload golden cases.

### TAG-12 Static (user-defined) tags (manual p. 104)
- **What:** Tags defined in the server and browsable by clients; the usual way to configure data, and
  the only kind that can be scaled.
- **Decision:** Adopt — every opencap tag is a static, configured tag.
- **Status:** Done
- **Requirements:**
  - Every enabled tag appears in `GET /api/v1/devices/{id}/values` and in the MQTT payload (`Data`, or
    `BadTags` when not good); the OPC UA address space follows in plan 02 (#74, #50).
- **Acceptance:** Phase 1 acceptance on both engines.

### TAG-13 Dynamic tags (manual p. 103–104)
- **What:** A client names a device address directly (`Channel.Device.<address>`, optional `@type`),
  and the server creates a temporary tag and starts scanning it.
- **Decision:** Defer — M5, off by default, because it lets clients create load on PLCs (FR-2). The
  DDE `@rate` form is not offered (DDE is out, ADR-0009).
- **Status:** Not started
- **Requirements:**
  - Device param `dynamic_tags: false` (default); when true, an OPC UA read or subscription of an
    unknown `Channel.Device.<address>[@type]` creates a runtime-only, read-only tag after `plan()`
    accepts the address; at most `dynamic_tags_max` per device; never written to the project.
  - Dynamic tags are not browsable; they are dropped when the last client releases them.
- **Acceptance:** OPC UA client test against the Modbus simulator: create, read, release; limit
  enforced; nothing reaches the wire when disabled.

### TAG-14 Tag groups in the browse tree (manual p. 104–105)
- **What:** Groups nest from the device level down to organise tags (for example one group per
  machine cell), so identical tag sets can sit side by side; the full path is
  `Channel.Device.Group.Sub.Tag`.
- **Decision:** Adapt — the group path is part of the tag name; north servers build folders from the
  dotted path (03 §3.5).
- **Status:** Done
- **Requirements:**
  - The importer flattens nested groups into `Group.Sub.TAG` names; two tags with the same name in
    different groups stay distinct.
  - Follow-up in plan 02: the OPC UA server shows each group level as a folder (03 §3.5, #74, #50).
- **Acceptance:** `tests/unit/test_kep_import.py` (nested groups).

### TAG-15 Tag group objects in the API and UI (manual p. 104–105)
- **What:** Groups are objects of their own: added, renamed and removed as a unit; new groups are
  browsable at once.
- **Decision:** Adapt — the planned explicit `group` field (03 §3.1, with a migration) or group routes
  on top of dot-joined names; renaming a group renames every tag under it in one audited change.
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/devices/{id}/groups` lists groups with tag counts; `PATCH …/groups/{path}`
    (`{"name": …}`) renames a group and moves its tag overrides; `DELETE …/groups/{path}` deletes the
    group's tags (409 unless `?recursive=true`).
  - The config editor shows tags as a tree.
- **Acceptance:** API unit tests for rename and delete; UI smoke test of the tree.

### TAG-16 Alias map (manual p. 105–107)
- **What:** Short alias names that stand for a `Channel.Device[.Group]` path, created automatically
  for older projects and by users, with a description, export/import, and a rate override for
  non-OPC clients.
- **Decision:** Defer — no migrated SCADA is known to depend on aliases; first make the importer
  report them. The rate override is not offered (it serves DDE-style clients, ADR-0009).
- **Status:** Not started
- **Requirements:**
  - `opencap import-kep` lists user-defined alias entries (name, target path) in
    `reports/import_summary.json`.
  - If adopted later: `aliases: {<name>: "<Channel>.<Device>[.<Group>]"}` in the project; the OPC UA
    server exposes each alias as an extra folder referencing the same nodes (no copies, no second
    poll).
- **Acceptance:** importer unit test with a synthetic alias map; OPC UA browse test when adopted.

## Open questions
- Do any SCADA projects being migrated address tags through KEPServerEX aliases (TAG-16)? If yes,
  aliases move from Defer to M3 with the OPC UA server.
- Explicit groups (TAG-15): add a `group` field with a config migration, or keep dot-joined names and
  add group routes only?
- Default per-tag rate: KEPServerEX uses 100 ms, opencap falls back to the device `scan_ms`
  (1000 ms). Keep the opencap default?
- Should dynamic tags (TAG-13) exist at all, given they let any client add PLC load?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| TAG-01 | Tag object, name and online edits | Adapt | Done | #63 | M1 |
| TAG-02 | Address | Adopt | Done | #63, #64 | M2 |
| TAG-03 | Core data types | Adapt | Done | #64 | M2 |
| TAG-04 | BCD, LBCD, Date and 8-bit types | Adopt | Not started | — | — |
| TAG-05 | Client access: read only | Adapt | Done | #77 (Rust guard) | M2 |
| TAG-06 | Client access: read/write | Out of scope | Out of scope | #19 | — |
| TAG-07 | Per-tag scan rate | Adapt | Done | #64 | M2 |
| TAG-08 | Description | Adopt | Done | — (OPC UA: #74, #50) | M1 |
| TAG-09 | Multiple tag generation | Adapt | Not started | — | — |
| TAG-10 | Scaling | Adopt | Done | #64; vectors #3 | M2 |
| TAG-11 | Scaled data type | Adopt | Not started | — | — |
| TAG-12 | Static (user-defined) tags | Adopt | Done | — | M1 |
| TAG-13 | Dynamic tags | Defer | Not started | — | M5 |
| TAG-14 | Tag groups in the browse tree | Adapt | Done | — (OPC UA: #74, #50) | M1 |
| TAG-15 | Tag group objects in the API and UI | Adapt | Not started | — | — |
| TAG-16 | Alias map | Defer | Not started | — | — |
