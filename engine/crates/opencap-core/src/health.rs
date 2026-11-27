//! Device health state machine and reconnect backoff (Python `opencap.core.health`).
//!
//! Both are pure: `now` is monotonic seconds (durations), `at` a UTC timestamp (reporting).
//!
//! Cycle outcomes (decided by the channel worker):
//! - good: at least one block of the cycle was read successfully;
//! - failed: nothing succeeded and at least one block failed for a communication reason
//!   (CommError after all retries, ConnectionLost, connect failure, backoff still pending,
//!   safety-net timeout);
//! - neutral: every block attempted was rejected with BlockConfigError. Health unchanged.
//!
//! States: `offline` (initial, and after a failed cycle that does not demote), `ok`,
//! `demoted` (after `demote_after` consecutive failed cycles; `demote_after <= 0`
//! disables demotion; one probe after `demote_for_ms`).

use chrono::{DateTime, Utc};

use crate::value::iso_utc;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Ok,
    Offline,
    Demoted,
}

impl State {
    pub fn as_str(&self) -> &'static str {
        match self {
            State::Ok => "ok",
            State::Offline => "offline",
            State::Demoted => "demoted",
        }
    }

    /// Prometheus `device_state` code: 0 = ok, 1 = offline, 2 = demoted.
    pub fn code(&self) -> i64 {
        match self {
            State::Ok => 0,
            State::Offline => 1,
            State::Demoted => 2,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HealthEvent {
    /// offline -> ok
    Ok,
    /// ok -> offline
    Offline,
    /// ok/offline -> demoted
    Demoted,
    /// demoted -> demoted (failed probe)
    Redemoted,
    /// demoted -> ok (good probe)
    Resumed,
}

/// `{"state", "last_good", "errors"}` (Python `DeviceHealth.status()`).
#[derive(Debug, Clone, PartialEq)]
pub struct HealthStatus {
    pub state: State,
    pub last_good: Option<String>,
    pub errors: u64,
}

#[derive(Debug, Clone)]
pub struct DeviceHealth {
    pub demote_after: i64,
    pub demote_for_s: f64,
    pub state: State,
    pub errors: u64,
    pub consecutive_failures: i64,
    pub last_good: Option<DateTime<Utc>>,
    /// UTC time of the cycle that took `consecutive_failures` from 0 to 1; cleared by a good cycle
    /// (system-tag spec section 9). A neutral cycle changes nothing.
    pub error_since: Option<DateTime<Utc>>,
    pub demoted_until: Option<f64>,
    pub demotions: u64,
}

impl DeviceHealth {
    pub fn new(demote_after: i64, demote_for_ms: i64) -> Self {
        Self {
            demote_after,
            demote_for_s: demote_for_ms.max(0) as f64 / 1000.0,
            state: State::Offline,
            errors: 0,
            consecutive_failures: 0,
            last_good: None,
            error_since: None,
            demoted_until: None,
            demotions: 0,
        }
    }

    pub fn demotion_enabled(&self) -> bool {
        self.demote_after > 0
    }

    pub fn record_good(&mut self, _now: f64, at: DateTime<Utc>) -> Option<HealthEvent> {
        let prev = self.state;
        self.state = State::Ok;
        self.consecutive_failures = 0;
        self.demoted_until = None;
        self.last_good = Some(at);
        self.error_since = None;
        match prev {
            State::Demoted => Some(HealthEvent::Resumed),
            State::Offline => Some(HealthEvent::Ok),
            State::Ok => None,
        }
    }

    pub fn record_failure(&mut self, now: f64, at: DateTime<Utc>) -> Option<HealthEvent> {
        let prev = self.state;
        self.errors += 1;
        if self.consecutive_failures == 0 {
            self.error_since = Some(at);
        }
        self.consecutive_failures += 1;
        if self.demotion_enabled() && self.consecutive_failures >= self.demote_after {
            self.state = State::Demoted;
            self.demoted_until = Some(now + self.demote_for_s);
            if prev == State::Demoted {
                return Some(HealthEvent::Redemoted);
            }
            self.demotions += 1;
            return Some(HealthEvent::Demoted);
        }
        self.state = State::Offline;
        if prev == State::Ok {
            Some(HealthEvent::Offline)
        } else {
            None
        }
    }

    /// Seconds the device must still be skipped (0 when not demoted or the probe is due).
    pub fn demoted_remaining(&self, now: f64) -> f64 {
        match (self.state, self.demoted_until) {
            (State::Demoted, Some(until)) => (until - now).max(0.0),
            _ => 0.0,
        }
    }

    pub fn probe_due(&self, now: f64) -> bool {
        self.state == State::Demoted && self.demoted_remaining(now) == 0.0
    }

    pub fn status(&self) -> HealthStatus {
        HealthStatus { state: self.state, last_good: self.last_good.map(iso_utc), errors: self.errors }
    }
}

/// Exponential reconnect backoff: base, 2*base, 4*base ... capped at `max_s`.
#[derive(Debug, Clone)]
pub struct Backoff {
    pub base_s: f64,
    pub max_s: f64,
    pub failures: u32,
    pub retry_at: Option<f64>,
}

impl Backoff {
    pub fn new(base_s: f64, max_s: f64) -> Self {
        Self { base_s, max_s, failures: 0, retry_at: None }
    }

    pub fn delay(&self, failures: u32) -> f64 {
        if failures == 0 {
            return 0.0;
        }
        (self.base_s * 2f64.powi((failures - 1).min(62) as i32)).min(self.max_s)
    }

    pub fn failure(&mut self, now: f64) -> f64 {
        self.failures += 1;
        let d = self.delay(self.failures);
        self.retry_at = Some(now + d);
        d
    }

    pub fn reset(&mut self) {
        self.failures = 0;
        self.retry_at = None;
    }

    pub fn remaining(&self, now: f64) -> f64 {
        self.retry_at.map(|t| (t - now).max(0.0)).unwrap_or(0.0)
    }

    pub fn pending(&self, now: f64) -> bool {
        self.remaining(now) > 0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn at() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 16, 10, 15, 0).unwrap()
    }

    #[test]
    fn starts_offline_and_goes_ok() {
        let mut h = DeviceHealth::new(3, 30_000);
        assert_eq!(h.state, State::Offline);
        assert_eq!(h.record_good(0.0, at()), Some(HealthEvent::Ok));
        assert_eq!(h.record_good(1.0, at()), None);
        assert_eq!(
            h.status(),
            HealthStatus { state: State::Ok, last_good: Some("2026-11-16T10:15:00.000Z".into()), errors: 0 }
        );
    }

    #[test]
    fn demotes_after_n_failures_then_probe() {
        let mut h = DeviceHealth::new(3, 30_000);
        h.record_good(0.0, at());
        assert_eq!(h.record_failure(1.0, at()), Some(HealthEvent::Offline));
        assert_eq!(h.record_failure(2.0, at()), None);
        assert_eq!(h.record_failure(3.0, at()), Some(HealthEvent::Demoted));
        assert_eq!(h.state, State::Demoted);
        assert_eq!(h.demoted_remaining(13.0), 20.0);
        assert!(!h.probe_due(32.9));
        assert!(h.probe_due(33.0));
        assert_eq!(h.record_failure(33.0, at()), Some(HealthEvent::Redemoted));
        assert_eq!(h.demoted_remaining(33.0), 30.0);
        assert_eq!(h.demotions, 1);
        assert_eq!(h.record_good(64.0, at()), Some(HealthEvent::Resumed));
        assert_eq!(h.errors, 4);
        assert_eq!(h.demoted_remaining(64.0), 0.0);
    }

    #[test]
    fn error_since_marks_the_start_of_a_failure_run() {
        let t = |s| at() + chrono::Duration::seconds(s);
        let mut h = DeviceHealth::new(3, 30_000);
        assert_eq!(h.error_since, None);
        h.record_failure(1.0, t(1));
        h.record_failure(2.0, t(2));
        assert_eq!(h.error_since, Some(t(1)), "kept through the run");
        h.record_good(3.0, t(3));
        assert_eq!(h.error_since, None, "cleared by a good cycle");
        h.record_failure(4.0, t(4));
        assert_eq!(h.error_since, Some(t(4)));
    }

    #[test]
    fn demotion_disabled() {
        let mut h = DeviceHealth::new(0, 30_000);
        for i in 0..10 {
            h.record_failure(i as f64, at());
        }
        assert_eq!(h.state, State::Offline);
        assert_eq!(h.errors, 10);
    }

    #[test]
    fn backoff_doubles_and_caps() {
        let mut b = Backoff::new(1.0, 60.0);
        let delays: Vec<f64> = (0..8).map(|_| b.failure(0.0)).collect();
        assert_eq!(delays, vec![1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]);
        assert!(b.pending(59.0) && !b.pending(60.0));
        b.reset();
        assert_eq!(b.remaining(0.0), 0.0);
        assert_eq!(b.delay(1000), 60.0);
    }
}
