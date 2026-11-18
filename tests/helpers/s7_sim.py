"""S7 PLC simulator for driver tests, built on python-snap7's own server (`snap7.server.Server`).

python-snap7 3.x ships a pure-Python S7 server that the real `snap7.client.Client` talks to
unmodified. This wrapper adds what the driver tests need:

- **TSAP check**: the COTP connection request must address `rack`/`slot`, as a real CPU
  requires; otherwise the socket is closed without a connection confirm. The connection type
  (first TSAP byte: 1 PG, 2 OP, 3 PC) of every connection is recorded in `tsaps`.
- **PDU negotiation** capped at `max_pdu` (snap7's server always caps at 480); a read whose
  reply would not fit the negotiated PDU gets header error 0x8500, as a real CPU refuses it.
- **Read-only**: every job other than setup communication and read var, and every USERDATA
  request other than an SZL read, is refused (header error 0x8104) and counted in
  `write_attempts`.
- **Faults**: `mute` (no reply), `item_errors[(area, db)] = return code`, `header_error` (every
  read gets that S7 header error), `short_reply` (one data byte missing), `stop()` (power off:
  open connections are reset, new ones refused) and `restart()` on the same port.
- **Counters**: `request_count`, `read_count`, `szl_count`, `refused_reads`, `oversize_reads`,
  `read_sizes`, `function_counts`, `connections`.
- `set(address, dtype, value)` / `value(address, dtype)` use an encoder written here, separate
  from the driver's parser and decoder, so a decoding mistake in the driver cannot cancel out.

Areas: `{"M": 512, "I": 64, "Q": 64, "DB100": 64, "T": 16, "C": 16}` (bytes; timers/counters
in elements, 2 bytes each), or initial bytes instead of a size.
"""

from __future__ import annotations

import asyncio
import random
import re
import socket
import struct
import threading
from collections import Counter
from typing import Any

from snap7.datatypes import S7Area, S7WordLen
from snap7.server import Server, ServerISOConnection
from snap7.type import SrvArea

SIM_HOST = "127.0.0.1"
PDU_JOB, PDU_USERDATA = 0x01, 0x07
FN_SETUP, FN_READ = 0xF0, 0x04

_SRV_AREAS = {"I": (SrvArea.PE, S7Area.PE), "Q": (SrvArea.PA, S7Area.PA), "M": (SrvArea.MK, S7Area.MK),
              "T": (SrvArea.TM, S7Area.TM), "C": (SrvArea.CT, S7Area.CT)}
_S7_TO_NAME = {S7Area.PE: "I", S7Area.PA: "Q", S7Area.MK: "M", S7Area.DB: "DB", S7Area.TM: "T", S7Area.CT: "C"}


# --- independent address codec (only what the tests write; not the driver's parser) -----------------

_WIDTH = {"X": 0, "DBX": 0, "": 0, "B": 1, "BYTE": 1, "DBB": 1, "C": 1, "CHAR": 1, "W": 2, "WORD": 2, "DBW": 2,
          "I": 2, "INT": 2, "D": 4, "DWORD": 4, "DBD": 4, "DI": 4, "DINT": 4, "REAL": 4, "LREAL": 8, "LINT": 8,
          "LWORD": 8, "STRING": -1}
_LOC = re.compile(r"(?:(?:DB(?P<db>\d+)[,.]?)|(?P<area>[IEQAMF]))(?P<type>[A-Z]*?)(?P<off>\d+)(?:\.(?P<sfx>\d+))?$")
_TC = re.compile(r"(?P<tc>[TCZ])(?P<n>\d+)$")
_AREA_ALIAS = {"E": "I", "A": "Q", "F": "M"}
_FMT = {("int", 1): "b", ("uint", 1): "B", ("int", 2): "h", ("uint", 2): "H", ("int", 4): "i",
        ("uint", 4): "I", ("int", 8): "q", ("uint", 8): "Q"}


