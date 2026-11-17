"""ChannelWorker with the scriptable fake driver (PLAN §3.1, §6; contract §2).

Tests run in real time with small scan rates (tens of ms) and use unique channel names so
the process-wide Prometheus metrics do not leak between tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from structlog.testing import capture_logs

from opencap.config.models import Scale, Tag
from opencap.core.cache import TagValue, ValueCache
from opencap.core.channel_worker import ChannelWorker, safety_timeout_s
from opencap.core.registry import DevicePlan
from opencap.drivers.base import CommError, ConnectionLost
from opencap.metrics import REGISTRY
from tests.helpers.fake_driver import (
    ConcurrencyProbe,
    FakeDriver,
    FakeFactory,
    channel,
    device,
    fake_plans,
    tags,
    uniq,
)


# --- helpers ------------------------------------------------------------------------------

def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def requests(device_id: str, result: str) -> float:
    return metric("poll_requests_total", device=device_id, result=result)


async def wait_until(pred, timeout: float = 2.0, step: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


@contextlib.asynccontextmanager
async def running(*workers: ChannelWorker):
    tasks = [asyncio.create_task(w.run()) for w in workers]
    try:
        yield tasks
    finally:
        for w in workers:
            await w.stop()


def one_device(tag_list: list[Tag] | None = None, **dev_kw):
    name = uniq()
    d = device(name, "PLC", tag_list or tags("A", 2), **dev_kw)
    return channel(name, [d]), d


def make_worker(ch, factory: FakeFactory | None = None, cache: ValueCache | None = None, **kw):
    factory = factory or FakeFactory()
    cache = cache or ValueCache()
    kw.setdefault("start_jitter", 0.0)
    worker = ChannelWorker(ch, fake_plans(ch), cache, driver_factory=factory, **kw)
    return worker, factory, cache


def qualities(values: dict[str, TagValue]) -> dict[str, str]:
    return {name: tv.quality for name, tv in values.items()}


# --- concurrency (PLAN §3.1) --------------------------------------------------------------

async def test_one_request_on_the_wire_per_channel_and_channels_in_parallel():
    everywhere = ConcurrencyProbe()
    per_channel = [ConcurrencyProbe(), ConcurrencyProbe()]
    cache = ValueCache()
    workers = []
    for probe in per_channel:
        name = uniq()
        devs = [device(name, f"D{i}", tags("A", 2) + tags("B", 2), scan_ms=30) for i in range(3)]
        factory = FakeFactory(probes=[probe, everywhere])
        for d in devs:
            factory.script(d.id).delay_s = 0.01
        workers.append(make_worker(channel(name, devs), factory, cache)[0])
    async with running(*workers):
        await asyncio.sleep(0.5)
    for probe in per_channel:
        assert probe.max_in_flight == 1          # strictly sequential inside a channel
        assert probe.total > 20                  # and it really polled all three devices
    assert everywhere.max_in_flight == 2         # the two channels overlapped on the wire


async def test_dead_device_in_channel_a_does_not_delay_channel_b():
    ch_a, dead = one_device(timeout_ms=50, retries=2)
    ch_b, alive = one_device(scan_ms=50)
    wa, fa, _ = make_worker(ch_a)
    wb, fb, _ = make_worker(ch_b)
    fa.script(dead.id).hang = True               # never answers: only the safety net ends it
    fb.script(alive.id).delay_s = 0.002
    async with running(wa, wb):
        await asyncio.sleep(0.6)
    starts = [t for _, t in fb.script(alive.id).reads]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) >= 10
    assert max(gaps) < 0.09                      # B kept its 50 ms cadence
    assert requests(dead.id, "timeout") >= 2     # while A was stuck in timeouts


# --- retries, timeouts, reconnects --------------------------------------------------------

@pytest.mark.parametrize("retries", [0, 2])
async def test_retries_count(retries):
    ch, d = one_device(retries=retries)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.fail_next(CommError("transient"), times=retries)    # fails `retries` times, then answers
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"good"}
    assert s.reads_of("A") == retries + 1
    assert requests(d.id, "timeout") == retries and requests(d.id, "ok") == 1

    s.timeout_blocks.add("A")                    # now it never answers
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"bad_comm"}
    assert s.reads_of("A") == (retries + 1) * 2  # exactly 1 + retries attempts per block
    await worker.stop()


async def test_timeout_marks_bad_comm_and_keeps_last_good_value():
    ch, d = one_device()
    worker, factory, cache = make_worker(ch)
    s = factory.script(d.id)
    first = await worker.read_once(d.id)
    assert first["A_0"].quality == "good" and first["A_0"].value == 1
    ts = first["A_0"].source_ts
    s.timeout_blocks.add("A")
    after = await worker.read_once(d.id)
    assert after["A_0"] == TagValue(1, "bad_comm", ts)
    assert cache.get(d.id, "A_1") == TagValue(1, "bad_comm", ts)
    status = worker.device_status(d.id)
    assert "timeout" in status["last_error"]
    assert status["state"] == "offline" and status["errors"] == 1
    await worker.stop()


async def test_close_on_timeout_reopens_the_socket_for_each_retry():
    ch, d = one_device(retries=2)
    worker, factory, _ = make_worker(ch, backoff_base_s=10.0)
    s = factory.script(d.id)
    s.close_on_timeout = True                    # like the Modbus driver: a timeout closes the socket
    await worker.read_once(d.id)
    connects, reads = len(s.connect_attempts), s.reads_of("A")
    s.timeout_blocks.add("A")
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"bad_comm"}
    assert s.reads_of("A") - reads == 3          # three real requests, none wasted on a dead socket
    assert len(s.connect_attempts) - connects == 2   # reopened before each retry
    assert requests(d.id, "conn_error") == 0
    # a successful reopen starts no backoff: the next poll reconnects at once
    s.timeout_blocks.clear()
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"good"}
    await worker.stop()


async def test_close_on_timeout_retry_succeeds_after_reopen():
    ch, d = one_device(retries=1)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.close_on_timeout = True
    await worker.read_once(d.id)
    s.fail_next(CommError("timeout"))
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"good"}
    assert len(s.connect_attempts) == 2 and s.reads_of("A") == 3
    await worker.stop()


async def test_failed_reopen_starts_backoff_and_sends_nothing_while_backing_off():
    ch, d = one_device(retries=2, scan_ms=20, demote_after=0)
    worker, factory, _ = make_worker(ch, backoff_base_s=10.0)
    s = factory.script(d.id)
    await worker.read_once(d.id)
    s.close_on_timeout = True
    s.timeout_blocks.add("A")
    s.fail_next_connect(ConnectionLost("refused"))
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"bad_comm"}
    assert s.reads_of("A") == 2                  # 1 good + 1 timed out; the reopen failed
    assert len(s.connect_attempts) == 2
    assert requests(d.id, "conn_error") == 1
    reads, connects = len(s.reads), len(s.connect_attempts)
    async with running(worker):
        await asyncio.sleep(0.2)                 # ~10 cycles, all inside the 10 s backoff
    assert len(s.reads) == reads and len(s.connect_attempts) == connects
    assert worker.health(d.id).errors >= 5       # cycles during backoff count as failed


async def test_connection_lost_on_idle_socket_reconnects_without_using_a_retry():
    ch, d = one_device(retries=0)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    await worker.read_once(d.id)
    s.fail_next(ConnectionLost("peer closed"))
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"good"}
    assert len(s.connect_attempts) == 2 and s.reads_of("A") == 3
    assert requests(d.id, "conn_error") == 1

    await worker.stop()


async def test_connection_lost_right_after_connect_ends_the_cycle():
    ch, d = one_device(tags("A", 1) + tags("B", 1), retries=3)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.lost_blocks.add("A")                       # the device drops every connection at once
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"bad_comm"}   # B not tried either
    assert len(s.connect_attempts) == 1 and s.reads == [("A", s.reads[0][1])]
    await worker.stop()


async def test_socket_closed_by_previous_timeout_is_reopened_before_the_next_poll():
    ch, d = one_device(retries=0)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.close_on_timeout = True
    await worker.read_once(d.id)
    s.fail_next(CommError("timeout"))            # times out, the driver closes its socket
    await worker.read_once(d.id)
    assert not worker.device_status(d.id)["connected"]
    values = await worker.read_once(d.id)
    assert set(qualities(values).values()) == {"good"}
    assert requests(d.id, "conn_error") == 0     # reopened up front, no failed request
    assert len(s.connect_attempts) == 2
    await worker.stop()


async def test_safety_net_timeout_is_a_comm_error_and_drops_the_connection():
    ch, d = one_device(timeout_ms=40, retries=1)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.hang = True                                # the driver ignores its own timeout
    t0 = time.monotonic()
    values = await worker.read_once(d.id)
    elapsed = time.monotonic() - t0
    assert set(qualities(values).values()) == {"bad_comm"}
    assert elapsed == pytest.approx(2 * safety_timeout_s(40), abs=0.1)
    assert requests(d.id, "timeout") == 2
    assert s.closes >= 2 and len(s.connect_attempts) == 2   # closed, reopened for the retry
    await worker.stop()


async def test_reconnect_backoff_doubles_and_caps():
    ch, d = one_device(scan_ms=5, demote_after=0)
    worker, factory, _ = make_worker(ch, backoff_base_s=0.02, backoff_max_s=0.08)
    s = factory.script(d.id)
    s.down = True
    async with running(worker):
        await asyncio.sleep(0.55)
    times = s.connect_attempts
    gaps = [b - a for a, b in zip(times, times[1:])]
    expected = [0.02, 0.04, 0.08, 0.08, 0.08]
    assert len(gaps) >= len(expected)
    for gap, exp in zip(gaps, expected):
        assert exp - 0.002 <= gap <= exp + 0.04, (gaps, expected)
    assert s.reads == []                         # never a request without a connection
    assert worker.health(d.id).errors > len(times)   # failed cycles while backing off


# --- demotion -----------------------------------------------------------------------------

async def test_demotion_after_failed_cycles_probe_and_resume():
    ch, d = one_device(scan_ms=20, demote_after=2, demote_for_ms=250)
    with capture_logs() as logs:
        worker, factory, cache = make_worker(ch, backoff_base_s=0.01, backoff_max_s=0.01)
        s = factory.script(d.id)
        s.down = True
        health = worker.health(d.id)
        async with running(worker):
            await wait_until(lambda: health.state == "demoted", 1.0)
            demoted_at = time.monotonic()
            connects = len(s.connect_attempts)
            assert connects == 2                 # one per failed cycle, then demoted
            await asyncio.sleep(0.15)
            assert len(s.connect_attempts) == connects      # skipped while demoted
            await wait_until(lambda: len(s.connect_attempts) == connects + 1, 0.5)   # the probe
            assert time.monotonic() - demoted_at >= 0.24
            await asyncio.sleep(0.03)
            assert health.state == "demoted"     # probe failed: demoted again
            assert len(s.connect_attempts) == connects + 1
            s.down = False
            await wait_until(lambda: health.state == "ok", 1.0)  # next probe passes
            assert time.monotonic() - demoted_at >= 0.48
            await wait_until(lambda: cache.get(d.id, "A_0").quality == "good", 0.5)
    events = [e["event"] for e in logs]
    assert events.count("device.demoted") == 1   # one INFO line per state change
    assert "device.resumed" in events and "device.connected" in events
    assert health.errors == 3 and health.demotions == 1
    assert metric("device_state", device=d.id) == 0


async def test_probe_stops_at_first_failed_block():
    ch, d = one_device(tags("A", 1) + tags("B", 1) + tags("C", 1), scan_ms=20,
                       demote_after=1, demote_for_ms=60)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.timeout_blocks.update({"A", "B", "C"})
    async with running(worker):
        await wait_until(lambda: worker.health(d.id).state == "demoted", 1.0)
        assert [k for k, _ in s.reads] == ["A", "B", "C"]   # normal cycle reads every block
        await wait_until(lambda: len(s.reads) >= 4, 1.0)
        await asyncio.sleep(0.03)
    assert [k for k, _ in s.reads][3:] and all(k == "A" for k, _ in s.reads[3:])


# --- scheduling ---------------------------------------------------------------------------

async def test_overrun_is_counted_and_cycles_are_not_queued():
    ch, d = one_device(scan_ms=50)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.delay_s = 0.12                             # every cycle takes 120 ms > 50 ms rate
    async with running(worker):
        await asyncio.sleep(0.65)
        slow_reads = len(s.reads)
        overruns = metric("poll_overrun_total", device=d.id)
        assert 4 <= slow_reads <= 6              # back-to-back, not 13 queued cycles
        assert overruns >= slow_reads - 1
        s.delay_s = 0.0
        mark = len(s.reads)
        await asyncio.sleep(0.3)
        fast = [t for _, t in s.reads[mark:]]
    gaps = [b - a for a, b in zip(fast, fast[1:])]
    assert len(fast) <= 0.3 / 0.05 + 2
    assert sum(1 for g in gaps if g < 0.03) == 0     # no catch-up burst of missed cycles
    assert metric("poll_overrun_total", device=d.id) <= overruns + 1


async def test_rate_groups_poll_at_their_own_rates():
    tag_list = tags("F", 2, scan_ms=50) + tags("S", 2, scan_ms=200)
    ch, d = one_device(tag_list, scan_ms=1000)
    worker, factory, _ = make_worker(ch)
    plans = fake_plans(ch)
    assert [g.rate_ms for g in plans[d.id].groups] == [50, 200]
    s = factory.script(d.id)
    async with running(worker):
        await asyncio.sleep(1.0)
    assert 18 <= s.reads_of("F") <= 21
    assert 4 <= s.reads_of("S") <= 6
    cycles = metric("poll_cycle_seconds_count", device=d.id)
    assert abs(cycles - (s.reads_of("F") + s.reads_of("S"))) <= 1   # one may be cut by stop()


async def test_stale_after_three_times_the_rate():
    ch, d = one_device(scan_ms=50)
    worker, factory, cache = make_worker(ch)
    s = factory.script(d.id)
    async with running(worker):
        await wait_until(lambda: cache.get(d.id, "A_0").quality == "good")
        s.delay_s = 0.6                          # the next read hangs for 600 ms
        await wait_until(lambda: cache.get(d.id, "A_0").quality == "stale", 0.5)
        tv = cache.get(d.id, "A_0")
        age = (datetime.now(UTC) - tv.source_ts).total_seconds()
    assert tv.value is not None                  # last good value kept
    assert 0.15 <= age <= 0.15 + 0.1             # 3 x 50 ms, plus one housekeeping tick


class _ReadTimeFake(FakeDriver):
    """A subscription-style driver: source_ts is the device's change time, not the reply time."""

    stale_by_read_time: ClassVar[bool] = True


