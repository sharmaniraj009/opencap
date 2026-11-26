"""`SystemTagTask`: it writes only changed values to virtual devices of the cache, quality good,
`source_ts` = computation time, and the tick class changes once per second (#120, spec 2.1)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opencap.core.runtime import Poller
from opencap.core import system_tags
from opencap.core.system_tags import TICK_TAGS, SystemTagTask, split_path
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices

T0 = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)
CATALOGUE = json.loads((Path(__file__).parents[1] / "conformance" / "system_tags_catalogue.json").read_text())


def make_poller(tmp_path, devices: int = 2) -> Poller:
    devs = make_sim_devices(devices, 3, seed=2, per_channel=2)
    poller = Poller(write_config(tmp_path, devs), serve_api=False)
    poller.started_at = T0
    return poller


def value(poller: Poller, path: str):
    device_id, name = split_path(path)
    tv = poller.cache.get(device_id, name)
    assert tv is not None, path
    return tv


def test_tick_names_are_the_catalogues_tick_class():
    by_name: dict[str, set[str]] = {}
    for t in CATALOGUE["tags"]:
        by_name.setdefault(t["name"], set()).add(t["update"])
    assert all(len(classes) == 1 for classes in by_name.values())       # a name has one class everywhere
    assert TICK_TAGS == {n for n, classes in by_name.items() if classes == {"tick"}}


def test_first_step_writes_every_tag_good_with_the_computation_time(tmp_path):
    poller = make_poller(tmp_path)
    task = SystemTagTask(poller)
    written = task.step(T0)
    assert written > 100
    tv = value(poller, "_System._ProductName")
    assert (tv.value, tv.quality, tv.source_ts) == ("opencap", "good", T0)
    assert value(poller, "_System._DateTime").value == "2026-11-18T10:00:00.000Z"
    # device-level virtual device: the path without its last segment
    dev = poller.loaded.channels[0].devices[0]
    assert value(poller, f"{dev.channel}.{dev.name}._System._State").value == "offline"
    assert value(poller, f"{dev.channel}._System._DeviceCount").value == 2
    assert all(tv.quality == "good" for did in poller.cache.devices() for tv in poller.cache.snapshot(did).values())


def test_an_unchanged_state_writes_nothing_and_a_tick_changes_the_clock_once_per_second(tmp_path):
    poller = make_poller(tmp_path)
    task = SystemTagTask(poller)
    task.step(T0)
    versions = {d: poller.cache.version(d) for d in poller.cache.devices()}
    assert task.step(T0) == 0                                     # same instant, same state
    assert {d: poller.cache.version(d) for d in poller.cache.devices()} == versions

    changed_paths = []
    for second in (1, 2, 3):
        now = T0 + timedelta(seconds=second)
        before = {p: system_tags_value(poller, p) for p in ("_System._DateTime", "_System._Time_Second",
                                                           "_System._UptimeS")}
        task.step(now)
        after = {p: system_tags_value(poller, p) for p in before}
        changed_paths.append([p for p in before if before[p] != after[p]])
        assert value(poller, "_System._UptimeS").value == second
        assert value(poller, "_System._Time_Second").value == (T0 + timedelta(seconds=second)).astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Kolkata")).second
        assert value(poller, "_System._DateTime").source_ts == now          # last change = this tick
    assert all("_System._DateTime" in c and "_System._UptimeS" in c for c in changed_paths)
    # a tag that did not change keeps the time of its last change
    assert value(poller, "_System._ProductName").source_ts == T0


def system_tags_value(poller: Poller, path: str):
    tv = value(poller, path)
    return (tv.value, tv.source_ts)


def test_an_event_step_leaves_the_tick_class_alone_and_a_tick_step_catches_up(tmp_path):
    poller = make_poller(tmp_path)
    task = SystemTagTask(poller)
    task.step(T0)
    task.step(T0 + timedelta(milliseconds=400), tick=False)
    assert value(poller, "_System._DateTime").value == "2026-11-18T10:00:00.000Z"      # not rewritten
    assert value(poller, "_System._UptimeS").value == 0
    task.step(T0 + timedelta(seconds=1), tick=True)
    assert value(poller, "_System._UptimeS").value == 1


def test_event_class_tags_follow_a_status_change_in_an_event_step(tmp_path):
    poller = make_poller(tmp_path)
    task = SystemTagTask(poller)
    task.step(T0)
    dev = poller.loaded.channels[0].devices[0]
    path = f"{dev.channel}.{dev.name}._Statistics._SuccessfulReads"
    from opencap.metrics import POLL_REQUESTS

    before = value(poller, path).value          # the metrics registry is process-wide
    POLL_REQUESTS.labels(dev.id, "ok").inc(5)
    task.step(T0 + timedelta(milliseconds=300), tick=False)
    assert value(poller, path).value == before + 5 and value(poller, path).source_ts == T0 + timedelta(milliseconds=300)


def test_reconfigure_adds_and_removes_virtual_devices(tmp_path):
    poller = make_poller(tmp_path, devices=2)
    task = SystemTagTask(poller)
    task.step(T0)
    assert "Site 001._System" in poller.cache.devices() or any(d.endswith("._System") for d in poller.cache.devices())
    channel = poller.loaded.channels[0]
    poller.loaded.channels[0] = channel.model_copy(update={"devices": channel.devices[:1]})
    task.reconfigure()
    task.step(T0 + timedelta(seconds=1))
    gone = f"{channel.name}.{channel.devices[1].name}._System"
    assert gone not in poller.cache.devices()
    assert value(poller, f"{channel.name}._System._DeviceCount").value == 1


def test_the_tick_is_aligned_to_the_utc_second():
    for frac in (0.0, 0.2, 0.5, 0.999):
        delay = SystemTagTask.seconds_to_next_tick(1000.0 + frac)
        assert (1000.0 + frac + delay) % 1.0 == pytest.approx(0.0, abs=0.001) or delay == pytest.approx(1.0)
        assert 0.0 < delay <= 1.0


@pytest.mark.parametrize("readings, sleeps", [
    ([19.978, 20.001], 1),          # fired 22 ms early (seen on Windows): wait out the rest, once
    ([19.990, 19.998, 20.0], 2),    # still early after the first wait (coarse clock): wait again
    ([20.004], 0),                  # on time or late: tick now
    ([16.0], 0),                    # clock stepped back 4 s (NTP): not waited out
])
async def test_an_early_timer_wake_is_waited_out(monkeypatch, readings, sleeps):
    clock = iter(readings)
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(system_tags.asyncio, "sleep", sleep)
    await system_tags._wait_out_early_wake(20.0, clock=lambda: next(clock))
    assert len(slept) == sleeps
    assert all(0.0 < d < system_tags.EARLY_WAKE_MAX_S + 0.1 for d in slept)


async def test_the_task_ticks_once_per_second_on_the_real_clock(tmp_path, no_gc):
    poller = make_poller(tmp_path)
    task = SystemTagTask(poller)
    await task.start()
    try:
        first = value(poller, "_System._DateTime").value       # the step of start(): any time of the second
        seen: list[str] = []
        deadline = time.monotonic() + 6
        while len(seen) < 3 and time.monotonic() < deadline:
            current = value(poller, "_System._DateTime").value
            if current != first and (not seen or seen[-1] != current):
                seen.append(current)
            await asyncio.sleep(0.02)
        assert len(seen) == 3, seen
        stamps = [datetime.fromisoformat(s.replace("Z", "+00:00")) for s in seen]
        # aligned to the second (50 ms + CI slack), and never before it: asyncio may fire the timer
        # one clock resolution early (~15.6 ms on Windows with Python 3.12), which the task waits out
        assert all(s.microsecond < 150_000 for s in stamps), seen
        assert [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)] == pytest.approx(
            [1.0, 1.0], abs=0.15)
    finally:
        await task.stop()
    assert not any(d.endswith("_System") or "._System" in d for d in poller.cache.devices())


