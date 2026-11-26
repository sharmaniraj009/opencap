"""`/health/ready` on a real poller: one fast simulator and one muted one (#123)."""

from __future__ import annotations

import asyncio

import httpx

from opencap.api.server import create_app
from opencap.config.models import ApiCfg
from opencap.core.runtime import Poller
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims


async def test_ready_is_503_until_the_muted_device_is_offline(tmp_path):
    configure_logging("WARNING")
    async with TestBroker() as broker:
        devices = make_sim_devices(2, 4, seed=5, scan_ms=100, timeout_ms=300)
        devices = [d.model_copy(update={"retries": 0}) for d in devices]
        sims = await start_sims(devices)
        sims[1].mute = True                       # accepts the connection, never answers
        fast, muted = sims[0].device.id, sims[1].device.id
        try:
            poller = Poller(write_config(tmp_path, [s.device for s in sims], mqtt_port=broker.port),
                            serve_api=False)
            await poller.start()
            api = create_app(poller, ApiCfg(audit_log=str(tmp_path / "audit.jsonl")), environ={})
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                             base_url="http://127.0.0.1") as http:
                    live = await http.get("/health/live")
                    assert live.status_code == 200
                    first = await http.get("/health/ready")
                    assert first.status_code == 503
                    assert muted in first.json()["waiting"] and first.json()["ready"] is False
                    assert (await http.get("/health")).json()["initial_scan_complete"] is False

                    deadline = asyncio.get_running_loop().time() + 20
                    while (r := await http.get("/health/ready")).status_code != 200:
                        assert asyncio.get_running_loop().time() < deadline, f"never ready: {r.json()}"
                        await asyncio.sleep(0.05)
                    assert r.json() == {"ready": True, "waiting": []}
                    assert poller.device_status(muted)["state"] == "offline"
                    assert poller.device_status(fast)["state"] == "ok"

                    health = (await http.get("/health")).json()
                    assert health["initial_scan_complete"] is True
                    assert health["initial_scan_ms"] >= 300       # waited for the muted device's timeout
                    # sticky: stays ready while the muted device keeps failing
                    await asyncio.sleep(0.5)
                    assert (await http.get("/health/ready")).status_code == 200
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)
