"""Modbus TCP driver (PLAN.md §5.1), following Kepware "Modbus TCP/IP Ethernet" semantics.

The pure part (address parser, block planner, register decoder) needs no socket
and no pymodbus. The I/O part (`ModbusTcpDriver.connect/read/close/identity`,
docs/phase1_contracts.md §1) imports pymodbus lazily in `connect()`.
The server is read-only: only FC01/02/03/04 and FC43/14 are sent, there is no write path.

Addresses (Kepware form, 5 or 6 digits: area digit + 1-based reference)
    0xxxxx  coil              FC01  bool
    1xxxxx  discrete input    FC02  bool
    3xxxxx  input register    FC04  16/32/64-bit, bool with .b, string
    4xxxxx  holding register  FC03  16/32/64-bit, bool with .b, string
    4xxxxx.b        bit b of the register (bool; see zero_based_bit, modicon_bit_order)
    4xxxxx[n]       1-D array, 4xxxxx[r][c] 2-D array (not for .b or strings)
    4xxxxx.lllH     string of lll bytes (1..250), first char in the high byte
    4xxxxx.lllL     same, first char in the low byte

Params (`Device.params`, see docs/driver_params.md) are read through
`ModbusParams`, which applies the documented defaults.
"""

from __future__ import annotations

import asyncio
import math
import re
import struct
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers.base import (
    Block,
    BlockConfigError,
    CommError,
    ConnectionLost,
    DecodeError,
    Driver,
    Plan,
)

# Protocol limits per request (Modbus spec; same values as pymodbus MAX_COUNT).
PROTOCOL_MAX: dict[int, int] = {1: 2000, 2: 2000, 3: 125, 4: 125}
LAST_ADDRESS = 0xFFFF
BITS_PER_REGISTER = 16
MAX_STRING_LEN = 250

_AREAS: dict[str, tuple[str, int]] = {
    "0": ("coil", 1),
    "1": ("discrete", 2),
    "3": ("input", 4),
    "4": ("holding", 3),
}
_FC_LIMIT_PARAM: dict[int, str] = {1: "max_coils", 2: "max_discrete", 3: "max_holding", 4: "max_input"}
REGS_PER_DTYPE: dict[str, int] = {
    "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2, "float32": 2,
    "int64": 4, "uint64": 4, "float64": 4,
}
DTYPES = frozenset(REGS_PER_DTYPE) | {"bool", "string"}
NONNORMAL_FLOAT_MODES = ("zero", "unmodified")   # Kepware: "Replace with Zero" / "Unmodified"
_BOOL_PARAMS = frozenset({
    "zero_based", "zero_based_bit", "modicon_bit_order", "byte_order_big",
    "first_word_low", "first_dword_low", "close_on_timeout",
})


# --- params -----------------------------------------------------------------------

