"""s7 driver against the S7 simulator (tests/helpers/s7_sim.py, built on snap7's own server).

Real python-snap7 3.x on the wire: one round trip per address form, both byte orders, PDU
negotiation (planned 960, negotiated 480: split reads), the TSAP (rack/slot/link type), SZL
identity, item return codes, a mute PLC, power-off and power-on, and the real ChannelWorker.
"""

from __future__ import annotations

import asyncio
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

s7_sim = pytest.importorskip("tests.helpers.s7_sim")

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.core.cache import TagValue  # noqa: E402
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost  # noqa: E402
from opencap.drivers.s7 import S7Block, S7Driver  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402

S7Sim = s7_sim.S7Sim
AREAS = {"M": 512, "I": 32, "Q": 32, "DB1": 256, "DB100": 64, "T": 16, "C": 16}

# One tag per address form, with the value the sim stores (encoded by the sim's own codec).
FORM_VALUES: list[tuple[str, str, Any]] = [
    ("MD232", "float32", 12.5),
    ("MD236", "uint32", 4_000_000_000),
    ("MD240", "int32", -40_000),
    ("MW20", "uint16", 0xBEEF),
    ("MW22", "int16", -1234),
    ("MB7", "uint16", 200),
    ("MB8", "int16", -100),
    ("M10.3", "bool", True),
    ("MX14.6", "bool", True),
    ("DB100,D04", "float32", -3.25),
    ("DB100,D016", "float32", 1e6),
    ("DB100,REAL8", "float32", 0.5),
    ("DB100,DBD20", "float32", 99.75),
    ("DB100,DBW24", "int16", -2),
    ("DB100,DBB26", "uint16", 0xAB),
    ("DB100,DBX27.5", "bool", True),
    ("DB100,W28", "uint16", 0x1234),
    ("DB100,B30", "uint16", 0x7F),
    ("DB100,X31.0", "bool", True),
    ("DB100.DBD32", "float32", 7.125),
    ("DB100.DBW36", "uint16", 65535),
    ("DB100.DBB38", "uint16", 1),
    ("DB100.DBX39.7", "bool", True),
    ("DB100DBD40", "float32", -0.125),
    ("DB1,DI0", "int32", -7),
    ("DB1,DINT4", "int32", 2_000_000_000),
    ("DB1,INT8", "int16", -32768),
    ("DB1,WORD10", "uint16", 40000),
    ("DB1,DWORD12", "uint32", 0xCAFEBABE),
    ("DB1,BYTE16", "uint16", 255),
    ("DB1,CHAR17", "int16", -1),
    ("DB1,LREAL24", "float64", -1.0e-3),
    ("DB1,LINT32", "int64", -(2**40)),
    ("DB1,LWORD40", "uint64", 2**63 + 1),
    ("DB1,W48.15", "bool", True),
    ("DB1,D52.31", "bool", True),
    ("DB1,STRING60.20", "string", "PUMP 1 RUN"),
    ("DB1,B90.12", "string", "RAW TEXT"),
    ("IW0", "int16", -5),
    ("EW2", "uint16", 6),
    ("I4.1", "bool", True),
    ("IREAL8", "float32", 3.5),
    ("QB1", "uint16", 9),
    ("A2.2", "bool", True),
    ("FD26", "float32", 2.25),
    ("C0", "uint16", 42),
    ("Z3", "int16", 999),
    ("T0", "uint32", 25_000),
    ("T5", "int32", 9_990_000),
]


def form_tags() -> list[Tag]:
    return [Tag(name=f"t{i:02d}_{a.replace(',', '_').replace('.', '_')}", address=a, dtype=d)
            for i, (a, d, _v) in enumerate(FORM_VALUES)]


def load_forms(sim: Any, *, big: bool = True) -> None:
    sim.set("DB1,STRING60.20", "string", "A LONGER OLD VALUE")      # stale characters after the actual length
    for a, d, v in FORM_VALUES:
        sim.set(a, d, v, big=big)


def assert_forms(out: dict[str, TagValue], tags: list[Tag]) -> None:
    bad = {t.name: out[t.name].quality for t in tags if out[t.name].quality != "good"}
    assert bad == {}
    for tag, (_a, _d, want) in zip(tags, FORM_VALUES):
        got = out[tag.name].value
        if isinstance(want, float):
            assert math.isclose(got, want, rel_tol=1e-6), (tag.address, got, want)
        else:
            assert got == want and type(got) is type(want), (tag.address, got, want)


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s7-sim-exec")
    yield ex
    ex.shutdown(wait=True)