def _read_time_factory(factory: FakeFactory):
    def make(dev, executor):
        drv = _ReadTimeFake(dev, executor, script=factory.script(dev.id))
        factory.drivers[dev.id] = drv
        return drv
    return make


async def test_old_source_ts_goes_stale_for_a_polled_driver():
    ch, d = one_device(scan_ms=50)
    worker, factory, cache = make_worker(ch)
    factory.script(d.id).clock = lambda: datetime.now(UTC) - timedelta(hours=1)
    async with running(worker):
        await wait_until(lambda: cache.get(d.id, "A_0").value is not None)
        await wait_until(lambda: cache.get(d.id, "A_0").quality == "stale", 0.5)


async def test_read_time_staleness_for_subscription_drivers():
    """D-028: values whose SourceTimestamp is an hour old stay good while reads succeed, and
    go stale 3 x the rate after the reads stop; source_ts is passed through unchanged."""
    ch, d = one_device(scan_ms=50)
    factory = FakeFactory()
    worker, _, cache = make_worker(ch, factory=_read_time_factory(factory))
    s = factory.script(d.id)
    old = datetime.now(UTC) - timedelta(hours=1)
    s.clock = lambda: old
    async with running(worker):
        await wait_until(lambda: cache.get(d.id, "A_0").quality == "good")
        seen = set()
        deadline = time.monotonic() + 0.4                     # > 3 x 50 ms of good reads
        while time.monotonic() < deadline:                    # never flaps to stale in between
            seen.add(cache.get(d.id, "A_0").quality)
            await asyncio.sleep(0.003)
        assert seen == {"good"}
        assert cache.get(d.id, "A_0").source_ts == old
        s.delay_s = 0.6                                       # reads stall
        t0 = time.monotonic()
        await wait_until(lambda: cache.get(d.id, "A_0").quality == "stale", 0.5)
        assert time.monotonic() - t0 <= 0.15 + 0.15
    assert cache.get(d.id, "A_0").source_ts == old


