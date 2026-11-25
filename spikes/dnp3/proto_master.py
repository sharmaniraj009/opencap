"""Throwaway DNP3 master prototype for the DNP3 spike (#14). NOT production code; never imported by
`src/`. It shows that a native implementation of the read subset is small: link layer (CRC-16/DNP,
16-byte blocks), transport segments, application READ / CONFIRM, the static and event objects of
docs/spikes/dnp3.md, and the flag -> opencap quality mapping. Written from IEEE 1815 frame layouts
as summarised in docs/spikes/dnp3.md; no other implementation's source was used.

Read-only by construction: `FC_ALLOWED` is the only set of application function codes this module
can frame (READ, CONFIRM); there is no WRITE, SELECT, OPERATE, DIRECT_OPERATE, FREEZE, RESTART or
ASSIGN_CLASS code anywhere in it.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime

# --- link layer ------------------------------------------------------------------------------------

START = b"\x05\x64"
LINK_UNCONFIRMED_USER_DATA = 0x04
DIR_FROM_MASTER = 0x80
PRM = 0x40


def crc_dnp(data: bytes) -> int:
    """CRC-16/DNP: polynomial 0x3D65 (reflected 0xA6BC), init 0, output inverted."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
    return ~crc & 0xFFFF


def _with_crc(chunk: bytes) -> bytes:
    return chunk + crc_dnp(chunk).to_bytes(2, "little")


def link_frame(user_data: bytes, *, dest: int, src: int, control: int) -> bytes:
    """One link frame: 10-byte header (start, length, control, dest, src, CRC), then the user data
    in 16-byte blocks, each followed by its CRC. Length counts control+addresses+user data."""
    if len(user_data) > 250:
        raise ValueError("link frame carries at most 250 user data bytes")
    header = START + bytes((5 + len(user_data), control)) + dest.to_bytes(2, "little") + src.to_bytes(2, "little")
    out = _with_crc(header)
    for i in range(0, len(user_data), 16):
        out += _with_crc(user_data[i:i + 16])
    return out


@dataclass
class LinkFrame:
    control: int
    dest: int
    src: int
    user_data: bytes


class LinkError(Exception):
    pass


async def read_link_frame(reader: asyncio.StreamReader) -> LinkFrame:
    header = await reader.readexactly(10)
    if header[:2] != START:
        raise LinkError(f"bad start {header[:2].hex()}")
    if crc_dnp(header[:8]) != int.from_bytes(header[8:10], "little"):
        raise LinkError("header CRC")
    length, control = header[2], header[3]
    n = length - 5
    data = b""
    while len(data) < n:
        size = min(16, n - len(data))
        block = await reader.readexactly(size + 2)
        if crc_dnp(block[:size]) != int.from_bytes(block[size:], "little"):
            raise LinkError("data block CRC")
        data += block[:size]
    return LinkFrame(control, int.from_bytes(header[4:6], "little"), int.from_bytes(header[6:8], "little"), data)


# --- transport ----------------------------------------------------------------------------------------

T_FIN, T_FIR = 0x80, 0x40


def transport_segments(apdu: bytes, seq: int) -> list[bytes]:
    """Split an APDU into transport segments of at most 249 bytes (+1 header byte each)."""
    chunks = [apdu[i:i + 249] for i in range(0, len(apdu), 249)] or [b""]
    out = []
    for i, chunk in enumerate(chunks):
        hdr = ((seq + i) & 0x3F) | (T_FIR if i == 0 else 0) | (T_FIN if i == len(chunks) - 1 else 0)
        out.append(bytes((hdr,)) + chunk)
    return out


# --- application layer -------------------------------------------------------------------------------

AC_FIR, AC_FIN, AC_CON, AC_UNS = 0x80, 0x40, 0x20, 0x10
FC_CONFIRM, FC_READ = 0x00, 0x01
FC_RESPONSE, FC_UNSOLICITED = 0x81, 0x82
FC_ALLOWED = frozenset({FC_CONFIRM, FC_READ})     # the only codes this module can frame

QUAL_ALL = 0x06


