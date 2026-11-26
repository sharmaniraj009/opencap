"""The DNP3 outstation simulator (#178), validated unmodified by two unrelated masters:

- opendnp3's master through yadnp3 3.2.1.5 (Apache-2.0), the reference C++ stack, threaded;
- dnp3py 0.6.0's master (MIT), pure Python on this event loop.

Both read the same static values and flags; events arrive with CON and are confirmed; unsolicited
responses arrive after ENABLE_UNSOLICITED and are confirmed; every fault switch is exercised; and
controls, writes and other state-changing functions are refused and counted (ADR-0008).
"""

from __future__ import annotations

import asyncio
import platform
import sys
import threading
import time
from datetime import UTC, datetime

import pytest
from dnp3.core.flags import IIN, AnalogQuality, BinaryQuality, CounterQuality
from dnp3.master import Master, MasterConfig, MasterTcpRunner
from dnp3.master.tcp_runner import ResponseTimeoutError

from tests.helpers.dnp3_sim import CLEAR_RESTART, Dnp3Sim, crc_dnp, link_frame, segments

# yadnp3 ships wheels for Linux x86_64 and Windows AMD64 only; elsewhere the dev extra skips it.
_WHEEL_PLATFORM = (sys.platform == "linux" and platform.machine() == "x86_64") or (
    sys.platform == "win32" and platform.machine() == "AMD64")
if _WHEEL_PLATFORM:
    import opendnp3 as o                                            # must import where a wheel exists
else:                                                               # pragma: no cover
    o = None
needs_opendnp3 = pytest.mark.skipif(o is None, reason="yadnp3 has no wheel for this platform")

T0 = datetime(2026, 11, 25, 10, 0, 0, 123000, tzinfo=UTC)
FC_CONFIRM, FC_READ, FC_WRITE, FC_COLD_RESTART = 0, 1, 2, 13


async def until(pred, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.02)


def populate(sim: Dnp3Sim) -> None:
    """Values and flags both masters must read the same way; untouched points keep RESTART."""
    sim.set_binary(0, True)
    sim.set_binary(1, False, BinaryQuality.ONLINE | BinaryQuality.COMM_LOST)
    sim.set_analog(0, 1234)
    sim.set_analog(1, -5, AnalogQuality.ONLINE | AnalogQuality.OVER_RANGE)
    sim.set_counter(0, 4711)


# --- dnp3py master --------------------------------------------------------------------------------

class PyHandler:
    def __init__(self) -> None:
        self.rows: list[tuple[str, int, object, int, datetime | None]] = []
        self.infos: list[object] = []

    def _add(self, kind, values) -> None:
        self.rows += [(kind, v.index, v.value, int(v.quality), v.timestamp) for v in values]

    def on_binary_input(self, values, info): self._add("binary", values)
    def on_binary_output(self, values, info): self._add("binary_output", values)
    def on_analog_input(self, values, info): self._add("analog", values)
    def on_analog_output(self, values, info): self._add("analog_output", values)
    def on_counter(self, values, info): self._add("counter", values)
    def on_frozen_counter(self, values, info): self._add("frozen_counter", values)
    def on_response_received(self, info): self.infos.append(info)
    def on_response_timeout(self): pass
    def on_communication_error(self, error): pass


def py_master(sim: Dnp3Sim, timeout: float = 2.0) -> tuple[MasterTcpRunner, PyHandler]:
    handler = PyHandler()
    master = Master(config=MasterConfig(address=sim.master_address, outstation_address=sim.address,
                                        response_timeout=timeout), handler=handler)
    return MasterTcpRunner(master=master, port=sim.port, response_timeout=timeout), handler


def static(rows) -> dict[tuple[str, int], tuple[object, int]]:
    """The last value and flags per point, from (kind, index, value, flags, time) rows. A binary's
    STATE bit is its value: opendnp3 reports it in the flags, dnp3py does not."""
    return {(kind, index): (value, flags & ~BinaryQuality.STATE if kind == "binary" else flags)
            for kind, index, value, flags, _ in rows}


# --- opendnp3 master ------------------------------------------------------------------------------

