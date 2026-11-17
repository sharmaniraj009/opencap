"""Siemens S7 driver (PLAN.md section 5.1, `s7`) for S7-300/400/1200/1500 over ISO-on-TCP,
following KEPServerEX "Siemens TCP/IP Ethernet" semantics, on python-snap7 3.x (pure Python).

Pure parts (no snap7 import, D-002): address parser, block planner, decoder, the read-only PDU
check and the error tables. I/O parts: `S7Driver.connect/read/close/identity`. Every snap7 call
runs on the channel's single-thread executor; snap7 is reached through `s7_client_class()`, a
small `snap7.client.Client` subclass whose hooks are pinned by tests/unit/test_s7_io.py.

Addresses (Kepware syntax, case-insensitive; docs/decisions.md D-029)
    <area><type><byte>[.bit | .len][[rows][cols]]   area I/E, Q/A, M/F
    <area><byte>.<bit>                              bit, e.g. M10.3, I0.0
    DB<n>,<type><byte>[...]                         e.g. DB100,D04  DB1,REAL8  DB1,X5.6  DB1,STRING0.20
    DB<n>,DBX<byte>.<bit> / DBB / DBW / DBD         Step 7 form after the comma
    DB<n>.DBX<byte>.<bit> / DBB / DBW / DBD         Siemens-native form (also DB<n>DBD.. without separator)
    T<n>  (S5TIME -> milliseconds),  C<n> / Z<n>  (BCD counter value)
  <type>: X bit; B/BYTE, C/CHAR 8-bit; W/WORD, I/INT 16-bit; D/DWORD, DI/DINT 32-bit (integer or float);
  REAL float32; LREAL float64; LINT/LWORD 64-bit; STRING (S7 string: max len, actual len, chars).
  The type letter fixes the width; the tag dtype fixes signedness and float vs integer.

One block is one S7 "read var" request of contiguous bytes (or timers/counters) in one area/DB.
Blocks are planned for `params.max_pdu`; if the PLC negotiates a smaller PDU, `read()` splits a
block that does not fit into several requests (logged once at connect).

The server is read-only: the client subclass refuses every PDU except setup communication,
read var and SZL reads (USERDATA group 4, subfunction 1) before it reaches the socket.
"""

from __future__ import annotations

import asyncio
import functools
import math
import re
import struct
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers._cip_common import Attempt, describe_exc
from opencap.drivers.base import (
    Block,
    BlockConfigError,
    CommError,
    ConnectionLost,
    DecodeError,
    Driver,
    Plan,
)

# --- protocol facts -------------------------------------------------------------------------

READ_OVERHEAD = 18            # response: 12 header + 2 parameter + 4 data item header
MIN_PDU, MAX_PDU = 240, 960   # Kepware's range; classic S7comm PDUs are 240..960 bytes
ADDRESS_SPACE = 65536         # Kepware: byte offsets 0..65535, every value must end within it
MAX_DB = 65535
MAX_TC = 65535
MAX_STRING = 254              # S7 STRING[n]
MAX_RAW_STRING = 932          # Kepware raw strings (B y.n)
DEFAULT_PORT = 102

AREA_CODES: dict[str, int] = {"I": 0x81, "Q": 0x82, "M": 0x83, "DB": 0x84, "C": 0x1C, "T": 0x1D}
WORD_LEN = {"byte": 0x02, "counter": 0x1C, "timer": 0x1D}    # S7 transport sizes used for reads
TC_AREAS = frozenset({"T", "C"})

# Kepware Link Type -> S7 connection type (first byte of the remote TSAP). D-030.
CONNECTION_TYPES = {"PG": 1, "OP": 2, "PC": 3, "BASIC": 3, "S7BASIC": 3}
CONNECTION_TYPE_NAMES = {1: "PG", 2: "OP", 3: "PC (S7 basic)"}
DEFAULT_CONNECTION_TYPE = 3   # Kepware default "PC"

NONNORMAL_FLOAT_MODES = ("zero", "unmodified")
DTYPES = frozenset({"bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64", "string"})
_INT_DTYPES = frozenset({"int16", "uint16", "int32", "uint32", "int64", "uint64"})

_AREA_LETTERS = {"I": "I", "E": "I", "Q": "Q", "A": "Q", "M": "M", "F": "M"}
_TC_LETTERS = {"T": "T", "C": "C", "Z": "C"}

# S7 data type -> (kind, bytes per element). STRING's size depends on the length suffix.
_TYPES: dict[str, tuple[str, int]] = {
    "X": ("bit", 1), "DBX": ("bit", 1),
    "B": ("byte", 1), "BYTE": ("byte", 1), "DBB": ("byte", 1), "C": ("byte", 1), "CHAR": ("byte", 1),
    "W": ("word", 2), "WORD": ("word", 2), "DBW": ("word", 2), "I": ("word", 2), "INT": ("word", 2),
    "D": ("dword", 4), "DWORD": ("dword", 4), "DBD": ("dword", 4), "DI": ("dword", 4), "DINT": ("dword", 4),
    "REAL": ("real", 4), "LREAL": ("lreal", 8), "LINT": ("lword", 8), "LWORD": ("lword", 8),
    "STRING": ("string", 0),
}
_STEP7_TYPES = frozenset({"DBX", "DBB", "DBW", "DBD"})
_UNSUPPORTED_TYPES = {
    "DT": "DATE_AND_TIME", "DATE": "DATE", "T": "TIME", "TIME": "TIME", "TOD": "TIME_OF_DAY",
    "S5TIME": "S5TIME", "HEXSTRING": "HEXSTRING", "WSTRING": "WSTRING", "DTL": "DTL",
}
_BIT_RANGE = {"bit": 7, "byte": 7, "word": 15, "dword": 31, "lword": 63}
_NUMERIC_DTYPES: dict[str, tuple[frozenset[str], str]] = {
    "byte": (_INT_DTYPES, "B/BYTE/C/CHAR is an 8-bit value: use an integer dtype (Kepware Byte/Char)"),
    "word": (frozenset({"int16", "uint16"}), "W/WORD/I/INT is a 16-bit value: use dtype int16 or uint16"),
    "dword": (frozenset({"int32", "uint32", "float32"}),
              "D/DWORD/DI/DINT is a 32-bit value: use dtype int32, uint32 or float32"),
    "real": (frozenset({"float32"}), "REAL holds a float: use dtype float32"),
    "lreal": (frozenset({"float64"}), "LREAL holds a 64-bit float: use dtype float64"),
    "lword": (frozenset({"int64", "uint64"}), "LINT/LWORD is a 64-bit value: use dtype int64 or uint64"),
}


