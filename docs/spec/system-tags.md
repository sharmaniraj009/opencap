# System and diagnostic tags

- **Status:** Draft for review (spec version 1)
- **Issue:** [#22](https://github.com/sharmaniraj009/opencap/issues/22). Implemented by the
  follow-up issues in §13 (Python) and [#56](https://github.com/sharmaniraj009/opencap/issues/56) (Rust).
- **Conformance:** [`tests/conformance/system_tags/`](../../tests/conformance/system_tags/)
  (catalogue, vectors, reference oracle, ADR-0014).
- **Architecture:** system tags are virtual devices in the tag store
  ([ADR-0005](../adr/0005-north-plugins-tag-store-event-bus.md)); quality per
  [ADR-0006](../adr/0006-quality-and-timestamps.md); read-only per
  [ADR-0008](../adr/0008-read-only-by-default.md).

KEPServerEX publishes server, channel and device diagnostics as tags, so a SCADA screen can show
and alarm on them without a second monitoring system. opencap already has the numbers (device
health, `/metrics`, `/health`); this spec fixes the names, types and semantics of the same numbers
as tags, once, for both engines and every north interface. Where KEPServerEX has a tag with the
same meaning, opencap uses its name, so screens built on KEPServerEX keep working after the
endpoint URL changes. Deliberate differences are listed in §12.

## 1. Requirements traced

| Plan row | What this spec fixes | Section |
|---|---|---|
| SYS-03 system tag model | paths, the virtual-device model, read-only, always `good` | §2, §3, §10 |
| SYS-04 server identity and clock tags | `_ProductName` … `_Time_PM`, `_Version`, `_Instance`, `_StartTime`, `_ProjectRevision` | §5.1, §5.2 |
| SYS-05 server counts | `_TotalTagCount`, `_ActiveTagCount`, `_ClientCount`, `_OpcClientNames`, device-state counts | §5.3 |
| SYS-07 channel system tags | `<Channel>._System.*` read-only mirrors | §7.1 |
| SYS-08 device system tags | `<Channel>.<Device>._System.*` status and mirrors | §8, §9 |
| SYS-11 statistics tags | `<Channel>._Statistics.*`, `<Channel>.<Device>._Statistics.*` | §7.2, §8.3 |
| SAF-15 store-and-forward status | `_System._North.<agent>.*` | §6 |
| DEV-02 device description, DEV-03 enable, DEV-04 simulated, DEV-10 auto-demotion, DEV-15 timing | `_Description`, `_Enabled`, `_Simulated`, `_AutoDemot*`, `_RequestTimeout`, `_RequestAttempts` | §8 |
| DEV-18 media redundancy | `_ActivePath` name reserved | §8.4 |
| PRJ-13, LOG-07 naming rules | reserved names | §3.2 |
| FR-10, NFR-13 | every device's state, cycle time and errors visible as tags | all |
| `docs/architecture/04-scale-reliability.md` §4.4 | `_System._Redundancy` (contents in RFC 0003, #20) | §5.5 |

Not covered here: SYS-06 licensing tags (out of scope), SYS-09 writable control tags (out of
scope, ADR-0008; equivalents are audited API actions), SYS-10 property tags (OPC UA attributes,
separate feature), SYS-12 modem tags and SYS-13 serialization tags (deferred with plan 08 and
CH-14; their folder names `_Modem` and `_CommunicationSerialization` are reserved, §3.2).

## 2. Model

1. **Virtual devices.** System tags live in the tag store like I/O tags, in virtual devices owned
   by one processing task per engine (the *system-tag task*). It is their only writer (ADR-0005).
   North plugins read them from the store and subscribe to their changes exactly as for I/O tags;
   no north plugin computes a system tag itself.
2. **Never polled.** No system tag causes a wire request. Values are computed from state the
   engine already holds: device health, the request counters behind `/metrics`, the loaded config,
   the outbox and the north servers.
3. **One source.** A system tag and the `/metrics` series, `/health` field or
   `GET /api/v1/devices/{id}/status` field with the same meaning are computed from the same
   counter or field. Sampled at the same instant on an idle engine, they are equal (§11 maps each).
4. **Always `good`.** Every system tag has quality `good` (ADR-0006). A value that does not exist
   yet has a defined placeholder (§4), not a bad quality: a SCADA alarm on `_Error` must never
   fire because the diagnostic itself is missing.
5. **Read-only.** Every north interface refuses writes to system tags (§10). Settings change only
   through the authenticated, audited API (SYS-09).
6. **Timestamps.** `source_ts` = `server_ts` = the UTC time at which the system-tag task computed
   the value. A tag is written to the store only when its value changes, so the timestamp is the
   time of the last change (clock tags change every second).

### 2.1 Update classes

| Class | Recomputed | Latency bound |
|---|---|---|
| `event` | at the end of each cycle of the device, after each connect attempt, on each health event, and at every `tick` | ≤ 1 s after the change |
| `tick` | once per second, aligned to the UTC second (± 50 ms) | ≤ 1 s |
| `config` | when a config is loaded or reloaded | applied with the reload |

The bound is measured inside the engine (store write time minus source change time). A north
interface adds its own publishing interval (for example the OPC UA server's `update_ms`).

## 3. Paths and names

### 3.1 Paths

| Level | Path | Example |
|---|---|---|
| node | `_System.<tag>` | `_System._DateTime` |
| north agent or server | `_System._North.<agent>.<tag>` | `_System._North.mqtt._Connected` |
| redundancy | `_System._Redundancy.<tag>` | `_System._Redundancy._Role` |
| channel | `<Channel>._System.<tag>` | `Plant A._System._BaudRate` |
| channel statistics | `<Channel>._Statistics.<tag>` | `Plant A._Statistics._FailedReads` |
| device | `<Channel>.<Device>._System.<tag>` | `Plant A.PLC1._System._Error` |
| device statistics | `<Channel>.<Device>._Statistics.<tag>` | `Plant A.PLC1._Statistics._Timeouts` |

The path is the same string on every interface: the OPC UA NodeId is `ns=<idx>;s=<path>` (the
same form KEPServerEX uses, D-046), the API returns `path`, Sparkplug and SubDevice JSON use it as
in §10. `<agent>` is the north agent's configured name; until north agents are configurable the
single MQTT agent is `mqtt` and the OPC UA server is `opcua_server`.

### 3.2 Reserved names

Names are compared without regard to case, as KEPServerEX compares them.

| Rule | Applies to | Severity |
|---|---|---|
| A channel may not be named `_System` | channel names | error (both engines, v1 and v2) |
| A device may not be named `_System` or `_Statistics` | device names | error |
| The first dot segment of a tag name may not be `_System` or `_Statistics` | tag names | error (`_System` is already an error since D-046) |
| An agent may not be named `_North` or `_Redundancy` | north agent names | error |
| Any other channel, device, group or tag name segment starting with `_` | all names | warning in v1, error in v2 and in API writes (PRJ-13) |

`_Modem`, `_CommunicationSerialization`, `_ActivePath` and `_Redundancy` are reserved for the
features named in §1 and §5.5; they never appear as user names under the second rule either.

## 4. Data types

System tags use the tag dtypes of `models.DType` plus `datetime` and `string_array`.

| dtype | OPC UA DataType | SubDevice JSON | Sparkplug B | Placeholder when the value does not exist |
|---|---|---|---|---|
| `bool` | Boolean | `true`/`false` | Boolean | `false` |
| `uint32` | UInt32 | integer | UInt32 | `0` |
| `uint64` | UInt64 | integer | UInt64 | `0` |
| `float64` | Double | number | Double | `0.0` |
| `string` | String | string | String | `""` |
| `datetime` | DateTime | ISO-8601 UTC string with milliseconds and `Z` | DateTime | OPC UA: DateTime MinValue (OPC UA's null DateTime); JSON and API: `null`; Sparkplug: `is_null` |
| `string_array` | String, ValueRank 1 | array of strings | StringArray | `[]` |

**Counter widths.** Counters with a KEPServerEX name (`_SuccessfulReads`, `_FailedReads`,
`_RxBytes`, `_TxBytes`) are `uint32` and wrap modulo 2³², as KEPServerEX's do. opencap's own
counters are `uint64` and never wrap. `float64` values are not rounded except where a row says so.

## 5. Node level: `_System.*`

### 5.1 Identity

| Tag | dtype | Unit | Update | Value | KEPServerEX |
|---|---|---|---|---|---|
| `_ProductName` | string | | config | `"opencap"` | same name |
| `_ProductVersion` | string | | config | package version, e.g. `"0.1.0"` | same name |
| `_Version` | string | | config | `"opencap-" + _ProductVersion`, the SubDevice JSON `FirmVersion` | — |
| `_Engine` | string | | config | `"python"` or `"rust"` | — |
| `_Instance` | string | | config | `poller.yaml` `instance` | — |
| `_ProjectName` | string | | config | name of the config directory (v1) or the project document's `name` (v2, #37) | same name |
| `_ProjectTitle` | string | | config | `""` in v1; the project document's `title` in v2 | same name |
| `_ProjectRevision` | string | | config | the config revision (the API's `revision`/ETag) | — |
| `_StartTime` | datetime | | config | process start, UTC | — |
| `_UptimeS` | uint64 | s | tick | whole seconds since `_StartTime` | — |

`_FullProjectName` (KEPServerEX's project file path) is not offered: it would publish host paths.

### 5.2 Clock

All clock tags are `tick`. `now` is the computation time; *local* means `now` in
`poller.yaml` `payload.timezone` (the zone SubDevice JSON already uses), with daylight saving
applied by the IANA tz database.

| Tag | dtype | Value | Example (`2026-11-18T10:15:42.250Z`, `Asia/Kolkata`) |
|---|---|---|---|
| `_DateTime` | string | UTC, `YYYY-MM-DDTHH:MM:SS.mmmZ` | `2026-11-18T10:15:42.250Z` |
| `_DateTimeLocal` | string | local, `YYYY-MM-DDTHH:MM:SS.mmm±HH:MM` | `2026-11-18T15:45:42.250+05:30` |
| `_Date` | string | local, `YYYY-MM-DD` | `2026-11-18` |
| `_Time` | string | local, `HH:MM:SS`, 24-hour | `15:45:42` |
| `_Date_Day` | uint32 | local day of month, 1–31 | `18` |
| `_Date_DayOfWeek` | uint32 | local weekday, 0 = Sunday … 6 = Saturday | `3` |
| `_Date_Month` | uint32 | local month, 1–12 | `11` |
| `_Date_Year2` | uint32 | local year modulo 100 | `26` |
| `_Date_Year4` | uint32 | local year | `2026` |
| `_Time_Hour` | uint32 | local hour on a 12-hour clock, 1–12 (00:xx is 12) | `3` |
| `_Time_Hour24` | uint32 | local hour, 0–23 | `15` |
| `_Time_Minute` | uint32 | 0–59 | `45` |
| `_Time_Second` | uint32 | 0–59 (a leap second is reported as 59) | `42` |
| `_Time_PM` | bool | `_Time_Hour24 >= 12` | `true` |

All names are KEPServerEX's. The issue's `_System._Time` (current time) is `_DateTime`;
`_Time` keeps KEPServerEX's meaning (local time of day).

### 5.3 Counts and links

| Tag | dtype | Update | Value |
|---|---|---|---|
| `_TotalTagCount` | uint32 | config | enabled I/O tags of enabled devices on enabled channels (system tags not counted) |
| `_ActiveTagCount` | uint32 | tick | of those, the tags currently scanned: accepted by the driver's `plan()` (not `Plan.invalid`, not in a block disabled by `BlockConfigError`) on devices whose state is not `demoted` |
| `_ClientCount` | uint32 | tick | client sessions on opencap's data servers (OPC UA sessions, Modbus server connections, later OPC DA); API and web UI sessions are not counted |
| `_OpcClientNames` | string_array | tick | one entry per OPC session: the OPC UA session's `ClientDescription.ApplicationName` text (OPC DA: the registered client name), sorted by code point; `[]` without an OPC server |
| `_DevicesOk` | uint32 | tick | enabled devices on enabled channels in state `ok` |
| `_DevicesOffline` | uint32 | tick | … in state `offline` |
| `_DevicesDemoted` | uint32 | tick | … in state `demoted` |
| `_DevicesDisabled` | uint32 | config | devices that are disabled or on a disabled channel |
| `_MqttConnected` | bool | tick | `true` when at least one MQTT agent is enabled and every enabled MQTT agent is connected |
| `_OutboxMessages` | uint64 | tick | sum of `_OutboxMessages` over the agents of §6 |
| `_OutboxAgeS` | float64 | tick | largest `_OutboxAgeS` over those agents; `0.0` when all are empty |
| `_OutboxDropped` | uint64 | tick | sum of `_OutboxDropped` over those agents |

KEPServerEX names: `_TotalTagCount`, `_ActiveTagCount`, `_ClientCount`, `_OpcClientNames`. In
KEPServerEX a tag is "active" while a client keeps it on scan. opencap scans every enabled tag
whatever the clients do, so the scanned set is the equivalent (this replaces SYS-05's draft
wording "tags with a subscriber").

### 5.4 Not offered at node level

`_LicensedFeatures`, `_TimeLimitedFeatures`, `_ExpiredFeatures`, `_IsDemo` (SYS-06, no licensing)
and `_FullProjectName` (§5.1).

### 5.5 Redundancy placeholder

`_System._Redundancy` is the folder that RFC 0003 (#20) fills. Until redundancy exists, and on a
node without a partner, it holds exactly:

| Tag | dtype | Update | Value without redundancy |
|---|---|---|---|
| `_Mode` | string | config | `"standalone"` |
| `_Role` | string | config | `"active"` |

RFC 0003 may add tags and values (`"warm_standby"`, `"standby"` …) but not change these two
meanings for a standalone node.

## 6. North agents and servers: `_System._North.<agent>.*`

One folder per configured north agent or data server, enabled or not.

| Tag | dtype | Update | Present for | Value | Source |
|---|---|---|---|---|---|
| `_Kind` | string | config | all | `mqtt`, `sparkplug`, `opcua_server`, `modbus_server`, … | config |
| `_Enabled` | bool | config | all | agent enabled | config |
| `_Connected` | bool | event | client agents (they connect to a peer) | link to the broker or peer is up | `mqtt_connected` in `/health` |
| `_Published` | uint64 | tick | client agents | messages acknowledged by the peer since start | `mqtt_published_total` |
| `_Running` | bool | event | servers | the server is listening | `opcua_server.running` in `/health` |
| `_Clients` | uint32 | tick | servers | open client sessions | `opcua_sessions` (planned) |
| `_LastError` | string | event | all | last error reported by the agent or server; `""` when none since start | `/health` |
| `_OutboxMessages` | uint64 | tick | durable agents | queued messages | `saf_queue_messages`, later `north_backlog_messages{agent}` |
| `_OutboxBytes` | uint64 | tick | durable agents | queued topic + payload bytes | `north_backlog_bytes{agent}` |
| `_OutboxAgeS` | float64 | tick | durable agents | age of the oldest queued message in seconds, rounded to 3 decimals; `0.0` when empty | `north_backlog_age_seconds{agent}` |
| `_OutboxCapacityBytes` | uint64 | tick | durable agents | `max(max_bytes − bytes, 0)` | `north_backlog_capacity_bytes{agent}` |
| `_OutboxFull` | bool | tick | durable agents | the outbox reports itself full | `saf_full{agent}` |
| `_OutboxDiskFreeBytes` | uint64 | tick | durable agents, once SAF-06 exists | free bytes above the reserve | `saf_disk_free_bytes{agent}` |
| `_OutboxDiskLow` | bool | tick | durable agents | free disk is below the reserve | `/health` `saf_problem == "disk_low"` |
| `_OutboxAvailable` | bool | tick | durable agents | the outbox file is open (SAF-04) | `saf_available` |
| `_OutboxDropped` | uint64 | tick | durable agents | messages dropped for the size cap or the disk reserve since start | `saf_dropped_total` + `saf_dropped_disk_total` |
| `_ForwardMode` | string | event | durable agents | `ordered` or `live_first` (SAF-08; `ordered` until then) | `saf_forward_mode{agent}` |

A *client agent* connects to a peer (MQTT, Sparkplug, REST client); a *server* accepts clients
(OPC UA, Modbus TCP server); a *durable agent* has an outbox (ADR-0011). Tags whose source feature
does not exist yet (SAF-06, SAF-14) take the placeholder of §4 until it does; the follow-up issue
names them. KEPServerEX shows this information in its `_ThingWorx` folder under its own names
(SAF-15); those names are not reused because the folder is specific to a PTC product.

## 7. Channel level

### 7.1 `<Channel>._System.*`

All `config` class, read-only mirrors of the effective config (overrides applied).

| Tag | dtype | Present | Value | KEPServerEX |
|---|---|---|---|---|
| `_Enabled` | bool | always | channel enabled | — |
| `_Description` | string | always | channel description; `""` until channels have one | same name |
| `_Driver` | string | always | the driver when every device of the channel uses the same one, else `""` | — |
| `_DeviceCount` | uint32 | always | devices configured on the channel | — |
| `_ComId` | uint32 | serial line | `N` when the shared COM port is `COM<N>`: the `com_port` param (KEPServerEX import), else the serial device path; `0` otherwise | same name |
| `_BaudRate` | uint32 | serial line | shared `baudrate` | same name |
| `_DataBits` | uint32 | serial line | shared `bytesize` | same name |
| `_Parity` | string | serial line | shared `parity` as `None`, `Even`, `Odd`, `Mark` or `Space` | same name |
| `_StopBits` | float64 | serial line | shared `stopbits` (1, 1.5 or 2) | same name |
| `_EncapsulationPort` | uint32 | serial over TCP | shared TCP port | same name |
| `_EncapsulationProtocol` | string | serial over TCP | `"TCP/IP"` | same name |

*Serial line*: every device of the channel is on a serial driver (`modbus_rtu`, `ab_df1`) with
`transport: serial`. *Serial over TCP*: every device is on a serial driver with `transport: tcp`.
**Shared** means the value all the channel's devices have (driver defaults applied, after the
`serial_ports` override). When the devices disagree, the tag holds the placeholder (`0`, `""`,
`0.0`), as plan 07 proposed.

Not offered: `_SharedConnection`, `_AvailableNetworkAdapters`, `_NetworkAdapter` (host details or
features opencap does not have), `_FloatHandlingType` (opencap has no float handling setting:
a non-finite float makes the tag bad), `_InterDeviceDelayMS` and the RTS settings (offered once
the settings exist), the write duty cycle (no writes, CH-15).

### 7.2 `<Channel>._Statistics.*`

Sums over the channel's devices of the device counters of §8.3, plus the channel's wire lock.

| Tag | dtype | Update | Value | KEPServerEX |
|---|---|---|---|---|
| `_SuccessfulReads` | uint32, wraps | event | wire requests that succeeded | same name |
| `_FailedReads` | uint32, wraps | event | block reads that failed after all retries | same name |
| `_RxBytes` | uint32, wraps | event | bytes received; present only when every device's driver counts bytes | same name |
| `_TxBytes` | uint32, wraps | event | bytes sent; same condition | same name |
| `_PendingReads` | uint32 | tick | requests in flight on the channel at the tick (0 or 1 under D-015) | same name |
| `_MaxPendingReads` | uint32 | tick | largest number of requests in flight at once since start | same name |
| `_RequestsOk` | uint64 | event | `poll_requests_total{result="ok"}` summed | — |
| `_Timeouts` | uint64 | event | `…{result="timeout"}` | — |
| `_ConnErrors` | uint64 | event | `…{result="conn_error"}` (failed connects included) | — |
| `_BadConfig` | uint64 | event | `…{result="bad_config"}` | — |
| `_OtherErrors` | uint64 | event | `…{result="error"}` | — |
| `_Overruns` | uint64 | event | `poll_overrun_total` summed | — |

Not offered: `_SuccessfulWrites`, `_FailedWrites`, `_PendingWrites`, `_MaxPendingWrites`,
`_NextWritePriority` (no writes, ADR-0008), `_NextReadPriority` (opencap has no read priorities)
and `_Reset` (writable; counters are monotonic like Prometheus counters). A constant `0` would
claim a capability opencap does not have; a client that binds one gets `BadNodeIdUnknown`.
KEPServerEX shows statistics only when channel diagnostics are on; opencap always does.

## 8. Device level

### 8.1 `<Channel>.<Device>._System.*` status

All `event` class. Section 9 defines the values as functions of device health.

| Tag | dtype | Value | KEPServerEX |
|---|---|---|---|
| `_State` | string | `ok`, `offline`, `demoted` or `disabled` | — |
| `_Error` | bool | the last counted cycle failed (§9) | same name and meaning |
| `_NoError` | bool | `not _Error` | same name |
| `_SecondsInError` | uint32 | whole seconds since the first failed cycle of the current failure run; `0` when `_Error` is false | same name |
| `_AutoDemoted` | bool | `_State == "demoted"` | same name |
| `_FailedConnection` | bool | the most recent connect attempt failed; `false` after a successful connect and before the first attempt | same name |
| `_Connected` | bool | the driver reports an open connection now | — |
| `_LastGood` | datetime | time of the last good cycle; placeholder before the first | — |
| `_LastError` | string | `last_error` of `GET …/status`; `""` when null | — |
| `_ErrorCount` | uint64 | failed cycles since the device's worker started (`errors` of `GET …/status`) | — |
| `_CycleMs` | float64 | duration of the last cycle in ms (`cycle_ms` of `GET …/status`) | — |
| `_Overruns` | uint64 | cycles that took longer than their scan rate (`poll_overrun_total{device}`) | — |

### 8.2 `<Channel>.<Device>._System.*` mirrors

All `config` class.

| Tag | dtype | Value | KEPServerEX |
|---|---|---|---|
| `_Enabled` | bool | device enabled (a device on a disabled channel reports its own flag) | same name, read-only here |
| `_Driver` | string | `driver` | — |
| `_DeviceId` | string | `<host>:<port>`; for `transport: serial`, `<host>` (the device path) | same name, other format (§12) |
| `_Description` | string | `description` (DEV-02); `""` until devices have one | same name |
| `_Simulated` | bool | `driver == "simulator"` (DEV-04) | same name |
| `_ScanMode` | string | `"tag_scan_rate"`: tags at their own `scan_ms`, else the device's | same name, other values |
| `_ScanRateMs` | uint32 | `scan_ms` | same name |
| `_RequestTimeout` | uint32 | `timeout_ms` | same name |
| `_RequestAttempts` | uint32 | `retries + 1` (KEPServerEX counts attempts) | same name |
| `_AutoDemotionEnabled` | bool | `demote_after > 0` | same name |
| `_AutoDemotedFailureCount` | uint32 | `max(demote_after, 0)` | same name |
| `_AutoDemotionIntervalMS` | uint32 | `demote_for_ms` | same name |
| `_EncapsulationIp` | string | `host`; present for serial drivers on `transport: tcp` | same name |
| `_EncapsulationPort` | uint32 | `port`; same condition | same name |
| `_EncapsulationProtocol` | string | `"TCP/IP"`; same condition | same name |

Offered later, with their settings: `_ConnectTimeout` (DEV-16 `connect_timeout_ms`, in seconds as
KEPServerEX reports it) and `_InterRequestDelay` (DEV-17). Not offered: `_AutoDemotionDiscardWrites` (no writes), `_DemandPoll`, `_LastDemandPoll` and every
other writable action tag (SYS-09; demand reads are `POST /api/v1/devices/{id}/read-once`).

### 8.3 `<Channel>.<Device>._Statistics.*`

The device's own counters, with the channel's names (KEPServerEX keeps statistics per channel;
opencap also publishes them per device, because its counters are per device).

| Tag | dtype | Update | Value |
|---|---|---|---|
| `_SuccessfulReads` | uint32, wraps | event | `poll_requests_total{device,result="ok"}` |
| `_FailedReads` | uint32, wraps | event | `poll_failed_reads_total{device}` (new): block reads that ended `bad_comm` after all retries, one per block per cycle |
| `_RxBytes` | uint32, wraps | event | `poll_rx_bytes_total{device}` (new); present when the driver counts bytes |
| `_TxBytes` | uint32, wraps | event | `poll_tx_bytes_total{device}` (new); same condition |
| `_RequestsOk` | uint64 | event | `poll_requests_total{device,result="ok"}` |
| `_Timeouts` | uint64 | event | `…{result="timeout"}` |
| `_ConnErrors` | uint64 | event | `…{result="conn_error"}` |
| `_BadConfig` | uint64 | event | `…{result="bad_config"}` |
| `_OtherErrors` | uint64 | event | `…{result="error"}` |

`poll_requests_total` counts every attempt, so one failed block read with `retries: 2` adds 3 to
`_Timeouts` and 1 to `_FailedReads`. Counters live as long as the process (like `/metrics`) and
survive a reload; `_ErrorCount` follows the worker and restarts at 0 when a reload restarts the
channel, like `errors` in `GET …/status`.

### 8.4 Reserved

`_ActivePath` (DEV-18, the active communication path) is added by that feature.

## 9. Device state behaviour

The health state machine is `opencap.core.health.DeviceHealth` / `opencap_core::health`
(unchanged). The system-tag values are functions of its fields plus two new status fields that
both workers maintain:

- `error_since`: the UTC time of the cycle that took `consecutive_failures` from 0 to 1; cleared
  by a good cycle. A neutral cycle (defined in `src/opencap/core/health.py`) changes nothing.
- `last_connect_failed`: set by each connect attempt (true on failure, false on success); false
  before the first attempt and after a reload.

| Device situation | `_State` | `_Error` | `_NoError` | `_SecondsInError` | `_AutoDemoted` |
|---|---|---|---|---|---|
| enabled, no cycle counted yet (start or reload) | `offline` | false | true | 0 | false |
| last counted cycle good | `ok` | false | true | 0 | false |
| failed cycles, fewer than `demote_after` (or demotion off) | `offline` | true | false | ⌊now − `error_since`⌋ | false |
| demoted (also while waiting for the probe, and after a failed probe) | `demoted` | true | false | ⌊now − `error_since`⌋, still counting | true |
| good probe after demotion | `ok` | false | true | 0 | false |
| only neutral cycles (every block rejected with `BlockConfigError`) | unchanged | unchanged | unchanged | unchanged | unchanged |
| device disabled, or on a disabled channel | `disabled` | false | true | 0 | false |

Formally `_Error = consecutive_failures > 0` and `_State = "disabled"` whenever the device or its
channel is disabled. A disabled device has no worker: `_Connected` and `_FailedConnection` are
false, `_LastGood` is the placeholder, `_ErrorCount` and `_CycleMs` are 0, `_LastError` is `""`;
its `_Statistics` counters keep their values.

**Change from the OPC UA server as built (D-046):** it serves `_Error = (state != "ok")`, which is
`true` before the first cycle; KEPServerEX's `_Error` is false until a cycle fails, and so is this
spec's. `_ErrorCount` becomes `uint64` (was `UInt32`, saturating). Disabled devices get their
`_System` and `_Statistics` folders (their I/O tags stay hidden), so a screen can show "disabled"
instead of `BadNodeIdUnknown`.

## 10. North interfaces

| Interface | Rule |
|---|---|
| OPC UA server | Folders `Objects/_System`, `Objects/_System/_North/<agent>`, `Objects/_System/_Redundancy`, `Objects/<Channel>/_System`, `Objects/<Channel>/_Statistics`, `Objects/<Channel>/<Device>/_System`, `Objects/<Channel>/<Device>/_Statistics`; NodeId `ns=<idx>;s=<path>`; DataType per §4; AccessLevel and UserAccessLevel `CurrentRead`; a Write returns `BadNotWritable` or `BadUserAccessDenied` and changes nothing; StatusCode always `Good`. |
| SubDevice JSON (MQTT) | Off by default. With the device's `publish.include_system_tags: true`, the device's `_System.*` and `_Statistics.*` tags are added to `Data` after the I/O tags, in catalogue order, keyed by their device-relative path (`"_System._Error"`). They never appear in `BadTags`, never affect `DataValid` or `DateTime`, and do not trigger an `on_change` publish on their own (clock-like churn would defeat the deadband). Node and channel tags are not in SubDevice JSON. |
| MQTT status topics | Unchanged. |
| Sparkplug B (#51) | Node and channel tags are node metrics named with `/` for `.` (`_System/_DateTime`, `Plant A/_Statistics/_FailedReads`); device tags are device metrics (`_System/_Error`). Types per §4. |
| Modbus TCP server (#52) | A system tag can be mapped by path like any tag; read-only by construction. |
| Config API | `GET /api/v1/system/tags[?prefix=<path prefix>]` (viewer) returns `{items: [{path, dtype, value, quality, source_ts}], revision}`; `GET /api/v1/devices/{id}/values?include_system=true` adds the device's system and statistics tags. |

### 10.1 Config schema change

`PublishCfg.include_system_tags: bool = false` (`devices.json` device `publish`, the map and
overrides `publish` blocks). Additive (ADR-0003): both engines' models, the JSON schema and a
config conformance fixture (`tests/conformance/config/valid/`) change in the same PR.

## 11. One source: tags against metrics, health and status

| Tags | Equal to |
|---|---|
| device `_State`, `_LastGood`, `_ErrorCount`, `_CycleMs`, `_LastError`, `_Connected` | `GET /api/v1/devices/{id}/status` fields; `_State` (except `disabled`) also `device_state` (0/1/2) |
| device `_Overruns`; channel `_Overruns` | `poll_overrun_total{device}`; its sum over the channel |
| device and channel `_Statistics` | `poll_requests_total`, `poll_failed_reads_total`, `poll_rx_bytes_total`, `poll_tx_bytes_total` (wrapping counters modulo 2³²) |
| `_DevicesOk/Offline/Demoted` | `/health` `devices` |
| `_MqttConnected`, `_OutboxMessages` | `/health` `mqtt_connected`, `saf_queue` |
| `_North.<agent>.*` | the metric named in §6 |
| `_ProjectRevision` | the API's `revision` |

## 12. Deliberate differences from KEPServerEX

| KEPServerEX | opencap | Why |
|---|---|---|
| Parameter-control and action tags are writable (`_Enabled`, `_DeviceId`, `_RequestTimeout`, `_DemandPoll`, `_Reset`, …) | all read-only, or not offered | ADR-0008; changes go through the audited API (SYS-09) |
| `_Date`, `_Time`, `_DateTime*` formatted per the Windows locale | fixed ISO-8601 forms of §5.2; `_DateTimeLocal` carries its UTC offset | deterministic across hosts and engines |
| `_DeviceId` is the driver's Device ID string (for example IP plus unit id) | `<host>:<port>` or the serial device path | one rule for every driver; the unit or station id is in the device config |
| `_ScanMode` values are KEPServerEX's scan modes | `"tag_scan_rate"`, opencap's only mode | other modes are not implemented |
| statistics per channel, only with diagnostics on | per channel and per device, always on | the counters exist anyway for `/metrics` |
| write statistics, `_NextReadPriority`, `_Reset` | not offered | no writes, no priorities, monotonic counters |
| `_ActiveTagCount` = tags on scan for clients | tags scanned (all enabled, plan-accepted, not demoted) | opencap scans independently of clients |
| licensing tags, `_FullProjectName`, network adapter tags, `_FloatHandlingType`, `_SharedConnection` | not offered | §5.4, §7.1 |
| — | added: `_State`, `_Connected`, `_LastGood`, `_LastError`, `_ErrorCount`, `_CycleMs`, `_Overruns`, `_Driver`, `_Version`, `_Engine`, `_Instance`, `_StartTime`, `_UptimeS`, `_ProjectRevision`, device-state counts, `_North`, `_Redundancy` | FR-10 |

Names follow KEPServerEX V6 as summarised in `plan/features/07-tag-management-and-system-tags.md`.
Two details are not confirmed against a running KEPServerEX and are pinned here as opencap's
rule: `_Date_DayOfWeek` counts from 0 = Sunday, and `_Time_Hour` reports 12 for the hours 00
and 12. If a site finds otherwise, the fix is a decision-log entry and a vector change.

## 13. Conformance and acceptance

### 13.1 Vectors

The vectors are the shared conformance area [`tests/conformance/system_tags/`](../../tests/conformance/system_tags/)
(format and regeneration: [`tests/conformance/README.md`](../../tests/conformance/README.md)), plus
[`tests/conformance/system_tags_catalogue.json`](../../tests/conformance/system_tags_catalogue.json):
every tag of this spec as data (level, name, dtype, update class, presence condition, KEPServerEX
name or not). An engine's address-space test compares its system folders against it.

Two kinds of vector:

- `snapshot`: an engine state (node, north agents, channels with device config, status and
  counters, at a fixed `now` and zone) → the expected `{path: {dtype, value}}` for the listed
  paths, plus paths that must be `absent`. Both engines implement the pure function
  *state → system tags* (`opencap.core.system_tags.evaluate`) and run every snapshot vector against it.
- `device_sequence`: a device config and a list of timed events (`connect_ok`,
  `connect_failed`, `cycle_good`, `cycle_failed`, `cycle_neutral`, `tick`) with the expected
  device `_System` tags after given steps. Both engines feed the events into their
  `DeviceHealth` plus the two status fields of §9.

The vector files are generated from the Python engine; the cases and the values this spec demands are
written by hand in `scripts/conformance_system_tags/`, and
`tests/conformance/system_tags/test_system_tag_vectors.py` checks the engine against those values
and that every catalogue name is documented here.

### 13.2 Acceptance tests each engine must pass

1. All vectors, through the engine's own state → tags function.
2. **Kill one device** (Phase 1 acceptance, both engines): with a simulator stopped, over OPC UA,
   `_Error` becomes true within one cycle after the first failed cycle, `_SecondsInError` grows,
   `_AutoDemoted` becomes true after `demote_after` failed cycles, `_DevicesDemoted` = 1; after
   restarting the simulator, the probe brings `_Error` false and `_State` `ok`. At each step the
   tags equal `GET …/status` and `/metrics` (§11).
3. **Read-only:** an OPC UA Write to `_System._DateTime`, a device `_Error` and a channel
   `_Statistics._FailedReads` returns a Bad status and the values do not change (L1 suite, #29).
4. **Browse:** the OPC UA address space contains exactly the catalogue's folders and tags for the
   config (presence conditions applied), with the §4 DataTypes.
5. **Payload:** with `include_system_tags` off, SubDevice JSON is byte-identical to today's; with
   it on, the `_System`/`_Statistics` keys follow the I/O tags and `DataValid`/`BadTags` are
   unchanged (payload vectors, #3).
6. **Clock:** with a fixed clock, the clock tags match the vectors in `Asia/Kolkata`, `UTC` and
   `America/New_York` on both sides of a DST change.
7. **Counters:** a simulator run with a known number of good, timed-out and refused requests gives
   the expected `_Statistics` values, equal to `/metrics`.

## 14. Follow-up implementation issues

| Issue | What | Status |
|---|---|---|
| [#120](https://github.com/sharmaniraj009/opencap/issues/120) | core: system-tag evaluation and status fields (Python): the state → tags function, `error_since`, `last_connect_failed`, `poll_failed_reads_total`, in-flight tracking; the conformance test runs against it | in review |
| [#122](https://github.com/sharmaniraj009/opencap/issues/122) | north: OPC UA server serves the full catalogue (Python), with the §9 changes to D-046 | `blocked` on #120 |
| [#125](https://github.com/sharmaniraj009/opencap/issues/125) | north: `publish.include_system_tags` (config in both engines, Python payload) | `blocked` on #120 |
| [#127](https://github.com/sharmaniraj009/opencap/issues/127) | api: `GET /api/v1/system/tags`, `include_system` on device values (Python, OpenAPI) | `blocked` on #120 |
| [#128](https://github.com/sharmaniraj009/opencap/issues/128) | core: reserved names of §3.2 in config validation (both engines) | `ready` |
| [#130](https://github.com/sharmaniraj009/opencap/issues/130) | driver: wire byte counters for `_RxBytes`/`_TxBytes` (Python drivers first) | `ready` |
| [#56](https://github.com/sharmaniraj009/opencap/issues/56) | engine-rust: everything above in Rust, through the same vectors | `blocked` on #22, #41 |

The north-agent tags that need SAF-06 and SAF-14 (`_OutboxBytes`, `_OutboxAgeS`,
`_OutboxCapacityBytes`, `_OutboxFull`, `_OutboxDiskFreeBytes`, `_OutboxDiskLow`) hold their
placeholder until those features are filed and built.

## 15. Decisions from the review (#146)

- The node-level clock tags are **not** suppressible (no `system_tags.clock: false`) until a
  measurement shows a cost for OPC UA servers with very many subscribed clients.
- `_ClientCount` **excludes** API and UI sessions (they are not data clients). A separate
  `_ApiClientCount` can be added when the WebSocket feed (#31) exists.
