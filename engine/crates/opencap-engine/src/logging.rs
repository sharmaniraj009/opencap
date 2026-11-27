//! One log line format (LOG-R3, LOG-R6, LOG-R7, #133): every stdout line is one JSON object with
//! `timestamp` (ISO-8601 UTC, `Z`), `level` (`debug`, `info`, `warning`, `error`), `event` and
//! `instance`, then the event's own fields: the same keys and spelling as the Python engine, so
//! the `jq` filters of `docs/deployment.md` work on both.
//!
//! - **Reason codes.** A `warning` or `error` carries `reason` from the closed lists of
//!   `opencap_core::reasons`. A call site that names none gets the default for its event; one that
//!   wrote free text under `reason` has it moved to `detail`.
//! - **Flood control.** Repeats of the same event, device and reason at `warning` or `error` are
//!   written the first time; the next line after a minute carries `suppressed=N` (the repeats in
//!   between). State-change events are never suppressed.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

use chrono::Utc;
use serde_json::{Map, Value};
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::fmt::format::Writer;
use tracing_subscriber::fmt::{FmtContext, FormatEvent, FormatFields};
use tracing_subscriber::registry::LookupSpan;

use opencap_core::reasons;

/// Repeats are summarised at most this often.
pub const FLOOD_INTERVAL_S: f64 = 60.0;
const PRUNE_AT: usize = 1024;

/// Warnings and errors that mark a state change: written every time.
const STATE_CHANGES: [&str; 6] = [
    "mqtt_disconnected",
    "mqtt_connected",
    "device.offline",
    "device.demoted",
    "device.resumed",
    "sparkplug.session_lost",
];

/// The instance named on every line (`poller.yaml` `instance`), set once the config is loaded.
static INSTANCE: OnceLock<String> = OnceLock::new();

pub fn set_instance(name: &str) {
    let _ = INSTANCE.set(name.to_string());
}

type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

/// (event, device, reason) -> (time of the last line written, repeats dropped since).
type Seen = HashMap<(String, String, String), (f64, u64)>;

/// Decides which repeats are written. Pure apart from its clock.
pub struct FloodControl {
    interval_s: f64,
    clock: Clock,
    seen: Mutex<Seen>,
    exempt: HashSet<&'static str>,
}

impl FloodControl {
    pub fn new(interval_s: f64, clock: Clock) -> Self {
        Self { interval_s, clock, seen: Mutex::new(HashMap::new()), exempt: STATE_CHANGES.into_iter().collect() }
    }

    pub fn system() -> Self {
        let t0 = Instant::now();
        Self::new(FLOOD_INTERVAL_S, Arc::new(move || t0.elapsed().as_secs_f64()))
    }

    /// `None`: drop this line (counted). `Some(n)`: write it, with `suppressed=n` when n > 0.
    pub fn admit(&self, event: &str, device: &str, reason: &str) -> Option<u64> {
        if self.exempt.contains(event) {
            return Some(0);
        }
        let now = (self.clock)();
        let mut seen = self.seen.lock().unwrap_or_else(|e| e.into_inner());
        let key = (event.to_string(), device.to_string(), reason.to_string());
        let mut suppressed = 0;
        if let Some(entry) = seen.get_mut(&key) {
            if now - entry.0 < self.interval_s {
                entry.1 += 1;
                return None;
            }
            suppressed = entry.1;
        }
        seen.insert(key, (now, 0));
        if seen.len() > PRUNE_AT {
            let interval = self.interval_s;
            seen.retain(|_, (at, dropped)| now - *at < interval * 2.0 || *dropped > 0);
        }
        Some(suppressed)
    }
}

/// The event's fields as JSON, in the order they were recorded.
struct Fields(Map<String, Value>);

impl Visit for Fields {
    fn record_str(&mut self, f: &Field, v: &str) {
        self.0.insert(f.name().into(), v.into());
    }
    fn record_i64(&mut self, f: &Field, v: i64) {
        self.0.insert(f.name().into(), v.into());
    }
    fn record_u64(&mut self, f: &Field, v: u64) {
        self.0.insert(f.name().into(), v.into());
    }
    fn record_bool(&mut self, f: &Field, v: bool) {
        self.0.insert(f.name().into(), v.into());
    }
    fn record_f64(&mut self, f: &Field, v: f64) {
        self.0.insert(f.name().into(), serde_json::Number::from_f64(v).map_or(Value::Null, Value::Number));
    }
    fn record_debug(&mut self, f: &Field, v: &dyn fmt::Debug) {
        self.0.insert(f.name().into(), format!("{v:?}").into());
    }
}

