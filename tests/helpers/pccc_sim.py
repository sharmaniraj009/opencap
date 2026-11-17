"""An SLC 500 / MicroLogix data-table PLC over EtherNet/IP (PCCC), for pycomm3 `SLCDriver`.

    sim = await PcccSim.start({"N7": [1, -2, 3], "F8": [1.5], "B3": [0, 0x8001],
                               "T4": [(0x2000, 100, 42)], "ST9": ["hello"], "I": [5, 6]})
    plc = SLCDriver(sim.path)                                 # "127.0.0.1:<port>" (no route, as KEPServer)
    await asyncio.to_thread(plc.open)                         # pycomm3 is synchronous: run it in a thread
    sim.set("N7:3", 7); sim.set("B3/17", True); sim.value("T4:0.ACC")   # fixture setup, not a PCCC write
    sim.mute = True                                           # read requests, never answer (client times out)
    sim.file_errors["N7"] = (0xF0, 0x06)                      # answer every read of N7 with STS/EXT STS
    await sim.stop(); await sim.restart()                     # power-off / power-on on the same port
    tables = tables_for(device.tags, seed=1)                  # data tables covering a tag list

It is the test double for the 30 real SLC/MicroLogix PLCs, written against the bytes stock
pycomm3 1.2.16 `SLCDriver` sends and parses (`pycomm3/slc_driver.py`, `cip/pccc.py`,
`const.py`, `cip_driver.py`, `packets/*`). The EtherNet/IP and CIP layers (session, ListIdentity,
identity object, Forward Open/Close, Unconnected Send, connected sequence counts) are
`tests/helpers/cip_sim.CipSim`'s; this module subclasses it and adds the PCCC object.

Wire facts matched
------------------
* `SLCDriver.open()` = TCP connect + RegisterSession only. The first `read()` (any method
  decorated `with_forward_open`) sends a Large Forward Open (0x5B, size 4000): SLC 5/05 and
  MicroLogix refuse it (0x08), pycomm3 then sends a standard Forward Open (0x54, size 500). The
  route is `[port 1 (backplane), link 0]` + Message Router (pycomm3's `_auto_slot_cip_path`
  adds the backplane segment to a bare `ip[:port]` path); a path with no route works too.
* Every PCCC request is an Execute PCCC (service 0x4B) to class 0x67 instance 1, connected
  (SendUnitData): requester id = USINT length 7 + vendor UINT (`_cfg["vid"]` 0x1009) + serial
  UDINT (`_cfg["vsn"]`, random per open), then CMD, STS 0, TNS UINT, FNC, function data. The
  reply echoes the requester id, then CMD|0x40, STS, TNS, [EXT STS when STS = 0xF0], data.
  CIP general status stays 0 for PCCC errors. pycomm3 reads STS at raw byte 58 and data from
  byte 61 (`SLC_REPLY_START`), so the requester id must be exactly 7 bytes.
* Protected typed logical read with three address fields (CMD 0x0F, FNC 0xA2): byte count,
  file number, file type, element, sub-element (an address field >= 255 is 0xFF + UINT).
  The reply data is the raw element bytes, no type header. pycomm3 asks
  `PCCC_DATA_SIZE[type] * count` bytes; T/C sub-elements (`T4:0.ACC`, `.DN`) read the whole
  6-byte element (sub-element 0) and pycomm3 picks word 1 (PRE), word 2 (ACC) or a bit of word
  0; `N7:0/5` reads the whole word and pycomm3 picks the bit; I/O (`I:x.y`) sends element =
  slot x, sub-element = word y; `S:n` is file 2. File type codes: O 0x82 (0x8B), I 0x83 (0x8C),
  S 0x84, B 0x85, T 0x86, C 0x87, R 0x88, N 0x89, F 0x8A, ST 0x8D, L 0x91. N/B/S/I/O/T/C words
  are signed on the wire (pycomm3 decodes INT), L is DINT, F is REAL, ST is UINT length +
  82 bytes with the bytes of every word swapped.
* `get_processor_type()`: diagnostic status (CMD 0x06, FNC 0x03); pycomm3 returns reply
  data bytes 5..15 (11 ASCII chars, stripped), here the catalog number from the product name
  ("1766-L32BXB").
* `get_file_directory()`: processor type, then protected typed logical reads with two address
  fields (FNC 0xA1) on file 0 with the system file type pycomm3 picks by catalog prefix
  (`_get_sys0_info`): 1766 -> type 0x03, size at word 0x2B (+19968), rows of 10 bytes from
  byte 233; 1762/1763/1764 -> 0x02, word 0x28 (+19968), 233/10; 1761 -> 0x00, word 0x23, 93/8;
  anything else (SLC 5/05) -> 0x01, word 0x23, 79/10. The sim keeps one system-file image per
  layout: header (byte 46 = program files, byte 52 = data files, the size word) + one row per
  file number 0..max (type code + UINT byte length, 0x81 for an unused number); pycomm3 reads it
  in 80-byte chunks at word offsets. Result: {"N7": {"elements": n, "length": bytes}, ...},
  which `PcccSim.directory()` also returns.
* Errors: a file that does not exist, a file type that does not match the file, an element or
  sub-element past the end, a byte count of 0 or reaching past the end of the file -> STS 0x10,
  which pycomm3 reports in `Tag.error` as "Illegal Command or Format, Address may not exist or
  not enough elements in data file" (`PCCC_ERROR_CODE[16]`) with `Tag.value` None. STS 0xF0
  (set through `file_errors`) becomes "Error code in EXT STS Byte" (pycomm3 drops the EXT STS).
  Unsimulated commands (unprotected read, word range read, typed read 0x68, ...) -> STS 0x10.
* Read-only: PCCC commands that change the PLC (protected/unprotected write and bit write
  CMD 0x00/0x02/0x05/0x08; CMD 0x0F with FNC 0x00 word range write, 0x02 bit write, 0x11 get
  edit resource, 0x18 write bytes physical, 0x26 read-modify-write, 0x3A set CPU mode,
  0x41 disable forces, 0x50/0x52/0x53/0x55 download/upload, 0x57 initialize memory,
  0x5E modify compatibility file, 0x67 typed write, 0x79 read-modify-write N, 0x80 change mode,
  0xA9/0xAA protected typed logical write, 0xAB masked write (what `SLCDriver.write()` sends);
  CMD 0x06 "set" diagnostics 0x02/0x04-0x07) get STS 0x60 (pycomm3: "Function not allows due to
  command protection selection") and are counted in `write_attempts`, as are the CIP write and
  set-attribute services. `set()` is fixture setup straight into the data table.

Not simulated: A (ASCII), MG/PD/PLS/BT/HSC and other function files (RTC: STS 0x10), PLC-5
typed read/write (0x67/0x68 with system addresses), word range read, data logging queues,
force tables, file protection, processor modes, multiple PCCC connections limits, DF1/DH+.
"""