def apdu_request(fc: int, seq: int, objects: bytes = b"", *, uns: bool = False) -> bytes:
    if fc not in FC_ALLOWED:
        raise ValueError(f"function code {fc:#04x} is not part of the read-only subset")
    return bytes((AC_FIR | AC_FIN | (AC_UNS if uns else 0) | (seq & 0x0F), fc)) + objects


def class_read(*classes: int) -> bytes:
    """Object headers for a class poll: class 1/2/3 events are g60v2/v3/v4, class 0 static is g60v1."""
    var = {0: 1, 1: 2, 2: 3, 3: 4}
    return b"".join(bytes((60, var[c], QUAL_ALL)) for c in classes)


INTEGRITY_POLL = class_read(1, 2, 3, 0)   # events first, then static, as IEEE 1815 recommends
EVENT_POLL = class_read(1, 2, 3)


# --- objects -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Point:
    kind: str                    # "binary", "double_binary", "analog", "counter", "frozen_counter", ...
    index: int
    value: object
    flags: int
    event: bool = False
    time: datetime | None = None
    group: int = 0
    variation: int = 0

    @property
    def quality(self) -> str:
        return quality_of(self.flags)


# ONLINE (bit 0), RESTART (1), COMM_LOST (2), REMOTE_FORCED (3), LOCAL_FORCED (4) are common to all
# point types; bit 5 is CHATTER_FILTER (binary) / OVER_RANGE (analog) / ROLLOVER (counter); bit 6
# REFERENCE_ERR (analog) / DISCONTINUITY (counter).
ONLINE, RESTART, COMM_LOST, REMOTE_FORCED, LOCAL_FORCED = 0x01, 0x02, 0x04, 0x08, 0x10


def quality_of(flags: int, kind: str = "analog") -> str:
    """DNP3 flags -> opencap quality (docs/spikes/dnp3.md section 5; ADR-0006 has five qualities)."""
    if flags & COMM_LOST:
        return "bad_comm"            # the outstation lost its own source of this point
    if not flags & ONLINE or flags & RESTART:
        return "stale"               # offline, or not updated since the outstation restarted
    if kind == "analog" and flags & 0x60:
        return "bad_decode"          # OVER_RANGE / REFERENCE_ERR: the number is not trustworthy
    return "good"                    # forced and chatter-filtered values are still the current value


def _time48(raw: bytes) -> datetime:
    return datetime.fromtimestamp(int.from_bytes(raw, "little") / 1000, UTC)


# (group, variation) -> (kind, size in bytes, decoder(bytes) -> (value, flags, time))
def _flags_only_binary(b: bytes):
    return bool(b[0] & 0x80), b[0] & 0x7F, None


def _flags_time_binary(b: bytes):
    return bool(b[0] & 0x80), b[0] & 0x7F, _time48(b[1:7])


def _double(b: bytes):
    return (b[0] >> 6) & 0x03, b[0] & 0x3F, None


def _double_time(b: bytes):
    return (b[0] >> 6) & 0x03, b[0] & 0x3F, _time48(b[1:7])


def _num(fmt: str, timed: bool = False, flag: bool = True):
    size = struct.calcsize("<" + fmt)

    def dec(b: bytes):
        off = 1 if flag else 0
        value = struct.unpack("<" + fmt, b[off:off + size])[0]
        return value, (b[0] if flag else ONLINE), (_time48(b[off + size:off + size + 6]) if timed else None)
    return (1 if flag else 0) + size + (6 if timed else 0), dec


OBJECTS: dict[tuple[int, int], tuple[str, int, object]] = {
    (1, 2): ("binary", 1, _flags_only_binary),
    (2, 1): ("binary", 1, _flags_only_binary),
    (2, 2): ("binary", 7, _flags_time_binary),
    (3, 2): ("double_binary", 1, _double),
    (4, 1): ("double_binary", 1, _double),
    (4, 2): ("double_binary", 7, _double_time),
    (10, 2): ("binary_output_status", 1, _flags_only_binary),
    (11, 1): ("binary_output_status", 1, _flags_only_binary),
    (11, 2): ("binary_output_status", 7, _flags_time_binary),
}
for _g, _kind in ((20, "counter"), (21, "frozen_counter"), (22, "counter"), (23, "frozen_counter")):
    OBJECTS[(_g, 1)] = (_kind, *_num("I"))
    OBJECTS[(_g, 2)] = (_kind, *_num("H"))
    if _g in (21, 22, 23):
        OBJECTS[(_g, 5)] = (_kind, *_num("I", timed=True))
        OBJECTS[(_g, 6)] = (_kind, *_num("H", timed=True))
