# Decisions and deviations from PLAN.md

Each entry: what PLAN.md says, what we did, and why.

## Phase 0

### D-001 `Driver.plan()` returns a `Plan`, not `list[Block]`
PLAN §5 has `plan(tags) -> list[Block]`. We return `Plan(blocks, invalid)` so that tags
with an unusable address are reported once as `bad_config` and never sent on the wire
(PLAN §5.1 "Do not retry `bad_config` tags every cycle").

### D-002 Driver modules keep `plan()` pure and import protocol libraries lazily
`opencap check` builds block plans for every device without opening sockets, and works
even when a protocol library (pycomm3, snap7…) is not installed.

### D-003 Development interpreter is Python 3.14
The build machine has Python 3.14 only. `pyproject.toml` still says `>=3.12` and the
code avoids 3.13+ only features. The Docker image (Phase 5) pins 3.12.

### D-004 Real KEPServer export is a git-ignored fixture
`tests/fixtures/project.json` (raw KEPServer export) and
`tests/fixtures/kep2poller_output.json` (output of the old `kep2poller.py`) contain site
IPs and OPC UA / MQTT credentials. They are git-ignored; tests that need them are skipped
when the files are absent.

### D-005 Modbus block size follows the KEPServer device setting (32), not kep2poller's 64
PLAN §5.1: "default 64, cap at the KEPServer device value". All 29 devices have block
sizes of 32, so the real project plans to **268** blocks. kep2poller ignored the device
value and always used 64 (232 blocks); with the same 64 limit opencap plans 231.

### D-006 Modbus planner details (Kepware semantics)
- FC01/02 gap is `max_gap × 16` bits (64 by default), overridable with `max_gap_bits`.
- A tag larger than its per-FC limit (`min(param, 125 regs / 2000 bits)`) goes to
  `Plan.invalid`. It is never sent as an oversized request.
- Strings `4xxxxx.LLLH|L`: length 1–250 bytes, ⌈len/2⌉ registers. Byte order comes only
  from the H/L suffix, the value is cut at the first NUL and decoded as latin-1.
- Bits in registers are taken after `byte_order_big` is applied. With
  `modicon_bit_order`, bit 0 is the MSB.
- NaN, ±Inf and denormal floats are replaced with 0.0 (Kepware "Replace with Zero"),
  following the channel's `CHANNEL_NON_NORMALIZED_FLOATING_POINT_HANDLING`. New param
  `nonnormal_float`.
- Rejected addresses (Kepware rules): a register bool without `.b`, a non-bool on a coil
  or discrete input, a bool array on a register, an array on a `.b` or string address,
  and reference 0 when zero-based addressing is on.
- Tags at the same address are one wire item, decoded once and served to every name.
- Not supported (absent from the project): "Treat Longs as Decimals", BCD/LBCD/Char/Byte types.

### D-007 `DecodeError` lives in `drivers/base.py`
All drivers return `DecodeError(reason)` as a per-tag value from `decode_block()`. The core
maps it to `bad_decode`. One shared frozen dataclass replaces the per-driver copies.

