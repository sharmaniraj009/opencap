# 14 UI and tools

> Source: KEPServerEX V6 server manual (© PTC), pages 144–164 ("Designing a Project", and "Allow Desktop Interactions" from the "How Do I…" list), summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
The manual walks through building a first project with the Simulator driver in the Windows
configuration tool (manual p. 144–163). The runtime normally runs as a service. It can also run
as a desktop application that an OPC client starts on demand. The configuration window opens from
the desktop icon or the system-tray Administration menu (manual p. 144). A **channel wizard**
picks the driver, names the channel and walks through its properties to a summary. A **device
wizard** asks for name, model, ID, scan mode and driver-specific pages (manual p. 145–149). Tags
are added in a properties dialog with name, address, description, data type, client access and
scan rate, plus driver "hints" and clear error messages (manual p. 149–150). A **tag browser**
picks tags from a tree, optionally with system groups and whole branches (manual p. 151–152). A
**multiple tag generation** tool builds many tags from static text, numeric ranges and text
sequences, with a preview (manual p. 152–154). **Scaling** is set per tag (manual p. 155). The
project is then saved (manual p. 155–158) and tested with the bundled **OPC Quick Client**, which
connects over COM/DCOM, groups items with update rate, deadband and active state, browses and adds
items, shows values, and writes values synchronously or asynchronously (manual p. 158–163).
Desktop interaction is needed only for DDE-type interfaces (manual p. 164).

## opencap today
- **Engine as a service:** systemd unit `deploy/opencap.service`, WinSW definition in
  `deploy/windows/`, Docker image (D-030, D-031). A native Windows service for the Rust engine is
  #57.
