"""The inter-frame silence is never shorter than configured, even if a timer wakes early (#219)."""

from __future__ import annotations

import asyncio
import time

from opencap.drivers.modbus_rtu import wait_until


class _CoarseClock:
    """A clock that only advances in `tick` steps, like time.monotonic on Windows (~15.6 ms)."""

    def __init__(self, tick: float) -> None:
        self.tick = tick
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def early_sleep(self, delay: float) -> None:
        """Wakes up to one tick early, as asyncio does for a timer due within one resolution."""
        self.now += max(delay - self.tick, 0.001)
        await asyncio.sleep(0)


async def test_wait_never_ends_before_the_deadline():
    for gap in (0.0036, 0.02, 0.12):
        clock = _CoarseClock(0.0156)
        start = clock.now
        await wait_until(start + gap, clock, clock.early_sleep)
        assert clock.now >= start + gap


async def test_a_wait_that_is_due_does_not_sleep():
    clock = _CoarseClock(0.0156)

    async def boom(_: float) -> None:
        raise AssertionError("slept")

    await wait_until(clock.now - 1, clock, boom)


async def test_sleeps_at_least_one_resolution_when_little_is_left():
    resolution = time.get_clock_info("monotonic").resolution
    clock = _CoarseClock(0.0156)
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)
        clock.now += 0.0156

    await wait_until(clock.now + resolution / 10, clock, record)
    assert delays and all(d >= resolution for d in delays)