fn level_name(level: &Level) -> &'static str {
    match *level {
        Level::ERROR => "error",
        Level::WARN => "warning",
        Level::INFO => "info",
        _ => "debug",
    }
}

/// Build the line for one event: `None` when flood control drops it.
pub fn render(
    level: &Level,
    mut fields: Map<String, Value>,
    flood: &FloodControl,
    instance: Option<&str>,
) -> Option<String> {
    let event = match fields.remove("message") {
        Some(Value::String(s)) => s,
        Some(other) => other.to_string(),
        None => String::new(),
    };
    let mut suppressed = 0;
    if matches!(*level, Level::WARN | Level::ERROR) {
        let error = fields.get("error").and_then(Value::as_str).map(str::to_string);
        // free text under `reason` is detail; the code comes from the closed lists
        let code = match fields.remove("reason") {
            Some(Value::String(s)) if reasons::is_reason(&s) => s,
            Some(text) => {
                fields.insert("detail".into(), text);
                reasons::event_default(&event, error.as_deref()).to_string()
            }
            None => reasons::event_default(&event, error.as_deref()).to_string(),
        };
        let device = fields.get("device").and_then(Value::as_str).unwrap_or("");
        suppressed = flood.admit(&event, device, &code)?;
        fields.insert("reason".into(), code.into());
    }
    let mut out = Map::new();
    out.insert("timestamp".into(), Utc::now().format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string().into());
    out.insert("level".into(), level_name(level).into());
    out.insert("event".into(), event.into());
    if let Some(i) = instance {
        out.insert("instance".into(), i.into());
    }
    for (k, v) in fields {
        out.insert(k, v);
    }
    if suppressed > 0 {
        out.insert("suppressed".into(), suppressed.into());
    }
    Some(Value::Object(out).to_string())
}

/// The `tracing` formatter: one JSON object per line.
pub struct LineFormat {
    flood: FloodControl,
}

impl LineFormat {
    pub fn new() -> Self {
        Self { flood: FloodControl::system() }
    }

    #[cfg(test)]
    pub fn with_flood(flood: FloodControl) -> Self {
        Self { flood }
    }
}

impl Default for LineFormat {
    fn default() -> Self {
        Self::new()
    }
}