from __future__ import annotations

import random
import re
import struct
from collections import Counter
from dataclasses import dataclass
from typing import Any

from opencap.config.models import Tag
from opencap.drivers.ab_pccc import parse_pccc_address

from tests.helpers.cip_sim import (
    GS_NOT_ENOUGH_DATA,
    GS_NOT_SUPPORTED,
    GS_OK,
    GS_PATH_UNKNOWN,
    GS_REPLY_TOO_LARGE,
    SIM_HOST,
    SVC_GET_ATTRIBUTE_SINGLE,
    SVC_GET_ATTRIBUTES_ALL,
    WRITE_SERVICES,
    CipSim,
    _CipError,
    _reply,
)

DEFAULT_PRODUCT_NAME = "1766-L32BXB B/14.00"     # MicroLogix 1400, series B, FRN 14
DEFAULT_PRODUCT_CODE = 90                         # arbitrary
SVC_EXECUTE_PCCC = 0x4B
CLASS_PCCC = 0x67
PCCC_OBJECT_PATH = [("class", CLASS_PCCC), ("instance", 1)]
IDENTITY_PATH = [("class", 0x01), ("instance", 1)]

# --- PCCC ------------------------------------------------------------------------------------------
STS_OK, STS_ILLEGAL, STS_PROTECTED, STS_EXT = 0x00, 0x10, 0x60, 0xF0
CMD_DIAGNOSTIC, CMD_EXTENDED = 0x06, 0x0F
FNC_ECHO, FNC_DIAGNOSTIC_STATUS = 0x00, 0x03
FNC_TYPED_READ_2, FNC_TYPED_READ_3 = 0xA1, 0xA2
FNC_COMMANDS = frozenset({0x06, 0x07, 0x0F})       # commands followed by a function code
WRITE_COMMANDS = frozenset({0x00, 0x02, 0x05, 0x08})
WRITE_FNC = {
    CMD_EXTENDED: frozenset({0x00, 0x02, 0x11, 0x18, 0x26, 0x3A, 0x41, 0x50, 0x52, 0x53, 0x55, 0x57,
                             0x5E, 0x67, 0x79, 0x80, 0xA9, 0xAA, 0xAB}),
    CMD_DIAGNOSTIC: frozenset({0x02, 0x04, 0x05, 0x06, 0x07}),
}
UNUSED_FILE_ROW = 0x81                             # directory row of a file number not in use
MAX_ELEMENTS = 256                                 # SLC/MicroLogix data files: elements 0-255