# --- params ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class S7Params:
    """Typed view of `Device.params` for s7 (docs/driver_params.md). Unknown keys are ignored."""

    rack: int = 0
    slot: int = 1
    max_pdu: int = 480
    link_type: int | str | None = None      # Kepware Link Type: 1 PG, 2 OP, 3 PC (default); or "PG"/"OP"/"PC"
    byte_order_big: bool = True             # false = Kepware "Little Endian": bytes of 16/32/64-bit values reversed
    max_gap: int = 16                       # unused bytes allowed between merged tags
    nonnormal_float: str = "zero"           # NaN/Inf/denormal floats: "zero" (Kepware default) or "unmodified"

    def __post_init__(self) -> None:
        for name in ("rack", "slot", "max_pdu", "max_gap"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"s7 param {name}={value!r}: expected an integer")
        if not isinstance(self.byte_order_big, bool):
            raise ValueError(f"s7 param byte_order_big={self.byte_order_big!r}: expected true or false")
        if not 0 <= self.rack <= 7:
            raise ValueError(f"s7 param rack={self.rack}: must be 0..7")
        if not 0 <= self.slot <= 31:
            raise ValueError(f"s7 param slot={self.slot}: must be 0..31")
        if not MIN_PDU <= self.max_pdu <= MAX_PDU:
            raise ValueError(f"s7 param max_pdu={self.max_pdu}: must be {MIN_PDU}..{MAX_PDU}")
        if self.max_gap < 0:
            raise ValueError(f"s7 param max_gap={self.max_gap}: must be >= 0")
        if self.nonnormal_float not in NONNORMAL_FLOAT_MODES:
            raise ValueError(f"s7 param nonnormal_float={self.nonnormal_float!r}: expected one of {NONNORMAL_FLOAT_MODES}")
        self.connection_type   # validates link_type

    @classmethod
    def from_mapping(cls, params: S7Params | Mapping[str, Any] | None) -> S7Params:
        if isinstance(params, cls):
            return params
        if params is None:
            return cls()
        names = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in params.items() if k in names and v is not None}
        if type(kwargs.get("byte_order_big")) is int and kwargs["byte_order_big"] in (0, 1):
            kwargs["byte_order_big"] = bool(kwargs["byte_order_big"])
        return cls(**kwargs)

    @property
    def connection_type(self) -> int:
        """S7 connection type for the remote TSAP: 1 PG, 2 OP, 3 PC/S7 basic (D-030)."""
        lt = self.link_type
        if lt is None:
            return DEFAULT_CONNECTION_TYPE
        if isinstance(lt, str):
            key = lt.strip().upper().replace("-", "").replace(" ", "")
            if key in CONNECTION_TYPES:
                return CONNECTION_TYPES[key]
        elif not isinstance(lt, bool) and isinstance(lt, int) and lt in CONNECTION_TYPE_NAMES:
            return lt
        raise ValueError(f"s7 param link_type={lt!r}: expected 1 (PG), 2 (OP), 3 (PC), a name, or null")

    @property
    def max_span(self) -> int:
        """Largest block in bytes for the planned PDU."""
        return self.max_pdu - READ_OVERHEAD


# --- address parser ------------------------------------------------------------------------------

@dataclass(frozen=True)
class S7Address:
    text: str                       # as written (stripped)
    area: str                       # I | Q | M | DB | T | C
    db: int                         # DB number, 0 outside DB
    start: int                      # byte offset; timer/counter number for T/C
    kind: str                       # bit byte word dword real lreal lword string rawstring timer counter
    size: int                       # bytes per element (strings: whole string)
    dtype: str
    count: int = 1                  # elements (arrays)
    bit: int | None = None          # bit inside the value (bool)
    dims: tuple[int, ...] = ()
    str_len: int | None = None      # string length in characters

    @property
    def area_code(self) -> int:
        return AREA_CODES[self.area]

    @property
    def nbytes(self) -> int:
        return self.size * self.count

    @property
    def units(self) -> int:
        """Size on the wire in request units: timers/counters for T/C, bytes otherwise."""
        return self.count if self.area in TC_AREAS else self.nbytes

    @property
    def end(self) -> int:
        """Exclusive end, in request units."""
        return self.start + self.units

    @property
    def key(self) -> tuple:
        """Identity of the data read (ignores the spelling): equal keys are read and decoded once."""
        return (self.area, self.db, self.start, self.kind, self.size, self.count, self.dtype,
                -1 if self.bit is None else self.bit, self.dims, self.str_len or 0)


