"""ab_logix driver against the CIP simulator (tests/helpers/cip_sim.py, contract §2).

Real pycomm3 1.2.16 on the wire: Logix and Micro800, Micro800 batching probe and fallback,
tag-existence check, timeouts, power-off and power-on.
"""

from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

cip_sim = pytest.importorskip("tests.helpers.cip_sim")

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.core.cache import TagValue  # noqa: E402
from opencap.drivers.ab_logix import AbLogixDriver, LogixBlock  # noqa: E402
from opencap.drivers.base import CommError, ConnectionLost  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402

CipSim = cip_sim.CipSim
make_logix_tags = cip_sim.make_logix_tags

CIP_DTYPE = {"REAL": "float32", "LREAL": "float64", "DINT": "int32", "INT": "int16", "SINT": "int16",
             "BOOL": "bool", "STRING": "string"}
SVC_READ, SVC_MSP, SVC_TAG_LIST = 0x4C, 0x0A, 0x55


def oo_tags(sim_tags: dict[str, tuple[str, Any]]) -> list[Tag]:
    """opencap tags covering every sim tag: scalars, a `.3` bit of each DINT, first and last
    array elements, UDT members (and one element of a member array)."""
    out: list[Tag] = []

    def add(address: str, dtype: str) -> None:
        out.append(Tag(name=address, address=address, dtype=dtype))

    for name, (spec, value) in sim_tags.items():
        if spec.startswith("UDT:"):
            for member, (mspec, _v) in value.items():
                if "[" in mspec:
                    add(f"{name}.{member}[1]", CIP_DTYPE[mspec.split("[")[0]])
                else:
                    add(f"{name}.{member}", CIP_DTYPE[mspec])
        elif "[" in spec:
            base, n = spec[:-1].split("[")
            add(f"{name}[0]", CIP_DTYPE[base])
            add(f"{name}[{int(n) - 1}]", CIP_DTYPE[base])
        else:
            add(name, CIP_DTYPE[spec])
            if spec == "DINT":
                add(f"{name}.3", "bool")
    return out


def expected(sim: Any, tag: Tag) -> Any:
    if m := re.fullmatch(r"(.+)\.(\d+)", tag.address):
        return bool((sim.value(m[1]) >> int(m[2])) & 1)
    value = sim.value(tag.address)
    return float(value) if tag.dtype.startswith("float") else value


def assert_values(sim: Any, tags: list[Tag], out: dict[str, TagValue]) -> None:
    bad = {t.name: out[t.name].quality for t in tags if out[t.name].quality != "good"}
    assert bad == {}
    for t in tags:
        want = expected(sim, t)
        got = out[t.name].value
        if isinstance(want, float):
            assert got == pytest.approx(want, rel=1e-6), t.name
        else:
            assert got == want and type(got) is type(want), t.name


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ab-sim-exec")
    yield ex
    ex.shutdown(wait=True)


def make_driver(sim: Any, tags: list[Tag], executor: ThreadPoolExecutor, *, timeout_ms: int = 1500,
                **params: Any) -> AbLogixDriver:
    micro800 = sim.kind == "micro800"
    p: dict[str, Any] = {"micro800": micro800, **params}
    if not micro800:
        p.setdefault("path", f"1,{sim.slot}")
        p.setdefault("slot", sim.slot)
    dev = Device(id=f"SIM/{sim.kind}", channel="SIM", name=sim.kind, driver="ab_logix", host=sim.host,
                 port=sim.port, params=p, tags=tags, timeout_ms=timeout_ms)
    return AbLogixDriver(dev, executor)


def blocks(drv: AbLogixDriver) -> list[LogixBlock]:
    plan = drv.plan(drv.device.tags)
    assert plan.invalid == {}
    return [b for b in plan.blocks if isinstance(b, LogixBlock)]


async def read_all(drv: AbLogixDriver) -> dict[str, TagValue]:
    out: dict[str, TagValue] = {}
    for block in blocks(drv):
        out.update(await drv.read(block))
    return out


def distinct_names(drv: AbLogixDriver) -> int:
    return sum(len(b.names) for b in blocks(drv))


async def wait_for(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


# --- Logix ---------------------------------------------------------------------------------------

async def test_logix_reads_via_multiple_service_packet(executor) -> None:
    sim_tags = make_logix_tags(60, seed=1)
    async with await CipSim.start(sim_tags, kind="logix") as sim:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor, connection_size=500)
        await drv.connect()
        ident = await drv.identity()
        assert ident["micro800"] is False and ident["batching"] is True
        assert ident["product_name"] == cip_sim.LOGIX_PRODUCT_NAME and ident["connection_size"] == 500
        assert sim.connection_sizes[-1] == 500                     # params.connection_size applied
        sim.reset_counters()
        out = await read_all(drv)
        assert_values(sim, tags, out)
        assert sim.service_counts[SVC_MSP] >= 1
        assert sim.request_count < distinct_names(drv) / 3
        await drv.close()
        assert await wait_for(lambda: sim.clients == 0 and sim.open_connections == 0)
        assert sim.write_attempts == 0


