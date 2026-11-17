"""Mitsubishi MELSEC driver: MC protocol 3E frame, binary, over TCP or UDP (PLAN.md §5.1,
`melsec_mc`), following Kepware "Mitsubishi Ethernet" (Q Series) address semantics.

The pure part (address parser, block planner, word decoder, reply checks) needs no socket and
no pymcprotocol (D-002). The I/O part (`MelsecMcDriver.connect/read/close/identity`) runs
pymcprotocol 0.3.0 `Type3E` on the channel's single-thread executor, through a small subclass
(`mc_client_class()`) whose hooks are pinned by tests/unit/test_melsec_io.py.

Addresses (Kepware Q Series table; case-insensitive, leading zeros allowed)
    bit devices   X Y DX DY B SB (hex numbers), M SM L F V TS TC SS SC CS CC (decimal)
    word devices  D SD R TN SN CN (decimal), W SW ZR (hex)
    D0000002          word device, 1/2/4 words by dtype (16/32/64-bit)
    B0000, M10        one point of a bit device (bool)
    M16 (uint16)      16 points of a bit device as a word; needs a 16-point boundary
    D100.05           bit 0..15 of a word, 16..31 of the 2-word value (bool)
    D100[4], D0[2][3] arrays on word devices (not bool, not strings)
    DSH100.020        string of 20 bytes, first char in the high byte (DSL: low byte);
                      also WSH/WSL, RSH/RSL, ZRSH/ZRSL; length 2..128, even; '.' or ':'

Every block is one "batch read in word units" (command 0401, subcommand 0000, or 0002 for
iQ-R): bit devices are read 16 points per word with the head on a 16-point boundary. A block
spans at most `max_words` (MC 3E limit: 960 words per request).

pymcprotocol 0.3.0 facts this module is built on (docs/decisions.md D-033..D-036):
- `Type3E._recv()` is a single `recv(4096)`. A reply split across TCP segments is decoded
  short, and `int.from_bytes(b"")` is 0, so the missing words read as 0. The subclass reads
  exactly one 3E frame (header, then the length it announces), and the driver checks that
  the reply carries exactly the words it asked for.
- The answer check raises a bare `MCProtocolError` with the end code as text (and
  `UnsupportedComandError` without any code for 0xC059). The subclass raises `McEndCode`
  with the integer.
- Only TCP is implemented by pymcprotocol; the subclass adds UDP (one frame per datagram).
- ASCII mode writes the numbers of hex devices (X, Y, B, W, ZR...) as decimal digits.
  Kepware's driver is "Binary Format only", and so is this one.

The server is read-only: the subclass refuses, before sending, every command except batch
read in word units (0401/0000, 0401/0002) and read CPU model (0101/0000).
"""

from __future__ import annotations

import asyncio
import functools
import math
import re
import socket
import struct
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers._cip_common import Attempt
from opencap.drivers.base import (
    Block,
    BlockConfigError,
    CommError,
    ConnectionLost,
    DecodeError,
    Driver,
    Plan,
)

PLC_TYPES = ("Q", "L", "QnA", "iQ-L", "iQ-R")
PROTOCOLS = ("tcp", "udp")
FRAMES = ("3E",)
NONNORMAL_FLOAT_MODES = ("zero", "unmodified")   # Kepware: "Replace with Zero" / "Unmodified"

# MC protocol 3E per-request limits (MELSEC communication protocol reference).
MAX_WORDS = 960        # batch read in word units (0401/0000); bit devices: 960 words = 15,360 points
MAX_BIT_POINTS = 7904  # batch read in bit units (0401/0001): not used, word units are cheaper
MAX_RANDOM = 192       # random read (0403): not used
MAX_STRING_LEN = 128   # Kepware: 2..128 bytes, even

WORDS_PER_DTYPE: dict[str, int] = {
    "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2, "float32": 2,
    "int64": 4, "uint64": 4, "float64": 4,
}
DTYPES = frozenset(WORDS_PER_DTYPE) | {"bool", "string"}
BIT_DEVICE_DTYPES = ("int16", "uint16", "int32", "uint32")     # Kepware: Short/Word/BCD, Long/DWord/LBCD
_BOOL_PARAMS = frozenset({"first_word_low"})
_STR_PARAMS: dict[str, tuple[str, ...]] = {
    "plc_type": PLC_TYPES, "protocol": PROTOCOLS, "frame": FRAMES, "nonnormal_float": NONNORMAL_FLOAT_MODES,
}
_INT_RANGES: dict[str, tuple[int, int]] = {
    "max_words": (1, MAX_WORDS), "max_gap": (0, 1_000_000),
    "network": (0, 0xFF), "pc": (0, 0xFF), "dest_moduleio": (0, 0xFFFF), "dest_modulesta": (0, 0xFF),
}


# --- params -----------------------------------------------------------------------