_DB_RE = re.compile(r"DB(?P<db>\d+)(?P<sep>[,.]?)(?P<rest>.*)")
_AREA_RE = re.compile(r"(?P<area>PI|PE|PQ|PA|I|E|Q|A|M|F)(?P<rest>.*)")
_TC_RE = re.compile(r"(?P<tc>[TCZ])(?P<num>\d+)(?P<suffix>\.\d+)?(?P<dims>(?:\[\s*\d+\s*\])*)")
_REST_RE = re.compile(r"(?P<type>[A-Z]*)(?P<off>\d+)(?:\.(?P<suffix>\d+))?(?P<dims>(?:\[\s*\d+\s*\])*)")
_DIM_RE = re.compile(r"\[\s*(\d+)\s*\]")


def parse_s7_address(address: str, dtype: str, params: S7Params | Mapping[str, Any] | None = None) -> S7Address:
    """Parse a Kepware S7 address for a tag of `dtype`. Raises ValueError with a clear reason."""
    p = S7Params.from_mapping(params)

    def bad(reason: str) -> ValueError:
        return ValueError(f"s7 address {address!r} ({dtype}): {reason}")

    if not isinstance(address, str):
        raise bad("address must be a string")
    text = address.strip()
    if not text:
        raise bad("empty address")
    if dtype not in DTYPES:
        raise bad(f"unknown dtype {dtype!r}")
    up = text.upper()

    if (m := _TC_RE.fullmatch(up)) is not None:
        return _parse_tc(text, m, dtype, bad)

    if (m := _DB_RE.fullmatch(up)) is not None and m["rest"]:
        db = int(m["db"])
        if not 1 <= db <= MAX_DB:
            raise bad(f"DB number {db} out of range 1..{MAX_DB}")
        rest = _REST_RE.fullmatch(m["rest"])
        if rest is None:
            raise bad("not an S7 address (expected DB<n>,<type><byte>, e.g. DB1,REAL0, or DB<n>.DBD<byte>)")
        if m["sep"] != "," and rest["type"] not in _STEP7_TYPES:
            sep = m["sep"] or ""
            raise bad(f"after 'DB{db}{sep}' the Siemens form needs DBX/DBB/DBW/DBD, e.g. DB{db}.DBW0 "
                      f"(Kepware form: DB{db},{rest['type'] or 'W'}{rest['off']})")
        return _parse_rest(text, "DB", db, rest, dtype, p, bad)

    if (m := _AREA_RE.fullmatch(up)) is not None and m["rest"]:
        if m["area"] in ("PI", "PE", "PQ", "PA"):
            raise bad("peripheral I/O (PI/PE/PQ/PA) is not supported by python-snap7; "
                      "read the process image (I/Q) instead")
        rest = _REST_RE.fullmatch(m["rest"])
        if rest is None:
            raise bad("not an S7 address (expected e.g. MD232, MW10, M10.3, IB0, DB1,REAL0)")
        if rest["type"] in _STEP7_TYPES:
            raise bad(f"{rest['type']} is only valid in a data block, e.g. DB1,{rest['type']}0")
        return _parse_rest(text, _AREA_LETTERS[m["area"]], 0, rest, dtype, p, bad)

    raise bad("not an S7 address (expected e.g. MD232, M10.3, DB100,D04, DB1.DBD0, C0, T0)")


def _dims(text: str, bad) -> tuple[int, ...]:
    dims = tuple(int(x) for x in _DIM_RE.findall(text))
    if len(dims) > 2:
        raise bad("at most 2 array dimensions ([rows][cols])")
    if any(d < 1 for d in dims):
        raise bad("array dimension must be >= 1")
    return dims


def _parse_tc(text: str, m: re.Match, dtype: str, bad) -> S7Address:
    area = _TC_LETTERS[m["tc"]]
    what = "timer" if area == "T" else "counter"
    num = int(m["num"])
    if m["suffix"]:
        raise bad(f"{what} addresses take no .suffix (T<n>, C<n>)")
    if m["dims"]:
        raise bad(f"arrays are not supported on timers and counters ({what})")
    if num > MAX_TC:
        raise bad(f"{what} number {num} out of range 0..{MAX_TC}")
    if area == "T" and dtype not in ("uint32", "int32"):
        raise bad("a timer reads as milliseconds (S5TIME scaled): use dtype uint32 or int32 (Kepware DWord/Long)")
    if area == "C" and dtype not in _INT_DTYPES - {"int64", "uint64"}:
        raise bad("a counter reads as its BCD-decoded value: use dtype uint16 or int16 (Kepware Word/Short)")
    return S7Address(text=text, area=area, db=0, start=num, kind=what, size=2, dtype=dtype)