impl<S, N> FormatEvent<S, N> for LineFormat
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    N: for<'a> FormatFields<'a> + 'static,
{
    fn format_event(&self, _ctx: &FmtContext<'_, S, N>, mut writer: Writer<'_>, event: &Event<'_>) -> fmt::Result {
        let mut fields = Fields(Map::new());
        event.record(&mut fields);
        match render(event.metadata().level(), fields.0, &self.flood, INSTANCE.get().map(String::as_str)) {
            Some(line) => writeln!(writer, "{line}"),
            None => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tracing_subscriber::fmt::MakeWriter;

    #[derive(Clone, Default)]
    struct Sink(Arc<Mutex<Vec<u8>>>);

    impl Write for Sink {
        fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(b);
            Ok(b.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl<'a> MakeWriter<'a> for Sink {
        type Writer = Sink;
        fn make_writer(&'a self) -> Sink {
            self.clone()
        }
    }

    fn lines(sink: &Sink) -> Vec<Value> {
        let text = String::from_utf8(sink.0.lock().unwrap().clone()).unwrap();
        text.lines().map(|l| serde_json::from_str(l).unwrap_or_else(|e| panic!("not JSON: {l}: {e}"))).collect()
    }

    fn with_subscriber(flood: FloodControl, body: impl FnOnce()) -> Sink {
        let sink = Sink::default();
        let sub = tracing_subscriber::fmt()
            .event_format(LineFormat::with_flood(flood))
            .with_writer(sink.clone())
            .with_max_level(tracing::Level::DEBUG)
            .finish();
        tracing::subscriber::with_default(sub, body);
        sink
    }

    fn fake_clock() -> (Arc<Mutex<f64>>, FloodControl) {
        let now = Arc::new(Mutex::new(0.0));
        let clock = now.clone();
        (now, FloodControl::new(60.0, Arc::new(move || *clock.lock().unwrap())))
    }

    #[test]
    fn a_line_has_the_common_keys_in_python_spelling() {
        let (_, flood) = fake_clock();
        let sink = with_subscriber(flood, || {
            tracing::info!(channel = "Site", device = "Site/PLC", ms = 12, "device.connected");
            tracing::warn!(device = "Site/PLC", error = "Connection refused (os error 111)", "mqtt_connect_failed");
            tracing::error!(error = "boom", "reload_failed");
            tracing::debug!("device.request");
        });
        let l = lines(&sink);
        assert_eq!(l.len(), 4);
        for line in &l {
            for key in ["timestamp", "level", "event"] {
                assert!(line.get(key).is_some(), "{key} in {line}");
            }
            assert!(line.get("message").is_none() && line.get("target").is_none(), "{line}");
            let ts = line["timestamp"].as_str().unwrap();
            assert!(ts.ends_with('Z') && ts.len() == 27, "{ts}");
        }
        let levels: Vec<&str> = l.iter().map(|x| x["level"].as_str().unwrap()).collect();
        assert_eq!(levels, ["info", "warning", "error", "debug"]);
        assert_eq!(l[0]["event"], "device.connected");
        assert_eq!((l[0]["channel"].as_str(), l[0]["ms"].as_i64()), (Some("Site"), Some(12)));
        assert!(l[0].get("reason").is_none(), "info lines get no reason");
        assert_eq!(l[1]["reason"], "refused", "from the words of the error");
        assert_eq!(l[2]["reason"], "invalid", "from the event");
    }

    #[test]
    fn free_text_reason_becomes_detail() {
        let (_, flood) = fake_clock();
        let sink = with_subscriber(flood, || {
            tracing::warn!(device = "S/D", reason = "no readable blocks left", "device.idle");
            tracing::warn!(device = "S/D", reason = "timeout", "something");
        });
        let l = lines(&sink);
        assert_eq!(
            (l[0]["reason"].as_str(), l[0]["detail"].as_str()),
            (Some("address_rejected"), Some("no readable blocks left"))
        );
        assert_eq!(l[1]["reason"], "timeout");
        assert!(l[1].get("detail").is_none());
    }

    #[test]
    fn a_burst_of_identical_failures_is_one_line_then_one_summary_a_minute() {
        let (now, flood) = fake_clock();
        let sink = with_subscriber(flood, || {
            for i in 0..1000 {
                *now.lock().unwrap() = f64::from(i) * 0.01; // 10 s in all
                tracing::warn!(device = "S/D", error = "timed out", "publish_failed");
            }
            *now.lock().unwrap() = 61.0;
            tracing::warn!(device = "S/D", error = "timed out", "publish_failed");
            tracing::warn!(device = "S/D", error = "timed out", "publish_failed");
            tracing::warn!(device = "S/OTHER", error = "timed out", "publish_failed");
            tracing::warn!(device = "S/D", error = "connection refused", "publish_failed");
        });
        let l = lines(&sink);
        assert_eq!(l.len(), 4, "{l:?}");
        assert!(l[0].get("suppressed").is_none());
        assert_eq!(l[1]["suppressed"], 999, "the repeats of the first minute");
        assert_eq!(
            (l[2]["device"].as_str(), l[3]["reason"].as_str()),
            (Some("S/OTHER"), Some("refused")),
            "own budgets"
        );
    }

    #[test]
    fn state_changes_and_info_lines_are_never_suppressed() {
        let (_, flood) = fake_clock();
        let sink = with_subscriber(flood, || {
            for _ in 0..5 {
                tracing::warn!(error = "Connection reset by peer", "mqtt_disconnected");
                tracing::info!(device = "S/D", "device.demoted");
            }
        });
        assert_eq!(lines(&sink).len(), 10);
    }

    #[test]
    fn instance_is_added_when_known() {
        let (_, flood) = fake_clock();
        let line = render(&Level::INFO, Map::new(), &flood, Some("edge-1")).unwrap();
        let v: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["instance"], "edge-1");
    }
}
