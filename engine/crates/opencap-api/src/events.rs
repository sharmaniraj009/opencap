//! `api.request_refused` (D-079, #222): one line for each request the API refused before a
//! handler ran (401, 403, 413), at most one per second per client address and status; the next
//! line written carries `suppressed`, the number of lines dropped in between. Never the token,
//! the query string or the body.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// What a client is called in the log when the request carried no valid token.
pub const UNKNOWN: &str = "unknown";

/// The fields of one `api.request_refused` line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub method: String,
    pub path: String,
    pub status: u16,
    pub who: String,
    pub client: String,
    pub via: &'static str,
    pub suppressed: u64,
}

type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

pub struct RefusalLog {
    interval_s: f64,
    clock: Clock,
    /// (client, status) -> (time of the last line, lines dropped since).
    state: Mutex<HashMap<(String, u16), (f64, u64)>>,
}

impl Default for RefusalLog {
    fn default() -> Self {
        let t0 = Instant::now();
        Self::new(1.0, Arc::new(move || t0.elapsed().as_secs_f64()))
    }
}

impl RefusalLog {
    const PRUNE_AT: usize = 512;

    /// `clock` returns seconds on a monotonic scale (tests inject their own).
    pub fn new(interval_s: f64, clock: Clock) -> Self {
        Self { interval_s, clock, state: Mutex::new(HashMap::new()) }
    }

    /// Decide whether this refusal is written now. `Some` carries the line's fields (with the
    /// `suppressed` count of the lines dropped since the last one); `None` = dropped and counted.
    pub fn admit(
        &self,
        method: &str,
        path: &str,
        status: u16,
        who: Option<&str>,
        client: &str,
        via: &'static str,
    ) -> Option<Refusal> {
        let now = (self.clock)();
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let key = (client.to_string(), status);
        let mut suppressed = 0;
        if let Some(entry) = state.get_mut(&key) {
            if now - entry.0 < self.interval_s {
                entry.1 += 1;
                return None;
            }
            suppressed = entry.1;
        }
        state.insert(key, (now, 0));
        if state.len() > Self::PRUNE_AT {
            state.retain(|_, (at, dropped)| now - *at < 60.0 || *dropped > 0);
        }
        Some(Refusal {
            method: method.to_string(),
            path: path.to_string(),
            status,
            who: who.unwrap_or(UNKNOWN).to_string(),
            client: client.to_string(),
            via,
            suppressed,
        })
    }

    /// Write the line if it is due: WARNING for 401 and 403, INFO otherwise.
    pub fn refused(&self, method: &str, path: &str, status: u16, who: Option<&str>, client: &str, via: &'static str) {
        let Some(r) = self.admit(method, path, status, who, client, via) else { return };
        macro_rules! line {
            ($level:ident) => {
                if r.suppressed > 0 {
                    tracing::$level!(method = %r.method, path = %r.path, status = r.status, who = %r.who,
                        client = %r.client, via = r.via, suppressed = r.suppressed, "api.request_refused")
                } else {
                    tracing::$level!(method = %r.method, path = %r.path, status = r.status, who = %r.who,
                        client = %r.client, via = r.via, "api.request_refused")
                }
            };
        }
        if matches!(r.status, 401 | 403) {
            line!(warn)
        } else {
            line!(info)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn at_most_one_line_a_second_per_client_and_status_with_a_suppressed_count() {
        let now = Arc::new(Mutex::new(100.0f64));
        let clock = now.clone();
        let log = RefusalLog::new(1.0, Arc::new(move || *clock.lock().unwrap()));
        let refuse = |client: &str, status: u16| log.admit("GET", "/api/v1/x", status, None, client, "api");
        let first = refuse("192.0.2.1", 401).expect("the first is written");
        assert_eq!((first.who.as_str(), first.suppressed), ("unknown", 0));
        for _ in 0..4 {
            assert!(refuse("192.0.2.1", 401).is_none());
        }
        assert!(refuse("192.0.2.1", 403).is_some(), "another status has its own budget");
        assert!(refuse("192.0.2.2", 401).is_some(), "another client has its own budget");
        *now.lock().unwrap() = 101.5;
        assert_eq!(refuse("192.0.2.1", 401).expect("due again").suppressed, 4);
        *now.lock().unwrap() = 101.6;
        assert!(refuse("192.0.2.1", 401).is_none());
        *now.lock().unwrap() = 103.0;
        assert_eq!(refuse("192.0.2.1", 401).expect("due").suppressed, 1);
        *now.lock().unwrap() = 105.0;
        assert_eq!(refuse("192.0.2.1", 401).expect("due").suppressed, 0, "nothing was dropped");
    }

    #[test]
    fn old_clients_are_pruned() {
        let now = Arc::new(Mutex::new(0.0f64));
        let clock = now.clone();
        let log = RefusalLog::new(1.0, Arc::new(move || *clock.lock().unwrap()));
        for i in 0..600 {
            assert!(log.admit("GET", "/", 401, None, &format!("10.0.{}.{}", i / 256, i % 256), "api").is_some());
        }
        *now.lock().unwrap() = 120.0;
        log.admit("GET", "/", 401, None, "192.0.2.9", "api");
        assert!(log.state.lock().unwrap().len() < 10, "stale entries dropped once the table is large");
    }
}
