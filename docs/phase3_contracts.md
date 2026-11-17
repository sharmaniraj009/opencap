# Phase 3 contracts: Allen-Bradley PCCC (`ab_pccc`, 30 devices, 10,320 tags)

KEPServer "Allen-Bradley ControlLogix Ethernet" model 23 devices: SLC 500 / MicroLogix data
tables (`F19:0`, `N54:17`, `B3:17/14`) reached directly over EtherNet/IP (id string `<ip>`,
no route). Two parts are built in parallel: the **`ab_pccc` driver I/O** and a **PCCC
extension of the CIP simulator**. The core, north side and runtime do not change. Phase 1/2
rules still apply (UTC timestamps, free ports in tests, metrics from `opencap.metrics`,
read-only).

## Already done (Phase 0, keep passing)
`parse_pccc_address`, `check_dtype`, `coerce_value`, `PcccBlock` (one pycomm3 read string per
block, e.g. `"F34:0{22}"`, ≤ 255 data bytes), `plan()`, `decode_block(block, values)`
(bits decoded locally from whole words, N/B masked to uint16, int → float for `N` tags
typed float32). Tests cover all 11 address patterns in the real project
(`tests/unit/test_pccc_*.py`, `tests/integration/test_ab_real_project.py`). pycomm3 facts in
`docs/decisions.md` D-008: `SLCDriver.read(*addrs)` sends one PCCC request per address; its
`B3/n` parsing is buggy (we never use it); T/C sub-elements cannot be block-read; RTC
function files are unsupported (those 4 tags are `bad_config` at plan time).

## 1. Driver I/O (`src/opencap/drivers/ab_pccc.py`)
`AbPcccDriver` (`is_async = False`, every pycomm3 call through `self._run_sync()`):
```python
@property connected -> bool
def connect_timeout_s(self) -> float | None       # connect = Forward Open + identity (+ file directory)
async def connect(self) -> None                   # SLCDriver open; ConnectionLost on failure
async def close(self) -> None                     # idempotent, never raises
async def read(self, block: PcccBlock) -> dict[str, TagValue]   # plc.read(block.address) → decode_block
async def identity(self) -> dict                  # vendor, product_name, revision, serial, processor type
```
- **Path:** `host[:port]`, plus `params.path` when set. Socket timeout = `device.timeout_ms`.
- **Errors:** connect failure / reset / closed → `ConnectionLost`; reply timeout → drop the
  socket, `CommError`; a PCCC status that means the file or element does not exist (or the
  address is illegal for this processor) → `BlockConfigError` (the block's tags become
  `bad_config` and the core stops reading it); other PCCC/CIP errors → `CommError`; a reply
  whose values cannot be decoded → `bad_decode` per tag via `decode_block`.
- **File check (like the Logix tag-existence check):** if pycomm3 can read the processor's
  file directory, do it once on first connect (cache it in the instance) and raise
  `BlockConfigError` *without sending* for blocks whose file is missing or too short.
  If the directory is not readable, skip the check and rely on the PLC's error replies.
- **Read-only:** never call `plc.write()` or send a PCCC write command.

## 2. PCCC simulator (`tests/helpers/pccc_sim.py`)
Reuses the CIP simulator's EtherNet/IP and CIP layers (`tests/helpers/cip_sim.py`) and adds
the PCCC object (class 0x67, Execute PCCC service 0x4B) as pycomm3 1.2.16's `SLCDriver` uses it:
protected typed logical read with three address fields (CMD 0x0F / FNC 0xA2), the
diagnostic status pycomm3 uses for `get_processor_type()`, and whatever `get_file_directory()`
needs if feasible. Answer PCCC write commands with an error and count them.
```python
class PcccSim:
    @classmethod
    async def start(cls, tables: dict[str, list], *, product_name: str = "1766-L32BXB B/14.00",
                    host="127.0.0.1", port=0) -> "PcccSim"
        # tables: "N7" -> [int...], "F8" -> [float...], "B3" -> [int words], "L9" -> [int...],
        #         "T4"/"C5" -> [(ctl_word, pre, acc), ...], "ST9" -> [str...], "I"/"O" -> [words]
    port: int; request_count: int; pccc_counts: Counter; write_attempts: int; mute: bool
    def set(self, address: str, value) -> None    # "N7:3", "F8:0", "B3:1/4", "T4:0.ACC"
    def value(self, address: str) -> Any
    async def stop(self) -> None; async def restart(self) -> None
def tables_for(tags: list[Tag], *, seed: int = 0) -> dict[str, list]
    # data tables that cover every PCCC address in `tags` (deterministic values), so a sim can
    # stand in for any real device's tag list
```
A read of a file that does not exist, or past its end, gets the PCCC error status a real
SLC/MicroLogix returns, so the driver's `BlockConfigError` mapping can be tested.
