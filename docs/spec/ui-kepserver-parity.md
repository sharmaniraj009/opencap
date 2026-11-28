# KEPServerEX-style desktop experience: parity audit and build order

What it takes to give the opencap desktop app the working feel of KEPServerEX's Configuration tool
and OPC Quick Client, measured against the Rust engine as it is today. It builds on the screen
specification in [`docs/ui/screens.md`](../ui/screens.md) (the 14 screens, their wireframes and
rules) and on the feature plans [`plan/features/14-ui-and-tools.md`](https://github.com/sharmaniraj009/opencap/blob/main/plan/features/14-ui-and-tools.md)
(UI-01…20), 04 channels, 05 devices, 06 tags and 12 diagnostics. This page does not repeat them. It
answers three questions: what exists in `engine/ui`, which routes each screen needs and the Rust
engine really serves, and in what order to build.

## Decision: one UI (ADR-0010 stays)

The desktop app is the Tauri shell in `desktop/` around the engine's own web UI. There is no
second UI. The KEPServerEX experience is built in `engine/ui`:

- Plain HTML, CSS and JS, with the already vendored Alpine.js CSP build (`vendor/alpine-csp-3.17.5.min.js`,
  MIT). No framework is added and no build step. `script-src 'self'`: no inline script, no `eval`.
- Only `/api/v1` and the legacy `/channels`, `/devices/…` routes. A screen that needs data the engine
  does not serve gets a route first ([gaps](#gaps-that-need-engine-api-work)), never a private one.
- Every visible string comes from `engine/ui/i18n/en.json` (`t('key')` in the markup, `tr('key')` in
  the code). A new screen adds keys under its own prefix.
- Read-only toward PLCs (ADR-0008): no control writes a value. Quick Client has no value input.
- Any new file under `engine/ui/` must be added to the `FILES` table in
  `engine/crates/opencap-api/src/ui.rs` (a Rust test fails otherwise), and so the bundle stays one
  executable.

**Two copies of the bundle.** `engine/ui/` and the Python engine's `src/opencap/api/ui/` are
byte-identical today. From step (a) on, `engine/ui/` is the only copy that is edited: the Python
engine is frozen (ADR-0018) and the Python UI tests (`tests/unit/test_ui.py`, the browser smoke test)
keep checking its old copy. The i18n discipline those tests enforce (no text outside the table, no
unused or missing keys) gets a Rust test over `engine/ui` in step (a), because otherwise nothing
enforces it for the copy that matters.

## How this audit was made

The engine was built from `main` (`opencap-engine`, `engine: rust`) and run on the quickstart
`config-sim` (two simulator devices, `Water Plant/Tank1` and `Water Plant/Pump1`, no token). Every
route the UI code calls (`grep` of `call(` in `engine/ui/app.js`) and every route in
`api/openapi.json` was requested. Results:

| Route | Rust engine today | Used by |
|---|---|---|
| `GET /health`, `/health/live`, `/health/ready`, `/metrics` | 200 | desktop launcher, dashboard |
| `GET /api/v1/about`, `/api/v1/auth-info`, `/api/v1/whoami` | 200 (`config_writable: false`, with the reason) | login, shell |
| `GET /api/v1/status` | 200: `health`, `channels[]` with `devices[]` (id, driver, state, last_good, errors, cycle_ms), `config` | dashboard, project tree |
| `GET /channels` | 200: the same channel and device summary | desktop launcher fallback |
| `GET /api/v1/devices/{id}/status` | 200: channel, driver, host, port, enabled, tags, blocks, bad_config, plan_error, topic, sub_device_id, state, last_good, errors, cycle_ms, last_error, last_reason, connected | device page, properties panel |
| `GET /api/v1/devices/{id}/values` | 200: `{tag: {value, quality, source_ts}}` (tag names, no address or type) | device page, Quick Client REST fallback |
| `POST /api/v1/devices/{id}/read-once` | 200 (read-only; 409 on a failed read) | device page |
| `GET /api/v1/system/tags[?prefix=]` | 200: `items[{path, value, quality, source_ts, dtype}]` (`_System.*`) | system tags |
| `POST /api/v1/reload`, `GET /api/v1/project/pending` | 200 | config page |
| `GET /api/v1/devices/{id}/plan`, `/identity`, `GET …/tags`, `GET /api/v1/devices/{id}` | **501** "not implemented in the Rust engine yet (#140)" | device page, editor, check |
| `GET /api/v1/channels`, `/channels/{c}`, `/devices`, `/drivers`, `/project`, `/project/status`, `/project/validate`, `/project/revisions`, `/map`, `/overrides`, `/audit`, `/events`, `/browse` | **404** | config, channel, editor, check, import, audit pages |
| `GET /api/v1/ws` (live values) | **404** (the Quick Client falls back to REST polling) | Quick Client |
| every write route (`POST/PUT/PATCH/DELETE` on channels, devices, tags, map, overrides, import) | 404 or 501; the API is also read-only when no token is configured | editors, import |

Reading: against the Rust engine the dashboard, the device page's status and values, Read once and
the Quick Client's polling fallback work. The configuration pages (configuration, channel, device
editor, check, import, audit) were written for the Python engine's configuration API and call routes
the Rust engine does not serve yet. The KEPServerEX experience therefore starts with what the
engine can already answer, and the editors wait for the configuration API (core worker, #140).

## Screens

Screen numbers are those of `docs/ui/screens.md`. "Exists" is what `engine/ui` has on `main`.
"Works on Rust" is what the engine answers today (table above).

| # | KEPServerEX screen | Exists in `engine/ui` | Works on Rust today | Routes it needs | Step |
|---|---|---|---|---|---|
| 1 | Project tree: Channels › Devices › Tag groups › Tags, with a properties panel | linked pages (`#/config`, `#/channel/<c>`), no tree pane | channels and devices from `/api/v1/status`; device properties from `…/status`; tag names from `…/values` | `status`, `devices/{id}/status`, `devices/{id}/values`, `system/tags`; later `GET …/tags` (address, type, scan rate, scaling), `GET /channels/{c}` (channel properties), groups | (a) |
| 2 | Channel editor and new-channel wizard | channel page with *Add a device* (JSON params) | no | `GET/POST/PATCH/DELETE /channels…`, `GET /drivers/{name}` (params schema) | (c) |
| 3 | Device editor and new-device wizard | device editor (all fields, params as JSON) | no | `PUT/PATCH/DELETE /devices/{id}`, `POST /devices/test`, `GET /project/validate` | (c) |
| 4 | Tag editor and tag table | inline table, *Add a tag*, `scale` as JSON | no | `GET/POST/PUT/DELETE …/tags`, `POST …/tags/validate`, `GET /drivers/{d}/address-help`, `…/tags:generate` | (c) |
| 5 | Quick Client: live table with quality and timestamps | `#/quick/<id>` over `/api/v1/ws`, REST fallback; value, quality, source time, update age | REST fallback only (no WebSocket) | `GET /api/v1/ws`; today `…/values` + `system/tags` | (b) |
| 6 | Device and communication diagnostics | device page: status, plan, identity, Read once | status and Read once; no plan or identity | `…/plan`, `…/identity`, counters on `…/status`, `GET /events` | (d) |
| 8 | Event log | none | no | `GET /api/v1/events` (filters, cursor), live events | (d) |
| — | System tags | listed under the device values with `include_system` | `GET /api/v1/system/tags` works; `include_system` on values is on the legacy route only | `system/tags`, `values?include_system=true` | (a), (b) |
| 14 | Settings, revisions, export | config page: Export, Reload | Reload only | `GET /project`, `/project/revisions`, `/project/status` | (c) |
| 13 | Connection profiles, health, tray | desktop launcher (done, #87) | yes (`/health`, `whoami`, `/channels`) | — | (e) |
| 7, 9–12 | North agents, import, users, certificates, node status | import done; the rest not started | no | see `docs/ui/screens.md` | later |

Not copied from KEPServerEX, on purpose: writes from the Quick Client (UI-17, ADR-0008), COM/DCOM
activation, OPC DA/DDE, a second desktop UI.

## Build order

One small PR per step, each with a Playwright run against a live engine on `config-sim` and 2x
screenshots in its description. Chromium and Playwright come with the build container; no JS test
framework is added (the checks are plain `node` scripts that `require('playwright')`, and need no
new dependency or licence entry).

- **(a) Project tree and read-only properties panel.** A left tree (Channels › Devices › Tags, plus
  `_System`) filled from `/api/v1/status`, `…/status` and `…/values`, with search and counts, and a
  right panel with the properties of the selected node: channel (name, device count, states), device
  (driver, host, port, enabled, tags, blocks, topic, sub-device id, state, last good, errors, cycle
  time, last error and reason, plan problems), tag (name, value, quality, source time). Works on
  the routes that exist and degrades visibly where a property needs a missing route ("available when
  the configuration API lands", no error banner). Adds the Rust i18n test and the bundle `FILES`
  entries.
- **(b) Quick Client live values.** The live table over `/api/v1/ws` when the engine serves it, and
  REST polling now: every tag of the selected branch with value, quality, source timestamp and update
  age, an update-rate selector (250 ms to 10 s, client side), a quality filter, sort, and system tags
  on request. Read-only.
- **(c) Device and tag editors.** Needs the configuration write routes (core owns them, #140). The
  channel and device wizards (summary and JSON before Finish, nothing written earlier), the tag
  properties dialog with address help and per-field errors, multiple tag generation. Not started
  without the lead's go-ahead.
- **(d) Diagnostics and event log.** Device diagnostics (plan, identity, counters) and the event log
  with filters and live tail, once `…/plan`, `…/identity` and `/events` exist.
- **(e) Desktop launcher polish.** Tray menu, start-on-login, engine sidecar and "Open last project"
  in `desktop/` (the engine sidecar is #58).

## Gaps that need engine API work

For the lead to route to the core worker. Issue numbers are those already filed in
`docs/ui/screens.md#missing-api-endpoints`.

| # | Missing in the Rust engine | Blocks | Filed |
|---|---|---|---|
| G1 | `GET /api/v1/devices/{id}/tags` (name, address, dtype, scan rate, scaling, enabled, group) and `GET /api/v1/devices/{id}` | tag properties in the tree (a, last panel rows), tag editor | #140 |
| G2 | `GET /api/v1/channels`, `/channels/{c}`, `/devices` (lists with `q`, `sort`, `limit`, `offset`) | channel properties, tree at 250k tags | #140, #129 |
| G3 | `GET /api/v1/devices/{id}/plan` and `/identity` | diagnostics (d) | #140 |
| G4 | `GET /api/v1/ws` (live values, subscription cap, error frames) | Quick Client push updates (b) | #164 |
| G5 | `GET /api/v1/browse?path=&depth=&include_system=` (one level at a time) | the tree at 250k tags without loading it all | #139 |
| G6 | `GET /api/v1/events` with filters and cursor, plus live events | event log (d) | #136 |
| G7 | Configuration write routes: channels, devices, tags (`PUT/POST/PATCH/DELETE`, `If-Match`), `POST /api/v1/reload` with a changed project, `GET /project/validate`, `POST /devices/test` | editors and wizards (c) | #140, #132 |
| G8 | `GET /api/v1/drivers`, `/drivers/{name}` (params schema and address help) | wizard parameter forms, address help | #129, #139 |
| G9 | `GET /api/v1/project`, `/project/revisions`, `/project/status`, `/audit` | settings, config page, audit page | #37, #140 |
| G10 | `include_system` on `…/values`, and `dtype` on `…/values` entries | one values call per device for the Quick Client | #127 |
| G11 | Per-device counters on `…/status` (requests by result, overruns, since start) | diagnostics (d) | unfiled (DIAG-10) |

Steps (a) and (b) need none of G1–G11: they use the routes in the table above. G1 and G10 are the
two that most improve them, and neither blocks them.

## Open questions for the lead

1. **Two bundle copies.** Confirm `engine/ui/` as the only edited copy, with `src/opencap/api/ui/`
   left as the frozen Python engine's (this page assumes so).
2. **Step (c).** Waiting for the configuration write routes (G7) before starting it, as agreed.
