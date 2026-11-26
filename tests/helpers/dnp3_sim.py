"""A DNP3 outstation over TCP for driver tests (#178; docs/spikes/dnp3.md §7.1, ADR-0015).

The application layer and point database are dnp3py's (MIT): `Outstation.process_request()` turns a
request fragment into response fragments. The link and transport front end below is ours, so every
fault switch lives in code we control and a master cannot tell it from a real device:

- link layer: CRC-16/DNP, 16-byte blocks, RESET_LINK_STATES / TEST_LINK_STATES / REQUEST_LINK_STATUS,
  confirmed and unconfirmed user data;
- transport: reassembly of requests, segmentation of responses (249 bytes per segment);
- application: CON on every non-final fragment of a multi-fragment response, the next fragment only
  after the master's CONFIRM; unsolicited responses (after the master's ENABLE_UNSOLICITED) with
  confirm timeout and retries, and the null unsolicited response with IIN1.7 after every start or
  restart (IEEE 1815-2012 4.6.4), repeated until a master confirms it.

Read-only toward the plant by construction (ADR-0008):
- SELECT / OPERATE / DIRECT_OPERATE (_NR) are counted in `control_attempts` and answered by dnp3py's
  default handler, which returns NOT_SUPPORTED for every point;
- WRITE is counted in `writes` and refused with IIN2.0, except g80v1 index 7 (clear IIN1.7, the
  standard restart acknowledgement, which touches no point and no clock);
- every other function outside `FC_SERVED` (freeze, restarts, assign class, file transfer, ...) is
  refused with IIN2.0 and only counted in `function_counts`.

Fault switches: `mute`, `drop_connection()`, `corrupt_crc`, `split_frames`, `delay`,
`unsolicited_burst()`, `event_overflow()`, `restart()` (same port, IIN1.7 set again), and per-point
flags and timestamps through `set_binary()` / `set_analog()` / `set_counter()`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from dnp3.application.builder import build_null_response, build_unsolicited_response
from dnp3.core.flags import IIN, AnalogQuality, BinaryQuality, CounterQuality
from dnp3.core.timestamp import DNP3Timestamp
from dnp3.database import Database, DatabaseConfig, EventBufferConfig
from dnp3.outstation import Outstation, OutstationConfig, UnsolicitedConfig

HOST = "127.0.0.1"

# --- link layer ------------------------------------------------------------------------------------

START = b"\x05\x64"
DIR, PRM = 0x80, 0x40
LINK_RESET, LINK_TEST, LINK_CONFIRMED, LINK_UNCONFIRMED, LINK_STATUS_REQUEST = 0, 2, 3, 4, 9
LINK_ACK, LINK_STATUS = 0, 11


def crc_dnp(data: bytes) -> int:
    """CRC-16/DNP: polynomial 0x3D65 (reflected 0xA6BC), init 0, output inverted; check value 0xEA82."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
    return ~crc & 0xFFFF


def _with_crc(chunk: bytes) -> bytes:
    return chunk + crc_dnp(chunk).to_bytes(2, "little")


def link_frame(control: int, dest: int, src: int, user_data: bytes = b"") -> bytes:
    header = START + bytes((5 + len(user_data), control)) + dest.to_bytes(2, "little") + src.to_bytes(2, "little")
    return _with_crc(header) + b"".join(_with_crc(user_data[i:i + 16]) for i in range(0, len(user_data), 16))


class LinkError(Exception):
    pass


@dataclass
class LinkFrame:
    control: int
    dest: int
    src: int
    user_data: bytes


async def read_link_frame(reader: asyncio.StreamReader) -> LinkFrame:
    header = await reader.readexactly(10)
    if header[:2] != START or crc_dnp(header[:8]) != int.from_bytes(header[8:10], "little"):
        raise LinkError(f"bad link header {header.hex()}")
    remaining, data = header[2] - 5, b""
    while remaining > 0:
        size = min(16, remaining)
        block = await reader.readexactly(size + 2)
        if crc_dnp(block[:size]) != int.from_bytes(block[size:], "little"):
            raise LinkError("bad data block CRC")
        data, remaining = data + block[:size], remaining - size
    return LinkFrame(header[3], int.from_bytes(header[4:6], "little"), int.from_bytes(header[6:8], "little"), data)


