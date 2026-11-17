# Configuration JSON Schema

Generated from the Pydantic models in `src/opencap/config/models.py` (the single source until
cut-over, [ADR-0003](../docs/adr/0003-shared-config-schema.md)). **Do not edit by hand.** Regenerate with

    python -m opencap.config.schema --out schema/

`tests/unit/test_config_schema.py` fails if the committed files differ from the models.

| File | Model | Document |
|---|---|---|
| `devices.schema.json` | `DevicesFile` | `config/devices.json` |
| `poller.schema.json` | `PollerConfig` | `config/poller.yaml` |
| `devices_map.schema.json` | `DeviceMap` | `config/devices_map.yaml` |
| `overrides.schema.json` | `Overrides` | `config/overrides.yaml` |

JSON Schema 2020-12; each file has `$id` and a top-level `version` (the schema contract version, bumped on a
non-additive config change together with a migration). Output is deterministic (sorted keys). Unknown keys are
rejected (`additionalProperties: false`), matching `extra="forbid"`. Driver `params` (and `publish`/`source`
dict overrides) are free-form objects here; per-driver schemas are future work (see `docs/driver_params.md`).

## Rules JSON Schema cannot express

Both engines must enforce these; each has an invalid fixture marked `"semantic": true`.

- **Device ids are unique across all channels** (`DevicesFile`) — `invalid/devices_duplicate_device_id_across_channels.json`.
- **Tag names are unique within a device** (`Device`) — `invalid/devices_duplicate_tag_names.json`.
- **Effective scan rate**: `effective_rate_ms(tag) = max(tag.scan_ms or device.scan_ms, device.min_scan_ms)`; tags
  with `enabled: false` are not polled. Exercised by `devices_scaled_tags.json` and `devices_publish_modes.json`.
- **`mqtt.topic_prefix`** is non-blank, has no `+`, `#` or NUL, does not start with `$`, and has no empty level —
  `invalid/poller_topic_prefix_*.json`.
- **Secrets are environment variables only**: config files carry the *name* of the variable
  (`mqtt.password_env`, `api.token_env`), never the secret.

## Conformance fixtures

`tests/conformance/config/valid/*.json` and `invalid/*.json`. Each file is
`{"document": "devices|poller|devices_map|overrides", "data": {...}}`; invalid files add `expected_error` (a
substring of the error message from the Python engine; other engines match on the failing field) and
`semantic` (true = the JSON Schema accepts it, only the semantic rules above reject it). All data is synthetic
(RFC 5737 addresses, `192.0.2.x`).
