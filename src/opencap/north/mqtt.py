"""MQTT link to the broker (PLAN.md section 7.3), on aiomqtt (paho-mqtt 2.x).

``publish()`` only appends to the SQLite outbox (``StoreAndForward``) and wakes the
drainer: it never waits on the network. ``run()`` is the connect loop:

1. connect (backoff 1 s, 2 s, 4 s ... 30 s between failed attempts; reset after a
   successful connect), with the LWT ``opencap/poller/<instance>/status`` = ``offline``
   (retained, QoS 1);
2. publish ``online`` (retained, QoS 1) directly, not through the outbox;
3. drain the outbox strictly in id order: a window of up to ``window`` messages is sent
   (paho writes them in call order and the broker acks QoS 1 in order), the rows the
   broker acknowledged are deleted, then the next window is sent;
4. on any connection problem the connection is dropped and the loop goes back to 1.
   Unacknowledged rows stay in the outbox and are sent again after the reconnect, so a
   message whose PUBACK was lost is delivered twice (QoS 1 = at least once).

aiomqtt facts this relies on (aiomqtt 2.5):

- ``Client.publish(qos=1)`` returns only after the PUBACK (paho ``on_publish``), or raises
  ``MqttError("Operation timed out")`` after ``timeout`` seconds.
- A lost connection does **not** wake pending ``publish()`` calls; it only completes the
  client's internal ``_disconnected`` future. The public way to observe it is
  ``client.messages``: iterating raises ``MqttError`` on disconnect. A watcher task does
  that, and the drainer is cancelled with it.
- Each connection uses a new ``Client`` (clean session): in-flight paho state is never
  reused, the outbox is the only source of truth.

``MQTT_PUBLISHED`` counts outbox messages (data and device status) acknowledged by the
broker; the link's own ``online``/``offline`` messages are not counted.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
from typing import Any

import aiomqtt
import structlog

from opencap.config.loader import slug
from opencap.config.models import MqttCfg
from opencap.metrics import MQTT_PUBLISHED
from opencap.north.saf import OutMsg, StoreAndForward

log = structlog.get_logger("opencap.north.mqtt")

ONLINE = b"online"
OFFLINE = b"offline"


def lwt_topic(instance: str) -> str:
    return f"opencap/poller/{slug(instance)}/status"


def check_topic(topic: str) -> None:
    """Reject topics that a broker would refuse; such a row would block the outbox forever."""
    if not isinstance(topic, str) or not topic:
        raise ValueError("MQTT topic must be a non-empty string")
    if "+" in topic or "#" in topic or "\x00" in topic:
        raise ValueError(f"invalid MQTT publish topic {topic!r} (wildcard or NUL)")
    if topic.startswith("$"):
        raise ValueError(f"invalid MQTT publish topic {topic!r} (leading '$' is reserved)")
    if len(topic.encode("utf-8")) > 65535:
        raise ValueError("MQTT topic longer than 65535 bytes")


class MqttLink:
    """Connection to the broker plus the outbox drainer. One instance per process."""

    def __init__(
        self,
        cfg: MqttCfg,
        instance: str,
        saf: StoreAndForward,
        *,
        backoff_min_s: float = 1.0,
        backoff_max_s: float = 30.0,
        window: int = 32,
        ack_timeout_s: float = 30.0,
        connect_timeout_s: float = 10.0,
    ) -> None:
        self._cfg = cfg
        self._instance = instance
        self._saf = saf
        self._backoff_min = backoff_min_s
        self._backoff_max = max(backoff_max_s, backoff_min_s)
        self._window = max(1, int(window))
        self._ack_timeout = ack_timeout_s
        self._connect_timeout = connect_timeout_s
        self.lwt_topic = lwt_topic(instance)
        self.client_id = cfg.client_id or instance
        self._wake = asyncio.Event()
        self._client: aiomqtt.Client | None = None
        self._connected = False
        self._stopping = False
        self._established = False         # the current/last session got a CONNACK
        self._run_task: asyncio.Task | None = None
        self.connects = 0                     # successful connections so far (for /health and tests)
        self.last_error: str | None = None

    # ------------------------------------------------------------------ public
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def is_secure(self) -> bool:
        return bool(self._cfg.tls)

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        """Queue a message (outbox first). Never waits on the network."""
        check_topic(topic)
        self._saf.put(topic, payload, qos=self._cfg.qos, retain=retain)
        self._wake.set()

    async def run(self) -> None:
        """Connect / reconnect until ``stop()`` or cancellation."""
        self._run_task = asyncio.current_task()
        backoff = self._backoff_min
        failures = 0
        while not self._stopping:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:             # noqa: BLE001 - every failure means: reconnect
                self.last_error = _describe(exc)
            if self._stopping:
                break
            if self._established:
                backoff = self._backoff_min
                failures = 0
                log.warning("mqtt_disconnected", host=self._cfg.host, port=self._cfg.port,
                            error=self.last_error, queued=self._saf.count())
            else:
                failures += 1
                (log.warning if failures == 1 else log.debug)(
                    "mqtt_connect_failed", host=self._cfg.host, port=self._cfg.port, error=self.last_error,
                    retry_in_s=backoff, attempt=failures, queued=self._saf.count())
            await asyncio.sleep(backoff)
            if not self._established:
                backoff = min(backoff * 2, self._backoff_max)

    async def stop(self) -> None:
        """Publish ``offline`` (retained) if connected, disconnect cleanly, end ``run()``.
        Messages still in the outbox stay there for the next start."""
        self._stopping = True
        self._wake.set()
        client = self._client
        if client is not None and self._connected:
            with contextlib.suppress(Exception):
                await client.publish(self.lwt_topic, OFFLINE, qos=1, retain=True, timeout=2.0)
        task = self._run_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ------------------------------------------------------------------ connection
    def client_kwargs(self) -> dict[str, Any]:
        """Arguments for ``aiomqtt.Client`` (separate so tests can check them)."""
        cfg = self._cfg
        password = os.environ.get(cfg.password_env) if cfg.password_env else None
        kwargs: dict[str, Any] = {
            "hostname": cfg.host,
            "port": cfg.port,
            "identifier": self.client_id,
            "keepalive": cfg.keepalive_s,
            "clean_session": True,
            "will": aiomqtt.Will(self.lwt_topic, OFFLINE, qos=1, retain=True),
            "max_inflight_messages": self._window,
            "timeout": self._connect_timeout,
        }
        if cfg.username is not None:
            kwargs["username"] = cfg.username
            kwargs["password"] = password
        if cfg.tls:
            kwargs["tls_context"] = ssl.create_default_context(cafile=cfg.ca_file)
        return kwargs

    async def _session(self) -> None:
        """One connection, from CONNECT until it breaks."""
        self._established = False
        client = aiomqtt.Client(**self.client_kwargs())
        client.pending_calls_threshold = self._window + 8      # silence aiomqtt's "pending calls" warning
        try:
            async with client:
                self._client = client
                self._established = True
                self._connected = True
                self.connects += 1
                self.last_error = None
                log.info("mqtt_connected", host=self._cfg.host, port=self._cfg.port, client_id=self.client_id,
                         tls=self.is_secure, queued=self._saf.count())
                async with asyncio.TaskGroup() as tg:          # the watcher runs from the first publish on
                    tg.create_task(self._watch(client), name="mqtt-watch")
                    tg.create_task(self._online_then_drain(client), name="mqtt-drain")
        finally:
            self._connected = False
            self._client = None

    @staticmethod
    async def _watch(client: aiomqtt.Client) -> None:
        """Raise MqttError as soon as the connection is lost (we never subscribe)."""
        async for _ in client.messages:
            pass
        raise aiomqtt.MqttError("message iterator ended")

    async def _online_then_drain(self, client: aiomqtt.Client) -> None:
        await client.publish(self.lwt_topic, ONLINE, qos=1, retain=True, timeout=self._ack_timeout)
        await self._drain(client)

    async def _drain(self, client: aiomqtt.Client) -> None:
        while True:
            batch = self._saf.peek(self._window)
            if not batch:
                self._wake.clear()               # no await between peek() and clear(): no lost wake-up
                await self._wake.wait()
                continue
            await self._send(client, batch)

    async def _send(self, client: aiomqtt.Client, batch: list[OutMsg]) -> None:
        """Send one window in id order; delete what the broker acknowledged."""
        # Tasks start in creation order and paho writes PUBLISH synchronously in publish(),
        # so the wire order is the id order.
        tasks = [asyncio.create_task(self._publish_one(client, m)) for m in batch]
        try:
            for t in tasks:
                await t                          # first failure ends the window (and the connection)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            acked = [m.id for m, t in zip(batch, tasks, strict=True)
                     if t.done() and not t.cancelled() and t.exception() is None]
            if acked:
                self._saf.ack(acked)
                published = sum(1 for t in tasks if t.done() and not t.cancelled()
                                and t.exception() is None and t.result())
                MQTT_PUBLISHED.inc(published)

    async def _publish_one(self, client: aiomqtt.Client, m: OutMsg) -> bool:
        """True = acknowledged by the broker; False = rejected locally and discarded."""
        try:
            await client.publish(m.topic, m.payload, qos=m.qos, retain=m.retain, timeout=self._ack_timeout)
        except (ValueError, TypeError) as exc:
            # paho refused the message itself (invalid topic, payload too large): it would
            # block the outbox forever, so it is dropped.
            log.error("mqtt_message_rejected", id=m.id, topic=m.topic, error=str(exc))
            return False
        return True


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


__all__ = ["MqttLink", "OFFLINE", "ONLINE", "check_topic", "lwt_topic"]
