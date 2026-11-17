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

## Configuration API and web UI

### D-032 Configuration API: the 03 §3.6 contract, and where it differs
- **Paths** follow [03 §3.6](architecture/03-deep-dive.md): `/api/v1/channels`, `/channels/{ch}/devices`,
  `/devices/{id}`, `/devices/{id}/tags[/{tag}]`, `/project` (the four files), `/project/import`,
  `/project/validate` (= `opencap check`), `/project/revisions`. Added because the v1 file layout has
  them: `/map[/defaults|/devices/{id}]` (`devices_map.yaml`) and `/overrides/devices/{id}`,
  `/overrides/channels/{ch}` (`overrides.yaml`); `/project/status`, `/project/pending`,
  `/project/schema`, `/whoami`, `/status` (dashboard in one call), `/auth-info` (open: does the API
  need a token?), and `/devices/{id}/status|values|plan|identity`, `POST …/read-once`, `POST /reload`
  under `/api/v1` (the unversioned runtime routes stay for existing clients). `/north` and `/plugins`
  wait for north plugins; the WebSocket feed and the committed OpenAPI document are #31 and #32.
- **Writes need `If-Match`** with the config revision: 428 without it, 412 when it is stale (409 stays
  for name clashes). The revision is a hash of the four files' bytes, not a counter, so it also
  changes on hand edits.
- **Deviation: a write does not apply itself.** §3.6 reads "persisted, audited, and then applied";
  the task for this PR says `POST /reload` applies, and an engineer editing twenty tags wants one
  restart of the channel, not twenty. Writes change the files; `POST /api/v1/reload` applies, or
  `?apply=true` on any write reloads right after it. `GET /api/v1/project/pending` says what a
  reload would restart (per channel, and poller.yaml settings that need a process restart:
  instance, log_level, mqtt, saf, api).
- `PUT` replaces, `PATCH` is a JSON merge patch (RFC 7386). Device ids are `<channel>/<device>`, so
  new or renamed channel/device names may not contain `/`, and a device may not be named `tags`,
  `values`, `plan`, `identity`, `status` or `read-once` (they would collide with the
  `/devices/{id}/<suffix>` routes).
- **Every change** is validated by its pydantic model (errors keep a location relative to the
  object, for inline UI errors), then the new texts are loaded by the real loader in a staging
  directory, then the affected devices are planned: a driver that rejects the params, or an
  enabled tag whose address/dtype/enabled changed and does not plan, is a 422. Plan-invalid tags the
  change did not touch are warnings, so an imported `bad_config` tag does not block other edits.
  A device whose driver is not implemented is a warning. Empty names, addresses and hosts are refused
  (the models accept empty strings).
- **Renames carry references** (a channel or device rename renames its keys in `devices_map.yaml`
  and `overrides.yaml`, a tag rename its tag override); **deletes cascade** to the map entry and the
  overrides. The audit entry holds everything removed.
- `ConfigStore` (`config/store.py`) is the `ProjectStore` of [ADR-0013](adr/0013-project-file-v2.md)
  over today's v1 layout: when project file v2 lands, the store changes and the API does not.
  Channels, devices and tags are in `devices.json`, which a KEPServerEX re-import replaces; the
  import dry run shows what would be lost, and fixes that must survive belong in `overrides.yaml`.
- `GET /api/v1/devices/{id}/identity` asks the driver over the device's existing connection only
  and never opens one (Micro800s have few CIP connections): `ChannelWorker.identity()` and
  `Poller.device_plan()/device_identity()` are new, additive. `core/plan_report.py` is the JSON form
  of `opencap check`; `check.py` is unchanged.

### D-033 Named tokens and roles (extends D-019)
- `poller.yaml` `api.tokens: [{name, role, token_env}]`; the secret stays in the env var. A listed
  token whose env var is unset, or two tokens with the same secret, stop the start. The legacy
  `api.token_env` token is the admin named `default` (the name is reserved).
- **viewer** = safe methods (GET/HEAD) only. **admin** = also every unsafe method: config changes,
  import, reload and read-once. Read-once is a PLC *read*, but it puts a full extra cycle on the wire
  on demand (≈23 s of requests on a non-batching Micro800), so it is not a viewer action. This is the
  two-role subset of the viewer/operator/engineer/admin set the security RFC will define (03 §3.7).
- Tokens are read at start; a change needs a restart, like the other `api` settings.
- With no token at all (loopback only, D-019) everyone is the admin `anonymous`. New: an unsafe
  request a browser marks as cross-site (`Sec-Fetch-Site`/`Origin`, unless listed in
  `api.cors_origins`) is refused, so a web page cannot drive the open local API.
- `api.cors_origins` (default empty) lets the desktop app or another UI origin call the API
  (ADR-0010); the API uses bearer tokens only, never cookies, so there is no CSRF surface.

