# 10 Configuration API

> Source: KEPServerEX V6 server manual (© PTC), pages 175–220, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
KEPServerEX ships a separate Windows service that exposes the project over a REST interface (manual p. 175).
Clients authenticate with HTTP Basic credentials from the server's User Manager, over HTTPS. Endpoints sit
under `/config/v1/...` and follow the project tree: project, channels, devices, tags and nested tag groups,
plug-in namespaces, server administration (users, groups, UA endpoints), logs, documentation, health and
product information (p. 176–179). A numeric project ID guards against stale edits, with a force flag to skip
it (p. 179, 195–196). GET requests can ask for different "content" (properties, property definitions, property
states, type definition, children) and can filter, sort and page collections (p. 182–191). Objects are
created, read, edited and deleted with POST/GET/PUT/DELETE, also several at once and with child hierarchies
(p. 193–207). Users, groups and per-object-type project permissions are managed the same way (p. 207–214).
Long actions (tag generation, project load/save, runtime reinitialisation) run as asynchronous services that
return a job to poll (p. 214–219). The section ends with the list of response codes and the device demand
poll mechanism (p. 219–220).

## opencap today
- **Python engine (reference):** a configuration REST API under `/api/v1`, merged in #63 (closes #75),
  decisions D-039 to D-044. Routes in `src/opencap/api/config_api.py`; the read-modify-write layer is
  `src/opencap/config/store.py` (`ConfigStore`); app wiring, open routes and error handlers in
  `src/opencap/api/server.py`; tokens and roles in `src/opencap/api/auth.py`; auth-before-body and body
  caps in `src/opencap/api/guard.py`; audit log in `src/opencap/api/audit.py`; pending-reload tracking in
  `src/opencap/api/context.py`. Contract: `docs/architecture/03-deep-dive.md` §3.6.
  - Resources: `whoami`, `auth-info` (open), `status`; channels, devices and tags CRUD; `map…`
    (`devices_map.yaml`) and `overrides…` (`overrides.yaml`); `project` (export of the four files),
    `project/import` (KEPServerEX `project.json` or an opencap export, dry run by default),
    `project/validate` (`opencap check` as JSON), `project/schema` (JSON Schemas of the models),
    `project/status`, `project/pending`, `project/revisions`; `audit`; `reload`; per device `status`,
    `values`, `plan`, `identity`, `POST read-once`.
  - Reads (lists included) return `revision` in the body and as `ETag`. Every write needs `If-Match`:
    428 without, 412 when stale, 409 for name clashes. `PUT` replaces, `PATCH` is an RFC 7386 merge patch.
    A write changes files only; `POST /api/v1/reload` or `?apply=true` applies (deviation, D-039).
  - Every change is validated by its model, the real loader (staged) and the driver plan (422 with
    field locations); multi-file writes are atomic with rollback, previous version kept as `<file>.bak`,
    YAML comments kept (D-041); every accepted change, import and reload is audited inside the store lock,
    and a change whose audit entry cannot be written is rolled back (D-043).
  - Auth: named bearer tokens with roles `viewer` (GET/HEAD) and `admin` from `poller.yaml` `api.tokens`;
    with no token the API is read-only and answers only loopback `Host` names; auth happens before the body
    is read; bodies are capped at 1 MiB (64 MiB for import) (D-040).
  - Verified by `tests/unit/test_config_api.py` (roles on every endpoint, open mode, If-Match, validation,
    conflicts, lifecycle, import, audit, read-only storage, audit-failure rollback) and
    `tests/unit/test_config_store.py`.
