"""ab_pccc driver against the PCCC simulator (tests/helpers/pccc_sim.py, contract §2).

Real pycomm3 1.2.16 `SLCDriver` on the wire: F/N/B data tables (word and bit-number bit
forms, N words typed float32), the file-directory check, PCCC error statuses, a mute PLC,
power-off and power-on, one real KEPServer device, and the real ChannelWorker.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

pccc_sim = pytest.importorskip("tests.helpers.pccc_sim")

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.core.cache import TagValue  # noqa: E402
from opencap.drivers.ab_pccc import AbPcccDriver, PcccBlock, parse_pccc_address  # noqa: E402
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402

PcccSim = pccc_sim.PcccSim
tables_for = pccc_sim.tables_for

TYPED_READ, DIRECTORY_READ, DIAGNOSTIC = (0x0F, 0xA2), (0x0F, 0xA1), (0x06, 0x03)
# O0, I1 and S2 exist on every SLC 500 / MicroLogix; the driver trusts a directory only with them.
SYSTEM_FILES = {"O": [0], "I": [0], "S": [0] * 66}

TAGS = [
    Tag(name="flow", address="F8:0", dtype="float32"),
    Tag(name="level", address="F8:1", dtype="float32"),
    Tag(name="total", address="F19:3", dtype="float32"),
    Tag(name="count", address="N7:1", dtype="uint16"),
    Tag(name="signed", address="N7:2", dtype="int16"),
    Tag(name="n66_float", address="N66:10", dtype="float32"),        # N word typed float32
    Tag(name="run", address="B3:1/15", dtype="bool"),                # word/bit form
    Tag(name="fault", address="B3:0/0", dtype="bool"),
    Tag(name="bitnum_16", address="B3/16", dtype="bool"),            # bit-number form: word 1 bit 0
    Tag(name="bitnum_33", address="B3/33", dtype="bool"),            # word 2 bit 1
    Tag(name="b_word", address="B3:2", dtype="uint16"),
]


def expected(sim: Any, tag: Tag) -> Any:
    addr = parse_pccc_address(tag.address)
    raw = sim.value(addr.canonical())                 # pycomm3's view: N/B words signed, bits bool
    if tag.dtype == "bool":
        return bool(raw)
    if tag.dtype == "uint16":
        return raw & 0xFFFF
    if tag.dtype.startswith("float"):
        return float(raw)
    return raw


def assert_values(sim: Any, tags: list[Tag], out: dict[str, TagValue]) -> None:
    bad = {t.name: out[t.name].quality for t in tags if out[t.name].quality != "good"}
    assert bad == {}
    for t in tags:
        want, got = expected(sim, t), out[t.name].value
        if isinstance(want, float):
            assert got == pytest.approx(want, rel=1e-6), t.name
        else:
            assert got == want and type(got) is type(want), t.name


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pccc-sim-exec")
    yield ex
    ex.shutdown(wait=True)


def make_driver(sim: Any, tags: list[Tag], executor: ThreadPoolExecutor, *, timeout_ms: int = 1500,
                **params: Any) -> AbPcccDriver:
    dev = Device(id="SIM/SLC", channel="SIM", name="SLC", driver="ab_pccc", host=sim.host, port=sim.port,
                 params=params, tags=tags, timeout_ms=timeout_ms)
    return AbPcccDriver(dev, executor)


def blocks(drv: AbPcccDriver) -> dict[str, PcccBlock]:
    plan = drv.plan(drv.device.tags)
    assert plan.invalid == {}
    return {b.address: b for b in plan.blocks if isinstance(b, PcccBlock)}


async def read_all(drv: AbPcccDriver) -> dict[str, TagValue]:
    out: dict[str, TagValue] = {}
    for block in blocks(drv).values():
        out.update(await drv.read(block))
    return out


async def start_sim(tags: list[Tag], **kw: Any) -> Any:
    return await PcccSim.start({**SYSTEM_FILES, **tables_for(tags, seed=3)}, **kw)


async def wait_for(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


# --- reads ------------------------------------------------------------------------------------------

async def test_reads_f_n_b_and_n_float_tags(executor) -> None:
    async with await start_sim(TAGS) as sim:
        sim.set("B3:1/15", True)
        sim.set("B3/33", True)
        sim.set("N7:1", -2)                                          # 65534 as uint16
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        ident = await drv.identity()
        assert ident["product_name"] == pccc_sim.DEFAULT_PRODUCT_NAME and ident["processor_type"] == "1766-L32BXB"
        assert ident["file_directory"].startswith("ok") and ident["files"]["F19"] == 4
        assert ident["connection_size"] == 500 and sim.connection_sizes == [500]
        assert sim.cm_counts[0x5B] == 0                               # no Large Forward Open attempt
        sim.reset_counters()
        out = await read_all(drv)
        assert_values(sim, TAGS, out)
        assert (out["count"].value, out["run"].value, out["bitnum_33"].value) == (65534, True, True)
        assert sim.pccc_counts[TYPED_READ] == len(blocks(drv)) == sim.request_count   # one request per block
        assert sorted(blocks(drv)) == ["B3:0{3}", "F19:3", "F8:0{2}", "N66:10", "N7:1{2}"]
        sim.set("F8:0", 42.5)
        sim.set("B3/16", True)
        out = await read_all(drv)
        assert out["flow"].value == 42.5 and out["bitnum_16"].value is True
        await drv.close()
        assert await wait_for(lambda: sim.clients == 0 and sim.open_connections == 0)
        assert sim.write_attempts == 0


@pytest.mark.parametrize("product_name", ["1747-L551/C 5/05", "1763-L16BWA B/12.00"])
async def test_other_processor_layouts(executor, product_name: str) -> None:
    """SLC 5/05 and MicroLogix 1100 file-directory layouts (pycomm3 `_get_sys0_info`)."""
    async with await start_sim(TAGS, product_name=product_name) as sim:
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        ident = await drv.identity()
        assert ident["file_directory"].startswith("ok"), ident
        assert ident["files"] == sim.files
        assert_values(sim, TAGS, await read_all(drv))
        await drv.close()


# --- file directory and PCCC errors -----------------------------------------------------------------

async def test_missing_file_is_rejected_without_sending(executor) -> None:
    tags = TAGS + [Tag(name="ghost", address="N9:0", dtype="uint16"),
                   Tag(name="past_end", address="N7:40", dtype="uint16"),
                   Tag(name="wrong_type", address="F7:0", dtype="float32")]
    async with await start_sim(TAGS) as sim:
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        bl = blocks(drv)
        sim.reset_counters()
        for address, reason in [("N9:0", "file N9 does not exist"), ("N7:40", "N7 has 3 elements"),
                                ("F7:0", "file 7 is a N file, not F")]:
            with pytest.raises(BlockConfigError, match=reason):
                await drv.read(bl[address])
        assert sim.request_count == 0                                # the directory said so: nothing sent
        assert drv.connected
        await drv.close()


async def test_directory_reused_across_reconnects(executor) -> None:
    async with await start_sim(TAGS) as sim:
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        assert sim.pccc_counts[DIRECTORY_READ] > 1
        sim.reset_counters()
        await drv.close()
        await drv.connect()
        assert sim.pccc_counts[DIRECTORY_READ] == 0 and sim.pccc_counts[DIAGNOSTIC] == 1
        assert_values(sim, TAGS, await read_all(drv))
        await drv.close()


@pytest.mark.parametrize("sim_kw", [{"directory": False}, {}])
async def test_without_directory_the_plc_error_rejects_the_block(executor, sim_kw: dict) -> None:
    """directory=False: file 0 cannot be read. {} with no O0/I1/S2 tables: the parsed directory
    is not plausible. Either way the check is skipped and the PLC's STS 0x10 decides."""
    tags = TAGS + [Tag(name="ghost", address="N9:0", dtype="uint16")]
    tables = tables_for(TAGS, seed=3) if not sim_kw else {**SYSTEM_FILES, **tables_for(TAGS, seed=3)}
    async with await PcccSim.start(tables, **sim_kw) as sim:
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        ident = await drv.identity()
        assert ident["file_directory"].startswith("unavailable"), ident
        sim.reset_counters()
        with pytest.raises(BlockConfigError, match=r"STS 0x10 \(illegal command or format"):
            await drv.read(blocks(drv)["N9:0"])
        assert sim.pccc_counts[TYPED_READ] == 1
        assert drv.connected
        out: dict[str, TagValue] = {}
        for address, block in blocks(drv).items():
            if address != "N9:0":
                out.update(await drv.read(block))
        assert_values(sim, TAGS, out)
        await drv.close()


