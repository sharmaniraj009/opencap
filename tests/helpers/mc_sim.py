"""MELSEC MC protocol 3E simulator for tests: binary and ASCII frames, over TCP and UDP.

Serves what pymcprotocol 0.3.0 `Type3E` sends; stock pymcprotocol works against it unmodified
(tests/unit/test_mc_sim.py):

- batch read in word units (0401/0000; iQ-R 0401/0002) and in bit units (0401/0001, 0401/0003),
- random read (0403/0000, 0403/0002), read CPU model (0101/0000), loopback test (0619/0000),
- device specs in the Q form (3-byte number, 1-byte code) and the iQ-R form (4-byte number,
  2-byte code); ASCII frames with the device number in decimal or hex as the MC protocol defines
  per device (pymcprotocol's ASCII mode writes decimal digits for hex devices: a library bug the
  simulator exposes rather than hides).

Error end codes (QnA-compatible 3E frame as documented by Mitsubishi; the choice of code for each
simulated fault is ours and NOT confirmed on a real PLC): points out of range 0xC051, past the
device's size 0xC056, unknown device code 0xC05B, word access to a bit device off a 16-point
boundary 0xC05C (strict: the driver always aligns), unsupported command 0xC059. Error replies
carry the 9-byte error information block (network, PC, module I/O, station, command,
subcommand) like a real 3E reply.

Read-only: every write and remote-control command gets 0xC059 and is counted in
`write_attempts`; the device memory never changes from the wire.

Values are set and read back with an encoder of its own (`set()` / `value()`): struct
little-endian packing per word, independent of the driver's integer word combining, so the
round-trip tests catch word-order, byte-order and bit-numbering mistakes in the driver.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import struct
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opencap.config.models import Tag
from tests.helpers.netcut import stable_port

SIM_HOST = "127.0.0.1"
DEFAULT_CPU_TYPE = "Q03UDVCPU"
DEFAULT_CPU_CODE = 0x0366


@dataclass(frozen=True)
class SimDevice:
    name: str
    kind: str          # "bit" | "word"
    base: int          # how the MC protocol writes the number in ASCII frames
    code: int          # binary device code


_TABLE: tuple[SimDevice, ...] = tuple(SimDevice(*row) for row in (
    ("X", "bit", 16, 0x9C), ("Y", "bit", 16, 0x9D), ("DX", "bit", 16, 0xA2), ("DY", "bit", 16, 0xA3),
    ("B", "bit", 16, 0xA0), ("SB", "bit", 16, 0xA1), ("M", "bit", 10, 0x90), ("SM", "bit", 10, 0x91),
    ("L", "bit", 10, 0x92), ("F", "bit", 10, 0x93), ("V", "bit", 10, 0x94),
    ("TS", "bit", 10, 0xC1), ("TC", "bit", 10, 0xC0), ("SS", "bit", 10, 0xC7), ("SC", "bit", 10, 0xC6),
    ("CS", "bit", 10, 0xC4), ("CC", "bit", 10, 0xC3),
    ("D", "word", 10, 0xA8), ("SD", "word", 10, 0xA9), ("W", "word", 16, 0xB4), ("SW", "word", 16, 0xB5),
    ("R", "word", 10, 0xAF), ("ZR", "word", 16, 0xB0), ("TN", "word", 10, 0xC2), ("SN", "word", 10, 0xC8),
    ("CN", "word", 10, 0xC5),
))
DEVICES: dict[str, SimDevice] = {d.name: d for d in _TABLE}
_BY_CODE: dict[int, SimDevice] = {d.code: d for d in _TABLE}
# ASCII device codes: Q form 2 chars ("D*", "TS"), iQ-R form 4 chars ("D***", "STS*").
_IQR_ALIASES = {"SS": "STS", "SC": "STC", "SN": "STN"}
_BY_ASCII: dict[str, SimDevice] = {}
for _d in _TABLE:
    _BY_ASCII[_d.name.ljust(2, "*")] = _d
    _BY_ASCII[_IQR_ALIASES.get(_d.name, _d.name).ljust(4, "*")] = _d

# Roughly the default device sizes of a Q CPU (points); file registers R/ZR made available.
DEFAULT_SIZES: dict[str, int] = {
    "X": 0x2000, "Y": 0x2000, "DX": 0x2000, "DY": 0x2000, "B": 0x2000, "SB": 0x800,
    "M": 8192, "SM": 2048, "L": 8192, "F": 2048, "V": 2048,
    "TS": 2048, "TC": 2048, "TN": 2048, "SS": 0, "SC": 0, "SN": 0, "CS": 1024, "CC": 1024, "CN": 1024,
    "D": 12288, "SD": 2048, "W": 0x2000, "SW": 0x800, "R": 32768, "ZR": 0x10000,
}

MAX_WORD_POINTS = 960
MAX_BIT_POINTS = 7904
MAX_RANDOM_POINTS = 192
END_POINTS = 0xC051
END_RANGE = 0xC056
END_UNSUPPORTED = 0xC059
END_DEVICE = 0xC05B
END_REQUEST = 0xC05C
END_LENGTH = 0xC061
# Write and remote-control commands: refused (read-only simulator) and counted.
WRITE_COMMANDS = frozenset({0x1401, 0x1402, 0x1403, 0x1406, 0x1001, 0x1002, 0x1003, 0x1005, 0x1006,
                            0x1630, 0x1631, 0x1617, 0x1618})
_STRUCT = {"int16": "<h", "uint16": "<H", "int32": "<i", "uint32": "<I", "float32": "<f",
           "int64": "<q", "uint64": "<Q", "float64": "<d"}


class _Fault(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"end code {code:#06x}")
        self.code = code


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, sim: McSim) -> None:
        self.sim = sim
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport   # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sim.frames["udp"] += 1
        reply = self.sim.handle(data)
        if reply is not None and not self.sim.mute and self.transport is not None:
            self.transport.sendto(reply, addr)


class McSim:
    """In-process MC 3E server (TCP and UDP on the same port number)."""

    def __init__(self, sizes: Mapping[str, int], *, host: str, port: int, udp: bool, cpu_type: str,
                 cpu_code: int) -> None:
        unknown = set(sizes) - set(DEVICES)
        if unknown:
            raise ValueError(f"unknown devices {sorted(unknown)}")
        self.host, self.port = host, port
        self.udp = udp
        self.cpu_type, self.cpu_code = cpu_type, cpu_code
        self.sizes = dict(sizes)
        self._bits: dict[str, bytearray] = {}
        self._words: dict[str, list[int]] = {}
        for name, size in self.sizes.items():
            if DEVICES[name].kind == "bit":
                self._bits[name] = bytearray(size)
            else:
                self._words[name] = [0] * size
        self.mute = False                       # read requests, answer nothing
        self.split_replies = False              # TCP: send each reply in two writes, 50 ms apart
        self.end_codes: dict[str, int] = {}     # device name -> end code for reads touching it
        self.cpu_model_end_code = 0             # non-zero: refuse 0101 with this code
        self.request_count = 0
        self.command_counts: Counter[tuple[int, int]] = Counter()
        self.frames: Counter[str] = Counter()   # "binary", "ascii", "tcp", "udp"
        self.write_attempts = 0
        self.error_replies: Counter[int] = Counter()   # end code -> error replies sent
        self._server: asyncio.base_events.Server | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @classmethod
    async def start(cls, devices: Mapping[str, int] | None = None, *, host: str = SIM_HOST, port: int = 0,
                    udp: bool = True, cpu_type: str = DEFAULT_CPU_TYPE, cpu_code: int = DEFAULT_CPU_CODE) -> McSim:
        """`devices`: device name -> size in points, on top of DEFAULT_SIZES."""
        sizes = {**DEFAULT_SIZES, **(devices or {})}
        sim = cls(sizes, host=host, port=port, udp=udp, cpu_type=cpu_type, cpu_code=cpu_code)
        await sim._listen()
        return sim

    # --- lifecycle ---------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def clients(self) -> int:
        return len(self._writers)

    async def _listen(self) -> None:
        # With no port given, candidates come from stable_port() (random, below the ephemeral
        # range), as in fins_sim: an ephemeral TCP port and then UDP on the same number can fail
        # 20 times in a row on Windows.
        loop = asyncio.get_running_loop()
        for _ in range(20):
            port = self.port or stable_port(self.host)
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
                    server.close()
                    await server.wait_closed()
                    if self.port:
                        raise
                    continue
                self._udp_transport = transport   # type: ignore[assignment]
            self._server, self.port = server, port
            return
        raise RuntimeError("no port free for both TCP and UDP")

    async def stop(self) -> None:
        """Power-off: stop listening (TCP and UDP) and drop every open connection."""
        udp, self._udp_transport = self._udp_transport, None
        if udp is not None:
            udp.close()
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), 2.0)

    async def restart(self) -> None:
        """Power-on again on the same port, same memory."""
        await self.stop()
        await self._listen()

    async def __aenter__(self) -> McSim:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def reset_counters(self) -> None:
        self.request_count = 0
        self.command_counts.clear()
        self.frames.clear()
        self.write_attempts = 0
        self.error_replies.clear()

    async def _serve_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                first = await reader.readexactly(2)
                if first == b"\x50\x00":                              # binary 3E request
                    header = first + await reader.readexactly(7)
                    frame = header + await reader.readexactly(int.from_bytes(header[7:9], "little"))
                elif first == b"50":                                  # ASCII 3E request
                    header = first + await reader.readexactly(16)
                    frame = header + await reader.readexactly(int(header[14:18], 16))
                else:
                    return                                            # not MC protocol: drop
                self.frames["tcp"] += 1
                reply = self.handle(frame)
                if reply is None or self.mute:
                    continue
                if self.split_replies and len(reply) > 1:
                    cut = len(reply) // 2
                    writer.write(reply[:cut])
                    await writer.drain()
                    await asyncio.sleep(0.05)
                    writer.write(reply[cut:])
                else:
                    writer.write(reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            self._writers.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()

    # --- protocol --------------------------------------------------------------------------------

    def handle(self, frame: bytes) -> bytes | None:
        """One request frame (binary or ASCII) -> reply frame (None: not answerable)."""
        if frame[:2] == b"\x50\x00":
            self.frames["binary"] += 1
            return self._handle_binary(frame)
        if frame[:4] == b"5000":
            self.frames["ascii"] += 1
            return self._handle_ascii(frame)
        return None

    def _handle_binary(self, frame: bytes) -> bytes | None:
        if len(frame) < 15:
            return None
        route = frame[2:7]                                           # network, PC, module I/O, station
        body = frame[9:]
        cmd, sub = int.from_bytes(body[2:4], "little"), int.from_bytes(body[4:6], "little")
        self.request_count += 1
        self.command_counts[(cmd, sub)] += 1
        if int.from_bytes(frame[7:9], "little") != len(body):
            end, payload = END_LENGTH, b""
        else:
            try:
                end, payload = 0, self._execute(cmd, sub, body[6:], ascii_=False)
            except _Fault as fault:
                end, payload = fault.code, b""
            except (IndexError, ValueError):                         # truncated request data
                end, payload = END_LENGTH, b""
        if end:
            self.error_replies[end] += 1
            payload = route + cmd.to_bytes(2, "little") + sub.to_bytes(2, "little")
        data = end.to_bytes(2, "little") + payload
        return b"\xd0\x00" + route + len(data).to_bytes(2, "little") + data

    def _handle_ascii(self, frame: bytes) -> bytes | None:
        try:
            text = frame.decode("ascii")
            route = text[4:14]
            body = text[18:]
            cmd, sub = int(body[4:8], 16), int(body[8:12], 16)
        except (UnicodeDecodeError, ValueError):
            return None
        self.request_count += 1
        self.command_counts[(cmd, sub)] += 1
        try:
            end, payload = 0, self._execute(cmd, sub, body[12:], ascii_=True)
        except _Fault as fault:
            end, payload = fault.code, ""
        except ValueError:
            end, payload = 0xC050, ""
        if end:
            self.error_replies[end] += 1
            payload = f"{route}{cmd:04X}{sub:04X}"
        data = f"{end:04X}{payload}"
        return f"D000{route}{len(data):04X}{data}".encode("ascii")

    def _execute(self, cmd: int, sub: int, data: Any, *, ascii_: bool) -> Any:
        if cmd in WRITE_COMMANDS:
            self.write_attempts += 1
            raise _Fault(END_UNSUPPORTED)
        if cmd == 0x0401 and sub in (0x0000, 0x0001, 0x0002, 0x0003):
            iqr, words = sub in (0x0002, 0x0003), sub in (0x0000, 0x0002)
            dev, number, rest = self._device_spec(data, iqr=iqr, ascii_=ascii_)
            points = int(rest[:4], 16) if ascii_ else int.from_bytes(rest[:2], "little")
            if words:
                return self._encode_words(self._read_words(dev, number, points), ascii_)
            return self._encode_bits(self._read_bits(dev, number, points), ascii_)
        if cmd == 0x0403 and sub in (0x0000, 0x0002):
            return self._random_read(data, iqr=sub == 0x0002, ascii_=ascii_)
        if cmd == 0x0101 and sub == 0x0000:
            if self.cpu_model_end_code:
                raise _Fault(self.cpu_model_end_code)
            name = self.cpu_type.ljust(16)[:16]
            return name + f"{self.cpu_code:04X}" if ascii_ else name.encode("ascii") + self.cpu_code.to_bytes(2, "little")
        if cmd == 0x0619 and sub == 0x0000:                           # loopback test
            return data
        raise _Fault(END_UNSUPPORTED)

    def _device_spec(self, data: Any, *, iqr: bool, ascii_: bool) -> tuple[SimDevice, int, Any]:
        if ascii_:
            width = 4 if iqr else 2
            dev = _BY_ASCII.get(data[:width])
            if dev is None:
                raise _Fault(END_DEVICE)
            digits = data[width:width + (8 if iqr else 6)]
            return dev, int(digits, dev.base), data[width + (8 if iqr else 6):]
        if iqr:
            number, code, rest = int.from_bytes(data[0:4], "little"), int.from_bytes(data[4:6], "little"), data[6:]
        else:
            number, code, rest = int.from_bytes(data[0:3], "little"), data[3], data[4:]
        dev = _BY_CODE.get(code)
        if dev is None:
            raise _Fault(END_DEVICE)
        return dev, number, rest

    def _check_device(self, dev: SimDevice) -> None:
        if dev.name in self.end_codes:
            raise _Fault(self.end_codes[dev.name])

    def _read_words(self, dev: SimDevice, number: int, points: int) -> list[int]:
        self._check_device(dev)
        if not 1 <= points <= MAX_WORD_POINTS:
            raise _Fault(END_POINTS)
        size = self.sizes.get(dev.name, 0)
        if dev.kind == "bit":
            if number % 16:
                raise _Fault(END_REQUEST)
            if number + 16 * points > size:
                raise _Fault(END_RANGE)
            bits = self._bits[dev.name]
            return [sum(bits[number + 16 * i + k] << k for k in range(16)) for i in range(points)]
        if number + points > size:
            raise _Fault(END_RANGE)
        return list(self._words[dev.name][number:number + points])

    def _read_bits(self, dev: SimDevice, number: int, points: int) -> list[int]:
        self._check_device(dev)
        if not 1 <= points <= MAX_BIT_POINTS:
            raise _Fault(END_POINTS)
        if dev.kind != "bit":
            raise _Fault(END_REQUEST)
        if number + points > self.sizes.get(dev.name, 0):
            raise _Fault(END_RANGE)
        return list(self._bits[dev.name][number:number + points])

    def _random_read(self, data: Any, *, iqr: bool, ascii_: bool) -> Any:
        if ascii_:
            n_word, n_dword, rest = int(data[0:2], 16), int(data[2:4], 16), data[4:]
        else:
            n_word, n_dword, rest = data[0], data[1], data[2:]
        if not 1 <= n_word + n_dword <= MAX_RANDOM_POINTS:
            raise _Fault(END_POINTS)
        out: list[int] = []
        for i in range(n_word + n_dword):
            dev, number, rest = self._device_spec(rest, iqr=iqr, ascii_=ascii_)
            width = 1 if i < n_word else 2
            if dev.kind == "bit":
                self._check_device(dev)
                if number + 16 * width > self.sizes.get(dev.name, 0):
                    raise _Fault(END_RANGE)
                bits = self._bits[dev.name]
                words = [sum(bits[number + 16 * j + k] << k for k in range(16)) for j in range(width)]
            else:
                self._check_device(dev)
                if number + width > self.sizes.get(dev.name, 0):
                    raise _Fault(END_RANGE)
                words = self._words[dev.name][number:number + width]
            out.extend(words)
        return self._encode_words(out, ascii_)

    @staticmethod
    def _encode_words(words: list[int], ascii_: bool) -> Any:
        if ascii_:
            return "".join(f"{w:04X}" for w in words)
        return b"".join(w.to_bytes(2, "little") for w in words)

    @staticmethod
    def _encode_bits(bits: list[int], ascii_: bool) -> Any:
        if ascii_:
            return "".join("1" if b else "0" for b in bits)
        out = bytearray()
        for i in range(0, len(bits), 2):
            hi = 0x10 if bits[i] else 0
            lo = 0x01 if i + 1 < len(bits) and bits[i + 1] else 0
            out.append(hi | lo)
        return bytes(out)

    # --- memory access (own encoder; never shared with the driver) ------------------------------------

    def word(self, device: str, number: int) -> int:
        dev = DEVICES[device]
        if dev.kind == "bit":
            bits = self._bits[device]
            return sum(bits[number + k] << k for k in range(16))
        return self._words[device][number]

    def set_word(self, device: str, number: int, value: int) -> None:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"word value {value} out of range")
        dev = DEVICES[device]
        if dev.kind == "bit":
            for k in range(16):
                self._bits[device][number + k] = (value >> k) & 1
        else:
            self._words[device][number] = value

    def bit(self, device: str, number: int) -> bool:
        return bool(self._bits[device][number])

    def set_bit(self, device: str, number: int, value: bool) -> None:
        self._bits[device][number] = 1 if value else 0

    def set(self, address: str, value: Any, dtype: str = "uint16", *, word_order: str = "low_first") -> None:
        """Write a value the way a PLC program would store it. `word_order`: how 32/64-bit values
        sit in consecutive words ("low_first" = Mitsubishi native = driver first_word_low)."""
        a = _parse(address)
        dev = DEVICES[a.device]
        if dtype == "bool":
            if dev.kind == "bit":
                self.set_bit(a.device, a.number, bool(value))
                return
            if a.bit is None:
                raise ValueError(f"{address}: bool on a word device needs .bit")
            index, bit = self._bit_word(a, word_order)
            w = self.word(a.device, index)
            self.set_word(a.device, index, (w | (1 << bit)) if value else (w & ~(1 << bit)))
            return
        if dtype == "string":
            raw = str(value).encode("latin-1")[:a.str_len].ljust(a.str_len or 0, b"\x00")
            for i in range(0, len(raw), 2):
                b0, b1 = raw[i], raw[i + 1]
                w = (b0 << 8) | b1 if a.str_order == "H" else b0 | (b1 << 8)
                self.set_word(a.device, a.number + i // 2, w)
            return
        values = _flatten(value) if isinstance(value, list) else [value]
        words: list[int] = []
        for v in values:
            words.extend(_encode(v, dtype, word_order))
        step = 16 if dev.kind == "bit" else 1
        for i, w in enumerate(words):
            self.set_word(a.device, a.number + i * step, w)

    def value(self, address: str, dtype: str = "uint16", *, word_order: str = "low_first",
              count: int | None = None) -> Any:
        """Read back what `set()` wrote (arrays: pass the address with [n] or [r][c])."""
        a = _parse(address)
        dev = DEVICES[a.device]
        if dtype == "bool":
            if dev.kind == "bit":
                return self.bit(a.device, a.number)
            index, bit = self._bit_word(a, word_order)
            return bool((self.word(a.device, index) >> bit) & 1)
        if dtype == "string":
            raw = bytearray()
            for i in range((a.str_len or 0) // 2):
                w = self.word(a.device, a.number + i)
                raw.extend((w >> 8, w & 0xFF) if a.str_order == "H" else (w & 0xFF, w >> 8))
            raw = raw[:a.str_len]
            nul = raw.find(0)
            return bytes(raw if nul < 0 else raw[:nul]).decode("latin-1")
        n = struct.calcsize(_STRUCT[dtype]) // 2
        step = 16 if dev.kind == "bit" else 1
        elements = count or (a.rows * a.cols if a.dims else 1)
        out = []
        for e in range(elements):
            words = [self.word(a.device, a.number + (e * n + i) * step) for i in range(n)]
            out.append(_decode(words, dtype, word_order))
        if not a.dims:
            return out[0]
        if len(a.dims) == 1:
            return out
        return [out[r * a.cols:(r + 1) * a.cols] for r in range(a.rows)]

    @staticmethod
    def _bit_word(a: _Addr, word_order: str) -> tuple[int, int]:
        assert a.bit is not None
        if a.bit < 16:
            return a.number, a.bit
        # bit of the 32-bit value: low_first keeps bits 16..31 in the second word
        return (a.number + 1 if word_order == "low_first" else a.number), a.bit - 16

    def fill(self, tags: list[Tag], *, seed: int = 0, word_order: str = "low_first") -> None:
        """Give every tag a deterministic, well-formed value (floats exactly representable)."""
        rng = random.Random(seed)
        for tag in tags:
            a = _parse(tag.address)
            elements = a.rows * a.cols if a.dims else 1
            values = [_sample(tag.dtype, rng) for _ in range(elements)]
            if tag.dtype == "string":
                self.set(tag.address, "".join(rng.choice("ABCDEFGH0123456789") for _ in range((a.str_len or 2) - 1)),
                         "string")
            elif a.dims:
                self.set(tag.address, values, tag.dtype, word_order=word_order)
            else:
                self.set(tag.address, values[0], tag.dtype, word_order=word_order)


# --- the simulator's own address parser (deliberately not the driver's) ---------------------------------

@dataclass(frozen=True)
class _Addr:
    device: str
    number: int
    bit: int | None
    dims: tuple[int, ...]
    str_len: int | None
    str_order: str | None

    @property
    def rows(self) -> int:
        return self.dims[0] if self.dims else 1

    @property
    def cols(self) -> int:
        return self.dims[1] if len(self.dims) > 1 else 1


_NAMES = sorted(DEVICES, key=len, reverse=True)
_SIM_ADDR = re.compile(r"(?P<num>[0-9A-F]+)(?:[.:](?P<sfx>\d+))?(?P<dims>(?:\s*\[\d+\])*)")


def _parse(address: str) -> _Addr:
    text = address.strip().upper()
    str_order: str | None = None
    for name in ("ZR", "D", "W", "R"):
        for order in ("H", "L"):
            if text.startswith(f"{name}S{order}"):
                device, rest, str_order = name, text[len(name) + 2:], order
                break
        if str_order:
            break
    else:
        device = next((n for n in _NAMES if text.startswith(n)
                       and _SIM_ADDR.fullmatch(text[len(n):]) and _valid_digits(n, text[len(n):])), "")
        if not device:
            raise ValueError(f"sim: cannot parse {address!r}")
        rest = text[len(device):]
    m = _SIM_ADDR.fullmatch(rest)
    if not m:
        raise ValueError(f"sim: cannot parse {address!r}")
    number = int(m["num"], DEVICES[device].base)
    dims = tuple(int(x) for x in re.findall(r"\[(\d+)\]", m["dims"]))
    sfx = int(m["sfx"]) if m["sfx"] is not None else None
    if str_order:
        return _Addr(device, number, None, (), sfx, str_order)
    return _Addr(device, number, sfx, dims, None, None)


def _valid_digits(device: str, rest: str) -> bool:
    digits = re.match(r"[0-9A-F]+", rest)
    return digits is not None and (DEVICES[device].base == 16 or digits.group().isdigit())


def _flatten(values: list) -> list:
    out: list = []
    for v in values:
        out.extend(_flatten(v) if isinstance(v, list) else [v])
    return out


def _encode(value: Any, dtype: str, word_order: str) -> list[int]:
    raw = struct.pack(_STRUCT[dtype], value)
    words = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
    return words[::-1] if word_order == "high_first" else words


def _decode(words: list[int], dtype: str, word_order: str) -> Any:
    seq = words[::-1] if word_order == "high_first" else words
    raw = b"".join(bytes((w & 0xFF, w >> 8)) for w in seq)
    return struct.unpack(_STRUCT[dtype], raw)[0]


def _sample(dtype: str, rng: random.Random) -> Any:
    if dtype == "bool":
        return rng.random() < 0.5
    if dtype.startswith("float"):
        return rng.randint(-200_000, 200_000) / 8          # exact in float32 and float64
    if dtype == "string":
        return ""
    bits = {"16": 16, "32": 32, "64": 64}[dtype[-2:]]
    if dtype.startswith("uint"):
        return rng.randrange(0, 1 << bits)
    return rng.randrange(-(1 << (bits - 1)), 1 << (bits - 1))


# --- synthetic tag lists ---------------------------------------------------------------------------------

# (device, dtype, words or points it occupies). Bit devices advance in points, word devices in words.
_KINDS: tuple[tuple[str, str], ...] = (
    ("D", "float32"), ("D", "float32"), ("D", "int32"), ("D", "uint16"), ("D", "int16"), ("M", "bool"),
    ("B", "bool"), ("X", "bool"), ("W", "uint16"), ("Dbit", "bool"), ("R", "float32"), ("ZR", "uint16"),
    ("Y", "bool"), ("D", "uint32"), ("D", "float32"), ("M", "bool"),
)
_WIDTH = {"uint16": 1, "int16": 1, "int32": 2, "uint32": 2, "float32": 2}


def _kep_address(device: str, number: int) -> str:
    """Address text the way KEPServer writes it (zero-padded; hex where the device is hex)."""
    if device == "D":
        return f"D{number:07d}"
    if device in ("B", "X", "Y", "W"):
        return f"{device}{number:04X}"
    if device == "ZR":
        return f"ZR{number:06X}"
    return f"{device}{number:04d}"


def make_mc_tags(n: int, *, seed: int = 0) -> list[Tag]:
    """`n` non-overlapping tags mixing D float32/int32/uint16/int16/uint32, bits in D words, M/B/X/Y
    bits, W and ZR (hex) words and R floats, with small gaps and a few big jumps (several blocks
    per device). Addresses are written like the KEPServer export (`D0000002`, `B0000`)."""
    rng = random.Random(seed)
    order = list(_KINDS)
    rng.shuffle(order)
    cursor: dict[str, int] = {"D": 2, "Dbit": 6000, "M": 0, "B": 0, "X": 0, "Y": 0, "W": 0, "R": 0, "ZR": 0}
    tags: list[Tag] = []
    for i in range(n):
        device, dtype = order[i % len(order)]
        jump = 60 if rng.random() < 0.06 else rng.randint(0, 3)
        if device == "Dbit":
            word = cursor["Dbit"]
            cursor["Dbit"] += 1 + (jump if jump > 3 else 0)
            address = f"D{word:07d}.{rng.randint(0, 15):02d}"
        elif device in ("M", "B", "X", "Y"):
            number = cursor[device] + jump * (16 if jump > 3 else 1)
            cursor[device] = number + 1
            address = _kep_address(device, number)
        else:
            number = cursor[device] + jump
            cursor[device] = number + _WIDTH[dtype]
            address = _kep_address(device, number)
        tags.append(Tag(name=f"MC_{device.upper()}_{dtype.upper()}_{i:03d}", address=address, dtype=dtype))
    return tags