def _parse_rest(text: str, area: str, db: int, m: re.Match, dtype: str, p: S7Params, bad) -> S7Address:
    tname, offset, suffix = m["type"], int(m["off"]), m["suffix"]
    dims = _dims(m["dims"], bad)
    if tname in _UNSUPPORTED_TYPES:
        raise bad(f"S7 data type {tname} ({_UNSUPPORTED_TYPES[tname]}) is not supported")
    if tname == "":
        if suffix is None:
            if dtype == "bool":
                raise bad(f"a bool needs a bit number, e.g. {text}.0")
            raise bad("needs an S7 data type, e.g. MW10, or a bit number, e.g. M10.3")
        kind, size = "bit", 1                       # M10.3, I0.0
    elif tname in _TYPES:
        kind, size = _TYPES[tname]
    else:
        raise bad(f"unknown S7 data type {tname!r}")

    bit: int | None = None
    str_len: int | None = None
    if dtype == "bool":
        if dims:
            raise bad("arrays of bools are not supported (Kepware)")
        if kind not in _BIT_RANGE:
            raise bad(f"{tname} has no bit access; use X, B, W, D or LWORD with .bit")
        if suffix is None:
            raise bad(f"a bool needs a bit number, e.g. {text}.0" if kind != "bit" else "a bool needs a bit number")
        bit = int(suffix)
        if bit > _BIT_RANGE[kind]:
            hint = f"; use {_hint_next_byte(text, offset, bit)}" if kind == "bit" else ""
            raise bad(f"bit {bit} out of range 0..{_BIT_RANGE[kind]}{hint}")
    elif dtype == "string":
        if dims:
            raise bad("arrays of strings are not supported (Kepware)")
        if kind == "string":                         # S7 STRING[n]: max length, actual length, chars
            str_len = int(suffix) if suffix is not None else (MAX_STRING if p.max_pdu >= 480 else 210)
            if not 1 <= str_len <= MAX_STRING:
                raise bad(f"string length {str_len} out of range 1..{MAX_STRING}")
            size = str_len + 2
        elif kind == "byte":                         # raw string, NUL-terminated
            if suffix is None:
                raise bad(f"a raw string needs a length, e.g. {text}.10")
            str_len = int(suffix)
            if not 1 <= str_len <= MAX_RAW_STRING:
                raise bad(f"string length {str_len} out of range 1..{MAX_RAW_STRING}")
            kind, size = "rawstring", str_len
        else:
            raise bad("a string needs STRING<byte>.<len> (S7 string) or B<byte>.<len> (raw string)")
    else:
        if kind == "bit":
            raise bad("a bit address (X or <byte>.<bit>) needs dtype bool")
        if kind == "string":
            raise bad("STRING needs dtype string")
        if suffix is not None:
            raise bad("a .n suffix is a bit number (dtype bool) or a string length (dtype string)")
        allowed, reason = _NUMERIC_DTYPES[kind]
        if dtype not in allowed:
            raise bad(reason)

    count = math.prod(dims) if dims else 1
    nbytes = size * count
    if offset + nbytes > ADDRESS_SPACE:
        raise bad(f"needs bytes {offset}..{offset + nbytes - 1}, past the end of the address space "
                  f"({ADDRESS_SPACE - 1})")
    return S7Address(text=text, area=area, db=db, start=offset, kind=kind, size=size, dtype=dtype,
                     count=count, bit=bit, dims=dims, str_len=str_len)


def _hint_next_byte(text: str, offset: int, bit: int) -> str:
    return f"byte {offset + bit // 8} bit {bit % 8}"


# --- blocks and planner ---------------------------------------------------------------------------

@dataclass(frozen=True)
class S7Item:
    """One distinct piece of data inside a block, served to every tag name that reads it."""

    names: tuple[str, ...]
    offset: int                     # byte offset of the item in the block's data
    addr: S7Address


@dataclass(kw_only=True)
class S7Block(Block):
    area: str
    db: int
    start: int                      # byte offset, or first timer/counter
    count: int                      # bytes, or timers/counters
    items: list[S7Item] = field(default_factory=list)

    @property
    def unit(self) -> str:
        return {"T": "timer", "C": "counter"}.get(self.area, "byte")

    @property
    def area_code(self) -> int:
        return AREA_CODES[self.area]

    @property
    def word_len(self) -> int:
        return WORD_LEN[self.unit]

    @property
    def nbytes(self) -> int:
        return self.count * (2 if self.area in TC_AREAS else 1)

    def describe(self) -> str:
        name = f"DB{self.db}" if self.area == "DB" else self.area
        last = self.start + self.count - 1
        where = str(self.start) if last == self.start else f"{self.start}..{last}"
        noun = self.unit + ("s" if self.count != 1 else "")
        return f"{name} {where} ({self.count} {noun})"


def plan_blocks(
    parsed: Iterable[tuple[str, S7Address]], params: S7Params | Mapping[str, Any] | None = None,
) -> tuple[list[S7Block], dict[str, str]]:
    """Merge parsed tags into wire blocks. Returns (blocks, invalid).

    Per (area, DB), greedy over items sorted by start: an item joins the open block when it
    starts at most `max_gap` bytes after the block end and the block stays within
    `max_pdu - 18` bytes (timers/counters: 2 bytes each). Output is sorted and independent of
    input order.
    """
    p = S7Params.from_mapping(params)
    span_bytes = p.max_span
    invalid: dict[str, str] = {}
    names_by_key: dict[tuple, list[str]] = {}
    addr_by_key: dict[tuple, S7Address] = {}
    for name, a in parsed:
        if a.nbytes > span_bytes:
            invalid[name] = (
                f"s7 address {a.text!r} ({a.dtype}): needs {a.nbytes} bytes in one request, more than "
                f"the {span_bytes} a PDU of {p.max_pdu} bytes carries (max_pdu={p.max_pdu})"
            )
            continue
        names_by_key.setdefault(a.key, []).append(name)
        addr_by_key.setdefault(a.key, a)

    groups: dict[tuple[str, int], list[S7Address]] = {}
    for a in addr_by_key.values():
        groups.setdefault((a.area, a.db), []).append(a)

    blocks: list[S7Block] = []
    for (area, db), addrs in sorted(groups.items()):
        tc = area in TC_AREAS
        span = span_bytes // 2 if tc else span_bytes
        gap = p.max_gap // 2 if tc else p.max_gap
        run: list[S7Address] = []
        end = 0

        def close() -> None:
            start = run[0].start
            unit_bytes = 2 if tc else 1
            items = [S7Item(names=tuple(sorted(names_by_key[a.key])), offset=(a.start - start) * unit_bytes, addr=a)
                     for a in run]
            blocks.append(S7Block(area=area, db=db, start=start, count=end - start, items=items,
                                  tags=sorted(n for it in items for n in it.names)))

        for a in sorted(addrs, key=lambda x: (x.start, x.end, x.key)):
            if run and a.start - end <= gap and max(end, a.end) - run[0].start <= span:
                run.append(a)
                end = max(end, a.end)
                continue
            if run:
                close()
            run, end = [a], a.end
        if run:
            close()
    return blocks, dict(sorted(invalid.items()))


