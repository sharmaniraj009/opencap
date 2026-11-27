//! SubDevice JSON payload (Python `opencap.north.payload`), byte-for-byte identical to the
//! Python engine's for the same input (golden tests: `tests/payload_golden.rs`).
//!
//! ```json
//! {"SubDeviceId":"...","FirmVersion":"opencap-0.1.0","Env":"prod","isSecure":false,
//!  "DataValid":true,"Data":{"FLOW_1":123.4},"DateTime":"2026-11-16 15:45:01","BadTags":["LEVEL_2"]}
//! ```
//!
//! - Key order is fixed; `BadTags` only when `bad_tags_field` is on and the list is not empty.
//! - `Data` holds only `good` tags, in the device's tag order; the rest are listed, sorted,
//!   in `BadTags`. `DataValid` is true only if every enabled tag is good.
//! - Floats are rounded to `float_decimals` decimal places with Python's `round()`; `-0.0`
//!   becomes `0.0`; NaN/Inf makes the tag bad. Floats are written with Python's `repr()`.
//! - `DateTime` is the newest `source_ts` among the tags in `Data`, in `timezone`, as
//!   `%Y-%m-%d %H:%M:%S`; with no good tag it is the current time.
//! - A missing SubDeviceId is JSON `null`.

use std::str::FromStr;

use chrono::{DateTime, Utc};
use chrono_tz::Tz;

use opencap_core::config::{slug, Device, PayloadCfg};
use opencap_core::health::HealthStatus;
use opencap_core::pyfmt::{py_float_repr, py_round, write_json_str};
use opencap_core::store::Snapshot;
use opencap_core::value::{Quality, Value};
use opencap_core::FIRM_VERSION;

pub const DATETIME_FORMAT: &str = "%Y-%m-%d %H:%M:%S";

/// JSON-ready value, or `None` when the value cannot be published (non-finite float, no value).
pub fn clean_value(value: Option<&Value>, decimals: i64) -> Option<Value> {
    match value? {
        Value::Bool(b) => Some(Value::Bool(*b)),
        Value::Int(i) => Some(Value::Int(*i)),
        Value::UInt(u) => Some(Value::UInt(*u)),
        Value::Float(f) => {
            if !f.is_finite() {
                return None;
            }
            let r = py_round(*f, decimals);
            Some(Value::Float(if r == 0.0 { 0.0 } else { r })) // no "-0.0" in the payload
        }
        Value::Str(s) => Some(Value::Str(s.clone())),
        Value::List(items) => {
            items.iter().map(|v| clean_value(Some(v), decimals)).collect::<Option<Vec<_>>>().map(Value::List)
        }
    }
}

pub fn zone(name: &str) -> Result<Tz, String> {
    Tz::from_str(name).map_err(|_| format!("No time zone found with key {name}"))
}

pub fn format_datetime(ts: DateTime<Utc>, tz_name: &str) -> Result<String, String> {
    Ok(ts.with_timezone(&zone(tz_name)?).format(DATETIME_FORMAT).to_string())
}

#[derive(Debug, Clone, PartialEq)]
pub struct Payload {
    pub sub_device_id: Option<String>,
    pub firm_version: String,
    pub env: String,
    pub is_secure: bool,
    pub data_valid: bool,
    pub data: Vec<(String, Value)>,
    pub date_time: String,
    pub bad_tags: Option<Vec<String>>,
    /// The device's `_System.*` / `_Statistics.*` tags, after the I/O tags in `Data`, keyed by
    /// their device-relative path; `None` is JSON `null` (`include_system_tags`, D-103). They
    /// never reach `BadTags`, `DataValid` or `DateTime`.
    pub system: Vec<(String, Option<Value>)>,
}