@dataclass(frozen=True)
class ModbusParams:
    """Typed view of `Device.params` for modbus_tcp. Unknown keys are ignored."""

    unit_id: int = 0
    zero_based: bool = True
    zero_based_bit: bool = True
    modicon_bit_order: bool = False
    byte_order_big: bool = True
    first_word_low: bool = True
    first_dword_low: bool = True
    max_coils: int = 64
    max_discrete: int = 64
    max_input: int = 64
    max_holding: int = 64
    max_gap: int = 4                    # registers
    max_gap_bits: int | None = None     # coils/discrete; None = max_gap * 16
    close_on_timeout: bool = True
    nonnormal_float: str = "zero"       # NaN/Inf/denormal floats: "zero" (Kepware default) or "unmodified"

    def __post_init__(self) -> None:
        for f in fields(self):
            name, value = f.name, getattr(self, f.name)
            if name in _BOOL_PARAMS:
                if not isinstance(value, bool):
                    raise ValueError(f"modbus_tcp param {name}={value!r}: expected true or false")
            elif name == "nonnormal_float":
                if value not in NONNORMAL_FLOAT_MODES:
                    raise ValueError(f"modbus_tcp param {name}={value!r}: expected one of {NONNORMAL_FLOAT_MODES}")
            elif not (name == "max_gap_bits" and value is None):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"modbus_tcp param {name}={value!r}: expected an integer")
        for name in _FC_LIMIT_PARAM.values():
            if getattr(self, name) < 1:
                raise ValueError(f"modbus_tcp param {name}={getattr(self, name)}: must be >= 1")
        if self.max_gap < 0:
            raise ValueError(f"modbus_tcp param max_gap={self.max_gap}: must be >= 0")
        if self.max_gap_bits is not None and self.max_gap_bits < 0:
            raise ValueError(f"modbus_tcp param max_gap_bits={self.max_gap_bits}: must be >= 0")
        if not 0 <= self.unit_id <= 255:
            raise ValueError(f"modbus_tcp param unit_id={self.unit_id}: must be 0..255")

    @classmethod
    def from_mapping(cls, params: ModbusParams | Mapping[str, Any] | None) -> ModbusParams:
        """Accept a `ModbusParams`, a `Device.params` dict, or None. A None value means "default"."""
        if isinstance(params, cls):
            return params
        if params is None:
            return cls()
        names = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in params.items() if k in names and v is not None}
        for name in _BOOL_PARAMS & kwargs.keys():       # hand-written YAML may say 0/1
            if type(kwargs[name]) is int and kwargs[name] in (0, 1):
                kwargs[name] = bool(kwargs[name])
        return cls(**kwargs)

    def max_span(self, fc: int) -> int:
        """Largest request for this function code: device setting, capped at the protocol limit."""
        return min(getattr(self, _FC_LIMIT_PARAM[fc]), PROTOCOL_MAX[fc])

    def max_gap_for(self, fc: int) -> int:
        """Unused registers (FC03/04) or bits (FC01/02) allowed between merged tags."""
        if fc in (1, 2):
            return self.max_gap_bits if self.max_gap_bits is not None else self.max_gap * BITS_PER_REGISTER
        return self.max_gap


# --- address parser -------------------------------------------------------------------

@dataclass(frozen=True)
class ModbusAddress:
    text: str                       # address as written (stripped)
    area: str                       # coil | discrete | input | holding
    fc: int                         # 1, 2, 4, 3
    ref: int                        # 1-based Modicon reference as written (digits after the area digit)
    address: int                    # protocol start address on the wire
    count: int                      # registers (FC03/04) or bits (FC01/02) on the wire
    dtype: str
    bit: int | None = None          # bit inside the register, 0 = LSB (after zero_based_bit / modicon)
    dims: tuple[int, ...] = ()      # array dimensions, () = scalar
    str_len: int | None = None      # string length in bytes
    str_order: str | None = None    # "H" (high byte first) or "L"

    @property
    def is_bits(self) -> bool:
        return self.fc in (1, 2)

    @property
    def end(self) -> int:
        """Exclusive end address."""
        return self.address + self.count

    @property
    def key(self) -> tuple:
        """Identity of the data read (ignores the spelling): equal keys are read and decoded once."""
        return (
            self.fc, self.address, self.count, self.dtype,
            -1 if self.bit is None else self.bit, self.dims,
            self.str_len or 0, self.str_order or "",
        )


_ADDR_RE = re.compile(
    r"(?P<digits>\d+)"
    r"(?:\.(?P<suffix>\d+)\s*(?P<order>[HL])?)?"
    r"(?P<dims>(?:\s*\[\s*\d+\s*\])*)",
    re.IGNORECASE,
)
_DIM_RE = re.compile(r"\[\s*(\d+)\s*\]")


