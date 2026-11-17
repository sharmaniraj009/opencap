"""Rate-group schedule timing (pure: monotonic seconds passed in)."""

from __future__ import annotations

import random

import pytest

from opencap.core.scheduler import DeviceSchedule, next_due_after, start_offset


def test_start_jitter_is_within_5_percent_of_the_rate():
    rng = random.Random(1)
    offsets = [start_offset(1.0, 0.05, rng) for _ in range(2000)]
    assert min(offsets) >= 0.0 and max(offsets) <= 0.05
    assert max(offsets) - min(offsets) > 0.04      # actually spread, not constant
    assert start_offset(1.0, 0.0) == 0.0


def test_initial_due_times_are_jittered_per_group():
    s = DeviceSchedule({0: 1.0, 1: 10.0}, now=100.0, rng=random.Random(7))
    slots = {sl.index: sl for sl in s.slots}
    assert 100.0 <= slots[0].next_due <= 100.05
    assert 100.0 <= slots[1].next_due <= 100.5


def test_no_jitter_means_everything_due_now_and_fastest_first():
    s = DeviceSchedule({0: 0.1, 1: 1.0, 2: 10.0}, now=5.0, jitter=0.0)
    assert [sl.next_due for sl in s.slots] == [5.0, 5.0, 5.0]
    assert s.due().index == 0                      # tie -> faster rate
    assert s.delay(5.0) == 0.0


def test_fixed_rate_without_drift():
    s = DeviceSchedule({0: 1.0}, now=0.0, jitter=0.0)
    slot = s.due()
    for k in range(5):
        start = slot.next_due + 0.003              # small wake-up latency
        assert s.complete(slot, start, start + 0.2) is False
        assert slot.next_due == pytest.approx(k + 1.0)


def test_earliest_due_group_runs_next():
    s = DeviceSchedule({0: 0.1, 1: 0.25}, now=0.0, jitter=0.0)
    order = []
    while s.due().next_due < 1.0 - 1e-9:         # window [0, 1)
        slot = s.due()
        now = slot.next_due
        order.append(slot.index)
        s.complete(slot, now, now)                 # instant cycles
    assert order.count(0) == 10 and order.count(1) == 4


def test_overrun_starts_next_cycle_at_once_and_drops_missed_slots():
    nxt, overrun = next_due_after(due=0.0, rate_s=0.1, started=0.0, ended=0.35)
    assert overrun is True
    assert nxt == pytest.approx(0.35)              # at once, not 0.1/0.2/0.3 queued


def test_late_start_short_cycle_is_not_an_overrun_and_not_doubled():
    # due at 0, started 0.25 late (device busy with another group), cycle 10 ms
    nxt, overrun = next_due_after(due=0.0, rate_s=0.1, started=0.25, ended=0.26)
    assert overrun is False
    assert nxt == pytest.approx(0.35)              # one period after the late start


def test_cycle_exactly_at_rate_is_not_an_overrun():
    nxt, overrun = next_due_after(due=0.0, rate_s=0.1, started=0.0, ended=0.1)
    assert overrun is False and nxt == pytest.approx(0.1)


def test_complete_counts_cycles_and_overruns():
    s = DeviceSchedule({0: 0.1}, now=0.0, jitter=0.0)
    slot = s.due()
    assert s.complete(slot, 0.0, 0.3) is True
    assert slot.next_due == pytest.approx(0.3)
    assert s.complete(slot, 0.3, 0.35) is False
    assert slot.next_due == pytest.approx(0.4)
    assert (slot.cycles, slot.overruns) == (2, 1)


def test_long_pause_then_resume_runs_once_not_a_burst():
    # e.g. after demotion: the group was due long ago
    s = DeviceSchedule({0: 0.1}, now=0.0, jitter=0.0)
    slot = s.due()
    assert s.complete(slot, 30.0, 30.01) is False
    assert slot.next_due == pytest.approx(30.1)


def test_remove_and_empty_schedule():
    s = DeviceSchedule({0: 0.1, 3: 1.0}, now=0.0, jitter=0.0)
    s.remove(0)
    assert len(s) == 1 and s.due().index == 3
    s.remove(3)
    assert len(s) == 0
    with pytest.raises(LookupError):
        s.due()


def test_zero_rate_is_floored():
    s = DeviceSchedule({0: 0.0}, now=0.0, jitter=0.0)
    assert s.due().rate_s > 0
