"""Reusable Modbus TCP simulator for tests (one pymodbus server per simulated device).

    devices = make_sim_devices(30, 53)          # synthetic devices, ~1,600 tags, no site data
    sims = await start_sims(devices)            # one server per device on 127.0.0.1, ephemeral ports
    driver = ModbusTcpDriver(sims[0].device)    # sim.device = copy of the device with the sim's port
    sims[0].expected()                          # {tag: raw value the driver must decode now}
    sims[0].tick()                              # change every value deterministically
    await sims[0].stop(); await sims[0].restart()   # kill / bring back on the same port
    await stop_sims(sims)

RTU: `ModbusSim.start(device, framer="rtu")` serves Modbus RTU frames (unit id + PDU + CRC) on
the TCP socket, as a serial-over-TCP gateway in transparent mode would (put `PtyBridge` in front
for a real tty). An RTU sim stays silent for a unit id it does not serve, like a serial bus.
`add_unit(device)` adds another unit (multi-drop line) with its own register image.

Fault injection on a running sim: `mute` (accept requests, never answer), `muted_units` (a set of unit
ids that stay silent, like a dead slave on a multi-drop line, while the others answer), `exception_code`
(answer every read with that Modbus exception), `short_reply` (drop N registers/bits from
every reply), `identity = None` (reject FC43/14 with exception 01).

The register image is written by an encoder that is deliberately independent of the
driver's parser and decoder (own address regex, struct-based packing), so a round trip
through the real driver proves `zero_based`, `zero_based_bit`, `modicon_bit_order`,
`byte_order_big`, `first_word_low` and `first_dword_low` handling. `expected()` reads the
image back with the simulator's own decoder, so overlapping tags stay consistent.
Setting values is test-fixture setup straight into the datastore, not a Modbus write:
the simulated device itself rejects every write request with exception 01.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import socket
import struct
import zlib
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pymodbus.constants import ExcCodes
from pymodbus.exceptions import NoSuchIdException
from pymodbus.framer import FramerType
from pymodbus.pdu import ExceptionResponse
from pymodbus.pdu.mei_message import ReadDeviceInformationResponse
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from opencap.config.models import Device, PublishCfg, Tag

SIM_HOST = "127.0.0.1"
IMAGE_MARGIN = 16        # readable registers/bits after the last tag; beyond that: exception 02
DEFAULT_IDENTITY = {"vendor": "opencap", "product_code": "modbus-sim", "revision": "1.0"}

# Same params as the real project's Modbus devices (zero-based, first word low), but small
# timings so integration tests run fast.
SIM_PARAMS: dict[str, Any] = {
    "unit_id": 0,
    "zero_based": True,
    "zero_based_bit": True,
    "modicon_bit_order": False,
    "byte_order_big": True,
    "first_word_low": True,
    "first_dword_low": True,
    "max_holding": 32,
    "max_input": 32,
    "max_coils": 64,
    "max_discrete": 64,
    "close_on_timeout": True,
}


def free_port() -> int:
    """An ephemeral TCP port that nothing listens on right now (for 'connection refused' tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((SIM_HOST, 0))
        return s.getsockname()[1]


# --- synthetic devices -----------------------------------------------------------------------

# (area, dtype, name prefix). Cycled in a per-device shuffled order, so every device gets a mix.
_KINDS: tuple[tuple[str, str, str], ...] = (
    ("holding", "float32", "HR_F32"), ("holding", "float32", "HR_F32"), ("holding", "float32", "HR_F32"),
    ("holding", "uint16", "HR_U16"), ("holding", "int16", "HR_I16"),
    ("holding", "int32", "HR_I32"), ("holding", "uint32", "HR_U32"),
    ("input", "float32", "IR_F32"), ("input", "float32", "IR_F32"),
    ("input", "uint16", "IR_U16"), ("input", "int16", "IR_I16"),
    ("input", "int32", "IR_I32"), ("input", "uint32", "IR_U32"),
    ("holding_bit", "bool", "HR_BIT"), ("holding_bit", "bool", "HR_BIT"), ("holding_bit", "bool", "HR_BIT"),
    ("coil", "bool", "COIL"), ("coil", "bool", "COIL"),
    ("discrete", "bool", "DI"), ("discrete", "bool", "DI"),
)
_AREA_DIGIT = {"coil": "0", "discrete": "1", "input": "3", "holding": "4"}
_WORDS = {"int16": 1, "uint16": 1, "int32": 2, "uint32": 2, "float32": 2,
          "int64": 4, "uint64": 4, "float64": 4}


def make_sim_devices(
    n: int = 30,
    tags_per_device: int = 53,
    *,
    host: str = SIM_HOST,
    seed: int = 0,
    params: dict[str, Any] | None = None,
    per_channel: int = 1,
    scan_ms: int = 200,
    timeout_ms: int = 500,
) -> list[Device]:
    """Synthetic modbus_tcp devices (no real site data), deterministic for a given seed.

    Each device has `tags_per_device` tags: float32/uint16/int16/int32/uint32 in holding and
    input registers, bools in holding registers (`.b`), coils and discrete inputs, plus one
    alias (two names on the same address, like the real project). Addresses are spread
    with small gaps (merged into one block) and large gaps (new block), so each device
    plans to several blocks. `params` overrides SIM_PARAMS (e.g. word order).
    `per_channel` devices share one channel. `port` is 0: `ModbusSim.start()` fills it in.
    """
    if n < 0 or tags_per_device < 1 or per_channel < 1:
        raise ValueError("n >= 0, tags_per_device >= 1 and per_channel >= 1 required")
    merged = {**SIM_PARAMS, **(params or {})}
    devices = []
    for k in range(n):
        rng = random.Random(f"opencap-sim/{seed}/{k}")
        channel = f"SIM_SITE_{k // per_channel + 1:02d}"
        name = f"SIM_PLC_{k + 1:02d}"
        devices.append(Device(
            id=f"{channel}/{name}",
            channel=channel,
            name=name,
            driver="modbus_tcp",
            host=host,
            port=0,
            params=dict(merged),
            scan_ms=scan_ms,
            min_scan_ms=scan_ms,
            timeout_ms=timeout_ms,
            retries=1,
            demote_after=3,
            demote_for_ms=2_000,
            publish=PublishCfg(sub_device_id=f"SIM_{k + 1:02d}", interval_ms=1_000),
            tags=_make_tags(rng, tags_per_device),
            source={"simulated": True},
        ))
    return devices


def _make_tags(rng: random.Random, count: int) -> list[Tag]:
    order = list(range(len(_KINDS)))
    rng.shuffle(order)
    cursor = {"holding": rng.randint(0, 40), "input": rng.randint(0, 40),
              "coil": rng.randint(0, 100), "discrete": rng.randint(0, 100)}
    bit_reg: int | None = None        # holding register currently used for .b tags
    free_bits: list[int] = []
    numbered: dict[str, int] = {}
    tags: list[Tag] = []

    def reg_gap() -> int:             # max_gap is 4 registers: 0..3 merges, 12..40 splits
        # (10% large gaps: 30 devices x 53 tags plan to ~276 blocks; the real project: 268)
        r = rng.random()
        return 0 if r < 0.5 else rng.randint(1, 3) if r < 0.9 else rng.randint(12, 40)

    def bit_gap() -> int:             # FC01/02 gap limit is 64 bits by default
        return rng.randint(0, 3) if rng.random() < 0.9 else rng.randint(80, 200)

    def take(area: str, size: int, gap: int) -> int:
        start = cursor[area] + gap
        cursor[area] = start + size
        return start

    n_regular = count - 1 if count >= 2 else count
    for i in range(n_regular):
        area, dtype, prefix = _KINDS[order[i % len(order)]]
        numbered[prefix] = numbered.get(prefix, 0) + 1
        name = f"{prefix}_{numbered[prefix]:03d}"
        if area == "holding_bit":
            if bit_reg is None or not free_bits or rng.random() < 0.3:
                bit_reg = take("holding", 1, reg_gap())
                free_bits = sorted(rng.sample(range(16), 8))
            bit = free_bits.pop(0)
            address = f"4{bit_reg + 1:05d}.{bit}"
        elif area in ("coil", "discrete"):
            address = f"{_AREA_DIGIT[area]}{take(area, 1, bit_gap()) + 1:05d}"
        else:
            address = f"{_AREA_DIGIT[area]}{take(area, _WORDS[dtype], reg_gap()) + 1:05d}"
        tags.append(Tag(name=name, address=address, dtype=dtype))
    if count >= 2:   # one alias: a second name on an existing numeric holding address
        numeric = [t for t in tags if t.address.startswith("4") and "." not in t.address]
        base = rng.choice(numeric or tags)
        tags.append(Tag(name=f"{base.name}_ALIAS", address=base.address, dtype=base.dtype))
    return tags


# --- independent address parser and encoder --------------------------------------------------

_SIM_ADDR_RE = re.compile(
    r"(?P<area>[0134])(?P<ref>\d{4,5})(?:\.(?P<sfx>\d{1,3})(?P<ord>[HLhl])?)?(?P<dims>(?:\[\d+\]){0,2})")
_AREAS = {"0": "coil", "1": "discrete", "3": "input", "4": "holding"}
_STRUCT = {"int16": "h", "uint16": "H", "int32": "i", "uint32": "I", "float32": "f",
           "int64": "q", "uint64": "Q", "float64": "d"}
_FLOAT32_MIN_NORMAL = 2.0 ** -126
_FLOAT64_MIN_NORMAL = 2.0 ** -1022


@dataclass(frozen=True)
class SimSlot:
    """Where one tag lives in the simulated device."""

    area: str                     # coil | discrete | input | holding
    start: int                    # protocol address of the first register/bit
    size: int                     # registers or bits
    dtype: str
    bit: int | None = None        # bit position in the logical register, 0 = LSB
    dims: tuple[int, ...] = ()
    str_len: int = 0
    str_order: str = ""

    @property
    def is_bits(self) -> bool:
        return self.area in ("coil", "discrete")

    @property
    def elements(self) -> int:
        return math.prod(self.dims) if self.dims else 1

    @property
    def key(self) -> tuple:
        return (self.area, self.start, self.size, self.dtype, self.bit, self.dims, self.str_len, self.str_order)


def sim_slot(address: str, dtype: str, params: dict[str, Any]) -> SimSlot | None:
    """Locate a Kepware Modbus address. None if the simulator cannot place it."""
    m = _SIM_ADDR_RE.fullmatch(address.replace(" ", ""))
    if not m:
        return None
    area = _AREAS[m["area"]]
    ref = int(m["ref"])
    start = ref - 1 if params.get("zero_based", True) else ref
    dims = tuple(int(d) for d in re.findall(r"\d+", m["dims"]))
    if start < 0:
        return None
    if area in ("coil", "discrete"):
        if dtype != "bool" or m["sfx"] is not None:
            return None
        return SimSlot(area, start, math.prod(dims) if dims else 1, "bool", dims=dims)
    if m["ord"]:                                           # string .lllH / .lllL
        if dtype != "string":
            return None
        length = int(m["sfx"])
        return SimSlot(area, start, (length + 1) // 2, "string", str_len=length, str_order=m["ord"].upper())
    if m["sfx"] is not None:                               # bit in register
        if dtype != "bool":
            return None
        written = int(m["sfx"])
        index = written if params.get("zero_based_bit", True) else written - 1
        if not 0 <= index <= 15:
            return None
        position = 15 - index if params.get("modicon_bit_order", False) else index
        return SimSlot(area, start, 1, "bool", bit=position)
    if dtype not in _WORDS:
        return None
    elements = math.prod(dims) if dims else 1
    return SimSlot(area, start, elements * _WORDS[dtype], dtype, dims=dims)


def _swap(word: int) -> int:
    return ((word & 0xFF) << 8) | (word >> 8)


def encode_words(dtype: str, value: int | float, params: dict[str, Any]) -> list[int]:
    """Wire registers for one numeric value (big-endian bytes, then Kepware word/dword order)."""
    try:
        raw = struct.pack(">" + _STRUCT[dtype], value)
    except (struct.error, OverflowError) as exc:
        raise ValueError(f"{value!r} does not fit {dtype}: {exc}") from None
    words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]   # most significant first
    if len(words) > 1:
        dwords = [words[i:i + 2] for i in range(0, len(words), 2)]                  # high dword first
        if params.get("first_word_low", True):
            dwords = [list(reversed(d)) for d in dwords]
        if len(dwords) == 2 and params.get("first_dword_low", True):
            dwords.reverse()
        words = [w for d in dwords for w in d]
    if not params.get("byte_order_big", True):
        words = [_swap(w) for w in words]
    return words


def decode_words(dtype: str, words: list[int], params: dict[str, Any]) -> int | float:
    """Inverse of `encode_words` (the simulator's own decoder, used by `expected()`)."""
    if not params.get("byte_order_big", True):
        words = [_swap(w) for w in words]
    if len(words) > 1:
        dwords = [words[i:i + 2] for i in range(0, len(words), 2)]
        if len(dwords) == 2 and params.get("first_dword_low", True):
            dwords.reverse()
        if params.get("first_word_low", True):
            dwords = [list(reversed(d)) for d in dwords]
        words = [w for d in dwords for w in d]
    raw = b"".join(w.to_bytes(2, "big") for w in words)
    value = struct.unpack(">" + _STRUCT[dtype], raw)[0]
    if dtype in ("float32", "float64") and params.get("nonnormal_float", "zero") == "zero":
        tiny = _FLOAT32_MIN_NORMAL if dtype == "float32" else _FLOAT64_MIN_NORMAL
        if math.isnan(value) or math.isinf(value) or (value != 0.0 and abs(value) < tiny):
            return 0.0
    return value


def encode_string(text: str, slot: SimSlot) -> list[int]:
    data = text.encode("latin-1")
    if len(data) > slot.str_len:
        raise ValueError(f"string {text!r} is longer than {slot.str_len} bytes")
    data = data.ljust(slot.size * 2, b"\x00")
    pairs = [(data[i], data[i + 1]) for i in range(0, len(data), 2)]
    if slot.str_order == "H":
        return [(a << 8) | b for a, b in pairs]
    return [(b << 8) | a for a, b in pairs]


def decode_string(words: list[int], slot: SimSlot) -> str:
    data = bytearray()
    for w in words:
        hi, lo = w >> 8, w & 0xFF
        data += bytes((hi, lo)) if slot.str_order == "H" else bytes((lo, hi))
    return bytes(data[:slot.str_len]).split(b"\x00", 1)[0].decode("latin-1")


def _flatten(value: Any, dims: tuple[int, ...]) -> list[Any]:
    if not dims:
        return [value]
    if len(dims) == 1:
        if not isinstance(value, (list, tuple)) or len(value) != dims[0]:
            raise ValueError(f"expected a list of {dims[0]} values")
        return list(value)
    if not isinstance(value, (list, tuple)) or len(value) != dims[0]:
        raise ValueError(f"expected {dims[0]} rows")
    flat = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != dims[1]:
            raise ValueError(f"expected rows of {dims[1]} values")
        flat.extend(row)
    return flat


def _shape(values: list[Any], dims: tuple[int, ...]) -> Any:
    if not dims:
        return values[0]
    if len(dims) == 1:
        return values
    return [values[r * dims[1]:(r + 1) * dims[1]] for r in range(dims[0])]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ValueError(f"{value!r} is not a bool")


# --- register image -----------------------------------------------------------------------------

class SimImage:
    """The four Modbus areas of one simulated device, plus each tag's slot. No sockets."""

    def __init__(self, device: Device) -> None:
        self.params = dict(device.params)
        self.slots: dict[str, SimSlot] = {}
        self.enabled: set[str] = set()
        self.unplaced: list[str] = []          # tags the simulator could not place
        for tag in device.tags:
            slot = sim_slot(tag.address, tag.dtype, self.params)
            if slot is None:
                self.unplaced.append(tag.name)
                continue
            self.slots[tag.name] = slot
            if tag.enabled:
                self.enabled.add(tag.name)
        ends = {area: 0 for area in ("coil", "discrete", "input", "holding")}
        for slot in self.slots.values():
            ends[slot.area] = max(ends[slot.area], slot.start + slot.size)
        self.words: dict[str, list[int]] = {
            a: [0] * min(ends[a] + IMAGE_MARGIN, 0x10000) for a in ("input", "holding")}
        self.bits: dict[str, list[bool]] = {
            a: [False] * min(ends[a] + IMAGE_MARGIN, 0x10000) for a in ("coil", "discrete")}
        self._device_id = device.id
        self._ticks = 0
        self._fill()

    # values

    def set_value(self, tag_name: str, value: Any) -> None:
        slot = self.slots.get(tag_name)
        if slot is None:
            raise KeyError(f"tag {tag_name!r} is not placed in the simulator")
        if slot.is_bits:
            bits = [_as_bool(v) for v in _flatten(value, slot.dims)]
            self.bits[slot.area][slot.start:slot.start + slot.size] = bits
            return
        area = self.words[slot.area]
        if slot.bit is not None:
            on = _as_bool(value)
            logical = self._logical(area[slot.start])
            logical = logical | (1 << slot.bit) if on else logical & ~(1 << slot.bit)
            area[slot.start] = self._wire(logical)
            return
        if slot.dtype == "string":
            if not isinstance(value, str):
                raise ValueError(f"{value!r} is not a str")
            words = encode_string(value, slot)
        else:
            words = []
            for v in _flatten(value, slot.dims):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(f"{v!r} is not a number")
                if slot.dtype not in ("float32", "float64") and not float(v).is_integer():
                    raise ValueError(f"{v!r} is not an integer")
                words += encode_words(slot.dtype, v if slot.dtype.startswith("float") else int(v), self.params)
        area[slot.start:slot.start + slot.size] = words

    def value(self, tag_name: str) -> Any:
        """Decode a tag from the image with the simulator's own decoder."""
        slot = self.slots[tag_name]
        if slot.is_bits:
            return _shape(list(self.bits[slot.area][slot.start:slot.start + slot.size]), slot.dims)
        words = self.words[slot.area][slot.start:slot.start + slot.size]
        if slot.bit is not None:
            return bool((self._logical(words[0]) >> slot.bit) & 1)
        if slot.dtype == "string":
            return decode_string(words, slot)
        n = _WORDS[slot.dtype]
        return _shape([decode_words(slot.dtype, words[i:i + n], self.params)
                       for i in range(0, len(words), n)], slot.dims)

    def expected(self) -> dict[str, Any]:
        return {name: self.value(name) for name in sorted(self.enabled)}

    def tick(self) -> None:
        self._ticks += 1
        self._fill()

    def _fill(self) -> None:
        """Write a deterministic value (function of device id, tag and tick) into every slot."""
        seen: set[tuple] = set()
        for name in sorted(self.slots):
            slot = self.slots[name]
            if slot.key in seen:            # aliases share one value
                continue
            seen.add(slot.key)
            h = zlib.crc32(f"{self._device_id}|{name}".encode())
            values = [_tick_value(slot, h + 7919 * e, self._ticks) for e in range(slot.elements)]
            self.set_value(name, values[0] if not slot.dims else _shape(values, slot.dims))

    # wire

    def _logical(self, wire: int) -> int:
        return wire if self.params.get("byte_order_big", True) else _swap(wire)

    def _wire(self, logical: int) -> int:
        return self._logical(logical)

    def read(self, area: str, start: int, count: int) -> list[int] | list[bool] | None:
        """Values a read request gets, or None for an illegal data address."""
        store: list[Any] = self.bits[area] if area in self.bits else self.words[area]
        if start < 0 or count < 1 or start + count > len(store):
            return None
        return list(store[start:start + count])


def _tick_value(slot: SimSlot, h: int, tick: int) -> Any:
    """A value for `slot` that differs from the previous tick's value."""
    dtype = slot.dtype
    if dtype == "bool":
        return (h + tick) % 2 == 1
    if dtype == "string":
        width = min(slot.str_len, 6)
        return str((h + tick) % (10 ** width)).zfill(width)
    if dtype in ("float32", "float64"):
        return ((h + 5 * tick) % 16000 - 8000) / 4.0          # exact in float32, changes every tick
    bits = 16 if dtype.endswith("16") else 32 if dtype.endswith("32") else 64
    u = (h * 2654435761 + tick * 40503) % (1 << bits)
    return u - (1 << (bits - 1)) if dtype.startswith("int") else u


# --- server -------------------------------------------------------------------------------------

_FC_AREA = {1: "coil", 2: "discrete", 3: "holding", 4: "input"}
_IDENTITY_IDS = {"vendor": 0x00, "product_code": 0x01, "revision": 0x02, "vendor_url": 0x03,
                 "product_name": 0x04, "model_name": 0x05, "user_application_name": 0x06}


class _Store:
    """pymodbus datastore facade: answers reads from the SimImage, rejects every write."""

    def __init__(self, sim: ModbusSim) -> None:
        self._sim = sim

    def device_ids(self) -> list[int]:
        return list(self._sim.images)

    async def async_getValues(self, device_id: int, func_code: int, address: int, count: int = 1):
        return self._sim._serve(device_id, func_code, address, count)

    async def async_setValues(self, device_id: int, func_code: int, address: int, values) -> ExcCodes:
        self._sim.write_attempts += 1
        return ExcCodes.ILLEGAL_FUNCTION


class ModbusSim:
    """One simulated Modbus TCP device on 127.0.0.1. Create with `await ModbusSim.start(device)`."""

    def __init__(self, device: Device, host: str = SIM_HOST, framer: str = "socket") -> None:
        if framer not in ("socket", "rtu"):
            raise ValueError("framer must be socket or rtu")
        self.framer = framer
        self.image = SimImage(device)
        self.host = host
        self.port = 0
        self.device = device
        self.unit_id = int(device.params.get("unit_id", 0) or 0)
        self.images: dict[int, SimImage] = {self.unit_id: self.image}
        self.identity: dict[str, str] | None = dict(DEFAULT_IDENTITY)
        self.mute = False                       # accept requests but never answer
        self.muted_units: set[int] = set()      # RTU: these unit ids stay silent, the others answer
        self.exception_code: int | None = None  # answer every read with this Modbus exception
        self.short_reply = 0                    # drop this many registers/bits from each reply
        self.request_count = 0                  # read requests served (any outcome)
        self.write_attempts = 0                 # write requests received (always rejected)
        self._server: ModbusTcpServer | None = None

    @classmethod
    async def start(cls, device: Device, *, port: int = 0, host: str = SIM_HOST, framer: str = "socket") -> ModbusSim:
        """Serve the device's register image. port=0 picks an ephemeral port. framer="rtu" serves
        RTU frames on the socket (RTU over TCP)."""
        sim = cls(device, host, framer)
        await sim._listen(port)
        return sim

    def add_unit(self, device: Device) -> SimImage:
        """Serve another unit id (from `device.params.unit_id`) on the same line."""
        unit = int(device.params.get("unit_id", 0) or 0)
        if unit in self.images:
            raise ValueError(f"unit {unit} is already served")
        self.images[unit] = SimImage(device)
        return self.images[unit]

    @property
    def running(self) -> bool:
        return self._server is not None

    # values

    def set_value(self, tag_name: str, value: Any) -> None:
        self.image.set_value(tag_name, value)

    def expected(self) -> dict[str, Any]:
        """Raw value each enabled, placed tag should decode to now (no scaling)."""
        return self.image.expected()

    def tick(self) -> None:
        self.image.tick()

    # lifecycle

    async def stop(self) -> None:
        """Close the listener and every client connection (like a PLC power-off)."""
        server, self._server = self._server, None
        if server is None:
            return
        listener = server.transport
        await server.shutdown()
        if listener is not None:
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await listener.wait_closed()

    async def restart(self) -> None:
        """Stop if running, then listen again on the same port with the same image."""
        await self.stop()
        await self._listen(self.port)

    async def _listen(self, port: int) -> None:
        dummy = SimDevice(self.unit_id, simdata=[SimData(0, count=1, values=0, datatype=DataType.REGISTERS)])
        rtu = self.framer == "rtu"
        server = ModbusTcpServer(
            dummy, address=(self.host, port), framer=FramerType.RTU if rtu else FramerType.SOCKET,
            ignore_missing_devices=rtu,
            trace_packet=self._trace_packet, trace_pdu=self._trace_pdu,
        )
        server.context = _Store(self)          # replace pymodbus' datastore with the SimImage facade
        await server.serve_forever(background=True)
        if not server.transport:
            raise RuntimeError(f"modbus sim could not listen on {self.host}:{port}")
        self.port = server.transport.sockets[0].getsockname()[1]
        self.device = self.device.model_copy(update={"host": self.host, "port": self.port})
        self._server = server

    # pymodbus hooks

    def _serve(self, device_id: int, func_code: int, address: int, count: int):
        self.request_count += 1
        image = self.images.get(device_id)
        if image is None or device_id in self.muted_units:
            raise NoSuchIdException(f"unit {device_id}")     # pymodbus answers 0B (TCP) / stays silent (RTU)
        if self.exception_code is not None:
            return ExcCodes(self.exception_code)
        area = _FC_AREA.get(func_code)
        if area is None:
            return ExcCodes.ILLEGAL_FUNCTION
        values = image.read(area, address, count)
        if values is None:
            return ExcCodes.ILLEGAL_ADDRESS
        if self.short_reply:
            values = values[:max(len(values) - self.short_reply, 1 if area in ("coil", "discrete") else 0)]
        return values

    def _trace_packet(self, sending: bool, data: bytes) -> bytes:
        return b"" if sending and self.mute else data

    def _trace_pdu(self, sending: bool, pdu: Any) -> Any:
        if sending and isinstance(pdu, ReadDeviceInformationResponse):
            if self.identity is None:
                return ExceptionResponse(pdu.function_code, ExcCodes.ILLEGAL_FUNCTION,
                                         device_id=pdu.dev_id, transaction=pdu.transaction_id)
            info = {_IDENTITY_IDS[k]: v.encode("latin-1") for k, v in self.identity.items() if k in _IDENTITY_IDS}
            return ReadDeviceInformationResponse(read_code=pdu.read_code, information=info,
                                                 dev_id=pdu.dev_id, transaction_id=pdu.transaction_id)
        return pdu


async def start_sims(devices: list[Device]) -> list[ModbusSim]:
    """Start one simulator per device (ephemeral ports), in order."""
    return list(await asyncio.gather(*(ModbusSim.start(d) for d in devices)))


async def stop_sims(sims: list[ModbusSim]) -> None:
    await asyncio.gather(*(s.stop() for s in sims), return_exceptions=True)
