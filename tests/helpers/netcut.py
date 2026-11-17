"""A TCP proxy you can unplug: simulates a network outage between a client and a server.

    async with NetCut("127.0.0.1", broker.port) as cut:   # clients connect to cut.port
        ...
        await cut.cut()        # drop every connection (RST) and refuse new ones
        await cut.restore()    # accept again on the SAME port

Used by the acceptance test to model the IPsec link (or the broker) being unreachable while
the broker itself keeps running.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import socket
import struct
from pathlib import Path


def stable_port(host: str = "127.0.0.1") -> int:
    """A free port OUTSIDE the kernel's ephemeral range.

    A test server that restarts on the same port must not use an ephemeral port: a client
    reconnecting in a tight loop can be given that number as its own source port and
    connect to itself (TCP self-connect), which leaves the port busy.
    """
    try:
        lo, _hi = (int(x) for x in Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split())
    except (OSError, ValueError):
        lo = 32768
    candidates = list(range(max(1024, lo - 12000), lo))
    random.shuffle(candidates)
    for port in candidates[:500]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port below the ephemeral range")


class NetCut:
    def __init__(self, target_host: str, target_port: int, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self.target = (target_host, target_port)
        self.host = host
        self.port = port or stable_port(host)
        self._server: asyncio.base_events.Server | None = None
        self._pipes: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.cuts = 0

    @property
    def up(self) -> bool:
        return self._server is not None

    async def __aenter__(self) -> NetCut:
        await self.restore()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.cut()

    async def restore(self, timeout: float = 5.0) -> None:
        if self._server is not None:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                self._server = await asyncio.start_server(self._accept, self.host, self.port, reuse_address=True)
                return
            except OSError:
                if asyncio.get_running_loop().time() > deadline:
                    raise
                await asyncio.sleep(0.05)

    async def cut(self) -> None:
        """Abort every open connection (RST, like a dead link) and stop accepting."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            self.cuts += 1
        for w in list(self._writers):
            sock = w.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            w.transport.abort()
        self._writers.clear()
        for t in list(self._pipes):
            t.cancel()
        await asyncio.gather(*self._pipes, return_exceptions=True)
        self._pipes.clear()
        if server is not None:
            await server.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.target)
        except OSError:
            writer.transport.abort()
            return
        self._writers.update((writer, up_writer))
        tasks = [asyncio.create_task(self._pump(reader, up_writer)), asyncio.create_task(self._pump(up_reader, writer))]
        self._pipes.update(tasks)
        for t in tasks:
            t.add_done_callback(self._pipes.discard)

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.transport.abort()