OBJECTS[(20, 5)] = ("counter", *_num("I", flag=False))
OBJECTS[(20, 6)] = ("counter", *_num("H", flag=False))
OBJECTS[(21, 9)] = ("frozen_counter", *_num("I", flag=False))
OBJECTS[(21, 10)] = ("frozen_counter", *_num("H", flag=False))
for _g, _kind in ((30, "analog"), (40, "analog_output_status")):
    OBJECTS[(_g, 1)] = (_kind, *_num("i"))
    OBJECTS[(_g, 2)] = (_kind, *_num("h"))
OBJECTS[(30, 3)] = ("analog", *_num("i", flag=False))
OBJECTS[(30, 4)] = ("analog", *_num("h", flag=False))
OBJECTS[(30, 5)] = ("analog", *_num("f"))
OBJECTS[(30, 6)] = ("analog", *_num("d"))
OBJECTS[(40, 3)] = ("analog_output_status", *_num("f"))
OBJECTS[(40, 4)] = ("analog_output_status", *_num("d"))
for _g, _kind in ((32, "analog"), (42, "analog_output_status")):
    OBJECTS[(_g, 1)] = (_kind, *_num("i"))
    OBJECTS[(_g, 2)] = (_kind, *_num("h"))
    OBJECTS[(_g, 3)] = (_kind, *_num("i", timed=True))
    OBJECTS[(_g, 4)] = (_kind, *_num("h", timed=True))
    OBJECTS[(_g, 5)] = (_kind, *_num("f"))
    OBJECTS[(_g, 6)] = (_kind, *_num("d"))
    OBJECTS[(_g, 7)] = (_kind, *_num("f", timed=True))
    OBJECTS[(_g, 8)] = (_kind, *_num("d", timed=True))
OBJECTS[(50, 4)] = ("time_and_interval", 11, lambda b: ((_time48(b[0:6]), int.from_bytes(b[6:10], "little"), b[10]),
                                                         ONLINE, None))
EVENT_GROUPS = {2, 4, 11, 22, 23, 32, 42}


class ParseError(Exception):
    pass


@dataclass
class Response:
    fc: int
    seq: int
    con: bool
    fin: bool
    iin: int
    points: list[Point] = field(default_factory=list)
    unknown: list[tuple[int, int]] = field(default_factory=list)


