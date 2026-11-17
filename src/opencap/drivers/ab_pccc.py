"""Allen-Bradley PCCC driver: SLC 500 / MicroLogix / PLC-5 data-table addresses,
read with pycomm3 `SLCDriver` over EtherNet/IP (PLAN.md section 5.1, `ab_pccc`;
docs/phase3_contracts.md §1).

Pure parts (no pycomm3 import, D-002): address parsing, block planning, decoding of the
values pycomm3 returns, the connection path, PCCC reply status parsing and error mapping,
and parsing of the processor's file directory.

I/O parts (Phase 3): `AbPcccDriver.connect/read/close/identity`. Every pycomm3 call runs on
the channel's single-thread executor. pycomm3 is reached through `slc_driver_class()`, a
small `SLCDriver` subclass whose hooks are pinned by tests/unit/test_ab_pccc_io.py. One block
is one `plc.read(block.address)` call, whose value goes through `decode_block`.

pycomm3 1.2.16 facts this module is built on (see docs/decisions.md):
- `SLCDriver.read(*addresses)` sends one PCCC request per address, so one block is one
  wire request.
- The request carries the byte count, file number and element number as one byte each:
  at most 255 data bytes per read, files 1-255, elements 0-255.
- pycomm3's own bit handling is not used: `B3/17` (bit-number form) decodes the wrong
  bit, and `I:0/19` (bit > 15) is rejected. Bits are read as whole words and decoded here.
- T/C addresses cannot carry an element count, and pycomm3 returns only the selected
  sub-element, so each T/C sub-element is its own read.
- MicroLogix function files (RTC, HSC, ...) are not supported by SLCDriver.
- A read reply's status reaches the caller only as text in `Tag.error` (PCCC STS byte only;
  any CIP-level error becomes "Unknown Status"), so the subclass keeps each reply's raw
  status bytes (encapsulation, CIP general status, PCCC STS and EXT STS).
- `get_file_directory()` prints to stdout, loops forever on an empty chunk and its layouts
  are marked untested in pycomm3; the driver uses its size read and chunk reader with a
  request budget, parses the directory itself and verifies it on the wire before trusting it.

The server is read-only: nothing here builds a write; the subclass refuses every PCCC
command except the three reads it needs, pycomm3's write paths and CIP Set/Write services.
"""

from __future__ import annotations

import asyncio
import functools
import math
import re
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers._cip_common import (
    WRITE_SERVICES,
    Attempt,
    describe_exc,
    is_comm_error,
    is_timeout,
    oo_socket_class,
)
from opencap.drivers.base import (
    Block,
    BlockConfigError,
    CommError,
    ConnectionLost,
    DecodeError,
    Driver,
    Plan,
)


# Bytes per element, as pycomm3 `PCCC_DATA_SIZE`.
ELEM_SIZE: dict[str, int] = {
    "N": 2, "B": 2, "S": 2, "I": 2, "O": 2, "L": 4, "F": 4, "T": 6, "C": 6, "ST": 84,
}
WORD_FILES = frozenset({"N", "B", "S", "I", "O"})          # 16-bit elements
TC_FIELDS: dict[str, tuple[str, ...]] = {
    "T": ("ACC", "PRE", "EN", "TT", "DN"),
    "C": ("ACC", "PRE", "CU", "CD", "DN", "OV", "UN", "UA"),
}
TC_WORD_FIELDS = frozenset({"ACC", "PRE"})
STRING_MAX_COUNT = 2           # pycomm3 ST regex accepts only {1} or {2}
PCCC_MAX_BYTES = 255           # one-byte size field in the PCCC read request
MAX_FILE = 255
MAX_ELEMENT = 255

# MicroLogix 1100/1400 function files. pycomm3 SLCDriver has no address form for them.
FUNCTION_FILES = frozenset({
    "RTC", "RTA", "HSC", "PTO", "PTOX", "PWM", "PWMX", "STI", "EII", "BHI", "MMI",
    "DAT", "TPI", "CS", "IOS", "LCD", "DLS", "ES",
})
SUPPORTED_FILE_TYPES = frozenset(ELEM_SIZE)

_DATA_RE = re.compile(r"(?P<ft>[NFLB])(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?:/(?P<bit>\d{1,3}))?")
_BITNUM_RE = re.compile(r"B(?P<fn>\d{1,3})/(?P<bitnum>\d{1,5})")
_TC_RE = re.compile(r"(?P<ft>[TC])(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?:[./](?P<sub>[A-Z]+))?")
_S_RE = re.compile(r"S(?P<fn>\d{1,3})?:(?P<el>\d{1,4})(?:/(?P<bit>\d{1,3}))?")
_IO_RE = re.compile(
    r"(?P<ft>[IO])(?P<fn>\d{1,3})?:(?P<slot>\d{1,4})(?:\.(?P<word>\d{1,4}))?(?:/(?P<bit>\d{1,4}))?"
)
_ST_RE = re.compile(r"ST(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?P<bit>/\d+)?")
_FUNC_RE = re.compile(r"(?P<ff>[A-Z]{2,4}):\d+(?:\.\w+)?(?:/\d+)?")
_FILETYPE_RE = re.compile(r"(?P<ft>[A-Z]{1,3})\d{0,3}:.*")


@dataclass(frozen=True, slots=True)
class PcccAddress:
    """One parsed data-table address.

    `element` is the element number in the file; for I/O files it is the slot and
    `word` is the word within the slot. `bit` is a bit inside the element word(s).
    `field` is the T/C sub-element (ACC, PRE, DN, ...).
    """

    file_type: str
    file_number: int
    element: int
    word: int = 0
    bit: int | None = None
    field: str | None = None

    @property
    def elem_size(self) -> int:
        return ELEM_SIZE[self.file_type]

    @property
    def is_bit(self) -> bool:
        return self.bit is not None or (self.field is not None and self.field not in TC_WORD_FIELDS)

    def canonical(self) -> str:
        bit = f"/{self.bit}" if self.bit is not None else ""
        ft = self.file_type
        if ft in ("I", "O"):
            return f"{ft}:{self.element}.{self.word}{bit}"
        if ft == "S":
            return f"S:{self.element}{bit}"
        if ft in TC_FIELDS:
            return f"{ft}{self.file_number}:{self.element}.{self.field}"
        return f"{ft}{self.file_number}:{self.element}{bit}"


def _check_range(what: str, value: int, lo: int, hi: int, address: str) -> int:
    if not lo <= value <= hi:
        raise ValueError(f"{what} {value} out of range ({lo}-{hi}) in {address!r}")
    return value


