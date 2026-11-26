//! Tag values, qualities and timestamps (Python `opencap.core.cache.TagValue`).

use std::fmt;
use std::sync::OnceLock;
use std::time::Instant;

use chrono::{DateTime, SecondsFormat, Timelike, Utc};
use serde_json::Value as Json;

use crate::pyfmt::{py_float_repr, py_repr_str};

/// A decoded tag value. Python ints are unbounded; here a value that fits `i64` is always
/// `Int`, `UInt` is only used above `i64::MAX` (uint64 registers).
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Bool(bool),
    Int(i64),
    UInt(u64),
    Float(f64),
    Str(String),
    List(Vec<Value>),
}

impl Value {
    pub fn from_u64(u: u64) -> Self {
        i64::try_from(u).map(Value::Int).unwrap_or(Value::UInt(u))
    }

    /// True for int and float values (not bool), like Python's
    /// `isinstance(v, (int, float)) and not isinstance(v, bool)`.
    pub fn is_number(&self) -> bool {
        matches!(self, Value::Int(_) | Value::UInt(_) | Value::Float(_))
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Int(i) => Some(*i as f64),
            Value::UInt(u) => Some(*u as f64),
            Value::Float(f) => Some(*f),
            _ => None,
        }
    }

    /// Python `repr()` of the equivalent object (used by `opencap-engine read`).
    pub fn py_repr(&self) -> String {
        match self {
            Value::Bool(b) => if *b { "True" } else { "False" }.into(),
            Value::Int(i) => i.to_string(),
            Value::UInt(u) => u.to_string(),
            Value::Float(f) => py_float_repr(*f),
            Value::Str(s) => py_repr_str(s),
            Value::List(items) => format!("[{}]", items.iter().map(Value::py_repr).collect::<Vec<_>>().join(", ")),
        }
    }

    /// JSON for the admin API (non-finite floats become null).
    pub fn to_json(&self) -> Json {
        match self {
            Value::Bool(b) => Json::Bool(*b),
            Value::Int(i) => Json::from(*i),
            Value::UInt(u) => Json::from(*u),
            Value::Float(f) => serde_json::Number::from_f64(*f).map(Json::Number).unwrap_or(Json::Null),
            Value::Str(s) => Json::String(s.clone()),
            Value::List(items) => Json::Array(items.iter().map(Value::to_json).collect()),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Quality {
    Good,
    BadComm,
    BadConfig,
    BadDecode,
    Stale,
}

impl Quality {
    pub fn as_str(&self) -> &'static str {
        match self {
            Quality::Good => "good",
            Quality::BadComm => "bad_comm",
            Quality::BadConfig => "bad_config",
            Quality::BadDecode => "bad_decode",
            Quality::Stale => "stale",
        }
    }
}

impl fmt::Display for Quality {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.pad(self.as_str())
    }
}

/// One tag's value as the tag store holds it. `value` is `None` for a tag never read good.
///
/// The two timestamps follow [ADR-0006](../../../../docs/adr/0006-quality-and-timestamps.md):
/// `source_ts` is when the reply arrived (or the device's own timestamp), `server_ts` is when
/// the tag store last accepted a write for the tag. Drivers leave `server_ts` at `None`; the
/// store sets it.
#[derive(Debug, Clone, PartialEq)]
pub struct TagValue {
    pub value: Option<Value>,
    pub quality: Quality,
    /// Time the reply arrived (UTC); `None` = never read.
    pub source_ts: Option<DateTime<Utc>>,
    /// Time the tag store accepted the last write (UTC); `None` = never written.
    pub server_ts: Option<DateTime<Utc>>,
}

impl TagValue {
    pub fn new(value: Option<Value>, quality: Quality, source_ts: Option<DateTime<Utc>>) -> Self {
        Self { value, quality, source_ts, server_ts: None }
    }

    /// Same value, quality and `source_ts` (the Python `TagValue` fields); `server_ts` is
    /// ignored.
    pub fn same_reading(&self, other: &TagValue) -> bool {
        self.quality == other.quality && self.source_ts == other.source_ts && self.value == other.value
    }

    pub fn good(value: Value, ts: DateTime<Utc>) -> Self {
        Self::new(Some(value), Quality::Good, Some(ts))
    }

    pub fn bad(quality: Quality, ts: Option<DateTime<Utc>>) -> Self {
        Self::new(None, quality, ts)
    }

    /// `{"value", "quality", "source_ts"}` as the Python API's `tag_value_json()` returns it.
    pub fn to_api_json(&self) -> Json {
        serde_json::json!({
            "value": self.value.as_ref().map(Value::to_json).unwrap_or(Json::Null),
            "quality": self.quality.as_str(),
            "source_ts": self.source_ts.map(py_isoformat),
        })
    }
}

/// Source of "now" for timestamps (tests inject a fixed clock).
pub type Clock = std::sync::Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>;

/// Current UTC time truncated to microseconds (Python `datetime` precision), so both engines
/// report the same timestamps.
pub fn now_utc() -> DateTime<Utc> {
    let now = Utc::now();
    let micros = now.nanosecond() / 1_000 * 1_000;
    now.with_nanosecond(micros).unwrap_or(now)
}

/// Monotonic seconds since the first call (Python `time.monotonic()`; only differences matter).
pub fn mono() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// Python `iso_utc()`: ISO-8601 UTC with milliseconds and `Z`, e.g. `2026-11-16T10:15:00.123Z`.
pub fn iso_utc(ts: DateTime<Utc>) -> String {
    ts.to_rfc3339_opts(SecondsFormat::Millis, true)
}

/// Python `datetime.isoformat()` of a UTC datetime: microseconds only when non-zero,
/// `+00:00` offset.
pub fn py_isoformat(ts: DateTime<Utc>) -> String {
    let micros = ts.nanosecond() / 1_000;
    if micros == 0 {
        ts.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{:06}+00:00", ts.format("%Y-%m-%dT%H:%M:%S"), micros)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn iso_formats_match_python() {
        let t = Utc.with_ymd_and_hms(2026, 11, 16, 10, 15, 0).unwrap();
        assert_eq!(iso_utc(t), "2026-11-16T10:15:00.000Z");
        assert_eq!(py_isoformat(t), "2026-11-16T10:15:00+00:00");
        let t2 = t.with_nanosecond(123_456_789).unwrap();
        assert_eq!(iso_utc(t2), "2026-11-16T10:15:00.123Z"); // truncated, as Python's timespec
        assert_eq!(py_isoformat(t2), "2026-11-16T10:15:00.123456+00:00");
    }

    #[test]
    fn repr_of_values() {
        let v = Value::List(vec![Value::Bool(true), Value::Float(1.0), Value::Str("x".into())]);
        assert_eq!(v.py_repr(), "[True, 1.0, 'x']");
        assert_eq!(Value::from_u64(u64::MAX), Value::UInt(u64::MAX));
        assert_eq!(Value::from_u64(5), Value::Int(5));
    }
}
