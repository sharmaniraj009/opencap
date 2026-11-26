//! `opencap.plugin.v1` messages <-> the engine's value model and driver errors.

use std::collections::HashMap;
use std::time::Duration;

use chrono::{DateTime, Utc};

use opencap_core::driver::DriverError;
use opencap_core::value::{Quality, TagValue, Value};

use crate::proto;
use crate::proto::driver_error::Kind;
use crate::proto::variant::Kind as V;

/// Static facts about the plugin's driver (`DriverInfo`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Info {
    pub reports_connection: bool,
    pub connect_timeout: Option<Duration>,
    pub stale_by_read_time: bool,
}

impl From<&proto::DriverInfo> for Info {
    fn from(i: &proto::DriverInfo) -> Self {
        Self {
            reports_connection: i.reports_connection,
            connect_timeout: i.connect_timeout_ms.map(|ms| Duration::from_millis(ms.into())),
            stale_by_read_time: i.stale_by_read_time,
        }
    }
}

/// A plugin-side driver error. `NOT_OPEN` is handled by the caller before this.
pub fn driver_error(e: &proto::DriverError) -> DriverError {
    let msg = e.message.clone();
    match Kind::try_from(e.kind).unwrap_or(Kind::Other) {
        Kind::Comm => DriverError::Comm(msg),
        Kind::ConnectionLost | Kind::NotOpen => DriverError::ConnectionLost(msg),
        Kind::BlockConfig => DriverError::BlockConfig(msg),
        Kind::Unsupported => DriverError::NotImplemented(msg),
        Kind::Other | Kind::Unspecified => DriverError::Other(msg),
    }
}

pub fn is_not_open(e: &Option<proto::DriverError>) -> bool {
    e.as_ref().is_some_and(|e| e.kind == Kind::NotOpen as i32)
}

/// `None` for a variant with no kind set (a sender bug): the tag becomes bad_decode.
pub fn value(v: &proto::Variant) -> Option<Value> {
    Some(match v.kind.as_ref()? {
        V::BoolValue(b) => Value::Bool(*b),
        V::IntValue(i) => Value::Int(*i),
        V::UintValue(u) => Value::from_u64(*u),
        V::FloatValue(f) => Value::Float(*f),
        V::StringValue(s) => Value::Str(s.clone()),
        V::ListValue(l) => Value::List(l.items.iter().map(value).collect::<Option<Vec<_>>>()?),
    })
}

pub fn variant(v: &Value) -> proto::Variant {
    let kind = match v {
        Value::Bool(b) => V::BoolValue(*b),
        Value::Int(i) => V::IntValue(*i),
        Value::UInt(u) => V::UintValue(*u),
        Value::Float(f) => V::FloatValue(*f),
        Value::Str(s) => V::StringValue(s.clone()),
        Value::List(items) => V::ListValue(proto::VariantList { items: items.iter().map(variant).collect() }),
    };
    proto::Variant { kind: Some(kind) }
}

pub fn quality(q: i32) -> Quality {
    match proto::Quality::try_from(q).unwrap_or(proto::Quality::Unspecified) {
        proto::Quality::Good => Quality::Good,
        proto::Quality::BadComm => Quality::BadComm,
        proto::Quality::BadConfig => Quality::BadConfig,
        proto::Quality::Stale => Quality::Stale,
        proto::Quality::BadDecode | proto::Quality::Unspecified => Quality::BadDecode,
    }
}

pub fn quality_code(q: Quality) -> proto::Quality {
    match q {
        Quality::Good => proto::Quality::Good,
        Quality::BadComm => proto::Quality::BadComm,
        Quality::BadConfig => proto::Quality::BadConfig,
        Quality::BadDecode => proto::Quality::BadDecode,
        Quality::Stale => proto::Quality::Stale,
    }
}

pub fn timestamp(us: i64) -> Option<DateTime<Utc>> {
    DateTime::from_timestamp_micros(us)
}

/// A plugin's tag value as the worker expects a driver result: a GOOD value without a
/// representable value is bad_decode.
pub fn tag_value(tv: &proto::TagValue) -> TagValue {
    let ts = tv.source_ts_us.and_then(timestamp);
    let q = quality(tv.quality);
    match (q, tv.value.as_ref().and_then(value)) {
        (Quality::Good, Some(v)) => TagValue::new(Some(v), Quality::Good, ts),
        (Quality::Good, None) => TagValue::new(None, Quality::BadDecode, ts),
        (q, _) => TagValue::new(None, q, ts),
    }
}

pub fn tag_values(values: &HashMap<String, proto::TagValue>) -> HashMap<String, TagValue> {
    values.iter().map(|(k, v)| (k.clone(), tag_value(v))).collect()
}

pub fn proto_tag_value(tv: &TagValue) -> proto::TagValue {
    proto::TagValue {
        value: tv.value.as_ref().map(variant),
        quality: quality_code(tv.quality) as i32,
        source_ts_us: tv.source_ts.map(|t| t.timestamp_micros()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn values_round_trip() {
        for v in [
            Value::Bool(true),
            Value::Int(i64::MIN),
            Value::UInt(u64::MAX),
            Value::Float(-1.5),
            Value::Str("ä".into()),
            Value::List(vec![Value::Int(1), Value::List(vec![Value::Bool(false)])]),
        ] {
            assert_eq!(value(&variant(&v)), Some(v));
        }
        assert_eq!(value(&proto::Variant { kind: Some(V::UintValue(5)) }), Some(Value::Int(5)), "fits i64");
        assert_eq!(value(&proto::Variant { kind: None }), None);
    }

    #[test]
    fn tag_values_follow_the_driver_contract() {
        let ts = Utc.with_ymd_and_hms(2026, 11, 16, 10, 0, 0).unwrap() + chrono::Duration::microseconds(123_456);
        let good = TagValue::good(Value::Float(2.5), ts);
        assert_eq!(tag_value(&proto_tag_value(&good)), good);
        let empty = proto::TagValue { value: None, quality: proto::Quality::Good as i32, source_ts_us: None };
        assert_eq!(tag_value(&empty), TagValue::bad(Quality::BadDecode, None));
        let unknown = proto::TagValue { value: None, quality: 99, source_ts_us: Some(ts.timestamp_micros()) };
        assert_eq!(tag_value(&unknown), TagValue::bad(Quality::BadDecode, Some(ts)), "unknown quality");
        let stale = proto::TagValue { quality: proto::Quality::Stale as i32, ..proto_tag_value(&good) };
        assert_eq!(tag_value(&stale), TagValue::bad(Quality::Stale, Some(ts)), "no value unless good");
    }

    #[test]
    fn errors_keep_their_class() {
        let e = |kind: Kind| driver_error(&proto::DriverError { kind: kind as i32, message: "m".into() });
        assert_eq!(e(Kind::Comm), DriverError::Comm("m".into()));
        assert_eq!(e(Kind::ConnectionLost), DriverError::ConnectionLost("m".into()));
        assert_eq!(e(Kind::BlockConfig), DriverError::BlockConfig("m".into()));
        assert_eq!(e(Kind::Unsupported), DriverError::NotImplemented("m".into()));
        assert_eq!(e(Kind::Other), DriverError::Other("m".into()));
        assert_eq!(driver_error(&proto::DriverError { kind: 42, message: "m".into() }), DriverError::Other("m".into()));
    }
}
