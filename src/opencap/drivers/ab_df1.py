"""Allen-Bradley DF1 full-duplex driver: SLC 500 / MicroLogix data tables over a serial line
(local port, RFC 2217, or a serial-over-TCP gateway; see `drivers/_serial.py`).

DF1 carries the same PCCC commands as EtherNet/IP, so the address parser, the block planner
and the value decoder are `ab_pccc`'s (`parse_pccc_address`, `plan_pccc`, `decode_block`): a
DF1 device plans to exactly the blocks an `ab_pccc` device with the same tags would, and each
block is one PCCC protected typed logical read. There is no mature Python DF1 library, so the
link layer is implemented here (DF1 Protocol and Command Set Reference Manual, Rockwell
publication 1770-6.5.16, chapter "Data-link layer: full-duplex").

Link layer (full duplex, point to point; pure parts at the top of this module)
- Frame: DLE STX, application data with every DLE doubled (DLE stuffing), DLE ETX, then the
  check: BCC (1 byte: two's complement of the 8-bit sum of the application bytes) or CRC-16
  (2 bytes, low byte first: CRC-16/ARC, polynomial x^16 + x^15 + x^2 + 1, initial value 0,
  over the application bytes and the ETX byte). Stuffed DLEs count once; the check bytes are
  not stuffed (the receiver knows how many follow DLE ETX).
- Link responses: DLE ACK (frame received), DLE NAK (bad check, malformed or too long frame),
  DLE ENQ (the sender lost the response: repeat your last one). A response symbol may arrive
  in the middle of a frame travelling the other way; the receiver takes it out of the frame.
- Sender: after a frame, wait `ack_timeout_ms` for ACK/NAK. NAK -> send the frame again, at
  most `nak_retries` times. No response -> DLE ENQ, at most `enq_retries` times. The receiver
  repeats its last response to an ENQ (ACK if it got the frame).
- Receiver: ACK every good frame, NAK every bad one. Duplicate detection: a frame whose SRC,
  CMD, STS and TNS bytes equal the previous good frame's is ACKed again and dropped (the
  other side retransmitted because our ACK was lost).

Application layer (PCCC; DST SRC CMD STS TNS(2, little-endian) FNC data)
- Every block read: CMD 0x0F FNC 0xA2, protected typed logical read with three address
  fields (byte count, file number, file type, element, sub-element), as pycomm3 sends it over
  EtherNet/IP; `params.read_fnc: a1` selects the two-field variant (FNC 0xA1) for PLCs that
  only know that one (I/O blocks that start past word 0 need the sub-element and keep 0xA2).
- `identity()`: CMD 0x06 FNC 0x03 diagnostic status (processor catalog number).
- The reply is matched by TNS and CMD | 0x40; anything else (a late reply to a request that
  timed out, a duplicate) is ACKed and dropped, so it can never be read as the answer to a
  newer request. The source station is not checked: on a point-to-point full-duplex link the
  PLC answers from its own configured station number whatever DST said; a different SRC is
  counted (`source_mismatch`) and logged once.
- Reply data are the raw element bytes; `pccc_values()` turns them into exactly what pycomm3's
  SLC driver returns for the same read, so `ab_pccc.decode_block` decodes both identically.

Not supported (documented in docs/driver_params.md): half-duplex master/slave (DLE SOH
polling), DH+ / DH-485 routing headers beyond plain DST/SRC, PLC-5 typed reads (FNC 0x68 with
PLC-5 system addresses), the processor file-directory check `ab_pccc` does over EtherNet/IP
(blocks for missing files are rejected by the PLC's own STS 0x10 instead), unsolicited
messages from the PLC (ACKed and dropped), and every write. The driver sends only the three
read commands above (`ab_pccc.READ_COMMANDS`).
"""

from __future__ import annotations