/// Build the message for one device from a store snapshot. `now` is only used when no
/// tag is good (tests pass it for a fixed output). Errors only for an unknown timezone.
pub fn build_payload(
    device: &Device,
    values: &Snapshot,
    cfg: &PayloadCfg,
    is_secure: bool,
    now: Option<DateTime<Utc>>,
) -> Result<Payload, String> {
    let mut data = Vec::new();
    let mut bad = Vec::new();
    let mut newest: Option<DateTime<Utc>> = None;
    for tag in device.tags.iter().filter(|t| t.enabled) {
        let Some(tv) = values.get(&tag.name).filter(|tv| tv.quality == Quality::Good) else {
            bad.push(tag.name.clone());
            continue;
        };
        let Some(v) = clean_value(tv.value.as_ref(), cfg.float_decimals) else {
            bad.push(tag.name.clone());
            continue;
        };
        data.push((tag.name.clone(), v));
        if let Some(ts) = tv.source_ts {
            if newest.is_none_or(|n| ts > n) {
                newest = Some(ts);
            }
        }
    }
    let when = newest.or(now).unwrap_or_else(Utc::now);
    let pub_ = &device.publish;
    bad.sort();
    Ok(Payload {
        sub_device_id: pub_.sub_device_id.clone(),
        firm_version: FIRM_VERSION.to_string(),
        env: pub_.env.clone(),
        is_secure,
        data_valid: bad.is_empty(),
        data,
        date_time: format_datetime(when, &cfg.timezone)?,
        bad_tags: (cfg.bad_tags_field && !bad.is_empty()).then_some(bad),
        system: Vec::new(),
    })
}

/// The device's system and statistics tags as payload entries, in catalogue order
/// (`_System._State`, ..., `_Statistics._SuccessfulReads`, ...). Empty unless the device has
/// `publish.include_system_tags`. `system` and `statistics` are the two folder snapshots.
pub fn system_entries(device: &Device, system: &Snapshot, statistics: &Snapshot) -> Vec<(String, Option<Value>)> {
    if !device.publish.include_system_tags {
        return Vec::new();
    }
    let part = |prefix: &str, snap: &Snapshot| -> Vec<(String, Option<Value>)> {
        snap.iter().map(|(name, tv)| (format!("{prefix}.{name}"), tv.value.clone())).collect()
    };
    let mut out = part("_System", system);
    out.extend(part("_Statistics", statistics));
    out
}

/// Append a value as Python's `json.dumps` writes it.
pub fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => out.push_str(&i.to_string()),
        Value::UInt(u) => out.push_str(&u.to_string()),
        Value::Float(f) => out.push_str(&py_float_repr(*f)),
        Value::Str(s) => write_json_str(out, s),
        Value::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
    }
}

fn write_opt_str(out: &mut String, s: Option<&str>) {
    match s {
        Some(s) => write_json_str(out, s),
        None => out.push_str("null"),
    }
}

/// Compact UTF-8 JSON, key order kept: `json.dumps(p, separators=(",", ":"), ensure_ascii=False)`.
pub fn encode(p: &Payload) -> Vec<u8> {
    let mut out = String::with_capacity(64 + p.data.len() * 24);
    out.push_str("{\"SubDeviceId\":");
    write_opt_str(&mut out, p.sub_device_id.as_deref());
    out.push_str(",\"FirmVersion\":");
    write_json_str(&mut out, &p.firm_version);
    out.push_str(",\"Env\":");
    write_json_str(&mut out, &p.env);
    out.push_str(",\"isSecure\":");
    out.push_str(if p.is_secure { "true" } else { "false" });
    out.push_str(",\"DataValid\":");
    out.push_str(if p.data_valid { "true" } else { "false" });
    out.push_str(",\"Data\":{");
    for (i, (name, v)) in p.data.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        write_json_str(&mut out, name);
        out.push(':');
        write_value(&mut out, v);
    }
    for (i, (name, v)) in p.system.iter().enumerate() {
        if i > 0 || !p.data.is_empty() {
            out.push(',');
        }
        write_json_str(&mut out, name);
        out.push(':');
        match v {
            Some(v) => write_value(&mut out, v),
            None => out.push_str("null"),
        }
    }
    out.push_str("},\"DateTime\":");
    write_json_str(&mut out, &p.date_time);
    if let Some(bad) = &p.bad_tags {
        out.push_str(",\"BadTags\":[");
        for (i, name) in bad.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            write_json_str(&mut out, name);
        }
        out.push(']');
    }
    out.push('}');
    out.into_bytes()
}

/// `<prefix>/<slug(channel)>/<slug(device)>/status`; `prefix` is `mqtt.topic_prefix`.
pub fn status_topic(device: &Device, prefix: &str) -> String {
    format!("{prefix}/{}/{}/status", slug(&device.channel), slug(&device.name))
}