def parse_pccc_address(address: str) -> PcccAddress:
    """Parse a PCCC data-table address. Raises ValueError with a readable reason."""
    text = (address or "").strip()
    if not text:
        raise ValueError("empty address")
    u = text.upper()
    if "{" in u or "}" in u:
        raise ValueError(f"element count {{n}} in a tag address is not supported: {text!r}")
    if u.startswith("PROGRAM:") or not re.search(r"[:/]", u):
        raise ValueError("symbolic address on a PCCC device")

    if m := _ST_RE.fullmatch(u):
        if m["bit"]:
            raise ValueError(f"bit access is not supported on a ST file: {text!r}")
        return PcccAddress(
            "ST",
            _check_range("file number", int(m["fn"]), 1, MAX_FILE, text),
            _check_range("element", int(m["el"]), 0, MAX_ELEMENT, text),
        )

    if m := _DATA_RE.fullmatch(u):
        ft = m["ft"]
        fn = _check_range("file number", int(m["fn"]), 1, MAX_FILE, text)
        el = _check_range("element", int(m["el"]), 0, MAX_ELEMENT, text)
        bit = None
        if m["bit"] is not None:
            if ft == "F":
                raise ValueError(f"bit access is not supported on a F file: {text!r}")
            bit = _check_range("bit", int(m["bit"]), 0, 31 if ft == "L" else 15, text)
        return PcccAddress(ft, fn, el, bit=bit)

    if m := _BITNUM_RE.fullmatch(u):
        fn = _check_range("file number", int(m["fn"]), 1, MAX_FILE, text)
        bitnum = _check_range("bit number", int(m["bitnum"]), 0, (MAX_ELEMENT + 1) * 16 - 1, text)
        return PcccAddress("B", fn, bitnum // 16, bit=bitnum % 16)

    if m := _TC_RE.fullmatch(u):
        ft = m["ft"]
        fn = _check_range("file number", int(m["fn"]), 1, MAX_FILE, text)
        el = _check_range("element", int(m["el"]), 0, MAX_ELEMENT, text)
        sub = m["sub"]
        if sub is None:
            raise ValueError(
                f"{ft} address needs a sub-element ({', '.join('.' + f for f in TC_FIELDS[ft])}): {text!r}"
            )
        if sub not in TC_FIELDS[ft]:
            raise ValueError(f"sub-element .{sub} is not valid for a {ft} file: {text!r}")
        return PcccAddress(ft, fn, el, field=sub)

    if m := _S_RE.fullmatch(u):
        if m["fn"] is not None and int(m["fn"]) != 2:
            raise ValueError(f"file number of the S (status) file must be 2: {text!r}")
        el = _check_range("element", int(m["el"]), 0, MAX_ELEMENT, text)
        bit = _check_range("bit", int(m["bit"]), 0, 15, text) if m["bit"] is not None else None
        return PcccAddress("S", 2, el, bit=bit)

    if m := _IO_RE.fullmatch(u):
        ft = m["ft"]
        expected_fn = 1 if ft == "I" else 0
        if m["fn"] is not None and int(m["fn"]) != expected_fn:
            raise ValueError(f"file number of an {ft} file must be {expected_fn}: {text!r}")
        slot = _check_range("slot", int(m["slot"]), 0, MAX_ELEMENT, text)
        word = int(m["word"] or 0)
        bit = None
        if m["bit"] is not None:
            # RSLogix bit-number form: I:0/19 is word 1, bit 3 of slot 0
            bitnum = int(m["bit"])
            word, bit = word + bitnum // 16, bitnum % 16
        word = _check_range("word", word, 0, MAX_ELEMENT, text)
        return PcccAddress(ft, expected_fn, slot, word=word, bit=bit)

    if m := _FUNC_RE.fullmatch(u):
        if m["ff"] in FUNCTION_FILES:
            raise ValueError(
                f"MicroLogix function file {m['ff']} is not supported by pycomm3 SLCDriver: {text!r}"
            )

    if m := _FILETYPE_RE.fullmatch(u):
        if m["ft"] not in SUPPORTED_FILE_TYPES and m["ft"] not in FUNCTION_FILES:
            raise ValueError(f"file type {m['ft']} is not supported by pycomm3 SLCDriver: {text!r}")

    raise ValueError(f"cannot parse PCCC address {text!r}")


# --- dtype handling ------------------------------------------------------------------

_INT_DTYPES: dict[str, tuple[int, bool]] = {
    "int16": (16, True), "uint16": (16, False), "int32": (32, True), "uint32": (32, False),
    "int64": (64, True), "uint64": (64, False),
}
_FLOAT_DTYPES = frozenset({"float32", "float64"})
_NUMERIC = frozenset(_INT_DTYPES) | _FLOAT_DTYPES | {"bool"}

_ALLOWED_DTYPES: dict[str, frozenset[str]] = {
    "bit": _NUMERIC,
    "word": frozenset({"bool", "int16", "uint16", "float32", "float64"}),
    "L": frozenset({"bool", "int32", "uint32", "int64", "uint64", "float32", "float64"}),
    "F": _FLOAT_DTYPES,
    "ST": frozenset({"string"}),
}


def _dtype_class(addr: PcccAddress) -> str:
    if addr.is_bit:
        return "bit"
    if addr.file_type in WORD_FILES or addr.file_type in TC_FIELDS:
        return "word"
    return addr.file_type          # L, F, ST


def check_dtype(addr: PcccAddress, dtype: str) -> None:
    """Raise ValueError when `dtype` cannot be read from `addr` without guessing."""
    cls = _dtype_class(addr)
    if dtype not in _ALLOWED_DTYPES[cls]:
        what = {"bit": "a bit address", "word": "a 16-bit word"}.get(cls, f"a {cls} file element")
        raise ValueError(
            f"dtype {dtype} is not supported on {what} ({addr.canonical()}); "
            f"allowed: {', '.join(sorted(_ALLOWED_DTYPES[cls]))}"
        )


def coerce_value(raw: Any, dtype: str) -> Any:
    """Convert a decoded raw value to the configured dtype (shared by the AB drivers)."""
    if raw is None:
        raise TypeError("no value")
    if dtype == "string":
        if not isinstance(raw, str):
            raise TypeError(f"expected a string, got {type(raw).__name__}")
        return raw
    if isinstance(raw, (str, bytes, list, tuple, dict)):
        raise TypeError(f"expected a number, got {type(raw).__name__}")
    if dtype == "bool":
        return bool(raw)
    if dtype in _FLOAT_DTYPES:
        return float(raw)
    if isinstance(raw, float):
        raise TypeError(f"float value {raw!r} for integer dtype {dtype}")
    bits, signed = _INT_DTYPES[dtype]
    value = int(raw) & ((1 << bits) - 1)
    if signed and value >= 1 << (bits - 1):
        value -= 1 << bits
    return value


def _as_int(raw: Any) -> int:
    if isinstance(raw, int):           # bool is an int as well
        return int(raw)
    raise TypeError(f"expected an integer word, got {type(raw).__name__}")


# --- blocks -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PcccItem:
    """One tag inside a block: where its value sits in the reply and how to decode it."""

    tag: str
    offset: int                    # element (I/O: word) index relative to the block start
    dtype: str
    bit: int | None = None
    field: str | None = None       # T/C sub-element


@dataclass(kw_only=True)
class PcccBlock(Block):
    """One `SLCDriver.read(address)` call = one PCCC request."""

    file_type: str
    file_number: int
    start: int                     # first element (I/O: first word within the slot)
    count: int
    elem_size: int
    address: str                   # pycomm3 read string, e.g. "F34:0{22}"
    items: list[PcccItem] = dc_field(default_factory=list)
    slot: int | None = None        # I/O files only
    field: str | None = None       # T/C files only

    @property
    def nbytes(self) -> int:
        return self.count * self.elem_size

    def describe(self) -> str:
        if self.field is not None:
            span = f"{self.file_type}{self.file_number}:{self.start}.{self.field}"
        elif self.slot is not None:
            span = f"{self.file_type} slot {self.slot} words {self.start}..{self.start + self.count - 1}"
        else:
            span = f"{self.file_type}{self.file_number} {self.start}..{self.start + self.count - 1}"
        return f"{self.address} ({span}, {self.count} elem, {self.nbytes} B, {len(self.tags)} tags)"

    def wire_requests(self) -> int:
        return 1                   # SLCDriver sends exactly one request per address


def _read_string(ft: str, fn: int, slot: int | None, start: int, count: int, fld: str | None) -> str:
    suffix = f"{{{count}}}" if count > 1 else ""
    if fld is not None:
        return f"{ft}{fn}:{start}.{fld}"
    if ft in ("I", "O"):
        return f"{ft}:{slot}.{start}{suffix}"
    if ft == "S":
        return f"S:{start}{suffix}"
    return f"{ft}{fn}:{start}{suffix}"


def decode_block(block: PcccBlock, values: Any) -> dict[str, Any | DecodeError]:
    """Decode the `.value` pycomm3 returned for `block.address`.

    pycomm3 returns a scalar for a single element and a list for `{n}` reads. Every
    tag gets either its value or a `DecodeError`; one bad tag never hides the others.
    """
    if values is None:
        err = f"{block.address}: no value in reply"
        return {item.tag: DecodeError(err) for item in block.items}
    seq = list(values) if isinstance(values, (list, tuple)) else [values]
    out: dict[str, Any | DecodeError] = {}
    for item in block.items:
        if item.offset >= len(seq):
            out[item.tag] = DecodeError(
                f"{block.address}: reply has {len(seq)} elements, element {block.start + item.offset} missing"
            )
            continue
        raw = seq[item.offset]
        try:
            out[item.tag] = _decode_item(block, item, raw)
        except (TypeError, ValueError, OverflowError, KeyError) as exc:
            out[item.tag] = DecodeError(f"{block.address}: {item.tag}: {exc}")
    return out


def _decode_item(block: PcccBlock, item: PcccItem, raw: Any) -> Any:
    if raw is None:
        raise TypeError("no value")
    ft = block.file_type
    if ft == "ST" or ft == "F":
        return coerce_value(raw, item.dtype)
    if item.field is not None and item.field not in TC_WORD_FIELDS:
        return coerce_value(bool(_as_int(raw)), item.dtype)   # pycomm3 already extracted the bit
    word = _as_int(raw)
    if item.bit is not None:
        return coerce_value(bool((word >> item.bit) & 1), item.dtype)
    return coerce_value(word, item.dtype)


# --- I/O: pure helpers ---------------------------------------------------------------------

_log = structlog.get_logger("opencap.drivers.ab_pccc")

DEFAULT_PORT = 44818
STANDARD_CONNECTION_SIZE = 500      # standard Forward Open; what pycomm3 falls back to for SLC
REQUESTOR_ID_LEN = 7                # pycomm3 sends (and parses replies with) a 7-byte requestor ID
DIRECTORY_GIVE_UP = 2               # directory loads in a row with no answer -> no more tries
MAX_DIRECTORY_BYTES = 8192          # SLC 5/05, MicroLogix 1400: < 3 KB for 256 files
DIRECTORY_CHUNK = 0x50              # pycomm3 `_read_whole_file_directory` chunk size

# Execute PCCC (CIP service 0x4B on the PCCC object, class 0x67 instance 1).
EXECUTE_PCCC = 0x4B
# The only PCCC commands this driver may send, as (CMD, FNC):
#   (0x0F, 0xA2) protected typed logical read, 3 address fields (every block read)
#   (0x0F, 0xA1) protected typed logical read, 2 address fields (file directory, file 0)
#   (0x06, 0x03) diagnostic status (processor type)
READ_COMMANDS = frozenset({(0x0F, 0xA2), (0x0F, 0xA1), (0x06, 0x03)})
DATALOG_FILE_TYPE = 0xA5            # reading a MicroLogix data-log queue removes the entries
# CIP services refused on the connection: the write/set services, plus Read-Modify-Write 0x4E
# (Forward Close is 0x4E too, but pycomm3 always sends it unconnected).
CONNECTED_WRITE_SERVICES = WRITE_SERVICES | {0x4E}

CIP_SUCCESS = 0x00
CIP_CONNECTION_FAILURE = 0x01       # on a connected request: our CIP connection is gone
EXT_STS_FOLLOWS = 0xF0
# PCCC STS (remote error, high nibble) texts, DF1 manual 1770-6.5.16.
PCCC_STS_TEXT = {
    0x10: "illegal command or format (address may not exist or not enough elements in the file)",
    0x20: "host has a problem and will not communicate",
    0x30: "remote node host is missing, disconnected or shut down",
    0x40: "host could not complete the function due to a hardware fault",
    0x50: "addressing problem or memory protect rungs",
    0x60: "function not allowed due to command protection selection",
    0x70: "processor is in Program mode",
    0x80: "compatibility mode file missing or communication zone problem",
    0x90: "remote node cannot buffer the command",
    0xB0: "remote node problem due to download",
    0xF0: "error code in the EXT STS byte",
}
# EXT STS texts (after STS 0xF0).
PCCC_EXT_STS_TEXT = {
    0x01: "a field has an illegal value",
    0x02: "fewer levels specified in the address than the minimum for any address",
    0x03: "more levels specified in the address than the system supports",
    0x04: "symbol not found",
    0x05: "symbol is of improper format",
    0x06: "address does not point to something usable",
    0x07: "file is wrong size",
    0x08: "cannot complete request, situation has changed since the start of the command",
    0x09: "data or file is too large",
    0x0A: "transaction size plus word address is too large",
    0x0B: "access denied, improper privilege",
    0x0C: "condition cannot be generated, resource is not available",
    0x0D: "condition already exists, resource is already available",
    0x0E: "command cannot be executed",
    0x0F: "histogram overflow",
    0x10: "no access",
    0x11: "illegal data type",
    0x12: "invalid parameter or invalid data",
    0x13: "address reference exists to deleted area",
    0x14: "command execution failure for unknown reason",
    0x15: "data conversion error",
    0x17: "type mismatch",
    0x1A: "file is open; another node owns it",
    0x1B: "another node is the program owner",
    0x1E: "data table element protection violation",
    0x1F: "temporary internal problem",
    0x23: "timeout",
    0x24: "unknown error",
}
# Statuses that mean "this file/element does not exist or this address is illegal for the
# processor" (permanent for the block) -> BlockConfigError. Everything else -> CommError.
CONFIG_STS = frozenset({0x10, 0x50})
CONFIG_EXT_STS = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x09, 0x0A, 0x11, 0x12, 0x13, 0x17})