@pytest.mark.parametrize(("sts", "error"), [
    ((0xF0, 0x06), BlockConfigError),          # EXT STS: address does not point to something usable
    ((0xF0, 0x07), BlockConfigError),          # file is wrong size
    ((0xF0, 0x0B), CommError),                 # access denied
    ((0x50, None), BlockConfigError),          # addressing problem
    ((0x70, None), CommError),                 # processor in Program mode
])
async def test_pccc_status_mapping(executor, sts: tuple[int, int | None], error: type) -> None:
    async with await start_sim(TAGS) as sim:
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        sim.file_errors["F8"] = sts
        with pytest.raises(error) as info:
            await drv.read(blocks(drv)["F8:0{2}"])
        assert type(info.value) is error and "PCCC STS" in str(info.value)
        assert drv.connected
        del sim.file_errors["F8"]
        assert_values(sim, TAGS, await read_all(drv))
        await drv.close()


async def test_file_removed_after_connect_is_rejected_by_the_plc(executor) -> None:
    async with await start_sim(TAGS) as sim:
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        sim.remove("F19")                                            # program download
        with pytest.raises(BlockConfigError, match="STS 0x10"):
            await drv.read(blocks(drv)["F19:3"])
        await drv.close()


# --- faults -------------------------------------------------------------------------------------------

