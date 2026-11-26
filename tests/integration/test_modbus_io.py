"""Modbus TCP driver I/O against the in-process simulator (docs/phase1_contracts.md §1).

Every server binds an ephemeral port on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import socket
import sys
import time
from collections import Counter
from datetime import UTC, datetime

import pytest

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers import modbus_tcp
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost
from opencap.drivers.modbus_tcp import ModbusBlock, ModbusTcpDriver
from tests.helpers.modbus_sim import ModbusSim, free_port, make_sim_devices, start_sims, stop_sims

# timing bounds (#69): no automatic GC pause inside a test (tests/conftest.py no_gc)
pytestmark = pytest.mark.usefixtures("no_gc")


def with_(device: Device, **update) -> Device:
    params = update.pop("params", None)
    if params is not None:
        update["params"] = {**device.params, **params}
    return device.model_copy(update=update)


async def read_all(driver: ModbusTcpDriver, tags: list[Tag] | None = None) -> dict[str, TagValue]:
    plan = driver.plan(tags if tags is not None else driver.device.tags)
    assert plan.invalid == {}
    out: dict[str, TagValue] = {}
    for block in plan.blocks:
        values = await driver.read(block)
        assert set(values) == set(block.tags)
        out.update(values)
    return out


def raw(values: dict[str, TagValue]) -> dict:
    return {name: tv.value for name, tv in values.items()}


@pytest.fixture
async def sim():
    s = await ModbusSim.start(make_sim_devices(1, 53, seed=7)[0])
    yield s
    await s.stop()


@pytest.fixture
async def driver(sim):
    d = ModbusTcpDriver(with_(sim.device, timeout_ms=300))
    await d.connect()
    yield d
    await d.close()


# --- generator ------------------------------------------------------------------------------------

def test_make_sim_devices_30_devices_about_1600_tags_all_plan():
    devices = make_sim_devices(30, 53)
    assert len(devices) == 30
    assert sum(len(d.tags) for d in devices) == 1590
    fcs: Counter[int] = Counter()
    dtypes: Counter[str] = Counter(t.dtype for d in devices for t in d.tags)
    blocks = 0
    for d in devices:
        plan = ModbusTcpDriver(d).plan(d.tags)
        assert plan.invalid == {}, d.id
        assert sorted(n for b in plan.blocks for n in b.tags) == sorted(t.name for t in d.tags)
        assert len(plan.blocks) >= 4, d.id
        fcs.update(b.fc for b in plan.blocks)
        blocks += len(plan.blocks)
    assert set(fcs) == {1, 2, 3, 4}
    assert set(dtypes) == {"float32", "uint16", "int16", "int32", "uint32", "bool"}
    assert 200 <= blocks <= 400       # real project: 1,594 tags in 268 blocks (D-005)


# --- round trip ----------------------------------------------------------------------------------------

WORD_ORDERS = [
    pytest.param({}, id="real-project-defaults"),
    pytest.param({"first_word_low": False}, id="first-word-high"),
    pytest.param({"byte_order_big": False}, id="bytes-swapped"),
    pytest.param({"first_word_low": False, "byte_order_big": False, "modicon_bit_order": True}, id="all-swapped"),
    pytest.param({"zero_based": False}, id="one-based"),
]


@pytest.mark.parametrize("params", WORD_ORDERS)
async def test_plan_read_matches_expected_then_tick(params):
    devices = make_sim_devices(2, 53, seed=len(str(params)), params=params)
    sims = await start_sims(devices)
    try:
        for s in sims:
            drv = ModbusTcpDriver(s.device)
            await drv.connect()
            before = datetime.now(UTC)
            first = await read_all(drv)
            after = datetime.now(UTC)
            assert raw(first) == s.expected()
            assert {tv.quality for tv in first.values()} == {"good"}
            assert all(before <= tv.source_ts <= after and tv.source_ts.tzinfo is not None
                       for tv in first.values())
            assert any(name.endswith("_ALIAS") for name in first)

            s.tick()
            second = await read_all(drv)
            assert raw(second) == s.expected()
            assert all(second[n].value != first[n].value for n in first)
            await drv.close()
            assert s.write_attempts == 0
    finally:
        await stop_sims(sims)


EXOTIC_TAGS = [
    Tag(name="F64", address="400001", dtype="float64"),
    Tag(name="I64", address="400005", dtype="int64"),
    Tag(name="U64", address="400009", dtype="uint64"),
    Tag(name="STR_H", address="400013.010H", dtype="string"),
    Tag(name="STR_L", address="400020.007L", dtype="string"),
    Tag(name="ARR", address="400030[4]", dtype="uint16"),
    Tag(name="ARR2", address="400040[2][3]", dtype="int16"),
    Tag(name="FARR", address="400050[2]", dtype="float32"),
    Tag(name="BIT_A", address="400060.1", dtype="bool"),
    Tag(name="BIT_B", address="400060.16", dtype="bool"),
    Tag(name="COILS", address="000010[5]", dtype="bool"),
    Tag(name="DI", address="100003", dtype="bool"),
    Tag(name="IR", address="300001", dtype="int32"),
]


@pytest.mark.parametrize("params", [
    pytest.param({"zero_based_bit": False, "first_dword_low": False}, id="dword-high"),
    pytest.param({"zero_based_bit": False, "first_word_low": False, "byte_order_big": False,
                  "modicon_bit_order": True}, id="all-swapped"),
])
async def test_64_bit_strings_arrays_and_unit_id(params):
    dev = Device(id="X/EXOTIC", channel="X", name="EXOTIC", driver="modbus_tcp", host="127.0.0.1", port=0,
                 params={"unit_id": 7, **params}, timeout_ms=500, tags=EXOTIC_TAGS)
    s = await ModbusSim.start(dev)
    try:
        drv = ModbusTcpDriver(s.device)
        await drv.connect()
        assert raw(await read_all(drv)) == s.expected()
        s.tick()
        assert raw(await read_all(drv)) == s.expected()
        s.set_value("FARR", [math.nan, 1.5])       # NaN -> 0.0 (nonnormal_float "zero")
        s.set_value("STR_H", "AB")
        values = raw(await read_all(drv))
        assert values["FARR"] == [0.0, 1.5] and values["STR_H"] == "AB"
        for a, b in ((True, False), (False, True)):  # bits .1 and .16 of one register: never confuse them
            s.set_value("BIT_A", a)
            s.set_value("BIT_B", b)
            values = raw(await read_all(drv))
            assert (values["BIT_A"], values["BIT_B"]) == (a, b)
        await drv.close()

        wrong_unit = ModbusTcpDriver(with_(s.device, params={"unit_id": 3}))
        await wrong_unit.connect()
        block = wrong_unit.plan(EXOTIC_TAGS).blocks[0]
        with pytest.raises(CommError, match="exception 11") as exc:
            await wrong_unit.read(block)
        assert not isinstance(exc.value, ConnectionLost)
        await wrong_unit.close()
    finally:
        await s.stop()


# --- faults -----------------------------------------------------------------------------------------------

async def test_timeout_closes_socket_then_reconnect_works(sim, driver):
    block = driver.plan(driver.device.tags).blocks[0]
    sim.mute = True
    t0 = time.monotonic()
    with pytest.raises(CommError, match="no reply within 300 ms") as exc:
        await driver.read(block)
    elapsed = time.monotonic() - t0
    assert not isinstance(exc.value, ConnectionLost)
    assert 0.25 <= elapsed < 0.8
    assert not driver.connected                      # close_on_timeout (default)
    with pytest.raises(ConnectionLost):
        await driver.read(block)
    sim.mute = False
    await driver.connect()
    values = await driver.read(block)
    assert {n: v.value for n, v in values.items()} == {n: sim.expected()[n] for n in block.tags}


async def test_timeout_keeps_socket_without_close_on_timeout(sim):
    drv = ModbusTcpDriver(with_(sim.device, timeout_ms=200, params={"close_on_timeout": False}))
    await drv.connect()
    block = drv.plan(drv.device.tags).blocks[-1]
    sim.mute = True
    with pytest.raises(CommError) as exc:
        await drv.read(block)
    assert not isinstance(exc.value, ConnectionLost)
    assert drv.connected
    sim.mute = False
    values = await drv.read(block)                   # same connection, no reconnect
    assert {tv.quality for tv in values.values()} == {"good"}
    await drv.close()


async def test_connection_refused():
    dev = make_sim_devices(1, 5)[0].model_copy(update={"port": free_port()})
    drv = ModbusTcpDriver(dev)
    t0 = time.monotonic()
    with pytest.raises(ConnectionLost, match="connect failed"):
        await drv.connect()
    # Windows retries a refused connect twice (SYN after RST, about 1-2 s) before reporting it
    assert time.monotonic() - t0 < (3.0 if sys.platform == "win32" else 0.4)
    assert not drv.connected
    with pytest.raises(ConnectionLost):
        await drv.read(drv.plan(dev.tags).blocks[0])
    await drv.close()


async def test_connect_bounded_by_timeout():
    """A listener whose accept queue is full drops our SYN: connect() must give up at timeout_ms."""
    listener = socket.socket()
    fillers: list[socket.socket] = []
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(0)
        port = listener.getsockname()[1]
        for _ in range(4):
            s = socket.socket()
            s.setblocking(False)
            fillers.append(s)
            try:
                s.connect(("127.0.0.1", port))
            except BlockingIOError:
                pass
        await asyncio.sleep(0.05)
        drv = ModbusTcpDriver(make_sim_devices(1, 5)[0].model_copy(update={"port": port, "timeout_ms": 300}))
        t0 = time.monotonic()
        try:
            await drv.connect()
        except ConnectionLost as exc:
            assert 0.25 <= time.monotonic() - t0 < 0.8
            assert "no answer within 300 ms" in str(exc)
        else:
            await drv.close()
            pytest.skip("this kernel accepted a connection beyond the listen backlog")
    finally:
        for s in fillers:
            s.close()
        listener.close()


async def test_sim_stopped_then_restarted(sim, driver):
    block = driver.plan(driver.device.tags).blocks[0]
    await driver.read(block)
    await sim.stop()
    t0 = time.monotonic()
    with pytest.raises(ConnectionLost):
        await driver.read(block)
    assert time.monotonic() - t0 < 0.25              # no waiting for the timeout
    assert not driver.connected
    with pytest.raises(ConnectionLost):              # nothing listens while stopped
        await driver.connect()

    port = sim.port
    await sim.restart()
    assert sim.port == port and sim.device.port == port
    await driver.connect()
    assert raw(await read_all(driver)) == sim.expected()


async def test_sim_stopped_during_a_read(sim):
    drv = ModbusTcpDriver(with_(sim.device, timeout_ms=3000))
    await drv.connect()
    block = drv.plan(drv.device.tags).blocks[0]
    sim.mute = True
    task = asyncio.create_task(drv.read(block))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await sim.stop()
    with pytest.raises(ConnectionLost, match="connection closed"):
        await task
    assert time.monotonic() - t0 < 1.0               # well before the 3 s timeout
    await drv.close()


async def test_illegal_data_address_is_block_config_error(sim, driver):
    far = driver.plan([Tag(name="FAR", address="460001", dtype="uint16")]).blocks[0]
    with pytest.raises(BlockConfigError, match="exception 02"):
        await driver.read(far)
    assert driver.connected                          # the connection is fine
    good = await driver.read(driver.plan(driver.device.tags).blocks[0])
    assert {tv.quality for tv in good.values()} == {"good"}


@pytest.mark.parametrize(("code", "error"), [
    (1, BlockConfigError), (2, BlockConfigError), (3, BlockConfigError),
    (4, CommError), (6, CommError), (11, CommError),
])
async def test_exception_codes(sim, driver, code, error):
    block = driver.plan(driver.device.tags).blocks[0]
    sim.exception_code = code
    with pytest.raises(error, match=f"exception {code:02d}") as exc:
        await driver.read(block)
    assert not isinstance(exc.value, ConnectionLost)
    assert driver.connected
    sim.exception_code = None
    await driver.read(block)


async def test_short_reply_gives_bad_decode_for_missing_items_only(sim, driver):
    tags = [Tag(name="A", address="400001", dtype="uint16"), Tag(name="B", address="400002", dtype="uint16")]
    block = driver.plan(tags).blocks[0]
    assert block.count == 2
    sim.short_reply = 1
    values = await driver.read(block)
    assert values["A"].quality == "good"
    assert values["B"] == TagValue(None, "bad_decode", values["B"].source_ts)
    assert values["B"].source_ts is not None


async def test_read_rejects_foreign_blocks(driver):
    from opencap.drivers.base import Block
    with pytest.raises(TypeError):
        await driver.read(Block(tags=["X"]))


# --- identity, close ----------------------------------------------------------------------------------------

async def test_identity(sim, driver):
    assert await driver.identity() == {"vendor": "opencap", "product_code": "modbus-sim", "revision": "1.0"}
    sim.identity = {"vendor": "ACME", "product_code": "PLC-1", "revision": "2.3", "model_name": "X1"}
    assert await driver.identity() == {"vendor": "ACME", "product_code": "PLC-1", "revision": "2.3",
                                       "model_name": "X1"}
    sim.identity = None                              # device rejects FC43 with exception 01
    assert await driver.identity() == {}
    assert driver.connected


async def test_identity_never_raises(sim, driver):
    sim.mute = True
    t0 = time.monotonic()
    assert await driver.identity() == {}
    assert time.monotonic() - t0 < 0.8
    assert driver.connected                          # FC43 timeouts do not close the socket
    sim.mute = False
    await driver.read(driver.plan(driver.device.tags).blocks[0])
    await driver.close()
    assert await driver.identity() == {}             # not connected
    assert await ModbusTcpDriver(sim.device).identity() == {}   # never connected


async def test_close_is_idempotent(sim):
    drv = ModbusTcpDriver(sim.device)
    await drv.close()                                # never connected
    await drv.connect()
    await drv.connect()                              # reconnect replaces the connection
    assert drv.connected
    await drv.close()
    await drv.close()
    assert not drv.connected
    with pytest.raises(ConnectionLost, match="not connected"):
        await drv.read(drv.plan(drv.device.tags).blocks[0])


def test_driver_never_calls_a_modbus_write():
    source = inspect.getsource(modbus_tcp)
    for name in ("write_coil", "write_register", "mask_write", "readwrite_registers", "ReadWrite"):
        assert name not in source
    assert set(modbus_tcp.READ_FUNCTIONS.values()) == {
        "read_coils", "read_discrete_inputs", "read_holding_registers", "read_input_registers"}


async def test_blocks_are_modbus_blocks(driver):
    assert all(isinstance(b, ModbusBlock) for b in driver.plan(driver.device.tags).blocks)
