"""ChannelWorker: polls every device of one channel (PLAN.md §3.1 and §6, contract §2).

Concurrency
- One worker per channel. Inside it each polled device runs its own rate-group loop
  (an asyncio task), but every wire operation (connect, read, close) holds the channel's
  FIFO wire lock, so exactly one request is on the wire at a time across the whole channel
  and devices interleave request by request.
- The worker owns one ThreadPoolExecutor(max_workers=1) and passes it to every driver;
  sync drivers run their blocking library calls there through `Driver._run_sync`.
- Every driver call is wrapped in a safety-net `asyncio.wait_for` of
  `timeout_ms * 1.25 + 0.1 s`. Channels share nothing, so a slow or dead device never
  blocks another channel.

Device loop: find the next due rate group (core/scheduler.py) -> read its blocks in order ->
update the cache once -> health -> `on_cycle(device_id)` -> sleep until the next due time.

Faults (health rules and the "failed cycle" definition are in core/health.py)
- CommError / safety-net timeout: retry up to `device.retries`, then the block's tags are
  `bad_comm` (last value kept). A safety-net timeout also closes the connection, because
  the driver is in an unknown state. Before each retry, if `driver.connected` is False
  (the driver closed its socket on the timeout, `close_on_timeout`), the worker calls
  `connect()` once at once, inside the same retry budget and without backoff, like
  KEPServer. Only a failed `connect()` starts the backoff.
- ConnectionLost on a read: close the driver and reconnect at once, then repeat the same
  attempt (a dead socket does not use up a retry). A ConnectionLost right after a fresh
  connect, a failed connect, or a pending backoff ends the cycle: the remaining blocks are
  `bad_comm` with no request sent.
- Connect failures back off 1 s, 2 s, 4 s ... 60 s (no request while backing off). The
  backoff resets on a successful connect.
- BlockConfigError: the block's tags are `bad_config` and the block is never read again by
  this worker. Plan-invalid tags are set to `bad_config` once, when the worker is built.
- NotImplementedError from the driver (driver I/O not written yet) or a driver that cannot
  be built: the device stops being polled, state stays `offline`, `last_error` says why.
- Demotion: the device's connection is closed when it is demoted. The probe cycle runs when
  the demotion period is over, the group is due, and no reconnect backoff is pending; it
  stops at its first failed block.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from opencap.config.models import Channel, Device, Scale, Tag
from opencap.core.cache import TagValue, ValueCache
from opencap.core.health import (
    EV_DEMOTED,
    EV_OFFLINE,
    EV_OK,
    EV_REDEMOTED,
    EV_RESUMED,
    Backoff,
    DeviceHealth,
)
from opencap.core.registry import DevicePlan
from opencap.core.scheduler import START_JITTER, DeviceSchedule
from opencap.drivers.base import Block, BlockConfigError, CommError, ConnectionLost, DecodeError, Driver
from opencap.metrics import DEVICE_STATE, DEVICE_STATE_CODES, POLL_CYCLE, POLL_OVERRUN, POLL_REQUESTS

_log = structlog.get_logger("opencap.core.channel_worker")
_mono = time.monotonic

SAFETY_FACTOR = 1.25
SAFETY_MARGIN_S = 0.1
STALE_FACTOR = 3            # a tag with no good read for 3 x its effective rate -> stale
LOOP_ERROR_PAUSE_S = 1.0

DriverFactory = Callable[[Device, Executor], Driver]


def safety_timeout_s(timeout_ms: int) -> float:
    """Outer bound for one driver call; the driver's own `timeout_ms` should fire first."""
    return max(timeout_ms, 0) / 1000 * SAFETY_FACTOR + SAFETY_MARGIN_S


def default_driver_factory(device: Device, executor: Executor) -> Driver:
    from opencap.drivers import get_driver_class

    return get_driver_class(device.driver)(device, executor)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _reports_connection(driver: Driver) -> bool:
    """True if the driver class implements `connected` (the base default is always False).

    For drivers that do not, the worker only reconnects before a retry, never before the
    first attempt, so it does not reconnect before every request.
    """
    return type(driver).connected is not Driver.connected


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def scale_value(scale: Scale, value: Any) -> Any:
    """Apply `scale` to a number, or element-wise to an array. bool, str and None pass through."""
    if _is_number(value):
        return scale.apply(value)
    if isinstance(value, (list, tuple)):
        return [scale_value(scale, v) for v in value]
    return value


