# 0003. JSON Schema generated from the Pydantic models is the cross-language config contract

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead

## Context
Two engines must read the same configuration. The Python Pydantic models
(`src/opencap/config/models.py`) are the de-facto contract today: `extra="forbid"`,
validators for unique ids and tag names, defaults that match KEPServerEX. The config API and
UI (`web-ui` PR) need a machine-readable schema for validation and form generation.

## Decision
- The Pydantic models are the **single source** until cut-over. JSON Schema files are
  generated from them (`DevicesFile`, `PollerConfig`, `DeviceMap`, `Overrides`, and the
  project v2 document once [ADR-0013](0013-project-file-v2.md) lands) into `schema/`,
  committed, and drift-tested in CI (a test regenerates and compares).
- Each document carries `version` (already in `devices.json`). Additive changes (new
  optional field with a default) keep the version; anything else bumps it and ships a
  migration (`opencap config migrate`) plus fixtures of both versions.
- Semantic rules that JSON Schema cannot express (unique ids across channels, rate rules)
  are listed in `schema/README.md` and have conformance fixtures in
  `tests/conformance/config/` (valid and invalid documents with the expected error),
  which both engines must pass.
- The Rust engine generates or hand-writes `serde` types from the schema, with
  `deny_unknown_fields` to match `extra="forbid"`, and runs the same fixtures.
- Driver `params` stay a free-form object in the schema; each driver publishes its own
  params schema (`schema/drivers/<driver>.schema.json`, from `docs/driver_params.md`).

## Consequences
- A config change is a Python model change first; the schema diff in the PR makes the
  contract change visible to the reviewer.
- UI forms and API validation come for free from the same files.

## Alternatives considered
- Hand-written JSON Schema as the source (two sources while Python validates with Pydantic).
- Protobuf for config (poor fit for hand-edited YAML/JSON, no defaults semantics).
- Rust types as the source (Rust engine not on `main` yet; Python would lose its validator).

## Revisit when
Cut-over (M4): the source of truth moves to the Rust types, and Python validates against
the committed schema instead.