@dataclass(frozen=True)
class McParams:
    """Typed view of `Device.params` for melsec_mc. Unknown keys are ignored."""

    plc_type: str = "Q"                 # pymcprotocol plctype: wire format and device codes (D-034)
    protocol: str = "tcp"
    first_word_low: bool = True
    frame: str = "3E"
    max_words: int = MAX_WORDS          # words per request, capped at the 3E limit
    max_gap: int = 16                   # unused words allowed between merged tags
    nonnormal_float: str = "zero"
    network: int = 0                    # 3E header: network number (0 = local)
    pc: int = 0xFF                      # 3E header: PC number (0xFF = the connected station)
    dest_moduleio: int = 0x3FF          # 3E header: request destination module I/O (0x3FF = own CPU)
    dest_modulesta: int = 0             # 3E header: request destination module station

    def __post_init__(self) -> None:
        for f in fields(self):
            name, value = f.name, getattr(self, f.name)
            if name in _BOOL_PARAMS:
                if not isinstance(value, bool):
                    raise ValueError(f"melsec_mc param {name}={value!r}: expected true or false")
            elif name in _STR_PARAMS:
                if value not in _STR_PARAMS[name]:
                    raise ValueError(f"melsec_mc param {name}={value!r}: expected one of {_STR_PARAMS[name]}")
            else:
                lo, hi = _INT_RANGES[name]
                if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                    raise ValueError(f"melsec_mc param {name}={value!r}: expected an integer {lo}..{hi}")

    @classmethod
    def from_mapping(cls, params: McParams | Mapping[str, Any] | None) -> McParams:
        """Accept a `McParams`, a `Device.params` dict, or None. A None value means "default"."""
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

    @property
    def iqr(self) -> bool:
        return self.plc_type == "iQ-R"


# --- devices ----------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceSpec:
    name: str              # Kepware / GX Works name
    kind: str              # "bit" | "word"
    base: int              # 10 or 16: how the device number is written
    last: int              # last device number in Kepware's Q Series table
    lib_name: str          # pymcprotocol device name
    bit_access: bool = False   # word devices: D100.05 bit addresses
    strings: bool = False      # word devices: DSH/DSL string addresses
    wide: bool = True          # word devices: 32/64-bit values allowed


def _bit(name: str, base: int, last: int, lib_name: str | None = None) -> DeviceSpec:
    return DeviceSpec(name, "bit", base, last, lib_name or name)


def _word(name: str, base: int, last: int, *, lib_name: str | None = None, bits: bool = True,
          strings: bool = False, wide: bool = True) -> DeviceSpec:
    return DeviceSpec(name, "word", base, last, lib_name or name, bit_access=bits, strings=strings, wide=wide)


# Kepware "Mitsubishi Ethernet" Q Series Address Descriptions (ranges and radix).
DEVICES: dict[str, DeviceSpec] = {s.name: s for s in (
    _bit("X", 16, 0x3FFF), _bit("DX", 16, 0x3FFF), _bit("Y", 16, 0x3FFF), _bit("DY", 16, 0x3FFF),
    _bit("B", 16, 0xEA60), _bit("SB", 16, 0x7D00),
    _bit("M", 10, 60000), _bit("SM", 10, 2047), _bit("L", 10, 32000), _bit("F", 10, 32000), _bit("V", 10, 32000),
    _bit("TS", 10, 32000), _bit("TC", 10, 32000), _bit("SS", 10, 2047, "STS"), _bit("SC", 10, 2047, "STC"),
    _bit("CS", 10, 32000), _bit("CC", 10, 32000),
    _word("D", 10, 4184063, strings=True), _word("SD", 10, 2047),
    _word("W", 16, 0x3FD7FF, strings=True), _word("SW", 16, 0x7D00),
    _word("R", 10, 32767, strings=True), _word("ZR", 16, 0x3FD7FF, strings=True),
    _word("TN", 10, 32000, bits=False, wide=False), _word("SN", 10, 2047, lib_name="STN", bits=False, wide=False),
    _word("CN", 10, 32000, bits=False, wide=False),
)}
# String forms: <device>SH (first char in the high byte) and <device>SL (low byte first).
_STRING_PREFIXES: dict[str, tuple[str, str]] = {
    f"{name}S{order}": (name, order) for name, s in DEVICES.items() if s.strings for order in ("H", "L")
}
# Longest prefix first, so DX wins over D and ZRSH over ZR.
_PREFIXES: tuple[str, ...] = tuple(sorted([*DEVICES, *_STRING_PREFIXES], key=lambda p: (-len(p), p)))
_REST_RE: dict[int, re.Pattern[str]] = {
    base: re.compile(
        rf"(?P<num>{digits})(?P<sfx>[.:]\d{{1,3}})?(?P<dims>(?:\s*\[\s*\d+\s*\])*)(?P<long>\s+L)?")
    for base, digits in ((10, r"\d+"), (16, r"[0-9A-F]+"))
}
_DIM_RE = re.compile(r"\[\s*(\d+)\s*\]")


def format_number(device: str, number: int) -> str:
    """A device number as Kepware writes it: hex (upper case) or decimal."""
    return f"{number:X}" if DEVICES[device].base == 16 else str(number)


