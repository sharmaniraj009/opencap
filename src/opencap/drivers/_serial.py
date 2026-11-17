"""Byte transports for serial PLCs (Modbus RTU, Allen-Bradley DF1).

A serial PLC is reached in one of three ways (`Device.params.transport`):

- ``tcp``      raw TCP to a serial-over-TCP gateway or virtual COM server (`host:port`): the
               bytes on the socket are the bytes on the serial line, nothing else (the gateway
               owns baud rate and parity). This is how the migrated KEPServer PC reaches its DF1
               PLCs (virtual COM ports mapped to router gateways).
- ``serial``   a local serial port; `Device.host` is the device path (`/dev/ttyUSB0`, `COM3`),
               opened with pyserial (optional extra `serial`) at `baudrate`/`bytesize`/`parity`/
               `stopbits`.
- ``rfc2217``  a Telnet COM-port-control server (RFC 2217) at `host:port`; pyserial negotiates
               the serial settings with it.
- ``unmapped`` the importer's default for a KEPServer COM port: the device cannot be reached
               until `overrides.yaml` `serial_ports` (or the device's own override) maps it.

`ByteTransport` is deliberately small: `open(timeout)`, `write(data)`, `read(timeout)` (at least
one byte, or TimeoutError), `discard()`, `close()`. A closed or failed link raises
`TransportClosed`, which is a `ConnectionLost`, so drivers can let it through to the
ChannelWorker unchanged.

Several devices on one physical line (stations on a multi-drop line, or several KEPServer
devices on one COM port) must share one connection: a local serial port can be opened once,
and many gateways accept a single TCP client. `LinkPool` hands every driver that uses the same
endpoint the same open connection (`acquire`), lets an error on the line drop it for all of
them (`Lease.drop`) and closes it when the last driver leaves (`Lease.release`). The
ChannelWorker already allows one request on the wire per channel; `SharedLink.io_lock` also
serialises requests from different channels that (wrongly) point at the same line.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from opencap.config.models import Device
from opencap.drivers.base import ConnectionLost

_log = structlog.get_logger("opencap.drivers.serial")

TRANSPORTS = ("tcp", "serial", "rfc2217", "unmapped")
NETWORK_KINDS = frozenset({"tcp", "rfc2217"})
PARITY_NAMES = {"N": "N", "NONE": "N", "E": "E", "EVEN": "E", "O": "O", "ODD": "O",
                "M": "M", "MARK": "M", "S": "S", "SPACE": "S"}
BYTESIZES = (5, 6, 7, 8)
STOPBITS = (1, 1.5, 2)
POLL_INTERVAL_S = 0.002          # read polling where the OS cannot wake us (Windows, RFC 2217)
READ_CHUNK = 4096


class TransportClosed(ConnectionLost):
    """The link is closed, could not be opened, or the other end went away."""


class TransportUnmapped(ValueError):
    """The device's serial port is not mapped to a transport yet (`transport: unmapped`)."""


# --- settings and endpoints ---------------------------------------------------------------------

def _int_param(params: Mapping[str, Any], key: str, default: int) -> int:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"serial param {key}={value!r}: expected an integer")
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"serial param {key}={value!r}: expected an integer") from None


