"""The store-and-forward outbox file is interchangeable between the engines (ADR-0011).

At cut-over a node may stop the Python engine with messages still queued; the Rust engine
must drain that file in order, and the reverse must hold for a rollback. Same SQLite schema,
same semantics (D-016).

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from opencap.north.saf import StoreAndForward
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.engine import RustEngine, rust_binary
from tests.helpers.modbus_sim import free_port, make_sim_devices

BIN = rust_binary()
pytestmark = pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)")


async def _wait(pred, timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.05)


async def test_rust_engine_drains_a_python_outbox_in_order(tmp_path):
    async with TestBroker() as broker, broker.collector("opencap/#") as col:
        await col.wait_ready()
        config_dir = write_config(tmp_path, [], mqtt_port=broker.port, api_port=free_port())
        saf = StoreAndForward(tmp_path / "saf.sqlite", max_bytes=10_000_000)   # the engine's saf.path
        payloads = [json.dumps({"n": i}).encode() for i in range(50)]
        for p in payloads:
            saf.put("opencap/plant_a/plc1/data", p)
        saf.put("opencap/plant_a/plc1/status", b'{"state":"ok","last_good":null,"errors":0}', retain=True)
        saf.close()

        engine = RustEngine(config_dir, BIN, tmp_path / "rust")
        await engine.start()
        try:
            await _wait(lambda: col.payloads("opencap/plant_a/plc1/data")[-1:] == payloads[-1:], 20, "the backlog")
            assert col.payloads("opencap/plant_a/plc1/data") == payloads           # all, in order, once
            await _wait(lambda: col.last("opencap/plant_a/plc1/status") is not None, 5, "the status row")
            await _wait(lambda: engine.saf.count() == 0, 5, "an empty outbox")
        finally:
            await engine.stop()


async def test_python_reads_an_outbox_left_by_the_rust_engine(tmp_path):
    # Broker unreachable: the engine queues the retained status of its (unreachable) device.
    device = make_sim_devices(1, 3)[0].model_copy(update={"port": free_port()})
    config_dir = write_config(tmp_path, [device], mqtt_port=free_port(), api_port=free_port())
    engine = RustEngine(config_dir, BIN, tmp_path / "rust")
    await engine.start()
    try:
        await _wait(lambda: engine.saf.count() >= 1, 10, "a queued status message")
    finally:
        await engine.stop()
    saf = StoreAndForward(tmp_path / "saf.sqlite", max_bytes=10_000_000)
    try:
        msgs = saf.peek(100)
        assert msgs and saf.count() == len(msgs)
        status = [m for m in msgs if m.topic == "opencap/sim_site_01/sim_plc_01/status"]
        assert status and status[0].retain and status[0].qos == 1
        assert json.loads(status[0].payload) == {"state": "offline", "last_good": None, "errors": 0}
    finally:
        saf.close()
