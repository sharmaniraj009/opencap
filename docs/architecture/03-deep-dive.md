# 3. Deep dive

Rust signatures below are the **target contract**. RFC 0001 (`rust-engine` PR) may rename
things; the lead reconciles the two when that PR lands and updates this file.

## 3.1 Data model

### Identity and naming
| Concept | Internal (Python today) | Internal (Rust) | External (north, UI, API) |
|---|---|---|---|
| Channel | `Channel.name` | `ChannelId(u32)` | `Channel` |
| Device | `Device.id = "<channel>/<device>"` | `DeviceId(u32)` | `Channel.Device` |
| Tag | `Tag.name` (group path dot-joined, e.g. `LTI002.Pv`) | `TagId(u32)` dense index into the store | `Channel.Device.Group.Tag` |
| OPC UA node | — | — | `ns=<idx>;s=Channel.Device.Group.Tag` (KEPServerEX uses the same string form, so SCADA tag references survive the migration; ADR-0009) |
| MQTT topic | `opencap/<slug(channel)>/<slug(device)>/data` (D-012; `opencap` is `mqtt.topic_prefix`, D-028) | same | same |

Internal integer ids are assigned at config load and are stable only within one config
revision. Nothing persistent or external may store them; external interfaces always use
the dotted path.

### Tag definition (config)
`Tag(name, address, dtype, scan_ms, scale, enabled, description)` today
(`src/opencap/config/models.py`). Planned additions, each additive to the schema:
`array: {dims}` (1-D exists implicitly through the driver address syntax), `units`,
`eu_range`, `access` (only `read_only` accepted, ADR-0008), `alarms` (references),
`group` (explicit groups instead of dot-joined names, with a migration).

### Tag value (runtime)
```
TagValue {
  value:      Variant          // Bool | I8..I64 | U8..U64 | F32 | F64 | String | DateTime | Bytes | Array(Variant[])
  quality:    Quality          // good | bad_comm | bad_config | bad_decode | stale  (ADR-0006)
  source_ts:  Option<UtcTime>  // when the reply arrived, or the device's own timestamp when the protocol has one
  server_ts:  UtcTime          // when the store accepted it (Rust adds this; Python derives it)
}
```
Rules carried over from the Python cache (contract §2 of `docs/phase1_contracts.md`): a good
value replaces value, quality and `source_ts`; any other quality keeps the last value and the
last good `source_ts` and changes only the quality; never-read tags are `(None, stale, None)`.

## 3.2 Tag store and change-event bus
([ADR-0005](../adr/0005-north-plugins-tag-store-event-bus.md))

- **Layout.** One `DeviceSlot` per device: a contiguous `Vec<TagValue>` indexed by the
  device-local tag index, a monotonic `version: u64`, `last_good`. Slots live in a
  `Vec<DeviceSlot>` indexed by `DeviceId`, created at config load.
