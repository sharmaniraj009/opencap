# 11 Store and forward

> Source: KEPServerEX V6 server manual (© PTC), pages 49–55 and 232, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
In this manual, store and forward belongs to the ThingWorx native interface (manual p. 49–55); the
IoT Gateway has its own version in its plug-in manual. When it is switched on, every property update
goes to a disk datastore first and stays there until the platform confirms it. A configurable
directory and maximum size (128 MB to 16 GB) limit the store, and the server also stops storing
when the disk gets within 500 MB of full. Once either limit is hit, new updates are dropped. After a
reconnect, the *Active* forward mode replays the backlog oldest first. The *On Hold* mode sends only
fresh updates and keeps the backlog until someone switches back to Active. The manual lists rules for
the storage path, notes that stored data survives restarts but not upgrades, and works a
fill-rate example: bytes per stored update by data type, and offline time against scan rate. A set
of `_ThingWorx` system tags shows datastore state, and three of them are writable controls. A
separate Windows service provides the datastore to the components that need it (manual p. 232).

## opencap today
- **Outbox-always** store-and-forward for the MQTT agent, in both engines (D-016, ADR-0011):
  every data and status message is written to a SQLite outbox (`saf.path`, default
  `data/saf.sqlite`; WAL, `synchronous=NORMAL`) before it is sent. The outbox drains in id order
  in windows of 32, and a row is deleted only after the broker's PUBACK. Python:
  `src/opencap/north/saf.py`. Rust: `engine/crates/opencap-north` (#64), same schema. A file
  written by one engine drains in the other (`tests/integration/test_rust_engine_outbox.py`).
- Size cap `saf.max_bytes` (default 2 GiB, counting topic + payload bytes). Over the cap, the
  **oldest** rows are dropped and counted in `saf_dropped_total`. Lowering the cap trims at the
  next open.
- Visibility: `saf_queue_messages` and `saf_dropped_total` in `/metrics`, `saf_queue` in
  `/health`, the outbox size on the web UI dashboard (#63). Log events `saf_reopened`,
  `saf_dropped_oldest` (at most once a minute) and `saf_not_wal`.
- `saf.*` is a restart-only setting: `GET /api/v1/project/pending` reports it (D-039).
- Planned: one outbox per durable agent (`data/outbox/<agent>.sqlite`) arrives with Sparkplug
  (#51). The "outage covered" formula is in `docs/architecture/04-scale-reliability.md` §4.1,
  and the per-agent backlog metric names are in §4.5. The ThingWorx native interface is out of
  scope (FR-8, ADR-0009).

## Features

### SAF-01 Keep every update until the peer confirms it (manual p. 49–51)
- **What:** Write each outgoing update to disk before sending, and delete it only once the peer
  confirms it. KEPServerEX makes this optional and off by default. It also buffers updates in
  memory before they reach disk.
- **Decision:** Adapt: the outbox is always on for durable agents and has no enable switch. A
  toggle that loses data is not offered (ADR-0011, D-016, NFR-8). Every message goes to disk
  first, with no memory staging buffer.
- **Status:** Done
- **Requirements:**
  - Every data and status message is in the outbox before the link sees it. A row is deleted only
    after its PUBACK.
  - The drain follows id order in windows of 32, and new messages queue behind the backlog.
  - A message committed to the outbox survives `kill -9`. After a power loss, at most the last
    ~1 s of messages may be lost (WAL, `synchronous=NORMAL`).
- **Acceptance:** `tests/unit/test_saf.py`. `tests/integration/test_phase1_acceptance.py`
  (network cut with no loss and order kept, 10 minutes with `OPENCAP_OUTAGE_S=600`) passes with
  `OPENCAP_ENGINE=python` and `rust`. Mosquitto restart test #8.

### SAF-02 Outbox location setting (manual p. 50)
- **What:** Choose the directory where the datastore lives. When the setting changes, the old
  store stays on disk, and changing the setting back resumes forwarding from it.
- **Decision:** Adapt: the setting is `saf.path` in `poller.yaml` (later `node.yaml`, ADR-0013),
  and it names a file, not a directory. An old file is never deleted. Pointing `saf.path` back at
  it resumes the drain, because rows carry no project identity.
- **Status:** Done
- **Requirements:**
  - A relative `saf.path` resolves from the working directory, in both engines.
  - Changing `saf.path` never deletes or truncates the previous file.
- **Acceptance:** `tests/unit/test_saf.py::test_reopen_continues` (reopening the file resumes
  the drain). Add a test that switching paths and back leaves the first file's rows intact and
  in order.

### SAF-03 Validate the outbox location (manual p. 51)
- **What:** The manual checks the store path when it is configured and again at runtime. The
  path must be of bounded length, absolute, and use only legal characters. A network share or
  removable media is refused.
- **Decision:** Adapt to Linux and Windows. Warn about locations that defeat durability. Refuse
  only those that cannot work.
- **Status:** Not started
- **Requirements:**
  - `opencap check` and the engine's startup report an **error** when the outbox's directory
    cannot be created, or is not writable by the service user.
  - They report a **warning** when the outbox is on a network filesystem (Linux: `nfs`, `cifs`,
    `smb3`, `sshfs`, `fuse.*` from `/proc/self/mounts`; Windows: UNC path or mapped network
    drive), on `tmpfs`/`ramfs` (lost at reboot), or on removable media where the OS reports it.
  - When `saf.path` changes and the running outbox still holds messages,
    `GET /api/v1/project/pending` names the old path and its message count.
- **Acceptance:** Unit tests with fake mount tables and a read-only temp directory. A
  conformance fixture of the warning text, run by both engines.

### SAF-04 Outbox cannot be opened (manual p. 50)
- **What:** If its datastore cannot be initialised, KEPServerEX queues updates in memory, keeps
  retrying, and reports the failure in the event log.
- **Decision:** Adapt: polling and the API never stop because the outbox is unavailable. The
  agent runs in a visible degraded state instead of exiting at start, which is what it does
  today.
- **Status:** Not started
- **Requirements:**
  - If the outbox file cannot be opened at start or later, the engine still polls and serves the
    API. The agent keeps a bounded in-memory queue (`saf.memory_max_messages`, default 10,000,
    oldest dropped first and counted) and retries the open every 30 s.
  - Each failure logs `saf.open_failed` (ERROR: `path`, `reason`) at most once a minute. A
    corrupt file is moved aside, never deleted (`saf.corrupt`, plan 15 LOG-36). `/health` shows
    `saf_ok: false` with `saf_problem: "unavailable"`, and a `saf_available` gauge reads 0.
  - When the open succeeds, the in-memory messages go into the outbox in order before new ones,
    and `saf_ok` returns to true.
- **Acceptance:** Integration test with an unwritable directory that later becomes writable: no
  message lost below the memory cap, order kept, events and gauge as specified. Both engines.

### SAF-05 Size cap and what gets dropped (manual p. 49–51)
- **What:** The store has a maximum size. Once it is full, KEPServerEX drops **new** updates
  until space frees up.
- **Decision:** Adapt: `saf.max_bytes` with **oldest-first** drop. After a long outage, recent
  data is worth more than the oldest backlog (D-016). Each drop is counted.
- **Status:** Done
- **Requirements:**
  - The cap counts topic + payload bytes, not file size. A message larger than the cap is dropped
    at once and counted.
  - Lowering `max_bytes` trims the oldest rows at the next open.
  - `saf_dropped_total` counts every dropped message.
- **Acceptance:** `tests/unit/test_saf.py` cap tests. The Rust outbox unit tests give the same
  results.

### SAF-06 Free-disk reserve (manual p. 49, 51, 54)
- **What:** KEPServerEX stops storing when less than 500 MB of disk is left, whatever the
  configured size.
- **Decision:** Adapt: add a reserve, so a backlog on a small edge box cannot fill the disk the OS
  and logs need.
- **Status:** Not started
- **Requirements:**
  - Add `saf.min_free_bytes` (default 500 MiB). The engine checks the outbox filesystem's free
    space at most once per second.
  - Below the reserve, the outbox is treated as full: the oldest rows are dropped to make room.
    If the outbox is empty, the new message is dropped. These drops are counted in
    `saf_dropped_disk_total`.
  - While the disk is low, the engine logs `saf.disk_low` at most once a minute, and `/health`
    shows `saf_ok: false` with `saf_problem: "disk_low"`. A write that fails anyway is
    `saf.write_failed` (LOG-36).
- **Acceptance:** Unit test with an injected free-space function. Dual-engine unit tests share
  the vectors (free space, sizes → kept/dropped ids).

### SAF-07 Ordered forwarding after a reconnect (manual p. 50, Active mode)
- **What:** After a reconnect, stored updates go out in the order they arrived until the backlog
  is empty. Then data flows in real time again.
- **Decision:** Adopt. This is opencap's only mode today (D-016).
- **Status:** Done
- **Requirements:** The backlog drains strictly in id order, and messages that arrive during the
  drain queue behind it.
- **Acceptance:** The order assertion in the Phase 1 outage test, on both engines.

### SAF-08 Live-first forwarding (manual p. 50–51, On Hold mode)
- **What:** After a reconnect, only fresh updates are sent. The backlog is held on disk until an
  operator switches back to ordered forwarding.
- **Decision:** Defer to M5. With today's sizing, a 45-minute backlog drains in about 2 minutes
  (04 §4.1). Live-first changes the ordering consumers see, so it needs its own design and
  consumer guidance.
- **Status:** Not started
- **Requirements:**
  - Per-agent `forward_mode: ordered | live_first` (default `ordered`). In `live_first`, messages
    newer than the reconnect are sent first, and the backlog is drained only after
    `forward_mode` returns to `ordered`.
  - Changing the mode is an audited admin API action on the agent, not a writable tag (see
    SAF-16). It applies without restarting the link.
  - The current mode shows in `/health`, in the metrics (`saf_forward_mode{agent}`) and in a
    system tag (SAF-15).
- **Acceptance:** Integration test with a network cut: in `live_first`, fresh messages arrive
  first; after the switch, the backlog arrives complete and in order. Both engines.

### SAF-09 Survive restarts (manual p. 51)
- **What:** Stored updates survive a server restart.
- **Decision:** Adopt.
- **Status:** Done
- **Requirements:** On start, an outbox that still holds rows is reopened (`saf_reopened` with
  the message and byte counts), and draining continues where it left off.
- **Acceptance:** `tests/unit/test_saf.py` (`test_reopen_continues`,
  `test_survives_process_kill`). `test_rust_engine_outbox.py` covers a file left by the other
  engine.

### SAF-10 Survive upgrades and the engine cut-over (manual p. 51)
- **What:** KEPServerEX cannot carry stored updates across a major or minor upgrade, so the
  backlog must be empty before upgrading.
- **Decision:** Adapt: the outbox format is part of the contract between versions and between
  the two engines. An upgrade, or the Python → Rust cut-over (M4), keeps the backlog.
- **Status:** Done
- **Requirements:**
  - The outbox schema (`outbox(id, topic, payload, qos, retain, size, created)`) is the same in
    both engines.
  - From now on, any schema change bumps `PRAGMA user_version`, migrates on open, and adds an
    old-format fixture to the interchange test.
  - Installers upgrade in place without touching the outbox (ROADMAP, #58).
- **Acceptance:** `tests/integration/test_rust_engine_outbox.py` (Python-written outbox drained
  by Rust, and the reverse).

### SAF-11 Changing store-and-forward settings (manual p. 51)
- **What:** In KEPServerEX, the settings live in project properties or the Configuration API.
  Changes apply without reinitialising the platform connection, and collection continues.
- **Decision:** Adapt: `saf.*` is a node setting (`poller.yaml`, later `node.yaml`, #37). A change
  takes effect at restart, and no data is lost across that restart. Editing node settings from
  the UI belongs to plans 09 and 10.
- **Status:** Done
- **Requirements:**
  - `GET /api/v1/project/pending` lists a changed `saf` section under `restart_needed`
    (`api/context.py` `RESTART_FIELDS`).
  - Polling continues during `POST /reload`, which does not touch the outbox.
- **Acceptance:** `tests/integration/test_runtime_reload.py`. Add a unit test that changes
  `saf.max_bytes` and expects `saf` in `restart_needed`: no test pins this yet.

### SAF-12 Sizing estimate: how long an outage is covered (manual p. 52–53)
- **What:** The manual's fill-rate example works out bytes per stored update by data type, adds
  them up over the project's tags and scan rates, and turns the result into offline minutes
  before data is lost.
- **Decision:** Adapt: opencap stores whole device messages, not single property updates. The
  estimate is therefore messages per second × bytes per message, against `saf.max_bytes` (04
  §4.1). `opencap check` computes it, so nobody has to do the arithmetic by hand.
- **Status:** Not started
- **Requirements:**
  - `opencap check` (and `GET /api/v1/project/validate`) prints, for each durable agent, the
    estimated messages/s from publish modes, intervals and heartbeats; the mean message bytes
    from a payload built with placeholder values for each device's tags; the resulting bytes/s;
    and "outage covered" as a duration for the configured `max_bytes`.
  - For on-change publishing, the estimate states its assumption (heartbeat rate as the floor,
    interval rate as the ceiling).
- **Acceptance:** Conformance vectors (config → estimate) in `tests/conformance/check/`, checked
  by both engines' `check` output parity test.

### SAF-13 Backlog count and drops (manual p. 51, 53–55)
- **What:** Monitor the number of stored updates and the number of dropped updates.
- **Decision:** Adapt: metrics and health fields replace the tag-only view.
- **Status:** Done
- **Requirements:** `saf_queue_messages` (gauge) and `saf_dropped_total` (counter) are in
  `/metrics`, `saf_queue` is in `/health`, and the outbox size is on the dashboard. All three
  come from the same counters in both engines.
- **Acceptance:** `tests/unit/test_config_api.py` (the status route reports `saf_queue`) and
  `test_saf.py` (drop counter). The acceptance adapter reads `saf_queue` from `/health` on both
  engines (RFC 0001 §6).

### SAF-14 Backlog bytes, age and capacity (manual p. 53–55)
- **What:** The manual's tags report the current store size, the space left against the
  configured maximum, the usable disk space after the reserve, and full flags.
- **Decision:** Adapt: per-agent metrics and API fields with the names planned in 04 §4.5, plus
  "outage covered" from live rates (ADR-0011).
- **Status:** Not started
- **Requirements:**
  - Metrics: `north_backlog_messages{agent}`, `north_backlog_bytes{agent}`,
    `north_backlog_age_seconds{agent}` (age of the oldest row),
    `north_backlog_capacity_bytes{agent}` (= `max_bytes` − bytes),
    `saf_disk_free_bytes{agent}` (after the reserve of SAF-06), and `saf_full{agent}` (0/1).
  - `GET /api/v1/status` and the dashboard show the backlog, its oldest age and the "outage
    covered" figure computed from the last 5 minutes' message rate.
  - The existing `saf_queue_messages` and `saf_dropped_total` stay, for existing dashboards.
- **Acceptance:** Unit tests on the outbox counters. API tests on both engines. The UI smoke test
  (`tests/ui/browser_smoke.js`) asserts that the dashboard shows the fields.

### SAF-15 Store-and-forward status as system tags (manual p. 53–55)
- **What:** A `_ThingWorx` folder holds read-only tags such as `_StoredUpdateCount`,
  `_DatastoreFull`, `_DatastoreDiskFull`, `_DatastoreAttachError`, `_DroppedUpdates`,
  `_DatastoreCurrentSizeMB`, `_DatastoreRemainingSpaceMB`, `_DatastoreUseableDiskSpaceMB` and
  `_ForwardModeStatus`, so clients can alarm on them.
- **Decision:** Adapt: the same facts as read-only system tags per agent, under the `_System`
  tree defined by the system-tags spec (#22; Rust #56). They carry the numbers of SAF-13/14 and
  the state of SAF-04/06/08. The dropped counter is 64-bit and never rolls over. Plan 07 owns
  the system-tag naming.
- **Status:** Planned (#22)
- **Requirements:**
  - One tag folder per durable agent with: queued messages, queued bytes, oldest age, capacity
    left, disk free, full, disk low, unavailable, dropped total and forward mode.
  - The values equal the metrics at the same instant (one source).
  - The tags are browsable through every north server, read-only (ADR-0008).
- **Acceptance:** Spec #22 vectors. An OPC UA L1 test reads the tags (#29). Both engines.

### SAF-16 Store-and-forward controls (manual p. 53–55)
- **What:** Three writable tags control the store: `_StoreAndForwardEnabled` switches it on or
  off, `_DeleteStoredData` purges it, and `_ForwardMode` sets the forward mode.
- **Decision:** Adapt: these change opencap's own state, not a PLC. They are therefore allowed,
  but as authenticated, audited admin API actions, never as writable tags: north servers stay
  read-only (ADR-0008). The enable/disable switch is not offered (SAF-01).
- **Status:** Not started
- **Requirements:**
  - `POST /api/v1/north/{agent}/outbox/purge` (admin) with `If-Match` set to the current backlog
    count deletes the queued messages. It answers 412 if the count changed, and the audit log
    records who purged how many.
  - `PUT /api/v1/north/{agent}/forward-mode` (admin, audited) sets SAF-08's mode.
  - Writes to the corresponding system tags are refused by every north server.
- **Acceptance:** API tests for roles (viewer gets 403), audit entries and the 412 race. A north
  write-refusal test (#29 for OPC UA).

### SAF-17 Store-and-forward events (manual p. 51)
- **What:** Datastore problems and status go to the event log.
- **Decision:** Adapt: structured log events with stable names. Plan 15 catalogues them and plan
  12 makes them viewable.
- **Status:** Done
- **Requirements:** The events `saf_reopened` (messages, bytes), `saf_dropped_oldest` (dropped,
  max_bytes, up_to_id, queued; at most once a minute) and `saf_not_wal` (journal_mode) are emitted
  as JSON log lines. The new events of SAF-03/04/06 (`saf.open_failed`, `saf.disk_low`, and
  LOG-36's `saf.write_failed` and `saf.corrupt`) follow the same rules.
- **Acceptance:** The Rust outbox (`opencap-north/src/saf.rs`) emits the same event names
  (RFC 0001 §2.9). Add `structlog.testing.capture_logs` tests for the three events: none pin
  them yet.

### SAF-18 No separate store-and-forward service (manual p. 232)
- **What:** KEPServerEX installs a separate service that starts and stops with the components
  that need local storage.
- **Decision:** Adapt: each durable agent owns its outbox inside the engine process. There is one
  fewer service to install, monitor and keep in step (ADR-0011).
- **Status:** Done
- **Requirements:** No extra OS service or process is needed for store-and-forward, and the
  engine's service unit is enough.
- **Acceptance:** The Phase 1 acceptance suite runs with only the engine process and a broker.

### SAF-19 One outbox per durable agent (manual p. 232)
- **What:** Several server components use store and forward.
- **Decision:** Adapt: every durable agent (each MQTT agent, Sparkplug B, REST client, data
  logger) gets its own outbox file, so they never share a failure domain (ADR-0011).
- **Status:** Planned (#51)
- **Requirements:**
  - Each agent's outbox is `data/outbox/<agent>.sqlite`. A single MQTT agent keeps `saf.path`
    until a second durable agent exists.
  - A slow agent never delays another agent's drain.
  - Sparkplug publishes a new BIRTH before draining its outbox.
- **Acceptance:** Sparkplug TCK and an outage test with two agents, one of them blocked (#51).
  Data logger sink outage test (spec #25).

### SAF-20 ThingWorx-specific store-and-forward parts (manual p. 47–51)
- **What:** Value streams on the ThingWorx platform, Thing binding, and SAF configuration tied
  to the ThingWorx native interface.
- **Decision:** Out of scope: the ThingWorx native interface is a closed PTC protocol (FR-8,
  ADR-0009). MQTT and REST agents with their own outboxes cover the goal. Plan 02 covers the
  interface itself.
- **Status:** Out of scope

## Open questions
- **Disk-low policy (SAF-06 vs LOG-36):** SAF-06 proposes dropping the oldest rows when free disk
  falls below the reserve, which keeps the file from growing. Plan 15 LOG-36 says no queued
  message may be deleted beyond the configured cap when the disk is full, so new messages would be
  lost instead. One rule should be picked.
- **Drop policy:** opencap drops the oldest data when full, and KEPServerEX drops the newest.
  Should `saf.full_policy: drop_oldest | drop_newest` be offered per agent, for consumers that
  need the oldest data (e.g. billing)?
- **Outbox unavailable (SAF-04):** is a degraded mode with a memory queue right, or should the
  engine refuse to start (fail fast, with the supervisor restarting it)?
- **Live-first mode (SAF-08):** is there a site that needs it? If not, it can stay deferred past
  M5.
- **Purge (SAF-16):** is purging an outbox an operation operators should have at all, or only
  through a CLI on the node?
- **Milestones:** SAF-03/04/06/12/14 fit M2 (both engines, small). Confirm, or keep them unplanned
  until an issue exists.

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| SAF-01 | Keep every update until the peer confirms it | Adapt | Done | D-016, #64 | M1 |
| SAF-02 | Outbox location setting | Adapt | Done | — | M1 |
| SAF-03 | Validate the outbox location | Adapt | Planned (#142) | #142 | — |
| SAF-04 | Outbox cannot be opened: degraded mode | Adapt | Planned (#142) | #142 | — |
| SAF-05 | Size cap with oldest-first drop | Adapt | Done | D-016 | M1 |
| SAF-06 | Free-disk reserve | Adapt | Planned (#142) | #142 | — |
| SAF-07 | Ordered forwarding after a reconnect | Adopt | Done | D-016 | M1 |
| SAF-08 | Live-first forwarding (On Hold) | Defer | Planned (#143) | #143 | M5 |
| SAF-09 | Survive restarts | Adopt | Done | D-016 | M1 |
| SAF-10 | Survive upgrades and the engine cut-over | Adapt | Done | #64 | M2 |
| SAF-11 | Changing store-and-forward settings | Adapt | Done | #63 | M1 |
| SAF-12 | Sizing estimate in `opencap check` | Adapt | Planned (#121) | #121 | — |
| SAF-13 | Backlog count and drops | Adapt | Done | #63, #64 | M1 |
| SAF-14 | Backlog bytes, age and capacity | Adapt | Planned (#143) | #143 | — |
| SAF-15 | Status as system tags | Adapt | Planned (#22) | #22, #56 | M5 |
| SAF-16 | Controls as admin API actions | Adapt | Planned (#143) | #143 | — |
| SAF-17 | Store-and-forward events | Adapt | Done | — | M1 |
| SAF-18 | No separate store-and-forward service | Adapt | Done | ADR-0011 | M1 |
| SAF-19 | One outbox per durable agent | Adapt | Planned (#51) | #51 | M3 |
| SAF-20 | ThingWorx-specific parts | Out of scope | Out of scope | ADR-0009 | — |