# --- block and tag faults -----------------------------------------------------------------

async def test_block_config_error_block_is_never_read_again():
    ch, d = one_device(tags("A", 2) + tags("B", 2), scan_ms=20)
    worker, factory, cache = make_worker(ch)
    s = factory.script(d.id)
    s.config_error_blocks.add("A")
    async with running(worker):
        await asyncio.sleep(0.2)
    assert s.reads_of("A") == 1
    assert s.reads_of("B") >= 5
    snap = cache.snapshot(d.id)
    assert qualities(snap) == {"A_0": "bad_config", "A_1": "bad_config", "B_0": "good", "B_1": "good"}
    assert requests(d.id, "bad_config") == 1
    assert worker.health(d.id).state == "ok"


async def test_all_blocks_rejected_is_neutral_and_device_goes_idle():
    ch, d = one_device(scan_ms=20)
    worker, factory, cache = make_worker(ch)
    s = factory.script(d.id)
    s.config_error_blocks.add("A")
    async with running(worker):
        await asyncio.sleep(0.15)
    assert s.reads_of("A") == 1
    assert worker.health(d.id).errors == 0       # neutral cycle: no health change
    assert set(qualities(cache.snapshot(d.id)).values()) == {"bad_config"}


async def test_plan_invalid_tags_are_bad_config_from_the_start():
    tag_list = tags("A", 2) + [Tag(name="BAD", address="!nope", dtype="float32")]
    ch, d = one_device(tag_list, scan_ms=20)
    worker, factory, cache = make_worker(ch)
    assert cache.get(d.id, "BAD") == TagValue(None, "bad_config", None)   # before run()
    async with running(worker):
        await asyncio.sleep(0.12)
    assert cache.get(d.id, "BAD") == TagValue(None, "bad_config", None)
    assert cache.get(d.id, "A_0").quality == "good"
    assert {k for k, _ in factory.script(d.id).reads} == {"A"}


