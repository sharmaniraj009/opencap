# 04 Channels

> Source: KEPServerEX V6 server manual (© PTC), pages 76–86, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
A channel is the communication path from the server to one or more devices: a serial port, a
plug-in card or a network socket, bound to exactly one driver. Only devices of that driver can
be added to it, channel names are unique in the project, and two channels may not claim the same
physical resource (two serial drivers on one COM port). The property groups are: identification
(name, description, driver), diagnostics capture, static tag counts, advanced settings (handling
of NaN/infinite/denormal floats, a delay between devices), the network adapter to bind,
serial settings (medium, COM port, line settings, flow control and manual RTS timing, error
reporting, idle close), Ethernet encapsulation of serial protocols through terminal servers,
communication serialization of several channels over one shared medium ("virtual networks"),
write optimizations, and a driver-specific device discovery dialog (manual p. 76–86).

## opencap today
- **Model:** `channels[]` in `config/devices.json` (`Channel`: `name`, `enabled`, `devices`) in
  `src/opencap/config/models.py`; the driver is chosen per device (`Device.driver`), not per channel.
  `overrides.yaml` `channels: {"<name>": {enabled: false}}` disables a channel by hand.
- **Runtime:** one `ChannelWorker` per channel (`src/opencap/core/channel_worker.py`) with a FIFO
  wire lock, so one request is on the wire per channel and channels run in parallel (D-015). The
  Rust engine ports the same rules (`engine/crates/opencap-core/src/worker.rs`, RFC 0001 §2.4, #64).
- **Serial lines:** `params.transport` = `tcp` (serial-over-TCP gateway), `serial` (local port),
  `rfc2217` (DF1 only) or `unmapped`; line settings `baudrate`/`bytesize`/`parity`/`stopbits`;
  devices on one line share one connection and a per-line lock (D-033, `src/opencap/drivers/_serial.py`);
  KEPServerEX COM ports are mapped in `overrides.yaml` `serial_ports` (D-037). Drivers `modbus_rtu`
  and `ab_df1` merged in #62; Rust ports are #47 and #53. Follow-ups (line-setting mismatch checks,
  shared-line behaviour) are #68.
- **Floats:** `params.nonnormal_float` (`zero` | `unmodified`) in `modbus_tcp`/`modbus_rtu`, imported
  from the channel property, also in the Rust `modbus_tcp` decoder.
