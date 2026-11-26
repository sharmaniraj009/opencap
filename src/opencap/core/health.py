"""Device health state machine and reconnect backoff (PLAN.md section 6).

Both classes are pure: they never read a clock. `now` is monotonic seconds (used for
durations), `at` is a tz-aware UTC datetime (used for reporting only).

Cycle outcomes (decided by the ChannelWorker, one per rate-group cycle or `read_once`):

    good     at least one block of the cycle was read successfully.
    failed   no block succeeded and at least one block failed for a communication reason:
             CommError after all retries, ConnectionLost, connect failure, reconnect
             backoff still pending (no request sent), or the safety-net timeout.
    neutral  nothing succeeded and nothing failed on the wire (every block attempted was
             rejected with BlockConfigError). Health does not change.

States:

    offline  initial state until the first good cycle; also after a failed cycle that does
             not (yet) demote.
    ok       the last counted cycle was good.
    demoted  `demote_after` consecutive failed cycles (`demote_after <= 0` disables
             demotion). The device is skipped until `demoted_until`; then one probe cycle
             runs. Good probe -> ok ("resumed"). Failed probe -> demoted again for another
             `demote_for_ms` ("redemoted", not a state change).

`errors` counts failed cycles since start. `error_since` is the (reporting) time of the cycle that took
`consecutive_failures` from 0 to 1, cleared by a good cycle; neutral cycles change nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from opencap.config.models import Device

State = Literal["ok", "offline", "demoted"]

# Events returned by DeviceHealth.record_*(); None = nothing to report.
EV_OK = "ok"                 # offline -> ok
EV_OFFLINE = "offline"       # ok -> offline
EV_DEMOTED = "demoted"       # ok/offline -> demoted
EV_REDEMOTED = "redemoted"   # demoted -> demoted (failed probe)
EV_RESUMED = "resumed"       # demoted -> ok (good probe)


def iso_utc(ts: datetime | None) -> str | None:
    """ISO-8601 UTC with milliseconds and a 'Z' suffix, e.g. '2026-11-16T10:15:00.123Z'."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DeviceHealth:
    def __init__(self, demote_after: int = 3, demote_for_ms: int = 30_000) -> None:
        self.demote_after = demote_after
        self.demote_for_s = max(demote_for_ms, 0) / 1000
        self.state: State = "offline"
        self.errors = 0                       # failed cycles since start
        self.consecutive_failures = 0
        self.last_good: datetime | None = None
        self.error_since: datetime | None = None   # UTC of the cycle that opened the failure run (system tags, spec §9)
        self.demoted_until: float | None = None
        self.demotions = 0                    # times the device entered "demoted"

    @classmethod
    def for_device(cls, device: Device) -> DeviceHealth:
        return cls(device.demote_after, device.demote_for_ms)

    @property
    def demotion_enabled(self) -> bool:
        return self.demote_after > 0

    def record_good(self, now: float, at: datetime) -> str | None:
        prev = self.state
        self.state = "ok"
        self.consecutive_failures = 0
        self.demoted_until = None
        self.error_since = None
        self.last_good = at
        if prev == "demoted":
            return EV_RESUMED
        if prev == "offline":
            return EV_OK
        return None

    def record_failure(self, now: float, at: datetime | None = None) -> str | None:
        prev = self.state
        if self.consecutive_failures == 0:
            self.error_since = at
        self.errors += 1
        self.consecutive_failures += 1
        if self.demotion_enabled and self.consecutive_failures >= self.demote_after:
            self.state = "demoted"
            self.demoted_until = now + self.demote_for_s
            if prev == "demoted":
                return EV_REDEMOTED
            self.demotions += 1
            return EV_DEMOTED
        self.state = "offline"
        return EV_OFFLINE if prev == "ok" else None

    def demoted_remaining(self, now: float) -> float:
        """Seconds the device must still be skipped (0 when not demoted or the probe is due)."""
        if self.state != "demoted" or self.demoted_until is None:
            return 0.0
        return max(0.0, self.demoted_until - now)

    def probe_due(self, now: float) -> bool:
        return self.state == "demoted" and self.demoted_remaining(now) == 0.0

    def status(self) -> dict:
        return {"state": self.state, "last_good": iso_utc(self.last_good), "errors": self.errors}


class Backoff:
    """Exponential reconnect backoff: base, 2*base, 4*base ... capped at `max_s`.

    `failure()` is called for every failed connect attempt and returns the delay before
    the next attempt is allowed. `reset()` after a successful connect.
    """

    def __init__(self, base_s: float = 1.0, max_s: float = 60.0) -> None:
        self.base_s = base_s
        self.max_s = max_s
        self.failures = 0
        self.retry_at: float | None = None

    def delay(self, failures: int) -> float:
        if failures <= 0:
            return 0.0
        # cap the exponent so huge failure counts cannot overflow
        return min(self.base_s * 2 ** min(failures - 1, 62), self.max_s)

    def failure(self, now: float) -> float:
        self.failures += 1
        d = self.delay(self.failures)
        self.retry_at = now + d
        return d

    def reset(self) -> None:
        self.failures = 0
        self.retry_at = None

    def remaining(self, now: float) -> float:
        return 0.0 if self.retry_at is None else max(0.0, self.retry_at - now)

    def pending(self, now: float) -> bool:
        return self.remaining(now) > 0.0
