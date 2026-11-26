"""`Driver.browse()` and `opencap browse` for ab_logix against the CIP simulator (#11).

Real pycomm3 on the wire, Logix and Micro800: atomic, string, array and UDT tags come back as opencap
tags that plan cleanly and read good through the channel worker, browse sends nothing beyond the
connect-time tag-list upload, and only read/list CIP services ever reach the controller.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

cip_sim = pytest.importorskip("tests.helpers.cip_sim")

from opencap.__main__ import app  # noqa: E402
from opencap.config.models import Channel, Device, Tag  # noqa: E402
from opencap.core.browse import BrowseError, browse_device, write_tags  # noqa: E402
from opencap.core.cache import ValueCache  # noqa: E402
from opencap.core.channel_worker import ChannelWorker  # noqa: E402
from opencap.core.registry import plan_device  # noqa: E402
from opencap.drivers._cip_common import WRITE_SERVICES  # noqa: E402
from opencap.drivers.ab_logix import AbLogixDriver  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402

CipSim = cip_sim.CipSim

# Read and list services only: Get Attributes All/List, Read Tag (+ fragmented), Multiple Service
# Packet (the Micro800 batching probe reads two tags), Get Instance Attribute List (tag-list upload).
READ_LIST_SERVICES = {0x01, 0x03, 0x0A, 0x4C, 0x52, 0x55}

LOGIX_TAGS: dict[str, tuple[str, Any]] = {
    "FLOW_1": ("REAL", 12.5),
    "COUNT_1": ("DINT", -7),
    "MODE_1": ("INT", 3),
    "SMALL_1": ("SINT", -5),
    "RUN_1": ("BOOL", True),
    "BIG_1": ("LINT", 1 << 40),
    "LABEL_1": ("STRING", "Line 1"),
    "TREND_1": ("REAL[4]", [1.0, 2.0, 3.0, 4.0]),
    "PUMP_1": ("UDT:SimUdt", {"TOTAL": ("REAL", 99.5), "COUNT": ("DINT", 4), "RUN": ("BOOL", True),
                              "MODE": ("INT", 2), "HIST": ("REAL[3]", [0.5, 1.5, 2.5])}),
}
MICRO_TAGS: dict[str, tuple[str, Any]] = {k: v for k, v in LOGIX_TAGS.items() if not v[0].startswith("UDT:")}
MICRO_TAGS["LABEL_1"] = ("STRING", "M8")          # SHORT_STRING on a Micro800


def device(sim: Any, tags: list[Tag] | None = None) -> Device:
    micro800 = sim.kind == "micro800"
    params: dict[str, Any] = {"micro800": micro800}
    if not micro800:
        params |= {"path": f"1,{sim.slot}", "slot": sim.slot}
    return Device(id=f"SIM/{sim.kind}", channel="SIM", name=sim.kind, driver="ab_logix", host=sim.host,
                  port=sim.port, params=params, tags=tags or [], timeout_ms=1500)


def assert_read_only(sim: Any) -> None:
    assert sim.write_attempts == 0
    assert not set(sim.service_counts) & set(WRITE_SERVICES)
    assert set(sim.service_counts) <= READ_LIST_SERVICES, dict(sim.service_counts)


@pytest.mark.parametrize("kind", ["logix", "micro800"])
async def test_browse_sends_nothing_after_connect(kind):
    sim = await CipSim.start(LOGIX_TAGS if kind == "logix" else MICRO_TAGS, kind=kind)
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            drv = AbLogixDriver(device(sim), ex)
            await drv.connect()
            before = sim.request_count
            result = await drv.browse(expand_arrays=True)
            assert sim.request_count == before                  # the tag list from connect() is reused
            await drv.close()
        assert result.tags and all(isinstance(t, Tag) for t in result.tags)
        assert_read_only(sim)
    finally:
        await sim.stop()


async def test_logix_browse_maps_every_kind_of_tag():
    sim = await CipSim.start(LOGIX_TAGS, kind="logix")
    try:
        report = await browse_device(device(sim))
        by = {t.name: t for t in report.tags}
        assert {n: (t.address, t.dtype) for n, t in by.items()} == {
            "BIG_1": ("BIG_1", "int64"), "COUNT_1": ("COUNT_1", "int32"), "FLOW_1": ("FLOW_1", "float32"),
            "LABEL_1": ("LABEL_1", "string"), "MODE_1": ("MODE_1", "int16"), "RUN_1": ("RUN_1", "bool"),
            "SMALL_1": ("SMALL_1", "int16"),
            "PUMP_1.TOTAL": ("PUMP_1.TOTAL", "float32"), "PUMP_1.COUNT": ("PUMP_1.COUNT", "int32"),
            "PUMP_1.RUN": ("PUMP_1.RUN", "bool"), "PUMP_1.MODE": ("PUMP_1.MODE", "int16"),
        }
        assert set(report.skipped) == {"TREND_1", "PUMP_1.HIST"}       # arrays without --expand-arrays
        assert report.invalid == {}
        expanded = await browse_device(device(sim), expand_arrays=True)
        assert {"TREND_1[0]", "TREND_1[3]", "PUMP_1.HIST[2]"} <= {t.name for t in expanded.tags}
        assert expanded.skipped == {} and expanded.invalid == {}
        assert_read_only(sim)
    finally:
        await sim.stop()


@pytest.mark.parametrize("kind", ["logix", "micro800"])
async def test_generated_tags_read_good_through_the_worker(kind):
    sim_tags = LOGIX_TAGS if kind == "logix" else MICRO_TAGS
    sim = await CipSim.start(sim_tags, kind=kind)
    try:
        report = await browse_device(device(sim), expand_arrays=True)
        dev = device(sim, report.tags)
        plan = plan_device(dev)
        assert plan.ok and all(g.plan.invalid == {} for g in plan.groups) and plan.enabled_tags == len(report.tags)
        cache = ValueCache()
        cache.register(dev)
        worker = ChannelWorker(Channel(name="SIM", devices=[dev]), {dev.id: plan}, cache)
        try:
            values = await worker.read_once(dev.id)
        finally:
            await worker.stop()
        assert {n: tv.quality for n, tv in values.items() if tv.quality != "good"} == {}
        assert values["FLOW_1"].value == 12.5 and values["TREND_1[2]"].value == 3.0
        assert values["LABEL_1"].value == sim_tags["LABEL_1"][1]
        if kind == "logix":
            assert values["PUMP_1.TOTAL"].value == 99.5 and values["PUMP_1.HIST[1]"].value == 1.5
        assert_read_only(sim)
    finally:
        await sim.stop()


async def test_browse_errors():
    dev = Device(id="M/D", channel="M", name="D", driver="modbus_tcp", host="192.0.2.1", port=502, tags=[])
    with pytest.raises(BrowseError, match="does not support browse"):
        await browse_device(dev)
    sim = await CipSim.start(MICRO_TAGS, kind="micro800")
    port = sim.port
    await sim.stop()                                                  # nothing listens there now
    dead = Device(id="M/L", channel="M", name="L", driver="ab_logix", host="127.0.0.1", port=port,
                  params={"micro800": True}, tags=[], timeout_ms=500)
    with pytest.raises(BrowseError, match="M/L"):
        await browse_device(dead)


def test_write_tags_formats(tmp_path):
    tags = [Tag(name="A", address="A", dtype="int32", description="DINT")]
    write_tags(tags, tmp_path / "t.json")
    assert TypeAdapter(list[Tag]).validate_json((tmp_path / "t.json").read_text(encoding="utf-8")) == tags
    with pytest.raises(BrowseError, match=".json or .csv"):
        write_tags(tags, tmp_path / "t.yaml")
    try:
        import opencap.config.kep_csv  # noqa: F401
    except ImportError:
        with pytest.raises(BrowseError, match="CSV writer"):
            write_tags(tags, tmp_path / "t.csv")
    else:
        write_tags(tags, tmp_path / "t.csv")
        assert (tmp_path / "t.csv").read_text(encoding="utf-8").startswith("Tag Name,Address,Data Type")


async def test_cli_browse(tmp_path):
    sim = await CipSim.start(LOGIX_TAGS, kind="logix")
    try:
        config_dir = write_config(tmp_path, [device(sim, [Tag(name="FLOW_1", address="FLOW_1", dtype="float32")])])
        out = tmp_path / "tags.json"
        runner = CliRunner()
        res = await _invoke(runner, ["browse", "SIM/logix", "--out", str(out), "--config", str(config_dir),
                                     "--expand-arrays"])
        assert res.exit_code == 0, res.output
        tags = TypeAdapter(list[Tag]).validate_json(out.read_text(encoding="utf-8"))
        assert len(tags) == 18 and "SIM/logix: 18 tags generated, 0 skipped" in res.output
        res = await _invoke(runner, ["browse", "SIM/logix", "--config", str(config_dir)])
        assert res.exit_code == 0, res.output
        printed = json.loads(res.stdout)
        assert len(printed) == 11 and "skipped TREND_1: array REAL[4]" in res.stderr
        assert_read_only(sim)
    finally:
        await sim.stop()


async def _invoke(runner: CliRunner, args: list[str]):
    """The CLI runs its own event loop (asyncio.run): call it from a thread while the sim serves."""
    import asyncio

    return await asyncio.to_thread(runner.invoke, app, args)
