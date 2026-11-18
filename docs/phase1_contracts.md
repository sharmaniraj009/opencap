# Phase 1 contracts

Three parts are built in parallel against these interfaces: the **Modbus driver I/O**, the
**core** (cache, health, scheduler, channel worker) and the **north side** (payload,
store-and-forward, MQTT link, publisher). The lead wires them together in
`core/runtime.py`, `api/server.py` and the CLI. Do not change a signature here without
saying so in your report.

General rules: all timestamps are tz-aware **UTC** internally (IST only in the payload).
Every async component stops cleanly when its task is cancelled. Tests bind only to
**ephemeral ports** (port 0 or a free-port helper), because several test runs may happen at
once on the same machine. Metrics come only from `opencap.metrics`.

## Shared types (already in the tree)
- `opencap.core.cache.TagValue(value, quality, source_ts: datetime | None)`
- `opencap.drivers.base`: `Block`, `Plan`, `Driver`, `DecodeError`, and the exceptions:
  - `CommError`: one attempt failed (timeout, transient device error). The worker retries.
  - `ConnectionLost(CommError)`: the connection is gone. The worker closes, then reconnects with backoff.
  - `BlockConfigError`: the device permanently rejects the block. Tags → `bad_config`, block skipped until reload.
- `opencap.core.registry`: `Registry`, `DevicePlan`, `RateGroupPlan`, `plan_device()`
- `opencap.metrics`: `POLL_REQUESTS{device,result}` (result ∈ ok, timeout, conn_error,
  bad_config, error), `POLL_CYCLE{device}`, `POLL_OVERRUN{device}`, `DEVICE_STATE{device}`
  (codes in `DEVICE_STATE_CODES`), `MQTT_PUBLISHED`, `SAF_QUEUE`, `SAF_DROPPED`.

## 1. Driver I/O (`drivers/modbus_tcp.py`)
```python
class ModbusTcpDriver(Driver):
    async def connect(self) -> None            # one attempt; raise ConnectionLost on failure
    async def close(self) -> None              # idempotent, never raises
    async def read(self, block: ModbusBlock) -> dict[str, TagValue]
    async def identity(self) -> dict           # FC43/14 if the device answers, else {}
```
`read()` makes **one** attempt, bounded by `device.timeout_ms`. Timeout → close the socket
when `close_on_timeout`, then raise `CommError`. Socket gone → `ConnectionLost`. Modbus
exception 01/02/03 → `BlockConfigError`; other exception codes → `CommError`. On success,
return a `TagValue` for **every** tag name in `block.tags`: `good` with the raw decoded value
(no scaling), or `bad_decode` for a `DecodeError`. `source_ts` = when the reply arrived.
Retries, backoff and scaling are the worker's job, not the driver's.

## 2. Core (`core/`)
```python
class ValueCache:
    def register(self, device: Device) -> None             # pre-fill every enabled tag: TagValue(None, "stale", None)
    def update(self, device_id: str, values: dict[str, TagValue]) -> None
        # good  -> store value, quality, ts
        # other -> keep the last value, set the new quality, keep last good ts
    def snapshot(self, device_id: str) -> dict[str, TagValue]   # copy, every registered tag
    def version(self, device_id: str) -> int                    # bumps on every update()
    def last_good(self, device_id: str) -> datetime | None      # newest good source_ts on the device
    def expire(self, device_id: str, now: datetime, max_age_s: dict[str, float], *,
               read_at: dict[str, datetime] | None = None) -> None
        # good tags whose last good read is older than max_age_s[name] -> "stale"
        # age from source_ts, or from read_at[name] for `Driver.stale_by_read_time` drivers (D-045)

class DeviceHealth:                       # pure state machine, time passed in
    state: Literal["ok", "offline", "demoted"]
    errors: int                           # failed cycles since start
    last_good: datetime | None
    def status(self) -> dict              # {"state", "last_good" (ISO-8601 UTC or null), "errors"}

class ChannelWorker:
    def __init__(self, channel: Channel, plans: dict[str, DevicePlan], cache: ValueCache, *,
                 on_cycle: Callable[[str], None] | None = None,       # called after each device cycle
                 driver_factory: Callable[[Device, Executor], Driver] | None = None,  # tests inject fakes
                 clock: Callable[[], datetime] | None = None)
    async def run(self) -> None           # until cancelled
    async def stop(self) -> None          # cancel, close drivers, shut down the executor
    async def read_once(self, device_id: str) -> dict[str, TagValue]  # one full cycle now, serialized with run()
    def health(self, device_id: str) -> DeviceHealth
    def device_status(self, device_id: str) -> dict   # health.status() + cycle_ms, last_error, connected
```
Rules (PLAN §3.1, §6): one worker per channel; inside a channel **one request on the wire at a
time**; a single-thread executor per channel for sync drivers; rate groups; ±5% start
jitter; overrun → start the next cycle at once and count `POLL_OVERRUN`; retries; reconnect
backoff 1 s → 60 s; demotion after `demote_after` failed cycles for `demote_for_ms`, then one
probe read; scaling applied to good numeric values (`Tag.scale`); a scaling error →
`bad_decode`; stale after 3 × the tag's effective rate with no good read. Plan
`invalid` tags → `bad_config` once.

## 3. North (`north/`)
```python
# payload.py
def build_payload(device: Device, values: dict[str, TagValue], cfg: PayloadCfg, *, is_secure: bool) -> dict
def encode(payload: dict) -> bytes
def status_topic(device: Device) -> str   # "opencap/<slug(channel)>/<slug(device)>/status"

# saf.py
class StoreAndForward:                     # SQLite WAL outbox
    def __init__(self, path: str | Path, max_bytes: int)
    def put(self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False) -> int
    def peek(self, limit: int = 100) -> list[OutMsg]   # oldest first
    def ack(self, ids: Iterable[int]) -> None
    def count(self) -> int
    def size_bytes(self) -> int
    def close(self) -> None

# mqtt.py
class MqttLink:
    def __init__(self, cfg: MqttCfg, instance: str, saf: StoreAndForward)
    async def run(self) -> None            # connect/reconnect loop, drain the outbox in order
    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None  # enqueue, never waits on the network
    connected: bool
    is_secure: bool                        # == cfg.tls
    async def stop(self) -> None

# publisher.py
class Publisher:
    def __init__(self, devices: list[Device], cache: ValueCache, link: MqttLink, cfg: PayloadCfg,
                 status_fn: Callable[[str], dict])
    async def run(self) -> None            # interval timers, heartbeats, status topics
    def notify_cycle(self, device_id: str) -> None   # hooked to ChannelWorker.on_cycle (on_change mode)
```
LWT: `opencap/poller/<instance>/status` = `offline` (retained); `online` (retained) after each
connect. Status topic per device is retained:
`{"state":"ok|demoted|offline","last_good":"...","errors":N}`.