if o is not None:
    _KIND = {"Binary": "binary", "Analog": "analog", "Counter": "counter"}

    class _SOE(o.ISOEHandler):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.rows: list[tuple[str, int, object, int, datetime | None, bool]] = []

        def BeginFragment(self, info): pass
        def EndFragment(self, info): pass

        def Process(self, info, values):
            for v in values:
                kind = _KIND.get(type(v).__name__.removeprefix("Indexed"))
                if kind is None:
                    continue
                m = v.value
                t = m.time
                when = datetime.fromtimestamp(t.value / 1000, UTC) if t.quality != o.TimestampQuality.INVALID else None
                with self.lock:
                    self.rows.append((kind, v.index, m.value, m.flags.value, when, info.isEventVariation))

        def snapshot(self) -> list:
            with self.lock:
                return list(self.rows)

    class _App(o.IMasterApplication):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.tasks: list[str] = []
            self.iins: list[int] = []
            self.opens = 0

        def OnReceiveIIN(self, iin):
            with self.lock:
                self.iins.append(iin.LSB | iin.MSB << 8)

        def OnTaskStart(self, t, i): pass

        def OnTaskComplete(self, info):
            with self.lock:
                self.tasks.append(f"{info.type.name}:{info.result.name}")

        def OnOpen(self):
            with self.lock:
                self.opens += 1

        def OnClose(self): pass
        def AssignClassDuringStartup(self): return False
        def Now(self): return o.DNPTime(int(time.time() * 1000))

        def done(self, task: str) -> int:
            with self.lock:
                return sum(t == f"{task}:SUCCESS" for t in self.tasks)


class OpenMaster:
    """opendnp3's master on its own threads: disable unsolicited, clear IIN1.7, integrity poll,
    enable unsolicited, then whatever the test asks."""

    def __init__(self, sim: Dnp3Sim) -> None:
        self.manager = o.DNP3Manager(1)
        retry = o.ChannelRetry(o.TimeDuration.Milliseconds(100), o.TimeDuration.Milliseconds(200))
        channel = self.manager.AddTCPClient("c", o.levels.NOTHING, retry, [o.IPEndpoint("127.0.0.1", sim.port)],
                                            "0.0.0.0", None)
        cfg = o.MasterStackConfig()
        cfg.link.LocalAddr, cfg.link.RemoteAddr = sim.master_address, sim.address
        cfg.master.responseTimeout = o.TimeDuration.Seconds(1)
        cfg.master.taskRetryPeriod = o.TimeDuration.Milliseconds(200)
        self.soe, self.app = _SOE(), _App()
        self.master = channel.AddMaster("m", self.soe, self.app, cfg)

    def __enter__(self) -> OpenMaster:
        self.master.Enable()
        return self

    def __exit__(self, *exc: object) -> None:
        self.manager.Shutdown()


# --- the front end itself --------------------------------------------------------------------------

def test_crc_frames_and_segments() -> None:
    assert crc_dnp(b"123456789") == 0xEA82                       # CRC-16/DNP check value
    frame = link_frame(0x44, 1, 10, bytes(20))
    assert frame[:4] == b"\x05\x64\x19\x44" and len(frame) == 10 + 16 + 2 + 4 + 2
    segs, nxt = segments(bytes(600), 62)
    assert [s[0] for s in segs] == [0x40 | 62, 63, 0x80 | 0] and nxt == 1    # FIR, -, FIN; seq wraps at 64
    assert CLEAR_RESTART == bytes((80, 1, 0, 7, 7, 0))


# --- both masters read the same thing ------------------------------------------------------------

@needs_opendnp3
async def test_both_masters_read_the_same_values_and_flags() -> None:
    async with Dnp3Sim() as sim:
        populate(sim)
        runner, handler = py_master(sim)
        await runner.open()
        try:
            infos = await runner.integrity_poll()
        finally:
            await runner.close()
        assert infos[0].iin & IIN.DEVICE_RESTART                    # IIN1.7 until a master clears it
        py = static(r for r in handler.rows if r[4] is None or r[0] == "counter")

        with OpenMaster(sim) as m:
            await until(lambda: m.app.done("STARTUP_INTEGRITY_POLL"), what="opendnp3 integrity poll")
            od = static((k, i, v, f, t) for k, i, v, f, t, event in m.soe.snapshot() if not event)
        assert sim.writes == 1 and not sim.iin & IIN.DEVICE_RESTART  # opendnp3 cleared IIN1.7 with g80v1
        expected = {
            ("binary", 0): (True, BinaryQuality.ONLINE),
            ("binary", 1): (False, BinaryQuality.ONLINE | BinaryQuality.COMM_LOST),
            ("binary", 2): (False, BinaryQuality.RESTART),
            ("analog", 0): (1234, AnalogQuality.ONLINE),
            ("analog", 1): (-5, AnalogQuality.ONLINE | AnalogQuality.OVER_RANGE),
            ("analog", 2): (0, AnalogQuality.RESTART),
            ("counter", 0): (4711, CounterQuality.ONLINE),
            ("counter", 1): (0, CounterQuality.RESTART),
        }
        for key, want in expected.items():
            assert od[key] == want, (key, od[key])
            assert py[key] == want, (key, py[key])
        assert sim.control_attempts == 0