def parse_address(address: str, dtype: str, params: ModbusParams | Mapping[str, Any] | None = None) -> ModbusAddress:
    """Parse a Kepware Modbus address for a tag of `dtype`. Raises ValueError with a clear reason."""
    p = ModbusParams.from_mapping(params)

    def bad(reason: str) -> ValueError:
        return ValueError(f"modbus address {address!r} ({dtype}): {reason}")

    if not isinstance(address, str):
        raise bad("address must be a string")
    text = address.strip()
    if not text:
        raise bad("empty address")
    m = _ADDR_RE.fullmatch(text)
    if not m:
        raise bad("not a Modbus address (expected 0xxxxx, 1xxxxx, 3xxxxx or 4xxxxx, "
                  "optionally with .bit, .lllH/.lllL or [n])")
    digits = m["digits"]
    if len(digits) not in (5, 6):
        raise bad(f"expected 5 or 6 digits (area digit + reference), got {len(digits)}")
    if digits[0] not in _AREAS:
        raise bad(f"unknown area prefix {digits[0]!r} "
                  "(0 = coil, 1 = discrete input, 3 = input register, 4 = holding register)")
    area, fc = _AREAS[digits[0]]
    if dtype not in DTYPES:
        raise bad(f"unknown dtype {dtype!r}")

    ref = int(digits[1:])
    max_ref = 9999 if len(digits) == 5 else LAST_ADDRESS + 1
    if ref > max_ref:
        raise bad(f"reference {ref} out of range 1..{max_ref}")
    if ref == 0 and p.zero_based:
        raise bad("reference 0 is not valid with zero_based addressing (references start at 1)")

    dims = tuple(int(x) for x in _DIM_RE.findall(m["dims"]))
    if len(dims) > 2:
        raise bad("at most 2 array dimensions ([rows][cols])")
    if any(d < 1 for d in dims):
        raise bad("array dimension must be >= 1")

    is_bits = fc in (1, 2)
    suffix, order = m["suffix"], m["order"]
    bit: int | None = None
    str_len: int | None = None
    str_order: str | None = None

    if order is not None:                                   # 4xxxxx.lllH / .lllL string
        if is_bits:
            raise bad("string addresses are only valid on registers (3xxxxx/4xxxxx)")
        if dtype != "string":
            raise bad("a .lllH/.lllL string address needs dtype string")
        if dims:
            raise bad("arrays of strings are not supported")
        str_len = int(suffix)
        if not 1 <= str_len <= MAX_STRING_LEN:
            raise bad(f"string length {str_len} out of range 1..{MAX_STRING_LEN}")
        str_order = order.upper()
        count = (str_len + 1) // 2
    elif suffix is not None:                                # 4xxxxx.b bit in register
        if dtype == "string":
            raise bad(f"string address needs an H or L byte-order suffix, e.g. {digits}.{suffix}H")
        if is_bits:
            raise bad("a .bit suffix is only valid on registers (3xxxxx/4xxxxx)")
        if dtype != "bool":
            raise bad("a .bit address must have dtype bool")
        if dims:
            raise bad("arrays are not supported on bit addresses")
        written = int(suffix)
        b0 = written if p.zero_based_bit else written - 1
        if not 0 <= b0 < BITS_PER_REGISTER:
            rng = "0..15 (zero_based_bit on)" if p.zero_based_bit else "1..16 (zero_based_bit off)"
            raise bad(f"bit .{written} out of range {rng}")
        bit = (BITS_PER_REGISTER - 1 - b0) if p.modicon_bit_order else b0
        count = 1
    else:
        if dtype == "string":
            raise bad(f"string needs a length and byte order, e.g. {digits}.020H")
        if is_bits and dtype != "bool":
            raise bad(f"{area} addresses are bool only")
        if not is_bits and dtype == "bool":
            raise bad(f"bool on a register needs a .bit suffix, e.g. {digits}.0")
        elements = math.prod(dims) if dims else 1
        count = elements * (1 if is_bits else REGS_PER_DTYPE[dtype])

    proto = ref - 1 if p.zero_based else ref
    if proto + count - 1 > LAST_ADDRESS:
        unit = "bits" if is_bits else "registers"
        raise bad(f"needs {count} {unit} from protocol address {proto}, past the last address {LAST_ADDRESS}")
    return ModbusAddress(
        text=text, area=area, fc=fc, ref=ref, address=proto, count=count, dtype=dtype,
        bit=bit, dims=dims, str_len=str_len, str_order=str_order,
    )