/// Status topic body `{"state","last_good","errors"}`.
pub fn encode_status(status: &HealthStatus) -> Vec<u8> {
    let mut out = String::from("{\"state\":");
    write_json_str(&mut out, status.state.as_str());
    out.push_str(",\"last_good\":");
    write_opt_str(&mut out, status.last_good.as_deref());
    out.push_str(&format!(",\"errors\":{}}}", status.errors));
    out.into_bytes()
}

#[cfg(test)]
#[allow(clippy::excessive_precision)] // float32 values widened to f64, written out exactly
mod tests {
    use super::*;
    use chrono::{Duration, TimeZone};
    use opencap_core::config::{DType, PublishCfg, Tag};
    use opencap_core::health::State;
    use opencap_core::value::TagValue;

    fn t0() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 16, 10, 15, 1).unwrap() // 15:45:01 IST
    }

    fn dev(tags: Vec<Tag>, env: &str) -> Device {
        let mut d = Device::new("Site 001/8 PUMP", "Site 001", "8 PUMP", "modbus_tcp", "192.0.2.1", 502, tags);
        d.publish = PublishCfg { sub_device_id: Some("SUB_TEST_PLC".into()), env: env.into(), ..Default::default() };
        d
    }

    fn tag(name: &str, dtype: DType) -> Tag {
        Tag::new(name, "400001", dtype)
    }

    fn snap(v: Vec<(&str, TagValue)>) -> Snapshot {
        v.into_iter().map(|(k, tv)| (k.to_string(), tv)).collect()
    }

    #[test]
    fn system_tags_follow_the_io_tags_and_leave_validity_alone() {
        use opencap_core::store::Snapshot;
        let mut d = dev(vec![tag("A", DType::Int16)], "test");
        let snap = Snapshot::from_pairs(&d.id, [("A", TagValue::good(Value::Int(1), t0()))]);
        let sys = Snapshot::from_pairs(
            "Site 001.8 PUMP._System",
            [
                ("_State", TagValue::good(Value::Str("ok".into()), t0())),
                ("_LastGood", TagValue::new(None, Quality::Good, Some(t0()))),
            ],
        );
        let stats =
            Snapshot::from_pairs("Site 001.8 PUMP._Statistics", [("_Timeouts", TagValue::good(Value::Int(3), t0()))]);
        let cfg = PayloadCfg::default();
        let plain = encode(&build_payload(&d, &snap, &cfg, false, None).unwrap());
        assert!(system_entries(&d, &sys, &stats).is_empty(), "off by default");
        d.publish.include_system_tags = true;
        let mut p = build_payload(&d, &snap, &cfg, false, None).unwrap();
        assert_eq!(encode(&p), plain, "no entries yet: byte-identical");
        p.system = system_entries(&d, &sys, &stats);
        let text = String::from_utf8(encode(&p)).unwrap();
        assert!(
            text.contains(
                "\"Data\":{\"A\":1,\"_System._State\":\"ok\",\"_System._LastGood\":null,\"_Statistics._Timeouts\":3}"
            ),
            "{text}"
        );
        assert!(text.contains("\"DataValid\":true") && !text.contains("BadTags"));
        p.data.clear();
        assert!(String::from_utf8(encode(&p)).unwrap().contains("\"Data\":{\"_System._State\""));
    }

    #[test]
    fn exact_json_for_known_input() {
        let d = dev(
            vec![
                tag("FLOW_1", DType::Float32),
                tag("LEVEL_1", DType::Float32),
                tag("PUMP_1_RUN", DType::Bool),
                tag("TOTAL", DType::Uint32),
                tag("PUMP_1_CURRENT_R", DType::Float32),
                tag("LEVEL_2", DType::Float32),
                Tag { enabled: false, ..tag("SPARE", DType::Float32) },
            ],
            "prod",
        );
        let values = snap(vec![
            ("FLOW_1", TagValue::good(Value::Float(123.40000152587891), t0() - Duration::seconds(3))),
            ("LEVEL_1", TagValue::good(Value::Float(2.5600000381469727), t0())),
            ("PUMP_1_RUN", TagValue::good(Value::Bool(true), t0() - Duration::seconds(1))),
            ("TOTAL", TagValue::good(Value::Int(4000000123), t0() - Duration::seconds(1))),
            ("PUMP_1_CURRENT_R", TagValue::new(Some(Value::Float(41.2)), Quality::BadComm, Some(t0()))),
            ("LEVEL_2", TagValue::bad(Quality::Stale, None)),
            ("SPARE", TagValue::good(Value::Float(1.0), t0())),
        ]);
        let p = build_payload(&d, &values, &PayloadCfg::default(), false, None).unwrap();
        let expected = format!(
            "{{\"SubDeviceId\":\"SUB_TEST_PLC\",\"FirmVersion\":\"{FIRM_VERSION}\",\"Env\":\"prod\",\
             \"isSecure\":false,\"DataValid\":false,\
             \"Data\":{{\"FLOW_1\":123.4,\"LEVEL_1\":2.56,\"PUMP_1_RUN\":true,\"TOTAL\":4000000123}},\
             \"DateTime\":\"2026-11-16 15:45:01\",\"BadTags\":[\"LEVEL_2\",\"PUMP_1_CURRENT_R\"]}}"
        );
        assert_eq!(String::from_utf8(encode(&p)).unwrap(), expected);
    }

    #[test]
    fn datetime_ist_across_midnight_and_fallback() {
        let late = Utc.with_ymd_and_hms(2026, 11, 16, 20, 0, 0).unwrap();
        let d = dev(vec![tag("A", DType::Float32), tag("B", DType::Float32)], "prod");
        let v = snap(vec![
            ("A", TagValue::good(Value::Float(1.0), late - Duration::hours(1))),
            ("B", TagValue::good(Value::Float(2.0), late)),
        ]);
        assert_eq!(
            build_payload(&d, &v, &PayloadCfg::default(), false, None).unwrap().date_time,
            "2026-11-17 01:30:00"
        );
        let edge = |h, m, s| Utc.with_ymd_and_hms(2026, 12, 31, h, m, s).unwrap();
        assert_eq!(format_datetime(edge(18, 29, 59), "Asia/Kolkata").unwrap(), "2026-12-31 23:59:59");
        assert_eq!(format_datetime(edge(18, 30, 0), "Asia/Kolkata").unwrap(), "2027-01-01 00:00:00");
        let v = snap(vec![("A", TagValue::bad(Quality::Stale, None))]);
        let fixed = Utc.with_ymd_and_hms(2026, 11, 16, 23, 0, 0).unwrap();
        let p = build_payload(&d, &v, &PayloadCfg::default(), false, Some(fixed)).unwrap();
        assert_eq!(p.date_time, "2026-11-17 04:30:00");
        assert!(p.data.is_empty() && !p.data_valid);
        assert_eq!(p.bad_tags, Some(vec!["A".to_string(), "B".to_string()]));
        assert!(build_payload(
            &d,
            &v,
            &PayloadCfg { timezone: "Mars/Olympus".into(), ..Default::default() },
            false,
            None
        )
        .is_err());
    }

    #[test]
    fn value_cleaning() {
        assert_eq!(clean_value(Some(&Value::Float(-0.00004)), 4), Some(Value::Float(0.0)));
        assert_eq!(clean_value(Some(&Value::Float(f64::NAN)), 4), None);
        assert_eq!(clean_value(Some(&Value::List(vec![Value::Float(1.0), Value::Float(f64::INFINITY)])), 4), None);
        assert_eq!(clean_value(None, 4), None);
        let mut out = String::new();
        write_value(&mut out, &clean_value(Some(&Value::Float(1e-5)), 6).unwrap());
        assert_eq!(out, "1e-05");
    }

    #[test]
    fn status_encoding_and_topic() {
        let s = HealthStatus { state: State::Demoted, last_good: Some("2026-11-16T10:15:01.000Z".into()), errors: 3 };
        assert_eq!(
            String::from_utf8(encode_status(&s)).unwrap(),
            "{\"state\":\"demoted\",\"last_good\":\"2026-11-16T10:15:01.000Z\",\"errors\":3}"
        );
        let s = HealthStatus { state: State::Offline, last_good: None, errors: 0 };
        assert_eq!(
            String::from_utf8(encode_status(&s)).unwrap(),
            "{\"state\":\"offline\",\"last_good\":null,\"errors\":0}"
        );
        assert_eq!(status_topic(&dev(vec![], "prod"), "opencap"), "opencap/site_001/8_pump/status");
        assert_eq!(status_topic(&dev(vec![], "prod"), "Acme/plant-1"), "Acme/plant-1/site_001/8_pump/status");
    }
}