# --- transport and application ------------------------------------------------------------------------

T_FIN, T_FIR = 0x80, 0x40
AC_FIR, AC_FIN, AC_CON, AC_UNS = 0x80, 0x40, 0x20, 0x10
FC_CONFIRM, FC_READ, FC_WRITE = 0, 1, 2
FC_CONTROLS = frozenset({3, 4, 5, 6})            # SELECT, OPERATE, DIRECT_OPERATE, DIRECT_OPERATE_NR
FC_ENABLE_UNSOLICITED, FC_DISABLE_UNSOLICITED, FC_DELAY_MEASURE = 20, 21, 23
FC_SERVED = frozenset({FC_CONFIRM, FC_READ, FC_ENABLE_UNSOLICITED, FC_DISABLE_UNSOLICITED, FC_DELAY_MEASURE})
FC_NO_ACK = frozenset({6, 8, 10, 12})            # the _NR functions: no response at all
CLEAR_RESTART = bytes((80, 1, 0x00, 7, 7, 0x00))  # WRITE g80v1, qualifier 0x00, index 7, value 0


def segments(apdu: bytes, seq: int) -> tuple[list[bytes], int]:
    """Transport segments of at most 249 application bytes each, and the next transport sequence."""
    chunks = [apdu[i:i + 249] for i in range(0, len(apdu), 249)] or [b""]
    out = []
    for i, chunk in enumerate(chunks):
        hdr = (seq & 0x3F) | (T_FIR if i == 0 else 0) | (T_FIN if i == len(chunks) - 1 else 0)
        out.append(bytes((hdr,)) + chunk)
        seq += 1
    return out, seq & 0x3F