# --- decoder ----------------------------------------------------------------------------------------

class _Fail(Exception):
    pass


def bcd_to_int(word: int) -> int:
    """Packed BCD (4 digits) -> int. Raises ValueError on a nibble above 9."""
    value, mult = 0, 1
    for shift in (0, 4, 8, 12):
        digit = (word >> shift) & 0xF
        if digit > 9:
            raise ValueError(f"0x{word:04X} is not BCD (digit {digit:X})")
        value += digit * mult
        mult *= 10
    return value


S5TIME_BASE_MS = (10, 100, 1000, 10_000)


def s5time_to_ms(word: int) -> int:
    """S5TIME (bits 12-13 time base, bits 0-11 three BCD digits) -> milliseconds."""
    try:
        value = bcd_to_int(word & 0x0FFF)
    except ValueError:
        raise ValueError(f"S5TIME 0x{word:04X}: digits are not BCD") from None
    return value * S5TIME_BASE_MS[(word >> 12) & 0x3]


def decode_block(block: S7Block, data: bytes | bytearray | Iterable[int],
                 params: S7Params | Mapping[str, Any] | None = None) -> dict[str, Any | DecodeError]:
    """Decode one block's bytes into {tag name: value or DecodeError}. No scaling."""
    p = S7Params.from_mapping(params)
    buf = bytes(data)
    out: dict[str, Any | DecodeError] = {}
    for item in block.items:
        try:
            value: Any = _decode_item(item, buf, p)
        except _Fail as exc:
            value = DecodeError(f"{item.addr.text} ({item.addr.dtype}): {exc}")
        for name in item.names:
            out[name] = _copy(value)
    return out


def _copy(value: Any) -> Any:
    return [_copy(v) for v in value] if isinstance(value, list) else value


def _decode_item(item: S7Item, buf: bytes, p: S7Params) -> Any:
    a = item.addr
    lo, hi = item.offset, item.offset + a.nbytes
    if len(buf) < hi:
        raise _Fail(f"short reply: need bytes up to {hi - 1} of the block, got {len(buf)}")
    chunk = buf[lo:hi]
    if a.kind == "bit":
        return bool((chunk[0] >> (a.bit or 0)) & 1)
    if a.kind == "rawstring":
        nul = chunk.find(0)
        return (chunk if nul < 0 else chunk[:nul]).decode("latin-1")
    if a.kind == "string":
        length = min(chunk[1], a.str_len or 0)
        text = chunk[2:2 + length]
        nul = text.find(0)
        return (text if nul < 0 else text[:nul]).decode("latin-1")
    if a.kind in ("timer", "counter"):
        word = int.from_bytes(chunk, "big")         # S5TIME / BCD: the PLC's format, not user data
        try:
            return s5time_to_ms(word) if a.kind == "timer" else bcd_to_int(word)
        except ValueError as exc:
            raise _Fail(str(exc)) from None
    values = []
    for i in range(a.count):
        elem = chunk[i * a.size:(i + 1) * a.size]
        if not p.byte_order_big:
            elem = elem[::-1]
        u = int.from_bytes(elem, "big")
        if a.bit is not None:
            return bool((u >> a.bit) & 1)
        values.append(_number(u, a.size, a.dtype, p))
    return _shape(values, a.dims)


def _number(u: int, size: int, dtype: str, p: S7Params) -> int | float:
    if dtype == "float32":
        return _float(u, 32, p)
    if dtype == "float64":
        return _float(u, 64, p)
    bits = size * 8
    if dtype.startswith("int") and u & (1 << (bits - 1)):
        return u - (1 << bits)
    return u


