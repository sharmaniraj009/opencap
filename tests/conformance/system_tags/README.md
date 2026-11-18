# System-tag conformance vectors

Language-neutral vectors for [`docs/spec/system-tags.md`](../../../docs/spec/system-tags.md)
(ADR-0014). Both engines run every vector; `test_system_tag_vectors.py` checks them in CI.

| File | What |
|---|---|
| `catalogue.json` | every system tag: `level`, `name`, `dtype`, `update` class, `present` condition, `kep` (KEPServerEX name and meaning) |
| `vectors/*.json` | `{"spec", "spec_version", "description", "cases": [...]}` |
| `reference.py` | test oracle that evaluates the cases as the spec words them (deleted once `opencap.core.system_tags` exists) |
| `test_system_tag_vectors.py` | vectors vs catalogue vs oracle; spec documents every catalogue name; every catalogue tag is asserted by some vector |

## `snapshot` cases

```json
{"kind": "snapshot", "name": "...", "sections": ["8.1"],
 "state": {...}, "expect": {"<path>": {"dtype": "bool", "value": true}}, "absent": ["<path or folder>"]}
```

`expect` is partial: only the listed paths are compared (dtype and value, exactly; floats are
compared as JSON numbers). A path in `absent` must not exist, nor anything below it. `state` is
the engine state at one instant:

```json
{
  "now": "2026-11-18T10:00:00.000Z",
  "node": {"product_version": "0.1.0", "engine": "python", "instance": "opencap-1",
           "project_name": "config", "project_title": "", "project_revision": "…",
           "start_time": "…Z", "timezone": "Asia/Kolkata",
           "sessions": [{"server": "opcua_server", "kind": "opcua_server", "name": "SCADA B"}],
           "redundancy": null},
  "north": [{"name": "mqtt", "kind": "mqtt", "enabled": true, "connected": true, "published": 10,
             "last_error": null,
             "outbox": {"messages": 0, "bytes": 0, "oldest": null, "max_bytes": 2147483648,
                        "full": false, "disk_free_bytes": null, "disk_low": false,
                        "available": true, "dropped": 0, "forward_mode": null}},
            {"name": "opcua_server", "kind": "opcua_server", "enabled": true, "running": true,
             "clients": 1, "last_error": null}],
  "channels": [{"name": "Plant A", "enabled": true, "description": null,
                "in_flight": 0, "max_in_flight": 1,
                "devices": [{"name": "PLC1", "enabled": true, "driver": "modbus_tcp",
                             "host": "192.0.2.10", "port": 502, "params": {},
                             "scan_ms": 1000, "timeout_ms": 3000, "retries": 2,
                             "demote_after": 3, "demote_for_ms": 30000, "description": null,
                             "tags_enabled": 10, "tags_scanned": 10, "counts_bytes": false,
                             "status": {"state": "ok", "consecutive_failures": 0,
                                        "error_since": null, "last_good": "…Z", "errors": 0,
                                        "last_error": null, "cycle_ms": 12.5,
                                        "connected": true, "last_connect_failed": false},
                             "counters": {"ok": 0, "timeout": 0, "conn_error": 0,
                                          "bad_config": 0, "error": 0, "failed_reads": 0,
                                          "overruns": 0, "rx_bytes": 0, "tx_bytes": 0}}]}]
}
```

- Device settings are the *effective* values (overrides and `serial_ports` applied); a missing
  device setting takes the `poller.yaml` default of `DeviceDefaults`; a missing serial param
  takes the driver default (`baudrate` 19200, `bytesize` 8, `parity` `N`, `stopbits` 1;
  `transport` `tcp` for `modbus_rtu`, `unmapped` for `ab_df1`).
- `status` is the device's health plus the two fields of spec §9; it is ignored (and may be
  `null`) for a disabled device or a device on a disabled channel.
- `counters` are the process-lifetime counters behind `/metrics`; missing keys are 0.
- An agent's `outbox` is present only for durable agents.

## `device_sequence` cases

```json
{"kind": "device_sequence", "name": "...", "sections": ["9"],
 "device": {"name": "PLC1", "driver": "modbus_tcp", "host": "192.0.2.10", "port": 502,
            "demote_after": 3, "demote_for_ms": 10000},
 "start": "2026-11-18T10:00:00.000Z",
 "steps": [{"at": "…Z", "event": "cycle_failed", "error": "…", "cycle_ms": 9001.2,
            "expect": {"_Error": {"dtype": "bool", "value": true}}}]}
```

Events, applied in order at their `at` time (monotonic time = seconds since `start`):

| Event | Effect |
|---|---|
| `connect_ok` | `connected` true, `last_connect_failed` false |
| `connect_failed` | `connected` false, `last_connect_failed` true |
| `cycle_good` | `DeviceHealth.record_good`; `error_since` cleared; `last_error` = the step's `error` (null when absent: a fully good cycle clears it) |
| `cycle_failed` | `error_since` = `at` if no failure run is open; `DeviceHealth.record_failure`; `last_error` = `error`; a demotion (or re-demotion) drops the connection |
| `cycle_neutral` | health unchanged; `last_error` = `error` when given |
| `tick` | time passes |

`cycle_ms`, when given, becomes `_CycleMs`. `expect` lists device `_System` names (no path)
evaluated at the step's `at`, after the event.

## Adding a vector

Write the expected values by hand from the spec, not from an implementation, then run
`pytest tests/conformance/system_tags`. A disagreement with the oracle is either a vector error or
an ambiguity in the spec: fix the spec first.