import asyncio
import random
import struct
import time
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers import _schema as sc
from opencap.drivers._serial import (
    ByteTransport,
    Endpoint,
    Lease,
    TransportClosed,
    TransportUnmapped,
    endpoint_for,
    link_pool,
    make_transport,
    serial_properties,
)
from opencap.drivers.ab_pccc import (
    PCCC_PROPERTIES,
    CONFIG_EXT_STS,
    CONFIG_STS,
    EXT_STS_FOLLOWS,
    READ_COMMANDS,
    PcccBlock,
    decode_block,
    plan_pccc,
    pccc_status_text,
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

_log = structlog.get_logger("opencap.drivers.ab_df1")

# --- link layer: symbols, checks, framing ---------------------------------------------------------

DLE, STX, ETX, ENQ, ACK, NAK = 0x10, 0x02, 0x03, 0x05, 0x06, 0x15
DLE_ACK, DLE_NAK, DLE_ENQ = bytes((DLE, ACK)), bytes((DLE, NAK)), bytes((DLE, ENQ))
ERROR_CHECKS = ("crc", "bcc")
MAX_APP_BYTES = 512             # longest application message accepted (a typed read reply is <= 261)
MIN_APP_BYTES = 6               # DST SRC CMD STS TNS TNS


def _crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16/ARC (reflected polynomial 0xA001, initial value 0), table driven."""
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc


def bcc(data: bytes) -> int:
    """Block check character: two's complement of the 8-bit sum."""
    return -sum(data) & 0xFF


def check_bytes(app: bytes, check: str) -> bytes:
    if check == "crc":
        return crc16(bytes(app) + bytes((ETX,))).to_bytes(2, "little")
    if check == "bcc":
        return bytes((bcc(app),))
    raise ValueError(f"unknown DF1 error check {check!r} (crc or bcc)")


def stuff(app: bytes) -> bytes:
    return bytes(app).replace(bytes((DLE,)), bytes((DLE, DLE)))


def encode_frame(app: bytes, check: str) -> bytes:
    """DLE STX <stuffed app> DLE ETX <BCC | CRC low, CRC high>."""
    return bytes((DLE, STX)) + stuff(app) + bytes((DLE, ETX)) + check_bytes(app, check)


@dataclass(frozen=True, slots=True)
class LinkEvent:
    """One thing the receiver recognised: "ack", "nak", "enq", "frame" (good, `data` = the
    unstuffed application bytes) or "bad" (a frame to NAK, `reason` says why)."""

    kind: str
    data: bytes = b""
    reason: str = ""


class FrameDecoder:
    """Incremental DF1 full-duplex receiver: feed it bytes as they come, get LinkEvents.

    Bytes outside a frame other than a DLE response symbol are noise and are skipped. Inside a
    frame, DLE DLE is one data DLE; DLE ACK / DLE NAK / DLE ENQ are response symbols embedded
    in the frame (reported, and the frame goes on); DLE STX restarts the frame (the partial one
    is reported bad); any other DLE pair ends the frame as bad.
    """

    _IDLE, _IDLE_DLE, _DATA, _DATA_DLE, _CHECK = range(5)

    def __init__(self, check: str) -> None:
        if check not in ERROR_CHECKS:
            raise ValueError(f"unknown DF1 error check {check!r} (crc or bcc)")
        self.check = check
        self._need = 2 if check == "crc" else 1
        self._state = self._IDLE
        self._app = bytearray()
        self._chk = bytearray()
        self.noise = 0                       # bytes skipped outside frames

    @property
    def in_frame(self) -> bool:
        return self._state not in (self._IDLE, self._IDLE_DLE)

    def feed(self, data: bytes) -> list[LinkEvent]:
        out: list[LinkEvent] = []
        for b in data:
            st = self._state
            if st == self._IDLE:
                if b == DLE:
                    self._state = self._IDLE_DLE
                else:
                    self.noise += 1
            elif st == self._IDLE_DLE:
                self._state = self._IDLE
                if b == STX:
                    self._start()
                elif b in (ACK, NAK, ENQ):
                    out.append(LinkEvent({ACK: "ack", NAK: "nak", ENQ: "enq"}[b]))
                elif b == DLE:
                    self._state = self._IDLE_DLE
                    self.noise += 1
                else:
                    self.noise += 2
            elif st == self._DATA:
                if b == DLE:
                    self._state = self._DATA_DLE
                elif len(self._app) >= MAX_APP_BYTES:
                    out.append(LinkEvent("bad", reason=f"frame longer than {MAX_APP_BYTES} bytes"))
                    self._state = self._IDLE
                else:
                    self._app.append(b)
            elif st == self._DATA_DLE:
                self._state = self._DATA
                if b == DLE:
                    self._app.append(DLE)
                elif b == ETX:
                    self._chk.clear()
                    self._state = self._CHECK
                elif b in (ACK, NAK, ENQ):
                    out.append(LinkEvent({ACK: "ack", NAK: "nak", ENQ: "enq"}[b]))
                elif b == STX:
                    out.append(LinkEvent("bad", reason="DLE STX inside a frame (frame restarted)"))
                    self._start()
                else:
                    out.append(LinkEvent("bad", reason=f"invalid DLE sequence 10 {b:02x} inside a frame"))
                    self._state = self._IDLE
            else:  # _CHECK
                self._chk.append(b)
                if len(self._chk) == self._need:
                    out.append(self._finish())
                    self._state = self._IDLE
        return out

    def _start(self) -> None:
        self._app.clear()
        self._state = self._DATA

    def _finish(self) -> LinkEvent:
        app = bytes(self._app)
        want = check_bytes(app, self.check)
        if bytes(self._chk) != want:
            return LinkEvent("bad", app, f"{self.check.upper()} mismatch: got {bytes(self._chk).hex()}, "
                                         f"computed {want.hex()}")
        if len(app) < MIN_APP_BYTES:
            return LinkEvent("bad", app, f"frame of {len(app)} bytes is shorter than a DF1 message header")
        return LinkEvent("frame", app)


# --- application layer -------------------------------------------------------------------------------

CMD_DIAGNOSTIC, CMD_EXTENDED = 0x06, 0x0F
FNC_DIAGNOSTIC_STATUS, FNC_TYPED_READ_2, FNC_TYPED_READ_3 = 0x03, 0xA1, 0xA2
REPLY_BIT = 0x40
MAX_STATION = 254               # 255 is the broadcast address
# PCCC file type codes (pycomm3 `PCCC_DATA_TYPE`; I/O as pycomm3 sends them).
FILE_TYPE_CODES: dict[str, int] = {
    "O": 0x82, "I": 0x83, "S": 0x84, "B": 0x85, "T": 0x86, "C": 0x87, "N": 0x89, "F": 0x8A,
    "ST": 0x8D, "L": 0x91,
}
TC_BIT = {"EN": 15, "TT": 14, "DN": 13, "CU": 15, "CD": 14, "OV": 12, "UN": 11, "UA": 10}
TC_WORD = {"PRE": 1, "ACC": 2}
STRING_DATA = 82


@dataclass(frozen=True, slots=True)
class Df1Message:
    dst: int
    src: int
    cmd: int
    sts: int
    tns: int
    data: bytes = b""            # after TNS: FNC + function data (commands), data (replies)

    @property
    def ext_sts(self) -> int | None:
        return self.data[0] if self.sts == EXT_STS_FOLLOWS and self.data else None

    @property
    def reply_data(self) -> bytes:
        return self.data[1:] if self.sts == EXT_STS_FOLLOWS else self.data

    def encode(self) -> bytes:
        return bytes((self.dst, self.src, self.cmd, self.sts)) + struct.pack("<H", self.tns) + self.data


def parse_message(app: bytes) -> Df1Message:
    if len(app) < MIN_APP_BYTES:
        raise ValueError(f"DF1 message of {len(app)} bytes is shorter than its 6-byte header")
    return Df1Message(app[0], app[1], app[2], app[3], struct.unpack_from("<H", app, 4)[0], bytes(app[6:]))


def address_field(value: int) -> bytes:
    """PCCC logical address field: one byte below 255, else 0xFF + UINT (little-endian)."""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"PCCC address field {value} out of range")
    return bytes((value,)) if value < 0xFF else b"\xff" + struct.pack("<H", value)