class _Unsupported(Exception):
    """The device cannot be read by this worker at all (driver missing / not implemented)."""


_OK, _BAD_CONFIG, _COMM, _CONN_DOWN = "ok", "bad_config", "comm", "conn_down"


@dataclass(slots=True)
class _BlockResult:
    kind: str
    values: dict[str, TagValue] | None = None
    error: str | None = None


STATS_INTERVAL_S = 300.0       # PLAN Phase 2: "cycle time per site logged" (INFO, every 5 min)


class CycleStats:
    """Cycle-time summary between two `device.stats` log lines."""

    __slots__ = ("cycles", "total_ms", "max_ms", "overruns", "bad_cycles")

    def __init__(self) -> None:
        self.cycles = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.overruns = 0
        self.bad_cycles = 0

    def add(self, ms: float, *, overrun: bool, good: bool) -> None:
        self.cycles += 1
        self.total_ms += ms
        self.max_ms = max(self.max_ms, ms)
        self.overruns += overrun
        self.bad_cycles += not good

    def take(self) -> dict | None:
        """The summary since the last call, or None when no cycle ran; then reset."""
        if not self.cycles:
            return None
        out = {"cycles": self.cycles, "mean_cycle_ms": round(self.total_ms / self.cycles, 1),
               "max_cycle_ms": round(self.max_ms, 1), "overruns": self.overruns,
               "bad_cycles": self.bad_cycles}
        self.__init__()
        return out


