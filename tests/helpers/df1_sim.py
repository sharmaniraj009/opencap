"""A DF1 full-duplex SLC 500 / MicroLogix on a TCP socket, as a serial-over-TCP gateway would
present it (one TCP connection = the serial line). `PtyBridge` puts it behind a real tty.

    sim = await Df1Sim.start({"N7": [1, 2, 3], "F8": [1.5]}, station=1, error_check="crc")
    driver: transport tcp, host 127.0.0.1, port sim.port, params {station: 1, error_check: crc}
    sim.set("N7:1", 42); sim.value("F8:0")                  # fixture setup on the station's data table
    sim.add_station(2, {"N7": [9]})                         # a second station on the same line
    sim.nak_next = 2                                        # NAK the next 2 frames (line noise)
    sim.corrupt_replies = 1                                 # send the next reply with a bad check
    await sim.stop(); await sim.restart()                   # power-off / power-on on the same port

The PLC behind each station is `tests/helpers/pccc_sim.PcccSim`'s data-table model (files,
typed reads with 2 and 3 address fields, diagnostic status, STS 0x10 for a missing file or a
read past the end, STS 0x60 + `write_attempts` for every write command), used without its
EtherNet/IP server. Only the link layer is new here, written independently of the driver's
(its own frame scanner and a bitwise CRC), so a mistake in either side shows up as a failed
round trip:

* frame = DLE STX, application bytes with DLE doubled, DLE ETX, BCC (two's complement of the
  8-bit sum) or CRC-16 (reflected 0xA001, initial 0, over the data + ETX, low byte first); the
  check bytes are not stuffed;
* every good frame is ACKed, every bad one (check mismatch, invalid DLE pair) NAKed; DLE ENQ is
  answered with the last response sent (NAK before any frame); a frame whose SRC/CMD/STS/TNS
  equal the previous good frame's is a duplicate: ACKed, not executed;
* a reply is retransmitted on NAK (up to 3 times) until ACKed.

Fault switches (each counts down per frame/reply): `mute` (never answer anything),
`nak_next` (NAK good frames), `lose_next` (the frame never arrives: no ACK, not executed),
`drop_acks` (execute and reply, but the ACK is lost), `no_reply` (ACK, never reply),
`corrupt_replies` (first transmission of a reply has a bad check), `duplicate_replies`
(send each reply twice), `stale_reply` (send an old, unmatched reply first), `embed_ack`
(put the DLE ACK inside the reply frame), `reply_delay_s` (reply this much later),
`answer_any_station` (point-to-point: the primary station answers every DST, from its own number).
Counters: `frames_in`, `bad_frames_in`, `acks_in`, `naks_in`, `enqs_in`, `duplicates_in`,
`acks_sent`, `naks_sent`, `replies_sent`, `retransmits`, `connections`; `commands` counts
(station, CMD, FNC); `write_attempts` sums the stations'.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections import Counter
from typing import Any

from tests.helpers.pccc_sim import (
    DEFAULT_PRODUCT_NAME,
    FNC_COMMANDS,
    STS_EXT,
    PcccSim,
    _PcccError,
)

SIM_HOST = "127.0.0.1"
_DLE, _STX, _ETX, _ENQ, _ACK, _NAK = 0x10, 0x02, 0x03, 0x05, 0x06, 0x15
REPLY_NAK_RETRIES = 3


def sim_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def sim_check(app: bytes, check: str) -> bytes:
    if check == "crc":
        return sim_crc(app + bytes([_ETX])).to_bytes(2, "little")
    return bytes([(256 - sum(app) % 256) % 256])


def sim_frame(app: bytes, check: str) -> bytes:
    body = bytearray([_DLE, _STX])
    for b in app:
        body += bytes([b, b]) if b == _DLE else bytes([b])
    return bytes(body) + bytes([_DLE, _ETX]) + sim_check(app, check)


def _scan(buf: bytearray, check: str) -> list[tuple[str, bytes]]:
    """Take complete symbols and frames out of `buf` (in place). Events: ("ack"|"nak"|"enq", b""),
    ("frame", app), ("bad", b"")."""
    n_check = 2 if check == "crc" else 1
    events: list[tuple[str, bytes]] = []
    i = 0
    while True:
        j = buf.find(_DLE, i)
        if j < 0 or j + 1 >= len(buf):
            del buf[: j if j >= 0 else len(buf)]
            return events
        code = buf[j + 1]
        if code in (_ACK, _NAK, _ENQ):
            events.append(({_ACK: "ack", _NAK: "nak", _ENQ: "enq"}[code], b""))
            i = j + 2
            continue
        if code != _STX:
            i = j + 1
            continue
        app, k, result = bytearray(), j + 2, None
        while k < len(buf):
            if buf[k] != _DLE:
                app.append(buf[k])
                k += 1
                continue
            if k + 1 >= len(buf):
                break
            nxt = buf[k + 1]
            if nxt == _DLE:
                app.append(_DLE)
                k += 2
            elif nxt == _ETX:
                if k + 2 + n_check > len(buf):
                    break
                got = bytes(buf[k + 2:k + 2 + n_check])
                result = ("frame" if got == sim_check(bytes(app), check) else "bad", bytes(app))
                k += 2 + n_check
                break
            else:
                result = ("bad", b"")
                k += 2
                break
        if result is None:                       # incomplete: wait for more bytes
            del buf[:j]
            return events
        events.append(result)
        i = k


class _Line:
    """One connection (= the serial line) and its link state."""

    def __init__(self, sim: Df1Sim, writer: asyncio.StreamWriter) -> None:
        self.sim, self.writer = sim, writer
        self.buf = bytearray()
        self.last_response = bytes([_DLE, _NAK])
        self.last_header: bytes | None = None
        self.awaiting: tuple[bytes, int] | None = None     # reply frame waiting for ACK, NAKs left

    def send(self, data: bytes) -> None:
        if not self.writer.is_closing():
            self.writer.write(data)

    def respond(self, ack: bool) -> None:
        self.last_response = bytes([_DLE, _ACK if ack else _NAK])
        self.send(self.last_response)
        if ack:
            self.sim.acks_sent += 1
        else:
            self.sim.naks_sent += 1

    def handle(self, kind: str, app: bytes) -> None:
        sim = self.sim
        if sim.mute:
            return
        if kind == "ack":
            sim.acks_in += 1
            self.awaiting = None
        elif kind == "nak":
            sim.naks_in += 1
            if self.awaiting is not None and self.awaiting[1] > 0:
                frame, left = self.awaiting
                self.awaiting = (frame, left - 1)
                sim.retransmits += 1
                self.send(frame)
        elif kind == "enq":
            sim.enqs_in += 1
            self.send(self.last_response)
        elif kind == "bad":
            sim.bad_frames_in += 1
            self.respond(False)
        else:
            self.frame(app)

    def frame(self, app: bytes) -> None:
        sim = self.sim
        sim.frames_in += 1
        if sim.lose_next:
            sim.lose_next -= 1
            return
        if sim.nak_next:
            sim.nak_next -= 1
            self.respond(False)
            return
        header = app[1:6]
        if header == self.last_header:
            sim.duplicates_in += 1
            self.respond(True)
            return
        self.last_header = header
        embed = sim.embed_ack
        if sim.drop_acks:
            sim.drop_acks -= 1
            self.last_response = bytes([_DLE, _ACK])      # the PLC sent it; the line lost it
        elif not embed:
            self.respond(True)
        if sim.no_reply:
            sim.no_reply -= 1
            return
        reply = sim.execute(app)
        if reply is None:
            return
        frame = sim_frame(reply, sim.error_check)
        first = frame
        if sim.corrupt_replies:
            sim.corrupt_replies -= 1
            first = frame[:-1] + bytes([frame[-1] ^ 0x5A])
        if embed:
            self.last_response = bytes([_DLE, _ACK])
            sim.acks_sent += 1
            first = first[:4] + bytes([_DLE, _ACK]) + first[4:]
        extra: list[bytes] = []
        if sim.stale_reply:
            sim.stale_reply -= 1
            stale = bytearray(reply)
            stale[4] ^= 0xFF                              # another TNS
            extra.append(sim_frame(bytes(stale), sim.error_check))
        dup = bool(sim.duplicate_replies)
        if dup:
            sim.duplicate_replies -= 1

        def send_reply() -> None:
            for f in extra:
                self.send(f)
            self.send(first)
            sim.replies_sent += 1
            self.awaiting = (frame, REPLY_NAK_RETRIES)
            if dup:
                self.send(frame)

        if sim.reply_delay_s > 0:
            asyncio.get_running_loop().call_later(sim.reply_delay_s, send_reply)
        else:
            send_reply()


class Df1Sim:
    def __init__(self, *, error_check: str, host: str) -> None:
        if error_check not in ("crc", "bcc"):
            raise ValueError("error_check must be crc or bcc")
        self.error_check = error_check
        self.host = host
        self.port = 0
        self.station = 1
        self.stations: dict[int, PcccSim] = {}
        self._server: asyncio.Server | None = None
        self._lines: set[_Line] = set()
        self.mute = False
        self.nak_next = self.lose_next = self.drop_acks = self.no_reply = 0
        self.corrupt_replies = self.duplicate_replies = self.stale_reply = 0
        self.embed_ack = False
        self.reply_delay_s = 0.0
        self.answer_any_station = False
        self.reset_counters()

    @classmethod
    async def start(cls, tables: dict[str, list], *, station: int = 1, error_check: str = "crc",
                    product_name: str = DEFAULT_PRODUCT_NAME, host: str = SIM_HOST, port: int = 0) -> Df1Sim:
        sim = cls(error_check=error_check, host=host)
        sim.station = station
        sim.add_station(station, tables, product_name=product_name)
        await sim._listen(port)
        return sim

    def add_station(self, station: int, tables: dict[str, list], *, product_name: str = DEFAULT_PRODUCT_NAME) -> PcccSim:
        plc = PcccSim(tables, product_name=product_name, host=self.host, port=0, large_forward_open=False,
                      directory=True, revision=None, serial=None, product_code=None, processor_type=None)
        self.stations[station] = plc
        return plc

    # --- fixture access ---

    @property
    def plc(self) -> PcccSim:
        return self.stations[self.station]

    def set(self, address: str, value: Any, *, station: int | None = None) -> None:
        self.stations[station if station is not None else self.station].set(address, value)

    def value(self, address: str, *, station: int | None = None) -> Any:
        return self.stations[station if station is not None else self.station].value(address)

    @property
    def write_attempts(self) -> int:
        return sum(p.write_attempts for p in self.stations.values())

    def reset_counters(self) -> None:
        self.frames_in = self.bad_frames_in = self.acks_in = self.naks_in = self.enqs_in = 0
        self.duplicates_in = self.acks_sent = self.naks_sent = self.replies_sent = self.retransmits = 0
        self.connections = 0
        self.commands: Counter[tuple[int, int, int | None]] = Counter()

    # --- PLC side ---

    def execute(self, app: bytes) -> bytes | None:
        dst, src, cmd = app[0], app[1], app[2]
        tns = struct.unpack_from("<H", app, 4)[0]
        plc = self.stations.get(dst)
        if plc is None and self.answer_any_station:
            dst, plc = self.station, self.stations[self.station]
        if plc is None:
            return None                                # nobody at that station
        body, fnc = app[6:], None
        try:
            if cmd in FNC_COMMANDS:
                if not body:
                    raise _PcccError(0x10)
                fnc, body = body[0], body[1:]
            self.commands[(dst, cmd, fnc)] += 1
            plc.request_count += 1
            plc.pccc_counts[(cmd, fnc)] += 1
            out, sts, ext = plc._pccc_command(cmd, fnc, body), 0, None
        except _PcccError as err:
            out, sts, ext = b"", err.sts, err.ext
        head = bytes([src, dst, cmd | 0x40, sts]) + struct.pack("<H", tns)
        if sts == STS_EXT:
            head += bytes([ext or 0])
        return head + out

    # --- server ---

    async def _listen(self, port: int) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = _Line(self, writer)
        self._lines.add(line)
        self.connections += 1
        try:
            while data := await reader.read(4096):
                line.buf += data
                for kind, app in _scan(line.buf, self.error_check):
                    line.handle(kind, app)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._lines.discard(line)
            with contextlib.suppress(Exception):
                writer.close()

    @property
    def running(self) -> bool:
        return self._server is not None

    async def stop(self) -> None:
        """Close the listener and the line (a PLC or gateway power-off)."""
        server, self._server = self._server, None
        for line in list(self._lines):
            with contextlib.suppress(Exception):
                line.writer.transport.abort()
        self._lines.clear()
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await server.wait_closed()

    async def restart(self) -> None:
        await self.stop()
        await self._listen(self.port)