def device_string(device: str, number: int) -> str:
    """The device text pymcprotocol parses back to `number` (hex devices need a leading digit)."""
    spec = DEVICES[device]
    return f"{spec.lib_name}0{number:X}" if spec.base == 16 else f"{spec.lib_name}{number}"


# --- address parser -------------------------------------------------------------------

@dataclass(frozen=True)
class McAddress:
    text: str                       # address as written (stripped)
    device: str                     # Kepware device name, e.g. "D", "M", "X"
    kind: str                       # "bit" | "word"
    number: int                     # device number as written (hex already converted)
    dtype: str
    start: int                      # first word read: device number (word devices) or number // 16 (bit devices)
    words: int                      # words read
    bit: int | None = None          # bool: bit of the (1- or 2-word) value, 0 = LSB
    dims: tuple[int, ...] = ()      # array dimensions, () = scalar
    str_len: int | None = None      # string length in bytes
    str_order: str | None = None    # "H" (first char in the high byte) or "L"

    @property
    def end(self) -> int:
        """Exclusive end, in words."""
        return self.start + self.words

    @property
    def key(self) -> tuple:
        """Identity of the data read (ignores the spelling): equal keys are read and decoded once."""
        return (
            self.device, self.start, self.words, self.dtype, -1 if self.bit is None else self.bit,
            self.dims, self.str_len or 0, self.str_order or "",
        )


def _match_device(text: str) -> tuple[DeviceSpec, str | None, re.Match[str]] | None:
    for prefix in _PREFIXES:
        if not text.startswith(prefix):
            continue
        name, order = _STRING_PREFIXES.get(prefix, (prefix, None))
        spec = DEVICES[name]
        m = _REST_RE[spec.base].fullmatch(text[len(prefix):])
        if m:
            return spec, order, m
    return None


def parse_address(address: str, dtype: str, params: McParams | Mapping[str, Any] | None = None) -> McAddress:
    """Parse a Kepware Mitsubishi (Q Series) address for a tag of `dtype`. Raises ValueError
    with a clear reason. `params` is accepted for symmetry with the other drivers (unused)."""
    McParams.from_mapping(params)

    def bad(reason: str) -> ValueError:
        return ValueError(f"melsec address {address!r} ({dtype}): {reason}")

    if not isinstance(address, str):
        raise bad("address must be a string")
    text = address.strip()
    if not text:
        raise bad("empty address")
    if dtype not in DTYPES:
        raise bad(f"unknown dtype {dtype!r}")
    found = _match_device(text.upper())
    if found is None:
        raise bad("unknown device or malformed number; supported devices: "
                  + ", ".join(DEVICES) + " (and DSH/DSL, WSH/WSL, RSH/RSL, ZRSH/ZRSL strings)")
    spec, order, m = found
    if m["long"]:
        raise bad("the ' L' suffix (Kepware long access) is not supported; use dtype int32/uint32")
    number = int(m["num"], spec.base)
    num_text = m["num"]
    dims = tuple(int(x) for x in _DIM_RE.findall(m["dims"]))
    if len(dims) > 2:
        raise bad("at most 2 array dimensions ([rows][cols])")
    if any(d < 1 for d in dims):
        raise bad("array dimension must be >= 1")
    sfx = m["sfx"]
    bit: int | None = None
    str_len: int | None = None

    if order is not None:                                    # DSH100.020 string
        if dtype != "string":
            raise bad(f"a {spec.name}S{order} string address needs dtype string")
        if sfx is None:
            raise bad(f"string needs a length, e.g. {spec.name}S{order}{num_text}.020")
        if dims:
            raise bad("arrays of strings are not supported")
        str_len = int(sfx[1:])
        if not (2 <= str_len <= MAX_STRING_LEN and str_len % 2 == 0):
            raise bad(f"string length {str_len} must be 2..{MAX_STRING_LEN} and even")
        start, words = number, str_len // 2
        last_point = number + words - 1
    else:
        if dtype == "string":
            hint = (f"use the {spec.name}SH/{spec.name}SL form, e.g. {spec.name}SH{num_text}.020" if spec.strings
                    else "strings are only supported on D, W, R and ZR")
            raise bad(f"string needs a length and byte order: {hint}")
        if sfx is not None and sfx[0] == ":":
            raise bad("':' is only valid in string addresses")
        if spec.kind == "bit":
            if sfx is not None:
                raise bad("a .bit suffix is only valid on word devices")
            if dims:
                raise bad("arrays are not supported on bit devices")
            if dtype == "bool":
                start, words, bit = number // 16, 1, number % 16
                last_point = number
            else:
                if dtype not in BIT_DEVICE_DTYPES:
                    raise bad(f"{spec.name} is a bit device: only bool, or 16/32-bit integers "
                              f"(16 or 32 points read as words)")
                if number % 16:
                    raise bad(f"word access to bit device {spec.name} must start on a 16-point boundary "
                              f"(e.g. {spec.name}{format_number(spec.name, number - number % 16)})")
                start, words = number // 16, WORDS_PER_DTYPE[dtype]
                last_point = number + 16 * words - 1
        else:
            if sfx is not None:                              # D100.05 bit in a word
                if not spec.bit_access:
                    raise bad(f"bit access is not supported on {spec.name}")
                if dtype != "bool":
                    raise bad("a .bit address must have dtype bool")
                if dims:
                    raise bad("bool arrays are not supported")
                bit = int(sfx[1:])
                if not 0 <= bit <= 31:
                    raise bad(f"bit .{bit} out of range 0..31")
                words = 1 if bit < 16 else 2
            else:
                if dtype == "bool":
                    raise bad(f"bool on word device {spec.name} needs a .bit suffix, e.g. {spec.name}{num_text}.00")
                if not spec.wide and WORDS_PER_DTYPE[dtype] > 1:
                    raise bad(f"{spec.name} holds 16-bit values only (int16/uint16)")
                words = (math.prod(dims) if dims else 1) * WORDS_PER_DTYPE[dtype]
            start = number
            last_point = number + words - 1

    if last_point > spec.last:
        raise bad(f"needs {spec.name}{format_number(spec.name, number)}..{format_number(spec.name, last_point)}, "
                  f"past the last {spec.name} device {format_number(spec.name, spec.last)} (Kepware Q Series range)")
    return McAddress(text=text, device=spec.name, kind=spec.kind, number=number, dtype=dtype, start=start,
                     words=words, bit=bit, dims=dims, str_len=str_len, str_order=order)


