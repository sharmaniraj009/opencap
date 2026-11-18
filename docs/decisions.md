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
  in that folder (TRUSTED_VALIDATION). `anonymous` on/off and one user name/password from
  the environment variables named by `username_env`/`password_env`. Anonymous off with no user
  set → the server refuses to start. An endpoint offering None on a non-loopback address logs a
  WARNING.
- **Runtime:** started after the workers and the publisher, stopped first. Reload: changed
  `opcua_server` settings restart the server (a failure is logged and polling continues);
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
