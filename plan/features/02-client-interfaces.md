# 02 Client interfaces

> Source: KEPServerEX V6 server manual (© PTC), pages 23–29 and 36–55, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
KEPServerEX serves many kinds of clients at the same time (manual p. 23): classic OPC over
COM/DCOM (Data Access 1.0a/2.05a/3.0, Alarms & Events 1.0/1.10, Historical Data Access), OPC UA
over binary TCP with a list of supported profiles and security policies, OPC .NET (a WCF wrapper
over DA and AE), three DDE formats, Wonderware's FastDDE and SuiteLink, a native link into the
iFIX process database, and an outbound native link to the ThingWorx platform (pp. 23–29). Each
interface has a "Project Properties" page (pp. 36–55) with an enable switch, limits and
timeouts, browse options, diagnostics switches and, for OPC DA, a set of compliance toggles for
unusual clients. A general page holds the project title, description and tag count (p. 36). The
ThingWorx page also covers store and forward, a fill-rate example and store-and-forward system
tags (pp. 49–55), which are planned in [11-store-and-forward.md](11-store-and-forward.md).

## opencap today
- **MQTT north** (both engines): SubDevice JSON per device with a SQLite outbox
  (`src/opencap/north/`, `engine/crates/opencap-north`; ADR-0011), byte-identical payloads.
