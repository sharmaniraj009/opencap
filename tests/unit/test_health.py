"""DeviceHealth state machine and reconnect Backoff (pure, time passed in)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from opencap.config.models import Device
from opencap.core.health import Backoff, DeviceHealth, iso_utc

AT = datetime(2026, 11, 16, 10, 15, 0, 123456, tzinfo=UTC)


def test_initial_state_is_offline():
    h = DeviceHealth()
    assert h.state == "offline"
    assert h.status() == {"state": "offline", "last_good": None, "errors": 0}


def test_first_good_cycle_goes_ok_with_iso_last_good():
    h = DeviceHealth()
    assert h.record_good(0.0, AT) == "ok"
    assert h.status() == {"state": "ok", "last_good": "2026-11-16T10:15:00.123Z", "errors": 0}
    assert h.record_good(1.0, AT) is None          # ok -> ok is not an event


def test_iso_utc_converts_other_zones_and_naive():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert iso_utc(datetime(2026, 11, 16, 15, 45, 1, tzinfo=ist)) == "2026-11-16T10:15:01.000Z"
    assert iso_utc(datetime(2026, 11, 16, 10, 0, 0)) == "2026-11-16T10:00:00.000Z"
    assert iso_utc(None) is None


def test_failed_cycle_goes_offline_and_counts_errors():
    h = DeviceHealth(demote_after=3)
    h.record_good(0.0, AT)
    assert h.record_failure(1.0) == "offline"
    assert h.state == "offline" and h.errors == 1 and h.consecutive_failures == 1
    assert h.record_failure(2.0) is None           # offline -> offline
    assert h.record_good(3.0, AT) == "ok"
    assert h.consecutive_failures == 0 and h.errors == 2   # errors = failed cycles since start


def test_demotion_after_n_consecutive_failures():
    h = DeviceHealth(demote_after=3, demote_for_ms=30_000)
    assert h.record_failure(0.0) is None
    assert h.record_failure(1.0) is None
    assert h.record_failure(2.0) == "demoted"
    assert h.state == "demoted" and h.demotions == 1
    assert h.demoted_remaining(2.0) == pytest.approx(30.0)
    assert h.demoted_remaining(20.0) == pytest.approx(12.0)
    assert not h.probe_due(31.9)
    assert h.probe_due(32.0)
    assert h.status()["state"] == "demoted"


def test_good_cycle_resets_the_consecutive_count():
    h = DeviceHealth(demote_after=2)
    h.record_failure(0.0)
    h.record_good(1.0, AT)
    assert h.record_failure(2.0) == "offline"
    assert h.state == "offline"


def test_failed_probe_demotes_again_good_probe_resumes():
    h = DeviceHealth(demote_after=2, demote_for_ms=10_000)
    h.record_failure(0.0)
    h.record_failure(1.0)                          # demoted until 11
    assert h.probe_due(11.0)
    assert h.record_failure(11.0) == "redemoted"   # failed probe: another full period
    assert h.state == "demoted" and h.demotions == 1
    assert h.demoted_remaining(11.0) == pytest.approx(10.0)
    assert h.record_good(21.0, AT) == "resumed"
    assert h.state == "ok" and h.demoted_remaining(21.0) == 0.0 and h.consecutive_failures == 0
    assert h.errors == 3


def test_demote_after_zero_disables_demotion():
    h = DeviceHealth(demote_after=0)
    for i in range(50):
        h.record_failure(float(i))
    assert h.state == "offline" and h.errors == 50 and not h.probe_due(100.0)


def test_for_device_takes_device_settings():
    d = Device(id="S/D", channel="S", name="D", driver="modbus_tcp", host="h", port=1, tags=[],
               demote_after=5, demote_for_ms=1234)
    h = DeviceHealth.for_device(d)
    assert h.demote_after == 5 and h.demote_for_s == pytest.approx(1.234)


def test_backoff_doubles_from_1s_and_caps_at_60s():
    b = Backoff()
    delays = [b.failure(0.0) for _ in range(9)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert b.delay(10_000) == 60                   # no overflow on long outages


def test_backoff_pending_window_and_reset():
    b = Backoff(base_s=1.0, max_s=60.0)
    assert not b.pending(0.0) and b.remaining(0.0) == 0.0
    b.failure(100.0)                               # 1 s
    assert b.pending(100.5) and b.remaining(100.5) == pytest.approx(0.5)
    assert not b.pending(101.0)
    b.failure(101.0)                               # 2 s
    assert b.pending(102.9) and not b.pending(103.0)
    b.reset()
    assert b.failures == 0 and not b.pending(103.0)
    assert b.failure(200.0) == 1.0                 # starts again at the base
