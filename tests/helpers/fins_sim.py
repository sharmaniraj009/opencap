"""Omron FINS simulator for tests: a CS/CJ-series CPU over FINS/UDP and FINS/TCP.

The conformance harness for the FINS driver (#13, #54). Protocol facts and their sources (Omron
W342, W421; PTC's Omron FINS Ethernet driver manual) are in docs/protocols/omron-fins.md; this
module follows that document.

Served over UDP and TCP on the same port number (a real Ethernet unit uses 9600 for both):

- FINS/UDP: one FINS frame per datagram; the reply goes to the datagram's source address (the
  unit's "automatic generation, dynamic" mode).
- FINS/TCP: the 16-byte FINS/TCP header ("FINS", length, command, error code), the node address
  handshake first (command 0 -> 1; client node 0 asks the server to allocate one, from 239-254),
  then FINS frames in "FINS frame send" (command 2) messages. A connection that does not start
  with "FINS" (for example raw FINS frames on a TCP socket) gets error 1 and is closed.

Commands, CS/CJ mode (W342 5-3):

- 0101 MEMORY AREA READ (word, bit, flag and 4-byte IR elements), 0104 MULTIPLE MEMORY AREA READ,
- 0501 CONTROLLER DATA READ (model and version for identity),
- areas CIO, WR, HR, AR, DM, EM banks 0-18 hex and the current EM bank (plus its bank number),
  timer/counter present values and completion flags, task flags, index and data registers, by
  the CS/CJ memory area codes.

Read-only: MEMORY AREA WRITE/FILL/TRANSFER (0102/0103/0105) and every other command that changes
the controller (run/stop, clock, forced set/reset, parameter/program/file writes, access right)
are refused with end code 0x0401 and counted in `write_attempts`; memory never changes from the
wire.

End codes follow W342 5-1-3. Which code a real CPU picks for some edge cases (a word read with a
non-zero bit field, a frame for another node) is NOT confirmed on hardware; those choices are
marked "sim choice" in docs/protocols/omron-fins.md.

Fault switches: `mute` (receive and count, answer nothing), `end_code` (answer every read with
this end code), `area_end_codes` (per area name), `cpu_data_end_code`, `cpu_error_bits` (OR into
SRES of every reply: 0x40 non-fatal, 0x80 fatal CPU error, data still valid), `short_reply` (drop
the last byte of every reply's FINS frame), `sid_offset` (answer with the wrong SID),
`reply_delay`, `split_replies` (TCP: each reply in two writes), `strict_node` (drop frames whose
DA1 is not the simulator's node), `restart()` on the same port with the same memory.

Values are set and read back with an encoder of its own (`set()` / `value()`) and an address
parser of its own for the KEPServerEX syntax (`parse_address()`), never shared with a driver, so
round-trip tests catch word-order, byte-order and bit-numbering mistakes in the driver.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import struct
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from tests.helpers.netcut import stable_port

SIM_HOST = "127.0.0.1"
DEFAULT_NODE = 10                     # the CPU's FINS node number (DA1 of requests)
DEFAULT_MODEL = "CJ1H-CPU66H"
DEFAULT_VERSION = "04.00"

FINS_HEADER_LEN = 10
MAX_FRAME = 2012                      # max FINS frame (header + command + data) over Ethernet (W421)
MAX_READ_WORDS = 999                  # 0101 over Ethernet (W342 5-2-2): (2012 - 14) / 2
MAX_MULTI_ITEMS = 167                 # 0104 over Ethernet/Controller Link (W342 5-3-5)

# FINS/TCP (W421 7-4)
TCP_MAGIC = b"FINS"
TCP_NODE_REQ = 0                      # client node address data send
TCP_NODE_RESP = 1                     # server node address data send
TCP_FRAME = 2                         # FINS frame send
TCP_ERROR_NOTICE = 3                  # FINS frame send error notification
TCP_ERR_MAGIC = 0x01
TCP_ERR_LENGTH = 0x02
TCP_ERR_COMMAND = 0x03
TCP_ERR_NODE_IN_USE = 0x21
TCP_ERR_NODE_RANGE = 0x23
TCP_ERR_NODE_SAME = 0x24
TCP_ERR_NO_NODE = 0x25
TCP_AUTO_NODES = range(239, 255)      # the unit's default automatic allocation range
TCP_MAX_CLIENTS = 16

# Commands
CMD_READ = 0x0101
CMD_MULTI_READ = 0x0104
CMD_CPU_DATA = 0x0501
WRITE_COMMANDS = frozenset({
    # the commands W342 5-1-1 marks as writing or changing the CPU ("See note."), CS/CJ mode
    0x0102, 0x0103, 0x0105,           # memory area write, fill, transfer
    0x0202, 0x0203,                   # parameter area write, fill (clear)
    0x0307, 0x0308,                   # program area write, clear
    0x0401, 0x0402,                   # run, stop
    0x0702,                           # clock write
    0x0C01, 0x0C02,                   # access right acquire, forced acquire
    0x2101, 0x2103, 0x2141,           # error clear, error log clear, FINS write access log clear
    0x2203, 0x2204, 0x2205, 0x2207, 0x2208, 0x220A, 0x220B, 0x220C, 0x2215, 0x2220,   # file memory
    0x2301, 0x2302,                   # forced set/reset, cancel
    0x2803, 0x2804, 0x2805,           # serial gateway (CompoWay/F, Modbus-RTU/ASCII passthrough)
    0x0304, 0x0305, 0x0403,           # CV-mode program protect / protect clear / reset
})

# End codes (MRES << 8 | SRES)
END_OK = 0x0000
END_UNSUPPORTED = 0x0401
END_TOO_LONG = 0x1001
END_TOO_SHORT = 0x1002
END_AREA = 0x1101
END_ADDRESS = 0x1103
END_RANGE = 0x1104
END_RESPONSE_TOO_LONG = 0x110B
END_PARAMETER = 0x110C


@dataclass(frozen=True)
class AreaSpec:
    """One FINS memory area as the simulator stores it."""

    name: str                 # "CIO", "W", "H", "A", "D", "E0".."E18", "T", "C", "TK", "DR", "IR"
    word_code: int | None     # area code for word (PV) access
    bit_code: int | None      # area code for bit / flag access
    size: int                 # elements
    offset: int = 0           # FINS word address of element 0 (counters 0x8000, IR 0x0100, DR 0x0200)
    flags: bool = False       # bit code reads one flag per element (T, C, TK), not bits of words
    width: int = 2            # bytes per word-code element (IR: 4)


def _em_codes(bank: int) -> tuple[int, int]:
    if bank <= 0x0F:
        return 0xA0 + bank, 0x20 + bank
    return 0x60 + bank - 0x10, 0xE0 + bank - 0x10


AREAS: dict[str, AreaSpec] = {a.name: a for a in (
    AreaSpec("CIO", 0xB0, 0x30, 6144),
    AreaSpec("W", 0xB1, 0x31, 512),
    AreaSpec("H", 0xB2, 0x32, 1536),                  # H512-H1535: function block holding area
    AreaSpec("A", 0xB3, 0x33, 960),                   # CS1/CJ1 (CJ2 adds A960-A1471, A10000-A11535)
    AreaSpec("D", 0x82, 0x02, 32768),
    *(AreaSpec(f"E{bank:X}", *_em_codes(bank), 32768) for bank in range(0x19)),
    AreaSpec("T", 0x89, 0x09, 4096, flags=True),
    AreaSpec("C", 0x89, 0x09, 4096, offset=0x8000, flags=True),
    AreaSpec("TK", None, 0x06, 32, flags=True),       # task flags (CJ2: 128)
    AreaSpec("IR", 0xDC, None, 16, offset=0x0100, width=4),
    AreaSpec("DR", 0xBC, None, 16, offset=0x0200),
)}
EM_CURRENT_WORD, EM_CURRENT_BIT = 0x98, 0x0A
EM_BANK_NO_CODE, EM_BANK_NO_ADDRESS = 0xBC, 0x0F00   # current EM bank number: BC 0F0000
DEFAULT_EM_BANKS = (0, 1, 2)

_STRUCT = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f",
           "int64": ">q", "uint64": ">Q", "float64": ">d"}


class _Fault(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"end code {code:#06x}")
        self.code = code


@dataclass
class TcpClient:
    """One FINS/TCP connection after the handshake."""

    client_node: int
    peer: tuple[str, int]
    frames: int = 0


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, sim: FinsSim) -> None:
        self.sim = sim
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport   # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sim.frames["udp"] += 1
        reply = self.sim.handle(data)
        if reply is None or self.sim.mute or self.transport is None:
            return
        transport = self.transport
        if self.sim.reply_delay:
            asyncio.get_running_loop().call_later(self.sim.reply_delay, self._send, transport, reply, addr)
        else:
            self._send(transport, reply, addr)

    @staticmethod
    def _send(transport: asyncio.DatagramTransport, reply: bytes, addr: tuple[str, int]) -> None:
        if not transport.is_closing():
            transport.sendto(reply, addr)


class FinsSim:
    """In-process FINS server for a CS/CJ CPU (UDP and TCP on the same port number)."""

    def __init__(self, *, host: str, port: int, udp: bool, tcp: bool, node: int, model: str,
                 version: str, em_banks: Iterable[int], sizes: Mapping[str, int]) -> None:
        if not 1 <= node <= 254:
            raise ValueError("FINS node must be 1..254")
        unknown = set(sizes) - set(AREAS)
        if unknown:
            raise ValueError(f"unknown areas {sorted(unknown)}")
        self.host, self.port = host, port
        self.udp, self.tcp = udp, tcp
        self.node = node
        self.model, self.version = model, version
        self.em_banks = sorted(set(em_banks))
        self.current_bank = self.em_banks[0] if self.em_banks else 0
        self.sizes = {name: spec.size for name, spec in AREAS.items()}
        self.sizes.update(sizes)
        self._words: dict[str, list[int]] = {}
        self._flags: dict[str, bytearray] = {}
        for name, spec in AREAS.items():
            if name.startswith("E") and int(name[1:], 16) not in self.em_banks:
                continue
            if spec.word_code is not None:
                self._words[name] = [0] * self.sizes[name]
            if spec.flags:
                self._flags[name] = bytearray(self.sizes[name])
        # fault switches
        self.mute = False
        self.end_code = 0
        self.area_end_codes: dict[str, int] = {}
        self.cpu_data_end_code = 0
        self.cpu_error_bits = 0
        self.short_reply = False
        self.sid_offset = 0
        self.reply_delay = 0.0
        self.split_replies = False
        self.strict_node = True
        # counters and what the last request looked like
        self.request_count = 0
        self.command_counts: Counter[int] = Counter()
        self.frames: Counter[str] = Counter()          # "udp", "tcp"
        self.write_attempts = 0
        self.error_replies: Counter[int] = Counter()
        self.misaddressed = 0
        self.malformed = 0
        self.tcp_errors: Counter[int] = Counter()     # FINS/TCP header error code -> times sent
        self.handshakes: list[int] = []               # client node per completed FINS/TCP handshake
        self.last_header: bytes | None = None
        self.tcp_clients: dict[asyncio.StreamWriter, TcpClient] = {}
        self._server: asyncio.base_events.Server | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @classmethod
    async def start(cls, *, host: str = SIM_HOST, port: int = 0, udp: bool = True, tcp: bool = True,
                    node: int = DEFAULT_NODE, model: str = DEFAULT_MODEL, version: str = DEFAULT_VERSION,
                    em_banks: Iterable[int] = DEFAULT_EM_BANKS, sizes: Mapping[str, int] | None = None) -> FinsSim:
        """`sizes`: area name -> elements, on top of the defaults in AREAS. EM banks not in
        `em_banks` are not fitted (reads get END_AREA, as W342 says for a missing EM area)."""
        sim = cls(host=host, port=port, udp=udp, tcp=tcp, node=node, model=model, version=version,
                  em_banks=em_banks, sizes=sizes or {})
        await sim._listen()
        return sim

    # --- lifecycle ---------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None or self._udp_transport is not None

    @property
    def clients(self) -> int:
        return len(self._writers)

    async def _listen(self) -> None:
        # With no port given, candidates come from stable_port(): random, below the ephemeral range.
        # Taking an ephemeral TCP port and then UDP on the same number failed 20 times in a row on
        # Windows, where ephemeral ports are handed out close together ("no port free", #216).
        loop = asyncio.get_running_loop()
        for _ in range(20):
            server = None
            port = self.port or stable_port(self.host)
            if self.tcp:
                try:
                    server = await asyncio.start_server(self._serve_tcp, self.host, port, reuse_address=True)
                except OSError:
                    if self.port:
                        raise
                    continue
                port = server.sockets[0].getsockname()[1]
            if self.udp:
                try:
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda: _UdpProtocol(self), local_addr=(self.host, port))
                except OSError:
                    if server is not None:
                        server.close()
                        await server.wait_closed()
                    if self.port:
                        raise
                    continue
                self._udp_transport = transport   # type: ignore[assignment]
                port = transport.get_extra_info("sockname")[1]
            self._server, self.port = server, port
            return
        raise RuntimeError("no port free for both TCP and UDP")

    async def stop(self) -> None:
        """Power-off: stop listening (TCP and UDP) and drop every open connection."""
        udp, self._udp_transport = self._udp_transport, None
        if udp is not None:
            udp.close()
        server, self._server = self._server, None
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()
        self.tcp_clients.clear()
        if server is not None:
            server.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 2.0)

    async def restart(self) -> None:
        """Power-on again on the same port, same memory."""
        await self.stop()
        await self._listen()

    async def __aenter__(self) -> FinsSim:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def reset_counters(self) -> None:
        self.request_count = 0
        self.command_counts.clear()
        self.frames.clear()
        self.write_attempts = 0
        self.error_replies.clear()
        self.misaddressed = 0
        self.malformed = 0
        self.tcp_errors.clear()
        self.handshakes.clear()

    # --- FINS/TCP ----------------------------------------------------------------------------------

    @staticmethod
    def _tcp_message(command: int, payload: bytes = b"", error: int = 0) -> bytes:
        return TCP_MAGIC + (8 + len(payload)).to_bytes(4, "big") + command.to_bytes(4, "big") \
            + error.to_bytes(4, "big") + payload

    async def _tcp_error(self, writer: asyncio.StreamWriter, command: int, error: int) -> None:
        """Send a FINS/TCP error; the caller then closes the connection (W421 7-4)."""
        self.tcp_errors[error] += 1
        writer.write(self._tcp_message(command, error=error))
        with contextlib.suppress(ConnectionError):
            await writer.drain()

    async def _serve_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(16)
                if header[:4] != TCP_MAGIC:
                    await self._tcp_error(writer, TCP_ERROR_NOTICE, TCP_ERR_MAGIC)
                    return
                length = int.from_bytes(header[4:8], "big")
                command = int.from_bytes(header[8:12], "big")
                if length < 8 or length > 8 + MAX_FRAME:
                    await self._tcp_error(writer, TCP_ERROR_NOTICE, TCP_ERR_LENGTH)
                    return
                payload = await reader.readexactly(length - 8)
                client = self.tcp_clients.get(writer)
                if command == TCP_NODE_REQ and client is None:
                    if not await self._handshake(writer, payload):
                        return
                    continue
                if command != TCP_FRAME or client is None:       # incl. a second handshake
                    await self._tcp_error(writer, TCP_ERROR_NOTICE, TCP_ERR_COMMAND)
                    return
                self.frames["tcp"] += 1
                client.frames += 1
                reply = self.handle(payload)
                if reply is None or self.mute:
                    continue
                if self.reply_delay:
                    await asyncio.sleep(self.reply_delay)
                message = self._tcp_message(TCP_FRAME, reply)
                if self.split_replies:
                    cut = len(message) // 2
                    writer.write(message[:cut])
                    await writer.drain()
                    await asyncio.sleep(0.05)
                    writer.write(message[cut:])
                else:
                    writer.write(message)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            self.tcp_clients.pop(writer, None)
            with contextlib.suppress(Exception):
                writer.close()

    async def _handshake(self, writer: asyncio.StreamWriter, payload: bytes) -> bool:
        if len(payload) != 4:
            await self._tcp_error(writer, TCP_NODE_RESP, TCP_ERR_LENGTH)
            return False
        requested = int.from_bytes(payload, "big")
        used = {c.client_node for c in self.tcp_clients.values()}
        if len(self.tcp_clients) >= TCP_MAX_CLIENTS:
            await self._tcp_error(writer, TCP_NODE_RESP, 0x20)
            return False
        if requested == 0:
            node = next((n for n in TCP_AUTO_NODES if n not in used and n != self.node), None)
            if node is None:
                await self._tcp_error(writer, TCP_NODE_RESP, TCP_ERR_NO_NODE)
                return False
        elif requested > 254:
            await self._tcp_error(writer, TCP_NODE_RESP, TCP_ERR_NODE_RANGE)
            return False
        elif requested == self.node:
            await self._tcp_error(writer, TCP_NODE_RESP, TCP_ERR_NODE_SAME)
            return False
        elif requested in used:
            await self._tcp_error(writer, TCP_NODE_RESP, TCP_ERR_NODE_IN_USE)
            return False
        else:
            node = requested
        self.tcp_clients[writer] = TcpClient(node, writer.get_extra_info("peername"))
        self.handshakes.append(node)
        writer.write(self._tcp_message(TCP_NODE_RESP, node.to_bytes(4, "big") + self.node.to_bytes(4, "big")))
        await writer.drain()
        return True

    # --- FINS frames -------------------------------------------------------------------------------

    def handle(self, frame: bytes) -> bytes | None:
        """One FINS command frame -> the FINS response frame (None: no reply)."""
        if len(frame) < FINS_HEADER_LEN + 2:
            self.malformed += 1
            return None
        icf = frame[0]
        if icf & 0x40:                                  # a response, not a command
            self.malformed += 1
            return None
        if self.strict_node and frame[4] != self.node:
            self.misaddressed += 1
            return None
        h = frame[:FINS_HEADER_LEN]
        command = int.from_bytes(frame[10:12], "big")
        self.last_header = h
        self.request_count += 1
        self.command_counts[command] += 1
        if len(frame) > MAX_FRAME:
            end, payload = END_TOO_LONG, b""
        else:
            try:
                end, payload = END_OK, self._execute(command, frame[12:])
            except _Fault as fault:
                end, payload = fault.code, b""
        if end:
            self.error_replies[end] += 1
        if icf & 0x01:                                  # response not required
            return None
        sid = (h[9] + self.sid_offset) & 0xFF
        # response: ICF response bit; destination and source swapped (SA1 = this node); SID echoed
        reply = bytes((0xC0, 0x00, 0x02, h[6], h[7], h[8], h[3], self.node, h[5], sid)) \
            + command.to_bytes(2, "big") + (end | self.cpu_error_bits).to_bytes(2, "big") + payload
        if self.short_reply:
            reply = reply[:-1]
        return reply

    def _execute(self, command: int, data: bytes) -> bytes:
        if command in WRITE_COMMANDS:
            self.write_attempts += 1
            raise _Fault(END_UNSUPPORTED)
        if command == CMD_READ:
            if self.end_code:
                raise _Fault(self.end_code)
            return self._memory_area_read(data)
        if command == CMD_MULTI_READ:
            if self.end_code:
                raise _Fault(self.end_code)
            return self._multiple_memory_area_read(data)
        if command == CMD_CPU_DATA:
            return self._controller_data(data)
        raise _Fault(END_UNSUPPORTED)

    def _resolve(self, code: int, address: int) -> tuple[str, str, int]:
        """Area code + FINS word address -> (area name, "word" | "bit" | "flag" | "bank", index)."""
        if code == EM_CURRENT_WORD:
            return f"E{self.current_bank:X}", "word", address
        if code == EM_CURRENT_BIT:
            return f"E{self.current_bank:X}", "bit", address
        if code == EM_BANK_NO_CODE and address == EM_BANK_NO_ADDRESS:
            return "", "bank", 0
        if code in (0x89, 0x09):                        # timers 0000.., counters 8000..
            name = "C" if address >= 0x8000 else "T"
            return name, ("flag" if code == 0x09 else "word"), address - AREAS[name].offset
        for spec in AREAS.values():
            if code == spec.word_code:
                return spec.name, "word", address - spec.offset
            if code == spec.bit_code:
                return spec.name, ("flag" if spec.flags else "bit"), address - spec.offset
        raise _Fault(END_AREA)

    def _read_items(self, code: int, address: int, bit: int, count: int) -> bytes:
        name, kind, index = self._resolve(code, address)
        if kind == "bank":
            if bit or count != 1:
                raise _Fault(END_ADDRESS)
            return self.current_bank.to_bytes(2, "big")
        if name.startswith("E") and name not in self._words:
            raise _Fault(END_AREA)                     # "no EM Area" (W342 end code 1101)
        if name in self.area_end_codes:
            raise _Fault(self.area_end_codes[name])
        size = self.sizes[name]
        if kind == "bit":
            if bit > 15 or not 0 <= index < size:
                raise _Fault(END_ADDRESS)
            first = index * 16 + bit
            if first + count > size * 16:
                raise _Fault(END_RANGE)
            words = self._words[name]
            return bytes((words[(first + i) // 16] >> ((first + i) % 16)) & 1 for i in range(count))
        if bit:
            raise _Fault(END_ADDRESS)                  # word/flag access: bit position must be 00 (sim choice)
        if not 0 <= index < size:
            raise _Fault(END_ADDRESS)
        if index + count > size:
            raise _Fault(END_RANGE)
        if kind == "flag":
            return bytes(self._flags[name][index:index + count])
        width = AREAS[name].width
        return b"".join(w.to_bytes(width, "big") for w in self._words[name][index:index + count])

    def _memory_area_read(self, data: bytes) -> bytes:
        if len(data) < 6:
            raise _Fault(END_TOO_SHORT)
        if len(data) > 6:
            raise _Fault(END_TOO_LONG)
        code, address, bit, count = data[0], int.from_bytes(data[1:3], "big"), data[3], int.from_bytes(data[4:6], "big")
        name, kind, _ = self._resolve(code, address)
        if count == 0:
            return b""                                 # W342: nothing read, normal response
        width = 1 if kind in ("bit", "flag") else (2 if kind == "bank" else AREAS[name].width)
        if count * width > MAX_READ_WORDS * 2:
            raise _Fault(END_RESPONSE_TOO_LONG)
        return self._read_items(code, address, bit, count)

    def _multiple_memory_area_read(self, data: bytes) -> bytes:
        if not data or len(data) % 4:
            raise _Fault(END_TOO_SHORT)
        if len(data) // 4 > MAX_MULTI_ITEMS:
            raise _Fault(END_TOO_LONG)
        out = bytearray()
        for i in range(0, len(data), 4):
            code, address, bit = data[i], int.from_bytes(data[i + 1:i + 3], "big"), data[i + 3]
            out.append(code)
            out += self._read_items(code, address, bit, 1)
        return bytes(out)

    def _controller_data(self, data: bytes) -> bytes:
        """0501 (W342 5-3-15): no data = everything (159 bytes); 00 = model, version, system use,
        area data (92); 01 = CPU Bus Unit configuration, remote I/O data, CPU Unit information (67)."""
        if self.cpu_data_end_code:
            raise _Fault(self.cpu_data_end_code)
        if len(data) > 1:
            raise _Fault(END_TOO_LONG)
        model = self.model.encode("ascii")[:20].ljust(20, b" ")          # space padded
        version = self.version.encode("ascii")[:20].ljust(20, b"\x00")   # null padded
        system = bytes((0x00, max(self.em_banks, default=0))) + bytes(38)   # DIP switches, largest EM bank
        # area data: program area size (KSteps), IOM size (KB), DM words, timer/counter size (KB),
        # EM non-file memory size (banks), reserved, memory card type, memory card size (KB)
        area = struct.pack(">HBHBBBBH", 0x00FA, 0x17, min(self.sizes["D"], 0xFFFF), 0x08,
                           len(self.em_banks), 0x00, 0x00, 0x0000)[:11] + b"\x00"
        units = bytes(64) + b"\x00\x00" + b"\x00"
        if not data:
            return model + version + system + area + units
        if data[0] == 0x00:
            return model + version + system + area
        if data[0] == 0x01:
            return units
        raise _Fault(END_PARAMETER)

    # --- memory access (own encoder; never shared with a driver) -----------------------------------

    def _store(self, name: str) -> list[int]:
        if name not in self._words:
            raise ValueError(f"area {name} is not fitted on this simulator")
        return self._words[name]

    def word(self, area: str, index: int) -> int:
        return self._store(area)[index]

    def set_word(self, area: str, index: int, value: int) -> None:
        width = AREAS[area].width if area in AREAS else 2
        if not 0 <= value < (1 << (8 * width)):
            raise ValueError(f"value {value} out of range for {area}")
        self._store(area)[index] = value

    def bit(self, area: str, index: int, bit: int) -> bool:
        return bool((self.word(area, index) >> bit) & 1)

    def set_bit(self, area: str, index: int, bit: int, value: bool) -> None:
        w = self.word(area, index)
        self.set_word(area, index, (w | (1 << bit)) if value else (w & ~(1 << bit)))

    def set_flag(self, area: str, index: int, value: bool) -> None:
        """Timer ("T"), counter ("C") completion flag or task flag ("TK")."""
        self._flags[area][index] = 1 if value else 0

    def flag(self, area: str, index: int) -> bool:
        return bool(self._flags[area][index])

    def set(self, address: str, value: Any, dtype: str | None = None, *, word_order: str = "low_first") -> None:
        """Store a value the way a CS/CJ program does. `dtype`: opencap name, plus "bcd16" and
        "bcd32" (KEPServerEX BCD/LBCD); default: the address's KEPServerEX default type.
        `word_order`: how 32-bit values sit in consecutive words ("low_first" = Omron native:
        the lower address holds the low word)."""
        a = parse_address(address)
        dtype = dtype or a.default_dtype
        area = self._area_name(a)
        if a.kind == "flag":
            self.set_flag(area, a.word, bool(value))
            return
        if a.kind == "bit" or dtype == "bool":
            if a.bit is None:
                raise ValueError(f"{address}: bool needs a bit address")
            self.set_bit(area, a.word, a.bit, bool(value))
            return
        if a.kind == "string":
            raw = str(value).encode("latin-1")[:a.str_len].ljust(a.str_len or 0, b"\x00")
            for i, w in enumerate(_string_words(raw, a.str_order or "H")):
                self.set_word(area, a.word + i, w)
            return
        if area == "IR":
            self.set_word("IR", a.word, struct.unpack(">I", struct.pack(_STRUCT[dtype], value))[0])
            return
        if a.bcd:
            dtype = {"uint16": "bcd16", "int16": "sbcd16", "uint32": "bcd32", "int32": "sbcd32"}[dtype]
        values = _flatten(value) if isinstance(value, list) else [value]
        words: list[int] = []
        for v in values:
            words.extend(_encode(v, dtype, word_order))
        for i, w in enumerate(words):
            self.set_word(area, a.word + i, w)

    def value(self, address: str, dtype: str | None = None, *, word_order: str = "low_first") -> Any:
        """Read back what `set()` stored (arrays: pass the address with [n] or [r][c])."""
        a = parse_address(address)
        dtype = dtype or a.default_dtype
        area = self._area_name(a)
        if a.kind == "flag":
            return self.flag(area, a.word)
        if a.kind == "bit" or dtype == "bool":
            if a.bit is None:
                raise ValueError(f"{address}: bool needs a bit address")
            return self.bit(area, a.word, a.bit)
        if a.kind == "string":
            n = len(_string_words(bytes(a.str_len or 0), a.str_order or "H"))
            raw = _string_bytes([self.word(area, a.word + i) for i in range(n)], a.str_order or "H")[:a.str_len]
            nul = raw.find(0)
            return bytes(raw if nul < 0 else raw[:nul]).decode("latin-1")
        if area == "IR":
            return struct.unpack(_STRUCT[dtype], self.word("IR", a.word).to_bytes(4, "big"))[0]
        if a.bcd:
            dtype = {"uint16": "bcd16", "int16": "sbcd16", "uint32": "bcd32", "int32": "sbcd32"}[dtype]
        n = _words_of(dtype)
        elements = a.rows * a.cols if a.dims else 1
        out = [_decode([self.word(area, a.word + e * n + i) for i in range(n)], dtype, word_order)
               for e in range(elements)]
        if not a.dims:
            return out[0]
        if len(a.dims) == 1:
            return out
        return [out[r * a.cols:(r + 1) * a.cols] for r in range(a.rows)]

    def _area_name(self, a: Address) -> str:
        return f"E{self.current_bank:X}" if a.area == "E" else a.area

    def fill(self, tags: Iterable[tuple[str, str]], *, seed: int = 0, word_order: str = "low_first") -> None:
        """Give every (address, dtype) a deterministic, well-formed value."""
        rng = random.Random(seed)
        for address, dtype in tags:
            a = parse_address(address)
            if a.kind == "string":
                self.set(address, "".join(rng.choice("ABCDEFGH0123456789") for _ in range(a.str_len or 2)))
                continue
            elements = a.rows * a.cols if a.dims else 1
            values = [_sample(dtype, rng) for _ in range(elements)]
            self.set(address, values if a.dims else values[0], dtype, word_order=word_order)


# --- the simulator's own address parser (KEPServerEX Omron FINS syntax; deliberately not a driver's) ---

# KEPServerEX data type -> opencap dtype (docs/protocols/omron-fins.md section 5)
KEP_DTYPES = {"Boolean": "bool", "Word": "uint16", "Short": "int16", "BCD": "bcd16", "DWord": "uint32",
              "Long": "int32", "LBCD": "bcd32", "Float": "float32", "String": "string"}
MODELS = ("CS1", "CJ1", "CJ2")


@dataclass(frozen=True)
class Address:
    """A parsed KEPServerEX-style Omron FINS address (CS1/CJ1/CJ2 models)."""

    area: str                     # "CIO","W","H","A","D","E0".."E18","E" (current bank),"T","C","TK","IR","DR"
    word: int                     # element number in KEPServerEX terms (timer 3 = 3; FINS address: codes())
    bit: int | None = None        # 0..15 for a bit address
    kind: str = "word"            # "word" | "bit" | "flag" (TS, CS, TK) | "string"
    dims: tuple[int, ...] = ()    # array dimensions
    str_len: int | None = None    # string length in characters
    str_order: str | None = None  # "H"/"L": two characters per word, high/low byte first; "D"/"E": one per word
    bcd: bool = False             # 'D' suffix: the Word/DWord is unsigned BCD, Short/Long signed BCD

    @property
    def rows(self) -> int:
        return self.dims[0] if self.dims else 1

    @property
    def cols(self) -> int:
        return self.dims[1] if len(self.dims) > 1 else 1

    @property
    def default_dtype(self) -> str:
        """KEPServerEX's default data type for a dynamically defined tag at this address."""
        if self.kind in ("bit", "flag"):
            return "bool"
        if self.kind == "string":
            return "string"
        if self.area in ("T", "C"):
            return "bcd16"
        if self.area == "IR":
            return "uint32"
        return "uint16"

    def codes(self) -> tuple[int, int]:
        """(FINS memory area code, FINS word address) of this address's first element."""
        if self.area == "E":
            return (EM_CURRENT_BIT if self.kind == "bit" else EM_CURRENT_WORD), self.word
        spec = AREAS[self.area]
        code = spec.bit_code if self.kind in ("bit", "flag") else spec.word_code
        assert code is not None
        return code, self.word + spec.offset

    def words(self, dtype: str | None = None) -> int:
        """Items a read of this tag covers: words, or bits/flags (1), or IR elements."""
        if self.kind in ("bit", "flag"):
            return 1
        if self.kind == "string":
            return len(_string_words(bytes(self.str_len or 0), self.str_order or "H"))
        if self.area == "IR":
            return 1
        return _words_of(dtype or self.default_dtype) * self.rows * self.cols


