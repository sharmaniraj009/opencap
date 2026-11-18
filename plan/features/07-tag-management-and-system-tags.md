# 07 Tag management and system tags

> Source: KEPServerEX V6 server manual (© PTC), pages 109–138, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
Tag management covers ways to build a tag database: groups, drag and drop, automatic generation
(plan 05, DEV-12) and CSV export/import of a device's or group's tags so they can be edited in a
spreadsheet (fixed column set, comma or semicolon delimiter). The server also publishes special tags:
**system tags** under a `_System` branch at server, channel and device level, split into read-only
status tags (errors, demotion, clocks, counts, project and product names) and read/write parameter
control tags that change channel or device settings from a client (baud rate, device id, timeouts,
enable, demand poll, tag generation); **property tags** that expose a tag's own settings by
appending a property name to its path; **statistics tags** per channel (read/write counts, bytes,
pending requests) when diagnostics are on; **modem tags** for dial-up channels; and
**communication serialization tags** for channels in a virtual network (manual p. 109–138).
Store-and-forward tags are in plan 11; diagnostics capture and the event log are in plan 12.

## opencap today
- **CSV:** none yet. KEPServerEX-compatible tag CSV import/export is issue #10 (M1): `read_kep_csv` /
  `write_kep_csv` in `src/opencap/config/kep_csv.py`, `opencap tags export|import`.