class _DeviceRunner:
    """Polling state of one device. Internal to ChannelWorker."""

    def __init__(self, worker: ChannelWorker, device: Device, plan: DevicePlan | None) -> None:
        self.w = worker
        self.device = device
        self.id = device.id
        self.plan = plan
        self.health = DeviceHealth.for_device(device)
        self.backoff = Backoff(worker.backoff_base_s, worker.backoff_max_s)
        self.cycle_lock = asyncio.Lock()      # serializes run() cycles with read_once()
        self.wake = asyncio.Event()           # read_once() changed state: re-evaluate sleeps
        self.driver: Driver | None = None
        self.connected = False                # our view of the driver connection
        self.link_up = False                  # operator view, for INFO connected/disconnected lines
        self.last_error: str | None = None
        self.connect_error: str | None = None
        self.cycle_ms: float | None = None
        self.stats = CycleStats()             # summary for the periodic INFO `device.stats` line
        self.unsupported: str | None = None
        self.registered = False
        self.schedule: DeviceSchedule | None = None
        self.tags: dict[str, Tag] = {t.name: t for t in device.tags if t.enabled}
        self.max_age_s = {n: STALE_FACTOR * device.effective_rate_ms(t) / 1000 for n, t in self.tags.items()}
        self.safety_s = safety_timeout_s(device.timeout_ms)
        usable = plan is not None and plan.ok
        self.groups: list[list[Block]] = [list(g.plan.blocks) for g in plan.groups] if usable else []
        self.rates_s: list[float] = [g.rate_ms / 1000 for g in plan.groups] if usable else []
        self.log = _log.bind(channel=device.channel, device=device.id)

    # --- static facts -----------------------------------------------------------------

    @property
    def broken(self) -> str | None:
        """Why this device can never be read by this worker, or None."""
        if self.plan is None:
            return "no block plan for this device"
        if self.plan.error:
            return self.plan.error
        return self.unsupported

    def not_polled_reason(self) -> str | None:
        if not self.w.channel.enabled:
            return "channel disabled"
        if not self.device.enabled:
            return "device disabled"
        if self.broken:
            return self.broken
        if not any(self.groups):
            return "no readable blocks"
        return None

    def read_budget_s(self, block: Block) -> float:
        """Safety net for one `read(block)`: a block can cost several round trips (e.g. a
        Micro800 without batching sends one request per tag), each bounded by `timeout_ms`."""
        return self.safety_s * max(1, block.wire_requests())

    def ensure_registered(self) -> None:
        if self.registered:
            return
        self.w.cache.register(self.device)
        invalid = self.plan.invalid if self.plan is not None and self.plan.ok else {}
        if invalid:
            self.w.cache.update(self.id, {name: TagValue(None, "bad_config", None) for name in invalid})
            self.log.info("device.invalid_tags", count=len(invalid), tags=sorted(invalid)[:20])
        self.registered = True

    def disable_block(self, block: Block) -> None:
        for i, blocks in enumerate(self.groups):
            if any(b is block for b in blocks):
                self.groups[i] = [b for b in blocks if b is not block]

    # --- connection -------------------------------------------------------------------

    def ensure_driver(self) -> Driver:
        if self.driver is None:
            try:
                self.driver = self.w.driver_factory(self.device, self.w.executor)
            except Exception as exc:
                raise _Unsupported(f"cannot create driver: {_describe(exc)}") from exc
        return self.driver

    async def connect(self, *, force: bool) -> str | None:
        """One connect attempt. Returns None when connected, else the reason."""
        now = _mono()
        if not force and self.backoff.pending(now):
            return f"{self.connect_error or 'connect failed'} (next attempt in {self.backoff.remaining(now):.1f} s)"
        drv = self.ensure_driver()
        t0 = _mono()
        try:
            async with self.w.wire:
                await asyncio.wait_for(drv.connect(), drv.connect_timeout_s() or self.safety_s)
        except asyncio.CancelledError:
            raise
        except NotImplementedError as exc:
            raise _Unsupported(_describe(exc)) from exc
        except Exception as exc:
            reason = "no answer within the safety timeout" if isinstance(exc, TimeoutError) else _describe(exc)
            self.connect_error = f"connect failed: {reason}"
            POLL_REQUESTS.labels(self.id, "conn_error").inc()
            delay = self.backoff.failure(_mono())
            await self.close_driver()
            self.log.debug("device.connect_failed", error=reason, retry_in_s=round(delay, 3),
                           ms=round((_mono() - t0) * 1000, 1))
            if self.link_up:
                self.link_up = False
                self.log.info("device.disconnected", error=reason)
            return self.connect_error
        self.connected = True
        self.connect_error = None
        self.backoff.reset()
        ms = round((_mono() - t0) * 1000, 1)
        if not self.link_up:
            self.link_up = True
            self.log.info("device.connected", host=self.device.host, port=self.device.port, ms=ms)
        else:
            self.log.debug("device.reconnected", ms=ms)
        return None

    async def drop(self, reason: str) -> None:
        if self.connected:
            self.log.debug("device.connection_dropped", reason=reason)
        self.connected = False
        await self.close_driver()

    async def close_driver(self) -> None:
        drv = self.driver
        if drv is None:
            return
        try:
            async with self.w.wire:
                await asyncio.wait_for(drv.close(), self.safety_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.debug("device.close_failed", error=_describe(exc))

    # --- one block --------------------------------------------------------------------

    def _count(self, result: str, block: Block, t0: float, attempt: int, error: str | None = None) -> None:
        POLL_REQUESTS.labels(self.id, result).inc()
        self.log.debug("device.request", block=block.describe(), result=result, attempt=attempt + 1,
                       ms=round((_mono() - t0) * 1000, 1), error=error)

    async def read_block(self, block: Block, *, force_connect: bool) -> _BlockResult:
        attempts = max(self.device.retries, 0) + 1
        attempt = 0
        fresh = False            # connected during the current attempt
        last_error = "no attempt made"
        while attempt < attempts:
            drv = self.driver
            if (self.connected and drv is not None and not drv.connected
                    and (attempt > 0 or _reports_connection(drv))):
                # The driver closed its socket (close_on_timeout after a timeout, or the
                # peer closed it). Reopen at once, like KEPServer: this uses no retry and no
                # backoff. Only a failed connect() starts the backoff.
                self.connected = False
                self.log.debug("device.reopen", block=block.describe(), attempt=attempt + 1)
            if not self.connected:
                err = await self.connect(force=force_connect)
                force_connect = False
                if err is not None:
                    return _BlockResult(_CONN_DOWN, error=err)
                fresh = True
            assert self.driver is not None
            t0 = _mono()
            try:
                async with self.w.wire:
                    values = await asyncio.wait_for(self.driver.read(block), self.read_budget_s(block))
            except asyncio.CancelledError:
                raise
            except BlockConfigError as exc:
                self._count("bad_config", block, t0, attempt, str(exc))
                return _BlockResult(_BAD_CONFIG, error=f"{block.describe()}: rejected: {_describe(exc)}")
            except ConnectionLost as exc:
                self._count("conn_error", block, t0, attempt, str(exc))
                last_error = f"connection lost: {_describe(exc)}"
                await self.drop(last_error)
                if fresh:
                    return _BlockResult(_CONN_DOWN, error=last_error)
                continue         # the old connection was dead: reconnect, same attempt
            except CommError as exc:
                self._count("timeout", block, t0, attempt, str(exc))
                last_error = f"{block.describe()}: {_describe(exc)}"
            except TimeoutError:
                self._count("timeout", block, t0, attempt, "safety-net timeout")
                last_error = f"{block.describe()}: no answer within the safety timeout"
                await self.drop(last_error)
            except NotImplementedError as exc:
                raise _Unsupported(_describe(exc)) from exc
            except Exception as exc:
                self._count("error", block, t0, attempt, _describe(exc))
                last_error = f"{block.describe()}: {_describe(exc)}"
            else:
                self._count("ok", block, t0, attempt)
                return _BlockResult(_OK, values=values or {})
            attempt += 1
            fresh = False
        return _BlockResult(_COMM, error=last_error)

    def postprocess(self, block: Block, result: dict[str, TagValue]) -> dict[str, TagValue]:
        """Driver result -> cache values: scaling, missing tags, default source_ts."""
        out: dict[str, TagValue] = {}
        fallback_ts: datetime | None = None
        for name in block.tags:
            tv = result.get(name)
            if tv is None:
                out[name] = TagValue(None, "bad_decode", None)
                continue
            if tv.quality != "good" or isinstance(tv.value, DecodeError):
                quality = "bad_decode" if tv.quality == "good" else tv.quality
                out[name] = TagValue(None, quality, tv.source_ts)
                continue
            value = tv.value
            tag = self.tags.get(name)
            if tag is not None and tag.scale is not None:
                try:
                    value = scale_value(tag.scale, value)
                except (ValueError, TypeError, ArithmeticError) as exc:
                    self.log.debug("device.scale_error", tag=name, error=_describe(exc))
                    out[name] = TagValue(None, "bad_decode", tv.source_ts)
                    continue
            ts = tv.source_ts
            if ts is None:
                fallback_ts = fallback_ts or self.w.clock()
                ts = fallback_ts
            out[name] = TagValue(value, "good", ts)
        return out

    # --- one cycle --------------------------------------------------------------------

    async def cycle(self, blocks: list[Block], *, probe: bool, force_connect: bool) -> str:
        """Read `blocks` in order, update cache and health. Returns good | failed | neutral."""
        values: dict[str, TagValue] = {}
        any_ok = comm_failed = False
        errors: list[str] = []
        for i, block in enumerate(blocks):
            res = await self.read_block(block, force_connect=force_connect)
            force_connect = False
            if res.kind == _OK:
                any_ok = True
                values.update(self.postprocess(block, res.values or {}))
            elif res.kind == _BAD_CONFIG:
                self.disable_block(block)
                errors.append(res.error or "block rejected")
                values.update({name: TagValue(None, "bad_config", None) for name in block.tags})
                self.log.warning("device.block_rejected", block=block.describe(), error=res.error)
            else:
                comm_failed = True
                errors.append(res.error or "communication failed")
                abort = res.kind == _CONN_DOWN or (probe and not any_ok)
                for b in blocks[i:] if abort else [block]:
                    values.update({name: TagValue(None, "bad_comm", None) for name in b.tags})
                if abort:
                    break
        if values:
            self.w.cache.update(self.id, values)
        now = _mono()
        if any_ok:
            outcome, event = "good", self.health.record_good(now, self.w.clock())
        elif comm_failed:
            outcome, event = "failed", self.health.record_failure(now)
        else:
            outcome, event = "neutral", None
        if errors:
            self.last_error = errors[-1]
        elif any_ok:
            self.last_error = None
        await self.on_event(event)
        return outcome

    async def on_event(self, event: str | None) -> None:
        if event is None:
            return
        DEVICE_STATE.labels(self.id).set(DEVICE_STATE_CODES[self.health.state])
        if event == EV_DEMOTED:
            self.log.info("device.demoted", after_failed_cycles=self.health.consecutive_failures,
                          for_ms=self.device.demote_for_ms, error=self.last_error)
        elif event == EV_REDEMOTED:
            self.log.debug("device.probe_failed", for_ms=self.device.demote_for_ms, error=self.last_error)
        elif event == EV_RESUMED:
            self.log.info("device.resumed")
        elif event == EV_OK:
            self.log.info("device.ok")
        elif event == EV_OFFLINE:
            self.log.info("device.offline", error=self.last_error)
        if event in (EV_DEMOTED, EV_REDEMOTED):
            await self.drop("demoted")
        self.wake.set()

    def mark_unsupported(self, reason: str) -> None:
        self.unsupported = reason
        self.last_error = reason
        self.connected = False
        self.log.warning("device.unsupported", error=reason)

    # --- loops ------------------------------------------------------------------------

    async def sleep(self, delay: float) -> None:
        # No await between the caller's state check and clear(), so a wake cannot be lost.
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), delay)
        except TimeoutError:
            pass

    async def poll_loop(self) -> None:
        sched = DeviceSchedule(
            {i: rate for i, rate in enumerate(self.rates_s) if self.groups[i]}, _mono(),
            jitter=self.w.start_jitter, rng=self.w.rng,
        )
        self.schedule = sched
        while len(sched):
            try:
                await self.poll_step(sched)
            except asyncio.CancelledError:
                raise
            except _Unsupported as exc:
                self.mark_unsupported(str(exc))
                return
            except Exception as exc:
                self.last_error = f"internal error: {_describe(exc)}"
                self.log.exception("device.loop_error")
                await asyncio.sleep(LOOP_ERROR_PAUSE_S)
        self.log.warning("device.idle", reason="no readable blocks left")

    async def poll_step(self, sched: DeviceSchedule) -> None:
        now = _mono()
        slot = sched.due()
        wait = slot.next_due - now
        if self.health.state == "demoted":
            backoff = 0.0 if self.connected else self.backoff.remaining(now)
            wait = max(wait, self.health.demoted_remaining(now), backoff)
        if wait > 0:
            await self.sleep(wait)
            return
        async with self.cycle_lock:
            if self.health.state == "demoted" and not self.health.probe_due(_mono()):
                return           # a read_once() demoted the device while we waited
            probe = self.health.state == "demoted"
            started = _mono()
            await self.cycle(list(self.groups[slot.index]), probe=probe, force_connect=False)
            ended = _mono()
        self.cycle_ms = round((ended - started) * 1000, 1)
        POLL_CYCLE.labels(self.id).observe(ended - started)
        overrun = sched.complete(slot, started, ended)
        self.stats.add(self.cycle_ms, overrun=overrun, good=self.health.state == "ok")
        if overrun:
            POLL_OVERRUN.labels(self.id).inc()
            self.log.debug("device.overrun", rate_ms=round(slot.rate_s * 1000), cycle_ms=self.cycle_ms)
        if not self.groups[slot.index]:
            sched.remove(slot.index)
        self.w.notify(self.id)

    async def read_once(self) -> dict[str, TagValue]:
        self.ensure_registered()
        async with self.cycle_lock:
            blocks = [b for group in self.groups for b in group]
            try:
                await self.cycle(blocks, probe=False, force_connect=True)
            except _Unsupported as exc:
                self.mark_unsupported(str(exc))
                raise RuntimeError(f"{self.id}: {exc}") from exc
        self.wake.set()
        self.w.notify(self.id)
        return self.w.cache.snapshot(self.id)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or "channel"