@pytest.mark.parametrize("revision", [(21, 0), (32, 11)])
async def test_logix_v21_still_addressed_symbolically(executor, revision: tuple[int, int]) -> None:
    """pycomm3 would use Symbol instance ids from v21; the driver forces names (safe with a
    tag list reused across reconnects)."""
    sim_tags = make_logix_tags(30, seed=2)
    async with await CipSim.start(sim_tags, kind="logix", revision=revision) as sim:
        drv = make_driver(sim, oo_tags(sim_tags), executor)
        await drv.connect()
        out = await read_all(drv)
        assert {v.quality for v in out.values()} == {"good"}
        assert sim.addressing["instance"] == 0 and sim.addressing["symbolic"] > 0
        await drv.close()


@pytest.mark.parametrize("kind", ["logix", "micro800"])
async def test_string_tags(executor, kind: str) -> None:
    """Logix STRING (structure) and Micro800 STRING (CIP SHORT_STRING 0xDA) decode to str."""
    sim_tags = {"MSG": ("STRING", "pump 1 running"), "EMPTY": ("STRING", ""), "N": ("DINT", 5),
                "F": ("REAL", 1.5)}
    async with await CipSim.start(sim_tags, kind=kind) as sim:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        out = await read_all(drv)
        assert_values(sim, tags, out)
        assert out["MSG"].value == "pump 1 running" and out["EMPTY"].value == ""
        await drv.close()


# --- Micro800 ------------------------------------------------------------------------------------

async def test_micro800_batching_accepted(executor) -> None:
    sim_tags = make_logix_tags(60, seed=3)
    async with await CipSim.start(sim_tags, kind="micro800", batching=True) as sim:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        ident = await drv.identity()
        assert ident["micro800"] is True and ident["batching"] is True, ident
        assert "probe ok" in ident["batching_reason"]
        sim.reset_counters()
        out = await read_all(drv)
        assert_values(sim, tags, out)
        names = distinct_names(drv)
        assert sim.service_counts[SVC_MSP] >= 1
        assert sim.request_count < names / 3, (sim.request_count, names)
        await drv.close()


async def test_micro800_batching_refused_falls_back_to_one_request_per_tag(executor) -> None:
    sim_tags = make_logix_tags(60, seed=4)
    async with await CipSim.start(sim_tags, kind="micro800", batching=False) as sim:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor)
        await drv.connect()                                        # the probe's 0x08 does not fail connect
        assert sim.service_counts[SVC_MSP] == 1                    # exactly one probe
        ident = await drv.identity()
        assert ident["batching"] is False and "0x08" in ident["batching_reason"], ident
        sim.reset_counters()
        out = await read_all(drv)
        assert_values(sim, tags, out)
        names = distinct_names(drv)
        assert sim.service_counts[SVC_MSP] == 0
        assert sim.service_counts[SVC_READ] == names and sim.request_count == names
        await drv.close()


@pytest.mark.parametrize("batching", ["on", "off"])
async def test_micro800_batching_override(executor, batching: str) -> None:
    sim_tags = make_logix_tags(20, seed=5)
    async with await CipSim.start(sim_tags, kind="micro800", batching=True) as sim:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor, batching=batching)
        await drv.connect()
        assert sim.service_counts[SVC_MSP] == 0                    # no probe
        sim.reset_counters()
        out = await read_all(drv)
        assert_values(sim, tags, out)
        assert (sim.service_counts[SVC_MSP] > 0) is (batching == "on")
        await drv.close()


# --- tag existence ----------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["micro800", "logix"])
async def test_unknown_tags_are_bad_config_and_never_sent(executor, kind: str) -> None:
    sim_tags = make_logix_tags(12, seed=6)
    async with await CipSim.start(sim_tags, kind=kind, batching=False) as sim:
        good = oo_tags(sim_tags)
        bogus = [Tag(name="NOT_THERE", address="NOT_THERE", dtype="float32"),
                 Tag(name="UDT_0000.NOPE", address="UDT_0000.NOPE", dtype="int32")]
        drv = make_driver(sim, good + bogus, executor, batching="off")
        await drv.connect()
        sim.reset_counters()
        out = await read_all(drv)
        assert out["NOT_THERE"].quality == "bad_config" and out["UDT_0000.NOPE"].quality == "bad_config"
        assert_values(sim, good, out)
        assert sim.service_counts[SVC_READ] == distinct_names(drv) - len(bogus)   # the bogus names stay off the wire
        await drv.close()


