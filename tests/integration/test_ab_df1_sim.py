"""ab_df1 driver against the DF1 simulator (tests/helpers/df1_sim.py), over raw TCP (a
serial-over-TCP gateway) and over a real tty (pty bridge): every PCCC address pattern of the
real project's DF1 and PCCC devices (docs/decisions.md D-008), CRC and BCC, the link-layer
recoveries (NAK -> retransmit, lost ACK -> ENQ, corrupted reply -> NAK), timeouts, stale and
duplicate replies, PCCC error mapping, two stations sharing one line, and a run through the
real ChannelWorker. The driver must only ever send read commands."""

from __future__ import annotations

import asyncio
import time

import pytest

from opencap.config.models import Channel, Device, Tag
from opencap.core.cache import ValueCache
from opencap.core.channel_worker import ChannelWorker
from opencap.core.registry import plan_device
from opencap.drivers.ab_df1 import CMD_EXTENDED, AbDf1Driver, Df1Message
from opencap.drivers.ab_pccc import coerce_value
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost
from tests.helpers.df1_sim import Df1Sim
from tests.helpers.modbus_sim import free_port
from tests.helpers.netcut import stable_port
from tests.helpers.pccc_sim import tables_for
from tests.helpers.pty_bridge import PtyBridge, needs_pyserial

TYPED_READ = (CMD_EXTENDED, 0xA2)

# Every address pattern of the real project's PCCC and DF1 devices (tests/unit/test_pccc_address.py
# PROJECT_PATTERNS), plus the other file types the planner supports.
PATTERN_TAGS = [
    Tag(name="FLOW", address="F34:3", dtype="float32"),              # F#:#
    Tag(name="FLOW_B", address="F34:5", dtype="float32"),
    Tag(name="LEVEL", address="N54:17", dtype="uint16"),             # N#:# uint16
    Tag(name="LEVEL_NEG", address="N54:18", dtype="int16"),
    Tag(name="TOTAL_F", address="N66:10", dtype="float32"),          # N#:# float32 (int -> float)
    Tag(name="PUMP_RUN", address="B3:17/14", dtype="bool"),          # B#:#/#
    Tag(name="PUMP_TRIP", address="B3/0", dtype="bool"),             # B#/# (bit-number form)
    Tag(name="PUMP_AUTO", address="B3/273", dtype="bool"),           # word 17, bit 1
    Tag(name="DI_0", address="I:0/0", dtype="bool"),                 # I:#/# (DF1 devices)
    Tag(name="DI_19", address="I:0/19", dtype="bool"),               # word 1, bit 3
    Tag(name="MSG", address="ST56:10", dtype="string"),              # ST#:#
    Tag(name="TIMER_PRE", address="T47:0.PRE", dtype="uint16"),      # T#:#.PRE
    Tag(name="TIMER_ACC", address="T47:0.ACC", dtype="uint16"),
    Tag(name="TIMER_DN", address="T47:0.DN", dtype="bool"),
    Tag(name="COUNT_ACC", address="C5:2.ACC", dtype="uint16"),
    Tag(name="LONG", address="L9:4", dtype="int32"),
    Tag(name="OUT_3", address="O:0/3", dtype="bool"),
    Tag(name="STATUS", address="S:1/5", dtype="bool"),
]
PLAN_TIME_BAD = [
    Tag(name="CLOCK_HR", address="RTC:0.HR", dtype="uint16"),       # RTC function file: not readable
    Tag(name="GSM_ONFB", address="GSM_ONFB", dtype="float32"),      # symbolic name on a PCCC device
]


def _tables(tags=PATTERN_TAGS, seed: int = 1) -> dict:
    t = tables_for(tags, seed=seed, system_files=True)
    t["S"] = [0x0020] * 66                                         # S:1/5 = 1
    return t


