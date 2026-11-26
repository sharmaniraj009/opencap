"""Mosquitto for acceptance tests (#8): a production-grade broker with persistence, restartable.

The in-process test broker (amqtt, `tests/helpers/broker.py`) acknowledges a message before it
routes it, so the Phase 1 test cuts the network instead of restarting the broker (D-018, D-021).
This helper runs a real Mosquitto with ``persistence true`` so a test can restart the broker
itself::

    async with Mosquitto(tmp_path / "mosquitto") as broker:     # broker.host, broker.port
        ...
        await broker.restart(down_s=2)                           # same port, same data

* **Where Mosquitto comes from:** the local ``mosquitto`` binary if there is one, else Docker
  (``IMAGE``, pinned by digest), on Linux only: a Windows runner's Docker runs Windows containers.
  ``available()`` says which; tests skip when it is ``None``, unless ``OPENCAP_REQUIRE_MOSQUITTO``
  is set (CI sets it on Linux, so a missing broker fails instead of skipping).
* **Port:** below the kernel's ephemeral range (``stable_port()``, D-022), kept across restarts.
* **Persistence:** ``persistence true`` with a data directory of the test's own; a stop is a
  SIGTERM, on which Mosquitto writes ``mosquitto.db``, and the next start loads it, so sessions,
  subscriptions and queued QoS 1 messages survive. No limit on queued messages per client
  (``max_queued_messages 0``): the default 1000 could drop messages for a subscriber that is
  away, and the test is about the broker restart, not about that limit.
* **Ready:** a start waits for a CONNACK to a real MQTT CONNECT, not just an open TCP port
  (Docker's port proxy accepts connections before Mosquitto listens).

``PersistentSubscriber`` is a QoS 1 subscriber with a persistent session (``clean_session``
false): the broker keeps its subscription and queues messages for it while it is away,
including across a broker restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import paho.mqtt.client as paho

from tests.helpers.broker import Msg
from tests.helpers.netcut import stable_port

# eclipse-mosquitto 2.x (2.0.22), multi-arch OCI index digest. Moving the pin is a PR of its own.
IMAGE = "eclipse-mosquitto:2.0.22@sha256:81a6193ae693942a3231cd3ac52875b02b7e3a19da671b2fb4b55cc874c5b279"
REQUIRE_ENV = "OPENCAP_REQUIRE_MOSQUITTO"


def _docker_usable() -> bool:
    if sys.platform != "linux" or shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def available() -> str | None:
    """"binary", "docker" or None: how this machine can run Mosquitto."""
    if shutil.which("mosquitto"):
        return "binary"
    return "docker" if _docker_usable() else None


def required() -> bool:
    return os.environ.get(REQUIRE_ENV, "").strip() not in ("", "0", "false")


def _connect_packet(client_id: str) -> bytes:
    """An MQTT 3.1.1 CONNECT with a clean session and a 10 s keepalive."""
    cid = client_id.encode()
    body = b"\x00\x04MQTT\x04\x02\x00\x0a" + len(cid).to_bytes(2, "big") + cid
    return bytes([0x10, len(body)]) + body


async def mqtt_ready(host: str, port: int) -> bool:
    """True if a broker at host:port answers a CONNECT with CONNACK, return code 0."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 2)
    except (OSError, TimeoutError):
        return False
    try:
        writer.write(_connect_packet(f"ready-{uuid.uuid4().hex[:8]}"))
        await writer.drain()
        connack = await asyncio.wait_for(reader.readexactly(4), 2)
        if connack[0] != 0x20 or connack[3] != 0:
            return False
        writer.write(b"\xe0\x00")                                   # DISCONNECT
        await writer.drain()
        return True
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class Mosquitto:
    """One Mosquitto broker with persistence in ``workdir`` (see the module docstring)."""

    def __init__(self, workdir: Path, *, host: str = "127.0.0.1", port: int = 0, mode: str | None = None) -> None:
        self.mode = mode or available()
        if self.mode is None:
            raise RuntimeError("no Mosquitto: install the mosquitto binary, or Docker on Linux")
        self.host = host
        self.port = port or stable_port(host)
        self.dir = Path(workdir)
        self.data = self.dir / "data"
        self.conf_dir = self.dir / "config"
        self.log_path = self.dir / "mosquitto.log"
        self.container = f"opencap-mosquitto-{uuid.uuid4().hex[:12]}"
        self.starts = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_file = None

    @property
    def db_path(self) -> Path:
        return self.data / "mosquitto.db"

    def _write_config(self) -> Path:
        self.data.mkdir(parents=True, exist_ok=True)
        self.conf_dir.mkdir(parents=True, exist_ok=True)
        docker = self.mode == "docker"
        lines = [
            f"listener {1883 if docker else self.port} {'0.0.0.0' if docker else self.host}",
            "allow_anonymous true",
            "persistence true",
            f"persistence_location {'/mosquitto/data/' if docker else str(self.data) + os.sep}",
            "max_queued_messages 0",
            "log_dest stdout",
            "log_type error",
            "log_type warning",
            "log_type notice",
            "log_type information",
            "connection_messages true",
        ]
        if not docker and hasattr(os, "geteuid") and os.geteuid() == 0:
            # Started as root, Mosquitto switches to the `mosquitto` user, which cannot reach a
            # data directory under pytest's 0700 tmp_path ("unable to remove mosquitto.db.new").
            lines.append("user root")
        conf = self.conf_dir / "mosquitto.conf"
        conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return conf

    # --- lifecycle
    async def start(self, *, timeout: float = 30.0) -> None:
        conf = self._write_config()
        if self.mode == "binary":
            self._log_file = open(self.log_path, "ab")               # noqa: SIM115 - closed in stop()
            self._proc = subprocess.Popen(["mosquitto", "-c", str(conf)], stdout=self._log_file,
                                          stderr=subprocess.STDOUT)
        elif self.starts == 0:
            # As this user, not root: the image's entrypoint would otherwise chown the mounted
            # data directory to its `mosquitto` user (uid 1883), and the test could no longer
            # manage or clean up its own temp directory (seen on the first CI run).
            await self._docker("run", "-d", "--name", self.container,
                               "--user", f"{os.getuid()}:{os.getgid()}",
                               "-p", f"{self.host}:{self.port}:1883",
                               "-v", f"{self.data}:/mosquitto/data",
                               "-v", f"{self.conf_dir}:/mosquitto/config:ro", IMAGE)
        else:
            await self._docker("start", self.container)
        self.starts += 1
        deadline = time.monotonic() + timeout
        while not await mqtt_ready(self.host, self.port):
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f"mosquitto exited with {self._proc.returncode}:\n{self.log_text()}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"mosquitto not ready on {self.host}:{self.port} within {timeout}s:\n"
                                   f"{self.log_text()}")
            await asyncio.sleep(0.1)

    async def stop(self, *, timeout: float = 15.0) -> None:
        """A clean stop (SIGTERM): Mosquitto writes its persistence file, then exits."""
        if self.mode == "binary":
            proc, self._proc = self._proc, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait)
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None
        else:
            await self._docker("stop", "-t", str(int(timeout)), self.container)

    async def restart(self, down_s: float = 0.0) -> None:
        await self.stop()
        await asyncio.sleep(down_s)
        await self.start()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop()
        if self.mode == "docker":
            if self.starts:
                with contextlib.suppress(Exception):
                    self.log_path.write_text(await self._docker("logs", self.container), encoding="utf-8")
            with contextlib.suppress(Exception):
                await self._docker("rm", "-f", self.container)

    async def __aenter__(self) -> Mosquitto:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def log_text(self) -> str:
        if self.mode == "docker":
            done = subprocess.run(["docker", "logs", self.container], capture_output=True, timeout=30)
            return (done.stdout + done.stderr).decode(errors="replace")
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    async def _docker(self, *args: str) -> str:
        done = await asyncio.to_thread(subprocess.run, ["docker", *args], capture_output=True, timeout=180)
        if done.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed ({done.returncode}): "
                               f"{done.stderr.decode(errors='replace').strip()}")
        return (done.stdout + done.stderr).decode(errors="replace")