- **Writers.** Exactly one writer per device: its channel worker (or, for virtual devices,
  the processing task that owns them). A write is one batch (one block's results) under a
  short per-device lock; no global lock exists.
- **Readers.** `get(tag)`, `snapshot(device)`; a snapshot is a copy taken under the device
  lock (hundreds of tags, microseconds). North plugins never hold a lock across an await.
- **Events.** After each batch the store publishes `Arc<ChangeBatch { device, version,
  changes: [(TagId, TagValue)] }>` on a bounded broadcast channel (capacity configurable,
  default 4,096 batches). A subscriber that falls behind receives `Lagged(n)` and must
  resynchronise from `snapshot()` of the devices it serves: conflation to the latest value,
  bounded memory, and the scan loop never blocks.
- **Lossless consumers** (MQTT agents in interval/on-change mode, Sparkplug, data logger)
  turn events into outbox rows as fast as they arrive; the outbox, not the bus, absorbs
  downstream outages. If such a consumer still lags (CPU starvation), it logs a
  `north.lagged` event, increments `north_lagged_total` and resynchronises; that is the
  only path on which intermediate changes can be skipped, and it is observable.
- **Python today:** `ValueCache` plus the `on_cycle(device_id)` callback that the publisher
  uses to notice changes. Same semantics at device granularity.

## 3.3 Driver contract

The Python ABC (`src/opencap/drivers/base.py`) is the reference. Rust mirrors it:

```rust
pub trait Driver: Send {
    /// Pure: parse addresses, merge tags into wire blocks. No I/O, no protocol library calls.
    fn plan(&self, tags: &[TagDef]) -> Plan;                      // Plan { blocks, invalid: map tag -> reason }
    fn connected(&self) -> bool;
    fn connect_timeout(&self) -> Option<Duration> { None }       // e.g. Logix tag-list upload
    async fn connect(&mut self) -> Result<(), DriverError>;      // ONE attempt
    async fn close(&mut self);                                   // idempotent, never fails
    async fn read(&mut self, block: &Block) -> Result<BlockResult, DriverError>; // ONE attempt
    async fn identity(&mut self) -> Result<Identity, DriverError> { Ok(Identity::default()) }
    async fn browse(&mut self) -> Result<Vec<TagDef>, DriverError> { Err(DriverError::Unsupported) }
}
pub enum DriverError { Comm(String), ConnectionLost(String), BlockConfig(String), Unsupported }
pub enum Reading { Value(Variant, Option<UtcTime>), Decode(String), BadConfig(String), BadComm(String) }
```

- There is **no write method** on the trait. A future `Writer` trait would live behind a
  Cargo feature and the approved write-safety RFC (ADR-0008).
- **Push drivers** (OPC UA client subscriptions, MQTT client, BACnet COV, DNP3 unsolicited
  responses) add `subscribe(blocks, sink)` that delivers batches into the same store path;
  the worker still owns connection state, retries and health. The Python OPC UA client
  (`opcua` PR) is the first one; its shape is reviewed against this paragraph.
- **Sync libraries** run on the channel's single-thread executor (Python, D-015) or a
  dedicated blocking thread per channel (Rust, `spawn_blocking` is not used for long-lived
  sockets).
- **Registration.** Rust drivers are crates compiled in behind Cargo features and registered
  in a static driver table, like `src/opencap/drivers/__init__.py`. No dynamic loading of
  Rust code (Rust has no stable ABI).
- **Out-of-process drivers** speak `opencap.plugin.v1` over gRPC (Unix socket, named pipe
  or loopback TCP): `Plan`, `Connect`, `Read`, `Close`, `Identity`, `Browse`, `Subscribe`
  (server stream), `Health`. The engine supervises the process (spawn, restart with
  backoff, kill on stop) and maps a dead plugin to `ConnectionLost` for its devices. Used
  for the Python driver host during migration and for libraries that must stay in a
  separate process (GPL, crash-prone C code).

## 3.4 Error and retry handling

| Condition | Raised as | Worker action | Tag quality |
|---|---|---|---|
| Address cannot be used (parse, type, size) | `Plan.invalid` (config time) | never sent | `bad_config` once |
| Device permanently rejects the block (Modbus 01/02/03, PCCC STS 0x10, CIP 0x04/0x05) | `BlockConfigError` | block disabled until reload; cycle *neutral* | `bad_config` |
| One attempt failed (timeout, transient device error) | `CommError` | retry up to `retries`; a closed socket is reopened once inside the same budget | `bad_comm` after the last retry |
| Connection gone (refused, reset, peer close) | `ConnectionLost` | close; reconnect with backoff 1 s → 60 s, reset on success | `bad_comm` |
| Value undecodable | `Reading::Decode` / `DecodeError` | none | `bad_decode` for that tag only |
| No good read within 3 × effective rate | stale tick | none | `stale` |
| `demote_after` consecutive failed cycles | — | device demoted for `demote_for_ms`, then one probe | unchanged |
| Driver call exceeds its safety net (`timeout × 1.25 + 0.1 s` × wire requests) | treated as `CommError` | connection closed | `bad_comm` |
| Rust driver task panics | caught at the channel task boundary | channel restarted, `driver.panic` event logged, metric | `bad_comm` until recovered |
| Out-of-process plugin dies | plugin host reports | restart with backoff; its devices are `ConnectionLost` | `bad_comm` |
| Broker / database / north peer down | north agent | outbox keeps rows, link retries with backoff | — |
| Outbox full (`max_bytes`) | outbox | drop oldest, `saf_dropped_total` | — |
| Invalid config on reload | config manager | reload rejected, old config keeps running (done) | — |
| Clock jump | — | schedules use the monotonic clock; timestamps use UTC wall time | — |

Known limit (D-015, same as KEPServerEX): a device that accepts TCP but never answers slows
the other devices on its channel until it is demoted. Sites with one device per channel are
not affected; the per-channel concurrency option (FR-3) is the mitigation for the others.

## 3.5 North plugin contract

```rust
#[async_trait]
pub trait NorthPlugin: Send {
    fn kind(&self) -> &'static str;                                // "mqtt", "sparkplug", "opcua", ...
    async fn run(self: Box<Self>, ctx: NorthContext) -> Result<(), NorthError>;   // until ctx.shutdown
}
pub struct NorthContext {
    pub store: TagStoreReader,               // get / snapshot / address-space browse
    pub events: ChangeReceiver,              // bounded broadcast; Lagged(n) => resync
    pub outbox: Option<Outbox>,              // present when the agent's config says durable
    pub status: DeviceStatusReader,          // health per device for status topics / system tags
    pub demand: DemandReader,                // rate-limited read_once, only if the device allows it
    pub shutdown: CancellationToken,
}
```

- **Caching and device reads.** Every north read is served from the tag store
  (KEPServerEX "initial updates from cache"). An OPC UA `Read` with `maxAge = 0`, or an OPC DA
  device read, becomes a demand poll only when the device sets `allow_demand_reads: true`, and
  at most once per device per `min_scan_ms`; otherwise it returns the cached value. This
  protects PLCs with few connections from SCADA clients.
- **Client-driven scan rate** (FR-3): servers report the fastest sampling interval their
  clients request per tag; the scheduler uses `max(requested, min_scan_ms)` when the tag has
  no fixed rate. Off until the OPC UA server supports it.
- **Address space** for servers is generated from the registry: folders for channels,
  devices and groups; variables for tags with `DataType`, `AccessLevel = CurrentRead`,
  `EURange`, `EngineeringUnits`, `Description`; `_System`/`_Statistics` virtual devices.

## 3.6 Configuration API (contract for the `web-ui` PR and both engines)

- Resources: `/api/v1/channels`, `/channels/{ch}/devices`, `/devices/{id}/tags`, `/north`,
  `/plugins`, `/project` (whole document), `/project/revisions`, `/project/import`
  (KEPServerEX JSON), `/project/validate` (= `opencap check`).
- Reads return the resource plus `revision`; writes require `If-Match: <revision>` and fail
  with `412` on a conflict (optimistic concurrency: two people editing never silently
  overwrite each other).
- Every write is validated against the same JSON Schema and the same semantic checks as the
  loader, persisted atomically, appended to the audit log (user, time, JSON diff), and then
  applied with the diff-based reload (only changed channels restart, done).
- Live data: `GET /api/v1/devices/{id}/values` (done, unversioned today) and a WebSocket
  `/api/v1/ws` subscription (device or tag list → change events), the Quick Client feed.
- Auth: bearer token today (D-019); users/roles per the security RFC. `/health` and
  `/metrics` stay open for monitoring.
- The OpenAPI document is generated, committed and drift-tested so both engines can be
  tested against it.

## 3.7 Security

| Surface | Mechanism |
|---|---|
| Admin/config API | Bearer tokens now; users with roles viewer / operator / engineer / admin (security RFC); TLS optional (reverse proxy or built-in) |
| OPC UA server | Security policies None (off by default when bound beyond loopback), Basic256Sha256, Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss; anonymous off by default; username/password and X.509 user tokens; trust list managed from the UI |
| MQTT/Sparkplug | TLS with CA file (done), username + password from env (done), client certificates (planned) |
| Modbus/DNP3/IEC 104 servers | Bind address allow-list; DNP3 Secure Authentication v5 and IEC 62351 only if a library supports them (not a blocker) |
| Secrets | Env vars or files named in config (done); never logged (structlog processors redact known secret keys) |
| Audit | Every config change, login, token use for writes to opencap state (alarm ack), and every future PLC write |
| Supply chain | Licence gate (`cargo-deny`, Python allow-list), pinned lockfiles, no telemetry |