def _float(u: int, width: int, p: S7Params) -> float:
    if width == 32:
        exp, mant, fmt = (u >> 23) & 0xFF, u & 0x7FFFFF, ">f"
        nonnormal = exp == 0xFF or (exp == 0 and mant != 0)
    else:
        exp, mant, fmt = (u >> 52) & 0x7FF, u & ((1 << 52) - 1), ">d"
        nonnormal = exp == 0x7FF or (exp == 0 and mant != 0)
    if nonnormal and p.nonnormal_float == "zero":
        return 0.0
    return struct.unpack(fmt, u.to_bytes(width // 8, "big"))[0]


def _shape(values: list[Any], dims: tuple[int, ...]) -> Any:
    if not dims:
        return values[0]
    if len(dims) == 1:
        return values
    cols = dims[1]
    return [values[r * cols:(r + 1) * cols] for r in range(dims[0])]


# --- read-only guard and error tables (pure) ---------------------------------------------------------

PDU_JOB, PDU_USERDATA = 0x01, 0x07
FN_SETUP_COMMUNICATION, FN_READ_VAR = 0xF0, 0x04
USERDATA_GROUP_SZL, USERDATA_READ_SZL = 0x04, 0x01
JOB_FUNCTION_NAMES = {
    0x04: "read var", 0x05: "write var", 0x1A: "request download", 0x1B: "download block",
    0x1C: "download ended", 0x1D: "start upload", 0x1E: "upload", 0x1F: "end upload",
    0x28: "PLC control", 0x29: "PLC stop", 0xF0: "setup communication",
}


class S7ReadOnlyViolation(Exception):
    """A PDU other than setup communication, read var or SZL read was about to be sent."""


def check_read_only(pdu: bytes | bytearray) -> str | None:
    """None if this S7 request PDU only reads; else why it must not be sent."""
    if len(pdu) < 11 or pdu[0] != 0x32:
        return "not an S7 request PDU"
    pdu_type = pdu[1]
    if pdu_type == PDU_JOB:
        fn = pdu[10]
        if fn in (FN_SETUP_COMMUNICATION, FN_READ_VAR):
            return None
        return f"S7 job 0x{fn:02X} ({JOB_FUNCTION_NAMES.get(fn, 'unknown')}) refused: opencap is read-only"
    if pdu_type == PDU_USERDATA:
        if len(pdu) < 17:
            return "short S7 USERDATA PDU"
        group, sub = pdu[15] & 0x0F, pdu[16]
        if group == USERDATA_GROUP_SZL and sub == USERDATA_READ_SZL:
            return None
        return f"S7 USERDATA group {group} subfunction 0x{sub:02X} refused: only SZL reads are sent"
    return f"S7 PDU type 0x{pdu_type:02X} refused: opencap is read-only"


RETURN_CODE_TEXT = {
    0x00: "reserved", 0x01: "hardware fault", 0x03: "accessing the object not allowed",
    0x05: "address out of range", 0x06: "data type not supported", 0x07: "data type inconsistent",
    0x0A: "object does not exist", 0xFF: "success",
}
# The PLC permanently rejects the block: wrong DB/area/offset, or no access (S7-1200/1500
# optimized block access, protection). Retrying cannot help.
CONFIG_RETURN_CODES = frozenset({0x03, 0x05, 0x06, 0x07, 0x0A})
# Header-level errors (error class << 8 | code) that mean the same for every read of the block.
CONFIG_HEADER_ERRORS = {0x8104: "function not supported in this context (S7-1200/1500: is PUT/GET access enabled?)"}


def return_code_text(rc: int) -> str:
    return f"item return code 0x{rc:02X} ({RETURN_CODE_TEXT.get(rc, 'unknown')})"


# --- I/O: snap7 client subclass ----------------------------------------------------------------------

_log = structlog.get_logger("opencap.drivers.s7")


@functools.cache
def s7_client_class() -> type:
    """python-snap7 3.x `Client` subclass used by the driver (built lazily, D-002):

    - `_connect`: ISO-on-TCP connect and PDU negotiation with our timeout (snap7 uses a fixed
      5 s), no heartbeat thread, no multi-read optimizer, no auto-reconnect.
    - `_send_data`: every PDU passes `check_read_only` first; a refused one never reaches the socket.
    - `oc_read`: one read var request; returns (item return code, data) instead of raising on a
      refused item, so the driver can map return codes to `BlockConfigError`.
    """
    from snap7.client import Client
    from snap7.connection import ISOTCPConnection
    from snap7.datatypes import S7Area, S7WordLen
    from snap7.type import Parameter

    class OcS7Client(Client):
        def __init__(self, timeout_s: float, pdu_request: int, connection_type: int) -> None:
            super().__init__()                         # auto_reconnect off, heartbeat off by default
            self.oc_timeout_s = timeout_s
            self.use_optimizer = False
            self.pdu_length = pdu_request              # requested in setup communication
            self._params[Parameter.PDURequest] = pdu_request
            self.set_connection_type(connection_type)
            self.oc_sent = 0
            self.oc_refused = 0

        def _connect(self, address: str, rack: int, slot: int, tcp_port: int) -> Any:
            self.host, self.port, self.rack, self.slot = address, tcp_port, rack, slot
            self._params[Parameter.RemotePort] = tcp_port
            conn = ISOTCPConnection(host=address, port=tcp_port, local_tsap=self.local_tsap,
                                    remote_tsap=self.remote_tsap)
            self.connection = conn
            try:
                conn.connect(timeout=self.oc_timeout_s)
                self._setup_communication()
            except BaseException:
                self.disconnect()
                raise
            self.connected = True
            self._is_alive = True
            return self

        def _send_data(self, conn: Any, request: bytes) -> None:
            problem = check_read_only(request)
            if problem is not None:
                self.oc_refused += 1
                raise S7ReadOnlyViolation(problem)
            self.oc_sent += 1
            super()._send_data(conn, request)

        def oc_read(self, area_code: int, db: int, start: int, word_len: int, count: int) -> tuple[int, bytes]:
            request = self.protocol.build_read_request(
                area=S7Area(area_code), db_number=db, start=start, word_len=S7WordLen(word_len), count=count)
            response = self._send_receive(request)
            item = response.get("data")
            rc = item.get("return_code") if isinstance(item, dict) else None
            if rc != 0xFF:
                return (rc if isinstance(rc, int) else -1), b""
            return 0xFF, bytes(self.protocol.extract_read_data(response, S7WordLen(word_len), count))

    return OcS7Client


# --- I/O: driver -------------------------------------------------------------------------------------

@dataclass(eq=False)
class _Session:
    """One open snap7 connection (touched by snap7 calls only on the executor)."""

    client: Any
    pdu: int                        # negotiated PDU length
    dead: bool = False
    closed: bool = False


class _ReadFailure(Exception):
    def __init__(self, reason: str, *, kind: str = "comm") -> None:
        super().__init__(reason)
        self.kind = kind            # "lost" | "config" | "comm"


def _split(start: int, count: int, chunk: int) -> list[tuple[int, int]]:
    parts = []
    while count > 0:
        n = min(count, chunk)
        parts.append((start, n))
        start, count = start + n, count - n
    return parts


def _text(raw: Any) -> str | None:
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).split(b"\x00", 1)[0].decode("latin-1")
    text = str(raw).strip() if raw is not None else ""
    return text or None


class S7Driver(Driver):
    """Siemens S7 over python-snap7 3.x (sync, channel executor). One attempt per call:
    retries, backoff and reconnects belong to the ChannelWorker."""

    name: ClassVar[str] = "s7"
    is_async: ClassVar[bool] = False

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)
        self.params = S7Params.from_mapping(device.params)       # ValueError on bad params
        self._session: _Session | None = None
        self._rejected: set[str] = set()
        self._pdu_logged: int | None = None

    # --- pure ---

    def plan(self, tags: list[Tag]) -> Plan:
        parsed: list[tuple[str, S7Address]] = []
        invalid: dict[str, str] = {}
        for tag in tags:
            if not tag.enabled:
                continue
            try:
                parsed.append((tag.name, parse_s7_address(tag.address, tag.dtype, self.params)))
            except ValueError as exc:
                invalid[tag.name] = str(exc)
        blocks, too_big = plan_blocks(parsed, self.params)
        invalid.update(too_big)
        return Plan(blocks=list(blocks), invalid=dict(sorted(invalid.items())))

    def decode(self, block: S7Block, data: bytes) -> dict[str, Any | DecodeError]:
        return decode_block(block, data, self.params)

    # --- I/O ---

    @property
    def connected(self) -> bool:
        """True while the connection is believed usable. snap7 notices a peer close only on the
        next request: that read raises ConnectionLost."""
        s = self._session
        return s is not None and not s.dead

    @property
    def timeout_s(self) -> float:
        return max(self.device.timeout_ms, 1) / 1000.0

    @property
    def negotiated_pdu(self) -> int | None:
        s = self._session
        return None if s is None else s.pdu

    def connect_timeout_s(self) -> float | None:
        """TCP connect, COTP connection request and setup communication: three waits."""
        return 3 * self.timeout_s + 1.0

    @property
    def _where(self) -> str:
        return f"{self.device.id} ({self.device.host}:{self.device.port})"

    async def connect(self) -> None:
        """ISO-on-TCP connect (TSAP from rack/slot/link type) and PDU negotiation. An open
        connection is closed first. Raises ConnectionLost on any failure."""
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
            reason = describe_exc(exc)
            _log.debug("s7 connect", device=self.device.id, result="failed", reason=reason,
                       ms=round((time.monotonic() - started) * 1000, 1))
            raise ConnectionLost(f"{self._where}: connect failed: {reason}") from exc
        if session is None:    # pragma: no cover - only after abandon(), i.e. cancelled
            raise ConnectionLost(f"{self._where}: connect abandoned")
        self._session = session
        _log.debug("s7 connect", device=self.device.id, result="ok", pdu=session.pdu,
                   ms=round((time.monotonic() - started) * 1000, 1))
        if session.pdu < self.params.max_pdu and self._pdu_logged != session.pdu:
            self._pdu_logged = session.pdu
            _log.info("s7.pdu_negotiated", device=self.device.id, requested=self.params.max_pdu,
                      negotiated=session.pdu,
                      note=f"blocks above {session.pdu - READ_OVERHEAD} bytes are read in several requests; "
                           f"set params.max_pdu={session.pdu} to plan blocks that fit")

    async def close(self) -> None:
        """Close the connection (COTP disconnect). Idempotent, never raises."""
        session, self._session = self._session, None
        if session is None or session.closed:
            return
        session.dead = True
        try:
            await self._run_sync(self._dispose_sync, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:     # executor missing or shut down: drop the socket at least
            _log.debug("s7 close error", device=self.device.id, error=describe_exc(exc))
            _abort_socket(session)

    async def read(self, block: Block) -> dict[str, TagValue]:
        """Read one block: one read var request (several if the negotiated PDU is smaller than
        the planned one). Raises BlockConfigError (item return code 0x03/0x05/0x06/0x07/0x0A, or
        header error 0x8104), ConnectionLost (not connected, refused/reset/closed) or CommError
        (reply timeout, after closing; other errors). Undecodable values: bad_decode."""
        if not isinstance(block, S7Block):
            raise TypeError(f"s7 cannot read {type(block).__name__}")
        session = self._session
        if session is None or session.dead:
            raise ConnectionLost(f"{self._where}: not connected")
        try:
            data, ts = await self._run_sync(self._read_sync, session, block)
        except _ReadFailure as exc:
            if session.dead and self._session is session:
                self._session = None
            if exc.kind == "config":
                self._log_rejected(block, str(exc))
                raise BlockConfigError(f"{self._where} {block.describe()}: {exc}") from exc.__cause__
            err = ConnectionLost if exc.kind == "lost" else CommError
            raise err(f"{self._where} {block.describe()}: {exc}") from exc.__cause__
        return self._values(block, data, ts)

    async def identity(self) -> dict[str, Any]:
        """CPU identity from SZL 0x001C (component identification), 0x0011 (order code and
        firmware) and 0x0424 (operating mode), plus the negotiated PDU and connection type.
        {} when not connected. Never raises: a failed SZL read is reported as `<name>_error`."""
        s = self._session
        if s is None or s.dead:
            return {}
        p = self.params
        base = {"pdu_requested": p.max_pdu, "pdu_negotiated": s.pdu, "rack": p.rack, "slot": p.slot,
                "connection_type": CONNECTION_TYPE_NAMES[p.connection_type],
                "byte_order": "big" if p.byte_order_big else "little"}
        try:
            return {**base, **await self._run_sync(self._identity_sync, s)}
        except Exception as exc:
            return {**base, "error": describe_exc(exc)}

    # --- executor side (every snap7 call happens below, on the channel executor) ---

    def _open_sync(self, attempt: Attempt) -> _Session | None:
        if attempt.abandoned:
            return None
        p = self.params
        client = s7_client_class()(self.timeout_s, p.max_pdu, p.connection_type)
        try:
            client.connect(self.device.host, p.rack, p.slot, self.device.port)
            pdu = int(client.get_pdu_length())
        except BaseException:
            _disconnect_quietly(client)
            raise
        session = _Session(client, pdu)
        if not attempt.finish(session):
            self._dispose_sync(session)
            return None
        return session

    def _read_sync(self, session: _Session, block: S7Block) -> tuple[bytes, datetime]:
        from snap7.error import S7ConnectionError, S7ProtocolError, S7TimeoutError

        client = session.client
        unit_bytes = 2 if block.area in TC_AREAS else 1
        chunk = max(1, (session.pdu - READ_OVERHEAD) // unit_bytes)
        parts = _split(block.start, block.count, chunk)
        started = time.monotonic()
        data = bytearray()
        try:
            for start, count in parts:
                rc, raw = client.oc_read(block.area_code, block.db, start, block.word_len, count)
                if rc != 0xFF:
                    kind = "config" if rc in CONFIG_RETURN_CODES else "comm"
                    raise _ReadFailure(return_code_text(rc), kind=kind)
                data += raw
        except _ReadFailure:
            raise
        except S7TimeoutError as exc:
            self._dispose_sync(session)
            raise _ReadFailure(f"no reply within {self.device.timeout_ms} ms; connection closed") from exc
        except (S7ConnectionError, OSError) as exc:
            self._dispose_sync(session)
            raise _ReadFailure(f"connection lost: {describe_exc(exc)}", kind="lost") from exc
        except S7ProtocolError as exc:
            code = getattr(exc, "error_code", None)
            if code in CONFIG_HEADER_ERRORS:
                raise _ReadFailure(f"S7 error 0x{code:04X}: {CONFIG_HEADER_ERRORS[code]}", kind="config") from exc
            if not code:                     # malformed or out-of-sequence reply: stream state unknown
                self._dispose_sync(session)
                raise _ReadFailure(f"bad reply: {describe_exc(exc)}; connection closed") from exc
            raise _ReadFailure(describe_exc(exc)) from exc
        except S7ReadOnlyViolation as exc:   # pragma: no cover - reads are never refused
            raise _ReadFailure(str(exc)) from exc
        except Exception as exc:
            self._dispose_sync(session)
            raise _ReadFailure(f"snap7 read failed: {describe_exc(exc)}; connection closed") from exc
        _log.debug("s7 read", device=self.device.id, block=block.describe(), requests=len(parts),
                   ms=round((time.monotonic() - started) * 1000, 1))
        return bytes(data), datetime.now(UTC)

    def _identity_sync(self, session: _Session) -> dict[str, Any]:
        from snap7.error import S7ConnectionError, S7TimeoutError
        from snap7.szl import parse_cpu_info_szl, parse_cpu_state_szl, parse_order_code_szl

        client = session.client
        out: dict[str, Any] = {}

        def cpu_info() -> None:
            info = parse_cpu_info_szl(client.read_szl(0x001C, 0))
            out.update({"module_type_name": _text(info.ModuleTypeName), "serial_number": _text(info.SerialNumber),
                        "as_name": _text(info.ASName), "module_name": _text(info.ModuleName),
                        "copyright": _text(info.Copyright)})

        def order_code() -> None:
            oc = parse_order_code_szl(client.read_szl(0x0011, 0))
            out["order_code"] = _text(oc.OrderCode)
            if oc.V1 or oc.V2 or oc.V3:
                out["firmware"] = f"V{oc.V1}.{oc.V2}.{oc.V3}"

        def cpu_state() -> None:
            state = parse_cpu_state_szl(client.read_szl(0x0424, 0))
            out["cpu_state"] = {"S7CpuStatusRun": "run", "S7CpuStatusStop": "stop"}.get(state, "unknown")

        for key, fn in (("cpu_info", cpu_info), ("order_code", order_code), ("cpu_state", cpu_state)):
            try:
                fn()
            except (S7TimeoutError, S7ConnectionError, OSError) as exc:
                self._dispose_sync(session)
                out[f"{key}_error"] = f"{describe_exc(exc)}; connection closed"
                break
            except Exception as exc:
                out[f"{key}_error"] = describe_exc(exc)
        return out

    def _dispose_sync(self, session: _Session) -> None:
        session.dead = True
        if session.closed:
            return
        session.closed = True
        _disconnect_quietly(session.client)
        _log.debug("s7 close", device=self.device.id)

    def _close_later(self, session: _Session) -> None:
        session.dead = True
        try:
            assert self._executor is not None
            self._executor.submit(self._dispose_sync, session)
        except Exception:
            _abort_socket(session)

    # --- loop side, no snap7 calls ---

    def _values(self, block: S7Block, data: bytes, ts: datetime) -> dict[str, TagValue]:
        decoded = decode_block(block, data, self.params)
        out: dict[str, TagValue] = {}
        for name in block.tags:
            value = decoded.get(name, DecodeError(f"{block.describe()}: tag {name!r} is not in the block items"))
            if isinstance(value, DecodeError):
                _log.debug("s7 decode error", device=self.device.id, tag=name, reason=value.reason)
                out[name] = TagValue(None, "bad_decode", ts)
            else:
                out[name] = TagValue(value, "good", ts)
        return out

    def _log_rejected(self, block: S7Block, reason: str) -> None:
        key = block.describe()
        if key in self._rejected:
            return
        self._rejected.add(key)
        _log.warning("s7.block_rejected", device=self.device.id, block=key, tags=block.tags[:20],
                     count=len(block.tags), reason=reason, quality="bad_config")


def _disconnect_quietly(client: Any) -> None:
    try:
        client.disconnect()
    except Exception as exc:   # pragma: no cover - snap7 disconnect swallows socket errors
        _log.debug("s7 close error", error=describe_exc(exc))


def _abort_socket(session: _Session) -> None:
    """Last resort when the executor is gone: close the TCP socket without COTP traffic."""
    session.dead = session.closed = True
    conn = getattr(session.client, "connection", None)
    sock = getattr(conn, "socket", None)
    try:
        if sock is not None:
            sock.close()
    except Exception:   # pragma: no cover
        pass