# file letter -> (element bytes, accepted PCCC file type codes; the first one is the directory's)
FILE_TYPES: dict[str, tuple[int, tuple[int, ...]]] = {
    "O": (2, (0x82, 0x8B)), "I": (2, (0x83, 0x8C)), "S": (2, (0x84,)), "B": (2, (0x85,)),
    "T": (6, (0x86,)), "C": (6, (0x87,)), "R": (6, (0x88,)), "N": (2, (0x89,)), "F": (4, (0x8A,)),
    "ST": (84, (0x8D,)), "L": (4, (0x91,)),
}
FIXED_FILE_NUMBER = {"O": 0, "I": 1, "S": 2}
WORD_FILES = frozenset({"N", "B", "S", "I", "O"})
STRUCT_FILES = frozenset({"T", "C", "R"})          # 3 words: control, PRE (LEN), ACC (POS)
IO_FILES = frozenset({"I", "O"})
STRING_LEN = 82
TC_WORDS = {"PRE": 1, "ACC": 2}
TC_BITS = {"EN": 15, "TT": 14, "DN": 13, "CU": 15, "CD": 14, "OV": 12, "UN": 11, "UA": 10}

_FILE_KEY = re.compile(r"(?P<ft>ST|[NBTCRFLSIO])(?P<fn>\d{1,3})?")
_ELEMENT_ADDR = re.compile(r"(?P<ft>[TCR])(?P<fn>\d{1,3}):(?P<el>\d{1,3})")


@dataclass(frozen=True)
class Sys0Layout:
    """Where pycomm3 1.2.16 `_get_sys0_info` looks for the file directory in system file 0."""

    file_type: int          # PCCC file type of the system file (FNC 0xA1, file number 0)
    size_element: int       # word holding the directory size (+ size_const)
    size_const: int
    file_position: int      # byte offset of the first directory row (file number 0)
    row_size: int


def sys0_layout(processor_type: str) -> Sys0Layout:
    prefix = processor_type[:4]
    if prefix == "1761":                                     # MicroLogix 1000 (pycomm3: untested)
        return Sys0Layout(0x00, 0x23, 0, 93, 8)
    if prefix in ("1762", "1763", "1764"):                   # MicroLogix 1200 / 1100 / 1500
        return Sys0Layout(0x02, 0x28, 19968, 233, 10)
    if prefix == "1766":                                     # MicroLogix 1400
        return Sys0Layout(0x03, 0x2B, 19968, 233, 10)
    return Sys0Layout(0x01, 0x23, 0, 79, 10)                 # SLC 5/05 and the rest


class _PcccError(Exception):
    def __init__(self, sts: int, ext: int | None = None) -> None:
        super().__init__(f"PCCC STS 0x{sts:02X}" + (f" EXT STS 0x{ext:02X}" if ext is not None else ""))
        self.sts, self.ext = sts, ext


# --- values ----------------------------------------------------------------------------------------

