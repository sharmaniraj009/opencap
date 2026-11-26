"""Start `python -m opencap.plugin_host` the way the engine does (proto/README.md "Launching a
plugin process") and talk to it with the generated gRPC stubs.

The process is a plain `subprocess.Popen` driven from worker threads, not an asyncio subprocess:
the event loop the suite runs on Windows has no subprocess support."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import grpc

from opencap.config.models import Device
from opencap.plugin.v1 import driver_pb2 as pb
from opencap.plugin.v1 import driver_pb2_grpc as pb_grpc

READY = "opencap-plugin-ready"


@dataclass
class HostProcess:
    proc: subprocess.Popen
    address: str
    token: str
    stub: pb_grpc.DriverStub
    channel: grpc.aio.Channel

    @property
    def metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token}"),)

    async def plan(self, device: Device, tags: list[str] | None = None) -> pb.PlanReply:
        names = tags if tags is not None else [t.name for t in device.tags if t.enabled]
        req = pb.PlanRequest(device=pb.DeviceConfig(json=device.model_dump_json()), tags=names)
        return await self.stub.Plan(req, metadata=self.metadata)

    async def open(self, device: Device) -> pb.OpenReply:
        req = pb.OpenRequest(device_id=device.id, device=pb.DeviceConfig(json=device.model_dump_json()))
        return await self.stub.Open(req, metadata=self.metadata)

    async def call(self, name: str, request, timeout: float = 10.0):  # noqa: ANN001, ANN201
        return await getattr(self.stub, name)(request, metadata=self.metadata, timeout=timeout)


@contextlib.asynccontextmanager
async def plugin_host(*drivers: str, tcp: bool | None = None, token: str | None = None,
                      stdin_eof: bool = True) -> AsyncIterator[HostProcess]:
    """`tcp=None`: loopback TCP on Windows, a Unix socket elsewhere (the engine's `auto`)."""
    token = token or secrets.token_hex(32)
    tcp = os.name == "nt" if tcp is None else tcp
    with tempfile.TemporaryDirectory(prefix="opencap-plugin-") as tmp:
        where = ["--listen", "127.0.0.1:0"] if tcp else ["--socket", str(Path(tmp) / "plugin.sock")]
        args = [sys.executable, "-m", "opencap.plugin_host", *where]
        for d in drivers:
            args += ["--driver", d]
        if stdin_eof:
            args.append("--exit-on-stdin-eof")
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                env={**os.environ, "OPENCAP_PLUGIN_TOKEN": token, "OPENCAP_LOG_LEVEL": "WARNING"})
        try:
            assert proc.stdout is not None
            line = (await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 30)).decode()
            assert line.startswith(READY + " "), f"no ready line, got {line!r}"
            info = json.loads(line[len(READY) + 1:])
            assert info["protocol"] == "opencap.plugin.v1"
            channel = grpc.aio.insecure_channel(info["address"])
            try:
                yield HostProcess(proc, info["address"], token, pb_grpc.DriverStub(channel), channel)
            finally:
                await channel.close()
        finally:
            if proc.poll() is None:
                proc.kill()
            await asyncio.to_thread(proc.wait)
            for pipe in (proc.stdin, proc.stdout):
                with contextlib.suppress(Exception):
                    if pipe is not None:
                        pipe.close()