# --- blocks and planner -----------------------------------------------------------------

@dataclass(frozen=True)
class ModbusItem:
    """One distinct piece of data inside a block, served to every tag name that reads it."""

    names: tuple[str, ...]
    offset: int                     # first register/bit of the item, relative to the block start
    addr: ModbusAddress


@dataclass(kw_only=True)
class ModbusBlock(Block):
    fc: int
    start: int                      # protocol start address
    count: int                      # registers or bits
    items: list[ModbusItem] = field(default_factory=list)

    @property
    def is_bits(self) -> bool:
        return self.fc in (1, 2)

    def describe(self) -> str:
        unit = "bits" if self.is_bits else "regs"
        return f"FC{self.fc:02d} {self.start}..{self.start + self.count - 1} ({self.count} {unit})"


def plan_blocks(
    parsed: Iterable[tuple[str, ModbusAddress]], params: ModbusParams | Mapping[str, Any] | None = None,
) -> tuple[list[ModbusBlock], dict[str, str]]:
    """Merge parsed tags into wire blocks. Returns (blocks, invalid) — invalid = tags that fit no block.

    Greedy over items sorted by (fc, address): an item joins the open block when it has the
    same FC, starts at most `max_gap_for(fc)` after the block end, and the merged span stays
    within `max_span(fc)`. Output is sorted and independent of input order.
    """
    p = ModbusParams.from_mapping(params)
    invalid: dict[str, str] = {}
    names_by_key: dict[tuple, list[str]] = {}
    addr_by_key: dict[tuple, ModbusAddress] = {}
    for name, a in parsed:
        limit = p.max_span(a.fc)
        if a.count > limit:
            unit = "bits" if a.is_bits else "registers"
            param = _FC_LIMIT_PARAM[a.fc]
            invalid[name] = (
                f"modbus address {a.text!r} ({a.dtype}): needs {a.count} {unit} in one request, "
                f"more than the limit {limit} ({param}={getattr(p, param)}, protocol max {PROTOCOL_MAX[a.fc]})"
            )
            continue
        names_by_key.setdefault(a.key, []).append(name)
        addr_by_key.setdefault(a.key, a)

    blocks: list[ModbusBlock] = []
    group: list[tuple[ModbusAddress, tuple[str, ...]]] = []
    end = 0

    def close() -> None:
        start = group[0][0].address
        items = [ModbusItem(names=names, offset=a.address - start, addr=a) for a, names in group]
        blocks.append(ModbusBlock(
            fc=group[0][0].fc, start=start, count=end - start, items=items,
            tags=sorted(n for it in items for n in it.names),
        ))

    for key in sorted(addr_by_key):
        a = addr_by_key[key]
        names = tuple(sorted(names_by_key[key]))
        if group:
            first = group[0][0]
            if (a.fc == first.fc and a.address - end <= p.max_gap_for(a.fc)
                    and max(end, a.end) - first.address <= p.max_span(a.fc)):
                group.append((a, names))
                end = max(end, a.end)
                continue
            close()
        group = [(a, names)]
        end = a.end
    if group:
        close()
    return blocks, invalid


# --- decoder -----------------------------------------------------------------------------

class _Fail(Exception):
    pass


def decode_block(
    block: ModbusBlock, data: Sequence[int] | Sequence[bool], params: ModbusParams | Mapping[str, Any] | None = None,
) -> dict[str, Any | DecodeError]:
    """Decode one block reply into {tag name: value or DecodeError}.

    `data` is the reply as pymodbus gives it: `.registers` (uint16 values, wire bytes
    high-first) for FC03/04, `.bits` (may be padded to a multiple of 8) for FC01/02.
    No scaling is applied here.
    """
    p = ModbusParams.from_mapping(params)
    out: dict[str, Any | DecodeError] = {}
    for item in block.items:
        try:
            value: Any = _decode_item(item, data, p)
        except _Fail as exc:
            value = DecodeError(f"{item.addr.text} ({item.addr.dtype}): {exc}")
        for name in item.names:
            out[name] = _copy(value)
    return out