- **REST reads:** `GET /devices/{id}/values` on both engines, `GET /api/v1/devices/{id}/values`
  on the Python engine (#63). Read-only by design (ADR-0008).
- **OPC UA server (Python):** done (#61, finished by the takeover for #74).
  `src/opencap/north/opcua_server.py`: `poller.yaml` `opcua_server` section (off
  by default, endpoint `opc.tcp://127.0.0.1:4840/opencap`, `namespace_uri` registered as ns=2,
  security policies None / Basic256Sha256 / Aes128_Sha256_RsaOaep / Aes256_Sha256_RsaPss in Sign
  and SignAndEncrypt, certificate paths, `trusted_certs_dir`, `anonymous`, one username/password
  from env vars, `update_ms`), KEPServerEX-style NodeIds `ns=2;s=Channel.Device.Tag`, per-device
  `_System` nodes, AccessLevel CurrentRead with writes refused. Rust OPC UA server: #50.
- **Strategy:** ADR-0009 (OPC UA first, KEPServerEX-compatible naming; SuiteLink, FastDDE/DDE,
  iFIX native and ThingWorx native not offered), `docs/architecture/scada-compatibility.md`,
  RFC 0001 §4. OPC DA through a Windows bridge (RFC 0004, #18). OPC .NET and OPC HDA are out of
  scope (01-requirements "Out of scope").
- **Nothing yet** for OPC DA, OPC AE, interface limits beyond the API, or SCADA recipes (#17).

## Features

### IF-01 Several client interfaces at once over one tag store (manual p. 23)
- **What:** The server answers many client technologies simultaneously; it only responds to
  client requests and never initiates them (except outbound platform links).
- **Decision:** Adapt — north interfaces are plugins reading the shared tag store (ADR-0005);
  several run at once, each with its own enable flag and failure domain; all are read-only toward
  PLCs (ADR-0008). Order: OPC UA, Sparkplug B, Modbus TCP server, DNP3/IEC 104, OPC DA bridge
  (ADR-0009).
- **Status:** Done for MQTT SubDevice JSON and REST reads on both engines; the rest per feature
  below.
- **Requirements:**
  - Every north interface is off by default and enabled by its own section (`poller.yaml` today,
    the project's north agents in v2, ADR-0013).
  - A stalled or failing interface does not change poll timing or other interfaces (event bus
    conflation, `north_lagged_total`).
  - Every interface uses the tag path `Channel.Device[.Group…].Tag` (ADR-0009).
  - No interface passes a value write toward a PLC; write attempts are refused and counted.
- **Acceptance:** SCADA L1 suite per interface (NFR-15); a test with two interfaces enabled, one
  stalled; a read-only refusal test per interface.

### IF-02 OPC DA server (manual pp. 23–24)
- **What:** OPC Data Access 1.0a, 2.05a and 3.0 for Windows clients over COM/DCOM, the most
  common classic OPC specification.
- **Decision:** Defer — a separate Windows bridge process serving DA 2.05a/3.0 on top of the
  engine's OPC UA server or API (ADR-0009 §3, RFC 0001 §4.1), designed in RFC 0004 (#18); the
  OPC Foundation redistributables question is the owner's (board decision 6). DA 1.0a only if a
  site needs it.
- **Status:** Planned (#18).
- **Requirements:**
  - Item IDs are opencap tag paths; the browse tree is channel / device / group.
  - Every item reports read-only access rights; every write call fails with a bad-rights error
    and is counted; nothing reaches the engine (ADR-0008, RFC 0002 #19 governs any change).
  - Quality and timestamps follow the ADR-0006 mapping to DA quality bits.
  - The bridge runs as its own Windows service; legacy ProgID aliases are in
    09-administration-and-security (ADM-12).
- **Acceptance:** an OPC DA test client on Windows CI browses and reads every item of a simulator
  project and sees writes fail; L2 recipe with FactoryTalk View (owner-run).

### IF-03 OPC DA settings: versions, browsing, timeouts and limits (manual pp. 36–38)
- **What:** Per-version enable switches; whether browsing shows driver address hints and tag
  properties; how long the server waits for clients at shutdown (10–60 s, default 15); a timeout
  for synchronous reads (5–60 s, default 15); OPC diagnostics capture; limits on connections
  (1–4000, default 512) and groups (10–4000, default 2000), with refusals logged.
- **Decision:** Defer with the bridge (#18). Adopt the limits and timeouts as bridge settings;
  address hints are not offered (they serve client-entered dynamic tags, which stay off, FR-2);
  tag properties map to standard DA item properties (units, description).
- **Status:** Planned (#18).
- **Requirements** (for RFC 0004 to fix):
  - Keys (proposed): `opcda.versions` (default `["2.05a", "3.0"]`), `opcda.max_connections`
    (512), `opcda.max_groups` (2000), `opcda.sync_read_timeout_s` (5–60, 15),
    `opcda.shutdown_wait_s` (10–60, 15), `opcda.diagnostics` (false).
  - Refusals past a limit are logged and counted (proposed `opcda_refused_total{reason}`).
  - Synchronous cache reads come from the tag store; device reads only where demand reads are
    allowed and rate-limited (FR-3).
  - Clients receive the shutdown notification before the bridge exits.
- **Acceptance:** Windows CI tests: connection n+1 refused, group n+1 refused, shutdown notice
  received by the test client, sync read times out at the configured value.

### IF-04 OPC DA compliance options (manual pp. 38–39)
- **What:** Nine switches for odd DA clients: reject unknown language IDs, ignore deadband on cache
  reads, ignore browse filters, 2.05a type-coercion rules, fail synchronous reads on bad quality,
  send initial updates in one callback, convert using the client's locale, return S_FALSE for
  non-good items, and push every change at once ("Return Data ASAP").
- **Decision:** Defer — RFC 0004 starts from strict specification behaviour and adds only the
  switches a tested client needs, each documented with that client.
- **Status:** Planned (#18).
- **Requirements:**
  - Defaults follow the specification: 2.05a coercion, S_FALSE for non-good items, deadband
    honoured, success returned for reads that complete with some bad items.
  - Each compatibility switch has a test that reproduces the client behaviour it exists for.
  - String/number conversions use an invariant locale unless a switch says otherwise.
- **Acceptance:** Windows CI conformance test per switch.

### IF-05 OPC AE server and its settings (manual pp. 24–25, 43)
- **What:** AE 1.0/1.10. Simple events are the server's event-log entries in error, warning and
  information categories; condition events come from the Alarms & Events plug-in (level,
  deviation, rate of change) with a severity of 0–1000 and area/source filters. Not supported:
  event attributes, item translation, per-client area enable, tracking events. Settings: enable
  AE, enable simple events, events per callback (0–1000, default 100, 0 = no limit), minimum time
  between callbacks and minimum keep-alive (100–60000 ms, default 1000); changes apply once all
  AE clients have left.
- **Decision:** Defer — alarms first reach clients through OPC UA Alarms & Conditions and an MQTT
  alarm topic (FR-5, alarms spec #24); OPC AE only through the Windows bridge after OPC DA (FR-8).
- **Status:** Not started (board backlog, no issue).
- **Requirements** (when built):
  - Event-log entries (ADM-11) as simple events in error/warning/information categories.
  - FR-5 alarm conditions as condition events, with the alarm severity mapped into the AE range
    by the alarms spec (#24), area = channel/device path; acknowledgement is an opencap state
    change only (ADR-0008).
  - Keys (proposed) `opcae.enabled`, `opcae.simple_events`, `opcae.max_events_per_callback` (100),
    `opcae.min_buffer_ms` (1000), `opcae.keepalive_ms` (1000), validated with range errors that
    name the key.
- **Acceptance:** Windows CI AE test client receives simple events and a condition event from a
  simulator-driven alarm; schema vectors for the ranges (ADR-0014).

### IF-06 OPC UA server: transport, profiles and address space (manual pp. 25–26)
- **What:** OPC UA 1.02 over binary TCP; full Standard UA Server profile with the Core and Data
  Access facets and UA-TCP / UA-SC / UA Binary; Base Server Behavior only in part; no HTTPS/SOAP
  transport and no complex data types.
- **Decision:** Adapt — the OPC UA server is the primary SCADA interface (ADR-0009): binary TCP
  only; a Data Access style address space with KEPServerEX-compatible NodeIds
  `ns=2;s=Channel.Device.Group.Tag` (namespace URI configurable); read-only access levels
  (ADR-0008); status codes per ADR-0006; aim at the current specification rather than 1.02.
  HTTPS/SOAP and complex types are not planned.
- **Status:** Done for Python (#61 via #74); Rust Planned (#50, evaluate
  async-opcua and open62541, RFC 0001 §3.3).
- **Requirements:**
  - `opcua_server.enabled` is false by default; the default endpoint is
    `opc.tcp://127.0.0.1:4840/opencap`.
  - Address space `Objects/<channel>/<device>/<group…>/<tag>` plus per-device `_System` nodes;
    NodeIds `ns=2;s=<path>`.
  - Variables have AccessLevel CurrentRead; Write, HistoryUpdate, Call and node management are
    refused for every session.
  - DataType follows the tag dtype (Double for scaled tags); SourceTimestamp is the cache source
    timestamp; quality maps to StatusCode per ADR-0006 (stale → UncertainLastUsableValue, never
    read → BadWaitingForInitialData).
  - The services SCADA relies on work (scada-compatibility.md): GetEndpoints, Browse,
    TranslateBrowsePathsToNodeIds on 100k nodes, Read of Value/DataType/AccessLevel/EURange,
    subscriptions with data-change filters and absolute deadband, ServerStatus, ServiceLevel.
- **Acceptance:** L1 in CI with Eclipse Milo, open62541 and asyncua clients against a simulator
  project (100k nodes for #50); values equal the SubDevice payload; write attempts refused.

### IF-07 OPC UA security policies and user authentication (manual pp. 26, 39–40)
- **What:** Policies None, Basic128Rsa15 and Basic256 (both deprecated, for old clients) and
  Basic256Sha256; anonymous login off by default; username/password checked against the User
  Manager, with the password decrypted using the endpoint's policy.
- **Decision:** Adapt — None, Basic256Sha256, Aes128_Sha256_RsaOaep and Aes256_Sha256_RsaPss,
  each Sign and SignAndEncrypt (03 §3.7); the deprecated policies are not offered. None and
  anonymous are refused on a non-loopback endpoint unless explicitly allowed. Username tokens come
  from opencap users (ADM-13, #21); X.509 user tokens later. Certificates from the certificate
  store (ADM-22).
- **Status:** Done for Python (#74): policy list, certificate/key paths, `trusted_certs_dir`,
  `anonymous` (default off), `allow_insecure_remote`, one username/password from env vars.
  Users Planned (#21).
- **Requirements:**
  - With a non-loopback endpoint the start fails if `None` is the only policy or `anonymous` is
    true, unless `opcua_server.allow_insecure: true` (proposed) is set, which logs a warning.
  - Secure policies need a server certificate; if none is configured one is generated
    (self-signed) and its thumbprint logged.
  - Client certificates are accepted only when trusted; rejected ones are kept for review
    (`pki/rejected`, 02 §2.4).
  - Username tokens: credentials from env vars now, from the user store after #21; failed logins
    are counted (proposed `opcua_auth_failures_total`) and logged.
  - Every session is read-only whatever the user (ADR-0008).
- **Acceptance:** L1 tests per policy with asyncua and open62541; negative tests (anonymous
  refused beyond loopback, untrusted certificate refused, wrong password refused); config
  validation unit tests on both engines.

### IF-08 OPC UA session, subscription and queue limits (manual pp. 39–41)
- **What:** Maximum connections (1–128, default 128); minimum and maximum session timeout
  (15 s and 60 s); a tag cache timeout (0–60 s, default 5) for clients reading unregistered tags;
  per-item data queue (1–100, default 2); per-subscription retransmit queue (0–100, default 10,
  0 = off); notifications per publish (1–65536, default 65536).
- **Decision:** Adapt — the same limits as config keys with the manual's defaults where they make
  sense, plus a monitored-item cap for NFR-1 scale; no tag cache timeout (reads come from the tag
  store, nothing is created per read); exported as metrics `opcua_sessions` and
  `opcua_monitored_items` (04 §4.5).
- **Status:** Planned (#50) — not in PR #61.
- **Requirements:**
  - Keys (proposed) under `opcua_server.limits`: `max_sessions` (128), `session_timeout_min_s`
    (15), `session_timeout_max_s` (60), `max_queue_size` (1–100, 2), `max_retransmit_queue`
    (0–100, 10), `max_notifications_per_publish` (1–65536, 65536), `max_monitored_items`.
  - Session n+1 is refused with BadTooManySessions and counted (proposed
    `opcua_refused_total{reason}`).
  - Client requests outside a range are revised to the nearest limit, as the specification
    allows.
- **Acceptance:** L1 tests opening max+1 sessions; a queue-overflow test with a slow publishing
  interval (newest values kept, overflow bit set); schema vectors for the ranges.

### IF-09 OPC UA browse options: tag properties and address hints (manual p. 40)
- **What:** Switches to show each tag's properties while browsing and to show driver address hints
  (which some clients try to add as tags and then fail).
- **Decision:** Adapt — tag metadata appears as standard UA properties (EngineeringUnits, EURange
  from scaling, Description), always on (FR-2); address hints are not offered, since clients cannot
  create tags by address until dynamic tags (FR-2, M5, off by default).
- **Status:** Not started.
- **Requirements:**
  - Scaled tags expose EURange (`eng_lo`, `eng_hi`) and EngineeringUnits (`units`).
  - A tag description is the node's Description.
  - The address space contains only groups, tags and `_System` nodes; no hint nodes.
- **Acceptance:** asyncua L1 test reads EURange and EngineeringUnits of a scaled simulator tag and
  browses a device without hint nodes.

### IF-10 OPC .NET (Xi) (manual pp. 26–27, 42)
- **What:** A WCF service wrapping the DA and AE interfaces for .NET clients over several
  bindings; needs .NET 3.5 and starts only with the server.
- **Decision:** Out of scope — Windows-only, superseded by OPC UA (01-requirements "Out of scope",
  RFC 0001 §3.4).
- **Status:** Out of scope

### IF-11 OPC HDA server (manual pp. 46–47)
- **What:** Classic historical data access for COM clients, with enable and diagnostics switches;
  disabling refuses new clients but keeps connected ones.
- **Decision:** Out of scope — COM/DCOM only (01-requirements "Out of scope"); history for clients
  will come from the local historian's query API and is revisited with it (FR-6).
- **Status:** Out of scope

### IF-12 DDE (CF_Text, XL_Table, Advanced DDE) and its settings (manual pp. 27, 41–42)
- **What:** Three DDE formats for desktop applications, with an enable switch (off by default),
  a service name, format toggles, a client update interval (20–60000 ms, default 100) and a
  request timeout (1–30 s, default 15); needs the runtime to interact with the desktop.
- **Decision:** Out of scope — obsolete Windows desktop messaging that needs an interactive
  session (ADR-0009 §5, FR-8); affected clients get a recipe (IF-16).
- **Status:** Out of scope

### IF-13 FastDDE and SuiteLink (manual pp. 27–28, 43–44)
- **What:** Wonderware's DDE variant and its TCP successor, available only when the vendor's
  runtime components are installed, with an application name and a client update interval
  (20–32000 ms, default 100).
- **Decision:** Out of scope — proprietary and undocumented (ADR-0009 §5); AVEVA systems reach
  opencap through OI Gateway over OPC UA (scada-compatibility.md).
- **Status:** Out of scope

### IF-14 iFIX native interface and PDB settings (manual pp. 28, 44–46)
- **What:** A direct link into the local iFIX process database with latched values, update on
  every poll, a startup item file, "unconfirmed updates" after writes, request and inactivity
  timeouts, and automatic deactivation of tags iFIX stops reading.
- **Decision:** Out of scope — proprietary interface (ADR-0009 §5); iFIX connects through its
  OPC UA client or the DA bridge. "Unconfirmed updates" is a write path (ADR-0008). The idea of
  scanning only what clients read is FR-3's client-driven scan. iFIX-specific topics from later
  in the manual are in [13-ifix.md](13-ifix.md).
- **Status:** Out of scope

### IF-15 ThingWorx native interface (manual pp. 28–29, 47–52)
- **What:** An outbound link that binds the server to an "Industrial Gateway" thing on a ThingWorx
  platform: host, port, resource, application key, certificate-trust and encryption switches, a
  thing cap, a publish floor, logging and an HTTP proxy.
- **Decision:** Out of scope — proprietary PTC protocol that also lets the platform write (ADR-0009
  §5, FR-8, ADR-0008); MQTT and REST agents instead (FR-7). Store and forward (pp. 49–55) is in
  [11-store-and-forward.md](11-store-and-forward.md); ThingWorx users are ADM-17. Outbound-link
  hardening lessons are taken into IF-17.
- **Status:** Out of scope

### IF-16 Migration recipes for clients of interfaces opencap does not offer (manual pp. 23–29)
- **What:** Sites using DDE, SuiteLink/FastDDE, the iFIX native link, ThingWorx, OPC .NET or HDA
  need another way to reach the data.
- **Decision:** Adapt — one recipe per SCADA under `docs/scada/<product>.md`
  (scada-compatibility.md): connection type, endpoint and security, tag path mapping, what changes
  when leaving KEPServerEX.
- **Status:** Planned (#17).
- **Requirements:**
  - Recipes for AVEVA InTouch/System Platform (OI Gateway over OPC UA instead of SuiteLink
    topics), GE iFIX (OPC UA client or DA bridge instead of the native link) and ThingWorx
    (MQTT, marked *verify*), with unverified steps marked *verify* until an owner-run L2 check.
  - `opencap import-kep` reports which of these interfaces the KEPServerEX project enabled, so
    the site knows which recipe applies (proposed; summary key in `reports/import_summary.json`).
- **Acceptance:** importer unit test with a synthetic project fixture (no site data) that enables
  each interface; recipe review; L2 owner-run.

### IF-17 Outbound links: certificate checks, insecure switches and proxies (manual pp. 47–52)
- **What:** The ThingWorx link shows the usual outbound pattern: a key, switches that relax
  certificate checks or disable encryption (marked "never in production"), a minimum publish
  interval, plain-text logs, and a proxy with none/basic/digest/NTLM authentication.
- **Decision:** Adapt for opencap's own outbound agents (MQTT today, Sparkplug and REST client
  later, FR-7; the agents themselves are planned in the IoT gateway file): certificate validation
  always on, no "trust all" switch, secrets only from env vars, proxy support for HTTP agents.
- **Status:** Not started (MQTT TLS with `mqtt.tls` / `mqtt.ca_file` and password from
  `mqtt.password_env` Done).
- **Requirements:**
  - MQTT and REST agents verify the server certificate against the system store or `ca_file`;
    there is no option to disable verification (a private broker is trusted by giving its CA).
  - A plain-TCP connection to a non-loopback broker logs a warning at start.
  - The REST client agent supports an HTTP(S) proxy with credentials from env vars.
  - Minimum publish spacing stays `interval_ms` / `heartbeat_ms` (exists).
- **Acceptance:** MQTT TLS test against a broker whose certificate is signed by an unknown CA
  fails to connect and succeeds with `ca_file` (Mosquitto in CI, #8); both engines.

### IF-18 Interface diagnostics (manual pp. 38, 40, 47, 49)
- **What:** Per-interface diagnostic capture (OPC DA, OPC UA stack, HDA, ThingWorx) into the event
  log or a diagnostics viewer, off by default because it costs performance.
- **Decision:** Adapt — per-interface metrics always on; detailed tracing per interface off by
  default, rate-limited, written to the logs and the event log (09 ADM-11, #56; see also
  [12-diagnostics.md](12-diagnostics.md)).
- **Status:** Not started.
- **Requirements:**
  - Each interface exports connections/sessions, requests, refusals and errors as metrics.
  - `<interface>.diagnostics` (default false) is switched by a reload, without restart.
  - Diagnostic output never contains passwords, tokens or keys.
- **Acceptance:** metric-presence tests on both engines; a unit test that a username/password
  session with diagnostics on leaves no secret in the logs.

### IF-19 Project Properties — General (manual p. 36)
- **What:** A project title (up to 64 characters), a free description and a read-only count of
  defined tags (used for licensing).
- **Decision:** Adapt — title and description in project v2 (`project.json`, ADR-0013), shown in
  the UI header and `GET /api/v1/project/status`; counts are already reported; no licensing.
- **Status:** Planned (#37); counts Done (`GET /api/v1/project/status` → `counts`).
- **Requirements:**
  - `project.title` (≤ 64 characters) and `project.description`, imported from the KEPServerEX
    project by `import-kep`.
  - Channel, device, tag and excluded-device counts (exists).
- **Acceptance:** importer unit test with a synthetic fixture; API test; UI smoke test shows the
  title.

### IF-20 Interface enable, defaults and when changes apply (manual pp. 36–52)
- **What:** Every Project Properties page has an enable switch and a "Defaults" button; some
  changes need a runtime reinitialize, AE changes wait until AE clients leave, disabling HDA keeps
  existing clients, OPC .NET starts only with the server.
- **Decision:** Adapt — every interface section has `enabled` and its defaults in the JSON Schema
  (ADR-0003); each key is documented as "applies on reload" or "needs restart"; a reload says which
  changed keys still need a restart.
- **Status:** Not started (today the `mqtt` connection settings need a restart, as documented in
  the README).
- **Requirements:**
  - The schema carries the default of every interface key; the UI's "reset to default" uses it.
  - `POST /api/v1/reload` lists changed keys that need a restart (proposed `restart_required`).
  - Disabling an interface on reload closes its listener and ends its sessions cleanly, with a
    log entry.
- **Acceptance:** schema conformance fixtures (#71, #39); reload API test on both engines.

## Open questions
- Answered in #74: `anonymous` now defaults to off, and policy `None` on an endpoint that is not
  loopback is a config error unless `allow_insecure_remote: true` (D-046).
- Deprecated OPC UA policies (Basic128Rsa15, Basic256): never offer, or offer behind an explicit
  flag for old SCADA clients? Do any target sites need them?
- OPC DA 1.0a: does any site client still use it, or is 2.05a/3.0 enough for the bridge?
- OPC AE: is it needed at all once OPC UA Alarms & Conditions exists? Which sites' SCADA uses AE?
- OPC HDA: confirm out of scope; is OPC UA Historical Access the later path for SCADA trends?
- Sites on SuiteLink or the iFIX native link: can the owner confirm they accept repointing tags
  to OPC UA (or the DA bridge) during migration?
- Any ThingWorx users among the sites? If yes, an MQTT recipe needs an owner-run check.
- OPC UA session cap: keep 128 as default, or size it from NFR-1?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| IF-01 | Several client interfaces over one tag store | Adapt | Done | — | M1 |
| IF-02 | OPC DA server | Defer | Planned (#18) | #18 | M5 |
| IF-03 | OPC DA settings: versions, browsing, timeouts, limits | Defer | Planned (#18) | #18 | M5 |
| IF-04 | OPC DA compliance options | Defer | Planned (#18) | #18 | M5 |
| IF-05 | OPC AE server and its settings | Defer | Not started | — | M5 |
| IF-06 | OPC UA server: transport, profiles, address space | Adapt | Done (Python; Rust Planned #50) | #61, #74, #50 | M1/M3 |
| IF-07 | OPC UA security policies and user authentication | Adapt | Done (Python; users Planned #21) | #61, #74, #21 | M1/M3 |
| IF-08 | OPC UA session, subscription and queue limits | Adapt | Planned (#50) | #50 | M3 |
| IF-09 | OPC UA browse options: properties and hints | Adapt | Not started | — | M3 |
| IF-10 | OPC .NET (Xi) | Out of scope | Out of scope | — | — |
| IF-11 | OPC HDA server | Out of scope | Out of scope | — | — |
| IF-12 | DDE and its settings | Out of scope | Out of scope | — | — |
| IF-13 | FastDDE and SuiteLink | Out of scope | Out of scope | — | — |
| IF-14 | iFIX native interface and PDB settings | Out of scope | Out of scope | — | — |
| IF-15 | ThingWorx native interface | Out of scope | Out of scope | — | — |
| IF-16 | Migration recipes for interfaces not offered | Adapt | Planned (#17) | #17 | M3 |
| IF-17 | Outbound links: certificate checks, proxies | Adapt | Not started | #8 | M3 |
| IF-18 | Interface diagnostics | Adapt | Not started | #56 | M5 |
| IF-19 | Project Properties — General | Adapt | Planned (#37) | #37 | M3 |
| IF-20 | Interface enable, defaults and apply semantics | Adapt | Not started | #71, #39 | M3 |
