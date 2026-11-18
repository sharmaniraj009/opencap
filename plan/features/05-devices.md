# 05 Devices

> Source: KEPServerEX V6 server manual (© PTC), pages 86–95, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
A device is one PLC, controller or other target on a channel, limited to what the channel's
driver supports. It has a name (part of the browse path `Channel.Device`), a description, a model
and a driver-specific id (station number in decimal, octal or hex, or an IP address). Its
operating mode turns data collection on or off and can put it into a simulation mode that returns
memory values without talking to the hardware. Further groups set the scan mode (client rate,
a floor rate, a forced rate, demand poll only, or per-tag rates) and initial updates from cache;
auto-demotion of a device that stops answering; the terminal-server address for encapsulated
serial devices; automatic tag database generation; the device time zone and clock
synchronisation; timeouts, attempts and an inter-request delay; and media-level redundancy as a
plug-in (manual p. 86–95).

## opencap today
- **Model:** `Device` in `src/opencap/config/models.py`: `id` = `<channel>/<device>`, `channel`,
  `name`, `driver`, `host`, `port`, `params` (station, unit id, CIP path, …, see
  `docs/driver_params.md`), `scan_ms` (1000), `min_scan_ms` (1000), `timeout_ms` (3000), `retries` (2),
  `demote_after` (3), `demote_for_ms` (30000), `enabled`, `publish`, `tags`, `source` (KEPServerEX
  provenance: driver, model, id string, scan mode). Field overrides in `overrides.yaml` `devices`.
- **Runtime:** rate groups `max(tag.scan_ms or device.scan_ms, min_scan_ms)`, retries, reconnect
  backoff 1 s → 60 s, auto-demotion with one probe, stale after 3 × rate, `read_once`
  (`src/opencap/core/channel_worker.py`, `health.py`, `scheduler.py`; D-015). Ported to Rust in #64
  (RFC 0001 §2.4); #78 tracks a flaky Rust test of the one-request rule.
- **Import:** `opencap import-kep` maps scan mode, request timeout, attempts (`retries` = attempts − 1),
  demotion settings, data collection (disabled devices are excluded and listed), and driver ids
  (`src/opencap/config/kep_import.py`, D-010).