# File directory type codes and element sizes (pycomm3 `PCCC_DATA_TYPE` / `PCCC_DATA_SIZE`).
DIRECTORY_TYPES: dict[int, str] = {
    0x82: "O", 0x83: "I", 0x84: "S", 0x85: "B", 0x86: "T", 0x87: "C", 0x88: "R", 0x89: "N",
    0x8A: "F", 0x8D: "ST", 0x8E: "A", 0x91: "L", 0x92: "MG", 0x93: "PD", 0x94: "PLS",
}
DIRECTORY_ELEM_SIZE: dict[str, int] = {**ELEM_SIZE, "A": 2, "R": 6, "MG": 50, "PD": 46, "PLS": 12}
DIRECTORY_SKIPPED_NUMBER = 0x81     # pycomm3: "reserved type, for skipped file numbers?"
CHECKED_FILE_TYPES = frozenset({"N", "F", "L", "B", "T", "C", "ST"})   # I/O and S: not checked
_VERIFY_TYPES = ("N", "F", "L", "B")


def connection_path(device: Device) -> str:
    """pycomm3 path: `host:port`, plus `params.path` ("1,0" -> `host:port/1/0`). pycomm3
    adds backplane slot 0 itself when there is no route (SLCDriver auto-slot). Raises ValueError."""
    host = (device.host or "").strip()
    if not host:
        raise ValueError("ab_pccc: empty host")
    base = f"{host}:{int(device.port or DEFAULT_PORT)}"
    route = device.params.get("path")
    segs = [s.strip() for s in re.split(r"[,/\\]", str(route)) if s.strip()] if route is not None else []
    if not segs:
        return base
    if len(segs) > 1 and len(segs) % 2:
        raise ValueError(f"ab_pccc param path={route!r}: a CIP route needs port/link pairs")
    return base + "/" + "/".join(segs)