def _device(port: int, tags=None, *, host="127.0.0.1", station: int = 1, timeout_ms: int = 1000,
            name: str = "PLC", **params) -> Device:
    p = {"station": station, "error_check": "crc", "ack_timeout_ms": 150, "transport": "tcp", **params}
    return Device(id=f"DF1_SITE/{name}", channel="DF1_SITE", name=name, driver="ab_df1", host=host, port=port,
                  params=p, scan_ms=200, min_scan_ms=200, timeout_ms=timeout_ms, retries=1,
                  demote_after=3, demote_for_ms=1000, tags=list(tags if tags is not None else PATTERN_TAGS))


def _expected(sim: Df1Sim, tag: Tag, station: int | None = None):
    return coerce_value(sim.value(tag.address, station=station), tag.dtype)


async def _read_all(driver: AbDf1Driver) -> dict:
    plan = driver.plan(driver.device.tags)
    out = {}
    for block in plan.blocks:
        out.update(await driver.read(block))
    return out


def _assert_values(sim: Df1Sim, tags, values, station: int | None = None) -> None:
    for tag in tags:
        tv = values[tag.name]
        assert tv.quality == "good", (tag.name, tv)
        want = _expected(sim, tag, station)
        if isinstance(want, float):
            assert tv.value == pytest.approx(want, rel=1e-6), tag.name
        else:
            assert tv.value == want, (tag.name, tv.value, want)


@pytest.fixture()
async def sim():
    s = await Df1Sim.start(_tables(), station=1, error_check="crc")
    yield s
    await s.stop()


@pytest.fixture()
async def driver(sim):
    d = AbDf1Driver(_device(sim.port))
    await d.connect()
    yield d
    await d.close()


# --- values ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("check", ["crc", "bcc"])
async def test_every_project_pattern_reads(check):
    sim = await Df1Sim.start(_tables(), station=3, error_check=check)
    sim.set("ST56:10", "LEVEL OK")
    sim.set("T47:0", (0x2000, 300, 120))                          # DN set, PRE 300, ACC 120
    sim.set("N54:18", -5)
    drv = AbDf1Driver(_device(sim.port, PATTERN_TAGS + PLAN_TIME_BAD, station=3, error_check=check))
    try:
        plan = drv.plan(drv.device.tags)
        assert set(plan.invalid) == {"CLOCK_HR", "GSM_ONFB"}       # bad_config at plan time, never sent
        await drv.connect()
        values = await _read_all(drv)
        _assert_values(sim, PATTERN_TAGS, values)
        assert values["MSG"].value == "LEVEL OK" and values["TIMER_DN"].value is True
        assert values["TIMER_PRE"].value == 300 and values["TIMER_ACC"].value == 120
        assert values["LEVEL_NEG"].value == -5 and values["STATUS"].value is True
        assert isinstance(values["TOTAL_F"].value, float)
        # one typed read per block, nothing else; no write ever
        assert sum(sim.commands.values()) == len(plan.blocks)
        assert set(sim.commands) == {(3, *TYPED_READ)}
        assert sim.write_attempts == 0 and sim.bad_frames_in == 0 and sim.naks_sent == 0
    finally:
        await drv.close()
        await sim.stop()


async def test_values_follow_the_plc(sim, driver):
    _assert_values(sim, PATTERN_TAGS, await _read_all(driver))
    sim.set("F34:3", 12.5)
    sim.set("B3:17/14", not sim.value("B3:17/14"))
    sim.set("I:0/19", True)
    values = await _read_all(driver)
    assert values["FLOW"].value == 12.5 and values["DI_19"].value is True
    _assert_values(sim, PATTERN_TAGS, values)


async def test_two_field_typed_read_variant(sim):
    drv = AbDf1Driver(_device(sim.port, read_fnc="a1"))
    await drv.connect()
    try:
        _assert_values(sim, PATTERN_TAGS, await _read_all(drv))
        assert (1, CMD_EXTENDED, 0xA1) in sim.commands
    finally:
        await drv.close()


