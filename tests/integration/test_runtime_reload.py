"""Config reload (PLAN §8): only changed channels restart; a bad config keeps the old one."""

from __future__ import annotations

import asyncio

import pytest
import yaml

from opencap.core.runtime import Poller
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims


async def _wait(pred, timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def test_reload_restarts_only_changed_channels(tmp_path):
    configure_logging("WARNING")
    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(3, 10, seed=11, scan_ms=200, timeout_ms=500))
        try:
            devices = [s.device for s in sims]
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port)
            poller = Poller(config_dir, serve_api=False)
            await poller.start()
            try:
                ids = [d.id for d in devices]
                chans = [d.channel for d in devices]
                await _wait(lambda: all(poller.device_detail(i)["state"] == "ok" for i in ids), 10, "all ok")
                before = dict(poller.workers)

                # 1. Change one device's scan rate: only its channel restarts.
                overrides = config_dir / "overrides.yaml"
                overrides.write_text(yaml.safe_dump({"devices": {ids[1]: {"scan_ms": 400}}}))
                changed = await poller.reload()
                assert changed["restarted"] == [chans[1]]
                assert sorted(changed["unchanged"]) == sorted([chans[0], chans[2]])
                assert poller.workers[chans[0]] is before[chans[0]]
                assert poller.workers[chans[2]] is before[chans[2]]
                assert poller.workers[chans[1]] is not before[chans[1]]
                await _wait(lambda: poller.device_detail(ids[1])["state"] == "ok", 10, "restarted channel ok")
                assert poller.registry.plans[ids[1]].device.scan_ms == 400

                # 2. Disable a channel: its worker stops, the others keep running.
                overrides.write_text(yaml.safe_dump({
                    "devices": {ids[1]: {"scan_ms": 400}},
                    "channels": {chans[2]: {"enabled": False}},
                }))
                changed = await poller.reload()
                assert changed["stopped"] == [chans[2]]
                assert chans[2] not in poller.workers
                with pytest.raises(KeyError):
                    poller.device_detail(ids[2])
                assert poller.device_detail(ids[0])["state"] == "ok"

                # 3. A broken config is rejected and the old one keeps running.
                good_workers = dict(poller.workers)
                (config_dir / "devices.json").write_text("{ not json")
                with pytest.raises(Exception):
                    await poller.reload()
                assert poller.workers == good_workers
                assert poller.device_detail(ids[0])["state"] == "ok"
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)


async def test_only_channels_polls_just_those_sites(tmp_path):
    configure_logging("WARNING")
    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(3, 5, seed=12, scan_ms=200, timeout_ms=500))
        try:
            devices = [s.device for s in sims]
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port)
            poller = Poller(config_dir, serve_api=False, only_channels=[devices[1].channel])
            await poller.start()
            try:
                assert list(poller.workers) == [devices[1].channel]
                await _wait(lambda: poller.device_detail(devices[1].id)["state"] == "ok", 10, "selected site ok")
                assert sims[0].request_count == 0 and sims[2].request_count == 0
                changed = await poller.reload()          # reload keeps the selection
                assert list(poller.workers) == [devices[1].channel]
                assert changed["unchanged"] == [devices[1].channel]
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)