- **API/UI:** `GET/PUT/PATCH/DELETE /api/v1/devices/{id}`, `GET …/status`, `/values`, `/plan`,
  `/identity`, `POST …/read-once`; device page and config editor in `/ui` (#63). Rust: admin routes
  `/devices/{id}`, `/values`, `/read-once`. Metrics `device_state`, `poll_requests_total`,
  `poll_cycle_seconds`, `poll_overrun_total`.
- Not present: device description, demand-poll-only mode, client-driven scan rate, separate connect
  timeout, inter-request delay, device time zone, media-level redundancy, automatic tag generation
  (planned in #11).

## Features

### DEV-01 Device object: name, channel, driver, model, id (manual p. 86–87)
- **What:** A device has a logical name used in the browse path, belongs to one channel, uses the
  channel's driver, has a model where the driver knows several, and a driver-specific id (numeric
  station in a chosen base, an IP address, or a string with extra routing fields).
- **Decision:** Adapt — model and id become `driver`, `host`/`port` and typed `params` (`unit_id`,
  `station`, `path`, `micro800`, …); the raw KEPServerEX model and id string stay in `source`.
- **Status:** Done
- **Requirements:**
  - Device ids are unique across the project (`DevicesFile` validator); `POST
    /api/v1/channels/{channel}/devices` with an existing name answers 409.
  - Every driver's params are documented in `docs/driver_params.md` with their KEPServerEX source key.
  - Station ids whose number base is uncertain are flagged in `reports/import_summary.json` (D-037).
- **Acceptance:** `tests/unit/test_kep_import.py`, `tests/unit/test_models.py`,
  `tests/unit/test_config_api.py`; config conformance fixtures shared with Rust (#39).

### DEV-02 Device description (manual p. 87)
- **What:** Free-text description per device, also exposed as a system tag.
- **Decision:** Adopt
- **Status:** Not started
- **Requirements:**
  - `Device.description: str | null` (additive, ADR-0003), imported from the KEPServerEX description.
  - Returned by `GET /api/v1/devices/{id}`, editable in the config editor, exposed as
    `<Channel>.<Device>._System._Description` (SYS-08 in plan 07) and as the OPC UA folder description.
- **Acceptance:** importer and API unit tests; config fixture; UI smoke test.

### DEV-03 Data collection on/off (manual p. 87)
- **What:** A device can be switched off: no requests go to it, clients see its data as invalid,
  and the switch works while running, also from a system tag.
- **Decision:** Adapt — `enabled` in `devices.json` or `overrides.yaml`, changed through the audited
  API (`PATCH /api/v1/devices/{id}` or the override routes) and applied by reload; the writable
  `_Enabled` tag is not offered (SYS-09 in plan 07).
- **Status:** Done
- **Requirements:**
  - A disabled device sends no request and publishes nothing; `opencap check` shows it `(disabled)`.
  - `POST …/read-once` still works on a disabled device (D-015) for commissioning.
  - Import: a device with data collection off is excluded and listed in `reports/import_summary.json`
    (open question: import it with `enabled: false` instead).
- **Acceptance:** `tests/unit/test_channel_worker.py`, `tests/unit/test_loader.py`,
  `tests/integration/test_runtime_reload.py`; Rust worker tests (#64).

### DEV-04 Simulated mode (manual p. 87–88)
- **What:** The device stops real communication but keeps returning valid data from memory that
  echoes what was written; meant for testing only.
- **Decision:** Adapt — test benches use the `simulator` driver (#12) or the protocol simulators in
  `tests/helpers/`; a production device is never switched to fake values, because `good` means a real
  read (ADR-0006) and there are no writes to echo (ADR-0008).
- **Status:** Planned (#12)
- **Requirements:**
  - Driver `simulator` per #12 (ramp, sine, random, square, constant, memory registers, fault
    switches), read-only by construction.
  - Beyond #12: a device can be moved to the simulator for a test bench by an override (`driver:
    simulator`, `params`), and `opencap check` warns when any device in the project uses `simulator`.
- **Acceptance:** #12's unit tests with a fake clock and its 50-device integration test; UI smoke
  test showing every quality.

### DEV-05 Static tag count per device (manual p. 88)
- **What:** Number of static tags defined under the device.
- **Decision:** Adopt
- **Status:** Done
- **Requirements:**
  - `GET /api/v1/devices` and `GET /api/v1/channels/{channel}` return `tags` per device; `GET
    /devices/{id}` (admin API) returns `tags` and `bad_config`.
- **Acceptance:** `tests/unit/test_config_api.py`, `tests/integration/test_runtime_device_info.py`.

### DEV-06 Scan rates: tag rates, floor rate, forced rate (manual p. 88–89, 97–98)
- **What:** Scan modes that honour each tag's own rate, cap how fast data is requested ("no faster
  than"), or force every tag to one rate.
- **Decision:** Adapt — tags with the same effective rate form a rate group; `min_scan_ms` is the
  floor; the forced rate is imported as device `scan_ms` with tag rates dropped (D-010).
- **Status:** Done
- **Requirements:**
  - Effective rate = `max(tag.scan_ms or device.scan_ms, device.min_scan_ms)`; groups run fastest first
    with start jitter ≤ 5 % (NFR-4); overruns counted in `poll_overrun_total`.
- **Acceptance:** `tests/unit/test_scheduler.py`, `tests/unit/test_models.py`; Phase 1 acceptance with
  `OPENCAP_ENGINE=python|rust`.

### DEV-07 Client-driven scan rate (manual p. 88)
- **What:** The default KEPServerEX mode: each tag is scanned at the rate its subscribing client asks
  for.
- **Decision:** Defer — M3, with the OPC UA server (FR-3, 03 §3.5): the scheduler uses the fastest
  sampling interval any client requests, floored by `min_scan_ms`; off until the server reports
  rates.
- **Status:** Not started
- **Requirements:**
  - Device `scan_mode: client` uses `max(requested, min_scan_ms)` for tags without a fixed rate;
    tags nobody subscribes to fall back to `scan_ms`.
  - Importer: KEPServerEX mode "respect client rate" maps to `scan_mode: client` once it exists
    (today it imports the tag rates, which KEPServerEX ignores in that mode).
- **Acceptance:** OPC UA server L1 test: two clients with different sampling intervals; the
  simulator sees requests at the faster rate, never faster than `min_scan_ms`.

### DEV-08 Demand poll only and `_DemandPoll` (manual p. 88–89, 124–125)
- **What:** A mode with no periodic scanning and no initial read: data is read only when a client
  asks explicitly or triggers a device-wide poll.
- **Decision:** Adapt — `scan_mode: demand` leaves the device unscheduled; reads come from `POST
  /api/v1/devices/{id}/read-once` (done) or from north demand reads where `allow_demand_reads` is
  set (03 §3.5). The `_DemandPoll` write trigger is replaced by the API route (SYS-09 in plan 07).
- **Status:** Not started
- **Requirements:**
  - Device `scan_mode: periodic | demand` (default `periodic`); a `demand` device has no rate groups,
    its tags start `stale` and stay so between reads; status shows `demand`.
  - Importer maps KEPServerEX demand-poll mode to `scan_mode: demand` (today: tag rates plus a warning).
- **Acceptance:** worker unit test (no request without read-once); simulator test counting requests;
  both engines.

### DEV-09 Initial updates from cache (manual p. 89)
- **What:** A newly subscribed item gets its first value from the cache instead of a fresh device
  read when another client already reads the same item.
- **Decision:** Adapt — every north read is served from the cache / tag store; a device read happens
  only through read-once or an explicitly allowed demand read (03 §3.5).
- **Status:** Done
- **Requirements:**
  - The MQTT publisher and `GET /api/v1/devices/{id}/values` serve cached values; only `read-once`
    adds a request to the device.
  - Follow-up in plan 02: the OPC UA server keeps the rule (#74 in Python, #50 in Rust), so
    subscriptions never trigger device reads and a demand read needs `allow_demand_reads` and happens
    at most once per `min_scan_ms` (03 §3.5).
- **Acceptance:** `tests/unit/test_cache.py`, `tests/integration/test_publisher.py`.

### DEV-10 Auto-demotion (manual p. 89, 139)
- **What:** After a number of successive failed request cycles (1–30, default 3) a device is taken off
  scan for a period (0.1 s to 1 h, default 10 s); its data is bad meanwhile; then one attempt decides
  whether it returns. Writes can be discarded while demoted.
- **Decision:** Adapt — `demote_after` (failed cycles, `<= 0` turns demotion off) and `demote_for_ms`;
  the connection is closed while demoted; one probe at the end (D-015). Discarding writes does not
  apply (ADR-0008).
- **Status:** Done
- **Requirements:**
  - State `demoted` in `GET …/status`, `device_state` = 2 in `/metrics`, and the device's MQTT status
    topic; `_AutoDemoted` follows with the system tags (SYS-08 in plan 07).
  - A failed probe demotes again for another `demote_for_ms`; a good probe resumes polling.
  - The importer copies the KEPServerEX count and period when demotion is on there.
- **Acceptance:** `tests/unit/test_health.py`, `tests/unit/test_channel_worker.py`; Phase 1 acceptance
  "kill one device, demotion and resume" on both engines.

### DEV-11 Communication parameters / Ethernet encapsulation per device (manual p. 89–91)
- **What:** For a serial device behind a terminal server: the server's IP, port and TCP/UDP; several
  multi-dropped devices may share one IP.
- **Decision:** Adapt — the device's `host`/`port` with `params.transport: tcp`, or the COM-port entry
  in `serial_ports`; devices on one endpoint share one connection (D-033). See CH-11 and CH-12 in plan 04.
- **Status:** Done
- **Requirements:**
  - Several devices with the same gateway `host:port` (stations or unit ids on one line) share one
    connection and take turns (D-033).
- **Acceptance:** `tests/unit/test_serial_config.py`, `tests/integration/test_serial_acceptance.py`.

### DEV-12 Automatic tag database generation (manual p. 91–92, 109, 123)
- **What:** Drivers that can read a device's tag list (or its I/O modules) build server tags: on
  demand, on every start or on the first start; with a policy for tags that already exist (delete
  all first, overwrite matching, keep, keep and log), an optional parent group and optional subgroups.
- **Decision:** Adapt — generation is an operator action that produces a reviewed tag list, never an
  automatic rewrite of the project on start: `Driver.browse()` and `opencap browse` (#11), merged
  into the project through the audited API. Duplicate handling becomes explicit merge options.
- **Status:** Planned (#11)
- **Requirements:**
  - `Driver.browse()` (read/list services only) for `ab_logix` first (#11), then `opcua_client` and
    others; `opencap browse <device-id> --out tags.json|tags.csv`.
  - Merge into a device: `POST /api/v1/devices/{id}/tags` with `on_duplicate: skip | replace | fail`
    and an optional `group` prefix; the response lists added, replaced and skipped tags; audited.
  - Generated tags plan cleanly (`plan()` gives no `invalid` for supported types).
- **Acceptance:** #11's CIP simulator tests (atomic, array, UDT; only read/list services sent);
  API unit test for each `on_duplicate` value.

### DEV-13 Device time zone for device timestamps (manual p. 93)
- **What:** The device's time zone and daylight saving rule, used when converting device timestamps
  to UTC.
- **Decision:** Defer — M5, with the first driver that delivers device timestamps in local time
  (DNP3, IEC 104 or a flow-computer driver). Internally all timestamps stay UTC (ADR-0006).
- **Status:** Not started
- **Requirements:**
  - `params.device_timezone` (IANA name or fixed offset) used only to convert device-supplied
    timestamps into `source_ts`; absent means the device reports UTC.
- **Acceptance:** conversion vectors around DST changes, shared by both engines.

### DEV-14 Device clock synchronisation (manual p. 93)
- **What:** The server sets the device clock: at a time of day, at an interval or after a poll, when
  the drift exceeds a threshold.
- **Decision:** Out of scope — setting a PLC clock is a write to the device (ADR-0008); any such
  feature goes through RFC 0002 write safety (#19).
- **Status:** Out of scope

### DEV-15 Timing: request timeout and attempts (manual p. 93–94)
- **What:** How long to wait for a reply (50–9999 ms) and how many attempts are made before the request
  counts as failed (1–10, counting the first one; also used for connect attempts).
- **Decision:** Adapt — `timeout_ms` and `retries` (= attempts − 1, D-010); every driver call has a
  safety net of `timeout_ms × 1.25 + 0.1 s` (D-015); connect failures back off 1 s → 60 s instead of
  being retried inside the request budget.
- **Status:** Done
- **Requirements:**
  - A block is `bad_comm` after `retries + 1` failed attempts; a socket closed by a timeout is reopened
    once inside the same budget without backoff.
  - The importer maps the KEPServerEX request timeout and attempts.
- **Acceptance:** `tests/unit/test_channel_worker.py` with the Modbus simulator's mute switch; Phase 1
  acceptance on both engines.

### DEV-16 Connect timeout (manual p. 94, 124)
- **What:** A separate limit for opening a socket connection (1–30 s, typically 3 s), longer than a
  normal request; not applicable to UDP.
- **Decision:** Adopt — today a connect is bounded by the driver's own connect timeout (Logix tag-list
  upload) or the safety net derived from `timeout_ms`.
- **Status:** Not started
- **Requirements:**
  - Optional `Device.connect_timeout_ms` (1000–30000); absent keeps today's behaviour; the importer
    maps the KEPServerEX connect timeout; ignored by UDP drivers.
  - `GET /api/v1/devices/{id}` shows the effective value.
- **Acceptance:** simulator test with a listener that accepts late (or a black-holed address via
  `tests/helpers/netcut.py`): the connect fails after `connect_timeout_ms`; both engines.

### DEV-17 Inter-request delay (manual p. 94)
- **What:** A pause before each new request to the device (0–300,000 ms), for slow devices or busy
  networks; it slows every device on the same channel.
- **Decision:** Adopt — applied inside the channel's wire lock, so the D-015 ordering stays intact;
  FR-3 lists it as the one timing item not yet done.
- **Status:** Not started
- **Requirements:**
  - `Device.inter_request_delay_ms: int = 0` (0–300000); the worker waits that long after the
    device's previous request ended before sending the next one to it, holding the channel.
  - `opencap check` includes the delay in the cycle estimate; importer maps the property.
- **Acceptance:** worker unit test with a fake clock; simulator timestamps; both engines.

### DEV-18 Media-level redundancy (manual p. 95)
- **What:** A plug-in that switches a device to an alternate communication path.
- **Decision:** Adapt — alternate host list per device in the core (FR-12, RFC 0001 §3.4, ADR-0012
  last bullet); independent of the server pair redundancy (RFC 0003, #20).
- **Status:** Not started
- **Requirements:**
  - `Device.hosts: [{host, port}]` (optional, after `host`/`port`); the worker moves to the next path
    after a failed connect or a demotion and reports the active path in `GET …/status` and a
    `<Channel>.<Device>._System._ActivePath` tag; metric `device_path_switches_total{device}`.
  - One path at a time; never two connections to one device.
- **Acceptance:** simulator test with two endpoints, one cut with `tests/helpers/netcut.py`; both engines.

## Open questions
- Import devices with data collection off as `enabled: false` (visible, editable) instead of
  excluding them (DEV-03)?
- When KEPServerEX demotion is off, should opencap keep demotion on with defaults (today) or turn it
  off (DEV-10)? Demotion protects the other devices on a channel (D-015 known limit).
- Should the config validator enforce KEPServerEX ranges (scan, timeout, demotion) or only warn?
- For KEPServerEX "respect client rate" devices, keep using tag rates floored by `min_scan_ms` until
  DEV-07 lands?
- Which sites need media-level redundancy (DEV-18), and is "next path after demotion" the right
  switch rule?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| DEV-01 | Device object: name, channel, driver, model, id | Adapt | Done | #63, #64 | M2 |
| DEV-02 | Device description | Adopt | Planned (#103) | #103 | — |
| DEV-03 | Data collection on/off | Adapt | Done | #63, #64 | M2 |
| DEV-04 | Simulated mode | Adapt | Planned (#12) | #12 | M2 |
| DEV-05 | Static tag count per device | Adopt | Done | #63 | M1 |
| DEV-06 | Scan rates: tag rates, floor rate, forced rate | Adapt | Done | #64 | M2 |
| DEV-07 | Client-driven scan rate | Defer | Planned (#111) | #111 | M3 |
| DEV-08 | Demand poll only and `_DemandPoll` | Adapt | Planned (#111) | #111 | — |
| DEV-09 | Initial updates from cache | Adapt | Done | — (OPC UA server: #74, #50) | M1 |
| DEV-10 | Auto-demotion | Adapt | Done | #64 | M2 |
| DEV-11 | Communication parameters / Ethernet encapsulation per device | Adapt | Done | #62 | M1 |
| DEV-12 | Automatic tag database generation | Adapt | Planned (#11) | #11 | M1 |
| DEV-13 | Device time zone for device timestamps | Defer | Planned (#112) | #112 | M5 |
| DEV-14 | Device clock synchronisation | Out of scope | Out of scope | #19 | — |
| DEV-15 | Timing: request timeout and attempts | Adapt | Done | #64 | M2 |
| DEV-16 | Connect timeout | Adopt | Planned (#105) | #105 | — |
| DEV-17 | Inter-request delay | Adopt | Planned (#105) | #105 | — |
| DEV-18 | Media-level redundancy | Adapt | Planned (#113) | #113 | M5 |