def _copy(value: Any) -> Any:
    return [_copy(v) for v in value] if isinstance(value, list) else value


def _decode_item(item: ModbusItem, data: Sequence[Any], p: ModbusParams) -> Any:
    a = item.addr
    lo, hi = item.offset, item.offset + a.count
    if len(data) < hi:
        unit = "bits" if a.is_bits else "registers"
        raise _Fail(f"short reply: need {unit} up to offset {hi - 1}, got {len(data)}")
    chunk = data[lo:hi]
    if a.is_bits:
        return _shape([_as_bit(v) for v in chunk], a.dims)

    words = [_as_word(v) for v in chunk]
    if a.str_len is not None:                    # byte order comes from the H/L suffix
        return _decode_string(words, a.str_len, a.str_order or "H")
    if not p.byte_order_big:
        words = [((w & 0xFF) << 8) | (w >> 8) for w in words]
    if a.bit is not None:
        return bool((words[0] >> a.bit) & 1)
    n = REGS_PER_DTYPE[a.dtype]
    values = [_decode_scalar(words[i:i + n], a.dtype, p) for i in range(0, len(words), n)]
    return _shape(values, a.dims)


def _shape(values: list[Any], dims: tuple[int, ...]) -> Any:
    if not dims:
        return values[0]
    if len(dims) == 1:
        return values
    cols = dims[1]
    return [values[r * cols:(r + 1) * cols] for r in range(dims[0])]