async def test_large_block_in_one_request(sim):
    tags = [Tag(name=f"N{i}", address=f"N54:{i}", dtype="int16") for i in range(0, 100)]
    sim.set("N54", list(range(-50, 50)))
    drv = AbDf1Driver(_device(sim.port, tags))
    await drv.connect()
    try:
        plan = drv.plan(tags)
        assert len(plan.blocks) == 1 and plan.blocks[0].nbytes == 200
        values = await _read_all(drv)
        assert [values[f"N{i}"].value for i in range(100)] == list(range(-50, 50))
    finally:
        await drv.close()


# --- link layer ------------------------------------------------------------------------------------------

async def test_nak_makes_the_driver_retransmit(sim, driver):
    sim.nak_next = 2
    _assert_values(sim, PATTERN_TAGS, await _read_all(driver))
    assert sim.naks_sent == 2
    stats = (await driver.identity())["link_stats"]
    assert stats["retransmissions"] == 2 and stats["naks_received"] == 2


async def test_nak_retries_used_up_is_comm_error_then_recovers(sim):
    drv = AbDf1Driver(_device(sim.port, nak_retries=2))
    await drv.connect()
    try:
        block = drv.plan(drv.device.tags).blocks[0]
        sim.nak_next = 10
        with pytest.raises(CommError, match=r"NAKed 3 time\(s\)"):
            await drv.read(block)
        assert sim.frames_in == 3 and drv.connected               # the line itself is fine
        sim.nak_next = 0
        assert all(tv.quality == "good" for tv in (await drv.read(block)).values())
    finally:
        await drv.close()


async def test_corrupted_reply_is_naked_and_resent(sim, driver):
    sim.corrupt_replies = 1
    _assert_values(sim, PATTERN_TAGS, await _read_all(driver))
    assert sim.naks_in == 1 and sim.retransmits == 1
    assert (await driver.identity())["link_stats"]["naks_sent"] == 1


async def test_lost_ack_is_recovered_with_enq(sim, driver):
    sim.drop_acks = 1
    sim.reply_delay_s = 0.4                                        # longer than the 150 ms ACK timeout
    block = driver.plan(driver.device.tags).blocks[0]
    values = await driver.read(block)
    assert all(tv.quality == "good" for tv in values.values())
    assert sim.enqs_in >= 1 and sim.frames_in == 1                # answered by ACK, nothing resent


async def test_lost_frame_on_a_fresh_line_is_resent_after_enq(sim, driver):
    sim.lose_next = 1                                              # the PLC never saw it; its last response is NAK
    block = driver.plan(driver.device.tags).blocks[0]
    values = await driver.read(block)
    assert all(tv.quality == "good" for tv in values.values())
    assert sim.enqs_in == 1 and sim.frames_in == 2


async def test_lost_frame_after_traffic_times_out_and_the_next_read_works(sim, driver):
    # DF1's known gap: the PLC answers the ENQ with its last response (ACK of the previous frame),
    # so the sender waits for a reply that never comes. The attempt fails within timeout_ms;
    # the worker's retry (a new TNS) succeeds.
    block = driver.plan(driver.device.tags).blocks[0]
    await driver.read(block)
    sim.lose_next = 1
    started = time.monotonic()
    with pytest.raises(CommError, match="no reply within 1000 ms"):
        await driver.read(block)
    assert time.monotonic() - started < 1.3
    assert all(tv.quality == "good" for tv in (await driver.read(block)).values())


async def test_silent_plc_is_comm_error_within_the_ack_budget(sim, driver):
    sim.mute = True
    block = driver.plan(driver.device.tags).blocks[0]
    started = time.monotonic()
    with pytest.raises(CommError, match=r"no ACK after 3 ENQ\(s\)"):
        await driver.read(block)
    assert time.monotonic() - started < 0.9                        # 4 x 150 ms ACK timeouts
    assert driver.connected                                        # close_on_timeout is off by default
    sim.mute = False
    assert all(tv.quality == "good" for tv in (await driver.read(block)).values())