### D-034 Atomic writes and comments in the hand-owned YAML files
- Each file is written to a temp file in the same directory, fsynced, renamed over the original;
  the previous version is kept as `<file>.bak` (one generation, a hard link to the old inode).
  Permissions are copied. `config/*.bak` and the temp files are git-ignored.
- A change touching several files (e.g. a device rename) writes them one by one. Each write is atomic
  and every intermediate state loads (unknown ids in the map/overrides are warnings), and
  `POST /reload` takes the store's lock, so a reload never sees half a change. `SIGHUP` does not.
- **Comments are kept** with ruamel.yaml's round-trip mode: the change is made on the plain data the
  loader reads and synced into the existing document (new keys appended, renamed keys keep their
  place). The result is parsed again with PyYAML (the loader's parser); if it differs from the
  intended data, the file is rewritten from the data instead, keeping the leading comment block, and
  the response lists it under `comments_lost`. Known effects: a renamed key's trailing comment is
  re-aligned; a touched YAML 1.1 bare scalar (`off`, `yes`) is written as `false`/`true`; strings
  PyYAML would misread are quoted. Without ruamel.yaml every YAML change takes the fallback.

### D-035 Import through the API
- `POST /api/v1/project/import` takes the file as the raw JSON body (no multipart parser): a
  KEPServerEX `project.json` (top-level `project`), run through the existing importer, or an opencap
  export (`GET /api/v1/project`). **Dry run is the default**; `?dry_run=false` writes and needs
  `If-Match` (the UI sends the dry run's revision, so the diff seen is the change applied). The body
  must be `application/json`, at most 64 MiB.
- KEPServerEX import = `opencap import-kep` minus the reports: `devices.json` replaced, stub entries
  appended to `devices_map.yaml` for new device ids, `overrides.yaml` template written if missing; the
  summary and problems are in the response instead of `reports/`. **Deviation:** the API passes
  poller.yaml `defaults` to the importer (as poller.yaml documents); the CLI passes the built-in
  defaults. They differ only when poller.yaml changes `defaults`.
- An export restores files byte-for-byte, also over files that no longer parse.

### D-036 Audit log
JSON lines at `api.audit_log` (default `data/audit.jsonl`): time, token name, role, `via` (`ui` when
the web UI sent it, else `api`), action, target, new revision, files and field-level changes
(old → new, at most 200 per entry). Logged: every accepted change, import and reload. Not logged:
rejected changes (nothing changed), reads, read-once. Logins are not logged because the UI has no
server-side login (it sends the token with each request). Rotates to `.1` at 16 MiB;
`GET /api/v1/audit` reads both, and `/project/revisions` lists the revisions it recorded.

### D-037 Web UI: a static bundle on the public API, Alpine.js CSP build
- Per [ADR-0010](adr/0010-one-ui-web-and-tauri.md): `src/opencap/api/ui/` (`index.html`, `app.js`,
  `app.css`) is served as static files under `/ui/` and calls only `/api/v1` (a unit test checks
  every call site). No build step. Hash routes (`#/device/<id>`, `#/edit/<id>`, …) so the same files
  work from any origin and inside the Tauri shell; `window.OPENCAP_API_BASE` points it at a remote node.
- **Alpine.js 3.17.5, CSP build** (`@alpinejs/csp`, MIT), vendored as
  `ui/vendor/alpine-csp-3.17.5.min.js` with `alpine-LICENSE.md` (from the alpinejs/alpine repo at
  tag v3.17.5; the npm package ships none) and listed in `NOTICE`; sha256
  `48c170c92032e527cbae71b8ce75cf1f96ca06fa869a6120ef0fe438d8e92c36`. The CSP build evaluates
  expressions without `eval`, so the UI runs under `script-src 'self'; style-src 'self'` with no
  inline script or style. Its expressions cannot use template literals or globals, so formatting
  lives in component methods. htmx was the other candidate; it swaps server-rendered HTML, which
  ADR-0010 rules out.
- **Login:** the token is checked with `GET /api/v1/whoami`, kept in the tab's `sessionStorage`
  (gone when the tab closes; nothing is stored server-side) and sent as `Authorization: Bearer` with
  `X-Opencap-Client: ui`. A 401 sends the UI back to the login page. Writes send `If-Match` with the
  revision of the data on screen; a 412 tells the operator to load the page again.
- Textareas for params, overrides and publish defaults take JSON (the API's format), not YAML.
- **Read-only towards PLCs:** the UI offers read-once and identity, never a value write.
- A browser smoke test (`tests/ui/browser_smoke.js`, run by `tests/integration/test_ui_browser.py`,
  marker `acceptance`) drives Chromium through every page at desktop and phone width and fails on any
  script error or CSP violation; it is skipped where Node.js and Playwright are not installed.