async def test_scaling_applied_and_scaling_error_is_bad_decode():
    lin = Scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10)
    tag_list = [
        Tag(name="LIN", address="A.0", dtype="uint16", scale=lin),
        Tag(name="BROKEN", address="A.1", dtype="uint16", scale=Scale(raw_lo=5, raw_hi=5)),
        Tag(name="FLAG", address="A.2", dtype="bool", scale=lin),
        Tag(name="TXT", address="A.3", dtype="string", scale=lin),
        Tag(name="ARR", address="A.4", dtype="uint16", scale=lin),
        Tag(name="RAW", address="A.5", dtype="uint16"),
        Tag(name="DEC", address="A.6", dtype="uint16", scale=lin),
    ]
    ch, d = one_device(tag_list)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.values.update({"LIN": 50, "BROKEN": 50, "FLAG": True, "TXT": "abc", "ARR": [0, 100], "RAW": 50, "DEC": 1})
    s.decode_error_tags.add("DEC")
    v = await worker.read_once(d.id)
    assert v["LIN"].quality == "good" and v["LIN"].value == pytest.approx(5.0)
    assert v["BROKEN"] == TagValue(None, "bad_decode", None)   # only this tag
    assert v["FLAG"].value is True and v["TXT"].value == "abc"
    assert v["ARR"].value == pytest.approx([0.0, 10.0])
    assert v["RAW"].value == 50
    assert v["DEC"].quality == "bad_decode"
    assert worker.health(d.id).state == "ok"
    s.values["LIN"] = 100
    s.values["BROKEN"] = 70
    v = await worker.read_once(d.id)
    assert v["LIN"].value == pytest.approx(10.0)
    assert v["BROKEN"].quality == "bad_decode"
    await worker.stop()


