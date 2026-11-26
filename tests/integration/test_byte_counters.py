"""Wire byte counters (#130): `Driver.counts_bytes`, the drivers that count, and the core metrics.

Expected counts are computed here from the protocol frame layouts, not taken from the driver:

Modbus TCP read (FC01/02/03/04): request = MBAP 7 + PDU (fc 1 + address 2 + count 2) = 12 bytes;
  register reply = 7 + fc 1 + byte count 1 + 2 per register; bit reply = 7 + 1 + 1 + ceil(bits / 8).
S7 read var, one item: request = TPKT 4 + COTP DT 3 + S7 header 10 + parameter 14 = 31 bytes;
  reply = TPKT 4 + COTP DT 3 + S7 ack header 12 + parameter 2 + data item header 4 + data bytes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from opencap.config.models import Device, Tag
from opencap.core.cache import ValueCache
from opencap.core.channel_worker import ChannelWorker
from opencap.drivers import DRIVERS, get_driver_class
from opencap.drivers.base import BlockConfigError, ByteCounter, CommError, Driver
from opencap.drivers.modbus_tcp import ModbusTcpDriver
from opencap.metrics import REGISTRY
from tests.helpers.fake_driver import FakeDriver, FakeFactory, channel, device, fake_plans, tags, uniq
from tests.helpers.modbus_sim import ModbusSim

MODBUS_READ_REQUEST = 12
S7_READ_REQUEST = 31


def modbus_register_reply(registers: int) -> int:
    return 7 + 1 + 1 + 2 * registers


def modbus_bit_reply(bits: int) -> int:
    return 7 + 1 + 1 + (bits + 7) // 8


def s7_read_reply(nbytes: int) -> int:
    return 4 + 3 + 12 + 2 + 4 + nbytes


# --- the base contract -------------------------------------------------------------------------

def test_byte_counter_take_resets() -> None:
    c = ByteCounter()
    c.add_tx(12)
    c.add_rx(15)
    c.add_tx(1)
    assert c.take() == (13, 15)
    assert c.take() == (0, 0)


def test_only_modbus_tcp_and_s7_count_bytes() -> None:
    assert Driver.counts_bytes is False
    counting = set()
    for name in DRIVERS:
        try:
            cls = get_driver_class(name)
        except LookupError:      # protocol library not installed
            continue
        if cls.counts_bytes:
            counting.add(name)
    assert counting <= {"modbus_tcp", "s7"}
    assert "modbus_tcp" in counting


def test_a_driver_that_does_not_count_reports_zero() -> None:
    drv = FakeDriver(device("X", "PLC", tags("A", 1)))
    drv.byte_counter.add_tx(5)
    assert drv.take_byte_counts() == (0, 0)


# --- modbus_tcp ----------------------------------------------------------------------------------

def modbus_device(tag_list: list[Tag]) -> Device:
    return Device(id="T/PLC", channel="T", name="PLC", driver="modbus_tcp", host="127.0.0.1", port=0,
                  timeout_ms=300, tags=tag_list)


async def test_modbus_counts_frame_bytes_exactly() -> None:
    regs = [Tag(name="a", address="400001", dtype="uint16"), Tag(name="b", address="400002", dtype="float32")]
    bits = [Tag(name=f"c{i}", address=f"{i + 1:06d}", dtype="bool") for i in range(16)]
    sim = await ModbusSim.start(modbus_device(regs + bits))
    drv = ModbusTcpDriver(sim.device)
    try:
        await drv.connect()
        assert drv.take_byte_counts() == (0, 0)            # connecting sends nothing at the Modbus level
        plan = drv.plan(drv.device.tags)
        assert plan.invalid == {}
        by_fc = {b.fc: b for b in plan.blocks}
        assert set(by_fc) == {1, 3} and by_fc[3].count == 3 and by_fc[1].count == 16
        await drv.read(by_fc[3])
        assert drv.take_byte_counts() == (MODBUS_READ_REQUEST, modbus_register_reply(3))
        for _ in range(4):
            await drv.read(by_fc[1])
        assert drv.take_byte_counts() == (4 * MODBUS_READ_REQUEST, 4 * modbus_bit_reply(16))
        assert drv.take_byte_counts() == (0, 0)
    finally:
        await drv.close()
        await sim.stop()


async def test_modbus_failed_request_still_counts_the_bytes_sent() -> None:
    sim = await ModbusSim.start(modbus_device([Tag(name="a", address="400001", dtype="uint16")]))
    drv = ModbusTcpDriver(sim.device)
    try:
        await drv.connect()
        block = drv.plan(drv.device.tags).blocks[0]
        sim.mute = True
        with pytest.raises(CommError):
            await drv.read(block)
        assert drv.take_byte_counts() == (MODBUS_READ_REQUEST, 0)
    finally:
        await drv.close()
        await sim.stop()


async def test_modbus_exception_reply_counts_both_ways() -> None:
    sim = await ModbusSim.start(modbus_device([Tag(name="a", address="400001", dtype="uint16")]))
    drv = ModbusTcpDriver(sim.device)
    try:
        await drv.connect()
        block = drv.plan(drv.device.tags).blocks[0]
        sim.exception_code = 2
        with pytest.raises(BlockConfigError):
            await drv.read(block)
        assert drv.take_byte_counts() == (MODBUS_READ_REQUEST, 7 + 1 + 1)   # MBAP + fc|0x80 + exception code
    finally:
        await drv.close()
        await sim.stop()


# --- s7 ----------------------------------------------------------------------------------------------

s7_sim = pytest.importorskip("tests.helpers.s7_sim")


def s7_driver(sim, tag_list: list[Tag], executor: ThreadPoolExecutor):
    from opencap.drivers.s7 import S7Driver

    dev = Device(id="SIM/S7", channel="SIM", name="S7", driver="s7", host=sim.host, port=sim.port,
                 params={"rack": 0, "slot": 1, "max_pdu": 480}, timeout_ms=300, tags=tag_list)
    return S7Driver(dev, executor)


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="byte-count-exec")
    yield ex
    ex.shutdown(wait=True)


async def test_s7_counts_tpkt_frame_bytes_exactly(executor) -> None:
    tag_list = [Tag(name="w", address="MW20", dtype="uint16"), Tag(name="x", address="MW22", dtype="int16"),
                Tag(name="r", address="DB100,D04", dtype="float32")]
    async with await s7_sim.S7Sim.start({"M": 64, "DB100": 32}) as sim:
        drv = s7_driver(sim, tag_list, executor)
        await drv.connect()
        assert drv.take_byte_counts() == (0, 0)             # the handshake is not counted
        plan = drv.plan(drv.device.tags)
        assert plan.invalid == {}
        assert sorted(b.nbytes for b in plan.blocks) == [4, 4]   # MW20..MW23 and DB100 D04: one request each
        for block in plan.blocks:
            await drv.read(block)
        assert drv.take_byte_counts() == (2 * S7_READ_REQUEST, 2 * s7_read_reply(4))
        await drv.close()
        assert drv.take_byte_counts() == (0, 0)             # nor is the disconnect request


async def test_s7_failed_request_still_counts_the_bytes_sent(executor) -> None:
    async with await s7_sim.S7Sim.start({"M": 64}) as sim:
        drv = s7_driver(sim, [Tag(name="w", address="MW20", dtype="uint16")], executor)
        await drv.connect()
        block = drv.plan(drv.device.tags).blocks[0]
        sim.mute = True
        with pytest.raises(CommError):
            await drv.read(block)
        assert drv.take_byte_counts() == (S7_READ_REQUEST, 0)
        await drv.close()


# --- the core ----------------------------------------------------------------------------------------

class CountingFake(FakeDriver):
    """Fake that "sends" 10 bytes and "receives" 25 per good read, and only sends when the read fails."""

    counts_bytes = True

    async def read(self, block):  # type: ignore[override]
        self.byte_counter.add_tx(10)
        out = await super().read(block)
        self.byte_counter.add_rx(25)
        return out


class CountingFactory(FakeFactory):
    def __call__(self, device, executor):
        drv = CountingFake(device, executor, script=self.script(device.id), probes=self.probes)
        self.drivers[device.id] = drv
        return drv


def sample(name: str, device_id: str) -> float:
    return REGISTRY.get_sample_value(name, {"device": device_id}) or 0.0


async def test_core_feeds_the_byte_metrics_per_device_including_failed_requests() -> None:
    name = uniq()
    dev = device(name, "PLC", tags("A", 2), retries=1)
    ch = channel(name, [dev])
    factory = CountingFactory()
    worker = ChannelWorker(ch, fake_plans(ch), ValueCache(), driver_factory=factory, start_jitter=0.0)
    try:
        await worker.read_once(dev.id)
        assert (sample("poll_tx_bytes_total", dev.id), sample("poll_rx_bytes_total", dev.id)) == (10, 25)
        factory.script(dev.id).fail_next(CommError("boom"), times=2)   # the attempt and its retry both fail
        await worker.read_once(dev.id)
        assert (sample("poll_tx_bytes_total", dev.id), sample("poll_rx_bytes_total", dev.id)) == (30, 25)
    finally:
        await worker.stop()


async def test_core_ignores_drivers_that_do_not_count() -> None:
    name = uniq()
    dev = device(name, "PLC", tags("A", 2))
    ch = channel(name, [dev])
    worker = ChannelWorker(ch, fake_plans(ch), ValueCache(), driver_factory=FakeFactory(), start_jitter=0.0)
    try:
        await worker.read_once(dev.id)
        assert sample("poll_tx_bytes_total", dev.id) == 0.0 and sample("poll_rx_bytes_total", dev.id) == 0.0
    finally:
        await worker.stop()