def locate(address: str) -> tuple[str, int, int, int, int | None]:
    """(area, db, byte offset or T/C number, width in bytes (0 = bit, -1 = S7 string), suffix)."""
    text = address.strip().upper()
    if m := _TC.fullmatch(text):
        return ("T" if m["tc"] == "T" else "C"), 0, int(m["n"]), 2, None
    m = _LOC.fullmatch(text)
    if m is None:
        raise ValueError(f"sim cannot place {address!r}")
    area = "DB" if m["db"] else _AREA_ALIAS.get(m["area"], m["area"])
    sfx = int(m["sfx"]) if m["sfx"] is not None else None
    return area, int(m["db"] or 0), int(m["off"]), _WIDTH[m["type"]], sfx


def to_bcd(n: int) -> int:
    out = 0
    for shift in (0, 4, 8, 12):
        out |= (n % 10) << shift
        n //= 10
    return out


def ms_to_s5time(ms: int) -> int:
    for base_code, base in enumerate((10, 100, 1000, 10_000)):
        if ms % base == 0 and ms // base <= 999:
            return (base_code << 12) | to_bcd(ms // base)
    raise ValueError(f"{ms} ms has no exact S5TIME")


def encode(width: int, dtype: str, value: Any, big: bool) -> bytes:
    if dtype == "float32":
        raw = struct.pack(">f", value)
    elif dtype == "float64":
        raw = struct.pack(">d", value)
    else:
        sign = "int" if dtype.startswith("int") else "uint"
        raw = struct.pack(">" + _FMT[(sign, width)], value)
    return raw if big or width == 1 else raw[::-1]


def decode(width: int, dtype: str, raw: bytes, big: bool) -> Any:
    if not big and width > 1:
        raw = raw[::-1]
    if dtype == "float32":
        return struct.unpack(">f", raw)[0]
    if dtype == "float64":
        return struct.unpack(">d", raw)[0]
    sign = "int" if dtype.startswith("int") else "uint"
    return struct.unpack(">" + _FMT[(sign, width)], raw)[0]


# --- server ----------------------------------------------------------------------------------------

class _SimIsoConnection(ServerISOConnection):
    def __init__(self, sock: socket.socket, sim: S7Sim) -> None:
        super().__init__(sock)
        self._sim = sim

    def _parse_cotp_cr(self, data: bytes) -> bool:
        if not super()._parse_cotp_cr(data):
            return False
        tsap = self.called_tsap or b""
        if len(tsap) != 2:
            self._sim.rejected_tsaps.append(tsap.hex())
            return False
        ctype, rack, slot = tsap[0], tsap[1] >> 5, tsap[1] & 0x1F
        self._sim.tsaps.append((ctype, rack, slot))
        if (rack, slot) != (self._sim.rack, self._sim.slot):
            self._sim.rejected_tsaps.append(tsap.hex())
            return False                    # no connection confirm: the socket is closed
        return True


class _SimServer(Server):
    def __init__(self, sim: S7Sim) -> None:
        super().__init__(log=False)
        self.sim = sim
        self.host = sim.host
        self._pdu: dict[Any, int] = {}

    # Same as snap7's `_handle_client`, plus the TSAP check and socket tracking for stop().
    def _handle_client(self, client_socket: socket.socket, address: Any) -> None:
        sim = self.sim
        with sim._lock:
            sim._sockets.add(client_socket)
            sim.connections += 1
        try:
            conn = _SimIsoConnection(client_socket, sim)
            if not conn.accept_connection():
                return
            while self.running:
                try:
                    request = conn.receive_data()
                except TimeoutError:
                    continue
                except Exception:
                    break
                response = self._process_request(request, address)
                if response:
                    try:
                        conn.send_data(response)
                    except Exception:
                        break
        finally:
            try:
                client_socket.close()
            except OSError:
                pass
            with sim._lock:
                sim._sockets.discard(client_socket)
            with self.client_lock:
                me = threading.current_thread()
                if me in self.clients:
                    self.clients.remove(me)
                self.client_count = max(0, self.client_count - 1)
            self._pdu.pop(threading.current_thread().ident, None)

    def _process_request(self, request_data: bytes, client_address: Any) -> bytes | None:
        sim = self.sim
        with sim._lock:
            sim.request_count += 1
            kind = _classify(request_data)
            sim.function_counts[kind] += 1
            if kind == "read":
                sim.read_count += 1
            elif kind == "szl":
                sim.szl_count += 1
            elif kind not in ("setup",):
                sim.write_attempts += 1
        if sim.mute:
            return None
        if kind not in ("setup", "read", "szl"):
            return self._build_error_response(self._parse_request(request_data), 0x8104)
        return super()._process_request(request_data, client_address)

    def _handle_setup_communication(self, request: dict[str, Any]) -> bytes:
        requested = request["parameters"].get("pdu_length", 480)
        negotiated = min(requested, self.sim.max_pdu)
        self.sim.pdu_requests.append(requested)
        reply = bytearray(super()._handle_setup_communication(request))
        struct.pack_into(">H", reply, len(reply) - 2, negotiated)
        self._pdu[threading.current_thread().ident] = negotiated
        return bytes(reply)

    def _handle_read_area(self, request: dict[str, Any], client_address: Any) -> bytes:
        sim = self.sim
        if sim.header_error is not None:
            return self._build_error_response(request, sim.header_error)
        spec = (request.get("parameters") or {}).get("address_spec") or {}
        width = 2 if spec.get("word_len") in (S7WordLen.TIMER, S7WordLen.COUNTER) else 1
        nbytes = int(spec.get("count", 0)) * width
        pdu = self._pdu.get(threading.current_thread().ident, 480)
        if 18 + nbytes > pdu:
            sim.oversize_reads += 1
            return self._build_error_response(request, 0x8500)
        sim.read_sizes.append(nbytes)
        return super()._handle_read_area(request, client_address)

    def _read_from_memory_area(self, area: S7Area, db_number: int, start: int, count: int) -> tuple[int, bytearray]:
        sim = self.sim
        name = _S7_TO_NAME.get(area, "?")
        forced = sim.item_errors.get((name, db_number if name == "DB" else 0))
        if forced is not None:
            sim.refused_reads += 1
            return forced, bytearray()
        rc, data = super()._read_from_memory_area(area, db_number, start, count)
        if rc != 0xFF:
            sim.refused_reads += 1
        if rc == 0xFF and sim.short_reply and data:
            data = data[:-1]
        return rc, data


def _classify(pdu: bytes) -> str:
    if len(pdu) < 11 or pdu[0] != 0x32:
        return "garbage"
    if pdu[1] == PDU_JOB:
        return {FN_SETUP: "setup", FN_READ: "read"}.get(pdu[10], f"job_0x{pdu[10]:02x}")
    if pdu[1] == PDU_USERDATA and len(pdu) >= 17:
        group, sub = pdu[15] & 0x0F, pdu[16]
        return "szl" if (group, sub) == (4, 1) else f"userdata_{group}_{sub}"
    return f"pdu_0x{pdu[1]:02x}"


class S7Sim:
    """One simulated S7 CPU on 127.0.0.1. Use `await S7Sim.start(...)`, or `async with`."""

    def __init__(self, areas: dict[str, int | bytes | bytearray], *, host: str, port: int, rack: int, slot: int,
                 max_pdu: int) -> None:
        self.host = host
        self.port = port
        self.rack, self.slot = rack, slot
        self.max_pdu = max_pdu
        self.mute = False
        self.short_reply = False
        self.header_error: int | None = None
        self.item_errors: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self.reset_counters()
        self.tsaps: list[tuple[int, int, int]] = []
        self.rejected_tsaps: list[str] = []
        self.pdu_requests: list[int] = []
        self.connections = 0
        self.areas: dict[tuple[str, int], bytearray] = {}
        self._server: _SimServer | None = None
        for key, spec in areas.items():
            self.add_area(key, spec)

    def reset_counters(self) -> None:
        self.request_count = 0
        self.read_count = 0
        self.szl_count = 0
        self.write_attempts = 0
        self.oversize_reads = 0
        self.refused_reads = 0                     # reads answered with an item error (missing DB, past the end)
        self.function_counts: Counter[str] = Counter()
        self.read_sizes: list[int] = []

    @classmethod
    async def start(cls, areas: dict[str, int | bytes | bytearray], *, host: str = SIM_HOST, port: int = 0,
                    rack: int = 0, slot: int = 1, max_pdu: int = 480) -> S7Sim:
        sim = cls(areas, host=host, port=port, rack=rack, slot=slot, max_pdu=max_pdu)
        sim._listen()
        return sim

    async def __aenter__(self) -> S7Sim:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # --- memory ---

    def add_area(self, key: str, spec: int | bytes | bytearray) -> None:
        name, db = ("DB", int(key[2:])) if key.upper().startswith("DB") else (key.upper(), 0)
        width = 2 if name in ("T", "C") else 1
        data = bytearray(spec * width) if isinstance(spec, int) else bytearray(spec)
        self.areas[(name, db)] = data
        if self._server is not None:
            self._register(self._server, name, db, data)

    def remove_area(self, key: str) -> None:
        name, db = ("DB", int(key[2:])) if key.upper().startswith("DB") else (key.upper(), 0)
        self.areas.pop((name, db))
        if self._server is not None:
            s7 = S7Area.DB if name == "DB" else _SRV_AREAS[name][1]
            self._server.memory_areas.pop((s7, db), None)

    @staticmethod
    def _register(server: _SimServer, name: str, db: int, data: bytearray) -> None:
        if name == "DB":
            server.register_area(SrvArea.DB, db, data)
        else:
            server.register_area(_SRV_AREAS[name][0], 0, data)   # snap7 server stores the same bytearray

    def raw(self, area: str, db: int = 0) -> bytearray:
        return self.areas[(area, db)]

    def set(self, address: str, dtype: str, value: Any, *, big: bool = True) -> None:
        area, db, off, width, sfx = locate(address)
        mem = self.areas[(area, db)]
        if area == "T":
            struct.pack_into(">H", mem, off * 2, ms_to_s5time(int(value)))
        elif area == "C":
            struct.pack_into(">H", mem, off * 2, to_bcd(int(value)))
        elif dtype == "bool" and width in (0, 1):
            mask = 1 << (sfx or 0)
            mem[off] = (mem[off] | mask) if value else (mem[off] & ~mask)
        elif dtype == "bool":                                   # bit of a word/dword value
            word = int.from_bytes(mem[off:off + width], "big" if big else "little")
            word = (word | (1 << sfx)) if value else (word & ~(1 << sfx))
            mem[off:off + width] = word.to_bytes(width, "big" if big else "little")
        elif dtype == "string" and width == -1:                 # S7 STRING[n]: like a CPU, a shorter string
            text = value.encode("latin-1")[:sfx]                # leaves the old characters after it
            mem[off] = sfx
            mem[off + 1] = len(text)
            mem[off + 2:off + 2 + len(text)] = text
        elif dtype == "string":                                 # raw string
            mem[off:off + sfx] = value.encode("latin-1").ljust(sfx, b"\x00")[:sfx]
        else:
            mem[off:off + width] = encode(width, dtype, value, big)

    def value(self, address: str, dtype: str, *, big: bool = True) -> Any:
        area, db, off, width, sfx = locate(address)
        mem = self.areas[(area, db)]
        if area == "T":
            word = struct.unpack_from(">H", mem, off * 2)[0]
            digits = int(f"{word & 0xFFF:03x}")
            return digits * (10, 100, 1000, 10_000)[(word >> 12) & 3]
        if area == "C":
            return int(f"{struct.unpack_from('>H', mem, off * 2)[0]:x}")
        if dtype == "bool" and width in (0, 1):
            return bool((mem[off] >> (sfx or 0)) & 1)
        if dtype == "bool":
            return bool((int.from_bytes(mem[off:off + width], "big" if big else "little") >> sfx) & 1)
        if dtype == "string" and width == -1:
            return bytes(mem[off + 2:off + 2 + min(mem[off + 1], sfx)]).decode("latin-1")
        if dtype == "string":
            return bytes(mem[off:off + sfx]).split(b"\x00", 1)[0].decode("latin-1")
        return decode(width, dtype, bytes(mem[off:off + width]), big)

    def fill_random(self, seed: int = 0) -> None:
        rng = random.Random(seed)
        for (name, _db), mem in self.areas.items():
            if name in ("T", "C"):
                for i in range(len(mem) // 2):
                    word = ms_to_s5time(rng.randrange(0, 999) * 10) if name == "T" else to_bcd(rng.randrange(1000))
                    struct.pack_into(">H", mem, 2 * i, word)
            else:
                mem[:] = bytes(rng.randrange(256) for _ in range(len(mem)))

    # --- power ---

    def _listen(self) -> None:
        server = _SimServer(self)
        for (name, db), data in self.areas.items():
            self._register(server, name, db, data)
        server.start(tcp_port=self.port)
        self.port = server.server_socket.getsockname()[1]
        self._server = server

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._sockets)

    async def stop(self) -> None:
        """Power off: reset every open connection, stop listening (new connects are refused)."""
        server, self._server = self._server, None
        if server is None:
            return
        with self._lock:
            socks = list(self._sockets)
        for s in socks:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # RST
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        await asyncio.to_thread(server.stop)

    async def restart(self) -> None:
        """Power on again on the same port, with the same memory."""
        if self._server is None:
            self._listen()


# --- synthetic devices (acceptance tests) -----------------------------------------------------------

def make_s7_tags(n: int, *, seed: int = 0, dbs: tuple[int, ...] = (1, 100)) -> tuple[list, dict[str, Any], dict[str, int]]:
    """`n` tags over the DBs, M, timers and counters, in a mix of Kepware and Siemens address
    forms, with random gaps (some above the 16-byte merge gap). Returns (tags, values by tag
    name, areas for `S7Sim.start`)."""
    from opencap.config.models import Tag

    rng = random.Random(seed)
    db_kinds = [("DB{db},REAL{o}", "float32", 4), ("DB{db},D{o:02d}", "float32", 4), ("DB{db},DBD{o}", "float32", 4),
                ("DB{db}.DBD{o}", "float32", 4), ("DB{db},DI{o}", "int32", 4), ("DB{db},W{o}", "uint16", 2),
                ("DB{db},I{o}", "int16", 2), ("DB{db},B{o}", "uint16", 1), ("DB{db},X{o}.{b}", "bool", 1),
                ("DB{db}.DBX{o}.{b}", "bool", 1)]
    m_kinds = [("MD{o}", "float32", 4), ("MW{o}", "int16", 2), ("MB{o}", "uint16", 1), ("M{o}.{b}", "bool", 1)]
    offsets = {**{f"DB{db}": 0 for db in dbs}, "M": 0}
    tc = {"T": 0, "C": 0}
    tags, values = [], {}
    for i in range(n):
        roll = rng.random()
        if roll < 0.08:
            area = rng.choice(("T", "C"))
            tc[area] += rng.choice((1, 1, 2, 12))
            address, dtype = f"{area}{tc[area]}", ("uint32" if area == "T" else "uint16")
            value: Any = (rng.randrange(1, 999) * rng.choice((10, 100, 1000)) if area == "T"
                          else rng.randrange(1000))
        else:
            in_m = roll < 0.30
            key = "M" if in_m else f"DB{rng.choice(dbs)}"
            pattern, dtype, width = rng.choice(m_kinds if in_m else db_kinds)
            offsets[key] += rng.choice((0, 0, 0, 2, 4, 20))           # 20: beyond the 16-byte gap
            off = offsets[key]
            address = pattern.format(db=key[2:], o=off, b=rng.randrange(8))
            offsets[key] += width
            value = {"float32": round(rng.uniform(-5000, 5000), 2), "int32": rng.randrange(-2**31, 2**31),
                     "uint16": rng.randrange(1 << (8 * width)), "int16": rng.randrange(-32768, 32768),
                     "bool": rng.random() < 0.5}[dtype]
        name = f"S7_{i:03d}_" + re.sub(r"\W", "_", address)
        tags.append(Tag(name=name, address=address, dtype=dtype))
        values[name] = value
    areas = {key: off + 8 for key, off in offsets.items()}
    areas.update({"T": tc["T"] + 1, "C": tc["C"] + 1})
    return tags, values, areas


def load(sim: S7Sim, tags: list, values: dict[str, Any], *, big: bool = True) -> None:
    for tag in tags:
        sim.set(tag.address, tag.dtype, values[tag.name], big=big)