def parse_file_check(value: Any) -> bool:
    """`params.file_check` (default True): read and use the processor's file directory."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("on", "true", "yes", "1"):
        return True
    if text in ("off", "false", "no", "0"):
        return False
    raise ValueError(f"ab_pccc param file_check={value!r} must be true or false")


@dataclass(frozen=True, slots=True)
class PcccReply:
    """Status bytes of one SendUnitData Execute PCCC reply, as pycomm3 lays it out:
    encapsulation status at 8, CIP general status at 48, extended status size at 49,
    requestor ID (length byte first) at 50, then PCCC CMD, STS, TNS (2) and data."""

    encap_status: int | None
    cip_status: int | None = None
    cip_ext: int | None = None
    sts: int | None = None
    ext_sts: int | None = None
    id_len: int | None = None
    data: bytes = b""               # after TNS (starts with the EXT STS byte when STS is 0xF0)


def parse_pccc_reply(raw: bytes | None) -> PcccReply | None:
    if raw is None:
        return None
    if len(raw) < 12:
        return PcccReply(None)
    encap = int.from_bytes(raw[8:12], "little")
    if encap != 0 or len(raw) < 50:
        return PcccReply(encap)
    cip, ext_words = raw[48], raw[49]
    if cip != CIP_SUCCESS:
        ext = int.from_bytes(raw[50:52], "little") if ext_words and len(raw) >= 52 else None
        return PcccReply(encap, cip, ext)
    p = 50 + 2 * ext_words
    if len(raw) <= p:
        return PcccReply(encap, cip)
    id_len = raw[p]
    q = p + id_len                  # PCCC CMD
    if len(raw) < q + 4:
        return PcccReply(encap, cip, id_len=id_len)
    sts = raw[q + 1]
    data = bytes(raw[q + 4:])
    ext_sts = data[0] if sts == EXT_STS_FOLLOWS and data else None
    return PcccReply(encap, cip, None, sts, ext_sts, id_len, data)


def _cip_status_text(status: int) -> str:
    try:
        from pycomm3.cip import SERVICE_STATUS
        text = SERVICE_STATUS.get(status, "unknown")
    except ImportError:   # pragma: no cover
        text = "unknown"
    return f"CIP status {status:#04x} ({text})"


def pccc_status_text(sts: int, ext_sts: int | None = None) -> str:
    # high nibble: remote error (from the PLC); low nibble: local error (from the link)
    meaning = PCCC_STS_TEXT.get(sts & 0xF0) or ("local error" if sts & 0x0F else "unknown")
    text = f"PCCC STS {sts:#04x} ({meaning})"
    if sts == EXT_STS_FOLLOWS:
        if ext_sts is None:
            return text + ", EXT STS missing"
        return text + f", EXT STS {ext_sts:#04x} ({PCCC_EXT_STS_TEXT.get(ext_sts, 'unknown')})"
    return text


def classify_reply(reply: PcccReply | None) -> tuple[str, str] | None:
    """None for a good reply, else (kind, reason). kind: "lost" (connection unusable),
    "config" (file/element does not exist or illegal address: permanent for the block),
    "comm" (any other PCCC/CIP error or a malformed reply)."""
    if reply is None:
        return "comm", "no reply status captured"
    if reply.encap_status is None:
        return "lost", "unreadable encapsulation header"
    if reply.encap_status != 0:
        return "lost", f"encapsulation status {reply.encap_status:#06x}"
    if reply.cip_status is None:
        return "comm", "truncated CIP reply"
    if reply.cip_status != CIP_SUCCESS:
        text = _cip_status_text(reply.cip_status)
        if reply.cip_ext is not None:
            text += f", extended {reply.cip_ext:#06x}"
        return ("lost" if reply.cip_status == CIP_CONNECTION_FAILURE else "comm"), text
    if reply.sts is None:
        return "comm", "truncated PCCC reply"
    if reply.id_len != REQUESTOR_ID_LEN:
        return "comm", f"unexpected requestor ID length {reply.id_len} in the reply"
    if reply.sts == 0:
        return None
    text = pccc_status_text(reply.sts, reply.ext_sts)
    if reply.sts == EXT_STS_FOLLOWS:
        return ("config" if reply.ext_sts in CONFIG_EXT_STS else "comm"), text
    return ("config" if reply.sts in CONFIG_STS else "comm"), text


def pccc_command(message: bytes) -> tuple[int, int | None, int | None] | None:
    """(CMD, FNC, file type) of an Execute PCCC request (CIP message starting with service
    0x4B), None when the message is not Execute PCCC. Raises ValueError when it is but cannot
    be parsed. FNC/file type are None when the command has no such byte."""
    if not message or message[0] != EXECUTE_PCCC:
        return None
    try:
        path_words = message[1]
        p = 2 + 2 * path_words              # requestor ID length byte
        q = p + message[p]                  # PCCC CMD
        cmd = message[q]
    except IndexError:
        raise ValueError("truncated Execute PCCC request") from None
    fnc = message[q + 4] if len(message) > q + 4 else None
    ftype = message[q + 7] if len(message) > q + 7 else None
    return cmd, fnc, ftype


def check_read_only(message: bytes) -> str | None:
    """Reason to refuse a connected CIP message, None when it may go out."""
    if not message:
        return None
    try:
        cmd = pccc_command(message)
    except ValueError as exc:
        return f"unparseable Execute PCCC request ({exc})"
    if cmd is None:
        return f"CIP service {message[0]:#04x} refused" if message[0] in CONNECTED_WRITE_SERVICES else None
    code, fnc, ftype = cmd
    if (code, fnc) not in READ_COMMANDS:
        return f"PCCC command {code:#04x} function {fnc if fnc is None else f'{fnc:#04x}'} refused"
    if code == 0x0F and ftype == DATALOG_FILE_TYPE:
        return "PCCC data-log queue read refused (it removes entries)"
    return None


class DirectoryUnavailable(Exception):
    """The processor's file directory cannot be read or trusted: skip the file check."""