@dataclass(frozen=True)
class SerialSettings:
    """Line settings. Used to open a local or RFC 2217 port and to compute character times."""

    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"            # N, E, O, M, S
    stopbits: float = 1

    def __post_init__(self) -> None:
        if not 50 <= self.baudrate <= 4_000_000:
            raise ValueError(f"serial param baudrate={self.baudrate}: must be 50..4000000")
        if self.bytesize not in BYTESIZES:
            raise ValueError(f"serial param bytesize={self.bytesize}: must be one of {BYTESIZES}")
        if self.parity not in PARITY_NAMES.values():
            raise ValueError(f"serial param parity={self.parity!r}: must be N, E, O, M or S")
        if self.stopbits not in STOPBITS:
            raise ValueError(f"serial param stopbits={self.stopbits}: must be 1, 1.5 or 2")

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None) -> SerialSettings:
        params = params or {}
        parity_raw = params.get("parity")
        parity = "N"
        if parity_raw is not None:
            parity = PARITY_NAMES.get(str(parity_raw).strip().upper(), "")
            if not parity:
                raise ValueError(f"serial param parity={parity_raw!r}: must be none, even, odd, mark or space")
        stop_raw = params.get("stopbits")
        if stop_raw is None:
            stopbits: float = 1
        else:
            try:
                stopbits = float(stop_raw)
            except (TypeError, ValueError):
                raise ValueError(f"serial param stopbits={stop_raw!r}: must be 1, 1.5 or 2") from None
            if isinstance(stop_raw, bool) or stopbits not in STOPBITS:
                raise ValueError(f"serial param stopbits={stop_raw!r}: must be 1, 1.5 or 2")
            stopbits = int(stopbits) if stopbits.is_integer() else stopbits
        return cls(
            baudrate=_int_param(params, "baudrate", 19200),
            bytesize=_int_param(params, "bytesize", 8),
            parity=parity,
            stopbits=stopbits,
        )

    def char_time_s(self) -> float:
        """Seconds per character on the wire: start bit + data bits + parity + stop bits."""
        bits = 1 + self.bytesize + (0 if self.parity == "N" else 1) + self.stopbits
        return bits / self.baudrate

    def describe(self) -> str:
        stop = f"{self.stopbits:g}"
        return f"{self.baudrate} {self.bytesize}{self.parity}{stop}"

    def pyserial_kwargs(self) -> dict[str, Any]:
        return {"baudrate": self.baudrate, "bytesize": self.bytesize, "parity": self.parity,
                "stopbits": self.stopbits}


@dataclass(frozen=True)
class Endpoint:
    """Where a serial line is reached. `host` is the serial device path for kind `serial`."""

    kind: str                    # tcp | serial | rfc2217
    host: str
    port: int
    settings: SerialSettings = field(default_factory=SerialSettings)

    @property
    def key(self) -> tuple[str, str, int]:
        """Identity of the physical line (settings are not part of it: one line, one setting)."""
        host = self.host if self.kind == "serial" else self.host.lower()
        return (self.kind, host, self.port)

    def url(self) -> str:
        if self.kind == "rfc2217":
            return f"rfc2217://{self.host}:{self.port}"
        if self.kind == "tcp":
            return f"socket://{self.host}:{self.port}"
        return self.host

    def describe(self) -> str:
        if self.kind == "serial":
            return f"serial {self.host} {self.settings.describe()}"
        return f"{self.kind} {self.host}:{self.port}"


_SCHEME_RE = re.compile(r"^(?P<scheme>[a-z0-9]+)://(?P<rest>.*)$", re.IGNORECASE)


def _host_port(text: str, original: str) -> tuple[str, int]:
    host, sep, port_text = text.rpartition(":")
    if not sep:
        raise ValueError(f"serial port mapping {original!r}: a network address needs a port (host:port)")
    host = host.strip().strip("[]")
    if not host:
        raise ValueError(f"serial port mapping {original!r}: empty host")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"serial port mapping {original!r}: port {port_text!r} is not a number") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"serial port mapping {original!r}: port {port} out of range 1..65535")
    return host, port


def parse_port_mapping(text: str) -> tuple[str, str, int]:
    """`overrides.yaml` `serial_ports` value -> (transport, host or device path, port).

    "192.0.2.50:4001" / "tcp://192.0.2.50:4001" -> tcp; "rfc2217://host:port" -> rfc2217;
    "/dev/ttyUSB0", "COM3", "serial:///dev/ttyS1" -> serial (port 0). Raises ValueError.
    """
    original = text
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        raise ValueError(f"serial port mapping {original!r}: empty")
    m = _SCHEME_RE.match(text)
    if m:
        scheme, rest = m["scheme"].lower(), m["rest"].strip()
        if scheme in ("tcp", "rfc2217"):
            host, port = _host_port(rest, original)
            return scheme, host, port
        if scheme == "serial":
            if not rest:
                raise ValueError(f"serial port mapping {original!r}: empty device path")
            return "serial", rest, 0
        raise ValueError(f"serial port mapping {original!r}: unknown scheme {scheme!r} "
                         "(use tcp://, rfc2217://, serial:// or a bare host:port / device path)")
    if text.startswith("/") or re.fullmatch(r"(?i)COM\d+", text) or text.startswith("\\\\.\\"):
        return "serial", text, 0
    host, port = _host_port(text, original)
    return "tcp", host, port