# --- read_once, status, lifecycle ---------------------------------------------------------

async def test_read_once_while_run_is_active_is_serialized():
    ch, d = one_device(tags("A", 2) + tags("B", 2), scan_ms=30)
    probe = ConcurrencyProbe()
    cycles: list[str] = []
    worker, factory, _ = make_worker(ch, FakeFactory(probes=[probe]), on_cycle=cycles.append)
    s = factory.script(d.id)
    s.delay_s = 0.01
    async with running(worker):
        await wait_until(lambda: len(s.reads) >= 4)
        before = len(cycles)
        results = await asyncio.gather(*(worker.read_once(d.id) for _ in range(3)))
        assert len(cycles) >= before + 3          # on_cycle after each read_once too
        await asyncio.sleep(0.05)
    for values in results:
        assert set(values) == {"A_0", "A_1", "B_0", "B_1"}
        assert set(qualities(values).values()) == {"good"}
    keys = [k for k, _ in s.reads]
    # every cycle reads A then B; a read_once interleaved with a run() cycle would give A, A
    assert keys[0] == "A" and all(a != b for a, b in zip(keys, keys[1:]))
    assert probe.max_in_flight == 1
    assert set(cycles) == {d.id}


async def test_read_once_without_run_and_on_disabled_device():
    name = uniq()
    off = device(name, "OFF", tags("A", 1), enabled=False)
    ch = channel(name, [off])
    worker, factory, cache = make_worker(ch)
    assert off.id not in cache                   # disabled devices are not registered/polled
    values = await worker.read_once(off.id)      # commissioning: still readable on demand
    assert values["A_0"].quality == "good"
    with pytest.raises(KeyError):
        await worker.read_once("nope/nope")
    await worker.stop()


