# 08 Communications management: modems, phonebook, auto-dial

> Source: KEPServerEX V6 server manual (© PTC), pages 139–143, summarised in our own words for planning. Status as of 2026-11-18.

## What the manual describes
The communications-management chapter first recaps three features covered elsewhere: auto-demotion
(plan 05, DEV-10), network interface selection (plan 04, CH-06) and Ethernet encapsulation (plan 04,
CH-11). The rest is about reaching remote serial devices over telephone modems. A serial channel can
use a modem as its physical medium; the server talks to modems installed in Windows through TAPI.
The manual explains that a normal modem carries at most 10 bits per character (start, data, parity
and stop bits together), so some line settings need a special modem; how to cable the remote
modem to the device with null-modem parts; and how to select and configure the initiating modem.
Clients control the modem through channel-level modem tags (dial, hang up, mode, number, status).
A per-channel phonebook holds named numbers that clients can dial by writing a phonebook tag. With
auto-dial, the server itself dials phonebook entries in priority order when a client needs data,
reuses the last number that worked, hangs up when no client references the channel any more, and
restricts client writes to the modem tags; the chapter also lists how runtime changes to auto-dial
take effect (manual p. 139–143).

## opencap today
- Nothing for modems: no `modem` transport in `src/opencap/drivers/_serial.py` (transports are `tcp`,
  `serial`, `rfc2217`, `unmapped`, D-033), no phonebook, no dialler, and no mention of modems in
  `docs/architecture/01-requirements.md` (FR-1…FR-13 or "Out of scope").
- Remote serial PLCs are reached through serial-over-TCP gateways on the site network, which sites
  reach over IPsec VPN (constraint C-6); KEPServerEX COM ports are mapped in `overrides.yaml`
  `serial_ports` (D-037).
- The importer would treat a KEPServerEX channel on a modem like any other serial channel (COM id and
  line settings only); it does not detect the modem medium.
- Platforms are Linux and Windows (FR-13); TAPI exists only on Windows.

## Features

### COM-01 Modem as a channel medium (manual p. 79, 81, 139–141)
- **What:** A serial channel reaches its devices through a dial-up modem: choose the installed modem,
  a connect timeout (default 60 s), vendor modem settings, error reporting and closing the line after
  an idle time; line settings must fit in 10 bits per character for a standard modem.
- **Decision:** Defer — not in FR-1…FR-13 and not used by the migrated project (sites reach serial
  PLCs through TCP gateways over VPN, C-6); revisit only on owner request, with no issue or milestone
  yet. If adopted, adapt it portably: a Hayes AT-command dialler on the existing serial transport
  instead of Windows TAPI, so it runs on Linux and Windows alike (FR-13).
- **Status:** Not started
- **Requirements:**
  - `opencap import-kep` detects channels whose physical medium is a modem and reports them in
    `reports/import_summary.json`, leaving their devices disabled with a clear reason (useful even if
    modems are never supported).
  - If adopted: `params.transport: modem` with `modem: {port, init, connect_timeout_s: 60,
    idle_close_s: 15}` on `modbus_rtu` and `ab_df1`; `opencap check` warns when start + data + parity +
    stop bits exceed 10.
- **Acceptance:** importer unit test with a synthetic modem channel; if adopted, a fake modem on a pty
  (`tests/helpers/pty_bridge.py`) answering AT commands with CONNECT, BUSY, NO CARRIER and NO DIALTONE.

### COM-02 Phonebook (manual p. 141–142)
- **What:** A list of named phone numbers per modem channel (name up to 256 characters, number up to 64
  digits, description), shown to clients as a `_Phonebook` branch; writing an entry's tag dials it; the
  order of entries is the auto-dial priority.
- **Decision:** Defer — with COM-01. If adopted, the phonebook is channel config (an ordered list
  edited through the audited API); dialling by writing a phonebook tag is not offered (COM-04).
- **Status:** Not started
- **Requirements:**
  - If adopted: `Channel.phonebook: [{name, number, description}]`, names unique per channel, order =
    dial priority; `GET/PUT /api/v1/channels/{channel}/phonebook` (admin for PUT, audited); entries
    exposed read-only as `<Channel>._Phonebook.<name>`.
  - Phone numbers count as site data: never in committed fixtures except obviously fictitious ones.
- **Acceptance:** config fixtures (valid, duplicate names); API unit test.

### COM-03 Auto-dial (manual p. 81, 142–143)
- **What:** The server dials phonebook entries in order until one connects whenever a client needs
  data from the channel, keeps using the last good entry (forgotten on restart), hangs up when no
  client references remain, takes its timing from the modem configuration, and applies changes to
  the auto-dial setting at the next disconnect.
- **Decision:** Defer — with COM-01. If adopted, adapt it to opencap's own schedule: opencap polls
  continuously rather than on client demand, so the dialler connects when a device on the channel is
  due and hangs up after `idle_close_s` without due requests; a config reload restarts the channel,
  which drops and re-dials the line.
- **Status:** Not started
- **Requirements:**
  - If adopted: entries are tried in phonebook order, starting with the last entry that connected;
    each attempt is bounded by `connect_timeout_s`; a failed round counts as a failed connect and
    enters the normal 1 s → 60 s backoff and demotion rules (D-015).
  - Metrics `modem_dials_total{channel,result}` and `modem_connected{channel}`; status tags per SYS-12
    in plan 07; structured events for connect, hang-up and failure (plan 15).
- **Acceptance:** if adopted, fake-modem pty test: first entry BUSY, second CONNECT; reconnect uses the
  second entry first; idle hang-up after the configured time; both engines.

### COM-04 Client control of the modem through tag writes (manual p. 133–135, 141–143)
- **What:** Without auto-dial, a client application writes a number and a dial command to modem tags,
  sets the line mode (call out, answer, both) and hangs up through another write; with auto-dial these
  writes are restricted.
- **Decision:** Out of scope — every north interface is read-only (ADR-0008); operator actions on
  opencap's own state go through the authenticated, audited API, and any write path from clients needs
  RFC 0002 write safety (#19). Incoming calls (answer mode) are not planned either.
- **Status:** Out of scope

## Open questions
- Does any site still reach PLCs over dial-up or radio modems? If not, the owner may move COM-01 to
  COM-03 to `Out of scope` and add "telephone modems (TAPI)" to the "Out of scope" list in
  `docs/architecture/01-requirements.md`.
- If modems are ever needed, is an AT-command dialler acceptable, or do sites depend on vendor
  settings that only TAPI exposes?
- Should `import-kep` exclude modem channels (as it does for Simulator devices) or import them disabled
  with the line settings kept for a later TCP-gateway mapping?

## Checklist rows
| ID | Feature | Decision | Status | Issue/PR | Milestone |
|---|---|---|---|---|---|
| COM-01 | Modem as a channel medium | Defer | Not started | — | — |
| COM-02 | Phonebook | Defer | Not started | — | — |
| COM-03 | Auto-dial | Defer | Not started | — | — |
| COM-04 | Client control of the modem through tag writes | Out of scope | Out of scope | #19 | — |