def parse_response(apdu: bytes) -> Response:
    if len(apdu) < 4:
        raise ParseError("short APDU")
    ac, fc = apdu[0], apdu[1]
    resp = Response(fc, ac & 0x0F, bool(ac & AC_CON), bool(ac & AC_FIN), int.from_bytes(apdu[2:4], "little"))
    pos = 4
    while pos < len(apdu):
        group, var, qual = apdu[pos], apdu[pos + 1], apdu[pos + 2]
        pos += 3
        prefix, code = (qual >> 4) & 0x07, qual & 0x0F
        if code in (0x00, 0x01, 0x02):                     # start-stop range
            w = {0x00: 1, 0x01: 2, 0x02: 4}[code]
            start = int.from_bytes(apdu[pos:pos + w], "little")
            stop = int.from_bytes(apdu[pos + w:pos + 2 * w], "little")
            pos += 2 * w
            indices = list(range(start, stop + 1))
        elif code in (0x07, 0x08, 0x09):                   # count
            w = {0x07: 1, 0x08: 2, 0x09: 4}[code]
            count = int.from_bytes(apdu[pos:pos + w], "little")
            pos += w
            indices = [None] * count
        else:
            raise ParseError(f"qualifier {qual:#04x} not in the prototype")
        pw = {0: 0, 1: 1, 2: 2, 3: 4}.get(prefix)
        if pw is None:
            raise ParseError(f"prefix {prefix} not in the prototype")
        spec = OBJECTS.get((group, var))
        if spec is None:
            if group in (110, 111):                        # octet strings: variation = length
                spec = ("octet_string", var, lambda b: (bytes(b), ONLINE, None))
            elif group == 1 and var == 1 or group == 10 and var == 1:   # packed bits
                kind = "binary" if group == 1 else "binary_output_status"
                nbytes = (len(indices) + 7) // 8
                bits = apdu[pos:pos + nbytes]
                for k, idx in enumerate(indices):
                    resp.points.append(Point(kind, idx, bool(bits[k // 8] >> (k % 8) & 1), ONLINE,
                                             group=group, variation=var))
                pos += nbytes
                continue
            else:
                raise ParseError(f"object g{group}v{var} not in the prototype")
        kind, size, dec = spec
        for idx in indices:
            if pw:
                idx = int.from_bytes(apdu[pos:pos + pw], "little")
                pos += pw
            raw = apdu[pos:pos + size]
            if len(raw) < size:
                raise ParseError(f"g{group}v{var} truncated")
            pos += size
            value, flags, ts = dec(raw)
            resp.points.append(Point(kind, idx, value, flags, group in EVENT_GROUPS, ts, group, var))
    return resp


# --- the master -----------------------------------------------------------------------------------

class Master:
    """Minimal TCP master: one outstation, request/response, application CONFIRM for event responses
    and unsolicited fragments that ask for one. No unsolicited enable, no time sync, no writes."""

    def __init__(self, host: str, port: int, *, local: int = 1, remote: int = 1024, timeout: float = 5.0) -> None:
        self.host, self.port, self.local, self.remote, self.timeout = host, port, local, remote, timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.app_seq = 0
        self.tx_seq = 0
        self.unsolicited: list[Response] = []
        self.frames_sent: list[bytes] = []

    async def __aenter__(self) -> Master:
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()

    async def _send_apdu(self, apdu: bytes) -> None:
        assert self.writer is not None
        for seg in transport_segments(apdu, self.tx_seq):
            frame = link_frame(seg, dest=self.remote, src=self.local,
                               control=DIR_FROM_MASTER | PRM | LINK_UNCONFIRMED_USER_DATA)
            self.frames_sent.append(frame)
            self.writer.write(frame)
            self.tx_seq = (self.tx_seq + 1) & 0x3F
        await self.writer.drain()

    async def _read_apdu(self) -> bytes:
        assert self.reader is not None
        apdu = b""
        while True:
            frame = await read_link_frame(self.reader)
            if frame.control & 0x0F != LINK_UNCONFIRMED_USER_DATA or not frame.user_data:
                continue                                   # link-layer only (e.g. link status)
            th, payload = frame.user_data[0], frame.user_data[1:]
            if th & T_FIR:
                apdu = b""
            apdu += payload
            if th & T_FIN:
                return apdu

    async def _confirm(self, seq: int, *, uns: bool) -> None:
        await self._send_apdu(apdu_request(FC_CONFIRM, seq, uns=uns))

    async def read(self, objects: bytes) -> Response:
        """One READ; returns the (multi-fragment-merged) solicited response. Unsolicited fragments
        that arrive meanwhile are confirmed and kept in `self.unsolicited`."""
        seq = self.app_seq
        self.app_seq = (self.app_seq + 1) & 0x0F
        await self._send_apdu(apdu_request(FC_READ, seq, objects))
        merged: Response | None = None
        while True:
            resp = parse_response(await asyncio.wait_for(self._read_apdu(), self.timeout))
            if resp.fc == FC_UNSOLICITED:
                if resp.con:
                    await self._confirm(resp.seq, uns=True)
                self.unsolicited.append(resp)
                continue
            if resp.fc != FC_RESPONSE:
                raise ParseError(f"unexpected function code {resp.fc:#04x}")
            if resp.con:
                await self._confirm(resp.seq, uns=False)
            if merged is None:
                merged = resp
            else:                                          # a multi-fragment response
                merged.points.extend(resp.points)
                merged.iin = resp.iin
            if resp.fin:
                return merged

    async def integrity_poll(self) -> Response:
        return await self.read(INTEGRITY_POLL)

    async def event_poll(self) -> Response:
        return await self.read(EVENT_POLL)
