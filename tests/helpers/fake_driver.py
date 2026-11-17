"""Scriptable fake `Driver` for core tests: no sockets, no protocol library.

Addresses: the part before the first "." is the block key, so tags "A.0", "A.1" share
block "A". An address starting with "!" is unusable and goes to `Plan.invalid`.

    factory = FakeFactory()                       # pass as ChannelWorker(driver_factory=...)
    script = factory.script("SITE/PLC")           # behaviour + records of one device
    script.delay_s = 0.01                         # every read takes 10 ms
    script.timeout_blocks.add("A")                # reads of block A raise CommError
    script.fail_next(CommError("boom"), times=2)  # the next two reads fail, then normal

Default values change on every good read: the value of a tag is the number of good reads
of its block so far (bool: odd/even, string: "s<n>"). `script.values[name]` overrides it
with a constant or a callable `f(n)`.

`ConcurrencyProbe` objects record how many requests (connect or read) are in flight at
once; pass one per channel and one shared by all channels to check §3.1.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from opencap.config.models import Channel, Device, Tag
from opencap.core.cache import TagValue
from opencap.core.registry import DevicePlan, RateGroupPlan
from opencap.drivers.base import Block, BlockConfigError, CommError, ConnectionLost, Driver, Plan

_ids = itertools.count(1)


def uniq(prefix: str = "CH") -> str:
    """Unique name, so global Prometheus metrics do not leak between tests."""
    return f"{prefix}{next(_ids)}"


@dataclass(kw_only=True)
class FakeBlock(Block):
    key: str = ""

    def describe(self) -> str:
        return f"fake {self.key} [{len(self.tags)} tags]"


class ConcurrencyProbe:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.total = 0

    def __enter__(self) -> ConcurrencyProbe:
        self.in_flight += 1
        self.total += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.in_flight -= 1


@dataclass
class FakeScript:
    """Behaviour and records of one fake device. Tests may change fields at any time."""

    # behaviour
    delay_s: float = 0.0
    block_delay: dict[str, float] = field(default_factory=dict)
    connect_delay_s: float = 0.0
    down: bool = False                       # connect() raises ConnectionLost
    unsupported: bool = False                # connect() raises NotImplementedError
    hang: bool = False                       # read() never returns (async driver only)
    timeout_blocks: set[str] = field(default_factory=set)       # read raises CommError
    lost_blocks: set[str] = field(default_factory=set)          # read raises ConnectionLost
    config_error_blocks: set[str] = field(default_factory=set)  # read raises BlockConfigError
    decode_error_tags: set[str] = field(default_factory=set)    # tag comes back bad_decode
    close_on_timeout: bool = False           # a CommError also closes the connection (Modbus-like)
    values: dict[str, Any] = field(default_factory=dict)        # name -> constant or f(n)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _read_failures: deque = field(default_factory=deque)
    _connect_failures: deque = field(default_factory=deque)

    # records
    connect_attempts: list[float] = field(default_factory=list)    # monotonic times
    connects_ok: int = 0
    closes: int = 0
    reads: list[tuple[str, float]] = field(default_factory=list)   # (block key, start time)
    read_spans: list[tuple[str, float, float]] = field(default_factory=list)  # (key, start, end)
    good_reads: Counter = field(default_factory=Counter)           # block key -> good reads
    threads: list[str] = field(default_factory=list)               # thread of every sync call

    def fail_next(self, exc: BaseException, times: int = 1) -> None:
        """The next `times` reads (any block) raise `exc` before anything else."""
        self._read_failures.extend([exc] * times)

    def fail_next_connect(self, exc: BaseException, times: int = 1) -> None:
        self._connect_failures.extend([exc] * times)

    def reads_of(self, key: str) -> int:
        return sum(1 for k, _ in self.reads if k == key)


def _default_value(tag: Tag, n: int) -> Any:
    if tag.dtype == "bool":
        return n % 2 == 1
    if tag.dtype == "string":
        return f"s{n}"
    return n


class FakeDriver(Driver):
    name: ClassVar[str] = "fake"
    is_async: ClassVar[bool] = True

    def __init__(
        self,
        device: Device,
        executor: Any = None,
        *,
        script: FakeScript | None = None,
        probes: Iterable[ConcurrencyProbe] = (),
    ) -> None:
        super().__init__(device, executor)
        self.script = script or FakeScript()
        self.probes = list(probes)
        self._connected = False
        self.closed = False
        self._tags = {t.name: t for t in device.tags}

    @property
    def connected(self) -> bool:
        return self._connected

    # --- pure -------------------------------------------------------------------------

    def plan(self, tags: list[Tag]) -> Plan:
        blocks: dict[str, FakeBlock] = {}
        invalid: dict[str, str] = {}
        for tag in tags:
            if not tag.enabled:
                continue
            if tag.address.startswith("!"):
                invalid[tag.name] = f"unusable fake address {tag.address!r}"
                continue
            key = tag.address.split(".", 1)[0]
            blocks.setdefault(key, FakeBlock(key=key)).tags.append(tag.name)
        return Plan(blocks=list(blocks.values()), invalid=invalid)

    # --- shared logic -----------------------------------------------------------------

    def _enter(self) -> None:
        for p in self.probes:
            p.__enter__()

    def _exit(self) -> None:
        for p in self.probes:
            p.__exit__()

    def _connect_logic(self) -> None:
        s = self.script
        if s._connect_failures:
            raise s._connect_failures.popleft()
        if s.unsupported:
            raise NotImplementedError("fake: connect() not implemented")
        if s.down:
            raise ConnectionLost("fake: connection refused")
        self._connected = True
        self.closed = False
        s.connects_ok += 1

    def _read_logic(self, block: FakeBlock) -> dict[str, TagValue]:
        s = self.script
        if not self._connected:
            raise ConnectionLost("fake: not connected")
        if s._read_failures:
            exc = s._read_failures.popleft()
            if isinstance(exc, CommError) and not isinstance(exc, ConnectionLost) and s.close_on_timeout:
                self._connected = False
            if isinstance(exc, ConnectionLost):
                self._connected = False
            raise exc
        if block.key in s.config_error_blocks:
            raise BlockConfigError(f"fake: illegal address in block {block.key}")
        if block.key in s.lost_blocks or s.down:
            self._connected = False
            raise ConnectionLost("fake: connection reset")
        if block.key in s.timeout_blocks:
            if s.close_on_timeout:
                self._connected = False
            raise CommError("fake: timeout")
        s.good_reads[block.key] += 1
        n = s.good_reads[block.key]
        ts = s.clock()
        out: dict[str, TagValue] = {}
        for name in block.tags:
            if name in s.decode_error_tags:
                out[name] = TagValue(None, "bad_decode", ts)
                continue
            v = s.values.get(name, None)
            if v is None:
                v = _default_value(self._tags[name], n)
            elif callable(v):
                v = v(n)
            out[name] = TagValue(v, "good", ts)
        return out

    def _delay(self, block: FakeBlock) -> float:
        return self.script.block_delay.get(block.key, self.script.delay_s)

    # --- async I/O --------------------------------------------------------------------

    async def connect(self) -> None:
        self._enter()
        try:
            self.script.connect_attempts.append(time.monotonic())
            if self.script.connect_delay_s:
                await asyncio.sleep(self.script.connect_delay_s)
            self._connect_logic()
        finally:
            self._exit()

    async def close(self) -> None:
        self._connected = False
        self.closed = True
        self.script.closes += 1

    async def read(self, block: FakeBlock) -> dict[str, TagValue]:  # type: ignore[override]
        self._enter()
        start = time.monotonic()
        self.script.reads.append((block.key, start))
        try:
            if self.script.hang:
                await asyncio.Event().wait()
            delay = self._delay(block)
            if delay:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
            return self._read_logic(block)
        finally:
            self.script.read_spans.append((block.key, start, time.monotonic()))
            self._exit()


class SyncFakeDriver(FakeDriver):
    """Same behaviour, but every call blocks and runs through `Driver._run_sync`."""

    is_async: ClassVar[bool] = False

    def _blocking(self, fn: Callable[[], Any], delay: float) -> Any:
        self.script.threads.append(threading.current_thread().name)
        if delay:
            time.sleep(delay)
        return fn()

    async def connect(self) -> None:
        self._enter()
        try:
            self.script.connect_attempts.append(time.monotonic())
            await self._run_sync(self._blocking, self._connect_logic, self.script.connect_delay_s)
        finally:
            self._exit()

    async def close(self) -> None:
        def _close() -> None:
            self._connected = False
            self.closed = True
            self.script.closes += 1

        await self._run_sync(self._blocking, _close, 0.0)

    async def read(self, block: FakeBlock) -> dict[str, TagValue]:  # type: ignore[override]
        self._enter()
        start = time.monotonic()
        self.script.reads.append((block.key, start))
        try:
            return await self._run_sync(self._blocking, lambda: self._read_logic(block), self._delay(block))
        finally:
            self.script.read_spans.append((block.key, start, time.monotonic()))
            self._exit()


class FakeFactory:
    """`driver_factory` for ChannelWorker; one FakeScript per device id."""

    def __init__(self, *, sync: bool = False, probes: Iterable[ConcurrencyProbe] = ()) -> None:
        self.sync = sync
        self.probes = list(probes)
        self.scripts: dict[str, FakeScript] = {}
        self.drivers: dict[str, FakeDriver] = {}
        self.executors: dict[str, Any] = {}

    def script(self, device_id: str) -> FakeScript:
        return self.scripts.setdefault(device_id, FakeScript())

    def __call__(self, device: Device, executor: Any) -> FakeDriver:
        cls = SyncFakeDriver if self.sync else FakeDriver
        drv = cls(device, executor, script=self.script(device.id), probes=self.probes)
        self.drivers[device.id] = drv
        self.executors[device.id] = executor
        return drv


# --- config builders ----------------------------------------------------------------------

def tags(block: str, n: int = 2, *, dtype: str = "float32", scan_ms: int | None = None, **kw: Any) -> list[Tag]:
    return [Tag(name=f"{block}_{i}", address=f"{block}.{i}", dtype=dtype, scan_ms=scan_ms, **kw) for i in range(n)]


def device(channel: str, name: str, tag_list: list[Tag], **kw: Any) -> Device:
    """A test device. Defaults are fast: 50 ms scan, no 'no faster than' floor."""
    defaults: dict[str, Any] = dict(
        scan_ms=50, min_scan_ms=1, timeout_ms=100, retries=0, demote_after=3, demote_for_ms=30_000,
    )
    defaults.update(kw)
    return Device(
        id=f"{channel}/{name}", channel=channel, name=name, driver="modbus_tcp",
        host="127.0.0.1", port=502, tags=tag_list, **defaults,
    )


def channel(name: str, devices: list[Device], *, enabled: bool = True) -> Channel:
    return Channel(name=name, enabled=enabled, devices=devices)


def fake_plan(dev: Device) -> DevicePlan:
    drv = FakeDriver(dev)
    groups = [
        RateGroupPlan(rate_ms=rate, tag_count=len(group), plan=drv.plan(group))
        for rate, group in dev.rate_groups().items()
    ]
    return DevicePlan(device=dev, groups=groups)


def fake_plans(*channels: Channel) -> dict[str, DevicePlan]:
    return {d.id: fake_plan(d) for ch in channels for d in ch.devices}