- **Web UI** at `/ui/` (#63, ADR-0010, D-044): static HTML/CSS/JS with vendored Alpine.js CSP build,
  calling only `/api/v1`, token in `sessionStorage`, phone-width layout. Pages: login, dashboard,
  device page (live values and quality, 2 s auto-refresh, identity, block plan, **Read once**),
  configuration (channels, *Add channel*, publish defaults, Export, Reload), channel page (devices,
  *Add a device*: name, driver, host, port, params JSON), device editor (all device fields, tag
  table with inline edit, *Add a tag*, `scale` as JSON), check (block plan and cycle estimates),
  import (KEPServerEX `project.json` or opencap export, dry-run diff, apply), and audit log with
  filters. It never writes a PLC value. Browser smoke test: `tests/ui/browser_smoke.js` via
  `tests/integration/test_ui_browser.py`.
- **Desktop app** (#87 for #27, merged; Tauri 2): launcher with connection profiles for local and
  remote engines, health check, tokens in the OS keychain, and the engine's `/ui/` in a native
  window. No sidecar, signing or auto-update yet. Installers are #58.
- **CLI:** `opencap import-kep`, `check`, `read`, `identity`, `run`.
- **Planned:** Quick Client over WebSocket (#31), OpenAPI contract (#32), import wizard (#33),
  screen and interaction spec for KEPServerEX-parity screens (#26), simulator driver for demos and
  UI tests (#12), Logix browse (#11), KEPServerEX CSV tags (#10).

## Features

### UI-01 Run the engine as a service, with the UI as a client (manual p. 144)
- **What:** The runtime runs as a service, or as a desktop process that a client starts. The
  configuration tool is started by hand.
- **Decision:** Adapt: the engine always runs as an OS service or container, and the UI and
  desktop app are clients of its API (ADR-0010 as amended, RFC 0001 §5). No COM activation by
  clients. Plan 01 covers process modes.
- **Status:** Done
- **Requirements:** The systemd unit and the Windows service definition start the engine at boot,
  restart it on failure, and reload on SIGHUP (Linux) or `POST /reload`. Polling continues with no
  UI open.
- **Acceptance:** `tests/unit/test_windows_compat.py`. The M4 acceptance in RFC 0001 §5 (clean
  VMs, survives a service restart) for the Rust services (#57).

### UI-02 Desktop app with connection profiles (manual p. 144)
- **What:** The configuration tool opens from the desktop or system tray, and can connect to a
  remote runtime.
- **Decision:** Adapt: a Tauri 2 shell (ADR-0010) keeps profiles for local and remote engines,
  checks their health, keeps tokens in the OS keychain, and opens the engine's own `/ui/`.
- **Status:** Done (#87); the engine sidecar is planned (#58)
- **Requirements:**
  - Profiles hold name and engine URL only, and tokens live only in the keychain.
  - The engine window cannot navigate to another origin, and gets no Tauri IPC.
  - It works against both engines (the Rust engine has no config API yet; the launcher falls back
    to `/channels`).
  - Linux AppImage/deb and Windows MSI (#58).
- **Acceptance:** `cargo test` in `desktop/` (profile storage, engine checks against a fake
  server). RFC 0001 §5 acceptance on clean Ubuntu LTS and Windows 11 VMs (M4).

### UI-03 Navigate the project: channels → devices → tags (manual p. 145–149)
- **What:** A project tree, with the Connectivity node, channels and devices, plus a tag list.
- **Decision:** Adapt: linked pages with breadcrumbs (Configuration → channel → device editor),
  using hash routes so the same files work in a browser and in the desktop app.
- **Status:** Done
- **Requirements:** From the configuration page, every channel, device and tag is reachable in at
  most two clicks. Counts of channels, devices, tags and excluded devices are shown, and so are
  the overrides that apply.
- **Acceptance:** UI smoke test at desktop and phone width (`tests/ui/browser_smoke.js`).

### UI-04 New-channel wizard (manual p. 145–147)
- **What:** Choose a driver, name the channel, set channel properties (e.g. serial settings), see
  a summary, and finish or go back.
- **Decision:** Adapt: in opencap the driver belongs to the device, not the channel. The wizard
  therefore creates a channel together with its first device: name, driver, transport (for serial
  drivers: gateway, RFC 2217 or local port), driver parameters from the driver's params schema
  (`schema/drivers/<driver>.schema.json`, ADR-0003), and a summary with the JSON to be written and
  the `check` estimate.
- **Status:** Planned (#26)
- **Requirements:**
  - Steps are name → driver → connection → parameters → summary. Back keeps the entered values,
    and nothing is written before Finish.
  - Parameter fields come from the driver schema, with defaults filled in. Unknown keys cannot be
    entered. The per-driver schemas are not in `schema/` yet; generating them from
    `docs/driver_params.md` and the driver models is part of this work.
  - Finish sends one `POST /api/v1/channels` with the device (`If-Match`). Errors are shown at the
    step that owns the field.
- **Acceptance:** UI smoke test with the simulator driver (#12): the wizard creates a channel and
  device, `check` shows its plan, and Reload starts polling.

### UI-05 New-device wizard (manual p. 147–149)
- **What:** Add a device to a channel: name, model, ID, scan mode, timing and driver pages, then a
  summary. The manual notes that the device is live before the project is saved.
- **Decision:** Adapt: a wizard on the channel page with the same steps (identity, driver
  parameters, scan settings `scan_ms`/`min_scan_ms`, timing `timeout_ms`/`retries`, demotion),
  and an optional **Test** step that reads the device's identity once, read-only. In opencap a new
  device is saved to the files but polled only after Reload, which the UI states.
- **Status:** Planned (#26)
- **Requirements:**
  - The Test step uses a new admin route, `POST /api/v1/devices/test` with an unsaved device. It
    connects once, asks for identity (or reads one block), closes, and is rate-limited per host.
  - The summary shows the block plan and cycle estimate from `GET /api/v1/project/validate` on the
    staged change.
  - After Finish, a "pending reload" banner names the channel that will restart.
- **Acceptance:** UI smoke test against a simulator. API test: the test route sends only read
  services (the read-only guard tests of each driver).

### UI-06 Add and edit user-defined tags (manual p. 149–150)
- **What:** A tag dialog with name, address, description, data type, client access and scan rate.
  Apply commits the tag, and New starts the next one.
- **Decision:** Adapt: an inline tag table with *Add a tag* in the device editor. Client access is
  always read-only, so it is not a field (FR-2, ADR-0008). Validation errors appear next to the
  field (PRJ-05).
- **Status:** Done
- **Requirements:**
  - Add, edit, rename, disable and delete tags. `POST …/tags` accepts one tag or a list.
  - A duplicate name, an address the driver cannot plan, or a type the address cannot have is
    refused with a field error (422).
  - Each saved row shows "saved". A tag override is shown as a badge.
- **Acceptance:** `tests/unit/test_config_api.py` and `tests/unit/test_ui.py` (every UI call site
  uses `/api/v1`). Browser smoke test.

### UI-07 Address help per driver (manual p. 149–150)
- **What:** Driver "hints" and an address help page tell the user what a valid address and data
  type look like.
- **Decision:** Adapt: an address help panel per driver in the tag editor. It is generated from
  each driver's address grammar (documented in `docs/driver_params.md` and the driver modules),
  and has a "check address" action that runs the driver's `plan()` without saving.
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/drivers/{driver}/address-help` returns syntax forms, examples (192.0.2.x where
    hosts appear), and the default dtype per address area.
  - `POST /api/v1/devices/{id}/tags/validate` plans a draft tag and returns `ok` or the driver's
    reason. It writes nothing.
  - The UI shows the help next to the address field and checks the address as the user types
    (debounced).
- **Acceptance:** API tests for every implemented driver (each example in the help must plan).
  UI smoke test.

### UI-08 Pick tags from the project (manual p. 151–152)
- **What:** A tag browser over the server's tag tree, with an option to show system and internal
  groups, branch-level selection, and multi-select.
- **Decision:** Adapt: a reusable tag picker in the web UI over the project's address space, for
  every feature that references tags (Quick Client watch lists, derived tags, alarms, agent tag
  selection). System tags appear when "include system tags" is on (plan 07).
- **Status:** Not started
- **Requirements:**
  - The tree has channel → device → groups → tags, with search, Shift/Ctrl multi-select and
    selection of a whole branch.
  - `GET /api/v1/browse?path=&depth=&include_system=` returns one level at a time and handles a
    250k-tag project (NFR-1) without loading it all.
- **Acceptance:** API test on a generated 250k-tag config (response time bound). UI smoke test of
  the picker.

### UI-09 Browse a device and add its tags (manual p. 151–152)
- **What:** For drivers that support it, browse the device's own tags and add the selected ones
  to the project.
- **Decision:** Adapt: `Driver.browse()` plus `opencap browse` (#11, starting with `ab_logix`). The
  UI then lists the device's symbols with the tag picker and adds the chosen ones through
  `POST …/tags` (FR-2 automatic tag generation).
- **Status:** Planned (#11)
- **Requirements:**
  - Browse is read-only and uses the device's existing connection rules (no extra connection on
    devices with few connections).
  - Tags the device offers but the project lacks are marked as new, and existing ones are marked
    as present.
  - Adding goes through the normal validation (PRJ-05).
- **Acceptance:** CIP simulator test (`cip_sim.py`) for browse. UI smoke test that adds three
  browsed tags.

### UI-10 Generate many tags from a pattern (manual p. 152–154)
- **What:** Build tag names and addresses from static text, numeric ranges (base, start, end,
  step) and text lists. A preview marks valid and invalid tags, names and start values can be
  edited, and the result can be added as a group.
- **Decision:** Adapt: a generator dialog in the device editor, over the generator of TAG-09 in
  plan 06: its template format, `opencap tags generate` and
  `POST /api/v1/devices/{id}/tags:generate?dry_run=true`. This row is the UI flow only. CSV import
  (#10) is the bulk alternative.
- **Status:** Not started
- **Requirements:**
  - The dialog edits a TAG-09 template: name prefix and counter, address parts (static text,
    numeric range with base and step, text sequence), dtype, scan rate and optional group.
  - **Preview** calls the dry run and lists every tag with ok, or the driver's reason. Duplicate
    names are marked. Names and start values can be changed before generating.
  - **Generate** applies the same template (`dry_run=false`, `If-Match`) and adds only valid tags,
    in one audited change. Afterwards the tag table shows the new tags.
- **Acceptance:** UI smoke test with the simulator driver (#12): preview shows invalid addresses,
  and generate adds exactly the valid ones. Generator vectors and API tests are TAG-09's.

### UI-11 Tag scaling in the editor (manual p. 155)
- **What:** Set linear or square-root scaling with raw and scaled ranges, clamps, scaled data type
  and units.
- **Decision:** Adapt: `scale` is edited as JSON in the tag row, checked by the `Scale` model, and
  shown as text in the table. The scaled data type is dropped today (engineering values are floats,
  D-010). TAG-11 in plan 06 proposes `scale.dtype`. Scaling behaviour itself is TAG-10.
- **Status:** Done
- **Requirements:** `type` (`linear`/`sqrt`), `raw_lo`, `raw_hi`, `eng_lo`, `eng_hi`, `clamp_lo`,
  `clamp_hi`, `negate` and `units` are accepted and validated. A raw span of zero is refused.
- **Acceptance:** `tests/unit/test_models.py` (Scale) and the config API tests. Scaling vectors (#3).

### UI-12 Scaling form with a live preview (manual p. 155)
- **What:** A guided scaling dialog instead of free text.
- **Decision:** Adapt: a structured form (type, ranges, clamps, negate, units) with a raw → eng
  preview. It uses the same arithmetic as the engine, so what the user sees is what is published.
- **Status:** Not started
- **Requirements:**
  - The form fields map one-to-one to `scale` keys, and an empty form removes `scale`.
  - The preview evaluates three raw values (low, mid, high) with the scaling vectors' rules. The
    UI's arithmetic is tested against those vectors.
- **Acceptance:** A JS unit test runs the scaling vectors through the preview function (#3). UI
  smoke test.

### UI-13 Save, apply and export from the UI (manual p. 155–157)
- **What:** Save or Save As, choose whether the new project becomes the runtime project, and open
  saved projects.
- **Decision:** Adapt: edits save on submit, a pending-reload banner and **Reload** apply them,
  **Export** downloads the whole config, and importing an export restores it (PRJ-06, PRJ-11).
  Encrypted projects are not used (PRJ-08, PRJ-09).
- **Status:** Done
- **Requirements:**
  - After any accepted change, the UI shows what a reload would restart. Reload reports
    started, restarted, stopped and unchanged channels.
  - Editing is off, with the reason shown, when the token is a viewer or the config directory is
    read-only.
- **Acceptance:** Browser smoke test and `tests/unit/test_config_api.py`.

### UI-14 KEPServerEX import wizard (manual p. 144–145)
- **What:** Start a project from an existing one.
- **Decision:** Adapt: the import page (dry-run diff, apply) is done in #63 (PRJ-07). The wizard
  (#33) adds an upload → report → confirm flow: the import summary's problems, stub SubDeviceIds to
  confirm per device, and serial COM ports to map. No YAML is edited by hand.
- **Status:** Planned (#33)
- **Requirements:**
  - Step 1: upload. Step 2: report (counts, excluded devices and why, type conversions, disabled
    tags, credentials to provide as env vars). Step 3: confirm SubDeviceIds and map COM ports
    (`serial_ports`). Step 4: apply and reload.
  - Each step can be left and resumed, and nothing is written before apply (`If-Match` of the dry
    run).
- **Acceptance:** UI smoke test with a synthetic export (192.0.2.x). API tests of the dry-run →
  apply race (412).

### UI-15 Live values and read once (manual p. 158–162)
- **What:** Test reads: see each tag's value and quality change.
- **Decision:** Adapt: the device page shows every tag's value, quality and source time,
  refreshed every 2 s, and **Read once** (admin) runs one full read now, serialized with polling.
- **Status:** Done
- **Requirements:**
  - `GET /api/v1/devices/{id}/values` is the source. Quality uses the five opencap qualities
    (ADR-0006).
  - Read once is admin-only, because it costs a full extra cycle on the wire (D-040).
- **Acceptance:** Config API tests and the browser smoke test. The Rust engine serves
  `/devices/{id}/values` and `read-once` (RFC 0001 §7).

### UI-16 Quick Client: subscriptions and watch lists (manual p. 158–162)
- **What:** Connect to the server, make groups with update rate, deadband and active state, add
  items by browsing, and watch values with quality, timestamps and error feedback.
- **Decision:** Adapt: a Quick Client page over the WebSocket feed `/api/v1/ws` (03 §3.6, #31).
  Watch lists stand in for groups, with an update rate (never faster than the tag's scan), an
  absolute deadband and pause/resume. Items come from the tag picker (UI-08), and both timestamps
  are shown (ADR-0006). ADM-05 in plan 09 is the same Quick Client, seen from the administration
  menu.
- **Status:** Planned (#31)
- **Requirements:**
  - The WebSocket subscribes to a device or a tag list and receives change events (value,
    quality, `source_ts`, `server_ts`). It is authenticated with the bearer token, and viewers are
    allowed.
  - Watch lists can be saved per browser and exported as JSON. A demand read button is available
    (admin).
  - A bad quality shows the device's last error and the ADR-0006 code that north protocols would
    report.
  - It works against both engines (RFC 0001 §7 "Quick Client": M3).
- **Acceptance:** WebSocket API tests on both engines. UI smoke test with a changing simulator
  value (#12).

### UI-17 Write values from the test client (manual p. 162–163)
- **What:** Synchronous and asynchronous writes to tags from the Quick Client.
- **Decision:** Out of scope: no PLC writes (ADR-0008). Writes wait for RFC 0002 (#19) and the
  owner's approval.
- **Status:** Out of scope

### UI-18 Test through a real north protocol (manual p. 158–159)
- **What:** The Quick Client tests the server over the same OPC interface SCADA uses, locally or
  remotely.
- **Decision:** Adapt: OSS OPC UA clients (Eclipse Milo, open62541, asyncua) test the OPC UA
  server in CI (L1, #29). The recipe pages tell engineers which free client to use for a manual
  check. Performance testing is the benchmark harness (#7), not a UI tool.
- **Status:** Planned (#29)
- **Requirements:**
  - The L1 suite browses, reads and subscribes to every tag of a simulator project and compares
    the values with the API.
  - `docs/scada/` names one OSS client and the steps to connect it (#17).
- **Acceptance:** The L1 suite green in CI (NFR-15).

### UI-19 Desktop interaction for DDE-type interfaces (manual p. 164)
- **What:** Services cannot reach the user's desktop, so DDE needs the runtime in interactive
  mode.
- **Decision:** Out of scope: DDE, FastDDE and SuiteLink are not offered (ADR-0009), and the engine
  never needs a desktop session. The desktop app is a separate client over loopback.
- **Status:** Out of scope

### UI-20 Building client applications (manual p. 163)
- **What:** The manual points to Visual Basic sample projects for writing OPC client applications.
- **Decision:** Adapt: a committed OpenAPI contract (#32) and documented examples (curl and Python,
  as in the README) for the REST and WebSocket API. OPC UA client development uses the standard
  OPC UA SDKs. There are no COM samples.
- **Status:** Planned (#32)
- **Requirements:** The OpenAPI document is generated, committed and drift-tested, and both engines
  are tested against it. README examples are smoke-tested in CI.
- **Acceptance:** OpenAPI drift test (#32), and API tests run against the document on both engines.

## Open questions
- **Wizards (UI-04/05):** should the channel wizard create the first device every time (opencap's
  channel has no driver), or should a channel be creatable empty, as today's form allows?
- **Test step (UI-05):** a new device's connection test sends traffic to a PLC before the device is
  applied. Is that acceptable (read-only, rate-limited), or should testing wait until after Reload?
- **Simulator driver (#12):** the manual's walkthrough relies on a simulator. Should #12 be raised
  in priority, since UI-04/05/10/16 acceptance tests need it?
- **Tag generator (UI-10):** should it be built, or is CSV import/export (#10) plus device browse
  (#11) enough for real migrations?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| UI-01 | Engine as a service, UI as a client | Adapt | Done | D-030, D-031, #57 | M1 |
| UI-02 | Desktop app with connection profiles | Adapt | Done (#87); sidecar Planned (#58) | #87, #27, #58 | M4 |
| UI-03 | Navigate channels → devices → tags | Adapt | Done | #63 | M1 |
| UI-04 | New-channel wizard | Adapt | Planned (#26) | #26, #12 | — |
| UI-05 | New-device wizard with a test step | Adapt | Planned (#26) | #26, #12 | — |
| UI-06 | Add and edit user-defined tags | Adapt | Done | #63 | M1 |
| UI-07 | Address help per driver | Adapt | Planned (#139) | #139 | — |
| UI-08 | Pick tags from the project (tag picker) | Adapt | Planned (#139) | #139 | — |
| UI-09 | Browse a device and add its tags | Adapt | Planned (#11) | #11 | — |
| UI-10 | Generate many tags from a pattern (dialog) | Adapt | Not started | TAG-09, #12 | — |
| UI-11 | Tag scaling in the editor (JSON) | Adapt | Done | #63 | M1 |
| UI-12 | Scaling form with a live preview | Adapt | Not started | #3 | — |
| UI-13 | Save, apply and export from the UI | Adapt | Done | #63 | M1 |
| UI-14 | KEPServerEX import wizard | Adapt | Planned (#33) | #33 | — |
| UI-15 | Live values and read once | Adapt | Done | #63 | M1 |
| UI-16 | Quick Client: subscriptions and watch lists | Adapt | Planned (#31) | #31 | M3 |
| UI-17 | Write values from the test client | Out of scope | Out of scope | ADR-0008, #19 | — |
| UI-18 | Test through a real north protocol | Adapt | Planned (#29) | #29, #17 | M3 |
| UI-19 | Desktop interaction for DDE-type interfaces | Out of scope | Out of scope | ADR-0009 | — |
| UI-20 | Building client applications (OpenAPI) | Adapt | Planned (#32) | #32 | M3 |
