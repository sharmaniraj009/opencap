# opencap

Open-source, **read-only** PLC polling server that replaces KEPServer. It polls PLCs over the
site network (VPN) and publishes one SubDevice JSON message per device per scan to MQTT.

Status: **Phase 3** — core polling engine; Modbus TCP, Allen-Bradley Logix/Micro800 and
Allen-Bradley PCCC (SLC/MicroLogix) drivers; MQTT with store-and-forward; admin API and
metrics. S7, Mitsubishi and OPC UA arrive in Phase 4. Devices whose driver is not ready are
skipped and reported, never polled.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,modbus,ab,north]'
```

## Commands

```bash
.venv/bin/opencap import-kep path/to/project.json          # config/devices.json + reports/
.venv/bin/opencap check [--site NAME] [-v]                 # block plan + estimated cycle time
.venv/bin/opencap read "Site 008/Device1" [--tag FLOW_1]   # one read, print values, no MQTT
.venv/bin/opencap identity "Site 008/Device1"              # vendor/model if the device reports it
.venv/bin/opencap run [--site NAME ...]                    # poll, publish, serve the API
```

Config lives in `config/`: `poller.yaml` (MQTT, outbox, API, defaults), `devices.json`
(generated), `devices_map.yaml` (SubDeviceId/topic per device — generated stubs must be
confirmed), `overrides.yaml` (hand fixes). The last three can also be edited in the web UI or
through the configuration API (below). Secrets come from environment variables:
`OPENCAP_MQTT_PASSWORD`, `OPENCAP_API_TOKEN`, `OPENCAP_OPCUA_<CHANNEL>_USER/_PASSWORD`.

`SIGHUP` or `POST /reload` re-reads the config and restarts only the channels that changed.

## Admin API (bind to localhost or the VPN only)

`GET /health`, `GET /metrics` (open) · `GET /channels`, `GET /devices/{id}`,
`GET /devices/{id}/values`, `GET /devices/{id}/plan`, `GET /devices/{id}/identity`,
`POST /devices/{id}/read-once`, `POST /reload` (`Authorization: Bearer <token>`, see below).
Device ids are `<channel>/<device>`.

## Web UI and configuration API

The same server (`api.bind`:`api.port`, default `127.0.0.1:8080`) serves an operator UI at
**`/ui`** and a configuration REST API, the open equivalent of KEPServerEX's Configuration API.
No Node build step: server-rendered HTML with a vendored copy of htmx.

**Tokens and roles.** Name your tokens in `poller.yaml`; each secret lives in an env var:

```yaml
api:
  tokens:
    - {name: alice, role: admin, token_env: OPENCAP_TOKEN_ALICE}      # may change the config
    - {name: grafana, role: viewer, token_env: OPENCAP_TOKEN_GRAFANA} # read-only (GET)
  audit_log: data/audit.jsonl
```

`viewer` may read everything (status, values, config, `/check`, export, audit); `admin` may also
change the config, import, reload and read-once. The older `api.token_env` token still works as
the admin named `default`. Open `http://127.0.0.1:8080/ui` and log in with a token (stored as an
HttpOnly session cookie). The UI works at phone width.

**UI pages.** Dashboard (sites and devices: ok/offline/demoted, last good, errors, cycle time,
MQTT link, outbox size) · device page (live values and quality, refreshing every 2 s; block plan;
`bad_config` tags; identity; read once) · config editor (channels, devices, tags, publish map,
overrides; validation errors next to the field; Reload) · check (block plan and cycle estimates)
· import (KEPServer `project.json` or an opencap export, with a dry-run diff) · audit log.
The UI never writes a PLC value.

**Configuration API** (JSON; `PUT` replaces, `PATCH` is a JSON merge patch where `null` removes a key):

| | |
|---|---|
| `GET /config` | revision, counts, warnings, drivers, and whether a reload is pending |
| `GET/POST /config/channels` · `GET/PATCH/DELETE /config/channels/{channel}` | channels |
| `GET/POST /config/channels/{channel}/devices` · `GET/PUT/PATCH/DELETE …/devices/{device}` | devices |
| `GET/POST …/devices/{device}/tags` · `GET/PUT/PATCH/DELETE …/tags/{tag}` | tags (POST takes one or a list) |
| `GET /config/map` · `PUT /config/map/defaults` · `GET/PUT/PATCH/DELETE /config/map/devices/{id}` | `devices_map.yaml` |
| `GET /config/overrides` · `GET/PUT/PATCH/DELETE /config/overrides/devices/{id}` · `GET/PUT/DELETE /config/overrides/channels/{channel}` | `overrides.yaml` |
| `GET /config/export` · `POST /config/import[?dry_run=false]` | whole config as JSON; import a KEPServer `project.json` or an export (dry run by default) |
| `GET /check[?site=…&verbose=true]` · `GET /audit[?action=…&who=…&limit=…]` · `GET /config/schema` | plan and estimates, audit log, JSON schemas |

