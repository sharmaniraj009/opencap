"""System tags on a real poller and a Modbus simulator: kill the device, bring it back (#120,
spec 13.2 test 2, through the value cache). Tags equal `device_status()` and `/metrics` (spec 11)."""

from __future__ import annotations

import asyncio

from opencap.core.runtime import Poller
from opencap.core.system_tags import split_path
from opencap.logsetup import configure_logging
from opencap.metrics import REGISTRY
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import ModbusSim, make_sim_devices


def tag(poller: Poller, path: str):
    device_id, name = split_path(path)
    tv = poller.cache.get(device_id, name)
    return None if tv is None else tv.value


async def until(pred, what: str, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


async def test_kill_one_device_and_bring_it_back(tmp_path):
    configure_logging("WARNING")
    async with TestBroker() as broker:
        device = make_sim_devices(1, 4, seed=7, scan_ms=100, timeout_ms=300)[0]
        device = device.model_copy(update={"retries": 0, "demote_after": 2, "demote_for_ms": 400})
        sim = await ModbusSim.start(device)
        device = device.model_copy(update={"port": sim.port})
        dev_path = f"{device.channel}.{device.name}"
        sys_ = f"{dev_path}._System."
        stats = f"{dev_path}._Statistics."
        poller = Poller(write_config(tmp_path, [device], mqtt_port=broker.port), serve_api=False)
        await poller.start()
        try:
            await until(lambda: tag(poller, sys_ + "_State") == "ok", "the device to read")
            assert tag(poller, sys_ + "_Error") is False and tag(poller, sys_ + "_NoError") is True
            assert tag(poller, sys_ + "_SecondsInError") == 0
            assert tag(poller, sys_ + "_FailedConnection") is False and tag(poller, sys_ + "_Connected") is True
            assert tag(poller, "_System._TotalTagCount") == 4
            await until(lambda: tag(poller, "_System._DevicesOk") == 1, "the tick to count the device")   # tick class
            assert tag(poller, "_System._ActiveTagCount") == 4

            await sim.stop()                                         # kill the device
            await until(lambda: tag(poller, sys_ + "_Error") is True, "_Error")
            assert tag(poller, sys_ + "_NoError") is False
            await until(lambda: tag(poller, sys_ + "_AutoDemoted") is True, "demotion")
            assert tag(poller, sys_ + "_State") == "demoted"
            assert tag(poller, sys_ + "_FailedConnection") is True and tag(poller, sys_ + "_Connected") is False
            await until(lambda: tag(poller, "_System._DevicesDemoted") == 1, "the tick to count the demotion")
            assert tag(poller, "_System._ActiveTagCount") == 0       # a demoted device is not scanned
            await until(lambda: tag(poller, sys_ + "_SecondsInError") >= 1, "_SecondsInError to grow")

            # the tags equal the status route's fields and /metrics (spec 11), once the engine is quiet
            status = poller.device_status(device.id)
            poller.system_tags.step()           # compute now, in the same event-loop turn as the status
            assert tag(poller, sys_ + "_ErrorCount") == status["errors"]
            assert tag(poller, sys_ + "_State") == status["state"]
            failed = REGISTRY.get_sample_value("poll_failed_reads_total", {"device": device.id}) or 0
            assert failed >= 2 and tag(poller, stats + "_FailedReads") == failed
            assert tag(poller, stats + "_ConnErrors") == REGISTRY.get_sample_value(
                "poll_requests_total", {"device": device.id, "result": "conn_error"})

            sim = await ModbusSim.start(device, port=sim.port)       # restart the simulator
            await until(lambda: tag(poller, sys_ + "_State") == "ok", "the probe to resume the device")
            assert tag(poller, sys_ + "_Error") is False and tag(poller, sys_ + "_AutoDemoted") is False
            assert tag(poller, sys_ + "_SecondsInError") == 0 and tag(poller, sys_ + "_FailedConnection") is False
            await until(lambda: tag(poller, "_System._DevicesDemoted") == 0, "the tick to clear the count")
        finally:
            await poller.stop()
            await sim.stop()
