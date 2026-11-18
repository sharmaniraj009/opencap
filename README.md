# opencap

[![CI](https://github.com/sharmaniraj009/opencap/actions/workflows/ci.yml/badge.svg)](https://github.com/sharmaniraj009/opencap/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)

**opencap** is an open-source industrial connectivity server, written to replace
KEPServerEX. It polls PLCs over their native protocols and publishes one JSON message per
device to an MQTT broker. Messages are stored and forwarded, so a broker outage loses no data.
It imports an existing KEPServerEX project, so you do not re-enter thousands of tags by hand.

It is **read-only by design**: no code path writes to a PLC.

- Asyncio Python 3.12+. One process, one config directory, JSON logs on stdout.
- KEPServerEX polling semantics: one request on the wire per channel, retries, reconnect
  backoff, auto-demotion, stale detection and tag scaling.
- MQTT QoS 1 through a SQLite outbox. Every message is written to disk first and deleted
  only after the broker's PUBACK, so it survives broker outages and restarts.
- `opencap check` validates the config, prints each device's block plan, and estimates its
  cycle time before anything goes on the wire.
- Admin REST API, Prometheus metrics, and config reload on SIGHUP without dropping
  unchanged channels.

Status: the core, Modbus TCP, both Allen-Bradley drivers, the serial drivers (DF1, Modbus
RTU), Siemens S7, Mitsubishi MC, the OPC UA client driver and the OPC UA server for SCADA are
done (S7, MC and OPC UA still need a check on real equipment). See [ROADMAP.md](ROADMAP.md).

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Driver support](#driver-support)
- [Migrating from KEPServerEX](#migrating-from-kepserverex)
- [Configuration reference](#configuration-reference)
- [MQTT topics and payload](#mqtt-topics-and-payload)
- [Secrets and environment variables](#secrets-and-environment-variables)
- [Admin API and metrics](#admin-api-and-metrics)
- [Web UI and configuration API](#web-ui-and-configuration-api)
- [Deployment](#deployment)
- [Testing](#testing)
- [Contributing, security, license](#contributing-security-license)

## Quick start

opencap is not on PyPI yet. Install it from a clone (or with
`pip install "opencap[modbus,ab,north] @ git+https://github.com/sharmaniraj009/opencap"`):

```bash
git clone https://github.com/sharmaniraj009/opencap && cd opencap
python3.12 -m venv .venv
.venv/bin/pip install -e '.[modbus,ab,north]'      # add ,dev,serial for the test suite
```

Extras choose the drivers: `modbus`, `ab` (Allen-Bradley), `s7`, `melsec`, `opcua`, plus
`north` (MQTT, API, metrics; needed for `opencap run`) and `serial` (pyserial: local serial
ports and RFC 2217; serial-over-TCP gateways need nothing extra). `all` installs everything.

```bash
# 1. Import a KEPServerEX project (File > Save As > .json in the KEPServerEX Configuration)
.venv/bin/opencap import-kep path/to/project.json
#    -> config/devices.json, config/devices_map.yaml, config/overrides.yaml,
#       reports/import_summary.json, reports/same_address.csv

# 2. Edit config/poller.yaml (broker, instance name) and config/devices_map.yaml (SubDeviceIds)

# 3. Validate: block plan and estimated cycle time per channel, no network traffic
.venv/bin/opencap check            # --site "Plant A" for one channel, -v for every block

# 4. Read one device once and print its values (no MQTT)
.venv/bin/opencap read "Plant A/PLC1"            # --tag FLOW_1 for one tag
.venv/bin/opencap identity "Plant A/PLC1"        # vendor, model, firmware if the PLC reports them

# 5. Run: poll, publish to MQTT, serve the admin API
export OPENCAP_MQTT_PASSWORD=...    # only if mqtt.username is set
.venv/bin/opencap run              # --site "Plant A" (repeatable) polls only those channels
```

Every command takes `--config DIR` (default `./config`). Device ids are
`<channel>/<device>`, exactly as in KEPServerEX.

With Docker: `docker build -t opencap .`, then see [docs/deployment.md](docs/deployment.md).

## Architecture

```
                 config/  poller.yaml  devices.json  devices_map.yaml  overrides.yaml
                                            |
                                  load_config(): validate, layer overrides, derive topics
                                            v
 +-------+   native    +--------------------------------+          +------------+
 | PLCs  | <---------- | ChannelWorker (one per channel)|  values  | ValueCache |
 |       |  protocol   |  one request on the wire       | -------> | value,     |
 |       |             |  retries, backoff, demotion    |          | quality,   |
 +-------+             |  stale marking, scaling        |          | timestamp  |
                       +---------------+----------------+          +-----+------+
                                       | Driver interface                |
                       +---------------+----------------+                v
                       | drivers/: plan() (pure)        |          +-----------+
                       |  connect / read / close        |          | Publisher |  interval or
                       |  modbus_tcp, ab_logix, ab_pccc |          |           |  on_change + heartbeat
                       +--------------------------------+          +-----+-----+
                                                                         v
 +-------------------------------+                              +------------------+
 | Admin API (FastAPI)           |                              | SQLite outbox    |  store-and-forward:
 |  /health /metrics /channels   |                              | (data/saf.sqlite)|  written first,
 |  /devices/... /reload         |                              +--------+---------+  deleted on PUBACK
 +-------------------------------+                                       v
                                                                +------------------+     +-------------+
 Prometheus metrics, JSON logs on stdout                        | MqttLink (QoS 1) | --> | MQTT broker |
                                                                |  LWT online/off  |     +-------------+
                                                                +------------------+
```

- **Config** (`src/opencap/config`): pydantic models, the KEPServerEX importer, and the
  loader that layers `overrides.yaml` and `devices_map.yaml` over `devices.json`.
- **Core** (`src/opencap/core`): the registry builds each device's block plan through its
  driver's pure `plan()`. One `ChannelWorker` per channel runs the devices' rate groups.
  Devices on a channel take turns, one request at a time. The worker also owns retries,
  backoff and demotion. `opencap check` uses the same planner without opening sockets.
- **Drivers** (`src/opencap/drivers`): every protocol lives behind `Driver` in `base.py`.
  The core never imports a protocol library.
- **North** (`src/opencap/north`): payload builder, SQLite outbox, MQTT link and publisher.
- **API** (`src/opencap/api`) and **metrics** (`src/opencap/metrics.py`).

Design decisions and every deviation from the original build plan are in
[docs/decisions.md](docs/decisions.md).

The production engine is moving to Rust ([ADR-0002](docs/adr/0002-rust-engine-python-reference.md)):
[`engine/`](engine/README.md) reads the same config, serves the same API and publishes the same
payload bytes; design in [RFC 0001](docs/rfc/0001-rust-engine.md). This Python code stays the
reference implementation.

## Driver support

| `driver` | Protocol | Devices | KEPServerEX driver it replaces | Extra | Status |
|---|---|---|---|---|---|
| `modbus_tcp` | Modbus TCP | any Modbus TCP server or gateway | Modbus TCP/IP Ethernet | `modbus` (pymodbus) | ✅ done |
| `ab_logix` | EtherNet/IP, CIP symbolic | ControlLogix, CompactLogix, Micro800 | Allen-Bradley ControlLogix Ethernet (Logix models), Allen-Bradley Micro800 Ethernet | `ab` (pycomm3) | ✅ done |
| `ab_pccc` | EtherNet/IP, PCCC | SLC 500, MicroLogix | Allen-Bradley ControlLogix Ethernet (data-table model) | `ab` (pycomm3) | ✅ done |
| `s7` | S7comm (ISO-on-TCP) | S7-300/400/1200/1500 | Siemens TCP/IP Ethernet | `s7` (python-snap7) | ✅ done (field check to do) |
| `melsec_mc` | MELSEC MC protocol | Mitsubishi Q/L/iQ-R | Mitsubishi Ethernet | `melsec` (pymcprotocol) | ✅ done (field check to do) |
| `opcua_client` | OPC UA (subscriptions) | any OPC UA server | OPC UA Client | `opcua` (asyncua) | ✅ done (field check to do) |
| `modbus_rtu` | Modbus RTU (serial, or RTU over TCP) | serial Modbus slaves | Modbus RTU Serial (configured by hand) | `modbus` (+ `serial` for a local port) | ✅ done |
| `ab_df1` | Allen-Bradley DF1 full duplex | SLC 500, MicroLogix | Allen-Bradley DF1 | none (+ `serial` for a local port) | ✅ done |
| `simulator` | none: values computed from the address | demos, the UI, desktop tests, benchmarks | Simulator (its devices are not imported) | none | ✅ done |
| OPC UA server | OPC UA (north side) | SCADA/HMI clients | KEPServerEX's OPC UA server | `opcua` (asyncua) | ✅ done, off by default (hardening: #28) |

Addresses and data types:

- **Modbus TCP:** `0xxxxx` coils, `1xxxxx` discrete inputs, `3xxxxx` input registers,
  `4xxxxx` holding registers, bits in registers (`400001.5`), strings (`400010.20H`).
  Types: bool, int16, uint16, int32, uint32, int64, uint64, float32, float64, string.
  Word, dword and byte order and zero-based addressing follow the KEPServerEX device settings.
- **Allen-Bradley Logix/Micro800:** symbolic tag names (atomic types, arrays, UDT members).
  Micro800 reads are batched (Multiple Service Packet) when the PLC accepts them; this is
  probed on connect.
- **Allen-Bradley PCCC:** data-table addresses (`N7:0`, `F8:3`, `B3:2/14`, `L9:0`,
  `T4:0.ACC`, `C5:1.PRE`, `S:1`, `ST10:0`). The PLC's file directory is read once, so blocks
  for missing files are rejected before they reach the wire.
- **Serial lines (`ab_df1`, `modbus_rtu`):** `params.transport` is `tcp` (raw TCP to a
  serial-over-TCP gateway), `serial` (a local port; `host` is the device path, e.g.
  `/dev/ttyUSB0`) or `rfc2217` (DF1 only). DF1 uses the PCCC addresses above and the same
  block plan; its own link layer does BCC/CRC, ACK/NAK/ENQ and retries (full duplex only).
  Modbus RTU uses the Modbus TCP addresses and params. Devices on one line (stations, unit
  ids) share one connection.
- **Siemens S7:** the KEPServerEX Siemens TCP/IP Ethernet forms (`MW10`, `MD232`, `MB4`,
  `M10.3`, `I0.0`, `Q1.2`, `DB100,D4`, `DB100,REAL8`, `DB5,X2.1`, `T3`, `C7`) and the Siemens
  `DB100.DBD4` spelling. Reads in one area and DB are merged into blocks that fit the PDU; if
  the CPU negotiates a smaller PDU, reads are split. Timers read as milliseconds (S5TIME),
  counters as BCD. Only setup, read-var and SZL requests are ever sent (D-048, D-051).
- **Mitsubishi MC (3E frame, binary):** the KEPServerEX Mitsubishi Ethernet Q-series forms
  (`D100`, `D0000002`, `W1A`, `R10`, `ZR100`, bits `M`, `X`, `Y`, `B`, bits in words `D10.3`,
  strings such as `DSH200.020`), TCP or UDP. Batch reads of up to 960 words. Only read requests are ever sent
  (D-053, D-055).
- **OPC UA client:** addresses are NodeIds (`ns=7;s=LTI002_PV`, `i=2258`, `nsu=<uri>;s=...`).
  Devices subscribe to their nodes instead of polling them, and values carry the server's
  SourceTimestamp (D-045). Credentials come only from environment variables named in
  `params.username_env` / `password_env`. A password is never sent over security policy `None`
  unless `params.allow_insecure_password: true`. Write, call and node-management services are
  refused inside the driver, even though the library offers them.
- **Simulator:** `RAMP(period_ms, low, high[, step])`, `SINE(period_ms, low, high[, phase_deg])`,
  `RANDOM(period_ms, low, high[, seed])`, `SQUARE(period_ms, low, high)`, `CONST(value)`,
  memory registers `K<n>` (`params.memory`), `TIME`, `COUNTER`. No network; deterministic for a
  given clock, `params.epoch` and seed, so both engines return the same values at the same
  instant. Fault switches `fail_connect`, `fail_every_n_reads`, `latency_ms` and
  `bad_decode_tags` make every quality visible in a demo (D-066).

### OPC UA server for SCADA

`poller.yaml` → `opcua_server` (off by default) serves the cache read-only with KEPServerEX's
NodeIds, `ns=2;s=<channel>.<device>[.<group>].<tag>` (tag groups are folders), plus
`<channel>.<device>._System._Error`, `_State`, `_LastGood`, ... per device. An existing SCADA
client changes only its endpoint URL. Every write, method call and node change is refused
(D-046). Secure by default:

- anonymous clients are refused unless `anonymous: true`; set one user with
  `username_env` / `password_env` (environment variable names, never values);
- security policy `None` (plaintext) is refused on an endpoint that is not loopback, unless
  `allow_insecure_remote: true`. For remote SCADA, offer only
  `Basic256Sha256_SignAndEncrypt` (and the `Aes*` policies) with a `certificate` and
  `private_key`;
- if the server cannot start, polling and MQTT carry on, and `/health` reports
  `opcua_server.running: false` with the error.

Per-driver parameters (`Device.params`) and their KEPServerEX sources:
[docs/driver_params.md](docs/driver_params.md).

Devices whose driver is not ready are imported and reported by `opencap check`, but never
polled. A device's status says why. Checks to run on the first real S7 or Mitsubishi PLC:
[docs/deployment.md](docs/deployment.md) ("Commissioning a Siemens S7 or Mitsubishi PLC").

## Migrating from KEPServerEX

opencap reads the KEPServerEX **project file**. It does not talk to a running KEPServerEX.
Both can run side by side during the migration, as long as the PLC accepts the extra
connections.

### 1. Export and import

1. In the KEPServerEX Configuration, use **File > Save As**, choose **JSON**, and save
   `project.json`. Treat this file as secret: it holds PLC IP addresses and may hold
   credentials. Never commit it or attach it to an issue.
2. `opencap import-kep project.json` writes:
   - `config/devices.json`: every included channel, device and tag (regenerated on each import).
   - `config/devices_map.yaml`: a **stub** SubDeviceId per device (`stub: true`). Existing
     entries are never overwritten; only new devices are appended.
   - `config/overrides.yaml`: an empty template, written only if the file is missing.
   - `reports/import_summary.json`: counts, excluded devices, and every problem found.
   - `reports/same_address.csv`: tags that read the same address, classified as `alias` or
     `suspect` (a likely copy-paste error in the project).
3. Read the summary. It lists data-type conversions (Char → int16, Byte → uint16), tags
   imported disabled (BCD, LBCD, Date), devices excluded and why (Simulator, U-CON, MQTT
   Client, data collection disabled), and OPC UA credentials you must provide as
   environment variables.

### 2. How KEPServerEX settings map

| KEPServerEX | opencap |
|---|---|
| Channel | channel (`devices.json`), one `ChannelWorker`; one request on the wire at a time |
| Device | device, id `<channel>/<device>` |
| Tag group path | dot-joined tag name (`Group.Sub.TAG`) |
| Tag address / data type | `address` / `dtype` (native address syntax kept) |
| Tag scan rate | `scan_ms` per tag; tags with the same rate form a rate group |
| Scan mode "Request data no faster than" | device `min_scan_ms` |
| Scan mode "Request all data at scan rate" | device `scan_ms`; tag rates dropped |
| Request timeout | `timeout_ms` |
| Retry attempts | `retries` = attempts − 1 (KEPServerEX counts the first request) |
| Auto-demotion | `demote_after` (failed cycles), `demote_for_ms` |
| Tag scaling (linear, square root, clamps, negate) | `scale` |
| Data collection disabled | device not imported (listed in the summary) |
| DF1 channel on a COM port | `ab_df1` device with the COM port's serial settings and `transport: unmapped`; skipped until `overrides.yaml` `serial_ports` maps the COM port (below) |
| Driver-specific device properties | `params` ([docs/driver_params.md](docs/driver_params.md)) |
| IoT Gateway MQTT agent (not imported) | `poller.yaml` `mqtt` + `devices_map.yaml` (topic, SubDeviceId), set by hand |
| Credentials in the project | never copied; environment variables instead |

Not imported yet: Advanced Tags, alarms, the data logger, IoT Gateway REST agents, the user
manager, and the OPC UA server's endpoint configuration. They are tracked in
[ROADMAP.md](ROADMAP.md).

### 3. Validate, compare, cut over

1. **Fill in `devices_map.yaml`**: set each device's real `sub_device_id` and delete its
   `stub: true` line. `opencap check` warns about every remaining stub.
2. **`opencap check -v`**: confirm the block plan. Tags with an unusable address show as
   `bad_config` here, before anything goes on the wire. Compare the estimated cycle time
   with the scan rate (`check.est_rtt_ms` sets the assumed round trip).
3. **`opencap read "<channel>/<device>"`**: compare every value with the KEPServerEX Quick
   Client. For Allen-Bradley, `opencap identity` also shows the controller, and whether
   Micro800 batching or the PCCC file directory check is active.
4. **Shadow run**: while KEPServerEX keeps running, point `poller.yaml` at a test broker,
   slow the device down in `overrides.yaml` (`min_scan_ms: 30000`), and run
   `opencap run --site "<channel>"`. Compare the two outputs. Skip this for PLCs with few
   free connections (Micro800): go straight to the cut-over.
5. **Cut over channel by channel**: disable the channel in KEPServerEX and remove the
   slow-down override. Add the channel to `opencap run --site ...`, or drop `--site` once
   every channel has moved.
6. **Watch**: `/metrics` (`poll_requests_total{result="conn_error"}` should stay flat),
   the device status topics, and the `device.stats` log lines (cycle times every 5 min).

Re-run `opencap import-kep` whenever the KEPServerEX project changes. `devices.json` is
regenerated, but your `devices_map.yaml` and `overrides.yaml` stay as they are.

### 4. Serial PLCs: map each KEPServerEX COM port

KEPServerEX reached DF1 PLCs through a COM port (often a virtual COM port that a virtual-COM
tool maps to a serial-over-TCP gateway). opencap imports them with `transport: unmapped` and
**skips them, with a warning, until you say where that line is now**:

```yaml
# overrides.yaml
serial_ports:
  COM112: 192.0.2.50:4001            # the gateway behind COM112 (raw TCP; also tcp://host:port)
  COM7: rfc2217://192.0.2.51:4002    # an RFC 2217 COM-port server
  COM3: /dev/ttyUSB0                 # a local serial port (baud/parity from the import)
```

One entry covers every device on that COM port. Then `opencap check --site "<channel>"` (no
longer `(disabled)`; `reports/import_summary.json` lists the settings to confirm: error
check, station, parity), `opencap identity` (processor type, link counters) and `opencap
read` (prints why when it fails). Stop KEPServerEX first if the gateway accepts one TCP client.
Details: [docs/driver_params.md](docs/driver_params.md) ("Serial lines", "ab_df1", "modbus_rtu").

## Configuration reference

All files live in the config directory (`--config`, default `./config`). Unknown keys are
errors. Unknown device ids or tag names in `overrides.yaml` and `devices_map.yaml` are
warnings. `SIGHUP` or `POST /reload` re-reads all four files and restarts only the channels
whose config changed.

### `poller.yaml`: global settings

Every key is optional; a missing file means all defaults. The annotated template is
[`config/poller.yaml`](config/poller.yaml).

| Key | Default | Meaning |
|---|---|---|
| `instance` | `opencap-1` | name of this server; used in the LWT topic and as the MQTT client id |
| `log_level` | `INFO` | `DEBUG` logs every request |
| `stats_interval_s` | `300` | one `device.stats` INFO line per device (cycle times); `0` = off |
| `mqtt.host` / `port` | `localhost` / `1883` | broker (8883 with TLS) |
| `mqtt.tls` / `ca_file` | `false` / `null` | TLS on, and an optional CA bundle; sets `isSecure` in the payload |
| `mqtt.username` / `password_env` | `null` / `OPENCAP_MQTT_PASSWORD` | user, and the **name** of the env var with the password |
| `mqtt.client_id` | `null` | `null` = `instance` |
| `mqtt.qos` / `keepalive_s` | `1` / `30` | |
| `mqtt.topic_prefix` | `opencap` | first level(s) of every derived topic, e.g. `acme/plant-1` ([topics](#mqtt-topics-and-payload)) |
| `saf.path` | `data/saf.sqlite` | outbox file, relative to the working directory |
| `saf.max_bytes` | 2 GiB | cap on queued topic + payload bytes; the oldest messages are dropped first |
| `api.bind` / `port` | `127.0.0.1` / `8080` | admin API; bind to localhost or a VPN address only |
| `api.token_env` | `OPENCAP_API_TOKEN` | **name** of the env var holding the bearer token |
| `api.tokens` | `[]` | named tokens `{name, role: viewer\|admin, token_env}` ([Web UI and configuration API](#web-ui-and-configuration-api)) |
| `api.audit_log` | `data/audit.jsonl` | audit log of config changes (JSON lines) |
| `api.cors_origins` | `[]` | other web origins allowed to call the API (desktop app) |
| `payload.float_decimals` | `4` | floats rounded to this many decimal places |
| `payload.bad_tags_field` | `true` | add `BadTags` for tags without good quality |
| `payload.timezone` | `Asia/Kolkata` | zone of the payload's `DateTime`; set your own (any IANA name, e.g. `UTC`) |
| `defaults.*` | see template | device defaults the importer uses when the project has no value (`scan_ms`, `min_scan_ms`, `timeout_ms`, `retries`, `demote_after`, `demote_for_ms`) |
| `check.est_rtt_ms` | `60` | assumed round trip per request in `opencap check` estimates |

Changes to `mqtt` connection settings and the LWT topic take effect at restart. A changed
`mqtt.topic_prefix` moves the data and status topics at the next reload.

### `devices.json`: channels, devices, tags

Generated by `opencap import-kep`. You can also write it by hand or generate it. Schema:
`DevicesFile` in [`src/opencap/config/models.py`](src/opencap/config/models.py).

```json
{
  "version": 1,
  "channels": [
    {
      "name": "Plant A",
      "enabled": true,
      "devices": [
        {
          "id": "Plant A/PLC1", "channel": "Plant A", "name": "PLC1",
          "driver": "modbus_tcp", "host": "192.0.2.10", "port": 502,
          "params": {"unit_id": 1, "zero_based": true},
          "scan_ms": 1000, "min_scan_ms": 1000, "timeout_ms": 3000, "retries": 2,
          "demote_after": 3, "demote_for_ms": 30000, "enabled": true,
          "tags": [
            {"name": "FLOW_1", "address": "400001", "dtype": "float32"},
            {"name": "PUMP_1_RUN", "address": "000010", "dtype": "bool", "scan_ms": 5000},
            {"name": "LEVEL_1", "address": "300005", "dtype": "uint16",
             "scale": {"raw_lo": 0, "raw_hi": 4095, "eng_lo": 0, "eng_hi": 10, "units": "m"}}
          ]
        }
      ]
    }
  ]
}
```

A tag polls at `max(tag.scan_ms or device.scan_ms, device.min_scan_ms)`. A tag with no
good read for 3 × that rate becomes `stale`.

### `devices_map.yaml`: SubDeviceId, topic and env per device

The importer writes stubs. You confirm them; the importer never overwrites them.

```yaml
defaults:                      # merged into every device's publish settings
  env: prod
  interval_ms: 10000
devices:
  "Plant A/PLC1":
    sub_device_id: PLANT_A_PLC1
    topic: plant-a/plc1/data   # optional; default <mqtt.topic_prefix>/<channel>/<device>/data
    env: staging               # optional
```

Publish settings (`PublishCfg`): `sub_device_id`, `topic`, `env` (default `prod`), `mode`
(`interval` or `on_change`), `interval_ms` (10000), `heartbeat_ms` (300000; `on_change`
only), `deadband` (absolute, `on_change` only). They layer, lowest priority first:
`devices.json` → `devices_map.yaml` `defaults` → `overrides.yaml` `publish` → the device's
`devices_map.yaml` entry.

### `overrides.yaml`: hand fixes that survive a re-import

```yaml
channels:
  "Plant B": {enabled: false}
devices:
  "Plant A/PLC1":
    host: 192.0.2.11
    min_scan_ms: 5000
    params: {max_holding: 32}                  # merged into params
    publish: {mode: on_change, deadband: 0.5}  # merged into publish settings
    tags:
      SPARE_1: {enabled: false}
      FLOW_1: {address: "400047", dtype: float32, scan_ms: 10000}
      LEVEL_1: {scale: {raw_lo: 0, raw_hi: 4095, eng_lo: 0, eng_hi: 10}}
```

Device fields: `enabled`, `host`, `port`, `scan_ms`, `min_scan_ms`, `timeout_ms`, `retries`,
`demote_after`, `demote_for_ms`, `params`, `publish`, `tags`. Tag fields: `enabled`,
`address`, `dtype`, `scan_ms`, `scale`. Top-level `serial_ports` maps KEPServerEX COM ports
of serial devices to a gateway, RFC 2217 server or local port (see "Serial PLCs" above).

## MQTT topics and payload

| Topic | Retained | Content |
|---|---|---|
| `<prefix>/<channel>/<device>/data`, or the device's `topic` | no | SubDevice JSON, below |
| `<prefix>/<channel>/<device>/status` | yes | `{"state": "ok" \| "offline" \| "demoted", "last_good": "<ISO-8601 UTC>", "errors": N}` |
| `<prefix>/poller/<instance>/status` | yes | `online` after each connect; `offline` on a clean stop, and as the LWT |

`<prefix>` is `mqtt.topic_prefix` (default `opencap`). It is used verbatim and may have
several levels; it must not contain `+`, `#`, empty levels, or start with `$`. Channel,
device and instance names are slugged: lowercase, and anything outside `[a-z0-9._-]`
becomes `_` (`Plant A` → `plant_a`).

```json
{"SubDeviceId": "PLANT_A_PLC1", "FirmVersion": "opencap-0.1.0", "Env": "prod",
 "isSecure": true, "DataValid": false,
 "Data": {"FLOW_1": 12.5, "PUMP_1_RUN": true},
 "DateTime": "2026-11-16 15:45:01", "BadTags": ["LEVEL_1"]}
```

- `Data` holds only tags with good quality. `DataValid` is false when any enabled tag is not
  good, and `BadTags` lists those tags.
- `DateTime` is the time of the newest good read (or now, if no tag is good), in `payload.timezone`.
- A device with no good read since start publishes only status messages.
- QoS 1 means at least once: after a reconnect a message can arrive twice.

## Secrets and environment variables

Secrets are never stored in config files. `poller.yaml` and `devices.json` hold only the
**names** of the environment variables that contain them, and the importer never copies
credentials out of a KEPServerEX project.

| Variable | Used for |
|---|---|
| `OPENCAP_MQTT_PASSWORD` | broker password (the name is `mqtt.password_env`) |
| `OPENCAP_API_TOKEN` | admin API bearer token (the name is `api.token_env`); required when `api.bind` is not loopback |
| any name in `api.tokens[].token_env` | one named API/UI token each (e.g. `OPENCAP_TOKEN_ALICE`) |
| `OPENCAP_OPCUA_<CHANNEL>_USER`, `OPENCAP_OPCUA_<CHANNEL>_PASSWORD` | OPC UA client credentials, written by the importer as `params.username_env` / `password_env` |
| `OPENCAP_HEALTH_URL` | Docker health check URL (default `http://127.0.0.1:8080/health`) |

Test-only: `OPENCAP_OUTAGE_S` (acceptance broker outage), `OPENCAP_KEP_PROJECT` and
`OPENCAP_PRIVATE_DIR` (a real export for `real_project` tests). Templates for an env
file, a systemd unit and a Windows service are in [`deploy/`](deploy/).

## Admin API and metrics

Bind the API to localhost or a VPN address only. `/health` and `/metrics` are open, for
monitoring. Every other route needs `Authorization: Bearer <token>` (named tokens and roles:
[Web UI and configuration API](#web-ui-and-configuration-api)). With no token set, the API
starts only on a loopback address.

| Method and path | Returns |
|---|---|
| `GET /health` | version, instance, MQTT connected, outbox size, device counts by state |
| `GET /metrics` | Prometheus text format |
| `GET /channels` | channels and their devices with state |
| `GET /devices/{id}` | config, block plan, `bad_config` tags, state, cycle time, last error |
| `GET /devices/{id}/values` | cached value, quality and source timestamp per tag |
| `POST /devices/{id}/read-once` | one full read now, serialized with polling |
| `POST /reload` | re-read the config; returns started / restarted / stopped / unchanged channels |

Device ids contain `/` and spaces: `curl -H "Authorization: Bearer $T" 'http://127.0.0.1:8080/devices/Plant%20A/PLC1/values'`.

| Metric | Type | Labels |
|---|---|---|
| `poll_requests_total` | counter | `device`, `result` = `ok`, `timeout`, `conn_error`, `bad_config`, `error` |
| `poll_cycle_seconds` | histogram | `device` |
| `poll_overrun_total` | counter | `device` (cycles longer than their scan rate) |
| `device_state` | gauge | `device` (0 ok, 1 offline, 2 demoted) |
| `mqtt_published_total` | counter | messages acknowledged by the broker |
| `saf_queue_messages` | gauge | messages waiting in the outbox |
| `saf_dropped_total` | counter | messages dropped by the `saf.max_bytes` cap |

## Web UI and configuration API

The same server serves an operator UI at **`http://127.0.0.1:8080/ui/`** and a configuration
REST API under `/api/v1`, the open equivalent of KEPServerEX's Configuration API. The UI is a
static page (plain HTML, CSS and a vendored copy of the Alpine.js CSP build, no build step) that
uses only the public `/api/v1` API, so the desktop app can load the same files
([ADR-0010](docs/adr/0010-one-ui-web-and-tauri.md)). It works at phone width.

**Tokens and roles.** Name your tokens in `poller.yaml`; each secret lives in an env var:

```yaml
api:
  tokens:
    - {name: alice, role: admin, token_env: OPENCAP_TOKEN_ALICE}      # may change the config
    - {name: grafana, role: viewer, token_env: OPENCAP_TOKEN_GRAFANA} # read-only (GET)
  audit_log: data/audit.jsonl
  cors_origins: []          # other web origins allowed to call the API (e.g. the desktop app)
```

`viewer` may read everything (status, values, config, validation, export, audit); `admin` may
also change the config, import, reload and read-once. `api.token_env` still works: it is the
admin named `default`. Log in to the UI with a token; it is kept in that browser tab only
(`sessionStorage`) and sent as a bearer token. With no token configured at all, the API and the
UI are read-only and answer only requests addressed to localhost.

**UI pages.** Dashboard (sites and devices: ok/offline/demoted, last good, errors, cycle time,
MQTT link, outbox size) · device page (live values and quality every 2 s, block plan,
`bad_config` tags, identity, read once) · config editor (channels, devices, tags, publish map,
overrides; validation errors next to the field; Reload) · check (block plan and cycle estimates)
· import (KEPServerEX `project.json` or an opencap export, with a dry-run diff) · audit log.
The UI never writes a PLC value.

**Configuration API** (contract: [03 §3.6](docs/architecture/03-deep-dive.md)). Reads, lists
included, return the config `revision` (also as `ETag`). **Every write needs `If-Match: <revision>`**: 428
without it, 412 when someone changed the config since you read it. `PUT` replaces, `PATCH` is a
JSON merge patch (`null` removes a key).

| Method and path | What |
|---|---|
| `GET /api/v1/whoami` · `GET /api/v1/auth-info` (open) | your token name and role · whether a token is needed |
| `GET /api/v1/status` | health, every polled device, pending reload (the dashboard in one call) |
| `GET/POST /api/v1/channels` · `GET/PATCH/DELETE /api/v1/channels/{channel}` | channels |
| `GET/POST /api/v1/channels/{channel}/devices` · `GET /api/v1/devices` | devices of a channel · all devices |
| `GET/PUT/PATCH/DELETE /api/v1/devices/{id}` | one device (`devices.json`) |
| `GET/POST /api/v1/devices/{id}/tags` · `GET/PUT/PATCH/DELETE …/tags/{tag}` | tags (POST takes one or a list) |
| `GET /api/v1/devices/{id}/status`, `/values`, `/plan`, `/identity` · `POST …/read-once` | the running device (identity is cached 5 min; `?refresh=true` needs admin) |
| `GET /api/v1/map` · `PUT /api/v1/map/defaults` · `GET/PUT/PATCH/DELETE /api/v1/map/devices/{id}` | `devices_map.yaml` |
| `GET /api/v1/overrides` · `GET/PUT/PATCH/DELETE /api/v1/overrides/devices/{id}` · `GET/PUT/DELETE /api/v1/overrides/channels/{channel}` | `overrides.yaml` |
| `GET /api/v1/project[?download=true]` | the whole config (the four files), a backup |
| `POST /api/v1/project/import[?dry_run=false]` | a KEPServerEX `project.json` or an export; **dry run by default** |
| `GET /api/v1/project/validate[?site=…&verbose=true]` | `opencap check` as JSON |
| `GET /api/v1/project/status` · `/pending` · `/revisions` · `/schema` | summary and warnings · pending reload · revisions written · JSON schemas |
| `GET /api/v1/audit[?action=…&who=…&target=…]` · `POST /api/v1/reload` | audit log · apply the config |

```bash
H="Authorization: Bearer $OPENCAP_TOKEN_ALICE"; API=http://127.0.0.1:8080/api/v1
REV=$(curl -s -H "$H" $API/project/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
curl -s -H "$H" -H "If-Match: $REV" -H 'Content-Type: application/json' -X PATCH \
  -d '{"timeout_ms": 1500}' "$API/devices/Plant%20A/PLC1"
curl -s -H "$H" -H 'Content-Type: application/json' --data-binary @project.json \
  "$API/project/import?filename=project.json"            # dry run: what would change
curl -s -H "$H" -X POST $API/reload                       # apply (or add ?apply=true to a write)
```

Every change is checked by its model, by the loader and by the device's driver (a tag address the
driver cannot read is refused) before anything is written; a 422 lists each error with its field.
A change to several files is written all or nothing (temp files first, then renames, rolled back
on failure), the previous version is kept as `<file>.bak`, and comments in the hand-owned
`devices_map.yaml` and `overrides.yaml` are kept. If the config directory is read-only (see
[Deployment](#deployment)), the UI turns editing off and writes answer 503 with the path. The
token is checked before a request body is read, and bodies are limited to 1 MiB (64 MiB for
imports). A write changes the files only:
`POST /api/v1/reload` applies them (only changed channels restart), or add `?apply=true` to the
write. Renaming a device or channel moves its map entry and override along; deleting one deletes
them. Channels, devices and tags live in `devices.json`, which a KEPServerEX re-import replaces:
put fixes that must survive a re-import in the device's override. Every accepted change, import
and reload is in the audit log (who, when, what). Details: `docs/decisions.md` D-039 to D-044.

## Deployment

[docs/deployment.md](docs/deployment.md) covers the Docker image (`python:3.12-slim`,
non-root, `config/` and `data/` volumes), the systemd unit
([`deploy/opencap.service`](deploy/opencap.service): restarts on failure, secrets from an
env file), log rotation (JSON on stdout, rotated by journald or Docker), and running as a
Windows service with WinSW ([`deploy/windows/`](deploy/windows/)).

## Testing

```bash
pip install -e '.[dev,modbus,ab,north,serial]'
pytest -m "not acceptance"                  # fast suite, ~30 s; runs in CI on every push and PR
pytest -m acceptance                        # simulated devices + broker outage; nightly in CI
OPENCAP_OUTAGE_S=600 pytest -m acceptance   # the full 10-minute broker outage
OPENCAP_ENGINE=rust pytest -m acceptance tests/integration/test_phase1_acceptance.py   # same suite, Rust engine
```

- Protocol tests run against simulators in `tests/helpers/` (Modbus, CIP/EtherNet-IP and
  PCCC), which the real client libraries talk to **unmodified**, and an in-process MQTT
  broker. `NetCut` cuts the network between poller and broker.
- Tests bind free ports only, so several runs can share a machine.
- Tests marked `real_project` need a real KEPServerEX export. They are skipped unless one
  exists at `.private/fixtures/project.json` (git-ignored) or `OPENCAP_KEP_PROJECT`.
- `tests/unit/test_no_private_data.py` fails if a tracked file contains a private IP
  address or a generated site config. Documentation uses `192.0.2.x`.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the suite on Python 3.12
and 3.13, builds the wheel and the container image, and runs the acceptance tests nightly
and on manual dispatch.

## Contributing, security, license

- [CONTRIBUTING.md](CONTRIBUTING.md): development setup, the engineering protocol (board,
  claims, reviews), how to add a driver, the decisions log.
- [SECURITY.md](SECURITY.md): how to report a vulnerability privately; what never to post in
  an issue (real PLC addresses, project exports, credentials).
- [ROADMAP.md](ROADMAP.md): the plan to cover what KEPServerEX does.
- [docs/architecture/](docs/architecture/README.md) and [docs/adr/](docs/adr/README.md): the
  architecture of record and its decision records.
- [Program board](https://github.com/sharmaniraj009/opencap/issues/1)
  ([mirror](docs/BOARD.md)): status, who works on what, next priorities.

## License

opencap is licensed under the [Apache License 2.0](LICENSE).
Copyright the opencap contributors.
