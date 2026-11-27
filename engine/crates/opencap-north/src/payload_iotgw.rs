//! KEPServerEX IoT Gateway JSON, the "standard template" (`publish.format: iotgw_json`, #36,
//! D-072, #170; spec `docs/spec/payload-iotgw-json.md`), byte for byte what the Python engine writes:
//!
//! ```json
//! {"timestamp":1794824101500,"values":[{"id":"Plant A.PLC1.FLOW_1","v":12.5,"q":true,"t":1794824101250}]}
//! ```
//!
//! One entry per enabled tag, in the device's tag order, good or not. `v` is the value cleaned as
//! for the SubDevice payload (`null` for a missing or non-finite one), `q` is true only for a
//! good tag with a value, `t` is the tag's `source_ts` (the message `timestamp` when it has none).
//! Epoch times are UTC milliseconds, floored; `payload.timezone` and `bad_tags_field` do not apply.

use chrono::{DateTime, Utc};

use opencap_core::config::{Device, PayloadCfg};
use opencap_core::pyfmt::write_json_str;
use opencap_core::store::Snapshot;
use opencap_core::value::Quality;

use crate::payload::{clean_value, write_value};

/// Milliseconds since the Unix epoch, floored.
pub fn epoch_ms(ts: DateTime<Utc>) -> i64 {
    ts.timestamp_millis()
}

/// `<channel>.<device>.<tag name>`.
pub fn tag_id(device: &Device, tag_name: &str) -> String {
    format!("{}.{}.{tag_name}", device.channel, device.name)
}

/// The message for one device from a store snapshot, as the UTF-8 bytes to publish. `now` is
/// the publish time.
pub fn build_iotgw(device: &Device, values: &Snapshot, cfg: &PayloadCfg, now: DateTime<Utc>) -> Vec<u8> {
    let when = epoch_ms(now);
    let mut out = String::with_capacity(48 + device.tags.len() * 64);
    out.push_str(&format!("{{\"timestamp\":{when},\"values\":["));
    let mut first = true;
    for tag in device.tags.iter().filter(|t| t.enabled) {
        if !first {
            out.push(',');
        }
        first = false;
        let tv = values.get(&tag.name);
        let cleaned = tv.and_then(|tv| clean_value(tv.value.as_ref(), cfg.float_decimals));
        let good = tv.is_some_and(|tv| tv.quality == Quality::Good) && cleaned.is_some();
        let t = tv.and_then(|tv| tv.source_ts).map_or(when, epoch_ms);
        out.push_str("{\"id\":");
        write_json_str(&mut out, &tag_id(device, &tag.name));
        out.push_str(",\"v\":");
        match &cleaned {
            Some(v) => write_value(&mut out, v),
            None => out.push_str("null"),
        }
        out.push_str(&format!(",\"q\":{good},\"t\":{t}}}"));
    }
    out.push_str("]}");
    out.into_bytes()
}