async def test_event_poll_confirm_and_counter_event_time() -> None:
    async with Dnp3Sim() as sim:
        runner, handler = py_master(sim)
        await runner.open()
        try:
            await runner.integrity_poll()                            # takes the start-up events
            confirms = sim.function_counts[FC_CONFIRM]
            sim.set_counter(1, 99, time=T0)
            sim.set_binary(3, True)
            handler.rows.clear()
            infos = await runner.class_poll(class_1=True, class_2=True, class_3=True)
            assert infos[0].con                                      # events need a CONFIRM
            await until(lambda: sim.function_counts[FC_CONFIRM] == confirms + 1, what="the CONFIRM")
            assert ("counter", 1, 99, CounterQuality.ONLINE, T0) in handler.rows     # g22v5 carries the time
            assert any(r[:3] == ("binary", 3, True) for r in handler.rows)
            handler.rows.clear()
            await runner.class_poll(class_1=True, class_2=True, class_3=True)
            assert handler.rows == []                                # confirmed events are gone
        finally:
            await runner.close()


# --- unsolicited ---------------------------------------------------------------------------------

@needs_opendnp3
async def test_unsolicited_with_confirm_both_masters() -> None:
    async with Dnp3Sim() as sim:
        with OpenMaster(sim) as m:
            await until(lambda: m.app.done("ENABLE_UNSOLICITED"), what="ENABLE_UNSOLICITED")
            confirms = sim.function_counts[FC_CONFIRM]
            sim.set_counter(1, 77, time=T0)
            await until(lambda: any(r[:2] == ("counter", 1) and r[2] == 77 for r in m.soe.snapshot()),
                        what="the unsolicited counter event")
            assert [r[4] for r in m.soe.snapshot() if r[:3] == ("counter", 1, 77)] == [T0]
            await until(lambda: sim.function_counts[FC_CONFIRM] > confirms, what="the unsolicited CONFIRM")
        assert sim.unsolicited_given_up == 0

    async with Dnp3Sim() as sim:
        runner, handler = py_master(sim)
        await runner.open()
        try:
            await runner.integrity_poll()
            await runner.enable_unsolicited()
            sim.set_binary(2, True)
            for _ in range(3):                                       # the start-up null response may come first
                info = await runner.listen_unsolicited(timeout=3)
                assert info is not None and info.is_unsolicited
                if any(r[:3] == ("binary", 2, True) for r in handler.rows):
                    break
            assert any(r[:3] == ("binary", 2, True) for r in handler.rows)
            assert not sim.null_unsolicited_pending                  # dnp3py confirmed it as well
        finally:
            await runner.close()


@needs_opendnp3
async def test_unsolicited_burst_arrives_in_order() -> None:
    async with Dnp3Sim() as sim:
        with OpenMaster(sim) as m:
            await until(lambda: m.app.done("ENABLE_UNSOLICITED"), what="ENABLE_UNSOLICITED")
            sent = sim.unsolicited_burst(30, index=2)
            await until(lambda: [r[2] for r in m.soe.snapshot() if r[:2] == ("analog", 2) and r[5]][-1:] == [sent[-1]],
                        what="the last burst value")
            got = [r[2] for r in m.soe.snapshot() if r[:2] == ("analog", 2) and r[5]]
            assert got == sent


# --- read-only: controls, writes and other functions are refused and counted -------------------

@needs_opendnp3
async def test_controls_writes_and_restarts_are_refused_and_counted() -> None:
    async with Dnp3Sim() as sim:
        populate(sim)
        with OpenMaster(sim) as m:
            await until(lambda: m.app.done("ENABLE_UNSOLICITED"), what="start-up")
            results: list[object] = []
            crob = o.ControlRelayOutputBlock(o.OperationType.LATCH_ON)
            m.master.DirectOperate(crob, 0, results.append)
            m.master.SelectAndOperate(crob, 1, results.append)
            m.master.DirectOperate(o.AnalogOutputFloat32(5.0), 0, results.append)
            m.master.Write(o.TimeAndInterval(o.DNPTime(0), 1, o.IntervalUnits.Seconds), 0)
            m.master.Restart(o.RestartType.COLD, results.append)
            await until(lambda: len(results) == 4, what="four command callbacks")
            await until(lambda: sim.function_counts[FC_COLD_RESTART] == 1 and sim.writes == 2, what="write, restart")
            assert [[p.status for p in r.to_list()] for r in results[:3]] == [[o.CommandStatus.NOT_SUPPORTED]] * 3
            assert results[1].to_list()[0].state == o.CommandPointState.SELECT_FAIL
            assert results[3].summary == o.TaskCompletion.FAILURE_BAD_RESPONSE    # COLD_RESTART: IIN2.0
        assert sim.control_attempts == 3                             # SELECT failed, so no OPERATE followed
        assert sim.writes == 2                                       # the IIN1.7 clear and the refused g50
        assert sim.outstation.database.get_binary_input(0).value is True
        assert sim.outstation.database.get_analog_input(0).value == 1234