async def test_mute_plc_gives_comm_error_then_recovers(executor) -> None:
    async with await start_sim(TAGS) as sim:
        drv = make_driver(sim, TAGS, executor, timeout_ms=300)
        await drv.connect()
        block = blocks(drv)["F8:0{2}"]
        sim.mute = True
        started = time.monotonic()
        with pytest.raises(CommError) as info:
            await drv.read(block)
        assert not isinstance(info.value, ConnectionLost)
        assert "no reply within 300 ms" in str(info.value)
        assert time.monotonic() - started < 1.5                      # one timeout, no Forward Close wait
        assert not drv.connected                                     # socket dropped on the timeout
        sim.mute = False
        await drv.connect()
        assert_values(sim, TAGS, await read_all(drv))
        await drv.close()


async def test_power_off_gives_connection_lost_and_power_on_reconnects(executor) -> None:
    sim = await PcccSim.start({**SYSTEM_FILES, **tables_for(TAGS, seed=3)}, port=stable_port())
    try:
        drv = make_driver(sim, TAGS, executor)
        await drv.connect()
        assert_values(sim, TAGS, await read_all(drv))
        await sim.stop()
        started = time.monotonic()
        with pytest.raises(ConnectionLost):
            await drv.read(blocks(drv)["F8:0{2}"])
        assert time.monotonic() - started < 1.0
        assert not drv.connected
        with pytest.raises(ConnectionLost, match="connect failed"):
            await drv.connect()
        await sim.restart()
        sim.reset_counters()
        await drv.connect()
        assert sim.pccc_counts[DIRECTORY_READ] == 0                  # directory reused
        sim.set("N7:2", 1234)
        out = await read_all(drv)
        assert_values(sim, TAGS, out)
        assert out["signed"].value == 1234
        await drv.close()
    finally:
        await sim.stop()