### D-008 pycomm3 behaviour that differs from PLAN §5.1 (Allen-Bradley)
- **Micro800: no Multiple Service Packet.** `LogixDriver.read(*tags)` sends one request per
  tag when the target is a Micro800 (`logix_driver.py`: `if len(parsed_tags) != 1 and not
  self._micro800`). The 66 Micro800 devices cost **6,727 requests per full pass**, up to
  378 on one device (~23 s at 60 ms RTT). Batching would give ~511. **Phase 2 must test
  multi-service on a real Micro800 (pycomm3 override, or libplctag as the plan's fallback)
  before cut-over.**
- `SLCDriver.read(*addrs)` sends one PCCC request per address, so one `PcccBlock` = one
  wire request. PCCC byte count, file and element numbers are 1 byte each: reads are
  capped at 255 data bytes (`request_size` is clamped to 255), elements > 255 and files
  outside 1–255 are invalid.
- pycomm3's `B3/n` bit-number parsing is buggy (always bit 0) and `I:x/b` rejects b > 15.
  We always read whole words and decode bits locally.
- T/C sub-elements cannot be block-read (`{n}` not accepted); each `.ACC`/`.PRE`/bit is its
  own 6-byte read. ST is supported (84 bytes, `{1}`/`{2}` only). RTC/HSC function files and
  A/R/MG/PD files are not, so `RTC:0.HR`/`.MIN` (4 devices) are `bad_config`.
- N and B words come back signed. Masking to uint16 and int→float for `N` tags typed
  float32 (`N66:10`, 9 tags) are done on our side. **Check on site** whether KEPServer
  converts the int or combines two words for those 9 tags.
- Logix packing is greedy by response size against the connection size (4000 B with
  Extended Forward Open, else 500 B). KEPServer's 500 B setting is only applied if the driver
  sets `plc._cfg["connection_size"]` (Phase 2). `read()` does not de-duplicate names; we do
  (229 duplicate Micro800 addresses). Tag lookup is case-sensitive.

### D-009 PCCC-style addresses on a Micro800 device are `bad_config`
One device (Micro800 driver in KEPServer) has 132 PCCC-style addresses (`F28:0`,
`B3:0/14`) that a Micro800 cannot serve. They fail in KEPServer too, so the device is
probably set to the wrong driver. The 13 symbolic names on PCCC devices are `bad_config`
as well.

### D-010 KEPServer importer rules
- **Source** is the raw KEPServer `project.json` (UTF-8 BOM). The old `kep2poller.py`
  output (`kep2poller_output.json`) is used only as a test oracle. Tag names, addresses,
  types, scan rates, IP/port and unit id match it for every device.
- **The 2 OPC UA devices are kept** although their channels ("Test", "Test_OPC1") look like
  test channels. PLAN §1/§5 need them for the `opcua_client` driver. Their credentials are
  never copied; the importer writes env var names `OPENCAP_OPCUA_<CHANNEL>_USER` /
  `_PASSWORD` instead.
- `retries` = KEPServer retry attempts − 1, because Kepware counts the first request (3 → 2).
- Data types: Char → int16 and Byte → uint16, each with a warning. BCD, LBCD and Date tags are
  imported disabled. Default (−1) takes the device's default data type.
- Modbus port comes from the device key (502). The channel port key is not the PLC port.
- Scan mode 2 ("all data at scan rate"): device `scan_ms` = rate, tag rates dropped.
  Scan mode 1 ("no faster than"): `min_scan_ms` = max(default, rate).
- A trailing-space id string such as `"192.0.2.10 "` (one device) is recovered.
  kep2poller dropped this device.
- Kepware's scaled data type has no field in `Scale` and is dropped (20 tags; 4 scale to Word).
- Mitsubishi `plc_type = "Q"` for KEPServer model 5 is a guess. **Confirm on site.** The
  OPC UA security-policy and message-mode code mapping is also unverified.

### D-011 Same-address report
Pairs are counted as n−1 per (device, address), which is how kep2poller reached
**1,248**. Counting all pairs would give 1,533. The report also covers excluded devices,
with a `status` column. Classification heuristic: **suspect** = different data types, a
different last word (`_Y` vs `_B`, START vs STOP), or only an index differs
(LEVEL_1 vs LEVEL_2). **alias** = a copy number (`X` vs `X_1`) or the same last word
(EU_MAX vs LEVEL_1_MAX). It is deliberately conservative: some aliases (e.g.
SEWAGE_LAST_DAY vs SEWAGE_PREVIOUS_DAY_TOTALIZER) come out as suspect.

### D-012 Config layering
Publish settings, lowest priority first: devices.json → `devices_map.yaml` `defaults` →
`overrides.yaml` `publish` → the device's `devices_map.yaml` entry. Default topic:
`opencap/<slug(channel)>/<slug(device)>/data`, lowercase, MQTT-safe (e.g.
`opencap/site_001/8_pump/data`); the `opencap` level is `mqtt.topic_prefix` (D-028). `devices_map.yaml` is never rewritten. New devices are
appended as `stub: true`, or written to `devices_map.stubs.yaml` when appending is not safe.

## Phase 1

### D-013 Modbus driver I/O (pymodbus 3.16)
- `AsyncModbusTcpClient(host, port, timeout, retries=0, reconnect_delay=0)`: pymodbus's own
  retries, auto-reconnect and auto-disconnect are off. The ChannelWorker owns retries and
  backoff. The unit id keyword is `device_id=`.
- The driver's own `device.timeout_ms` timer decides timeouts. pymodbus's timer is set 1 s
  later so a timeout is never confused with another I/O error. Timeout → socket closed
  (`close_on_timeout`, as KEPServer) → `CommError`.
- pymodbus does not fail a request in flight when the peer closes the socket. The driver
  races the request against the connection-lost signal, so a stopped PLC gives
  `ConnectionLost` at once instead of after the timeout.
- Exception codes 01/02/03 → `BlockConfigError` (tags `bad_config`, block skipped until
  reload); other codes → `CommError`.
- `identity()` uses FC43/14 (objects 0x00–0x06 → vendor, product_code, revision…). A timeout
  there does not close the socket, because many devices silently ignore FC43.
- `Driver.connected` is part of the base interface. Before a retry the worker reconnects
  once, immediately, if the socket was closed by a timeout (KEPServer does the same).

### D-014 Test simulator
`tests/helpers/modbus_sim.py` serves synthetic devices on ephemeral ports with an
independent encoder (word, dword and byte order, bit numbering). Breaking any of those in
the driver makes the round-trip tests fail (mutation-checked). `make_sim_devices(30, 53)`
= 30 devices, 1,590 tags, 276 blocks. Fault switches: `mute`, `exception_code`,
`short_reply`. pymodbus's datastore classes are deprecated, so the simulator uses its own
store.

### D-015 Core polling engine
- **One request on the wire per channel.** Each device has its own task, but every connect,
  read and close takes the channel's first-in-first-out wire lock, so devices take turns
  request by request.
- **Cycle outcome:** *good* = at least one block succeeded; *failed* = nothing succeeded
  and at least one block failed on the wire (CommError after retries, ConnectionLost,
  failed connect, or backoff still pending); *neutral* = every block rejected with
  `BlockConfigError` (health unchanged).
- **States:** `offline` at start and after a failed cycle; `ok` after a good cycle;
  `demoted` after `demote_after` consecutive failed cycles (connection closed). When the
  demotion ends, one probe runs: success resumes, failure demotes again.
- **Safety net** on every driver call: `timeout_ms × 1.25 + 0.1 s`. When it fires it counts
  as a CommError and closes the connection.
- Before a retry, a closed socket is reopened at once inside the same retry budget. Only a
  failed `connect()` starts the 1 s → 60 s backoff, which resets on a successful connect.
- Start jitter is U(0, 5 %) of the rate (one-sided). Overrun: missed slots are dropped,
  and the next cycle is due at `max(ended, started + rate)`.
- Stale marking runs from a per-channel tick (between 0.02 and 1 s), not only after cycles.
  Scaling also applies element-wise to arrays.
- `read_once` works on disabled devices, ignores demotion, makes one connect attempt even
  during backoff, feeds health and calls `on_cycle`.
- **Known limit (same as KEPServer):** a device that accepts TCP but never answers slows
  the other devices on its channel until it is demoted. At real settings
  (~9 blocks × 3 attempts × 2 s) that is about 54 s per cycle, roughly 2.7 min before
  demotion. Each real site is one device per channel, so other sites are not affected.

### D-016 Northbound: outbox-always store-and-forward
PLAN §7.3 says to write to SQLite "if publish fails or the broker is down". We write
**every** message (data and status) to the SQLite outbox first (WAL, `synchronous=NORMAL`).
The link drains it in id order, in windows of 32, and deletes a row only after its PUBACK.
This gives ordering, no loss across broker outages, and survival across a process
restart, at the cost of one small SQLite write per message (~15 msg/s for 144 devices at a
10 s interval). QoS 1 means a message can arrive twice at a reconnect. `max_bytes` caps
topic + payload bytes (not file size), and the oldest rows are dropped first
(`saf_dropped_total`).

### D-017 Payload and publishing details
- Floats are rounded to `float_decimals` **decimal places** (PLAN §7.1 says "significant
  decimals"). `-0.0` becomes `0.0`. A NaN or infinite value makes the tag bad.
- A missing SubDeviceId is sent as JSON `null`; the code never invents one.
- A device with no good read since start publishes **no data**, only status messages.
- In `interval` mode the first data message goes out one interval after start.
- `on_change` sends the full snapshot each time. A quality change means good ↔ not good.
  The deadband is absolute and compared with the last published, rounded value.
- Status messages (retained) are sent at start, on a state change (checked every 1 s) and
  every 60 s. `stop()` publishes `offline` (retained) before a clean disconnect. The
  last-will topic is `opencap/poller/<slug(instance)>/status` (prefix configurable, D-028).
- aiomqtt's `publish(qos=1)` returns only on a real PUBACK (paho fires `on_publish` for
  QoS > 0 only from its PUBACK handler). A lost connection does not wake a waiting publish,
  so the link watches `client.messages` to detect a disconnect and opens a new client per
  connection.
- On Python 3.13+ `ssl.create_default_context` rejects a CA certificate without keyUsage.
  Python 3.12 (Docker image) accepts it. Install `tzdata` in the image; without a tz
  database the payload falls back to a fixed +05:30 offset.

### D-018 Phase 1 outage test: what failed, and why it was the test, not opencap
The broker-outage acceptance check failed intermittently while it was being built. Every
cause found was in the test setup. Established by probes (link-PUBACK, broker-parse,
broker-route and subscriber markers on every message):
1. **The link deletes an outbox row only after a real PUBACK.** paho fires `on_publish` for
   QoS 1 only from its PUBACK handler. When the link was cut 20 times right after its outbox
   emptied, it never deleted a message the broker had not parsed.
2. **amqtt PUBACKs on arrival and routes later, at ~1,600 msg/s.** The link drains an
   18,000-message backlog (a 10-minute outage) in ~4 s; amqtt delivered all 18,000 to the
   subscriber 7 s after that.
3. **amqtt discards acknowledged-but-unrouted messages when the publisher disconnects.**
   16 of 20 cuts lost 5–24 messages while amqtt was still busy, none when it had caught up.
   Mosquitto routes before it PUBACKs, so it does not have this window.
4. **Test mistakes:** comparing after a fixed wait (too early for a backlog), and including
   the newest batch, which is always still in transit because the poller keeps publishing
   every interval. The second caused the recurring "one message missing on the last few
   devices" failures, some of which were first blamed on amqtt's shutdown.
Inferred from amqtt's code but not proven by a probe: a broker *shutdown* can also drop
messages it has PUBACKed but not yet routed. The test broker keeps an in-hand counter
around `_handle_message_delivery` as a safeguard, and records every message it accepts.
**The test now:** cuts the network between poller and broker (D-021) once the broker is
idle; after the backlog drains, freezes the set of messages produced so far; waits until
every one of them is at the broker and at the subscriber, in order; fails with the missing
counts otherwise. Production should use a broker with persistence (e.g. Mosquitto,
`persistence true`). The backlog burst after a long outage should be load-tested against
the real SubDevice JSON broker.

### D-020 Acceptance test timing
The Phase 1 acceptance test polls at production-like timing (1 s scan, 1 s timeout, 2 s
publish interval). At 200 ms scans, 30 devices plus the broker and subscriber in one Python
process went briefly `stale` (no good read within 3 × 200 ms) whenever the machine was busy
(load ≈ 9 on 8 cores). That was correct poller behaviour, but a test artefact.

### D-019 Admin API auth
`/health` and `/metrics` are open for monitoring. Every other route needs
`Authorization: Bearer <token>`, read from the env var named by `api.token_env`. With no
token set, the API refuses to start unless bound to a loopback address. Device ids contain
`/`, so routes use a path parameter and the specific `/values` and `/read-once` routes are
matched first.

### D-021 Acceptance outage is a network cut, not a broker shutdown
PLAN Phase 1 says "stopping the broker for 10 min loses no messages". The acceptance test
cuts the TCP path between the poller and the broker (`tests/helpers/netcut.py`: connections
reset, new ones refused) and keeps the broker running. That is what the real IPsec link does,
and it avoids testing amqtt's shutdown behaviour (D-018). **Still to do on a real broker:** a
broker-restart test against Mosquitto with `persistence true` in staging.

### D-022 Test servers that restart use ports outside the ephemeral range
A server restarted on the same port must not sit in the kernel's ephemeral range
(32768–60999 here). During the 10-minute run a reconnecting client was handed the broker's
port as its own source port and connected to itself (TCP self-connect), so the broker could
not restart ("address already in use"). `stable_port()` picks a free port below the range.

## Phase 2

### D-023 Cycle time per site: a periodic INFO summary
PLAN Phase 2 asks for "cycle time per site logged", and PLAN §9 allows only state changes
at INFO. Each device emits one INFO `device.stats` line every `stats_interval_s` (poller.yaml,
default 300 s; 0 = off): cycles, mean and max cycle ms, overruns, bad cycles, state. The
`poll_cycle_seconds` histogram keeps the full distribution for Prometheus.

### D-024 Own CIP/EtherNet-IP simulator for Allen-Bradley tests
`cpppo` cannot serve pycomm3: it lacks Get Instance Attribute List (0x55), and pycomm3 will
not read without an uploaded tag list. libplctag has no Python 3.14 wheel. So
`tests/helpers/cip_sim.py` implements what pycomm3 1.2.16 sends: RegisterSession,
ListIdentity, identity Get Attributes All, (Large) Forward Open/Close, the program name, the
paged tag-list upload, UDT templates, Read Tag, Read Tag Fragmented and Multiple Service
Packet. Stock pycomm3 works against it unmodified.
- Revisions: Logix 20.11 (a 1756-L61 stops at v20, so symbolic addressing), Micro800 12.11.
  pycomm3 uses instance-id addressing only at revision ≥ 21 and never on a Micro800.
- Connection size 4000 with Large Forward Open, 500 with the standard one.
- A refused Multiple Service Packet gets general status 0x08 with no body. pycomm3's own MSP
  path then raises `DataError`, and `generic_message` reports "Service not supported".
- Unknown names: absent from the uploaded list → pycomm3 says "Tag doesn't exist" without
  sending anything; on the wire an unknown symbol gives 0x04, an unknown instance 0x05.
- Read-only: write and set-attribute services (0x4D/0x4E/0x53, 0x02/0x04/0x10) get 0x08 and
  are counted. Not simulated: multi-dimensional arrays, Logix BOOL arrays, arrays of UDTs,
  nested UDTs, program-scoped tags, I/O module tags, connection limits.

### D-025 ab_logix driver on pycomm3 1.2.16, with Micro800 batching
- **Batching hook:** a small `LogixDriver` subclass routes Micro800 reads through pycomm3's
  own `_read_build_multi_requests` when batching is on. It records each reply's CIP status,
  raises on an unusable Multiple Service Packet reply (bad general status, BufferEmptyError
  on 0x08, wrong reply count), keeps the raw reply, reads exactly one encapsulation message
  per reply (pycomm3's reader loops forever if the PLC closes mid-reply), and refuses every
  write and set service. Tests pin each hook with crafted replies through real pycomm3.
- **Probe on connect:** a batched read of two existing tags. Accepted → batching on; 0x08,
  malformed reply, `DataError` or a failing probe tag → off; no reply → connect fails. After
  2 probes in a row with no usable answer, batching stays off without probing. A batched read
  that later fails as a whole switches batching off at runtime (`auto` only). In simulation
  a 402-tag pass took 11 requests batched vs 402 without; at the acceptance level 9 vs ~420
  requests in 3.5 s. **Untested on real Micro800 firmware**: check `opencap identity`.
- **Tag existence:** the tag list is uploaded once per driver instance and reused on
  reconnects (`init_tags=False`), keyed by controller name, product code, serial and
  revision. Names not in it are `bad_config` and never sent. Tags are always addressed by
  name, never by symbol instance id, so a reused list cannot point at the wrong tag. Name
  lookups fall back to case-insensitive (logged once).
- **Errors:** connect failure, reset, peer close or encapsulation error → `ConnectionLost`;
  reply timeout → socket dropped, `CommError`; unusable batched reply → `CommError`; CIP
  0x04/0x05/0x16/0x26 (or 0xFF with 0x2104/05/07) per tag → `bad_config`; other per-tag CIP
  errors → `bad_comm`; unparseable value → `bad_decode`.
- **Core budgets:** `Driver.connect_timeout_s()` lets a driver ask for a longer connect
  (ab_logix: ≥ 60 s for the first tag-list upload, 5 × timeout after). The read safety net is
  `safety × block.wire_requests()`, so a refusing Micro800's 40-request block is not cut off.
- **Known limits:** a program download that changes a tag's type with the same controller
  identity is not detected until restart or reload; tags added in the PLC stay `bad_config`
  until the driver is recreated; one unparseable UDT template fails pycomm3's whole upload.
- `opencap check` estimates Micro800 cost **without** batching (upper bound). With batching
  accepted the real request count is far lower.

### D-026 PCCC simulator for SLC/MicroLogix tests
`tests/helpers/pccc_sim.py` subclasses the CIP simulator and adds the PCCC object (class
0x67, Execute PCCC 0x4B): typed logical reads with 3 and 2 address fields (FNC 0xA2/0xA1),
diagnostic status (CMD 0x06/FNC 0x03) and the system-file directory in each layout pycomm3
picks by catalog prefix. Stock pycomm3 `SLCDriver` works against it unmodified.
- pycomm3 `open()` is TCP + RegisterSession only; the first request sends a Large Forward
  Open, which SLC/MicroLogix refuse (0x08), then a standard one (connection size 500).
  pycomm3 adds route [1, 0] to a bare `ip:port`.
- A missing file, wrong file type or read past the end gets STS 0x10: pycomm3 reports
  "Illegal Command or Format, Address may not exist or not enough elements in data file".
  STS 0xF0 hides the reason in EXT STS, which pycomm3 drops. PCCC writes get STS 0x60 and
  are counted. The 0x10 mapping follows pycomm3's error table; it is not confirmed on real
  hardware.
- `tables_for(tags)` builds data tables that cover a device's addresses, so the 30 real
  PCCC devices can be simulated with their real tag lists and block plans.
- **Real-project finding:** on one SLC device, one tag is `F54:17`
  while its 36 siblings use `N54` (`N54:17`, …). It cannot read on the PLC and is
  `bad_config`. Check it on site; it is almost certainly a typo in the KEPServer project.

## Phase 3

### D-027 ab_pccc driver on pycomm3 1.2.16 `SLCDriver`
- **Shared CIP plumbing:** the safe socket reader (fails on a peer close instead of looping),
  timeout and comm-error helpers and the write-service guard live in
  `drivers/_cip_common.py`; ab_logix imports them unchanged.
- **Connect:** RegisterSession, ListIdentity, a standard Forward Open (500 B; the Large one
  that SLC/MicroLogix refuse is skipped), then the processor type (diagnostic status), so
  connect errors surface in `connect()`. Budget: max(60 s, 20 × timeout) until the file
  directory is known, then 6 × timeout + 1 s.
- **File directory check (`params.file_check`, default true):** read once per driver
  instance, cached by identity, then every block is checked before sending (missing file,
  wrong type, too short → `BlockConfigError`, nothing sent). pycomm3's
  `get_file_directory()` is not used as is: it prints to stdout and loops forever on an empty
  chunk. The driver uses pycomm3's size and chunk reads with a request budget and its own
  parser. The directory is trusted only if it starts with O0, I1, S2 and the last element of
  the highest N/F/L/B file reads while the next one fails with an address error; otherwise
  the check is skipped and the PLC's own errors decide. **The directory layouts come from
  pycomm3 and are marked untested there: check `opencap identity` (`file_directory`, `files`)
  on a real PLC.**
- **Errors:** STS 0x10, 0x50, and 0xF0 with EXT 01–07/09/0A/11/12/13/17 → `BlockConfigError`;
  other STS/EXT, STS 0x60, and CIP errors → `CommError`; CIP 0x01, encapsulation error, reset
  or peer close → `ConnectionLost`; reply timeout → socket dropped, `CommError`. EXT STS is read
  from the raw reply because pycomm3's `Tag.error` drops it.
- **Read-only:** the subclass sends only PCCC (0x0F, 0xA2), (0x0F, 0xA1) and (0x06, 0x03) and
  refuses everything else, including data-log queue reads (they remove entries).
- A block that runs partly past the end of a file makes all its tags `bad_config`.
- **Simulated acceptance, real-shaped:** the 30 real PCCC devices with their real tag lists
  and block plans: 10,298 tags verified (KEPServer scaling included), 21 `bad_config` at plan
  time (RTC, symbolic), 1 rejected by the directory (`F54:17`), 694 blocks → 691 requests/s at
  a 1 s scan (one request per block).

## Open-source project setup

### D-028 Configurable MQTT topic prefix (`mqtt.topic_prefix`)
Every derived topic used to start with a hard-coded `opencap/`. `poller.yaml`
`mqtt.topic_prefix` (default `"opencap"`, so existing deployments are unchanged) now sets
the first level(s) of the default data topic `<prefix>/<channel>/<device>/data` (applied
by the loader), the device status topic `<prefix>/<channel>/<device>/status`, and the LWT
and online topic `<prefix>/poller/<instance>/status`.
- The prefix is used **verbatim**: it is not slugged, so `Acme/plant-1` stays as typed.
  Channel, device and instance levels are still slugged. A prefix may have several
  levels; it is rejected at load time if it is empty, contains `+`, `#` or NUL, starts with
  `$`, or has an empty level (`/x`, `x/`, `x//y`).
- An explicit `publish.topic` (`devices_map.yaml` or `overrides.yaml`) is used as is; the
  prefix does not apply to it.
- `default_topic()`, `status_topic()`, `lwt_topic()` and `Publisher` take the prefix as an
  optional argument that defaults to `opencap`, so existing callers keep working.
- **Reload:** a changed prefix moves the data and status topics at the next reload (the
  loader and the publisher are rebuilt). The MQTT link, and with it the LWT topic and
  every other `mqtt` setting, is built once at start, so those change only at restart.
  A reload that changes `mqtt` logs `mqtt_settings_changed`. Retained status messages
  under the old prefix stay on the broker until they are cleared.

### D-029 CI runs on Python 3.12 and 3.13
D-003: development was on 3.14 only. CI (`.github/workflows/ci.yml`) now runs
`pytest -m "not acceptance"` on 3.12 and 3.13 for every push and pull request, and
`pytest -m acceptance` on both nightly and on manual dispatch (`OPENCAP_OUTAGE_S` is an
input, default 8 s). Running the suite on 3.12.3 and 3.13 exposed no incompatibility; no
code change was needed. A third job builds the wheel, installs it in a clean virtualenv,
builds the container image, and smoke-tests it (`--help`, `import-kep` of the sample
project, `check`, non-root uid). `pyproject.toml` now needs setuptools ≥ 77, which is what
reads the SPDX `license = "Apache-2.0"` expression and `license-files` (PEP 639).

### D-030 Packaging: container image, systemd, logs
- **Image:** `python:3.12-slim` (D-017: 3.12 accepts a CA without keyUsage), a
  two-stage build (wheels, then a runtime image with no build tools). `tzdata` is
  installed for `payload.timezone`. It runs as the non-root user `opencap` (uid 10001)
  from `/opencap`, so the default `saf.path` `data/saf.sqlite` lands in the
  `/opencap/data` volume; config is a second, read-only volume. Entrypoint `opencap`,
  default command `run --config /opencap/config`. `EXTRAS` (build arg) selects drivers.
  The health check probes `/health`, which is liveness only (it answers while the process
  runs).
- **API in a container:** the default `api.bind: 127.0.0.1` is not reachable through a
  published port. The image does not change the default; the deployment guide says to set
  `api.bind: 0.0.0.0` together with a token, which D-019 enforces.
- **systemd:** `Restart=on-failure`, no start limit, secrets from an optional
  `EnvironmentFile`, `StateDirectory=` as the working directory, a hardened sandbox, and
  SIGHUP as `ExecReload`.
- **Logs:** opencap never writes or rotates log files. JSON lines go to stdout, and
  journald, Docker's log driver or WinSW rotate them (`docs/deployment.md`).

### D-031 Windows: no SIGHUP, selector event loop, WinSW
- `Poller.run_forever()` referenced `signal.SIGHUP`, which does not exist on Windows, so
  `opencap run` failed there with an `AttributeError` before it started. The signal table
  now includes SIGHUP only where it exists. On Windows, reload is `POST /reload`.
- aiomqtt (paho) needs `loop.add_reader`, which the default Windows `ProactorEventLoop`
  lacks. `opencap run` passes `asyncio.run(..., loop_factory=event_loop_factory())`:
  `SelectorEventLoop` on Windows, the default loop elsewhere.
- The service wrapper is WinSW v2 (`deploy/windows/opencap-service.xml`). It runs
  `python.exe -m opencap run` (one process, so WinSW's Ctrl+C on stop reaches opencap;
  asyncio turns it into a cancellation, and `run_forever()` then calls `stop()`; asyncio
  then re-raises `KeyboardInterrupt`, which `opencap run` turns into a clean exit 0). It
  restarts on failure and rolls logs by size. That stop path was checked on Linux with
  `add_signal_handler` disabled, as on Windows: `poller_stopped` and every channel closed.
- **Not tested on real Windows yet.** Both fixes are covered by unit tests on Linux; the
  WinSW setup follows WinSW's documentation.

## Rust engine

### D-032 Rust engine slice 1: what is identical, how it is proven, and where it differs
RFC 0001 (`docs/rfc/0001-rust-engine.md`) adds `engine/`: Modbus TCP, MQTT with the outbox,
the admin API and `run`/`check`/`read`/`identity`, ported rule by rule from the Python engine.
- **Proven identical:** the Phase 1 acceptance suite passes against the Rust binary with the
  same simulators, broker and assertions (`OPENCAP_ENGINE=rust`, adapter in
  `tests/helpers/engine.py`; also with a 60 s outage: 930 messages produced during the cut,
  0 lost). 229 golden payload cases generated by Python are byte-identical
  (`tests/helpers/payload_golden.py`). `check -v` (every block, warnings) and `read` print the
  same text. An outbox file written by either engine is drained or read by the other.
- **Byte identity needs CPython's float rules:** `round(x, n)` is exact decimal rounding with
  ties to even (Rust's exact `{:.n}` formatting matches it); `json.dumps` writes floats with
  `repr()` (shortest round-trip digits, exponent form below 1e-4 and from 1e16, two-digit
  exponents); strings are escaped as with `ensure_ascii=False` (`\u00XX` in lower case).
  Timestamps are truncated to microseconds, Python's precision.
- **Deliberate differences** (RFC 0001 §2.9): an unknown `driver` name loads and is reported
  as not implemented instead of rejecting `devices.json`; invalid driver `params` are a plan
  error for that device instead of stopping the process; no pydantic lax coercion; YAML 1.2
  (`yes`/`no` are strings: write `true`/`false`); a Modbus peer close on an idle socket is
  seen on the next request (ConnectionLost, reopen, same attempt) instead of before it;
  `read-once` on an unusable device returns 500 with a `detail` body; `/metrics` has no
  `_created` samples; log lines carry the event name in `message` instead of `event`.
- **rumqttc acknowledgements:** `publish()` returns when the request is queued, not on
  PUBACK. The link matches each PUBACK to its outbox row through the order of rumqttc's
  `Outgoing::Publish(pkid)` events, deletes a row only after its PUBACK, and uses a new client
  and event loop per connection. rumqttc reports the same error for an invalid topic and for
  a closed event loop; the link drops a row only if the topic is invalid, otherwise it treats
  the error as a lost connection and keeps the row.
- **Test-only hook:** `OPENCAP_PUBLISH_TAP=<file>` makes the engine append every message
  handed to the MQTT link to that file (JSON lines) before it enters the outbox; the
  conformance adapter uses it where the Python test spied on `link.publish`.

## Serial PLCs: Allen-Bradley DF1, Modbus RTU (M1)

### D-033 Serial transports, and one connection per physical line
- `params.transport` selects how a serial line is reached: `tcp` (raw TCP to a serial-over-TCP gateway or
  virtual COM server, the migrated site's setup), `serial` (a local port; the device path goes in `Device.host`,
  `port` 0, so no new field and the API/`check` still show where the device is), `rfc2217`, or `unmapped`.
  pyserial is the optional extra `serial`; a TCP gateway needs no extra package.
- **Shared lines.** Several devices on one line (stations behind a bridge, unit ids on RS-485, several KEPServer
  devices on one COM port) must share one connection: a serial port opens once (pymodbus opens it exclusively)
  and many gateways accept one TCP client. `_serial.LinkPool` (one per event loop) hands every driver on the same
  endpoint (kind, host, port) the same open link; an error that closes the line closes it for all of them (they
  reconnect on their next request); the last driver to leave closes it. The ChannelWorker's wire lock already
  allows one request per channel; a per-line lock also serialises devices of different channels pointed (by
  mistake) at one line.
- Errors: open failure, peer close, hang-up (EIO on a tty) → `TransportClosed`, a `ConnectionLost`; a read
  timeout is a `TimeoutError` that the drivers turn into `CommError`.
- RFC 2217 goes through pyserial's `rfc2217://` client (polled reads, no file descriptor), tested against
  pyserial's own `PortManager`. pymodbus' serial transport needs a file descriptor, so modbus_rtu offers no RFC 2217.
- Test artefact: a Linux pty refuses parity on every open but the first (EINVAL from tcsetattr); the pty tests
  use 8N1. Real serial ports do not do this.

### D-034 ab_df1: own DF1 full-duplex link layer, PCCC reads from the ab_pccc planner
- No maintained Python DF1 library exists, so the link layer is in `drivers/ab_df1.py` (DF1 manual 1770-6.5.16):
  DLE STX/ETX framing, DLE stuffing (check bytes are not stuffed), BCC and CRC-16/ARC (over data + ETX, low byte
  first), DLE ACK/NAK/ENQ (also embedded in a frame), NAK → resend (`nak_retries` 3), ACK timeout → ENQ
  (`ack_timeout_ms` 1000, `enq_retries` 3), duplicate detection on SRC/CMD/STS/TNS. The whole transaction is
  bounded by `device.timeout_ms`; the ChannelWorker's retries use a new TNS.
- **Reads:** one `ab_pccc` block = one protected typed logical read, CMD 0x0F FNC 0xA2 with exactly the fields
  pycomm3 sends over EtherNet/IP (cross-checked against pycomm3's `parse_tag`); `read_fnc: a1` selects the
  two-field variant. Reply bytes become exactly what pycomm3's `_parse_read_reply` returns (cross-checked on random
  data for every block kind), so `ab_pccc.decode_block` decodes both drivers identically. `plan_pccc()` was pulled
  out of `AbPcccDriver.plan` (behaviour unchanged) so both drivers share it.
- **Reply matching by TNS and CMD | 0x40 only, not by SRC.** On a point-to-point full-duplex link the PLC answers
  from its own configured station number whatever DST said; checking SRC would reject every reply of a PLC whose
  station differs from the KEPServer device id. A different SRC is counted (`source_mismatch`) and logged once.
  Late and duplicate replies are ACKed and dropped (a late reply after a timeout is never taken for a newer request).
- `close_on_timeout` defaults to **false** (Modbus: true): TNS matching makes a late reply harmless, and closing a
  serial port or gateway socket does not help a PLC that does not answer; demotion still closes the line.
- No file-directory check over DF1 (reading system file 0 is ~35 extra requests on a slow line): a block for a
  missing file is rejected by the PLC's STS 0x10 → `BlockConfigError`, as `ab_pccc` does without a directory.
- **Known DF1 gap (spec behaviour):** if a frame is lost entirely after earlier traffic, the PLC answers our ENQ
  with its last response (the ACK of the previous frame); we then wait for a reply that never comes and the
  attempt fails within `timeout_ms`. The worker's retry succeeds. On a fresh line the last response is NAK and the
  frame is resent at once.
- Not supported: half duplex, PLC-5 typed reads (FNC 0x68), DH+/DH-485 routing beyond DST/SRC, unsolicited PLC
  messages (ACKed, dropped), writes (only (0x0F, 0xA2), (0x0F, 0xA1), (0x06, 0x03) can be sent).

### D-035 DF1 check vectors are not the manual's own
The DF1 manual's worked example frames could not be retrieved from this environment (no copy of 1770-6.5.16
reachable). The tests use instead: the CRC-16/ARC catalogue check value (b"123456789" → 0xBB3D, the algorithm the
manual specifies), frames computed with an independent bitwise CRC and checked by hand for the BCC (including a
check byte equal to DLE), random round trips split at every chunk boundary, and the simulator's separate link
implementation. Breaking the CRC scope (no ETX), duplicate detection, TNS matching, ENQ, NAK, retransmission or
word signedness each fails the tests (mutation-checked). **Still to do:** compare one captured frame from the site
(or the manual) byte for byte.

### D-036 modbus_rtu on pymodbus' RTU framer
- `ModbusRtuDriver` subclasses `ModbusTcpDriver`: same params, parser, planner, decoder, error mapping and
  identity (FC43/14). Wire: `AsyncModbusSerialClient(framer=RTU)` for `serial`, `AsyncModbusTcpClient(framer=RTU)`
  for `tcp` (RTU over TCP). Units on one line share the client (D-033).
- pymodbus does not keep the RTU inter-frame silence: the driver waits `inter_frame_ms` (default 3.5 characters at
  `baudrate`, ≥ 1.75 ms) since the last frame on the line before each request.
- RTU has no transaction id, so a reply that arrives after its request timed out could be taken for the next
  request's (pymodbus only checks unit id and function code). `close_on_timeout` stays true by default: closing
  the line throws such late bytes away.
- The RTU simulator is pymodbus' own server with the RTU framer (`ModbusSim(framer="rtu")`, silent for unknown
  unit ids like a serial bus, `add_unit` for multi-drop); `PtyBridge` puts it behind a tty for the serial path.

### D-037 Importer and loader: DF1 devices are imported, unmapped until overrides.yaml maps the COM port
- "Allen-Bradley DF1" devices are no longer excluded ("DF1 serial: Phase 7" is gone): they become `ab_df1` with
  `transport: unmapped`, `host` = the COM port name (`COM112`), `port` 0, the channel's serial settings, the
  device's station and error check. Raw KEPServer serial/DF1 values are kept in `source.kep_serial` / `kep_df1`.
- `overrides.yaml` gets `serial_ports: {COMnnn: "host:port" | "tcp://…" | "rfc2217://…" | "/dev/…"}`. The loader
  maps every unmapped serial device whose `params.com_port` matches (case-insensitive); per-device overrides win.
  A device that stays unmapped is **disabled with a warning** (not polled, not published; `opencap check` shows
  it `(disabled)` with its block plan); a bad mapping or an unsupported transport is a config error.
- `opencap read` now prints the device's last error on stderr when tags are not good (it printed only
  `bad_comm`; this helps every driver).