def make_driver(sim: Any, tags: list[Tag], executor: ThreadPoolExecutor, *, timeout_ms: int = 1500,
                **params: Any) -> S7Driver:
    params = {"rack": 0, "slot": 1, "max_pdu": 480, "link_type": 3, "byte_order_big": True, **params}
    dev = Device(id="SIM/S7", channel="SIM", name="S7", driver="s7", host=sim.host, port=sim.port,
                 params=params, tags=tags, timeout_ms=timeout_ms)
    return S7Driver(dev, executor)


def blocks(drv: S7Driver) -> list[S7Block]:
    plan = drv.plan(drv.device.tags)
    assert plan.invalid == {}
    return [b for b in plan.blocks if isinstance(b, S7Block)]


async def read_all(drv: S7Driver) -> dict[str, TagValue]:
    out: dict[str, TagValue] = {}
    for block in blocks(drv):
        out.update(await drv.read(block))
    return out


# --- reads ---------------------------------------------------------------------------------------

async def test_every_address_form_round_trips(executor) -> None:
    tags = form_tags()
    async with await S7Sim.start(AREAS) as sim:
        load_forms(sim)
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        sim.reset_counters()
        out = await read_all(drv)
        assert_forms(out, tags)
        assert sim.read_count == len(blocks(drv)) == 8            # M (2), I, Q, DB1, DB100, T, C: one request each
        sim.set("DB100,D04", "float32", 42.0)
        sim.set("M10.3", "bool", False)
        out = await read_all(drv)
        assert out[tags[9].name].value == 42.0 and out[tags[7].name].value is False
        await drv.close()
        assert sim.write_attempts == 0


async def test_little_endian_device(executor) -> None:
    tags = form_tags()
    async with await S7Sim.start(AREAS) as sim:
        load_forms(sim, big=False)
        drv = make_driver(sim, tags, executor, byte_order_big=False)
        await drv.connect()
        assert_forms(await read_all(drv), tags)
        await drv.close()


async def test_pdu_negotiation_splits_blocks_planned_for_a_bigger_pdu(executor) -> None:
    """KEPServer says max_pdu 960, the CPU negotiates 480: a 900-byte block needs two requests,
    and the sim refuses a single 900-byte read, as a real CPU does."""
    tags = [Tag(name=f"r{i}", address=f"DB1,REAL{4 * i}", dtype="float32") for i in range(225)]   # 900 bytes
    async with await S7Sim.start({"DB1": 1024}, max_pdu=480) as sim:
        for i in range(225):
            sim.set(f"DB1,REAL{4 * i}", "float32", i + 0.5)
        drv = make_driver(sim, tags, executor, max_pdu=960)
        assert len(blocks(drv)) == 1 and blocks(drv)[0].nbytes == 900
        await drv.connect()
        assert drv.negotiated_pdu == 480 and sim.pdu_requests == [960]
        sim.reset_counters()
        out = await read_all(drv)
        assert [out[f"r{i}"].value for i in range(225)] == [i + 0.5 for i in range(225)]
        assert sim.read_count == 2 and sim.read_sizes == [462, 438] and sim.oversize_reads == 0
        ident = await drv.identity()
        assert (ident["pdu_requested"], ident["pdu_negotiated"]) == (960, 480)
        await drv.close()


async def test_planned_pdu_that_fits_is_one_request(executor) -> None:
    tags = [Tag(name=f"r{i}", address=f"DB1,REAL{4 * i}", dtype="float32") for i in range(225)]
    async with await S7Sim.start({"DB1": 1024}, max_pdu=960) as sim:
        drv = make_driver(sim, tags, executor, max_pdu=960)
        await drv.connect()
        sim.reset_counters()
        out = await read_all(drv)
        assert all(tv.quality == "good" for tv in out.values())
        assert sim.read_count == 1 and sim.read_sizes == [900]
        await drv.close()


@pytest.mark.parametrize(("link_type", "ctype"), [(None, 3), (3, 3), (1, 1), (2, 2), ("PG", 1)])
async def test_tsap_uses_rack_slot_and_link_type(executor, link_type: Any, ctype: int) -> None:
    async with await S7Sim.start({"DB1": 16}, rack=0, slot=2) as sim:
        drv = make_driver(sim, [Tag(name="w", address="DB1,W0", dtype="uint16")], executor, slot=2,
                          link_type=link_type)
        await drv.connect()
        assert sim.tsaps == [(ctype, 0, 2)]
        await drv.close()


