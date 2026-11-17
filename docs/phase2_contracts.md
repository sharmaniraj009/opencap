# Phase 2 contracts: Allen-Bradley Micro800 + Logix (`ab_logix`, 69 devices)

Two parts are built in parallel: the **`ab_logix` driver I/O** and a **CIP test simulator**.
The core (`ChannelWorker`), the north side and the runtime do not change: `ab_logix` plugs in
behind the `Driver` interface exactly like `modbus_tcp`. Phase 1 rules still apply: tz-aware
UTC timestamps, ephemeral ports in tests, metrics only from `opencap.metrics`, read-only.

## Facts established before the build
- pycomm3 1.2.16 `LogixDriver.read(*tags)` sends a CIP **Multiple Service Packet** (0x0A)
  for Logix, but **one request per tag for Micro800** (`logix_driver.py`:
  `if len(parsed_tags) != 1 and not self._micro800`). The real 66 Micro800 devices would cost
  ~6,700 requests per full pass. pycomm3 already has `MultiServiceRequestPacket` and
  `_read_build_multi_requests`; only that condition stops Micro800 batching.
- pycomm3 detects a Micro800 from the identity product name (prefix `"2080"`), and needs the
  controller's **tag list** (Get Instance Attribute List, service 0x55 on the Symbol class
  0x6B, plus templates 0x6C for UDTs) before it can read: with `init_tags=False` every read
  fails with "Tag doesn't exist".
- `cpppo` (the usual Python EtherNet/IP simulator) does not implement 0x55, so it cannot
  serve pycomm3. libplctag has no Python 3.14 wheel. Hence our own simulator.
- pycomm3 accepts `"host:port/slot"` paths.

## 1. Driver I/O (`src/opencap/drivers/ab_logix.py`)
`AbLogixDriver` (`is_async = False`): every pycomm3 call runs through `self._run_sync()` on
the channel's single-thread executor.
```python
@property connected -> bool
async def connect(self) -> None        # open LogixDriver, upload the tag list, probe batching; ConnectionLost on failure
async def close(self) -> None          # idempotent, never raises
async def read(self, block: LogixBlock) -> dict[str, TagValue]
async def identity(self) -> dict       # vendor, product_name, product_code, revision, serial, micro800, batching
```
- **Path:** `host[:port]` for Micro800; `host[:port]/<slot>` when `params.slot` is set (from
  `<ip>,1,0`). Socket timeout = `device.timeout_ms`. Apply `params.connection_size` when set.
- **Tag existence (PLAN §5.1):** after connecting, compare every planned read name with the
  uploaded tag list (base name before `.`/`[`; members of UDTs via pycomm3's definitions).
  Missing ones get `TagValue(None, "bad_config", ts)` on every read **without** going on the
  wire. Log them once at WARNING. Keep the uploaded tag list across reconnects of the same
  driver instance when pycomm3 allows it (avoid re-uploading on every reconnect).
- **Micro800 batching:** a `LogixDriver` subclass that lets Micro800 use the Multiple Service
  Packet path. On connect, **probe** with a 2-name batch read of tags that exist: success →
  batching on; CIP general status 0x08 (service not supported) or any malformed reply →
  batching off for this connection, one request per tag (pycomm3's normal Micro800 path).
  Log the outcome once at INFO and report it in `identity()["batching"]`. A
  `params.batching` override (`"auto"` default, `"on"`, `"off"`) skips or forces the probe.
- **Errors:** connection refused/reset/closed → `ConnectionLost`; reply timeout → close, then
  `CommError`; per-tag CIP error in an otherwise good reply → `bad_config` for "doesn't
  exist"/path errors, `bad_comm` for others; the whole reply failing → `CommError`.
  Values go through the existing pure `decode_block()` (dtype coercion, `.n` bits).
- **Read-only:** never call `plc.write()` or `generic_message` with a write service.

## 2. CIP simulator (`tests/helpers/cip_sim.py`)
An asyncio EtherNet/IP server faithful enough for pycomm3 1.2.16, on an ephemeral port:
```python
class CipSim:
    @classmethod
    async def start(cls, tags: dict[str, tuple[str, Any]], *, kind: Literal["logix", "micro800"] = "logix",
                    batching: bool = True, large_forward_open: bool = True, slot: int = 0) -> "CipSim"
        # tags: name -> (CIP type "BOOL|SINT|INT|DINT|REAL|LREAL|STRING" or "<TYPE>[n]", value)
        # optional UDTs: name -> ("UDT:<name>", {member: (type, value)})  (one level is enough)
    port: int
    request_count: int            # CIP requests served (a Multiple Service Packet counts once)
    service_counts: Counter       # by CIP service code
    def set(self, name: str, value) -> None
    def value(self, name: str) -> Any
    mute: bool                    # stop answering (timeout)
    async def stop(self) -> None; async def restart(self) -> None   # same port
def make_logix_tags(n: int, *, seed: int = 0) -> dict[str, tuple[str, Any]]
```
Must support what pycomm3 sends: RegisterSession/UnRegisterSession, ListIdentity, identity
Get Attributes All, Forward Open (normal and large; refuse large when
`large_forward_open=False`), Forward Close, SendRRData/SendUnitData, Read Tag (0x4C), Read
Tag Fragmented (0x52) for big arrays, Multiple Service Packet (0x0A; reply status 0x08 when
`batching=False`), Get Instance Attribute List (0x55) for the tag list, and the template
services pycomm3 uses for UDTs. Identity product name: `"1756-L61/B LOGIX5561"` (logix) or
`"2080-LC50-48QWB"` (micro800). Read-only: answer write services with "service not supported".
