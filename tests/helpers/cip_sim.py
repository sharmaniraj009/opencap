"""An EtherNet/IP + CIP server that impersonates a Rockwell Logix controller or a Micro800.

    sim = await CipSim.start(make_logix_tags(50))            # 1756-L61, rev 20.11, slot 0
    plc = LogixDriver(sim.path)                               # "127.0.0.1:<port>/0" (Micro800: no slot)
    await asyncio.to_thread(plc.open)                         # pycomm3 is synchronous: run it in a thread
    sim.set("REAL_0001", 1.5); sim.value("UDT_0000.TOTAL")    # fixture setup, not a CIP write
    sim.mute = True                                           # read requests, never answer (client times out)
    sim.remove("DINT_0002")                                   # tag vanishes after the upload (wire path error)
    await sim.stop(); await sim.restart()                     # power-off / power-on on the same port

It is the test double for the real Micro800 and ControlLogix PLCs, so it is written against
the bytes stock pycomm3 1.2.16 `LogixDriver` sends and parses (read `pycomm3/logix_driver.py`,
`cip_driver.py`, `packets/*`). `cpppo` cannot serve pycomm3 (no Get Instance Attribute List).

Wire facts matched
------------------
* Encapsulation (TCP, 24-byte header): RegisterSession 0x65 (protocol 1, options 0),
  UnRegisterSession 0x66 (no reply; we close the socket like a PLC), ListIdentity 0x63 (also
  ListServices 0x04, ListInterfaces 0x64, NOP), SendRRData 0x6F (unconnected, null address +
  0xB2 item) and SendUnitData 0x70 (connected: 0xA1 address with the O->T connection id we
  issued + 0xB1 item = UINT sequence count + CIP message). pycomm3 parses replies at fixed
  offsets (SendRRData: service at byte 40, status 42, data 44; SendUnitData: 46/48/50), so a
  *successful* reply never carries extended status. A repeated sequence count on a connection
  gets the previous reply again without re-executing (class 3 duplicate detection).
* `LogixDriver.open()`: RegisterSession -> ListIdentity (product name prefix "2080" makes it a
  Micro800) -> Get Attributes All on Identity (class 1/1): Logix wraps it in Unconnected Send
  0x52 with route `[port 1, slot]`; Micro800 sends it bare with the route path *appended after*
  the request (the sim ignores trailing bytes, like the PLC) -> Logix only: Large Forward Open
  0x5B (connection size 4000, 32-bit network parameters), on refusal Forward Open 0x54 (size 500,
  16-bit parameters), then Get Attributes All on class 0x64 (program name, UINT-length string) ->
  Micro800: pycomm3 strips the backplane segment, so its Forward Open carries no port segment ->
  Get Instance Attribute List 0x55 on Symbol class 0x6B from instance 0, attributes 1,2,3,5,6,8
  (+10 external access when revision major >= 18); status 0x06 means "more, continue from last
  instance + 1" -> per UDT: Get Attribute List 0x03 on Template 0x6C (attrs 4,5,2,1) and Read
  Tag 0x4C on the template (DINT offset, UINT byte count = definition words*4 - 21) -> for every
  "Program:X" symbol a program-scoped 0x55 (path: ANSI symbol "Program:X" + class 0x6B).
* Addressing: pycomm3 uses Symbol Instance addressing (class 0x6B + instance id) only when the
  identity revision major >= 21 and the target is not a Micro800; otherwise ANSI extended
  symbol segments (0x91). Array index = member logical segment, UDT member = another 0x91
  segment. The sim accepts both on Logix and only symbolic on Micro800.
* Reads: Read Tag 0x4C (UINT element count), Read Tag Fragmented 0x52 (+ UDINT byte offset,
  status 0x06 until the last fragment), Multiple Service Packet 0x0A on the Message Router
  (2/1). Reply type field: UINT atomic code, or 0xA0 0x02 + UINT structure handle for
  structures (incl. Logix STRING). Every connected reply (sequence count + CIP reply) fits in
  the negotiated connection size; MSP members that do not fit get status 0x06, a Read Tag that
  does not fit gets 0x06 with partial data. pycomm3 itself sends no MSP to a Micro800.
* Errors: unknown symbol -> 0x04 (path segment error) + ext 0x0000; unknown instance id,
  unknown class -> 0x05; elements past the end -> 0xFF/0x2105; MSP with a failed member ->
  0x1E; refused services -> 0x08 with no data. NB pycomm3 itself reports "Tag doesn't exist"
  client-side, before the wire, for names missing from its uploaded tag list.

Micro800 vs Logix: Micro800 has no program-name object, no Unconnected Send, no routing,
STRING is CIP SHORT_STRING 0xDA (1-byte length) instead of the Logix STRING structure
(DINT LEN + SINT[82]), no system symbols in the tag list, symbolic addressing only.

Read-only: Write Tag 0x4D, Read-Modify-Write 0x4E, Write Tag Fragmented 0x53 (and the
Set Attribute services 0x02/0x04/0x10) are answered 0x08 "service not supported" and counted
in `write_attempts`. `set()` is test-fixture setup straight into the tag store.

Not simulated: multi-dimensional arrays, Logix BOOL arrays (DWORD-packed), arrays of UDTs,
nested UDTs or STRING members, reading STRING .LEN/.DATA, program-scoped tags, I/O module tags, connection timeouts
(RPI x multiplier), connection limits, CIP security, UDP ListIdentity discovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import random
import re
import socket
import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

SIM_HOST = "127.0.0.1"
LOGIX_PRODUCT_NAME = "1756-L61/B LOGIX5561"
MICRO800_PRODUCT_NAME = "2080-LC50-48QWB"
# 1756-L61 firmware stops at v20: pycomm3 then uses symbolic addressing (instance ids need >= 21)
# and requests the external-access attribute (>= 18). Micro800 at 12.x: no attribute 10.
DEFAULT_REVISION = {"logix": (20, 11), "micro800": (12, 11)}

# --- encapsulation -------------------------------------------------------------------------------
ENCAP_HEADER = struct.Struct("<HHII8sI")      # command, length, session, status, context, options
NOP, LIST_SERVICES, LIST_IDENTITY, LIST_INTERFACES = 0x0000, 0x0004, 0x0063, 0x0064
REGISTER_SESSION, UNREGISTER_SESSION, SEND_RR_DATA, SEND_UNIT_DATA = 0x0065, 0x0066, 0x006F, 0x0070
ENCAP_INVALID_COMMAND, ENCAP_INCORRECT_DATA = 0x0001, 0x0003
ENCAP_INVALID_SESSION, ENCAP_UNSUPPORTED_PROTOCOL = 0x0064, 0x0069
CPF_NULL, CPF_IDENTITY, CPF_CONNECTED_ADDR = 0x0000, 0x000C, 0x00A1
CPF_CONNECTED_DATA, CPF_UNCONNECTED_DATA, CPF_SERVICES = 0x00B1, 0x00B2, 0x0100

# --- CIP -------------------------------------------------------------------------------------------
GS_OK, GS_CONNECTION_FAILURE, GS_PATH_SEGMENT, GS_PATH_UNKNOWN = 0x00, 0x01, 0x04, 0x05
GS_PARTIAL, GS_NOT_SUPPORTED, GS_ATTR_LIST, GS_REPLY_TOO_LARGE = 0x06, 0x08, 0x0A, 0x11
GS_NOT_ENOUGH_DATA, GS_ATTR_NOT_SUPPORTED, GS_EMBEDDED_ERROR, GS_GENERAL = 0x13, 0x14, 0x1E, 0xFF
EXT_CONNECTION_IN_USE, EXT_TRANSPORT_NOT_SUPPORTED, EXT_CONNECTION_NOT_FOUND = 0x0100, 0x0103, 0x0107
EXT_INVALID_CONNECTION_SIZE, EXT_PORT_NOT_AVAILABLE = 0x0109, 0x0311
EXT_LINK_NOT_AVAILABLE, EXT_INVALID_SEGMENT = 0x0312, 0x0315
EXT_OFFSET_OUT_OF_RANGE, EXT_BEYOND_END = 0x2104, 0x2105

SVC_GET_ATTRIBUTES_ALL, SVC_GET_ATTRIBUTE_LIST, SVC_MULTIPLE = 0x01, 0x03, 0x0A
SVC_GET_ATTRIBUTE_SINGLE, SVC_READ_TAG, SVC_READ_FRAGMENTED = 0x0E, 0x4C, 0x52
SVC_GET_INSTANCE_ATTRIBUTE_LIST = 0x55
WRITE_SERVICES = frozenset({0x02, 0x04, 0x10, 0x4D, 0x4E, 0x53})   # set-attr*, write, RMW, write frag
CM_FORWARD_CLOSE, CM_UNCONNECTED_SEND, CM_FORWARD_OPEN, CM_LARGE_FORWARD_OPEN = 0x4E, 0x52, 0x54, 0x5B

CLASS_IDENTITY, CLASS_MESSAGE_ROUTER, CLASS_CONNECTION_MANAGER = 0x01, 0x02, 0x06
CLASS_PROGRAM_NAME, CLASS_SYMBOL, CLASS_TEMPLATE = 0x64, 0x6B, 0x6C
CM_PATH = [("class", CLASS_CONNECTION_MANAGER), ("instance", 1)]
ROUTER_PATH = [("class", CLASS_MESSAGE_ROUTER), ("instance", 1)]

UCMM_MAX_REPLY = 504            # largest unconnected CIP reply
LARGE_CONNECTION_MAX = 4002     # Large Forward Open: 16-bit size, Logix accepts up to 4002
STANDARD_CONNECTION_MAX = 511   # Forward Open: 9-bit size field
VENDOR_ROCKWELL, DEVICE_TYPE_PLC = 1, 0x0E
STATUS_REMOTE_RUN = 0x3060      # bytes 0x60 0x30 -> pycomm3 KEYSWITCH "REMOTE RUN"
STATE_OPERATIONAL = 3
SOFTWARE_CONTROL_BASE_TAG = 1 << 26   # pycomm3 BASE_TAG_BIT: set = base tag, not an alias
SYSTEM_SYMBOL = 0x1000                 # symbol type bit 12: pycomm3 skips these
USER_INSTANCE_BASE, USER_INSTANCE_STEP = 0x40, 3   # sparse ids; > 255 soon (16-bit segments)
UDT_TEMPLATE_BASE = 0x0A10                          # user templates live in 0x100..0xEFF
STRING_TEMPLATE_ID = 0x0FCE                         # predefined Logix STRING

_CLOSE = object()


class _CipError(Exception):
    def __init__(self, status: int, *ext: int, data: bytes = b"") -> None:
        super().__init__(f"CIP status 0x{status:02X} ext {[hex(e) for e in ext]}")
        self.status, self.ext, self.data = status, ext, data


def _reply(service: int, status: int = GS_OK, data: bytes = b"", ext: tuple[int, ...] = ()) -> bytes:
    return bytes([service | 0x80, 0, status, len(ext)]) + b"".join(struct.pack("<H", e) for e in ext) + data


def _encap(command: int, session: int, context: bytes, payload: bytes = b"", status: int = 0) -> bytes:
    return ENCAP_HEADER.pack(command, len(payload), session, status, context, 0) + payload


def _cpf(*items: tuple[int, bytes]) -> bytes:
    out = struct.pack("<IHH", 0, 0, len(items))     # interface handle, timeout, item count
    return out + b"".join(struct.pack("<HH", t, len(d)) + d for t, d in items)


def _parse_cpf(body: bytes) -> list[tuple[int, bytes]]:
    _iface, _timeout, count = struct.unpack_from("<IHH", body, 0)
    off, items = 8, []
    for _ in range(count):
        typ, length = struct.unpack_from("<HH", body, off)
        off += 4
        if off + length > len(body):
            raise ValueError("truncated CPF item")
        items.append((typ, body[off:off + length]))
        off += length
    return items


_LOGICAL_TYPES = ("class", "instance", "member", "connection_point", "attribute", "special", "service")


def _parse_epath(buf: bytes) -> list[tuple[str, Any]]:
    """Padded EPATH -> [(kind, value)]: logical (8/16/32-bit), ANSI extended symbol, port."""
    segs: list[tuple[str, Any]] = []
    i = 0
    try:
        while i < len(buf):
            seg = buf[i]
            if seg == 0x91:                                    # ANSI extended symbol
                n = buf[i + 1]
                name = buf[i + 2:i + 2 + n]
                if len(name) != n:
                    raise _CipError(GS_PATH_SEGMENT, 0)
                segs.append(("symbol", name.decode("latin-1")))
                i += 2 + n + (n & 1)
            elif seg & 0xE0 == 0x20:                           # logical
                kind, fmt = _LOGICAL_TYPES[(seg >> 2) & 0x07], seg & 0x03
                if fmt == 0:
                    value, i = buf[i + 1], i + 2
                elif fmt == 1:
                    value, i = struct.unpack_from("<H", buf, i + 2)[0], i + 4
                elif fmt == 3:
                    value, i = struct.unpack_from("<I", buf, i + 2)[0], i + 6
                else:
                    raise _CipError(GS_PATH_SEGMENT, 0)
                segs.append((kind, value))
            elif seg & 0xE0 == 0x00:                           # port
                port = seg & 0x0F
                if seg & 0x10:                                 # extended link address
                    n = buf[i + 1]
                    link = bytes(buf[i + 2:i + 2 + n])
                    i += 2 + n + (n & 1)
                else:
                    link = bytes(buf[i + 1:i + 2])
                    i += 2
                segs.append(("port", (port, link)))
            else:
                raise _CipError(GS_PATH_SEGMENT, 0)
    except (IndexError, struct.error) as err:
        raise _CipError(GS_PATH_SEGMENT, 0) from err
    return segs


def _split_request(msg: bytes) -> tuple[int, list[tuple[str, Any]], bytes]:
    if len(msg) < 2:
        raise _CipError(GS_NOT_ENOUGH_DATA)
    words = msg[1]
    end = 2 + 2 * words
    if len(msg) < end:
        raise _CipError(GS_PATH_SEGMENT, 0)
    return msg[0], _parse_epath(msg[2:end]), msg[end:]


def _round_up(n: int, align: int) -> int:
    return -(-n // align) * align


# --- data types ------------------------------------------------------------------------------------

class _Atomic:
    is_struct = False

    def __init__(self, name: str, code: int, size: int, fmt: str) -> None:
        self.name, self.code, self.size, self.fmt = name, code, size, fmt

    def type_bytes(self) -> bytes:
        return struct.pack("<H", self.code)

    def symbol_type(self) -> int:
        return self.code

    def default(self) -> Any:
        return False if self.name == "BOOL" else 0.0 if self.name in ("REAL", "LREAL") else 0

    def coerce(self, value: Any) -> Any:
        if value is None:
            return self.default()
        if self.name == "BOOL":
            return bool(value)
        if self.name in ("REAL", "LREAL"):
            try:
                return struct.unpack(self.fmt, struct.pack(self.fmt, float(value)))[0]  # float32 rounding
            except (OverflowError, struct.error) as err:
                raise ValueError(f"{value!r} does not fit {self.name}") from err
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise ValueError(f"{value!r} is not an integer for {self.name}")
        try:
            struct.pack(self.fmt, value)
        except struct.error as err:
            raise ValueError(f"{value!r} out of range for {self.name}") from err
        return int(value)

    def encode(self, value: Any) -> bytes:
        if self.name == "BOOL":
            return b"\x01" if value else b"\x00"
        return struct.pack(self.fmt, value)


ATOMICS: dict[str, _Atomic] = {
    a.name: a for a in (
        _Atomic("BOOL", 0xC1, 1, "<B"), _Atomic("SINT", 0xC2, 1, "<b"), _Atomic("INT", 0xC3, 2, "<h"),
        _Atomic("DINT", 0xC4, 4, "<i"), _Atomic("LINT", 0xC5, 8, "<q"), _Atomic("USINT", 0xC6, 1, "<B"),
        _Atomic("UINT", 0xC7, 2, "<H"), _Atomic("UDINT", 0xC8, 4, "<I"), _Atomic("ULINT", 0xC9, 8, "<Q"),
        _Atomic("REAL", 0xCA, 4, "<f"), _Atomic("LREAL", 0xCB, 8, "<d"),
    )
}


class _ShortString:
    """Micro800 STRING: CIP SHORT_STRING (0xDA), 1-byte length + characters."""

    is_struct = False
    name, code, size, max_len = "STRING", 0xDA, None, 255

    def type_bytes(self) -> bytes:
        return struct.pack("<H", self.code)

    def symbol_type(self) -> int:
        return self.code

    def default(self) -> str:
        return ""

    def coerce(self, value: Any) -> str:
        return _coerce_str(value, self.max_len)

    def encode(self, value: str) -> bytes:
        return bytes([len(value)]) + value.encode("latin-1")


def _coerce_str(value: Any, max_len: int) -> str:
    value = "" if value is None else value
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a string")
    value.encode("latin-1")
    if len(value) > max_len:
        raise ValueError(f"string longer than {max_len}")
    return value


@dataclass
class _Member:
    name: str
    etype: _Atomic
    count: int = 0                 # 0 = scalar, n = array
    offset: int = 0
    bit: int | None = None         # BOOL members: bit in the hidden host SINT
    hidden: bool = False


class _Template:
    """A Logix structure (UDT or the predefined STRING): layout, template definition, encoding."""

    is_struct = True

    def __init__(self, name: str, instance: int, spec: list[tuple[str, _Atomic, int]], *,
                 string_len: int | None = None) -> None:
        self.name, self.instance, self.string_len = name, instance, string_len
        self.handle = instance if string_len else (0x5A00 ^ (instance * 0x9E37)) & 0xFFFF   # "CRC"
        self.members: list[_Member] = []
        offset, host, host_bits, hosts, align_max = 0, None, 8, 0, 4
        for mname, etype, count in spec:
            if etype.name == "BOOL" and count:
                raise ValueError(f"{name}.{mname}: BOOL arrays in UDTs are not simulated")
            if etype.name == "BOOL":
                if host is None or host_bits == 8:    # consecutive BOOLs share a hidden SINT host
                    host = _Member(f"ZZZZZZZZZZ{name}{hosts}", ATOMICS["SINT"], 0, offset, hidden=True)
                    self.members.append(host)
                    hosts, host_bits, offset = hosts + 1, 0, offset + 1
                self.members.append(_Member(mname, etype, 0, host.offset, bit=host_bits))
                host_bits += 1
                continue
            host = None
            align = etype.size if not count else max(4, etype.size)
            align_max = max(align_max, align)
            offset = _round_up(offset, align)
            self.members.append(_Member(mname, etype, count, offset))
            offset += etype.size * max(count, 1)
        self.size = _round_up(max(offset, 1), align_max)
        self._by_name = {m.name.upper(): m for m in self.members if not m.hidden}
        info = b"".join(
            struct.pack("<HHI", m.bit if m.bit is not None else m.count, m.etype.code, m.offset)
            for m in self.members
        )
        type_name = "ASCIISTRING82" if string_len else name
        names = "\x00".join([f"{type_name};n", *(m.name for m in self.members)]) + "\x00"
        definition = info + names.encode("latin-1")
        self.definition = definition + bytes(_round_up(len(definition), 4) - len(definition))
        # pycomm3 reads (definition_words * 4 - 21) bytes; Rockwell documents "- 23"
        self.definition_words = _round_up(len(self.definition) + 23, 4) // 4

    def type_bytes(self) -> bytes:
        return b"\xa0\x02" + struct.pack("<H", self.handle)

    def symbol_type(self) -> int:
        return 0x8000 | self.instance

    def member(self, name: str) -> _Member | None:
        return self._by_name.get(name.upper())

    def visible(self) -> list[_Member]:
        return [m for m in self.members if not m.hidden]

    def default(self) -> Any:
        if self.string_len:
            return ""
        return {m.name: ([m.etype.default()] * m.count if m.count else m.etype.default()) for m in self.visible()}

    def coerce(self, value: Any, base: Any = None) -> Any:
        if self.string_len:
            return _coerce_str(value, self.string_len)
        out = copy.deepcopy(base) if base is not None else self.default()
        for key, v in (value or {}).items():
            m = self.member(key)
            if m is None:
                raise KeyError(f"{self.name} has no member {key!r}")
            out[m.name] = _coerce_array(m.etype, m.count, v) if m.count else m.etype.coerce(v)
        return out

    def encode(self, value: Any) -> bytes:
        buf = bytearray(self.size)
        if self.string_len:
            raw = value.encode("latin-1")
            struct.pack_into("<i", buf, 0, len(raw))
            buf[4:4 + len(raw)] = raw
            return bytes(buf)
        for m in self.members:
            if m.hidden:
                continue
            v = value[m.name]
            if m.bit is not None:
                if v:
                    buf[m.offset] |= 1 << m.bit
            elif m.count:
                for i, x in enumerate(v):
                    at = m.offset + i * m.etype.size
                    buf[at:at + m.etype.size] = m.etype.encode(x)
            else:
                buf[m.offset:m.offset + m.etype.size] = m.etype.encode(v)
        return bytes(buf)


def _coerce_array(etype: Any, count: int, value: Any) -> list:
    if value is None:
        return [etype.default() for _ in range(count)]
    if isinstance(value, (list, tuple)):
        if len(value) != count:
            raise ValueError(f"expected {count} elements, got {len(value)}")
        return [etype.coerce(v) for v in value]
    return [etype.coerce(value) for _ in range(count)]     # scalar: fill


@dataclass
class _Tag:
    name: str
    instance: int
    etype: Any                  # _Atomic | _ShortString | _Template
    count: int                  # 0 = scalar
    values: list                # one entry for scalars

    def symbol_type(self) -> int:
        return self.etype.symbol_type() | ((1 << 13) if self.count else 0)

    def dimensions(self) -> tuple[int, int, int]:
        return (self.count, 0, 0)

    def element_size(self) -> int:
        return self.etype.size or 0


@dataclass
class _Symbol:                  # non-user entries of the tag list (filtered out by pycomm3)
    name: str
    instance: int
    type_code: int

    def symbol_type(self) -> int:
        return self.type_code

    def dimensions(self) -> tuple[int, int, int]:
        return (0, 0, 0)

    def element_size(self) -> int:
        return 4


@dataclass
class _Ref:
    """A resolved tag path: a list of same-type elements and the index the path points at."""

    etype: Any
    values: list | None         # element container (tag values or an array member)
    start: int
    is_array: bool
    holder: dict | None = None  # scalar UDT member lives in holder[key]
    key: str | None = None

    def elements(self) -> list:
        return [self.holder[self.key]] if self.holder is not None else self.values

    def store(self, value: Any) -> None:
        if self.holder is not None:
            self.holder[self.key] = value
        else:
            self.values[self.start] = value


@dataclass
class _Connection:
    o_t_id: int                 # issued by us; the client addresses SendUnitData with it
    t_o_id: int                 # chosen by the client; we put it in our replies
    triad: tuple[int, int, int] # connection serial, vendor id, originator serial
    size: int
    large: bool
    last_seq: int | None = None
    last_reply: bytes | None = None


@dataclass(eq=False)          # hashable by identity (kept in a set)
class _Client:
    writer: asyncio.StreamWriter
    session: int = 0
    connections: dict[int, _Connection] = field(default_factory=dict)


_TYPE_SPEC = re.compile(r"^(?P<udt>UDT:)?(?P<type>[A-Za-z_]\w*)(?:\[(?P<n>\d+)\])?$")
_USER_NAME = re.compile(
    r"^(?P<base>[A-Za-z_]\w*)(?:\[(?P<i>\d+)\])?(?:\.(?P<member>[A-Za-z_]\w*)(?:\[(?P<j>\d+)\])?)?$"
)


class CipSim:
    """Simulated Logix / Micro800 controller. Build with `await CipSim.start(tags, ...)`.

    Statistics (reset with `reset_counters()`):
      request_count      CIP requests served (an MSP or an Unconnected Send counts once)
      service_counts     object services by code, each service inside an MSP or Unconnected Send
                         counted too (0x4C read, 0x52 read fragmented, 0x0A MSP, 0x55 tag list...)
      cm_counts          Connection Manager services by code (0x54/0x5B open, 0x4E close,
                         0x52 unconnected send); kept apart because 0x4E/0x52 mean other things
      write_attempts     write/set services refused
      connection_sizes   size of every accepted Forward Open, in order
      max_reply_bytes    largest connected reply (sequence count + CIP reply)
      addressing         Counter of "symbolic" / "instance" tag paths resolved
      duplicate_requests repeated sequence counts answered from the cache
      accepted           TCP connections accepted
    """

    def __init__(self, tags: dict[str, tuple[str, Any]], *, kind: Literal["logix", "micro800"],
                 batching: bool, large_forward_open: bool, slot: int, host: str, port: int,
                 revision: tuple[int, int] | None, plc_name: str, serial: int | None,
                 product_code: int | None) -> None:
        if kind not in ("logix", "micro800"):
            raise ValueError(f"kind must be 'logix' or 'micro800', not {kind!r}")
        self.kind = kind
        self.batching = batching
        self.large_forward_open = large_forward_open
        self.slot = slot
        self.host = host
        self.port = port
        self.revision = revision or DEFAULT_REVISION[kind]
        self.plc_name = plc_name
        self.product_name = MICRO800_PRODUCT_NAME if kind == "micro800" else LOGIX_PRODUCT_NAME
        self.product_code = product_code or (166 if kind == "micro800" else 54)   # arbitrary
        self.serial = serial if serial is not None else (0x80C0FFEE if kind == "micro800" else 0x60C0FFEE)
        self.mute = False
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[_Client] = set()
        self._rng = random.Random(port or 1)
        self._next_session = 0x1000 + self._rng.randrange(0x1000)
        self._tags: dict[str, _Tag] = {}          # by upper-case name (Logix names are case-insensitive)
        self._by_instance: dict[int, _Tag] = {}
        self._templates: dict[str, _Template] = {}
        self._templates_by_id: dict[int, _Template] = {}
        self._string_template = _Template("STRING", STRING_TEMPLATE_ID,
                                          [("LEN", ATOMICS["DINT"], 0), ("DATA", ATOMICS["SINT"], 82)],
                                          string_len=82)
        self._system: list[_Symbol] = []
        self._programs: dict[str, list[_Symbol]] = {}
        if kind == "logix":   # what a real controller lists besides user tags; pycomm3 filters them
            self._system = [
                _Symbol("Program:MainProgram", 0x08, 0x1068),
                _Symbol("Task:MainTask", 0x0B, 0x1070),
                _Symbol("Map:Local", 0x0E, 0x1069),
                _Symbol("__SimHidden", 0x11, 0x00C4),
                _Symbol("SimSystemFlag", 0x14, SYSTEM_SYMBOL | 0x00C4),
            ]
            self._programs = {"MAINPROGRAM": [_Symbol("Routine:MainRoutine", 0x10, 0x106D)]}
        for i, (name, (type_spec, value)) in enumerate(tags.items()):
            self._add_tag(name, type_spec, value, USER_INSTANCE_BASE + USER_INSTANCE_STEP * i)
        self.reset_counters()

    # --- public API ------------------------------------------------------------------------------

    @classmethod
    async def start(cls, tags: dict[str, tuple[str, Any]], *, kind: Literal["logix", "micro800"] = "logix",
                    batching: bool = True, large_forward_open: bool = True, slot: int = 0,
                    host: str = SIM_HOST, port: int = 0, revision: tuple[int, int] | None = None,
                    plc_name: str = "SIM_PLC", serial: int | None = None,
                    product_code: int | None = None) -> CipSim:
        """Start listening (port 0 = ephemeral; use `netcut.stable_port()` if you will restart)."""
        sim = cls(tags, kind=kind, batching=batching, large_forward_open=large_forward_open, slot=slot,
                  host=host, port=port, revision=revision, plc_name=plc_name, serial=serial,
                  product_code=product_code)
        await sim._listen()
        return sim

    @property
    def path(self) -> str:
        """pycomm3 connection path: `host:port/slot` for Logix, `host:port` for Micro800."""
        base = f"{self.host}:{self.port}"
        return base if self.kind == "micro800" else f"{base}/{self.slot}"

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def tag_names(self) -> list[str]:
        """User tags as pycomm3 will list them after the upload."""
        return [t.name for t in self._tags.values()]

    @property
    def clients(self) -> int:
        """Open TCP connections."""
        return len(self._clients)

    @property
    def open_connections(self) -> int:
        """Open CIP (Forward Open) connections."""
        return sum(len(c.connections) for c in self._clients)

    def reset_counters(self) -> None:
        self.request_count = 0
        self.service_counts: Counter[int] = Counter()
        self.cm_counts: Counter[int] = Counter()
        self.write_attempts = 0
        self.connection_sizes: list[int] = []
        self.max_reply_bytes = 0
        self.addressing: Counter[str] = Counter()
        self.duplicate_requests = 0
        self.accepted = 0

    def set(self, name: str, value: Any) -> None:
        """Set a value: "A", "ARR" (whole list), "ARR[3]", "UDT1" (dict, merged), "UDT1.MEMBER[2]"."""
        ref = self._user_ref(name)
        if ref.is_array and ref.start is None:
            ref.values[:] = _coerce_array(ref.etype, len(ref.values), value)
        elif ref.etype.is_struct:
            current = ref.elements()[ref.start if ref.holder is None else 0]
            ref.store(ref.etype.coerce(value, base=current))
        else:
            ref.store(ref.etype.coerce(value))

    def value(self, name: str) -> Any:
        """The value the PLC holds (REAL already rounded to float32), same name forms as `set`."""
        ref = self._user_ref(name)
        if ref.is_array and ref.start is None:
            return copy.deepcopy(ref.values)
        return copy.deepcopy(ref.elements()[0 if ref.holder is not None else ref.start])

    def remove(self, name: str) -> None:
        """Delete a tag (like a program download): later reads of it fail on the wire."""
        tag = self._tags.pop(name.upper())
        self._by_instance.pop(tag.instance, None)

    async def stop(self) -> None:
        """Power-off: stop listening and reset every open connection."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for client in list(self._clients):
            client.connections.clear()
            client.writer.transport.abort()
        self._clients.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), 2.0)

    async def restart(self) -> None:
        """Power-on again on the same port, same tag values."""
        await self.stop()
        await self._listen()

    async def __aenter__(self) -> CipSim:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # --- tag store -------------------------------------------------------------------------------

    def _add_tag(self, name: str, type_spec: str, value: Any, instance: int) -> None:
        if not re.fullmatch(r"[A-Za-z_]\w{0,39}", name):
            raise ValueError(f"invalid tag name {name!r}")
        if name.upper() in self._tags:
            raise ValueError(f"duplicate tag {name!r} (Logix names are case-insensitive)")
        m = _TYPE_SPEC.match(type_spec.strip())
        if not m:
            raise ValueError(f"{name}: bad type {type_spec!r}")
        count = int(m["n"]) if m["n"] else 0
        if m["n"] is not None and count < 1:
            raise ValueError(f"{name}: array length must be >= 1")
        if m["udt"]:
            if count:
                raise ValueError(f"{name}: arrays of UDTs are not simulated")
            etype = self._udt(m["type"], value)
            value = {k: v[1] for k, v in (value or {}).items()}
        else:
            etype = self._element_type(m["type"].upper())
            if etype.name == "BOOL" and count and self.kind == "logix":
                raise ValueError(f"{name}: Logix BOOL arrays (DWORD-packed) are not simulated")
            if etype.is_struct and count and self.kind == "micro800":
                raise ValueError(f"{name}: STRING arrays are not simulated on Micro800")
        values = _coerce_array(etype, count, value) if count else [etype.coerce(value)]
        tag = _Tag(name, instance, etype, count, values)
        self._tags[name.upper()] = tag
        self._by_instance[instance] = tag

    def _element_type(self, type_name: str) -> Any:
        if type_name == "STRING":
            return _ShortString() if self.kind == "micro800" else self._string_template
        if type_name not in ATOMICS:
            raise ValueError(f"unsupported type {type_name!r}")
        return ATOMICS[type_name]

    def _udt(self, type_name: str, members: Any) -> _Template:
        if not isinstance(members, dict) or not members:
            raise ValueError(f"UDT {type_name}: value must be {{member: (type, value)}}")
        spec = []
        for mname, (mtype, _v) in members.items():
            mm = _TYPE_SPEC.match(mtype.strip())
            if not mm or mm["udt"] or mm["type"].upper() not in ATOMICS:
                raise ValueError(f"UDT {type_name}.{mname}: only atomic members are simulated, not {mtype!r}")
            if not re.fullmatch(r"[A-Za-z_]\w{0,39}", mname):
                raise ValueError(f"UDT {type_name}: bad member name {mname!r}")
            spec.append((mname, ATOMICS[mm["type"].upper()], int(mm["n"] or 0)))
        known = self._templates.get(type_name.upper())
        if known is not None:
            if [(m.name, m.etype.name, m.count) for m in known.visible()] != [(n, e.name, c) for n, e, c in spec]:
                raise ValueError(f"UDT {type_name} defined twice with different members")
            return known
        template = _Template(type_name, UDT_TEMPLATE_BASE + len(self._templates) * 7, spec)
        self._templates[type_name.upper()] = template
        self._templates_by_id[template.instance] = template
        return template

    def _user_ref(self, name: str) -> _Ref:
        m = _USER_NAME.match(name)
        if not m:
            raise KeyError(name)
        segs: list[tuple[str, Any]] = [("symbol", m["base"])]
        if m["i"] is not None:
            segs.append(("member", int(m["i"])))
        if m["member"]:
            segs.append(("symbol", m["member"]))
            if m["j"] is not None:
                segs.append(("member", int(m["j"])))
        try:
            ref = self._resolve(segs, count_addressing=False)
        except _CipError as err:
            raise KeyError(name) from err
        if ref.is_array and (m["j"] if m["member"] else m["i"]) is None:
            ref.start = None        # whole array
        return ref

    def _resolve(self, segs: list[tuple[str, Any]], *, count_addressing: bool = True) -> _Ref:
        if not segs:
            raise _CipError(GS_PATH_SEGMENT, 0)
        (kind, val), rest = segs[0], segs[1:]
        if kind == "symbol":
            tag = self._tags.get(val.upper())
            if tag is None:
                raise _CipError(GS_PATH_SEGMENT, 0)
            mode = "symbolic"
        elif kind == "class" and val == CLASS_SYMBOL and rest and rest[0][0] == "instance":
            if self.kind == "micro800":
                raise _CipError(GS_PATH_UNKNOWN, 0)
            tag = self._by_instance.get(rest[0][1])
            if tag is None:
                raise _CipError(GS_PATH_UNKNOWN, 0)
            rest, mode = rest[1:], "instance"
        else:
            raise _CipError(GS_PATH_UNKNOWN, 0)
        if count_addressing:
            self.addressing[mode] += 1
        index, indexed = 0, False
        if rest and rest[0][0] == "member":
            if not tag.count:
                raise _CipError(GS_PATH_SEGMENT, 0)
            index, rest, indexed = rest[0][1], rest[1:], True
            if index >= tag.count:
                raise _CipError(GS_PATH_UNKNOWN, 0)
            if rest and rest[0][0] == "member":
                raise _CipError(GS_PATH_SEGMENT, 0)          # multi-dimensional: not simulated
        if not rest:
            return _Ref(tag.etype, tag.values, index, bool(tag.count))
        # structure member
        if not tag.etype.is_struct or tag.etype.string_len or (tag.count and not indexed):
            raise _CipError(GS_PATH_SEGMENT, 0)
        kind, mname = rest[0]
        member = tag.etype.member(mname) if kind == "symbol" else None
        if member is None:
            raise _CipError(GS_PATH_SEGMENT, 0)
        holder, rest = tag.values[index], rest[1:]
        if member.count:
            mi = 0
            if rest and rest[0][0] == "member":
                mi, rest = rest[0][1], rest[1:]
                if mi >= member.count:
                    raise _CipError(GS_PATH_UNKNOWN, 0)
            if rest:
                raise _CipError(GS_PATH_SEGMENT, 0)
            return _Ref(member.etype, holder[member.name], mi, True)
        if rest:
            raise _CipError(GS_PATH_SEGMENT, 0)
        return _Ref(member.etype, None, 0, False, holder=holder, key=member.name)

    # --- network ---------------------------------------------------------------------------------

    async def _listen(self, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                self._server = await asyncio.start_server(self._serve, self.host, self.port, reuse_address=True)
                break
            except OSError:
                if not self.port or loop.time() > deadline:
                    raise
                await asyncio.sleep(0.05)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        self._clients.add(client)
        self.accepted += 1
        try:
            while True:
                header = await reader.readexactly(ENCAP_HEADER.size)
                command, length, session, _status, context, _options = ENCAP_HEADER.unpack(header)
                body = await reader.readexactly(length) if length else b""
                if self.mute:
                    continue
                reply = self._encapsulation(client, command, session, context, body)
                if reply is _CLOSE:
                    break
                if reply:
                    writer.write(reply)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(client)
            client.connections.clear()
            with contextlib.suppress(Exception):
                writer.close()

    def _encapsulation(self, client: _Client, command: int, session: int, context: bytes, body: bytes) -> Any:
        if command == NOP:
            return None
        if command == LIST_IDENTITY:
            return _encap(command, session, context, struct.pack("<H", 1) + self._identity_item())
        if command == LIST_SERVICES:
            item = struct.pack("<HH", 1, 0x0120) + b"Communications\x00\x00"
            return _encap(command, session, context, struct.pack("<HHH", 1, CPF_SERVICES, len(item)) + item)
        if command == LIST_INTERFACES:
            return _encap(command, session, context, struct.pack("<H", 0))
        if command == REGISTER_SESSION:
            if len(body) != 4 or struct.unpack("<HH", body) != (1, 0):
                return _encap(command, 0, context, body, status=ENCAP_UNSUPPORTED_PROTOCOL)
            if client.session:
                return _encap(command, client.session, context, body, status=ENCAP_INVALID_COMMAND)
            client.session, self._next_session = self._next_session, self._next_session + 1
            return _encap(command, client.session, context, body)
        if command == UNREGISTER_SESSION:
            return _CLOSE
        if command not in (SEND_RR_DATA, SEND_UNIT_DATA):
            return _encap(command, session, context, status=ENCAP_INVALID_COMMAND)
        if not client.session or session != client.session:
            return _encap(command, session, context, status=ENCAP_INVALID_SESSION)
        try:
            items = _parse_cpf(body)
        except (ValueError, struct.error):
            return _encap(command, session, context, status=ENCAP_INCORRECT_DATA)
        if command == SEND_RR_DATA:
            if len(items) != 2 or items[0][0] != CPF_NULL or items[1][0] != CPF_UNCONNECTED_DATA:
                return _encap(command, session, context, status=ENCAP_INCORRECT_DATA)
            self.request_count += 1
            reply = self._cip(client, None, items[1][1])
            return _encap(command, session, context, _cpf((CPF_NULL, b""), (CPF_UNCONNECTED_DATA, reply)))
        # SendUnitData
        if (len(items) != 2 or items[0][0] != CPF_CONNECTED_ADDR or len(items[0][1]) != 4
                or items[1][0] != CPF_CONNECTED_DATA or len(items[1][1]) < 2):
            return _encap(command, session, context, status=ENCAP_INCORRECT_DATA)
        conn = client.connections.get(struct.unpack("<I", items[0][1])[0])
        if conn is None:
            return _encap(command, session, context, status=ENCAP_INCORRECT_DATA)
        seq = struct.unpack_from("<H", items[1][1])[0]
        if seq == conn.last_seq and conn.last_reply is not None:
            self.duplicate_requests += 1
            reply = conn.last_reply
        else:
            self.request_count += 1
            reply = self._cip(client, conn, items[1][1][2:])
            conn.last_seq, conn.last_reply = seq, reply
        data = struct.pack("<H", seq) + reply
        self.max_reply_bytes = max(self.max_reply_bytes, len(data))
        return _encap(command, session, context,
                      _cpf((CPF_CONNECTED_ADDR, struct.pack("<I", conn.t_o_id)), (CPF_CONNECTED_DATA, data)))

    def _identity_item(self) -> bytes:
        sockaddr = struct.pack(">hH4s8s", socket.AF_INET, self.port, socket.inet_aton(self.host), bytes(8))
        payload = struct.pack("<H", 1) + sockaddr + self._identity_attributes() + bytes([STATE_OPERATIONAL])
        return struct.pack("<HH", CPF_IDENTITY, len(payload)) + payload

    def _identity_attributes(self) -> bytes:
        name = self.product_name.encode("latin-1")
        major, minor = self.revision
        return struct.pack("<HHHBBHI", VENDOR_ROCKWELL, DEVICE_TYPE_PLC, self.product_code, major, minor,
                           STATUS_REMOTE_RUN, self.serial) + bytes([len(name)]) + name

    # --- CIP -------------------------------------------------------------------------------------

    def _cip(self, client: _Client, conn: _Connection | None, msg: bytes) -> bytes:
        service = msg[0] if msg else 0
        try:
            service, segs, data = _split_request(msg)
            if segs == CM_PATH:
                return self._connection_manager(client, conn, service, data)
            space = conn.size - 2 if conn is not None else UCMM_MAX_REPLY   # 2 = sequence count
            return self._object(service, segs, data, space)
        except _CipError as err:
            return _reply(service, err.status, err.data, err.ext)

    def _connection_manager(self, client: _Client, conn: _Connection | None, service: int, data: bytes) -> bytes:
        self.cm_counts[service] += 1
        if conn is not None:
            raise _CipError(GS_NOT_SUPPORTED)
        if service == CM_UNCONNECTED_SEND:
            if self.kind == "micro800":
                raise _CipError(GS_NOT_SUPPORTED)
            return self._unconnected_send(client, data)
        if service in (CM_FORWARD_OPEN, CM_LARGE_FORWARD_OPEN):
            return self._forward_open(client, service, data)
        if service == CM_FORWARD_CLOSE:
            return self._forward_close(client, data)
        raise _CipError(GS_NOT_SUPPORTED)

    def _route_error(self, segs: list[tuple[str, Any]]) -> int | None:
        """Check a route to the controller. Logix: optional [port 1 (backplane), slot]."""
        ports = [v for k, v in segs if k == "port"]
        if self.kind == "micro800":
            return EXT_INVALID_SEGMENT if ports else None
        if not ports:
            return None                                       # no route: talking to the controller
        if len(ports) != 1 or ports[0][0] != 1:
            return EXT_PORT_NOT_AVAILABLE
        if ports[0][1] != bytes([self.slot]):
            return EXT_LINK_NOT_AVAILABLE
        return None

    def _unconnected_send(self, client: _Client, data: bytes) -> bytes:
        try:
            _priority, _ticks, size = struct.unpack_from("<BBH", data, 0)
            embedded = data[4:4 + size]
            off = 4 + size + (size & 1)
            words = data[off]
            route = _parse_epath(data[off + 2:off + 2 + 2 * words])     # off + 1: reserved
        except (IndexError, struct.error) as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        if len(embedded) != size:
            raise _CipError(GS_NOT_ENOUGH_DATA)
        error = self._route_error(route)
        if error is not None:
            raise _CipError(GS_CONNECTION_FAILURE, error, data=bytes([words, 0]))
        return self._cip(client, None, embedded)      # the target's reply goes back as is

    def _forward_open(self, client: _Client, service: int, data: bytes) -> bytes:
        large = service == CM_LARGE_FORWARD_OPEN
        if large and not self.large_forward_open:
            raise _CipError(GS_NOT_SUPPORTED)
        p = "I" if large else "H"
        fmt = struct.Struct(f"<BBIIHHIB3xI{p}I{p}B")
        try:
            (_prio, _ticks, _ot_cid, to_cid, serial, vendor, orig_serial, _mult,
             ot_rpi, ot_params, to_rpi, to_params, transport) = fmt.unpack_from(data, 0)
            words = data[fmt.size]
            route = _parse_epath(data[fmt.size + 1:fmt.size + 1 + 2 * words])
        except (IndexError, struct.error) as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        triad = (serial, vendor, orig_serial)
        fail_data = struct.pack("<HHIBB", serial, vendor, orig_serial, 0, 0)
        mask, limit = (0xFFFF, LARGE_CONNECTION_MAX) if large else (0x01FF, STANDARD_CONNECTION_MAX)
        size = min(ot_params & mask, to_params & mask)
        if transport != 0xA3:
            raise _CipError(GS_CONNECTION_FAILURE, EXT_TRANSPORT_NOT_SUPPORTED, data=fail_data)
        if not 4 <= size <= limit:
            raise _CipError(GS_CONNECTION_FAILURE, EXT_INVALID_CONNECTION_SIZE, limit, data=fail_data)
        error = self._route_error(route)
        if error is None and [s for s in route if s[0] != "port"] != ROUTER_PATH:
            error = EXT_INVALID_SEGMENT
        if error is not None:
            raise _CipError(GS_CONNECTION_FAILURE, error, data=fail_data)
        if any(c.triad == triad for c in client.connections.values()):
            raise _CipError(GS_CONNECTION_FAILURE, EXT_CONNECTION_IN_USE, data=fail_data)
        o_t_id = self._rng.getrandbits(32) | 1
        client.connections[o_t_id] = _Connection(o_t_id, to_cid, triad, size, large)
        self.connection_sizes.append(size)
        return _reply(service, GS_OK, struct.pack("<IIHHIIIBB", o_t_id, to_cid, serial, vendor, orig_serial,
                                                  ot_rpi, to_rpi, 0, 0))

    def _forward_close(self, client: _Client, data: bytes) -> bytes:
        try:
            _prio, _ticks, serial, vendor, orig_serial = struct.unpack_from("<BBHHI", data, 0)
        except struct.error as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        body = struct.pack("<HHIBB", serial, vendor, orig_serial, 0, 0)
        for cid, conn in list(client.connections.items()):
            if conn.triad == (serial, vendor, orig_serial):
                del client.connections[cid]
                return _reply(CM_FORWARD_CLOSE, GS_OK, body)
        raise _CipError(GS_CONNECTION_FAILURE, EXT_CONNECTION_NOT_FOUND, data=body)

    def _object(self, service: int, segs: list[tuple[str, Any]], data: bytes, space: int) -> bytes:
        self.service_counts[service] += 1
        if service in WRITE_SERVICES:
            self.write_attempts += 1
            raise _CipError(GS_NOT_SUPPORTED)
        if service == SVC_MULTIPLE:
            if segs != ROUTER_PATH:
                raise _CipError(GS_PATH_UNKNOWN, 0)
            if not self.batching:
                raise _CipError(GS_NOT_SUPPORTED)
            return self._multiple(data, space)
        cls = segs[0][1] if segs and segs[0][0] == "class" else None
        inst = segs[1][1] if len(segs) > 1 and segs[1][0] == "instance" else None
        if service == SVC_GET_ATTRIBUTES_ALL:
            if cls == CLASS_IDENTITY and inst == 1 and len(segs) == 2:
                return _reply(service, GS_OK, self._identity_attributes())
            if cls == CLASS_PROGRAM_NAME and inst == 1 and len(segs) == 2 and self.kind == "logix":
                name = self.plc_name.encode("latin-1")
                return _reply(service, GS_OK, struct.pack("<H", len(name)) + name)
            raise _CipError(GS_PATH_UNKNOWN, 0)
        if service == SVC_GET_ATTRIBUTE_SINGLE:
            return self._identity_attribute(segs)
        if service == SVC_GET_ATTRIBUTE_LIST:
            if cls == CLASS_TEMPLATE and inst in self._template_ids() and len(segs) == 2:
                return self._template_attributes(self._template_ids()[inst], data)
            raise _CipError(GS_PATH_UNKNOWN, 0)
        if service == SVC_GET_INSTANCE_ATTRIBUTE_LIST:
            return self._instance_attribute_list(segs, data, space)
        if service == SVC_READ_TAG and cls == CLASS_TEMPLATE:
            if inst not in self._template_ids() or len(segs) != 2:
                raise _CipError(GS_PATH_UNKNOWN, 0)
            return self._read_template(self._template_ids()[inst], data, space)
        if service in (SVC_READ_TAG, SVC_READ_FRAGMENTED):
            return self._read_tag(service, segs, data, space)
        raise _CipError(GS_NOT_SUPPORTED)

    def _multiple(self, data: bytes, space: int) -> bytes:
        try:
            n = struct.unpack_from("<H", data, 0)[0]
            offsets = list(struct.unpack_from(f"<{n}H", data, 2))
        except struct.error as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        bounds = [*offsets, len(data)]
        replies = []
        for k in range(n):
            msg = data[bounds[k]:bounds[k + 1]]
            service = msg[0] if msg else 0
            try:
                service, segs, body = _split_request(msg)
                if segs == CM_PATH or service == SVC_MULTIPLE:
                    raise _CipError(GS_NOT_SUPPORTED)
                replies.append(self._object(service, segs, body, space))
            except _CipError as err:
                replies.append(_reply(service, err.status, err.data, err.ext))
        budget = space - 4 - 2 - 2 * n
        fitted = []
        for r in replies:
            if len(r) > budget:
                r = _reply(r[0] & 0x7F, GS_PARTIAL)       # does not fit in this packet
            budget -= len(r)
            fitted.append(r)
        if budget < 0:
            raise _CipError(GS_REPLY_TOO_LARGE)
        offset, table = 2 + 2 * n, b""
        for r in fitted:
            table += struct.pack("<H", offset)
            offset += len(r)
        status = GS_EMBEDDED_ERROR if any(r[2] != GS_OK for r in fitted) else GS_OK
        return _reply(SVC_MULTIPLE, status, struct.pack("<H", n) + table + b"".join(fitted))

    def _identity_attribute(self, segs: list[tuple[str, Any]]) -> bytes:
        if segs[:2] != [("class", CLASS_IDENTITY), ("instance", 1)] or len(segs) != 3 or segs[2][0] != "attribute":
            raise _CipError(GS_PATH_UNKNOWN, 0)
        name = self.product_name.encode("latin-1")
        values = {
            1: struct.pack("<H", VENDOR_ROCKWELL), 2: struct.pack("<H", DEVICE_TYPE_PLC),
            3: struct.pack("<H", self.product_code), 4: bytes(self.revision),
            5: struct.pack("<H", STATUS_REMOTE_RUN), 6: struct.pack("<I", self.serial),
            7: bytes([len(name)]) + name,
        }
        if segs[2][1] not in values:
            raise _CipError(GS_ATTR_NOT_SUPPORTED)
        return _reply(SVC_GET_ATTRIBUTE_SINGLE, GS_OK, values[segs[2][1]])

    def _template_ids(self) -> dict[int, _Template]:
        return {**self._templates_by_id, STRING_TEMPLATE_ID: self._string_template} if self.kind == "logix" \
            else dict(self._templates_by_id)

    def _template_attributes(self, template: _Template, data: bytes) -> bytes:
        try:
            n = struct.unpack_from("<H", data, 0)[0]
            ids = struct.unpack_from(f"<{n}H", data, 2)
        except struct.error as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        out = struct.pack("<H", n)
        for a in ids:
            if a == 1:
                out += struct.pack("<HHH", a, 0, template.handle)
            elif a == 2:
                out += struct.pack("<HHH", a, 0, len(template.members))
            elif a == 4:
                out += struct.pack("<HHI", a, 0, template.definition_words)
            elif a == 5:
                out += struct.pack("<HHI", a, 0, template.size)
            else:
                out += struct.pack("<HH", a, GS_ATTR_NOT_SUPPORTED)
        return _reply(SVC_GET_ATTRIBUTE_LIST, GS_OK, out)

    def _read_template(self, template: _Template, data: bytes, space: int) -> bytes:
        try:
            offset, count = struct.unpack_from("<iH", data, 0)
        except struct.error as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        if not 0 <= offset <= len(template.definition):
            raise _CipError(GS_GENERAL, EXT_OFFSET_OUT_OF_RANGE)
        room = space - 4
        chunk = template.definition[offset:offset + min(count, room)]
        more = count > room and offset + len(chunk) < len(template.definition)
        return _reply(SVC_READ_TAG, GS_PARTIAL if more else GS_OK, chunk)

    def _instance_attribute_list(self, segs: list[tuple[str, Any]], data: bytes, space: int) -> bytes:
        symbols: list[Any] = [*self._system, *self._tags.values()]
        if segs and segs[0][0] == "symbol":
            name = segs[0][1]
            if not name.startswith("Program:") or name[8:].upper() not in self._programs:
                raise _CipError(GS_PATH_SEGMENT, 0)
            symbols, segs = self._programs[name[8:].upper()], segs[1:]
        if len(segs) != 2 or segs[0] != ("class", CLASS_SYMBOL) or segs[1][0] != "instance":
            raise _CipError(GS_PATH_UNKNOWN, 0)
        try:
            n = struct.unpack_from("<H", data, 0)[0]
            attrs = struct.unpack_from(f"<{n}H", data, 2)
        except struct.error as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        if any(a not in (1, 2, 3, 5, 6, 7, 8, 10) for a in attrs):
            raise _CipError(GS_ATTR_LIST)
        room, out, more = space - 4, b"", False
        for sym in sorted((s for s in symbols if s.instance >= segs[1][1]), key=lambda s: s.instance):
            record = struct.pack("<I", sym.instance) + b"".join(self._symbol_attribute(sym, a) for a in attrs)
            if len(out) + len(record) > room:
                more = True
                break
            out += record
        if more and not out:
            raise _CipError(GS_REPLY_TOO_LARGE)
        return _reply(SVC_GET_INSTANCE_ATTRIBUTE_LIST, GS_PARTIAL if more else GS_OK, out)

    @staticmethod
    def _symbol_attribute(sym: Any, attr: int) -> bytes:
        if attr == 1:
            name = sym.name.encode("latin-1")
            return struct.pack("<H", len(name)) + name
        if attr == 2:
            return struct.pack("<H", sym.symbol_type())
        if attr == 3:
            return struct.pack("<I", 0x1000_0000 + sym.instance * 0x40)       # symbol address
        if attr == 5:
            return struct.pack("<I", 0x2000_0000 + sym.instance * 0x20)       # symbol object address
        if attr == 6:
            return struct.pack("<I", SOFTWARE_CONTROL_BASE_TAG)
        if attr == 7:
            return struct.pack("<H", sym.element_size())
        if attr == 8:
            return struct.pack("<III", *sym.dimensions())
        return b"\x00"                                                         # 10: Read/Write

    def _read_tag(self, service: int, segs: list[tuple[str, Any]], data: bytes, space: int) -> bytes:
        try:
            elements = struct.unpack_from("<H", data, 0)[0]
            offset = struct.unpack_from("<I", data, 2)[0] if service == SVC_READ_FRAGMENTED else 0
        except struct.error as err:
            raise _CipError(GS_NOT_ENOUGH_DATA) from err
        ref = self._resolve(segs)
        values = ref.elements()
        if ref.start + elements > len(values):
            raise _CipError(GS_GENERAL, EXT_BEYOND_END)
        payload = b"".join(ref.etype.encode(v) for v in values[ref.start:ref.start + elements])
        type_bytes = ref.etype.type_bytes()
        room = space - 4 - len(type_bytes)
        size = ref.etype.size
        if size and size <= room:
            room -= room % size                       # fragments end on element boundaries
        if service == SVC_READ_TAG:
            if len(payload) <= space - 4 - len(type_bytes):
                return _reply(service, GS_OK, type_bytes + payload)
            return _reply(service, GS_PARTIAL, type_bytes + payload[:room])
        if offset > len(payload):
            raise _CipError(GS_GENERAL, EXT_OFFSET_OUT_OF_RANGE)
        chunk = payload[offset:offset + room]
        status = GS_PARTIAL if offset + len(chunk) < len(payload) else GS_OK
        return _reply(service, status, type_bytes + chunk)


# --- synthetic tags --------------------------------------------------------------------------------

_SCALAR_KINDS = ("REAL", "REAL", "REAL", "REAL", "DINT", "DINT", "INT", "INT", "BOOL")
_ARRAY_KINDS = (("REAL", 10), ("DINT", 8), ("INT", 16))
SIM_UDT_NAME = "SimUdt"


def make_logix_tags(n: int, *, seed: int = 0, udt: bool = True) -> dict[str, tuple[str, Any]]:
    """`n` deterministic tags: REAL/DINT/INT/BOOL scalars, an array every 20 tags (REAL[10],
    DINT[8], INT[16]) and, if `udt`, one `SimUdt` tag first (UDT_0000: TOTAL REAL, COUNT DINT,
    RUN/FAULT BOOL, MODE INT, HIST REAL[4]). Names: `<TYPE>_<nnnn>`, `ARR_<TYPE>_<nnnn>`."""
    rng = random.Random(seed)
    tags: dict[str, tuple[str, Any]] = {}

    def scalar(kind: str) -> Any:
        if kind == "REAL":
            return round(rng.uniform(-1000.0, 1000.0), 3)
        if kind == "DINT":
            return rng.randint(-2_000_000, 2_000_000)
        if kind == "INT":
            return rng.randint(-30_000, 30_000)
        return rng.random() < 0.5

    if udt and n > 0:
        tags["UDT_0000"] = (f"UDT:{SIM_UDT_NAME}", {
            "TOTAL": ("REAL", scalar("REAL")), "COUNT": ("DINT", scalar("DINT")),
            "RUN": ("BOOL", True), "FAULT": ("BOOL", False), "MODE": ("INT", scalar("INT")),
            "HIST": ("REAL[4]", [scalar("REAL") for _ in range(4)]),
        })
    while len(tags) < n:
        k = len(tags)
        if k % 20 == 10:
            kind, count = _ARRAY_KINDS[(k // 20) % len(_ARRAY_KINDS)]
            tags[f"ARR_{kind}_{k:04d}"] = (f"{kind}[{count}]", [scalar(kind) for _ in range(count)])
        else:
            kind = _SCALAR_KINDS[rng.randrange(len(_SCALAR_KINDS))]
            tags[f"{kind}_{k:04d}"] = (kind, scalar(kind))
    return tags
