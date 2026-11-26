"""In-process MQTT broker and collecting subscriber for tests (amqtt broker, paho client).

Quick start::

    from tests.helpers.broker import TestBroker

    async def test_something():
        async with TestBroker() as broker:                # 127.0.0.1, free ephemeral port
            async with broker.collector("opencap/#") as col:    # real MQTT subscriber, QoS 1
                link = MqttLink(broker.mqtt_cfg(), "test", saf)   # MqttCfg pointing at the broker
                ...
                msgs = await col.wait_count("opencap/site/dev/data", 10)
                await broker.stop()                        # outage ...
                await broker.start()                       # ... back on the SAME port

Or use the ready-made fixture: ``from tests.helpers.broker import mqtt_broker  # noqa: F401``.

Guarantees that make outage tests deterministic
-----------------------------------------------
* ``stop()`` is *lossless for subscribers*: before shutting down it stops reading from every
  non-collector client and waits until every message the broker already acknowledged
  (PUBACK) has been delivered to the connected collectors. A broker-side drop of an acked
  message would otherwise look like a bug in the publisher. Messages still on the wire are
  not acked, so the publisher must resend them (QoS 1): that is what the tests check.
* ``start()`` after a ``stop()`` admits **only collectors** until every open collector has
  reconnected and re-subscribed; then other clients may connect (they get CONNACK
  "not authorised" before that and must retry). So nothing a publisher sends after the
  restart can be missed by a collector.
* Retained messages and sessions do not survive a restart (amqtt keeps them in memory),
  like a broker without persistence.
* amqtt 0.12 quirk: a new subscription receives each retained message **twice** (a QoS 0 and
  a QoS 1 copy, both with retain=True). Assert on sets or on ``last()``, not on counts.

API summary
-----------
``TestBroker(host="127.0.0.1", port=0, *, listener=None)``
    ``listener`` adds amqtt listener keys, e.g. ``{"ssl": True, "certfile": ..., "keyfile": ...}``
    for TLS (collectors do not speak TLS).
    ``await start()`` / ``await stop()`` / ``await restart(down_s=0)``, ``async with``.
    ``host``, ``port`` (fixed after the first start), ``running``, ``generation`` (starts so far).
    ``mqtt_cfg(**kw) -> MqttCfg`` for opencap.
    ``collector(*topics="#", client_id=None) -> Collector`` (async context manager).
    ``connected_clients() -> set[str]``, ``await wait_client(client_id, connected=True)``.
    ``await kick(client_id)``: abort a client's TCP connection from the broker side
    (abnormal disconnect, so the broker publishes the client's LWT).
``Collector``
    ``messages`` (every ``Msg`` in arrival order), ``topic(t)``, ``payloads(t)``, ``last(t)``,
    ``clear()``, ``await wait_for(pred, timeout)``, ``await wait_count(t, n, timeout)``,
    ``await wait_payload(t, payload, timeout)``, ``connects`` (how many times it connected).
``Msg(topic, payload, qos, retain)`` with ``.text`` and ``.json()``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import warnings
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as paho
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from amqtt.broker import Broker
    from amqtt.errors import BrokerError
    from amqtt.plugins.base import BaseAuthPlugin

from opencap.config.models import MqttCfg
from tests.helpers.netcut import stable_port

__all__ = ["Collector", "Msg", "TestBroker", "mqtt_broker"]

log = logging.getLogger(__name__)

_OWNERS: weakref.WeakValueDictionary[str, TestBroker] = weakref.WeakValueDictionary()
_ids = itertools.count(1)


@dataclass(frozen=True, slots=True)
class Msg:
    topic: str
    payload: bytes
    qos: int
    retain: bool

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.payload)


class _GatePlugin(BaseAuthPlugin):
    """amqtt auth plugin: admits collectors always, other clients only when the gate is open."""

    @dataclass
    class Config:
        owner: str = ""
        generation: int = 0

    async def authenticate(self, *, session: Any) -> bool | None:
        owner = _OWNERS.get(self.config.owner)
        if owner is None:
            return True
        return owner._admit(session.client_id, self.config.generation)


def _transport(handler: Any) -> asyncio.Transport | None:
    writer = getattr(getattr(handler, "writer", None), "_writer", None)   # amqtt StreamWriterAdapter
    return getattr(writer, "transport", None)


class TestBroker:
    __test__ = False                      # not a pytest test class

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, listener: dict[str, Any] | None = None) -> None:
        self.host = host
        self.port = port
        self._listener_extra = dict(listener or {})       # e.g. {"ssl": True, "certfile": ..., "keyfile": ...}
        self.generation = 0
        self._broker: Broker | None = None
        self._gate_open = True
        self._collectors: set[Collector] = set()
        self._in_hand = 0                     # messages amqtt has PUBACKed but not yet queued for broadcast
        # Every PUBLISH the broker accepted (PUBACKed) from a non-collector client, in arrival order:
        # (client_id, topic, payload). Lets tests check a publisher's guarantee at the broker itself,
        # independent of how reliably amqtt fans messages out to subscribers.
        self.accepted: list[tuple[str, str, bytes]] = []
        self._key = f"tb{next(_ids)}-{id(self)}"
        _OWNERS[self._key] = self

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._broker is not None

    def _config(self) -> dict[str, Any]:
        return {
            "listeners": {"default": {"type": "tcp", "bind": f"{self.host}:{self.port}", **self._listener_extra}},
            "plugins": {f"{__name__}._GatePlugin": {"owner": self._key, "generation": self.generation}},
            # amqtt drops a message to a subscriber that has not PUBACKed it within this time
            # (default 5 s). A large backlog replayed after an outage needs much longer.
            "qos1_puback_timeout": 120,
        }

    async def start(self, *, timeout: float = 5.0) -> None:
        """Start listening (port 0 on the first start = pick a free port, then keep it)."""
        if self._broker is not None:
            return
        self.generation += 1
        self._gate_open = False
        deadline = time.monotonic() + timeout
        if self.port == 0:
            # Outside the ephemeral range: a reconnecting client can never self-connect to it,
            # so a restart on the same port cannot find it busy.
            self.port = stable_port(self.host)
        while True:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                broker = Broker(self._config())
            self._track_in_hand(broker)
            try:
                await broker.start()
                break
            except BrokerError:
                # the port can stay busy for a moment after a stop; a new port is never chosen
                if self.port == 0 or time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.05)
        self._broker = broker
        if self.port == 0:
            server = broker._servers["default"].instance             # noqa: SLF001
            self.port = server.sockets[0].getsockname()[1]
        # Collectors first, then everyone else.
        await asyncio.gather(*(c.wait_ready(timeout=timeout) for c in list(self._collectors)))
        self._gate_open = True

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Quiesce (every acked message reaches the collectors), then shut down."""
        broker = self._broker
        if broker is None:
            return
        self._gate_open = False
        await self._quiesce(broker, timeout)
        self._broker = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            await broker.shutdown()

    async def restart(self, down_s: float = 0.0) -> None:
        await self.stop()
        if down_s:
            await asyncio.sleep(down_s)
        await self.start()

    async def __aenter__(self) -> TestBroker:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        for col in list(self._collectors):
            await col.close()
        await self.stop()

    # ------------------------------------------------------------------ internals
    def _admit(self, client_id: str | None, generation: int) -> bool:
        # A connection accepted by a broker instance that is shutting down (or already gone)
        # must never be admitted: it would ack messages that nobody forwards.
        if generation != self.generation or self._broker is None:
            return False
        if self._gate_open:
            return True
        return any(c.client_id == client_id for c in self._collectors)

    def _collector_ids(self) -> set[str]:
        return {c.client_id for c in self._collectors}

    def _pause_publishers(self, broker: Broker) -> None:
        keep = self._collector_ids()
        for client_id, (_session, handler) in list(broker.sessions.items()):
            if client_id in keep:
                continue
            transport = _transport(handler)
            if transport is not None and not transport.is_closing():
                transport.pause_reading()

    def _track_in_hand(self, broker: Broker) -> None:
        """Count messages inside amqtt's `_handle_message_delivery`.

        amqtt sends the PUBACK when it reads a QoS 1 PUBLISH, then awaits two plugin events
        and the topic filter before it puts the message on the broadcast queue. In that window
        the message is in no queue, so without this counter `_idle()` could report idle and
        `stop()` would drop messages the publisher already saw acknowledged.
        """
        original = broker._handle_message_delivery                     # noqa: SLF001

        async def tracked(*args: Any, **kwargs: Any) -> Any:
            self._in_hand += 1
            session, _handler, wait_deliver = (list(args) + [None, None, None])[:3]
            try:
                msg = wait_deliver.result() if wait_deliver is not None and wait_deliver.done() else None
            except BaseException:                                        # noqa: BLE001
                msg = None
            client_id = getattr(session, "client_id", None)
            if msg is not None and getattr(msg, "topic", None) and client_id not in self._collector_ids():
                self.accepted.append((client_id, msg.topic, bytes(msg.data)))
            try:
                return await original(*args, **kwargs)
            finally:
                self._in_hand -= 1

        broker._handle_message_delivery = tracked                      # noqa: SLF001

    def _idle(self, broker: Broker) -> bool:
        if self._in_hand:
            return False
        if broker._broadcast_queue.qsize():                             # noqa: SLF001
            return False
        if any(not t.done() for t in broker._tasks_queue):               # noqa: SLF001
            return False
        for session, handler in list(broker.sessions.values()):
            if session.delivered_message_queue.qsize():
                return False
            reader = getattr(getattr(handler, "reader", None), "_reader", None)
            if reader is not None and len(getattr(reader, "_buffer", b"")):
                return False
        return True

    async def _quiesce(self, broker: Broker, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        quiet = 0
        while quiet < 3:
            # re-pause every round: asyncio's StreamReader may resume a transport it paused itself
            self._pause_publishers(broker)
            await asyncio.sleep(0.02)
            quiet = quiet + 1 if self._idle(broker) else 0
            if time.monotonic() > deadline:
                log.warning("test broker did not go idle within %.1fs; stopping anyway", timeout)
                return

    # ------------------------------------------------------------------ helpers for tests
    def idle(self) -> bool:
        """True when the running broker holds no message: none queued, in hand or being fanned
        out. amqtt PUBACKs on arrival but forwards at ~1,600 msg/s, so after a publisher
        drains a big backlog the broker can still be forwarding for several seconds."""
        return self._broker is not None and self._idle(self._broker)

    def accepted_payloads(self, topic: str) -> list[bytes]:
        """Payloads the broker accepted on `topic` from publishers, in arrival order."""
        return [payload for _cid, t, payload in self.accepted if t == topic]

    def mqtt_cfg(self, **kw: Any) -> MqttCfg:
        kw.setdefault("password_env", None)
        return MqttCfg(host=self.host, port=self.port, **kw)

    def collector(self, *topics: str, client_id: str | None = None) -> Collector:
        return Collector(self, topics or ("#",), client_id or f"test-collector-{next(_ids)}")

    def connected_clients(self) -> set[str]:
        if self._broker is None:
            return set()
        return {cid for cid, (s, _h) in self._broker.sessions.items() if s.transitions.state == "connected"}

    async def wait_client(self, client_id: str, *, connected: bool = True, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while (client_id in self.connected_clients()) != connected:
            if time.monotonic() > deadline:
                state = "connect" if connected else "disconnect"
                raise AssertionError(f"client {client_id!r} did not {state} within {timeout}s; "
                                     f"connected: {sorted(self.connected_clients())}")
            await asyncio.sleep(0.02)

    async def kick(self, client_id: str) -> bool:
        """Abort the client's connection from the broker side (no DISCONNECT: the LWT fires)."""
        if self._broker is None or client_id not in self._broker.sessions:
            return False
        transport = _transport(self._broker.sessions[client_id][1])
        if transport is None:
            return False
        transport.abort()
        await asyncio.sleep(0)
        return True


class Collector:
    """A real MQTT subscriber (QoS 1) that records every message and reconnects by itself.

    It records each message in paho's `on_message`, which paho runs *before* it sends the PUBACK,
    so a message the broker counts as delivered has always been recorded (#220). It ran on
    aiomqtt's message iterator before; that iterator takes a message off its queue in a separate
    task and drops it when the reader is cancelled or disconnected in the same instant, after the
    PUBACK (D-083). paho's network thread runs it, independent of the test's event loop.
    """

    __test__ = False

    def __init__(self, broker: TestBroker, topics: Sequence[str], client_id: str) -> None:
        self.broker = broker
        self.topics = tuple(topics)
        self.client_id = client_id
        self.messages: list[Msg] = []                    # appended from paho's network thread
        self.connects = 0
        self._ready_gen = 0
        self._client: paho.Client | None = None

    async def __aenter__(self) -> Collector:
        self.broker._collectors.add(self)                                # noqa: SLF001
        client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=0.05, max_delay=0.5)       # broker restarts: back fast
        client.connect_async(self.broker.host, self.broker.port, keepalive=10)
        client.loop_start()
        self._client = client
        await self.wait_ready()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self.broker._collectors.discard(self)                            # noqa: SLF001
        client, self._client = self._client, None
        if client is not None:
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
        self._ready_gen = 0

    @property
    def ready(self) -> bool:
        return self.broker.running and self._ready_gen == self.broker.generation

    async def wait_ready(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.ready:
            if time.monotonic() > deadline:
                raise AssertionError(f"collector {self.client_id} not subscribed within {timeout}s")
            await asyncio.sleep(0.01)

    # --- paho callbacks (its network thread)
    def _on_connect(self, client: paho.Client, userdata: object, flags: object, reason_code: Any,
                    properties: object) -> None:
        if not reason_code.is_failure:
            client.subscribe([(t, 1) for t in self.topics])

    def _on_subscribe(self, client: paho.Client, userdata: object, mid: int, reason_codes: Any,
                      properties: object) -> None:
        self._ready_gen = self.broker.generation
        self.connects += 1

    def _on_disconnect(self, client: paho.Client, userdata: object, flags: object, reason_code: Any,
                       properties: object) -> None:
        self._ready_gen = 0

    def _on_message(self, client: paho.Client, userdata: object, m: paho.MQTTMessage) -> None:
        self.messages.append(Msg(m.topic, bytes(m.payload), m.qos, bool(m.retain)))

    # ------------------------------------------------------------------ queries
    def topic(self, topic: str) -> list[Msg]:
        return [m for m in list(self.messages) if m.topic == topic]

    def payloads(self, topic: str) -> list[bytes]:
        return [m.payload for m in list(self.messages) if m.topic == topic]

    def last(self, topic: str) -> Msg | None:
        for m in reversed(list(self.messages)):
            if m.topic == topic:
                return m
        return None

    def clear(self) -> None:
        self.messages.clear()

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for m in list(self.messages):
            counts[m.topic] = counts.get(m.topic, 0) + 1
        return ", ".join(f"{t}: {n}" for t, n in sorted(counts.items())) or "nothing"

    async def wait_for(self, pred: Callable[[], bool], timeout: float = 5.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise AssertionError(f"{what} not met within {timeout}s; received {self.summary()}")
            await asyncio.sleep(0.01)

    async def wait_count(self, topic: str, n: int, timeout: float = 5.0) -> list[Msg]:
        await self.wait_for(lambda: len(self.topic(topic)) >= n, timeout, f"{n} messages on {topic}")
        return self.topic(topic)

    async def wait_payload(self, topic: str, payload: bytes, timeout: float = 5.0) -> Msg:
        await self.wait_for(lambda: payload in self.payloads(topic), timeout, f"{payload!r} on {topic}")
        return next(m for m in list(self.messages) if m.topic == topic and m.payload == payload)


@pytest.fixture
async def mqtt_broker():
    """A running TestBroker on a free port; stopped after the test."""
    async with TestBroker() as broker:
        yield broker