# --- blocks and planner -----------------------------------------------------------------

@dataclass(frozen=True)
class McItem:
    """One distinct piece of data inside a block, served to every tag name that reads it."""

    names: tuple[str, ...]
    offset: int                     # first word of the item, relative to the block start
    addr: McAddress


@dataclass(kw_only=True)
class McBlock(Block):
    device: str                     # Kepware device name
    start: int                      # first word: device number (word devices) or point // 16 (bit devices)
    count: int                      # words
    items: list[McItem] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return DEVICES[self.device].kind

    @property
    def head_number(self) -> int:
        """Device number of the first point read (bit devices: a multiple of 16)."""
        return self.start * 16 if self.kind == "bit" else self.start

    @property
    def last_number(self) -> int:
        return (self.start + self.count) * 16 - 1 if self.kind == "bit" else self.start + self.count - 1

    @property
    def head(self) -> str:
        """Head device as pymcprotocol takes it."""
        return device_string(self.device, self.head_number)

    def describe(self) -> str:
        d = self.device
        return (f"{d}{format_number(d, self.head_number)}..{d}{format_number(d, self.last_number)} "
                f"({self.count} words)")


def plan_blocks(
    parsed: Iterable[tuple[str, McAddress]], params: McParams | Mapping[str, Any] | None = None,
) -> tuple[list[McBlock], dict[str, str]]:
    """Merge parsed tags into wire blocks. Returns (blocks, invalid): invalid = tags that fit no block.

    Greedy over items sorted by (device, start word): an item joins the open block when it is
    on the same device, starts at most `max_gap` words after the block end, and the merged span
    stays within `max_words`. Output is sorted and independent of input order.
    """
    p = McParams.from_mapping(params)
    invalid: dict[str, str] = {}
    names_by_key: dict[tuple, list[str]] = {}
    addr_by_key: dict[tuple, McAddress] = {}
    for name, a in parsed:
        if a.words > p.max_words:
            invalid[name] = (f"melsec address {a.text!r} ({a.dtype}): needs {a.words} words in one request, "
                             f"more than max_words={p.max_words} (MC 3E limit {MAX_WORDS})")
            continue
        names_by_key.setdefault(a.key, []).append(name)
        addr_by_key.setdefault(a.key, a)

    blocks: list[McBlock] = []
    group: list[tuple[McAddress, tuple[str, ...]]] = []
    end = 0

    def close() -> None:
        start = group[0][0].start
        items = [McItem(names=names, offset=a.start - start, addr=a) for a, names in group]
        blocks.append(McBlock(device=group[0][0].device, start=start, count=end - start, items=items,
                              tags=sorted(n for it in items for n in it.names)))

    for key in sorted(addr_by_key):
        a = addr_by_key[key]
        names = tuple(sorted(names_by_key[key]))
        if group:
            first = group[0][0]
            if (a.device == first.device and a.start - end <= p.max_gap
                    and max(end, a.end) - first.start <= p.max_words):
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
    block: McBlock, data: Sequence[int], params: McParams | Mapping[str, Any] | None = None,
) -> dict[str, Any | DecodeError]:
    """Decode one block reply into {tag name: value or DecodeError}.

    `data` is the word list as pymcprotocol's `batchread_wordunits` returns it (signed
    int16; unsigned 0..65535 is accepted too). No scaling is applied here.
    """
    p = McParams.from_mapping(params)
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