async def test_tag_removed_after_upload_is_bad_config_on_the_wire(executor) -> None:
    sim_tags = make_logix_tags(12, seed=7)
    async with await CipSim.start(sim_tags, kind="micro800", batching=True) as sim:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        victim = next(n for n, (spec, _v) in sim_tags.items() if spec == "REAL")
        sim.remove(victim)                                         # "program download"
        out = await read_all(drv)
        assert out[victim].quality == "bad_config"
        rest = [t for t in tags if t.name != victim]
        assert_values(sim, rest, out)
        await drv.close()


# --- faults -------------------------------------------------------------------------------------

async def test_mute_plc_gives_comm_error_then_recovers(executor) -> None:
    sim_tags = make_logix_tags(10, seed=8)
    async with await CipSim.start(sim_tags, kind="micro800") as sim:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor, timeout_ms=300)
        await drv.connect()
        block = blocks(drv)[0]
        sim.mute = True
        started = time.monotonic()
        with pytest.raises(CommError) as info:
            await drv.read(block)
        assert not isinstance(info.value, ConnectionLost)
        assert "no reply within 300 ms" in str(info.value)
        assert time.monotonic() - started < 1.5                    # one timeout, no Forward Close wait
        assert not drv.connected                                   # closed on timeout
        sim.mute = False
        await drv.connect()
        assert_values(sim, tags, await read_all(drv))
        await drv.close()


async def test_power_off_gives_connection_lost_and_power_on_reconnects(executor) -> None:
    sim_tags = make_logix_tags(20, seed=9)
    sim = await CipSim.start(sim_tags, kind="micro800", port=stable_port())
    try:
        tags = oo_tags(sim_tags)
        drv = make_driver(sim, tags, executor)
        await drv.connect()
        assert_values(sim, tags, await read_all(drv))
        await sim.stop()
        started = time.monotonic()
        with pytest.raises(ConnectionLost):
            await drv.read(blocks(drv)[0])
        assert time.monotonic() - started < 1.0
        assert not drv.connected
        with pytest.raises(ConnectionLost, match="connect failed"):
            await drv.connect()
        await sim.restart()
        sim.reset_counters()
        await drv.connect()
        assert sim.service_counts[SVC_TAG_LIST] == 0               # tag list reused, not re-uploaded
        assert (await drv.identity())["batching"] is True
        sim.set(next(iter(sim_tags)), sim.value(next(iter(sim_tags))))
        assert_values(sim, tags, await read_all(drv))
        await drv.close()
    finally:
        await sim.stop()


# --- through the real ChannelWorker ---------------------------------------------------------------

async def test_channel_worker_end_to_end(executor) -> None:
    """The core drives the driver: cycles, a mute PLC, a power cycle, reconnect without re-upload."""
    from opencap.config.models import Channel
    from opencap.core.cache import ValueCache
    from opencap.core.channel_worker import ChannelWorker
    from opencap.core.registry import plan_device

    sim_tags = make_logix_tags(30, seed=10)
    sim = await CipSim.start(sim_tags, kind="micro800", batching=False, port=stable_port())
    tags = oo_tags(sim_tags)
    device = make_driver(sim, tags, executor, timeout_ms=300).device
    channel = Channel(name="SIM", devices=[device])
    worker = ChannelWorker(channel, {device.id: plan_device(device)}, ValueCache(), start_jitter=0.0,
                           backoff_base_s=0.05, backoff_max_s=0.2)
    try:
        snap = await worker.read_once(device.id)
        assert_values(sim, tags, snap)
        assert worker.device_status(device.id)["connected"] is True

        sim.mute = True
        snap = await worker.read_once(device.id)
        assert {v.quality for v in snap.values()} == {"bad_comm"}
        sim.mute = False
        assert_values(sim, tags, await worker.read_once(device.id))

        await sim.stop()
        snap = await worker.read_once(device.id)
        assert {v.quality for v in snap.values()} == {"bad_comm"}
        await sim.restart()
        sim.reset_counters()
        assert_values(sim, tags, await worker.read_once(device.id))
        assert sim.service_counts[SVC_TAG_LIST] == 0               # same driver instance: list reused
        assert sim.write_attempts == 0
    finally:
        await worker.stop()
        await sim.stop()