- **API/UI:** `GET/POST /api/v1/channels`, `GET/PATCH/DELETE /api/v1/channels/{channel}`, rename moves
  the map entry and override along; config editor page in `/ui` (#63). The Rust engine serves only
  the admin routes (`/channels`, `/devices/…`, `/reload`).
- Not present: channel description, inter-device delay, network adapter binding, flow control,
  communication serialization across channels, device discovery.

## Features

### CH-01 Channel object: create, rename, remove, unique name (manual p. 76–77)
- **What:** A channel is created with a name and a driver, can be renamed or deleted while the
  server runs, and must have a name no other channel uses. Clients holding items under the old
  name keep them until they re-acquire.
- **Decision:** Adapt — the channel is opencap's unit of wire serialization (D-015); the driver
  lives on each device, so one channel may hold devices of several drivers that share a medium
  (FR-2, ADR-0003). Naming rules are in plan 03.
- **Status:** Done
- **Requirements:**
  - `POST /api/v1/channels` with an existing name answers 409; a name with `/` answers 422.
  - Renaming a channel through `PATCH /api/v1/channels/{channel}` rewrites every device id
    `<channel>/<device>` and moves `devices_map.yaml` and `overrides.yaml` entries along.
  - `POST /api/v1/reload` (or `SIGHUP`) restarts only channels whose config changed.
- **Acceptance:** `tests/unit/test_config_store.py` and `tests/unit/test_config_api.py` (rename,
  conflict, delete); `tests/integration/test_runtime_reload.py` (only changed channels restart).
  Rust: same checks once the config API is ported (plan 10).

### CH-02 Channel description and driver summary (manual p. 77)
- **What:** Free-text description per channel, and a read-only display of the driver it uses.
- **Decision:** Adapt — add an optional `description` to `Channel`; the "driver" of a channel is
  derived from its devices and shown, never configured. Diagnostics capture is plan 12.
- **Status:** Not started
- **Requirements:**
  - `Channel.description: str | null` (additive schema change, ADR-0003); `schema/devices.schema.json`
    regenerated and the Rust serde model updated.
  - `opencap import-kep` copies the channel's description property.
  - `PATCH /api/v1/channels/{channel}` accepts `description` (today only `name` and `enabled`);
    `GET /api/v1/channels` returns `description` and `drivers` (sorted distinct driver names).
- **Acceptance:** importer unit test with a synthetic project; config conformance fixture in
  `tests/conformance/config/valid/`; UI smoke test shows the field in the config editor.

### CH-03 Static tag count per channel (manual p. 77–78)
- **What:** The channel shows how many static tags are defined under it, for troubleshooting and
  load balancing.
- **Decision:** Adopt
- **Status:** Not started
- **Requirements:**
  - Per-device counts exist already (DEV-05 in plan 05); this adds the channel total.
  - `GET /api/v1/channels` and `GET /api/v1/channels/{channel}` return `tags` (all configured tags)
    and `tags_enabled` per channel.
  - The dashboard in `/ui` shows the count per site.
- **Acceptance:** API unit test against a fixture with known counts; UI smoke test.

### CH-04 Non-normalized float handling (manual p. 78)
- **What:** A choice of how NaN, infinity and denormal IEEE-754 values read from a device reach
  clients: replaced by zero (default) or passed through unchanged.
- **Decision:** Adapt — a per-device driver param `nonnormal_float` (`zero` | `unmodified`) filled
  from the channel property at import, applied where the driver decodes floats from raw bytes.
- **Status:** Done
- **Requirements:**
  - `modbus_tcp` and `modbus_rtu` (Python) and `modbus_tcp` (Rust) honour `params.nonnormal_float`,
    default `zero`; `opencap import-kep` fills it from the channel property and reports unknown codes.
  - With `unmodified`, a NaN or infinite value is never written as a JSON number: SubDevice JSON lists
    the tag under `BadTags` (`src/opencap/north/payload.py`).
  - Every new driver that decodes IEEE-754 from raw bytes (`s7`, `melsec_mc`, FINS, …) takes the same
    param as part of its ADR-0004 §5 acceptance.
- **Acceptance:** `tests/unit/test_modbus_decode.py` and the Rust decoder tests today; decode
  conformance vectors (#3) covering NaN, ±Inf, denormal and −0.0 in both modes, run by both engines.

### CH-05 Inter-device delay (manual p. 78)
- **What:** After a reply from one device, the channel waits a set time before sending to the
  next device sharing the channel; a value of 0 means no delay.
- **Decision:** Adopt — implemented in the channel's wire lock, which already orders devices.
- **Status:** Not started
- **Requirements:**
  - `Channel.inter_device_delay_ms: int = 0` (0–60000); the wire lock holds the line idle for that
    time when the next request is for a different device than the previous one.
  - `opencap check` adds the delay to its cycle-time estimate.
  - Importer maps the KEPServerEX channel property where present.
- **Acceptance:** worker unit test with a fake clock (`tests/unit/test_channel_worker.py`);
  simulator test recording request timestamps for two devices on one channel; same test with
  `OPENCAP_ENGINE=rust`.

### CH-06 Network adapter / interface selection (manual p. 78, 83–84, 139)
- **What:** Ethernet drivers (and serial drivers in encapsulation mode) can be bound to one network
  adapter, by name or IP, instead of the operating system's route choice; "Default" keeps the OS
  choice.
- **Decision:** Adapt — bind by local IP address (`bind_address`), portable across Linux and
  Windows, instead of adapter names; default stays OS routing.
- **Status:** Not started
- **Requirements:**
  - Optional `params.bind_address` (IPv4/IPv6 literal) on every TCP/UDP driver; the driver binds its
    socket to it before connecting.
  - `opencap check` reports a `bind_address` that is not assigned on the host as an error.
  - The importer does not copy adapter names (they are host-specific); it reports them in
    `reports/import_summary.json`.
- **Acceptance:** simulator test bound to a second loopback address, asserting the
  peer address seen by `tests/helpers/modbus_sim.py`; both engines.

### CH-07 Serial line settings and COM-port mapping (manual p. 76, 79–80)
- **What:** Physical medium (COM port, modem, encapsulation, none), COM id, baud rate, data bits,
  parity and stop bits for serial drivers.
- **Decision:** Adapt — `params.transport` and line params per device; the KEPServerEX COM port is
  mapped to a local port, a TCP gateway or an RFC 2217 server in `overrides.yaml` `serial_ports`
  (D-033, D-037). Modem medium: plan 08.
- **Status:** Done
- **Requirements:**
  - `params.transport` is one of `tcp`, `serial`, `rfc2217` (DF1 only), `unmapped`; line params
    `baudrate`, `bytesize`, `parity`, `stopbits` are validated; a bad mapping is a config error.
  - For DF1 devices the importer copies COM id and line settings from the channel; an unmapped COM port keeps the
    device disabled with a warning, and `opencap check` shows it `(disabled)`.
  - Devices on one port or gateway share one open connection; the last one leaving closes it.
- **Acceptance:** `tests/unit/test_serial_config.py`, `tests/unit/test_kep_import_df1.py`,
  `tests/integration/test_serial_acceptance.py`. Rust: the same suites with `OPENCAP_ENGINE=rust`
  when #47 and #53 land.

### CH-08 Shared serial line: setting checks and "shared" indicator (manual p. 76, 79)
- **What:** Channels may not claim the same port with different settings; when a driver allows a
  shared connection, the channel shows that its port settings are shared.
- **Decision:** Adapt — sharing is per physical line (D-033); mismatched settings on one line become
  a config error, and the status shows which devices share a line.
- **Status:** Planned (#68)
- **Requirements:**
  - Devices that share a port or gateway with different baud, parity, data bits, stop bits or error
    check are a config error at load time, for `ab_df1` and `modbus_rtu` (#68).
  - One silent unit on a multi-drop RTU line does not close the line for healthy units (#68).
  - `GET /api/v1/devices/{id}/status` adds `shared_line: true|false` and the line endpoint (beyond #68).
- **Acceptance:** loader unit tests for each mismatch; simulator test with two units, one muted (#68).

### CH-09 Flow control and manual RTS timing (manual p. 76, 80)
- **What:** How RTS/DTR lines are driven (none, DTR, RTS, both, RTS always, RTS manual with raise,
  drop and poll-delay times), mainly for RS-232 to RS-485 converters and radio modems.
- **Decision:** Defer — not needed by the migrated project (TCP gateways own the line); revisit
  when a site uses a local RS-485 converter without automatic direction control.
- **Status:** Not started
- **Requirements:**
  - `params.flow_control`: `none` | `dtr` | `rts` | `rts_dtr` | `rts_always` | `rts_manual`, plus
    `rts_raise_ms`, `rts_drop_ms`, `poll_delay_ms` (0–9999) for `rts_manual`, applied to
    `transport: serial` only; any other transport with a non-`none` value is a config error.
  - The importer maps the KEPServerEX flow-control value it already keeps in `source.kep_serial`.
- **Acceptance:** unit test of the pyserial settings built from params; manual field check on a
  real converter (owner-run, ADR-0014 §5).

### CH-10 Serial operational behaviour: error reporting and idle close (manual p. 80–81)
- **What:** Low-level line errors can be kept out of the event log; the port can close when no
  client references any tag, after an idle time.
- **Decision:** Adapt — opencap polls on its own schedule, not on client demand, so there is no
  "idle" channel; the line closes when every device on it is disabled, demoted or removed. Errors
  are always counted; how much is logged is plan 12/15.
- **Status:** Done
- **Requirements:**
  - Demotion, disable and reload close the device's connection; the last device leaving a shared
    line closes it (D-015, D-033).
  - Every failed request is counted in `poll_requests_total{result}` whatever the log level.
- **Acceptance:** `tests/unit/test_channel_worker.py`, `tests/integration/test_serial_acceptance.py`.

### CH-11 Ethernet encapsulation of serial protocols (manual p. 80–82, 89–91, 139)
- **What:** A serial driver talks to devices behind a terminal server over the network: terminal
  server IP, port (default 2101), TCP or UDP, connect timeout; the serial protocol itself is
  unchanged.
- **Decision:** Adapt — `params.transport: tcp` (raw TCP) or `rfc2217`, with the gateway in the
  device's `host`/`port` or in `serial_ports`; several devices behind one gateway share one
  connection (D-033).
- **Status:** Done
- **Requirements:**
  - `transport: tcp` carries the serial bytes on a raw TCP socket to the gateway, `rfc2217` negotiates
    the line settings with the COM-port server (DF1); the protocol layer is the same as on a local port.
  - A gateway that goes away demotes only the devices behind it; they resume when it is back.
  - Connect timeout: DEV-16. UDP terminal servers: CH-12.
- **Acceptance:** `tests/integration/test_serial_acceptance.py` (gateway power-off demotes only its
  devices); `tests/unit/test_serial_transport.py`.

### CH-12 Import KEPServerEX encapsulation settings (manual p. 79, 81–82, 89–91)
- **What:** KEPServerEX projects that already use encapsulation hold the terminal server address
  in the channel or device properties.
- **Decision:** Adopt — import them straight into `transport: tcp` instead of `unmapped`.
- **Status:** Not started
- **Requirements:**
  - `opencap import-kep` maps a channel whose physical medium is Ethernet encapsulation to
    `transport: tcp`, `host`/`port` from the encapsulation properties (device-level IP wins over
    channel-level), with no `serial_ports` entry needed.
  - A UDP terminal server is reported in `reports/import_summary.json` and the device stays disabled
    until a TCP transport is configured (open question below); unknown medium codes are reported too.
- **Acceptance:** importer unit tests with synthetic channels for each medium (COM, encapsulation,
  modem, none); addresses in 192.0.2.x.

### CH-13 Operation with no communications (manual p. 79, 81)
- **What:** A channel with no physical medium; explicit device reads are ignored or failed.
- **Decision:** Out of scope — the only device without a medium in opencap is the simulator
  driver (#12), which needs no channel setting; explicit reads are the read-once API, which
  always reports what happened.
- **Status:** Out of scope

### CH-14 Communication serialization (virtual networks) (manual p. 82–83)
- **What:** Channels that share one physical pipe (for example an Ethernet radio) join a numbered
  virtual network; only one of them talks at a time, taking turns round-robin with a set number
  of transactions per turn; a global mode chooses load-balanced or priority order (writes, then
  explicit reads, then scans).
- **Decision:** Adapt — an optional `serialization_group` on channels puts them under one shared
  wire lock with round-robin turns; priority mode reduces to "read-once before scheduled reads"
  because there are no writes (ADR-0008). Recommended together with auto-demotion (DEV-10 in plan 05).
- **Status:** Not started
- **Requirements:**
  - `Channel.serialization_group: str | null` and `Channel.transactions_per_turn: int = 1` (1–99);
    channels in one group never have two requests in flight together.
  - Node setting `serialization_mode: load_balanced | priority` (default `load_balanced`); in
    `priority` a pending `read-once` gets the next turn.
  - Metrics `serialization_turns_total{channel}`, `serialization_owned_seconds_total{channel}`;
    tags per SYS-13 in plan 07.
  - `opencap check` estimates the group's cycle as the sum of its channels.
- **Acceptance:** simulator test with three channels in one group: request intervals never overlap,
  turns follow the configured count; a muted device delays the others until demoted; both engines.

### CH-15 Write optimizations (manual p. 84–85)
- **What:** How queued client writes are sent (all values, latest value for non-Boolean tags, latest
  value for all tags) and a write-to-read duty cycle.
- **Decision:** Out of scope — write path; opencap never writes PLC values (ADR-0008). Any write
  design goes through RFC 0002 write safety (#19).
- **Status:** Out of scope

### CH-16 Device discovery (manual p. 85–86)
- **What:** For drivers that can find devices on the network, a dialog scans with driver-specific
  settings (range, timeout, scope), lists name, id and description, and adds the chosen ones.
- **Decision:** Defer — M5, with the first driver that has a discovery service (BACnet Who-Is,
  EtherNet/IP List Identity); read-only services only, and only on operator request.
- **Status:** Not started
- **Requirements:**
  - Optional driver method `discover(options) -> list[DeviceCandidate]` (ADR-0004), using only
    identity/list services; a driver without it answers "not supported".
  - `opencap discover --driver <name> --range 192.0.2.0/28 [--timeout-ms N]` and
    `POST /api/v1/channels/{channel}/discover` (admin, audited) return candidates; adding them uses the
    existing `POST /api/v1/channels/{channel}/devices`.
  - Discovery never runs automatically and is rate-limited per channel.
- **Acceptance:** simulator test (CIP simulator answering List Identity) checking that only
  identity services were sent; UI smoke test of the candidate list.

## Open questions
- Do any sites put several channels behind one radio or shared pipe (CH-14), or is the per-line
  lock of D-033 enough?
- Is UDP encapsulation (CH-12) used by any migrated terminal server, or can opencap stay TCP-only?
- Should `bind_address` (CH-06) be per device or per channel? Per device is simpler and covers both.
- Drivers with native float types (`ab_logix`, `ab_pccc`, `opcua_client`) pass NaN/Inf through today
  (SubDevice JSON then marks the tag bad): should they also honour `nonnormal_float` (CH-04)?
- Device discovery sends broadcast traffic onto plant networks: may opencap offer it at all, and
  behind which role?
- Per-channel concurrency (FR-3, ADR-0004 `max_inflight`) is the opposite of CH-14; confirm both
  can be options on the same `Channel` model.

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| CH-01 | Channel object: create, rename, remove, unique name | Adapt | Done | #63 | M1 |
| CH-02 | Channel description and driver summary | Adapt | Planned (#103) | #103 | — |
| CH-03 | Static tag count per channel | Adopt | Planned (#103) | #103 | — |
| CH-04 | Non-normalized float handling | Adapt | Done | #64 | M2 |
| CH-05 | Inter-device delay | Adopt | Planned (#105) | #105 | — |
| CH-06 | Network adapter / interface selection | Adapt | Planned (#106) | #106 | — |
| CH-07 | Serial line settings and COM-port mapping | Adapt | Done | #62 (Rust: #47, #53) | M1 |
| CH-08 | Shared serial line: setting checks and "shared" indicator | Adapt | Planned (#68) | #68 | M1 |
| CH-09 | Flow control and manual RTS timing | Defer | Planned (#107) | #107 | — |
| CH-10 | Serial operational behaviour: error reporting, idle close | Adapt | Done | #62 | M1 |
| CH-11 | Ethernet encapsulation of serial protocols | Adapt | Done | #62 | M1 |
| CH-12 | Import KEPServerEX encapsulation settings | Adopt | Planned (#108) | #108 | — |
| CH-13 | Operation with no communications | Out of scope | Out of scope | — | — |
| CH-14 | Communication serialization (virtual networks) | Adapt | Planned (#104) | #104 | — |
| CH-15 | Write optimizations | Out of scope | Out of scope | #19 | — |
| CH-16 | Device discovery | Defer | Planned (#110) | #110 | M5 |
