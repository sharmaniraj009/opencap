"""Simulated acceptance for the s7 and melsec_mc drivers through the whole poller
(ChannelWorker, cache, publisher, MQTT), against the S7 and MC 3E simulators.

S7 fleet (python-snap7 on both ends of the wire):
- 6 CPUs: big and little endian, KEPServer's max_pdu 960 negotiated down to 480 (a block split
  in two requests) and 960 kept (one request), rack 0 slot 2 with link type PG, a missing DB.
- every device publishes correct values; a tag in a missing DB is reported once as bad_config
  (BadTags) and the CPU is asked for it once only;
- values follow the PLC; powering off one CPU demotes only that device, it resumes after power-on;
- `opencap identity` reports the CPU (SZL) and the negotiated PDU; `device.stats` logs cycle times;
- no simulator ever sees a write.

MELSEC fleet (stock pymcprotocol 0.3.0 `Type3E`, subclassed, against the MC 3E simulator):
- 5 PLCs: TCP and UDP, `first_word_low` on and off, a 960-word block (the 3E limit) whose replies
  arrive split across TCP writes, a device past the PLC's D range;
- same checks: correct values, the out-of-range block reported once as bad_config and asked for
  once, values follow the PLC, a power cycle demotes only that PLC, `opencap identity` reports
  the CPU model.

The real-site parts (values match KEPServer on a real S7 and a real MELSEC site, link type and
plc_type confirmed, 24 h without errors) need site access.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from dataclasses import dataclass
from typing import Any, Callable

import pytest

# the simulators import python-snap7 and pymcprotocol: without them, skip this module instead
# of failing collection for the whole suite
pytest.importorskip("snap7")
pytest.importorskip("pymcprotocol")

from structlog.testing import capture_logs  # noqa: E402

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.core.runtime import Poller  # noqa: E402
from opencap.logsetup import configure_logging  # noqa: E402
from tests.helpers.broker import TestBroker  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.engine import run_process  # noqa: E402
from tests.helpers.mc_sim import McSim, make_mc_tags  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402
from tests.helpers.s7_sim import S7Sim, load, make_s7_tags  # noqa: E402
from tests.integration.test_melsec_sim import expected as mc_expected  # noqa: E402

# --engine=rust: skipped as "driver not ported" until the binary implements s7, melsec_mc
pytestmark = [pytest.mark.acceptance, pytest.mark.drivers("s7", "melsec_mc")]

S7_TAGS = 60
INTERVAL_MS = 2000
S7_MISSING = "S7_MISSING_DB"
BIG_ARRAY = "S7_BIG_ARRAY"            # 800 bytes in one block: two requests at PDU 480, one at 960

MC_TAGS = 60
MC_MISSING = "MC_PAST_D_RANGE"        # D12300: inside Kepware's Q range, past the PLC's 12,288 D registers
MC_BIG = "MC_960_WORDS"               # D10000[480] float32 = 960 words, the MC 3E limit for one request

MC_SPECS: list[dict[str, Any]] = [
    {"params": {}, "missing": True},
    {"params": {"protocol": "udp"}},
    {"params": {"first_word_low": False}},
    {"params": {}, "split": True, "big_block": True},
    {"params": {}, "victim": True},
]

# (params, sim kwargs, byte order big, extra tags)
S7_SPECS: list[dict[str, Any]] = [
    {"params": {}, "sim": {}, "big": True, "missing": True},
    {"params": {"byte_order_big": False}, "sim": {}, "big": False},
    {"params": {"max_pdu": 960}, "sim": {"max_pdu": 480}, "big": True, "big_array": True},
    {"params": {"max_pdu": 960}, "sim": {"max_pdu": 960}, "big": True, "big_array": True},
    {"params": {"slot": 2, "link_type": 1}, "sim": {"slot": 2}, "big": True},
    {"params": {}, "sim": {}, "big": True, "victim": True},
]


@dataclass
class Unit:
    """One simulated PLC and the device that polls it."""

    device: Device
    sim: Any
    expected: Callable[[Tag], Any]
    missing: str | None = None
    victim: bool = False


async def _wait(pred, timeout: float, what: str, step: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(step)


def _matches(got: Any, want: Any) -> bool:
    if isinstance(want, list):
        return isinstance(got, list) and len(got) == len(want) and all(_matches(g, w) for g, w in zip(got, want))
    if isinstance(want, float):
        return math.isclose(float(got), round(want, 4), rel_tol=1e-6, abs_tol=1e-4)
    return got == want


async def _opencap(*args: str, cwd) -> tuple[int, str]:
    return await run_process([sys.executable, "-m", "opencap", *args], cwd=cwd, timeout=60)


def _device(site: str, driver: str, sim: Any, tags: list[Tag], params: dict[str, Any]) -> Device:
    return Device(id=f"{site}/PLC", channel=site, name="PLC", driver=driver, host="127.0.0.1", port=sim.port,
                  params=params, scan_ms=1000, min_scan_ms=1000, timeout_ms=1000, retries=1, demote_after=3,
                  demote_for_ms=2000, tags=tags)


async def _s7_fleet() -> list[Unit]:
    units = []
    for i, spec in enumerate(S7_SPECS):
        tags, values, areas = make_s7_tags(S7_TAGS, seed=200 + i)
        if spec.get("big_array"):
            areas["DB300"] = 800
            tags.append(Tag(name=BIG_ARRAY, address="DB300,REAL0[200]", dtype="float32"))
        if spec.get("missing"):
            tags.append(Tag(name=S7_MISSING, address="DB77,W0", dtype="uint16"))
        port = stable_port() if spec.get("victim") else 0
        sim = await S7Sim.start(areas, port=port, **spec["sim"])
        big = spec["big"]
        load(sim, [t for t in tags if t.name in values], values, big=big)
        if spec.get("big_array"):
            for k in range(200):
                sim.set(f"DB300,REAL{4 * k}", "float32", k * 1.5 - 100.0)
        params = {"rack": 0, "slot": 1, "max_pdu": 480, "link_type": 3, "byte_order_big": True, **spec["params"]}

        def expected(tag: Tag, sim=sim, big=big) -> Any:
            if tag.name == BIG_ARRAY:
                return [sim.value(f"DB300,REAL{4 * k}", "float32") for k in range(200)]
            return sim.value(tag.address, tag.dtype, big=big)

        units.append(Unit(_device(f"S7_SITE_{i:02d}", "s7", sim, tags, params), sim, expected,
                          missing=S7_MISSING if spec.get("missing") else None, victim=bool(spec.get("victim"))))
    return units


async def _mc_fleet() -> list[Unit]:
    units = []
    for i, spec in enumerate(MC_SPECS):
        tags = make_mc_tags(MC_TAGS, seed=300 + i)
        if spec.get("big_block"):
            tags.append(Tag(name=MC_BIG, address="D0010000[480]", dtype="float32"))
        port = stable_port() if spec.get("victim") else 0
        sim = await McSim.start(port=port)
        params = {"plc_type": "Q", "protocol": "tcp", "first_word_low": True, "frame": "3E", **spec["params"]}
        fwl = params["first_word_low"]
        sim.fill(tags, seed=300 + i, word_order="low_first" if fwl else "high_first")
        if spec.get("split"):
            sim.split_replies = True
        if spec.get("missing"):
            tags.append(Tag(name=MC_MISSING, address="D0012300", dtype="uint16"))

        def expected(tag: Tag, sim=sim, fwl=fwl) -> Any:
            return mc_expected(sim, tag, first_word_low=fwl)

        units.append(Unit(_device(f"MC_SITE_{i:02d}", "melsec_mc", sim, tags, params), sim, expected,
                          missing=MC_MISSING if spec.get("missing") else None, victim=bool(spec.get("victim"))))
    return units


async def test_s7_melsec_acceptance(tmp_path):
    configure_logging("INFO")            # device.stats is an INFO line; the filter must let it through
    units: list[Unit] = []
    try:
        units += await _s7_fleet()
        units += await _mc_fleet()
        devices = [u.device for u in units]
        async with asyncio.timeout(240), TestBroker() as broker, broker.collector("opencap/#") as col:
            await col.wait_ready()
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port,
                                      poller={"stats_interval_s": 3}, publish={"interval_ms": INTERVAL_MS})
            poller = Poller(config_dir, serve_api=False)
            with capture_logs() as logs:
                await poller.start()
                try:
                    await _scenario(tmp_path, config_dir, poller, col, units)
                finally:
                    await poller.stop()
            stats = [e for e in logs if e.get("event") == "device.stats"]
            assert {e["device"] for e in stats} >= {d.id for d in devices}, "every device logged its cycle time"
            assert all(e["mean_cycle_ms"] > 0 for e in stats), stats
            pdu_logs = [e for e in logs if e.get("event") == "s7.pdu_negotiated"]
            assert {e["device"] for e in pdu_logs} == {units[2].device.id}
        assert all(u.sim.write_attempts == 0 for u in units)
    finally:
        await asyncio.gather(*(u.sim.stop() for u in units), return_exceptions=True)


async def _scenario(tmp_path, config_dir, poller, col, units: list[Unit]) -> None:
    topics = {u.device.id: poller.registry.plans[u.device.id].device.publish.topic for u in units}

    # 1. Every device publishes correct values; only the missing tag is reported.
    await _wait(lambda: all(len(col.topic(t)) >= 2 for t in topics.values()), 40, "2 messages per device")
    for u in units:
        msg = col.last(topics[u.device.id]).json()
        want_bad = [u.missing] if u.missing else []
        assert msg.get("BadTags", []) == want_bad, (u.device.id, msg.get("BadTags"))
        assert msg["DataValid"] is (not want_bad), u.device.id
        for tag in u.device.tags:
            if tag.name == u.missing:
                continue
            assert _matches(msg["Data"][tag.name], u.expected(tag)), (u.device.id, tag.name, tag.address)
        if u.missing:
            assert poller.device_values(u.device.id)[u.missing]["quality"] == "bad_config"

    # 2. The missing DB was asked for once; PDU 480 splits the 800-byte block, PDU 960 does not.
    s7 = [u for u in units if u.device.driver == "s7"]
    assert s7[0].sim.refused_reads == 1, s7[0].sim.refused_reads                      # first cycle only
    for u in s7:
        u.sim.reset_counters()
    await asyncio.sleep(3.5)
    assert s7[0].sim.refused_reads == 0 and s7[0].sim.read_count > 0
    split, whole = s7[2].sim, s7[3].sim
    assert 800 not in split.read_sizes and max(split.read_sizes) == 462 and split.oversize_reads == 0
    assert 800 in whole.read_sizes
    print(f"\n[s7] requests in 3.5 s: PDU 480 {split.read_count}, PDU 960 {whole.read_count}; "
          f"tsaps {s7[4].sim.tsaps[-1]}")
    assert s7[4].sim.tsaps and all(t == (1, 0, 2) for t in s7[4].sim.tsaps)          # PG, rack 0, slot 2
    assert all(t[0] == 3 for t in s7[0].sim.tsaps)                                   # PC

    mc = [u for u in units if u.device.driver == "melsec_mc"]
    assert mc[0].sim.error_replies == {0xC056: 1}, mc[0].sim.error_replies                 # first cycle only
    for u in mc:
        u.sim.reset_counters()
    await asyncio.sleep(3.5)
    assert not mc[0].sim.error_replies and mc[0].sim.request_count > 0
    assert mc[1].sim.frames["udp"] > 0 and mc[1].sim.frames["tcp"] == 0
    reads = {u.device.id: u.sim.command_counts[(0x0401, 0x0000)] for u in mc}
    print(f"[mc] batch reads in 3.5 s: {reads}")

    # 3. Values follow the PLC.
    for u in (s7[0], s7[1], mc[1], mc[2]):
        real = next(t for t in u.device.tags if t.dtype == "float32" and "[" not in t.address)
        if u.device.driver == "s7":
            u.sim.set(real.address, "float32", 12.5, big=u.device.params.get("byte_order_big", True))
        else:
            order = "low_first" if u.device.params["first_word_low"] else "high_first"
            u.sim.set(real.address, 12.5, "float32", word_order=order)
        await _wait(lambda: col.last(topics[u.device.id]).json()["Data"].get(real.name) == 12.5,
                    3 * INTERVAL_MS / 1000 + 2, f"{u.device.id} {real.name} = 12.5")

    # 4. Power off one PLC: only it is demoted; it resumes after power-on.
    for victim in (u for u in units if u.victim):
        vid = victim.device.id
        await victim.sim.stop()
        await _wait(lambda: poller.device_detail(vid)["state"] == "demoted", 30, f"{vid} demoted")
        others = {u.device.id: poller.device_detail(u.device.id)["state"] for u in units if u is not victim}
        assert set(others.values()) == {"ok"}, {k: v for k, v in others.items() if v != "ok"}
        await victim.sim.restart()
        await _wait(lambda: poller.device_detail(vid)["state"] == "ok", 30, f"{vid} resumed")

    # 5. `opencap identity` reports the CPU and the negotiated PDU.
    code, out = await _opencap("identity", s7[2].device.id, "--config", str(config_dir), cwd=tmp_path)
    assert code == 0, out
    ident = json.loads(out[out.index("{"):])
    assert ident["module_type_name"] and ident["order_code"].startswith("6ES7")
    assert (ident["pdu_requested"], ident["pdu_negotiated"], ident["cpu_state"]) == (960, 480, "run")
    print(f"[s7] identity {s7[2].device.id}: {ident['module_type_name']!r} {ident['order_code']!r} "
          f"{ident.get('firmware')} pdu {ident['pdu_negotiated']}")
    code, out = await _opencap("identity", mc[1].device.id, "--config", str(config_dir), cwd=tmp_path)
    assert code == 0, out
    ident = json.loads(out[out.index("{"):])
    assert (ident["cpu_type"], ident["protocol"], ident["plc_type"]) == ("Q03UDVCPU", "udp", "Q"), ident
    print(f"[mc] identity {mc[1].device.id}: {ident}")
