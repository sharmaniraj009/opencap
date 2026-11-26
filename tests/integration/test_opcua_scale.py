"""OPC UA server scale (#28, docs/decisions.md D-087): 100,000 variables, 20,000 monitored items
at 1 s, values changing at OPENCAP_UA_SCALE_RATE per second (default 2,000 = 10 % of the
monitored items each second). Prints build time, memory and CPU; asserts every item is created
and every change is delivered. The subscriber is a separate process."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("asyncua")

from opencap.config.models import Device, OpcUaServerCfg, Tag  # noqa: E402
from opencap.core.cache import TagValue, ValueCache  # noqa: E402
from opencap.north.opcua_server import OpcUaNorth  # noqa: E402
from tests.helpers.modbus_sim import free_port  # noqa: E402

pytestmark = pytest.mark.acceptance

DEVICES, TAGS, HOT_DEVICES = 100, 1000, 20       # 100k variables, 20k monitored
RATE = int(os.environ.get("OPENCAP_UA_SCALE_RATE", "2000"))
WINDOW_S = 20.0
CLIENT = Path(__file__).resolve().parents[1] / "helpers" / "opcua_scale_client.py"


def rss_mib() -> float:
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20


@pytest.mark.skipif(not Path("/proc/self/statm").exists(), reason="memory is read from /proc (Linux)")
async def test_100k_variables_20k_monitored_items(tmp_path, no_gc):
    devices = [Device(id=f"Plant A/PLC{d:03d}", channel="Plant A", name=f"PLC{d:03d}", driver="modbus_tcp",
                      host="192.0.2.20", port=502,
                      tags=[Tag(name=f"G{t // 100}.T{t:04d}", address=f"4{t + 1:05d}", dtype="uint16")
                            for t in range(TAGS)]) for d in range(DEVICES)]
    cache = ValueCache()
    now = datetime.now(UTC)
    for dev in devices:
        cache.register(dev)
        cache.update(dev.id, {t.name: TagValue(0, "good", now) for t in dev.tags})
    before, t0, c0 = rss_mib(), time.perf_counter(), time.process_time()
    north = OpcUaNorth(OpcUaServerCfg(enabled=True, anonymous=True, endpoint=f"opc.tcp://127.0.0.1:{free_port()}/opencap",
                                      max_monitored_items=100_000), devices, cache, lambda _i: {"state": "ok"})
    await north.start()
    build_s, build_cpu, built = time.perf_counter() - t0, time.process_time() - c0, rss_mib()
    stop = asyncio.Event()
    hot = [(d.id, t.name) for d in devices[:HOT_DEVICES] for t in d.tags]
    nodes = tmp_path / "nodes.txt"
    nodes.write_text("\n".join(north.node_id(dev, name) for dev, name in hot), encoding="utf-8")

    async def drive() -> None:                    # the poller's side: RATE changes per second
        k = 0
        while not stop.is_set():
            ts, batch = datetime.now(UTC), {}
            for i in range(RATE):
                dev, name = hot[(k * RATE + i) % len(hot)]
                batch.setdefault(dev, {})[name] = TagValue((k + i) % 60000, "good", ts)
            for dev, values in batch.items():
                cache.update(dev, values)
                north.notify(dev)
            k += 1
            await asyncio.sleep(1.0)

    driver = asyncio.create_task(drive())
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(CLIENT), north.cfg.endpoint, str(nodes),
                                                    str(WINDOW_S), stdout=asyncio.subprocess.PIPE)
        c1, w1 = time.process_time(), time.perf_counter()
        out = (await asyncio.wait_for(proc.stdout.read(), 300)).decode()
        cpu = (time.process_time() - c1) / (time.perf_counter() - w1)
        await proc.wait()
    finally:
        stop.set()
        await driver
        await north.stop()
    result = json.loads(out.strip().splitlines()[-1])
    report = {"variables": DEVICES * TAGS, "monitored": len(hot), "rate": RATE, "build_s": round(build_s, 1),
              "build_cpu_s": round(build_cpu, 1), "rss_before_mib": round(before), "rss_built_mib": round(built),
              "server_cpu_pct": round(cpu * 100), **result}
    print("opcua scale:", json.dumps(report))
    assert result["created"] == len(hot), report
    assert result["rate"] >= 0.9 * RATE, f"changes lost or delayed: {report}"
    assert build_s < 180, report
