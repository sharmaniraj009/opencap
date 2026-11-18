# 15 Event log messages

> Source: KEPServerEX V6 server manual (© PTC), pages 250–331, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes

Pages 250–307 list the messages the server writes to its event log pane. Each entry has a
severity (informational, warning, error, or a separate "security" kind for user and
permission changes) and, for most errors and warnings, likely causes and remedies. The
catalogue starts with how to see the server and driver versions (pp. 250–251). It then covers
project loading and saving, driver and plug-in loading, building the channel/device/tag tree,
automatic tag generation, modem lines, demotion, and user management. Further sections cover
runtime lifecycle and project replacement, CSV and alias import, the Configuration API
service, OPC UA endpoints and certificates, the legacy client interfaces (OPC .NET, HDA, DDE,
AE), the script engine, the ThingWorx interface and its store-and-forward datastore, and the
generic serial and socket errors that drivers share. Page 308 lists support resources; pages
309–331 are the index. The pattern that matters to us: every message names the object it is
about (channel, device, tag, file, endpoint, user), has a fixed severity, and comes with a cause
and a fix the operator can act on.

## opencap today

**Python reference engine.**
- **Process log.** `src/opencap/logsetup.py` configures structlog to write one JSON object per
  line to stdout, with `timestamp` (ISO-8601 UTC), `level` (lower case), `event` and context
  fields. opencap never writes or rotates log files: journald, Docker or WinSW do that
  (D-030, `docs/deployment.md` "Logs and log rotation").
- **Levels.** INFO carries only state changes, plus one `device.stats` line per device every
  `stats_interval_s` (D-023, `docs/architecture/04-scale-reliability.md` §4.5). DEBUG logs
  every request.
- **Library logs.** Records from libraries that use the standard `logging` module are printed
  as bare text (`format="%(message)s"`), not JSON. Uvicorn's start-up lines are an example.
- **Event names today:**
  - Device worker (`src/opencap/core/channel_worker.py`, bound with `channel` and `device`):
    `device.connected`, `device.disconnected`, `device.ok`, `device.offline`,
    `device.demoted`, `device.resumed`, `device.not_polled`, `device.invalid_tags`,
    `device.stats` (INFO); `device.block_rejected`, `device.unsupported`, `device.idle`
    (WARNING); `device.connect_failed`, `device.reconnected`, `device.connection_dropped`,
    `device.reopen`, `device.probe_failed`, `device.overrun`, `device.scale_error`,
    `device.request` (DEBUG); `device.loop_error`, `channel.on_cycle_error` (exception).
  - Channel: `channel.started`, `channel.stopped`, `channel.closed`.
  - Runtime (`src/opencap/core/runtime.py`): `poller_started`, `poller_stopped`,
    `config_reloaded`, `reload_failed` (ERROR), `mqtt_settings_changed` (WARNING).
  - North side: `mqtt_connected`, `mqtt_disconnected`, `mqtt_message_rejected`
    (`north/mqtt.py`); `publish_failed`, `status_publish_failed`, `device_status_changed`
    (`north/publisher.py`); `saf_reopened`, `saf_dropped_oldest` (at most one a minute),
    `saf_not_wal` (`north/saf.py`).
  - Drivers: `ab_logix.*` (`batching`, `batching_demoted`, `batching_probe_given_up`,
    `kind_mismatch`, `tags_missing`, `tags_recased`, `identity_changed`, `tag_list`) and
    `ab_pccc.*` (`block_rejected`, `file_directory`, `file_directory_no_answer`,
    `identity_changed`). The other DEBUG driver lines use free-text names (`"modbus
    connect"`, `"df1 bad frame"`, …).
- **Tests on event names.** Tests check a few names with `structlog.testing.capture_logs`:
  `device.connected`, `device.demoted`, `device.resumed`, `device.stats`,
  `mqtt_settings_changed`, and the `ab_logix.*` and `ab_pccc.*` names (`tests/unit`,
  `tests/integration`).
- **Audit log** (`src/opencap/api/audit.py`, D-043). This is a separate JSONL file
  (`api.audit_log`, default `data/audit.jsonl`), written with fsync and rotated to `.1` at
  16 MiB. It records every accepted config change, import and reload: time, token name, role,
  `via` (ui/api), action, target, revision, files and field-level changes. Actions include
  `channel.*`, `device.*`, `tag.*`, `map.*`, `override.*`, `config.import`,
  `config.import_kep` and `reload`. Rejected changes, reads and logins are not recorded.
  `GET /api/v1/audit` filters by action prefix, who and target, and the web UI has an
  **Audit** page. There is no event-log page.
- **`/health`.** Fields: `status`, `version`, `instance`, `mqtt_connected`, `saf_queue`,
  `channels`, and `devices` (counts of ok/offline/demoted). It is liveness only (D-030).
  `GET /api/v1/status` adds every device and the pending reload.
- **Metrics** (`src/opencap/metrics.py`): `poll_requests_total{device,result}`,
  `poll_cycle_seconds`, `poll_overrun_total`, `device_state`, `mqtt_published_total`,
  `saf_queue_messages`, `saf_dropped_total`.
- **HTTP errors.** `ConfigError` subclasses in `src/opencap/config/store.py` map to 400, 404,
  409, 412, 422, 428, 503 (read-only), 507 (disk full) and 500 (partial write). The body
  carries `detail` plus pydantic-style `{loc, msg, type}` errors. Auth errors answer 401/403.
  None of these responses writes a log line.
- **`opencap check`** (`src/opencap/core/check.py`, also `GET /api/v1/project/validate`):
  - an invalid config prints the error and exits 1;
  - the per-device block plan, the `BAD` count and the `bad_config` reasons (`--verbose`);
  - devices with no plan;
  - an overload warning per channel;
  - the loader warnings (`src/opencap/config/loader.py`): unknown ids in overrides or the
    map, unused serial port mappings, missing SubDeviceId, duplicate values.

  The loader warnings are not logged when `run` starts.
- **Import report.** `opencap import-kep` and the import dry run list skipped tags and
  excluded devices (`src/opencap/config/kep_import.py`).

**Rust engine** (`engine/crates/*`):
- **Format.** `tracing_subscriber` JSON on stdout (`opencap-engine/src/main.rs`). The event
  name sits under `message`, not `event` (RFC 0001 §2.9), and levels use tracing's upper-case
  spelling.
- **Same names as Python** for the device worker (`opencap-core/src/worker.rs`), the runtime
  (`poller_started`, `poller_stopped`, `config_reloaded`, `reload_failed`,
  `mqtt_settings_changed`) and the north side.
- **Rust-only events:** `api_started` and `poller_stopping` (with the signal).
- **Rust-only metric:** `driver_panics_total{device}` (D-061).
- **Missing:** the Rust API (`opencap-api`) has no configuration API and no audit log yet
  (RFC 0001 §7).

**Plans on record:**
- FR-10 (`docs/architecture/01-requirements.md`) lists an event log among the diagnostics.
- `04-scale-reliability.md` §4.5 plans a bounded ring buffer plus a JSONL file, shown in the
  UI.
- `02-high-level-design.md` stores the event log and the audit log as append-only JSON Lines
  with size-based rotation.
