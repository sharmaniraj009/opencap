"""`SystemTagTask`: it writes only changed values to virtual devices of the cache, quality good,
`source_ts` = computation time, and the tick class changes once per second (#120, spec 2.1)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opencap.core import system_tags
from opencap.core.runtime import Poller
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
    from opencap.metrics import count_request

    before = value(poller, path).value          # the metrics registry is process-wide
    for _ in range(5):
        count_request(dev.id, "ok")
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




# --- cost guards (#238): a step must not grow with the registry, nor an event step with the node

def test_no_step_collects_the_metrics_registry(tmp_path, monkeypatch):
    """`REGISTRY.get_sample_value` collects the whole registry per call: reading every device that way
    made one step O(devices²) and took 77 % of the CPU at 10k tags. The counters come from
    `opencap.metrics.device_counts`, so collecting the registry here is an error."""
    from opencap.metrics import REGISTRY

    def collect():
        raise AssertionError("a system-tag step collected the metrics registry")

    monkeypatch.setattr(REGISTRY, "collect", collect)
    poller = make_poller(tmp_path, devices=4)
    task = SystemTagTask(poller)
    assert task.step(T0) > 0
    dev = poller.loaded.channels[0].devices[0]
    task.step(T0 + timedelta(milliseconds=200), tick=False, devices={dev.id})
    task.step(T0 + timedelta(seconds=1))


def _count_calls(monkeypatch, name: str) -> list[str]:
    calls: list[str] = []
    real = getattr(system_tags, name)

    def spy(*args, **kwargs):
        calls.append(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(system_tags, name, spy)
    return calls


def test_an_event_step_computes_only_the_devices_that_notified(tmp_path, monkeypatch):
    poller = make_poller(tmp_path, devices=200)                # 100 channels of 2 devices
    task = SystemTagTask(poller)
    task.step(T0)
    entries = _count_calls(monkeypatch, "device_entry")
    devices = _count_calls(monkeypatch, "_device")
    channels = _count_calls(monkeypatch, "_channel")
    dev = poller.loaded.channels[7].devices[1]
    task.step(T0 + timedelta(milliseconds=200), tick=False, devices={dev.id})
    assert (len(entries), len(devices), len(channels)) == (1, 1, 1)
    entries.clear()
    task.step(T0 + timedelta(seconds=1))                       # a tick computes everything, once
    assert len(entries) == 200


def test_the_loop_runs_an_event_step_for_the_notified_devices_only(tmp_path, monkeypatch):
    poller = make_poller(tmp_path, devices=6)
    task = SystemTagTask(poller)
    seen: list[object] = []
    monkeypatch.setattr(task, "step", lambda now=None, *, tick=True, devices=None: seen.append((tick, devices)))
    a, b = poller.loaded.channels[0].devices

    async def scenario():
        task._full = False
        task._tick_second = int(time.time()) + 10              # no tick in this test
        run = asyncio.create_task(task._run())
        try:
            for trigger in ([a.id, b.id, a.id], None):
                seen.clear()
                if trigger is None:
                    task.reconfigure()                         # a config step computes everything
                else:
                    for device_id in trigger:
                        task.notify(device_id)
                while not seen:
                    await asyncio.sleep(0.01)
                yield seen[0]
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    async def collect():
        return [step async for step in scenario()]

    assert asyncio.run(collect()) == [(False, {a.id, b.id}), (False, None)]


def test_event_steps_give_the_values_of_a_full_computation(tmp_path):
    """The device and channel tags an event step writes are the ones `evaluate` gives for the whole
    state: the vectors in tests/conformance/system_tags/ keep describing what the engine shows."""
    from opencap.metrics import count_failed_read, count_overrun, count_request

    poller = make_poller(tmp_path, devices=6)
    task = SystemTagTask(poller)
    task.step(T0)
    when = T0 + timedelta(milliseconds=300)
    moved = [poller.loaded.channels[0].devices[1], poller.loaded.channels[2].devices[0]]
    for dev in moved:
        count_request(dev.id, "ok")
        count_request(dev.id, "timeout")
        count_failed_read(dev.id)
        count_overrun(dev.id)
    assert task.step(when, tick=False, devices={d.id for d in moved}) > 0
    want = system_tags.evaluate(system_tags.snapshot(poller, when, max_in_flight=task._max_in_flight))
    for path, (_, expected) in want.items():
        if split_path(path)[1] in TICK_TAGS or not path.startswith(tuple(f"{c.name}." for c in poller.loaded.channels)):
            continue
        assert value(poller, path).value == expected, path


def test_the_plain_totals_match_the_registry():
    from opencap.metrics import (
        REGISTRY,
        count_failed_read,
        count_overrun,
        count_published,
        count_request,
        count_saf_dropped,
        device_counts,
        total,
    )

    dev = "Totals/Check"
    for result in ("ok", "ok", "timeout", "conn_error", "bad_config", "error"):
        count_request(dev, result)
    count_failed_read(dev)
    count_overrun(dev)
    count_published(3)
    count_saf_dropped(2)
    counts = device_counts(dev)
    for result in ("ok", "timeout", "conn_error", "bad_config", "error"):
        assert counts[result] == REGISTRY.get_sample_value("poll_requests_total", {"device": dev, "result": result})
    assert counts["failed_reads"] == REGISTRY.get_sample_value("poll_failed_reads_total", {"device": dev})
    assert counts["overruns"] == REGISTRY.get_sample_value("poll_overrun_total", {"device": dev})
    assert total("mqtt_published") == REGISTRY.get_sample_value("mqtt_published_total")
    assert total("saf_dropped") == REGISTRY.get_sample_value("saf_dropped_total")


def test_the_counted_metrics_are_only_incremented_in_opencap_metrics():
    """`.inc()` straight on these metrics would bypass the totals the system tags read."""
    import re

    src = Path(__file__).parents[2] / "src" / "opencap"
    pattern = re.compile(r"\b(POLL_REQUESTS|POLL_FAILED_READS|POLL_OVERRUN|MQTT_PUBLISHED|SAF_DROPPED)\b.*\.inc\(")
    hits = [f"{p.relative_to(src)}:{n}" for p in src.rglob("*.py") if p.name != "metrics.py"
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert not hits, hits


def test_tick_steps_on_the_compare_first_path_give_the_values_of_a_full_computation(tmp_path, monkeypatch):
    """After the first full step every path is registered, so a tick only compares and writes what
    changed (`_write_changed`). Its result is still every value `evaluate` gives, tick class included."""
    from opencap.metrics import count_request

    poller = make_poller(tmp_path, devices=6)
    task = SystemTagTask(poller)
    task.step(T0)
    fast = _count_calls(monkeypatch, "split_path")
    dev = poller.loaded.channels[1].devices[0]
    for i in range(1, 4):
        count_request(dev.id, "ok")
        when = T0 + timedelta(seconds=i)
        fast.clear()
        written = task.step(when)
        want = system_tags.evaluate(system_tags.snapshot(poller, when, max_in_flight=task._max_in_flight))
        assert len(fast) == written < len(want)           # only the changed paths were split
        for path, (_, expected) in want.items():
            assert value(poller, path).value == expected, path
