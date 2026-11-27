//! Device time zone (DEV-13, #112): `params.device_timezone` and the one helper that turns a
//! device's local timestamp into the UTC `source_ts` (ADR-0006: timestamps are UTC internally).
//!
//! The setting is an IANA zone name (`Europe/Berlin`) or a fixed offset (`+05:30`, `-0800`,
//! `+02`; at most +-14:00). Absent means the device reports UTC. No driver uses it yet; the
//! first driver that reports local timestamps converts with [`DeviceZone::local_to_utc`].
//!
//! Around a DST change a local time can be missing or twice there:
//! - in the **gap** (clocks jump forward) the time does not exist; it is read with the offset
//!   that was in force before the jump, so it lands on the instant just after the change;
//! - in the **overlap** (clocks go back) it happens twice; the first occurrence (the DST one)
//!   is taken, and the caller is told it was ambiguous.
//!
//! The vectors are `tests/conformance/timezones/device_time/`.

use chrono::{DateTime, FixedOffset, LocalResult, NaiveDateTime, Offset, TimeZone, Utc};
use chrono_tz::Tz;

use crate::pyfmt::py_repr_str;

const MAX_OFFSET_S: i32 = 14 * 3600;

/// How a local time met a DST change.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fold {
    /// An ordinary local time.
    None,
    /// The local time does not exist (spring forward): read with the offset before the jump.
    Gap,
    /// The local time happens twice (fall back): the first occurrence was taken.
    Ambiguous,
}

impl Fold {
    pub fn as_str(&self) -> &'static str {
        match self {
            Fold::None => "none",
            Fold::Gap => "gap",
            Fold::Ambiguous => "ambiguous",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeviceZone {
    Utc,
    Fixed(FixedOffset),
    Named(Tz),
}

/// `+05:30`, `-0800`, `+02`: a sign, hours, optional minutes (with or without a colon).
fn parse_offset(text: &str) -> Option<FixedOffset> {
    let (sign, rest) = match text.as_bytes().first()? {
        b'+' => (1, &text[1..]),
        b'-' => (-1, &text[1..]),
        _ => return None,
    };
    let digits: String = rest.chars().filter(|c| *c != ':').collect();
    let colon_ok = !rest.contains(':') || (rest.len() == 5 && rest.as_bytes()[2] == b':');
    if !colon_ok || !digits.bytes().all(|b| b.is_ascii_digit()) || !(digits.len() == 2 || digits.len() == 4) {
        return None;
    }
    let hours: i32 = digits[..2].parse().ok()?;
    let minutes: i32 = if digits.len() == 4 { digits[2..].parse().ok()? } else { 0 };
    let seconds = hours * 3600 + minutes * 60;
    if minutes > 59 || seconds > MAX_OFFSET_S {
        return None;
    }
    FixedOffset::east_opt(sign * seconds)
}

impl DeviceZone {
    /// Parse an IANA zone name or a fixed offset. The error says what is accepted.
    pub fn parse(text: &str) -> Result<Self, String> {
        if let Some(off) = parse_offset(text) {
            return Ok(DeviceZone::Fixed(off));
        }
        match text.parse::<Tz>() {
            Ok(Tz::UTC) => Ok(DeviceZone::Utc),
            Ok(tz) => Ok(DeviceZone::Named(tz)),
            Err(_) => Err(format!(
                "{} is not an IANA time zone name or a fixed offset (+HH:MM, -HH:MM, up to 14:00)",
                py_repr_str(text)
            )),
        }
    }

    /// The device's local time as the UTC instant, and what DST did to it.
    pub fn local_to_utc(&self, local: NaiveDateTime) -> (DateTime<Utc>, Fold) {
        match self {
            DeviceZone::Utc => (local.and_utc(), Fold::None),
            DeviceZone::Fixed(off) => {
                (off.from_local_datetime(&local).single().expect("a fixed offset is unambiguous").to_utc(), Fold::None)
            }
            DeviceZone::Named(tz) => match tz.from_local_datetime(&local) {
                LocalResult::Single(t) => (t.to_utc(), Fold::None),
                LocalResult::Ambiguous(first, _) => (first.to_utc(), Fold::Ambiguous),
                LocalResult::None => {
                    // read it with the offset in force a day earlier, which is the one before the jump
                    let before = *tz
                        .from_local_datetime(&(local - chrono::Duration::days(1)))
                        .earliest()
                        .expect("a day earlier exists")
                        .offset();
                    let off = before.fix();
                    (off.from_local_datetime(&local).single().expect("fixed").to_utc(), Fold::Gap)
                }
            },
        }
    }
}

/// Validate `params.device_timezone` of a device (`None`: the key is absent).
pub fn check_param(value: Option<&serde_json::Value>) -> Result<Option<DeviceZone>, String> {
    match value {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(s)) => DeviceZone::parse(s).map(Some),
        Some(other) => Err(format!(
            "{} is not an IANA time zone name or a fixed offset (+HH:MM, -HH:MM, up to 14:00)",
            crate::pyfmt::py_repr_json(other)
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn offsets_parse_strictly() {
        for (text, secs) in [("+05:30", 19_800), ("-0800", -28_800), ("+02", 7_200), ("+14:00", 50_400), ("-00:00", 0)]
        {
            assert_eq!(parse_offset(text).map(|o| o.local_minus_utc()), Some(secs), "{text}");
        }
        for text in ["", "+", "05:30", "+5", "+15:00", "+05:60", "+0530:", "+05:3", "UTC+5", "+05 30", "+٠٥"] {
            assert!(parse_offset(text).is_none(), "{text:?}");
        }
    }

    #[test]
    fn a_device_with_a_bad_zone_is_refused_naming_the_key() {
        let mut d = crate::config::Device::new("S/D", "S", "D", "modbus_tcp", "192.0.2.1", 502, Vec::new());
        assert!(d.validate().is_ok());
        d.params.insert("device_timezone".into(), serde_json::json!("Europe/Berlin"));
        assert!(d.validate().is_ok());
        d.params.insert("device_timezone".into(), serde_json::json!("Mars/Olympus"));
        let e = d.validate().unwrap_err();
        assert!(e.starts_with("S/D: params.device_timezone 'Mars/Olympus' is not an IANA"), "{e}");
        d.params.insert("device_timezone".into(), serde_json::json!(3));
        assert!(d.validate().unwrap_err().contains("params.device_timezone 3 is not an IANA"));
    }

    #[test]
    fn names_and_utc() {
        assert_eq!(DeviceZone::parse("UTC"), Ok(DeviceZone::Utc));
        assert!(matches!(DeviceZone::parse("Europe/Berlin"), Ok(DeviceZone::Named(_))));
        assert!(DeviceZone::parse("Mars/Olympus").unwrap_err().contains("'Mars/Olympus' is not an IANA"));
        assert_eq!(check_param(None), Ok(None));
        assert!(check_param(Some(&serde_json::json!(5))).is_err());
    }
}