- RFC 0001 §7 tracks "Diagnostics / system tags" (event log included) under #56 for M5,
  with the system tags spec #22. #56 depends on #22 and #41 (`docs/BOARD.md`).
- The BOARD backlog also lists "event log" under Diagnostics as not yet filed.

## Event classes

Convention: **Status** is the state of that class's requirements as written below.
**opencap event(s)** lists what already exists. Requirements that apply to every class
(catalogue entry, fields, test, both engines) are in the cross-cutting section and are not
repeated.

### LOG-01 Driver and plug-in loading, component inventory (manual pp. 250–254, 258–261, 263–264, 266, 272, 277, 301, 307)
- **Covers:** the version and driver summary screens; a driver or plug-in library that is
  missing, duplicated, the wrong version or corrupt; no drivers loaded at all; a feature the
  project needs but that is not installed; a driver that fails to initialise; banners when a
  driver starts, stops, loads or unloads.
- **Severities:** Error, Warning, Informational.
- **Decision:** Adapt. Drivers are built into opencap, and protocol libraries are imported
  lazily (D-002). There is no DLL to lose; the opencap failure cases are:
  - an unknown `driver` name (config invalid, D-061);
  - a known driver whose optional extra is not installed, or that this engine does not
    implement yet (the device is not polled);
  - later, a plug-in process that fails to start (plugin host #49).
- **Status:** Planned (#49)
- **opencap event(s):** `device.unsupported` (WARNING, `error` names the missing library or
  "not implemented"); config invalid at load. Proposed: `poller_started` gains `drivers` (the
  available drivers with versions), plus `plugin.start_failed` and `plugin.restarted`
  (with #49).
- **Requirements / Acceptance:**
  - A device whose driver's optional extra is missing logs one `device.unsupported` at
    WARNING, with `device`, `driver`, and a fix naming the extra to install (for example
    `pip install 'opencap[ab]'`). Its `/devices/{id}` `last_error` gives the same reason.
  - `poller_started` lists every driver that this engine and this install can run, with its
    library version. `GET /api/v1/status` returns the same list (the opencap version of the
    manual's version screen).
  - A plug-in process that exits or fails its handshake logs `plugin.start_failed` (ERROR,
    `plugin`, `reason`). A restart logs `plugin.restarted` and increments
    `plugin_restarts_total{plugin}` (04 §4.5).

### LOG-02 Project load and validation (manual pp. 252–255, 260, 266, 273, 275, 277)
- **Covers:** the project file cannot be parsed, is corrupt, is from an incompatible or newer
  version, refers to a missing object, a device model this build lacks, or an object type
  that is not allowed; informational opening and closing of a project.
- **Severities:** Error (most), Warning, Informational.
- **Decision:** Adapt. opencap's "project" is the config directory. An invalid config is
  refused as a whole: `run` does not start, a reload keeps the running config, and the API
  answers 422. The two gaps: nothing on stdout says which config revision is running, and the
  loader warnings only show up in `opencap check`.
- **Status:** Not started
- **opencap event(s):** `reload_failed` (ERROR, `error`); `check` prints the invalid-config
  error and exits 1; 422 with `{loc,msg,type}` errors. Proposed: `config.loaded` (INFO:
  `revision`, `channels`, `devices`, `tags`, `warnings`), `config.warning` (WARNING: `file`,
  `path`, `reason`), and `config.invalid` (ERROR: `file`, `errors` (first 20), `count`).
- **Requirements / Acceptance:**
  - At start and after every reload, exactly one `config.loaded` line, carrying the same
    `revision` as `GET /api/v1/project/status`.
  - Each loader warning is logged once per load as `config.warning`, with the file and key it
    concerns. The text matches the line `opencap check` prints.
  - An invalid config at start logs `config.invalid` before the process exits non-zero. A test
    starts `run` on a config with a broken `devices.json` and asserts the event and the exit
    code.
  - A config written by a newer schema version is refused with a message that names both
    versions (project file v2, #37).

### LOG-03 Project save, file names and formats (manual pp. 254, 260, 275, 277, 279)
- **Covers:** a save that fails; save-format restrictions per driver; a dependent file that
  cannot be written; file-name rules for loading and saving through the API (empty names,
  invalid characters, overwriting, the required directory and extensions).
- **Severities:** Error, Warning.
- **Decision:** Adapt. Writes are atomic and all or nothing (D-041). A failed write answers
  503 (read-only), 507 (disk full) or 500 (partial write, files named). The import takes a
  file name and a body with size caps. What is missing is a log line, so an operator who did
  not make the request never learns that a write failed.
- **Status:** Not started
- **opencap event(s):** HTTP 503/507/500 and 422 bodies. Proposed: `config.write_failed`
  (ERROR: `who`, `action`, `files`, `reason` = `read_only` | `disk_full` | `partial` | `io`,
  `status`).
- **Requirements / Acceptance:**
  - Every `StorageError` or `PartialWrite` the API returns is also logged as
    `config.write_failed`, with the same files and status. A test with a read-only config
    directory asserts the 503 and the event.
  - A `partial` write is logged at ERROR, and the fix names the `.bak` files to restore.

### LOG-04 Project backups and revisions (manual pp. 254, 265)
- **Covers:** a save is aborted because its backup copy cannot be written; an informational
  note when a backup is made.
- **Severities:** Error, Informational.
- **Decision:** Adapt. Every write keeps the previous file as `<file>.bak`. If that copy
  cannot be made, nothing is changed (`StorageError`). Each accepted change records its new
  revision in the audit log, and `/project/revisions` lists them. The audit entry replaces
  the manual's "backup created" note.
- **Status:** Done
- **opencap event(s):** audit entries with `revision`; `GET /api/v1/project/revisions`; a
  failed backup surfaces through LOG-03 (`config.write_failed`).
- **Requirements / Acceptance:**
  - An accepted write leaves `<file>.bak` holding the previous content and one audit entry
    with the new revision.
  - A write whose backup step fails changes nothing and answers 503/507.

### LOG-05 Project file encryption and passwords (manual pp. 277–279)
- **Covers:** rules on which file types may be saved encrypted, and a password being
  required (or refused) for encrypted project files.
- **Severities:** Error.
- **Decision:** Out of scope. opencap config files hold no secrets: tokens and credentials
  come from environment variables (NFR-11, FR-11). The files are protected by file
  permissions and a read-only mount (D-030). There is no encrypted project format to report
  on.
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-06 Adding, editing, moving and deleting objects (manual pp. 252–253, 255, 258, 272, 277, 279, 305)
- **Covers:**
  - adding a channel or device fails because of the driver, or because of a duplicate;
  - changing a device's model is refused while the device is referenced;
  - an add, move, update or delete fails, with a reason;
  - an object being replaced is still in use, or was removed meanwhile;
  - edits are refused while a driver is processing tags.
- **Severities:** Error, Warning.
- **Decision:** Adapt. The configuration API answers 404, 409 or 422 with field-level errors.
  The driver validates addresses before anything is written. A write changes files only, and
  the reload applies it (D-039), so opencap has no "busy while polling" refusal. Rejected
  changes stay out of the audit log (D-043, nothing changed), but they should appear in the
  process log so someone can troubleshoot a UI session afterwards.
- **Status:** Not started
- **opencap event(s):** HTTP 404/409/412/422/428. Proposed: `config.change_rejected` (INFO:
  `who`, `via`, `action`, `target`, `status`, `count`, the first error's `loc` and `msg`).
- **Requirements / Acceptance:**
  - Every 4xx from a config write logs `config.change_rejected` at INFO. Field values are
    never logged, only locations and messages.
  - A test posts a duplicate device and asserts the 409 and one event; it also asserts that
    no audit entry was written.

### LOG-07 Naming rules and renames (manual pp. 273–275)
- **Covers:** a rename collides with an existing name; a name contains forbidden characters
  or starts with an underscore; a name is longer than allowed.
- **Severities:** Error, Warning.
- **Decision:** Adapt. opencap forbids `/` in channel and device names (device ids are
  `<channel>/<device>`). It also rejects empty names and device names reserved by the API
  paths. Duplicates answer 409/422. A rename moves the map entry and the override along. Tag
  names may contain dots (group paths). Once system tags exist, names starting with `_` must
  be reserved for them.
- **Status:** Planned (#22)
- **opencap event(s):** 422/409 bodies; LOG-06's `config.change_rejected`.
- **Requirements / Acceptance:**
  - A tag, device or channel name starting with `_` is refused with 422 once system tags are
    specified (#22). The importer reports such names from a KEPServerEX project instead of
    importing them.
  - Each naming rule has one test that triggers its 422 and checks `loc` and `msg`.

### LOG-08 Automatic tag generation (manual pp. 255–256, 262, 265–266)
- **Covers:**
  - start and completion of tag generation from a device;
  - counts of tags created, overwritten or kept;
  - an existing tag is not overwritten;
  - the device did not answer, or returned nothing without a reason;
  - suppression of further per-tag messages once there are too many.
- **Severities:** Warning, Informational.
- **Decision:** Defer to the Logix browse issue (#11). Today opencap reads the controller tag
  list only to check configured tags. Generation must never write to the PLC.
- **Status:** Planned (#11)
- **opencap event(s):** existing `ab_logix.tag_list`, `ab_logix.tags_missing`,
  `ab_logix.tags_recased`, `ab_pccc.file_directory`. Proposed: `taggen.started`,
  `taggen.completed` (`created`, `overwritten`, `kept`, `skipped`), `taggen.failed` (`reason`
  = `no_response` | `empty` | `unsupported`).
- **Requirements / Acceptance:**
  - One `taggen.completed` line per run, with counts. Per-tag conflicts are summarised (the
    first 20 names plus a count), never one line per tag. This is the same idea as the
    manual's message suppression (p. 256).
  - A run against a simulator that does not answer logs `taggen.failed` with
    `reason=no_response` and changes no files.

### LOG-09 Tag address validation (manual pp. 257–258, 262, 305)
- **Covers:** a tag address that is too long or invalid for the device, invalid tag
  parameters, and a tag-import file name that includes a path.
- **Severities:** Warning.
- **Decision:** Adopt. opencap checks addresses at three points:
  - at plan time: the tag becomes `bad_config` and is listed in `check`, `/devices/{id}` and
    the UI;
  - at write time: the API answers 422;
  - at run time: when the PLC rejects a block, that block is disabled until reload.
- **Status:** Done
- **opencap event(s):** `device.invalid_tags` (INFO: `count`, `tags` (first 20)),
  `device.block_rejected` (WARNING: `block`, `error`), `ab_pccc.block_rejected`,
  `ab_df1.block_rejected`, `bad_config` quality, `poll_requests_total{result="bad_config"}`,
  and `check`'s `BAD` column.
- **Requirements / Acceptance:**
  - A config with one bad address loads, logs one `device.invalid_tags` naming the tag, and
    serves the tag with `bad_config` quality.
  - When a simulator rejects a block, the engine logs `device.block_rejected` once and stops
    requesting that block until reload.

### LOG-10 Scaling parameter validation (manual p. 259)
- **Covers:** invalid scaling parameters set on a tag.
- **Severities:** Warning.
- **Decision:** Adapt. Today a degenerate scale (`raw_hi == raw_lo`) is accepted at load
  (`Scale` in `config/models.py` has no validator). Every read of that tag then becomes
  `bad_decode`, logged only at DEBUG as `device.scale_error`. The config should be refused at
  load instead.
- **Status:** Not started
- **opencap event(s):** `device.scale_error` (DEBUG). Proposed: a load-time validation error,
  and `device.scale_error` raised to WARNING once per tag per load.
- **Requirements / Acceptance:**
  - A tag with `raw_hi == raw_lo`, or a non-finite scale bound, is a validation error: 422
    from the API, and `opencap check` exits 1 naming the tag. Both engines agree (config
    fixture, #39).
  - A run-time scaling failure logs `device.scale_error` at WARNING, at most once per tag
    until the next reload. Later failures are counted, not logged.

### LOG-11 Auto-demotion and promotion (manual pp. 259–260, 265–266)
- **Covers:** a device is demoted after repeated communication failures; it is promoted again
  for a retry; a client enables or disables demotion at run time.
- **Severities:** Warning, Informational.
- **Decision:** Adopt (state machine of D-015). Toggling demotion from a client would be a
  write to a system tag. That part is Out of scope (ADR-0008) and goes to #19 and #22.
- **Status:** Done
- **opencap event(s):** `device.demoted` (INFO: `after_failed_cycles`, `for_ms`, `error`),
  `device.probe_failed` (DEBUG), `device.resumed` (INFO), `device_state` = 2,
  `/health.devices.demoted`, and the `offline`/`ok` status topic (`device_status_changed`).
- **Requirements / Acceptance:**
  - Killing one simulator demotes its device after `demote_after` failed cycles, with exactly
    one `device.demoted` line. Restarting it gives one `device.resumed` line. Both engines do
    this (phase 1 acceptance, `tests/unit/test_channel_worker.py`).

### LOG-12 Data collection enabled or disabled (manual p. 265)
- **Covers:** a device's data collection being switched on or off.
- **Severities:** Informational.
- **Decision:** Adapt. opencap switches a device off through `enabled` in `devices.json` or
  `overrides.yaml`. The change goes through the API and the audit log, and the reload applies
  it. There is no run-time toggle tag (that would be a write).
- **Status:** Done
- **opencap event(s):** `device.not_polled` (INFO, `reason` = `device disabled` | `channel
  disabled` | `no readable blocks` | plan error); audit `device.update` / `override.update`;
  `last_error` on `/devices/{id}`.
- **Requirements / Acceptance:**
  - Disabling a device through the API and reloading gives one audit entry and one
    `device.not_polled reason="device disabled"` line, and stops all requests to that device.

### LOG-13 Simulation mode (manual pp. 264–265)
- **Covers:** a device being switched into or out of simulated (no wire traffic) mode.
- **Severities:** Informational.
- **Decision:** Defer to the simulator/memory driver (#12). If a per-device "simulated" flag
  is added later, it must be visible in data quality as well as in the log, so that a SCADA
  never mistakes simulated values for live ones.
- **Status:** Planned (#12)
- **opencap event(s):** proposed `device.simulated` (INFO: `enabled`), logged at start and on
  change.
- **Requirements / Acceptance:**
  - A device in simulated mode logs `device.simulated enabled=true` at start. `/devices/{id}`
    and the status topic show it as simulated, and no request reaches the network (checked
    with a simulator that counts connections).

### LOG-14 Device discovery (manual pp. 254, 276)
- **Covers:** discovery starting, completing (with a count), being cancelled, failing to
  start, or exceeding the maximum number of devices.
- **Severities:** Error, Informational.
- **Decision:** Defer (M5, not filed). Discovery must be read-only, rate-limited and opt-in,
  because sites sit behind a VPN (C-6).
- **Status:** Not started
- **opencap event(s):** proposed `discovery.started`, `discovery.completed` (`found`),
  `discovery.cancelled`, `discovery.failed` (`reason`), `discovery.limit_reached`.
- **Requirements / Acceptance:**
  - A discovery run on a range larger than the cap stops at the cap, logs
    `discovery.limit_reached`, and returns what it found. Every run writes an audit entry
    naming who started it.

### LOG-15 Modem and telephony lines (manual pp. 252, 257–259, 261–266)
- **Covers:** opening, dialling, connecting and dropping modem lines; telephony service
  errors; phonebook and auto-dial configuration.
- **Severities:** Error, Warning, Informational.
- **Decision:** Out of scope. opencap reaches sites over IP and VPN (C-6). There is no
  dial-up and no Windows telephony layer.
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-16 Network adapter selection (manual pp. 258, 262, 302)
- **Covers:** the network adapter a channel names does not exist on this PC (the server falls
  back to the default, or cannot bind).
- **Severities:** Warning, Error.
- **Decision:** Defer (M5, not filed). opencap leaves interface choice to OS routing. If a
  per-channel source address is ever added, it must fail loudly rather than fall back
  silently.
- **Status:** Not started
- **opencap event(s):** proposed `channel.bind_failed` (ERROR: `channel`, `address`,
  `reason`).
- **Requirements / Acceptance:**
  - A channel configured with a source address that does not exist on the host is not
    started. It logs `channel.bind_failed`, its devices report the reason, and the other
    channels keep polling.

### LOG-17 Serial over TCP (Ethernet encapsulation) (manual p. 260)
- **Covers:** an invalid IP address for a device on a serial channel tunnelled over Ethernet.
- **Severities:** Warning.
- **Decision:** Adapt. opencap uses `transport: tcp|rfc2217|serial` and the `serial_ports`
  map in `overrides.yaml` (`docs/driver_params.md`, D-033, D-037):
  - a mapping that cannot be parsed makes the config invalid;
  - a mapping no device uses is a warning;
  - an `unmapped` port leaves the device disabled, with a warning.
- **Status:** Done
- **opencap event(s):** config invalid at load; `check` warnings; `device.not_polled`.
- **Requirements / Acceptance:**
  - `serial_ports: {COM5: "not-an-address"}` fails `opencap check` (exit 1) with the key
    named. An unused entry appears as a warning, and the config still loads.

### LOG-18 Serial port errors (manual pp. 301–302, 304)
- **Covers:** a COM port that is missing, in use or cannot be opened; line settings the port
  refuses; no thread available for serial I/O; framing or parity errors on the line.
- **Severities:** Error, Warning.
- **Decision:** Adapt. pyserial errors surface as a failed connect, then the device goes
  offline with the error text. Stable `reason` codes come from LOG-R6.
- **Status:** Done
- **opencap event(s):** `device.connect_failed` (DEBUG: `error`, `retry_in_s`),
  `device.offline` (INFO: `error`), `poll_requests_total{result="conn_error"}`, and DF1
  `df1 bad frame` (DEBUG).
- **Requirements / Acceptance:**
  - A device on a non-existent local port goes offline with `error` naming the port, and
    `/devices/{id}` shows the same text (Linux CI with a missing `/dev/tty*` path).

### LOG-19 Socket errors and devices not responding (manual pp. 302–307)
- **Covers:** socket errors while connecting, sending, receiving or binding; a device that
  does not answer within the request timeout; Windows socket-library start-up problems.
- **Severities:** Error, Warning.
- **Decision:** Adopt for socket and timeout failures. The Windows socket-library messages
  (pp. 302–303) are Out of scope (they are platform-specific).
- **Status:** Done
- **opencap event(s):** `device.connect_failed`, `device.connection_dropped`,
  `device.reconnected`, `device.disconnected`, `device.offline`, `device.demoted`,
  `poll_requests_total{result="timeout"|"conn_error"}`, and `last_error` on
  `/devices/{id}`.
- **Requirements / Acceptance:**
  - A simulator that accepts TCP but never answers produces `timeout` results, then
    `device.offline`, then `device.demoted`, in that order, in both engines (D-015 known
    limit).

### LOG-20 Write failures and write rejections (manual pp. 280, 288, 292, 294, 304–305)
- **Covers:** writes refused on read-only items or disabled devices, type conversion and
  scaling failures on write, array size mismatches, and writes the device rejected.
- **Severities:** Warning.
- **Decision:** Out of scope. opencap never writes PLC values (ADR-0008). The question goes
  back to RFC 0002 (#19). The north-server files decide how a client's write attempt is
  refused and logged (for example on OPC UA).
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-21 Time zones (manual p. 261)
- **Covers:** a device configured with a time zone the system does not know; the server falls
  back to a default.
- **Severities:** Warning.
- **Decision:** Adapt. opencap has one `payload.timezone` (no per-device zone; time sync to a
  PLC would be a write). It is not checked at load, so an unknown name only fails once
  payloads are built. opencap must refuse such a name at load and never fall back silently.
- **Status:** Not started
- **opencap event(s):** none at load; `publish_failed` at run time. Proposed: a validation
  error.
- **Requirements / Acceptance:**
  - `payload.timezone: Mars/Olympus` fails `opencap check` (exit 1) and the API (422), naming
    the key. Both engines accept the same set of zone names (shared fixture).

### LOG-22 CSV tag import (manual pp. 262, 271–275)
- **Covers:**
  - a general CSV import failure;
  - a malformed header (overflowing, unknown or duplicate field names, or no header);
  - row-level failures: field overflow, insertion failed, invalid tag or group name, name too
    long, missing address.
- **Severities:** Error, Warning.
- **Decision:** Adapt with the KEPServerEX CSV issue (#10). Errors are reported per row in the
  dry-run response, like the project import. Nothing is written while any row is invalid.
- **Status:** Planned (#10)
- **opencap event(s):** proposed audit action `config.import_csv`; LOG-06's
  `config.change_rejected` for refused uploads.
- **Requirements / Acceptance:**
  - The dry run returns `errors` with `row`, `column`, `value` (truncated) and `reason` for
    every bad row, plus header errors with no row number. A test file with one bad row in
    each category gets one error per category.
  - A file name containing a path is refused (422). The upload cap applies.

### LOG-23 Alias map import (manual p. 273)
- **Covers:** CSV rows mapping an alias to an invalid tag address, or with an invalid alias
  name.
- **Severities:** Error.
- **Decision:** Out of scope. Alias maps exist for DDE and legacy client addressing. opencap
  has no DDE (RFC 0001 §7), and clients address devices as `<channel>/<device>`. If another
  file adopts aliases, this class moves there.
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-24 Users, groups, permissions and access denials (manual pp. 267–271, 274, 278)
- **Covers:**
  - access to an object denied for a user;
  - users and groups created, renamed, moved, enabled, disabled or deleted;
  - permission changes and password changes or resets;
  - user import replacing or failing;
  - the Windows account lacking rights to run the application.
- **Severities:** Security, Error.
- **Decision:** Adapt with the security RFC (#21). Security-kind events go to the audit log,
  not only to stdout. Denied requests are logged at WARNING, rate-limited. Today tokens and
  roles are fixed in `poller.yaml` (D-040), and 401/403 answers are not logged. The Windows
  account case is Out of scope.
- **Status:** Planned (#21)
- **opencap event(s):** 401/403 responses; audit for config changes only. Proposed:
  `api.auth_failed` (401) and `api.forbidden` (403), both WARNING with `method`, `route`,
  `token` (the name if known, never the secret), `remote` and `suppressed`. Also proposed:
  audit actions `user.*` / `role.*` when #21 adds them, and `tokens.changed` (names and roles
  only) when a reload changes the token set.
- **Requirements / Acceptance:**
  - A wrong token logs one `api.auth_failed` and answers 401. A burst of 100 wrong tokens
    gives one line plus a summary with `suppressed=99` within a minute. A test greps all
    captured output for the token value and finds none.
  - A viewer calling `POST /api/v1/reload` gets 403 and one `api.forbidden` with
    `token=<name>`.

### LOG-25 Configuration sessions and concurrent edits (manual pp. 275, 280, 282)
- **Covers:** configuration sessions starting, ending, gaining or losing write access, and
  having permissions changed; a second configuration tool conflicting with the first; a
  startup project ignored while a session is open.
- **Severities:** Security, Warning.
- **Decision:** Adapt. opencap has no sessions: the REST API is stateless and uses optimistic
  concurrency (`If-Match`: 428 when it is missing, 412 when it is stale). Every accepted
  change is audited with `who` and `via`.
- **Status:** Done
- **opencap event(s):** 428/412 responses; audit `who`, `role` and `via`.
- **Requirements / Acceptance:**
  - Two writers using the same revision: the first gets 200 and an audit entry, the second
    gets 412 and no audit entry (`tests/unit/test_config_api.py`).

### LOG-26 Runtime lifecycle, reload and project replacement (manual pp. 271–272, 274–277, 279–281)
- **Covers:**
  - the runtime starting, re-initialising, exiting and shutting down;
  - the running project being replaced, reset or saved;
  - replacing with the same project is refused;
  - a startup project failing to load or update;
  - remote connection or synchronisation with the runtime failing;
  - a server-wide communication mode change.
- **Severities:** Informational, Warning, Error.
- **Decision:** Adopt. opencap's equivalents:
  - start and stop of the poller;
  - `POST /api/v1/reload` or SIGHUP: only changed channels restart, and an invalid config
    keeps the old one;
  - import through the API (dry run by default).

  The communication-mode change (p. 275) belongs to the channel serialization file (D-015).
- **Status:** Done
- **opencap event(s):** `poller_started` (`version`, `channels`, `devices`),
  `poller_stopping` (Rust, `signal`), `poller_stopped`, `config_reloaded` (counts of
  started/restarted/stopped/unchanged), `reload_failed` (ERROR), `mqtt_settings_changed`
  (WARNING, restart needed), audit `reload` / `config.import*`.
- **Requirements / Acceptance:**
  - A reload with an invalid file logs `reload_failed`, answers 400, and keeps every channel
    polling.
  - A reload that changes the MQTT section logs `mqtt_settings_changed`
    (`tests/integration/test_topic_prefix_e2e.py`).

### LOG-27 Windows service, installation and operating mode (manual pp. 269, 276, 280–281)
- **Covers:** registering or removing the system service, shutting down for an installation,
  changing the runtime operating mode, and a missing application data directory.
- **Severities:** Informational.
- **Decision:** Out of scope. systemd and WinSW own service registration (D-030, D-031). The
  installers (#34) log their own work. opencap has no licensing or operating modes (FR
  out-of-scope list).
- **Status:** Out of scope
- **opencap event(s):** none (`poller_started` and `poller_stopped` cover process start and
  stop).

### LOG-28 OPC ProgID redirect (manual pp. 269–270)
- **Covers:** OPC ProgIDs added to, removed from, or cleaned out of the redirect list.
- **Severities:** Informational.
- **Decision:** Out of scope. This is OPC DA classic and DCOM registration. Any DA bridge is
  RFC 0004's (#18) and logs its own events.
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-29 Configuration API service: port and TLS (manual pp. 283–284, 288–289)
- **Covers:** the REST configuration service starting with or without TLS on a port, failing
  to bind its port, and certificate problems (bad signature, cannot load, expired,
  self-signed, outdated TLS version).
- **Severities:** Error, Warning, Informational.
- **Decision:** Adapt. opencap's API is plain HTTP bound to localhost or a VPN address, and
  refuses a non-loopback bind without a token (D-019, D-062). TLS, where wanted, is a reverse
  proxy's job (deployment doc). The gaps:
  - Python logs no structured start line;
  - a bind failure is not a named event in either engine.
- **Status:** Not started
- **opencap event(s):** Rust `api_started` (`bind`, `port`); Python only uvicorn's plain-text
  lines. Proposed: `api.started` (INFO: `bind`, `port`, `open_mode`, `tokens` (count)) and
  `api.bind_failed` (ERROR: `bind`, `port`, `reason`), in both engines.
- **Requirements / Acceptance:**
  - Starting with the API port already taken logs `api.bind_failed`, and the process exits
    non-zero (it does not run without its API). A test exists in both engines.
  - Starting with no token on loopback logs `api.started open_mode=true`.

### LOG-30 OPC UA server endpoints, certificates and discovery (manual pp. 268, 284–287)
- **Covers:**
  - endpoints added, removed, enabled or disabled;
  - the instance certificate missing, unimportable or expired;
  - the server failing to start because of its certificate;
  - endpoint configuration that fails to load or initialise (for example an adapter without
    IPv4);
  - socket errors while listening;
  - registering or unregistering with a discovery server.
- **Severities:** Security, Error, Warning, Informational.
- **Decision:** Adopt with the OPC UA server (Python #74, Rust #50). Endpoint changes are
  config changes (audited). Certificate expiry is warned about in advance, not only once it
  has happened.
- **Status:** Planned (#74)
- **opencap event(s):** proposed:
  - `opcua.started` (`endpoints`, `security_policies`);
  - `opcua.listen_failed` (ERROR);
  - `opcua.cert_missing`, `opcua.cert_invalid`, `opcua.cert_expired` (ERROR);
  - `opcua.cert_expiring` (WARNING, `days_left`);
  - `opcua.client_cert_rejected` (WARNING, rate-limited);
  - `opcua.lds_register_failed` (WARNING);
  - audit `opcua.endpoint.*`.
- **Requirements / Acceptance:**
  - A certificate expiring within 30 days logs `opcua.cert_expiring` at start and once a day.
    An expired certificate stops the secured endpoints, logs ERROR, and `/health` reports it.
  - An untrusted client certificate logs `opcua.client_cert_rejected` with the certificate's
    thumbprint and lands in `pki/rejected` (02 §2.4).

### LOG-31 OPC .NET, OPC HDA and DDE interfaces (manual pp. 284, 287–288)
- **Covers:** the OPC .NET service failing to start or not being installed; history reads
  timing out; DDE items or topics that cannot be added; writes through DDE.
- **Severities:** Error, Warning.
- **Decision:** Out of scope. OPC .NET (Xi) and OPC HDA are on the FR out-of-scope list (HDA
  is revisited with the local historian). DDE is ✗ in RFC 0001 §7.
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-32 OPC AE subscription filters (manual p. 288)
- **Covers:** a client setting an alarm subscription filter on an unknown area or source.
- **Severities:** Error.
- **Decision:** Defer to the alarms spec (#24). OPC AE is a backlog north item; an equivalent
  filter error on OPC UA Alarms & Conditions belongs there too.
- **Status:** Planned (#24)
- **opencap event(s):** proposed `alarms.filter_rejected` (INFO, `client`, `filter`),
  rate-limited.
- **Requirements / Acceptance:**
  - A subscription with an unknown area is refused with the protocol's error code and one
    rate-limited event.

### LOG-33 Script engine and profile logs (manual pp. 282–283)
- **Covers:** the script engine service starting, stopping, or failing to bind its port;
  exceptions and errors inside user script functions; log lines emitted by device profiles.
- **Severities:** Error, Informational.
- **Decision:** Out of scope. opencap has no user scripting. Derived tags (#23) are
  declarative; their evaluation errors belong to that spec.
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-34 North-side platform connection (manual pp. 289–292, 296–298)
- **Covers:** the connection to the upstream platform failing (with a known or unknown
  reason), being closed, being rejected for bad credentials, and being established;
  publishing errors; updates lost because the send buffer was full.
- **Severities:** Error, Warning, Informational.
- **Decision:** Adapt. The manual describes the ThingWorx link. opencap's equivalent is the
  MQTT link with an outbox-always design (D-016).
- **Status:** Done
- **opencap event(s):** `mqtt_connected`, `mqtt_disconnected`, `mqtt_connect_failed` (Rust),
  `mqtt_message_rejected` (ERROR), `publish_failed`, `status_publish_failed`,
  `/health.mqtt_connected`, `mqtt_published_total`, `saf_queue_messages`.
- **Requirements / Acceptance:**
  - A network cut to the broker gives one `mqtt_disconnected`, then reconnect attempts with
    backoff, then one `mqtt_connected`, and no data loss (phase 1 outage acceptance, D-021).

### LOG-35 ThingWorx-specific interface events (manual pp. 289–298)
- **Covers:**
  - auto-binding of properties;
  - Thing restarts;
  - push-type mismatches;
  - add/remove item service errors;
  - configuration transfer to and from the platform;
  - the maximum number of Things;
  - settings changes pushed by the platform.
- **Severities:** Error, Warning, Informational.
- **Decision:** Out of scope. RFC 0001 §7 lists the ThingWorx native interface as ✗,
  replaced by documented recipes. The link-level part is LOG-34.
- **Status:** Out of scope
- **opencap event(s):** none.

### LOG-36 Store-and-forward datastore (manual pp. 290–291, 294–296, 298–300)
- **Covers:**
  - the datastore full (disk nearly full, or size limit reached);
  - falling back to memory when the store cannot be attached;
  - reset after file errors or corruption;
  - recreated after an upgrade or a size change;
  - stored data deleted on request, and a delete failing;
  - mode changes, an invalid path, a port conflict, service start and stop.
- **Severities:** Error, Warning, Informational.
- **Decision:** Adapt. opencap's outbox is one SQLite file per durable agent (D-016,
  ADR-0011). It drops the oldest messages at the size cap and logs that once a minute. The
  gaps: disk-full and corruption, which today surface only as generic publish errors.
- **Status:** Not started
- **opencap event(s):** `saf_reopened` (INFO: `messages`, `bytes`), `saf_dropped_oldest`
  (WARNING, rate-limited: `dropped`, `queued`), `saf_not_wal` (WARNING),
  `saf_queue_messages`, `saf_dropped_total`, `/health.saf_queue`. Proposed: `saf.write_failed`
  (ERROR: `path`, `reason` = `disk_full` | `io`) and `saf.corrupt` (ERROR: `path`,
  `moved_to`).
- **Requirements / Acceptance:**
  - With the outbox volume full, polling continues. `saf.write_failed reason=disk_full` is
    logged at most once a minute, `/health` reports `saf_ok=false`, and no queued message is
    deleted to make room beyond the configured cap.
  - A corrupt outbox file at start is moved aside (never deleted), logged as `saf.corrupt`,
    and a fresh outbox is used (see open questions).

### LOG-37 Communication diagnostics capture (manual p. 274)
- **Covers:** channel diagnostics (frame capture) failing to start.
- **Severities:** Error.
- **Decision:** Adopt with #56 (FR-10: frame capture on demand, rate limited).
- **Status:** Planned (#56)
- **opencap event(s):** proposed `diag.capture_started` and `diag.capture_stopped` (`device`,
  `who`, `duration_s`, `frames`), and `diag.capture_failed` (ERROR, `reason`). Each start is
  also an audit entry.
- **Requirements / Acceptance:**
  - Starting a capture writes an audit entry and one INFO line. A capture stops by itself
    after its time limit. A second capture on the same device while one runs answers 409.

### LOG-38 Event log service health (manual pp. 276–277)
- **Covers:** the server losing its connection to the event logging service.
- **Severities:** Security.
- **Decision:** Adapt. The opencap event log is in-process (LOG-R9). If its file cannot be
  written, polling must not stop or slow down, and the failure must be visible.
- **Status:** Planned (#56)
- **opencap event(s):** proposed `event_log.write_failed` (ERROR, once a minute at most) and
  `/health.event_log_ok`.
- **Requirements / Acceptance:**
  - Making the event-log file read-only keeps polling and stdout logging working. It logs
    `event_log.write_failed`, `/health` shows `event_log_ok=false`, and the ring buffer still
    serves `GET /api/v1/events`.

## Logging requirements (cross-cutting)

**LOG-R1 Event names.**
- New events are named `<source>.<what>`, with lower-case snake segments. Sources: `poller`,
  `config`, `api`, `channel`, `device`, the driver name (`modbus_tcp`, `ab_logix`, …),
  `mqtt`, `saf`, `opcua`, `taggen`, `discovery`, `diag`, `event_log`, `plugin`.
- Free-text DEBUG names in drivers (`"modbus connect"`, `"df1 bad frame"`) become dotted
  names (`modbus_tcp.connect`, `ab_df1.bad_frame`).
- Existing INFO and higher names are frozen until the open question on renames is decided,
  because consumers key on them (RFC 0001 §2.9, `docs/deployment.md` examples).

**LOG-R2 Event catalogue.**
- One machine-readable catalogue (proposed: `schema/events.json`, rendered into the docs)
  lists every event at INFO or higher. Each entry has:
  - name, level, source, and required and optional fields with types;
  - the engines that emit it, and the version it appeared in;
  - an operator message, a cause and a fix, all written in our own words.
- CI fails if source code emits a name at INFO or higher that the catalogue lacks, or the
  catalogue lists a name that no engine emits.

**LOG-R3 Line format and common fields.**
- Every stdout line is one JSON object with `timestamp` (ISO-8601 UTC, `Z`), `level`
  (`debug`/`info`/`warning`/`error`), `event`, and `instance`.
- Context fields where they apply:
  - `channel`, `device` (`<channel>/<device>`), `tag`;
  - `path` (a config or data file);
  - `who` (the token name);
  - `reason` (a stable code, LOG-R6) and `error` (free text);
  - `revision` (the config revision).
- The Rust engine must write the name under `event` and use the same level spelling as
  Python. Today it writes `message` and upper-case levels, so the `jq` filters in
  `docs/deployment.md` only match Python lines.

**LOG-R4 Severity rules.** The manual's kinds map as follows: Informational → `info`,
Warning → `warning`, Error → `error`, and Security → an audit-log entry plus an `info` or
`warning` line. Levels:
- `info`: state changes and lifecycle (PLAN §9).
- `warning`: the service continues, but something needs fixing (bad addresses, rejected
  blocks, dropped outbox data, auth failures).
- `error`: a function is down (config refused at start, API not listening, outbox or
  event-log file not writable, partial config write).
- `debug`: per request, never stored in the event log.

**LOG-R5 Operator text.**
- Each catalogue entry has a one-sentence `message` template, a `cause` and a `fix`, written
  for an operator and naming the config key or file to change.
- The event-log API and the UI render them. Stdout lines keep only `event` plus fields (see
  open questions).
- A test checks that every catalogue entry at `warning` or `error` has a non-empty fix.

**LOG-R6 Stable reason codes.**
- Every `warning` or `error` event carries `reason` from a closed list per source. Starting
  lists:
  - device communication: `timeout`, `refused`, `reset`, `unreachable`, `name_resolution`,
    `port_missing`, `port_busy`, `line_settings`, `protocol_error`, `address_rejected`;
  - config writes: `invalid`, `read_only`, `disk_full`, `partial`, `io`;
  - outbox: `disk_full`, `io`, `corrupt`.
- The free-text `error` stays alongside. `/devices/{id}` exposes `last_reason` next to
  `last_error`.
- Both engines map the same simulator faults to the same codes (conformance vectors).

**LOG-R7 Flood control.**
- Repeated events with the same name, device and reason are logged the first time. After
  that, a summary line with `suppressed=N` is logged at most once a minute. This generalises
  what `saf_dropped_oldest` does, and the manual's tag-generation suppression (p. 256).
- State-change events (`device.offline`, `device.demoted`, `device.resumed`,
  `mqtt_connected`, …) are never suppressed.
- Tests: a 1 000-failure burst produces at most two lines per minute.

**LOG-R8 Where events are surfaced.**

| Surface | What goes there |
|---|---|
| JSON on stdout | every event at the configured level (the source of truth; the platform rotates it, D-030) |
| Audit log JSONL | accepted config changes, imports and reloads (today), and security events: token set changes, user and role changes (#21), endpoint changes, diagnostics captures |
| Event log (LOG-R9) | `info` and higher, queryable via the API, shown in the UI |
| `/health` | counts and flags: `mqtt_connected`, `saf_queue`, devices by state (today); proposed `config_revision`, `saf_ok`, `event_log_ok`, `warnings_1h`, `errors_1h` |
| Prometheus | `log_events_total{level,event}` (bounded by the catalogue) next to today's metrics |
| `opencap check` / `/project/validate` | everything detected at load: invalid config, warnings, `bad_config` tags, overload |
| HTTP error bodies | `detail`, plus `{loc,msg,type}` errors for every refused request |
| Web UI | the event log page (LOG-R11), the existing Audit page, per-device `last_error` and `last_reason` |
| MQTT status topics, system tags (#22) | device state, for SCADA alarming without Prometheus |

**LOG-R9 Event log store and retention.**
- The process keeps an in-memory ring buffer of the last N events (default 10 000) at `info`
  and higher. It is mirrored to `data/events.jsonl`, which rotates to `.1` at
  `event_log.max_bytes` (default 16 MiB), as the audit log does.
- Settings: `event_log.path` (empty = memory only) and `event_log.min_level` (default
  `info`).
- Writing is non-blocking. A slow or full disk drops event-log writes (counted), never
  polling work (LOG-38).

**LOG-R10 Event log API.**
- `GET /api/v1/events` takes `min_level`, `source`, `event` (prefix), `channel`, `device`,
  `since`, `until`, `q`, `limit` and `cursor`. It returns entries newest first, each with the
  catalogue's `message`, `cause` and `fix`.
- The viewer role is enough. A live stream (WebSocket or SSE) follows the Quick Client
  transport (#31).
- Both engines serve the same contract (OpenAPI #32).

**LOG-R11 UI event log view.**
- A page with:
  - one row per event: time (local, UTC on hover), severity icon, source, device or channel,
    and message;
  - filters: minimum severity, source, device or channel, time range, free text;
  - pause and resume of live updates;
  - row detail with cause, fix and raw JSON, and a link to the device page;
  - export of the filtered view as JSONL or CSV.
- It works at phone width and needs only the viewer role.
- Its counters agree with `/health` `warnings_1h` and `errors_1h`.

**LOG-R12 Secrets and privacy.**
- No token, password, broker credential or certificate key appears in any log, audit or
  event-log line (NFR-11). Tag values never appear at `info` or higher.
- A test runs the API and MQTT suites with known secret values and greps every captured line
  for them.

**LOG-R13 Tests per event.** Each catalogued event at `info` or higher has at least one test
that triggers it and asserts the name, level and required fields:
- Python: `structlog.testing.capture_logs`, as done today for `device.demoted`;
- Rust: captured JSON stdout or a tracing test layer.

A catalogue-coverage test lists names with no triggering test.

**LOG-R14 Engine parity.**
- For every shared feature, both engines emit the same names, levels and field keys.
- The dual-engine acceptance (#42) runs one scenario per class on both engines and compares
  the sets of `(event, level, field keys)`.
- Known gaps to close: Python lacks `poller_stopping` and `api_started`/`api.started`; Rust
  lacks the audit log until it has the configuration API.

**LOG-R15 Correlation.**
- `config.loaded`, `config_reloaded` and every audit entry carry the config `revision`.
- Events caused by an API call carry `who` and `via`.
- `poller_started` carries the version, engine, instance, revision and driver inventory
  (LOG-01).

**LOG-R16 Library log lines are JSON.** Records from the standard `logging` module (uvicorn,
pymodbus, paho, …) go through structlog's formatter, so every stdout line parses as JSON with
the same keys. A test starts the API and asserts that every captured stdout line is valid
JSON with `event` and `level`.

## Open questions

1. **Log file or not.** D-030 says opencap never writes log files, while 04 §4.5 plans an
   event-log JSONL file. Is the event log a product file (like `audit.jsonl`) and therefore
   allowed? It needs a decision entry.
2. **Tracking issue.** RFC 0001 §7 tracks the event log under #56, while the BOARD backlog
   still lists "event log" under Diagnostics as not filed. Does #56 cover the store, the API
   and the UI, or should a separate issue be filed?
3. **Legacy names.** Rename the underscore names (`poller_started`, `config_reloaded`,
   `reload_failed`, `mqtt_connected`, `saf_dropped_oldest`, `publish_failed`) to dotted form
   with a deprecation window, or freeze them forever?
4. **Rust line format.** Change Rust to write `event` and lower-case levels (recommended: one
   `jq` filter for both engines), or document the difference and teach consumers both?
5. **Text in stdout lines.** Should stdout lines carry the operator `message` and `fix`
   (readable in journald) or only `event` plus fields (smaller lines, text from the
   catalogue)?
6. **Severity of state changes.** Keep `device.offline` and `device.demoted` at INFO (PLAN
   §9), or raise them to WARNING as the manual does for demotion? The UI could also highlight
   them by event class instead of by level.
7. **Rejected writes.** Log rejected config writes (LOG-06) and auth failures (LOG-24) to
   stdout and the event log, while D-043 keeps them out of the audit log? Is the remote
   address acceptable in those lines?
8. **Corrupt outbox.** Move the file aside and start empty (data loss, but visible), or
   refuse to start until an operator acts?

## Checklist rows

| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| LOG-01 | Driver and plug-in loading, component inventory | Adapt | Planned (#49) | #49 | M3 |
| LOG-02 | Project load and validation events (`config.loaded`, `config.warning`, `config.invalid`) | Adapt | Not started | #37 | M3 |
| LOG-03 | Project save and file-name errors (`config.write_failed`) | Adapt | Planned (#135) | #135 | M3 |
| LOG-04 | Project backups and revisions | Adapt | Done | — | M3 |
| LOG-05 | Project file encryption and passwords | Out of scope | Out of scope | — | — |
| LOG-06 | Object add/edit/delete failures (`config.change_rejected`) | Adapt | Planned (#135) | #135 | M3 |
| LOG-07 | Naming rules and renames (`_` reserved for system tags) | Adapt | Planned (#22) | #22 | M5 |
| LOG-08 | Automatic tag generation events | Defer | Planned (#11) | #11 | — |
| LOG-09 | Tag address validation | Adopt | Done | — | M2 |
| LOG-10 | Scaling parameter validation at load | Adapt | Not started | #39 | M2 |
| LOG-11 | Auto-demotion and promotion | Adopt | Done | — | M2 |
| LOG-12 | Data collection enabled/disabled | Adapt | Done | — | M2 |
| LOG-13 | Simulation mode | Defer | Planned (#12) | #12 | — |
| LOG-14 | Device discovery | Defer | Planned (#110) | #110 | M5 |
| LOG-15 | Modem and telephony lines | Out of scope | Out of scope | — | — |
| LOG-16 | Network adapter selection | Defer | Planned (#106) | #106 | M5 |
| LOG-17 | Serial over TCP (Ethernet encapsulation) | Adapt | Done | — | M3 |
| LOG-18 | Serial port errors | Adapt | Done | — | M3 |
| LOG-19 | Socket errors and unresponsive devices | Adopt | Done | — | M2 |
| LOG-20 | Write failures and rejections | Out of scope | Out of scope | #19 | — |
| LOG-21 | Time zone validation | Adapt | Planned (#112) | #112 | M2 |
| LOG-22 | CSV tag import errors | Adapt | Planned (#10) | #10 | — |
| LOG-23 | Alias map import | Out of scope | Out of scope | — | — |
| LOG-24 | Users, permissions, access denials | Adapt | Planned (#21) | #21 | M5 |
| LOG-25 | Configuration sessions and concurrent edits | Adapt | Done | — | M3 |
| LOG-26 | Runtime lifecycle, reload, project replacement | Adopt | Done | — | M2 |
| LOG-27 | Windows service, installation, operating mode | Out of scope | Out of scope | #34 | — |
| LOG-28 | OPC ProgID redirect | Out of scope | Out of scope | #18 | — |
| LOG-29 | Configuration API port and TLS events (`api.started`, `api.bind_failed`) | Adapt | Planned (#135) | #135 | M3 |
| LOG-30 | OPC UA endpoints, certificates, discovery | Adopt | Planned (#74) | #74, #50 | M3 |
| LOG-31 | OPC .NET, OPC HDA, DDE | Out of scope | Out of scope | — | — |
| LOG-32 | OPC AE subscription filters | Defer | Planned (#24) | #24 | M5 |
| LOG-33 | Script engine and profile logs | Out of scope | Out of scope | — | — |
| LOG-34 | North-side platform connection (MQTT link) | Adapt | Done | — | M2 |
| LOG-35 | ThingWorx-specific interface events | Out of scope | Out of scope | — | — |
| LOG-36 | Store-and-forward datastore (disk full, corruption) | Adapt | Planned (#142) | #142 | M2 |
| LOG-37 | Communication diagnostics capture | Adopt | Planned (#56) | #56 | M5 |
| LOG-38 | Event log service health | Adapt | Planned (#56) | #56 | M5 |
| LOG-R1 | Event naming scheme | Adopt | Planned (#134) | #134 | M3 |
| LOG-R2 | Event catalogue with CI check | Adopt | Planned (#134) | #134 | M3 |
| LOG-R3 | Line format and common fields (Rust `event` key, level spelling) | Adopt | Planned (#133) | #133 | M2 |
| LOG-R4 | Severity rules | Adopt | Planned (#134) | #134 | M3 |
| LOG-R5 | Operator message, cause and fix per event | Adopt | Planned (#134) | #134 | M5 |
| LOG-R6 | Stable reason codes | Adopt | Planned (#133) | #133 | M3 |
| LOG-R7 | Flood control | Adopt | Planned (#133) | #133 | M3 |
| LOG-R8 | Surfacing map (`/health` flags, `log_events_total`) | Adopt | Not started | #56 | M5 |
| LOG-R9 | Event log store and retention | Adopt | Planned (#136) | #136 | M5 |
| LOG-R10 | Event log API | Adopt | Planned (#136) | #136, #32 | M5 |
| LOG-R11 | UI event log view | Adopt | Planned (#136) | #136 | M5 |
| LOG-R12 | No secrets in logs (test) | Adopt | Planned (#135) | #135 | M2 |
| LOG-R13 | One test per catalogued event | Adopt | Planned (#134) | #134 | M3 |
| LOG-R14 | Engine parity of events | Adopt | Not started | #42 | M4 |
| LOG-R15 | Correlation fields (`revision`, `who`) | Adopt | Planned (#135) | #135 | M3 |
| LOG-R16 | Library log lines as JSON | Adopt | Planned (#133) | #133 | M1 |
