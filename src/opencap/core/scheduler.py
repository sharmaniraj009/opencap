"""Rate-group schedule of one device (PLAN.md section 6). Pure: time is passed in as
monotonic seconds, so the timing rules are unit-testable without sleeping.

Rules
- One slot per rate group (`DevicePlan.groups`; the effective rate is already
  `max(tag.scan_ms or device.scan_ms, device.min_scan_ms)`).
- Start jitter: the first due time of each slot is `now + U(0, jitter * rate)` with
  `jitter = 0.05`. (A negative offset would be in the past, so the ±5 % band of the plan is
  applied one-sided.)
- `due()` = the slot with the earliest due time; ties go to the faster rate.
- `complete()` after a cycle: the next due time is `due + rate` (fixed rate, no drift).
  If that slot has already passed when the cycle ends (long cycle, or a late start), missed
  slots are dropped, never queued: the next cycle is due at `max(ended, started + rate)`,
  i.e. at once after a cycle that took longer than its rate, or one period after a late
  start.
- Overrun = the cycle itself took longer than its rate (`ended - started > rate`). Waiting
  for the channel wire lock counts, because it is part of the cycle.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass

START_JITTER = 0.05
MIN_RATE_S = 0.001


@dataclass
class GroupSlot:
    index: int          # position in DevicePlan.groups
    rate_s: float
    next_due: float
    cycles: int = 0
    overruns: int = 0


def start_offset(rate_s: float, jitter: float, rng: random.Random | None = None) -> float:
    if jitter <= 0:
        return 0.0
    return (rng or random).uniform(0.0, jitter * rate_s)


def next_due_after(due: float, rate_s: float, started: float, ended: float) -> tuple[float, bool]:
    """Return (next due time, overrun) for a cycle that was due at `due`."""
    nxt = due + rate_s
    if nxt <= ended:
        nxt = max(ended, started + rate_s)
    return nxt, (ended - started) > rate_s


class DeviceSchedule:
    def __init__(
        self,
        rates_s: Mapping[int, float],
        now: float,
        *,
        jitter: float = START_JITTER,
        rng: random.Random | None = None,
    ) -> None:
        self._slots: dict[int, GroupSlot] = {}
        for index, rate in rates_s.items():
            rate = max(float(rate), MIN_RATE_S)
            self._slots[index] = GroupSlot(index, rate, now + start_offset(rate, jitter, rng))

    def __len__(self) -> int:
        return len(self._slots)

    @property
    def slots(self) -> list[GroupSlot]:
        return list(self._slots.values())

    def due(self) -> GroupSlot:
        """The slot to run next (earliest due time, faster rate first on a tie)."""
        if not self._slots:
            raise LookupError("schedule is empty")
        return min(self._slots.values(), key=lambda s: (s.next_due, s.rate_s, s.index))

    def delay(self, now: float) -> float:
        return max(0.0, self.due().next_due - now)

    def complete(self, slot: GroupSlot, started: float, ended: float) -> bool:
        """Book a finished cycle of `slot`; return True if it overran."""
        slot.next_due, overrun = next_due_after(slot.next_due, slot.rate_s, started, ended)
        slot.cycles += 1
        if overrun:
            slot.overruns += 1
        return overrun

    def remove(self, index: int) -> None:
        self._slots.pop(index, None)
