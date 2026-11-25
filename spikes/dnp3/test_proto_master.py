"""The throwaway DNP3 master prototype against two independent outstations (spike #14).

    pip install yadnp3==3.2.1.5 dnp3py==0.6.0 pytest pytest-asyncio
    pytest spikes/dnp3 -q

- yadnp3 3.2.1.5 (Apache-2.0): Python bindings of an opendnp3 fork, the reference C++ stack
  (opendnp3 3.1.2 passed DNP3 Subset Level 2 conformance; upstream archived 2022).
- dnp3py 0.6.0 (MIT): an unrelated pure-Python master/outstation.

Not collected by CI (`testpaths = ["tests"]`): a spike, not product code. Each peer's tests skip
when that peer is not installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from datetime import UTC, datetime

import pytest

from proto_master import (
    COMM_LOST,
    FC_ALLOWED,
    INTEGRITY_POLL,
    ONLINE,
    Master,
    apdu_request,
    crc_dnp,
    link_frame,
    quality_of,
    transport_segments,
)

T0 = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
T0_MS = int(T0.timestamp() * 1000)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def connected(port: int, **kw) -> Master:
    deadline = time.monotonic() + 5
    while True:
        try:
            return await Master("127.0.0.1", port, **kw).__aenter__()
        except OSError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.05)


# --- pure framing (no peer) ---------------------------------------------------------------------------

def test_crc_check_value_and_frames() -> None:
    assert crc_dnp(b"123456789") == 0xEA82                       # CRC-16/DNP check value
    # READ class 1,2,3,0 as one APDU: AC (FIR|FIN|seq 0), FC 01, four g60 headers with qualifier 06
    apdu = apdu_request(0x01, 0, INTEGRITY_POLL)
    assert apdu == bytes.fromhex("C0 01 3C02 06 3C03 06 3C04 06 3C01 06")
    seg = transport_segments(apdu, 5)
    assert seg == [bytes((0xC5,)) + apdu]                        # FIR|FIN|seq 5
    frame = link_frame(seg[0], dest=1024, src=1, control=0xC4)
    assert frame[:8] == bytes.fromhex("0564 14 C4 0004 0100")    # length 5 + 15 user bytes
    assert len(frame) == 10 + 15 + 2                             # one data block of 15 bytes + CRC


def test_read_only_function_codes() -> None:
    assert FC_ALLOWED == {0x00, 0x01}
    for fc in (0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0D, 0x0E, 0x14, 0x16):   # write, select, operate, ...
        with pytest.raises(ValueError):
            apdu_request(fc, 0)


def test_flag_mapping() -> None:
    assert quality_of(ONLINE) == "good"
    assert quality_of(ONLINE | 0x08) == "good"                   # remote forced: still the value
    assert quality_of(ONLINE | COMM_LOST) == "bad_comm"
    assert quality_of(0x00) == "stale"                           # offline
    assert quality_of(0x02) == "stale"                           # RESTART: never updated
    assert quality_of(ONLINE | 0x20) == "bad_decode"             # analog OVER_RANGE
    assert quality_of(ONLINE | 0x20, "binary") == "good"         # binary CHATTER_FILTER


# --- opendnp3 (yadnp3) outstation ---------------------------------------------------------------------

try:
    import opendnp3 as o
except ImportError:                                              # pragma: no cover
    o = None
needs_opendnp3 = pytest.mark.skipif(o is None, reason="yadnp3 not installed")


if o is not None:
    class _App(o.IOutstationApplication):
        def __init__(self) -> None:
            super().__init__()

        def SupportsWriteAbsoluteTime(self): return False
        def WriteAbsoluteTime(self, t): return False
        def SupportsAssignClass(self): return False
        def GetApplicationIIN(self): return o.ApplicationIIN()
        def ColdRestartSupport(self): return o.RestartMode.UNSUPPORTED
        def WarmRestartSupport(self): return o.RestartMode.UNSUPPORTED
        def ColdRestart(self): return 0
        def WarmRestart(self): return 0
        def Now(self): return o.DNPTime(int(time.time() * 1000))

    class _Commands(o.ICommandHandler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.lock = threading.Lock()

        def Begin(self): pass
        def End(self): pass

        def Select(self, c, i):
            with self.lock:
                self.calls += 1
            return o.CommandStatus.NOT_SUPPORTED

        def Operate(self, c, i, h, t):
            with self.lock:
                self.calls += 1
            return o.CommandStatus.NOT_SUPPORTED


@pytest.fixture
def opendnp3_outstation():
    manager = o.DNP3Manager(1)
    port = free_port()
    channel = manager.AddTCPServer("s", o.levels.NOTHING, o.ServerAcceptMode.CloseExisting,
                                   o.IPEndpoint("127.0.0.1", port), None)
    cfg = o.OutstationStackConfig(o.DatabaseConfig(4))
    cfg.outstation.eventBufferConfig = o.EventBufferConfig.AllTypes(50)
    cfg.outstation.params.allowUnsolicited = False
    cfg.link.LocalAddr, cfg.link.RemoteAddr = 1024, 1
    commands, app = _Commands(), _App()
    outstation = channel.AddOutstation("o", commands, app, cfg)
    outstation.Enable()

    def apply(*updates):
        builder = o.UpdateBuilder()
        for value, index in updates:
            builder.Update(value, index)
        outstation.Apply(builder.Build())

    try:
        yield port, apply, commands
    finally:
        manager.Shutdown()


@needs_opendnp3
async def test_integrity_poll_values_flags_and_quality(opendnp3_outstation) -> None:
    port, apply, commands = opendnp3_outstation
    apply((o.Binary(True, o.Flags(ONLINE)), 0),
          (o.Binary(True, o.Flags(ONLINE | COMM_LOST)), 1),
          (o.Analog(250.0, o.Flags(ONLINE)), 0),
          (o.Analog(1e9, o.Flags(ONLINE | 0x20)), 1),           # OVER_RANGE
          (o.Counter(77, o.Flags(ONLINE)), 0))
    m = await connected(port)
    try:
        r = await m.integrity_poll()
    finally:
        await m.__aexit__()
    assert r.iin & 0x0080                                        # IIN1.7 DEVICE_RESTART (never cleared: no WRITE)
    pts = {(p.kind, p.index): p for p in r.points}
    assert (pts["binary", 0].value, pts["binary", 0].quality) == (True, "good")
    assert pts["binary", 1].quality == "bad_comm"
    assert (pts["analog", 0].value, pts["analog", 0].quality) == (250, "good")   # default static g30v1: int32
    assert (pts["analog", 0].group, pts["analog", 0].variation) == (30, 1)
    assert quality_of(pts["analog", 1].flags, "analog") == "bad_decode"
    assert (pts["counter", 0].value, pts["counter", 0].quality) == (77, "good")
    assert pts["binary", 3].quality == "stale"                   # never updated: RESTART flag
    assert commands.calls == 0
    assert all(f[3] & 0x0F == 0x04 for f in m.frames_sent)       # link: unconfirmed user data only
    assert {f[12] for f in m.frames_sent} <= FC_ALLOWED            # application: READ / CONFIRM only


@needs_opendnp3
async def test_explicit_variation_read_gets_floats(opendnp3_outstation) -> None:
    port, apply, _ = opendnp3_outstation
    apply((o.Analog(12.5, o.Flags(ONLINE)), 2))
    m = await connected(port)
    try:
        r = await m.read(bytes((30, 5, 0x06)))                   # g30v5 (float with flag), all points
    finally:
        await m.__aexit__()
    pts = {p.index: p for p in r.points}
    assert (pts[2].value, pts[2].group, pts[2].variation) == (12.5, 30, 5)


@needs_opendnp3
async def test_event_poll_timestamps_and_confirm(opendnp3_outstation) -> None:
    port, apply, _ = opendnp3_outstation
    m = await connected(port)
    try:
        await m.integrity_poll()                                 # drain the startup events
        apply((o.Binary(True, o.Flags(ONLINE), o.DNPTime(T0_MS)), 2),
              (o.Analog(42.0, o.Flags(ONLINE), o.DNPTime(T0_MS + 5)), 3))
        # ask for the time-tagged event variations: binary g2v2, analog g32v7 (float + time)
        r = await m.read(bytes((2, 2, 0x06, 32, 7, 0x06)))
        assert r.con                                             # events need an application CONFIRM
        events = {(p.kind, p.index): p for p in r.points if p.event}
        assert events["binary", 2].value is True and events["binary", 2].time == T0
        assert events["analog", 3].value == 42.0
        assert events["analog", 3].time == datetime.fromtimestamp((T0_MS + 5) / 1000, UTC)
        again = await m.read(bytes((2, 2, 0x06, 32, 7, 0x06)))
        assert not [p for p in again.points if p.event]          # confirmed: the buffer is empty
        assert sum(1 for f in m.frames_sent if f[12] == 0x00) >= 1   # a CONFIRM went out
    finally:
        await m.__aexit__()


# --- dnp3py outstation --------------------------------------------------------------------------------

try:
    from dnp3.core.flags import BinaryQuality as PyBinaryQuality
    from dnp3.database import Database
    from dnp3.outstation.config import OutstationConfig
    from dnp3.outstation.outstation import Outstation
    from dnp3.outstation.tcp_runner import OutstationTcpRunner
except ImportError:                                              # pragma: no cover
    Database = None
needs_dnp3py = pytest.mark.skipif(Database is None, reason="dnp3py not installed")


@needs_dnp3py
async def test_integrity_poll_against_dnp3py() -> None:
    db = Database()
    db.add_binary_input(0)
    db.add_binary_input(1)
    db.add_analog_input(0)
    db.add_counter(0)
    db.update_binary_input(0, True)
    db.update_binary_input(1, True, PyBinaryQuality.ONLINE | PyBinaryQuality.COMM_LOST)
    db.update_analog_input(0, 321.0)
    db.update_counter(0, 9)
    outstation = Outstation(config=OutstationConfig(address=1024, master_address=1, time_sync_required=False),
                            database=db)
    port = free_port()
    runner = OutstationTcpRunner(outstation, host="127.0.0.1", port=port)
    task = asyncio.create_task(runner.run())
    try:
        m = await connected(port)
        try:
            r = await m.integrity_poll()
        finally:
            await m.__aexit__()
        pts = {(p.kind, p.index): p for p in r.points if not p.event}
        assert (pts["binary", 0].value, pts["binary", 0].quality) == (True, "good")
        assert pts["binary", 1].quality == "bad_comm"
        assert pts["analog", 0].value in (321, 321.0) and pts["analog", 0].quality == "good"
        assert (pts["counter", 0].value, pts["counter", 0].quality) == (9, "good")
    finally:
        with contextlib.suppress(Exception):
            await runner.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