def _as_word(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not -0x8000 <= v <= 0xFFFF:
        raise _Fail(f"word value {v!r} is not a 16-bit integer")
    return v & 0xFFFF


def combine_words(words: Sequence[int], first_word_low: bool) -> int:
    """Words (uint16, lowest device number first) to one unsigned integer."""
    seq = words if first_word_low else list(reversed(words))
    u = 0
    for i, w in enumerate(seq):
        u |= w << (16 * i)
    return u


def _decode_item(item: McItem, data: Sequence[Any], p: McParams) -> Any:
    a = item.addr
    lo, hi = item.offset, item.offset + a.words
    if len(data) < hi:
        raise _Fail(f"short reply: need words up to offset {hi - 1}, got {len(data)}")
    words = [_as_word(v) for v in data[lo:hi]]
    if a.str_len is not None:
        return _decode_string(words, a.str_len, a.str_order or "H")
    if a.bit is not None:
        return bool((combine_words(words, p.first_word_low) >> a.bit) & 1)
    n = WORDS_PER_DTYPE[a.dtype]
    values = [_decode_scalar(words[i:i + n], a.dtype, p) for i in range(0, len(words), n)]
    return _shape(values, a.dims)


def _shape(values: list[Any], dims: tuple[int, ...]) -> Any:
    if not dims:
        return values[0]
    if len(dims) == 1:
        return values
    cols = dims[1]
    return [values[r * cols:(r + 1) * cols] for r in range(dims[0])]


def _decode_scalar(words: list[int], dtype: str, p: McParams) -> int | float:
    u = combine_words(words, p.first_word_low)
    width = 16 * len(words)
    if dtype.startswith("uint"):
        return u
    if dtype.startswith("int"):
        return u - (1 << width) if u >> (width - 1) else u
    return _float(u, width, p)


def _float(u: int, width: int, p: McParams) -> float:
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


# --- 3E frames, end codes, read-only guard (pure) ---------------------------------------------

SUBHEADER_REQUEST = b"\x50\x00"
SUBHEADER_REPLY = b"\xd0\x00"
REPLY_HEADER_LEN = 9               # subheader 2, network 1, PC 1, module I/O 2, station 1, data length 2
END_CODE_LEN = 2

CMD_BATCH_READ = 0x0401
CMD_READ_CPU_MODEL = 0x0101
READ_COMMANDS = frozenset({(CMD_BATCH_READ, 0x0000), (CMD_BATCH_READ, 0x0002), (CMD_READ_CPU_MODEL, 0x0000)})

# End-code texts, paraphrased from the MELSEC communication protocol reference (QnA-compatible
# 3E frame / Q CPU error codes). Not confirmed on hardware.
END_CODE_TEXT: dict[int, str] = {
    0xC050: "ASCII data that cannot be converted to binary",
    0xC051: "number of read/write points out of range",
    0xC052: "number of read/write points out of range",
    0xC053: "number of read/write points out of range",
    0xC054: "number of read/write points out of range",
    0xC055: "number of points exceeds the file data range",
    0xC056: "read/write request exceeds the maximum address",
    0xC058: "request data length does not match the character count",
    0xC059: "command/subcommand wrong or not supported by the target",
    0xC05B: "the CPU cannot read/write the specified device",
    0xC05C: "error in the request content (e.g. bit device in word units off a 16-point boundary)",
    0xC05F: "request cannot be executed on the target CPU",
    0xC060: "error in the request content (bit device specification)",
    0xC061: "request data length does not match the number of data",
    0xC06F: "communication data code (binary/ASCII) does not match the module setting",
    0x4030: "the specified device cannot be accessed (device does not exist)",
    0x4031: "the specified device number is out of range",
}
# The PLC permanently rejects this request: the block (or plc_type) is wrong, retrying cannot help.
CONFIG_END_CODES = frozenset({0xC051, 0xC052, 0xC053, 0xC054, 0xC056, 0xC059, 0xC05B, 0xC05C, 0xC060,
                              0x4030, 0x4031})


def end_code_text(code: int) -> str:
    return f"MC end code {code:#06X} ({END_CODE_TEXT.get(code, 'unknown error')})".replace("0X", "0x", 1)


class McEndCode(Exception):
    """The PLC answered with a non-zero end code."""

    def __init__(self, code: int, reply: bytes = b"") -> None:
        super().__init__(end_code_text(code))
        self.code = code
        self.reply = bytes(reply)


class McFrameError(Exception):
    """The reply is not a well-formed 3E binary reply, or does not carry what was asked."""


class McReadOnlyError(RuntimeError):
    """A command other than the reads the driver needs was about to be sent."""


def request_command(frame: bytes) -> tuple[int, int] | None:
    """(command, subcommand) of a binary 3E request frame, or None if it is not one."""
    if len(frame) < 15 or frame[:2] != SUBHEADER_REQUEST:
        return None
    return int.from_bytes(frame[11:13], "little"), int.from_bytes(frame[13:15], "little")


def check_read_only(frame: bytes) -> str | None:
    """None when `frame` is a request the driver may send, else why it is refused."""
    cmd = request_command(frame)
    if cmd is None:
        return "not a binary 3E request frame (the driver sends binary 3E frames only)"
    if cmd not in READ_COMMANDS:
        return (f"command {cmd[0]:#06x} subcommand {cmd[1]:#06x} is not a read the driver uses "
                "(read-only: only 0401/0000, 0401/0002 and 0101/0000 are sent)")
    return None


def check_reply_frame(frame: bytes) -> int:
    """Validate a binary 3E reply header against the frame length. Returns the announced data
    length (end code + data, in bytes). Raises McFrameError."""
    if len(frame) < REPLY_HEADER_LEN + END_CODE_LEN:
        raise McFrameError(f"reply of {len(frame)} bytes is shorter than a 3E reply header")
    if frame[:2] != SUBHEADER_REPLY:
        raise McFrameError(f"unexpected reply subheader {frame[:2].hex()} (expected d000)")
    length = int.from_bytes(frame[7:9], "little")
    if REPLY_HEADER_LEN + length != len(frame):
        raise McFrameError(f"reply announces {length} bytes after the header, carries {len(frame) - REPLY_HEADER_LEN}")
    return length


def monitoring_timer(timeout_ms: int) -> int:
    """3E monitoring timer (units of 250 ms) for a request timeout."""
    return max(1, min(0xFFFF, math.ceil(timeout_ms / 250)))


# --- I/O: pymcprotocol subclass ---------------------------------------------------------------

_log = structlog.get_logger("opencap.drivers.melsec_mc")


@functools.cache
def mc_client_class() -> type:
    """pymcprotocol `Type3E` subclass: framed replies, UDP, integer end codes, read-only guard.

    Built lazily so `plan()` works without pymcprotocol (D-002).
    """
    import pymcprotocol

    class OoType3E(pymcprotocol.Type3E):
        """Configured by `oo_configure()` before `connect()`. Touched only on the channel executor."""

        def __init__(self, plctype: str = "Q") -> None:
            super().__init__(plctype)
            self._set_commtype("binary")
            self.oo_protocol = "tcp"
            self.oo_timeout = 3.0
            self.oo_last_reply = b""     # last raw reply frame
            self.oo_sent = False         # the last call put a request on the wire
            self._sock: socket.socket | None = None

        def oo_configure(self, *, protocol: str, timeout_s: float, timer: int, network: int, pc: int,
                         dest_moduleio: int, dest_modulesta: int) -> None:
            self.oo_protocol = protocol
            self.oo_timeout = timeout_s
            self.timer = timer
            self.network, self.pc = network, pc
            self.dest_moduleio, self.dest_modulesta = dest_moduleio, dest_modulesta

        # -- socket --
        def connect(self, ip: str, port: int) -> None:
            self._ip, self._port = ip, port
            if self.oo_protocol == "udp":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    sock.settimeout(self.oo_timeout)
                    sock.connect((ip, port))    # fixes the peer: send()/recv() and ICMP errors
                except BaseException:
                    sock.close()
                    raise
            else:
                sock = socket.create_connection((ip, port), timeout=self.oo_timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.oo_timeout)
            self._sock = sock
            self._is_connected = True

        def close(self) -> None:
            sock, self._sock = self._sock, None
            self._is_connected = False
            if sock is not None:
                try:
                    sock.close()
                except OSError:   # pragma: no cover
                    pass

        def _send(self, send_data: bytes) -> None:
            problem = check_read_only(send_data)
            if problem is not None:
                raise McReadOnlyError(problem)
            if not self._is_connected or self._sock is None:
                raise ConnectionError("not connected")
            self.oo_last_reply = b""
            self.oo_sent = True
            self._sock.sendall(send_data)

        def _recv(self) -> bytes:
            assert self._sock is not None
            if self.oo_protocol == "udp":
                frame = self._sock.recv(65535)
                check_reply_frame(frame)
            else:
                header = self._recv_exact(REPLY_HEADER_LEN)
                if header[:2] != SUBHEADER_REPLY:
                    raise McFrameError(f"unexpected reply subheader {header[:2].hex()} (expected d000)")
                length = int.from_bytes(header[7:9], "little")
                frame = header + self._recv_exact(length)
                check_reply_frame(frame)
            self.oo_last_reply = frame
            return frame

        def _recv_exact(self, n: int) -> bytes:
            assert self._sock is not None
            buf = bytearray()
            while len(buf) < n:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionResetError("connection closed by the PLC")
                buf += chunk
            return bytes(buf)

        def _check_cmdanswer(self, recv_data: bytes) -> None:
            code = int.from_bytes(recv_data[REPLY_HEADER_LEN:REPLY_HEADER_LEN + END_CODE_LEN], "little")
            if code:
                raise McEndCode(code, recv_data)

        # -- reads --
        def oo_read_words(self, head: str, count: int) -> list[int]:
            """One batch read in word units; the reply must carry exactly `count` words."""
            self.oo_sent = False
            values = self.batchread_wordunits(head, count)
            data_len = len(self.oo_last_reply) - REPLY_HEADER_LEN - END_CODE_LEN
            if data_len != 2 * count:
                raise McFrameError(f"reply carries {data_len} data bytes for {count} words")
            return values

        def oo_read_cpu_model(self) -> tuple[str, str]:
            self.oo_sent = False
            return self.read_cputype()

    return OoType3E


# --- I/O: driver state ---------------------------------------------------------------------------

@dataclass(eq=False)
class _Session:
    """One open socket (touched by pymcprotocol calls only on the executor)."""

    client: Any
    dead: bool = False          # no longer usable (closed, or failed in the executor)
    closed: bool = False        # socket already closed


class _ReadFailure(Exception):
    def __init__(self, reason: str, *, kind: str = "comm") -> None:
        super().__init__(reason)
        self.kind = kind        # "lost" | "config" | "comm"


def _describe(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if type(exc).__name__ in text else f"{type(exc).__name__}: {text}"


# --- driver -------------------------------------------------------------------------------

class MelsecMcDriver(Driver):
    """MELSEC MC protocol 3E binary via pymcprotocol `Type3E` (sync, channel executor).

    Every call makes one attempt: retries, backoff and reconnects belong to the ChannelWorker.
    """

    name: ClassVar[str] = "melsec_mc"
    is_async: ClassVar[bool] = False

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)
        self.params = McParams.from_mapping(device.params)   # ValueError on bad params
        self._session: _Session | None = None
        self._rejected: set[str] = set()                     # blocks already logged as rejected

    # --- pure ---

    def plan(self, tags: list[Tag]) -> Plan:
        parsed: list[tuple[str, McAddress]] = []
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

    def decode(self, block: McBlock, data: Sequence[int]) -> dict[str, Any | DecodeError]:
        return decode_block(block, data, self.params)

    # --- I/O ---

    @property
    def connected(self) -> bool:
        """True while the socket is believed usable. A TCP peer close is noticed on the next
        request (ConnectionLost); UDP has no connection, a dead PLC shows as timeouts."""
        s = self._session
        return s is not None and not s.dead

    @property
    def timeout_s(self) -> float:
        return max(self.device.timeout_ms, 1) / 1000.0

    @property
    def _where(self) -> str:
        return f"{self.device.id} ({self.device.host}:{self.device.port}/{self.params.protocol})"

    async def connect(self) -> None:
        """Open the socket (TCP connect, or a connected UDP socket): one attempt, bounded by
        `device.timeout_ms`. An open connection is closed first. Raises ConnectionLost."""
        await self.close()
        if self._executor is None:
            raise RuntimeError(f"{self.name}: sync driver needs the channel executor")
        attempt = Attempt()
        started = time.monotonic()
        try:
            session = await self._run_sync(self._open_sync, attempt)
        except asyncio.CancelledError:
            leftover = attempt.abandon()
            if leftover is not None:
                self._close_later(leftover)
            raise
        except Exception as exc:
            reason = "no answer within %d ms" % self.device.timeout_ms if isinstance(exc, TimeoutError) \
                else _describe(exc)
            _log.debug("melsec connect", device=self.device.id, result="failed", reason=reason,
                       ms=round((time.monotonic() - started) * 1000, 1))
            raise ConnectionLost(f"{self._where}: connect failed: {reason}") from exc
        if session is None:    # pragma: no cover - only after abandon(), i.e. cancelled
            raise ConnectionLost(f"{self._where}: connect abandoned")
        self._session = session
        _log.debug("melsec connect", device=self.device.id, result="ok", plc_type=self.params.plc_type,
                   ms=round((time.monotonic() - started) * 1000, 1))

    async def close(self) -> None:
        """Close the socket. Idempotent, never raises."""
        session, self._session = self._session, None
        if session is None or session.closed:
            return
        session.dead = True
        try:
            await self._run_sync(self._dispose_sync, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:     # executor missing or shut down: close the socket from here
            _log.debug("melsec close error", device=self.device.id, error=_describe(exc))
            _abort(session)

    async def read(self, block: Block) -> dict[str, TagValue]:
        """Read one block with one batch read in word units.

        Raises BlockConfigError (end code saying the device/range/command is wrong),
        ConnectionLost (not connected, refused, reset, peer close) or CommError (timeout, after
        closing the socket; malformed reply; any other end code). Undecodable values: bad_decode.
        """
        if not isinstance(block, McBlock):
            raise TypeError(f"melsec_mc cannot read {type(block).__name__}")
        session = self._session
        if session is None or session.dead:
            raise ConnectionLost(f"{self._where}: not connected")
        what = block.describe()
        try:
            values, ts = await self._run_sync(self._read_sync, session, block)
        except _ReadFailure as exc:
            if session.dead and self._session is session:
                self._session = None
            if exc.kind == "config":
                self._log_rejected(block, str(exc))
                raise BlockConfigError(f"{self._where} {what}: {exc}") from exc.__cause__
            err = ConnectionLost if exc.kind == "lost" else CommError
            raise err(f"{self._where} {what}: {exc}") from exc.__cause__
        decoded = decode_block(block, values, self.params)
        out: dict[str, TagValue] = {}
        for name in block.tags:
            value = decoded.get(name, DecodeError(f"{what}: tag {name!r} is not in the block items"))
            if isinstance(value, DecodeError):
                _log.debug("melsec decode error", device=self.device.id, tag=name, reason=value.reason)
                out[name] = TagValue(None, "bad_decode", ts)
            else:
                out[name] = TagValue(value, "good", ts)
        return out

    async def identity(self) -> dict[str, Any]:
        """{"plc_type", "protocol", "frame", "commtype", "cpu_type", "cpu_code"} from "read CPU
        model" (0101). On an error end code or no answer: what is known plus "cpu_type_error".
        {} when not connected. Never raises (a timeout closes the socket, as for a read)."""
        s = self._session
        if s is None or s.dead:
            return {}
        out: dict[str, Any] = {"plc_type": self.params.plc_type, "protocol": self.params.protocol,
                               "frame": self.params.frame, "commtype": "binary"}
        try:
            cpu_type, cpu_code = await self._run_sync(self._cpu_sync, s)
        except _ReadFailure as exc:
            if s.dead and self._session is s:
                self._session = None
            out["cpu_type_error"] = str(exc)
        except Exception as exc:
            out["cpu_type_error"] = _describe(exc)
        else:
            out.update(cpu_type=cpu_type, cpu_code=cpu_code)
        return out

    # --- executor side (every pymcprotocol call happens below, on the channel executor) ---

    def _open_sync(self, attempt: Attempt) -> _Session | None:
        if attempt.abandoned:
            return None
        p = self.params
        client = mc_client_class()(p.plc_type)
        client.oo_configure(protocol=p.protocol, timeout_s=self.timeout_s, timer=monitoring_timer(self.device.timeout_ms),
                            network=p.network, pc=p.pc, dest_moduleio=p.dest_moduleio, dest_modulesta=p.dest_modulesta)
        client.connect(self.device.host, self.device.port)
        session = _Session(client)
        if not attempt.finish(session):
            self._dispose_sync(session)
            return None
        return session

    def _call(self, session: _Session, fn: Any, *args: Any) -> Any:
        """Run one pymcprotocol request; map failures to _ReadFailure (closing when needed)."""
        client = session.client
        if session.dead:
            raise _ReadFailure("connection closed", kind="lost")
        try:
            return fn(*args)
        except McEndCode as exc:
            kind = "config" if exc.code in CONFIG_END_CODES else "comm"
            raise _ReadFailure(str(exc), kind=kind) from exc
        except McReadOnlyError as exc:     # pragma: no cover - the driver only builds reads
            raise _ReadFailure(f"refused by the read-only guard: {exc}") from exc
        except TimeoutError as exc:
            self._dispose_sync(session)
            raise _ReadFailure(f"no reply within {self.device.timeout_ms} ms; socket closed "
                               "(3E replies carry no sequence number)") from exc
        except McFrameError as exc:
            self._dispose_sync(session)
            raise _ReadFailure(f"bad reply: {exc}; socket closed") from exc
        except OSError as exc:             # refused, reset, broken pipe, peer close
            self._dispose_sync(session)
            raise _ReadFailure(f"connection lost: {_describe(exc)}", kind="lost") from exc
        except Exception as exc:           # pymcprotocol could not build the request
            if not client.oo_sent:
                raise _ReadFailure(f"pymcprotocol cannot build the request: {_describe(exc)}",
                                   kind="config") from exc
            self._dispose_sync(session)
            raise _ReadFailure(f"pymcprotocol failed: {_describe(exc)}; socket closed") from exc

    def _read_sync(self, session: _Session, block: McBlock) -> tuple[list[int], datetime]:
        started = time.monotonic()
        values = self._call(session, session.client.oo_read_words, block.head, block.count)
        ts = datetime.now(UTC)
        _log.debug("melsec read", device=self.device.id, block=block.describe(), head=block.head,
                   ms=round((time.monotonic() - started) * 1000, 1))
        return values, ts

    def _cpu_sync(self, session: _Session) -> tuple[str, str]:
        cpu_type, cpu_code = self._call(session, session.client.oo_read_cpu_model)
        return str(cpu_type).strip(), str(cpu_code)

    def _dispose_sync(self, session: _Session) -> None:
        session.dead = True
        if session.closed:
            return
        session.closed = True
        try:
            session.client.close()
        except Exception as exc:   # pragma: no cover
            _log.debug("melsec close error", device=self.device.id, error=_describe(exc))
        _log.debug("melsec close", device=self.device.id)

    def _close_later(self, session: _Session) -> None:
        session.dead = True
        try:
            assert self._executor is not None
            self._executor.submit(self._dispose_sync, session)
        except Exception:
            _abort(session)

    # --- loop side, no pymcprotocol calls ---

    def _log_rejected(self, block: McBlock, reason: str) -> None:
        what = block.describe()
        if what in self._rejected:
            return
        self._rejected.add(what)
        _log.warning("melsec.block_rejected", device=self.device.id, block=what, tags=block.tags[:20],
                     count=len(block.tags), reason=reason, quality="bad_config")


def _abort(session: _Session) -> None:
    """Last resort when the executor is gone: close the socket from the event loop."""
    session.dead = session.closed = True
    sock = getattr(session.client, "_sock", None)
    try:
        if sock is not None:
            sock.close()
    except Exception:   # pragma: no cover
        pass
