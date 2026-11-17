"""Byte transports for serial PLCs (`opencap.drivers._serial`): endpoint and mapping parsing,
serial settings, raw TCP (serial-over-TCP gateways), a local serial port (a pty here), RFC 2217,
and the shared per-endpoint link that lets several stations on one line use one connection."""

from __future__ import annotations

import asyncio
import os
import pty
import time
import tty

import pytest

from opencap.config.models import Device
from opencap.drivers._serial import (
    Endpoint,
    LinkPool,
    Rfc2217Transport,
    SerialSettings,
    SerialTransport,
    TcpTransport,
    TransportClosed,
    TransportUnmapped,
    endpoint_for,
    make_transport,
    parse_port_mapping,
)
from opencap.drivers.base import ConnectionLost
from tests.helpers.modbus_sim import free_port
from tests.helpers.pty_bridge import needs_pyserial


def _device(**kw) -> Device:
    base = dict(id="SITE/PLC", channel="SITE", name="PLC", driver="ab_df1", host="127.0.0.1", port=4001,
                params={}, tags=[])
    base.update(kw)
    return Device(**base)


# --- mapping strings (overrides.yaml serial_ports) -------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    ("192.0.2.50:4001", ("tcp", "192.0.2.50", 4001)),
    ("tcp://192.0.2.50:4001", ("tcp", "192.0.2.50", 4001)),
    (" TCP://gw-1.example:20108 ", ("tcp", "gw-1.example", 20108)),
    ("rfc2217://192.0.2.51:4002", ("rfc2217", "192.0.2.51", 4002)),
    ("/dev/ttyUSB0", ("serial", "/dev/ttyUSB0", 0)),
    ("serial:///dev/ttyS1", ("serial", "/dev/ttyS1", 0)),
    ("serial://COM3", ("serial", "COM3", 0)),
    ("COM3", ("serial", "COM3", 0)),
])
def test_parse_port_mapping(text, expected):
    assert parse_port_mapping(text) == expected


@pytest.mark.parametrize(("text", "why"), [
    ("", "empty"),
    ("192.0.2.50", "needs a port"),
    ("192.0.2.50:0", "port"),
    ("192.0.2.50:70000", "port"),
    ("192.0.2.50:abc", "port"),
    ("udp://192.0.2.50:4001", "scheme"),
    ("tcp://:4001", "host"),
])
def test_parse_port_mapping_rejects(text, why):
    with pytest.raises(ValueError, match=why):
        parse_port_mapping(text)


# --- serial settings --------------------------------------------------------------------------------

def test_serial_settings_defaults_and_char_time():
    s = SerialSettings.from_params({})
    assert (s.baudrate, s.bytesize, s.parity, s.stopbits) == (19200, 8, "N", 1)
    assert s.char_time_s() == pytest.approx(10 / 19200)          # start + 8 data + 1 stop
    e = SerialSettings.from_params({"baudrate": 9600, "parity": "even", "stopbits": 2, "bytesize": 7})
    assert (e.baudrate, e.bytesize, e.parity, e.stopbits) == (9600, 7, "E", 2)
    assert e.char_time_s() == pytest.approx(11 / 9600)           # start + 7 + parity + 2 stop
    assert SerialSettings.from_params({"parity": "None"}).parity == "N"
    assert SerialSettings.from_params({"parity": "o", "stopbits": 1.5}).stopbits == 1.5
    assert SerialSettings.from_params({"baudrate": None}).baudrate == 19200    # None = default
    assert "9600 7E2" in e.describe()


@pytest.mark.parametrize(("params", "why"), [
    ({"baudrate": 0}, "baudrate"),
    ({"baudrate": "fast"}, "baudrate"),
    ({"baudrate": True}, "baudrate"),
    ({"bytesize": 9}, "bytesize"),
    ({"parity": "X"}, "parity"),
    ({"stopbits": 3}, "stopbits"),
])
def test_serial_settings_rejects(params, why):
    with pytest.raises(ValueError, match=why):
        SerialSettings.from_params(params)


# --- endpoints ---------------------------------------------------------------------------------------

def test_endpoint_tcp_is_the_default():
    ep = endpoint_for(_device(params={"baudrate": 9600}))
    assert ep == Endpoint("tcp", "127.0.0.1", 4001, SerialSettings(baudrate=9600))
    assert ep.describe() == "tcp 127.0.0.1:4001"


def test_endpoint_serial_uses_host_as_device_path():
    ep = endpoint_for(_device(host="/dev/ttyUSB0", port=0, params={"transport": "serial", "baudrate": 9600}))
    assert (ep.kind, ep.host, ep.port) == ("serial", "/dev/ttyUSB0", 0)
    assert ep.describe() == "serial /dev/ttyUSB0 9600 8N1"


