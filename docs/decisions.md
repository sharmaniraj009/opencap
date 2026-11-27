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
`opencap/site_001/8_pump/data`); the `opencap` level is `mqtt.topic_prefix` (D-028). `devices_map.yaml` is never rewritten by an import. New devices are
appended as `stub: true`, or written to `devices_map.stubs.yaml` when appending is not safe.
**Amended by D-041:** the config API and the web UI edit entries of `devices_map.yaml` in
place when an operator changes them, keeping comments and key order (falling back to a
rewrite that keeps the header and is reported); an import through the API still only appends.

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
  WinSW setup follows WinSW's documentation. *(Superseded by D-082: CI tests it on Windows.)*

### D-082 Windows CI: what running on windows-latest found (#35)
CI now runs the fast suite (and the acceptance suite nightly) on windows-latest with Python
3.12, plus a WinSW service smoke test (`deploy/windows/service_smoke.py`). D-031's fixes hold:
the shipped `opencap-service.xml` installs, starts, answers `/health` with MQTT connected, and on
`stop` WinSW's Ctrl+C gives `poller_stopped` and exit code 0 in about half a second.
- **Expected:** the tests would run unchanged. **Found:** they ran on asyncio's default Windows
  loop (proactor), where aiomqtt cannot work, so every MQTT test hung to its timeout.
  `tests/conftest.py` now selects the selector loop on Windows, as `opencap run` does. That loop
  cannot start asyncio subprocesses on Windows, so test helpers start child processes with
  `subprocess` in a worker thread (`run_process` in `tests/helpers/engine.py`).
- **Platform differences, not product bugs**, handled in the tests: no POSIX mode bits, SIGKILL
  or SIGHUP; a refused TCP connect is reported only after 1-2 s of SYN retries; a UDP datagram
  to a closed port comes back as `ConnectionResetError`; a socket closed with unread data is
  reset (WebSocket 1006 before 1009); `socket.socketpair()` is a loopback TCP connect; text-mode
  writes turn `\r\n` into `\r\r\n`. Tests that need a POSIX pty skip with a reason.
- **Python 3.12 on Windows:** `time.monotonic` ticks every ~15.6 ms and asyncio may run a timer
  that early. Timing tests allow the clock's resolution. One product fix came out of it: the
  system-tag tick (spec §2.1) fired up to ~22 ms before its UTC second, so `_System._DateTime`
  read `19.978` for second 20 and second 19 was ticked twice; the task now waits out a wake that
  is less than 250 ms early (a clock stepped back is not waited out). Cycle-time metrics are
  quantised to that tick (a fast cycle reads 0 ms): #199 measures durations with `perf_counter`.
- **WinSW 2.12 quirks:** `stop` can return before the service enters "stop pending", and
  `status` prints `Started` for anything but Stopped, so the smoke test polls `status`. Logs are
  named after the executable (`opencap-service.out.log`), not the service id.

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

### D-059 Licence gate: allow-list over installed distributions, `OR`/`AND` aware (#6)
`scripts/check_licenses.py` + `licenses/python-allow.toml` check every distribution installed from `.[all,dev]`.
The pip classifier "BSD License" is ambiguous (2- or 3-clause) and is treated as BSD-3-Clause since both are allowed.
`A OR B` passes when either side is allowed (cryptography, paho-mqtt); `A AND B` needs both. Exceptions: `asyncua`
(LGPL-3.0, imported unmodified, ADR-0007). MPL-2.0 is allowed (certifi, unmodified). A root `deny.toml` is the
project-wide Rust floor (adds MPL-2.0 and Unicode-DFS-2016); `engine/deny.toml` stays the engine owner's file.
**Not verified here:** the cargo-deny job has not run locally (no network for installing it); it runs in CI.

### D-060 Config JSON Schema exported from the Pydantic models (#2)
`python -m opencap.config.schema --out schema/` writes `schema/{devices,poller,devices_map,overrides}.schema.json`
(JSON Schema 2020-12, sorted keys, `$id`, top-level `version`; the `$id` base is the repo URL, not a hosted
endpoint). A drift test regenerates and compares. Conformance fixtures in `tests/conformance/config/` are
`{"document", "data"}` envelopes (invalid ones add `expected_error` and `semantic`, true when only Pydantic's
validators, not the schema, can reject it) so the Rust engine can load them without knowing the file naming.
`jsonschema` (MIT) is a dev dependency only. The CLI is not wired into `opencap.__main__` yet (issue scope).

### D-061 Rust engine follow-ups from the #64 review (#66)
- **Unknown `driver` is a config error** in Rust as in Python (`DriverName` literal). `KNOWN_DRIVERS` now lists all eight
  names (adds `modbus_rtu`, `ab_df1`); the message is `<device id>: driver: Input should be 'a', 'b' ... or 'h' (got 'x')`.
  A known driver the Rust build does not implement still loads and is reported "not implemented". This supersedes the
  earlier "driver is an open string" note in `models.rs`.
- **YAML 1.1 booleans** (`yes`/`no`/`on`/`off`, also `y`/`n`) are strings under YAML 1.2 (serde_yaml_ng). Where a bool is
  expected, the error now names the spelling and says to use `true`/`false`. They are not silently accepted: files must mean
  the same thing to both engines, and PyYAML-only spellings are a footgun.
- **`driver_panics_total{device}`** (counter) is incremented each time a device loop restarts after `device.loop_error`.
- **Read-only guard:** the Modbus function-code match is exhaustive; a block with FC outside 1-4 is `BlockConfig`, never
  FC04. `tests/modbus_read_only.rs` scans the driver sources for write APIs and checks planned FCs.
- **Advisories:** `cargo deny check advisories` runs in CI. Vulnerabilities fail; yanked releases warn; "unmaintained"
  notices do not fail (`unmaintained = "none"`). First CI run showed why: `chrono` and `chrono-tz`, both direct dependencies,
  are flagged unmaintained (RUSTSEC-2026-0543/0555, no fix). Follow-up: evaluate `jiff` or `time` for time zones.
- **rusqlite 0.37 -> 0.41** (libsqlite3-sys 0.35 -> 0.39.1, bundled SQLite 3.54): the new advisories check flagged the
  bundled SQLite of 0.35 (CVE-2026-119375, fixed in libsqlite3-sys 0.39). No code changes were needed. It adds two
  wasm-only crates (`rsqlite-vfs`, `sqlite-wasm-rs`) to the lock file; the licence gate covers them in CI.
- **RUSTSEC-2026-0382, -0049, -0098, -0099, -0104 ignored (temporary):** all are `rustls-webpki` 0.102.8, which
  comes only through `rumqttc` 0.25.1 (latest; pins `^0.102.8`). rumqttc uses it only for the `webpki::Error` type;
  verification runs in rustls 0.23 on the patched webpki 0.103.17 in the lock file. Ignored in `engine/deny.toml`,
  each with the reason; remove when rumqttc moves to webpki >= 0.103.16. The lead should confirm these ignores.
- **Not done:** pinning third-party actions in `rust-engine.yml` by commit SHA. The session could not read those
  repositories to resolve tags to SHAs, and a SHA written from memory would be unsafe. Left on the issue as a follow-up.
- **RFC 0001:** direct-dependency table completed with licence links at the lock-file versions (checked against the
  crate sources); OPC DA bridge order made consistent with §8 (R5); "0600 token file" marked planned.
- **Harness:** `rust_binary()` prefers `OPENCAP_ENGINE_BIN` and warns when it falls back to `target/{release,debug}`.

## Configuration API and web UI

### D-039 Configuration API: the 03 §3.6 contract, and where it differs
- **Paths** follow [03 §3.6](architecture/03-deep-dive.md): `/api/v1/channels`, `/channels/{ch}/devices`,
  `/devices/{id}`, `/devices/{id}/tags[/{tag}]`, `/project` (the four files), `/project/import`,
  `/project/validate` (= `opencap check`), `/project/revisions`. Added because the v1 file layout has
  them: `/map[/defaults|/devices/{id}]` (`devices_map.yaml`) and `/overrides/devices/{id}`,
  `/overrides/channels/{ch}` (`overrides.yaml`); `/project/status`, `/project/pending`,
  `/project/schema`, `/whoami`, `/status` (dashboard in one call), `/auth-info` (open: does the API
  need a token?), and `/devices/{id}/status|values|plan|identity`, `POST …/read-once`, `POST /reload`
  under `/api/v1` (the unversioned runtime routes stay for existing clients). `/north` and `/plugins`
  wait for north plugins; the WebSocket feed and the committed OpenAPI document are #31 and #32.
