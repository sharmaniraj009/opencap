"""Subscriber for the OPC UA server scale test (tests/integration/test_opcua_scale.py), run as
its own process so its CPU is not counted as the server's.

    python tests/helpers/opcua_scale_client.py <endpoint> <nodes file> <window s>

Subscribes every NodeId in the file at publishing/sampling 1 s (queue 1), lets the initial
values settle, counts data changes for `window` seconds and prints one JSON line:
{"created": n, "create_s": s, "rate": changes per second}.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from asyncua import Client, ua


class _Count:
    n = 0

    def datachange_notification(self, node, val, data) -> None:
        _Count.n += 1

    def status_change_notification(self, status) -> None:
        print(json.dumps({"status_change": str(status)}), file=sys.stderr, flush=True)


async def main(endpoint: str, nodes_file: str, window: float) -> None:
    with open(nodes_file, encoding="utf-8") as f:
        ids = [line.strip() for line in f if line.strip()]
    async with Client(endpoint, timeout=60) as client:
        sub = await client.create_subscription(ua.CreateSubscriptionParameters(
            RequestedPublishingInterval=1000, RequestedLifetimeCount=600, RequestedMaxKeepAliveCount=20,
            MaxNotificationsPerPublish=0, PublishingEnabled=True), _Count())
        started = time.monotonic()
        created = 0
        for k in range(0, len(ids), 1000):          # chunks keep both event loops responsive
            reqs = [ua.MonitoredItemCreateRequest(
                ItemToMonitor=ua.ReadValueId(NodeId=ua.NodeId.from_string(i), AttributeId=ua.AttributeIds.Value),
                MonitoringMode=ua.MonitoringMode.Reporting,
                RequestedParameters=ua.MonitoringParameters(ClientHandle=k + j, SamplingInterval=1000, QueueSize=1,
                                                            DiscardOldest=True))
                for j, i in enumerate(ids[k:k + 1000])]
            created += sum(1 for r in await sub.create_monitored_items(reqs) if not isinstance(r, ua.StatusCode))
        create_s = time.monotonic() - started
        await asyncio.sleep(5)                     # initial values
        n0, t0 = _Count.n, time.monotonic()
        await asyncio.sleep(window)
        rate = (_Count.n - n0) / (time.monotonic() - t0)
    print(json.dumps({"created": created, "create_s": round(create_s, 1), "rate": round(rate)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3])))