- **Unverified KEPServer keys (no real DF1 export here):** serial settings are read from
  `servermain.CHANNEL_SERIAL_COMMUNICATIONS_{COM_ID,BAUD_RATE,DATA_BITS,PARITY,STOP_BITS}` (with a suffix match as
  fallback), parity as ASCII codes (78 N, 69 E, 79 O) or names; the error check from any DF1-prefixed key named
  `*ERROR_CHECK*`/`*CHECKSUM*` (strings with CRC/BCC trusted; 0/1 read as BCC/CRC and flagged); absent → CRC,
  flagged. The station id is read as decimal; ids ≥ 8 are flagged because the meaning of
  `servermain.DEVICE_ID_FORMAT` (octal/decimal/hex codes) is not verified. Half duplex in a link-protocol key is
  flagged. **Check all of these against the real export** (`reports/import_summary.json` lists each one).

### D-038 Simulated serial acceptance; real hardware still to do
`tests/integration/test_serial_acceptance.py` (acceptance): 5 DF1 devices on 4 lines (CRC, BCC, two stations on one
line, one line behind a tty mapped as a local serial port) and 4 Modbus RTU devices on 3 lines (mapped through
`serial_ports`, configured by hand, two units on one line), plus one unmapped DF1 device, through the whole poller
and MQTT: correct values, values follow the PLC, NAKs and corrupted replies cost nothing, a gateway power-off
demotes only its devices and they resume, nothing but reads on any line, the unmapped device is never polled.
**Still to do on real hardware:** a MicroLogix/SLC on a real RS-232 port and through the site's router gateways
(map the real COM ports; check error check, station numbers, `request_size` — a PLC that refuses large reads
answers STS 0x10 and the block becomes `bad_config` — and ACK timing over the IPsec link); a Modbus RTU slave on
RS-485 (inter-frame timing, local echo); one byte-for-byte frame capture (D-035); RFC 2217 against a real
COM-port server.

### D-059 Config JSON Schema exported from the Pydantic models (#2)
`python -m opencap.config.schema --out schema/` writes `schema/{devices,poller,devices_map,overrides}.schema.json`
(JSON Schema 2020-12, sorted keys, `$id`, top-level `version`; the `$id` base is the repo URL, not a hosted
endpoint). A drift test regenerates and compares. Conformance fixtures in `tests/conformance/config/` are
`{"document", "data"}` envelopes (invalid ones add `expected_error` and `semantic`, true when only Pydantic's
validators, not the schema, can reject it) so the Rust engine can load them without knowing the file naming.
`jsonschema` (MIT) is a dev dependency only. The CLI is not wired into `opencap.__main__` yet (issue scope).