def endpoint_for(device: Device, *, allowed: tuple[str, ...] = ("tcp", "serial", "rfc2217")) -> Endpoint:
    """The endpoint of a serial-line device. Raises TransportUnmapped or ValueError."""
    params = device.params or {}
    kind = str(params.get("transport") or "tcp").strip().lower()
    if kind not in TRANSPORTS:
        raise ValueError(f"{device.id}: param transport={params.get('transport')!r}: must be one of "
                         f"{', '.join(TRANSPORTS)}")
    if kind == "unmapped":
        com = params.get("com_port") or device.host or "the serial port"
        raise TransportUnmapped(
            f"{device.id}: {com} is not mapped to a transport; map it in overrides.yaml serial_ports "
            f"(e.g. {com}: 192.0.2.50:4001 for a serial-over-TCP gateway, or {com}: /dev/ttyUSB0)")
    if kind not in allowed:
        raise ValueError(f"{device.id}: transport {kind} is not supported by {device.driver} "
                         f"(supported: {', '.join(allowed)})")
    settings = SerialSettings.from_params(params)
    host = (device.host or "").strip()
    if kind == "serial":
        if not host:
            raise ValueError(f"{device.id}: transport serial needs the device path in host (e.g. /dev/ttyUSB0)")
        return Endpoint("serial", host, 0, settings)
    if not host:
        raise ValueError(f"{device.id}: transport {kind} needs a host")
    if not 1 <= int(device.port or 0) <= 65535:
        raise ValueError(f"{device.id}: transport {kind} needs a port 1..65535, got {device.port} "
                         f"(host {host!r}); map the COM port in overrides.yaml serial_ports")
    return Endpoint(kind, host, int(device.port), settings)


def make_transport(endpoint: Endpoint) -> ByteTransport:
    if endpoint.kind == "tcp":
        return TcpTransport(endpoint.host, endpoint.port)
    if endpoint.kind == "serial":
        return SerialTransport(endpoint.host, endpoint.settings)
    if endpoint.kind == "rfc2217":
        return Rfc2217Transport(endpoint.host, endpoint.port, endpoint.settings)
    raise ValueError(f"no transport for {endpoint.kind!r}")


# --- transports ---------------------------------------------------------------------------------

class ByteTransport:
    """A bidirectional byte stream with a receive buffer. Subclasses feed `_feed()` / `_lost()`."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._wake = asyncio.Event()
        self._closed: TransportClosed | None = TransportClosed(f"{self.describe()}: not open")
        self.bytes_in = 0
        self.bytes_out = 0

    def describe(self) -> str:   # pragma: no cover - overridden
        return type(self).__name__

    @property
    def is_open(self) -> bool:
        return self._closed is None

    # receive side, called by subclasses
    def _feed(self, data: bytes) -> None:
        if data:
            self._buf += data
            self.bytes_in += len(data)
            self._wake.set()

    def _lost(self, reason: str) -> None:
        if self._closed is None:
            self._closed = TransportClosed(f"{self.describe()}: {reason}")
            _log.debug("transport closed", transport=self.describe(), reason=reason)
        self._wake.set()

    def _opened(self) -> None:
        self._buf.clear()
        self._closed = None

    async def open(self, timeout: float) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        if self._closed is not None:
            raise self._closed
        self._write(bytes(data))
        self.bytes_out += len(data)

    def _write(self, data: bytes) -> None:
        raise NotImplementedError

    async def read(self, timeout: float) -> bytes:
        """Every byte received so far (at least one). Raises TimeoutError after `timeout` s
        with nothing received, TransportClosed when the link is (or goes) down and the buffer
        is empty: bytes that arrived before a hang-up are still delivered first."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        while not self._buf:
            if self._closed is not None:
                raise self._closed
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"{self.describe()}: nothing received within {timeout:.3f} s")
            self._wake.clear()
            try:
                async with asyncio.timeout(remaining):
                    await self._wake.wait()
            except TimeoutError:
                pass
        data = bytes(self._buf)
        self._buf.clear()
        return data

    def discard(self) -> int:
        """Drop everything received but not read yet; returns the byte count."""
        n = len(self._buf)
        self._buf.clear()
        return n


class _TcpProtocol(asyncio.Protocol):
    def __init__(self, owner: TcpTransport) -> None:
        self.owner = owner

    def data_received(self, data: bytes) -> None:
        self.owner._feed(data)

    def eof_received(self) -> bool:
        self.owner._lost("closed by the peer")
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        self.owner._lost(f"connection lost: {exc}" if exc else "closed by the peer")