async def test_not_polled_plan_error_disabled_device_and_disabled_channel():
    name = uniq()
    ok = device(name, "OK", tags("A", 1))
    off = device(name, "OFF", tags("A", 1), enabled=False)
    ab = device(name, "AB", tags("A", 1))
    ch = channel(name, [ok, off, ab])
    plans = fake_plans(ch)
    plans[ab.id] = DevicePlan(device=ab, error="driver 'ab_logix' is not implemented yet")
    factory = FakeFactory()
    worker = ChannelWorker(ch, plans, ValueCache(), driver_factory=factory, start_jitter=0.0)
    async with running(worker):
        await asyncio.sleep(0.12)
        assert set(factory.drivers) == {ok.id}
        assert worker.device_status(ab.id) == {
            "state": "offline", "last_good": None, "errors": 0, "cycle_ms": None,
            "last_error": "driver 'ab_logix' is not implemented yet", "connected": False,
        }
        assert worker.device_status(off.id)["last_error"] == "device disabled"
        st = worker.device_status(ok.id)
        assert st["state"] == "ok" and st["connected"] is True and st["last_error"] is None
        assert st["cycle_ms"] is not None and st["last_good"].endswith("Z")
        with pytest.raises(RuntimeError, match="not implemented"):
            await worker.read_once(ab.id)

    off_name = uniq()
    off_ch = channel(off_name, [device(off_name, "D", tags("A", 1))], enabled=False)
    w2, f2, _ = make_worker(off_ch)
    async with running(w2):
        await asyncio.sleep(0.05)
    assert f2.drivers == {}
    assert w2.device_status(f"{off_name}/D")["last_error"] == "channel disabled"


async def test_driver_without_io_stops_polling_the_device():
    ch, d = one_device(scan_ms=20)
    worker, factory, _ = make_worker(ch)
    s = factory.script(d.id)
    s.unsupported = True                         # connect() raises NotImplementedError
    async with running(worker):
        await asyncio.sleep(0.12)
        assert len(s.connect_attempts) == 1      # not retried every cycle
        st = worker.device_status(d.id)
        assert st["state"] == "offline" and "NotImplementedError" in st["last_error"]
        with pytest.raises(RuntimeError):
            await worker.read_once(d.id)


