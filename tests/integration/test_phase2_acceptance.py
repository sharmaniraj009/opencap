"""PLAN.md Phase 2 acceptance in simulation: Allen-Bradley Micro800 + Logix through the whole
poller (ChannelWorker, cache, publisher, MQTT), against the CIP simulator.

- 10 Micro800 (5 accept the Multiple Service Packet, 5 refuse it) and 3 Logix controllers;
- every device publishes correct values; a tag missing in the PLC is reported once as
  bad_config (BadTags) and never sent;
- Micro800 batching cuts the request count; refused batching falls back and still reads;
- powering off one PLC demotes only that device, and it resumes after power-on;
- `opencap identity` reports the batching outcome; `device.stats` logs cycle times.

Engine-agnostic (ADR-0014 §3): the engine is driven only through its config directory, its
API, its CLI, its log and the wire, so `--engine=rust` runs the same scenario against the Rust
binary once it implements `ab_logix` (until then: skipped, "driver not ported").

The real-site parts of Phase 2 (values match KEPServer on one real Micro800 and one real
Logix site, no CIP connection errors over 24 h) need site access.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from opencap.config.models import Device, Tag
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.cip_sim import CipSim, make_logix_tags
from tests.helpers.configdir import write_config
from tests.helpers.netcut import stable_port
from tests.integration.test_ab_logix_sim import expected, oo_tags

pytestmark = [pytest.mark.acceptance, pytest.mark.drivers("ab_logix")]

N_MICRO = 10
N_LOGIX = 3
SIM_TAGS = 100                       # the real Micro800 devices average ~107 tags
INTERVAL_MS = 2000
MISSING = "NOT_IN_PLC"
VICTIM = 3                           # a batching Micro800 that we power off


async def _wait(pred, timeout: float, what: str, step: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(step)


def _device(i: int, sim: CipSim, kind: str, sim_tags: dict, *, missing: bool) -> Device:
    tags = oo_tags(sim_tags) + ([Tag(name=MISSING, address=MISSING, dtype="float32")] if missing else [])
    params = {"micro800": True} if kind == "micro800" else {"path": "1,0", "slot": 0}
    site = f"AB_SITE_{i:02d}"
    return Device(id=f"{site}/PLC", channel=site, name="PLC", driver="ab_logix", host="127.0.0.1",
                  port=sim.port, params=params, scan_ms=1000, min_scan_ms=1000, timeout_ms=1000,
                  retries=1, demote_after=3, demote_for_ms=2000, tags=tags)


def _matches(got, want) -> bool:
    if isinstance(want, float):
        return math.isclose(float(got), round(want, 4), rel_tol=1e-6, abs_tol=1e-4)
    return got == want


async def test_phase2_acceptance(tmp_path, engine_factory):
    configure_logging("INFO")            # device.stats is an INFO line; the filter must let it through
    specs = [("micro800", i % 2 == 0) for i in range(N_MICRO)] + [("logix", True)] * N_LOGIX
    sims: list[CipSim] = []
    try:
        for i, (kind, batching) in enumerate(specs):
            sims.append(await CipSim.start(make_logix_tags(SIM_TAGS, seed=100 + i), kind=kind, batching=batching))
        devices = [_device(i, sim, kind, make_logix_tags(SIM_TAGS, seed=100 + i), missing=i in (0, N_MICRO))
                   for i, (sim, (kind, _b)) in enumerate(zip(sims, specs))]
        async with asyncio.timeout(240), TestBroker() as broker, broker.collector("opencap/#") as col:
            await col.wait_ready()
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, api_port=stable_port(),
                                      poller={"stats_interval_s": 3, "log_level": "INFO"},
                                      publish={"interval_ms": INTERVAL_MS})
            poller = engine_factory(config_dir)     # the Python Poller, or the Rust engine (--engine=rust)
            await poller.start()
            try:
                await _scenario(tmp_path, config_dir, poller, col, sims, specs, devices)
            finally:
                await poller.stop()
            stats = poller.log_events("device.stats")
            assert {e["device"] for e in stats} >= {d.id for d in devices}, "every device logged its cycle time"
            assert all(e["mean_cycle_ms"] > 0 for e in stats)
    finally:
        await asyncio.gather(*(s.stop() for s in sims), return_exceptions=True)


async def _scenario(tmp_path, config_dir, poller, col, sims, specs, devices) -> None:
    loaded = {d.id: d for c in poller.loaded.channels for d in c.devices}
    topics = {d.id: loaded[d.id].publish.topic for d in devices}

    # 1. Every device publishes correct values; the missing tag is reported, nothing else.
    await _wait(lambda: all(len(col.topic(t)) >= 2 for t in topics.values()), 40, "2 messages per device")
    for i, (dev, sim) in enumerate(zip(devices, sims)):
        msg = col.last(topics[dev.id]).json()
        want_bad = [MISSING] if any(t.name == MISSING for t in dev.tags) else []
        assert msg.get("BadTags", []) == want_bad, (dev.id, msg.get("BadTags"))
        assert msg["DataValid"] is (not want_bad), dev.id
        for tag in dev.tags:
            if tag.name == MISSING:
                continue
            assert _matches(msg["Data"][tag.name], expected(sim, tag)), (dev.id, tag.name)
    for i in (0, N_MICRO):
        assert poller.device_values(devices[i].id)[MISSING]["quality"] == "bad_config"

    # 2. Batching: an accepting Micro800 needs far fewer requests than a refusing one.
    for s in sims:
        s.reset_counters()
    await asyncio.sleep(3.5)
    on = [s.request_count for s, (k, b) in zip(sims, specs) if k == "micro800" and b]
    off = [s.request_count for s, (k, b) in zip(sims, specs) if k == "micro800" and not b]
    assert min(off) > 5 * max(on), (on, off)
    print(f"\n[phase2] requests in 3.5 s: Micro800 batching {on}, refused {off}")

    # 3. Values follow the PLC: change one REAL in a refusing Micro800 and in a Logix.
    for idx in (1, N_MICRO + 1):
        real = next(t.name for t in devices[idx].tags if t.dtype == "float32" and "." not in t.address
                    and "[" not in t.address and t.name != MISSING)
        sims[idx].set(real, 12.5)
        await _wait(lambda: col.last(topics[devices[idx].id]).json()["Data"].get(real) == 12.5,
                    3 * INTERVAL_MS / 1000 + 2, f"{devices[idx].id} {real} = 12.5")

    # 4. Power off one PLC: only it is demoted; it resumes after power-on.
    victim = devices[VICTIM].id
    await sims[VICTIM].stop()
    await _wait(lambda: poller.device_detail(victim)["state"] == "demoted", 30, "victim demoted")
    others = {d.id: poller.device_detail(d.id)["state"] for d in devices if d.id != victim}
    assert set(others.values()) == {"ok"}, {k: v for k, v in others.items() if v != "ok"}
    await sims[VICTIM].restart()
    await _wait(lambda: poller.device_detail(victim)["state"] == "ok", 30, "victim resumed")

    # 5. `opencap identity` reports the batching outcome per device.
    for idx, want in ((0, True), (1, False), (N_MICRO, None)):
        code, out = await poller.cli("identity", devices[idx].id, "--config", str(config_dir), cwd=tmp_path)
        assert code == 0, out
        ident = json.loads(out[out.index("{"):])
        assert ident.get("micro800") is (idx < N_MICRO), ident
        if want is not None:
            assert ident.get("batching") is want, ident
        print(f"[phase2] identity {devices[idx].id}: product={ident.get('product_name')!r} "
              f"batching={ident.get('batching')} reason={ident.get('batching_reason')!r}")
