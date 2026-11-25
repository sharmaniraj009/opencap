"""Browser-equivalent clients of the live feed, for tests/integration/test_live_load.py (#31).

    python -m tests.helpers.ws_load_client <url> <token> <clients> <seconds> <device> [<device> ...]

Runs in its own process, so parsing the feed does not share the poller's event loop (a browser
would not either). Each client opens `/api/v1/ws` with the subprotocol token, subscribes to
every device, keeps the values the way the Quick Client does (snapshot replaces, changes
update) and records when each device's messages arrive. Prints one JSON summary line.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from typing import Any

from websockets.asyncio.client import connect


async def one(url: str, protocols: list[str], devices: list[str], seconds: float) -> dict[str, Any]:
    out: dict[str, Any] = {"snapshots": 0, "changes": 0, "bytes": 0, "errors": [], "min_gap": None,
                           "tags": 0, "stale_batches": 0}
    held: dict[str, dict[str, Any]] = {}
    versions: dict[str, int] = {}
    last: dict[str, float] = {}
    async with connect(url, subprotocols=protocols, max_size=None, open_timeout=30) as ws:
        await ws.send(json.dumps({"subscribe": {"devices": devices}}))
        until = time.monotonic() + seconds
        while (left := until - time.monotonic()) > 0:
            try:
                text = await asyncio.wait_for(ws.recv(), left)
            except TimeoutError:
                break
            out["bytes"] += len(text)
            m = json.loads(text)
            kind, device = m["type"], m.get("device")
            if kind == "snapshot":
                out["snapshots"] += 1
                held[device] = {c["tag"]: c["value"] for c in m["changes"]}
                versions[device] = m["version"]
                last.pop(device, None)
            elif kind == "changes":
                if m["version"] <= versions.get(device, -1):
                    out["stale_batches"] += 1
                versions[device] = m["version"]
                out["changes"] += 1
                held[device].update((c["tag"], c["value"]) for c in m["changes"])
                now = time.monotonic()
                if device in last:
                    gap = now - last[device]
                    out["min_gap"] = gap if out["min_gap"] is None else min(out["min_gap"], gap)
                last[device] = now
            elif kind in ("error", "unavailable"):
                out["errors"].append(m)
    out["tags"] = sum(len(v) for v in held.values())
    return out


async def main(argv: list[str]) -> None:
    url, token, clients, seconds, *devices = argv
    protocols = ["opencap.v1", "opencap.bearer." + base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")]
    results = await asyncio.gather(*(one(url, protocols, devices, float(seconds)) for _ in range(int(clients))),
                                   return_exceptions=True)
    failed = [repr(r) for r in results if isinstance(r, BaseException)]
    ok = [r for r in results if not isinstance(r, BaseException)]
    gaps = [r["min_gap"] for r in ok if r["min_gap"] is not None]
    print(json.dumps({
        "clients": len(ok), "failed": failed,
        "snapshots": [r["snapshots"] for r in ok], "changes": [r["changes"] for r in ok],
        "tags": [r["tags"] for r in ok], "errors": [e for r in ok for e in r["errors"]][:5],
        "stale_batches": sum(r["stale_batches"] for r in ok), "min_gap": min(gaps) if gaps else None,
        "mbytes": round(sum(r["bytes"] for r in ok) / 1e6, 1),
    }))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
