# 12 Diagnostics

> Source: KEPServerEX V6 server manual (© PTC), pages 47 and 233–249, plus the event-log concept as those pages use it (the event-log settings pages are in plan 09), summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
KEPServerEX has two built-in diagnostic views, which the manual says to use only while
troubleshooting because they cost performance (manual p. 233). The **OPC Diagnostics Viewer**
records every call a client makes to the server and every callback the server makes to a client.
It has a live mode fed from the event log, which can be paused, and a file mode for saved
captures. It can find text, filter by method (the filter affects only the view), and export or
import filter sets (manual p. 233–236). Capture is switched on per client interface in project
properties: OPC DA, OPC UA and OPC HDA (manual p. 47). The manual then lists the OPC DA COM
interfaces and methods that can appear (manual p. 236–244) and the OPC UA service sets (manual
p. 244–247). **Communication Diagnostics** is enabled per channel. Its viewer shows the TX/RX frames
of a channel or device in real time, with counters (TX, RX, good and failed reads and writes, total
events) that can be reset. It can show frames in hex or ASCII, find text, copy, save as text,
autoscroll and clear. It stays open while channel settings are changed, so their effect shows
immediately (manual p. 247–249). The event log is where both views persist their data. Individual
event messages are catalogued in plan 15.

## opencap today
- **Health and metrics** (Python, and Rust #64/#84): `GET /health` (version, instance, MQTT,
  outbox, device counts by state), Prometheus `/metrics` (`poll_requests_total{device,result}`,
  `poll_cycle_seconds`, `poll_overrun_total`, `device_state`, `mqtt_published_total`,
  `saf_queue_messages`, `saf_dropped_total`), `GET /devices/{id}` (block plan, `bad_config` tags,
  state, cycle time, last error). `opencap check` prints block plans and cycle estimates.
  `opencap identity` reads the device identity.
