# opencap

Open-source, **read-only** PLC polling server that replaces KEPServer. It polls PLCs over the
site network (VPN) and publishes one SubDevice JSON message per device per scan to MQTT.

Status: **Phase 3** — core polling engine; Modbus TCP, Allen-Bradley Logix/Micro800,
Allen-Bradley PCCC (SLC/MicroLogix), Siemens S7 and Mitsubishi MC 3E drivers; MQTT with
store-and-forward; admin API and metrics. OPC UA arrives in Phase 4. Devices whose driver is not
ready are skipped and reported, never polled.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,modbus,ab,s7,melsec,north]'
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
confirmed), `overrides.yaml` (hand fixes). Secrets come from environment variables:
`OPENCAP_MQTT_PASSWORD`, `OPENCAP_API_TOKEN`, `OPENCAP_OPCUA_<CHANNEL>_USER/_PASSWORD`.

`SIGHUP` or `POST /reload` re-reads the config and restarts only the channels that changed.

## Admin API (bind to localhost or the VPN only)

`GET /health`, `GET /metrics` (open) · `GET /channels`, `GET /devices/{id}`,
`GET /devices/{id}/values`, `POST /devices/{id}/read-once`, `POST /reload`
(`Authorization: Bearer $OPENCAP_API_TOKEN`). Device ids are `<channel>/<device>`.

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

## Commissioning one real S7 or Mitsubishi site (needs site access)

1. `opencap identity "SITE S7/PLC"` — CPU module and order code (SZL), firmware, run/stop, and
   the PDU the CPU negotiated (`pdu_negotiated`). If it is below `pdu_requested`, set
   `params.max_pdu` to it in `overrides.yaml` so blocks are planned to fit (D-031). A connect
   that fails during the ISO handshake may mean the link type: try `params.link_type: 1` (PG),
   D-030.
2. `opencap identity "SITE MELSEC/STP"` — CPU model from "read CPU model". It confirms that the
   importer's `plc_type: Q` guess talks to this PLC (D-034); check `protocol` (TCP/UDP) too.
3. `opencap read ...` — compare with KEPServer's Quick Client, especially S7 timers/counters
   (ms / BCD) and MELSEC 32-bit values (`first_word_low`).

## Tests

```bash
.venv/bin/pytest -m "not acceptance"                      # fast suite (~30 s)
.venv/bin/pytest -m acceptance                            # 30 simulated devices + broker outage
OPENCAP_OUTAGE_S=600 .venv/bin/pytest -m acceptance        # the plan's 10-minute broker outage
```

Tests marked `real_project` need the real KEPServer export at `tests/fixtures/project.json`
(git-ignored, it holds site IPs and credentials) and are skipped without it.

See `docs/decisions.md` for every place the code differs from the build plan.