class PcccLinkError(Exception):
    """A reply outside a block read says the connection is unusable (encapsulation error,
    CIP connection failure)."""


def parse_file_directory(sys0: Mapping[str, Any], data: bytes) -> dict[int, tuple[str, int]]:
    """File number -> (file type, elements) from the raw file directory (system file 0).

    Same walk as pycomm3 `_parse_file0` (rows of `row_size` bytes from `file_position`:
    type code, UINT length in bytes; known types and 0x81 count as a file number), without
    its print and bounds problems. Raises DirectoryUnavailable unless the result starts with
    the files every SLC/MicroLogix has: O0, I1, S2.
    """
    pos, row = int(sys0["file_position"]), int(sys0["row_size"])
    if row < 3:
        raise DirectoryUnavailable(f"bad directory row size {row}")
    files: dict[int, tuple[str, int]] = {}
    number = 0
    while pos < len(data):
        ftype = DIRECTORY_TYPES.get(data[pos])
        if ftype is not None:
            if pos + 3 > len(data):
                raise DirectoryUnavailable(f"file directory truncated at byte {pos}")
            length = int.from_bytes(data[pos + 1:pos + 3], "little")
            files[number] = (ftype, length // DIRECTORY_ELEM_SIZE.get(ftype, 2))
        if ftype is not None or data[pos] == DIRECTORY_SKIPPED_NUMBER:
            number += 1
        pos += row
    head = tuple(files.get(n, ("?", 0))[0] for n in (0, 1, 2))
    if head != ("O", "I", "S"):
        raise DirectoryUnavailable(f"file directory does not start with O0, I1, S2 (parsed {head})")
    return files


def directory_problem(files: Mapping[int, tuple[str, int]], block: PcccBlock) -> str | None:
    """Why the directory says `block` cannot be read, or None (also for unchecked file types)."""
    ft, fn = block.file_type, block.file_number
    if ft not in CHECKED_FILE_TYPES:
        return None
    entry = files.get(fn)
    if entry is None:
        return f"file {ft}{fn} does not exist (processor file directory)"
    if entry[0] != ft:
        return f"file {fn} is a {entry[0]} file, not {ft} (processor file directory)"
    end = block.start + block.count
    if end > entry[1]:
        return (f"{ft}{fn} has {entry[1]} elements, the block needs {block.start}..{end - 1} "
                f"(processor file directory)")
    return None


def verify_targets(files: Mapping[int, tuple[str, int]]) -> tuple[str, str | None] | None:
    """Two reads that check a parsed directory against the PLC: the last element of the
    highest-numbered N/F/L/B file must be readable, the element after it must not (None when
    the file is full: element 256 cannot be addressed). None when there is no such file."""
    cands = [(n, ft, el) for n, (ft, el) in files.items() if ft in _VERIFY_TYPES and el > 0]
    if not cands:
        return None
    n, ft, el = max(cands)
    last = min(el, MAX_ELEMENT + 1) - 1
    after = f"{ft}{n}:{el}" if el <= MAX_ELEMENT else None
    return f"{ft}{n}:{last}", after


@dataclass(frozen=True, slots=True)
class PcccResult:
    """Outcome of `plc.oo_read(address)`."""

    value: Any                      # pycomm3 Tag.value
    error: str | None               # pycomm3 Tag.error
    sent: bool                      # the PCCC read went on the wire
    reply: PcccReply | None         # its reply status (None: no reply seen)


# --- I/O: pycomm3 SLCDriver subclass ---------------------------------------------------------

@functools.cache
def slc_driver_class() -> type:
    """The pycomm3 `SLCDriver` subclass the driver instantiates (built lazily, D-002).

    Hooks, each minimal and pinned by tests against pycomm3 1.2.16:
    - `__init__`: standard Forward Open at 500 bytes (SLC/MicroLogix refuse the Large Forward
      Open pycomm3 tries first; this is pycomm3's own fallback, minus the refused request).
    - `open`: installs the socket reader that reads exactly one encapsulation message and
      fails on a peer close (pycomm3's reader loops forever on a close mid-reply).
    - `send`: refuses every connected message except the three PCCC reads (and CIP
      Set/Write services), counts PCCC requests, keeps each PCCC reply's status bytes, and
      enforces a request budget while the file directory is read.
    - `write`, `_write_tag`, `get_datalog_queue`, `_get_datalog` and Set/Write services in
      `generic_message` raise (read-only).
    """
    from pycomm3 import SLCDriver
    from pycomm3.cip_driver import with_forward_open
    from pycomm3.exceptions import RequestError, ResponseError
    from pycomm3.packets import SendUnitDataRequestPacket
    from pycomm3.slc_driver import _get_sys0_info

    OoSocket = oo_socket_class()

    class OoSLCDriver(SLCDriver):
        socket_class = OoSocket

        def __init__(self, path: str, *args: Any, **kwargs: Any) -> None:
            super().__init__(path, *args, **kwargs)
            self._cfg["extended forward open"] = False
            self._cfg["connection_size"] = STANDARD_CONNECTION_SIZE
            self._oo_reply: PcccReply | None = None
            self._oo_budget: int | None = None
            self.oo_sent = 0                         # PCCC requests sent by the last oo_* call

        # --- connection ---

        def open(self) -> bool:
            if self._sock is None:
                self._sock = OoSocket(self._cfg["socket_timeout"])
            return super().open()

        def oo_list_identity(self) -> dict:
            return dict(self._list_identity() or {})

        def oo_forward_open(self) -> None:
            with_forward_open(lambda _plc: None)(self)    # raises ResponseError if it fails

        def oo_abort(self) -> None:
            """Drop the TCP connection without Forward Close / UnRegister Session."""
            self._target_is_connected = False
            self._session = 0
            self.close()

        def _oo_link_check(self) -> None:
            problem = classify_reply(self._oo_reply)
            if problem is not None and problem[0] == "lost":
                raise PcccLinkError(problem[1])

        # --- reads ---

        def oo_read(self, address: str) -> PcccResult:
            self._oo_reply = None
            self.oo_sent = 0
            tag = self.read(address)
            if isinstance(tag, list):                # pragma: no cover - one address, one Tag
                tag = tag[0]
            return PcccResult(tag.value, tag.error, self.oo_sent > 0, self._oo_reply)

        def oo_processor_type(self) -> str | None:
            """Catalog string from PCCC diagnostic status (e.g. "1766-L32BXB"), None if the
            PLC does not answer it with a good status or the text is not printable."""
            self._oo_reply = None
            self.oo_sent = 0
            typ = self.get_processor_type()
            self._oo_link_check()
            if classify_reply(self._oo_reply) is not None or not isinstance(typ, str):
                return None
            typ = typ.strip(" \x00")
            return typ if typ and typ.isprintable() else None

        def oo_file_directory(self, plc_type: str) -> dict[int, tuple[str, int]]:
            """pycomm3's file directory reads (size, then 80-byte chunks of file 0) with a
            request budget, parsed by `parse_file_directory`. Raises DirectoryUnavailable."""
            sys0 = _get_sys0_info(plc_type)
            self._oo_reply = None
            self.oo_sent = 0
            size = self._get_file_directory_size(sys0)
            self._oo_link_check()
            if size is None:
                problem = classify_reply(self._oo_reply)
                raise DirectoryUnavailable(f"size not readable: {problem[1] if problem else 'bad reply'}")
            if not 0 < size <= MAX_DIRECTORY_BYTES:
                raise DirectoryUnavailable(f"implausible directory size {size} bytes")
            self._oo_budget = math.ceil(size / DIRECTORY_CHUNK) + 1
            try:
                data = self._read_whole_file_directory({**sys0, "size": size})
            except ResponseError as exc:
                self._oo_link_check()
                raise DirectoryUnavailable(f"contents not readable: {exc}") from None
            finally:
                self._oo_budget = None
            return parse_file_directory(sys0, data[:size])

        def send(self, request: Any) -> Any:
            pccc = False
            if isinstance(request, SendUnitDataRequestPacket):
                message = request.build_message()[2:]          # after the sequence count
                refused = check_read_only(message)
                if refused is not None:
                    raise RequestError(f"opencap is read-only: {refused}")
                pccc = bool(message) and message[0] == EXECUTE_PCCC
            if pccc:
                if self._oo_budget is not None:
                    if self._oo_budget <= 0:
                        raise DirectoryUnavailable("file directory read made no progress (request budget used up)")
                    self._oo_budget -= 1
                self.oo_sent += 1
                self._oo_reply = None
            response = super().send(request)
            if pccc:
                self._oo_reply = parse_pccc_reply(response.raw)
            return response

        # --- read-only guards ---

        def write(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("opencap is read-only: writes to PLCs are not supported")

        def _write_tag(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("opencap is read-only: writes to PLCs are not supported")

        def get_datalog_queue(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("opencap is read-only: data-log queue reads remove entries")

        def _get_datalog(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("opencap is read-only: data-log queue reads remove entries")

        def generic_message(self, service: Any, *args: Any, **kwargs: Any) -> Any:
            code = service[0] if isinstance(service, (bytes, bytearray)) else int(service)
            if code in WRITE_SERVICES:
                raise NotImplementedError(f"opencap is read-only: CIP service {code:#04x} refused")
            return super().generic_message(service, *args, **kwargs)

    return OoSLCDriver


# --- I/O: driver state ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Directory:
    """Outcome of one file-directory load, kept across reconnects of one driver instance."""

    key: tuple                                       # controller identity it was read from
    files: dict[int, tuple[str, int]] | None         # None: no file check
    status: str                                      # for identity() and logs


@dataclass(eq=False)
class _Session:
    """One open pycomm3 connection (touched by pycomm3 calls only on the executor)."""

    plc: Any
    identity: dict[str, Any]
    directory: _Directory
    dead: bool = False          # no longer usable (closed, or failed in the executor)
    closed: bool = False        # pycomm3 close/abort already done


class _ReadFailure(Exception):
    def __init__(self, reason: str, *, kind: str = "comm") -> None:
        super().__init__(reason)
        self.kind = kind        # "lost" | "config" | "comm"


class _Abandoned(Exception):
    pass


def _identity_snapshot(info: Mapping[str, Any]) -> dict[str, Any]:
    rev = info.get("revision")
    if isinstance(rev, Mapping):
        revision = f"{rev.get('major', '?')}.{rev.get('minor', '?')}"
    else:
        revision = None if rev is None else str(rev)
    return {
        "vendor": info.get("vendor"),
        "product_name": info.get("product_name"),
        "product_code": info.get("product_code"),
        "product_type": info.get("product_type"),
        "revision": revision,
        "serial": info.get("serial"),
    }


def _identity_key(identity: Mapping[str, Any]) -> tuple:
    return tuple(identity.get(k) for k in ("product_name", "product_code", "serial", "revision", "processor_type"))


def _is_request_error(exc: BaseException) -> bool:
    """pycomm3 could not build the request (bad address, value out of a field's range)."""
    from pycomm3.exceptions import DataError, RequestError
    return isinstance(exc, (RequestError, DataError))


# --- driver -------------------------------------------------------------------------------

def _param(params: dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = params.get(key)
    if value is None:
        return default
    value = int(value)
    if value < minimum:
        raise ValueError(f"ab_pccc param {key}={value} must be >= {minimum}")
    return value


class AbPcccDriver(Driver):
    """SLC 500 / MicroLogix data tables via pycomm3 `SLCDriver` (sync, channel executor).

    Contract: docs/phase3_contracts.md §1. Every pycomm3 call runs on the channel's
    single-thread executor (`_run_sync`). One attempt per call: retries, backoff and
    reconnects belong to the ChannelWorker.
    """

    name: ClassVar[str] = "ab_pccc"
    is_async: ClassVar[bool] = False

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)
        self.path = connection_path(device)                          # ValueError on bad params
        self.file_check = parse_file_check(device.params.get("file_check"))
        self._session: _Session | None = None
        self._directory: _Directory | None = None     # survives reconnects of this instance
        self._directory_misses = 0                    # directory loads in a row with no answer
        self._rejected: set[str] = set()              # block addresses already logged as rejected

    def plan(self, tags: list[Tag]) -> Plan:
        params = self.device.params
        max_elements = _param(params, "max_elements", 100, 1)
        max_gap = _param(params, "max_gap", 4, 0)
        max_bytes = min(_param(params, "request_size", 232, 1), PCCC_MAX_BYTES)

        invalid: dict[str, str] = {}
        groups: dict[tuple, list[tuple[PcccAddress, Tag]]] = defaultdict(list)
        for tag in tags:
            if not tag.enabled:
                continue
            try:
                addr = parse_pccc_address(tag.address)
                check_dtype(addr, tag.dtype)
            except ValueError as exc:
                invalid[tag.name] = str(exc)
                continue
            groups[_group_key(addr)].append((addr, tag))

        blocks: list[Block] = []
        for key in sorted(groups):
            ft = key[0]
            entries = sorted(groups[key], key=lambda e: (_index(e[0]), _sort_bit(e[0]), e[1].name))
            cap = _max_count(ft, max_elements, max_bytes)
            run: list[tuple[PcccAddress, Tag]] = []
            start = last = 0
            for addr, tag in entries:
                idx = _index(addr)
                if run and idx - last - 1 <= max_gap and idx - start + 1 <= cap:
                    run.append((addr, tag))
                    last = idx
                    continue
                if run:
                    blocks.append(_make_block(key, start, last, run))
                run, start, last = [(addr, tag)], idx, idx
            if run:
                blocks.append(_make_block(key, start, last, run))

        return Plan(blocks=blocks, invalid=dict(sorted(invalid.items())))

    # --- I/O ---

    def connect_timeout_s(self) -> float | None:
        """Connect = TCP + RegisterSession + ListIdentity + Forward Open + diagnostic status,
        plus the file directory the first time (size, ~35 chunks of 80 bytes for a full
        MicroLogix 1400, two verification reads). Give the first one room."""
        per_request = self.device.timeout_ms / 1000
        if self._directory is None and self.file_check:
            return max(60.0, 20 * per_request)
        return 6 * per_request + 1.0

    @property
    def connected(self) -> bool:
        """True while the connection is believed usable. pycomm3 notices a peer close only
        on the next request: that read raises ConnectionLost."""
        s = self._session
        return s is not None and not s.dead

    @property
    def timeout_s(self) -> float:
        return max(self.device.timeout_ms, 1) / 1000.0

    @property
    def _where(self) -> str:
        return f"{self.device.id} ({self.path})"

    async def connect(self) -> None:
        """Open the connection: RegisterSession, ListIdentity, standard Forward Open,
        processor type, and the file directory (read once per driver instance and controller
        identity). An open connection is closed first. Raises ConnectionLost on any failure."""
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
            _log.debug("ab_pccc connect", device=self.device.id, result="failed", reason=reason,
                       ms=round((time.monotonic() - started) * 1000, 1))
            raise ConnectionLost(f"{self._where}: connect failed: {reason}") from exc
        if session is None:    # pragma: no cover - only after abandon(), i.e. cancelled
            raise ConnectionLost(f"{self._where}: connect abandoned")
        self._session = session
        _log.debug("ab_pccc connect", device=self.device.id, result="ok",
                   processor_type=session.identity.get("processor_type"),
                   file_directory=session.directory.status, ms=round((time.monotonic() - started) * 1000, 1))

    async def close(self) -> None:
        """Close the connection (Forward Close, UnRegister Session). Idempotent, never raises."""
        session, self._session = self._session, None
        if session is None or session.closed:
            return
        session.dead = True
        try:
            await self._run_sync(self._dispose_sync, session, True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:     # executor missing or shut down: drop the socket at least
            _log.debug("ab_pccc close error", device=self.device.id, error=describe_exc(exc))
            _abort_socket(session)

    async def read(self, block: Block) -> dict[str, TagValue]:
        """Read one block with one `plc.read(block.address)` call (one PCCC request).

        Raises BlockConfigError (file/element missing per the file directory, without
        sending; or a PCCC status saying so), ConnectionLost (not connected, connection
        reset/closed, encapsulation error, CIP connection failure) or CommError (reply
        timeout, after closing; any other PCCC/CIP error). Undecodable values: bad_decode.
        """
        if not isinstance(block, PcccBlock):
            raise TypeError(f"ab_pccc cannot read {type(block).__name__}")
        session = self._session
        if session is None or session.dead:
            raise ConnectionLost(f"{self._where}: not connected")
        files = session.directory.files
        if files is not None and (reason := directory_problem(files, block)) is not None:
            self._log_rejected(block, reason, source="file_directory")
            raise BlockConfigError(f"{self._where} {block.describe()}: {reason}")
        try:
            result, ts = await self._run_sync(self._read_sync, session, block.address)
        except _ReadFailure as exc:
            if session.dead and self._session is session:
                self._session = None
            if exc.kind == "config":
                self._log_rejected(block, str(exc), source="plc")
                raise BlockConfigError(f"{self._where} {block.describe()}: {exc}") from exc.__cause__
            err = ConnectionLost if exc.kind == "lost" else CommError
            raise err(f"{self._where} {block.describe()}: {exc}") from exc.__cause__
        return self._values(block, result, ts)

    async def identity(self) -> dict[str, Any]:
        """Controller identity captured at connect (no extra request): vendor, product_name,
        product_code, product_type, revision, serial, processor_type, connection_size,
        file_directory (status) and files ({"N7": elements, ...} when the directory is used).
        {} when not connected."""
        s = self._session
        if s is None or s.dead:
            return {}
        out = {**s.identity, "file_directory": s.directory.status}
        if s.directory.files is not None:
            out["files"] = {f"{ft}{n}": el for n, (ft, el) in sorted(s.directory.files.items())}
        return out

    # --- executor side (every pycomm3 call happens below, on the channel executor) ---

    def _open_sync(self, attempt: Attempt) -> _Session | None:
        if attempt.abandoned:
            return None
        plc = slc_driver_class()(self.path)
        plc._cfg["socket_timeout"] = self.timeout_s
        try:
            if not plc.open():
                raise ConnectionError("EtherNet/IP session not registered")
            identity = _identity_snapshot(plc.oo_list_identity())
            plc.oo_forward_open()
            identity["processor_type"] = plc.oo_processor_type()
            identity["connection_size"] = plc.connection_size
            if attempt.abandoned:
                raise _Abandoned()
            directory = self._load_directory(plc, identity)
        except _Abandoned:
            _close_quietly(plc, graceful=True)
            return None
        except BaseException as exc:
            _close_quietly(plc, graceful=not (is_comm_error(exc) or isinstance(exc, PcccLinkError)))
            raise
        session = _Session(plc, identity, directory)
        if not attempt.finish(session):
            self._dispose_sync(session, True)
            return None
        return session

    def _load_directory(self, plc: Any, identity: Mapping[str, Any]) -> _Directory:
        """The processor's file directory, read once per driver instance and controller
        identity, verified with two reads. Not readable or not trustworthy -> no file check.
        No answer (socket error) -> the connect fails; after DIRECTORY_GIVE_UP of those in a
        row the directory is given up for this driver instance."""
        key = _identity_key(identity)
        if not self.file_check:
            return _Directory(key, None, "off (params.file_check)")
        cached = self._directory
        if cached is not None and (cached.key == key or self._directory_misses >= DIRECTORY_GIVE_UP):
            _log.debug("ab_pccc.file_directory", device=self.device.id, source="reused", status=cached.status)
            return cached
        if cached is not None:
            _log.info("ab_pccc.identity_changed", device=self.device.id, note="re-reading the file directory",
                      before=dict(zip(("product_name", "product_code", "serial", "revision", "processor_type"),
                                      cached.key)), after=dict(identity))
        started = time.monotonic()
        try:
            ptype = identity.get("processor_type")
            if not ptype:
                raise DirectoryUnavailable("processor type unknown (no good diagnostic status reply)")
            files = plc.oo_file_directory(ptype)
            problem = self._verify_directory(plc, files)
            if problem is not None:
                raise DirectoryUnavailable(f"does not match the PLC: {problem}")
            directory = _Directory(key, files, f"ok: {len(files)} files")
        except DirectoryUnavailable as exc:
            directory = _Directory(key, None, f"unavailable: {exc}")
        except Exception as exc:
            if not (is_comm_error(exc) or isinstance(exc, PcccLinkError)):
                directory = _Directory(key, None, f"unavailable: {describe_exc(exc)}")
            else:
                self._directory_misses += 1
                if self._directory_misses >= DIRECTORY_GIVE_UP:
                    self._directory = _Directory(
                        key, None, f"unavailable: {self._directory_misses} reads in a row got no answer "
                                   f"(last: {describe_exc(exc)})")
                    _log.warning("ab_pccc.file_directory_given_up", device=self.device.id,
                                 status=self._directory.status)
                else:
                    _log.warning("ab_pccc.file_directory_no_answer", device=self.device.id,
                                 error=describe_exc(exc), note="connect fails; the next connect reads it again")
                raise
        self._directory_misses = 0
        self._directory = directory      # kept even if this attempt is abandoned: the next one reuses it
        log = _log.info if directory.files is not None else _log.warning
        log("ab_pccc.file_directory", device=self.device.id, source="read", status=directory.status,
            processor_type=identity.get("processor_type"), ms=round((time.monotonic() - started) * 1000, 1))
        return directory

    def _verify_directory(self, plc: Any, files: Mapping[int, tuple[str, int]]) -> str | None:
        """Check the parsed directory on the wire: the last element of its highest N/F/L/B
        file must be readable and the next element must be refused with an address error.
        Returns the problem, or None when the directory matches the PLC."""
        targets = verify_targets(files)
        if targets is None:
            return "no N/F/L/B file to check it against"
        last, after = targets
        r = plc.oo_read(last)
        problem = classify_reply(r.reply)
        if problem is not None:
            if problem[0] == "lost":
                raise PcccLinkError(problem[1])
            return f"{last} is listed but not readable: {problem[1]}"
        if r.value is None:
            return f"{last} is listed but its reply has no value"
        if after is not None:
            problem = classify_reply(plc.oo_read(after).reply)
            if problem is None:
                return f"{after} is readable although the directory lists {after.split(':')[1]} elements"
            if problem[0] == "lost":
                raise PcccLinkError(problem[1])
            if problem[0] != "config":
                return f"{after} past the end: expected an address error, got {problem[1]}"
        return None

    def _read_sync(self, session: _Session, address: str) -> tuple[PcccResult, datetime]:
        plc = session.plc
        started = time.monotonic()
        try:
            result = plc.oo_read(address)
        except Exception as exc:
            if is_comm_error(exc):
                self._dispose_sync(session, False)
                if is_timeout(exc):
                    raise _ReadFailure(f"no reply within {self.device.timeout_ms} ms; connection closed") from exc
                raise _ReadFailure(f"connection lost: {describe_exc(exc)}", kind="lost") from exc
            if not plc.oo_sent and _is_request_error(exc):
                raise _ReadFailure(f"pycomm3 cannot build a read of {address!r}: {describe_exc(exc)}",
                                   kind="config") from exc
            raise _ReadFailure(f"pycomm3 read failed: {describe_exc(exc)}") from exc
        if not result.sent:
            raise _ReadFailure(f"pycomm3 returned without sending a read: {result.error or 'no error text'}")
        problem = classify_reply(result.reply)
        if problem is not None:
            kind, reason = problem
            if kind == "lost":
                self._dispose_sync(session, False)
                reason += "; connection closed"
            raise _ReadFailure(reason, kind=kind)
        _log.debug("ab_pccc read", device=self.device.id, address=address, error=result.error,
                   ms=round((time.monotonic() - started) * 1000, 1))
        return result, datetime.now(UTC)

    def _dispose_sync(self, session: _Session, graceful: bool) -> None:
        session.dead = True
        if session.closed:
            return
        session.closed = True
        _close_quietly(session.plc, graceful=graceful)
        _log.debug("ab_pccc close", device=self.device.id, graceful=graceful)

    def _close_later(self, session: _Session) -> None:
        session.dead = True
        try:
            assert self._executor is not None
            self._executor.submit(self._dispose_sync, session, True)
        except Exception:
            _abort_socket(session)

    # --- loop side, no pycomm3 calls ---

    def _values(self, block: PcccBlock, result: PcccResult, ts: datetime) -> dict[str, TagValue]:
        if result.error is not None:      # good status, but pycomm3 could not parse the data
            _log.debug("ab_pccc reply not parsed", device=self.device.id, address=block.address, error=result.error)
        decoded = decode_block(block, result.value if result.error is None else None)
        out: dict[str, TagValue] = {}
        for item in block.items:
            value = decoded.get(item.tag, DecodeError(f"{block.address}: not decoded"))
            if isinstance(value, DecodeError):
                _log.debug("ab_pccc decode error", device=self.device.id, tag=item.tag, reason=value.reason)
                out[item.tag] = TagValue(None, "bad_decode", ts)
            else:
                out[item.tag] = TagValue(value, "good", ts)
        return out

    def _log_rejected(self, block: PcccBlock, reason: str, *, source: str) -> None:
        if block.address in self._rejected:
            return
        self._rejected.add(block.address)
        _log.warning("ab_pccc.block_rejected", device=self.device.id, block=block.address, source=source,
                     tags=block.tags[:20], count=len(block.tags), reason=reason, quality="bad_config")


def _group_key(addr: PcccAddress) -> tuple[str, int, int, int, str]:
    """(file type, file number, I/O slot or -1, T/C element or -1, T/C field or '')."""
    if addr.file_type in ("I", "O"):
        return (addr.file_type, addr.file_number, addr.element, -1, "")
    if addr.file_type in TC_FIELDS:
        return (addr.file_type, addr.file_number, -1, addr.element, addr.field or "")
    return (addr.file_type, addr.file_number, -1, -1, "")


def _index(addr: PcccAddress) -> int:
    return addr.word if addr.file_type in ("I", "O") else addr.element


def _sort_bit(addr: PcccAddress) -> int:
    return -1 if addr.bit is None else addr.bit


def _max_count(ft: str, max_elements: int, max_bytes: int) -> int:
    if ft in TC_FIELDS:
        return 1
    cap = min(max_elements, max_bytes // ELEM_SIZE[ft])
    if ft == "ST":
        cap = min(cap, STRING_MAX_COUNT)
    return max(cap, 1)


def _make_block(key: tuple, start: int, last: int, run: list[tuple[PcccAddress, Tag]]) -> PcccBlock:
    ft, fn, slot, _tc_element, fld = key
    slot_ = slot if slot >= 0 else None
    fld_ = fld or None
    count = last - start + 1
    items = [
        PcccItem(tag=tag.name, offset=_index(addr) - start, dtype=tag.dtype, bit=addr.bit, field=addr.field)
        for addr, tag in run
    ]
    return PcccBlock(
        tags=[i.tag for i in items],
        file_type=ft,
        file_number=fn,
        start=start,
        count=count,
        elem_size=ELEM_SIZE[ft],
        address=_read_string(ft, fn, slot_, start, count, fld_),
        items=items,
        slot=slot_,
        field=fld_,
    )


def _close_quietly(plc: Any, *, graceful: bool) -> None:
    try:
        if graceful:
            plc.close()
        else:
            plc.oo_abort()
    except Exception as exc:
        _log.debug("ab_pccc close error", error=describe_exc(exc), graceful=graceful)
        if graceful:
            try:
                plc.oo_abort()
            except Exception:   # pragma: no cover
                pass


def _abort_socket(session: _Session) -> None:
    """Last resort when the executor is gone: close the TCP socket without any CIP traffic."""
    session.dead = session.closed = True
    sock = getattr(session.plc, "_sock", None)
    try:
        if sock is not None:
            sock.close()
    except Exception:   # pragma: no cover
        pass