class _Connection:
    def __init__(self, sim: Dnp3Sim, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.sim, self.reader, self.writer = sim, reader, writer
        self.master = sim.master_address
        self.lock = asyncio.Lock()               # one frame on the wire at a time
        self.t_seq = 0
        self.partial = b""
        self.confirmed: dict[tuple[bool, int], asyncio.Event] = {}
        self.tasks: set[asyncio.Task] = set()

    async def send_frame(self, frame: bytes) -> None:
        sim = self.sim
        if sim.corrupt_crc > 0:
            sim.corrupt_crc -= 1
            frame = frame[:8] + bytes((frame[8] ^ 0xFF,)) + frame[9:]
        async with self.lock:
            with contextlib.suppress(ConnectionError):   # the reader sees the close and ends
                if sim.split_frames:                     # a frame across several TCP writes
                    for i in range(0, len(frame), 7):
                        self.writer.write(frame[i:i + 7])
                        await self.writer.drain()
                        await asyncio.sleep(0.002)
                else:
                    self.writer.write(frame)
                    await self.writer.drain()

    async def send_apdu(self, apdu: bytes) -> None:
        segs, self.t_seq = segments(apdu, self.t_seq)
        for seg in segs:
            await self.send_frame(link_frame(PRM | LINK_UNCONFIRMED, self.master, self.sim.address, seg))

    async def send_and_wait(self, apdu: bytes, timeout: float, retries: int) -> bool:
        """Send a fragment that has CON set and wait for its CONFIRM, retrying with the same bytes."""
        key = (bool(apdu[0] & AC_UNS), apdu[0] & 0x0F)
        event = self.confirmed[key] = asyncio.Event()
        try:
            for _ in range(retries + 1):
                await self.send_apdu(apdu)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout)
                    return True
            return False
        finally:
            self.confirmed.pop(key, None)

    async def run(self) -> None:
        unsolicited = asyncio.create_task(self._unsolicited_loop())
        try:
            while True:
                frame = await read_link_frame(self.reader)
                if self.sim.mute or frame.dest != self.sim.address or not frame.control & PRM:
                    continue
                self.master = frame.src
                fc = frame.control & 0x0F
                if fc in (LINK_RESET, LINK_TEST, LINK_CONFIRMED):
                    await self.send_frame(link_frame(LINK_ACK, self.master, self.sim.address))
                elif fc == LINK_STATUS_REQUEST:
                    await self.send_frame(link_frame(LINK_STATUS, self.master, self.sim.address))
                if fc in (LINK_CONFIRMED, LINK_UNCONFIRMED) and frame.user_data:
                    self._transport(frame.user_data)
        except (asyncio.IncompleteReadError, ConnectionError, LinkError):
            pass
        finally:
            unsolicited.cancel()
            for task in self.tasks:
                task.cancel()
            self.writer.close()

    def _transport(self, segment: bytes) -> None:
        hdr, body = segment[0], segment[1:]
        if hdr & T_FIR:
            self.partial = b""
        self.partial += body
        if hdr & T_FIN:
            apdu, self.partial = self.partial, b""
            task = asyncio.create_task(self._application(apdu))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _application(self, apdu: bytes) -> None:
        sim, ac, fc = self.sim, apdu[0], apdu[1]
        sim.requests += 1
        sim.function_counts[fc] += 1
        if fc == FC_CONFIRM:
            sim.outstation.process_request(apdu)
            if (event := self.confirmed.get((bool(ac & AC_UNS), ac & 0x0F))) is not None:
                event.set()
            return
        if fc in FC_CONTROLS:
            sim.control_attempts += 1
        if fc == FC_WRITE:
            sim.writes += 1
        refused = fc not in FC_SERVED | FC_CONTROLS and not (fc == FC_WRITE and apdu[2:] == CLEAR_RESTART)
        if sim.delay:
            await asyncio.sleep(sim.delay)
        if refused and fc in FC_NO_ACK:
            return
        took_events = False
        if refused:
            fragments = [build_null_response(iin=sim.outstation.iin | IIN.NO_FUNC_CODE_SUPPORT, seq=ac & 0x0F)]
        else:
            before = sim.event_count()
            fragments = sim.outstation.process_request(apdu)
            took_events = sim.event_count() < before
        for i, fragment in enumerate(fragments):
            data = bytearray(fragment.to_bytes())
            last = i == len(fragments) - 1
            if not last or took_events:  # IEEE 1815 4.2.2.4.3: events need a CONFIRM
                data[0] |= AC_CON
            if not last:
                if not await self.send_and_wait(bytes(data), sim.confirm_timeout, 0):
                    return
            else:
                await self.send_apdu(bytes(data))

    async def _unsolicited_loop(self) -> None:
        sim = self.sim
        while True:
            await asyncio.sleep(0.02)
            if sim.mute:
                continue
            if sim.null_unsolicited_pending:
                # dnp3py 0.6.0 has no call for the start-up null response; its unsolicited sequence is
                # used so that the next unsolicited response cannot repeat this one's number
                seq = sim.outstation._state.sequences.next_unsolicited_seq()
                null = build_unsolicited_response(objects=(), iin=sim.outstation.iin, seq=seq, con=True)
                if await self.send_and_wait(null.to_bytes(), sim.confirm_timeout, sim.unsolicited_retries):
                    sim.null_unsolicited_pending = False
                continue
            if (fragment := sim.outstation.generate_unsolicited()) is None:
                continue
            data = fragment.to_bytes()
            if not await self.send_and_wait(data, sim.confirm_timeout, sim.unsolicited_retries):
                sim.unsolicited_given_up += 1
                # dnp3py waits for this confirm before it builds the next unsolicited response
                sim.outstation.process_request(bytes((AC_FIR | AC_FIN | AC_UNS | (data[0] & 0x0F), FC_CONFIRM)))


# --- the simulator ---------------------------------------------------------------------------------

@dataclass
class _Last:
    value: object
    flags: int
    time: datetime | None