async def test_close_on_timeout_drops_the_line(sim):
    drv = AbDf1Driver(_device(sim.port, close_on_timeout=True))
    await drv.connect()
    try:
        sim.no_reply = 1
        with pytest.raises(CommError, match="line closed"):
            await drv.read(drv.plan(drv.device.tags).blocks[0])
        assert not drv.connected
        await drv.connect()
        assert sim.connections == 2
    finally:
        await drv.close()


async def test_duplicate_and_stale_replies_are_dropped(sim, driver):
    sim.duplicate_replies = 1
    sim.stale_reply = 1
    blocks = driver.plan(driver.device.tags).blocks
    first = await driver.read(blocks[0])
    assert all(tv.quality == "good" for tv in first.values())
    _assert_values(sim, PATTERN_TAGS, await _read_all(driver))
    stats = (await driver.identity())["link_stats"]
    assert stats["duplicates"] == 1 and stats["unmatched"] == 1


async def test_a_late_reply_is_never_taken_for_a_newer_request(sim, driver):
    tag = Tag(name="FLOW", address="F34:3", dtype="float32")
    block = driver.plan([tag]).blocks[0]
    sim.set("F34:3", 1.0)
    sim.reply_delay_s = 1.3                                        # the reply comes after timeout_ms (1 s)
    with pytest.raises(CommError, match="no reply"):
        await driver.read(block)
    sim.reply_delay_s = 0.0
    sim.set("F34:3", 2.0)
    await asyncio.sleep(0.5)                                       # the stale reply (1.0) is now buffered
    assert (await driver.read(block))["FLOW"].value == 2.0
    assert (await driver.identity())["link_stats"]["unmatched"] == 1


async def test_point_to_point_plc_answers_from_its_own_station_number(sim):
    sim.answer_any_station = True                                  # PLC is station 1, we address 5
    drv = AbDf1Driver(_device(sim.port, station=5))
    await drv.connect()
    try:
        _assert_values(sim, PATTERN_TAGS, await _read_all(drv))
        assert (await drv.identity())["link_stats"]["source_mismatch"] >= 1
    finally:
        await drv.close()


async def test_unknown_station_behind_a_bridge_times_out(sim):
    drv = AbDf1Driver(_device(sim.port, station=9, timeout_ms=400))
    await drv.connect()
    try:
        with pytest.raises(CommError, match="no reply within 400 ms"):
            await drv.read(drv.plan(drv.device.tags).blocks[0])
    finally:
        await drv.close()


async def test_ack_embedded_in_the_reply_frame(sim, driver):
    sim.embed_ack = True
    _assert_values(sim, PATTERN_TAGS, await _read_all(driver))


# --- PCCC status -------------------------------------------------------------------------------------------

async def test_missing_file_is_block_config_error(sim, driver):
    ghost = Tag(name="GHOST", address="N88:0", dtype="int16")
    with pytest.raises(BlockConfigError, match="illegal command or format"):
        await driver.read(driver.plan([ghost]).blocks[0])
    past_end = Tag(name="PAST", address="N54:200", dtype="int16")
    with pytest.raises(BlockConfigError):
        await driver.read(driver.plan([past_end]).blocks[0])


@pytest.mark.parametrize(("sts", "error"), [
    ((0xF0, 0x06), BlockConfigError),          # address does not point to something usable
    ((0x50, None), BlockConfigError),          # addressing problem
    ((0x70, None), CommError),                 # processor in Program mode: comes back
    ((0xF0, 0x0B), CommError),                 # access denied
])
async def test_pccc_status_mapping(sim, driver, sts, error):
    sim.plc.file_errors["F34"] = sts
    block = next(b for b in driver.plan(driver.device.tags).blocks if b.file_type == "F")
    with pytest.raises(error) as info:
        await driver.read(block)
    assert not isinstance(info.value, ConnectionLost)
    assert f"STS {sts[0]:#04x}" in str(info.value)