async def test_on_cycle_errors_do_not_stop_polling():
    ch, d = one_device(scan_ms=20)
    calls: list[str] = []

    def on_cycle(device_id: str) -> None:
        calls.append(device_id)
        raise ValueError("publisher bug")

    worker, factory, _ = make_worker(ch, on_cycle=on_cycle)
    async with running(worker):
        await asyncio.sleep(0.12)
    assert len(calls) >= 4


async def test_stop_closes_drivers_and_the_executor():
    name = uniq()
    devs = [device(name, f"D{i}", tags("A", 1), scan_ms=20) for i in range(2)]
    ch = channel(name, devs)
    worker, factory, _ = make_worker(ch)
    task = asyncio.create_task(worker.run())
    await wait_until(lambda: len(factory.drivers) == 2 and all(dr.connected for dr in factory.drivers.values()))
    await worker.stop()
    assert task.done()
    for drv in factory.drivers.values():
        assert drv.closed and not drv.connected
    with pytest.raises(RuntimeError):
        worker.executor.submit(lambda: None)
    with pytest.raises(RuntimeError):
        await worker.read_once(devs[0].id)
    with pytest.raises(RuntimeError):
        await worker.run()
    await worker.stop()                          # idempotent


async def test_cancelling_run_closes_the_connections():
    ch, d = one_device(scan_ms=20)
    worker, factory, _ = make_worker(ch)
    task = asyncio.create_task(worker.run())
    await wait_until(lambda: d.id in factory.drivers and factory.drivers[d.id].connected)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert factory.drivers[d.id].closed
    await worker.stop()


async def test_sync_driver_runs_on_the_channel_executor():
    workers, factories = [], []
    for _ in range(2):
        name = uniq()
        devs = [device(name, f"D{i}", tags("A", 2), scan_ms=20) for i in range(2)]
        factory = FakeFactory(sync=True)
        for dev in devs:
            factory.script(dev.id).delay_s = 0.003
        workers.append(make_worker(channel(name, devs), factory)[0])
        factories.append(factory)
    async with running(*workers):
        await asyncio.sleep(0.2)
        values = [w.cache.snapshot(w.device_ids[0]) for w in workers]
    per_channel_threads = []
    for worker, factory in zip(workers, factories):
        assert all(type(dr).is_async is False for dr in factory.drivers.values())
        assert all(ex is worker.executor for ex in factory.executors.values())
        threads = {t for s in factory.scripts.values() for t in s.threads}
        assert len(threads) == 1                 # one thread for the whole channel
        (thread,) = threads
        assert thread.startswith("opencap-") and thread != threading.main_thread().name
        assert all(s.closes >= 1 for s in factory.scripts.values())   # close ran there too
        per_channel_threads.append(thread)
    assert per_channel_threads[0] != per_channel_threads[1]   # never shared between channels
    for snap in values:
        assert set(qualities(snap).values()) == {"good"}


async def test_periodic_cycle_stats_log_line():
    """PLAN Phase 2: cycle time per site logged (one INFO summary per device per period)."""
    name = uniq()
    dev = device(name, "PLC", tags("A", 2), scan_ms=30)
    worker, factory, _ = make_worker(channel(name, [dev]), stats_interval_s=0.3)
    factory.script(dev.id).delay_s = 0.005
    with capture_logs() as logs:
        async with running(worker):
            await asyncio.sleep(0.8)
    stats = [e for e in logs if e["event"] == "device.stats"]
    assert stats, [e["event"] for e in logs]
    first = stats[0]
    assert first["device"] == dev.id and first["log_level"] == "info"
    assert first["cycles"] >= 3 and first["mean_cycle_ms"] >= 5 and first["max_cycle_ms"] >= first["mean_cycle_ms"]
    assert first["state"] == "ok" and first["bad_cycles"] == 0


async def test_cycle_stats_can_be_turned_off():
    name = uniq()
    dev = device(name, "PLC", tags("A", 1), scan_ms=30)
    worker, _, _ = make_worker(channel(name, [dev]), stats_interval_s=0)
    with capture_logs() as logs:
        async with running(worker):
            await asyncio.sleep(0.3)
    assert not [e for e in logs if e["event"] == "device.stats"]
