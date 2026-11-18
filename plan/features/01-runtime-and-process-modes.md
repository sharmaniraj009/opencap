# 01 Runtime and process modes

> Source: KEPServerEX V6 server manual (© PTC), pages 17–22, 30–35, 58–61 and 74–75, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
KEPServerEX is a Windows-only product made of four parts: a runtime that does the device
communication and serves clients, a configuration client that edits the runtime's project
(locally or on a remote machine, several users at once), an administration tray tool, and
an event-log service (manual p. 21). The runtime normally runs as an OS service that starts
with the machine; an alternative "interactive" mode starts it only when a client attaches
and stops it when the last one leaves (p. 22). Install-time choices fix the application-data
folder that holds the running project and certificates, and the manual gives least-privilege
advice for its permissions (pp. 20–21). It lists supported Windows editions and a small
hardware floor (p. 19). Runtime settings cover process priority and CPU affinity (p. 59),
automatic project backups before replacement (p. 60) and a handful of internal TCP ports
(pp. 58, 61, 74–75). The configuration client's own options decide when it connects, what
it confirms and whether it backs up project files (pp. 33–35).

## opencap today
- **Python engine** (`src/opencap/`): one process, `opencap run|check|read|identity|import-kep`
  (`src/opencap/__main__.py`), config directory with `devices.json`, `devices_map.yaml`,
  `overrides.yaml`, `poller.yaml` (ADR-0003, JSON Schema #71), data directory with the outbox
  (`saf.path`, default `data/saf.sqlite`) and the audit log (`api.audit_log`).
- **Service packaging:** hardened systemd unit `deploy/opencap.service` (dedicated user,
  `StateDirectory`, `UMask=0027`, restart on failure, SIGHUP reload), Docker image running as
  uid 10001 (`Dockerfile`), WinSW template `deploy/windows/opencap-service.xml` (not yet tested
  on real Windows, D-031). Deployment guide: `docs/deployment.md`.
- **Config API and web UI** (#63): `/api/v1` with revisions/ETag, atomic multi-file writes with
  one `.bak` generation (D-041), import with dry-run diff (D-042), audit log (D-043), UI at `/ui/`
  with delete/import confirmations and a pending-reload indicator.
- **Rust engine** (`engine/`): `opencap-engine run|check|read|identity`, same config files and
  admin API with the same roles (#84); config conformance with the shared fixtures (#86, #39). Native
  Windows service for Rust is planned (#57); Windows CI is planned (#35).
- **Desktop:** Tauri shell with connection profiles (#87, issue #27, merged); sidecar mode
  is part of the M4 desktop work (RFC 0001 §5, ADR-0010).
- **Not yet:** benchmark-based sizing (#7), revision history with retention (#37), event log
  (#56), per-platform priority/affinity guidance.

## Features

### RT-01 One runtime with pluggable drivers and interfaces (manual p. 18)
- **What:** The product presents itself as a single server with many device drivers and client
  interfaces added as plug-ins, all configured from one user interface.
- **Decision:** Adapt — one engine process per node; southbound drivers behind the `Driver`
  contract (ADR-0004), northbound interfaces as north plugins on the tag store and event bus
  (ADR-0005), one UI over the public API (ADR-0010). Copyleft stacks only as optional
  out-of-process plugins (board decision 5).
- **Status:** Done (Python `src/opencap/drivers/base.py`; Rust `Driver` trait in
  `engine/crates/opencap-core`); Python driver host for the Rust engine Planned (#49).
- **Requirements:**
  - The core imports no protocol library; `opencap check` plans every device without the
    protocol library installed.
  - A device whose driver is not available in the build loads, is reported by `opencap check`
    and the device status with the reason, and is never polled (both engines, D-061).
  - North interfaces read only from the tag store; a stalled north consumer does not change
    poll cycle times (ADR-0005), and skipped updates are counted (`north_lagged_total`).
  - An unported Python driver runs under the Rust engine through the driver host (#49).
- **Acceptance:** existing `check`-without-library test; Rust "not implemented" driver test;
  a stalled-subscriber test with unchanged `poll_cycle_seconds` when the event bus lands (#41);
  the #49 suite with `OPENCAP_ENGINE=rust`.

### RT-02 Supported platforms: Linux and containers (manual p. 19)
- **What:** The manual supports only Windows desktop and server editions, runs the product in
  32-bit compatibility mode on 64-bit Windows, excludes Server Core installs, and points to a
  separate install guide and secure deployment guide.
- **Decision:** Adapt — Linux x86_64 and arm64 (native 64-bit) and a container image are
  first-class targets (FR-13); no 32-bit builds; headless hosts are the normal case.
- **Status:** Done for Linux x86_64 and the Docker image (CI on Python 3.12/3.13, Rust CI);
  arm64 images and deb packages Planned (#34).
- **Requirements:**
  - CI builds and tests both engines on Linux x86_64 on every PR (exists).
  - The release pipeline publishes a multi-arch image (amd64, arm64) and Linux packages (#34).
  - The docs keep a platform table: OS, CPU architecture, Python versions, engine, and whether
    support is CI-tested or field-checked.
  - The engine needs no GUI libraries; it runs on a minimal server OS or in a slim container.
  - A hardening section (the counterpart of the secure deployment guide) stays in
    `docs/deployment.md` and covers accounts, file modes, listeners and secrets.
- **Acceptance:** CI matrix green; arm64 image smoke test (`opencap check` on the sample config)
  in #34; Phase 1 acceptance suite runs inside the slim image.

### RT-03 Supported platforms: Windows x64 (manual p. 19)
- **What:** Same platform list as RT-02, seen from the Windows side.
- **Decision:** Adapt — Windows x64 as a native 64-bit process with the same config files and
  API as Linux. Windows-only COM interfaces live in a separate bridge (see 02-client-interfaces).
- **Status:** Planned (#35 Windows CI, #57 native Windows service for the Rust engine). Today the
  WinSW template exists and the Rust Windows build job is informational.
- **Requirements:**
  - Windows CI runs the Python fast suite and `cargo test` as required checks (#35).
  - Reload on Windows is `POST /reload` (no SIGHUP), documented and tested (D-031).
  - The docs name the supported Windows versions (owner to confirm the oldest one).
- **Acceptance:** required Windows CI job; M4 installer acceptance on a clean Windows 11 VM
  (RFC 0001 §5).

### RT-04 Hardware requirements and footprint (manual p. 19)
- **What:** The manual gives a minimum of a 1 GHz CPU, 1 GB RAM, roughly half a gigabyte of disk
  and a network card, and leaves larger sizing to the vendor's engineers.
- **Decision:** Adapt — publish measured figures rather than a floor: the NFR-2 edge target
  (10,000 tags at 1 s on a 4-core ARM64 box with 1 GB RAM, ≤ 10 % of one core, ≤ 64 MB RSS),
  the NFR-9 ceiling at full scale, and a sizing table from the benchmark.
- **Status:** Planned (#7).
- **Requirements:**
  - The benchmark harness records CPU, RSS and disk use per engine for at least 1k, 10k and
    250k tags and stores the results in the repository.
  - The docs give minimum and recommended hardware per scale band, taken from the latest
    benchmark run.
  - Disk sizing includes the outbox cap (`saf.max_bytes`, default 2 GiB) and the audit log
    rotation (16 MiB plus one rotated file, D-043).
- **Acceptance:** benchmark job output (#7); NFR-2 measured on ARM64 as in RFC 0001 §8 R1.

### RT-05 Application data location and file permissions (manual pp. 20–21)
- **What:** The installer picks a data folder (by default under ProgramData) that holds the
  project and runtime state; anyone who configures the product needs read and write access to
  it; the manual recommends a dedicated group, removing broad inherited read rights, and
  backing up the folder that holds the running configuration and certificates.
- **Decision:** Adapt — two directories: config (project files and `poller.yaml`) and data
  (outbox, audit log, later event log and PKI). The engine runs as a dedicated unprivileged
  account; people change the configuration through the API with tokens (09-administration-and-security),
  not through file permissions.
- **Status:** Done for Linux and Docker (`deploy/opencap.service`, sticky `/etc/opencap`,
  read-only config mode answering 503, D-041); Windows ACLs Planned with the installers (#34, M4).
- **Requirements:**
  - Default paths: Linux `/etc/opencap` and `/var/lib/opencap`; Windows
    `%ProgramData%\opencap\config`, `\data`, `\logs`; container `/opencap/config`, `/opencap/data`.
  - The engine never needs root or Administrator rights; files it creates are not
    world-readable (`UMask=0027`; on Windows only Administrators and the service account).
  - The secrets file (`/etc/opencap/opencap.env`) is not writable by the service account.
  - A read-only config directory is supported: the UI turns editing off and writes answer 503
    naming the path (exists).
  - With project v2, data paths move to `node.yaml` and relative paths resolve against the data
    directory, not the working directory (ADR-0013, #37).
  - The docs list what to back up (config directory, PKI, audit log) and warn not to copy the
    outbox while the engine runs (exists in `docs/deployment.md`).
- **Acceptance:** existing read-only-config tests; a unit test for the modes of created files;
  installer acceptance on clean VMs checks owners and ACLs (RFC 0001 §5).

### RT-06 Project backups and revision history (manual pp. 21, 34, 59–60)
- **What:** The configuration tool can copy a project file before overwriting it, and the
  runtime backs up its project before it is replaced (on by default), keeps the newest N copies
  (1–1000, default 10), can delete them all on request, and names each backup in the event log.
- **Decision:** Adapt — every accepted change keeps the previous revision under
  `config/.revisions/` with a retention count (ADR-0013, 02 §2.4); listing, restore and cleanup
  go through the configuration API (see 10-configuration-api.md) and are audited.
- **Status:** Planned (#37). On main: one `.bak` generation per API write (D-041), full export
  with `GET /api/v1/project?download=true`, revisions listed from the audit log
  (`GET /api/v1/project/revisions`).
- **Requirements:**
  - Before any write that replaces project files (API write, import, restore) the current files
    are stored as one snapshot `config/.revisions/<revision>/`.
  - Retention key (proposed `revisions.keep` in `node.yaml`, range 1–1000, default 10); the
    oldest snapshots go first; cleanup never touches the running config.
  - Clearing all snapshots is admin-only and writes an audit entry.
  - Each audit entry names the snapshot that holds the previous revision.
- **Acceptance:** store unit tests (snapshot before rename, retention, fault injection keeps the
  old snapshot); API tests through `tests/helpers/engine.py` on both engines once Rust has the
  config API.

### RT-07 Components: runtime, configuration clients, administration, event log (manual p. 21)
- **What:** Separate programs for the runtime (a service, reachable locally or remotely), the
  configuration client (several users, remote editing), the administration tray tool, the
  project file and an event-log service that feeds the configuration window. CSV tag
  import/export is a configuration-client feature.
- **Decision:** Adapt — one engine process holds the runtime, the API and (later) the event log;
  configuration clients are the web UI, the desktop app (same bundle, ADR-0010) and the CLI;
  administration is the CLI plus the OS service manager. CSV tag import/export belongs to tag
  management (#10, [07-tag-management-and-system-tags.md](07-tag-management-and-system-tags.md)).
- **Status:** Done (engine, CLI, admin and config API, web UI, #63; Rust API parity #84); desktop
  launcher (#87); event log Planned (#56).
- **Requirements:**
  - Polling never depends on a client: closing every UI or desktop window changes nothing on the
    wire.
  - Every UI action is an `/api/v1` call (unit test over the UI call sites, D-044), so a remote
    node is configured exactly like a local one.
  - Several clients may edit at once; a stale write gets 412 (`If-Match`), never a silent
    overwrite (exists).
  - `GET /health` reports the engine kind (`python` or `rust`) next to `version` and `instance`
    (proposed field).
- **Acceptance:** UI browser smoke test (`tests/ui/browser_smoke.js`); API tests on both engines
  (`tests/integration/test_rust_engine_api.py`); existing 412 tests.

### RT-08 System service mode (manual pp. 22, 59)
- **What:** By default the runtime is an OS service that starts with the machine and needs no
  logged-on user; changing the mode restarts the runtime and drops every client; some native
  interfaces require service mode.
- **Decision:** Adopt — the service is the default and the production mode on every platform
  (ADR-0010 amendment, RFC 0001 §5).
- **Status:** Done for Linux (systemd) and Docker; WinSW template untested (D-031); native
  Windows service for Rust Planned (#57).
- **Requirements:**
  - systemd: start after `network-online.target`, restart 5 s after a failure with no start
    limit, reload on SIGHUP, clean stop on SIGTERM (LWT `offline`, PLC connections closed) (exists).
  - Crash → restart ≤ 5 s and polling again ≤ 10 s (NFR-6).
  - Windows: a Service Control Manager stop runs the same clean stop as SIGTERM; reload is
    `POST /reload`; the service account is configurable (a low-privilege account recommended).
  - A desktop logoff has no effect on the engine.
- **Acceptance:** existing stop and LWT tests; kill-and-restart timing under systemd in the M4
  installer acceptance; Windows service install/stop test in CI (#35, #57).

### RT-09 Foreground and portable modes (manual pp. 22, 59)
- **What:** In "interactive" mode the runtime starts when a client asks for it and stops when all
  clients leave or the user logs off; the manual requires it for desktop-bound interfaces like DDE.
- **Decision:** Adapt — no start-on-client-connect (that is Windows COM activation, and polling
  would stop with the last client). Instead `opencap run` in a terminal for tests and
  commissioning, and the desktop app's sidecar mode for portable and demo installs (ADR-0010).
- **Status:** Done (foreground `opencap run` and `opencap-engine run`); sidecar Planned (desktop
  #27 and #58, M4).
- **Requirements:**
  - `opencap run` needs no service manager; Ctrl+C or SIGTERM gives the same clean stop as the
    service.
  - Sidecar mode uses a private config and data directory, and the engine stops when the app
    exits; it never takes over a node's service configuration.
  - One config directory runs unchanged in foreground, service and sidecar mode.
- **Acceptance:** CLI integration tests on both engines; desktop M4 acceptance (sidecar shows a
  simulator device's values; exiting the app stops the engine).

### RT-10 Process priority and CPU affinity (manual p. 59)
- **What:** The runtime can be raised to high OS priority (which the manual itself discourages)
  and restricted to chosen CPUs on multi-core machines.
- **Decision:** Adapt — left to the OS service manager and documented, not engine settings:
  systemd `Nice=`, `CPUAffinity=`, `CPUQuota=`; Docker `--cpuset-cpus` and `--cpus`; Windows via
  the service wrapper. The Rust runtime sizes its worker threads from the CPUs it may use.
- **Status:** Not started.
- **Requirements:**
  - `docs/deployment.md` explains priority and CPU pinning per platform, with the warning that a
    raised priority can starve other software on the host.
  - The engine never changes its own priority or affinity.
  - Rust: the worker thread count follows the CPUs available to the process (affinity and cgroup
    aware), with an override (proposed `runtime.worker_threads` in `node.yaml`), and is exported
    as a gauge (proposed `engine_worker_threads`).
- **Acceptance:** doc review; a Rust integration test runs the engine pinned to 2 CPUs and reads
  the gauge.

### RT-11 Network listeners and service ports (manual pp. 58, 61, 71, 74–75)
- **What:** KEPServerEX opens several TCP ports: configuration-to-runtime traffic, the event log,
  HTTP and HTTPS for its Configuration API, a local store-and-forward service and an internal key
  service; most are fixed at install and need a runtime restart to change.
- **Decision:** Adapt — one process, so no internal service ports. The only listeners are the
  client-facing ones a node enables: the admin/config API (default `127.0.0.1:8080`), the OPC UA
  server (4840 when enabled), the Modbus TCP server (5020 when enabled), later DNP3/IEC 104. Each
  is explicit config, off unless configured, with defaults ≥ 1024 (NFR-11, NFR-12).
- **Status:** Done for the API (`api.bind`, `api.port`; a non-loopback bind without a token is
  refused, D-019/D-062); other listeners arrive with their interfaces (02-client-interfaces).
- **Requirements:**
  - The sample config opens exactly one listening socket: the API on loopback.
  - A bind failure (port in use, permission) stops the start with an error naming the config key
    and the address.
  - `GET /api/v1/status` lists every active listener: interface, address, port, TLS on or off
    (proposed).
  - The docs keep a table of default ports and say which changes need a restart.
- **Acceptance:** an integration test on both engines lists the process's listening sockets after
  start with the sample config; a port-in-use start test.

### RT-12 Configuration client behaviour (manual pp. 30–35)
- **What:** The Windows configuration tool shows the runtime and project it is attached to, a
  project tree, a detail list, a property editor that marks pending edits until applied and can
  restore defaults, an event-log pane and a status bar. Its options choose auto-connect at start,
  the recent-projects list length, the CSV delimiter, and confirmations before deletes, before
  actions that drop clients, before closing with unsaved changes and before replacing the runtime
  project.
- **Decision:** Adapt — the web UI and desktop app give the same cues the web way; confirmations
  for destructive actions are always on rather than optional; "pending" is the API's
  pending-reload state. Plug-in tree nodes are planned in each plug-in's file; the CSV delimiter
  goes with CSV import (#10, 07-tag-management-and-system-tags.md).
- **Status:** Done in part (#63: delete and import confirmations, dry-run diff before import,
  pending-reload indicator via `GET /api/v1/project/pending`, field-level validation errors);
  defaults/reset and unsaved-edit warning Not started; desktop profiles and auto-connect Done
  (#87); event-log pane Planned (#56).
- **Requirements:**
  - The UI header shows the engine address, instance, version and whether the files differ from
    the running config.
  - Deleting a channel, device, tag or map entry, importing, and reloading require an explicit
    confirmation that names what is affected.
  - Form fields show the schema default (`GET /api/v1/project/schema`) and offer "reset to
    default"; unsaved edits are marked, and leaving the page with unsaved edits asks first.
  - The desktop app stores connection profiles (tokens in the OS keychain) and can open the last
    one at start (#87).
- **Acceptance:** browser smoke test extended per item at desktop and phone width; desktop unit
  tests (#87).

### RT-13 Offline editing and replacing the running project (manual pp. 35, 56, 60)
- **What:** A project can be edited while disconnected and then replace the runtime's project;
  the runtime loads its default project at start, saves it at stop, and backs it up before a
  replacement.
- **Decision:** Adapt — the files are the project. Offline editing is editing a config directory
  (or an export) and running `opencap check`; replacement is `POST /api/v1/project/import` of an
  opencap export (dry run by default, `If-Match` on the real write) or copying files and
  `POST /reload`. The engine never writes the project at start or stop.
- **Status:** Done (#63, D-042).
- **Requirements:**
  - `opencap check --config DIR` validates an offline copy without network traffic (exists).
  - Importing an export restores the files byte for byte after a dry-run diff; the previous files
    are kept (`.bak` today, RT-06 snapshots later).
  - Only API writes and `opencap import-kep` change config files; start, stop and reload do not.
  - An invalid replacement keeps the old config running (exists).
- **Acceptance:** existing import and reload tests (reload on both engines); a test that config
  file contents and mtimes are unchanged across start, reload and stop.

## Open questions
- Hardware guidance before the benchmark (#7) lands: publish the NFR-2 target as provisional, or
  say nothing until measured?
- Windows for the Python engine: keep WinSW as the supported path until the Rust service (#57), or
  mark Python-on-Windows as unsupported until Windows CI (#35) is green?
- Revision snapshots (RT-06): include `poller.yaml` (it holds env var names, no secrets) or only
  the project files? Is a default of 10 snapshots right for small edge boxes?
- Sidecar mode (RT-09): may a sidecar engine poll real PLCs, or should it be limited to
  simulators and demos to avoid unplanned extra connections?
- RT-10: is a `worker_threads` override wanted, or should CPU limits come only from the OS?
- Which Windows versions are the floor (RT-03)? The manual still lists Windows 8.1.

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| RT-01 | One runtime with pluggable drivers and interfaces | Adapt | Done (driver host Planned) | #49 | M3 |
| RT-02 | Platforms: Linux and containers | Adapt | Done (arm64 Planned) | #34 | M1 |
| RT-03 | Platforms: Windows x64 | Adapt | Planned (#35) | #35, #57 | M4 |
| RT-04 | Hardware requirements and footprint | Adapt | Planned (#7) | #7 | M2 |
| RT-05 | Application data location and permissions | Adapt | Done (Windows ACLs Planned) | #34, #37 | M1/M4 |
| RT-06 | Project backups and revision history | Adapt | Planned (#37) | #37 | M3 |
| RT-07 | Components: runtime, clients, administration, event log | Adapt | Done | #63, #84, PR #87, #56 | M1 |
| RT-08 | System service mode | Adopt | Done (Windows service Planned) | #57 | M1/M4 |
| RT-09 | Foreground and portable modes | Adapt | Done (sidecar Planned) | #27, #58 | M4 |
| RT-10 | Process priority and CPU affinity | Adapt | Planned (#144) | #144 | M4 |
| RT-11 | Network listeners and service ports | Adapt | Done | — | M1 |
| RT-12 | Configuration client behaviour | Adapt | Done (partial) | #63, PR #87, #56 | M1/M4 |
| RT-13 | Offline editing and project replacement | Adapt | Done | #63 | M1 |