class Dnp3Sim:
    """`async with Dnp3Sim() as sim:` serves `sim.port` on 127.0.0.1 until the block ends.

    Points: binary inputs 0..binaries-1, analog inputs 0..analogs-1, counters 0..counters-1, all in
    event class 1 and starting with value 0 and the RESTART flag, as a device does after power-up.
    """

    def __init__(self, *, address: int = 10, master_address: int = 1, binaries: int = 4, analogs: int = 4,
                 counters: int = 2, event_buffer: int = 100, max_fragment_size: int = 2048,
                 confirm_timeout: float = 1.0, unsolicited_retries: int = 2) -> None:
        self.address, self.master_address = address, master_address
        self.sizes = (binaries, analogs, counters)
        self.event_buffer, self.max_fragment_size = event_buffer, max_fragment_size
        self.confirm_timeout, self.unsolicited_retries = confirm_timeout, unsolicited_retries
        # fault switches
        self.mute = False                       # read and drop every frame, answer nothing
        self.corrupt_crc = 0                    # corrupt the header CRC of the next N frames sent
        self.split_frames = False               # write every frame in 7-byte pieces
        self.delay = 0.0                        # seconds before each response
        # counters
        self.requests = 0
        self.function_counts: Counter[int] = Counter()
        self.control_attempts = 0
        self.writes = 0
        self.connections = 0
        self.unsolicited_given_up = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._conns: set[_Connection] = set()
        self._last: dict[tuple[str, int], _Last] = {}
        self.outstation = self._new_outstation()

    def _new_outstation(self) -> Outstation:
        n = self.event_buffer
        db = Database(DatabaseConfig(event_buffer_config=EventBufferConfig(
            max_binary_events=n, max_analog_events=n, max_counter_events=n)))
        binaries, analogs, counters = self.sizes
        for i in range(binaries):
            last = self._last.get(("binary", i))
            db.add_binary_input(i, value=bool(last.value) if last else False,
                                quality=BinaryQuality(last.flags) if last else BinaryQuality.RESTART)
        for i in range(analogs):
            last = self._last.get(("analog", i))
            db.add_analog_input(i, value=float(last.value) if last else 0.0,
                                quality=AnalogQuality(last.flags) if last else AnalogQuality.RESTART)
        for i in range(counters):
            last = self._last.get(("counter", i))
            db.add_counter(i, value=int(last.value) if last else 0,
                           quality=CounterQuality(last.flags) if last else CounterQuality.RESTART)
        config = OutstationConfig(address=self.address, master_address=self.master_address,
                                  max_fragment_size=self.max_fragment_size, time_sync_required=False,
                                  unsolicited=UnsolicitedConfig(enabled=True))
        self.null_unsolicited_pending = True
        return Outstation(config=config, database=db)

    # -- lifecycle --

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, HOST, self.port, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self.drop_connection()
            await self._server.wait_closed()
            self._server = None

    async def restart(self) -> None:
        """A device restart: connections drop, events are lost, the same port comes back with IIN1.7."""
        await self.stop()
        self.outstation = self._new_outstation()
        await self.start()

    async def __aenter__(self) -> Dnp3Sim:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        conn = _Connection(self, reader, writer)
        self._conns.add(conn)
        try:
            await conn.run()
        finally:
            self._conns.discard(conn)

    # -- faults --

    def drop_connection(self) -> None:
        """Close every open connection; the listener stays up."""
        for conn in list(self._conns):
            conn.writer.close()

    def unsolicited_burst(self, n: int, index: int = 0) -> list[float]:
        """N changes of analog input `index` at once (n events), sent unsolicited once enabled."""
        values = [float(1000 + i) for i in range(n)]
        for value in values:
            self.set_analog(index, value)
        return values

    def event_overflow(self) -> None:
        """More analog events than class 1 holds: IIN2.3 is set and the newest event is dropped."""
        self.unsolicited_burst(self.outstation.database.event_buffer.class1.max_size + 1)

    # -- points --

    def set_binary(self, index: int, value: bool, flags: int = BinaryQuality.ONLINE,
                   time: datetime | None = None) -> None:
        self._last[("binary", index)] = _Last(value, flags, time)
        self.outstation.database.update_binary_input(index, value, BinaryQuality(flags), _ts(time))

    def set_analog(self, index: int, value: float, flags: int = AnalogQuality.ONLINE,
                   time: datetime | None = None) -> None:
        self._last[("analog", index)] = _Last(value, flags, time)
        self.outstation.database.update_analog_input(index, value, AnalogQuality(flags), _ts(time))

    def set_counter(self, index: int, value: int, flags: int = CounterQuality.ONLINE,
                    time: datetime | None = None) -> None:
        self._last[("counter", index)] = _Last(value, flags, time)
        self.outstation.database.update_counter(index, value, CounterQuality(flags), _ts(time))

    @property
    def iin(self) -> IIN:
        return self.outstation.iin

    def event_count(self) -> int:
        buffer = self.outstation.database.event_buffer
        return buffer.class1.count + buffer.class2.count + buffer.class3.count


def _ts(time: datetime | None) -> DNP3Timestamp | None:
    return DNP3Timestamp.from_datetime(time) if time is not None else None
