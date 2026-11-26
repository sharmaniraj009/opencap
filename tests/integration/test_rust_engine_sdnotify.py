"""The Rust engine's systemd notifications (#57; deploy/opencapd.service is `Type=notify`).

systemd counts a `Type=notify` service as started when it sends `READY=1` to `$NOTIFY_SOCKET`.
This test plays systemd: it binds that socket, runs the real engine binary and checks
- `READY=1` arrives, and only once the API answers (config loaded, poller started, API bound);
- SIGHUP (`systemctl reload`) is framed by `RELOADING=1` and `READY=1`;
- SIGTERM (`systemctl stop`) sends `STOPPING=1`, then the engine exits 0;
- a config that does not load sends nothing and exits non-zero, so `systemctl start` fails.
No MQTT broker is needed: the engine starts and queues without one.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys

import httpx
import pytest

from tests.helpers.configdir import write_config
from tests.helpers.engine import rust_binary
from tests.helpers.modbus_sim import free_port, make_sim_devices, start_sims, stop_sims
from tests.helpers.netcut import stable_port

BIN = rust_binary()
pytestmark = [
    pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)"),
    pytest.mark.skipif(sys.platform != "linux", reason="systemd notifications are Linux-only"),
]


class NotifySocket:
    """The `$NOTIFY_SOCKET` end systemd would hold: a unix datagram socket."""

    def __init__(self, path) -> None:
        self.path = str(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.setblocking(False)

    async def recv(self, timeout: float = 30.0) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                return self.sock.recv(4096).decode()
            except BlockingIOError:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError(f"no notification within {timeout}s") from None
                await asyncio.sleep(0.02)

    def close(self) -> None:
        self.sock.close()


def _start(config_dir, notify: NotifySocket) -> subprocess.Popen:
    env = {**os.environ, "NOTIFY_SOCKET": notify.path}
    return subprocess.Popen([str(BIN), "run", "--config", str(config_dir)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


async def test_ready_reload_and_stop_are_notified(tmp_path):
    notify = NotifySocket(tmp_path / "notify.sock")
    sims = await start_sims(make_sim_devices(1, 3))
    proc = None
    try:
        api_port = stable_port()
        config_dir = write_config(tmp_path, [s.device for s in sims], mqtt_port=free_port(), api_port=api_port)
        proc = _start(config_dir, notify)
        ready = await notify.recv()
        assert ready.splitlines()[0] == "READY=1", ready
        async with httpx.AsyncClient(timeout=5) as http:      # READY came after the API was bound
            assert (await http.get(f"http://127.0.0.1:{api_port}/health")).status_code == 200

        proc.send_signal(signal.SIGHUP)
        assert (await notify.recv()).splitlines()[0] == "RELOADING=1"
        assert (await notify.recv()).splitlines()[0] == "READY=1"

        proc.send_signal(signal.SIGTERM)
        assert (await notify.recv()).splitlines()[0] == "STOPPING=1"
        assert await asyncio.to_thread(proc.wait, 30) == 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        await stop_sims(sims)
        notify.close()


async def test_a_config_that_does_not_load_sends_nothing(tmp_path):
    notify = NotifySocket(tmp_path / "notify.sock")
    broken = tmp_path / "config"
    broken.mkdir()
    (broken / "poller.yaml").write_text("mqtt: [this is not a mapping\n", encoding="utf-8")
    proc = _start(broken, notify)
    try:
        assert await asyncio.to_thread(proc.wait, 30) != 0
        with pytest.raises(BlockingIOError):
            notify.sock.recv(4096)
    finally:
        if proc.poll() is None:
            proc.kill()
        notify.close()
