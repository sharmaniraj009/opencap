//! Typed view of `Device.params` for ab_logix (Python `AbLogixDriver.__init__`,
//! `parse_batching`, `connection_path`), with the same validation order and messages.
//! Values keep Python's lenient reading: `micro800` is any truthy value, `slot` goes through
//! `int()`, `max_batch` is `int(x or 40)`.

use serde_json::{Map, Value as Json};

use opencap_core::config::Device;
use opencap_core::pyfmt::py_repr_json;

pub const DEFAULT_PORT: i64 = 44818;
pub const DEFAULT_MAX_BATCH: i64 = 40;
pub const CONNECTION_SIZE_LARGE: usize = 4000; // Large Forward Open
pub const CONNECTION_SIZE_STANDARD: usize = 500; // standard Forward Open; Micro800 default
/// The connection size field of a standard Forward Open is 9 bits.
pub const STANDARD_FORWARD_OPEN_MAX: usize = 511;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Batching {
    Auto,
    On,
    Off,
}

impl Batching {
    /// `params.batching` (Python `parse_batching`): booleans and the strings auto/on/off.
    pub fn parse(value: Option<&Json>) -> Result<Self, String> {
        match value {
            None | Some(Json::Null) => Ok(Batching::Auto),
            Some(Json::Bool(b)) => Ok(if *b { Batching::On } else { Batching::Off }),
            Some(v) => {
                let text = match v {
                    Json::String(s) => s.clone(),
                    other => py_str(other),
                };
                match text.trim().to_lowercase().as_str() {
                    "auto" => Ok(Batching::Auto),
                    "on" => Ok(Batching::On),
                    "off" => Ok(Batching::Off),
                    _ => Err(format!("ab_logix param batching={} must be one of auto, on, off", py_repr_json(v))),
                }
            }
        }
    }
}

/// One hop of a CIP route (port number and link address).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Link {
    Number(u8),
    /// Extended link address, e.g. an IP address on an EtherNet/IP port.
    Text(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hop {
    pub port: u8,
    pub link: Link,
}

#[derive(Debug, Clone)]
pub struct AbParams {
    pub batching: Batching,
    pub micro800: bool,
    /// `connection_size` as configured.
    pub connection_size: Option<usize>,
    pub max_batch: usize,
    /// pycomm3-style path text, `host:port[/hop...]` (log and error messages).
    pub path: String,
    /// Route to the controller; empty for a Micro800.
    pub route: Vec<Hop>,
    pub host: String,
    pub port: u16,
}

/// Python truthiness of a JSON value.
pub fn py_truthy(v: &Json) -> bool {
    match v {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Json::String(s) => !s.is_empty(),
        Json::Array(a) => !a.is_empty(),
        Json::Object(o) => !o.is_empty(),
    }
}

/// Python `str(v)` for a JSON value.
fn py_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        other => py_repr_json(other),
    }
}

/// Python `int(v)`; `None` where Python raises.
pub fn py_int(v: &Json) -> Option<i64> {
    match v {
        Json::Bool(b) => Some(*b as i64),
        Json::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)),
        Json::String(s) => {
            let t = s.trim();
            let digits = t.strip_prefix(['+', '-']).unwrap_or(t);
            let ok = !digits.is_empty()
                && !digits.starts_with('_')
                && !digits.ends_with('_')
                && !digits.contains("__")
                && digits.chars().all(|c| c.is_ascii_digit() || c == '_');
            if ok {
                t.replace('_', "").parse().ok()
            } else {
                None
            }
        }
        _ => None,
    }
}

impl AbParams {
    pub fn from_device(device: &Device) -> Result<Self, String> {
        let params: &Map<String, Json> = &device.params;
        let batching = Batching::parse(params.get("batching"))?;
        let (path, route, host, port) = connection_path(device)?;
        let connection_size = match params.get("connection_size") {
            None | Some(Json::Null) => None,
            Some(Json::Number(n)) if n.as_i64().is_some_and(|i| i >= 1) => Some(n.as_i64().unwrap() as usize),
            Some(other) => {
                return Err(format!(
                    "ab_logix param connection_size={} must be a positive integer",
                    py_repr_json(other)
                ))
            }
        };
        let micro800 = params.get("micro800").is_some_and(py_truthy);
        let max_batch = match params.get("max_batch") {
            Some(v) if py_truthy(v) => py_int(v)
                .ok_or_else(|| format!("ab_logix param max_batch={} must be a positive integer", py_repr_json(v)))?,
            _ => DEFAULT_MAX_BATCH,
        };
        if max_batch < 1 {
            return Err(format!("ab_logix param max_batch={max_batch} must be >= 1"));
        }
        Ok(Self { batching, micro800, connection_size, max_batch: max_batch as usize, path, route, host, port })
    }

    /// The size the planner assumes: configured, else 500 B on a Micro800 and 4000 B on a Logix.
    pub fn plan_connection_size(&self) -> usize {
        self.connection_size.unwrap_or(if self.micro800 { CONNECTION_SIZE_STANDARD } else { CONNECTION_SIZE_LARGE })
    }
}

fn split_route(text: &str) -> Vec<String> {
    text.split([',', '/', '\\']).map(str::trim).filter(|s| !s.is_empty()).map(String::from).collect()
}

