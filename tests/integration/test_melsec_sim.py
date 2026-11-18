"""melsec_mc driver against the MC 3E simulator (tests/helpers/mc_sim.py).

Real pymcprotocol 0.3.0 on the wire through the driver's subclass: every address form round
trip, word order, iQ-R frames, end codes, a mute PLC, power-off and power-on, a reply split
across TCP segments, UDP, and the real ChannelWorker.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

mc_sim = pytest.importorskip("tests.helpers.mc_sim")
pytest.importorskip("pymcprotocol")

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.core.cache import TagValue  # noqa: E402
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost  # noqa: E402
from opencap.drivers.melsec_mc import McBlock, MelsecMcDriver  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402

McSim = mc_sim.McSim
make_mc_tags = mc_sim.make_mc_tags
BATCH_WORDS_Q, BATCH_WORDS_IQR = (0x0401, 0x0000), (0x0401, 0x0002)

# One tag per address form (Kepware Q Series syntax).
TAGS = [
    Tag(name="FLOW_1", address="D0000002", dtype="float32"),           # the KEPServer sample device
    Tag(name="PUMP_1_RUN", address="B0000", dtype="bool"),
    Tag(name="b_hex", address="B001F", dtype="bool"),
    Tag(name="d_u16", address="D0000010", dtype="uint16"),
    Tag(name="d_i16", address="D11", dtype="int16"),
    Tag(name="d_i32", address="D12", dtype="int32"),
    Tag(name="d_u32", address="D14", dtype="uint32"),
    Tag(name="d_f64", address="D16", dtype="float64"),
    Tag(name="d_i64", address="D20", dtype="int64"),
    Tag(name="d_bit5", address="D30.05", dtype="bool"),
    Tag(name="d_bit17", address="D31.17", dtype="bool"),
    Tag(name="d_arr", address="D40[4]", dtype="int16"),
    Tag(name="d_arr2", address="D50[2][2]", dtype="float32"),
    Tag(name="d_str_h", address="DSH60.010", dtype="string"),
    Tag(name="d_str_l", address="DSL70:006", dtype="string"),
    Tag(name="m10", address="M10", dtype="bool"),
    Tag(name="m_word", address="M32", dtype="uint16"),
    Tag(name="x1a", address="X1A", dtype="bool"),
    Tag(name="x_dword", address="X0020", dtype="int32"),
    Tag(name="y7", address="Y7", dtype="bool"),
    Tag(name="dx3", address="DX3", dtype="bool"),
    Tag(name="sb1", address="SB1", dtype="bool"),
    Tag(name="sm400", address="SM400", dtype="bool"),
    Tag(name="l5", address="L5", dtype="bool"),
    Tag(name="f3", address="F3", dtype="bool"),
    Tag(name="v2", address="V2", dtype="bool"),
    Tag(name="ts1", address="TS1", dtype="bool"),
    Tag(name="cc2", address="CC2", dtype="bool"),
    Tag(name="w1f", address="W1F", dtype="uint16"),
    Tag(name="w_bit", address="W20.15", dtype="bool"),
    Tag(name="w_str", address="WSH30.004", dtype="string"),
    Tag(name="sw10", address="SW10", dtype="uint16"),
    Tag(name="sd100", address="SD100", dtype="int16"),
    Tag(name="r_f32", address="R100", dtype="float32"),
    Tag(name="zr10", address="ZR10", dtype="uint16"),
    Tag(name="tn5", address="TN5", dtype="uint16"),
    Tag(name="cn5", address="CN5", dtype="int16"),
]


def _nonnormal(x: float) -> bool:
    return not math.isfinite(x) or (x != 0.0 and abs(x) < 2.2250738585072014e-308) or \
        (x != 0.0 and abs(struct.unpack("<f", struct.pack("<f", x))[0]) < 1.1754943508222875e-38)


def expected(sim: Any, tag: Tag, *, first_word_low: bool = True) -> Any:
    """What the driver must report for `tag`, read from the simulator's own encoder (Kepware
    "Replace with Zero" applied to NaN/Inf/denormal floats)."""
    order = "low_first" if first_word_low else "high_first"
    value = sim.value(tag.address, tag.dtype, word_order=order)

    def fix(v: Any) -> Any:
        if isinstance(v, list):
            return [fix(x) for x in v]
        if isinstance(v, float) and _nonnormal(v):
            return 0.0
        return v

    return fix(value)


def assert_values(sim: Any, tags: list[Tag], out: dict[str, TagValue], *, first_word_low: bool = True) -> None:
    bad = {t.name: out[t.name].quality for t in tags if out[t.name].quality != "good"}
    assert bad == {}
    for t in tags:
        want, got = expected(sim, t, first_word_low=first_word_low), out[t.name].value
        if isinstance(want, float):
            assert got == pytest.approx(want, rel=1e-6), t.name
        else:
            assert got == want and type(got) is type(want), t.name


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mc-sim-exec")
    yield ex
    ex.shutdown(wait=True)


def make_driver(sim: Any, tags: list[Tag], executor: ThreadPoolExecutor, *, timeout_ms: int = 1000,
                **params: Any) -> MelsecMcDriver:
    dev = Device(id="SIM/QCPU", channel="SIM", name="QCPU", driver="melsec_mc", host=sim.host, port=sim.port,
                 params={"plc_type": "Q", "protocol": "tcp", "first_word_low": True, "frame": "3E", **params},
                 tags=tags, timeout_ms=timeout_ms)
    return MelsecMcDriver(dev, executor)


def blocks(drv: MelsecMcDriver) -> list[McBlock]:
    plan = drv.plan(drv.device.tags)
    assert plan.invalid == {}
    return [b for b in plan.blocks if isinstance(b, McBlock)]


async def read_all(drv: MelsecMcDriver) -> dict[str, TagValue]:
    out: dict[str, TagValue] = {}
    for block in blocks(drv):
        out.update(await drv.read(block))
    return out


async def start_sim(**kw: Any) -> Any:
    return await McSim.start({"SS": 64, "SC": 64, "SN": 64}, **kw)


async def wait_for(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


# --- reads ------------------------------------------------------------------------------------------

async def test_every_address_form_round_trips(executor) -> None:
    async with await start_sim() as sim:
        sim.fill(TAGS, seed=5)
        sim.set("D0000002", 12.5, "float32")
        sim.set("B001F", True, "bool")
        sim.set("D31.17", True, "bool")
        sim.set("W20.15", True, "bool")
        sim.set("DSH60.010", "PUMP A", "string")
        sim.set("DSL70:006", "ok", "string")
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        sim.reset_counters()
        out = await read_all(drv)
        assert_values(sim, TAGS, out)
        assert out["FLOW_1"].value == 12.5 and out["b_hex"].value is True and out["d_bit17"].value is True
        assert (out["d_str_h"].value, out["d_str_l"].value, out["w_bit"].value) == ("PUMP A", "ok", True)
        assert sim.request_count == len(blocks(drv)) == sim.command_counts[BATCH_WORDS_Q]   # one request per block
        sim.set("D0000002", -3.75, "float32")
        sim.set("M10", not out["m10"].value, "bool")
        again = await read_all(drv)
        assert again["FLOW_1"].value == -3.75 and again["m10"].value is (not out["m10"].value)
        await drv.close()
        assert await wait_for(lambda: sim.clients == 0)
        assert sim.write_attempts == 0


async def test_first_word_low_off(executor) -> None:
    async with await start_sim() as sim:
        sim.fill(TAGS, seed=8, word_order="high_first")
        drv = make_driver(sim, TAGS, executor, first_word_low=False)
        await drv.connect()
        assert_values(sim, TAGS, await read_all(drv), first_word_low=False)
        await drv.close()


async def test_iqr_frames(executor) -> None:
    async with await start_sim() as sim:
        sim.fill(TAGS, seed=9)
        drv = make_driver(sim, TAGS, executor, plc_type="iQ-R")
        await drv.connect()
        sim.reset_counters()
        assert_values(sim, TAGS, await read_all(drv))
        assert sim.command_counts[BATCH_WORDS_IQR] == len(blocks(drv)) and sim.command_counts[BATCH_WORDS_Q] == 0
        await drv.close()


async def test_full_960_word_block_split_across_tcp_segments(executor) -> None:
    """A 960-word reply (1,931 bytes) arriving in two writes 50 ms apart reads correctly (stock
    pymcprotocol decodes the second half as zeros, tests/unit/test_mc_sim.py)."""
    tags = [Tag(name="big", address="D1000[960]", dtype="uint16"), Tag(name="f", address="D0000002", dtype="float32")]
    async with await start_sim() as sim:
        sim.fill(tags, seed=2)
        sim.split_replies = True
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        assert [b.count for b in blocks(drv)] == [2, 960]
        assert_values(sim, tags, await read_all(drv))
        await drv.close()


async def test_identity(executor) -> None:
    async with await McSim.start(cpu_type="Q06UDVCPU", cpu_code=0x0367) as sim:
        drv = make_driver(sim, TAGS[:1], executor)
        assert await drv.identity() == {}
        await drv.connect()
        assert await drv.identity() == {"plc_type": "Q", "protocol": "tcp", "frame": "3E", "commtype": "binary",
                                        "cpu_type": "Q06UDVCPU", "cpu_code": "0367"}
        sim.cpu_model_end_code = 0xC059
        ident = await drv.identity()
        assert "0xC059" in ident["cpu_type_error"] and "cpu_type" not in ident
        assert drv.connected                                         # an end code does not close
        await drv.close()


# --- end codes ---------------------------------------------------------------------------------------

async def test_block_past_the_plc_device_size_is_rejected(executor) -> None:
    tags = [Tag(name="ok", address="D10", dtype="uint16"), Tag(name="past", address="D500", dtype="uint16")]
    async with await McSim.start({"D": 100}) as sim:
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        bl = {b.describe(): b for b in blocks(drv)}
        with pytest.raises(BlockConfigError, match=r"MC end code 0xC056 \(read/write request exceeds"):
            await drv.read(bl["D500..D500 (1 words)"])
        assert drv.connected
        assert (await drv.read(bl["D10..D10 (1 words)"]))["ok"].quality == "good"
        await drv.close()


@pytest.mark.parametrize(("code", "error"), [
    (0xC056, BlockConfigError), (0x4031, BlockConfigError), (0xC05B, BlockConfigError),
    (0xC059, BlockConfigError), (0xC061, CommError), (0x4010, CommError),
])
async def test_end_code_mapping(executor, code: int, error: type) -> None:
    async with await start_sim() as sim:
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        d_block = next(b for b in blocks(drv) if b.device == "D")
        sim.end_codes["D"] = code
        with pytest.raises(error) as info:
            await drv.read(d_block)
        assert type(info.value) is error and f"{code:#06X}".replace("0X", "0x") in str(info.value)
        assert drv.connected
        del sim.end_codes["D"]
        sim.fill(TAGS, seed=1)
        assert_values(sim, TAGS, await read_all(drv))
        await drv.close()


# --- faults -----------------------------------------------------------------------------------------

async def test_mute_plc_gives_comm_error_then_recovers(executor) -> None:
    async with await start_sim() as sim:
        drv = make_driver(sim, TAGS, executor, timeout_ms=300)
        await drv.connect()
        block = blocks(drv)[0]
        sim.mute = True
        started = time.monotonic()
        with pytest.raises(CommError) as info:
            await drv.read(block)
        assert not isinstance(info.value, ConnectionLost)
        assert "no reply within 300 ms; socket closed" in str(info.value)
        assert time.monotonic() - started < 1.0
        assert not drv.connected                                     # a late reply must not be taken as the next one
        sim.mute = False
        await drv.connect()
        sim.fill(TAGS, seed=3)
        assert_values(sim, TAGS, await read_all(drv))
        await drv.close()


async def test_power_off_gives_connection_lost_and_power_on_reconnects(executor) -> None:
    sim = await start_sim(port=stable_port())
    try:
        sim.fill(TAGS, seed=4)
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        assert_values(sim, TAGS, await read_all(drv))
        await sim.stop()
        started = time.monotonic()
        with pytest.raises(ConnectionLost):
            await drv.read(blocks(drv)[0])
        assert time.monotonic() - started < 1.0
        assert not drv.connected
        with pytest.raises(ConnectionLost, match="connect failed"):
            await drv.connect()
        await sim.restart()
        await drv.connect()
        sim.set("D0000002", 99.5, "float32")
        out = await read_all(drv)
        assert_values(sim, TAGS, out)
        assert out["FLOW_1"].value == 99.5
        await drv.close()
    finally:
        await sim.stop()


@pytest.mark.usefixtures("no_gc")
async def test_udp(executor) -> None:
    sim = await start_sim(port=stable_port())
    try:
        sim.fill(TAGS, seed=6)
        drv = make_driver(sim, TAGS, executor, timeout_ms=1000, protocol="udp")   # CI runners stall
        await drv.connect()
        sim.reset_counters()
        assert_values(sim, TAGS, await read_all(drv))
        assert sim.frames["udp"] == len(blocks(drv)) and sim.frames["tcp"] == 0
        assert (await drv.identity())["protocol"] == "udp"

        sim.mute = True                                              # no answer: timeout, new socket next time
        with pytest.raises(CommError, match="no reply within 1000 ms") as info:
            await drv.read(blocks(drv)[0])
        assert not isinstance(info.value, ConnectionLost) and not drv.connected
        sim.mute = False
        await drv.connect()
        assert_values(sim, TAGS, await read_all(drv))

        await sim.stop()                                             # port closed: ICMP port unreachable
        with pytest.raises(ConnectionLost, match="connection lost"):
            await drv.read(blocks(drv)[0])
        await sim.restart()
        await drv.connect()
        assert_values(sim, TAGS, await read_all(drv))
        await drv.close()
    finally:
        await sim.stop()


async def test_synthetic_tags_plan_and_read(executor) -> None:
    tags = make_mc_tags(150, seed=11)
    async with await start_sim() as sim:
        sim.fill(tags, seed=11)
        drv = make_driver(sim, tags, executor)
        plan = drv.plan(tags)
        assert plan.invalid == {}
        assert {b.device for b in plan.blocks} >= {"D", "M", "B", "X", "Y", "W", "R", "ZR"}
        assert len(plan.blocks) < len(tags) / 3
        await drv.connect()
        sim.reset_counters()
        assert_values(sim, tags, await read_all(drv))
        assert sim.request_count == len(plan.blocks)
        await drv.close()


# --- through the real ChannelWorker ---------------------------------------------------------------

async def test_channel_worker_end_to_end(executor) -> None:
    """The core drives the driver: cycles, a rejected block, a mute PLC, a power cycle."""
    from opencap.config.models import Channel
    from opencap.core.cache import ValueCache
    from opencap.core.channel_worker import ChannelWorker
    from opencap.core.registry import plan_device

    # 3 blocks (D, B, M): a mute PLC costs blocks x (retries + 1) x timeout per cycle (D-015).
    live = [t for t in TAGS if t.name in ("FLOW_1", "d_i32", "PUMP_1_RUN", "b_hex", "m10", "m_word")]
    tags = live + [Tag(name="ghost", address="D9000", dtype="uint16")]       # past the PLC's D size
    sim = await McSim.start({"D": 1000, "SS": 64, "SC": 64, "SN": 64}, port=stable_port())
    device = make_driver(sim, tags, executor, timeout_ms=300).device
    channel = Channel(name="SIM", devices=[device])
    worker = ChannelWorker(channel, {device.id: plan_device(device)}, ValueCache(), start_jitter=0.0,
                           backoff_base_s=0.05, backoff_max_s=0.2)
    try:
        sim.fill(live, seed=12)
        snap = await worker.read_once(device.id)
        assert snap["ghost"].quality == "bad_config"
        assert_values(sim, live, snap)
        assert worker.device_status(device.id)["connected"] is True

        sim.mute = True
        snap = await worker.read_once(device.id)
        assert {snap[t.name].quality for t in live} == {"bad_comm"}
        sim.mute = False
        assert_values(sim, live, await worker.read_once(device.id))

        await sim.stop()
        snap = await worker.read_once(device.id)
        assert {snap[t.name].quality for t in live} == {"bad_comm"}
        await sim.restart()
        sim.reset_counters()
        sim.set("D0000002", -7.25, "float32")
        snap = await worker.read_once(device.id)
        assert_values(sim, live, snap)
        assert snap["FLOW_1"].value == -7.25 and snap["ghost"].quality == "bad_config"
        assert sim.request_count == len(blocks(make_driver(sim, live, executor)))   # the rejected block is not re-sent
        assert sim.write_attempts == 0
    finally:
        await worker.stop()
        await sim.stop()