class PersistentSubscriber:
    """A QoS 1 subscriber with a persistent session (``clean_session`` false) that reconnects by
    itself and records every message, duplicates included.

    It uses paho directly and records each message in ``on_message``, which paho runs before it
    sends the PUBACK: a message the broker counts as delivered has always been recorded. aiomqtt's
    message iterator can lose one. It takes a message off its queue in a separate task, and when
    the iterating task is cancelled (``pause()``) or the connection drops at that moment, the
    message is discarded after it was acknowledged (one message in about 1 run in 15 while #8
    was being built; D-083, #220).
    """

    __test__ = False

    def __init__(self, host: str, port: int, topics: Sequence[str], client_id: str) -> None:
        self.host, self.port = host, port
        self.topics = tuple(topics)
        self.client_id = client_id
        self.messages: list[Msg] = []                      # appended from paho's network thread
        self.connects = 0                                  # successful subscribes, in all
        self.connected = False
        self._client: paho.Client | None = None

    async def __aenter__(self) -> PersistentSubscriber:
        self.resume()
        await self.wait_connected(1)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.pause()

    def resume(self) -> None:
        if self._client is not None:
            return
        client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=self.client_id, clean_session=False)
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=0.2, max_delay=2)
        client.connect_async(self.host, self.port, keepalive=10)
        client.loop_start()                                # reconnects by itself after a broker restart
        self._client = client

    async def pause(self) -> None:
        """Disconnect cleanly and stay away: the broker keeps the session and queues for it."""
        client, self._client = self._client, None
        if client is not None:
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
        self.connected = False

    async def wait_connected(self, n: int, timeout: float = 30.0) -> None:
        """Wait until the subscriber has (re)connected and subscribed ``n`` times in all."""
        deadline = time.monotonic() + timeout
        while not (self.connected and self.connects >= n):
            if time.monotonic() > deadline:
                raise AssertionError(f"subscriber {self.client_id}: connect #{n} not within {timeout}s")
            await asyncio.sleep(0.05)

    # --- paho callbacks (its network thread)
    def _on_connect(self, client: paho.Client, userdata: object, flags: object, reason_code: Any,
                    properties: object) -> None:
        if not reason_code.is_failure:
            client.subscribe([(t, 1) for t in self.topics])   # data topics only: no retained replays

    def _on_subscribe(self, client: paho.Client, userdata: object, mid: int, reason_codes: Any,
                      properties: object) -> None:
        self.connects += 1
        self.connected = True

    def _on_disconnect(self, client: paho.Client, userdata: object, flags: object, reason_code: Any,
                       properties: object) -> None:
        self.connected = False

    def _on_message(self, client: paho.Client, userdata: object, msg: paho.MQTTMessage) -> None:
        self.messages.append(Msg(msg.topic, bytes(msg.payload), msg.qos, bool(msg.retain)))

    def payloads(self, topic: str) -> list[bytes]:
        return [m.payload for m in list(self.messages) if m.topic == topic]
