"""The live feed (`GET /api/v1/ws`, #31) on a real poller: Modbus simulators, uvicorn and a real
WebSocket client (`websockets`), so the handshake, the subprotocol and conflation are exercised
the way a browser meets them."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager

import pytest
import sys
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from opencap.api import live
from opencap.api.server import create_app
from opencap.config.models import ApiCfg, ApiToken
from opencap.core.runtime import Poller
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims

ENV = {"T_VIEW": "v1ewer-secret"}


def bearer(token: str) -> str:
    return "opencap.bearer." + base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")


@asynccontextmanager
async def serve_api(poller: Poller, tmp_path):
    cfg = ApiCfg(tokens=[ApiToken(name="viewer", role="viewer", token_env="T_VIEW")],
                 audit_log=str(tmp_path / "audit.jsonl"))
    app = create_app(poller, cfg, environ=ENV)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False,
                                           lifespan="off", **live.UVICORN_WS))       # as runtime._start_api
    task = asyncio.create_task(server.serve())
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while not server.started:
            assert asyncio.get_running_loop().time() < deadline, "API did not start"
            await asyncio.sleep(0.02)
        yield server.servers[0].sockets[0].getsockname()[1], app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def _until(pred, timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.05)


async def test_live_feed_on_a_real_poller(tmp_path, no_gc):
    configure_logging("WARNING")
    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(2, 12, seed=31, scan_ms=50, timeout_ms=500))
        try:
            devices = [s.device for s in sims]
            ids = [d.id for d in devices]
            poller = Poller(write_config(tmp_path, devices, mqtt_port=broker.port), serve_api=False)
            await poller.start()
            try:
                await _until(lambda: all(poller.device_detail(i)["state"] == "ok" for i in ids), 10, "devices ok")
                async with serve_api(poller, tmp_path) as (port, app):
                    url = f"ws://127.0.0.1:{port}/api/v1/ws"
                    # refused before the upgrade, like the REST API
                    with pytest.raises(InvalidStatus) as refused:
                        async with connect(url, subprotocols=["opencap.v1", bearer("wrong")]):
                            pass
                    assert refused.value.response.status_code == 401

                    async with connect(url, subprotocols=["opencap.v1", bearer(ENV["T_VIEW"])]) as ws:
                        assert ws.subprotocol == "opencap.v1"
                        await ws.send(json.dumps({"subscribe": {"devices": ids}}))
                        ack = json.loads(await ws.recv())
                        assert ack["type"] == "subscribed" and ack["devices"] == ids
                        held: dict[str, dict[str, dict]] = {}
                        versions: dict[str, int] = {}
                        for _ in ids:
                            snap = json.loads(await ws.recv())
                            assert snap["type"] == "snapshot"
                            held[snap["device"]] = {c["tag"]: c for c in snap["changes"]}
                            versions[snap["device"]] = snap["version"]
                        assert set(held) == set(ids)
                        assert {t: v["value"] for t, v in held[ids[0]].items()} == \
                            {t: v["value"] for t, v in poller.device_values(ids[0]).items()}

                        # the simulators change every value every 20 ms for 1.5 s; the poller reads
                        # every 50 ms; the feed sends at most one message per device per 250 ms
                        arrivals: dict[str, list[float]] = {i: [] for i in ids}
                        loop = asyncio.get_running_loop()

                        async def ticker():
                            for _ in range(75):
                                for s in sims:
                                    s.tick()
                                await asyncio.sleep(0.02)

                        async def reader():
                            while True:
                                m = json.loads(await ws.recv())
                                assert m["type"] == "changes", m
                                assert m["version"] > versions[m["device"]]
                                versions[m["device"]] = m["version"]
                                arrivals[m["device"]].append(loop.time())
                                for c in m["changes"]:
                                    held[m["device"]][c["tag"]] = c

                        read = asyncio.create_task(reader())
                        try:
                            await ticker()
                            # after the last tick the next poll and one more slot settle the state
                            await _until(lambda: all({t: v["value"] for t, v in held[i].items()}
                                                     == {t: v["value"] for t, v in poller.device_values(i).items()}
                                                     for i in ids), 5, "client state equals the cache")
                        finally:
                            read.cancel()
                            await asyncio.gather(read, return_exceptions=True)
                        for i in ids:
                            times = arrivals[i]
                            assert 3 <= len(times) <= 1.5 / live.CONFLATE_S + 3, (i, len(times))
                            gaps = [b - a for a, b in zip(times, times[1:])]
                            # one message per slot; a little less than 250 ms only through socket jitter
                            assert min(gaps) >= live.CONFLATE_S - 0.06, (i, gaps)
                    await _until(lambda: not app.state.live.clients, 5, "the hub forgets the connection")
                    assert app.state.live.feeds == {}
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)


async def test_the_pollers_own_api_serves_the_feed(tmp_path, monkeypatch):
    """`opencap run`'s API server (runtime._start_api, uvicorn with live.UVICORN_WS), in open mode:
    an anonymous viewer on loopback gets the feed; a frame over MAX_MESSAGE closes the socket."""
    from tests.helpers.modbus_sim import free_port

    monkeypatch.delenv("OPENCAP_API_TOKEN", raising=False)
    configure_logging("WARNING")
    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(1, 6, seed=32, scan_ms=100, timeout_ms=500))
        try:
            dev = sims[0].device
            port = free_port()
            poller = Poller(write_config(tmp_path, [dev], mqtt_port=broker.port, api_port=port))
            await poller.start()
            try:
                await _until(lambda: poller.device_detail(dev.id)["state"] == "ok", 10, "device ok")
                url = f"ws://127.0.0.1:{port}/api/v1/ws"
                async with connect(url, subprotocols=["opencap.v1"]) as ws:
                    await ws.send(json.dumps({"subscribe": {"devices": [dev.id]}}))
                    assert json.loads(await ws.recv())["type"] == "subscribed"
                    snap = json.loads(await ws.recv())
                    assert snap["type"] == "snapshot" and len(snap["changes"]) == len(poller.device_values(dev.id))
                async with connect(url, subprotocols=["opencap.v1"], max_size=None) as ws:
                    await ws.send("x" * (live.MAX_MESSAGE + 1))
                    await asyncio.wait_for(ws.wait_closed(), 5)
                    # message too big (uvicorn). Windows resets a socket closed with unread data, so
                    # the client can see the connection drop (1006) before the close frame
                    assert ws.close_code in ((1009, 1006) if sys.platform == "win32" else (1009,))
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)