async def test_wrong_slot_is_a_failed_connect(executor) -> None:
    async with await S7Sim.start({"DB1": 16}, rack=0, slot=2) as sim:
        drv = make_driver(sim, [Tag(name="w", address="DB1,W0", dtype="uint16")], executor, slot=1)
        with pytest.raises(ConnectionLost, match="connect failed"):
            await drv.connect()
        assert not drv.connected and sim.rejected_tsaps


async def test_identity(executor) -> None:
    async with await S7Sim.start({"DB1": 16}) as sim:
        drv = make_driver(sim, [Tag(name="w", address="DB1,W0", dtype="uint16")], executor)
        assert await drv.identity() == {}
        await drv.connect()
        ident = await drv.identity()
        assert ident["module_type_name"] == "CPU 315-2 PN/DP" and ident["order_code"].startswith("6ES7 315")
        assert ident["firmware"] == "V3.3.0" and ident["cpu_state"] == "run" and ident["serial_number"]
        assert (ident["connection_type"], ident["rack"], ident["slot"], ident["byte_order"]) == \
            ("PC (S7 basic)", 0, 1, "big")
        assert sim.szl_count == 3 and sim.write_attempts == 0
        assert drv.connected
        await drv.close()


# --- PLC errors -----------------------------------------------------------------------------------

async def test_missing_db_and_out_of_range_are_block_config_errors(executor) -> None:
    tags = [Tag(name="ok", address="DB1,W0", dtype="uint16"), Tag(name="ghost", address="DB9,W0", dtype="uint16"),
            Tag(name="past_end", address="DB1,REAL300", dtype="float32"), Tag(name="m", address="MW0", dtype="uint16")]
    async with await S7Sim.start({"DB1": 64, "M": 16}) as sim:
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        by_tag = {b.tags[0]: b for b in blocks(drv)}
        with pytest.raises(BlockConfigError, match=r"0x0A \(object does not exist\)"):
            await drv.read(by_tag["ghost"])
        with pytest.raises(BlockConfigError, match=r"0x05 \(address out of range\)"):
            await drv.read(by_tag["past_end"])
        assert drv.connected
        assert (await drv.read(by_tag["ok"]))["ok"].quality == "good"
        sim.item_errors[("M", 0)] = 0x03
        with pytest.raises(BlockConfigError, match="accessing the object not allowed"):
            await drv.read(by_tag["m"])
        sim.item_errors[("M", 0)] = 0x01
        with pytest.raises(CommError, match="hardware fault") as info:
            await drv.read(by_tag["m"])
        assert not isinstance(info.value, (ConnectionLost, BlockConfigError)) and drv.connected
        await drv.close()


@pytest.mark.parametrize(("code", "error"), [(0x8104, BlockConfigError), (0x8304, CommError)])
async def test_header_errors(executor, code: int, error: type) -> None:
    async with await S7Sim.start({"DB1": 16}) as sim:
        drv = make_driver(sim, [Tag(name="w", address="DB1,W0", dtype="uint16")], executor)
        await drv.connect()
        sim.header_error = code
        with pytest.raises(error) as info:
            await drv.read(blocks(drv)[0])
        assert type(info.value) is error and drv.connected
        sim.header_error = None
        assert (await drv.read(blocks(drv)[0]))["w"].quality == "good"
        await drv.close()


async def test_malformed_reply_is_comm_error_and_drops_the_connection(executor) -> None:
    async with await S7Sim.start({"DB1": 16}) as sim:
        drv = make_driver(sim, [Tag(name="w", address="DB1,W0", dtype="uint16")], executor)
        await drv.connect()
        sim.short_reply = True
        with pytest.raises(CommError, match="bad reply") as info:
            await drv.read(blocks(drv)[0])
        assert not isinstance(info.value, ConnectionLost) and not drv.connected
        sim.short_reply = False
        await drv.connect()
        assert (await drv.read(blocks(drv)[0]))["w"].quality == "good"
        await drv.close()


# --- faults --------------------------------------------------------------------------------------

async def test_mute_plc_gives_comm_error_then_recovers(executor) -> None:
    tags = form_tags()
    async with await S7Sim.start(AREAS) as sim:
        load_forms(sim)
        drv = make_driver(sim, tags, executor, timeout_ms=300)
        await drv.connect()
        sim.mute = True
        started = time.monotonic()
        with pytest.raises(CommError) as info:
            await drv.read(blocks(drv)[0])
        assert not isinstance(info.value, ConnectionLost)
        assert "no reply within 300 ms" in str(info.value)
        assert time.monotonic() - started < 1.5
        assert not drv.connected                                 # a late reply must not be taken for the next one
        sim.mute = False
        await drv.connect()
        assert_forms(await read_all(drv), tags)
        await drv.close()