- **Reads return `revision` and `ETag`**, list reads included (`{"channels": […], "revision": …}`).
  **Writes need `If-Match`** with the config revision: 428 without it, 412 when it is stale (409 stays
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
  and never opens one (Micro800s have few CIP connections). Asking can cost a request on the wire
  (Modbus FC43), so the answer is cached for 5 minutes and `?refresh=true` is admin-only: `ChannelWorker.identity()` and
  `Poller.device_plan()/device_identity()` are new, additive. `core/plan_report.py` is the JSON form
  of `opencap check`; `check.py` is unchanged.

### D-040 Named tokens and roles (extends D-019)
- `poller.yaml` `api.tokens: [{name, role, token_env}]`; the secret stays in the env var. A listed
  token whose env var is unset, or two tokens with the same secret, stop the start. The legacy
  `api.token_env` token is the admin named `default` (the name is reserved).
- **viewer** = safe methods (GET/HEAD) only. **admin** = also every unsafe method: config changes,
  import, reload and read-once. Read-once is a PLC *read*, but it puts a full extra cycle on the wire
  on demand (≈23 s of requests on a non-batching Micro800), so it is not a viewer action. This is the
  two-role subset of the viewer/operator/engineer/admin set the security RFC will define (03 §3.7).
- Tokens are read at start; a change needs a restart, like the other `api` settings.
- **Open mode** (no token at all, loopback bind only, D-019) is now **read-only**: everyone is the
  viewer `anonymous`, every unsafe method gets 403, and only requests whose `Host` is `127.0.0.1`,
  `localhost` or `[::1]` are answered. A page using DNS rebinding sends its own host name, so it
  can neither read nor change anything. (Before, open mode allowed `POST /reload`; comparing
  `Origin` with `Host` did not stop rebinding, review of #63.)
- **Authentication happens before the body is read**: a pure ASGI guard (`api/guard.py`) in front of
  the app authenticates every non-open route from its headers, so an unauthenticated caller gets
  401 without a body byte read or parsed. It also caps bodies at 1 MiB (64 MiB for
  `POST /api/v1/project/import`): a larger `Content-Length` gets 413 at once, and a chunked body
  is counted while it is read.
- `api.cors_origins` (default empty) lets the desktop app or another UI origin call the API
  (ADR-0010); the API uses bearer tokens only, never cookies, so there is no CSRF surface.

### D-041 Atomic writes and comments in the hand-owned YAML files
- Each file is written to a temp file in the same directory, fsynced, renamed over the original;
  the previous version is kept as `<file>.bak` (one generation, a hard link to the old inode).
  Permissions are copied. `config/*.bak` and the temp files are git-ignored.
- **A change touching several files** (e.g. a device rename) is committed in two phases: every temp
  file is written and fsynced first, the `.bak` links are made, then the renames. If any step fails,
  the files already renamed get their previous bytes back and nothing changed (503); only if that
  restore also fails is the result a partial write (500, naming the files left new, audited as
  `<action>.partial`). A fault-injection test makes the second rename of a device rename fail.
  `POST /reload` takes the store's lock, so a reload never sees a change in progress; `SIGHUP` does
  not.
- **The audit entry is written inside the lock**, right after the files. If it cannot be written,
  the change is rolled back (503): there are no unaudited changes.
- **Read-only or full storage** gives 503 (507 for a full disk) with the path, never a bare 500.
  `GET /api/v1/project/status` and `/whoami` report `writable` (`access()` plus the read-only mount
  flag), and the UI turns editing off when it is false. `deploy/opencap.service` sets
  `ReadWritePaths=/etc/opencap`; `docs/deployment.md` makes the directory sticky (1775,
  root:opencap) with opencap owning only the config files, so opencap can replace them but not
  `opencap.env`, and explains how to keep the config read-only (and the Docker mount for editing).
  The sticky-bit layout follows POSIX semantics; it was not exercised on a real systemd host.
- **Comments are kept** with ruamel.yaml's round-trip mode: the change is made on the plain data the
  loader reads and synced into the existing document (new keys appended, renamed keys keep their
  place). The result is parsed again with PyYAML (the loader's parser); if it differs from the
  intended data, the file is rewritten from the data instead, keeping the leading comment block, and
  the response lists it under `comments_lost`. Known effects: a renamed key's trailing comment is
  re-aligned; a touched YAML 1.1 bare scalar (`off`, `yes`) is written as `false`/`true`; strings
  PyYAML would misread are quoted. Without ruamel.yaml every YAML change takes the fallback.

### D-042 Import through the API
- `POST /api/v1/project/import` takes the file as the raw JSON body (no multipart parser): a
  KEPServerEX `project.json` (top-level `project`), run through the existing importer, or an opencap
  export (`GET /api/v1/project`). **Dry run is the default**; `?dry_run=false` writes and needs
  `If-Match` (the UI sends the dry run's revision, so the diff seen is the change applied). The body
  must be `application/json`, at most 64 MiB.
- KEPServerEX import = `opencap import-kep` minus the reports: `devices.json` replaced, stub entries
  appended below the existing `devices_map.yaml` text with the CLI's own helpers (never a rewrite),
  or written to `devices_map.stubs.yaml` when appending is not safe; `overrides.yaml` template
  written if missing. The summary and problems are in the response instead of `reports/`. **Deviation:** the API passes
  poller.yaml `defaults` to the importer (as poller.yaml documents); the CLI passes the built-in
  defaults. They differ only when poller.yaml changes `defaults`.
- An export restores files byte-for-byte, also over files that no longer parse. It can replace
  `poller.yaml` (including `api`), so it is admin-only like every import, and its audit entry holds
  a unified diff of each YAML file and a device-level summary for `devices.json`.

### D-043 Audit log
JSON lines at `api.audit_log` (default `data/audit.jsonl`): time, token name, role, `via` (`ui` when
the web UI sent it, else `api`), action, target, new revision, files and field-level changes
(old → new, at most 200 per entry). Logged: every accepted change, import and reload. Not logged:
rejected changes (nothing changed), reads, read-once. Logins are not logged because the UI has no
server-side login (it sends the token with each request). Rotates to `.1` at 16 MiB;
`GET /api/v1/audit` reads both, and `/project/revisions` lists the revisions it recorded.

### D-044 Web UI: a static bundle on the public API, Alpine.js CSP build
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
- Editing controls need the admin role **and** a writable config directory (`whoami` reports
  `config_writable`); otherwise the UI says why editing is off. Open mode is read-only (D-040).
- **Read-only towards PLCs:** the UI offers read-once and identity, never a value write.
- A browser smoke test (`tests/ui/browser_smoke.js`, run by `tests/integration/test_ui_browser.py`,
  marker `acceptance`) drives Chromium through every page at desktop and phone width and fails on any
  script error or CSP violation; it is skipped where Node.js and Playwright are not installed.
- **Amended by #126 (string tables):** every visible string comes from `ui/i18n/<lang>.json`
  (`en.json` only; `languages.json` lists the tables), looked up with `t('key', {name: value})` in
  templates and `tr()` in app.js. The language is the tab's choice (`sessionStorage`
  `opencap.lang`), else the first of `navigator.languages` with a table (exact, then without the
  region), else English; missing keys fall back to English. The CSP build refuses `x-html`, so
  text with markup uses a small `x-rich` directive that builds only `code`, `strong`, `em` and
  same-site `a` elements and fills values into text nodes. The tables are files of the bundle, so
  app.js has a second `fetch` (relative `i18n/`, never the API base). The browser smoke test loads
  a test-only pseudo table (every string in ⟦ ⟧) and fails on visible text outside the markers that
  is not API data; `test_ui.py` checks the static half and that every key exists.

## Rust engine: API parity with #63

### D-062 Named tokens, read-only open mode and auth before the body in `opencap-api` (#82)
- **Config:** `api.tokens`, `api.audit_log` and `api.cors_origins` load in Rust (`ApiToken`, `Role`), with the
  same checks and messages as `ApiCfg` (non-empty `name`/`token_env`, "default" reserved, duplicate names). A
  `poller.yaml` written for the Python engine with named tokens is no longer refused by `deny_unknown_fields`.
  `audit_log` and `cors_origins` are accepted and unused: the Rust engine has no config API and sends no CORS
  headers yet.
- **Roles and open mode as `auth.py`:** `token_env` is the admin "default"; `viewer` may use GET/HEAD only, so
  `POST /reload` and `POST .../read-once` need `admin`. A listed token whose env var is unset, or two tokens with
  the same secret, stop the start with Python's message. With no token the API still starts only on a loopback
  bind (D-019) and is now read-only (the viewer "anonymous") and answers only loopback `Host` names (DNS
  rebinding). 403 and 401 texts are Python's; 401 carries `WWW-Authenticate: Bearer`.
- **Guard:** an axum middleware in front of every route and the fallback, with `guard.py`'s open paths. It
  authenticates from the headers before any body byte is read, then caps the body at 1 MiB: a `Content-Length`
  over the cap, or a chunked body passing it, gets 413 with `Connection: close`. Unknown paths need a token (401
  before 404), as in Python.
- **Refused bodies are drained (lingering close):** before the 413 the guard reads and drops the rest of the
  body, up to 8 MiB or 5 s; a body declared larger than 8 MiB is refused without reading it. Without this the
  server closed the socket with unread request bytes, the kernel sent a reset, and the client saw "connection
  reset" instead of the 413: CI caught it, and over real TCP it happened in 2 of 5 tries at 1 MiB + 1 and in every
  try from 4 MiB. Nothing is kept, and only an authenticated caller gets this far. The ASGI-level Python tests
  cannot show the effect (no socket).
- **Deliberate difference:** Rust reads every request body up front (at most 1 MiB, after auth), so a chunked
  body over the cap gets 413 on any route. Python counts only the bodies a route reads, and its legacy routes
  read none, so there such a body is ignored. Both refuse an oversized `Content-Length`. None of the Rust routes
  takes a body, so buffering costs nothing and keeps the cap in one place.
- **No body without a token (lead's note on #84):** the open paths (`/health`, `/metrics`, `/api/v1/auth-info`,
  the UI paths) and `OPTIONS` skip authentication, so they read no body either: any body there gets 413 at once,
  unread and undrained. An anonymous caller cannot make the server buffer or drain anything; drain only ever runs
  for an authenticated caller. Python ignores such a body instead (no open route reads one).
- **Host names (lead's note on #84):** open mode answers exactly Python's `LOOPBACK_HOSTS` (`127.0.0.1`,
  `localhost`, `::1`); other 127/8 names are refused, as in Python. The start-up bind check still accepts any
  loopback address, as Python's `_is_loopback` does.
- **Dependency:** `http-body-util` 0.1 (MIT) becomes a direct dependency of `opencap-api` to read the body frame by
  frame under the cap and to drain it. It was already in the lock file through axum; `tower-http` was not added.
- **Tests:** `opencap-api` unit tests (roles, open mode, rebinding, 401 before the body, 413 by length and by
  chunks, start-up errors); `tests/integration/test_rust_engine_api.py` runs the open-mode, rebinding, role,
  401-before-body and 413 checks against both engines: Python through ASGI with `tests/helpers/fake_poller.py`,
  Rust as the binary. The reload test now gives the engine a token.

### D-063 Rust config models: the shared fixtures and a cross-language comparison (#39)
- **Hand-written, not generated:** the serde types stay hand-written (`typify` output would need the same
  validators, messages and `IndexMap` ordering added by hand). Drift is caught by data instead:
  `tests/conformance/config/compare.py` runs the Rust dumper (`opencap-core` example `config_dump`) and the
  Python models over every fixture. A valid fixture must give the same document with every default filled in
  (`model_dump(mode="json")` against serde) and, for `devices`, the same resolved channels and warnings from
  `load_config`; an invalid one must be refused by both. A field missing on one side shows as "only in
  Python/Rust" for every model that appears in a valid fixture. A mutation check (one Rust default changed)
  fails it. CI runs it in the `conformance` job.
- **Rust test over the fixtures:** `engine/crates/opencap-core/tests/config_conformance.rs` loads every valid
  fixture and refuses every invalid one with the fixture's `expected_error`. serde words one error
  differently: pydantic's "Extra inputs are not permitted" is serde's "unknown field".
- **Errors name the field:** serde errors now carry the location in pydantic's spelling
  (`channels.0.devices.0.port: invalid type: ...`) through `serde_path_to_error` 0.1 (MIT OR Apache-2.0,
  already in the lock file through axum). Before, a wrong type said only "invalid type".
- **`overrides.yaml` `serial_ports`** (COM port -> gateway, D-037) loads in Rust. It is accepted and
  unused until the serial drivers are ported (#47).
- **New fixtures:** `poller_api_tokens` (valid) and the invalid `poller_api_tokens_duplicate_names`,
  `poller_api_tokens_reserved_name` (semantic: the schema accepts them) and `poller_api_token_bad_role`.

### D-064 OpenAPI contract of the admin + config API (#32)

- **Committed and generated:** `api/openapi.json` comes from `scripts/gen_openapi.py` (FastAPI's `app.openapi()`
  on an app built with no poller, no sockets, `sort_keys` JSON). The running server still has `openapi_url=None`
  (D-019). `tests/unit/test_openapi_contract.py` fails with the regeneration command when the file drifts.
- **Response models** live in `src/opencap/api/schemas.py` and are attached with `response_model=` plus
  `response_model_exclude_unset=True`, so what the handlers return is unchanged. Handlers that return
  `JSONResponse` (to set `ETag`) bypass validation, so the models only describe them; the contract test is what
  checks them. Models allow extra keys (additive v1).
- **Deviation: request bodies are not typed.** Configuration writes validate through `ConfigStore` and answer
  422 with `loc`s relative to the resource, which a pydantic body model would change. The bodies stay free-form
  JSON in the contract and point to `GET /api/v1/project/schema`. Typing them is a follow-up if the Rust engine
  wants generated clients.
- **Amended by #89 (contract 1.2.0): request bodies are described, validation is unchanged.** The handlers still
  take the raw JSON body (`Body()` of type `Any`), so `ConfigStore` validates exactly as before and 422 `loc`s keep
  their shape (`test_validation_errors_are_422_with_locations` asserts no `("body", ...)` prefix). The OpenAPI hook
  in `server.py` replaces FastAPI's untyped `Body` with `schemas.request_body_components()`. The six config models
  (channel, device, tag, map entry, map defaults, device override) go in as `Config*` components, generated the same
  way as `GET /api/v1/project/schema`; a test checks they are identical. Per-route variants:
  - `ConfigChannelCreate`, `ConfigDeviceCreate`: `id`, `channel`, `devices` and `tags` are not required, because the
    server derives them.
  - `ConfigDevicePut`, `ConfigTagPut`: `name` and `channel` default to the current ones.
  - `Config*Patch`: every property, no `required`, `null` allowed. That is JSON merge-patch semantics, a step beyond
    the issue's "the same schema with no `required`".
  - `ConfigChannelPatch`: only `name`/`enabled`.
  - `ConfigChannelOverride`: `{enabled}`.
  - `ConfigProjectImport`: any object (a KEPServerEX project or an opencap export).
  - `POST …/tags`: one tag or a list.

  The contract test validates every body it sends that the server accepts against the documented schema, and
  fails when a documented body is never exercised or a write route has none. The schemas are descriptive: the
  server stays lenient where pydantic coerces (`"502"` for an integer port).
- **Resource payloads** (device, tag, map entry, override) are open objects for the same reason; the envelope
  (`revision`, lists, counts, `WriteResult`) is typed field by field.
- **Versioning:** `/api/v1` was already applied by #63; the unversioned runtime routes stay as aliases and are
  marked `deprecated` (rules in `api/README.md`). `info.version` is `1.0.0`.
- **Licences:** no new dependency; `jsonschema` (MIT) was already a dev dependency and validates the contract.

### D-067 Live values over WebSocket, `GET /api/v1/ws` (#31)

- **Contract:** protocol `opencap.v1`, JSON text frames. OpenAPI 3.1 has no WebSocket operations, so
  `api/openapi.json` documents the upgrade as `GET /api/v1/ws` (101, 400, 401, 403, 503) with an `x-websocket` block
  whose `client_messages` / `server_messages` point to the `Ws*` schemas in `components.schemas`. The contract
  test validates real messages against them. Additive: `info.version` 1.1.0.
- **Messages (as built vs the issue's wording):** one client message, `{"subscribe": {"devices": [...], "tags":
  [{"device", "tag"}]}}`. Each subscribe *replaces* the previous one (no unsubscribe message; an empty subscribe
  stops everything), so a client never has to track server state. Tags are `{device, tag}` objects because channel,
  device and tag names may contain `/`. Server messages carry a `type`: `snapshot` and `changes` both have the
  issue's shape `{device, version, changes: [{tag, value, quality, source_ts}]}`; `subscribed` acks;
  `unavailable` says a device is not polled (it stays subscribed and gets a snapshot when it is polled again);
  `error` answers a message the server could not use, without closing. `version` is the value cache's change
  counter of the device (`Poller.live_version()`): it grows within a connection, so a client can drop a stale batch.
- **Auth in the handshake**, with the REST tokens and roles (subscribing is a read: viewers may). Browsers cannot set
  `Authorization` on a WebSocket, so they offer the subprotocol `opencap.bearer.<base64url(token)>` next to
  `opencap.v1` (the pattern Kubernetes uses); the server selects `opencap.v1` and never echoes the token. Not a query
  parameter: URLs end up in access logs, proxies and browser history. A refusal is an HTTP 400/401/403/503 before the
  upgrade (uvicorn's `websocket.http.response` extension), so no socket is opened.
- **Origin check (security):** CORS does not apply to WebSockets. In open mode (no token, loopback only) any web page
  open in a browser on that machine could connect to `ws://127.0.0.1:<port>` with a correct `Host` and read live
  values. A browser `Origin` must therefore equal the API's own origin (`Host`) or be listed in `api.cors_origins`;
  clients without `Origin` (not browsers) are unaffected. Behind a reverse proxy, keep `Host` or list the public
  origin in `api.cors_origins`.
- **Conflation by polling a change counter, not an `on_cycle` hook:** the worker's housekeeping marks tags stale with
  `ValueCache.expire()` without a cycle callback, so a hook would miss quality changes. A hub task (only while
  connections exist) reads `live_version` of each subscribed device every 50 ms: one dict lookup per device. A
  changed device whose last message left at least 250 ms ago is diffed once against the values last sent; the batch
  is JSON-encoded once and the same string goes to every whole-device subscriber (snapshots are cached per version).
  The first change after a quiet period goes out within 50 ms, then at most one message per device per 250 ms.
  The only runtime change is `Poller.live_version()`.
- **Slow clients:** each connection has a queue bounded by 4 MiB and `16 + 4 x devices` messages. Over the bound the
  queue is dropped and every subscribed device gets a fresh snapshot, built when it is sent (one at a time, so memory
  holds one snapshot). Batches for a device waiting on a snapshot are not queued; a snapshot due to a reload (tag set
  changed) or a device coming back drops that device's older queued batches. At most 256 connections (503), 1 MiB
  per client frame (uvicorn closes with 1009), 100,000 devices and tags per subscribe.
- **permessage-deflate is off** (`live.UVICORN_WS`, used by `runtime._start_api`). It compresses per connection on the
  poll loop: with 50 clients x 5,000 changing tags the event loop stalled 40-150 ms every second; without it the only
  stall was ~90 ms while the 50 clients connected. The price is bandwidth: about 80 bytes per changed tag,
  uncompressed, to each client.
- **Non-finite floats** go out as the strings `"NaN"`, `"Infinity"`, `"-Infinity"` (as the proto3 JSON mapping does):
  JSON has no such numbers and a browser's `JSON.parse` rejects them.
- **New dependency:** `websockets` (BSD-3-Clause) in the `north` extra. uvicorn serves WebSockets only with a WebSocket
  library installed; it was present before only through `amqtt` (dev), so a production install could not have served
  the feed.
- **UI (Quick Client, `#/quick/<device>`):** device tree, tag table (address and type from the config, value,
  quality colour, source time, age, update count), tag filter, at most 1,000 rows rendered. "Read now" calls
  `POST /api/v1/devices/{id}/read-once` (admin until RFC 0005; never a PLC write). If the socket cannot be opened three
  times in a row (an engine without the route, such as the Rust engine today) the screen polls
  `GET /api/v1/devices/{id}/values` every 2 s and says so. CSP stays `connect-src 'self'`, which covers `ws:`/`wss:`
  to the same host in current browsers (CSP3); where it does not, the fallback applies.
- **Load result** (`tests/integration/test_live_load.py`, acceptance): 50 clients in their own process, each
  subscribed to 10 Modbus simulators x 500 tags with every value changing on every 1 s poll: no poll overruns,
  cycles ~100 ms, all clients hold all 5,000 tags, 120 batches per client in 12 s, no resyncs, ~27 MB/s delivered.
- **Rust engine:** not part of this change; it has to serve the same `x-websocket` contract (the dual-engine suite,
  #42, can reuse `tests/unit/test_live_api.py`'s message checks).

### D-069 Engine-agnostic acceptance suites and the Rust port gap (#42)
Builds on #64's harness (`engine_factory`, `OPENCAP_ENGINE_BIN`, names kept). ADR-0014 §3:
an acceptance suite reaches an engine only through its config directory, API, CLI, log and
the wire.
- **Which suites run on Rust is decided by the binary, not a list in the tests.** Suites
  name the PLC drivers they poll (`@pytest.mark.drivers("ab_pccc")`); `opencap-engine drivers`
  prints the schema's `KNOWN_DRIVERS` in three lists: `implemented` (compiled in,
  `opencap_drivers::compiled_in()`), `not_compiled_in` (a driver crate exists but its Cargo
  feature is off, #100) and `not_implemented` (no crate yet). `tests/conftest.py` skips a suite
  as `driver not ported: <names>`, or `driver not compiled in: <names>`, when one is missing.
  Porting a driver turns its suite on with no test change. This subcommand has no Python
  counterpart (Python implements every driver).
- **A suite that does not use `engine_factory` is skipped on Rust as "not engine-agnostic"**,
  so it can never silently test the Python engine in a Rust job. Today that is
  `test_ui_browser.py` (the Python web UI against a fake poller), and the serial, S7/MC and
  OPC UA suites once their drivers are ported: converting them is part of each port.
- **Port gap report:** with `--engine=rust`, the run ends with a "Rust port gap" section and,
  when `$GITHUB_STEP_SUMMARY` is set, a Markdown table of every acceptance suite (drivers,
  passed/failed/skipped, reason). The `acceptance-rust` job installs every driver extra so that
  no suite disappears through a module-level `importorskip` before it can be listed.
- **Phase 2 converted; Phase 3 written.** Phase 2 now reads topics from `engine.loaded`
  instead of the Python registry, runs `identity` through `engine.cli()` (the Python CLI or
  `opencap-engine identity`), and checks `device.stats` through `engine.log_events()` (structlog
  capture in the Python adapter; JSON lines on the binary's stdout, where tracing's `message`
  is structlog's `event`). It needed no `is_python` guard. There was no Phase 3 acceptance
  suite (only driver-level tests), so `test_phase3_acceptance.py` is new: 12 SLC/MicroLogix
  PLCs over three file-directory layouts through the whole engine, file-directory rejection
  checked on the wire (no typed read of the missing file), the directory read once per
  connection, power cycle, `identity`, no write attempt.
- **The Python adapter now serves the API** in every suite (Phase 2 used `serve_api=False`),
  on a free port, as the Rust engine always does.
- **CI:** Phase 1 on the debug binary moved out of the `conformance` job into `acceptance-rust`
  (release build, every suite); the workflow also runs nightly at 02:41 UTC. The Python
  acceptance run stays in `ci.yml` (nightly, Python 3.12 and 3.13).
### D-087 Staged project imports: the KEPServerEX import wizard (#33)

- **Flow:** `POST /api/v1/project/import?stage=true` runs the importer and keeps its output; the answer is the
  report plus `import_id`, `created_at` and `expires_at`. `GET /api/v1/project/import/{id}` returns the report again,
  `POST …/{id}/apply` writes it (`If-Match` is the report's `revision`, 412 when the config changed since; `?apply=true`
  reloads, as on every write, D-039), and `DELETE …/{id}` discards it. The existing dry-run and apply calls of
  `/project/import` are unchanged. Staging, reading, applying and discarding need the admin role (engineer/admin once
  RFC 0005 lands).
- **Deviation: the upload is the raw JSON body, not `multipart/form-data`.** It is the body `/project/import` already
  takes (`curl --data-binary @project.json -H 'Content-Type: application/json'`). Multipart would add
  `python-multipart` and a form parser to carry one JSON file. The guard caps the body at 64 MiB while it streams, and
  a non-JSON `Content-Type` is refused, as before. Bearer-only auth means a cross-site form post cannot authenticate.
- **What is staged is the importer's output, never the export.** The upload is written as a 0600 temporary copy in the
  staging directory, read by `import_kep` (unchanged) and deleted before the answer goes out. `staged.json` holds the
  generated `devices.json` document, the importer summary and notes, and the report. Credentials are replaced by
  env-var names exactly as the CLI importer does (D-010), so none of them is ever at rest. `apply` installs that output
  through the same transaction as a direct import (`ConfigStore.apply_staged` -> `_import_kep`: atomic write, new
  revision, audit `config.import_kep`); a test checks that the files equal a direct import's.
- **Where:** `<config dir>/.import-staging/<32 hex>/` with mode 0700 on both directories and 0600 on the file, never the
  system temp dir; the revision hashes only the four config files, so staging does not change it. A config directory
  the API cannot write answers 503 (an import could not be applied there anyway). Ids are random 128-bit values and
  are checked against `^[0-9a-f]{32}$` before any path is built.
- **Expiry:** removed after apply or discard, or one hour after staging: checked on every staging call, by a timer
  (`loop.call_later`) while the API runs, and at the next call after a restart. A directory without a readable
  `staged.json` (a crash mid-upload) expires by its age.
- **Report additions (additive, contract 1.3.0):** `excluded_devices` (channel, device, KEPServerEX driver, tags,
  reason: from `devices_file.excluded`) and `same_address` (the D-011 report, at most 500 rows) join
  `import_summary` (devices and tags per driver, exclusions by reason).
- **UI:** the Import page is a wizard. Upload, then review: devices per driver, devices not imported with reasons,
  same-address groups (suspects highlighted), the changes to the configuration, importer notes. Then apply or discard.
  New devices' stub `devices_map` entries (`stub: true`) are highlighted, and Apply stays disabled until the user
  ticks that they will confirm those SubDeviceIds. The environment variables to set for credentials that were not
  copied get their own box. Leaving the page does not lose the review: the staged id is kept in the tab's
  `sessionStorage`, and the wizard reopens it (until it expires). Mapping KEPServerEX COM ports to local serial
  ports (plan UI-14, step 3) is not part of #33; it is a follow-up. Every visible string of the wizard is in `src/opencap/api/ui/i18n/en.json` (`import.*`, #235).
- **Privacy test:** `tests/unit/test_import_wizard.py` puts unique markers in every username, password and endpoint
  user-info field of a synthetic export, then searches every response, every structlog event and logging record,
  every file under the test directory while the import is staged and after it is applied, and the system temp dir.
  None of the markers may appear. Mutation check: keeping the raw upload in `staged.json` fails it.

## S7 and MELSEC MC drivers

### D-048 s7 driver on python-snap7 3.5 (pure Python)
- **Library:** python-snap7 3.x is a pure-Python S7comm stack (1.x wrapped the C library and
  has a different API), so the extra is pinned `python-snap7>=3.5,<4`. It installs on any Python
  without a native library.
- **Client subclass** (`s7_client_class()`, pinned by `tests/unit/test_s7_io.py`):
  `_connect` uses the device timeout for the TCP connect, the COTP connection and setup
  communication (snap7 hard-codes 5 s), with no heartbeat thread, no multi-read optimizer and no
  auto-reconnect; `oc_read` sends one read var request and returns the item return code (snap7's
  `read_area` reports it only inside an exception message, and splits big reads on its own).
- **Read-only:** `_send_data` checks every PDU before the socket: only setup communication
  (0xF0), read var (0x04) and SZL reads (USERDATA group 4, subfunction 1) are sent. Every snap7
  API that writes (write var, PLC stop/start, clock set, compress, password, download, delete,
  `db_fill`, ...) is refused before anything is sent (tested one by one against a fake
  connection). The simulator also refuses and counts writes; no test sees one.
- **One block = one read var request** of bytes (transport size BYTE), or of timers/counters
  (TIMER/COUNTER). Multi-variable reads (up to ~20 items per PDU) are not used; they could cut
  requests for devices with many small scattered blocks.
- **Errors:** connect failure, refused, reset, peer close → `ConnectionLost`; reply timeout →
  connection closed, `CommError`; item return codes 0x03 (access not allowed), 0x05 (address out of
  range), 0x06/0x07 (data type), 0x0A (object does not exist) → `BlockConfigError`; 0x01 (hardware
  fault) and other codes → `CommError`, connection kept; header error 0x8104 (function not supported
  in this context; S7-1200/1500 with PUT/GET disabled, **unconfirmed**) → `BlockConfigError`;
  other header errors → `CommError`; a malformed or out-of-sequence reply → connection closed,
  `CommError`.
- **identity():** SZL 0x001C (module type, serial, AS and module name), 0x0011 (order code,
  firmware) and 0x0424 (run/stop), plus the requested and negotiated PDU, rack, slot, connection
  type and byte order. A refused SZL is reported as `<name>_error`; identity never raises.
- `opencap.logsetup` keeps the `snap7` logger at WARNING (it logs every connect at INFO).

### D-049 S7 addresses: Kepware syntax, widths, byte order
Source: Kepware "Siemens TCP/IP Ethernet" help, *Standard S7-300/400/1200/1500 Item Syntax* and
*Legacy* pages, and *Addressing Options*.
- **Forms:** `<area><type><byte>` for I/E, Q/A, M/F (`MD232`, `MW10`, `MB7`, `IREAL8`, `MDI4`),
  bits `<area><byte>.<bit>` / `<area>X<byte>.<bit>` (`M10.3`, `IX1.7`), data blocks
  `DB<n>,<type><byte>` (`DB100,D04`, `DB100,D016` = DWORD at byte 16, `DB1,REAL8`, `DB1,X5.6`,
  `DB1,W2`, `DB1,B3`), Step 7 types after the comma (`DB1,DBD12`, `DB1,DBX5.6`), the Siemens form
  `DB1.DBD12` and Kepware's legacy alternate `DB1DBD12`; timers `T<n>`, counters `C<n>`/`Z<n>`.
  All spellings of the same data are one wire item.
- **Types:** the type letter fixes the width: X bit; B/BYTE, C/CHAR 8 bits; W/WORD, I/INT 16;
  D/DWORD, DI/DINT, REAL 32; LREAL, LINT, LWORD 64; `STRING<byte>.<n>` S7 string (max length,
  actual length, n characters; default n = 254, 210 below PDU 480); `B<byte>.<n>` with dtype string
  = raw NUL-terminated string. The **tag dtype** decides signed/unsigned and integer/float (D with
  float32 = REAL, as Kepware's "Float" data type on a D address). Kepware Byte/Char tags arrive as
  uint16/int16 (importer widening) and read one byte. Bool on B/W/D/LWORD with `.bit` = bit of the
  value; arrays `[n]`/`[r][c]` with element stride = width (`IW0[4]` = IW0, IW2, IW4, IW6).
- **Byte order (`byte_order_big`):** big endian is the S7 default. Kepware's "Little Endian"
  reverses all bytes of a 16- and 32-bit value and leaves bit numbers unchanged; we apply the
  same to 64-bit values (Kepware's 2012 help predates LREAL/LINT), and never to timers, counters,
  strings or single bytes. A bit of a word or dword is taken after the byte order.
- **Timers and counters:** Kepware returns T values in milliseconds, scaled from the S5TIME word
  (time base in bits 12-13: 10 ms, 100 ms, 1 s, 10 s; three BCD digits), dtype DWord/Long →
  uint32/int32; C/Z values BCD-decoded (Kepware: `DB1:KH0 @ BCD = DB1:KC0 @ Word`), dtype
  Word/Short. A word that is not valid BCD is `bad_decode`. **Check on a real CPU** that the
  counter word read with transport size COUNTER is BCD (as libnodave and Kepware assume).
- **Rejected (`bad_config` at plan time):** peripheral I/O `PI/PE/PQ/PA` (python-snap7 has no
  area 0x80), DT/DATE/TIME/TOD/S5TIME/HEXSTRING types (note `DB1,T0` is S7 TIME, not a timer),
  Kepware's legacy `DB1:KH30` forms, bool arrays and string arrays (as Kepware), X bits 8..15
  (Kepware lists 0..15 for X without saying which byte they address; the reason names the byte
  and bit to use), a value ending past byte 65535, DB 0.

### D-050 S7 link type, TSAP, rack and slot
The remote TSAP is `connection type << 8 | rack << 5 | slot`; the local TSAP is 0x0100. Kepware
documents the Link Type options PC (applications, the default), OP and PG, but not their values
in `project.json`; every S7 device in the sample export says 3. **Assumed:** the value is the
TSAP connection-type byte, 1 = PG, 2 = OP, 3 = PC (S7 basic), and null means 3 (Kepware's
default; snap7's own default would be PG). `link_type` also accepts `"PG"`, `"OP"`, `"PC"` for
hand edits. **Confirm on site** with `opencap identity`; a CPU that refuses the connection type
closes the connection during the COTP handshake (seen as `connect failed`), and `link_type: 1`
(PG) is the fallback to try. The simulator checks rack/slot and records the connection type.

### D-051 S7 planner and the negotiated PDU
- PLAN §5.1: merge per (area, DB) when the gap is at most `max_gap` (16 bytes) and the block stays
  within `max_pdu` − 18 bytes (12 header + 2 parameter + 4 item header). Timers and counters
  merge in elements (2 bytes each, gap 8). Items larger than one PDU are `bad_config` at plan time.
- Plans are built once, before any connection (a separate driver instance in the registry), so
  they use `params.max_pdu`, KEPServer's setting (960 in the sample export). The CPU decides the
  PDU at connect: S7-300/400 usually negotiate 240 or 480, S7-1500 960. When it negotiates less,
  `read()` splits a block into ⌈bytes / (PDU − 18)⌉ requests and logs `s7.pdu_negotiated` once at
  INFO with the advice to set `max_pdu` to the negotiated value. Until then `opencap check`
  under-counts requests for that device, and the worker's per-read safety net still counts the
  block as one request (a split block on a CPU that answers each part near the timeout can hit it).
- python-snap7's server always negotiates at most 480; the simulator negotiates up to its
  `max_pdu` and refuses a read whose reply would not fit with header error 0x8500 (what a real
  CPU returns there is **not confirmed**). In simulation: a 900-byte block planned for 960 and
  negotiated at 480 is read as 462 + 438 bytes.

### D-052 S7 simulator
`tests/helpers/s7_sim.py` wraps python-snap7's own `snap7.server.Server`, which the stock snap7
client talks to unmodified (`tests/unit/test_s7_sim.py`). It adds the rack/slot TSAP check
(no connection confirm otherwise), configurable PDU negotiation with oversize refusal, refusal
and counting of every job except setup/read var and every USERDATA except SZL reads, fault
switches (`mute`, `item_errors`, `header_error`, `short_reply`, power off/on on the same port with
connections reset), request counters, and an address codec of its own (not the driver's) for
`set()`/`value()`. Like a CPU, a shorter S7 STRING leaves the old characters after its length
byte. Mutation check: ignoring the byte order, the S5TIME base, BCD, bit numbering, signedness,
the STRING length byte, the split size or the timer/counter offsets each fails the tests.
Not simulated: S7-1200/1500 optimized blocks and PUT/GET protection (their codes are injected),
real SZL contents (snap7's server reports a fixed CPU 315-2 PN/DP), routing, multi-var reads.

### D-053 melsec_mc driver on pymcprotocol 0.3.0 (MC protocol 3E frame, binary)
- **One block = one "batch read in word units"** (command 0401, subcommand 0000; 0002 for
  `plc_type: iQ-R`) through a small `pymcprotocol.Type3E` subclass (`mc_client_class()`), run on
  the channel's single-thread executor. Its hooks are pinned by tests/unit/test_melsec_io.py.
- **Framed replies.** pymcprotocol's `_recv()` is a single `recv(4096)`. A reply split across
  TCP segments (a 960-word reply is 1,931 bytes, more than one 1,460-byte segment) is decoded
  short, and `int.from_bytes(b"")` is 0, so the missing words read as **0 with good quality**.
  Shown with stock pymcprotocol against the simulator (tests/unit/test_mc_sim.py). The subclass
  reads exactly one 3E frame (9-byte header, then the length it announces), and the driver
  checks that the reply carries exactly the words it asked for, else `CommError`.
- **End codes as integers.** Stock `_check_cmdanswer` raises `MCProtocolError` with the code as
  text, and `UnsupportedComandError` with no code at all for 0xC059. The subclass raises
  `McEndCode(code)`.
- **UDP** (`params.protocol: udp`, KEPServer's default for this driver) is added by the subclass:
  a connected UDP socket, one frame per datagram, checked against the header length. pymcprotocol
  itself is TCP only.
- **A timeout closes the socket**, TCP and UDP alike. 3E replies carry no sequence number, so a
  late reply would be taken as the answer to the next request. A new UDP socket gets a new source
  port, so a late datagram is dropped by the kernel. The worker reconnects before the retry (D-013).
- **Binary only.** Kepware's Mitsubishi Ethernet driver is "Binary Format only". pymcprotocol's
  ASCII mode writes the numbers of hex devices (X, Y, B, W, ZR…) as decimal digits, so `W1A` would
  read W26 (hex); pinned in tests/unit/test_mc_sim.py.
- **Read-only:** the subclass refuses, before anything is sent, every command except 0401/0000,
  0401/0002 and 0101/0000 (read CPU model). Batch writes, random writes, remote RUN/STOP/RESET/
  latch clear, lock/unlock, and even unused reads (bit units, random read, loopback) raise
  `McReadOnlyError` with nothing on the wire.
- The 3E monitoring timer is `ceil(timeout_ms / 250)` (≥ 1); the socket timeout is `timeout_ms`.
  `connect()` is the TCP connect only (one round trip; a UDP "connect" cannot fail, a dead PLC
  shows as timeouts). `identity()` sends 0101: `plc_type`, `protocol`, `frame`, `commtype`,
  `cpu_type`, `cpu_code`, or `cpu_type_error` when the PLC refuses it (A series, FX3U and some
  Ethernet modules do not support 0101).

### D-054 MELSEC addresses, value mapping and `plc_type`
- **Syntax = Kepware "Mitsubishi Ethernet" Q Series Address Descriptions** (case-insensitive,
  leading zeros allowed). Bit devices: X, Y, DX, DY, B, SB in **hex**; M, SM, L, F, V, TS, TC, SS,
  SC, CS, CC in decimal. Word devices: D, SD, R, TN, SN, CN in decimal; W, SW and **ZR in hex**
  (Kepware's table: `ZR0000-ZR3FD7FF (Hex)`; GX Works writes ZR in decimal, so a hand-written ZR
  address from a ladder listing must be converted). Ranges are Kepware's Q table; a tag past them
  is `bad_config` at plan time.
- Forms: `D0000002` (1/2/4 words by dtype), `B0000` / `M10` (one point, bool), `M16` as
  uint16/int16/uint32/int32 (16 or 32 points as words; **must start on a 16-point boundary**,
  Kepware's rule), `D100.05` (bit 0–15 of the word; `.16`–`.31` = bit of the 2-word value),
  `D100[4]` / `D100[2][3]` (word devices, not bool, not strings), `DSH100.020` / `DSL100:020`
  (also WSH/WSL, RSH/RSL, ZRSH/ZRSL; 2–128 bytes, even; H = first char in the high byte; cut at
  the first NUL, latin-1). TN/SN/CN are 16-bit only, without bit access.
- **Word order:** `first_word_low: true` (Kepware default, Mitsubishi native) = the lower device
  number holds the low word. Kepware documents Double as "first two registers = low DWord", so
  the same rule applies to every multi-word value: true = little-endian word order throughout,
  false = all words reversed (also for 64-bit values; Kepware only documents the 32-bit case).
  Bytes in a word are always little-endian on the wire (pymcprotocol decodes them).
- Bits of a bit device read as words: the lowest device number is bit 0 (`M0` = b0 of the word
  read at `M0`). NaN/Inf/denormal floats → 0.0 (`nonnormal_float: zero`, Kepware's channel
  default, as D-006).
- Not supported (rejected with a reason): step relays S and index registers Z (no pymcprotocol
  device code), K-digit forms (`K4M0`), Kepware's `CS0000 L` long suffix (use dtype int32),
  bool arrays and string arrays. BCD/LBCD/Date tags are already imported disabled (D-010).
- **`plc_type`** only matters to pymcprotocol in one way: `iQ-R` sends subcommands 0002/0003 with
  a 4-byte device number and a 2-byte device code; `Q`, `L`, `QnA` and `iQ-L` build identical
  frames (QnA-compatible 3E: subcommand 0000, 3-byte number, 1-byte code). Per Mitsubishi's MC
  protocol reference, Q, L and iQ-R CPUs (and FX5 / iQ-F) accept the QnA-compatible frame for
  devices inside the Q ranges, so the importer's `plc_type: Q` guess for KEPServer model 5 is the
  low-risk choice: a wrong "Q" still reads every address the project uses, while a wrong "iQ-R"
  against a Q/L CPU would be refused (0xC059, all blocks `bad_config`). The project's digit widths
  (`D0000002`: 7 digits, `B0000`: 4 hex digits) match Kepware's Q Series table, which supports the
  guess. If model 5 were an A series or FX3U (1E frame only), every read would fail: `opencap
  identity` shows it (0101 refused or no answer). **Confirm on site.**
- KEPServer's `mitsubishi_ethernet.DEVICE_CPU` is 1023 = 0x3FF in the sample, which is exactly
  the 3E "request destination module I/O" for the own CPU (0x3E0–0x3E3 = CPU No. 1–4 in a
  multi-CPU system). The driver takes it as `params.dest_moduleio` (default 0x3FF); the importer
  does not write it yet. The id string's `:Nzzz:XXX` network/PC numbers are also not imported
  (D-010 reports them); set `params.network` / `params.pc` by hand if a site uses them.

### D-055 MELSEC block planner and MC 3E limits
- Per device, items sorted by start word; an item joins the open block when it is on the same
  device, starts at most `max_gap` (default 16) words after the block end, and the span stays
  within `max_words` (default and cap **960 words**, the 3E limit for batch read in word units;
  for bit devices that is 15,360 points). Bit devices are always read in word units with the
  head on a 16-point boundary: 16 points per word on the wire instead of 2 per byte in bit units
  (whose limit is 7,904 points). Random read (192 points per request) is not used: one batch read
  per block keeps one request per block and the reply length checkable.
- Identical items (same device, words, dtype, bit…) are read and decoded once and served to every
  tag name (as D-006). An item larger than `max_words` is `bad_config` at plan time.

### D-056 MELSEC error mapping
- Refused, reset, broken pipe, peer close (also mid-frame), UDP ICMP port unreachable →
  `ConnectionLost` (socket closed). No reply within `timeout_ms` → socket closed, `CommError`.
  A malformed reply (wrong subheader, length mismatch, wrong word count) → socket closed, `CommError`.
- End codes 0xC051–0xC054 (points out of range), 0xC056 (past the maximum address), 0xC059
  (command/subcommand not supported, e.g. wrong `plc_type`), 0xC05B (CPU cannot read the device),
  0xC05C / 0xC060 (request content error), 0x4030 (device does not exist), 0x4031 (device number
  out of range) → `BlockConfigError` (block skipped until reload, connection kept). Every other
  end code → `CommError` (connection kept). Texts are paraphrased from the MELSEC communication
  protocol reference; **which code a real Q CPU returns for which fault is not confirmed on
  hardware.**
- pymcprotocol failing to build a request (device not supported for the `plc_type`, number too
  large for the frame) before anything is sent → `BlockConfigError`.

### D-057 MC 3E simulator for MELSEC tests
`tests/helpers/mc_sim.py` is an asyncio MC 3E server on TCP and UDP (same port number), binary and
ASCII frames, Q and iQ-R device specs. It serves batch read in word and bit units (0401/0000–0003),
random read (0403), read CPU model (0101) and loopback (0619); stock pymcprotocol works against it
unmodified. Device memory uses roughly a Q CPU's default sizes (D 12,288, M 8,192, X/Y/B/W 0x2000…).
- Faults: `mute`, `split_replies` (each TCP reply in two writes, 50 ms apart), `end_codes[device]`,
  `cpu_model_end_code`, `stop()` / `restart()` on the same port (connections dropped, UDP port
  closed). Out of range → 0xC056, more than 960 words / 7,904 bits → 0xC051, unknown device code
  → 0xC05B, word access to a bit device off a 16-point boundary → 0xC05C (strict; the driver
  always aligns). These code choices are ours, not confirmed against a real PLC.
- Read-only: write and remote-control commands get 0xC059 and are counted in `write_attempts`.
- `set()` / `value()` use the simulator's own address parser and a struct-based encoder, never the
  driver's code. Mutating the driver's word order, bit numbering, hex/decimal radix, string byte
  order or int16 masking makes the round-trip tests fail (mutation-checked).

### D-058 S7 and MELSEC simulated acceptance, and what is left for real hardware
`tests/integration/test_s7_melsec_acceptance.py` (`-m acceptance`, ~30 s) runs 11 simulated
PLCs through the whole poller and MQTT: 6 S7 CPUs (big and little endian; KEPServer's PDU 960
negotiated to 480, so an 800-byte block is read as 462 + 338 bytes, and kept at 960, one request;
rack 0 slot 2 with link type PG; a missing DB) and 5 MELSEC PLCs (TCP and UDP, `first_word_low`
on and off, a 960-word block whose replies arrive split across TCP writes, a device past the
PLC's D range). Every value published is checked against the simulators' own encoders; the
missing DB and the out-of-range D block are `bad_config` in `BadTags` and asked for once; values
follow the PLC; powering one S7 and one MELSEC PLC off demotes only that device, and it resumes;
`opencap identity` reports the CPU (SZL, negotiated PDU) and the MELSEC CPU model; every device
logs `device.stats`; no simulator sees a write.

**Still to do on real hardware**
- S7: values match KEPServer on a real site; the link-type enum (D-050); the negotiated PDU and
  `max_pdu` per CPU; counters BCD and timers S5TIME (D-049); what a CPU returns for a missing DB,
  an optimized DB, PUT/GET disabled and an oversize read (D-048, D-051).
- MELSEC: values match KEPServer (in particular `D0000002` float32 with `first_word_low`);
  `opencap identity` confirms model 5 is a Q-family CPU and `plc_type: Q` works (D-054); which end
  codes a real CPU returns for a missing or too-short device (D-056); TCP or UDP and the
  network/PC numbers in use on site.

## Phase 4 / Phase 6: OPC UA (client driver and northbound server)

### D-045 opcua_client driver on asyncua 2.1: subscriptions inside the polling core
PLAN §5.1 says "use subscriptions (monitored items) at the device scan rate, not polling". The
core asks every driver for values once per rate-group period, so the subscription lives
inside the driver and `read()` hands out what the server pushed:
- **connect()** opens a session (security policy and message mode from params; user name and
  password from the environment variables NAMED in params), creates **one subscription per
  device** and **one monitored item per distinct NodeId** (all rate groups; tags at the same
  NodeId share it), then **reads those nodes once**, so the first `read()` (and
  `opencap read`) has values without waiting for the first publish. Publishing interval =
  `publishing_interval_ms`, else the fastest rate group; sampling interval =
  `sampling_interval_ms`, else the tag's own rate; queue size 1, discard oldest; keep-alive
  requested about every 5 s, lifetime count 4 × the keep-alive count. The server's revised
  values are used (`opencap identity` shows them).
- **read(block)** sends nothing: it returns the latest sample of each tag. `source_ts` = the
  server's SourceTimestamp (ServerTimestamp when absent; the worker's clock when both are).
  `OpcUaBlock.wire_requests()` is 0, so `opencap check` shows 0 requests per cycle for OPC UA.
- **Core change (additive): staleness by read time.** A monitored item reports only changes, so
  a constant value keeps an old SourceTimestamp, and the core's "stale after 3 × rate with no
  good read" (measured from `source_ts`) would flag it, also when the server's clock is
  behind. New `Driver.stale_by_read_time` (False by default, True here): the worker records
  when `read()` last returned each tag good and `ValueCache.expire(..., read_at=...)` measures
  the age from that. Polled drivers are unchanged. Values cached before a restarted worker
  (config reload) get one staleness period from when the new driver is created.
- **Consequence for the payload:** `DateTime` is the newest `source_ts` of the device's good
  tags (D-017). For an OPC UA device whose values did not change it can be older than the
  publish time. DataValid and BadTags are not affected. Check with the SubDevice consumer.
- **Session loss → reconnect → resubscribe.** The session is unusable when the TCP connection
  closes, asyncua's keep-alive probe fails (a Read of ServerState every max(1 s, timeout_ms),
  bounded by the same time), the subscription reports a bad status (BadShutdown, BadTimeout),
  or no publish response or keep-alive arrives for 1.5 × the keep-alive period + 2 × timeout.
  Then `connected` is False and `read()` raises `ConnectionLost`; the worker reconnects at
  once, inside the same retry budget (as for Modbus `close_on_timeout`), and a failed connect
  starts its 1 s → 60 s backoff. `connect()` builds a new session and subscribes everything
  again. asyncua's own auto-reconnect is off and its stale-subscription watchdog does not see
  this subscription: recovery belongs to the worker (as D-013 for pymodbus). The driver logs
  `opcua.session_lost` at INFO once per session; asyncua's ERROR traceback for the Publish
  request that fails with the connection is filtered out.
- **Wire lock:** an OPC UA session always has a Publish request outstanding and asyncua probes
  ServerState in the background, outside the channel's wire lock. connect, close and read take
  the lock as usual. Each device has its own session (KEPServer shares one per channel); the
  project has one OPC UA device per channel.
- **StatusCode → quality:** Good* → good. **Uncertain* → stale**: the value may be usable but
  is not confirmed good, so it does not replace the last good value, the tag is listed in
  BadTags, and the OPC UA server passes it on as UncertainLastUsableValue. BadNodeIdUnknown
  → bad_config, as asked; **deviation:** BadNodeIdInvalid, BadAttributeIdInvalid,
  BadNotReadable and BadUserAccessDenied are bad_config too (the node, as configured, cannot be
  read, not a communication fault). Every other Bad → bad_comm. A monitored item the server
  refuses keeps its status until the next reconnect (no periodic retry). Device health is the
  session's: a session that delivers only Bad values is still `ok`.
- **Values** are coerced to the tag's dtype: numbers between int, float and bool (float → int
  rounds; out of range → bad_decode), strings from String, LocalizedText, QualifiedName,
  ByteString or any scalar; arrays element-wise.
- **Addresses:** `ns=<i>;s=|i=|g=|b=`, and `nsu=<uri>;...` resolved through the server's
  NamespaceArray at connect (an unknown URI is bad_config). Parsed without asyncua at plan
  time (D-002); a bad NodeId is `Plan.invalid`.
- **Security:** None, Basic256Sha256, Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss (Basic256
  and Basic128Rsa15 if the installed asyncua still has them), Sign or SignAndEncrypt. The client
  certificate and key are paths in params; when both files are missing a self-signed pair is
  generated there (key mode 0600, logged with a hint to trust it on the server). Without
  `server_certificate` the server certificate is taken from GetEndpoints unchecked: pin it
  for production. Params named `password`, `username`... are rejected; errors name the
  environment variable, never a value. The KEPServer security-policy codes are still
  unverified (D-010).
- **Tested against asyncua's own server only** (D-047), including Basic256Sha256 Sign and
  SignAndEncrypt and user name/password. Not yet against the project's two real OPC UA
  servers: run `opencap identity` and `opencap read` on site. asyncua 2.1's server does not
  notify live monitored items when a node is deleted (its callback coroutine is never
  awaited); the driver's handling of that status is tested by setting it on a node.
- **Read-only guard (#74):** right after the asyncua `Client` is built, its `write`,
  `write_attributes`, `call`, `history_update`, `add_nodes`, `delete_nodes`, `add_references`
  and `delete_references` are replaced with raising stubs on both the `UaClient` and its
  `UaSession` (asyncua 2.1 `Node` objects call the session directly). A test shows that
  `Node.write_value` and `call_method` raise, and that the server sees no write.
- **Passwords over None (#74):** a `password_env` with security policy None is refused at
  connect, before any socket is opened, unless `params.allow_insecure_password: true`. The
  importer lists such KEPServer channels in the import summary and does not fail the import.
  asyncua is pinned `>=2.1,<2.2` because the driver touches its internals.

### D-046 Northbound OPC UA server (PLAN Phase 6)
For SCADA/HMI clients that read KEPServer today (`north/opcua_server.py`, `poller.yaml`
`opcua_server`, **off by default**):
- **Address space, KEPServer style:** `Objects/<channel>/<device>/<group>/.../<tag>`, NodeId
  `ns=2;s=<channel>.<device>.<group>.<tag>`, BrowseNames in ns 2. A tag name's dots are its
  tag-group path, as in KEPServer, so an existing client only changes the endpoint URL. The
  namespace (`namespace_uri`, default `urn:opencap`) is registered first and gets index 2;
  set `namespace_uri: KEPServerEX` for clients that address the namespace by URI. Disabled
  devices and tags are not exposed (the publisher's set). **Check one real SCADA tag path on
  site.**
- **Values** are copied from the cache when a device's cache version changes (woken by every
  device cycle, at the latest every `update_ms`), and only for tags whose value or quality
  changed. SourceTimestamp = the cache's `source_ts` at that change; ServerTimestamp = the
  copy time. KEPServer refreshes timestamps on every scan; we do not, to keep the cost
  proportional to changes (tens of thousands of tags at 1 s), which is also what OPC UA
  subscribers see by default (DataChangeTrigger StatusValue). DataType follows the dtype
  (Double for scaled tags); a variable becomes an array (ValueRank 1) at its first list value.
- **Quality → StatusCode:** good → Good; stale → UncertainLastUsableValue (value kept),
  never read → BadWaitingForInitialData; bad_comm → BadCommunicationError; bad_config →
  BadConfigurationError; bad_decode → BadDataEncodingInvalid. A value that does not fit the
  variable's type is BadDataEncodingInvalid.
- **`_System` per device:** `_State`, `_Error`, `_NoError`, `_AutoDemoted`, `_Connected`,
  `_LastGood`, `_ErrorCount`, `_LastError`, `_CycleMs`, `_DeviceId`, `_Driver`, `_Enabled`,
  from `ChannelWorker.device_status()`. `_Error`, `_NoError`, `_AutoDemoted` and `_DeviceId`
  keep KEPServer's names and meaning. All read-only (KEPServer's `_Enabled` is writable).
- **Read-only, three layers:** variables have AccessLevel CurrentRead; the user manager never
  grants the Admin role; a permission ruleset refuses Write, HistoryUpdate, Call, node
  management and server registration for every session (BadUserAccessDenied). Method calls
  are refused too, including standard ones such as ConditionRefresh. RegisterNodes (a read
  optimisation some SCADA clients use) is allowed.
- **Security:** `security_policies` (None, Basic256Sha256 / Aes128_Sha256_RsaOaep /
  Aes256_Sha256_RsaPss with _Sign or _SignAndEncrypt); certificate and key paths, generated
  self-signed when both are missing. Without `trusted_certs_dir` any client certificate is
  accepted (asyncua's default) and access rests on the user login; with it, only certificates
  in that folder (TRUSTED_VALIDATION). `anonymous` (default **off**) and one user name/password from
  the environment variables named by `username_env`/`password_env`. An enabled server with
  anonymous off and no `username_env` is a config error; if the variables are unset at start,
  the server does not start (logged, below). **Policy None on an endpoint that is not loopback
  is a config error** unless `allow_insecure_remote: true` (then it starts with a WARNING).
  These defaults were set in the takeover (#74), from the lead review of #61 and 03 §3.7.
- **Reserved name:** `_System` (any case) cannot be the first segment of a tag name, because it
  is each device's diagnostics folder. Device validation refuses it in both engines.
- **Runtime:** started after the workers and the publisher, stopped first. A start failure,
  at poller start or on reload, is logged as `opcua_server_start_failed`; polling and MQTT
  continue, and `/health` shows `opcua_server: {running: false, error}`. Reload: changed
  `opcua_server` settings restart the server;
  otherwise devices are added, removed or rebuilt (by a fingerprint of name, driver, address and
  tag types), so clients keep their sessions. asyncua builds its standard address space on every
  start (~1 s).

### D-047 OPC UA test simulator
`tests/helpers/opcua_sim.py` is a real asyncua server (no mocked wire) on a `stable_port()`
(it restarts on the same port, D-022), nodes in ns 7 like the real project, values and status
codes that survive a restart, users, anonymous off, security policies with a generated
certificate, and counters for client Read calls (`tag_reads` counts only the sim's own nodes,
so asyncua's ServerState keep-alive probe is not mistaken for polling), Write attempts and
monitored items. The first server of a test process builds asyncua's standard address space
into a shelf in a temp dir (~3 s); later servers load it in milliseconds. `stop()` cancels
asyncua's once-a-second clock task first (private attribute, guarded), which saves 1 s per stop.

### D-065 Worker overlap flake is a test artefact (#78)
- **Conclusion: test artefact, not a D-015 violation.** The `Probe` in `engine/crates/opencap-core/tests/worker.rs`
  counted a request as in flight until a manual `exit()`. `ChannelWorker::stop()` aborts the device tasks one by one,
  so a request future dropped mid-flight (also the safety-net timeout path) never called `exit()`, and a sibling
  device still polling saw 2. The real wire lock is a `tokio::sync::Mutex` guard held across the driver call and is
  released on drop, so a cancelled request cannot leave the channel locked; `src/worker.rs` is unchanged.
- **Fix:** `Probe::enter()` returns a drop guard. `probe_detects_overlap_and_is_drop_safe` is the mutation check:
  two overlapping requests still report `max == 2`, and an abandoned request releases its count.
- **Timing flake:** the panicking-`on_cycle` test waits on the call counter with a 10 s deadline instead of a fixed 300 ms sleep.
- **Verification:** 100 consecutive `cargo test -p opencap-core --test worker` runs with 8 busy loops on 4 cores: 0 failures
  (was ~4/25). Command: start `sh -c 'while :; do :; done'` x8, then loop the test.

### D-066 Tag store and change-event bus as built (#41)
`engine/crates/opencap-core/src/store.rs` replaces the Rust `ValueCache` with ADR-0005's tag store. Every Python cache
rule is kept; `tests/unit/test_cache.py` is ported case by case. Where the store goes beyond what a reader of
ADR-0005 / 03 §3.2 would expect:
- **One writer, enforced.** `register()` returns the device's only valid `DeviceWriter`. Registering the device again
  (reload) makes earlier writers stale: their writes are ignored and publish nothing. Expected: a convention. Why: on
  reload the old channel worker is stopped on a spawned task and could otherwise write once more, possibly with an
  old tag layout, after its successor started.
- **`ChangeBatch.changes` holds what changed, not every tag written.** A tag is in the batch when its value, quality or
  `source_ts` changed; a write that changed nothing (the same bad quality again, a good read with the same value and
  device timestamp) publishes no batch. The version still bumps on every update (Python rule). A new source_ts is a
  change, so every good poll of a timestamp-less protocol is published. `register()` publishes the whole device
  (`full: true`) only when the slot is new or its tags changed; `unregister()` publishes nothing (devices change only
  on reload, which restarts the north plugins).
- **On `Lagged` the receiver jumps to the newest batch.** tokio resumes a lagged receiver at the oldest slot, which the
  next send overwrites, so a subscriber that spends a few milliseconds resynchronising lags again at once. Measured
  (250k updates/s, slow subscriber): 461 resyncs in 3 s and writer p99 32 µs before; 2 resyncs/s and p99 within
  noise of the all-fast case after. The queued batches are older than the snapshots the subscriber takes next, so
  dropping them loses nothing. `north_lagged_total{agent}` counts skipped batches (overwritten plus dropped), not lag
  events; the `north.lagged` warning is logged at most every 10 s per subscriber.
- **`server_ts`** ([ADR-0006](adr/0006-quality-and-timestamps.md)) is set by the store on every accepted write,
  including bad qualities and expiry to stale. It is not in the admin API's JSON or the payload, which stay identical
  to Python's; the OPC UA server (#46) is its first consumer.
- **The MQTT publisher keeps snapshot semantics.** It is now the `mqtt` north plugin (`NorthPlugin`, agent `mqtt`) and
  wakes on its devices' batches instead of `on_cycle`; on `Lagged` every device loop re-reads its snapshot. It still
  compares snapshots with what it last published (Python's deadband rule), so payloads stay byte-identical.
  `WorkerOptions::on_cycle` remains (Python parity, tests); the runtime no longer sets it.
- **Not configurable yet:** the bus capacity is the 4,096 default (a `poller.yaml` key would change the shared
  schema; add it with the first site that needs it). `DemandReader` serves the store only (`allow_demand_reads`
  arrives with #46). The variant type keeps its Rust name, `Value` (03 §3.1 says `Variant`).
- **Benchmarks** use Criterion 0.7 (Apache-2.0 OR MIT) with default features off: 0.8 needs Rust 1.86 and the
  workspace declares 1.85. Numbers: 04 §4.1.

## Omron FINS conformance harness (#13)

### D-068 FINS simulator: sources, KEPServerEX syntax over the issue's examples, and what is not proven
- **Expected (issue #13):** the KEPServerEX syntax for an EM bank is `E0_100`, and a permissively
  licensed Python FINS client validates the simulator over UDP and TCP.
- **Done:** we followed PTC's *Omron FINS Ethernet Driver* manual (v1.083) for the syntax. It writes
  a banked EM word as `E<bank>:<word>` with a **decimal** bank (`E00:00100`, `E12:32767`; CJ2 up
  to `E24:`), and the current bank as `E<word>`. The source is the "Expansion Data Memory" rows:
  CS1 p. 59, CJ1 p. 64 (`E00:00000-E12:32767`), CJ2 p. 68 (`E00:00000-E24:32767`), with the current bank
  `E00000-E32767` on the same pages (re-checked for #166). `E0_100` is Omron's own notation (W342 5-2-2).
  The vectors list `E0_100` and `EM100` as invalid, so an importer cannot accept them silently.
  The same manual sets the rest of the vector set: data types (no 64-bit types and no Double),
  default types (BCD for timers and counters, DWord for IR), `H`/`L`/`D`/`E` strings, the `D`
  BCD suffix, and per-model ranges (CJ2's extra AR words, EM banks and task flags).
- **Independent client:** fins-driver 0.3.1 (MIT), unmodified, validates FINS/UDP only. Its
  `mode="tcp"` writes bare FINS frames on a TCP socket, with no FINS/TCP header or node
  handshake (W421 7-4), so no Ethernet unit would accept it; the simulator refuses it with
  error 1, and a test pins that. FINS/TCP is validated with frames hand-built from W421 in the
  test. The PTC driver is UDP-only, so FINS/TCP in #54 is an opencap extension.
- **Not proven by the simulator (check at commissioning, ADR-0014 §5):** KEPServerEX's word order
  for 32-bit values (its manual does not state it; Omron CPUs keep the low word first, which is
  the simulator's default); the end code a CPU returns for a word read with a non-zero bit field,
  and for more than 999 words; whether a CPU answers a frame addressed to another node (the
  simulator drops it, the driver sees a timeout). The manuals did settle three points that differ
  from first guesses: a count of 0 is a normal completion; a missing EM bank is `1101`; SRES
  bit 7 is the fatal and bit 6 the non-fatal CPU error flag.

### D-070 Modbus RTU in the Rust engine: tokio-modbus RTU over an `opencap-serial` line (#47)
- **Layout.** `engine/drivers/modbus_rtu` (`opencap-driver-modbus-rtu`, feature `driver-modbus-rtu`) is its own
  crate (#67) and reuses `opencap-driver-modbus-tcp` for everything above the wire: params, parser, planner,
  decoder, exception mapping, identity objects (`plan_tags`, `values_from_reply`, `identity_from_objects` were
  pulled out of `ModbusTcpDriver`, behaviour unchanged). A test checks that RTU plans equal TCP plans on random
  tags, so the Modbus conformance vectors (#3, #40) cover both transports. Line handling that DF1 (#53) will
  share lives in `engine/crates/opencap-serial`: `SerialSettings` and `endpoint_for` with Python's checks and
  messages, the shared-line pool, `LocalEcho`, and the TCP / local serial streams.
- **Wire.** tokio-modbus' RTU client (`rtu` feature) over a TCP stream (RTU over TCP) or a `tokio-serial` port.
  Only FC01-04 and FC43/14 are sent: the source scan and a test that a block with FC 5/6/15/16/22/23 is refused
  before it reaches the line guard this (ADR-0008).
- **Error mapping as D-036.** Timeout -> CommError "no reply within N ms", plus "socket closed (close_on_timeout)"
  when the line is closed (default). A frame with a bad CRC is dropped by tokio-modbus' framer (it resynchronises
  byte by byte), so a corrupted reply is silence and ends in that timeout, as with pymodbus. A unit id nobody
  serves is silence too. Exceptions 01/02/03 -> BlockConfigError, others -> CommError, the line stays open.
  Transport error -> ConnectionLost, the line is closed for every unit.
- **Deviation: a reply from another unit id, or with another function code** (tokio-modbus reports a header or
  function-code mismatch; pymodbus waits on and times out) is a CommError at once, and with `close_on_timeout`
  the line is closed: RTU has no transaction id, so the stream is out of step and late bytes must go.
- **Shared lines (D-033).** One connection per (driver, transport, host or device path, port). The pool is per
  process (Python: per event loop); the engine has one tokio runtime. Leases carry the connection's generation:
  after a unit drops the line, the others see "not connected" until they reconnect. The last lease dropped
  closes the line (also when a driver is dropped without `close()`). The line's lock is the per-line I/O lock
  and keeps the inter-frame gap (`inter_frame_ms`, default 3.5 characters, at least 1.75 ms).
- **Local serial ports.** `tokio-serial` 5.5 (MIT) on `serialport` 4.10 (MPL-2.0, used unmodified, ADR-0007; added
  to `engine/deny.toml`), default features off, so no libudev. Ports open exclusively (TIOCEXCL + flock on Unix),
  in a blocking task bounded by `timeout_ms`. `serialport` depends on `unescaper` (MIT OR GPL-3.0-only): taken
  under MIT. Windows: the library itself prefixes `\\.\`, so a configured `\\.\COM12` is opened as `COM12`; on
  Linux a `COMn` name gets an error that says to use the device path or `serial_ports`.
- **Not supported by the Rust serial library, valid in the config:** mark/space parity and 1.5 stop bits (the port
  cannot be opened; the error names the setting). RFC 2217 is not offered for modbus_rtu (as in Python).
- **Tests.** `tests/integration/test_rust_engine_rtu.py`: `check -v` and `read` identical to Python over RTU on TCP
  and over a pty; silent unit, unknown unit id, exceptions 02/06 and a corrupted CRC (a noisy gateway in front of
  the simulator) give the same qualities in both engines; the whole engine (`engine_factory`, acceptance) polls
  two units on one line and one behind a tty, follows value changes, demotes only the powered-off gateway's units
  and resumes them, and sends only reads. `drivers/modbus_rtu/tests/rtu_slave.rs` runs the fault paths, line
  sharing, the gap and identity against an independent RTU slave in `cargo test` (also on Windows).
- **Follow-up (not in this issue's files):** the Rust config loader does not yet apply `overrides.yaml`
  `serial_ports` or disable devices still at `transport: unmapped` (Python loader step 5, D-037). Until it does,
  a Rust engine reports such a device's connect error ("COM4 is not mapped ...") instead of skipping it with a
  warning. Configure Rust-engine RTU devices with `transport: tcp|serial` directly.

### D-071 Simulator driver: time base, determinism and grammar (#12)
- **Expected:** a KEPServerEX-style Simulator whose waveforms start when the device starts, with KEPServerEX's
  own address syntax and writable memory registers.
- **Did:** `simulator` computes every value from the address and the read time, in **whole milliseconds since
  `params.epoch`** (default the Unix epoch), not since connect. Two engines, or two runs, reading at the same
  instant return the same values, which is what a side-by-side shadow run needs (M4), and a reload or
  reconnect does not restart a waveform. `RANDOM` uses SplitMix64 indexed by the period number (random access,
  no state, trivially portable), not Python's Mersenne Twister. Integer tags round half away from zero
  (Rust `f64::round`); `RAMP` uses exact integer arithmetic when low, high and step are integer literals,
  otherwise a 1e-9 epsilon so a float step that divides the span still reaches `high`. `COUNTER` is the only
  stateful function: it counts good reads per driver instance (reset by a reload). The grammar is opencap's
  own (`RAMP(period_ms, low, high[, step])`, ...), not KEPServerEX's (`RAMP(Rate, Lo, Hi, Increment)` with
  register types); the importer keeps excluding KEPServerEX Simulator devices.
- **Why:** determinism across engines and restarts is worth more for demos, UI tests and conformance than
  "starts at low". Read-only (ADR-0008): memory registers `K<n>` return the value in `params.memory` and
  nothing can change them. The formulas are in `docs/driver_params.md` (simulator) and pinned by
  `tests/conformance/simulator/vectors.json` for the Rust port.
- **Fault switches** (`fail_connect`, `fail_every_n_reads`, `latency_ms`, `bad_decode_tags`) act inside the
  driver, so the worker's own retry, timeout and demotion paths run unchanged; latency at or above
  `timeout_ms` fails the read after `timeout_ms`, like a silent device.

### D-072 IoT Gateway JSON format for MQTT agents (#36)
`publish.format: iotgw_json` writes KEPServerEX's IoT Gateway "standard template"
(`{"timestamp","values":[{"id","v","q","t"}]}`, spec `docs/spec/payload-iotgw-json.md`). The field is per device
(`PublishCfg.format`, default `subdevice_json`), so one broker can carry both formats. Where it goes beyond what a
reader of the KEPServerEX manual would expect:
- **Full snapshot, not changed tags only.** KEPServerEX can publish only the tags that changed; opencap keeps the
  SubDevice rule (one full message per device, `on_change` decides *when*), so D-016 and the publisher are untouched.
- **Bad tags are listed** with `q: false` and their last value (or `null`), instead of being left out as in
  `Data`/`BadTags`. `payload.bad_tags_field` and `payload.timezone` do not apply; `t` and `timestamp` are UTC epoch ms.
- **`t`** is `source_ts` (ADR-0006), the message `timestamp` when a tag has none; `timestamp` is the publish time.
- **Not reproduced:** agent-level tag selection, custom templates, batching and payload-size splitting.
- **Rust engine:** add `format` to its `PublishCfg` (schema already updated), dispatch in the MQTT plugin, and run the
  vectors in `tests/conformance/payload/iotgw/` (area `payload/iotgw`).

### D-075 Readiness, about and support information (#123)

- **Ready means "first cycle finished", not "good".** `GET /health/ready` is 200 when the config is loaded, the north
  servers have been started and every enabled, polled device has finished one poll cycle with any outcome, or is
  `demoted`. A device that is down therefore counts as ready once its first cycle failed (`offline`): the SCADA start
  must not wait for a PLC that is switched off. A device that is not polled at all (disabled, bad plan) never
  waits. MQTT connectivity is not part of readiness: the outbox absorbs a missing broker.
- **Sticky.** Once ready the process stays ready until restart, and `initial_scan_ms` in `/health` is the time from
  process start to that moment. A device added by a later reload does not turn a running service unready, so an
  orchestrator never takes a working instance out of rotation because of an edit. The OPC UA server stays `Running`
  throughout (clients may connect before readiness).
- **`waiting` names device ids** (`<channel>/<device>`) on an open route, as the issue asks. Device ids come from the
  config, so the API port stays on the loopback or VPN address (already the rule).
- **`/api/v1/about` is open and carries no site data**; the full support summary is `GET /api/v1/support` (viewer
  role) and `opencap info`, which share one builder. It lists the OS name and architecture, the toolchain and library
  versions, the driver names, the enabled north interfaces and the config revision, and no IP address, host name,
  path, user name or environment variable name. `opencap info` falls back to the build facts when the config cannot
  be loaded. `version` is the package version (`0.1.0`), not `FIRM_VERSION` (`opencap-0.1.0`).
- `info.version` of the OpenAPI contract moved to 1.3.0 (additive).

### D-073 KEPServerEX tag CSV import and export (#10)
`src/opencap/config/kep_csv.py` reads and writes a device's tags as KEPServerEX's tag CSV. Header, in this order:
`Tag Name,Address,Data Type,Respect Data Type,Client Access,Scan Rate,Scaling,Raw Low,Raw High,Scaled Low,Scaled High,
Scaled Data Type,Clamp Low,Clamp High,Eng Units,Description,Negate Value` (written on one line, CRLF, UTF-8 without a
BOM; read tolerates a BOM, any column order and case, extra and missing optional columns). The spelling is from the
public KEPServerEX manual's CSV import/export description; no real export was used. Data type names map with
`kep_import.py`'s tables (Char/Byte widened with a warning, BCD/LBCD/Date imported disabled). Where it differs from what a
reader would expect:
- **`Default` data type** needs the device's default, which a CSV does not carry: `read_kep_csv(..., default_dtype=)` /
  `--default-type`; without one the row fails (kep_import skips such a tag too).
- **`Read/Write` access** is imported as read-only with a warning (ADR-0008); the export always writes `Read Only`.
- **A failing row fails the import.** Every bad row is reported with its physical line number (`KepCsvError.errors`), the
  CLI exits 1 and writes no JSON, so a half-converted tag list is never used by accident.
- **Address check is a warning, not an error:** each tag is planned alone with the driver's default parameters; a
  rejected address warns, because a driver's parameters (e.g. address style) can change what is valid.
- **`Enabled` column (opencap extension).** The CSV has no way to say "disabled", so the export appends an `Enabled`
  column only when some tag is disabled; this makes write-then-read lossless. Drop the column before importing into
  KEPServerEX. Fields that exist only in opencap or only in KEPServerEX (`Respect Data Type`, `Scaled Data Type`, array
  types, tag groups as separate rows) are written with fixed values, ignored, or rejected (arrays) respectively.
- **Scan Rate** empty or 0 means the device's rate (`scan_ms` None), unlike KEPServerEX's own default of 100 ms in an
  export, so a re-export of an imported file keeps the "device rate" meaning.
- **CLI:** `opencap tags export <device-id> out.csv` and `opencap tags import in.csv --driver <name> --out tags.json
  [--default-type <dtype>]`. Wiring the JSON into a project waits for project v2 (ADR-0013).

### D-074 Reserved system-tag names in config validation (#128)
- **Expected:** one rule for every `_` name. Spec 3.2 lists errors for the folder names and a warning (v1) for
  any other name segment starting with `_`.
- **Done:** errors in the Python and Rust models for a channel named `_System`, a device named `_System` or
  `_Statistics`, and a tag whose first dot segment is `_System` or `_Statistics` (all case-insensitive; the
  message names the field and the reserved word). `_Statistics` is allowed as a channel name, as the spec only
  reserves `_System` there. The loader adds one warning per other name with a `_` segment, after all other
  warnings, only for `devices.json` v1. The REST API refuses names that would add such a name (422) and still
  edits a file that already has one; it compares the names before and after, so existing names never block an
  unrelated write. The KEPServerEX importer skips a reserved tag or channel and excludes a reserved device,
  each with a line in the import problems, and keeps other `_` names with a note. Not done: the `_North`
  and `_Redundancy` agent-name errors (poller.yaml agents; separate follow-up).
- **Why:** no I/O name may collide with a system-tag folder (PRJ-13, LOG-07), and a project that already
  has such names must still load, be edited and import.

### D-076 ab_logix in the Rust engine: a native CIP client, read services only (#43)
- **Library choice: none.** Compared on the issue's criteria: *Multiple Service Packet control* (the
  Micro800 probe needs to send one MSP on demand and read the per-member and whole-packet
  status): `rseip` and libplctag decide batching themselves; a native client sends exactly what the
  probe asks. *Tag-list upload and caching by identity*: libplctag keeps its own per-handle tag
  state and rseip has no upload; native code uploads once and keys the list by (product name,
  product code, serial, revision) as Python does (D-025). *Connection size / Large Forward Open*:
  native code sets both. *Read-only guarantee*: the native crate has builders for read services
  only (Read Tag, Read Tag Fragmented, Multiple Service Packet of reads, Get Attribute List, Get
  Instance Attribute List, Get Attributes All) and refuses `WRITE_SERVICES` in the packet builder
  and at the send; with libplctag the write API is in the same binary. *Cross-compiling* (aarch64,
  Windows): pure Rust, no C toolchain. Licence: no new dependency (`rand`, `regex`, `indexmap`,
  `tokio` already in the tree), so `deny.toml` is unchanged; libplctag (MPL-2.0 or LGPL) is not
  linked.
- **Scope of the client:** TCP + RegisterSession, ListIdentity (replaces Get Attributes All on
  the identity object: same fields, one request), Large Forward Open then standard (500 B) on
  refusal, connected messaging (SendUnitData, sequence counts), program name (Logix), the Symbol
  Object instance list (controller scope and every `Program:` scope, External Access from
  revision 18) and the UDT templates behind it (member existence, the Logix STRING layout).
  Names are always addressed symbolically, so a reused tag list can never point at the wrong tag.
- **Behaviour parity with D-008/D-025:** missing tag (also a missing UDT member, External Access
  None) -> `bad_config`, never sent; case-insensitive fallback logged once; probe auto/on/off with
  the same outcomes and the 2-misses give-up; a whole batched read failing on a Micro800 switches
  batching off (auto only); greedy packing by reply size against the negotiated connection size,
  an entry larger than the packet is read alone (fragmented); de-duplication; error mapping;
  connect budget `max(60 s, 20 x timeout)` for the first upload, `5 x timeout + 1 s` after.
  The conformance vectors (`ab_logix/plan`, `ab_logix/decode`) pass, and so does the Phase 2
  acceptance with `--engine=rust` against `tests/helpers/cip_sim.py`.
- **Known differences from the pycomm3 driver:** a whole-structure read is `bad_decode` unless it
  is a Logix STRING (`dtype: string`); address a member. `keyswitch` is not in `identity`.
  Fragmented reads of one element are exercised against the simulator only with small connection
  sizes. Untested on real controllers, like the Python driver (D-025): check `opencap identity`.
- **Side fix found by the parity test:** `py_float_repr` now breaks exact ties to even like
  CPython (a float32 widened to f64, e.g. 819.40802001953125, printed one digit apart).

### D-077 Automatic tag generation: `Driver.browse()` and `opencap browse` for ab_logix (#11)
- **Contract:** `async Driver.browse(*, expand_arrays=False) -> BrowseResult(tags, skipped)` on a connected
  driver, not the `-> list[Tag]` of the issue: the CLI must report what was skipped and why, and a second
  return channel is clearer than a side attribute. The default raises `NotImplementedError("browse not
  supported")`. Read and list services only (ADR-0008); every generated tag must plan cleanly
  (`opencap browse` exits 3 if one does not, a bug).
- **ab_logix** maps the tag list pycomm3 uploaded at connect (D-025), so browse sends **no request of its
  own**: the simulator test checks the request count does not move and that only Get Attributes, Read Tag
  (fragmented), Multiple Service Packet and the tag-list upload were ever served.
- **Mapping:** BOOL, SINT/USINT (widened to int16/uint16, lossless), INT, UINT, DINT, UDINT, LINT, ULINT,
  REAL, LREAL, Logix STRING structures (any length) and Micro800 SHORT_STRING. A UDT becomes one tag per
  atomic member, one level (`PUMP_1.TOTAL`; the dot makes the UDT a tag group); names keep the controller's
  spelling and are sorted without case. The description is the controller type (`REAL`, `SimUdt.TOTAL: REAL`):
  pycomm3 does not upload tag descriptions.
- **Arrays:** the driver reads single elements only (`{n}` block reads are refused and a list cannot be
  coerced to a dtype), so an array is **skipped with a reason** unless `--expand-arrays`, which makes one tag
  per element (`TREND_1[0]`, `M[0,1]`, row-major), at most 4096 per array. Logix BOOL arrays (DWORD-packed)
  are skipped.
- **Skipped with a reason:** program-scoped tags (the issue asks for controller scope; Micro800 cannot read
  them), External Access None, arrays of UDTs, nested UDTs and STRING members (not simulated, D-024),
  bit-string and other non-atomic types (BYTE, WORD, DWORD, LWORD, TIMER, MESSAGE...).
- **Output:** a JSON tag list (`Device.tags` shape) to `--out tags.json` or stdout; `--out tags.csv` uses the
  KEPServerEX CSV writer (#10) when it is in the build, else it fails with that reason. Merging into a device
  is the config API's job (plan DEV-12), not this command's.
- **Rust:** not ported; follow-up issue (trait method, ab_logix mapping on its own CIP client, same skip rules).

### D-079 Config and API events, refusal records, API start and bind failure (#135)

- **Events** (Python engine): `config.write_failed` (ERROR: `who`, `via`, `action`, `files`, `reason` = `read_only` |
  `disk_full` | `partial` | `io`, `status`, and for `partial` the `.bak` files to copy back as `restore`),
  `config.change_rejected` (INFO: `who`, `via`, `action`, `target`, `status`, `count`, first error's `loc` and `msg`),
  `api.request_refused` (WARNING for 401 and 403, INFO otherwise: `method`, `path`, `status`, `who`, `client`, `via`,
  `suppressed`), `api.started` (INFO: `bind`, `port`, `open_mode`, `tokens`) and `api.bind_failed` (ERROR: `bind`,
  `port`, `reason` = `in_use` | `permission_denied` | `address_unavailable` | `bad_address` | `config` | `io`).
- **One event per refusal.** A refused config write (any 4xx of a POST/PUT/PATCH/DELETE through the config API,
  including 412, 422 and 428) is `config.change_rejected` only; `api.request_refused` covers everything else with
  401, 403, 412, 413, 422 or 428 (bad token, role, body size, malformed query). Nothing is written to the audit log
  (D-043). Field values, bodies, query strings and token values are never logged; `who` is the token name or `unknown`.
- **Flood control.** `api.request_refused` is written at most once per second per client address and status; the next
  line carries `suppressed`. This is a local limiter; replace it with the shared flood control of #133 when that lands.
- **Open question 7 (plan 15): the client address is acceptable** in these lines. The API listens on loopback or a VPN
  address, the address is the peer's (behind a reverse proxy it is the proxy's), and it is what an operator needs to
  find a failing client. It is not logged for accepted requests.
- **API start.** The API socket is bound by opencap before uvicorn takes it, so a taken port, a bad address or open
  mode on a non-loopback bind is reported as `api.bind_failed` and `opencap run` exits with status 1 after stopping what
  had started. Python had no start line before; Rust logs `api_started` (`bind`, `port`).
- **Legacy name.** Rust keeps `api_started` for now: existing INFO names are frozen until LOG-R1's rename question is
  decided (#134). Python emits only the dotted `api.started`, so until then the engines differ in this one name.
- **Correlation (LOG-R15).** `config_reloaded` carries `revision`, and `who` and `via` when the reload came from the API;
  `poller_started` carries `version`, `engine`, `instance` and `revision`. Audit entries already carry `revision`.
- **Rust engine** (not in this change, no Rust build was run): `config.change_rejected`/`config.write_failed` come with
  the Rust config API (API-35, #140); until then it needs `api.request_refused` for its 401, 403 and 413 answers with the
  same fields and the one-per-second limit, `api.bind_failed` (reason codes above) with a non-zero exit when the port
  is taken, `revision` on `config_reloaded`, and `engine`, `instance`, `revision` on `poller_started`.
- **Secrets test.** `tests/unit/test_api_events.py` and `tests/integration/test_api_events_runtime.py` run the API with
  known token and broker-password values and fail if any captured event or audit line contains them.
### D-080 melsec_mc in the Rust engine: a native MC 3E binary client, read commands only (#45)
- **Decision:** `engine/drivers/melsec_mc` (`opencap-driver-melsec-mc`, feature `driver-melsec-mc`)
  ports the Python driver (D-053 to D-057) with its own client instead of pymcprotocol (no Rust
  crate exists; the framing is a 15-byte header plus a device spec). The address parser, planner,
  decoder, params validation and error texts are the Python ones; conformance areas
  `melsec_mc/plan`, `decode`, `frame` and `reply` (1,048 vectors generated from Python, request
  bytes taken from pymcprotocol itself) pass in `opencap-conformance`.
- **Whole frames (D-053):** a TCP reply is read as the 9-byte header and then exactly the bytes it
  announces; the reply must carry exactly the words asked for, else `Comm` and the socket is
  dropped. A reply split across segments reads correctly (unit test and the simulator's
  `split_replies`). 3E has no sequence number, so a timeout also drops the socket.
- **Read-only:** `frame.rs` can build only 0401/0000, 0401/0002 (iQ-R) and 0101/0000;
  `check_read_only` runs before every send. Tests assert 0x1401/0x1402/remote run/stop and the
  rest of the write list are refused and never named elsewhere in the sources.
- **Error mapping (D-056):** same end-code classes: `BlockConfig` for 0xC051-0xC054, 0xC056,
  0xC059, 0xC05B, 0xC05C, 0xC060, 0x4030, 0x4031, `Comm` otherwise; the socket stays open.
- **Not ported:** ASCII frames and 4E (as in Python). Message text for OS errors differs from
  Python's exception text. Untested on real PLCs, like the Python driver.
- **Licence:** no new dependency; `deny.toml` unchanged.
### D-081 Driver catalogue, params schemas, collection queries (#129)

- **Schemas live next to each driver** as `Driver.params_schema` (JSON Schema draft 2020-12, built with
  `drivers/_schema.py`), with `transport`, `dtypes`, `address_examples` and `supports_browse`; `supports_identity` is
  true when the driver overrides `identity()`. `GET /api/v1/drivers[/{name}]` serves them (viewer role). The schemas
  state types, ranges and enums; cross-key rules (OPC UA security policy needs a certificate, a Logix path with an odd
  number of segments) stay in the driver and still surface at plan time.
- **Lenient on purpose.** Unknown keys are allowed (drivers ignore them and the importer keeps provenance keys), and a
  `null` value means the default, as in every driver. Booleans must be `true`/`false` and numbers numbers: the 0/1 and
  string spellings some drivers still tolerate in hand-written YAML are not accepted on an API write. A `PATCH` that
  leaves such a value in place therefore gets a 422 until it is corrected.
- **Only API writes are checked** (device create, update, and channel create with devices). Loading files from disk,
  `opencap check` and import are unchanged, so existing configs keep loading. The 422 `loc` of a bad param is now
  `["params", "<key>"]` instead of `["params"]`; the message no longer comes from the driver constructor.
  This tightens request validation inside `/api/v1`, which `api/README.md` calls a v2 matter; the issue asks for it
  and the project is pre-release, so the contract moved to 1.4.0 (additive routes and parameters).
- **Own validator, no new dependency.** `validate_params` implements `type`, `enum`, `minimum`, `maximum`,
  `minLength`, `items` and `required`; tests run every schema through `jsonschema` (a dev dependency) and compare.
- **Collections.** `q` matches `name` and `description` (channels and devices have no description, so only the name);
  `sort` offers a documented field list per collection (channels: name, enabled, devices; devices: id, channel, name,
  driver, host, port, enabled, tags; tags: name, address, dtype, scan_ms, enabled, description). Strings sort naturally
  without case (`A2` before `A10`), numbers numerically; an item without a value goes last in both directions. Without
  `limit` the whole collection is returned and `limit` is `null`; `total` counts after filtering. `/channels/{c}/devices`
  is unchanged.
- **Rust engine** (API-35, #140): serve the same `/drivers` entries (the schemas are data; generate a JSON file per driver
  from the Python classes into the shared fixtures so both engines return identical documents), run the same schema check on
  device writes with `loc` `["params", key]`, and add `q`, `sort`, `order`, `limit`, `offset` to the three list routes with
  the natural-order comparison above (case-folded text split into digit and non-digit runs).

### D-083 Broker restarts against Mosquitto with persistence lose nothing (#8)
Closes what D-021 left open. `tests/integration/test_mosquitto_restart.py` (acceptance) restarts
a real Mosquitto (`persistence true`, `max_queued_messages 0`, clean SIGTERM stop, same port and
data directory; `tests/helpers/mosquitto.py`) three times, 2 s down each, while 10 simulated
Modbus devices publish every second to a persistent QoS 1 subscriber (`clean_session` false).
The first restart is with the subscriber connected. For the second and third the subscriber is
away, so the broker holds messages it has already PUBACKed to opencap (whose outbox has deleted
them) across the restart: the case persistence exists for. The check is D-018's: freeze what was
produced once the outbox drains, then every frozen message must reach the subscriber, in order.
- **Observed** (Mosquitto 2.0.18 binary, 20 runs with the Python engine, 1 with the Rust
  engine): lost 0 every run. The outbox held about 20 messages per outage; the broker held
  60–80 per away restart. Duplicates: 0 to 37 per run, mostly 0 or 7 (one on each of 7 devices):
  QoS 1 redelivery of messages whose PUBACK the broker had not processed before the stop or the
  subscriber's disconnect. They are allowed and counted, never compared away.
- **The test can fail:** with `persistence false` (a negative control) every run lost 16
  messages per device and no `mosquitto.db` was written; the test reports both.
- **Test-client finding:** with aiomqtt as the subscriber, about 1 run in 15 lost one message.
  aiomqtt 2.5.1's `MessagesIterator.__anext__` takes the next message off its queue in a separate
  task; if the iterating task is cancelled (the test pausing the subscriber) or the connection
  drops in the same instant, that message is discarded after paho has PUBACKed it. Reproduced
  deterministically outside the test. The subscriber now uses paho directly and records each
  message in `on_message`, which runs before the PUBACK. The aiomqtt `Collector` in
  `tests/helpers/broker.py` iterated the same way; #220 moved it to paho's `on_message` too.
- **Mosquitto as root** drops to its `mosquitto` user, which cannot write under pytest's 0700
  `tmp_path` ("unable to remove mosquitto.db.new"): the helper adds `user root` when it runs the
  binary as root. The Docker image (`eclipse-mosquitto:2.0.22`, pinned by digest) runs as the
  test's own user (`--user uid:gid`): as root, its entrypoint chowns the bind-mounted data
  directory to uid 1883, and the test could no longer manage its own temp directory (first CI run).
- **CI:** the Linux acceptance jobs (`acceptance-pr`, nightly `acceptance`, the Rust engine's
  acceptance) run it through Docker and set `OPENCAP_REQUIRE_MOSQUITTO`, so a missing broker
  fails the job instead of skipping the test. Windows skips it with a reason (no binary, and the
  runner's Docker runs Windows containers).
- **Not covered: a broker crash.** Mosquitto writes its persistence file on a clean shutdown and
  every `autosave_interval` (default 1800 s). After a SIGKILL or power loss it can lose what it
  accepted since the last save, including messages opencap's outbox has already deleted after
  their PUBACK. A production broker should save more often (`autosave_interval`, or
  `autosave_on_changes`) as far as its disk allows.

### D-084 Naming rules and the payload time zone in the Python engine (#112)
- **Expected:** names that make tag paths ambiguous are caught at load, and a bad `payload.timezone`
  fails before payloads are built; no name is ever altered.
- **Done (Python half):** `name_rule_problems` in `config/models.py` lists, besides the `_` rule of
  D-074, a channel or device name that is empty, has surrounding whitespace, or contains `"`, `.` or
  `/`; a device named like a REST sub-resource (`tags`, `values`, `plan`, `identity`, `status`,
  `read-once`); and a tag whose dot segment is empty, padded or contains `"`. For a v1
  `devices.json` each is a loader warning (so `opencap check` shows it) and an API write that would
  add one gets a 422 with the `loc` of the name; a name already on disk never blocks an unrelated
  write. `payload.timezone` must be a tz-database name (or one of the three fixed-offset fallbacks
  of `north.payload`), else the model is refused naming `payload.timezone`. Names with `%`, `#`,
  `?`, `&` and `+` round-trip through their percent-encoded path (pinned by a test).
- **Rust half:** `name_rule_problems` and `PayloadCfg::validate` in `opencap-core` give the same texts
  and the same warnings (compared by `compare.py` on `valid/devices_bad_names.json`); the zone names
  both engines accept or refuse are in `tests/conformance/timezones/zones.json`.
- **Not done yet:** the v2 error severity (waits for #37), the importer reporting these names, and
  `params.device_timezone` with its DST vectors (DEV-13).
- **Why:** PRJ-13, API-07, LOG-21.

### D-085 s7 in the Rust engine: a native S7comm read subset, read PDUs only (#44)
- **Decision:** `engine/drivers/s7` (`opencap-driver-s7`, feature `driver-s7`) ports the Python driver
  (D-048 to D-052) with its own client instead of python-snap7 (LGPL snap7 cannot be linked statically,
  ADR-0007; no Rust crate with a usable licence). TPKT and COTP connect with rack/slot/link-type TSAPs,
  setup communication (PDU negotiation), read var, SZL read for `identity` (0x001C, 0x0011, 0x0424, with
  follow-up fragments). Address parser, planner, decoder, params validation and error texts are the Python
  ones. Conformance areas `s7/plan`, `decode`, `frame` and `reply` (python-snap7 builds the frame bytes and
  parses the replies) pass in `opencap-conformance`; one request/reply is also checked against the S7comm
  field layout, independently of snap7 (`proto.rs` unit test), as the #60 review asked.
- **Read-only:** `proto.rs` builds only setup communication, read var and the SZL read; `check_read_only`
  runs before every send. Tests refuse write var (0x05), download/upload (0x1A-0x1F), PLC control (0x28),
  stop (0x29) and other USERDATA groups, and scan the sources for them.
- **PDU split counted (D-051 review item):** `S7Block::wire_requests()` reads the PDU the CPU negotiated
  (shared with the driver that planned the block) and counts the split, so the worker's safety net and
  `check` budget the real number of requests. A block planned by another driver instance (the registry
  plans with its own) still counts the planned PDU: the worker's per-call budget is then one request short
  for a block that splits.
- **Multi-item read var:** the builder and parser take several items per request (tested), but the `Driver`
  trait reads one block at a time, so the driver sends one item per request, like Python. Packing scattered
  blocks of one cycle into one request needs a batch-read method on the trait (not part of this change).
- **Error mapping (D-051):** item return codes 0x03, 0x05, 0x06, 0x07, 0x0A and header error 0x8104 are
  `BlockConfig`; other item codes and header errors are `Comm` and keep the connection; a timeout, a
  malformed reply or a PDU reference ahead of the request drops it; a reset or close is `ConnectionLost`.
  A reply with an older PDU reference is skipped (up to 3), like python-snap7.
- **Not ported / differences:** peripheral I/O (PI/PE/PQ/PA) is refused as in Python; USERDATA SZL parsing
  covers the three lists `identity` reads, and a failing SZL read reports `<name>_error` with Rust's text.
  Untested on real CPUs, like the Python driver (D-058).
- **Licence:** no new dependency; `deny.toml` unchanged.

### D-086 The Rust engine as a service: sd_notify by hand, a native Windows service (#57)
- **Linux:** `deploy/opencapd.service` is `opencap.service` with `Type=notify`. The engine sends
  `READY=1` once the config has loaded, the poller has started and the API listens; `RELOADING=1`
  and `READY=1` around a SIGHUP reload; `STOPPING=1` on SIGTERM. It speaks the sd_notify protocol
  itself (one datagram to `$NOTIFY_SOCKET`, path or abstract name, `src/sdnotify.rs`) rather than
  linking libsystemd: no new dependency, and nothing happens outside systemd. Tested against the
  real binary with a socket the test owns (`test_rust_engine_sdnotify.py`), and the unit with
  `systemd-analyze verify`.
- **Windows:** a native service through `windows-service` (MIT OR Apache-2.0), not WinSW:
  `opencap-engine service install|uninstall|run`. Stop and shutdown reach the same stop future
  as SIGTERM, so the shutdown path is the one the acceptance suites exercise. Recovery:
  restart after 5 s, 5 s, 30 s, also on a non-zero exit. `%ProgramData%\opencap` is the working
  directory; logs go to a file there, because a service has no console.
- **Event log:** written with Windows' own `eventcreate.exe`, which registers the source on
  first use (at install) with its message file, in which ids 1–1000 show the text as written;
  uninstall removes the source with `reg delete`. The workspace forbids `unsafe`, so no FFI to
  ReportEventW. Only start, stop and a fatal error are written there; the JSON log has the rest.
- **Not yet:** a reload on Windows (no SIGHUP; stop and start), and the desktop launcher handing
  over to this service (#58 left that to this issue's follow-up).

### D-092 Out-of-process plugin protocol v1 and the Python driver host as built (#49)
`proto/opencap/plugin/v1/driver.proto`, `engine/crates/opencap-plugin`, `src/opencap/plugin_host.py`. Where they go
beyond ADR-0004 §3 / 03 §3.3:
- **Opt-in per device: `plugin: python`** (devices.json or overrides.yaml; `plugins.python` in poller.yaml says how to
  start the host). Expected: the engine picks the host for drivers it lacks. Why: routing stays explicit and
  reviewable, a native port can take over device by device, and the Python engine ignores the key.
- **One host process per channel**, so the host's single executor is the channel executor (D-015), and a crash or
  hang in a PLC library affects one channel only. Cost: one Python process (~40 MB) per such channel.
- **`plan()` stays pure in Rust.** `PluginRegistry::prepare()` asks the host to `Plan` every rate group before the
  engine builds its registry (start, reload, `check`/`read`/`identity`); `PluginDriver::plan()` answers from those
  plans. A device the host cannot plan (bad params, no Python, the host does not start) is a driver-factory error:
  listed, not polled.
- **Added RPCs `Hello` and `Open`**: `Hello` checks the protocol and the served drivers; `Open` creates the device's
  driver instance in the process, and `NOT_OPEN` tells the engine to open it again after a restart. Generations are
  unique across hosts, so a device is never taken as open in a successor process. Connect/Close replies also carry
  `DriverInfo`, since a connect budget can change once connected (PCCC: 60 s before the file directory is cached,
  10 s after).
- **Block payloads are pickles signed with HMAC-SHA256** under the run's token; the host verifies before it
  unpickles and caches verified blocks. Opaque to the engine, and stateless across host restarts. A payload that
  does not verify is `BLOCK_CONFIG` (block disabled until reload), never deserialised.
- **Transport:** Unix socket in a 0700 directory; loopback TCP (Windows, or `transport: tcp`) needs the token, which
  every call carries. Named pipes are not used. The host exits when its stdin closes, so it never outlives the engine.
- **Restart:** a process that exits, or a call that fails in transport, ends its generation; the next start waits
  1 s, 2 s ... 60 s (reset after 60 s alive). Its devices see `ConnectionLost`, go `bad_comm`, and recover by the
  worker's normal reconnect.
- **Not wired yet:** `Browse` has no caller in the Rust trait; `Subscribe` returns `UNSUPPORTED` from the Python host
  (push drivers are served through `Read`, as in the Python engine); `stale_by_read_time` is reported but the Rust
  worker does not use read times (D-045) yet.
- **Overhead target missed:** ~0.6 ms p50 / ~1.1 ms p99 added per read (target < 0.5 ms p99), almost all of it the
  fixed per-call cost of Python grpcio (04 §4.1). Kept for now (small against PLC round trips); #252 tracks the
  target (batched reads per call, a leaner `v2` transport, or the native PCCC port in #48), so #49 stays open.
- **Codegen:** Rust compiles the `.proto` with protox in `build.rs` (no `protoc` on build machines); tonic is pinned
  at 0.14.5 for the workspace's Rust 1.85. Python stubs are committed (grpcio-tools 1.85.4), with a drift test; the
  `plugin` extra needs protobuf >= 7.35.1 and grpcio >= 1.85.4, the stubs' gencode versions.
- **Harness:** `opencap-engine drivers` reports `"plugins": ["python"]`, and `@pytest.mark.drivers(..., plugin="python")`
  lets an engine-agnostic suite run on the Rust engine through the host (Phase 3 does).

### D-088 OPC UA server hardening (#28)
Builds on D-046; the L1 suite (#29, `tests/scada/`) checks it with Eclipse Milo, open62541 and asyncua.
- **Per-server asyncua overrides, no global patch** (`north/opcua_services.py`): a `Server` subclass
  whose binary server hands every connection a processor subclass (it answers BrowseNext; asyncua's
  dispatcher has no branch for it), our `InternalSession` subclass (installed through
  `iserver.create_session`) and our `MonitoredItemService` subclass per subscription. `UaServer.start`
  is asyncua 2.1's with that one change; the `opcua` extra pins `asyncua>=2.1,<2.2` (D-045).
- **Deadband against the last reported value.** asyncua compared each sample with the previous sample,
  so a value ramping by less than the deadband per sample was never reported (found by the L1 suite).
  Now: Part 4 7.22.2 (absolute; percent of the EURange span; arrays element-wise), a status change is
  always reported, a deadband on a non-numeric variable is refused (`BadFilterNotAllowed`; before, it
  raised inside the value copy loop and stalled updates for every client), percent without an
  EURange is `BadDeadbandFilterInvalid`. The decoded filter is duck-typed: asyncua re-creates
  structure classes from the nodeset, so `isinstance(..., ua.DataChangeFilter)` can be false.
- **Writes** are answered per node with `Bad_NotWritable` (`Bad_NodeIdUnknown` for an unknown node)
  instead of a ServiceFault `BadUserAccessDenied`; nothing is written. HistoryUpdate, Call and node
  management stay refused by the permission ruleset.
- **Browse paging:** at most `max_references_per_browse` (default 1000, or fewer if the client asks)
  references per node, the rest through continuation points (16 per session, then
  `BadNoContinuationPoints`), BrowseNext with release. asyncua ignored RequestedMaxReferencesPerNode.
- **Limits:** `max_sessions` (`BadTooManySessions`), `max_subscriptions_per_session`,
  `max_monitored_items` server-wide (`BadTooManyMonitoredItems` per item),
  `min_publishing_interval_ms` (revised up) and `min_sampling_interval_ms` (revised up in the
  result). Sampling is change-driven: values reach the address space when a device cycle ends or
  every `update_ms`, so the sampling interval is reported, not enforced per item; the publishing
  interval bounds the notification rate.
- **Address space:** each device is one AddNodes batch (asyncua's helpers read the parent's type
  definition per folder). `ValueRank`/`ArrayDimensions` come from the address through the driver's own
  pure parser (Modbus, S7, MELSEC: `[n]`, `[r][c]`; brackets mean an element elsewhere, e.g. Logix),
  before the first value; other drivers still switch to ValueRank 1, ArrayDimensions `[0]` at their
  first list value. Scaled tags are `AnalogItemType` with `EURange` (eng_lo..eng_hi, sorted) and
  `EngineeringUnits` (UNECE Rec. 20 code for about 40 common units, UnitId -1 otherwise).
  `Description` is the tag's description (empty, not the name, when there is none).
- **Namespace:** `kepserverex_namespace_index: true` (default) makes start fail if the tag
  namespace would not be ns=2.
- **Security:** `pki_dir` with `own/` (generated server certificate, key 0600), `trusted/`, `issuers/`
  (intermediates for chain building only), `crl/`, `rejected/` (refused client certificates saved as
  `CN [thumbprint].der`), `users/` (X.509 user tokens, exact certificate match). The folders are read
  on every CreateSession, so trusting a client needs no restart. An empty `trusted/` refuses every
  client cleanly (`BadCertificateUntrusted`; the cryptography store cannot be empty). Several users
  through `users: [{username_env, password_env}]`; the single pair still works; a user whose
  variables are unset is skipped with a warning. X.509 user tokens: the session records the token
  type before asyncua calls the user manager, because asyncua passes the channel certificate for
  anonymous tokens too. `advertised_endpoint` binds on `endpoint` and advertises one URL
  (asyncua otherwise echoes the URL each client used); a generated certificate names the advertised
  host, or this computer's name for a wildcard bind.
- **Quality → StatusCode** comes from `core/quality.py` (#30), unchanged here.
- **Not done here:** `maxAge=0` demand reads (`allow_demand_reads`, 03 §3.5) need a rate-limited
  read path into the ChannelWorker; every read is served from the cache (proposed as its own issue).
- **Scale** (`tests/integration/test_opcua_scale.py`, acceptance; this container, 4 vCPU,
  Python 3.12): 100,000 variables in 100 devices build in 9.4 s with garbage collection paused
  (19-22 s with it running) and take 820 MiB (966 MiB process RSS); 20,000 monitored items at 1 s
  are created in 0.8 s. Server CPU, monitored items changing per second: 2,000 → 7 % of a core
  (GC paused) / 19 % (GC on), 5,000 → 34 %, 10,000 → 56 %. At 10,000/s and more the asyncua *client*
  (one Python process) falls behind and its keep-alive times out, so the server's own ceiling was not
  reached with that client. asyncua's deep copy of every DataValue in the monitored-item callback was
  over half of the CPU and is now a shallow copy (the server writes a new DataValue per change).
  A full GC over an address space this size pauses the loop for seconds (hence `no_gc` in the
  test); the Rust server (#50) is the target for larger plants.

### D-089 Project v2, part 1: what moves into `project.json`, and when the layering runs (#37)
- **Split:** #37 ships in two PRs. Part 1 is the v2 layout, the loader, `opencap config migrate` and
  `import-kep --format v2`. Part 2 is the `ProjectStore` protocol, the v2 store (revision counter,
  `.revisions/`) and the v1 compatibility routes.
- **`project.json`** is a `DevicesFile` with `"version": 2` and `revision`. It holds the devices as v1 layering
  leaves them: `overrides.yaml` device, params and tag fields, channel `enabled`, publish settings from devices.json,
  map `defaults`, overrides and map entries, and mapped `serial_ports`. It is written in `devices.json`'s format (one
  tag per line, nulls left out). **Not** baked in: a default topic (it would stop following `mqtt.topic_prefix`) and
  the "unmapped serial line → disabled" rule. Both are finalize steps that the v2 loader applies on every load, as v1
  does. An unmapped line therefore stays `transport: unmapped`, `enabled: true` in the file and is disabled with a
  warning when loaded.
- **`node.yaml`** is `poller.yaml` copied byte for byte, comments included. `NodeConfig` has exactly
  `PollerConfig`'s fields: ADR-0013 puts north agents in the project, but they are MQTT and OPC UA settings in
  `poller.yaml` until ADR-0005's north plug-in model exists. Without `node.yaml` the v2 loader reads `poller.yaml`
  and warns.
- **Naming rules:** a name segment starting with `_` and the other PRJ-13 naming problems are warnings in v1 and
  **errors** in a project v2 (system-tags spec 3.2), so a v1 project that breaks them cannot be migrated until the
  names are fixed (migrate refuses, nothing written). `name` and `title` in `project.json` feed the system tags
  `_ProjectName` (default: the directory name, as in v1) and `_ProjectTitle`.
- **Lost on purpose:** `devices_map.yaml`'s `stub: true` flag has no v2 field. `config migrate` reports the stub
  list once, among the v1 warnings, and the SubDeviceIds go in as given.
- **Safety:** `config migrate` loads the would-be v2 files from a scratch directory first. Every `Device`, channel
  (name, enabled), the excluded list and the node settings must equal the v1 load, or nothing is written. It writes
  `node.yaml`, then `project.json` (atomic). A crash before `project.json` leaves a directory that still loads as v1;
  running migrate again finishes the job. The v1 files are then **moved** to `config/v1-backup/` (or `-2`, `-3`...),
  never deleted, so they stop producing "ignored" warnings.
- **`import-kep --format v2`** runs today's import, with the same stub-append and overrides-template rules, and then
  migrates. Both formats refuse a directory that already has a `project.json`: a re-import into v2 is the three-way
  merge follow-up.
- **Config API on v2, until part 2:** every `ConfigStore` route answers 409, because the v1 files it would edit are
  gone or ignored. `load_disk()` (the reload-pending tracker) loads v2 through the loader, so status, values and
  reload keep working. The revision is then a hash of `project.json` and `node.yaml`.
- **Rust:** its loader reads v1 only. Follow-up #186: load `project.json` + `node.yaml` with the same finalize
  steps, and the shared config fixtures for the `project` and `node` documents.

### D-091 Omron FINS driver, Rust-first (#54)
- **Layout.** `engine/drivers/omron_fins` (`opencap-driver-omron-fins`, feature `driver-omron-fins`, a default). There
  is no Python FINS driver (ADR-0002 §4): `omron_fins` is added to `DriverName` and `KNOWN_DRIVERS`, and the Python
  engine reports it "not implemented yet". Written from docs/protocols/omron-fins.md and accepted against
  `tests/helpers/fins_sim.py`; the address parser passes every `omron_fins/addresses` vector (111).
- **Read-only.** `wire::Command` can only be 0101, 0104 or 0501; every frame is checked again before it is sent, and a
  test feeds every write/control command code of W342 5-1-1 to the guard. The simulator's `write_attempts` stays 0.
- **Planning.** Bits are read as part of their word (word code), so `D100` and `D100.03` are one read; flags (TS, CS,
  TK) are 1-byte elements with the flag code; IR is a 4-byte element. Per area code, items merge when the hole is at most
  `max_gap` (8) elements and the read stays within `min(request_size, 1998)` bytes; timers (below 0x8000) and counters
  (0x8000+) share code 0x89 but never one read. When at least two ranges are single elements, they share a MULTIPLE
  MEMORY AREA READ (0104, up to 167 items): in the integration test, 33 tags cost 5 requests.
- **0104 fallback.** A 0104 refused with 0401/0402 switches the device to 0101 for good (logged once; the block's
  `wire_requests()` then counts one per range). A 0104 refused with any other configuration end code (the simulator
  answers the whole command with the failing item's code, protocol notes §2.2) is re-read range by range in the same
  call, so only the ranges the CPU rejects become `bad_config` (and are not read again); the others keep their values.
- **End codes** as the protocol notes §4 (`code & 0x7F3F`; a relay error word after MRES bit 7 is skipped). A reply
  is matched by SID and command code; any other frame (a late reply, another client) is dropped while waiting, so a
  timeout is a CommError and the socket is kept (no `close_on_timeout`: SIDs make late replies harmless). CPU error
  bits (SRES 7/6) with a normal end code are logged once (`omron_fins.cpu_error`); the data is used.
- **Transports.** UDP binds an ephemeral port by default (`local_port: 9600` for units in static or IP-table mode),
  connects the socket to the unit and needs no handshake: the first read shows whether the CPU answers; DA1 defaults to
  the last octet of the host (KEPServerEX), SA1 to the last octet of the local address. TCP connects and runs the node
  address handshake (node 0 = allocate, or `src_node`); DA1/SA1 are then the nodes the unit reports; FINS/TCP error
  notifications and handshake errors are named (W421 7-4).
- **No BCD dtypes in the config.** KEPServerEX BCD/LBCD become `uint16`/`uint32` with the `D` suffix (Short/Long with
  `D` = signed BCD, MSB as the sign, PTC's `D0D @ Short = -50`). Timer and counter present values decode as BCD when
  `tc_bcd` (default true, the CS/CJ default; the parser refuses `D` on T/C). The parser still accepts the `bcd16`/
  `bcd32` dtype names the vectors use. **Importer:** "Omron FINS Ethernet" devices now map to `omron_fins` (UDP, the
  model family from the KEPServerEX model text, node/request-size properties by key suffix). No FINS export is
  available here, so the property keys are **not verified** and every device is flagged; BCD/LBCD tags are still
  imported disabled, as for every driver (a follow-up can rewrite them to the `D` suffix).
- **Identity** is CPU UNIT DATA READ with data `00`: model and version (plus transport, nodes, model family); a refused
  0501 gives an empty identity and reads go on.
- **Tests.** `tests/integration/test_rust_engine_fins.py` (UDP and TCP): every address form against `sim.value()`, the
  0104 pool, `check -v`, identity, mute, one area refused (only its tags `bad_config`, also inside the 0104), wrong CPU
  mode, replies with another SID, a wrong destination node, split TCP replies, a CPU without 0104; the engine-level case
  (acceptance, Rust only, skipped on the Python engine) power-cycles the simulator and checks demotion and resume.
- **Still to check on hardware:** KEPServerEX's word order for 32-bit values (`first_word_low`), the reply port in
  static/table mode, and whether a real CPU answers a multi-read item error like the simulator.

### D-095 System tags cost what changed, not O(devices²) (#238)
- **Counters:** `opencap.metrics` keeps plain per-device totals beside the Prometheus counters.
  Code counts through `count_request`, `count_failed_read`, `count_overrun`, `count_published`
  and `count_saf_dropped`, never `.inc()` on those metrics (a test scans the source). The system
  tags read the totals. `REGISTRY.get_sample_value` collected the whole registry on every call,
  seven calls per device per step, so a step was O(devices²). That was 77 % of the CPU at 10k
  tags.
- **Not the workers' own counters:** the values stay process-lifetime, like the counters, so
  they survive a reload that builds new workers and stay equal to `/metrics` (a test compares
  them).
- **Steps:** a device event recomputes that device and its channel. It uses fresh entries for
  the notified devices and the cached entries of their siblings. Node and agent tags of the
  `event` class follow at the next tick, within spec §2.1's 1 s bound (spec note under §2.1).
  The tick and config steps still compute every tag. Once every path is registered, a tick
  compares first and splits and writes only what changed. Enabled-tag counts are taken once per
  config object.
- **Values unchanged:** `evaluate()` and the vectors in `tests/conformance/system_tags/` are
  untouched. Tests check that event and tick steps write exactly what `evaluate` gives for the
  whole state.
- **Result** (4-vCPU container, `bench/README.md`): 10k tags at 33–34 % of one core (99 %
  before), 25k keeps up at 89 %, 50k reaches 19.7k tag updates/s against 21.6k without system
  tags.
- **Rust (#56):** it has no system tags yet. When it gets them it should do the same: atomic
  per-device counters, and per-device event updates rather than a node-wide recomputation.

## Sparkplug B (#51)

### D-090 Sparkplug B codec: proto + prost, not srad; what the specification leaves open
Spec: `docs/spec/payload-sparkplug.md`. Code: `engine/north/sparkplug`. Vectors: `tests/conformance/sparkplug/`.
- **srad not used.** `oss-reuse.md` asked #51 to evaluate the `srad` crates first. They are MIT OR Apache-2.0 but
  pre-1.0, with breaking releases from 0.4 to 0.7 in eight months and about 5k downloads in total. `srad-types` also
  ships prost code generated from the EPL-2.0 `sparkplug_b.proto`, comments included, under MIT/Apache without an
  EPL notice. srad is also a framework that owns the MQTT session, which would bypass the outbox (ADR-0011) and the
  NorthPlugin contract. Instead the proto is vendored unmodified (Tahu v1.0.19, `proto/third_party/`, listed in
  `NOTICE`) and compiled at build time by prost-build and protox. protox is a pure-Rust compiler, so no system
  `protoc` is needed and no generated code is committed.
- **Negative Int8/Int16/Int32:** stored as value mod 2^width in `int_value`. The spec gives `uint32` and no rule.
  Tahu Java sign-extends to 32 bits, while Tahu Python adds 2^width. The Java decoder casts (`(short) getIntValue()`),
  so it reads either form; the Rust decoder accepts both.
- **UInt32 in `int_value`,** the spec's field type. Tahu Java writes `long_value`, and its decoder and ours accept both.
- **Scaled tags are Double**, because the value is always a float. Arrays use the spec's packed little-endian bytes.
  BooleanArray puts the first value in the MSB (the spec's example) with zero padding bits.
- **Quality (ADR-0006):** a `Quality` Int32 property (192, 0x18, 0x04, 0x00, 0x44). `bad_config` and `bad_decode`
  are `is_null` even with a last value; `bad_comm` and `stale` keep it. #30's shared module will replace
  `quality.rs`.
- **Names and aliases.** A tag's dots become `/` in the metric name, because dots are the group path, as in
  KEPServerEX. Aliases are unique per edge node and handed out from 1 in birth order: `bdSeq`,
  `Node Control/Rebirth`, then each device's tags in config order. `bdSeq` carries an alias in NBIRTH, because every
  birth metric needs one (tck-id-payloads-alias-birth-requirement); it has none in NDEATH.
- **Properties `engUnit`/`engLow`/`engHigh`**: these are the names Ignition's MQTT Engine maps; *verify* at L2.
- **Commands:** only `Node Control/Rebirth` = true in an NCMD is executed, by name or by its alias. Everything else
  in an NCMD, and every DCMD metric, is rejected with a reason (ADR-0008).
- **STATE:** only the 3.0 JSON form. Plain-text `ONLINE`/`OFFLINE` from Sparkplug 2.x hosts is not supported.
- **Vectors from an independent reference.** No Python engine implements Sparkplug, so the vectors come from a
  second implementation of the spec page: `tests/helpers/sparkplug_ref.py`, on Google's protobuf runtime (Python
  `protobuf`, BSD-3-Clause, `dev` extra, test-only). It loads the schema from `sparkplug_b.desc`, which protox
  compiles without source info so line endings and comments don't matter, and which a Rust test keeps identical to
  the build. prost and Google's runtime produce identical bytes for every case. Two mutations confirm the replay
  test catches regressions: Tahu Java's sign extension for Int16, and LSB-first BooleanArray bits.
- **Licence gate:** `scripts/check_licenses.py` learned the `3-Clause BSD License` spelling of protobuf's metadata.
- **Quality from the shared table.** `quality.rs` follows `tests/conformance/quality/map.json`, and `tests/vectors.rs`
  checks it. A stale tag that was never read has Quality 0x00, not 0x44. The Python reference calls
  `opencap.core.quality.to_sparkplug`.
- **The edge node (spec §8).**
  - It has its own MQTT connection, because Sparkplug needs its own Will (NDEATH with a new bdSeq on every CONNECT)
    and its own subscriptions. The broker settings come from `mqtt`, through the new shared
    `opencap_north::mqtt::client_options`.
  - A STATE `online: false` ends the session (NDEATH, disconnect, a new CONNECT after 2 s), as the spec requires for
    a primary host that lost this server.
  - Data held while the session has no births goes to the node's own outbox as historical DDATA. It is replayed
    after the next births, with new seq numbers. That includes data read before the host was first online, because
    it is history the host has not seen.
  - The outbox rows are deleted once handed to the client, because DDATA is QoS 0 by the spec.
  - The runtime restarts the node when the `sparkplug` or `mqtt` settings or the device set change, so the births
    always list every metric.
- **Known limits.**
  - `StoreAndForward::put` sets the global `saf_queue` gauge, so it now also counts the Sparkplug outbox. Per-agent
    labels belong to #142.
  - The TCK has no headless runner; the tck-ids the integration test covers are listed in spec §8.5.


### D-093 Readiness, about and support information in the Rust engine (#194)
The Rust half of D-075: `/health/ready`, `/health/live`, `GET /api/v1/about` (open), `GET /api/v1/support` (viewer),
`initial_scan_complete` / `initial_scan_ms` in `/health`, and `opencap-engine info [--json]`. Same rules as D-075 (ready =
north side started and every enabled, polled device has finished one poll cycle or is demoted; sticky; latched from the
worker's `on_cycle` hook, as in Python). Where the Rust answers differ on purpose:
- **`about`** is identical to Python's except `engine: "rust"` (dual-engine test); `config_schema_version` is
  `opencap_core::config::SCHEMA_VERSION`, kept equal to Python's `SCHEMA_VERSION` by that test.
- **`support`/`info`**: `toolchain` is `{"rust": <rustc version at build time>}`; `drivers` lists the compiled-in drivers
  with `library: null` ("built in"; there is no separate protocol library to version); `north_interfaces` lists what
  this engine runs (`mqtt`, `api`), so `opcua_server` is absent even when enabled in the config until the Rust OPC UA
  server exists (#46); `os` uses Python's `platform.system()` names (Linux, Windows, Darwin) and `arch` Rust's.
- **`config_revision`** is computed exactly as Python's `ConfigStore.revision()` (`opencap_core::config::config_revision`,
  SHA-256 via `ring`, already in the tree); a unit test pins it to the Python value for the same directory.

### D-096 Serial follow-ups from #62 (#68)
- **Non-blocking serial writes.** `SerialTransport` hands the tty what it takes now. The rest is queued and
  written by the event loop's writer callback when the port becomes writable; it never sleeps on the loop. If the
  queue does not empty within `write_stall_s` (1 s), the line is lost ("output buffer stays full"), as before. The
  non-POSIX path (pyserial without a file descriptor) is unchanged.
- **Late DF1 ACK/NAK.** At the start of each transaction, the link decodes whatever the line delivered since the
  last one and drops stale DLE ACK/NAK symbols (`link_stats.stale_dropped`): they belong to an older frame.
  Late frames and ENQs are **not** dropped. DF1 requires the receiver to ACK every good frame, or the PLC
  retransmits it, so the loop still ACKs a late reply and counts it as unmatched or duplicate. The issue asked to
  clear everything; dropping only the symbols fixes the bug without leaving frames unacknowledged.
- **DF1 ACK timeout.** `ack_timeout_ms` now defaults to **auto**: `timeout_ms / (enq_retries + 1)`, at most
  1000 ms (with `timeout_ms: 1000`, 250 ms). The old fixed 1000 ms used the whole attempt, so a lost frame
  failed with "ENQs 0". An explicit value that leaves no room for the ENQ retries is a load-time warning. It is
  not clamped, so a slow line can keep a long ACK wait on purpose.
- **One setting per line.** Enabled devices that share a port or gateway (same `Endpoint.key`) must agree on the
  driver, `baudrate`, `bytesize`, `parity`, `stopbits` and, for `ab_df1`, `error_check`. A mismatch is a load-time
  config error (`_serial.check_shared_lines`). Before, the second device's connect failed at run time (DF1) or
  silently used the first device's settings (RTU).
- **Multi-drop RTU.** With `close_on_timeout` unset, a timeout on a line that several units share no longer
  closes the line for every unit. pymodbus 3.16 clears its receive buffer before each request, skips a reply
  whose unit id or function code differs from the request's, and waits out a cancelled request's late reply before
  it reuses that unit and function code. A unit alone on its line, or an explicit `close_on_timeout: true`,
  closes as before (D-036).
- **Container.** The default image includes the `serial` extra (pyserial, BSD-3-Clause, already an allowed
  dependency). `docs/deployment.md` shows `--device` and `--group-add` for local ports.
- **Vectors.** Four format-1 areas, `ab_df1/frame`, `receive`, `request` and `values`
  (`scripts/gen_conformance_df1.py`), take the directed cases of `tests/unit/test_df1_frame.py` plus seeded
  mixes. Two mutants (DLE not stuffed, BCC as a plain sum) prove the frame vectors catch encoding bugs. The Rust
  port (#53) consumes them.
- **Rust:** the Rust Modbus RTU driver (#149, D-070) should get the same shared-line check and the
  multi-drop timeout rule. That belongs in its loader and driver, not in this PR.

### D-100 System tags in Rust, part 1: the pure evaluator and the health fields (#56)
- `opencap_core::system_tags` is the Rust *state -> system tags* function of `docs/spec/system-tags.md`,
  split by level (`node_tags`, `north_tags`, `channel_tags`, `device_tags`) so the runtime task can
  recompute one device and its channel on a device event and leave the rest to the once-a-second tick
  (D-095). The state is a typed struct (serde) in the shape of `tests/conformance/system_tags/README.md`;
  `TICK_TAGS` is the `tick` class and a test compares it with the catalogue.
- `DeviceHealth` gains `error_since` (set by the first failed cycle of a run, cleared by a good one);
  `record_failure` now takes the UTC time. `DeviceStatus` gains `consecutive_failures`, `error_since`
  and `last_connect_failed` (set by each real connect attempt; a connect skipped for backoff does not
  count). `_Error` is `consecutive_failures > 0` (spec section 9).
- All `snapshot` and `device_sequence` vectors pass through `crates/opencap-conformance/tests/system_tags.rs`.
  The tag store task, the counters, the payload flag and the API follow in part 2.

### D-102 Rust simulator driver (#124)
- `engine/drivers/simulator` (`opencap-driver-simulator`, feature `driver-simulator`) follows `docs/driver_params.md`
  ("simulator") and D-071. It runs every vector of `tests/conformance/simulator/vectors.json` (84 valid, 41 invalid;
  invalid addresses must fail with the Python message's reason). The vectors hold doubles to the last digit, so the
  crate's tests parse them with serde_json's `float_roundtrip` (a dev-dependency only; the engine's parsing is unchanged).
- Integer literals stay exact (`i128`) so RAMP uses integer arithmetic exactly when low, high and step are integer
  literals (D-066). `COUNTER` counts good reads per tag; a failed read (`fail_every_n_reads`, latency at or above the
  timeout) does not count. The clock is injectable (`SimulatorDriver::with_clock`); `latency_ms` sleeps with tokio's
  timer, so tests run it on a paused clock.
- The issue's last criterion (`tests/integration/test_simulator_poller.py` switched to the Rust binary) is not done
  here: ADR-0018 forbids touching Python tests. The Rust behaviour is covered by `tests/driver.rs` and the vectors.

### D-120 DF1 serial is native Rust on opencap-serial; libplctag has no DF1 (#53)
Status: accepted (ADR-0018 OSS-first rule, step d). PCCC address parsing, planning and decoding are shared
Rust code, checked by the existing vectors.

- **Evaluated:** libplctag 2.7.2 (C, MPL-2.0 OR LGPL-2+), by reading its source. Its Allen-Bradley protocols
  (`src/libplctag/protocols/ab`: `eip_cip`, `eip_lgx_pccc`, `eip_plc5_pccc`, `eip_slc_pccc` and the DH+ variants) are
  EtherNet/IP only. `grep -ri df1` over the vendored tree finds one comment in the `ab_server` simulator and no
  DF1 link layer (no DLE framing, no BCC/CRC, no ACK/NAK/ENQ). Serial port code exists only in its platform layer,
  used by its Modbus RTU support. No other open-source DF1 library was evaluated: none is known to us with a
  full-duplex link layer and a licence that passes ADR-0007.
- **Decision:** write DF1 natively. It is small (framing, receiver, one transaction loop, about 700 lines) and
  sits on `opencap-serial` (tcp serial gateway and local port, shared lines).
- **Shape:** `drivers/ab_pccc` is the shared PCCC library (address parser, planner, decoder, STS tables), ported
  from `ab_pccc.py` and run against `tests/conformance/ab_pccc` (plan 110 vectors, decode 50). The EtherNet/IP
  PCCC driver (libplctag, task 1b) will reuse it. `drivers/ab_df1` is the DF1 driver, run against the four
  `ab_df1` vector areas (frame, receive, request, values), plus an in-process fake SLC on a TCP socket for the
  driver (every file type, NAK, lost ACK, bad reply check, stale and duplicate replies, silence,
  `close_on_timeout`, a 300-round soak).
- **Read-only (ADR-0008):** `Df1Link::transact` refuses every command except `(0x0F,0xA2)`, `(0x0F,0xA1)` and
  `(0x06,0x03)` before it reaches the wire; a test sends the write functions 0xAA, 0xAB, 0x67 and expects a
  refusal with zero frames sent.
- **Not in this port:** `rfc2217` transport and `local_echo` (Python supports `rfc2217` for DF1; the Rust serial
  crate has no RFC 2217 client), half-duplex, and the load-time shared-line `error_check` check (the driver
  refuses at connect instead, as Python does).

### D-097 Rust is the only engine (ADR-0018)
- The owner's order of 2026-11-27: Rust only, no new Python, OSS drivers first (C/C++ through FFI allowed),
  delete Python when v0.1 is done. Plan: `docs/plan/v0.1-rust.md`.
- ADR-0008 stays: FFI wrappers expose reads only, with a CI check that no write function is bound.

### D-105 Device time zone and the last of #112 in the Rust engine (DEV-13)
- Of #112, the naming rules (PRJ-13, with the v2 errors from D-104), the reserved device names and the
  `payload.timezone` check (LOG-21, shared `zones.json`) were already in the Rust models. What was missing is DEV-13:
  `params.device_timezone` is validated when the device is loaded (an IANA name or a fixed offset `+05:30`, `-0800`,
  `+02`, at most 14:00; the error names the key) and `opencap_core::device_time::DeviceZone::local_to_utc` turns a device's
  local time into the UTC `source_ts`. No driver calls it yet (ADR-0006: timestamps stay UTC).
- **DST rule.** A local time in the gap of a spring-forward does not exist: it is read with the offset in force before the
  jump (02:30 in New York on 2026-03-08 is 07:30Z). A time in the overlap of a fall-back happens twice: the first
  occurrence (the DST one) is taken and the helper reports `Fold::Ambiguous`. 30-minute jumps (Lord Howe) follow the same rule.
- The 30 vectors are in `tests/conformance/timezones/device_time/convert.json` (derived by hand, not from the code) and run
  by `opencap-conformance`. Python does not implement `device_timezone` (frozen), so there is no shared invalid fixture:
  the Python models would accept the key as any `params` value. The API 422 rule, the percent-encoding round trip and
  `import-kep` reporting are Python code and are not ported.

### D-123 OPC UA server: async-opcua, read-only, KEPServerEX-style address space (#50)
Status: accepted (ADR-0018 order: Rust crate first). Crate `north/opcua` (`opencap-north-opcua`), plugin kind `opcua`.

- **Rust crates searched** (crates.io): `async-opcua` 0.19.0 (MPL-2.0; first published January 2025, 362k downloads, 133k
  in the last 90 days, last release July 2026; the successor of `opcua`/locka99) and the older `opcua` 0.12. async-opcua
  has the server, the client, all seven security policies the config names, user/password, X.509 user tokens, a
  certificate store with trusted/rejected folders and subscriptions. It is usable, so the C library `open62541` was not
  evaluated (ADR-0018 uses C only when no usable Rust crate exists). Dependencies: no licence outside the ADR-0007 allow-list.
- **Benchmark gate** (`north/opcua/tests/bench.rs`, `#[ignore]`d, release build, Linux): 250 devices x 1000 tags =
  250k variables in groups: the server is listening 0.9 s after start; RSS +457 MB, **1.87 KB per variable** (the gate in
  `04-scale-reliability.md` §4.1 is 0.6-2 KB, 150-500 MB: inside it, at the upper end); a change of all 250k tags in the
  store takes 27 ms to write and the address space follows within one `update_ms` tick with +21 MB. Not measured: the CPU of
  250k monitored items sampled at 1 s (needs a subscribing client at that scale), so the "~1 core" line stays open.
- **Address space** as in Python: `Objects/<channel>/<device>/<group>/.../<tag>`, `ns=2;s=<channel>.<device>.<group>.<tag>`
  (the tag namespace is registered first after the server's own, so it is ns=2). A tag name's dots are its group path; a
  name that is both a tag and a group is skipped with a warning. DataType follows the tag dtype (Double when scaled),
  StatusCode follows ADR-0006 (`values::status_code`), SourceTimestamp is the store's `source_ts`. Values are copied from
  the change bus, batched per `update_ms` (default 250), and on `Lagged` resynchronised from snapshots.
- **Read-only (ADR-0008), tested with the async-opcua client:** every variable has `AccessLevel`/`UserAccessLevel` =
  CurrentRead; `Write` returns a bad status and the value is unchanged; `AddNodes`, `DeleteNodes`, `Call` and
  `HistoryUpdate` are refused (and the node survives). No write callback is registered.
- **Security:** policies from `security_policies`, one endpoint each; user tokens from `username_env/password_env/users`
  (variable names, values from the environment; a missing variable fails the start); anonymous only when `anonymous`.
  **Fail closed:** `trust_client_certs(false)`, so a client certificate is accepted only from `trusted/`; a refused one is
  saved in `rejected/` (the operator moves the file to trust it). Tested: untrusted certificate refused, `rejected/`
  filled, trusted certificate with a wrong password refused, with the right password accepted. `rejected/` is capped at
  100 files (oldest removed every 30 s, `pki::prune_rejected`) so an anonymous client cannot fill the disk (#248).
- **Runtime:** `opcua_server.enabled` starts the plugin from `Runtime::start` and restarts it on a reload that changes the
  config or the devices (a restart, not a live address-space edit: clients reconnect).
- **Not in this PR (follow-ups):** X.509 user tokens (`x509_users`, `<pki_dir>/users/`), the `_System` folder per device,
  array tags and `EURange`/`EngineeringUnits` on scaled tags, `advertised_endpoint` host rewriting, the
  `allow_insecure_remote` check, session and monitored-item counters as metrics, `BrowseNext` limit
  (`max_references_per_browse`), and the OPC UA **client** driver (#46).