fn parse_hop(port: &str, link: &str) -> Result<Hop, String> {
    let port = match port.to_lowercase().as_str() {
        "backplane" | "bp" => 1,
        "enet" => 2,
        p => p.parse::<u8>().ok().filter(|p| (1..=15).contains(p)).ok_or_else(|| format!("bad route port {port:?}"))?,
    };
    let link = match link.parse::<u8>() {
        Ok(n) => Link::Number(n),
        Err(_) if !link.is_empty() => Link::Text(link.to_string()),
        Err(_) => return Err("empty route link".into()),
    };
    Ok(Hop { port, link })
}

/// pycomm3 path of a device (Python `connection_path`): `host:port` for a Micro800,
/// `host:port/<slot>` for a Logix slot, `host:port/<port>/<link>/...` for a longer route in
/// `params.path`. Also the parsed route.
#[allow(clippy::type_complexity)]
fn connection_path(device: &Device) -> Result<(String, Vec<Hop>, String, u16), String> {
    let params = &device.params;
    let host = device.host.trim().to_string();
    if host.is_empty() {
        return Err("ab_logix: empty host".into());
    }
    let port_n = if device.port != 0 { device.port } else { DEFAULT_PORT };
    let base = format!("{host}:{port_n}");
    let port = u16::try_from(port_n).map_err(|_| format!("ab_logix: port {port_n} out of range"))?;
    if params.get("micro800").is_some_and(py_truthy) {
        return Ok((base, vec![], host, port)); // no backplane on a Micro800
    }
    let route_param = params.get("path").filter(|v| py_truthy(v));
    let segs = route_param.map(|r| split_route(&py_str(r))).unwrap_or_default();
    let is_backplane = segs.len() == 2 && matches!(segs[0].to_lowercase().as_str(), "1" | "bp" | "backplane");
    if !segs.is_empty() && !is_backplane {
        if segs.len() % 2 == 1 {
            return Err(format!(
                "ab_logix param path={}: a CIP route needs port/link pairs",
                py_repr_json(route_param.unwrap())
            ));
        }
        let hops = segs
            .chunks(2)
            .map(|p| parse_hop(&p[0], &p[1]))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("ab_logix param path={}: {e}", py_repr_json(route_param.unwrap())))?;
        return Ok((format!("{base}/{}", segs.join("/")), hops, host, port));
    }
    let slot_param = match params.get("slot") {
        None | Some(Json::Null) => segs.get(1).map(|s| Json::String(s.clone())),
        Some(v) => Some(v.clone()),
    };
    let Some(slot) = slot_param else {
        // pycomm3 defaults to backplane slot 0
        return Ok((base, vec![Hop { port: 1, link: Link::Number(0) }], host, port));
    };
    let slot_n =
        py_int(&slot).ok_or_else(|| format!("ab_logix param slot={} must be an integer", py_repr_json(&slot)))?;
    if !(0..=255).contains(&slot_n) {
        return Err(format!("ab_logix param slot={slot_n} out of range 0-255"));
    }
    Ok((format!("{base}/{slot_n}"), vec![Hop { port: 1, link: Link::Number(slot_n as u8) }], host, port))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn dev(params: Json) -> Device {
        let mut d = Device::new("SITE/PLC", "SITE", "PLC", "ab_logix", "192.0.2.1", 44818, vec![]);
        d.params = params.as_object().unwrap().clone();
        d
    }

    #[test]
    fn paths_follow_connection_path() {
        let p = |v| AbParams::from_device(&dev(v)).unwrap();
        assert_eq!(p(json!({})).path, "192.0.2.1:44818");
        assert_eq!(p(json!({"slot": 3})).path, "192.0.2.1:44818/3");
        assert_eq!(p(json!({"slot": "4"})).path, "192.0.2.1:44818/4");
        assert_eq!(p(json!({"path": "1,2"})).path, "192.0.2.1:44818/2");
        assert_eq!(p(json!({"path": "1,0,2,192.0.2.7"})).path, "192.0.2.1:44818/1/0/2/192.0.2.7");
        assert_eq!(p(json!({"micro800": true, "slot": 9})).path, "192.0.2.1:44818");
        assert!(p(json!({"micro800": true})).route.is_empty());
        assert_eq!(p(json!({"slot": 5})).route, vec![Hop { port: 1, link: Link::Number(5) }]);
    }

    #[test]
    fn python_int_forms() {
        assert_eq!(py_int(&json!(" 7 ")), Some(7));
        assert_eq!(py_int(&json!("1_0")), Some(10));
        assert_eq!(py_int(&json!(3.9)), Some(3));
        assert_eq!(py_int(&json!(true)), Some(1));
        assert_eq!(py_int(&json!("x")), None);
        assert_eq!(py_int(&json!("")), None);
    }

    #[test]
    fn max_batch_zero_means_default_negative_is_refused() {
        assert_eq!(AbParams::from_device(&dev(json!({"max_batch": 0}))).unwrap().max_batch, 40);
        assert_eq!(AbParams::from_device(&dev(json!({"max_batch": null}))).unwrap().max_batch, 40);
        assert_eq!(
            AbParams::from_device(&dev(json!({"max_batch": -1}))).unwrap_err(),
            "ab_logix param max_batch=-1 must be >= 1"
        );
    }
}