async def test_connect_timeout_is_the_device_timeout(executor) -> None:
    """A port that accepts TCP but never confirms the ISO connection: the connect gives up after
    timeout_ms (snap7's own default would wait 5 s)."""
    async def silent(reader, writer):
        await asyncio.sleep(10)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        dev = Device(id="SIM/S7", channel="SIM", name="S7", driver="s7", host="127.0.0.1", port=port,
                     params={}, tags=[], timeout_ms=300)
        drv = S7Driver(dev, executor)
        started = time.monotonic()
        with pytest.raises(ConnectionLost, match="connect failed"):
            await drv.connect()
        assert time.monotonic() - started < 1.5
    finally:
        server.close()


async def test_refused_connect(executor) -> None:
    from tests.helpers.modbus_sim import free_port

    dev = Device(id="SIM/S7", channel="SIM", name="S7", driver="s7", host="127.0.0.1", port=free_port(),
                 params={}, tags=[], timeout_ms=500)
    with pytest.raises(ConnectionLost, match="connect failed"):
        await S7Driver(dev, executor).connect()


async def test_power_off_gives_connection_lost_and_power_on_reconnects(executor) -> None:
    tags = form_tags()
    sim = await S7Sim.start(AREAS, port=stable_port())
    try:
        load_forms(sim)
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        assert_forms(await read_all(drv), tags)
        await sim.stop()
        started = time.monotonic()
        with pytest.raises(ConnectionLost):
            await drv.read(blocks(drv)[0])
        assert time.monotonic() - started < 1.0
        assert not drv.connected
        with pytest.raises(ConnectionLost, match="connect failed"):
            await drv.connect()
        await sim.restart()
        sim.set("DB1,DI0", "int32", 1234)
        await drv.connect()
        out = await read_all(drv)
        assert out[tags[24].name].value == 1234
        await drv.close()
        assert sim.write_attempts == 0
    finally:
        await sim.stop()


async def test_read_only_guard_stops_writes_before_the_socket(executor) -> None:
    from opencap.drivers.s7 import S7ReadOnlyViolation

    async with await S7Sim.start({"DB1": 16}) as sim:
        drv = make_driver(sim, [Tag(name="w", address="DB1,W0", dtype="uint16")], executor)
        await drv.connect()
        client = drv._session.client
        sim.reset_counters()
        for attempt in (lambda: client.db_write(1, 0, bytearray(2)), client.plc_stop, client.plc_hot_start,
                        lambda: client.set_plc_datetime(__import__("datetime").datetime(2026, 1, 1))):
            with pytest.raises(S7ReadOnlyViolation):
                await asyncio.get_running_loop().run_in_executor(executor, attempt)
        assert sim.request_count == 0 and sim.write_attempts == 0
        assert client.oc_refused == 4
        await drv.close()


# --- through the real ChannelWorker ---------------------------------------------------------------

async def test_channel_worker_end_to_end(executor) -> None:
    """The core drives the driver: cycles, a rejected block, a mute PLC, a power cycle."""
    from opencap.config.models import Channel
    from opencap.core.cache import ValueCache
    from opencap.core.channel_worker import ChannelWorker
    from opencap.core.registry import plan_device

    tags = form_tags() + [Tag(name="ghost", address="DB9,W0", dtype="uint16")]
    sim = await S7Sim.start(AREAS, port=stable_port())
    load_forms(sim)
    device = make_driver(sim, tags, executor, timeout_ms=300).device
    channel = Channel(name="SIM", devices=[device])
    worker = ChannelWorker(channel, {device.id: plan_device(device)}, ValueCache(), start_jitter=0.0,
                           backoff_base_s=0.05, backoff_max_s=0.2)
    real = tags[:-1]
    try:
        snap = await worker.read_once(device.id)
        assert snap["ghost"].quality == "bad_config"
        assert_forms(snap, real)
        assert worker.device_status(device.id)["connected"] is True

        sim.mute = True
        snap = await worker.read_once(device.id)
        assert {snap[t.name].quality for t in real} == {"bad_comm"}
        sim.mute = False
        assert_forms(await worker.read_once(device.id), real)

        await sim.stop()
        snap = await worker.read_once(device.id)
        assert {snap[t.name].quality for t in real} == {"bad_comm"}
        await sim.restart()
        sim.reset_counters()
        sim.set("DB100,D04", "float32", -7.25)
        snap = await worker.read_once(device.id)
        assert snap[real[9].name].value == -7.25 and snap["ghost"].quality == "bad_config"
        assert sim.read_count == 8                                 # the rejected DB9 block is not read again
        assert sim.write_attempts == 0
    finally:
        await worker.stop()
        await sim.stop()