def test_endpoint_rfc2217():
    ep = endpoint_for(_device(params={"transport": "rfc2217"}))
    assert ep.kind == "rfc2217" and ep.url() == "rfc2217://127.0.0.1:4001"


def test_endpoint_unmapped_names_the_com_port():
    dev = _device(host="COM112", port=0, params={"transport": "unmapped", "com_port": "COM112"})
    with pytest.raises(TransportUnmapped, match=r"COM112.*serial_ports"):
        endpoint_for(dev)


@pytest.mark.parametrize(("kw", "why"), [
    ({"host": "", "params": {"transport": "tcp"}}, "host"),
    ({"port": 0, "params": {"transport": "tcp"}}, "port"),
    ({"host": "COM112", "port": 0, "params": {"transport": "tcp"}}, "port"),
    ({"host": "", "port": 0, "params": {"transport": "serial"}}, "device path"),
    ({"params": {"transport": "carrier-pigeon"}}, "transport"),
])
def test_endpoint_rejects(kw, why):
    with pytest.raises(ValueError, match=why):
        endpoint_for(_device(**kw))


def test_endpoint_respects_allowed_kinds():
    with pytest.raises(ValueError, match="rfc2217.*not supported"):
        endpoint_for(_device(params={"transport": "rfc2217"}), allowed=("tcp", "serial"))


def test_same_line_same_key():
    a = endpoint_for(_device(host="GW.example", params={"baudrate": 9600}))
    b = endpoint_for(_device(host="gw.example", params={"baudrate": 19200}))
    assert a.key == b.key            # one physical line, whatever the per-device settings say


def test_make_transport_kinds():
    s = SerialSettings()
    assert isinstance(make_transport(Endpoint("tcp", "127.0.0.1", 1, s)), TcpTransport)
    assert isinstance(make_transport(Endpoint("serial", "/dev/null", 0, s)), SerialTransport)
    assert isinstance(make_transport(Endpoint("rfc2217", "127.0.0.1", 1, s)), Rfc2217Transport)


# --- raw TCP ------------------------------------------------------------------------------------------

