"""The northbound OPC UA server inside the Poller: off by default; start, reload and stop."""

from __future__ import annotations

import asyncio
import time

import pytest
import yaml

pytest.importorskip("asyncua")

from asyncua import Client, ua  # noqa: E402

from opencap.core.runtime import Poller  # noqa: E402
from opencap.logsetup import configure_logging  # noqa: E402
from tests.helpers.broker import TestBroker  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402


async def wait_for(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = pred()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def test_off_by_default(tmp_path):
    configure_logging("WARNING")
    devices = make_sim_devices(1, 3, seed=5)
    poller = Poller(write_config(tmp_path, devices, mqtt_port=stable_port()), serve_api=False)
    assert poller.loaded.poller.opcua_server.enabled is False
    await poller.start()
    try:
        assert poller.opcua is None
    finally:
        await poller.stop()


async def test_serves_cache_values_and_follows_reload(tmp_path):
    configure_logging("WARNING")
    port = stable_port()
    endpoint = f"opc.tcp://127.0.0.1:{port}/opencap"
    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(2, 6, seed=21, scan_ms=200, timeout_ms=500))
        try:
            devices = [s.device for s in sims]
            ua_cfg = {"enabled": True, "endpoint": endpoint, "update_ms": 50, "anonymous": True}
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, poller={"opcua_server": ua_cfg})
            poller = Poller(config_dir, serve_api=False)
            await poller.start()
            try:
                assert poller.opcua is not None and poller.opcua.running
                ids = [d.id for d in devices]
                await wait_for(lambda: all(poller.device_detail(i)["state"] == "ok" for i in ids), 10, "devices ok")
                dev = devices[0]
                tag = next(t for t in dev.tags if t.dtype != "string")
                node = poller.opcua.node_id(dev.id, tag.name)
                assert node == f"ns=2;s={dev.channel}.{dev.name}.{tag.name}"
                async with Client(endpoint, timeout=2) as c:
                    async def matches() -> bool:
                        dv = await c.get_node(node).read_data_value(raise_on_bad_status=False)
                        cached = poller.device_values(dev.id)[tag.name]
                        return dv.StatusCode.is_good() and dv.Value.Value == pytest.approx(cached["value"])
                    await wait_for(matches, 5, "OPC UA value == cache value")
                    state = await c.get_node(poller.opcua.node_id(dev.id, "_System._State")).read_value()
                    assert state == "ok"

                    # reload: disable the second channel -> its nodes go away, the server keeps running
                    first = poller.opcua
                    (config_dir / "overrides.yaml").write_text(yaml.safe_dump(
                        {"channels": {devices[1].channel: {"enabled": False}}}))
                    await poller.reload()
                    assert poller.opcua is first
                    gone = f"ns=2;s={devices[1].channel}.{devices[1].name}._System._State"
                    dv = await c.get_node(gone).read_data_value(raise_on_bad_status=False)
                    assert dv.StatusCode.value == ua.StatusCodes.BadNodeIdUnknown
                    assert (await c.get_node(node).read_data_value(raise_on_bad_status=False)).StatusCode.is_good()

                # reload with changed server settings -> the server restarts on the same port
                cfg = yaml.safe_load((config_dir / "poller.yaml").read_text())
                cfg["opcua_server"]["server_name"] = "opencap renamed"
                (config_dir / "poller.yaml").write_text(yaml.safe_dump(cfg))
                await poller.reload()
                assert poller.opcua is not None and poller.opcua is not first
                async with Client(endpoint, timeout=2) as c:
                    names = {e.Server.ApplicationName.Text for e in await c.get_endpoints()}
                    assert names == {"opencap renamed"}
                    await wait_for(matches_again(c, poller, dev, node, tag.name), 5, "values after restart")
            finally:
                await poller.stop()
            assert poller.opcua is None
            c = Client(endpoint, timeout=1)
            with pytest.raises(Exception):
                await c.connect()                              # stop() closed the listening socket
            await c.disconnect()
        finally:
            await stop_sims(sims)


def matches_again(c, poller, dev, node, name):
    async def check() -> bool:
        dv = await c.get_node(node).read_data_value(raise_on_bad_status=False)
        return dv.StatusCode.is_good() and dv.Value.Value == pytest.approx(poller.device_values(dev.id)[name]["value"])
    return check
