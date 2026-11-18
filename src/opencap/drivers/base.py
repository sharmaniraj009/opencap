"""Driver interface (PLAN.md section 5).

Every protocol lives behind `Driver`. The core only sees `plan()` and `read()`.
`plan()` is pure (no I/O, no protocol library import) so it can run in
`opencap check` and in unit tests.

The server is READ-ONLY: `write()` raises and must never be implemented.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import Executor
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, ClassVar, TypeVar

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue

T = TypeVar("T")


class CommError(Exception):
    """One read attempt failed on the wire (timeout, transient device error).

    The ChannelWorker retries up to `device.retries`, then marks the block's tags `bad_comm`.
    """


class ConnectionLost(CommError):
    """The connection is gone (refused, reset, not connected). The worker closes the
    driver and reconnects with exponential backoff."""


class BlockConfigError(Exception):
    """The device permanently rejects this block (e.g. Modbus illegal data address).

    The worker marks the block's tags `bad_config` and stops reading the block until the
    next config reload. This is not a communication failure.
    """


@dataclass(frozen=True)
class DecodeError:
    """Per-tag decode failure, returned as a value in decode results.

    The core turns it into quality `bad_decode` for that tag only.
    """

    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(kw_only=True)
class Block:
    """One wire request. Drivers subclass this and add their own fields."""

    tags: list[str] = field(default_factory=list)   # names of the tags this block serves

    def describe(self) -> str:
        """Short human-readable form for `opencap check`, e.g. 'FC03 44..75 (32 regs)'."""
        return f"{type(self).__name__}[{len(self.tags)} tags]"

    def wire_requests(self) -> int:
        """Estimated number of request/reply round trips this block costs."""
        return 1


@dataclass
class Plan:
    """Result of `Driver.plan()`: the blocks to read, plus tags that cannot be read.

    `invalid` maps tag name -> reason. Those tags get quality `bad_config` and are
    never put on the wire.
    """

    blocks: list[Block] = field(default_factory=list)
    invalid: dict[str, str] = field(default_factory=dict)

    @property
    def wire_requests(self) -> int:
        return sum(b.wire_requests() for b in self.blocks)


class Driver(ABC):
    name: ClassVar[str]
    is_async: ClassVar[bool] = True
    # True when `TagValue.source_ts` is the device's own timestamp of the value (OPC UA
    # SourceTimestamp: when it last changed) rather than when the reply arrived. The worker then
    # judges staleness by when `read()` last returned the tag good (docs/decisions.md D-045).
    stale_by_read_time: ClassVar[bool] = False

    def __init__(self, device: Device, executor: Executor | None = None) -> None:
        self.device = device
        self._executor = executor   # single-thread executor owned by the ChannelWorker (sync drivers)

    @abstractmethod
    def plan(self, tags: list[Tag]) -> Plan:
        """Pure: parse addresses and merge tags into wire blocks."""

    @property
    def connected(self) -> bool:
        """True while the driver believes its connection is usable."""
        return False

    def connect_timeout_s(self) -> float | None:
        """Outer time budget for `connect()`, or None for the core's default per-call safety net.

        Drivers whose connect does more than one round trip (e.g. a tag-list upload) override it.
        """
        return None

    async def connect(self) -> None:
        raise NotImplementedError(f"{self.name}: connect() not implemented yet")

    async def close(self) -> None:
        raise NotImplementedError(f"{self.name}: close() not implemented yet")

    async def read(self, block: Block) -> dict[str, TagValue]:
        raise NotImplementedError(f"{self.name}: read() not implemented yet")

    async def identity(self) -> dict[str, Any]:
        return {}

    async def write(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("opencap is read-only: writes to PLCs are not supported")

    async def _run_sync(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a blocking library call on the channel's own single-thread executor."""
        if self._executor is None:
            raise RuntimeError(f"{self.name}: sync driver needs the channel executor")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))