# --- fault switches ------------------------------------------------------------------------------

async def test_mute_times_out_and_recovers(no_gc) -> None:
    async with Dnp3Sim() as sim:
        runner, _ = py_master(sim, timeout=0.3)
        await runner.open()
        try:
            sim.mute = True
            with pytest.raises(ResponseTimeoutError):
                await runner.integrity_poll()
            sim.mute = False
            assert (await runner.integrity_poll())[0].fin
        finally:
            await runner.close()


async def test_corrupt_crc_drops_one_frame() -> None:
    async with Dnp3Sim() as sim:
        runner, _ = py_master(sim, timeout=0.3)
        await runner.open()
        try:
            await runner.integrity_poll()
            sim.corrupt_crc = 1
            with pytest.raises(ResponseTimeoutError):               # the master discards the bad frame
                await runner.integrity_poll()
            assert sim.corrupt_crc == 0
            assert (await runner.integrity_poll())[0].fin
        finally:
            await runner.close()


@needs_opendnp3
async def test_split_frames_and_delay(no_gc) -> None:
    async with Dnp3Sim() as sim:
        populate(sim)
        sim.split_frames, sim.delay = True, 0.3
        runner, handler = py_master(sim)
        await runner.open()
        try:
            started = time.perf_counter()
            await runner.integrity_poll()
            # asyncio fires a timer up to one clock tick early, and the tick is ~15.6 ms on Windows
            # (#199): allow two ticks. Without the delay a local poll takes a few milliseconds.
            assert time.perf_counter() - started >= sim.delay - 0.05
        finally:
            await runner.close()
        assert static(handler.rows)[("analog", 0)] == (1234, AnalogQuality.ONLINE)
        with OpenMaster(sim) as m:
            await until(lambda: m.app.done("STARTUP_INTEGRITY_POLL"), what="opendnp3 integrity poll")
            assert ("analog", 0, 1234) in {r[:3] for r in m.soe.snapshot()}


@needs_opendnp3
async def test_drop_connection_and_restart_on_the_same_port() -> None:
    async with Dnp3Sim() as sim:
        with OpenMaster(sim) as m:
            await until(lambda: m.app.done("ENABLE_UNSOLICITED"), what="start-up")
            assert sim.writes == 1                                   # IIN1.7 cleared once
            sim.drop_connection()
            await until(lambda: sim.connections == 2 and m.app.opens == 2, what="reconnect")
            sim.set_analog(3, 42)                                    # the session goes on: unsolicited
            await until(lambda: ("analog", 3, 42) in {r[:3] for r in m.soe.snapshot()}, what="a value after it")
            assert sim.writes == 1                                   # no restart, nothing to clear
            port = sim.port
            await sim.restart()
            assert sim.port == port and sim.iin & IIN.DEVICE_RESTART
            await until(lambda: sim.writes == 2, what="IIN1.7 cleared again after the restart")
            assert sim.connections == 3


async def test_event_overflow_sets_iin23() -> None:
    async with Dnp3Sim(event_buffer=5) as sim:
        runner, handler = py_master(sim)
        await runner.open()
        try:
            sim.event_overflow()
            infos = await runner.integrity_poll()
            assert infos[0].iin & IIN.EVENT_BUFFER_OVERFLOW
            assert len([r for r in handler.rows if r[:2] == ("analog", 0) and r[4] is None]) >= 5
        finally:
            await runner.close()


@needs_opendnp3
async def test_multi_fragment_response_both_masters() -> None:
    async with Dnp3Sim(analogs=100, max_fragment_size=249) as sim:
        for i in range(100):
            sim.set_analog(i, 100 + i)
        runner, handler = py_master(sim)
        await runner.open()
        try:
            await runner.integrity_poll()                            # takes the 100 start-up events
            handler.rows.clear()
            confirms = sim.function_counts[FC_CONFIRM]
            infos = await runner.integrity_poll()
        finally:
            await runner.close()
        assert len(infos) > 1 and all(i.con for i in infos[:-1])     # CON on every non-final fragment
        assert sim.function_counts[FC_CONFIRM] - confirms >= len(infos) - 1
        assert {i: v for k, i, v, _, _ in handler.rows if k == "analog"} == {i: 100 + i for i in range(100)}
        with OpenMaster(sim) as m:
            await until(lambda: m.app.done("STARTUP_INTEGRITY_POLL"), what="opendnp3 integrity poll")
            got = {r[1]: r[2] for r in m.soe.snapshot() if r[0] == "analog" and not r[5]}
            assert got == {i: 100 + i for i in range(100)}
