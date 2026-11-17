"""Poller.device_plan / device_identity (device page of the UI) on a real poller and simulator."""

from __future__ import annotations

import asyncio

import pytest

from opencap.core.runtime import Poller
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims


async def test_device_plan_and_identity(tmp_path):
    configure_logging("WARNING")
    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(1, 8, seed=21, scan_ms=200, timeout_ms=500))
        try:
            dev = sims[0].device
            poller = Poller(write_config(tmp_path, [dev], mqtt_port=broker.port), serve_api=False)
            await poller.start()
            try:
                deadline = asyncio.get_running_loop().time() + 10
                while poller.device_detail(dev.id)["state"] != "ok":
                    assert asyncio.get_running_loop().time() < deadline, "device never ok"
                    await asyncio.sleep(0.05)
                plan = poller.device_plan(dev.id)
                assert plan["id"] == dev.id and plan["blocks"] == poller.registry.plans[dev.id].blocks
                assert all(b["describe"].startswith("FC") for b in plan["block_plan"])
                ident = await poller.device_identity(dev.id)
                assert ident["connected"] is True and isinstance(ident["identity"], dict)
                with pytest.raises(KeyError):
                    poller.device_plan("NOPE/X")
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)
