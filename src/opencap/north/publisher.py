"""Publisher: when to send what (PLAN.md section 7.2).

One asyncio task per device plus one status task, all inside ``run()``.

Data messages (topic ``device.publish.topic`` or ``default_topic(device, topic_prefix)``, not retained):

- ``interval`` mode: the full snapshot every ``publish.interval_ms``. The first message goes
  out one interval after ``run()`` starts (the first poll cycle has had time to finish).
  Missed slots (a stalled loop) are skipped, never sent in a burst.
- ``on_change`` mode: a message when a tag goes between good and not good, or when a good
  value moves by more than ``publish.deadband`` (absolute, on the rounded value as it
  appears in the payload) compared with the **last published** value, so slow drift is
  caught too. Bools and strings change on any difference. Plus a heartbeat: a message at
  least every ``publish.heartbeat_ms``. Every message carries the full snapshot (the
  SubDevice JSON format has one ``Data`` object per device; ``DataValid``/``BadTags`` would be
  wrong for a partial one). The cache is checked when ``notify_cycle()`` is called and,
  as a fallback, every ``on_change_poll_s``.
- A device that has **never had a good read** since start (no tag with a ``source_ts``)
  publishes no data message: an empty message stamped "now" would look like fresh data.
  It still gets status messages. Once it has had one good read, it publishes on schedule
  even if all its tags go bad (``Data`` may then be ``{}``, ``DataValid`` false).

Status messages (``status_topic(device, topic_prefix)``, retained): ``{"state","last_good","errors"}``
from ``status_fn(device_id)``, published at start, whenever ``state`` changes (checked
every ``status_check_s``) and refreshed every ``status_refresh_s``.

The cache and link are typed as small Protocols so the publisher can be tested with fakes.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import structlog

from opencap.config.loader import default_topic
from opencap.config.models import DEFAULT_TOPIC_PREFIX, Device, PayloadCfg
from opencap.core.cache import TagValue
from opencap.north.payload import build_payload, build_status, clean_value, encode, is_bad, status_topic

log = structlog.get_logger("opencap.north.publisher")

MIN_PERIOD_MS = 50          # floor for interval_ms / heartbeat_ms (protects against 0 in config)


class CacheView(Protocol):
    def snapshot(self, device_id: str) -> dict[str, TagValue]: ...
    def version(self, device_id: str) -> int: ...


class LinkView(Protocol):
    @property
    def is_secure(self) -> bool: ...
    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None: ...


def ever_good(snapshot: Mapping[str, TagValue]) -> bool:
    """True once any tag of the device has had a good read (the cache keeps the last good ts)."""
    return any(tv.source_ts is not None for tv in snapshot.values())


def _good_value(tv: TagValue | None, decimals: int) -> Any:
    """The value as published, or the bad marker."""
    if tv is None or tv.quality != "good":
        return clean_value(None, decimals)
    return clean_value(tv.value, decimals)


def changed(device: Device, old: Mapping[str, TagValue], new: Mapping[str, TagValue], decimals: int) -> bool:
    """on_change rule: good <-> not good, or a value move beyond the device's deadband."""
    deadband = device.publish.deadband
    for tag in device.tags:
        if not tag.enabled:
            continue
        a = _good_value(old.get(tag.name), decimals)
        b = _good_value(new.get(tag.name), decimals)
        if is_bad(a) or is_bad(b):
            if is_bad(a) != is_bad(b):
                return True
            continue
        if _is_number(a) and _is_number(b):
            if abs(b - a) > deadband:
                return True
        elif a != b:
            return True
    return False


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class Publisher:
    def __init__(
        self,
        devices: list[Device],
        cache: CacheView,
        link: LinkView,
        cfg: PayloadCfg,
        status_fn: Callable[[str], dict],
        *,
        status_check_s: float = 1.0,
        status_refresh_s: float = 60.0,
        on_change_poll_s: float = 1.0,
        topic_prefix: str = DEFAULT_TOPIC_PREFIX,   # poller.yaml mqtt.topic_prefix
    ) -> None:
        self.devices = [d for d in devices if d.enabled]
        self._cache = cache
        self._link = link
        self._cfg = cfg
        self._status_fn = status_fn
        self._status_check_s = status_check_s
        self._status_refresh_s = status_refresh_s
        self._poll_s = on_change_poll_s
        self._data_topic = {d.id: d.publish.topic or default_topic(d, topic_prefix) for d in self.devices}
        self._status_topic = {d.id: status_topic(d, topic_prefix) for d in self.devices}
        self._wake: dict[str, asyncio.Event] = {d.id: asyncio.Event() for d in self.devices}
        self.data_published: dict[str, int] = {d.id: 0 for d in self.devices}   # for /health and tests

    def notify_cycle(self, device_id: str) -> None:
        """ChannelWorker.on_cycle hook. Call from the event loop thread."""
        ev = self._wake.get(device_id)
        if ev is not None:
            ev.set()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            for dev in self.devices:
                loop_fn = self._on_change_loop if dev.publish.mode == "on_change" else self._interval_loop
                tg.create_task(loop_fn(dev), name=f"publish:{dev.id}")
            tg.create_task(self._status_loop(), name="publish:status")

    # ------------------------------------------------------------------ data
    async def _publish_data(self, dev: Device, snapshot: dict[str, TagValue]) -> None:
        payload = build_payload(dev, snapshot, self._cfg, is_secure=self._link.is_secure)
        await self._link.publish(self._data_topic[dev.id], encode(payload), retain=False)
        self.data_published[dev.id] += 1

    async def _interval_loop(self, dev: Device) -> None:
        loop = asyncio.get_running_loop()
        period = max(dev.publish.interval_ms, MIN_PERIOD_MS) / 1000
        next_t = loop.time() + period
        while True:
            await asyncio.sleep(max(0.0, next_t - loop.time()))
            try:
                snap = self._cache.snapshot(dev.id)
                if ever_good(snap):
                    await self._publish_data(dev, snap)
            except asyncio.CancelledError:
                raise
            except Exception:                                    # noqa: BLE001 - keep publishing other cycles
                log.exception("publish_failed", device=dev.id)
            next_t += period
            now = loop.time()
            if next_t <= now:                                     # fell behind: skip, do not burst
                next_t = now + period

    async def _on_change_loop(self, dev: Device) -> None:
        loop = asyncio.get_running_loop()
        heartbeat = max(dev.publish.heartbeat_ms, MIN_PERIOD_MS) / 1000
        decimals = self._cfg.float_decimals
        wake = self._wake[dev.id]
        last_version: int | None = None
        last_sent: dict[str, TagValue] | None = None
        last_sent_t = loop.time()
        while True:
            timeout = self._poll_s
            if last_sent is not None:                             # heartbeat counts from the last message
                timeout = max(0.0, min(timeout, last_sent_t + heartbeat - loop.time()))
            try:
                await asyncio.wait_for(wake.wait(), timeout)
            except TimeoutError:
                pass
            wake.clear()
            try:
                now = loop.time()
                due = last_sent is not None and now - last_sent_t >= heartbeat
                version = self._cache.version(dev.id)
                if version == last_version and not due:
                    continue
                last_version = version
                snap = self._cache.snapshot(dev.id)
                if not ever_good(snap):
                    continue
                if last_sent is None or due or changed(dev, last_sent, snap, decimals):
                    await self._publish_data(dev, snap)
                    last_sent, last_sent_t = snap, now
            except asyncio.CancelledError:
                raise
            except Exception:                                    # noqa: BLE001
                log.exception("publish_failed", device=dev.id)

    # ------------------------------------------------------------------ status
    async def _status_loop(self) -> None:
        loop = asyncio.get_running_loop()
        last_state: dict[str, str] = {}
        last_sent: dict[str, float] = {}
        while True:
            now = loop.time()
            for dev in self.devices:
                try:
                    body = build_status(self._status_fn(dev.id))
                except Exception:                                # noqa: BLE001 - e.g. during a reload
                    log.debug("status_unavailable", device=dev.id, exc_info=True)
                    continue
                state_changed = body["state"] != last_state.get(dev.id)
                if state_changed or now - last_sent.get(dev.id, -math.inf) >= self._status_refresh_s:
                    try:
                        await self._link.publish(self._status_topic[dev.id], encode(body), retain=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception:                            # noqa: BLE001
                        log.exception("status_publish_failed", device=dev.id)
                        continue
                    if state_changed and dev.id in last_state:
                        log.info("device_status_changed", device=dev.id, old=last_state[dev.id],
                                 new=body["state"])
                    last_state[dev.id] = body["state"]
                    last_sent[dev.id] = now
            await asyncio.sleep(self._status_check_s)
