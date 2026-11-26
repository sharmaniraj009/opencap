# opencap demo: a working system in one command

Two simulated devices (a water tank and a pump, 12 tags, no PLC needed) polled by the real
opencap poller and published to an MQTT broker, with the admin API and web UI running.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[north]'
PATH="$PWD/.venv/bin:$PATH" demo/run_demo.sh        # also needs mosquitto: apt install mosquitto
```

The script starts a throwaway broker on `127.0.0.1:1883` if none is running, then `opencap run`.
Each device publishes one SubDevice JSON every 2 s to `opencap/water_plant/<device>/data`:

```json
{"SubDeviceId": "DEMO_TANK_1", "FirmVersion": "opencap-0.1.0", "Env": "demo", "isSecure": false,
 "DataValid": true, "Data": {"Level_pct": 56.0474, "Temp_C": 25.9989, "Inflow_lps": 2,
 "HighLevel": false, "Setpoint_pct": 70.0, "Heartbeat": 10, "SampleTime": "2026-11-26T19:27:59.630Z"},
 "DateTime": "2026-11-27 00:57:59"}
```

## What to look at

| What | How |
|---|---|
| Messages | `mosquitto_sub -h 127.0.0.1 -t 'opencap/#' -v` |
| Health | `curl -s localhost:8080/health` (status, MQTT link, queue depth, device states) |
| Devices (needs the token) | `curl -s -H "Authorization: Bearer demo-token" localhost:8080/api/v1/devices` |
| Web UI | `http://127.0.0.1:8080/ui/` (paste the token `demo-token`) |
| Config check, no network | `opencap check --config demo/config` |
| One read, no MQTT | `opencap read "Water Plant/Tank1" --config demo/config` |

Without the token the API answers `401`; `/health` and `/metrics` stay open by design.

## The store-and-forward test (the point of the project)

1. Run the demo and watch `curl -s localhost:8080/health`: `mqtt_connected: true`, `saf_queue: 0`.
2. `pkill mosquitto`. The poller keeps polling; `mqtt_connected` goes `false` and `saf_queue` climbs
   (every message is written to the SQLite outbox first and deleted only after the broker's PUBACK).
3. Start the broker again. `saf_queue` returns to 0 and the whole backlog arrives in order, with
   unbroken `SampleTime` stamps and nothing missing.

Checked on 2026-11-26 with this exact config: a 12 s outage queued 12 messages (6 per device);
after the broker returned all 12 were delivered in order, 2 s apart with no gap.

## Files

`config/devices.json` (the devices and tags, `driver: simulator`), `config/devices_map.yaml`
(SubDeviceIds), `config/poller.yaml` (broker, API). The simulator driver is described in
`docs/driver_params.md`. Swap a device's `driver`, `host`, `port` and addresses for a real PLC to use
the same setup against hardware; opencap is read-only toward PLCs by design.
