"""modbus_rtu driver against pymodbus' RTU server (tests/helpers/modbus_sim.py, framer="rtu"):
RTU frames on a TCP socket (a serial-over-TCP gateway) and the same simulator behind a real tty
(pty bridge, pymodbus' serial client). Values go through the same planner/decoder as Modbus
TCP, so a round trip against the simulator's independent encoder proves the RTU path."""

from __future__ import annotations

import asyncio
import time

import pytest

from opencap.config.models import Channel, Device
from opencap.core.cache import ValueCache
from opencap.core.channel_worker import ChannelWorker
from opencap.core.registry import plan_device
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost
from opencap.drivers.modbus_rtu import MIN_INTER_FRAME_S, ModbusRtuDriver, inter_frame_s
from opencap.drivers._serial import SerialSettings
from tests.helpers.modbus_sim import ModbusSim, free_port, make_sim_devices
from tests.helpers.netcut import stable_port
from tests.helpers.pty_bridge import PtyBridge, needs_pyserial


def _rtu(device: Device, port: int, *, host: str = "127.0.0.1", **params) -> Device:
    return device.model_copy(update={
        "driver": "modbus_rtu", "host": host, "port": port, "timeout_ms": 500,
        "params": {**device.params, "transport": "tcp", "baudrate": 19200, **params},
    })


async def _read_all(driver: ModbusRtuDriver) -> dict:
    out = {}
    for block in driver.plan(driver.device.tags).blocks:
        out.update(await driver.read(block))
    return out


def _assert_matches(sim_image, values) -> None:
    for name, want in sim_image.expected().items():
        tv = values[name]
        assert tv.quality == "good", (name, tv)
        assert tv.value == want, (name, tv.value, want)


@pytest.fixture()
async def rtu_sim():
    dev = make_sim_devices(1, 40, seed=21)[0]
    sim = await ModbusSim.start(dev, framer="rtu")
    yield sim
    await sim.stop()


async def test_reads_match_the_image_over_rtu_on_tcp(rtu_sim):
    drv = ModbusRtuDriver(_rtu(rtu_sim.device, rtu_sim.port))
    try:
        await drv.connect()
        assert drv.connected
        values = await _read_all(drv)
        _assert_matches(rtu_sim.image, values)
        rtu_sim.tick()
        _assert_matches(rtu_sim.image, await _read_all(drv))
        assert rtu_sim.write_attempts == 0
        assert rtu_sim.request_count == 2 * len(drv.plan(drv.device.tags).blocks)
    finally:
        await drv.close()


@needs_pyserial
async def test_reads_over_a_tty(rtu_sim):
    bridge = await PtyBridge.start("127.0.0.1", rtu_sim.port)
    # 8N1: a Linux pty refuses parity on every open but the first (EINVAL), real ports do not
    drv = ModbusRtuDriver(_rtu(rtu_sim.device, 0, host=bridge.path, transport="serial", baudrate=9600))
    try:
        await drv.connect()
        _assert_matches(rtu_sim.image, await _read_all(drv))
        assert bridge.bytes_to_sim > 0
        ident = await drv.identity()
        assert ident.get("vendor") == "opencap"
    finally:
        await drv.close()
        await bridge.stop()


async def test_silent_device_is_comm_error_and_line_is_reopened(rtu_sim):
    drv = ModbusRtuDriver(_rtu(rtu_sim.device, rtu_sim.port))
    try:
        await drv.connect()
        block = drv.plan(drv.device.tags).blocks[0]
        rtu_sim.mute = True
        started = time.monotonic()
        with pytest.raises(CommError, match="no reply within 500 ms"):
            await drv.read(block)
        assert time.monotonic() - started < 1.0
        assert not drv.connected                     # close_on_timeout: late bytes are thrown away
        rtu_sim.mute = False
        await drv.connect()
        assert all(tv.quality == "good" for tv in (await drv.read(block)).values())
    finally:
        await drv.close()


async def test_wrong_unit_id_is_silence_not_an_answer(rtu_sim):
    drv = ModbusRtuDriver(_rtu(rtu_sim.device, rtu_sim.port, unit_id=9))
    try:
        await drv.connect()
        with pytest.raises(CommError, match="no reply"):
            await drv.read(drv.plan(drv.device.tags).blocks[0])
    finally:
        await drv.close()


@pytest.mark.parametrize(("code", "error"), [(2, BlockConfigError), (6, CommError)])
async def test_exception_codes(rtu_sim, code, error):
    drv = ModbusRtuDriver(_rtu(rtu_sim.device, rtu_sim.port))
    try:
        await drv.connect()
        rtu_sim.exception_code = code
        with pytest.raises(error, match=f"exception {code:02d}"):
            await drv.read(drv.plan(drv.device.tags).blocks[0])
        assert drv.connected
    finally:
        await drv.close()


