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
Device "2 PUMP" (Micro800 driver in KEPServer) has 132 PCCC-style addresses (`F28:0`,
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
- The trailing-space id string `"192.0.2.10 "` (Site 130) is recovered.
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
`opencap/site_001/8_pump/data`). `devices_map.yaml` is never rewritten. New devices are
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
  last-will topic is `opencap/poller/<slug(instance)>/status`.
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
- **Real-project finding:** on Site 092/10 PUMP, `LT_1_CURRENTDAY_HOUR` is `F54:17`
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
