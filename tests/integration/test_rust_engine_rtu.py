"""Modbus RTU in the Rust engine (#47) against the Python reference and its RTU simulator
(`ModbusSim(framer="rtu")`, pymodbus' own RTU server; `PtyBridge` puts it behind a real tty):

- `check -v` and `read` print the same text from both engines: same planner, same decoder,
  RTU on TCP (serial-over-TCP gateway) and a local serial port (pty);
- the fault switches (silent unit, wrong unit id, exception codes, a corrupted CRC on the line)
  give the same qualities in both engines;
- the whole engine (`engine_factory`: --engine / OPENCAP_ENGINE) polls two unit ids sharing one
  line plus a unit behind a tty: correct values, values follow the PLC, powering off the gateway
  demotes only its units and they resume, and nothing but reads reaches the simulator.

The Rust parts are skipped when the binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import sys
from collections.abc import Callable

import pytest

from opencap.config.models import Device
from tests.helpers.configdir import write_config
from tests.helpers.engine import rust_binary
from tests.helpers.modbus_sim import ModbusSim, free_port, make_sim_devices
from tests.helpers.netcut import stable_port
from tests.helpers.pty_bridge import PtyBridge, needs_pyserial

BIN = rust_binary()
needs_rust = pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)")


def _rtu(src: Device, *, channel: str, name: str, host: str, port: int, **params) -> Device:
    return src.model_copy(update={
        "id": f"{channel}/{name}", "channel": channel, "name": name, "driver": "modbus_rtu",
        "host": host, "port": port, "timeout_ms": 500, "retries": 0,
        "params": {**src.params, "transport": "tcp", "baudrate": 19200, **params},
    })


def _value_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\S+ +(good|bad_comm|bad_config|bad_decode|stale) ", line)]


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    return proc.returncode, out.decode(), err.decode()


async def _both(*args: str) -> tuple[tuple[int, str, str], tuple[int, str, str]]:
    py = await _run(sys.executable, "-m", "opencap", *args)
    rs = await _run(str(BIN), *args)
    return py, rs


class NoisyLine:
    """A serial-over-TCP gateway in front of the simulator that can corrupt replies: with
    `corrupt_next = n` the next n replies arrive with their last (CRC) byte flipped."""

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.corrupt_next = 0
        self.corrupted = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    @classmethod
    async def start(cls, target_port: int) -> NoisyLine:
        line = cls(target_port)
        line._server = await asyncio.start_server(line._serve, "127.0.0.1", 0)
        line.port = line._server.sockets[0].getsockname()[1]
        return line

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.target_port)

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, replies: bool) -> None:
            with contextlib.suppress(OSError, asyncio.CancelledError):
                while data := await src.read(4096):
                    if replies and self.corrupt_next > 0:
                        self.corrupt_next -= 1
                        self.corrupted += 1
                        data = data[:-1] + bytes([data[-1] ^ 0xFF])
                    dst.write(data)
                    await dst.drain()
            dst.close()

        tasks = [asyncio.create_task(pump(reader, up_writer, False)), asyncio.create_task(pump(up_reader, writer, True))]
        self._tasks.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()


@pytest.fixture()
async def line():
    """One RTU line (gateway simulator) with unit 0 and unit 7."""
    base = make_sim_devices(2, 30, seed=81)
    sim = await ModbusSim.start(base[0], framer="rtu")
    unit7 = base[1].model_copy(update={"params": {**base[1].params, "unit_id": 7}})
    image7 = sim.add_unit(unit7)
    yield sim, unit7, image7
    await sim.stop()


@needs_rust
async def test_check_and_read_match_python_over_rtu_on_tcp(tmp_path, line):
    sim, unit7, image7 = line
    devices = [
        _rtu(sim.device, channel="RTU_SITE_01", name="UNIT0", host="127.0.0.1", port=sim.port),
        _rtu(unit7, channel="RTU_SITE_01", name="UNIT7", host="127.0.0.1", port=sim.port, unit_id=7),
        _rtu(unit7, channel="RTU_SITE_02", name="SERIAL", host="/dev/ttyUSB9", port=0, transport="serial",
             baudrate=9600, parity="E", inter_frame_ms=5),
    ]
    config_dir = write_config(tmp_path, devices)
    for args in (["check", "-c", str(config_dir), "-v"], ["check", "-c", str(config_dir)]):
        py, rs = await _both(*args)
        assert rs == py, f"{args}:\n--- python\n{py[1]}{py[2]}\n--- rust\n{rs[1]}{rs[2]}"
    assert "modbus_rtu" in py[1]
    for dev in devices[:2]:
        py, rs = await _both("read", dev.id, "-c", str(config_dir))
        assert rs == py, f"--- python\n{py}\n--- rust\n{rs}"
        assert py[0] == 0 and len(_value_lines(py[1])) == 30, py
    image7.tick()
    sim.tick()
    py, rs = await _both("read", devices[1].id, "-c", str(config_dir))
    assert rs == py and py[0] == 0
    assert sim.write_attempts == 0


@needs_rust
@needs_pyserial
async def test_read_and_identity_over_a_tty(tmp_path, line):
    sim, _, _ = line
    bridge = await PtyBridge.start("127.0.0.1", sim.port)
    try:
        # 8N1: a Linux pty refuses parity on every open but the first (EINVAL), real ports do not
        dev = _rtu(sim.device, channel="RTU_TTY", name="METER", host=bridge.path, port=0, transport="serial",
                   baudrate=9600)
        config_dir = write_config(tmp_path, [dev])
        py, rs = await _both("read", dev.id, "-c", str(config_dir))
        assert rs == py, f"--- python\n{py}\n--- rust\n{rs}"
        assert py[0] == 0 and len(_value_lines(py[1])) == 30
        code, out, err = await _run(str(BIN), "identity", dev.id, "-c", str(config_dir))
        assert code == 0 and "opencap" in out, (code, out, err)
        assert bridge.bytes_to_sim > 0
    finally:
        await bridge.stop()


@needs_rust
async def test_fault_switches_give_the_same_qualities(tmp_path, line):
    sim, _, _ = line
    noisy = await NoisyLine.start(sim.port)
    try:
        good = _rtu(sim.device, channel="RTU_SITE_01", name="UNIT0", host="127.0.0.1", port=sim.port)
        stranger = _rtu(sim.device, channel="RTU_SITE_02", name="UNIT9", host="127.0.0.1", port=sim.port, unit_id=9)
        via_noise = _rtu(sim.device, channel="RTU_SITE_03", name="NOISY", host="127.0.0.1", port=noisy.port)
        config_dir = write_config(tmp_path, [good, stranger, via_noise])

        async def compare(dev: Device) -> tuple[int, list[str]]:
            py, rs = await _both("read", dev.id, "-c", str(config_dir))
            # pymodbus logs to stdout on some failures: compare the value lines and exit codes
            assert (rs[0], _value_lines(rs[1])) == (py[0], _value_lines(py[1])), f"--- python\n{py}\n--- rust\n{rs}"
            return py[0], sorted({line.split()[1] for line in _value_lines(py[1])})

        # a unit id nobody answers is silence, not an answer
        assert await compare(stranger) == (3, ["bad_comm"])
        # exception 02 (illegal data address) is permanent: bad_config; 06 (busy) is transient
        sim.exception_code = 2
        assert await compare(good) == (3, ["bad_config"])
        sim.exception_code = 6
        assert await compare(good) == (3, ["bad_comm"])
        sim.exception_code = None
        # a silent device
        sim.mute = True
        assert await compare(good) == (3, ["bad_comm"])
        sim.mute = False
        assert await compare(good) == (0, ["good"])
        # a corrupted CRC is dropped by the framer: no reply within timeout_ms
        noisy.corrupt_next = 1000
        assert await compare(via_noise) == (3, ["bad_comm"])
        assert noisy.corrupted >= 2
        noisy.corrupt_next = 0
        assert await compare(via_noise) == (0, ["good"])
        assert noisy.corrupted > 0
        assert sim.write_attempts == 0
    finally:
        await noisy.stop()


# --- the whole engine -----------------------------------------------------------------------------

def _close(got, want) -> bool:
    if isinstance(want, float):
        return math.isclose(float(got), want, rel_tol=1e-6, abs_tol=1e-4)
    return got == want


async def _wait(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with contextlib.suppress(KeyError):
            if pred():
                return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.1)


def _matches(engine, dev_id: str, image) -> bool:
    values = engine.device_values(dev_id)
    for name, want in image.expected().items():
        tv = values.get(name) or {}
        if tv.get("quality") != "good" or not _close(tv.get("value"), want):
            return False
    return True


@pytest.mark.acceptance
@needs_pyserial
async def test_rtu_lines_through_the_engine(tmp_path, engine_factory):
    base = make_sim_devices(3, 25, seed=91)
    gateway = await ModbusSim.start(base[0], framer="rtu", port=stable_port())
    unit7 = base[1].model_copy(update={"params": {**base[1].params, "unit_id": 7}})
    image7 = gateway.add_unit(unit7)
    behind_tty = await ModbusSim.start(base[2], framer="rtu")
    bridge = await PtyBridge.start("127.0.0.1", behind_tty.port)
    timing = {"scan_ms": 500, "min_scan_ms": 500, "timeout_ms": 500, "retries": 1, "demote_after": 2,
              "demote_for_ms": 1500}
    devices = [
        _rtu(gateway.device, channel="RTU_SITE_01", name="UNIT0", host="127.0.0.1", port=gateway.port),
        _rtu(unit7, channel="RTU_SITE_01", name="UNIT7", host="127.0.0.1", port=gateway.port, unit_id=7),
        _rtu(behind_tty.device, channel="RTU_SITE_02", name="METER", host=bridge.path, port=0, transport="serial",
             baudrate=9600),
    ]
    devices = [d.model_copy(update=timing) for d in devices]
    images = {devices[0].id: gateway.image, devices[1].id: image7, devices[2].id: behind_tty.image}
    config_dir = write_config(tmp_path, devices, api_port=free_port(), mqtt_port=free_port())
    engine = engine_factory(config_dir)
    await engine.start()
    try:
        async with asyncio.timeout(90):
            # 1. every unit reads the simulator's values, two units sharing one line
            await _wait(lambda: all(_matches(engine, d, img) for d, img in images.items()), 20, "good values")
            # 2. values follow the PLC
            gateway.tick()
            image7.tick()
            behind_tty.tick()
            await _wait(lambda: all(_matches(engine, d, img) for d, img in images.items()), 10, "new values")
            # 3. the gateway powers off: only its units are demoted, the tty line carries on
            await gateway.stop()
            for d in devices[:2]:
                await _wait(lambda d=d: engine.device_detail(d.id)["state"] == "demoted", 15, f"{d.id} demoted")
            assert engine.device_detail(devices[2].id)["state"] == "ok"
            behind_tty.tick()
            await _wait(lambda: _matches(engine, devices[2].id, behind_tty.image), 10, "tty line still read")
            # 4. power back on: both units resume
            await gateway.restart()
            gateway.tick()
            await _wait(lambda: all(engine.device_detail(d.id)["state"] == "ok" for d in devices[:2]), 20, "resumed")
            await _wait(lambda: all(_matches(engine, d, img) for d, img in images.items()), 10, "values after restart")
        assert gateway.write_attempts == 0 and behind_tty.write_attempts == 0
        assert bridge.bytes_to_sim > 0
    finally:
        await engine.stop()
        await bridge.stop()
        await asyncio.gather(gateway.stop(), behind_tty.stop(), return_exceptions=True)