# --- connections -----------------------------------------------------------------------------------------

async def test_refused_gateway_is_connection_lost():
    drv = AbDf1Driver(_device(free_port()))
    with pytest.raises(ConnectionLost, match="connect failed"):
        await drv.connect()
    assert not drv.connected


async def test_unmapped_com_port_says_how_to_map_it():
    dev = _device(0, host="COM112", transport="unmapped", com_port="COM112")
    drv = AbDf1Driver(dev)
    assert drv.plan(dev.tags).blocks                                # check/plan works without a transport
    with pytest.raises(ConnectionLost, match="COM112 is not mapped.*serial_ports"):
        await drv.connect()


async def test_power_off_is_connection_lost_and_power_on_reconnects():
    sim = await Df1Sim.start(_tables(), port=stable_port())
    drv = AbDf1Driver(_device(sim.port))
    try:
        await drv.connect()
        block = drv.plan(drv.device.tags).blocks[0]
        await drv.read(block)
        await sim.stop()
        with pytest.raises(ConnectionLost):
            await drv.read(block)
        assert not drv.connected
        with pytest.raises(ConnectionLost):
            await drv.connect()
        await sim.restart()
        await drv.connect()
        assert all(tv.quality == "good" for tv in (await drv.read(block)).values())
    finally:
        await drv.close()
        await sim.stop()


async def test_two_stations_share_one_line(sim):
    tags2 = [Tag(name="FLOW", address="F34:3", dtype="float32"), Tag(name="LEVEL", address="N54:17", dtype="uint16")]
    sim.add_station(2, _tables(tags2, seed=9))
    a = AbDf1Driver(_device(sim.port, station=1, name="A"))
    b = AbDf1Driver(_device(sim.port, tags2, station=2, name="B"))
    try:
        await a.connect()
        await b.connect()
        assert sim.connections == 1                                # one TCP connection for the line
        _assert_values(sim, PATTERN_TAGS, await _read_all(a))
        _assert_values(sim, tags2, await _read_all(b), station=2)
        assert sim.value("F34:3", station=2) != sim.value("F34:3", station=1)
        both = await asyncio.gather(_read_all(a), _read_all(b))   # one transaction at a time on the line
        _assert_values(sim, PATTERN_TAGS, both[0])
        _assert_values(sim, tags2, both[1], station=2)
        await a.close()
        assert b.connected                                         # the line stays open for B
        _assert_values(sim, tags2, await _read_all(b), station=2)
        bcc = AbDf1Driver(_device(sim.port, station=4, name="C", error_check="bcc"))
        with pytest.raises(ConnectionLost, match="error_check bcc differs"):
            await bcc.connect()
    finally:
        await a.close()
        await b.close()


async def test_line_failure_drops_every_station(sim):
    sim.add_station(2, _tables(seed=9))
    a = AbDf1Driver(_device(sim.port, station=1, name="A"))
    b = AbDf1Driver(_device(sim.port, station=2, name="B"))
    try:
        await a.connect()
        await b.connect()
        for line in list(sim._lines):
            line.writer.transport.abort()                          # the gateway drops the TCP connection
        with pytest.raises(ConnectionLost):
            await _read_all(a)
        assert not a.connected and not b.connected
        await b.connect()
        await a.connect()
        _assert_values(sim, PATTERN_TAGS, await _read_all(a))
        assert sim.connections == 2
    finally:
        await a.close()
        await b.close()


@needs_pyserial
async def test_over_a_tty(sim):
    bridge = await PtyBridge.start("127.0.0.1", sim.port)
    drv = AbDf1Driver(_device(0, host=bridge.path, transport="serial", baudrate=19200, parity="N"))
    try:
        await drv.connect()
        _assert_values(sim, PATTERN_TAGS, await _read_all(drv))
        ident = await drv.identity()
        assert ident["link"].startswith(f"serial {bridge.path}")
        assert bridge.bytes_to_sim > 0 and bridge.bytes_from_sim > 0
    finally:
        await drv.close()
        await bridge.stop()