def _as_bit(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if type(v) is int and v in (0, 1):
        return bool(v)
    raise _Fail(f"bit value {v!r} is not a bool")


def _as_word(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFF:
        raise _Fail(f"register value {v!r} is not a 16-bit unsigned integer")
    return v


def _dword(first: int, second: int, first_word_low: bool) -> int:
    return (second << 16) | first if first_word_low else (first << 16) | second


def _decode_scalar(words: list[int], dtype: str, p: ModbusParams) -> int | float:
    if len(words) == 1:
        w = words[0]
        return w - 0x10000 if dtype == "int16" and w & 0x8000 else w
    if len(words) == 2:
        u = _dword(words[0], words[1], p.first_word_low)
        if dtype == "uint32":
            return u
        if dtype == "int32":
            return u - (1 << 32) if u & (1 << 31) else u
        return _float(u, 32, p)
    d0 = _dword(words[0], words[1], p.first_word_low)
    d1 = _dword(words[2], words[3], p.first_word_low)
    u = (d1 << 32) | d0 if p.first_dword_low else (d0 << 32) | d1
    if dtype == "uint64":
        return u
    if dtype == "int64":
        return u - (1 << 64) if u & (1 << 63) else u
    return _float(u, 64, p)


def _float(u: int, width: int, p: ModbusParams) -> float:
    if width == 32:
        exp, mant, fmt = (u >> 23) & 0xFF, u & 0x7FFFFF, ">f"
        nonnormal = exp == 0xFF or (exp == 0 and mant != 0)
    else:
        exp, mant, fmt = (u >> 52) & 0x7FF, u & ((1 << 52) - 1), ">d"
        nonnormal = exp == 0x7FF or (exp == 0 and mant != 0)
    if nonnormal and p.nonnormal_float == "zero":
        return 0.0
    return struct.unpack(fmt, u.to_bytes(width // 8, "big"))[0]


def _decode_string(words: list[int], length: int, order: str) -> str:
    raw = bytearray()
    for w in words:
        hi, lo = w >> 8, w & 0xFF
        raw.extend((hi, lo) if order == "H" else (lo, hi))
    raw = raw[:length]
    nul = raw.find(0)
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode("latin-1")


# --- I/O helpers ----------------------------------------------------------------------------

_log = structlog.get_logger("opencap.drivers.modbus_tcp")

# pymodbus' own response timer is set this much later than ours, so the driver's timer
# (device.timeout_ms) always fires first and a timeout is never confused with another error.
_LIBRARY_TIMEOUT_SLACK_S = 1.0
# Disable pymodbus' "close after N unanswered requests": the driver alone decides (close_on_timeout).
_NO_AUTO_DISCONNECT = 1_000_000

READ_FUNCTIONS: dict[int, str] = {
    1: "read_coils", 2: "read_discrete_inputs", 3: "read_holding_registers", 4: "read_input_registers",
}
EXCEPTION_NAMES: dict[int, str] = {
    1: "illegal function", 2: "illegal data address", 3: "illegal data value",
    4: "server device failure", 5: "acknowledge", 6: "server device busy",
    7: "negative acknowledge", 8: "memory parity error",
    10: "gateway path unavailable", 11: "gateway target device failed to respond",
}
# The device permanently rejects the request: the block is wrong, retrying cannot help.
CONFIG_EXCEPTIONS = frozenset({1, 2, 3})
# FC43/14 object ids (Modbus Application Protocol v1.1b3, 6.21).
IDENTITY_OBJECTS: dict[int, str] = {
    0x00: "vendor", 0x01: "product_code", 0x02: "revision", 0x03: "vendor_url",
    0x04: "product_name", 0x05: "model_name", 0x06: "user_application_name",
}
_READ_DEVICE_ID_BASIC = 0x01


def exception_text(code: int) -> str:
    return f"Modbus exception {code:02d} ({EXCEPTION_NAMES.get(code, 'unknown')})"


def _identity_text(raw: Any) -> str:
    if isinstance(raw, list):
        return ", ".join(t for t in (_identity_text(r) for r in raw) if t)
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("latin-1")
    return str(raw).replace("\x00", "").strip()


# --- driver --------------------------------------------------------------------------------

class ModbusTcpDriver(Driver):
    """Modbus TCP over pymodbus' async client, one TCP connection per device.

    Contract (docs/phase1_contracts.md §1): every call makes one attempt. Retries,
    backoff, reconnects and scaling belong to the ChannelWorker. pymodbus' own retries,
    auto-reconnect and auto-disconnect are switched off.
    """

    name: ClassVar[str] = "modbus_tcp"
    is_async: ClassVar[bool] = True

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)
        self.params = ModbusParams.from_mapping(device.params)   # ValueError on bad params
        self._client: Any = None                      # pymodbus AsyncModbusTcpClient while connected
        self._lost: asyncio.Future[None] | None = None   # resolved when the peer closes the socket

    # --- pure ---

    def plan(self, tags: list[Tag]) -> Plan:
        parsed: list[tuple[str, ModbusAddress]] = []
        invalid: dict[str, str] = {}
        for tag in tags:
            if not tag.enabled:
                continue
            try:
                parsed.append((tag.name, parse_address(tag.address, tag.dtype, self.params)))
            except ValueError as exc:
                invalid[tag.name] = str(exc)
        blocks, too_big = plan_blocks(parsed, self.params)
        invalid.update(too_big)
        return Plan(blocks=list(blocks), invalid=dict(sorted(invalid.items())))

    def decode(self, block: ModbusBlock, data: Sequence[int] | Sequence[bool]) -> dict[str, Any | DecodeError]:
        return decode_block(block, data, self.params)

    # --- I/O ---

    @property
    def connected(self) -> bool:
        """True while the TCP connection is open. False after close(), a peer close, or a
        timeout with `close_on_timeout`: the next read() then raises ConnectionLost."""
        return self._client is not None and bool(self._client.connected)

    @property
    def timeout_s(self) -> float:
        return max(self.device.timeout_ms, 1) / 1000.0

    @property
    def _where(self) -> str:
        return f"{self.device.id} ({self.device.host}:{self.device.port})"

    async def connect(self) -> None:
        """Open the TCP connection: one attempt, bounded by `device.timeout_ms`.

        An open connection is closed first. Raises ConnectionLost on failure.
        """
        await self.close()
        from pymodbus.client import AsyncModbusTcpClient   # lazy: plan() works without pymodbus (D-002)

        lost: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def on_connect(up: bool) -> None:   # pymodbus trace_connect: True on connect, False on peer close
            if not up and not lost.done():
                lost.set_result(None)

        client = AsyncModbusTcpClient(
            self.device.host,
            port=self.device.port,
            name=self.device.id,
            timeout=self.timeout_s + _LIBRARY_TIMEOUT_SLACK_S,
            retries=0,                  # one attempt per call
            reconnect_delay=0,          # no auto-reconnect: the worker reconnects with backoff
            reconnect_delay_max=0,
            trace_connect=on_connect,
        )
        client.set_max_no_responses(_NO_AUTO_DISCONNECT)
        started = time.monotonic()
        reason = ""
        ok = False
        try:
            async with asyncio.timeout(self.timeout_s):
                ok = bool(await client.connect())
        except TimeoutError:
            reason = f"no answer within {self.device.timeout_ms} ms"
        except Exception as exc:   # pymodbus normally returns False instead of raising
            reason = f"{type(exc).__name__}: {exc}"
        except BaseException:
            client.close()
            raise
        elapsed_ms = (time.monotonic() - started) * 1000
        if not ok or not client.connected:
            client.close()
            if not reason:
                reason = "refused or unreachable"
            _log.debug("modbus connect", device=self.device.id, result="failed", reason=reason,
                       ms=round(elapsed_ms, 1))
            raise ConnectionLost(f"{self._where}: connect failed: {reason}")
        self._client, self._lost = client, lost
        _log.debug("modbus connect", device=self.device.id, result="ok", ms=round(elapsed_ms, 1))

    async def close(self) -> None:
        """Close the connection. Idempotent, never raises."""
        client, lost = self._client, self._lost
        self._client = self._lost = None
        if lost is not None and not lost.done():
            lost.set_result(None)      # wakes a request still waiting on this connection
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:   # pragma: no cover - pymodbus close() does not raise today
            _log.debug("modbus close error", device=self.device.id, error=repr(exc))
        else:
            _log.debug("modbus close", device=self.device.id)

    async def read(self, block: Block) -> dict[str, TagValue]:
        """Read one block: one request, bounded by `device.timeout_ms`.

        Returns a TagValue for every name in `block.tags` (`good` with the raw value, or
        `bad_decode`). Raises ConnectionLost (not connected, socket closed), CommError
        (timeout, other Modbus exceptions) or BlockConfigError (exceptions 01/02/03).
        """
        if not isinstance(block, ModbusBlock):
            raise TypeError(f"modbus_tcp cannot read {type(block).__name__}")
        client = self._client
        if client is None or not client.connected:
            raise ConnectionLost(f"{self._where}: not connected")
        what = block.describe()
        method = getattr(client, READ_FUNCTIONS[block.fc])
        reply, ts = await self._request(
            lambda: method(block.start, count=block.count, device_id=self.params.unit_id),
            what, close_on_timeout=self.params.close_on_timeout,
        )
        if reply.isError():
            code = int(getattr(reply, "exception_code", 0) or 0)
            msg = f"{self._where} {what}: {exception_text(code)}"
            if code in CONFIG_EXCEPTIONS:
                raise BlockConfigError(msg)
            raise CommError(msg)
        data = reply.bits if block.is_bits else reply.registers
        decoded = decode_block(block, data, self.params)
        out: dict[str, TagValue] = {}
        for name in block.tags:
            value = decoded.get(name, DecodeError(f"{what}: tag {name!r} is not in the block items"))
            if isinstance(value, DecodeError):
                _log.debug("modbus decode error", device=self.device.id, tag=name, reason=value.reason)
                out[name] = TagValue(None, "bad_decode", ts)
            else:
                out[name] = TagValue(value, "good", ts)
        return out

    async def identity(self) -> dict[str, Any]:
        """FC43/14 basic device identification ({"vendor", "product_code", "revision", ...}).

        Returns {} when not connected, or when the device does not answer or rejects the
        request. Never raises. A timeout here does not close the socket: many devices
        silently ignore FC43.
        """
        client = self._client
        if client is None or not client.connected:
            return {}
        try:
            reply, _ = await self._request(
                lambda: client.read_device_information(
                    read_code=_READ_DEVICE_ID_BASIC, object_id=0x00, device_id=self.params.unit_id),
                "FC43/14 device identification", close_on_timeout=False,
            )
            if reply.isError():
                return {}
            out: dict[str, Any] = {}
            for oid, raw in sorted((getattr(reply, "information", None) or {}).items()):
                text = _identity_text(raw)
                if text:
                    out[IDENTITY_OBJECTS.get(oid, f"object_{oid:#04x}")] = text
            return out
        except Exception as exc:
            _log.debug("modbus identity failed", device=self.device.id, error=str(exc))
            return {}

    async def _request(
        self, make: Callable[[], Awaitable[Any]], what: str, *, close_on_timeout: bool,
    ) -> tuple[Any, datetime]:
        """Send one request and wait for its reply, a peer close, or the timeout.

        Returns (reply PDU, arrival time UTC). Error replies (exception PDUs) are returned,
        not raised. Raises ConnectionLost or CommError.
        """
        client, lost = self._client, self._lost
        if client is None or lost is None:
            raise ConnectionLost(f"{self._where}: not connected")
        started = time.monotonic()

        def done(result: str, **kw: Any) -> None:
            _log.debug("modbus request", device=self.device.id, request=what, unit=self.params.unit_id,
                       result=result, ms=round((time.monotonic() - started) * 1000, 1), **kw)

        try:
            task = asyncio.ensure_future(make())
        except Exception as exc:   # pymodbus raises ConnectionException synchronously when not connected
            done("error", error=repr(exc))
            raise await self._translate(client, what, exc) from exc
        try:
            finished, _ = await asyncio.wait(
                (task, lost), timeout=self.timeout_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not task.done():
                task.cancel()
        if task in finished:
            ts = datetime.now(UTC)
            try:
                reply = task.result()
            except Exception as exc:
                done("error", error=repr(exc))
                raise await self._translate(client, what, exc) from exc
            if reply is None:
                done("error", error="empty reply")
                raise CommError(f"{self._where} {what}: no reply object")
            if reply.isError():
                done("exception", code=int(getattr(reply, "exception_code", 0) or 0))
            else:
                done("ok")
            return reply, ts
        # No reply. Let the cancelled request unwind first: it holds pymodbus' transaction lock.
        await asyncio.wait((task,))
        if not task.cancelled():
            task.exception()   # a reply that raced the timeout is dropped; mark it retrieved
        if lost.done() or not client.connected:
            done("conn_lost")
            await self._drop(client)
            raise ConnectionLost(f"{self._where} {what}: connection closed")
        done("timeout", closed=close_on_timeout)
        if close_on_timeout:
            await self._drop(client)
        raise CommError(
            f"{self._where} {what}: no reply within {self.device.timeout_ms} ms"
            + ("; socket closed (close_on_timeout)" if close_on_timeout else ""))

    async def _translate(self, client: Any, what: str, exc: Exception) -> CommError:
        """Map a library exception to CommError or ConnectionLost (closing the connection)."""
        from pymodbus.exceptions import ConnectionException

        if isinstance(exc, (ConnectionException, OSError)) or not client.connected:
            await self._drop(client)
            return ConnectionLost(f"{self._where} {what}: {exc}")
        return CommError(f"{self._where} {what}: {exc}")

    async def _drop(self, client: Any) -> None:
        """Close `client` if it is still the current connection (never a newer one)."""
        if self._client is client:
            await self.close()
        else:
            try:
                client.close()
            except Exception:   # pragma: no cover
                pass