async def test_units_on_one_line_share_one_client(rtu_sim):
    other = make_sim_devices(1, 25, seed=22, params={"unit_id": 7})[0]
    image7 = rtu_sim.add_unit(other)
    a = ModbusRtuDriver(_rtu(rtu_sim.device, rtu_sim.port))
    b = ModbusRtuDriver(_rtu(other.model_copy(update={"id": "SIM_SITE_01/UNIT7", "name": "UNIT7"}),
                             rtu_sim.port, unit_id=7))
    try:
        await a.connect()
        await b.connect()
        assert a._client is b._client                  # one connection for the line
        va, vb = await asyncio.gather(_read_all(a), _read_all(b))
        _assert_matches(rtu_sim.image, va)
        _assert_matches(image7, vb)
        await a.close()
        assert b.connected
        _assert_matches(image7, await _read_all(b))
        # a timeout on one unit (close_on_timeout) drops the line for every unit
        await a.connect()
        rtu_sim.mute = True
        with pytest.raises(CommError):
            await a.read(a.plan(a.device.tags).blocks[0])
        assert not a.connected and not b.connected
        rtu_sim.mute = False
        with pytest.raises(ConnectionLost):
            await b.read(b.plan(b.device.tags).blocks[0])
        await b.connect()
        await a.connect()
        assert a._client is b._client
        _assert_matches(image7, await _read_all(b))
    finally:
        await a.close()
        await b.close()


async def test_inter_frame_gap(rtu_sim):
    assert inter_frame_s(SerialSettings(baudrate=9600), None) == pytest.approx(3.5 * 10 / 9600)
    assert inter_frame_s(SerialSettings(baudrate=115200), None) == MIN_INTER_FRAME_S
    assert inter_frame_s(SerialSettings(), 50) == 0.05
    with pytest.raises(ValueError, match="inter_frame_ms"):
        inter_frame_s(SerialSettings(), "soon")
    drv = ModbusRtuDriver(_rtu(rtu_sim.device, rtu_sim.port, inter_frame_ms=120))
    try:
        await drv.connect()
        block = drv.plan(drv.device.tags).blocks[0]
        await drv.read(block)
        started = time.monotonic()
        for _ in range(3):
            await drv.read(block)
        assert time.monotonic() - started >= 3 * 0.12 - 0.01
    finally:
        await drv.close()


async def test_transport_errors(rtu_sim):
    refused = ModbusRtuDriver(_rtu(rtu_sim.device, free_port()))
    with pytest.raises(ConnectionLost, match="connect failed"):
        await refused.connect()
    rfc = ModbusRtuDriver(_rtu(rtu_sim.device, rtu_sim.port, transport="rfc2217"))
    with pytest.raises(ConnectionLost, match="rfc2217 is not supported by modbus_rtu"):
        await rfc.connect()
    unmapped = ModbusRtuDriver(_rtu(rtu_sim.device, 0, host="COM4", transport="unmapped", com_port="COM4"))
    with pytest.raises(ConnectionLost, match="COM4 is not mapped"):
        await unmapped.connect()
    missing = ModbusRtuDriver(_rtu(rtu_sim.device, 0, host="/dev/opencap-no-such-tty", transport="serial"))
    with pytest.raises(ConnectionLost, match="connect failed"):
        await missing.connect()
    with pytest.raises(ValueError, match="baudrate"):
        ModbusRtuDriver(_rtu(rtu_sim.device, 1, baudrate=0))
    with pytest.raises(ValueError, match="local_echo"):
        ModbusRtuDriver(_rtu(rtu_sim.device, 1, local_echo="sometimes"))


async def test_gateway_power_off_and_on():
    dev = make_sim_devices(1, 20, seed=23)[0]
    sim = await ModbusSim.start(dev, framer="rtu", port=stable_port())
    drv = ModbusRtuDriver(_rtu(sim.device, sim.port))
    try:
        await drv.connect()
        block = drv.plan(drv.device.tags).blocks[0]
        await drv.read(block)
        await sim.stop()
        with pytest.raises(ConnectionLost):
            await drv.read(block)
        await sim.restart()
        await drv.connect()
        assert all(tv.quality == "good" for tv in (await drv.read(block)).values())
    finally:
        await drv.close()
        await sim.stop()


async def test_channel_worker_read_once(rtu_sim):
    dev = _rtu(rtu_sim.device, rtu_sim.port).model_copy(update={"timeout_ms": 150, "retries": 0})
    worker = ChannelWorker(Channel(name=dev.channel, devices=[dev]), {dev.id: plan_device(dev)}, ValueCache(),
                           start_jitter=0.0, backoff_base_s=0.05, backoff_max_s=0.2)
    snap = await worker.read_once(dev.id)
    _assert_matches(rtu_sim.image, snap)
    rtu_sim.mute = True
    snap = await worker.read_once(dev.id)
    assert {tv.quality for tv in snap.values()} == {"bad_comm"}
    rtu_sim.mute = False
    _assert_matches(rtu_sim.image, await worker.read_once(dev.id))
    await worker.stop()
