//! `Device.params` of an `ab_df1` device (Python `Df1Params`).

use serde_json::{Map, Value as Json};

use opencap_core::pyfmt::{py_repr_json, py_repr_str};

use crate::frame::Check;
use crate::request::{FNC_TYPED_READ_2, FNC_TYPED_READ_3};

pub const MAX_STATION: i64 = 254;
pub const DEFAULT_ACK_TIMEOUT_MS: i64 = 1000;

fn int(p: &Map<String, Json>, key: &str, default: i64, lo: i64, hi: i64) -> Result<i64, String> {
    let bad = |v: &Json| format!("ab_df1 param {key}={}: expected an integer", py_repr_json(v));
    let value = match p.get(key) {
        None | Some(Json::Null) => return Ok(default),
        Some(v @ Json::Bool(_)) => return Err(bad(v)),
        Some(v @ Json::Number(n)) => n.as_i64().ok_or_else(|| bad(v))?,
        Some(v @ Json::String(s)) => s.trim().parse::<i64>().map_err(|_| bad(v))?,
        Some(v) => return Err(bad(v)),
    };
    if !(lo..=hi).contains(&value) {
        return Err(format!("ab_df1 param {key}={value}: must be {lo}..{hi}"));
    }
    Ok(value)
}

fn boolean(p: &Map<String, Json>, key: &str, default: bool) -> Result<bool, String> {
    let bad = |v: &Json| format!("ab_df1 param {key}={}: expected true or false", py_repr_json(v));
    match p.get(key) {
        None | Some(Json::Null) => Ok(default),
        Some(Json::Bool(b)) => Ok(*b),
        Some(v) => {
            if let Some(n) = v.as_i64().filter(|n| *n == 0 || *n == 1) {
                return Ok(n == 1);
            }
            let text = match v {
                Json::String(s) => s.trim().to_lowercase(),
                other => py_repr_json(other).to_lowercase(),
            };
            match text.as_str() {
                "true" | "yes" | "on" | "1" => Ok(true),
                "false" | "no" | "off" | "0" => Ok(false),
                _ => Err(bad(v)),
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Df1Params {
    /// destination station (KEPServer device id)
    pub station: u8,
    /// our station number on the link
    pub src_station: u8,
    pub error_check: Check,
    /// `None` = auto: room for every ENQ retry
    pub ack_timeout_ms: Option<i64>,
    pub nak_retries: u32,
    pub enq_retries: u32,
    pub read_fnc: u8,
    pub duplicate_detect: bool,
    pub close_on_timeout: bool,
}

impl Df1Params {
    pub fn from_map(p: &Map<String, Json>) -> Result<Self, String> {
        let check_raw = match p.get("error_check") {
            None | Some(Json::Null) => "crc".to_string(),
            Some(Json::String(s)) if s.is_empty() => "crc".to_string(),
            Some(Json::String(s)) => s.trim().to_lowercase(),
            Some(other) => py_repr_json(other).to_lowercase(),
        };
        let error_check = Check::parse(&check_raw).map_err(|_| {
            format!(
                "ab_df1 param error_check={}: must be crc or bcc",
                py_repr_json(p.get("error_check").unwrap_or(&Json::Null))
            )
        })?;
        let fnc_raw = p.get("read_fnc");
        let fnc_text = match fnc_raw {
            None | Some(Json::Null) => "a2".to_string(),
            Some(Json::String(s)) => s.trim().to_lowercase(),
            Some(other) => py_repr_json(other).to_lowercase(),
        };
        let read_fnc = match fnc_text.as_str() {
            "a2" | "0xa2" | "162" => FNC_TYPED_READ_3,
            "a1" | "0xa1" | "161" => FNC_TYPED_READ_2,
            _ => {
                return Err(format!(
                    "ab_df1 param read_fnc={}: must be a2 or a1",
                    py_repr_json(fnc_raw.unwrap_or(&Json::Null))
                ))
            }
        };
        let ack = match p.get("ack_timeout_ms") {
            None | Some(Json::Null) => None,
            Some(_) => Some(int(p, "ack_timeout_ms", 1000, 1, 60_000)?),
        };
        let _ = py_repr_str;
        Ok(Self {
            station: int(p, "station", 1, 0, MAX_STATION)? as u8,
            src_station: int(p, "src_station", 0, 0, MAX_STATION)? as u8,
            error_check,
            ack_timeout_ms: ack,
            nak_retries: int(p, "nak_retries", 3, 0, 20)? as u32,
            enq_retries: int(p, "enq_retries", 3, 0, 20)? as u32,
            read_fnc,
            duplicate_detect: boolean(p, "duplicate_detect", true)?,
            close_on_timeout: boolean(p, "close_on_timeout", false)?,
        })
    }

    /// `ack_timeout_ms` if set, else `timeout_ms / (enq_retries + 1)` capped at 1000 ms: one wait
    /// for the ACK plus one per ENQ must fit in the attempt, or the ENQ retries never happen.
    pub fn effective_ack_timeout_ms(&self, timeout_ms: i64) -> i64 {
        if let Some(ms) = self.ack_timeout_ms {
            return ms;
        }
        (timeout_ms.max(1) / (i64::from(self.enq_retries) + 1)).clamp(1, DEFAULT_ACK_TIMEOUT_MS)
    }
}

/// Load-time check: an explicit `ack_timeout_ms` that leaves no room for the ENQ retries.
pub fn timing_warning(device: &opencap_core::config::Device) -> Option<String> {
    let p = Df1Params::from_map(&device.params).ok()?;
    let ack = p.ack_timeout_ms?;
    if p.enq_retries == 0 {
        return None;
    }
    let timeout = device.timeout_ms.max(1);
    let retries = i64::from(p.enq_retries);
    if ack * (retries + 1) <= timeout {
        return None;
    }
    let fit = (timeout / ack - 1).clamp(0, retries);
    Some(format!(
        "{}: ack_timeout_ms={ack} with timeout_ms={}: only {fit} of enq_retries={retries} ENQ(s) fit in one attempt, so a lost frame fails early; use ack_timeout_ms <= {} or leave it unset (auto)",
        device.id,
        device.timeout_ms,
        timeout / (retries + 1)
    ))
}