def typed_read_function(block: PcccBlock, read_fnc: int = FNC_TYPED_READ_3) -> bytes:
    """FNC + function data of the protected typed logical read for one `ab_pccc` block.

    Same fields as pycomm3's `SLCDriver._read_tag` for `block.address`: byte count =
    elements x element size (a T/C sub-element reads the whole 6-byte element), I/O = element
    slot + sub-element word, S = file 2.
    """
    size = block.nbytes
    if not 1 <= size <= 0xFF:
        raise ValueError(f"{block.address}: {size} bytes do not fit the one-byte PCCC size field")
    code = FILE_TYPE_CODES.get(block.file_type)
    if code is None:
        raise ValueError(f"{block.address}: file type {block.file_type} has no PCCC type code")
    if block.slot is not None:                      # I/O: element = slot, sub-element = word
        element, sub = block.slot, block.start
    else:
        element, sub = block.start, 0
    fields = bytes((size,)) + address_field(block.file_number) + bytes((code,)) + address_field(element)
    if read_fnc == FNC_TYPED_READ_2 and sub == 0:
        return bytes((FNC_TYPED_READ_2,)) + fields
    return bytes((FNC_TYPED_READ_3,)) + fields + address_field(sub)


def _words(raw: bytes) -> list[int]:
    return [v for (v,) in struct.iter_unpack("<h", raw[: len(raw) // 2 * 2])]


def pccc_values(block: PcccBlock, data: bytes) -> Any:
    """Raw reply bytes -> the value pycomm3 `SLCDriver.read(block.address)` returns:
    a scalar for one element, a list for `{n}`; N/B/S/I/O words signed, L signed 32-bit,
    F float32, ST strings, T/C `.ACC`/`.PRE` the word, T/C bits a bool. Only whole elements
    are decoded: a short reply gives a shorter list (decode_block reports what is missing)."""
    ft = block.file_type
    if block.field is not None:                     # T/C sub-element: whole 6-byte element
        if len(data) < 6:
            return None
        words = _words(data[:6])
        if block.field in TC_WORD:
            return words[TC_WORD[block.field]]
        return bool(words[0] & (1 << TC_BIT[block.field]))
    size = block.elem_size
    data = data[: block.nbytes]
    raw = [data[i:i + size] for i in range(0, len(data) - size + 1, size)]
    if ft == "F":
        values: list[Any] = [struct.unpack("<f", r)[0] for r in raw]
    elif ft == "L":
        values = [struct.unpack("<i", r)[0] for r in raw]
    elif ft == "ST":
        values = []
        for r in raw:
            n = struct.unpack_from("<H", r)[0]
            text = bytearray(r[2:2 + STRING_DATA])
            text[0::2], text[1::2] = text[1::2], text[0::2]
            values.append(bytes(text[:n]).decode("iso-8859-1", "ignore"))
    else:
        values = [struct.unpack("<h", r)[0] for r in raw]
    if not values:
        return None
    return values if len(values) > 1 or block.count > 1 else values[0]


# --- params ---------------------------------------------------------------------------------------------

def _int(params: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"ab_df1 param {key}={value!r}: expected an integer")
    try:
        value = int(value)
    except ValueError:
        raise ValueError(f"ab_df1 param {key}={value!r}: expected an integer") from None
    if not lo <= value <= hi:
        raise ValueError(f"ab_df1 param {key}={value}: must be {lo}..{hi}")
    return value


def _bool(params: Mapping[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    raise ValueError(f"ab_df1 param {key}={value!r}: expected true or false")


@dataclass(frozen=True)
class Df1Params:
    """Typed view of `Device.params` for ab_df1 (defaults in docs/driver_params.md)."""

    station: int = 1                 # destination station (KEPServer device id)
    src_station: int = 0             # our station number on the link
    error_check: str = "crc"
    ack_timeout_ms: int = 1000
    nak_retries: int = 3
    enq_retries: int = 3
    read_fnc: int = FNC_TYPED_READ_3
    duplicate_detect: bool = True
    close_on_timeout: bool = False

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any] | None) -> Df1Params:
        p = params or {}
        check = str(p.get("error_check") or "crc").strip().lower()
        if check not in ERROR_CHECKS:
            raise ValueError(f"ab_df1 param error_check={p.get('error_check')!r}: must be crc or bcc")
        fnc_raw = p.get("read_fnc")
        fnc_text = str(fnc_raw).strip().lower() if fnc_raw is not None else "a2"
        fnc = {"a2": FNC_TYPED_READ_3, "0xa2": FNC_TYPED_READ_3, "162": FNC_TYPED_READ_3,
               "a1": FNC_TYPED_READ_2, "0xa1": FNC_TYPED_READ_2, "161": FNC_TYPED_READ_2}.get(fnc_text)
        if fnc is None:
            raise ValueError(f"ab_df1 param read_fnc={fnc_raw!r}: must be a2 or a1")
        return cls(
            station=_int(p, "station", 1, 0, MAX_STATION),
            src_station=_int(p, "src_station", 0, 0, MAX_STATION),
            error_check=check,
            ack_timeout_ms=_int(p, "ack_timeout_ms", 1000, 1, 60_000),
            nak_retries=_int(p, "nak_retries", 3, 0, 20),
            enq_retries=_int(p, "enq_retries", 3, 0, 20),
            read_fnc=fnc,
            duplicate_detect=_bool(p, "duplicate_detect", True),
            close_on_timeout=_bool(p, "close_on_timeout", False),
        )


# --- the link ---------------------------------------------------------------------------------------------

class LinkFailure(CommError):
    """A DF1 transaction failed on the link (no ACK, NAK retries used up, no reply)."""


@dataclass
class LinkStats:
    frames_sent: int = 0
    retransmissions: int = 0
    enqs_sent: int = 0
    acks_received: int = 0
    naks_received: int = 0
    acks_sent: int = 0
    naks_sent: int = 0
    enqs_answered: int = 0
    duplicates: int = 0
    unmatched: int = 0               # good frames that were not the reply we waited for
    source_mismatch: int = 0         # replies from another station number than the one addressed
    bad_frames: int = 0
    commands: Counter = field(default_factory=Counter)    # (CMD, FNC) sent

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "commands"}
        out["commands"] = {f"{c:#04x}/{f:#04x}": n for (c, f), n in sorted(self.commands.items())}
        return out


class Df1Link:
    """One DF1 full-duplex link (one serial line), shared by every station on it.

    `transact()` is called with `io_lock` held, so one transaction runs at a time. Received
    frames are always answered (ACK/NAK) at once, whether or not they are the awaited reply.
    """

    def __init__(self, transport: ByteTransport, check: str, *, duplicate_detect: bool = True) -> None:
        self.transport = transport
        self.check = check
        self.decoder = FrameDecoder(check)
        self.duplicate_detect = duplicate_detect
        self.io_lock = asyncio.Lock()
        self.stats = LinkStats()
        self._last_response = DLE_NAK        # repeated on DLE ENQ; NAK until a frame was received
        self._last_header: bytes | None = None
        self._backlog: deque[LinkEvent] = deque()
        self._tns = random.randrange(0x10000)

    def next_tns(self) -> int:
        self._tns = (self._tns + 1) & 0xFFFF
        return self._tns

    def _send(self, data: bytes) -> None:
        self.transport.write(data)

    def _respond(self, symbol: bytes) -> None:
        self._last_response = symbol
        self._send(symbol)
        if symbol == DLE_ACK:
            self.stats.acks_sent += 1
        else:
            self.stats.naks_sent += 1

    async def close(self) -> None:
        await self.transport.close()

    async def transact(self, request: Df1Message, *, timeout_s: float, ack_timeout_s: float,
                       nak_retries: int, enq_retries: int) -> Df1Message:
        """Send `request`, see it ACKed, and return the matching reply. Raises LinkFailure
        (a CommError) or TransportClosed (a ConnectionLost)."""
        if (request.cmd, request.data[:1][0] if request.data else None) not in READ_COMMANDS:
            raise NotImplementedError("opencap is read-only: only PCCC read commands are sent")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        frame = encode_frame(request.encode(), self.check)
        expect = (request.cmd | REPLY_BIT, request.tns)
        self.stats.commands[(request.cmd, request.data[0])] += 1
        self._send(frame)
        self.stats.frames_sent += 1
        acked = False
        naks = enqs = 0
        ack_due = loop.time() + ack_timeout_s
        while True:
            if not self._backlog:
                now = loop.time()
                if now >= deadline:
                    what = "reply" if acked else "ACK"
                    raise LinkFailure(f"no {what} within {timeout_s * 1000:.0f} ms "
                                      f"(NAKs {naks}, ENQs {enqs})")
                wait = (min(deadline, ack_due) if not acked else deadline) - now
                try:
                    data = await self.transport.read(max(wait, 0.0))
                except TimeoutError:
                    if not acked and loop.time() >= ack_due and loop.time() < deadline:
                        if enqs >= enq_retries:
                            raise LinkFailure(f"no ACK after {enqs} ENQ(s) "
                                              f"({ack_timeout_s * 1000:.0f} ms each)") from None
                        enqs += 1
                        self.stats.enqs_sent += 1
                        self._send(DLE_ENQ)
                        ack_due = loop.time() + ack_timeout_s
                    continue
                self._backlog.extend(self.decoder.feed(data))
                continue
            ev = self._backlog.popleft()
            if ev.kind == "ack":
                self.stats.acks_received += 1
                acked = True
            elif ev.kind == "nak":
                self.stats.naks_received += 1
                if acked:
                    continue                          # a stray NAK after the ACK: nothing to resend
                if naks >= nak_retries:
                    raise LinkFailure(f"message NAKed {naks + 1} time(s) (nak_retries={nak_retries})")
                naks += 1
                self.stats.retransmissions += 1
                self._send(frame)
                ack_due = loop.time() + ack_timeout_s
            elif ev.kind == "enq":
                self.stats.enqs_answered += 1
                self._send(self._last_response)
            elif ev.kind == "bad":
                self.stats.bad_frames += 1
                _log.debug("df1 bad frame", link=self.transport.describe(), reason=ev.reason)
                self._respond(DLE_NAK)
            else:
                self._respond(DLE_ACK)
                header = ev.data[1:6]
                if self.duplicate_detect and header == self._last_header:
                    self.stats.duplicates += 1
                    continue
                self._last_header = header
                msg = parse_message(ev.data)
                if (msg.cmd, msg.tns) == expect:
                    if msg.src != request.dst:
                        self.stats.source_mismatch += 1
                        if self.stats.source_mismatch == 1:
                            _log.info("df1 reply from another station", link=self.transport.describe(),
                                      addressed=request.dst, answered=msg.src)
                    return msg                        # a reply implies the request was received
                self.stats.unmatched += 1
                _log.debug("df1 unmatched frame", link=self.transport.describe(), src=msg.src, dst=msg.dst,
                           cmd=f"{msg.cmd:#04x}", tns=msg.tns, expected_tns=request.tns)


# --- driver -----------------------------------------------------------------------------------------------

def _classify(msg: Df1Message) -> tuple[str, str] | None:
    """None for STS 0, else ("config" | "comm", text), with ab_pccc's status tables."""
    if msg.sts == 0:
        return None
    text = pccc_status_text(msg.sts, msg.ext_sts)
    if msg.sts == EXT_STS_FOLLOWS:
        return ("config" if msg.ext_sts in CONFIG_EXT_STS else "comm"), text
    return ("config" if msg.sts in CONFIG_STS else "comm"), text


class AbDf1Driver(Driver):
    """SLC 500 / MicroLogix over DF1 full duplex. One attempt per call: retries, backoff and
    reconnects belong to the ChannelWorker (the link-level NAK/ENQ retries are part of one
    attempt and fit inside `device.timeout_ms`)."""

    name: ClassVar[str] = "ab_df1"
    is_async: ClassVar[bool] = True
    transport: ClassVar[str] = "serial, tcp, rfc2217"
    address_examples: ClassVar[tuple[str, ...]] = ("N7:0", "F8:3", "B3:0/5", "T4:0.ACC", "ST10:0")
    params_schema: ClassVar[dict[str, Any]] = sc.params_schema("ab_df1 params", {
        **PCCC_PROPERTIES,
        **serial_properties(("tcp", "serial", "rfc2217", "unmapped")),
        "station": sc.integer("destination station of the PLC on the link", 1, minimum=0, maximum=MAX_STATION),
        "src_station": sc.integer("our own station number on the link", 0, minimum=0, maximum=MAX_STATION),
        "error_check": sc.string("frame check: crc or bcc", "crc", enum=ERROR_CHECKS),
        "ack_timeout_ms": sc.integer("wait for the link-level ACK", 1000, minimum=1, maximum=60_000),
        "nak_retries": sc.integer("resends after a NAK", 3, minimum=0, maximum=20),
        "enq_retries": sc.integer("ENQs sent when no ACK arrives", 3, minimum=0, maximum=20),
        "read_fnc": sc.string("typed read function: a2 (default) or a1", "a2", enum=("a2", "a1")),
        "duplicate_detect": sc.boolean("drop a retransmitted reply with the same TNS", True),
        "close_on_timeout": sc.boolean("close the line after a timeout", False),
    })

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)
        self.params = Df1Params.from_mapping(device.params)       # ValueError on bad params
        self._lease: Lease | None = None
        self._endpoint: Endpoint | None = None
        self._rejected: set[str] = set()

    # --- pure ---

    def plan(self, tags: list[Tag]) -> Plan:
        return plan_pccc(tags, self.device.params, who=self.name)

    # --- I/O ---

    @property
    def timeout_s(self) -> float:
        return max(self.device.timeout_ms, 1) / 1000.0

    @property
    def connected(self) -> bool:
        return self._lease is not None and self._lease.alive

    @property
    def _where(self) -> str:
        ep = self._endpoint.describe() if self._endpoint is not None else self.device.host
        return f"{self.device.id} (station {self.params.station}, {ep})"

    def _link(self) -> Df1Link:
        lease = self._lease
        if lease is None or not lease.alive:
            raise ConnectionLost(f"{self._where}: not connected")
        return lease.conn

    async def connect(self) -> None:
        """Open (or join) the serial line. One attempt, bounded by `device.timeout_ms`.
        Raises ConnectionLost (unmapped COM port, open failed, other settings on the line)."""
        await self.close()
        try:
            endpoint = self._endpoint = endpoint_for(self.device)
        except (TransportUnmapped, ValueError) as exc:
            raise ConnectionLost(str(exc)) from None
        params = self.params

        async def opener() -> Df1Link:
            transport = make_transport(endpoint)
            await transport.open(self.timeout_s)
            return Df1Link(transport, params.error_check, duplicate_detect=params.duplicate_detect)

        async def closer(link: Df1Link) -> None:
            await link.close()

        started = time.monotonic()
        try:
            lease = await link_pool().acquire(("ab_df1", endpoint.key), id(self), opener, closer)
        except ConnectionLost as exc:
            _log.debug("df1 connect", device=self.device.id, result="failed", reason=str(exc))
            raise ConnectionLost(f"{self._where}: connect failed: {exc}") from None
        link: Df1Link = lease.conn
        if link.check != params.error_check:
            await lease.release()
            raise ConnectionLost(f"{self._where}: error_check {params.error_check} differs from "
                                 f"{link.check}, used by another device on the same line")
        self._lease = lease
        _log.debug("df1 connect", device=self.device.id, result="ok", link=endpoint.describe(),
                   shared=len(lease.link.users) > 1, ms=round((time.monotonic() - started) * 1000, 1))

    async def close(self) -> None:
        """Leave the line (closed when no other device uses it). Idempotent, never raises."""
        lease, self._lease = self._lease, None
        if lease is not None:
            try:
                await lease.release()
            except Exception as exc:   # pragma: no cover - transports do not raise on close
                _log.debug("df1 close error", device=self.device.id, error=repr(exc))

    async def _drop(self) -> None:
        lease = self._lease
        if lease is not None:
            await lease.drop()

    async def _transact(self, cmd: int, function: bytes, what: str) -> tuple[Df1Message, datetime]:
        link = self._link()
        p = self.params
        async with link.io_lock:
            if not self.connected:          # another device dropped the line while we waited
                raise ConnectionLost(f"{self._where}: not connected")
            request = Df1Message(p.station, p.src_station, cmd, 0, link.next_tns(), function)
            started = time.monotonic()
            try:
                reply = await link.transact(
                    request, timeout_s=self.timeout_s, ack_timeout_s=p.ack_timeout_ms / 1000,
                    nak_retries=p.nak_retries, enq_retries=p.enq_retries)
            except TransportClosed as exc:
                await self._drop()
                _log.debug("df1 request", device=self.device.id, request=what, result="conn_lost", error=str(exc))
                raise ConnectionLost(f"{self._where} {what}: {exc}") from None
            except LinkFailure as exc:
                if p.close_on_timeout:
                    await self._drop()
                _log.debug("df1 request", device=self.device.id, request=what, result="link_failure",
                           error=str(exc), ms=round((time.monotonic() - started) * 1000, 1))
                raise CommError(f"{self._where} {what}: {exc}"
                                + ("; line closed (close_on_timeout)" if p.close_on_timeout else "")) from None
            _log.debug("df1 request", device=self.device.id, request=what, tns=request.tns, sts=reply.sts,
                       ms=round((time.monotonic() - started) * 1000, 1))
            return reply, datetime.now(UTC)

    async def read(self, block: Block) -> dict[str, TagValue]:
        """One block = one protected typed logical read. Raises ConnectionLost, CommError (link
        failure, PCCC error) or BlockConfigError (the PLC says the address does not exist)."""
        if not isinstance(block, PcccBlock):
            raise TypeError(f"ab_df1 cannot read {type(block).__name__}")
        try:
            function = typed_read_function(block, self.params.read_fnc)
        except ValueError as exc:
            raise BlockConfigError(f"{self._where} {block.describe()}: {exc}") from None
        reply, ts = await self._transact(CMD_EXTENDED, function, block.address)
        problem = _classify(reply)
        if problem is not None:
            kind, text = problem
            msg = f"{self._where} {block.describe()}: {text}"
            if kind == "config":
                if block.address not in self._rejected:
                    self._rejected.add(block.address)
                    _log.warning("ab_df1.block_rejected", device=self.device.id, block=block.address,
                                 tags=block.tags[:20], count=len(block.tags), reason=text, quality="bad_config")
                raise BlockConfigError(msg)
            raise CommError(msg)
        data = reply.reply_data
        if len(data) != block.nbytes:
            _log.debug("df1 reply size", device=self.device.id, block=block.address, got=len(data),
                       expected=block.nbytes)
        decoded = decode_block(block, pccc_values(block, data))
        out: dict[str, TagValue] = {}
        for item in block.items:
            value = decoded.get(item.tag, DecodeError(f"{block.address}: not decoded"))
            if isinstance(value, DecodeError):
                _log.debug("df1 decode error", device=self.device.id, tag=item.tag, reason=value.reason)
                out[item.tag] = TagValue(None, "bad_decode", ts)
            else:
                out[item.tag] = TagValue(value, "good", ts)
        return out

    async def identity(self) -> dict[str, Any]:
        """Diagnostic status (CMD 0x06 FNC 0x03): processor catalog number, plus the link setup
        and counters. {} when not connected or the PLC does not answer. Never raises."""
        if not self.connected:
            return {}
        try:
            reply, _ = await self._transact(CMD_DIAGNOSTIC, bytes((FNC_DIAGNOSTIC_STATUS,)), "diagnostic status")
        except Exception as exc:
            _log.debug("df1 identity failed", device=self.device.id, error=str(exc))
            return {}
        out: dict[str, Any] = {
            "station": self.params.station,
            "link": self._endpoint.describe() if self._endpoint else None,
            "error_check": self.params.error_check,
        }
        if reply.sts == 0 and len(reply.reply_data) >= 16:
            catalog = reply.reply_data[5:16].decode("ascii", "replace").strip(" \x00")
            if catalog.isprintable():
                out["processor_type"] = catalog
        else:
            out["diagnostic_status"] = pccc_status_text(reply.sts, reply.ext_sts) if reply.sts else "short reply"
        lease = self._lease
        if lease is not None and lease.alive:
            out["link_stats"] = lease.conn.stats.as_dict()
        return out


__all__ = [
    "AbDf1Driver", "Df1Link", "Df1Message", "Df1Params", "FrameDecoder", "LinkEvent", "LinkFailure",
    "bcc", "check_bytes", "crc16", "encode_frame", "parse_message", "pccc_values", "stuff",
    "typed_read_function",
]