class TcpTransport(ByteTransport):
    """Raw TCP: a serial-over-TCP gateway (TCP server mode) or virtual COM server."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self._transport: asyncio.Transport | None = None
        super().__init__()

    def describe(self) -> str:
        return f"tcp {self.host}:{self.port}"

    async def open(self, timeout: float) -> None:
        await self.close()
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                transport, _ = await loop.create_connection(lambda: _TcpProtocol(self), self.host, self.port)
        except TimeoutError:
            raise TransportClosed(f"{self.describe()}: connect: no answer within {timeout:.3f} s") from None
        except OSError as exc:
            reason = "refused" if isinstance(exc, ConnectionRefusedError) else (exc.strerror or str(exc))
            raise TransportClosed(f"{self.describe()}: connect {reason}") from exc
        sock = transport.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                import socket
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._transport = transport          # type: ignore[assignment]
        self._opened()

    def _write(self, data: bytes) -> None:
        assert self._transport is not None
        self._transport.write(data)

    async def close(self) -> None:
        transport, self._transport = self._transport, None
        self._lost("closed")
        if transport is not None:
            transport.abort()                # no lingering: the line is either usable or not


class SerialTransport(ByteTransport):
    """A local serial port through pyserial (`pip install opencap[serial]`).

    POSIX: the file descriptor is watched by the event loop. Elsewhere (or if the loop cannot
    watch it) the port is polled every few milliseconds."""

    def __init__(self, path: str, settings: SerialSettings) -> None:
        self.path, self.settings = path, settings
        self._serial: Any = None
        self._fd: int | None = None
        self._poll_task: asyncio.Task | None = None
        super().__init__()

    def describe(self) -> str:
        return f"serial {self.path}"

    def _open_sync(self) -> Any:
        try:
            import serial
        except ImportError as exc:    # pragma: no cover - the extra is installed in tests
            raise TransportClosed(f"{self.describe()}: pyserial is not installed "
                                  "(pip install 'opencap[serial]')") from exc
        try:
            ser = serial.serial_for_url(self.path, timeout=0, write_timeout=None, exclusive=True,
                                        **self.settings.pyserial_kwargs())
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportClosed(f"{self.describe()}: open failed: {exc}") from exc
        with contextlib.suppress(Exception):
            ser.reset_input_buffer()
        return ser

    async def open(self, timeout: float) -> None:
        await self.close()
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                ser = await loop.run_in_executor(None, self._open_sync)
        except TimeoutError:
            raise TransportClosed(f"{self.describe()}: open: no answer within {timeout:.3f} s") from None
        self._serial = ser
        self._opened()
        fd = None
        with contextlib.suppress(Exception):
            fd = ser.fileno()
        if fd is not None and os.name == "posix":
            try:
                loop.add_reader(fd, self._readable)
                self._fd = fd
                return
            except (NotImplementedError, ValueError, OSError):
                pass
        self._poll_task = asyncio.create_task(self._poll(), name=f"serial-poll {self.path}")

    def _readable(self) -> None:
        ser = self._serial
        if ser is None:
            return
        try:
            data = os.read(self._fd, READ_CHUNK) if self._fd is not None else ser.read(READ_CHUNK)
        except BlockingIOError:
            return
        except OSError as exc:            # EIO: the other end of a pty, or the adapter, went away
            self._detach()
            self._lost(f"read failed: {exc}")
            return
        if not data:
            self._detach()
            self._lost("hang-up (end of file)")
            return
        self._feed(data)

    async def _poll(self) -> None:
        while self._serial is not None:
            try:
                waiting = self._serial.in_waiting
                data = self._serial.read(waiting or 1) if waiting else b""
            except Exception as exc:
                self._lost(f"read failed: {exc}")
                return
            if data:
                self._feed(data)
            else:
                await asyncio.sleep(POLL_INTERVAL_S)

    def _detach(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._fd)
            self._fd = None

    def _write(self, data: bytes) -> None:
        try:
            if self._fd is not None:
                view = memoryview(data)
                deadline = time.monotonic() + 1.0
                while view:                  # a tty takes a frame at once; loop only on a full buffer
                    try:
                        view = view[os.write(self._fd, view):]
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise OSError("output buffer stays full") from None
                        time.sleep(0.001)
            else:
                self._serial.write(data)
        except Exception as exc:
            self._lost(f"write failed: {exc}")
            raise self._closed from exc     # type: ignore[misc]

    async def close(self) -> None:
        ser, self._serial = self._serial, None
        self._detach()
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._lost("closed")
        if ser is not None:
            with contextlib.suppress(Exception):
                ser.close()


class Rfc2217Transport(SerialTransport):
    """A Telnet COM Port Control server (RFC 2217) via pyserial's `rfc2217://` client. Opening
    negotiates the line settings, so it can take a few round trips; reads are polled."""

    def __init__(self, host: str, port: int, settings: SerialSettings) -> None:
        self.host, self.port = host, port
        super().__init__(f"rfc2217://{host}:{port}", settings)

    def describe(self) -> str:
        return f"rfc2217 {self.host}:{self.port}"

    def _open_sync(self) -> Any:
        try:
            import serial
        except ImportError as exc:    # pragma: no cover
            raise TransportClosed(f"{self.describe()}: pyserial is not installed "
                                  "(pip install 'opencap[serial]')") from exc
        try:
            ser = serial.serial_for_url(self.path, timeout=0, **self.settings.pyserial_kwargs())
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportClosed(f"{self.describe()}: open failed: {exc}") from exc
        return ser

    async def open(self, timeout: float) -> None:
        await self.close()
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                ser = await loop.run_in_executor(None, self._open_sync)
        except TimeoutError:
            raise TransportClosed(f"{self.describe()}: open: no answer within {timeout:.3f} s") from None
        self._serial = ser
        self._opened()
        self._poll_task = asyncio.create_task(self._poll(), name=f"rfc2217-poll {self.host}:{self.port}")

    def _write(self, data: bytes) -> None:
        try:
            self._serial.write(data)
        except Exception as exc:
            self._lost(f"write failed: {exc}")
            raise self._closed from exc     # type: ignore[misc]


# --- shared links -------------------------------------------------------------------------------

Opener = Callable[[], Awaitable[Any]]
Closer = Callable[[Any], Awaitable[None]]


@dataclass(eq=False)
class SharedLink:
    """One physical line and its open connection, shared by every driver that uses it."""

    key: Hashable
    closer: Closer
    conn: Any = None                        # the open transport / library client, None when closed
    generation: int = 0                     # bumped on every open: a lease is alive for one generation
    users: set[Hashable] = field(default_factory=set)
    open_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: dict[str, Any] = field(default_factory=dict)     # protocol state that belongs to the line

    async def close_conn(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                await self.closer(conn)
            except Exception as exc:     # pragma: no cover - closers do not raise
                _log.debug("shared link close error", link=str(self.key), error=repr(exc))


@dataclass(eq=False)
class Lease:
    """A driver's handle on a shared link, valid for the generation it was opened in."""

    link: SharedLink
    owner: Hashable
    generation: int

    @property
    def alive(self) -> bool:
        return self.link.conn is not None and self.link.generation == self.generation

    @property
    def conn(self) -> Any:
        return self.link.conn if self.alive else None

    async def drop(self) -> None:
        """The line failed: close it for every user (they reconnect on their next request)."""
        if self.alive:
            await self.link.close_conn()

    async def release(self) -> None:
        """This driver leaves the line; the last one out closes it."""
        self.link.users.discard(self.owner)
        if not self.link.users:
            await self.link.close_conn()


class LinkPool:
    """Shared links by key. One pool per event loop (`link_pool()`)."""

    def __init__(self) -> None:
        self._links: dict[Hashable, SharedLink] = {}

    def users(self, key: Hashable) -> set[Hashable]:
        link = self._links.get(key)
        return set(link.users) if link is not None else set()

    async def acquire(self, key: Hashable, owner: Hashable, opener: Opener, closer: Closer) -> Lease:
        """The open link for `key`, opening it with `opener()` when nobody has it open. Raises
        whatever `opener` raises (nothing is registered then)."""
        link = self._links.get(key)
        if link is None:
            link = self._links[key] = SharedLink(key, closer)
        async with link.open_lock:
            if link.conn is None:
                conn = await opener()
                link.conn = conn
                link.generation += 1
            link.users.add(owner)
            return Lease(link, owner, link.generation)


_POOLS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, LinkPool] = weakref.WeakKeyDictionary()


def link_pool() -> LinkPool:
    """The pool of the running event loop."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop)
    if pool is None:
        pool = _POOLS[loop] = LinkPool()
    return pool