class ChannelWorker:
    """Polls the devices of one channel. See the module docstring for the rules.

    Keyword-only extras beyond contract §2 (all optional, for tests and tuning):
    `start_jitter` (fraction of the rate, default 0.05), `backoff_base_s` / `backoff_max_s`
    (default 1 s / 60 s) and `rng` (random.Random for the start jitter).
    """

    def __init__(
        self,
        channel: Channel,
        plans: dict[str, DevicePlan],
        cache: ValueCache,
        *,
        on_cycle: Callable[[str], None] | None = None,
        driver_factory: DriverFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        start_jitter: float = START_JITTER,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        rng: Any = None,
        stats_interval_s: float = STATS_INTERVAL_S,
    ) -> None:
        self.channel = channel
        self.cache = cache
        self.on_cycle = on_cycle
        self.driver_factory: DriverFactory = driver_factory or default_driver_factory
        self.clock: Callable[[], datetime] = clock or _utcnow
        self.start_jitter = start_jitter
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.rng = rng
        self.stats_interval_s = stats_interval_s
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"opencap-{_slug(channel.name)}")
        self.wire = asyncio.Lock()
        self.log = _log.bind(channel=channel.name)
        self._runners: dict[str, _DeviceRunner] = {
            d.id: _DeviceRunner(self, d, plans.get(d.id)) for d in channel.devices
        }
        self._run_task: asyncio.Task | None = None
        self._oneshots: set[asyncio.Task] = set()
        self._stopped = False
        if channel.enabled:
            for runner in self._runners.values():
                if runner.device.enabled:
                    runner.ensure_registered()

    # --- public API (contract §2) -----------------------------------------------------

    @property
    def device_ids(self) -> list[str]:
        return list(self._runners)

    async def run(self) -> None:
        """Poll until cancelled (or until `stop()`)."""
        if self._stopped:
            raise RuntimeError("channel worker stopped")
        if self._run_task is not None:
            raise RuntimeError("channel worker already running")
        self._run_task = asyncio.current_task()
        polled = [r for r in self._runners.values() if r.not_polled_reason() is None]
        for r in self._runners.values():
            reason = r.not_polled_reason()
            if reason is not None:
                r.log.info("device.not_polled", reason=reason)
        tasks: list[asyncio.Task] = []
        try:
            for r in polled:
                DEVICE_STATE.labels(r.id).set(DEVICE_STATE_CODES[r.health.state])
                tasks.append(asyncio.create_task(r.poll_loop(), name=f"poll {r.id}"))
            if polled:
                tasks.append(asyncio.create_task(self._housekeeping(polled), name=f"expire {self.channel.name}"))
            self.log.info("channel.started", devices=len(polled))
            await asyncio.Future()           # until cancelled
        finally:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for r in polled:
                if not r.cycle_lock.locked():    # a read_once() may still be using the driver
                    r.connected = False
                    await r.close_driver()
            self._run_task = None
            self.log.info("channel.stopped")

    async def stop(self) -> None:
        """Cancel `run()` and any `read_once()`, close every driver, shut down the executor."""
        if self._stopped:
            return
        self._stopped = True
        task = self._run_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.wait({task})
        oneshots = [t for t in self._oneshots if not t.done()]
        for t in oneshots:
            t.cancel()
        if oneshots:
            await asyncio.wait(oneshots)
        for r in self._runners.values():
            r.connected = False
            await r.close_driver()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.log.info("channel.closed")

    async def read_once(self, device_id: str) -> dict[str, TagValue]:
        """Run one full cycle (every rate group) of the device now and return its cache snapshot.

        Serialized with `run()` on the same device; requests still share the channel wire
        lock. Works on disabled devices and while `run()` is not running (CLI), ignores
        demotion and makes one connect attempt even during reconnect backoff. The result
        feeds health like any cycle. Raises KeyError for an unknown device and RuntimeError
        for a device without a usable driver/plan or after `stop()`.
        """
        if self._stopped:
            raise RuntimeError("channel worker stopped")
        runner = self._runner(device_id)
        if runner.broken:
            raise RuntimeError(f"{device_id}: {runner.broken}")
        task = asyncio.create_task(runner.read_once(), name=f"read-once {device_id}")
        self._oneshots.add(task)
        task.add_done_callback(self._oneshots.discard)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and self._stopped and (current is None or not current.cancelling()):
                raise RuntimeError("channel worker stopped") from None
            raise

    def health(self, device_id: str) -> DeviceHealth:
        return self._runner(device_id).health

    def device_status(self, device_id: str) -> dict:
        r = self._runner(device_id)
        status = r.health.status()
        status["cycle_ms"] = r.cycle_ms
        status["last_error"] = r.last_error if r.last_error is not None else r.not_polled_reason()
        status["connected"] = r.connected and r.driver is not None and r.driver.connected
        return status

    # --- internals --------------------------------------------------------------------

    def _runner(self, device_id: str) -> _DeviceRunner:
        try:
            return self._runners[device_id]
        except KeyError:
            raise KeyError(f"device {device_id!r} is not in channel {self.channel.name!r}") from None

    def notify(self, device_id: str) -> None:
        if self.on_cycle is None:
            return
        try:
            self.on_cycle(device_id)
        except Exception:
            self.log.exception("channel.on_cycle_error", device=device_id)

    async def _housekeeping(self, runners: Iterable[_DeviceRunner]) -> None:
        """Mark good tags stale when their group has not been read for 3 x its rate."""
        runners = list(runners)
        fastest = min((rate for r in runners for rate in r.rates_s), default=1.0)
        tick = min(1.0, max(0.02, fastest / 2))
        next_stats = _mono() + self.stats_interval_s
        while True:
            await asyncio.sleep(tick)
            now = self.clock()
            for r in runners:
                if r.id in self.cache:
                    self.cache.expire(r.id, now, r.max_age_s)
            if self.stats_interval_s > 0 and _mono() >= next_stats:
                next_stats = _mono() + self.stats_interval_s
                for r in runners:
                    summary = r.stats.take()
                    if summary is not None:
                        self.log.info("device.stats", device=r.id, state=r.health.state,
                                      period_s=round(self.stats_interval_s), **summary)
