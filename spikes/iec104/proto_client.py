"""Throwaway IEC 60870-5-104 client prototype for the IEC 104 spike (#16). NOT production code; never
imported by `src/`. It shows the client subset a native driver needs is small: APCI (I/S/U frames,
send/receive sequence numbers, k/w windows, t1/t2/t3), STARTDT/STOPDT/TESTFR, general and counter
interrogation, the monitor-direction ASDUs of docs/spikes/iec104.md, CP56Time2a and the
IV/NT/SB/BL/OV -> opencap quality mapping. Written from the IEC 60870-5-101/104 frame layouts as
summarised there; no other implementation's source (in particular not lib60870's) was used.

Read-only by construction: `COMMANDS_ALLOWED` is the only set of control-direction type ids this
module can frame: C_IC_NA_1 (general interrogation) and C_CI_NA_1 (counter interrogation, with
FRZ = 0 "read" only; freeze and reset change the counter). There is no C_SC/C_DC/C_RC/C_SE/C_BO,
no clock sync (C_CS writes the RTU clock), no reset process (C_RP) and no parameter load (P_*).
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# --- APCI ------------------------------------------------------------------------------------------

START = 0x68
U_STARTDT_ACT, U_STARTDT_CON = 0x07, 0x0B
U_STOPDT_ACT, U_STOPDT_CON = 0x13, 0x23
U_TESTFR_ACT, U_TESTFR_CON = 0x43, 0x83


def u_frame(function: int) -> bytes:
    return bytes((START, 4, function, 0, 0, 0))


def s_frame(recv_seq: int) -> bytes:
    return bytes((START, 4, 0x01, 0x00)) + ((recv_seq << 1) & 0xFFFF).to_bytes(2, "little")


def i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    if len(asdu) > 249:
        raise ValueError("ASDU longer than 249 bytes")
    return bytes((START, 4 + len(asdu))) + ((send_seq << 1) & 0xFFFF).to_bytes(2, "little") \
        + ((recv_seq << 1) & 0xFFFF).to_bytes(2, "little") + asdu


@dataclass(frozen=True)
class Apdu:
    kind: str                    # "I" | "S" | "U"
    send_seq: int = 0
    recv_seq: int = 0
    function: int = 0
    asdu: bytes = b""


async def read_apdu(reader: asyncio.StreamReader) -> Apdu:
    head = await reader.readexactly(2)
    if head[0] != START:
        raise ValueError(f"bad start byte {head[0]:#04x}")
    body = await reader.readexactly(head[1])
    c1 = body[0]
    if c1 & 0x01 == 0:
        return Apdu("I", int.from_bytes(body[0:2], "little") >> 1, int.from_bytes(body[2:4], "little") >> 1,
                    asdu=body[4:])
    if c1 & 0x03 == 0x01:
        return Apdu("S", recv_seq=int.from_bytes(body[2:4], "little") >> 1)
    return Apdu("U", function=c1)


# --- ASDU ------------------------------------------------------------------------------------------

# monitor direction
M_SP_NA_1, M_DP_NA_1, M_ME_NA_1, M_ME_NB_1, M_ME_NC_1, M_IT_NA_1 = 1, 3, 9, 11, 13, 15
M_SP_TB_1, M_DP_TB_1, M_ME_TD_1, M_ME_TE_1, M_ME_TF_1, M_IT_TB_1 = 30, 31, 34, 35, 36, 37
M_EI_NA_1 = 70
# control direction
C_IC_NA_1, C_CI_NA_1 = 100, 101
COMMANDS_ALLOWED = frozenset({C_IC_NA_1, C_CI_NA_1})

COT_PERIODIC, COT_SPONTANEOUS, COT_ACTIVATION, COT_ACT_CON, COT_ACT_TERM = 1, 3, 6, 7, 10
COT_INROGEN, COT_REQCOGEN = 20, 37
COT_NEGATIVE = 0x40


def cp56time2a(raw: bytes) -> tuple[datetime | None, bool]:
    """7-byte CP56Time2a -> (UTC datetime or None if marked invalid, summer-time flag). The
    prototype treats the time as UTC (a site setting in the driver, docs/spikes/iec104.md §5.2)."""
    ms = int.from_bytes(raw[0:2], "little")
    minute, iv = raw[2] & 0x3F, bool(raw[2] & 0x80)
    hour, su = raw[3] & 0x1F, bool(raw[3] & 0x80)
    day, month, year = raw[4] & 0x1F, raw[5] & 0x0F, 2000 + (raw[6] & 0x7F)
    if iv:
        return None, su
    return datetime(year, month, day, hour, minute, tzinfo=UTC) + timedelta(milliseconds=ms), su


def encode_cp56time2a(t: datetime) -> bytes:
    ms = t.second * 1000 + t.microsecond // 1000
    return ms.to_bytes(2, "little") + bytes((t.minute, t.hour, t.day | (t.isoweekday() << 5), t.month, t.year - 2000))


# quality descriptor bits: OV (QDS only), BL, SB, NT, IV
OV, BL, SB, NT, IV = 0x01, 0x10, 0x20, 0x40, 0x80


def quality_of(q: int, *, counter: bool = False) -> str:
    """IEC 104 quality -> opencap quality (docs/spikes/iec104.md §5.1). For counters (BCR) the
    sequence byte carries IV (0x80) only; CA/CY are not quality."""
    if q & IV:
        return "bad_decode"           # invalid: the value is not usable
    if not counter and q & NT:
        return "stale"                # not topical: not updated within the station's time
    if not counter and q & OV:
        return "bad_decode"           # overflow: the number is outside its range
    return "good"                     # BL (blocked) and SB (substituted) values are reported as is


@dataclass(frozen=True)
class Point:
    type_id: int
    ca: int
    ioa: int
    value: object
    quality: int
    cot: int
    time: datetime | None = None

    @property
    def opencap_quality(self) -> str:
        return quality_of(self.quality, counter=self.type_id in (M_IT_NA_1, M_IT_TB_1))


def _element(type_id: int, b: bytes) -> tuple[object, int, datetime | None, int]:
    """-> (value, quality bits, time, size)"""
    match type_id:
        case 1 | 30:
            t = cp56time2a(b[1:8])[0] if type_id == 30 else None
            return bool(b[0] & 0x01), b[0] & 0xF0, t, 1 + (7 if type_id == 30 else 0)
        case 3 | 31:
            t = cp56time2a(b[1:8])[0] if type_id == 31 else None
            return b[0] & 0x03, b[0] & 0xF0, t, 1 + (7 if type_id == 31 else 0)
        case 9 | 34:
            v = struct.unpack("<h", b[0:2])[0] / 32768
            return v, b[2], cp56time2a(b[3:10])[0] if type_id == 34 else None, 3 + (7 if type_id == 34 else 0)
        case 11 | 35:
            v = struct.unpack("<h", b[0:2])[0]
            return v, b[2], cp56time2a(b[3:10])[0] if type_id == 35 else None, 3 + (7 if type_id == 35 else 0)
        case 13 | 36:
            v = struct.unpack("<f", b[0:4])[0]
            return v, b[4], cp56time2a(b[5:12])[0] if type_id == 36 else None, 5 + (7 if type_id == 36 else 0)
        case 15 | 37:
            v = struct.unpack("<i", b[0:4])[0]
            return v, b[4] & 0x80, cp56time2a(b[5:12])[0] if type_id == 37 else None, 5 + (7 if type_id == 37 else 0)
        case 70:
            return b[0], 0, None, 1
    raise ValueError(f"type id {type_id} not in the prototype")


@dataclass
class Asdu:
    type_id: int
    cot: int
    negative: bool
    test: bool
    originator: int
    ca: int
    points: list[Point] = field(default_factory=list)
    raw_objects: bytes = b""


def parse_asdu(data: bytes) -> Asdu:
    type_id, vsq = data[0], data[1]
    sq, n = bool(vsq & 0x80), vsq & 0x7F
    cot_byte = data[2]
    asdu = Asdu(type_id, cot_byte & 0x3F, bool(cot_byte & COT_NEGATIVE), bool(cot_byte & 0x80), data[3],
                int.from_bytes(data[4:6], "little"), raw_objects=data[6:])
    if type_id >= 100:                                    # a confirmation of one of our commands
        return asdu
    pos = 6
    ioa = 0
    for i in range(n):
        if not sq or i == 0:
            ioa = int.from_bytes(data[pos:pos + 3], "little")
            pos += 3
        else:
            ioa += 1
        value, q, t, size = _element(type_id, data[pos:])
        pos += size
        asdu.points.append(Point(type_id, asdu.ca, ioa, value, q, asdu.cot, t))
    return asdu


def command_asdu(type_id: int, ca: int, qualifier: int, *, cot: int = COT_ACTIVATION, originator: int = 0) -> bytes:
    if type_id not in COMMANDS_ALLOWED:
        raise ValueError(f"type id {type_id} is not part of the read-only subset")
    if type_id == C_CI_NA_1 and qualifier & 0xC0:
        raise ValueError("counter interrogation with freeze/reset changes the counter: not read-only")
    return bytes((type_id, 0x01, cot, originator)) + ca.to_bytes(2, "little") + b"\x00\x00\x00" + bytes((qualifier,))


QOI_STATION = 20          # general interrogation of the whole station
QCC_GENERAL_READ = 5      # RQT = 5 (general counter request), FRZ = 0 (read)


# --- the client --------------------------------------------------------------------------------------

class Client:
    """Minimal IEC 104 controlling station. Owns the k/w windows: never more than `k` unacknowledged
    I-frames in flight, acknowledges received I-frames after `w` frames or `t2` seconds, answers
    TESTFR act; t1 bounds every wait for a confirmation. Received monitor points go to `points`
    (latest per (ca, ioa)) and `events` (every spontaneous one)."""

    def __init__(self, host: str, port: int, *, k: int = 12, w: int = 8, t1: float = 15.0, t2: float = 10.0,
                 originator: int = 0) -> None:
        self.host, self.port, self.k, self.w, self.t1, self.t2 = host, port, k, w, t1, t2
        self.originator = originator
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.vs = 0                   # send sequence
        self.vr = 0                   # receive sequence
        self.ack = 0                  # highest of our I-frames the server acknowledged
        self.unacked_rx = 0
        self.points: dict[tuple[int, int], Point] = {}
        self.events: list[Point] = []
        self.confirmations: asyncio.Queue[Asdu] = asyncio.Queue()
        self.u_frames: asyncio.Queue[int] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.s_frames_sent = 0
        self.testfr_answered = 0
        self._rx_task: asyncio.Task | None = None
        self._t2: asyncio.TimerHandle | None = None

    async def __aenter__(self) -> Client:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self._rx_task = asyncio.create_task(self._receive())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._rx_task:
            self._rx_task.cancel()
        if self.writer:
            self.writer.close()

    def _write(self, frame: bytes) -> None:
        assert self.writer is not None
        self.sent.append(frame)
        self.writer.write(frame)

    def _send_s(self) -> None:
        self._write(s_frame(self.vr))
        self.s_frames_sent += 1
        self.unacked_rx = 0
        if self._t2:
            self._t2.cancel()
            self._t2 = None

    async def _receive(self) -> None:
        assert self.reader is not None
        loop = asyncio.get_running_loop()
        while True:
            apdu = await read_apdu(self.reader)
            if apdu.kind == "U":
                if apdu.function == U_TESTFR_ACT:
                    self._write(u_frame(U_TESTFR_CON))
                    self.testfr_answered += 1
                else:
                    self.u_frames.put_nowait(apdu.function)
                continue
            self.ack = apdu.recv_seq
            if apdu.kind == "S":
                continue
            if apdu.send_seq != self.vr:
                raise ValueError(f"sequence error: got {apdu.send_seq}, expected {self.vr}")
            self.vr = (self.vr + 1) & 0x7FFF
            self.unacked_rx += 1
            asdu = parse_asdu(apdu.asdu)
            if asdu.type_id >= 100:
                self.confirmations.put_nowait(asdu)
            for p in asdu.points:
                self.points[(p.ca, p.ioa)] = p
                if p.cot == COT_SPONTANEOUS:
                    self.events.append(p)
            if asdu.type_id == M_EI_NA_1:
                self.confirmations.put_nowait(asdu)
            if self.unacked_rx >= self.w:
                self._send_s()
            elif self._t2 is None:
                self._t2 = loop.call_later(self.t2, self._send_s)

    async def _expect_u(self, function: int) -> None:
        got = await asyncio.wait_for(self.u_frames.get(), self.t1)
        if got != function:
            raise ValueError(f"expected U {function:#04x}, got {got:#04x}")

    async def startdt(self) -> None:
        self._write(u_frame(U_STARTDT_ACT))
        await self._expect_u(U_STARTDT_CON)

    async def stopdt(self) -> None:
        if self.unacked_rx:
            self._send_s()
        self._write(u_frame(U_STOPDT_ACT))
        await self._expect_u(U_STOPDT_CON)

    async def testfr(self) -> None:
        self._write(u_frame(U_TESTFR_ACT))
        await self._expect_u(U_TESTFR_CON)

    async def _command(self, type_id: int, ca: int, qualifier: int) -> list[Asdu]:
        if (self.vs - self.ack) & 0x7FFF >= self.k:
            raise RuntimeError("k unacknowledged I-frames in flight")
        self._write(i_frame(self.vs, self.vr, command_asdu(type_id, ca, qualifier, originator=self.originator)))
        self.vs = (self.vs + 1) & 0x7FFF
        if self._t2:
            self._t2.cancel()
            self._t2 = None
        self.unacked_rx = 0                          # the I-frame carried our receive sequence
        replies = []
        while True:
            asdu = await asyncio.wait_for(self.confirmations.get(), self.t1)
            if asdu.type_id != type_id:
                continue
            replies.append(asdu)
            if asdu.negative or asdu.cot == COT_ACT_TERM:
                return replies

    async def general_interrogation(self, ca: int) -> list[Asdu]:
        return await self._command(C_IC_NA_1, ca, QOI_STATION)

    async def counter_interrogation(self, ca: int) -> list[Asdu]:
        return await self._command(C_CI_NA_1, ca, QCC_GENERAL_READ)