def _int(value: Any, bits: int) -> int:
    """Signed or unsigned `bits`-wide integer -> its unsigned bit pattern."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{value!r} is not an integer")
    if not -(1 << (bits - 1)) <= value < (1 << bits):
        raise ValueError(f"{value} does not fit {bits} bits")
    return value & ((1 << bits) - 1)


def _swap(raw: bytes) -> bytes:
    """SLC strings: the two bytes of every word are swapped."""
    b = bytearray(raw)
    b[0::2], b[1::2] = b[1::2], b[0::2]
    return bytes(b)


def _encode(letter: str, value: Any) -> bytes:
    if letter in WORD_FILES:
        return struct.pack("<H", _int(value, 16))
    if letter == "L":
        return struct.pack("<I", _int(value, 32))
    if letter == "F":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{value!r} is not a number")
        try:
            return struct.pack("<f", float(value))
        except (OverflowError, struct.error) as err:
            raise ValueError(f"{value!r} does not fit float32") from err
    if letter in STRUCT_FILES:
        if not isinstance(value, (tuple, list)) or len(value) != 3:
            raise ValueError(f"{letter} element must be (control word, PRE, ACC), not {value!r}")
        return b"".join(struct.pack("<H", _int(v, 16)) for v in value)
    if letter == "ST":
        if not isinstance(value, str):
            raise ValueError(f"{value!r} is not a string")
        raw = value.encode("latin-1")
        if len(raw) > STRING_LEN:
            raise ValueError(f"string longer than {STRING_LEN}")
        return struct.pack("<H", len(raw)) + _swap(raw.ljust(STRING_LEN, b"\x00"))
    raise ValueError(f"file type {letter} is not simulated")


def _decode(letter: str, raw: bytes) -> Any:
    if letter in WORD_FILES:
        return struct.unpack("<h", raw)[0]
    if letter == "L":
        return struct.unpack("<i", raw)[0]
    if letter == "F":
        return struct.unpack("<f", raw)[0]
    if letter in STRUCT_FILES:
        return struct.unpack("<hhh", raw)
    n = struct.unpack_from("<H", raw)[0]
    return _swap(raw[2:])[:n].decode("latin-1")


class _DataFile:
    """One data table: raw little-endian bytes, as the PLC holds them."""

    def __init__(self, letter: str, number: int, values: list) -> None:
        self.letter, self.number = letter, number
        self.elem_size, codes = FILE_TYPES[letter]
        self.type_code, self.type_codes = codes[0], codes
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"{self.name}: table must be a list, not {type(values).__name__}")
        self.slots: list[int] | None = None          # I/O: words per slot
        if letter in IO_FILES and values and all(isinstance(v, (list, tuple)) for v in values):
            self.slots = [len(s) for s in values]
            values = [w for s in values for w in s]
        elif letter in IO_FILES:
            self.slots = [len(values)]
        if not values:
            raise ValueError(f"{self.name}: a data file needs at least one element")
        if letter not in IO_FILES and len(values) > MAX_ELEMENTS:
            raise ValueError(f"{self.name}: {len(values)} elements, a data file holds at most {MAX_ELEMENTS}")
        try:
            self.data = bytearray(b"".join(_encode(letter, v) for v in values))
        except ValueError as err:
            raise ValueError(f"{self.name}: {err}") from err

    @property
    def name(self) -> str:
        return f"{self.letter}{self.number}"

    @property
    def count(self) -> int:
        return len(self.data) // self.elem_size

    def slot_start(self, slot: int) -> int:
        assert self.slots is not None
        return sum(self.slots[:slot])

    def values(self) -> list:
        out = [_decode(self.letter, bytes(self.data[i:i + self.elem_size]))
               for i in range(0, len(self.data), self.elem_size)]
        if self.slots is not None and len(self.slots) > 1:
            return [out[self.slot_start(s):self.slot_start(s) + n] for s, n in enumerate(self.slots)]
        return out

    def byte_offset(self, element: int, sub: int) -> int:
        """Typed logical address -> byte offset, or STS 0x10 like the PLC."""
        if self.slots is not None:                   # I/O: element = slot, sub-element = word
            if element >= len(self.slots) or sub >= self.slots[element]:
                raise _PcccError(STS_ILLEGAL)
            return (self.slot_start(element) + sub) * 2
        if element >= self.count or sub * 2 >= self.elem_size:
            raise _PcccError(STS_ILLEGAL)
        return element * self.elem_size + sub * 2


@dataclass
class _Ref:
    """A resolved `set`/`value` address: `size` bytes at `start`, decoded as `letter` (or a bit)."""

    file: _DataFile
    start: int
    letter: str
    bit: int | None = None

    @property
    def size(self) -> int:
        return FILE_TYPES[self.letter][0]

    def get(self) -> Any:
        raw = bytes(self.file.data[self.start:self.start + self.size])
        if self.bit is not None:
            return bool((int.from_bytes(raw, "little") >> self.bit) & 1)
        return _decode(self.letter, raw)

    def put(self, value: Any) -> None:
        if self.bit is not None:
            word = int.from_bytes(self.file.data[self.start:self.start + self.size], "little")
            word = word | (1 << self.bit) if value else word & ~(1 << self.bit)
            raw = word.to_bytes(self.size, "little")
        else:
            raw = _encode(self.letter, value)
        self.file.data[self.start:self.start + self.size] = raw


def _file_key(key: str) -> tuple[str, int]:
    m = _FILE_KEY.fullmatch(key.strip().upper())
    if not m:
        raise ValueError(f"bad data file name {key!r} (expected e.g. N7, F8, B3, T4, ST9, L10, I, O, S)")
    letter = m["ft"]
    fixed = FIXED_FILE_NUMBER.get(letter)
    number = int(m["fn"]) if m["fn"] is not None else fixed
    if number is None:
        raise ValueError(f"data file {key!r} needs a file number")
    if fixed is not None and number != fixed:
        raise ValueError(f"the {letter} file is file {fixed}, not {number}")
    if not 0 <= number <= 255 or (fixed is None and number < 3):
        raise ValueError(f"file number {number} out of range for {letter} (3-255)")
    return letter, number


class PcccSim(CipSim):
    """Simulated SLC 500 / MicroLogix. Build with `await PcccSim.start(tables, ...)`.

    Statistics (reset with `reset_counters()`), besides CipSim's (`request_count` = CIP
    requests served, each Execute PCCC counts once; `service_counts`, `cm_counts`, ...):
      pccc_counts        Counter of (CMD, FNC) per PCCC command (FNC None for commands
                         without one): (0x0F, 0xA2) typed read, (0x06, 0x03) diagnostic status,
                         (0x0F, 0xA1) file-directory read, (0x0F, 0xAB) refused write ...
      write_attempts     refused PCCC write commands + refused CIP write/set services
      max_read_bytes     largest byte count asked by a typed read
    Knobs: `mute` (read, never answer), `file_errors` ({"N7": (sts, ext_sts or None)}: every
    typed read of that file gets this status, existing or not), `directory` (False: file 0
    cannot be read, so `get_file_directory()` fails).
    """

    def __init__(self, tables: dict[str, list], *, product_name: str, host: str, port: int,
                 large_forward_open: bool, directory: bool, revision: tuple[int, int] | None,
                 serial: int | None, product_code: int | None, processor_type: str | None) -> None:
        super().__init__({}, kind="logix", batching=False, large_forward_open=large_forward_open, slot=0,
                         host=host, port=port, revision=revision or _revision(product_name), plc_name="",
                         serial=serial if serial is not None else 0x40C0FFEE,
                         product_code=product_code or DEFAULT_PRODUCT_CODE)
        self.product_name = product_name
        self.processor_type = (processor_type or product_name.split()[0])[:11]
        self.layout = sys0_layout(self.processor_type)
        self.directory_enabled = directory
        self.file_errors: dict[str, tuple[int, int | None]] = {}
        self._files: dict[int, _DataFile] = {}
        for key, values in tables.items():
            letter, number = _file_key(key)
            if number in self._files:
                raise ValueError(f"file number {number} used twice ({self._files[number].name}, {key})")
            self._files[number] = _DataFile(letter, number, values)

    # --- public API ------------------------------------------------------------------------------

    @classmethod
    async def start(cls, tables: dict[str, list], *, product_name: str = DEFAULT_PRODUCT_NAME,  # type: ignore[override]
                    host: str = SIM_HOST, port: int = 0, large_forward_open: bool = False,
                    directory: bool = True, revision: tuple[int, int] | None = None,
                    serial: int | None = None, product_code: int | None = None,
                    processor_type: str | None = None) -> PcccSim:
        """Start listening (port 0 = ephemeral; use `netcut.stable_port()` if you will restart).

        `tables`: "N7" -> [int], "F8" -> [float], "B3" -> [int words], "L9" -> [int],
        "T4"/"C5"/"R6" -> [(control word, PRE, ACC)], "ST9" -> [str], "S" -> [words],
        "I"/"O" -> [words] (slot 0) or [[slot 0 words], [slot 1 words], ...].
        16-bit values may be given signed or unsigned (65535 == -1); reads return them signed.
        `revision` defaults to the "/<major>.<minor>" in the product name; `processor_type`
        (the diagnostic-status catalog number) to the product name's first word.
        """
        sim = cls(tables, product_name=product_name, host=host, port=port,
                  large_forward_open=large_forward_open, directory=directory, revision=revision,
                  serial=serial, product_code=product_code, processor_type=processor_type)
        await sim._listen()
        return sim

    @property
    def path(self) -> str:
        """pycomm3 `SLCDriver` path: `host:port` (pycomm3 adds the backplane segment itself)."""
        return f"{self.host}:{self.port}"

    @property
    def files(self) -> dict[str, int]:
        """Data files and their element counts (I/O: words), by file number."""
        return {f.name: f.count for _, f in sorted(self._files.items())}

    def directory(self) -> dict[str, dict[str, int]]:
        """What pycomm3 `get_file_directory()` returns for this PLC ("O0", "I1", "S2", "N7", ...)."""
        return {f.name: {"elements": f.count, "length": len(f.data)} for _, f in sorted(self._files.items())}

    def reset_counters(self) -> None:
        super().reset_counters()
        self.pccc_counts: Counter[tuple[int, int | None]] = Counter()
        self.max_read_bytes = 0

    def set(self, address: str, value: Any) -> None:  # type: ignore[override]
        """Fixture setup. "N7:3", "F8:0", "B3:1/4", "B3/17" (word 1 bit 1), "L9:0/20", "T4:0.ACC",
        "T4:0.DN", "T4:0" (control, PRE, ACC), "ST9:0", "S:5", "I:0.1", "I:1/3", "O:0".
        A bare file name ("N7", "I") replaces (or creates) the whole file, like a download."""
        if _FILE_KEY.fullmatch(address.strip().upper()):
            letter, number = _file_key(address)
            self._files[number] = _DataFile(letter, number, value)
            return
        ref = self._ref(address)
        try:
            ref.put(value)
        except ValueError as err:
            raise ValueError(f"{address}: {err}") from err

    def value(self, address: str) -> Any:  # type: ignore[override]
        """What the PLC holds, as pycomm3 decodes it: N/B/S/I/O/T/C words signed, F float32,
        bits bool, T/C/R elements (control, PRE, ACC), a bare file name -> list."""
        if _FILE_KEY.fullmatch(address.strip().upper()):
            return self._file(*_file_key(address), address).values()
        return self._ref(address).get()

    def remove(self, name: str) -> None:
        """Delete a data file ("N7"), like a program download: later reads get STS 0x10."""
        letter, number = _file_key(name)
        self._file(letter, number, name)
        del self._files[number]

    # --- address resolution (fixture side) -------------------------------------------------------

    def _file(self, letter: str, number: int, address: str) -> _DataFile:
        f = self._files.get(number)
        if f is None or f.letter != letter:
            raise KeyError(f"{address}: file {letter}{number} does not exist")
        return f

    def _ref(self, address: str) -> _Ref:
        text = address.strip().upper()
        if m := _ELEMENT_ADDR.fullmatch(text):
            f = self._file(m["ft"], int(m["fn"]), address)
            return _Ref(f, self._element_start(f, int(m["el"]), address), f.letter)
        addr = parse_pccc_address(text)                  # the driver's own address model
        f = self._file(addr.file_type, addr.file_number, address)
        if f.letter in IO_FILES:
            assert f.slots is not None
            if addr.element >= len(f.slots) or addr.word >= f.slots[addr.element]:
                raise KeyError(f"{address}: past the end of {f.name}")
            return _Ref(f, (f.slot_start(addr.element) + addr.word) * 2, f.letter, addr.bit)
        start = self._element_start(f, addr.element, address)
        if addr.field in TC_WORDS:
            return _Ref(f, start + 2 * TC_WORDS[addr.field], "N")
        if addr.field is not None:
            return _Ref(f, start, "N", TC_BITS[addr.field])
        return _Ref(f, start, f.letter, addr.bit)

    @staticmethod
    def _element_start(f: _DataFile, element: int, address: str) -> int:
        if element >= f.count:
            raise KeyError(f"{address}: past the end of {f.name} ({f.count} elements)")
        return element * f.elem_size

    # --- CIP: the PCCC object --------------------------------------------------------------------

    def _object(self, service: int, segs: list[tuple[str, Any]], data: bytes, space: int) -> bytes:
        if service == SVC_EXECUTE_PCCC:
            self.service_counts[service] += 1
            if segs != PCCC_OBJECT_PATH:
                raise _CipError(GS_PATH_UNKNOWN, 0)
            reply = _reply(service, GS_OK, self._execute_pccc(data))
            if len(reply) > space:
                raise _CipError(GS_REPLY_TOO_LARGE)
            return reply
        if service in WRITE_SERVICES or (
                service in (SVC_GET_ATTRIBUTES_ALL, SVC_GET_ATTRIBUTE_SINGLE) and segs[:2] == IDENTITY_PATH):
            return super()._object(service, segs, data, space)       # refused writes / identity
        self.service_counts[service] += 1
        raise _CipError(GS_NOT_SUPPORTED)        # no Logix tag services, MSP, program name ...

    def _execute_pccc(self, data: bytes) -> bytes:
        rid_len = data[0] if data else 0
        if rid_len < 7 or len(data) < rid_len + 4:
            raise _CipError(GS_NOT_ENOUGH_DATA)
        requester = data[:rid_len]
        cmd, tns = data[rid_len], struct.unpack_from("<H", data, rid_len + 2)[0]
        body, fnc = data[rid_len + 4:], None
        try:
            if cmd in FNC_COMMANDS:
                if not body:
                    raise _PcccError(STS_ILLEGAL)
                fnc, body = body[0], body[1:]
            self.pccc_counts[(cmd, fnc)] += 1
            out, sts, ext = self._pccc_command(cmd, fnc, body), STS_OK, None
        except _PcccError as err:
            if fnc is None and cmd in FNC_COMMANDS:
                self.pccc_counts[(cmd, None)] += 1
            out, sts, ext = b"", err.sts, err.ext
        head = requester + bytes([cmd | 0x40, sts]) + struct.pack("<H", tns)
        if sts == STS_EXT:
            head += bytes([ext or 0])
        return head + out

    def _pccc_command(self, cmd: int, fnc: int | None, body: bytes) -> bytes:
        if cmd in WRITE_COMMANDS or fnc in WRITE_FNC.get(cmd, ()):
            self.write_attempts += 1
            raise _PcccError(STS_PROTECTED)
        if cmd == CMD_EXTENDED and fnc in (FNC_TYPED_READ_2, FNC_TYPED_READ_3):
            return self._typed_read(body, three_fields=fnc == FNC_TYPED_READ_3)
        if cmd == CMD_DIAGNOSTIC and fnc == FNC_DIAGNOSTIC_STATUS:
            return self._diagnostic_status()
        if cmd == CMD_DIAGNOSTIC and fnc == FNC_ECHO:
            return body
        raise _PcccError(STS_ILLEGAL)

    def _diagnostic_status(self) -> bytes:
        # mode/status, type extender 0xEE, extended interface type, extended processor type,
        # series/revision, catalog number (11 ASCII chars: pycomm3 reads bytes 5..15), more status
        catalog = self.processor_type.ljust(11).encode("ascii")
        return bytes([0x06, 0xEE, 0x36, 0x9C, 0x1E]) + catalog + bytes(8)

    def _typed_read(self, body: bytes, *, three_fields: bool) -> bytes:
        try:
            size, i = body[0], 1
            number, i = _address_field(body, i)
            file_type, i = body[i], i + 1
            element, i = _address_field(body, i)
            sub = 0
            if three_fields:
                sub, i = _address_field(body, i)
        except (IndexError, struct.error) as err:
            raise _PcccError(STS_ILLEGAL) from err
        self.max_read_bytes = max(self.max_read_bytes, size)
        if number == 0 and file_type == self.layout.file_type:     # system file 0: the directory
            if not self.directory_enabled:
                raise _PcccError(STS_ILLEGAL)
            raw, offset = self._system_file(), (element + sub) * 2
        else:
            f = self._files.get(number)
            letter = next((k for k, (_, codes) in FILE_TYPES.items() if file_type in codes), None)
            for key, (sts, ext) in self.file_errors.items():         # "S" and "S2" are the same file
                if _file_key(key) == (letter, number):
                    raise _PcccError(sts, ext)
            if f is None or file_type not in f.type_codes:
                raise _PcccError(STS_ILLEGAL)
            raw, offset = f.data, f.byte_offset(element, sub)
        if size == 0 or offset + size > len(raw):
            raise _PcccError(STS_ILLEGAL)
        return bytes(raw[offset:offset + size])

    def _system_file(self) -> bytes:
        lay = self.layout
        rows = max(self._files, default=-1) + 1
        size = lay.file_position + rows * lay.row_size
        buf = bytearray(size)
        buf[46] = 3                                              # program files (SYS0, SYS1, LAD2)
        buf[52] = len(self._files)                               # data files
        struct.pack_into("<H", buf, 2 * lay.size_element, size + lay.size_const)
        for number in range(rows):
            at = lay.file_position + number * lay.row_size
            f = self._files.get(number)
            if f is None:
                buf[at] = UNUSED_FILE_ROW
            else:
                buf[at] = f.type_code
                struct.pack_into("<H", buf, at + 1, len(f.data))
        return bytes(buf)


def _address_field(body: bytes, i: int) -> tuple[int, int]:
    """PCCC logical address field: one byte, or 0xFF + UINT for values >= 255."""
    if body[i] == 0xFF:
        return struct.unpack_from("<H", body, i + 1)[0], i + 3
    return body[i], i + 1


def _revision(product_name: str) -> tuple[int, int]:
    m = re.search(r"/(\d+)\.(\d+)", product_name)
    return (int(m[1]), int(m[2])) if m else (1, 0)


# --- tables for a tag list -------------------------------------------------------------------------

def tables_for(tags: list[Tag], *, seed: int = 0, pad: int = 0, system_files: bool = False) -> dict[str, list]:
    """Deterministic data tables that cover every PCCC address in `tags`.

    Addresses are parsed with the driver's `parse_pccc_address`; tags it rejects (symbolic
    names, RTC and other function files) are skipped. Every file is exactly as long as the
    highest element used (+ `pad`), so a block that reads past the last tag fails like on the
    PLC. Values depend only on `seed` and the file name. I/O files get one list per slot when
    a slot other than 0 is used.

    A PLC file number has one type. When the tags use one number with two types (the real
    project has `F54:17` among 36 `N54` tags on one device), the type most tags use wins and
    the other addresses stay uncovered, so they fail with STS 0x10 as on the PLC; data files on
    numbers 0-2 (O0, I1, S2 on every SLC/MicroLogix) are left out the same way.
    """
    need: dict[tuple[str, int], int] = {}
    io: dict[str, dict[int, int]] = {}
    votes: Counter[tuple[int, str]] = Counter()
    for tag in tags:
        try:
            addr = parse_pccc_address(tag.address)
        except ValueError:
            continue
        if addr.file_number < 3 and addr.file_type not in FIXED_FILE_NUMBER:
            continue                                                       # e.g. N1:0, B2:0
        votes[(addr.file_number, addr.file_type)] += 1
        if addr.file_type in IO_FILES:
            slots = io.setdefault(addr.file_type, {})
            slots[addr.element] = max(slots.get(addr.element, 0), addr.word + 1)
            continue
        key = (addr.file_type, addr.file_number)
        need[key] = max(need.get(key, 0), addr.element + 1)
    owner: dict[int, str] = {}
    for number, letter in sorted(votes, key=lambda k: (-votes[k], k)):    # most tags, then file name
        owner.setdefault(number, letter)

    tables: dict[str, list] = {}
    for (letter, number), count in sorted(need.items(), key=lambda kv: kv[0][1]):
        if owner[number] != letter:
            continue
        name = "S" if letter == "S" else f"{letter}{number}"
        rng = random.Random(f"{seed}:{name}")
        tables[name] = [_random_value(letter, rng, name, i) for i in range(min(count + pad, MAX_ELEMENTS))]
    for letter, slots in sorted(io.items()):
        rng = random.Random(f"{seed}:{letter}")
        words = [[_random_value(letter, rng, letter, i) for i in range(slots.get(s, 1) + pad)]
                 for s in range(max(slots) + 1)]
        tables[letter] = words[0] if len(words) == 1 else words
    if system_files:
        # Every SLC/MicroLogix has O0, I1 and S2. The ab_pccc driver only trusts a file
        # directory that starts with them, so a realistic PLC image includes them.
        tables.setdefault("O", [0] * 4)
        tables.setdefault("I", [0] * 4)
        tables.setdefault("S", [0] * 66)
    return tables


def _random_value(letter: str, rng: random.Random, name: str, i: int) -> Any:
    if letter == "F":
        return struct.unpack("<f", struct.pack("<f", round(rng.uniform(-1000.0, 1000.0), 3)))[0]
    if letter == "L":
        return rng.randint(-(1 << 31), (1 << 31) - 1)
    if letter in STRUCT_FILES:
        pre = rng.randint(0, 30_000)
        control = rng.getrandbits(16) & 0xE000                    # EN/TT/DN or CU/CD/DN
        return (control - 0x10000 if control & 0x8000 else control, pre, rng.randint(0, pre))
    if letter == "ST":
        return f"{name}:{i} {rng.getrandbits(32):08x}"
    return rng.randint(-32768, 32767)                             # N, B, S, I, O: signed words


def file_type_conflicts(tags: list[Tag]) -> set[str]:
    """Addresses whose file number other tags use with a different file type.

    A PLC file number has one type, so the minority addresses cannot read (the real project has
    one `F54:17` among 36 `N54` tags). Same ownership rule as `tables_for`: most tags, then
    file name.
    """
    votes: Counter[tuple[int, str]] = Counter()
    entries: list[tuple[str, int, str]] = []
    for tag in tags:
        try:
            addr = parse_pccc_address(tag.address)
        except ValueError:
            continue
        if addr.file_type in IO_FILES or (addr.file_number < 3 and addr.file_type not in FIXED_FILE_NUMBER):
            continue
        votes[(addr.file_number, addr.file_type)] += 1
        entries.append((tag.address, addr.file_number, addr.file_type))
    owner: dict[int, str] = {}
    for number, letter in sorted(votes, key=lambda k: (-votes[k], k)):
        owner.setdefault(number, letter)
    return {address for address, number, letter in entries if owner[number] != letter}