- **Health and counters exist, as API and metrics, not as tags:** device state `ok|offline|demoted`,
  `last_good`, `errors`, `cycle_ms`, `last_error`, `connected` (`GET /api/v1/devices/{id}/status`,
  `src/opencap/core/health.py`, `channel_worker.py`); `/health`; Prometheus `poll_requests_total
  {device,result}`, `poll_cycle_seconds`, `poll_overrun_total`, `device_state`, `saf_queue_messages`,
  `mqtt_published_total`; MQTT status topics per device (README). Rust has the same metrics and health
  (#64).
- **System tags:** spec issue #22 (M3: `docs/spec/system-tags.md` plus vectors in
  `tests/conformance/system_tags/`), Rust implementation #56 (M3, after #22 and the tag store #41).
  Design: system tags are virtual devices in the tag store (ADR-0005), always `good`, never polled,
  read-only, kept out of SubDevice JSON `Data` unless `publish.include_system_tags` (#22). RFC 0001 §7
  lists diagnostics/system tags under M5; the issues say M3.
- **Property tags, modem tags, serialization tags:** nothing.

## Features

### SYS-01 CSV export of a tag list (manual p. 109–110)
- **What:** Export the tags of a device or tag group to a CSV file with a header row, one row per tag,
  and columns for name (with group prefix), address, data type, respect-data-type flag, client access,
  scan rate, scaling fields, units, description and negate; useful as a template.
- **Decision:** Adopt — same header and spellings as KEPServerEX so files move both ways (#10).
- **Status:** Planned (#10)
- **Requirements:**
  - `opencap tags export <device-id> out.csv [--group <path>] [--delimiter ,|;]` writes the KEPServerEX
    header (`Tag Name`, `Address`, `Data Type`, `Respect Data Type`, `Client Access`, `Scan Rate`,
    `Scaling`, `Raw Low`, `Raw High`, `Scaled Low`, `Scaled High`, `Scaled Data Type`, `Clamp Low`,
    `Clamp High`, `Eng Units`, `Description`, `Negate Value`) from the loaded config, overrides applied.
  - Client Access is always written as read only; group paths become dotted `Tag Name` prefixes.
  - Beyond #10: `GET /api/v1/devices/{id}/tags?format=csv` (viewer role) and an Export button on the
    device page.
- **Acceptance:** #10's round-trip property test (`write` then `read` gives identical tags) with the
  synthetic fixture `tests/fixtures/kep_tags_sample.csv`; API test for the CSV route.

### SYS-02 CSV import of a tag list (manual p. 109–111)
- **What:** Re-import an edited CSV into the same or another device or group; files with other
  delimiters must be converted to comma or semicolon first.
- **Decision:** Adopt — same mapping rules as `import-kep` (Char → `int16` with a warning, BCD/LBCD/Date
  disabled, Default → device default, Read/Write → read only with a warning, ADR-0008).
- **Status:** Planned (#10)
- **Requirements:**
  - `opencap tags import in.csv --driver <name> --out tags.json` (#10) reports every failed row with
    its line number and exits non-zero if any row failed; comma and semicolon are detected from the
    header row.
  - Every imported address is checked by the driver's `plan()`; invalid rows are reported, not dropped
    silently.
  - Beyond #10: `POST /api/v1/devices/{id}/tags:import?dry_run=true` (admin) returns added, changed
    and failed rows; with `dry_run=false` it writes them through the audited store; the import page in
    `/ui` accepts a CSV (plan 14, #33).
- **Acceptance:** #10's unit tests for each column and type mapping and for invalid rows; API dry-run
  and apply tests.

### SYS-03 System tag model: `_System` branch, status and control classes (manual p. 109, 111–112, 129–130)
- **What:** System tags appear under a `_System` branch of the server, each channel and each device;
  status tags only report, parameter-control tags let a client change settings; which ones exist
  depends on the driver.
- **Decision:** Adapt — status tags are adopted with KEPServerEX names where they exist, so SCADA
  screens keep working (#22); parameter-control tags are offered only as read-only mirrors of the
  config (changes go through the audited API, SYS-09).
- **Status:** Planned (#22)
- **Requirements:**
  - `docs/spec/system-tags.md` (#22) lists every tag with name, data type, unit, update rule and
    source, and every deliberate difference from KEPServerEX.
  - Paths: `_System.<tag>` (node), `<Channel>._System.<tag>`, `<Channel>.<Device>._System.<tag>`; the same
    paths in OPC UA (`ns=<idx>;s=<path>`), the API and MQTT.
  - System tags are always `good`, never polled, read-only on every north interface, and excluded from
    SubDevice JSON `Data` unless `publish.include_system_tags: true`.
- **Acceptance:** `tests/conformance/system_tags/*.json` (health/metric state → expected tag values) run
  by Python and Rust (#22, #56); OPC UA browse test shows the `_System` folders.

### SYS-04 Server-level identity and clock tags (manual p. 112–115)
- **What:** Read-only tags with the product name and version, project file name, full path and title,
  and the host clock as date, time, UTC and local timestamps and their parts (day, weekday, month,
  two/four-digit year, hour, 24 h hour, minute, second, PM flag).
- **Decision:** Adapt — keep the KEPServerEX names (`_ProductName`, `_ProductVersion`, `_ProjectName`,
  `_ProjectTitle`, `_DateTime`, `_DateTimeLocal`, `_Date`, `_Time` and the `_Date_*`/`_Time_*` parts)
  next to opencap's own (`_Version`, `_Instance`, `_StartTime`, `_ProjectRevision`, #22).
  `_FullProjectName` is not offered (it would expose host paths).
- **Status:** Planned (#22)
- **Requirements:**
  - `_DateTime` is UTC ISO-8601 with milliseconds; `_DateTimeLocal` uses `payload.timezone`; the parts
    follow the same zone; clock tags update once per second.
  - `_ProjectName` is the config directory name; `_ProjectRevision` the config revision (ETag).
- **Acceptance:** system-tag vectors with a fixed clock and zone (including a DST change) in both
  engines.

### SYS-05 Server-level counts: tags and clients (manual p. 112, 114–115)
- **What:** Number of active tags, number of tags being accessed, number of connected clients and the
  names OPC clients registered.
- **Decision:** Adapt — `_TotalTagCount` = configured enabled tags, `_ActiveTagCount` = tags with a
  subscriber on any north interface, `_ClientCount` = sessions over all north servers plus API
  WebSocket clients; `_OpcClientNames` only with an OPC server that receives client names (OPC UA
  session names, OPC DA bridge later).
- **Status:** Planned (#22)
- **Requirements:**
  - Counts are also exported as metrics (`tag_store_tags`, `opcua_sessions`, 04 §4.5) with the same
    values.
  - `_OpcClientNames` is a string array; empty when no OPC server runs.
- **Acceptance:** OPC UA server test with two sessions: `_ClientCount` = 2 and both names listed;
  vectors for the counts.

### SYS-06 Licensing tags (manual p. 113–115)
- **What:** Lists of licensed, time-limited and expired features, and a legacy demo flag.
- **Decision:** Out of scope — opencap has no licensing or activation (01 "Out of scope").
- **Status:** Out of scope

### SYS-07 Channel-level system tags (manual p. 115–123)
- **What:** Per channel: description, whether the port is shared, available network adapters, and the
  serial/Ethernet settings (baud rate, COM id, data bits, parity, stop bits, flow control, RTS timings,
  float handling, inter-device delay, network adapter, encapsulation port/protocol, diagnostics switch,
  error reporting, write duty cycle) as readable and writable values.
- **Decision:** Adapt — read-only mirrors of the effective config under the KEPServerEX names
  (`_Description`, `_SharedConnection`, `_BaudRate`, `_DataBits`, `_Parity`, `_StopBits`, `_ComId`,
  `_FloatHandlingType`, `_InterDeviceDelayMS`, `_EncapsulationPort`, `_EncapsulationProtocol`) where the
  setting exists in opencap; `_AvailableNetworkAdapters` and `_NetworkAdapter` are not offered (host
  details); write duty cycle is out (CH-15 in plan 04).
- **Status:** Planned (#22)
- **Requirements:**
  - #22's list is extended with the channel mirrors above, each mapped to its config key (for serial
    settings, the value shared by the channel's devices; empty when they differ).
  - Values change only through config reload; a write from any north client is refused.
- **Acceptance:** system-tag vectors from config fixtures (serial and Ethernet channels) in both engines.

### SYS-08 Device-level system tags (manual p. 104, 123–129)
- **What:** Per device: error flag and its inverse, seconds in error, demoted flag, failed-connection
  flag, scan mode and rate, description, simulated flag, plus readable/writable settings (device id,
  enabled, request timeout and attempts, connect timeout, inter-request delay, demotion settings,
  encapsulation IP/port/protocol).
- **Decision:** Adapt — status tags with the KEPServerEX names and meaning, computed from device
  health; settings as read-only mirrors (#22 adds opencap's `_State`, `_LastError`, `_LastGood`,
  `_CycleMs`, `_Overruns`).
- **Status:** Planned (#22)
- **Requirements:**
  - `_Error` is true when the last counted cycle failed or the device is demoted, false after a good
    cycle; `_NoError` is its inverse; `_SecondsInError` counts from the first failed cycle (0 when not in
    error); `_AutoDemoted` = state `demoted`; `_FailedConnection` = the last connect attempt failed.
  - Mirrors: `_DeviceId` (host, or host plus station/unit id), `_Enabled`, `_RequestTimeout` (=
    `timeout_ms`), `_RequestAttempts` (= `retries` + 1, KEPServerEX counting), `_ConnectTimeout`,
    `_InterRequestDelay`, `_AutoDemotionEnabled` (`demote_after` > 0), `_AutoDemotedFailureCount` (=
    `demote_after`), `_AutoDemotionIntervalMS` (= `demote_for_ms`), `_ScanRateMs` (= `scan_ms`),
    `_ScanMode` (opencap's mode name, a listed difference), `_Description` (DEV-02), `_Encapsulation*`
    for serial devices on `transport: tcp`.
  - `_Simulated` is true only for devices on the `simulator` driver (DEV-04).
  - Every value equals what `GET /api/v1/devices/{id}/status` and `/metrics` report at the same moment.
- **Acceptance:** #22 vectors (health transitions → tag values); Phase 1 "kill one device" acceptance
  also asserts `_Error`, `_AutoDemoted` and `_SecondsInError` over OPC UA on both engines (#56).

### SYS-09 Writable parameter-control and action tags (manual p. 112, 115–129, 132, 137–138)
- **What:** Writing system tags to change a channel or device setting (line settings, device id,
  enable, timeouts, demotion, simulation), to trigger an action (demand poll, tag database creation,
  counter reset) or to change serialization settings.
- **Decision:** Out of scope — every north interface is read-only (ADR-0008); opencap's own settings
  change only through the authenticated, audited API, and any write path needs RFC 0002 write safety
  (#19). Equivalents today: `PATCH /api/v1/devices/{id}` (enabled, host, timeouts, demotion, params),
  `POST /api/v1/devices/{id}/read-once` (demand poll), `opencap browse` (#11, tag generation).
- **Status:** Out of scope

### SYS-10 Property tags (manual p. 109, 130–131)
- **What:** Read-only access to a tag's own settings by appending a property name to its path: name,
  address, description, raw data type and every scaling field including units.
- **Decision:** Adapt — OPC UA clients get the standard attributes and properties (`Description`,
  `EURange`, `EngineeringUnits`, 03 §3.5); for clients that address KEPServerEX property items by
  name, the OPC UA server also resolves `<tag path>._Description`, `._Address`, `._RawDataType`,
  `._ScalingType`, `._ScalingRawLow`/`High`, `._ScalingScaledLow`/`High`, `._ScalingClampLow`/`High`,
  `._ScalingUnits` as read-only variables.
- **Status:** Not started
- **Requirements:**
  - Property items are off the browse tree unless `opcua.browse_tag_properties: true`, and are never
    polled (values come from config).
  - When a tag has no scaling, scaling properties return the `Scale` defaults, as KEPServerEX does.
- **Acceptance:** OPC UA client test reads each property item of a scaled and an unscaled tag (with the
  OPC UA server, plan 02).

### SYS-11 Statistics tags (manual p. 109, 131–133)
- **What:** Per channel, when diagnostics are on: successful and failed reads and writes, bytes
  received and sent, current and maximum pending reads and writes, the priority of the next read, and a
  reset; counters roll over at 32 bits.
- **Decision:** Adapt — always available (the counters already exist for `/metrics`), under
  `<Channel>._Statistics`; read-side names kept (`_SuccessfulReads`, `_FailedReads`, `_RxBytes`,
  `_TxBytes`, `_PendingReads`, `_MaxPendingReads`, `_NextReadPriority`) plus #22's `_RequestsOk`,
  `_Timeouts`, `_ConnErrors`, `_BadConfig`. Write counters and `_Reset` are not offered: there are no
  writes, and counters are monotonic like Prometheus counters.
- **Status:** Planned (#22)
- **Requirements:**
  - `_SuccessfulReads`/`_FailedReads` count wire requests per channel and equal the sums of
    `poll_requests_total{result}` over the channel's devices; a failed read counts once, after retries.
  - `_RxBytes`/`_TxBytes` need a byte counter in each driver's transport (new metrics
    `channel_rx_bytes_total`, `channel_tx_bytes_total`).
  - `_PendingReads` is 0 or 1 by the one-request-per-channel rule (D-015) unless per-channel
    concurrency is enabled.
- **Acceptance:** simulator test with a known number of good and failed requests; vectors for the
  mapping from metrics to tags; both engines.

### SYS-12 Modem tags (manual p. 109, 133–136)
- **What:** For channels on a modem: status (bit field and text), last event (code and text), the
  number actually dialled, and writable dial, hang-up, mode and phone number tags.
- **Decision:** Defer — follows plan 08 (COM-01): no modem support is planned. If adopted, only the
  status tags (`_Status`, `_StringStatus`, `_LastEvent`, `_StringLastEvent`, `_DialNumber`) as
  read-only values; client control of the modem (dial, hang up, mode, number) is COM-04 in
  plan 08 and out of scope.
- **Status:** Not started
- **Requirements:**
  - With COM-01: `<Channel>._Modem.<tag>` read-only, codes documented in `docs/spec/system-tags.md`.
- **Acceptance:** with COM-01, a fake-modem pty test drives every status transition.

### SYS-13 Communication serialization tags (manual p. 109, 136–138)
- **What:** For channels in a virtual network: whether the channel owns the network now, whether and
  where it is registered, how often and how long it held ownership (count, total and average seconds),
  transactions per cycle, the network it belongs to, and a statistics reset.
- **Decision:** Adapt — with CH-14 (plan 04): read-only status tags under
  `<Channel>._CommunicationSerialization` (`_NetworkOwner`, `_Registered`, `_RegisteredTo`,
  `_StatisticNetworkOwnershipCount`, `_StatisticNetworkOwnershipTimeSec`,
  `_StatisticAvgNetworkOwnershipTimeSec`, `_TransactionsPerCycle`, `_VirtualNetwork`); no reset.
- **Status:** Not started
- **Requirements:**
  - Values come from the serialization group's lock and match the metrics
    `serialization_turns_total{channel}` and `serialization_owned_seconds_total{channel}` (CH-14).
- **Acceptance:** the CH-14 simulator test also checks the tag values over OPC UA; both engines.

## Open questions
- Milestone of system tags: issues #22 and #56 say M3, RFC 0001 §7/§8 put them in M5 (R5). Which one
  holds?
- Should KEPServerEX write-side statistics (`_SuccessfulWrites`, `_FailedWrites`, `_PendingWrites`) be
  published as constant 0 so existing SCADA screens bind without errors, or left out?
- Is `_ClientCount` meant to include API and UI sessions, or only data clients (OPC UA, Modbus server)?
- Should the CSV routes in the config API (SYS-01/02) be added to #10 or filed as a follow-up?
- Channel-level serial mirrors (SYS-07) only make sense when all devices on a channel share one line;
  is "empty when they differ" acceptable?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| SYS-01 | CSV export of a tag list | Adopt | Planned (#10) | #10 | M1 |
| SYS-02 | CSV import of a tag list | Adopt | Planned (#10) | #10 | M1 |
| SYS-03 | System tag model: `_System` branch, status and control classes | Adapt | Planned (#22) | #22, #56 | M3 |
| SYS-04 | Server-level identity and clock tags | Adapt | Planned (#22) | #22, #56 | M3 |
| SYS-05 | Server-level counts: tags and clients | Adapt | Planned (#22) | #22, #56 | M3 |
| SYS-06 | Licensing tags | Out of scope | Out of scope | — | — |
| SYS-07 | Channel-level system tags | Adapt | Planned (#22) | #22, #56 | M3 |
| SYS-08 | Device-level system tags | Adapt | Planned (#22) | #22, #56 | M3 |
| SYS-09 | Writable parameter-control and action tags | Out of scope | Out of scope | #19 | — |
| SYS-10 | Property tags | Adapt | Planned (#119) | #119 | M3 |
| SYS-11 | Statistics tags | Adapt | Planned (#22) | #22, #56 | M3 |
| SYS-12 | Modem tags | Defer | Planned (#109) | #109 | — |
| SYS-13 | Communication serialization tags | Adapt | Planned (#104) | #104 | — |