- **Web UI** (#63): dashboard (state, last good, errors, cycle time, MQTT link, outbox), device page
  (live values with quality, identity, block plan), audit log page with filters
  (`GET /api/v1/audit?action=&who=&target=`).
- **Logs:** structured JSON lines on stdout (`src/opencap/logsetup.py`; Rust uses `tracing` with the
  same event names, RFC 0001 §2.9), rotated by journald or Docker (D-030). There are stable event
  names (`device.offline`, `device.demoted`, `device.resumed`, `config_reloaded`, `reload_failed`,
  `mqtt_disconnected`, `saf_dropped_oldest`, …), a `device.stats` summary every 5 min (D-023), and
  a `device.request` line per request at `DEBUG` (block, result, attempt; no frame bytes).
- **Not yet:** an event-log store, API or UI view (planned in 02 §2.4 and 04 §4.3 as a bounded ring
  buffer plus JSONL file; board backlog "Diagnostics"), frame capture (FR-10, board backlog), and
  protocol traces for north servers. The OPC UA server is in flight (#61 → #74, hardening #28). OPC
  DA comes only through a Windows bridge (RFC 0004, #18). OPC HDA is out of scope (01 "Out of
  scope"). System and diagnostic tags are plan 07 (#22, Rust #56).

## Features

### DIAG-01 Server- and driver-level diagnostics at a glance (manual p. 233)
- **What:** Views that show what the server and each driver are doing, for troubleshooting.
- **Decision:** Adapt: always-on, cheap diagnostics through health, metrics, the status API and
  the UI. Captures that cost something are opt-in (DIAG-05, DIAG-11).
- **Status:** Done
- **Requirements:**
  - `/health`, `/metrics`, `GET /api/v1/status`, `GET /api/v1/devices/{id}/status|plan|identity`
    and `opencap check` give each device's state, cycle time, errors, last error and block plan
    (NFR-13).
  - `/health` and `/metrics` stay open, and the rest needs a token (D-040).
- **Acceptance:** `tests/unit/test_api.py`, `tests/unit/test_config_api.py` and
  `tests/integration/test_rust_engine_api.py` (same routes on both engines). UI smoke test
  (`tests/ui/browser_smoke.js`).

### DIAG-02 Structured event stream (manual p. 233, 236)
- **What:** Server components report what happens to one event log, which both diagnostic views
  read from.
- **Decision:** Adapt: one structured event stream (JSON lines) with stable event names, a level
  and fields. This is the source for the event-log store (DIAG-03) and for log shippers. Plan 15
  owns the catalogue and its naming rules.
- **Status:** Done
- **Requirements:**
  - Every event has `event` (Python) / `message` (Rust), `level` and an ISO UTC `timestamp`.
    Device events carry the device id.
  - State changes log at INFO, and per-request detail at DEBUG only.
  - No secret appears in any event (NFR-11).
- **Acceptance:** Existing unit tests capture device and reload events
  (`tests/unit/test_channel_worker.py`). `test_no_private_data.py`.

### DIAG-03 Event-log store with retention (manual p. 233, 236)
- **What:** The event log keeps events (and diagnostic captures) so they can be viewed later. It
  can persist to disk, and the manual warns that its extended persistence mode can use a lot of
  disk.
- **Decision:** Adapt: an in-process bounded ring buffer (fast "last N events") plus an
  append-only JSONL file with size-based rotation (02 §2.4, 04 §4.3). The audit log stays
  separate (D-043). Persistence-mode settings are in plan 09.
- **Status:** Not started
- **Requirements:**
  - `event_log.memory_events` (default 10,000) keeps the newest events in memory.
    `event_log.path` (default `data/events.jsonl`) holds events at `event_log.min_level` (default
    INFO) and above, rotated at `event_log.max_bytes` (default 16 MiB) with `event_log.keep_files`
    (default 4) old files.
  - Writing an event never blocks polling. If the file cannot be written, the event stays in
    memory and the failure is counted (`event_log_write_errors_total`).
  - Events in the file survive a restart, and the ring buffer is refilled from the file tail at
    start.
- **Acceptance:** Unit tests for rotation, the memory bound and a write failure. A conformance
  fixture of the JSONL line format, read by both engines.

### DIAG-04 Event-log API and viewer (manual p. 233–236)
- **What:** View events live or historically, pause the view, search and filter.
- **Decision:** Adapt: an API route and an **Events** page in the web UI (and so in the desktop
  app, ADR-0010).
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/events?level=&source=&device=&q=&since=&until=&limit=` (viewer role) reads memory
    and files, newest first. `GET /api/v1/events?follow=true` (or the WebSocket of #31) streams new
    events.
  - The Events page has filters for level, source (channel, device, north agent, API), time range
    and free text. It also has live/paused toggling, autoscroll that stops when a row is selected,
    and export of the filtered rows as JSONL.
  - Deleting events is not offered from the UI. Retention is by rotation only, and the audit log
    is never deletable.
- **Acceptance:** API tests (filters, roles) on both engines. A UI smoke test for the Events page at
  desktop and phone width.

### DIAG-05 North protocol trace capture (manual p. 47, 233–236)
- **What:** Record each client request to the server, and each server callback, with its
  parameters and result. Capture is switched on per client interface.
- **Decision:** Adapt: an opt-in, time-limited trace per north server (OPC UA first, then the
  Modbus server and others), stored as events with source `north.<server>.trace`. Capture slows
  the server, so it is never on by default.
- **Status:** Not started
- **Requirements:**
  - Capture is off by default. It is started and stopped by an admin API action
    (`POST /api/v1/diagnostics/north/{server}/trace` with a duration, default 10 min, max 60 min;
    audited), and it stops by itself when the duration ends.
  - Each entry records time, session or client id, service or method, the nodes or items involved
    (truncated to a configured count), status code and duration. Values may be included only when
    `include_values: true`, and user tokens and passwords never are.
  - Capture is rate-limited (`max_entries_per_s`, default 500). Entries over the limit are counted
    and reported as one "N entries dropped" entry.
  - The overhead is measured in the benchmark (#7) and documented.
- **Acceptance:** An L1 test with an OSS OPC UA client (#29) runs a session with trace on and
  checks the entries for CreateSession, Browse, Read and CreateSubscription. A rate-limit unit test.

### DIAG-06 Trace viewer (manual p. 233–236)
- **What:** Live view with pause, saved files reopened later, find (case match, include details),
  and a view-only filter by method whose filter set can be exported and imported.
- **Decision:** Adapt: a **Trace** tab on the Events page (DIAG-04), working on the same API.
- **Status:** Not started
- **Requirements:**
  - Live and paused modes. Capture continues while the view is paused.
  - File mode: export the shown entries as JSONL and open such a file in the viewer. The UI parses
    it in the browser and uploads nothing.
  - Find with match case and "search details", plus next and previous.
  - The filter by service set and service is applied to the view only. Saved filter sets are kept
    per browser (local storage) and can be exported and imported as JSON.
  - An export says whether a filter was applied.
- **Acceptance:** A UI smoke test drives live, pause, filter, export and reopen with a recorded
  fixture.

### DIAG-07 OPC UA service trace content (manual p. 244–247)
- **What:** The manual lists the OPC UA service sets that appear in the trace: Attribute (Read,
  Write), Discovery, MonitoredItem, Secure Channel, Session, Subscription, View, and others
  (ServiceFault, unsupported services).
- **Decision:** Adapt: trace every service set the opencap OPC UA server implements. Write and
  method calls are answered as refused (read-only, ADR-0008) and traced with their status code, so
  an operator can see a SCADA trying to write.
- **Status:** Not started
- **Requirements:**
  - The trace covers GetEndpoints and FindServers; Open/CloseSecureChannel; Create, Activate and
    CloseSession, and Cancel; Browse, BrowseNext, TranslateBrowsePathsToNodeIds and
    Register/UnregisterNodes; Read and Write; Create, Modify, Delete, SetMonitoringMode and
    SetTriggering for monitored items; and Create, Modify, Delete, Publish, Republish,
    SetPublishingMode and TransferSubscriptions for subscriptions.
  - A ServiceFault is traced with its status code. Unsupported services are traced as such.
  - A Write is traced with `Bad_NotWritable` (or the server's chosen refusal code) and counted
    (DIAG-13).
- **Acceptance:** An L1 suite with Milo, open62541 and asyncua (#29) checks one trace entry per
  service it calls. Both engines once the Rust OPC UA server exists (#50).

### DIAG-08 OPC DA method trace (manual p. 236–244)
- **What:** The manual lists the OPC DA 1.0/2.0/3.0 COM interfaces and methods that appear in the
  trace (IOPCServer, IOPCItemMgt, IOPCSyncIO, IOPCAsyncIO2/3, IOPCDataCallback, browse
  interfaces, …).
- **Decision:** Defer: OPC DA exists only through the Windows bridge (ADR-0009, RFC 0004 #18). The
  bridge will trace its own COM methods into the engine's event log through the API. Write methods
  (Write, WriteVQT, async writes) are refused and traced.
- **Status:** Not started
- **Requirements:** These are set in RFC 0004: method-level trace entries in the format of
  DIAG-05, sent to the engine, and refused writes counted.
- **Acceptance:** An OPC DA test client on Windows CI (RFC 0001 §4.1) produces the expected entries.

### DIAG-09 OPC HDA diagnostics (manual p. 47)
- **What:** Capture OPC HDA calls to the event log.
- **Decision:** Out of scope: OPC HDA is out of scope (01 "Out of scope"; to revisit with the local
  historian, FR-6).
- **Status:** Out of scope

### DIAG-10 Communication statistics per channel and device (manual p. 247–248)
- **What:** Counts of transmitted and received frames, good and failed reads (and writes), with a
  reset.
- **Decision:** Adapt: per-device request counters by result, cycle-time histograms and error
  counts, through metrics and the API. Write counters do not exist (ADR-0008). A UI "reset" is a
  view baseline, because Prometheus counters never go back.
- **Status:** Done
- **Requirements:**
  - `poll_requests_total{device,result}` with `ok`, `timeout`, `conn_error`, `bad_config` and
    `error`. `poll_cycle_seconds{device}`, `poll_overrun_total{device}` and `device_state{device}`.
  - Each device's `errors` (failed cycles since start) is in the API and on its MQTT status
    topic, and `last_error` is in `GET /devices/{id}`.
- **Acceptance:** `tests/unit/test_channel_worker.py`, `tests/unit/test_health.py`, and the metrics
  names compared by the Rust API test (RFC 0001 §2.9).

### DIAG-11 Frame capture on demand (manual p. 247–249)
- **What:** Capture the actual request and response frames of a channel or device in real time,
  while capture is enabled.
- **Decision:** Adapt: an opt-in, rate-limited, time-limited capture per channel or device (FR-10),
  kept in a bounded buffer in memory. It is written to disk only when exported. Drivers report
  frames through one hook in the channel worker, so every driver gets it.
- **Status:** Not started
- **Requirements:**
  - `POST /api/v1/diagnostics/channels/{channel}/capture` (admin, audited) takes an optional device
    filter, a duration (default 5 min, max 30 min) and a buffer size (default 2,000 frames, max
    20,000). A `DELETE` stops it. Capture stops by itself and never survives a restart.
  - Each frame carries time, device, direction (TX/RX), bytes (hex), length, request id or
    sequence, and the result of the request it belongs to. Credentials in frames (e.g. an OPC UA
    client's user token) are masked by the driver.
  - Capture never changes timing on the wire. The overhead is measured in the benchmark (#7).
  - `GET /api/v1/diagnostics/channels/{channel}/capture` returns the buffer, and `?follow=true`
    streams it.
  - Status shows "capturing" with the time left, or "capture off", for each channel.
- **Acceptance:** Simulator tests (Modbus, CIP, PCCC, DF1) in which the captured TX/RX bytes equal
  what the simulator saw. Rate and size limit tests. Both engines (#56).

### DIAG-12 Communication diagnostics viewer (manual p. 247–249)
- **What:** A window per channel or device showing frames as hex or ASCII, with counters and
  reset, find, copy, save as text, autoscroll and clear. It stays open while channel settings are
  edited, so changes show at once.
- **Decision:** Adapt: a **Diagnostics** panel on the device page and the channel page of the web
  UI. The desktop app may pop it out into its own window. "Always on top" is a desktop-only option,
  deferred with #27.
- **Status:** Not started
- **Requirements:**
  - Start and stop capture (admin), a live frame list with hex/ASCII toggle, find with next, copy
    as text, download as text, autoscroll that stops when a row is selected, and clear view.
  - The counters of DIAG-10 are shown with a "reset view" baseline kept in the browser.
  - The panel stays live across a `POST /api/v1/reload` of that channel, and the capture continues
    on the restarted worker.
- **Acceptance:** A UI smoke test with a simulator device: start capture, see frames, toggle hex
  and ASCII, find, download, reload the channel and keep seeing frames.

### DIAG-13 Refused-write diagnostics (manual p. 240–247, 248)
- **What:** In KEPServerEX, write calls show up in the traces and in the good and failed write
  counters.
- **Decision:** Adapt: opencap never writes (ADR-0008). Instead, every write attempt a north server
  refuses is counted and logged, so operators can find SCADA screens that still try to write.
- **Status:** Not started
- **Requirements:**
  - `north_write_refused_total{server,client}`, plus the event `north.write_refused` (server,
    client, node or item, at most once a minute per client and node).
  - The OPC UA server answers writes with a Bad status, and the Modbus server answers write
    function codes with exception 01 (ADR-0008, RFC 0001 §4.1). Both are counted.
- **Acceptance:** An L1 OPC UA test (#29) and a pymodbus client test for the Modbus server (#52)
  attempt writes and assert the refusal, counter and event.

## Open questions
- **Milestone:** RFC 0001 §7 puts diagnostics in M5 (#56). `plan/architecture.md` §6 lists plan 12
  under M3. Which one wins for the event log (DIAG-03/04)? The UI needs it early for commissioning.
- **Event log format:** should the event log reuse the audit log's rotation code and format (D-043)
  or have its own? Should log shippers read `events.jsonl` or stdout?
- **Values in north traces (DIAG-05):** off by default, as proposed, or allowed for viewer-role
  tokens?
- **Frame capture on serial lines (DIAG-11):** may capture also record timing gaps (inter-frame
  silence) for RTU and DF1 troubleshooting?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| DIAG-01 | Server- and driver-level diagnostics at a glance | Adapt | Done | #63, #64, #84 | M1 |
| DIAG-02 | Structured event stream | Adapt | Done | D-023, D-030 | M1 |
| DIAG-03 | Event-log store with retention | Adapt | Not started | — | M5 |
| DIAG-04 | Event-log API and viewer | Adapt | Not started | — | M5 |
| DIAG-05 | North protocol trace capture | Adapt | Not started | — | M5 |
| DIAG-06 | Trace viewer | Adapt | Not started | — | M5 |
| DIAG-07 | OPC UA service trace content | Adapt | Not started | #74, #29 | M5 |
| DIAG-08 | OPC DA method trace | Defer | Not started | #18 | M5 |
| DIAG-09 | OPC HDA diagnostics | Out of scope | Out of scope | — | — |
| DIAG-10 | Communication statistics per channel and device | Adapt | Done | — | M1 |
| DIAG-11 | Frame capture on demand | Adapt | Not started | #56 | M5 |
| DIAG-12 | Communication diagnostics viewer | Adapt | Not started | — | M5 |
| DIAG-13 | Refused-write diagnostics | Adapt | Not started | #29, #52 | M3 |