```bash
TOKEN=$OPENCAP_TOKEN_ALICE
curl -s -H "Authorization: Bearer $TOKEN" -X PATCH -H 'Content-Type: application/json' \
  -d '{"timeout_ms": 1500}' "http://127.0.0.1:8080/config/channels/Site%20008/devices/Device1"
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @project.json "http://127.0.0.1:8080/config/import?filename=project.json"   # dry run
curl -s -H "Authorization: Bearer $TOKEN" -X POST http://127.0.0.1:8080/reload
```

Every change is checked by its model, by the loader and by the device's driver (a tag address the
driver cannot read is refused) before anything is written; a 422 lists each error with its field.
Files are written atomically and the previous version is kept as `<file>.bak`; comments in the
hand-owned `devices_map.yaml` and `overrides.yaml` are kept. Changes take effect on
`POST /reload` (only changed channels restart). Responses carry the config `revision` (also as
`ETag`); send it back as `If-Match` to refuse a change when someone else changed the config first.
Renaming a device or channel moves its map entry and override along; deleting one deletes them.
Channels, devices and tags live in `devices.json`, which a KEPServer re-import replaces: put fixes
that must survive a re-import in the device's override. Every accepted change, import and reload
is in the audit log (who, when, what). Details: `docs/decisions.md` D-028 to D-034.

## Commissioning one real Modbus site (Phase 1 acceptance, needs site access)

1. `opencap check --site "Site 008" -v` — confirm the block plan.
2. `opencap read "Site 008/Device1"` — compare every value with KEPServer's Quick Client.
3. Shadow test per PLAN §12: in `overrides.yaml` set the device's `min_scan_ms: 30000`
   (imported tags carry their own 100 ms KEPServer rate, so `scan_ms` alone does not slow
   polling), point `poller.yaml` at a test broker, then `opencap run --site "Site 008"` while
   KEPServer still runs; compare for 1 hour.

## Commissioning one real Allen-Bradley site (Phase 2 acceptance, needs site access)

1. `opencap identity "Site 001/8 PUMP"` — product, firmware, and whether the Micro800
   accepts batched reads (`batching` + `batching_reason`). This decides the real cycle time.
2. `opencap read "Site 001/8 PUMP"` — compare with KEPServer's Quick Client. Tags the PLC
   does not have show `bad_config`.
3. Shadow test as for Modbus (`min_scan_ms: 30000`), but only if the PLC has a free CIP
   connection (Micro800s allow few); otherwise go straight to the cut-over (PLAN §12).
4. Watch for 24 h: `device.stats` log lines (cycle times every 5 min) and `/metrics`
   (`poll_requests_total{result="conn_error"}` should stay flat).

## Commissioning one real PCCC site (SLC/MicroLogix; Phase 3 acceptance, needs site access)

1. `opencap identity "Site 004/7 PUMP"` — processor type and whether the PLC's file
   directory was read and trusted (`file_directory`, `files`). If it was, blocks for files the
   PLC does not have are rejected before they go on the wire.
2. `opencap read "Site 004/7 PUMP"` — compare with KEPServer's Quick Client.
3. Known: `F54:17` on Site 092/10 PUMP clashes with `N54` and shows `bad_config`; the
   4 `RTC:0.HR/.MIN` tags are not readable through pycomm3.

## Tests

```bash
.venv/bin/pytest -m "not acceptance"                      # fast suite (~30 s)
.venv/bin/pytest -m acceptance                            # 30 simulated devices + broker outage
OPENCAP_OUTAGE_S=600 .venv/bin/pytest -m acceptance        # the plan's 10-minute broker outage
```

Tests marked `real_project` need the real KEPServer export at `tests/fixtures/project.json`
(git-ignored, it holds site IPs and credentials) and are skipped without it.

See `docs/decisions.md` for every place the code differs from the build plan.