_ADDR = re.compile(
    r"(?P<area>CIO|DR|IR|TK|TS|CS|T|C|W|H|A|D|E)"
    r"(?:(?P<bank>\d{1,2}):)?"
    r"(?P<num>\d+)"
    r"(?:\.(?P<sfx>\d+)(?P<order>[HLDE])?|(?P<bcd>D))?"
    r"(?P<dims>(?:\s*\[\s*\d+\s*\])*)$")


def _limits(area: str, model: str) -> tuple[range, ...]:
    """Element numbers KEPServerEX accepts for an area on a model (PTC manual, CS1/CJ1/CJ2 tables)."""
    cj2 = model == "CJ2"
    return {
        "CIO": (range(6144),), "W": (range(512),), "H": (range(1536),), "D": (range(32768),),
        "A": (range(1472), range(10000, 11536)) if cj2 else (range(960),),
        "T": (range(4096),), "C": (range(4096),), "TK": (range(128 if cj2 else 32),),
        "IR": (range(16),), "DR": (range(16),),
    }.get(area, (range(32768),))                          # EM banks and the current bank


class AddressError(ValueError):
    """An address KEPServerEX would refuse. `reason`: "syntax" (not the grammar), "range" (a number,
    bank, bit, string length or extent outside what the model has) or "unsupported" (a combination
    KEPServerEX does not allow: a bit on a timer, the 'D' suffix on a Float, a 16-bit IR, ...)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def parse_address(address: str, dtype: str | None = None, *, model: str = "CJ1") -> Address:
    """KEPServerEX Omron FINS address -> Address. `dtype` (opencap name) is checked against the
    area's range (a 32-bit value needs two words). Raises AddressError (a ValueError) with a
    `reason` of "syntax", "range" or "unsupported" for text KEPServerEX does not accept on `model`."""
    if model not in MODELS:
        raise ValueError(f"fins sim: model {model!r} not modelled")
    text = address.strip().upper()
    m = _ADDR.match(text)
    if not m:
        raise AddressError("syntax", f"fins sim: cannot parse {address!r}")
    raw, number = m["area"], int(m["num"])
    dims = tuple(int(x) for x in re.findall(r"\d+", m["dims"]))
    sfx = int(m["sfx"]) if m["sfx"] is not None else None
    order, bcd = m["order"], m["bcd"] is not None
    if m["bank"] is not None:
        if raw != "E":
            raise AddressError("syntax", f"fins sim: {address!r}: only EM takes a bank")
        bank = int(m["bank"])
        if bank > (24 if model == "CJ2" else 12):
            raise AddressError("range", f"fins sim: {address!r}: no EM bank {bank} on {model}")
        area = f"E{bank:X}"
    else:
        area = raw
    if dims and (len(dims) > 2 or 0 in dims):
        raise AddressError("unsupported", f"fins sim: bad array dimensions in {address!r}")
    if raw in ("TS", "CS", "TK"):                          # completion and task flags
        if sfx is not None or bcd or dims:
            raise AddressError("unsupported", f"fins sim: {address!r}: a flag has no bit, suffix or array")
        area = {"TS": "T", "CS": "C", "TK": "TK"}[raw]
        _check(area, number, 1, address, model)
        return Address(area, number, None, "flag")
    if order is not None:                                  # string: <word>.<length>H|L|D|E
        assert sfx is not None
        two_per_word = order in "HL"
        if dims or raw in ("T", "C", "IR", "DR") or (two_per_word and sfx % 2):
            raise AddressError("unsupported", f"fins sim: bad string address {address!r}")
        if not (2 <= sfx <= 512 if two_per_word else 1 <= sfx <= 256):
            raise AddressError("range", f"fins sim: string length out of range in {address!r}")
        a = Address(area, number, None, "string", (), sfx, order)
        _check(area, number, a.words(), address, model)
        return a
    if sfx is not None:                                    # bit
        if dims or raw in ("T", "C", "IR", "DR"):
            raise AddressError("unsupported", f"fins sim: bad bit address {address!r}")
        if sfx > 15:
            raise AddressError("range", f"fins sim: bit above 15 in {address!r}")
        _check(area, number, 1, address, model)
        return Address(area, number, sfx, "bit")
    if dtype in ("bool", "string"):
        raise AddressError("unsupported", f"fins sim: {address!r}: {dtype} needs a bit or string address")
    if bcd and (raw in ("T", "C") or dtype in ("bcd16", "bcd32", "float32")):
        raise AddressError("unsupported", f"fins sim: {address!r}: 'D' suffix not allowed with {dtype or 'a BCD default'}")
    if raw == "IR" and dtype in ("uint16", "int16", "bcd16"):
        raise AddressError("unsupported", f"fins sim: {address!r}: index registers are 32-bit")
    a = Address(area, number, None, "word", dims, bcd=bcd)
    _check(area, number, a.words(dtype), address, model)
    return a


def _check(area: str, number: int, extent: int, address: str, model: str) -> None:
    for r in _limits("E" if area.startswith("E") else area, model):
        if number in r and number + extent - 1 in r:
            return
    raise AddressError("range", f"fins sim: {address!r} is outside {area} on {model}")


def _string_words(raw: bytes, order: str) -> list[int]:
    if order in "DE":                                      # one character per word
        return [(b << 8) if order == "D" else b for b in raw]
    raw = raw + b"\x00" * (len(raw) % 2)
    return [(raw[i] << 8) | raw[i + 1] if order == "H" else raw[i] | (raw[i + 1] << 8)
            for i in range(0, len(raw), 2)]


def _string_bytes(words: list[int], order: str) -> bytes:
    if order in "DE":
        return bytes((w >> 8) if order == "D" else (w & 0xFF) for w in words)
    out = bytearray()
    for w in words:
        out.extend((w >> 8, w & 0xFF) if order == "H" else (w & 0xFF, w >> 8))
    return bytes(out)


def _flatten(values: list) -> list:
    out: list = []
    for v in values:
        out.extend(_flatten(v) if isinstance(v, list) else [v])
    return out


def _words_of(dtype: str) -> int:
    if dtype in ("bcd16", "sbcd16"):
        return 1
    if dtype in ("bcd32", "sbcd32"):
        return 2
    return struct.calcsize(_STRUCT[dtype]) // 2


def to_bcd(value: int, digits: int) -> int:
    if not 0 <= value < 10 ** digits:
        raise ValueError(f"{value} does not fit in {digits} BCD digits")
    return int(str(value), 16)


def from_bcd(raw: int) -> int:
    text = f"{raw:x}"
    if any(c not in "0123456789" for c in text):
        raise ValueError(f"{raw:#x} is not BCD")
    return int(text)


def _encode(value: Any, dtype: str, word_order: str) -> list[int]:
    if dtype in ("bcd16", "bcd32", "sbcd16", "sbcd32"):
        n = _words_of(dtype)
        v = int(value)
        if dtype.startswith("s"):                          # signed BCD: MSB set for negative, 3 or 7 digits
            raw = to_bcd(abs(v), 4 * n - 1) | ((1 << (16 * n - 1)) if v < 0 else 0)
        else:
            raw = to_bcd(v, 4 * n)
        words = [(raw >> (16 * i)) & 0xFFFF for i in range(n)]          # low word first
    else:
        packed = struct.pack(_STRUCT[dtype], value)                      # big-endian bytes
        words = [int.from_bytes(packed[i:i + 2], "big") for i in range(0, len(packed), 2)][::-1]
    return words if word_order == "low_first" else words[::-1]


def _decode(words: list[int], dtype: str, word_order: str) -> Any:
    low_first = words if word_order == "low_first" else words[::-1]
    if dtype in ("bcd16", "bcd32", "sbcd16", "sbcd32"):
        raw = sum(w << (16 * i) for i, w in enumerate(low_first))
        if dtype.startswith("s"):
            sign = 1 << (16 * len(words) - 1)
            return -from_bcd(raw & ~sign) if raw & sign else from_bcd(raw)
        return from_bcd(raw)
    packed = b"".join(w.to_bytes(2, "big") for w in reversed(low_first))
    return struct.unpack(_STRUCT[dtype], packed)[0]


def _sample(dtype: str, rng: random.Random) -> Any:
    if dtype == "bool":
        return rng.random() < 0.5
    if dtype.startswith("float"):
        return rng.randint(-200_000, 200_000) / 8          # exact in float32 and float64
    if dtype == "bcd16":
        return rng.randrange(0, 10_000)
    if dtype == "bcd32":
        return rng.randrange(0, 100_000_000)
    bits = {"16": 16, "32": 32, "64": 64}[dtype[-2:]]
    if dtype.startswith("uint"):
        return rng.randrange(0, 1 << bits)
    return rng.randrange(-(1 << (bits - 1)), 1 << (bits - 1))