class _Peer:
    """A TCP server that records what it receives and lets the test write back or hang up."""

    def __init__(self) -> None:
        self.received = bytearray()
        self.writer: asyncio.StreamWriter | None = None
        self.connected = asyncio.Event()
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> _Peer:
        async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self.writer = writer
            self.connected.set()
            while data := await reader.read(4096):
                self.received += data
        self.server = await asyncio.start_server(on_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.writer is not None:
            self.writer.close()
        assert self.server is not None
        self.server.close()


async def test_tcp_transport_round_trip_timeout_and_peer_close():
    peer = await _Peer().start()
    t = TcpTransport("127.0.0.1", peer.port)
    try:
        await t.open(timeout=2)
        assert t.is_open
        await asyncio.wait_for(peer.connected.wait(), 2)
        t.write(b"\x10\x02abc")
        await asyncio.sleep(0.05)
        assert bytes(peer.received) == b"\x10\x02abc"
        peer.writer.write(b"\x10\x06")
        assert await t.read(1.0) == b"\x10\x06"
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await t.read(0.2)
        assert 0.15 < time.monotonic() - started < 1.0
        peer.writer.write(b"last words")          # data sent just before the hang-up is delivered first
        await peer.writer.drain()
        peer.writer.close()
        got = b""
        with pytest.raises(TransportClosed):
            while True:
                got += await t.read(1.0)
        assert got == b"last words"
        assert not t.is_open
        assert issubclass(TransportClosed, ConnectionLost)
        with pytest.raises(TransportClosed):
            t.write(b"x")
    finally:
        await t.close()
        await t.close()                            # idempotent
        await peer.stop()


async def test_tcp_transport_discard():
    peer = await _Peer().start()
    t = TcpTransport("127.0.0.1", peer.port)
    try:
        await t.open(timeout=2)
        await asyncio.wait_for(peer.connected.wait(), 2)
        peer.writer.write(b"stale bytes")
        await asyncio.sleep(0.05)
        assert t.discard() == len(b"stale bytes")
        with pytest.raises(TimeoutError):
            await t.read(0.05)
    finally:
        await t.close()
        await peer.stop()


async def test_tcp_transport_refused_is_connection_lost():
    t = TcpTransport("127.0.0.1", free_port())
    with pytest.raises(ConnectionLost, match="refused|connect"):
        await t.open(timeout=2)
    assert not t.is_open


async def test_tcp_transport_open_is_bounded_by_its_timeout():
    t = TcpTransport("192.0.2.1", 9)               # TEST-NET-1: never answers (or is unreachable)
    started = time.monotonic()
    with pytest.raises(ConnectionLost):
        await t.open(timeout=0.3)
    assert time.monotonic() - started < 2.0


async def test_read_when_not_open():
    with pytest.raises(TransportClosed):
        await TcpTransport("127.0.0.1", 1).read(0.1)


# --- local serial port (pty) -------------------------------------------------------------------------

@pytest.fixture()
def pty_pair():
    master, slave = pty.openpty()
    tty.setraw(slave)
    path = os.ttyname(slave)
    os.set_blocking(master, False)
    yield master, path
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


async def _read_master(master: int, n: int, timeout: float = 2.0) -> bytes:
    got = b""
    deadline = time.monotonic() + timeout
    while len(got) < n and time.monotonic() < deadline:
        try:
            got += os.read(master, 4096)
        except BlockingIOError:
            await asyncio.sleep(0.005)
    return got


@needs_pyserial
async def test_serial_transport_over_a_pty(pty_pair):
    master, path = pty_pair
    t = SerialTransport(path, SerialSettings(baudrate=9600))     # a pty takes parity only once
    try:
        await t.open(timeout=2)
        assert t.is_open
        t.write(b"\x10\x02\x10\x10\x10\x03")
        assert await _read_master(master, 6) == b"\x10\x02\x10\x10\x10\x03"
        os.write(master, b"\x10\x06reply")
        got = b""
        while len(got) < 7:
            got += await t.read(1.0)
        assert got == b"\x10\x06reply"
        with pytest.raises(TimeoutError):
            await t.read(0.1)
    finally:
        await t.close()
    assert not t.is_open


async def test_serial_transport_missing_device_is_connection_lost(tmp_path):
    t = SerialTransport(str(tmp_path / "no-such-tty"), SerialSettings())
    with pytest.raises(ConnectionLost, match="no-such-tty"):
        await t.open(timeout=2)


@needs_pyserial
async def test_serial_transport_hangup_is_transport_closed(pty_pair):
    master, path = pty_pair
    t = SerialTransport(path, SerialSettings())
    try:
        await t.open(timeout=2)
        os.close(master)                           # the other end goes away (USB adapter unplugged)
        with pytest.raises(TransportClosed):
            for _ in range(20):
                await t.read(0.5)
    finally:
        await t.close()


# --- RFC 2217 (pyserial's client against pyserial's own port manager) --------------------------------

@needs_pyserial
async def test_rfc2217_transport_round_trip(pty_pair):
    from tests.helpers.pty_bridge import Rfc2217Server

    master, path = pty_pair
    server = await Rfc2217Server.start(path)
    t = Rfc2217Transport("127.0.0.1", server.port, SerialSettings(baudrate=9600))
    try:
        await t.open(timeout=5)
        t.write(b"\x10\x05")
        assert await _read_master(master, 2) == b"\x10\x05"
        os.write(master, b"\x10\x06")
        got = b""
        while len(got) < 2:
            got += await t.read(2.0)
        assert got == b"\x10\x06"
        assert server.settings_seen().get("baudrate") == 9600
    finally:
        await t.close()
        await server.stop()


# --- shared links -------------------------------------------------------------------------------------

class _Conn:
    def __init__(self, n: int) -> None:
        self.n = n
        self.closed = False


async def test_shared_link_one_connection_per_endpoint():
    pool = LinkPool()
    opened: list[_Conn] = []

    async def opener() -> _Conn:
        opened.append(_Conn(len(opened)))
        return opened[-1]

    async def closer(conn: _Conn) -> None:
        conn.closed = True

    ep = Endpoint("tcp", "127.0.0.1", 4001, SerialSettings())
    a = await pool.acquire(("df1", ep.key), "A", opener, closer)
    b = await pool.acquire(("df1", ep.key), "B", opener, closer)
    assert len(opened) == 1 and a.conn is b.conn and a.alive and b.alive
    assert a.link is b.link

    await a.drop()                                 # an error on the line: both lose it
    assert opened[0].closed and not a.alive and not b.alive
    c = await pool.acquire(("df1", ep.key), "A", opener, closer)
    assert len(opened) == 2 and c.alive and not b.alive
    b2 = await pool.acquire(("df1", ep.key), "B", opener, closer)
    assert b2.conn is c.conn

    await c.release()                              # one user leaves: the line stays open
    assert not opened[1].closed and b2.alive
    await b2.release()                             # the last user leaves: closed
    assert opened[1].closed
    other = await pool.acquire(("df1", ("tcp", "127.0.0.1", 4002)), "A", opener, closer)
    assert other.conn is opened[2]


async def test_shared_link_failed_open_leaves_nothing_behind():
    pool = LinkPool()

    async def opener():
        raise ConnectionLost("refused")

    async def closer(conn):
        raise AssertionError("nothing to close")

    with pytest.raises(ConnectionLost):
        await pool.acquire(("df1", "k"), "A", opener, closer)
    assert pool.users(("df1", "k")) == set()
