# 09 Administration and security

> Source: KEPServerEX V6 server manual (© PTC), pages 33, 35 and 56–75, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
Administration in KEPServerEX is a Windows tray tool (manual pp. 33, 56–57). Its menu opens the
configuration client, starts, stops and reinitializes the runtime, resets the event log, opens
the OPC UA and OPC .NET configuration managers, the Quick Client, the license utility, help and a
support-information dialog. Its Settings dialog (p. 57) has pages for the tray tool itself,
configuration-client connections (p. 58), the runtime process and runtime options (pp. 58–61),
event-log persistence (pp. 61–62), ProgID redirection for classic OPC clients (pp. 62–63), the
User Manager with groups, users, permission categories, password rules and import/export
(pp. 64–69), the Configuration API service with ports, CORS, TLS, a transaction log and its
certificate (pp. 70–73), a per-feature certificate and trust store (pp. 73–74), and internal
service ports (pp. 74–75). The configuration client can require a login before editing (p. 35).
Security Policies, Local Historian and IoT Gateway add their own Settings pages (p. 57); they
are planned in their plug-in files.

## opencap today
- **Administration surface:** CLI (`opencap import-kep|check|read|identity|run`,
  `opencap-engine run|check|read|identity`), admin API (`/health`, `/metrics`, `/channels`,
  `/devices/{id}…`, `/read-once`, `/reload`) on both engines, configuration API `/api/v1` and web
  UI `/ui/` on the Python engine (#63), desktop launcher with connection profiles
  (#87, #27). Service control through systemd, Docker or WinSW (`deploy/`).
- **Authentication:** named bearer tokens in `poller.yaml` `api.tokens` with roles `viewer` and
  `admin`, secrets only in env vars (D-040, `src/opencap/api/auth.py`); with no token the API is
  read-only and loopback-only (D-040); authentication before the body is read and a body cap
  (`src/opencap/api/guard.py`); the same on Rust (#84, D-062). Users, groups and the
  viewer/operator/engineer/admin roles wait for the security RFC (#21).
- **Audit:** JSON-lines audit log `api.audit_log` (default `data/audit.jsonl`) with who/role/via
  and field-level changes, rotated at 16 MiB, `GET /api/v1/audit` (D-043).
- **CORS:** `api.cors_origins` (Python applies it; Rust accepts and ignores it, D-062).
- **OpenAPI contract:** In review (PR #88, #32); the running server does not serve API docs
  (`openapi_url=None`).
- **Not yet:** built-in TLS for the API, an event log (#56), a certificate store, support bundle,
  per-tag access policies.

## Features

### ADM-01 Administration surface (manual pp. 33, 56–57)
- **What:** A tray tool, optionally started at logon, whose menu reaches every administrative
  action and tool listed above.
- **Decision:** Adapt — no tray tool. Administration is the CLI, the web UI, the desktop launcher
  (ADR-0010) and the OS service manager; each tray item maps to a feature below.
- **Status:** Done (CLI, admin/config API, web UI, #63, #84; desktop launcher, #87).
- **Requirements:**
  - Every administrative action except service start/stop is available through the API, so the UI
    and the desktop app need no other channel.
  - The desktop launcher shows each node's `/health` (version, instance, device states, MQTT)
    and checks the stored token (#87).
- **Acceptance:** UI browser smoke test (`tests/ui/browser_smoke.js`); desktop unit tests (#87).

### ADM-02 Start, stop and reinitialize the runtime (manual p. 56)
- **What:** Start loads the default project; stop drops all clients and saves the project;
  reinitialize drops every client, saves and reloads the project without ending the process.
- **Decision:** Adapt — start and stop belong to systemd, the Windows SCM or Docker;
  reinitialize becomes `POST /api/v1/reload` (or SIGHUP), which restarts only the channels that
  changed and leaves the others connected; nothing is saved at stop (01 RT-13).
- **Status:** Done (reload and SIGHUP on both engines; `?apply=true` on writes).
- **Requirements:**
  - `POST /api/v1/reload` (admin) returns the started, restarted, stopped and unchanged channels;
    an invalid config keeps the old one running (exists).
  - Every reload is in the audit log with the token name (exists, D-043).
  - A full reinitialize that restarts every channel (proposed `POST /api/v1/reload?all=true`,
    admin, audited) for troubleshooting.
- **Acceptance:** existing reload tests on both engines (`tests/integration/test_rust_engine_api.py`);
  a new test for `all=true`.

### ADM-03 Support information (manual pp. 56–57)
- **What:** A dialog that summarises the server and its installed drivers with versions, for
  support cases.
- **Decision:** Adapt — `opencap info` (CLI) and `GET /api/v1/about` (proposed): engine kind and
  version, OS and architecture, toolchain, drivers in this build with their library versions,
  enabled north interfaces, config revision; no addresses, hostnames or secrets, so it can be
  pasted into a public issue (SECURITY.md).
- **Status:** Not started (`GET /health` already reports version and instance).
- **Requirements:**
  - Same keys from both engines.
  - Output never contains an IP address, a hostname from the config or a secret.
- **Acceptance:** unit tests on both engines; a test that reuses the private-data patterns of
  `tests/unit/test_no_private_data.py` against the output of a config with private addresses.

### ADM-04 Licensing and the license utility (manual pp. 17, 19, 56)
- **What:** Product licences, activation and a license utility; the tag count is shown for
  licensing.
- **Decision:** Out of scope — opencap has no licensing or activation (01-requirements "Out of
  scope"); there are no tag or device limits beyond the NFRs.
- **Status:** Out of scope

### ADM-05 Quick Client (manual p. 56)
- **What:** A test client launched from the tray menu to browse and watch tag values.
- **Decision:** Adapt — the web UI's device page (live values and quality, read once) plus a
  WebSocket live feed (`/api/v1/ws`, 03 §3.6); never a value write.
- **Status:** Planned (#31); device page with values refreshed every 2 s Done (#63).
- **Requirements:**
  - The UI shows value, quality and source timestamp for any tag of any device, filtered by
    channel, device or tag name.
  - Live updates come from the WebSocket feed, not polling, once #31 lands.
  - Read once stays admin-only (D-040); there is no write control.
- **Acceptance:** browser smoke test; both engines show the same values for a simulator device
  (RFC 0001 §7 "Quick Client" row).

### ADM-06 OPC UA configuration page (manual p. 56)
- **What:** A separate OPC UA configuration manager for endpoints and certificate trust (and an
  OPC .NET manager, see 02 IF-10).
- **Decision:** Adapt — an "OPC UA server" page in the web UI over the configuration API:
  endpoint, policies, anonymous flag and limits (02 IF-06–IF-08), and the trust list (ADM-22).
  The OPC .NET manager is out of scope with OPC .NET.
- **Status:** Not started (needs #74 and later #50).
- **Requirements:**
  - The page lists endpoints with their policies, and trusted and rejected client certificates
    with trust, reject and delete actions (admin, audited).
  - Edits are validated by the same schema as the `opcua_server` section.
- **Acceptance:** UI smoke test against an engine with OPC UA enabled; an L1 test in which a
  client is rejected, then trusted through the API, then connects.

### ADM-07 Tray-tool settings: auto-start and language (manual p. 57)
- **What:** Whether the tray tool starts at logon, and the UI language (from the installer or the
  OS).
- **Decision:** Defer — auto-start does not apply (no tray tool); the UI stays English until the
  owner asks for another language.
- **Status:** Not started.
- **Requirements** (when started):
  - UI strings live in one table per language, so a translation needs no code change.
  - The language follows the browser's `Accept-Language`, with a manual choice kept in the tab.
- **Acceptance:** browser smoke test per language.

### ADM-08 Configuration-client connections: port, concurrency, idle timeout (manual p. 58)
- **What:** The TCP port between the configuration client and the runtime; a cap on simultaneous
  configuration connections (1–64, default 10); an idle session timeout (10–3600 s, default 60).
- **Decision:** Adapt — configuration clients use the API listener (`api.bind`, `api.port`,
  01 RT-11); there are no server sessions (a bearer token on each request), so concurrent editors
  are handled by revisions and the store lock (D-041); an idle logout in the UI replaces the
  session timeout; a request cap protects small edge boxes.
- **Status:** Done for the port; idle logout and caps Not started.
- **Requirements:**
  - `api.port` defaults to 8080 on `127.0.0.1` (exists).
  - The UI clears its token from `sessionStorage` after a period without activity (proposed
    `ui.idle_logout_s`, value for the owner to choose).
  - A cap on concurrent API requests (proposed `api.max_concurrent_requests`) answers 503 past the
    cap.
  - Writes are serialized by the store lock (exists).
- **Acceptance:** browser smoke test with a short idle timeout; an API test that exceeds the cap.

### ADM-09 Runtime process and runtime options settings (manual pp. 58–60)
- **What:** Pages for process mode, high priority and CPU affinity (changing the mode restarts the
  runtime and resets DCOM settings), and for runtime project backups (keep N, clean up).
- **Decision:** Adapt — process mode, priority and affinity are set at the OS service manager
  (01 RT-08–RT-10); backups are revision snapshots (01 RT-06). The UI only shows the process
  facts.
- **Status:** Not started.
- **Requirements:**
  - `GET /api/v1/status` reports pid, start time, uptime, how the engine runs (systemd, Windows
    service, container, foreground; proposed field) and the CPUs available to it.
  - The UI dashboard shows them read-only.
- **Acceptance:** API test on both engines; UI smoke test.

### ADM-10 OPC connection security through DCOM (manual pp. 60–61)
- **What:** The server either uses the machine's DCOM authentication settings (with a shortcut to
  the DCOM tool) or bypasses them, accepting unauthenticated calls and impersonating the client,
  which the manual calls the least secure choice.
- **Decision:** Defer with the OPC DA bridge (RFC 0004, #18): DCOM authentication always on, no
  bypass mode; OPC UA is recommended between machines.
- **Status:** Planned (#18).
- **Requirements:**
  - The bridge enforces DCOM authentication at a level RFC 0004 fixes; settings can only raise it.
  - There is no setting that turns authentication off.
  - The docs describe the DCOM setup per supported Windows version.
- **Acceptance:** Windows CI (or owner-run L2 if CI cannot host two machines): an unauthenticated
  remote client is refused.

### ADM-11 Event log, its settings and reset (manual pp. 56, 61–62)
- **What:** Separate logs (event log, communications diagnostics, OPC diagnostics, ThingWorx
  diagnostics), each kept in memory, in one file or in a multi-file store, with a record cap
  (100–100,000, default 25,000), a path, a file size (100–10,000 KB, default 1,000) and days to
  keep (1–90, default 30); a TCP port of its own; records reloaded from disk at start; disk errors
  reported to the OS event viewer; mapped drives may fail under the service account; a reset that
  records who reset it; access controlled through the User Manager.
- **Decision:** Adapt — JSON logs on stdout stay (platform rotation, `docs/deployment.md`); add an
  in-engine event log (04 §4.5, 02 §2.4): bounded ring buffer plus a JSONL file with size-based
  rotation, shown in the UI and the API; communication diagnostics separate and off by default
  (FR-10); no log port (one process). The diagnostics views are in
  [12-diagnostics.md](12-diagnostics.md) and the message catalogue in
  [15-event-log-messages.md](15-event-log-messages.md).
- **Status:** Planned (#56).
- **Requirements:**
  - Keys (proposed) `event_log.mode` (`memory` or `file`, default `file`), `event_log.max_records`
    (100–100,000, default 25,000), `event_log.path` (default `data/events.jsonl`),
    `event_log.max_file_bytes`, `event_log.retain_days` (1–90, default 30).
  - Entry: UTC time, level (`info`, `warning`, `error`, `security`), source (engine, channel,
    device, interface), event name (the same names as the log events), message; one format for
    both engines.
  - File mode reloads the entries at start; memory mode starts empty.
  - `GET /api/v1/events?level=&source=&since=` (viewer) and a UI page with the same filters.
  - Clearing is admin-only and leaves a `security` entry with who and when.
  - An unwritable path or full disk never stops polling: the log falls back to memory, `/health`
    says so and a counter rises (proposed `event_log_write_errors_total`).
  - Communication capture (proposed `diagnostics.comm_capture`, per device, off by default,
    rate-limited) writes its own file.
- **Acceptance:** unit tests for rotation, retention and reload; golden event files compared
  between engines (ADR-0014); role tests for read and clear; a disk-full fault-injection test.

### ADM-12 ProgID redirect (manual pp. 62–63)
- **What:** Registers a legacy server's ProgID so classic OPC clients reach this server without
  changing their tag database; redirected IDs do not show in OPC server browsing, and clients
  should be closed while one is added.
- **Decision:** Defer with the DA bridge (#18). RFC 0004 decides whether the bridge registers
  alias ProgIDs; registering another vendor's ProgID needs an owner and legal check (C-7).
- **Status:** Planned (#18).
- **Requirements** (if adopted):
  - Proposed `opcda.progid_aliases` list; install and uninstall add and remove the registry
    entries; aliases are listed in `GET /api/v1/status`; every change is audited.
- **Acceptance:** Windows CI: a DA test client connects through an alias ProgID and reads an item.

### ADM-13 Users, groups and roles (manual pp. 64–66)
- **What:** A User Manager with groups that contain users. Four built-in groups and users
  (administrators, anonymous clients, server users, ThingWorx interface) cannot be renamed or
  disabled; custom ones can be added, disabled and restored (a disabled group disables its
  users), deleted and moved; names are limited in length and may not contain slashes; user names
  cannot change.
- **Decision:** Adapt — today named API tokens with the roles `viewer` and `admin` (D-040); the
  security RFC (#21) adds users and the roles viewer/operator/engineer/admin (FR-11, 03 §3.7).
  Groups become roles; the built-in anonymous client is the read-only loopback open mode (D-040).
  Disabling keeps the audit history.
- **Status:** Planned (#21); named tokens and two roles Done on both engines (#63, #84).
- **Requirements:**
  - `api.tokens: [{name, role, token_env}]` with unique names and `default` reserved (exists).
  - #21 defines user records (name, role, enabled, credential); `enabled: false` gives 401 and
    keeps the audit trail; names are immutable, 1–64 characters, without `/` or `\` (proposed).
  - One role-to-permission table (ADM-14) shared by both engines as conformance vectors.
  - The last enabled admin cannot be disabled or deleted.
- **Acceptance:** role tests for every API route on both engines (RFC 0001 §7 User Manager row);
  vectors for the role table.

### ADM-14 Permission categories (manual pp. 67–68)
- **What:** Group permissions in categories: project changes, server functions (never for
  anonymous clients), access to device I/O tags, system tags and internal tags, browsing the
  namespace, and reading each event-log severity; allow-all or deny-all per category.
- **Decision:** Adapt — permissions are role capabilities: config read, config write, import,
  reload, read-once (a demand read on the wire), audit read, event-log read and clear, user
  administration. North-side tag access becomes a per-interface read policy (who may browse and
  read which channels, devices and tags: the Security Policies plug-in counterpart, FR-11). Tag
  write permissions do not exist (ADR-0008; any change only through RFC 0002, #19).
- **Status:** Planned (#21); the viewer/admin split (viewer = safe methods, admin = the rest
  including read-once) Done (D-040).
- **Requirements:**
  - #21 publishes the capability table; every API route declares exactly one capability.
  - Read policies apply to OPC UA browse and read (a denied node is not browsable and reads
    return BadUserAccessDenied) and to REST value reads.
  - System tags (`_System`, `_Statistics`, `_Error`) follow the same policies as their device.
  - Event-log read is a viewer capability; clearing is admin.
- **Acceptance:** a coverage test that fails when a route has no capability; an OPC UA L1 test
  with two users holding different read policies.

### ADM-15 Password and token strength (manual p. 68)
- **What:** Case-sensitive user passwords of 14–512 characters mixing letter cases, digits and
  symbols; the old password is needed to change one.
- **Decision:** Adapt — API tokens are random secrets provided through env vars and get a minimum
  length; user passwords (after #21) follow the 14-character rule and are stored only as argon2
  hashes (RFC 0001 §3.4).
- **Status:** Planned (#21).
- **Requirements:**
  - Start refuses a token shorter than a minimum length (proposed 32 characters; a warning first
    for one release); duplicate token secrets are refused (exists, D-040).
  - Passwords: at least 14 characters from at least three character classes; argon2id hashes;
    changing one needs the current password or an admin.
  - A helper prints a new random token (proposed `opencap token new`).
- **Acceptance:** unit tests on both engines for the refusal messages; vectors for the password
  rule.

### ADM-16 User import and export (manual p. 65)
- **What:** Users can be exported to an optionally password-protected XML file and imported on
  another machine, replacing the users there (except the built-in administrator).
- **Decision:** Adapt — token and user definitions are node config (`poller.yaml` now, `node.yaml`
  with v2) and contain no secrets, so moving a node means copying that file and re-provisioning
  the secrets; a hashed user store (after #21) is exported as hashes only.
- **Status:** Done for token definitions; user records Planned (#21).
- **Requirements:**
  - `GET /api/v1/project` exports token definitions without any secret value.
  - Importing a config that changes `api` needs admin and is audited (exists, D-042).
- **Acceptance:** a test sets token env vars, exports, and checks that no secret value appears in
  the export.

### ADM-17 ThingWorx interface users (manual pp. 68–69, 75)
- **What:** A built-in group for the ThingWorx link, which needs project-change rights for data
  exchange and for store and forward.
- **Decision:** Out of scope — the ThingWorx native interface is not offered (02 IF-15,
  ADR-0009).
- **Status:** Out of scope

### ADM-18 Login before configuring (manual p. 35)
- **What:** An optional login dialog before the configuration client may edit the runtime, which
  the manual recommends turning on with one account per person.
- **Decision:** Adapt — always on: every non-open route needs a bearer token; with no token the
  API is read-only and answers loopback requests only (D-040, D-062). Each person gets a named
  token so the audit log names them.
- **Status:** Done (#63, #84).
- **Requirements** (exist):
  - 401 before any body byte is read; 403 for a viewer on unsafe methods.
  - The UI keeps the token in the tab's `sessionStorage` only; the desktop app keeps it in the
    OS keychain (PR #87).
- **Acceptance:** existing API tests on both engines (`tests/integration/test_rust_engine_api.py`);
  UI smoke test.

### ADM-19 Configuration API service settings (manual pp. 70–71)
- **What:** Enable the REST service, allow plain HTTP or not, HTTP and HTTPS ports (57412 and
  57512 by default, which must differ), allowed CORS origins, serve the API documentation, and
  links to open it.
- **Decision:** Adapt — the configuration API shares the admin listener (`api.bind`, `api.port`,
  loopback by default) and is always on (the UI and monitoring use it); CORS through
  `api.cors_origins` (empty by default); the API documentation is the committed OpenAPI contract
  (#32), not served by default. The API itself is planned in
  [10-configuration-api.md](10-configuration-api.md).
- **Status:** Done (bind, port, CORS, #63); OpenAPI contract In review (PR #88).
- **Requirements:**
  - CORS origins match exactly; no wildcard; no cookies (exists in Python).
  - The Rust engine sends CORS headers when `api.cors_origins` is set (today accepted and unused,
    D-062).
  - Proposed `api.docs` (default false) serves the committed contract at
    `/api/v1/openapi.json` to viewers.
- **Acceptance:** CORS preflight test on both engines; contract drift test (PR #88).

### ADM-20 HTTPS for the API (manual pp. 70–71)
- **What:** HTTPS next to or instead of HTTP, plain HTTP only on otherwise protected networks,
  strict-transport headers in every response, TLS 1.2 as the default floor, and a warning not to
  send secrets over HTTP.
- **Decision:** Adapt — optional built-in TLS or a reverse proxy (03 §3.7, RFC 0001 §5); TLS 1.2
  or newer only; HSTS only when served over TLS; remote access needs a token and TLS.
- **Status:** Planned (#21); today there is no built-in TLS and the docs say to bind to loopback
  or a VPN address.
- **Requirements:**
  - Keys (proposed) `api.tls.cert_file`, `api.tls.key_file`, `api.tls.min_version` (`1.2`
    default, `1.3` allowed); with TLS on, the same port does not serve plain HTTP.
  - A non-loopback bind without TLS logs a warning at start (or is refused unless
    `api.allow_plain_http: true`; owner decision).
  - HSTS header only on TLS responses.
  - The certificate comes from the certificate store (ADM-22).
- **Acceptance:** integration test on both engines with a test certificate: a TLS 1.1 handshake is
  refused, TLS 1.2 works, the token check still applies.

### ADM-21 Audit log (Configuration API transaction log) (manual pp. 71–72)
- **What:** A transaction log of API calls kept in memory, one file or a multi-file store, with a
  record cap (100–30,000, default 1,000), path, file size, days to keep, and a verbose mode that
  also stores request and response bodies.
- **Decision:** Adapt — the audit log (D-043): JSON lines at `api.audit_log`, who/role/via,
  field-level old → new changes, rotation at 16 MiB, `GET /api/v1/audit` with filters. No verbose
  body logging: bodies can hold whole projects or secrets, and the field diff gives the same
  insight.
- **Status:** Done (#63); configurable retention Not started; Rust audit arrives with its config
  API (see 10-configuration-api.md).
- **Requirements:**
  - Every accepted change, import and reload is logged inside the store lock; a change that cannot
    be audited is rolled back (exists, D-041).
  - Retention keys (proposed `api.audit_max_bytes`, `api.audit_keep` generations).
  - No audit entry contains a secret value.
- **Acceptance:** existing audit tests; a rotation test; the same suite on Rust when its config
  API exists.

### ADM-22 Certificate store and API certificate management (manual pp. 72–74)
- **What:** For each TLS-using feature: view, export (PEM), reissue (self-signed, ten-year) or
  import (certificate plus unencrypted key) the instance certificate; view, export, delete and
  import trusted CA or self-signed certificates. Imports refuse bad signatures and duplicates and
  ask about expired certificates; a new API certificate applies after a service restart.
- **Decision:** Adapt — one PKI directory per feature in the OPC UA layout
  `pki/<feature>/{own,trusted,rejected,issuers}` (02 §2.4) for the OPC UA server, API TLS and
  MQTT client certificates; managed through the configuration API (routes in
  10-configuration-api.md) and a UI page; every change audited; private keys never leave the node
  through the API.
- **Status:** Not started (PR #61 generates an OPC UA server certificate when missing and reads
  `trusted_certs_dir`).
- **Requirements:**
  - Per feature: view (subject, issuer, validity, thumbprint), export the certificate as PEM,
    reissue self-signed, import certificate and key (key unencrypted or with a passphrase from an
    env var), list, import and delete trusted certificates, move rejected to trusted.
  - Import refuses bad signatures and duplicates; an expired certificate needs an explicit
    `force`, and the response and audit entry carry a warning.
  - Private key files are readable only by the service account (0600 on Linux); no route returns
    a private key.
  - New certificates apply without an engine restart where the stack allows, else the response
    says a restart is needed.
  - Expiry is exported (proposed `cert_expiry_seconds{feature}`) and warned in the event log
    30 days ahead.
- **Acceptance:** unit tests with generated test certificates (expired, bad signature,
  duplicate); an L1 OPC UA test after trusting a client certificate through the API; a test that
  no response contains key material.

### ADM-23 Internal service ports (manual pp. 74–75)
- **What:** Local ports for a store-and-forward service and an internal key service (falling back
  to another port when busy); changes need a runtime restart; enabling store and forward needs
  project-change rights.
- **Decision:** Adapt — not needed: opencap is one process, the outbox is a SQLite file inside the
  engine (ADR-0011) and secrets come from env vars, so there is no key service (01 RT-11). Store
  and forward settings are in [11-store-and-forward.md](11-store-and-forward.md).
- **Status:** Done (by design).
- **Requirements:**
  - The engine opens no listening socket other than the configured client-facing listeners.
- **Acceptance:** the listener test of 01 RT-11 on both engines.

## Open questions
- API without TLS on a non-loopback address (ADM-20): warn only, or refuse unless explicitly
  allowed? Today a token over plain HTTP on a VPN address is accepted.
- Users with passwords (#21): does the owner want username/password login in the UI (server
  sessions, CSRF to handle), or stay with tokens for people and use passwords only for OPC UA
  user tokens?
- ProgID aliases (ADM-12): may the DA bridge register a KEPServerEX ProgID so clients need no
  change? This needs a trademark/legal view (C-7).
- Token minimum length (ADM-15): enforcing it would stop nodes with short tokens at upgrade; is a
  one-release warning enough?
- UI idle logout (ADM-08): what period? The manual's default is 60 s for its console sessions.
- Event log (ADM-11): default retention, and must the file format be identical between engines
  from the start?
- Self-signed certificate validity (ADM-22): the manual uses ten years; a shorter default with
  expiry warnings is safer. Owner preference?
- UI localisation (ADM-07): needed for any site?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| ADM-01 | Administration surface | Adapt | Done | #63, #84, #87 | M1/M4 |
| ADM-02 | Start, stop and reinitialize | Adapt | Done | — | M1 |
| ADM-03 | Support information | Adapt | Not started | — | M3 |
| ADM-04 | Licensing and license utility | Out of scope | Out of scope | — | — |
| ADM-05 | Quick Client | Adapt | Planned (#31) | #31 | M3 |
| ADM-06 | OPC UA configuration page | Adapt | Not started | #74, #50 | M3 |
| ADM-07 | Tray settings: auto-start and language | Defer | Not started | — | — |
| ADM-08 | Configuration-client connections | Adapt | Done (partial) | — | M1/M5 |
| ADM-09 | Runtime process and runtime options settings | Adapt | Not started | #37 | M4 |
| ADM-10 | OPC DCOM connection security | Defer | Planned (#18) | #18 | M5 |
| ADM-11 | Event log, settings and reset | Adapt | Planned (#56) | #56 | M5 |
| ADM-12 | ProgID redirect | Defer | Planned (#18) | #18 | M5 |
| ADM-13 | Users, groups and roles | Adapt | Planned (#21) | #63, #84, #21 | M5 |
| ADM-14 | Permission categories | Adapt | Planned (#21) | #21 | M5 |
| ADM-15 | Password and token strength | Adapt | Planned (#21) | #21 | M5 |
| ADM-16 | User import and export | Adapt | Done (users Planned) | #21 | M5 |
| ADM-17 | ThingWorx interface users | Out of scope | Out of scope | — | — |
| ADM-18 | Login before configuring | Adapt | Done | #63, #84 | M1 |
| ADM-19 | Configuration API service settings | Adapt | Done (OpenAPI In review) | #63, PR #88 | M1/M3 |
| ADM-20 | HTTPS for the API | Adapt | Planned (#21) | #21 | M4 |
| ADM-21 | Audit log (transaction log) | Adapt | Done | #63 | M1 |
| ADM-22 | Certificate store and API certificates | Adapt | Not started | PR #61 | M3 |
| ADM-23 | Internal service ports | Adapt | Done | — | — |