# --- a real KEPServer device ------------------------------------------------------------------------

def _plannable(tag: Tag) -> bool:
    try:
        parse_pccc_address(tag.address)
    except ValueError:
        return False                       # symbolic / RTC: bad_config at plan time
    return True


async def test_real_project_device(executor, kep2poller_output: dict) -> None:
    """A real PCCC device: every tag reads, except addresses whose file number the PLC holds with
    another type (the real project has an `F54:17` typo on an N file), which the file directory
    rejects without sending. Picks the device with such a conflict, else the largest."""
    from tests.helpers.pccc_sim import file_type_conflicts

    devices = [[Tag(name=t["name"], address=t["address"], dtype=t["type"]) for t in d["tags"]]
               for s in kep2poller_output["sites"] for d in s["devices"]
               if d["driver"] == "Allen-Bradley ControlLogix Ethernet" and d["model"] == 23]
    tags = max(devices, key=lambda ts: (bool(file_type_conflicts(ts)), len(ts)))
    tags = [t for t in tags if _plannable(t)]
    conflicts = file_type_conflicts(tags)
    async with await start_sim(tags) as sim:
        drv = make_driver(sim, tags, executor)
        plan = drv.plan(tags)
        assert plan.invalid == {}
        await drv.connect()
        sim.reset_counters()
        out: dict[str, TagValue] = {}
        rejected = []
        for block in plan.blocks:
            try:
                out.update(await drv.read(block))
            except BlockConfigError as exc:
                rejected.append((block.address, str(exc)))
        assert sorted(a for a, _ in rejected) == sorted(conflicts)
        assert all("is a" in msg and "file" in msg for _, msg in rejected), rejected
        typo = {t.name for t in tags if t.address in conflicts}
        assert_values(sim, [t for t in tags if t.name not in typo], out)
        assert sim.request_count == len(plan.blocks) - len(rejected)
        await drv.close()


# --- through the real ChannelWorker ---------------------------------------------------------------

async def test_channel_worker_end_to_end(executor) -> None:
    """The core drives the driver: cycles, a rejected block, a mute PLC, a power cycle."""
    from opencap.config.models import Channel
    from opencap.core.cache import ValueCache
    from opencap.core.channel_worker import ChannelWorker
    from opencap.core.registry import plan_device

    tags = TAGS + [Tag(name="ghost", address="N9:0", dtype="uint16")]
    sim = await PcccSim.start({**SYSTEM_FILES, **tables_for(TAGS, seed=3)}, port=stable_port())
    device = make_driver(sim, tags, executor, timeout_ms=300).device
    channel = Channel(name="SIM", devices=[device])
    worker = ChannelWorker(channel, {device.id: plan_device(device)}, ValueCache(), start_jitter=0.0,
                           backoff_base_s=0.05, backoff_max_s=0.2)
    try:
        snap = await worker.read_once(device.id)
        assert snap["ghost"].quality == "bad_config"
        assert_values(sim, TAGS, snap)
        assert worker.device_status(device.id)["connected"] is True

        sim.mute = True
        snap = await worker.read_once(device.id)
        assert {snap[t.name].quality for t in TAGS} == {"bad_comm"}
        sim.mute = False
        assert_values(sim, TAGS, await worker.read_once(device.id))

        await sim.stop()
        snap = await worker.read_once(device.id)
        assert {snap[t.name].quality for t in TAGS} == {"bad_comm"}
        await sim.restart()
        sim.reset_counters()
        sim.set("F8:1", -7.25)
        snap = await worker.read_once(device.id)
        assert_values(sim, TAGS, snap)
        assert snap["level"].value == -7.25 and snap["ghost"].quality == "bad_config"
        assert sim.pccc_counts[DIRECTORY_READ] == 0                  # same driver instance: directory reused
        assert sim.pccc_counts[TYPED_READ] == len(blocks(make_driver(sim, TAGS, executor)))
        assert sim.write_attempts == 0
    finally:
        await worker.stop()
        await sim.stop()
