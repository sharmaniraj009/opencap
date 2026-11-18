"""Load on the live feed (#31): 50 browser-equivalent WebSocket clients, each subscribed to 5,000
tags on Modbus simulators, must not make the poller overrun its scan rates.

The clients run in their own process (tests/helpers/ws_load_client.py), as browsers would; the
poller, its API and the simulators share this process's event loop. Every value changes on
every poll, the worst case for the feed. Marked `acceptance` (about 30 s); the client count can
be set with OPENCAP_LIVE_CLIENTS.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from opencap import metrics
from opencap.api import live
from opencap.core.runtime import Poller
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims
from tests.integration.test_live_feed import ENV, _until, serve_api

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).parents[2]
CLIENTS = int(os.environ.get("OPENCAP_LIVE_CLIENTS", "50"))
DEVICES, TAGS_PER_DEVICE = 10, 500          # 5,000 tags
SCAN_MS = 1000
SECONDS = 12


def _overruns(ids: list[str]) -> dict[str, float]:
    return {i: metrics.POLL_OVERRUN.labels(device=i)._value.get() for i in ids}


async def test_50_clients_x_5000_tags_without_poll_overruns(tmp_path):
    configure_logging("WARNING")
    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(DEVICES, TAGS_PER_DEVICE, seed=41, scan_ms=SCAN_MS, timeout_ms=1000))
        try:
            devices = [s.device for s in sims]
            ids = [d.id for d in devices]
            tags = sum(len([t for t in d.tags if t.enabled]) for d in devices)
            poller = Poller(write_config(tmp_path, devices, mqtt_port=broker.port), serve_api=False)
            await poller.start()
            try:
                await _until(lambda: all(poller.device_detail(i)["state"] == "ok" for i in ids), 20, "devices ok")
                async with serve_api(poller, tmp_path) as (port, app):
                    before = _overruns(ids)
                    lag = {"max": 0.0}

                    async def ticker():                   # every value changes before every poll
                        while True:
                            for s in sims:
                                s.tick()
                            await asyncio.sleep(SCAN_MS / 2000)

                    async def lag_probe():                 # how late the event loop wakes up
                        while True:
                            t = time.monotonic()
                            await asyncio.sleep(0.05)
                            lag["max"] = max(lag["max"], time.monotonic() - t - 0.05)

                    helpers = [asyncio.create_task(ticker()), asyncio.create_task(lag_probe())]
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "tests.helpers.ws_load_client", f"ws://127.0.0.1:{port}/api/v1/ws",
                            ENV["T_VIEW"], str(CLIENTS), str(SECONDS), *ids,
                            cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        out, err = await asyncio.wait_for(proc.communicate(), SECONDS + 60)
                    finally:
                        for h in helpers:
                            h.cancel()
                        await asyncio.gather(*helpers, return_exceptions=True)
                    after = _overruns(ids)
                    cycles = {i: poller.device_detail(i)["cycle_ms"] for i in ids}
                    resyncs = app.state.live.resyncs
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)

    assert proc.returncode == 0, err.decode()[-3000:]
    summary = json.loads(out.decode().strip().splitlines()[-1])
    detail = f"summary={summary} lag_max={lag['max']:.3f}s cycles_ms={cycles} resyncs={resyncs}"
    assert after == before, f"poll overruns under load: {after} vs {before}; {detail}"
    # steady state stays well under a scan period (about 0.1 s here, for the connection burst)
    assert lag["max"] < 0.5, detail
    assert all(c is not None and c < SCAN_MS for c in cycles.values()), detail
    assert summary["clients"] == CLIENTS and not summary["failed"] and not summary["errors"], detail
    assert all(t == tags for t in summary["tags"]), detail                    # each client holds every tag
    assert all(s >= DEVICES for s in summary["snapshots"]), detail
    # values change on every 1 s poll: about one batch per device and second, after the snapshots
    assert min(summary["changes"]) >= DEVICES * (SECONDS - 4), detail
    assert summary["stale_batches"] == 0, detail
    assert summary["min_gap"] >= live.CONFLATE_S - 0.06, detail