- **Rust engine:** `engine/crates/opencap-api/src/lib.rs` has only the legacy unversioned runtime routes
  (`/health`, `/metrics`, `/channels`, `/devices/{id}`, `/devices/{id}/values`, `POST …/read-once`,
  `POST /reload`) with the same named tokens, roles, read-only open mode, auth before body and body cap
  (#84, closes #82; D-062). No `/api/v1` config API, no audit log; `api.audit_log` and `api.cors_origins`
  are parsed and unused. `tests/integration/test_rust_engine_api.py` runs the auth checks against both engines.
  RFC 0001 §7 puts Rust config API parity at M3, gated on the OpenAPI contract.
- **UI:** the web UI (`/ui/`, D-044, ADR-0010) uses only `/api/v1`.
- **Open work on the board:** OpenAPI contract #32, Quick Client / WebSocket feed #31, staged import wizard
  #33, project file v2 #37 (ADR-0013: `ProjectStore` behind the API), security RFC #21, write safety
  RFC #19.

## Features

### API-01 Service architecture (manual p. 175)
- **What:** KEPServerEX runs the REST interface as its own Windows service next to the runtime, started and
  stopped through the service manager.
- **Decision:** Adapt: the API is served by the engine process itself, on the same port as the UI and the
  monitoring routes (`api.bind`, `api.port`), so it starts and stops with the engine (systemd unit, Windows
  service, container). One process means one config lock shared by writes and `POST /reload` (D-041).
- **Status:** Done
- **Requirements:**
  - The API listens on `api.bind:api.port` from `poller.yaml`; there is no second process or port.
  - Without a config directory the configuration routes answer 503 and the runtime routes keep working.
- **Acceptance:** `test_config_api_without_config_dir_is_503` (Python); both engines' start-up tests in
  `tests/integration/test_rust_engine_api.py`.

### API-02 Authentication and roles (manual p. 175, 193, 207)
- **What:** Basic authentication with User Manager accounts; an initial Administrator; only one writer at a
  time while the desktop configuration client is connected.
- **Decision:** Adapt: named bearer tokens (`api.tokens: [{name, role, token_env}]`, secret in the env var)
  with roles `viewer` (GET/HEAD) and `admin` (all methods); the legacy `api.token_env` token is the admin
  `default`. With no token: loopback bind only, read-only, loopback `Host` names only (DNS rebinding).
  Authentication runs before the body is read. No exclusive editing session: concurrency is handled by
  revisions (API-10). Full users and groups wait for #21 (API-26). D-019, D-040, D-062, FR-11.
- **Status:** Done
- **Requirements:**
  - A request without a valid bearer token on any non-open route gets 401 with `WWW-Authenticate: Bearer`,
    before any body byte is read; unknown paths also need a token (401 before 404).
  - A viewer token on any unsafe method gets 403 naming the role; an admin token is accepted.
  - With no token configured, a non-loopback bind stops the start; unsafe methods get 403; requests whose
    `Host` is not `127.0.0.1`, `localhost` or `[::1]` get 403.
  - A listed token whose env var is unset, or two tokens sharing one secret, stop the start.
  - Bodies over 1 MiB (64 MiB for `POST /api/v1/project/import`) get 413.
- **Acceptance:** `test_roles_on_every_endpoint`, `test_open_mode_is_read_only_and_answers_only_localhost`,
  `test_unauthenticated_requests_are_refused_before_the_body_is_read`, `test_body_size_cap`
  (`tests/unit/test_config_api.py`); the same checks on both engines in
  `tests/integration/test_rust_engine_api.py`; once #32 lands, a test that every route in the OpenAPI
  document declares its role and is exercised by the role test.

### API-03 HTTPS transport (manual p. 175)
- **What:** the REST service only speaks HTTPS.
- **Decision:** Defer to the security RFC (#21): today TLS is terminated by a reverse proxy and the API
  binds to loopback or a VPN address (03 §3.7, RFC 0001 §5 "Security defaults"). The RFC decides between
  built-in TLS and documented proxy recipes.
- **Status:** Planned (#21)
- **Requirements:**
  - Either `api.tls` (certificate and key file paths, never inline secrets) serves HTTPS in both engines,
    or `docs/deployment.md` carries a tested reverse-proxy recipe; #21 picks one.
  - A non-loopback bind without TLS is logged at start as a warning naming the bind address.
- **Acceptance:** an integration test that starts each engine with a self-signed certificate (or behind the
  proxy recipe) and completes an authenticated `GET /api/v1/whoami` over TLS.

### API-04 Credential strength and failed-login lockout (manual p. 175, 207, 213–214)
- **What:** password length rules for the Administrator and users, and a temporary account lockout after
  repeated wrong passwords.
- **Decision:** Defer to #21: opencap uses random bearer tokens, not passwords, so the rules become a minimum
  token length and throttling of failed attempts per client address. A per-account lockout is not adopted
  (an attacker could lock out the admin).
- **Status:** Planned (#21)
- **Requirements:**
  - A configured token shorter than the minimum chosen by #21 stops the start with a message naming the
    token, in both engines.
  - After N failed authentications from one client address within a window (values from #21), further
    requests from it get 429 for the rest of the window; successful requests from other addresses are
    unaffected.
- **Acceptance:** unit tests in both engines (short token refused; throttling with a fake clock).

### API-05 Documentation endpoint and OpenAPI contract (manual p. 175–177, 182, 193)
- **What:** a landing page and a `/config/v1/doc` tree describing every endpoint, its properties, child
  collections and parameters, as HTML or JSON.
- **Decision:** Adapt: a generated OpenAPI document, committed to the repository and drift-tested, is the
  documentation and the contract both engines are tested against (03 §3.6, #32). JSON Schemas of the models
  are already served by `GET /api/v1/project/schema`. FastAPI's own docs routes stay off
  (`docs_url=None`, `openapi_url=None` today); the committed document is served instead.
- **Status:** Planned (#32)
- **Requirements:**
  - The OpenAPI document is committed (path chosen by #32) and generated from the Python routes; CI fails
    when the generated document differs from the committed one.
  - `GET /api/v1/openapi.json` (viewer) returns the committed document, in both engines once Rust has the
    config API.
  - Every route lists its success and error status codes (API-32) and the role it needs (API-02).
- **Acceptance:** OpenAPI drift test in CI; a test that walks the document and calls every route on both
  engines with the documented role, checking the status code is one the document lists.

### API-06 Endpoint layout and versioning (manual p. 176–177)
- **What:** a deep path per object (`/config/v1/project/channels/{c}/devices/{d}/tag_groups/{g}/tags/{t}`),
  plus admin, log, doc, status and about roots.
- **Decision:** Adapt: flat resources under `/api/v1` with device ids `<channel>/<device>`
  (`/api/v1/devices/{channel}/{device}/tags/{tag}`); the version is in the path; the unversioned runtime
  routes stay for existing clients (D-039). Devices may not be named `tags`, `values`, `plan`, `identity`,
  `status` or `read-once`, and names may not contain `/`.
- **Status:** Done
- **Requirements:**
  - Every configuration route lives under `/api/v1`; a breaking change needs `/api/v2` alongside v1.
  - Creating or renaming a device to a reserved name, or any name containing `/`, gets 422 with the field.
  - Unknown ids get 404 with the id in `detail`.
- **Acceptance:** `test_device_and_tag_reads`, `test_validation_errors_are_422_with_locations`
  (`tests/unit/test_config_api.py`); route list checked by the OpenAPI drift test once #32 lands.

### API-07 Object names in URLs (manual p. 193)
- **What:** names with spaces or reserved characters must be percent-encoded; the server silently trims
  leading and trailing spaces, so the stored name can differ from the one sent.
- **Decision:** Adapt: percent-decoding of path segments already works (device ids with spaces, README
  examples). opencap never alters a name silently: a name with leading or trailing whitespace is refused.
- **Status:** Not started
- **Requirements:**
  - POST or PATCH/PUT with a channel, device or tag name that has leading or trailing whitespace gets 422
    with `loc` on the name field; nothing is written.
  - A name containing `%`, `#`, `?`, `&` or `+` round-trips: create it, then GET it through its
    percent-encoded path, and the response carries the exact name.
- **Acceptance:** unit tests in `tests/unit/test_config_api.py`; the same cases on the Rust engine when
  API-35 lands.

### API-08 Health status (manual p. 177–178)
- **What:** an unauthenticated endpoint saying whether the REST service runs.
- **Decision:** Adapt: `GET /health` is open on both engines and reports status, version, instance, MQTT
  link, outbox size and device counts by state; `GET /api/v1/status` (viewer) is the dashboard in one call.
  Separate liveness and readiness routes are in the board backlog and belong to plan 12.
- **Status:** Done
- **Requirements:**
  - `GET /health` answers 200 without a token on both engines with at least `status` and `version`.
  - `GET /api/v1/status` needs a token and returns `health`, `channels` and `config` (pending reload).
- **Acceptance:** `test_open_endpoints`, `test_runtime_status` (`tests/unit/test_config_api.py`);
  `test_bearer_token_and_open_monitoring_routes` on the Rust binary (`tests/integration/test_rust_engine_api.py`);
  make that check dual-engine when API-09 adds its test.

### API-09 About: product information (manual p. 178–179)
- **What:** an unauthenticated endpoint with product id, name and version split into parts.
- **Decision:** Adapt: `GET /api/v1/about`, open like `/health`, so tools can tell which engine and API they
  talk to before they hold a token. `/health` already exposes `version`, so nothing new is disclosed.
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/about` answers 200 without a token on both engines with `product` (`opencap`), `version`,
    `version_major`, `version_minor`, `version_patch`, `engine` (`python` or `rust`), `api_versions`
    (`["v1"]`) and the config schema version.
  - The route is in the open-path list of both guards and in the OpenAPI document.
- **Acceptance:** a dual-engine test in `tests/integration/test_rust_engine_api.py` comparing every field
  except `engine`.

### API-10 Optimistic concurrency (manual p. 179, 195–196, 198–199)
- **What:** a numeric project ID returned on reads and changed by every edit; edits must send it; the new ID
  comes back in a response header.
- **Decision:** Adapt: the config `revision` is a hash of the four config files' bytes, so hand edits change
  it too (D-039). It is in every read body (lists included) and in `ETag`; every write needs `If-Match`
  (428 without, 412 when stale) and returns the new revision in the body and `ETag`. Unlike KEPServerEX, the
  precondition applies to deletes and creates as well, and there is no exclusive editing session.
- **Status:** Done
- **Requirements:**
  - Every GET under `/api/v1` that reads config returns `revision` and `ETag: "<revision>"`.
  - Every config write without `If-Match` gets 428 and changes nothing; with a stale revision gets 412 and
    changes nothing; two writes with the same revision: the second gets 412.
  - A successful write returns `ok`, `revision`, `files`, `changes`, `warnings` and `pending_reload`.
- **Acceptance:** `test_writes_need_if_match`, `test_project_status_and_etag`,
  `test_change_response_audit_and_pending_reload` (`tests/unit/test_config_api.py`); RFC 0001 §7 requires
  the 412 test on both engines (API-35).

### API-11 Force update flag (manual p. 179, 196)
- **What:** a body flag that makes the server ignore the project ID, for single-client setups.
- **Decision:** Out of scope: it brings back silent overwrites, which D-039 exists to prevent. A client that
  wants last-writer-wins reads `GET /api/v1/project/status` and sends that revision.
- **Status:** Out of scope

### API-12 Event log retrieval (manual p. 179–181)
- **What:** reading the server event log over REST, filtered by event type and time range, limited, sorted
  by a property and paged.
- **Decision:** Defer: opencap has no event log yet (structured JSON logs go to journald or Docker); the
  event log is FR-10 and is planned in plan 12 and plan 15 (M5). When it exists, its route follows the
  filter and paging rules of API-17 and API-18.
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/events` (viewer) returns events newest first with `severity`, `since`, `until` (UTC
    ISO 8601), `limit` (default 100) and `offset`; several severities can be given comma-separated.
  - `sort` and `order=asc|desc` work on `ts`, `severity`, `source`, `event`.
  - Unknown parameters or bad timestamps get 422 with the parameter as `loc`.
- **Acceptance:** unit tests on a fixture log; dual-engine test once both engines write the event log;
  OpenAPI drift test.

### API-13 Transaction log of configuration changes (manual p. 179, 181)
- **What:** a log of every request the REST service received, with an optional verbose mode that records
  request and response bodies, readable over REST, denied to non-administrators by default.
- **Decision:** Adapt: the audit log records every accepted change, import and reload with time, token name,
  role, `via` (`ui` or `api`), action, target, new revision, files and field-level old/new values (at most
  200 per entry; restoring an export also records a unified diff per YAML file), which replaces the
  verbose mode. It is readable with
  `GET /api/v1/audit` (filters `action`, `who`, `target`, paging with `limit`/`offset`) and
  `GET /api/v1/project/revisions`. Viewers may read it because config files hold no secrets (NFR-11).
  Reads and rejected requests are not in it (D-043); see API-14. Audit retention settings belong to plan 09.
- **Status:** Done
- **Requirements:**
  - Each accepted change appends exactly one JSON line before the response is sent; if the line cannot be
    written the change is rolled back and the request gets 503.
  - `GET /api/v1/audit` returns newest first; `action` matches a prefix; `limit` is 1–1000.
  - Rejected changes (4xx) and failed reloads add no entry.
- **Acceptance:** `test_change_response_audit_and_pending_reload`, `test_audit_endpoint_filters`,
  `test_reload_failure_keeps_running_and_is_not_audited`, `test_unwritable_audit_log_rolls_the_change_back`
  (`tests/unit/test_config_api.py`), `tests/unit/test_audit.py`; Rust once API-35 lands.

### API-14 Records of refused requests (manual p. 181)
- **What:** because KEPServerEX logs every request, refused ones are visible to an administrator.
- **Decision:** Adapt: refused write attempts and authentication failures become structured log events (not
  audit entries, which stay "what changed"), so brute force or a misconfigured client shows up. Event names
  and severities are set in plan 15.
- **Status:** Not started
- **Requirements:**
  - A 401, 403, 412, 413, 422 or 428 answer emits one structured event with method, path, status, token
    name (or `unknown`), client address and `via`, never the token value or the body.
  - Repeated identical events from one client are rate-limited (at most one per second per client and status,
    with a suppressed count).
- **Acceptance:** unit tests capturing log output in both engines (the 401/403/413 cases first, as Rust has
  those routes today).

### API-15 Content retrieval: response shapes and definitions (manual p. 182–187)
- **What:** collection GETs return arrays of objects, object GETs return the object; a `content` parameter
  asks for property definitions (type, range, length, enumeration, default), property states (allow/enable
  conditions), the type definition (can create/delete/modify, child collections) or children with links.
- **Decision:** Adapt: lists return `{"<collection>": [...], "revision": ...}` and objects return the
  resource plus `revision` (D-039); a channel GET includes its device summaries, a device GET its tags,
  map entry and override. Type and property definitions come from the JSON Schemas at
  `GET /api/v1/project/schema` and from the OpenAPI document (#32), not from a `content` parameter.
  Allow/enable conditions are not modelled: opencap's models have no conditional fields outside driver
  params (API-16).
- **Status:** Done
- **Requirements:**
  - `GET /api/v1/project/schema` returns the JSON Schema of `channel`, `device`, `tag`, `map_entry`,
    `map_defaults` and `device_override`, generated from the same models the loader uses.
  - List responses always use an object with a named array, never a bare array, so fields can be added.
- **Acceptance:** `test_project_status_and_etag`, `test_device_and_tag_reads`
  (`tests/unit/test_config_api.py`); schema equality with `schema/` checked by the conformance tests
  (ADR-0003); OpenAPI drift test once #32 lands.

### API-16 Driver catalogue (manual p. 176–177, 198, 201, 203)
- **What:** per-driver documentation endpoints for channel, device and model properties, which clients use to
  learn what a driver needs.
- **Decision:** Adapt: a read-only catalogue of opencap drivers with their `params` schema, address syntax
  examples and supported dtypes, generated from the drivers themselves (today `params` is an open object and
  the rules are in `docs/driver_params.md`; `/project/status` lists only driver names).
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/drivers` (viewer) lists every implemented driver with `name`, `transport` and whether it
    supports identity and browse.
  - `GET /api/v1/drivers/{name}` returns a JSON Schema for `params`, the dtypes it reads and address
    examples; an unknown driver gets 404.
  - A device write whose `params` violate the driver schema gets 422 with `loc` under `params`.
- **Acceptance:** unit test that every driver in the registry appears and that each schema validates the
  conformance fixtures in `tests/conformance/config/`; dual-engine test once Rust has the route.

### API-17 Filtering and sorting collections (manual p. 188)
- **What:** a substring filter on name or description and sorting by any property, ascending or descending.
- **Decision:** Adapt: query parameters on the list routes: `q` (case-insensitive substring of name and
  description), plus `sort` and `order=asc|desc` on documented fields. `GET /api/v1/devices?channel=` and the
  audit filters already exist. Large devices (thousands of Logix tags) make this matter for the UI.
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/channels`, `/devices` and `/devices/{id}/tags` accept `q`; only matching items are returned,
    and `revision` is still the whole config's revision.
  - `sort` on a field not listed for that collection gets 422; string sorts are natural (`A2` before `A10`),
    numeric sorts are numeric.
- **Acceptance:** unit tests on the fixture project; OpenAPI drift test lists the parameters; dual-engine
  once API-35 lands.

### API-18 Paging collections (manual p. 180–181, 188–191)
- **What:** `pageNumber` and `pageSize` parameters; paging metadata appended as an extra element inside the
  returned array.
- **Decision:** Adapt: `limit` and `offset`, as `/api/v1/audit` already does, with `total` next to the
  list in the response object (no metadata mixed into the array). Without paging parameters the whole
  collection is returned, as today.
- **Status:** Not started
- **Requirements:**
  - The list routes of API-17 accept `limit` (1–10000) and `offset` (≥ 0) and return `total`, `limit` and
    `offset` alongside the list; out-of-range values get 422.
  - Paging is applied after filtering and sorting; an empty page returns an empty list with `total`.
- **Acceptance:** unit tests (first, middle, last, empty page); OpenAPI drift test.

### API-19 Localised responses (manual p. 188)
- **What:** the client picks the language of display names and messages with `Accept-Language`.
- **Decision:** Out of scope: the API returns stable machine-readable fields (`loc`, `type`, status codes)
  with English messages; translation, if ever needed, belongs to the UI.
- **Status:** Out of scope

### API-20 Server (node) settings through the API (manual p. 191–193)
- **What:** an admin endpoint to read and change server-wide settings (log persistence, size and retention
  for the event log, diagnostics and the API transaction log) independent of the project.
- **Decision:** Adapt: node settings are `poller.yaml` today (`node.yaml` in project v2, ADR-0013, #37). Today
  they change only by hand or by importing a whole export (D-042). Add a node-settings resource that reports
  which changes need a process restart (`RESTART_FIELDS` in `context.py`). Which settings exist is plan 09's
  and plan 12's business.
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/node` (viewer) returns the node settings with `revision`; `PATCH /api/v1/node` (admin,
    `If-Match`) merge-patches them with comments kept (D-041), audited like any change.
  - The response lists `restart_needed` fields; the `api` section (tokens, bind, port) cannot be changed
    through this route (422), so a client cannot lock itself out.
- **Acceptance:** unit tests in `tests/unit/test_config_api.py` (patch, restart_needed, `api` refused,
  audit entry); dual-engine once API-35 lands.

### API-21 Creating objects: single, several, with children (manual p. 193–195)
- **What:** POST creates one object with defaults for omitted properties, an array creates several (207
  with a status per object when some fail), and a POST may carry the object's whole child hierarchy.
- **Decision:** Adapt: POST creates a channel (optionally with devices and their tags), a device (optionally
  with tags) or one or more tags (a list). A request is **all or nothing**: any error is one 422 that lists
  every problem with its index in `loc`, so no 207 partial success. Defaults come from the models.
- **Status:** Done
- **Requirements:**
  - `POST /api/v1/devices/{id}/tags` with a list creates all tags or none; duplicate names inside the list or
    clashes with existing tags give 422/409 and nothing is written.
  - `POST /api/v1/channels` with nested `devices[].tags[]` creates the whole tree in one revision and one
    audit entry.
  - Successful creates answer 201 with the created object under `data`.
- **Acceptance:** `test_channel_device_tag_lifecycle`, `test_conflicts`,
  `test_validation_errors_are_422_with_locations` (`tests/unit/test_config_api.py`).

### API-22 Reading, editing and deleting; error format (manual p. 195–197)
- **What:** GET returns every property; PUT edits named properties and may report properties it did not
  apply (read-only, disallowed, restricted by a connected client, rounded); DELETE removes an object and its
  subtree but never several siblings at once; errors are JSON with a code and message.
- **Decision:** Adapt: `PUT` replaces, `PATCH` merges (RFC 7386, `null` removes a key). opencap never applies
  part of an edit: an unknown, read-only or invalid field is a 422 with its `loc`, and the response's
  `changes` lists exactly what changed. Deletes cascade to tags, the map entry and overrides, and the audit
  entry keeps what was removed (D-039). Changes take effect at reload, so there are no "client-restricted"
  properties. Errors are `{"detail": ...}` with `loc`/`msg`/`type` items for validation.
- **Status:** Done
- **Requirements:**
  - `PATCH` with an unknown field gets 422 with that field in `loc`; nothing is written.
  - `DELETE` of a device removes its `devices_map.yaml` entry and override in the same revision.
  - Renaming a channel or device carries its map and override keys along.
- **Acceptance:** `test_validation_errors_are_422_with_locations`, `test_channel_device_tag_lifecycle`,
  `test_map_and_overrides_endpoints` (`tests/unit/test_config_api.py`); fault-injection tests in
  `tests/unit/test_config_store.py`.

### API-23 Channel, device and tag CRUD (manual p. 197–205)
- **What:** worked examples for creating, updating (including rename, which moves the URL) and removing
  channels, devices and tags, with KEPServerEX's `common.*` and `servermain.*` property names.
- **Decision:** Adapt: same operations on `/api/v1/channels`, `/api/v1/devices/{id}` and
  `/api/v1/devices/{id}/tags[/{tag}]` with opencap's own field names (`name`, `driver`, `host`, `port`,
  `scan_ms`, `timeout_ms`, `retries`, `address`, `dtype`, …). Every change is checked by the model, a staged
  loader run and the device's driver plan before it is written (D-039). The KEPServerEX names are mapped by
  the importer, not by the API (see API-34).
- **Status:** Done
- **Requirements:**
  - Channels: GET/POST list, GET/PATCH/DELETE one (PATCH only `name` and `enabled`).
  - Devices: GET list (optionally `?channel=`), POST under a channel, GET/PUT/PATCH/DELETE one.
  - Tags: GET/POST list, GET/PUT/PATCH/DELETE one; a tag address the driver cannot plan gets 422 on `address`.
- **Acceptance:** `test_roles_on_every_endpoint`, `test_channel_device_tag_lifecycle`,
  `test_validation_errors_are_422_with_locations` (`tests/unit/test_config_api.py`).

### API-24 Tag groups (manual p. 203, 205–207)
- **What:** tag groups as objects under a device, nestable, with their own tags, local and total tag counts,
  and create, rename and delete (deleting a group deletes its subtree).
- **Decision:** Adapt: opencap stores a tag's group path in its dot-joined name (`LTI002.Pv`, `models.py`),
  and the OPC UA address space shows the groups as folders (03 §3.5). The API exposes groups derived from
  those names; renaming or deleting a group is one atomic change over all its tags. Whether groups become
  real objects is decided by plan 06 and project file v2 (#37).
- **Status:** Not started
- **Requirements:**
  - `GET /api/v1/devices/{id}/groups` (viewer) lists group paths with local and total tag counts.
  - `PATCH /api/v1/devices/{id}/groups/{path}` with `{"name": ...}` renames the prefix of every tag under it
    (tag overrides follow); `DELETE` removes them; both need `If-Match`, write one revision and one audit
    entry, and 409 on a name clash.
- **Acceptance:** unit tests on a fixture with nested groups; OpenAPI drift test.

### API-25 Plug-in endpoints: north agents and plug-ins (manual p. 177)
- **What:** plug-ins add their own namespaces and collections under the project endpoint.
- **Decision:** Defer: 03 §3.6 reserves `/api/v1/north` and `/api/v1/plugins` for north agents and
  processing plug-ins (ADR-0005); they arrive when those are configurable objects (project file v2, #37, and
  the plug-in specs). FR-9 lists north agents and plug-ins in the CRUD scope.
- **Status:** Not started
- **Requirements:**
  - Each north agent kind and plug-in gets CRUD under `/api/v1/north/{kind}/{name}` or
    `/api/v1/plugins/{kind}/{name}` with the same revision, If-Match, validation and audit rules as channels.
  - Secrets in agent settings are referenced by env var name only; a literal secret field gets 422.
- **Acceptance:** unit tests per agent kind; OpenAPI drift test; dual-engine once API-35 lands.

### API-26 Users and user groups (manual p. 207–211, 213–214)
- **What:** CRUD for server users (group, enabled, password) and user groups (enabled, data access, server
  permissions), admin-only, without a project ID; built-in groups and users cannot be renamed or disabled.
- **Decision:** Defer to the security RFC (#21): today tokens live in `poller.yaml` and change with a
  restart (D-040); RFC 0001 §3.4 plans argon2 for stored credentials. The I/O and system tag *write*
  permissions are out of scope (ADR-0008). Dynamic addressing and namespace browse permissions belong to
  plan 02 and plan 09.
- **Status:** Planned (#21)
- **Requirements:**
  - Admin-only CRUD for users and groups under `/api/v1/admin/…` (paths set by #21); viewers get 403.
  - Secrets are never returned by any GET; stored credentials are hashed.
  - Removing or disabling the last admin gets 409; every change is audited.
- **Acceptance:** role tests for every new route (RFC 0001 §7 "role tests for every API route"); audit
  entries checked; dual-engine once API-35 lands.

### API-27 Group permissions per object type (manual p. 209, 212–214)
- **What:** each user group has fixed per-object-type permissions (add, edit, delete for channels, devices,
  tags, tag groups, aliases, …) and server permissions (replace project, reset logs, read the API log).
- **Decision:** Defer to #21: the target is the role set viewer / operator / engineer / admin (03 §3.7,
  FR-11); per-type permissions are a refinement #21 may adopt. Any permission that would allow writing tag
  values is out of scope (ADR-0008, #19).
- **Status:** Planned (#21)
- **Requirements:**
  - Each `/api/v1` route declares the permission it needs (in code and in the OpenAPI document); a token
    without it gets 403 naming the permission.
  - Built-in admin permissions cannot be reduced (409).
- **Acceptance:** a generated test that calls every route with each role and compares the result with the
  permission table; OpenAPI drift test.

### API-28 Asynchronous services and jobs (manual p. 214–215)
- **What:** actions beyond CRUD are services under the object; a PUT starts a job (202 with its link, or 429
  when too many run); the job is polled for completion and error, and is deleted after a time-to-live.
- **Decision:** Defer: today's actions (`reload`, `read-once`, `import`) are synchronous and short enough.
  The job pattern is adopted with the first long action, tag browse (API-29); then `read-once` on slow
  devices (D-040 notes ≈23 s on a non-batching Micro800) may move to it.
- **Status:** Not started
- **Requirements:**
  - A job-starting `POST` answers 202 with `Location: /api/v1/jobs/{id}` and a body with the same link;
    when the per-action concurrency limit is reached it answers 429 with `Retry-After`.
  - `GET /api/v1/jobs/{id}` returns `state` (`running`, `done`, `failed`), `error` and `result`; a job is
    kept for a `ttl_s` chosen at start (default 30, maximum 300) after it finishes, then 404.
  - Jobs are in memory, need the same role as the action, and the action is audited when it changes config.
- **Acceptance:** unit tests with a fake long action and a fake clock; dual-engine once both have jobs.

### API-29 Automatic tag generation (manual p. 200–201, 215–216)
- **What:** a service on a device that builds tags from what the controller reports, using the device's
  tag generation settings.
- **Decision:** Defer: it maps to the driver contract's `browse()` (ADR-0004), which is a PLC read and in
  scope. Logix browse is #11; other drivers follow plan 05 and plan 14. The result is a proposal: applying it
  is an ordinary tag create with `If-Match` and audit, dry run first like import.
- **Status:** Planned (#11)
- **Requirements:**
  - `POST /api/v1/devices/{id}/browse` (admin) starts a job (API-28) whose result lists proposed tags and
    their differences from the current tags; it never writes config by itself.
  - Browse uses the device's existing connection rules and the one-request-on-the-wire rule (D-015).
  - A driver without browse answers 409 naming the driver.
- **Acceptance:** simulator test with `cip_sim.py` (ADR-0014) for Logix; unit test for the 409.

### API-30 Project load and save (manual p. 216–218)
- **What:** services that load a project file from a server path (with a password for encrypted files) and
  save the project to a path under the application data folder.
- **Decision:** Adapt: load = `POST /api/v1/project/import` with the file as the JSON body (KEPServerEX
  `project.json` or an opencap export), dry run by default, `?dry_run=false` with `If-Match` to write
  (D-042); save = `GET /api/v1/project[?download=true]` to the client. Reading or writing arbitrary server
  paths is deliberately not offered. Encrypted and binary project formats are not read (FR-9). The staged
  import wizard is #33.
- **Status:** Done
- **Requirements:**
  - Import without `dry_run=false` writes nothing and returns the diff; with it, `If-Match` is required.
  - An export imported back restores the files byte for byte.
  - A body that is not JSON, or neither format, gets 422.
- **Acceptance:** `test_export_and_import_round_trip`, `test_import_kepserver_dry_run_then_apply`,
  `test_import_rejects_unknown_documents`, `test_non_json_body_is_refused` (`tests/unit/test_config_api.py`).

### API-31 Reinitialise the runtime (manual p. 218–219)
- **What:** a service that reinitialises the server runtime.
- **Decision:** Adapt: `POST /api/v1/reload` (admin) re-reads the config and restarts only the channels that
  changed; on a bad config it keeps the old one and answers 400. It is synchronous, takes the store lock and
  is audited. Settings that need a process restart are listed by `GET /api/v1/project/pending`.
- **Status:** Done
- **Requirements:**
  - Reload returns `started`, `restarted`, `stopped`, `unchanged` channel lists; a failure changes nothing
    and adds no audit entry.
  - A reload never runs while a config write holds the lock.
- **Acceptance:** `test_change_response_audit_and_pending_reload`, `test_reload_failure_keeps_running_and_is_not_audited`,
  `test_reload_waits_for_a_config_write_in_progress` (`tests/unit/test_config_api.py`);
  `test_reload_restarts_only_the_changed_channels` on the Rust binary and `test_named_tokens_and_roles` (reload
  role) on both engines (`tests/integration/test_rust_engine_api.py`).

### API-32 Response codes (manual p. 219)
- **What:** the list of status codes the REST service may return.
- **Decision:** Adapt: opencap answers 200, 201, 400 (reload failed), 401, 403, 404, 409 (name clash or
  read-once not possible), 412, 413, 422 (validation), 428, 500 (partial multi-file write), 503 (storage
  read-only, no config dir, audit unwritable) and 507 (disk full). 202 and 429 come with jobs (API-28) and
  throttling (API-04); 207 is not used (API-21).
- **Status:** Done
- **Requirements:**
  - Every error body is JSON with `detail`; validation errors carry `loc`, `msg` and `type` per item.
  - Storage failures never surface as a bare 500: read-only gives 503 with the path, a full disk 507.
- **Acceptance:** `test_read_only_config_is_reported_and_writes_answer_503`,
  `test_unwritable_audit_log_rolls_the_change_back` (`tests/unit/test_config_api.py`); the codes per route
  are pinned by the OpenAPI drift test once #32 lands.

### API-33 Device demand poll (manual p. 219–220)
- **What:** each device exposes a `_DemandPoll` tag; a client writes it to poll every referenced tag once,
  and further writes fail until the poll finishes.
- **Decision:** Adapt: `POST /api/v1/devices/{id}/read-once` (admin) runs one full cycle of the device now,
  serialized with its polling and sharing the channel's wire lock, and returns the values; it is a read
  (ADR-0008) and works on disabled or demoted devices. Triggering through a client write to a `_DemandPoll`
  system tag is plan 07's decision (north servers are read-only today); demand reads from north clients
  (`allow_demand_reads`, 03 §3.5) are plan 02's; a "demand poll only" scan mode is plan 05's.
- **Status:** Done
- **Requirements:**
  - Read-once needs the admin role (viewer gets 403), answers 404 for an unknown device and 409 when the
    device has no usable driver or plan.
  - Read-once never overlaps a poll of the same device on the wire.
- **Acceptance:** `test_device_plan_identity_and_read_once` (`tests/unit/test_config_api.py`);
  `test_bearer_token_and_open_monitoring_routes` (read-once on the Rust binary,
  `tests/integration/test_rust_engine_api.py`); add a dual-engine viewer-gets-403 check for read-once
  (today only Python's role test covers it).

### API-34 KEPServerEX Configuration API compatibility layer (manual p. 176–177, 197–205)
- **What:** clients and scripts written for `/config/v1/...` with `common.*` / `servermain.*` property names.
- **Decision:** Defer: a translating shim (KEPServerEX paths and property names onto `/api/v1`) would ease
  migration of existing scripts, but only if sites use such scripts. Waits for the owner's answer (open
  questions).
- **Status:** Not started
- **Requirements:**
  - If adopted: a documented subset (channel, device, tag CRUD) under `/config/v1/project/...`, with the
    same auth, roles, If-Match and audit as `/api/v1`; unmapped properties get 422 naming the property.
- **Acceptance:** replay tests of recorded request/response pairs written from the manual's examples (in our
  own fixtures, no copied text) against both engines.

### API-35 Configuration API in the Rust engine (RFC 0001 §7)
- **What:** not a manual feature: the Rust engine must offer the same `/api/v1` so it can become the default.
- **Decision:** Adopt (opencap contract): RFC 0001 §7 requires both engines to pass the same API tests,
  drift-tested against the OpenAPI document (#32), including 412 on stale revisions, plus the audit log
  (the User Manager row notes Rust has none yet).
- **Status:** Not started
- **Requirements:**
  - Every route in the committed OpenAPI document is served by the Rust engine with the same status codes,
    bodies (apart from documented differences in `docs/decisions.md`) and roles.
  - Writes are atomic with `.bak`, audited inside the lock, and keep YAML comments, as in Python (D-041).
- **Acceptance:** `tests/unit/test_config_api.py` made engine-agnostic (or mirrored in
  `tests/integration/`) and run with `OPENCAP_ENGINE=rust` (ADR-0014 §3, #42); OpenAPI drift test.

## Open questions
- Do any sites run scripts or tools against the KEPServerEX Configuration API? That decides API-34 (shim or
  not) and how much of the KEPServerEX property naming the docs should map.
- Built-in TLS or reverse proxy only (API-03), and the minimum token length and throttling values (API-04):
  both for the security RFC (#21).
- Should the audit log stay readable by every viewer (today, D-043), or get its own permission once #21
  defines more roles?
- Are tag groups real objects in project file v2 (#37), or stay a dot path in the tag name (API-24)?
- When jobs arrive (API-28), should `read-once` become a job, or stay synchronous with a timeout?
- `GET /api/v1/about` open without a token (API-09): acceptable, given `/health` already shows the version?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| API-01 | Service architecture (API in the engine process) | Adapt | Done | #63 | M1 |
| API-02 | Authentication and roles (named bearer tokens) | Adapt | Done | #63, #84 | M1 |
| API-03 | HTTPS transport | Defer | Planned (#21) | #21 | M4 |
| API-04 | Credential strength and failed-login throttling | Defer | Planned (#21) | #21 | M5 |
| API-05 | Documentation endpoint and OpenAPI contract | Adapt | Planned (#32) | #32 | M3 |
| API-06 | Endpoint layout and versioning | Adapt | Done | #63 | M1 |
| API-07 | Object names in URLs (no silent trimming) | Adapt | Planned (#112) | #112 | M3 |
| API-08 | Health status | Adapt | Done | #63, #84 | M1 |
| API-09 | About: product information | Adapt | Planned (#123) | #123 | M3 |
| API-10 | Optimistic concurrency (revision, ETag, If-Match) | Adapt | Done | #63 | M1 |
| API-11 | Force update flag | Out of scope | Out of scope | — | — |
| API-12 | Event log retrieval | Defer | Planned (#136) | #136 | M5 |
| API-13 | Transaction log of configuration changes (audit) | Adapt | Done | #63 | M1 |
| API-14 | Records of refused requests | Adapt | Planned (#135) | #135 | M5 |
| API-15 | Content retrieval: response shapes and definitions | Adapt | Done | #63 | M1 |
| API-16 | Driver catalogue | Adapt | Planned (#129) | #129 | M3 |
| API-17 | Filtering and sorting collections | Adapt | Planned (#129) | #129 | M3 |
| API-18 | Paging collections | Adapt | Planned (#129) | #129 | M3 |
| API-19 | Localised responses | Out of scope | Out of scope | — | — |
| API-20 | Server (node) settings through the API | Adapt | Planned (#132) | #132 | M4 |
| API-21 | Creating objects: single, several, with children | Adapt | Done | #63 | M1 |
| API-22 | Reading, editing and deleting; error format | Adapt | Done | #63 | M1 |
| API-23 | Channel, device and tag CRUD | Adapt | Done | #63 | M1 |
| API-24 | Tag groups | Adapt | Planned (#117) | #117 | M4 |
| API-25 | Plug-in endpoints: north agents and plug-ins | Defer | Planned (#132) | #132 | M3 |
| API-26 | Users and user groups | Defer | Planned (#21) | #21 | M5 |
| API-27 | Group permissions per object type | Defer | Planned (#21) | #21 | M5 |
| API-28 | Asynchronous services and jobs | Defer | Planned (#132) | #132 | M3 |
| API-29 | Automatic tag generation (browse) | Defer | Planned (#11) | #11 | M3 |
| API-30 | Project load and save (import, export) | Adapt | Done | #63 | M1 |
| API-31 | Reinitialise the runtime (reload) | Adapt | Done | #63 | M1 |
| API-32 | Response codes | Adapt | Done | #63 | M1 |
| API-33 | Device demand poll (read-once) | Adapt | Done | #63, #84 | M1 |
| API-34 | KEPServerEX Configuration API compatibility layer | Defer | Planned (#141) | #141 | — |
| API-35 | Configuration API in the Rust engine | Adopt | Planned (#140) | #140 | M3 |