async def test_identity(sim, driver):
    ident = await driver.identity()
    assert ident["processor_type"] == "1766-L32BXB"
    assert (ident["station"], ident["error_check"], ident["link"]) == (1, "crc", f"tcp 127.0.0.1:{sim.port}")
    assert (1, 0x06, 0x03) in sim.commands
    sim.mute = True
    assert await driver.identity() == {}                           # never raises
    await driver.close()
    assert await driver.identity() == {}


async def test_read_only(sim, driver):
    with pytest.raises(NotImplementedError, match="read-only"):
        await driver.write("N7:0", 1)
    link = driver._lease.conn
    for cmd, fnc in ((0x0F, 0xAA), (0x0F, 0xAB), (0x08, None), (0x0F, 0x67)):
        data = bytes([fnc]) if fnc is not None else b"\x00"
        with pytest.raises(NotImplementedError, match="read-only"):
            await link.transact(Df1Message(1, 0, cmd, 0, 1, data), timeout_s=0.5, ack_timeout_s=0.1,
                                nak_retries=0, enq_retries=0)
    assert sim.frames_in == 0 and sim.write_attempts == 0


# --- the real ChannelWorker ----------------------------------------------------------------------------------

async def test_channel_worker_run_with_two_stations_on_one_line():
    sim = await Df1Sim.start(_tables(), station=1, port=stable_port())
    tags2 = [Tag(name="FLOW", address="F34:3", dtype="float32"), Tag(name="DI_0", address="I:0/0", dtype="bool")]
    sim.add_station(2, _tables(tags2, seed=4))
    dev_a = _device(sim.port, PATTERN_TAGS + PLAN_TIME_BAD, station=1, name="A", timeout_ms=500)
    dev_b = _device(sim.port, tags2, station=2, name="B", timeout_ms=500)
    channel = Channel(name="DF1_SITE", devices=[dev_a, dev_b])
    cache = ValueCache()
    worker = ChannelWorker(channel, {d.id: plan_device(d) for d in (dev_a, dev_b)}, cache,
                           start_jitter=0.0, backoff_base_s=0.05, backoff_max_s=0.2)
    task = asyncio.create_task(worker.run())
    try:
        async def until(pred, timeout=10.0):
            deadline = time.monotonic() + timeout
            while not pred():
                assert time.monotonic() < deadline, "timed out"
                await asyncio.sleep(0.05)

        await until(lambda: worker.device_status(dev_a.id)["state"] == "ok"
                    and worker.device_status(dev_b.id)["state"] == "ok")
        snap_a, snap_b = cache.snapshot(dev_a.id), cache.snapshot(dev_b.id)
        assert snap_a["CLOCK_HR"].quality == "bad_config" and snap_a["GSM_ONFB"].quality == "bad_config"
        _assert_values(sim, PATTERN_TAGS, snap_a)
        _assert_values(sim, tags2, snap_b, station=2)
        assert sim.connections == 1

        sim.set("F34:3", 77.5)
        await until(lambda: cache.snapshot(dev_a.id)["FLOW"].value == 77.5)

        await sim.stop()                                           # gateway power-off
        await until(lambda: cache.snapshot(dev_a.id)["FLOW"].quality == "bad_comm"
                    and cache.snapshot(dev_b.id)["FLOW"].quality == "bad_comm")
        await sim.restart()
        sim.set("F34:3", -3.25, station=2)
        await until(lambda: cache.snapshot(dev_b.id)["FLOW"].value == -3.25, timeout=15)
        await until(lambda: cache.snapshot(dev_a.id)["FLOW"].quality == "good", timeout=15)
        assert sim.write_attempts == 0
        assert {(st, cmd, fnc) for st, cmd, fnc in sim.commands} <= {(1, *TYPED_READ), (2, *TYPED_READ)}
    finally:
        await worker.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await sim.stop()
