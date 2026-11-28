# Web UI and desktop app: screens and interactions

The reference for what opencap's web UI shows and does on each KEPServerEX-parity screen (#26,
ADR-0010). The desktop app (`desktop/`) opens the same bundle from each engine, so everything here
applies to it as well. Only the remote-node switcher (screen 13) lives in the desktop launcher.

Use this file to keep UI issues small: an issue names a screen and the rows it builds, and the
section says what "done" looks like. Feature IDs (`UI-16`, `DEV-06`, …) refer to the KEPServerEX
parity plan in [`plan/features/`](../../plan/features/). Issue numbers say where the work is
tracked. Wireframes are ASCII, at desktop width, and show content and order rather than final
styling.

**Status of this document:** a specification. Screens marked *Done* describe what is on `main`;
the others are the target for the issues named.

## Contents

- [0. Rules every screen follows](#0-rules-every-screen-follows)
- [Screen index](#screen-index)
- Screens:
  [1 Project tree](#1-project-tree) ·
  [2 Channel editor and new-channel wizard](#2-channel-editor-and-new-channel-wizard) ·
  [3 Device editor and new-device wizard](#3-device-editor-and-new-device-wizard) ·
  [4 Tag table](#4-tag-table) ·
  [5 Quick Client](#5-quick-client) ·
  [6 Device diagnostics](#6-device-diagnostics) ·
  [7 North agents](#7-north-agents) ·
  [8 Event log](#8-event-log) ·
  [9 Import wizard](#9-import-wizard) ·
  [10 Users and tokens](#10-users-and-tokens) ·
  [11 Certificates](#11-certificates) ·
  [12 Node and redundancy status](#12-node-and-redundancy-status) ·
  [13 Remote-node switcher (desktop)](#13-remote-node-switcher-desktop) ·
  [14 Settings and revisions](#14-settings-and-revisions)
- [Accessibility and theming](#accessibility-and-theming)
- [Missing API endpoints](#missing-api-endpoints)

## 0. Rules every screen follows

### Architecture
- The UI is the static bundle under `/ui/`: plain HTML, CSS and JS with the vendored Alpine.js CSP
  build, and no build step (ADR-0010, D-044). It runs under `script-src 'self'`, with no inline
  script and no eval.
- It talks only to the public API: REST under `/api/v1` plus the live-values WebSocket `/api/v1/ws`
  (D-067). `tests/unit/test_ui.py` checks every call site. A screen needing data the API lacks gets
  an endpoint in the contract first ([missing endpoints](#missing-api-endpoints)), never a private
  route.
- Hash routes (`#/<screen>/<arg>`), so the same files work from any origin and inside the desktop
  window. An argument that is a device id keeps its `/` (`#/edit/SITE%20MODBUS/Device1`).
- The bearer token stays in the tab's `sessionStorage`. The desktop app writes it there for the
  engine's own origin. There are no cookies.
- **No visible text is written into the markup or the code.** Every string comes from the table of
  the user's language, `src/opencap/api/ui/i18n/<lang>.json` (English is `en.json`; #235):
  `t('key')` and `rich('key')` in `index.html`, `tr('key', {vars})` in `app.js`. `tests/unit/test_ui.py`
  fails on text outside the table, on a key the code uses that the table lacks, and on a key nothing
  uses. A new screen adds its keys under its own prefix (`quick.*`, `import.*`, `events.*`).

### Read-only toward PLCs (ADR-0008)
No screen has a control that writes a value to a PLC: no value input in the Quick Client, no "set
value", and no writable system tags (UI-17, TAG-06, SYS-09 are out of scope). The footer says so
on every page. The only actions that reach a PLC are reads: **Read now**/**Read once**, identity,
a device test (UI-05) and device discovery (CH-16). They are rate-limited and role-gated.

### Roles
Today the API has two roles, given by named tokens in `poller.yaml` (D-040): `viewer` may use
safe methods (GET and the WebSocket), `admin` may do everything else. RFC 0005 (#21) adds users
and the roles below. Until it lands, every right listed for operator, engineer or admin needs
`admin`.

| Capability | viewer | operator | engineer | admin |
|---|:-:|:-:|:-:|:-:|
| See status, configuration, values, Quick Client, diagnostics, events, audit | ✓ | ✓ | ✓ | ✓ |
| Demand read (read now/once), refresh identity, start a frame capture | | ✓ | ✓ | ✓ |
| Edit the configuration, import, reload, test a device, discover devices | | | ✓ | ✓ |
| Purge an outbox, change forward mode, clear the event log | | | | ✓ |
| Users and tokens, certificates, node settings | | | | ✓ |

This table is the UI's proposal to RFC 0005, which owns the final split. Each screen below lists
the lowest role that can use it. A control the role may not use is hidden, and the reason is shown
once per page in the banner ("Your role is viewer: changes need an engineer token").

### Writes and conflicts
- Every configuration write sends `If-Match: <revision>` from the last read (03 §3.6). On 412 the
  form keeps the user's input and says "Someone changed the configuration since you loaded it.
  Load it again, then redo your change." On 428 (a bug in the UI) the same message appears.
- 422 answers list `{loc, msg}`. The message appears next to the field named by the first `loc`
  element, and the rest above the form (PRJ-05).
- A write changes the files only. The **pending-reload banner** names what a reload would start,
  restart or stop, and offers **Reload now** (engineer). A screen may offer "save and reload" with
  `?apply=true` instead (D-039).
- When the config directory is read-only, every edit control is off and a banner names the path
  (`config_writable_reason`, RT-05).

### Shared states
- **Loading:** a muted "loading…" line where the content goes, after 300 ms (no spinner flash).
- **Empty:** a sentence that says what to do next, with a link ("No channels yet: add one or import
  a KEPServerEX project").
- **401:** back to the login page, token forgotten. **403:** the reason from the answer, inline.
  **404:** "not found or no longer there" with a link up one level. **503:** the reason (no config
  directory, or the config directory is read-only).
- **Not polled:** a device that is configured but not polled (channel disabled, not reloaded, not
  selected with `--site`) says so in every screen that shows runtime data.
- **Polling cadence:** REST polling runs only while the tab is visible: dashboard 5 s, device page
  2 s, pending banner 30 s. Live values come from the WebSocket.

### The shell

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ opencap  Dashboard  Quick Client  Config  Import  Events  Audit  Admin   alice · admin  Log out │
├──────────────────────────────────────────────────────────────────────────────┤
│ [info]  No API token is configured: read-only, localhost only.        (open mode)│
│ [warn]  Changes not applied yet: SITE MODBUS changes.          [Reload now]   │
│ [ok]    Reloaded: restarted SITE MODBUS                                   [×] │
├──────────────────────────────────────────────────────────────────────────────┤
│                               screen content                                 │
├──────────────────────────────────────────────────────────────────────────────┤
│ read-only towards PLCs: this UI never writes a PLC value · licences          │
└──────────────────────────────────────────────────────────────────────────────┘
```

*Events* and *Admin* appear when their screens exist (#136, #21). At phone width the navigation
wraps and tables become stacked cards.

## Screen index

| # | Screen | Route | Status | Work | Plan |
|---|---|---|---|---|---|
| — | Login, Dashboard, Check, Audit | `#/login`, `#/dashboard`, `#/check`, `#/audit` | Done | #63 | ADM-18, PRJ-14, ADM-21 |
| 1 | Project tree | `#/tree` (also the landing page), `#/config` (`#/channel/<c>`) | Read-only tree and properties panel on `#/tree` (D-141, D-144, screenshots in `docs/ui/screenshots/`): channels, devices, tag groups from dotted names, configured tags with address, type and scan settings, a read-only tag table; the editors wait for the write routes | #103, #117 | UI-03, CH-01, TAG-14/15 |
| 2 | Channel editor, new-channel wizard | `#/channel/<c>`, `#/new/channel` | Editor partial; wizard not started | #103, #104, #105, #129 | UI-04, CH-01/02/05/14 |
| 3 | Device editor, new-device wizard | `#/edit/<id>`, `#/new/device/<c>` | Editor done (params as JSON); wizard not started | #129, #105, #113 | UI-05, DEV-01…18 |
| 4 | Tag table | `#/edit/<id>` (tags section) | Inline edit done; bulk, CSV, generator not started | #10, #117, #139, #115 | UI-06/07/10/12, TAG-*, SYS-01/02 |
| 5 | Quick Client | `#/quick/<id>` | Done (#151) | #164 | UI-15/16, ADM-05 |
| 6 | Device diagnostics | `#/device/<id>` | Partial: status, plan, identity | #138, #130, #133 | DIAG-01/10/11/12 |
| 7 | North agents | `#/north`, `#/north/<kind>/<name>` | Not started (dashboard cards only) | #132, #143, #137 | SAF-*, ADM-06, DIAG-05/06 |
| 8 | Event log | `#/events` | Not started | #136 | DIAG-03/04, LOG-R10/R11 |
| 9 | Import wizard | `#/import` | Done (#33) | #108, #37 | UI-14, PRJ-07 |
| 10 | Users and tokens | `#/admin/users` | Not started | #21 | ADM-13…16, API-26 |
| 11 | Certificates | `#/admin/certificates` | Not started | (to file) | ADM-22, ADM-06 |
| 12 | Node and redundancy status | `#/node` | Not started (dashboard instance card) | #123, #20 | ADM-09, RT-07/12 |
| 13 | Remote-node switcher | desktop launcher | Done (#87) | #58 | UI-02, RT-12 |
| 14 | Settings and revisions | `#/admin/settings`, `#/admin/revisions` | Not started | #132, #37 | API-20, RT-06, PRJ-12 |

---

## 1. Project tree

**Purpose.** KEPServerEX's left pane: the whole project as a tree (channels → devices → tag groups →
tags), with counts, so the user always knows where they are and can jump anywhere in two clicks
(UI-03).

**Plan.** UI-03, CH-01, DEV-01, DEV-05, CH-03 (#103), TAG-14 (groups from dotted names, done),
TAG-15/API-24 (group objects, #117), PRJ-13 (naming rules, #112), SYS-03 (`_System` branch, #22),
RT-12 (pending indicator).

**Data shown.**
- Channels: name, enabled, override badge, driver summary (#103), device count, tag count (#103).
- Devices: name, driver, state dot (ok, offline, demoted, not polled), tag count, stub-map badge.
- Groups: the dotted prefix of tag names (`LTI002.Pv` → group `LTI002`), with tag count.
- Tags: name, dtype, enabled. Clicking a tag opens the tag table filtered to it.
- Totals at the top: channels, devices, tags, devices excluded by the last import.

**Actions.** Expand or collapse; filter by name (matches open their parents); add a channel
(wizard, screen 2); add a device to the selected channel (wizard, screen 3); rename or delete a
channel, device or group (#117), with a confirmation that names what goes with it (devices, map
entries, overrides); open the Quick Client on a device or group. Context actions are buttons on
the selected row, not a right-click menu (keyboard and touch).

**API.**
- ✓ `GET /api/v1/channels`, `GET /api/v1/channels/{c}/devices`, `GET /api/v1/devices/{id}/tags`,
  `GET /api/v1/status` (states), `GET /api/v1/project/status` (counts).
- ✓ `POST/PATCH/DELETE` on channels and devices.
- ✗ `GET /api/v1/browse?path=&depth=1&include_system=` (one level at a time, for 250k-tag projects;
  UI-08, #139). Until it exists, the tree loads the device's tags only when the device is opened.
- ✗ Group routes `GET /devices/{id}/groups`, `PATCH/DELETE …/groups/{path}` (API-24, #117).
- ✗ `tags` and `tags_enabled` on channels, `description` and `drivers` on channels (#103).

**Roles.** viewer (browse); engineer (add, rename, delete).

**Empty and error states.** No channels: "No channels yet: add one or import a KEPServerEX
project." A device with no tags: "No tags: add tags, import a CSV or browse the device." Config
invalid on disk: the tree shows the last valid state with an error banner ("devices.json does not
load: …; a reload would keep the running config").

```text
┌ Project ─────────────────────────┐┌ SITE MODBUS / Device1 ─────────────────────────┐
│ [Filter…                       ] ││ modbus_tcp · 192.0.2.10:502 · 9 tags · ● ok   │
│ 9 channels · 10 devices · 37 tags││ [Edit] [Quick Client] [Diagnostics] [Delete]  │
│ ▾ SITE MODBUS          1 dev  9 t││                                               │
│   ▾ ● Device1 modbus_tcp     9 t ││ (the selected node's editor or summary)       │
│       ▸ PUMP_1           4 t     ││                                               │
│         FLOW_1   float32         ││                                               │
│ ▸ SITE PCCC            1 dev  5 t││                                               │
│ ▸ SITE LOGIX  (override: off)    ││                                               │
│ [+ Channel] [+ Device]           ││                                               │
└──────────────────────────────────┘└───────────────────────────────────────────────┘
```

At phone width the tree is a full-width list above the content, collapsed to channels.

## 2. Channel editor and new-channel wizard

**Purpose.** Edit a channel's own settings, and create a channel together with its first device.
In opencap the driver belongs to the device, so the wizard always creates the first device too
(UI-04; the plan's open question, answered here: an empty channel can still be made from the tree,
but the wizard is the guided path).

**Plan.** CH-01, CH-02 (description, driver summary, #103), CH-05 (inter-device delay, #105), CH-14
(communication serialization, #104), CH-08 (shared serial line, #68), CH-09 (flow control and RTS,
#107), CH-16 (device discovery, #110), RT-12 (defaults, reset, unsaved-edit warning). CH-15 (write
optimizations) is out of scope.

**Data and fields.** Name, enabled, override (`overrides.yaml` enabled on/off/unset), description
(#103), `inter_device_delay_ms` 0–60000 (#105), `serialization_group` and `transactions_per_turn`
1–99 (#104). Devices of the channel with driver, host and state. A "shared line" note when two
channels use the same serial port (#68).

**Wizard steps** (nothing is written before Finish; Back keeps the entered values):
1. **Name** the channel (naming rules inline, PRJ-13).
2. **Driver** from the driver catalogue (`GET /api/v1/drivers`, #129): name, protocol, read-only
   note, Python/Rust availability.
3. **Connection**: host and port, or for serial drivers the transport (gateway TCP, RFC 2217,
   local port) with baud, bytesize, parity and stop bits (DEV-11, CH-07).
4. **Parameters**: a form generated from the driver's params schema with defaults filled in;
   unknown keys cannot be entered (ADR-0003).
5. **Summary**: the JSON to be written, the `check` estimate for the device (blocks, requests per
   pass, cycle time, load) and **Finish**. Finish sends one `POST /api/v1/channels` with the device
   and `If-Match`. A 422 sends the user back to the step that owns the field.

**Actions (editor).** Save (PATCH name and enabled; the override separately); delete with a
confirmation that lists devices, map entries and overrides; discover devices (CH-16, #110: an
engineer starts it, it sends read-only identity requests, then lists candidates to add);
**Diagnostics** opens the channel's frame capture (screen 6).

**API.**
- ✓ `GET/PATCH/DELETE /api/v1/channels/{c}`, `PUT/DELETE /api/v1/overrides/channels/{c}`,
  `POST /api/v1/channels` (with `devices`), `GET /api/v1/project/validate`.
- ✓ `GET /api/v1/drivers`, `GET /api/v1/drivers/{name}` (API-16, #129).
- ✗ `description`, `inter_device_delay_ms`, serialization fields in the channel model (#103,
  #104, #105).
- ✗ `POST /api/v1/project/validate` on a draft change, so the summary step can show the plan of an
  unsaved channel. Today `GET /project/validate` checks only the files on disk.
- ✗ `POST /api/v1/channels/{c}/discover` (CH-16, #110).

**Roles.** viewer (see); engineer (edit, wizard, discover).

**Empty and error states.** Unknown channel: 404 with a link to the tree. A driver without a params
schema: the parameters step falls back to a JSON editor with the documented keys
(`docs/driver_params.md`) listed beside it.

```text
New channel                                   1 Name · 2 Driver · 3 Connection · [4 Parameters] · 5 Summary
┌──────────────────────────────────────────────────────────────────────────────┐
│ Driver: s7  (Siemens S7comm, read-only)                                      │
│  rack            [0     ]  0-7                                               │
│  slot            [2     ]  0-31   (S7-300: 2; S7-1200/1500: 1)               │
│  pdu_size        [480   ]                                                    │
│  nonnormal_float (•) zero  ( ) unmodified                                    │
│                                         [Back]  [Next: summary]  [Cancel]    │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 3. Device editor and new-device wizard

**Purpose.** Every device property, including the driver parameters as a form, the publish-map
entry and the device override. The wizard adds a device to an existing channel, with an optional
read-only **Test** step (UI-05).

**Plan.** DEV-01, DEV-02 (description, #103), DEV-03 (data collection), DEV-04 (simulated mode, #12),
DEV-06 (scan rates), DEV-07/08 (client-driven and demand scan modes, #111), DEV-10 (auto-demotion),
DEV-15 (timeout and attempts), DEV-16/17 (connect timeout and inter-request delay, #105), DEV-11
(transport), CH-04 (non-normalized floats), CH-06 (bind address, #106), DEV-18 (alternate host
paths, #113), DEV-13 (device time zone, #112), TAG-13 (dynamic tags, #118), DEV-12/API-29 (browse
the device for tags, #11), API-16 (driver catalogue, #129), PRJ-05 (validation). DEV-14 (clock
sync) is out of scope.

**Sections.**
1. **Identity:** name, channel (moving a device keeps its map entry and override), driver,
   description, enabled. KEPServerEX provenance (`source`) shown read-only.
2. **Connection:** host (and alternate hosts, #113), port, transport fields, bind address (#106).
3. **Driver parameters:** the generated form (as in the wizard). The JSON stays available behind
   "Edit as JSON".
4. **Timing:** `scan_ms`, `min_scan_ms`, `timeout_ms`, `retries` (shown as "attempts = retries +
   1"), `connect_timeout_ms`, inter-request delay, `demote_after`, `demote_for_ms`. Each shows its
   default and a **reset to default** link, and the effective value when an override applies.
5. **Scan mode** (#111): all tags at their own rates, demand only (tags stay `stale` between
   reads), client-driven (later).
6. **Publish map:** `sub_device_id`, `topic`, `env`, `stub` (a stub entry is highlighted until
   confirmed).
7. **Override** (`overrides.yaml`): the fields it changes, as a diff against the device.
8. **Tags:** the tag table (screen 4).

**Wizard steps.** Identity → driver and connection → parameters → scan and timing → **Test**
(optional) → summary. Test calls `POST /api/v1/devices/test` with the unsaved device: one
connection, one identity request (or one block read), close. It is rate-limited per host and says
which read it sends. The summary shows the plan and the pending-reload note ("the device is polled
after Reload").

**API.**
- ✓ `GET/PUT/PATCH/DELETE /api/v1/devices/{id}`, `POST /api/v1/channels/{c}/devices`,
  `PUT/PATCH/DELETE /api/v1/map/devices/{id}`, `PUT/PATCH/DELETE /api/v1/overrides/devices/{id}`;
  request bodies are typed in the contract (#89).
- ✓ Driver catalogue `GET /api/v1/drivers[/{name}]` (#129).
- ✗ `POST /api/v1/devices/test` (UI-05): unfiled.
- ✗ `POST /api/v1/devices/{id}/browse` (API-29, #11).
- ✗ The new device fields above (#103, #105, #106, #111, #112, #113, #118).

**Roles.** viewer (see); engineer (edit, wizard, test, browse).

**Empty and error states.** A driver not implemented in the running engine (the Rust engine during
the port): the driver list marks it "not in this engine", and the device page says the device is
not polled for that reason (LOG-01, #49). A device whose plan fails: the plan error at the top with
the bad tags listed (`bad_config`).

```text
SITE MODBUS / Device1                                    [Save] [Delete] [Quick Client]
 Identity    name [Device1     ]  channel [SITE MODBUS ▾]  driver modbus_tcp   [x] enabled
 Connection  host [192.0.2.10  ]  port [502 ]  transport (•) tcp ( ) serial ( ) rfc2217
 Parameters  unit_id [1 ]  zero_based [x]  first_word_low [x]  max_holding [32 ]   Edit as JSON
 Timing      scan_ms [1000] default 1000 · timeout_ms [1000] · attempts 3 (retries [2])
 Publish map sub_device_id [OL_SITE_MODBUS ]  (stub: confirm the real id)       [Save map]
 Override    timeout_ms 1000 → 2500                                             [Edit]
 Tags (9)    … screen 4 …
```

## 4. Tag table

**Purpose.** View and edit a device's tags in bulk: inline edits, multi-row changes, CSV import and
export compatible with KEPServerEX, a pattern generator, address help and a scaling form (UI-06,
UI-07, UI-10, UI-12).

**Plan.** TAG-01/02/03/07/08/10/12 (done), TAG-04 and TAG-11 (8-bit, BCD, date, scaled data type,
#115), TAG-05 (access is always "Read only"; no access column to edit), TAG-09 (multiple tag
generation, #117), SYS-01/02 (CSV export and import, #10), API-17/18 (filter, sort, page, #129),
API-21 (a POSTed list is all-or-nothing), UI-07 (address help, #139). TAG-06 (read/write) is out of
scope.

**Data shown.** Name, group, address, dtype, scan rate (and the effective rate), enabled, scaling
summary, description, override badge, `bad_config` marker with the planner's reason. Large devices
are paged (100 rows) with a filter and sortable columns.

**Actions.**
- **Inline edit** a row (done): Save and Cancel per row; field errors under the cell.
- **Select rows** (checkboxes, Shift-click ranges) → **Bulk edit**: set scan rate, enabled, dtype or
  group for all selected rows in one change, or delete them (one confirmation, one audit entry).
- **Add a tag** (done), with **address help** beside the address field: the driver's syntax forms
  and examples, and a debounced "check address" that plans the draft tag without saving (#139).
- **Scaling form** (UI-12): type (none, linear, square root), raw and engineering ranges, clamps,
  negate, units, scaled dtype (#115), and a preview of three raw values computed with the engine's
  arithmetic. An empty form removes `scale`.
- **CSV export** (SYS-01, #10): the KEPServerEX column layout, `,` or `;`, optionally one group.
- **CSV import** (SYS-02, #10): upload → dry run listing added, changed and failed rows (row, column,
  value, reason) → apply (one change, `If-Match`).
- **Generate tags** (TAG-09, #117): a dialog with name prefix and counter, address parts (static
  text, numeric range with base and step, text list), dtype, scan rate and group; **Preview** lists
  every tag with ok or the driver's reason; **Generate** adds only the valid ones.

**API.**
- ✓ `GET/POST /api/v1/devices/{id}/tags` (POST takes a list), `GET/PUT/PATCH/DELETE …/tags/{tag}`.
- ✗ Bulk edit and delete: `PATCH /api/v1/devices/{id}/tags` with `{names: [...], patch: {...}}`
  and `DELETE /api/v1/devices/{id}/tags?names=…`, each one audited change: unfiled.
- ✓ `q`, `sort`, `order`, `limit`, `offset` on the tag list (API-17/18, #129).
- ✗ `GET /api/v1/drivers/{driver}/address-help`, `POST /api/v1/devices/{id}/tags/validate` (#139).
- ✗ `GET …/tags?format=csv`, `POST …/tags:import?dry_run=` (#10).
- ✗ `POST …/tags:generate?dry_run=` (#117).

**Roles.** viewer (see, export); engineer (edit, import, generate).

**Empty and error states.** No tags: three buttons (Add a tag, Import CSV, Generate). A CSV with no
valid rows: the dry run lists every failure and Apply stays disabled. Filter with no match: "No tag
matches 'x'."

```text
Tags (2,431)   [Filter…        ] [+ Add] [Import CSV] [Export CSV] [Generate…]   page 1/25 ‹ ›
 [ ] Name ▲            Group    Address    Type     Scan    En  Scaling          Description
 [x] PUMP_1_CURRENT_R  PUMP_1   400133     float32  1000    ✓   linear 0-4095→0-10 A
 [x] PUMP_1_HOURS      PUMP_1   400080     uint32   5000    ✓
 [ ] LEVEL_1                    400063     uint16   1000    ✓                    bad_config: …
 2 selected: [Set scan rate…] [Enable] [Disable] [Move to group…] [Delete]
```

## 5. Quick Client

**Status.** Done (#31 via #151, D-067). Follow-ups in #164 (subscription cap, unknown-device
errors, Rust port).

**Purpose.** Watch a device's tags live, with quality and timestamps, and demand-read them
(UI-15, UI-16, ADM-05). No write control (UI-17).

**Plan.** UI-16, ADM-05, API-33 (read-once), DEV-08 (demand-only devices stay `stale` between
reads), SYS-04/05/07/08/11 (system tags, #22, #127).

**Data shown.** A device tree (channels → devices with state dots, filter) and the selected
device's tags: name, address and dtype (from the config), value (muted when the quality is not
good: it is the last good value), quality badge (text and colour), source time, age (ticks every
second, highlighted after 60 s), update count. Changed rows flash briefly. A mode badge says
"live", "connecting…" or "polling every 2 s" (fallback when the engine has no `/api/v1/ws`).

**Actions.** Pick a device; filter tags by name or address; **Read now** (operator, admin today):
one full read through `POST …/read-once`, serialized with polling. Planned (UI-16): watch lists of
tags from several devices, picked with the tag picker (#139), saved per browser and exported as
JSON; pause and resume; an update-rate cap and deadband per list (applied in the browser, never
faster than the scan); the server time next to the source time; for a bad quality, the device's
last error and the ADR-0006 status code north protocols report; `_System` tags (#127).

**API.**
- ✓ `GET /api/v1/ws` (subscribe, snapshot, conflated changes, `unavailable`, `error`; auth in the
  handshake), `GET /api/v1/status`, `GET /api/v1/devices/{id}/tags`,
  `GET /api/v1/devices/{id}/values` (fallback), `POST /api/v1/devices/{id}/read-once`.
- ✗ `server_ts` in WebSocket changes (UI-16): unfiled.
- ✗ Subscription cap and error frames for unknown devices; `/api/v1/ws` in the Rust engine (#164).
- ✗ System tags in the feed (`include_system`, #127).

**Roles.** viewer (watch); operator (read now; admin until #21).

**Empty and error states.** No device chosen: "Choose a device on the left." Device not polled: a
banner with the reason; it reappears with a snapshot when polled again. No enabled tags: "This
device has no enabled tags." More than 1,000 matching rows: the first 1,000 and "filter to see the
others". Socket closed: "connecting…" with backoff; after three failures without ever connecting,
REST polling.

```text
Quick Client                                                                     [live]
┌ Devices ─────────────┐┌ SITE MODBUS/Device1           [Filter tags…   ] [Read now] ┐
│ [Filter devices…   ] ││ 10 tags · 9 good · 1 bad_comm                               │
│ SITE MODBUS          ││ Tag          Address Type    Value  Quality   Source time   Age Upd │
│  ● Device1           ││ FLOW_1       400045  float32 104    good      10:43:04.750  0 s  12 │
│ SITE M800            ││ FLOW_2               float32 3.2    bad_comm  09:00:00.000  1 h   0 │
│  ● 8 PUMP            ││ LEVEL_1      400063  uint16  1      good      …            24 h   0 │
└──────────────────────┘└─────────────────────────────────────────────────────────────┘
```

## 6. Device diagnostics

**Purpose.** Why a device is slow, failing or demoted: state, timings, counters, the last errors,
the block plan, identity, `opencap check`'s estimate, and a capture of the frames on the wire
(DIAG-01, DIAG-10, DIAG-11/12).

**Plan.** DIAG-01 and DIAG-10 (done), DIAG-11/12 (frame capture and the Diagnostics panel, #138),
LOG-37 (one capture at a time: a second one gets 409), CH-08 (`shared_line`, #68), DEV-18 (active
path, #113), LOG-R6 (`last_reason`, #133), SYS-08/11 (`_Error`, `_SecondsInError`, byte counters,
#22, #130), PRJ-14 (check view, done), DIAG-13 (refused writes: a counter only).

**Data shown.**
- **Status cards:** state (ok, offline, demoted, not polled), connected, last good, error count,
  cycle time, overruns, active host path (#113).
- **Last error** with its reason code (#133) and, for an unsupported driver, the fix text (#49).
- **Counters:** requests by result (ok, timeout, conn_error, bad_config, error), overruns, bytes
  sent and received (#130), each with "since view reset" (the baseline is kept in the browser,
  DIAG-10).
- **Block plan:** blocks per rate group, requests per pass, estimated cycle, load, `bad_config`
  tags with reasons (done).
- **Identity** over the existing connection, cached five minutes; "ask again" (operator).
- **Frame capture** (#138): start (operator) with duration ≤ 30 min and a buffer size; a live view
  with time, direction, hex and ASCII; find, copy, download, autoscroll, clear; "capturing, 12 min
  left". The capture keeps running across a reload. A frame shows what went over the wire for a
  read; there is no write to show.

**API.**
- ✓ `GET /api/v1/devices/{id}/status|plan|identity`, `GET /api/v1/project/validate`, `/metrics`
  (counters, via the API's own fields once added).
- ✗ Counters per device in `GET /devices/{id}/status` (requests by result, bytes, overruns since
  start), so the UI does not parse Prometheus text: unfiled.
- ✗ `POST/GET/DELETE /api/v1/diagnostics/channels/{c}/capture` (DIAG-11, #138).
- ✗ `last_reason` (#133), `shared_line` (#68), active path (#113).

**Roles.** viewer (see); operator (identity refresh, capture start and stop; admin until #21).

**Empty and error states.** Not polled: the reason, and a link to the pending-reload banner if
that is the cause. No identity: "The device does not report an identity." Capture busy: "Another
capture is running on SITE MODBUS (started by alice, 8 min left)."

```text
SITE MODBUS/Device1 · diagnostics                         [Quick Client] [Edit config]
 State ● ok  connected   Last good 2 s ago   Errors 0   Cycle 42 ms   Overruns 0
 Requests  ok 12,840 · timeout 3 · conn_error 1        (since view reset 10:12)  [Reset view]
 Plan      3 blocks, 4 requests per pass, ~48 ms per pass, load 5 %       ▸ blocks
 Identity  vendor Example Vendor · revision 1.2                              [Ask again]
 Capture   [Start 10 min ▾]   — or —   ● capturing, 7 min left [Stop] [Download]
   10:43:04.701 → 00 01 00 00 00 06 01 03 00 2C 00 0A          read holding 44-53
   10:43:04.742 ← 00 01 00 00 00 17 01 03 14 …                  20 bytes
```

## 7. North agents

**Purpose.** Where the data goes: MQTT agents with their outboxes, the OPC UA server and, later,
other north plug-ins. Status, backlog and how long an outage the outbox covers, with the few admin
controls KEPServerEX offers (SAF-16).

**Plan.** SAF-13 (queue and dropped count, done), SAF-14 (bytes, oldest age, capacity, disk free,
"outage covered", #143), SAF-03/04/06 (outbox health and path warnings, #142), SAF-08 and SAF-16
(forward mode and purge, #143), SAF-19 (one outbox per agent, #51), API-25 (agent resources,
#132), ADM-06 (OPC UA server page), DIAG-05/06 (OPC UA trace, #137), SYS-05 (client counts and
names, #22), LOG-34 (MQTT link state, done), RT-11 (listeners).

**Data shown.**
- **Agent list:** kind (MQTT, OPC UA server, …), name, state (connected, reconnecting, disabled),
  endpoint (broker host, or listening address and port with TLS), messages per minute.
- **MQTT agent:** link state and since when, broker, client id, topic prefix, payload format;
  outbox: messages, bytes, oldest message age, capacity used, disk free, outage covered at the
  current rate, dropped total, forward mode (ordered or live-first).
- **OPC UA server:** endpoint URLs, security policies and modes, anonymous on/off, user-token
  types, limits, sessions and subscriptions (client names), the trust list (link to screen 11).
- **Trace** (#137): start and stop a protocol trace for one server, then view it.

**Actions.** Purge an outbox (admin; `If-Match` is the backlog count shown, so a growing backlog
is never purged blind; audited); change the forward mode (admin, audited); start a trace
(operator). Agent settings themselves are configuration (`poller.yaml` today; agent resources in
#132), and most of them need a restart, which the form says.

**API.**
- ✓ `GET /health` (`mqtt_connected`, `saf_queue`, `opcua_server`), `/metrics`.
- ✗ `GET /api/v1/north`, `GET /api/v1/north/{kind}/{name}` with status (API-25, #132).
- ✗ Outbox fields and `POST /north/{agent}/outbox/purge`, `PUT /north/{agent}/forward-mode`
  (SAF-14/16, #143).
- ✗ OPC UA server status (sessions, subscriptions, client names, endpoints) (ADM-06, SYS-05):
  part of #132, or a separate issue.
- ✗ `POST /api/v1/diagnostics/north/{server}/trace` (DIAG-05, #137).

**Roles.** viewer (see); operator (trace); admin (purge, forward mode).

**Empty and error states.** No agents: "Nothing is published: add an MQTT broker in poller.yaml or
enable the OPC UA server." Outbox unavailable (#142): a red card with the reason and the path.

```text
North                                                                                    
 Agent              Kind        State                 Endpoint                 Outbox
 plant-broker       MQTT        ● connected 3 h       broker.example:8883 TLS  0 msgs
 opc-ua             OPC UA srv  ● 2 sessions          opc.tcp://0.0.0.0:4840   —
plant-broker · outbox: 1,204 msgs · 18 MB · oldest 6 min · 2 % of 1 GB · covers ~41 h
 forward mode (•) ordered ( ) live first                  [Purge 1,204 messages…]
```

## 8. Event log

**Purpose.** The operator's history of what happened: devices going offline and back, demotions,
config reloads, refused requests, certificate warnings, north link changes. KEPServerEX's event log
pane (DIAG-03/04).

**Plan.** ADM-11 and DIAG-03 (settings; clear is admin-only and leaves a `security` entry, #136),
DIAG-04 and LOG-R11 (the page, #136), LOG-R10 and API-12 (the API, #136), LOG-R5 (message, cause,
fix, #134), LOG-R7 (`suppressed=N`, #133), LOG-38 (`event_log_ok` banner, #56), API-14 (refused
requests, #135), LOG-24 (auth events, #21). The Audit page (ADM-21, done) stays separate: it lists
configuration changes, while the event log lists runtime events.

**Data shown.** Rows: local time (UTC on hover), severity (text and icon, not colour alone), source
(device, channel, agent, api, system), device link, message, and `suppressed=N` when flood control
merged repeats. A row opens a detail panel: cause, fix, the event key, raw JSON, a link to the
device's diagnostics.

**Actions.** Filter by severity (at least), source, device and text; time range; live or paused
(live appends new rows at the top and keeps the scroll position when the user has scrolled);
export the filtered rows as JSONL or CSV; clear (admin, confirmation, leaves a `security` event).

**API.**
- ✗ `GET /api/v1/events` with filters and a cursor, and live events (#136). The three plan files
  use different parameter names (DIAG-04 `level`/`limit`, LOG-R10 `min_level`/`cursor`, API-12
  `severity`/`offset`/`sort`). #136 must settle one set; this UI assumes `min_level`, `source`,
  `device`, `q`, `since`, `until`, `cursor`, `limit`, matching the audit list's style.
- ✗ Live events: either a second message kind on `/api/v1/ws` (`{"subscribe": {"events": {...}}}`)
  or polling with the cursor. Proposal: the WebSocket, so the page and the Quick Client share one
  connection.
- ✗ `DELETE /api/v1/events` (clear, admin) (#136).

**Roles.** viewer (see, export); admin (clear).

**Empty and error states.** No events in range: "No events from 10:00 to 11:00 at warning or
above." Event store failing (#56): a banner from `event_log_ok`.

```text
Events   severity [warning+ ▾]  source [all ▾]  device [        ]  text [      ]  (•) live ( ) paused  [Export ▾]
 10:43:12  ⚠ warning  device   SITE PCCC/7 PUMP   demoted after 3 failed cycles (timeout)      ▸
 10:41:57  ✖ error    device   SITE PCCC/7 PUMP   connection refused by 192.0.2.40:44818       ▸ suppressed=4
 10:40:00  ℹ info     system   config reloaded: restarted SITE MODBUS (by alice)                ▸
```

## 9. Import wizard

**Status.** Done (#33, D-087).

**Purpose.** Start or update a project from a KEPServerEX `project.json`, or restore an opencap
export: upload, review what will change, apply or discard (UI-14, PRJ-07).

**Plan.** UI-14, PRJ-07, API-30, PRJ-09 (credentials become env-var names), CH-12 and TAG-16
(encapsulation settings and alias reports, #108), PRJ-13 (name violations, #112), CH-06 (adapter
names, #106), PRJ-02 (three-way re-import, #37). PRJ-08 (encrypted `.opf`/`.sopf`) is out of
scope: the page tells the user to save the project as JSON in KEPServerEX first.

**Steps.**
1. **Upload** the file (raw JSON, at most 64 MiB). The server runs the importer into
   `<config>/.import-staging/` and deletes the upload; nothing is written to the configuration.
2. **Review:** what the export contains and what is imported; devices per driver; devices not
   imported, each with its reason; same-address groups (D-011), suspects highlighted; the changes
   to the configuration (channels, devices and tags added, removed, changed); the environment
   variables to set for credentials that were not copied; importer notes; files to be written.
   New devices' stub publish entries are highlighted, and **Apply** waits until the user confirms
   them. The staged import survives leaving the page (until it expires after one hour).
3. **Applied:** what was written, and whether the reload ran. Or **Discard** from step 2.

Planned additions: map KEPServerEX COM ports to local serial ports or gateways before applying
(UI-14 step 3, follow-up); a three-way re-import that keeps edits made in opencap (PRJ-02, #37).

**API.** ✓ `POST /api/v1/project/import?stage=true`, `GET /api/v1/project/import/{id}`,
`POST /api/v1/project/import/{id}/apply` (`If-Match`, `?apply=true`),
`DELETE /api/v1/project/import/{id}`; the older dry-run and apply calls of `/project/import`.
✗ COM-port mapping during an import: unfiled.

**Roles.** engineer (admin until #21).

**Empty and error states.** Not JSON, or not a known format: the reason with the expected formats.
Expired staging: "This staged import expired: upload the file again." Config changed since the
upload (412): "Discard it and upload the file again." Read-only config directory: 503 with the
path.

```text
Import a project              1 Upload · [2 Review] · 3 Applied
 Review: project.json (KEPServerEX project) · staged until 18:57
 In the export 15 devices · Imported 10 devices · Not imported 5 devices · Same address 3 / 2
 Devices per driver   ab_logix 2 · modbus_tcp 1 · s7 3 · …
 Not imported         SIMULATOR/Device1  Simulator driver: no real device · …
 Changes              devices +1 −0 ~1 · tags +5 −1 ~1 · files devices.json, devices_map.yaml
 ┌ 1 new device gets a stub publish entry ───────────────────────────────────┐
 │ SITE PCCC/7 PUMP     [ ] I will confirm these SubDeviceIds              │
 └───────────────────────────────────────────────────────────────────────────┘
 [Apply import] [x] Reload now   [Discard]
```

## 10. Users and tokens

**Purpose.** Who may do what. Today: the named tokens from `poller.yaml`, read-only. After RFC 0005
(#21): users with roles, created and disabled here.

**Plan.** ADM-18/API-02 (login, whoami, open-mode notice: done), ADM-13 (users and roles, #21;
named tokens with two roles done), ADM-14/API-27 (capability table, #21), ADM-15/API-04 (token
length, throttling, #21), API-26 (user CRUD; removing the last admin gets 409), ADM-16, ADM-08
(idle logout, not started), ADM-17 (out of scope).

**Data shown.** Today: each token's name and role, where it comes from (`api.tokens` or the legacy
`api.token_env`), and the env var name that holds it. **Never the secret.** The current principal
is highlighted. After #21: users (name, role, enabled, last login, created by), and the capability
table for each role.

**Actions.** Today none (a note says tokens are changed in `poller.yaml` and need a restart). After
#21: add user, change role, disable or enable, reset credential (shows the new one once), delete
(confirmation; the last admin cannot be removed). Log out everywhere for a user (revoke sessions,
if #21 has sessions).

**API.**
- ✓ `GET /api/v1/whoami`, `GET /api/v1/auth-info`.
- ✗ `GET /api/v1/admin/tokens`: names, roles and sources of the configured tokens, no secrets.
  Small, useful before #21: unfiled.
- ✗ `/api/v1/admin/users…` (API-26, #21).

**Roles.** admin.

**Empty and error states.** Open mode: "No API token is configured: everyone on this machine is the
read-only viewer 'anonymous'." A viewer opening the page: 403 with the role needed.

```text
Admin › Users and tokens
 Name      Role    Source                         Env var
 alice     admin   api.tokens                     OPENCAP_TOKEN_ALICE      (you)
 grafana   viewer  api.tokens                     OPENCAP_TOKEN_GRAFANA
 default   admin   api.token_env (legacy)         OPENCAP_API_TOKEN
 Tokens are set in poller.yaml (api.tokens) and need a restart. Users and roles: RFC 0005 (#21).
```

## 11. Certificates

**Purpose.** The certificates opencap presents and the ones it trusts: the OPC UA server's
application certificate and trust list first, later the API's TLS certificate and the MQTT client
certificate (ADM-22).

**Plan.** ADM-22 (not started; no issue), ADM-06 (the OPC UA page's trust list), LOG-30 (expiring
in 30 days, expired, rejected client: events), ADM-20/API-03 (API TLS, #21), ADM-10 (OPC DA, #18).

**Data shown.** Per feature (OPC UA server, OPC UA client driver, API TLS, MQTT): own certificate
(subject, issuer, validity with "expires in N days", thumbprint, key size; **never the private
key**), trusted certificates, rejected certificates (clients that tried to connect), issuers and
CRLs. A badge when a change needs a restart of that feature.

**Actions.** Export a certificate as PEM; reissue the self-signed certificate (confirmation, says
clients must trust the new one); import a certificate and key (PEM or DER); trust a rejected
certificate (moves it to trusted); remove a trusted one; `force` to trust an expired one, with a
warning.

**API.**
- ✗ `GET /api/v1/certificates`, `GET /api/v1/certificates/{feature}`,
  `POST …/{feature}/reissue`, `PUT …/{feature}/own` (import), `POST …/{feature}/trusted`
  (trust; from rejected by thumbprint), `DELETE …/{feature}/trusted/{thumbprint}`: unfiled
  (ADM-22). Changes are audited; private keys never leave the server.

**Roles.** viewer (see); admin (change).

**Empty and error states.** OPC UA server disabled: "The OPC UA server is off (poller.yaml
opcua_server.enabled)." No rejected certificates: "No client was rejected." Expiring soon: a
warning card at the top and on the dashboard.

```text
Admin › Certificates › OPC UA server                                    restart needed: no
 Own        CN=opencap · expires 2027-11-01 (341 days) · SHA-1 3F:2A:…   [Export PEM] [Reissue…] [Import…]
 Trusted    CN=scada-client-01 · expires 2028-01-12                      [Remove]
 Rejected   CN=unknown-client · first seen 10:12 · 192.0.2.77            [Trust] [Delete]
```

## 12. Node and redundancy status

**Purpose.** What this node is and how it is doing: version, engine (Python or Rust), uptime,
process and host resources, run mode, and, once redundancy exists, its role in the pair (ADM-09,
RT-07, RT-12).

**Plan.** ADM-09 (process info, not started), RT-07 (engine kind in `/health`, done), RT-12 (header
facts, partial), RT-08 (read-only service mode, done), RT-10 (worker threads, #144), ADM-03/API-09
(About and support information, #123), ADM-02 (reload, done; `?all=true` proposed), LOG-01 (driver
inventory, #49). Server-pair redundancy is RFC 0003 (#20, M5); DEV-18 (alternate host paths) is
per device (screen 3).

**Data shown.**
- **About** (#123): product, version, engine kind, build, licences link, config directory,
  instance name, OS, Python or Rust version, the drivers compiled in. A "copy support info" button
  puts a redacted text block on the clipboard (no tokens, no hosts unless asked).
- **Process:** pid, uptime, CPU and memory use, worker threads (#144), open connections, API
  listeners (address, port, TLS).
- **Readiness** (#123): `/health/ready` and `/health/live` results with the failing checks.
- **Redundancy** (#20): role (active, standby), peer address and state, last sync of the project,
  witness. Shown only when configured.

**Actions.** Copy support info; reload (engineer); after #20: switch over (admin, confirmation).

**API.**
- ✓ `GET /health`, `GET /api/v1/status`.
- ✓ `GET /api/v1/about` (open), `GET /api/v1/support`, `/health/ready`, `/health/live` (#123).
- ✗ Process figures (uptime, CPU, memory, threads, listeners) in `/api/v1/about` or
  `/api/v1/status`: part of #123 or #144; unfiled as such.
- ✗ Redundancy status and switch-over (#20, after RFC 0003).

**Roles.** viewer (see); engineer (reload); admin (switch over).

**Empty and error states.** Redundancy not configured: the section is hidden. A node that is
standby: a banner on every page ("This node is standby: configuration changes are made on the
active node").

## 13. Remote-node switcher (desktop)

**Status.** Done (#87); installers with a sidecar engine are #58.

**Purpose.** KEPServerEX's "connect to a remote runtime": the desktop launcher keeps connection
profiles for local and remote engines, shows each one's health, and opens the engine's own UI in a
native window (UI-02, RT-12).

**Plan.** UI-02, RT-12/ADM-01 (profiles, last opened, keychain tokens, health and token check),
RT-07 (all access through `/api/v1`), RT-09 (sidecar, #58), API-09 (identify a node before holding
a token, #123), API-35 (Rust engines have only the admin routes, #140), ADM-20 (TLS for remote
access, #21).

**Data shown.** Profiles: name, address, health (version, instance, devices ok/offline/demoted,
MQTT link), token state (stored, needed, refused, bound to another address), a warning when a
token would go over plain `http://` to another computer.

**Actions.** Add, edit, delete a profile; store or forget a token (OS keychain); open the engine's
UI; reopen the last one on start. Each engine window is its own origin and cannot navigate away;
the bundle inside is the engine's own, so every engine version shows its own UI.

**API.** ✓ `/health`, `/api/v1/whoami` (or `/channels` on an engine without the config API), from
Rust in the launcher. ✓ `GET /api/v1/about` is open (#123), so a profile can show what it points
to before a token is entered (the launcher does not use it yet).

**Roles.** None in the launcher; the engine decides with the stored token.

**Empty and error states.** No profiles: one prefilled for `http://127.0.0.1:8080`. Unreachable:
"No answer from 192.0.2.5:8080 (connection refused)." Token refused: "The engine refused the
token: enter a new one."

```text
opencap desktop
 ● Local          http://127.0.0.1:8080     0.1.0 · 10 ok / 0 offline · token stored     [Open]
 ● Plant A        https://192.0.2.5:8443    0.1.0 · 31 ok / 1 offline · token stored     [Open]
 ○ Plant B        http://192.0.2.9:8080     no answer (connection refused)                [Edit]
 [+ Add a node]
```

## 14. Settings and revisions

**Purpose.** Node settings that are not part of the project, the configuration's revision history
with restore, and the backup and validation actions (API-20, RT-06, PRJ-12).

**Plan.** API-20 (node settings with `restart_needed`; the `api` section is refused over the API,
#132), PRJ-02/PRJ-03 (project file v2 and node settings, #37), RT-06/PRJ-12 (revisions list,
restore, clear; retention is `revisions.keep` default 10 in RT-06 but `config.keep_revisions`
default 50 in PRJ-12, which #37 must settle), PRJ-11 (export, done), RT-05 (read-only config,
done), PRJ-05/06 (validate, pending, apply: done), ADM-11 (event-log settings, #136), ADM-20
(`api.tls.*`, #21), ADM-19 (bind, port, CORS), SAF-02/06 (`saf.path`, limits, #142), ADM-07
(language, #126).

**Data shown.** Sections of `poller.yaml` as forms (instance, logging, MQTT, store-and-forward,
OPC UA server, check estimates, defaults), each field with its default and a "needs restart"
marker. Revisions: revision id, time, who, action, files (from the audit log), with a diff against
the current revision.

**Actions.** Save settings (engineer; `restart_needed` lists what waits for a restart); restore a
revision (admin, `If-Match`, then the pending-reload banner); export the whole configuration
(done); run `check` (done, screen "Check").

**API.**
- ✓ `GET /api/v1/project`, `GET /api/v1/project/revisions`, `GET /api/v1/project/validate`,
  `GET /api/v1/project/pending`.
- ✗ `GET/PATCH /api/v1/node` (API-20, #132).
- ✗ `POST /api/v1/project/revisions/{rev}/restore` and revision contents (PRJ-12, #37). Until
  then, a revision can only be restored from its `.bak` files or an export.

**Roles.** viewer (see); engineer (settings); admin (restore).

**Empty and error states.** No revisions written through the API: "No revisions yet: changes made
here or by import appear in this list." A setting the API may not change (`api.*`): shown
read-only with "edit poller.yaml and restart".

---

## Accessibility and theming

**Keyboard.**
- Every action is a `<button>` or a link; nothing depends on hover or right-click. Tab order
  follows the visual order; focus is always visible (2 px accent outline).
- Forms submit with Enter; dialogs and confirmations close with Escape and return focus to the
  control that opened them.
- The project tree and the Quick Client device tree follow the WAI-ARIA tree pattern: arrow keys
  move and expand, Home/End jump, typing a letter moves to the next match.
- Wizards: steps are an ordered list with `aria-current="step"`; Back and Next are buttons, and the
  step that failed validation receives focus.
- A "skip to content" link precedes the navigation; `header`, `nav`, `main` and `footer`
  landmarks; one `h1` per screen.

**Screen readers.**
- Tables use `<th scope>`; status and quality badges carry text, never colour or an icon alone.
- Flash messages and errors are `role="status"` or `role="alert"`. Live tables (Quick Client,
  events) are **not** live regions, because they would talk constantly. The Quick Client announces
  only its connection state, and the event log announces new error-level events when the user
  turns that on.

**Colour and theme.**
- Colours are CSS custom properties in `app.css`, with a dark set under
  `prefers-color-scheme: dark`. Planned: a manual light, dark or system switch in the header,
  remembered per browser (`localStorage`), which sets `data-theme` on the root element.
- Text contrast meets WCAG 2.1 AA (4.5:1, 3:1 for large text and UI components) in both themes. The
  quality colours (good, bad_comm, bad_config, bad_decode, stale) and the state colours are always
  paired with their word.
- `prefers-reduced-motion`: no row flash or transitions; changed Quick Client rows get a static
  marker instead.

**Layout.** Works from 360 px to wide screens: a 16 px gutter, no horizontal page scroll, wide
tables scroll inside their container or become stacked cards (`table.responsive`), secondary
columns hide on phones (as the Quick Client does). Touch targets are at least 32 px high.

**Language.** Every visible string comes from a table per language (`i18n/<lang>.json`, #126 via
#235), with the browser's language chosen by default; dates and numbers use the browser's locale,
and times show UTC on hover.

**Desktop app.** The same bundle and rules. The launcher follows the same colour tokens, keyboard
rules and contrast.

## Missing API endpoints

What the screens above need and the contract (`api/openapi.json`) does not have yet. "Filed" means
an open issue covers it; the unfiled rows are for the lead to file (#26 asks for this list).

| Endpoint or field | Screen | Plan | Filed |
|---|---|---|---|
| `GET /api/v1/drivers`, `GET /api/v1/drivers/{name}` | 2, 3 | API-16 | done (#129) |
| `q`, `sort`, `order`, `limit`, `offset` on channel, device and tag lists | 1, 4 | API-17/18 | done (#129) |
| `description`, `drivers`, `tags`, `tags_enabled` on channels; `description` on devices | 1, 2, 3 | CH-02/03, DEV-02 | #103 |
| Channel and device timing fields (inter-device delay, connect timeout, inter-request delay) | 2, 3 | CH-05, DEV-16/17 | #105 |
| Serialization fields on channels | 2 | CH-14 | #104 |
| `GET /api/v1/browse?path=&depth=&include_system=` | 1, 4, 5 | UI-08 | #139 |
| `GET /api/v1/drivers/{driver}/address-help`, `POST /api/v1/devices/{id}/tags/validate` | 4 | UI-07 | #139 |
| Group routes `GET /devices/{id}/groups`, `PATCH/DELETE …/groups/{path}`; `POST …/tags:generate` | 1, 4 | API-24, TAG-09 | #117 |
| `GET …/tags?format=csv`, `POST …/tags:import?dry_run=` | 4 | SYS-01/02 | #10 |
| `POST /api/v1/devices/{id}/browse` | 3 | API-29 | #11 |
| `POST /api/v1/channels/{c}/discover` | 2 | CH-16 | #110 |
| `GET /api/v1/system/tags`, `include_system` on values and the feed | 1, 5 | SYS-* | #127, #22 |
| Subscription cap, error frames for unknown devices; `/api/v1/ws` in Rust | 5 | UI-16 | #164 |
| `POST/GET/DELETE /api/v1/diagnostics/channels/{c}/capture` | 6 | DIAG-11 | #138 |
| `last_reason` on device status | 6 | LOG-R6 | #133 |
| Byte counters per device | 6 | SYS-11 | #130 |
| `GET /api/v1/north`, `GET /api/v1/north/{kind}/{name}`; `GET/PATCH /api/v1/node`; async jobs | 7, 14 | API-25, API-20, API-28 | #132 |
| Outbox bytes, age, capacity; `POST /north/{agent}/outbox/purge`, `PUT /north/{agent}/forward-mode` | 7 | SAF-14/16 | #143 |
| `POST /api/v1/diagnostics/north/{server}/trace` | 7 | DIAG-05 | #137 |
| `GET /api/v1/events` (filters, cursor), `DELETE /api/v1/events`, live events | 8 | API-12, LOG-R10 | #136 |
| `/api/v1/admin/users…`, capability table, throttling | 10 | API-26/27, API-04 | #21 |
| `GET /api/v1/about`, `/health/ready`, `/health/live` | 12, 13 | API-09 | done (#123) |
| `POST /api/v1/project/revisions/{rev}/restore`, revision contents | 14 | PRJ-12 | #37 |
| Redundancy status and switch-over | 12 | RFC 0003 | #20 |
| **`POST /api/v1/devices/test`**: connect once, read identity or one block, close; rate-limited per host | 3 | UI-05 | unfiled |
| **`POST /api/v1/project/validate`** on a draft change (plan and cycle estimate before saving) | 2, 3 | UI-04/05 | unfiled |
| **`PATCH /api/v1/devices/{id}/tags`** (bulk patch by names) and **`DELETE …/tags?names=`**, one audited change each | 4 | UI-06, TAG-* | unfiled |
| **Counters on `GET /devices/{id}/status`**: requests by result, overruns, since start | 6 | DIAG-10 | unfiled |
| **OPC UA server status**: endpoints, policies, sessions, subscriptions, client names | 7 | ADM-06, SYS-05 | unfiled (or part of #132) |
| **`server_ts`** in WebSocket changes, and a last-error hint for bad qualities | 5 | UI-16 | unfiled |
| **`GET /api/v1/admin/tokens`**: configured token names, roles and sources, no secrets | 10 | ADM-13 | unfiled |
| **Certificates**: `GET /api/v1/certificates[/{feature}]`, reissue, import, trust, remove | 11 | ADM-22 | unfiled |
| **Process figures** (uptime, CPU, memory, threads, listeners) in about or status | 12 | ADM-09, RT-10 | unfiled (or part of #123/#144) |
| **COM-port mapping** during a staged import (KEPServerEX COM ports → local port or gateway) | 9 | UI-14 | unfiled |
