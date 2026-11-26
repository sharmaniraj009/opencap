//! Typed view of `Device.params` for s7 (Python `S7Params`), with the same validation order and
//! messages. `null` means "default", `0`/`1` are accepted for `byte_order_big`, unknown keys are
//! ignored.

use serde_json::{Map, Value as Json};

use opencap_core::pyfmt::py_repr_json;

/// S7 response overhead: 12 header + 2 parameter + 4 data item header.
pub const READ_OVERHEAD: usize = 18;
/// Kepware's range; classic S7comm PDUs are 240..960 bytes.
pub const MIN_PDU: i64 = 240;
pub const MAX_PDU: i64 = 960;
pub const DEFAULT_CONNECTION_TYPE: u8 = 3; // Kepware default "PC"

#[derive(Debug, Clone, PartialEq)]
pub struct S7Params {
    pub rack: u8,
    pub slot: u8,
    pub max_pdu: usize,
    /// Remote TSAP connection type: 1 PG, 2 OP, 3 PC / S7 basic.
    pub connection_type: u8,
    pub byte_order_big: bool,
    pub max_gap: usize,
    pub nonnormal_float: String,
}

impl Default for S7Params {
    fn default() -> Self {
        Self {
            rack: 0,
            slot: 1,
            max_pdu: 480,
            connection_type: DEFAULT_CONNECTION_TYPE,
            byte_order_big: true,
            max_gap: 16,
            nonnormal_float: "zero".into(),
        }
    }
}

pub fn connection_type_name(t: u8) -> &'static str {
    match t {
        1 => "PG",
        2 => "OP",
        _ => "PC (S7 basic)",
    }
}

impl S7Params {
    /// Largest block in bytes for the planned PDU.
    pub fn max_span(&self) -> usize {
        self.max_pdu - READ_OVERHEAD
    }

    /// Python `S7Params.from_mapping(device.params)`; the error text is Python's `ValueError`.
    pub fn from_map(params: &Map<String, Json>) -> Result<Self, String> {
        let get = |name: &str| params.get(name).filter(|v| !v.is_null());
        let int = |name: &str, default: i64| -> Result<i64, String> {
            match get(name) {
                None => Ok(default),
                Some(Json::Number(n)) if n.is_i64() || n.is_u64() => Ok(n.as_i64().unwrap_or(i64::MAX)),
                Some(v) => Err(format!("s7 param {name}={}: expected an integer", py_repr_json(v))),
            }
        };
        let rack = int("rack", 0)?;
        let slot = int("slot", 1)?;
        let max_pdu = int("max_pdu", 480)?;
        let max_gap = int("max_gap", 16)?;
        let byte_order_big = match get("byte_order_big") {
            None => true,
            Some(Json::Bool(b)) => *b,
            Some(Json::Number(n)) if n.as_i64() == Some(0) || n.as_i64() == Some(1) => n.as_i64() == Some(1),
            Some(v) => return Err(format!("s7 param byte_order_big={}: expected true or false", py_repr_json(v))),
        };
        if !(0..=7).contains(&rack) {
            return Err(format!("s7 param rack={rack}: must be 0..7"));
        }
        if !(0..=31).contains(&slot) {
            return Err(format!("s7 param slot={slot}: must be 0..31"));
        }
        if !(MIN_PDU..=MAX_PDU).contains(&max_pdu) {
            return Err(format!("s7 param max_pdu={max_pdu}: must be {MIN_PDU}..{MAX_PDU}"));
        }
        if max_gap < 0 {
            return Err(format!("s7 param max_gap={max_gap}: must be >= 0"));
        }
        let nonnormal_float = match get("nonnormal_float") {
            None => "zero".to_string(),
            Some(Json::String(s)) if s == "zero" || s == "unmodified" => s.clone(),
            Some(v) => {
                return Err(format!(
                    "s7 param nonnormal_float={}: expected one of ('zero', 'unmodified')",
                    py_repr_json(v)
                ))
            }
        };
        let connection_type = connection_type(get("link_type"))?;
        Ok(Self {
            rack: rack as u8,
            slot: slot as u8,
            max_pdu: max_pdu as usize,
            connection_type,
            byte_order_big,
            max_gap: max_gap as usize,
            nonnormal_float,
        })
    }
}

/// Kepware Link Type -> S7 connection type (first byte of the remote TSAP), D-050.
fn connection_type(link_type: Option<&Json>) -> Result<u8, String> {
    let bad =
        |v: &Json| format!("s7 param link_type={}: expected 1 (PG), 2 (OP), 3 (PC), a name, or null", py_repr_json(v));
    match link_type {
        None => Ok(DEFAULT_CONNECTION_TYPE),
        Some(v @ Json::String(s)) => {
            let key: String = s.trim().to_uppercase().replace(['-', ' '], "");
            match key.as_str() {
                "PG" => Ok(1),
                "OP" => Ok(2),
                "PC" | "BASIC" | "S7BASIC" => Ok(3),
                _ => Err(bad(v)),
            }
        }
        Some(v @ Json::Number(n)) => match n.as_i64() {
            Some(i @ 1..=3) => Ok(i as u8),
            _ => Err(bad(v)),
        },
        Some(v) => Err(bad(v)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn p(v: Json) -> Result<S7Params, String> {
        S7Params::from_map(v.as_object().unwrap())
    }

    #[test]
    fn defaults_and_forms() {
        assert_eq!(p(json!({})).unwrap(), S7Params::default());
        assert!(!p(json!({"byte_order_big": 0})).unwrap().byte_order_big);
        assert_eq!(p(json!({"link_type": "pg"})).unwrap().connection_type, 1);
        assert_eq!(p(json!({"link_type": "S7-Basic"})).unwrap().connection_type, 3);
        assert_eq!(p(json!({"link_type": 2, "slot": null})).unwrap().connection_type, 2);
    }

    #[test]
    fn refused_with_the_python_text() {
        assert_eq!(p(json!({"rack": 8})).unwrap_err(), "s7 param rack=8: must be 0..7");
        assert_eq!(p(json!({"slot": true})).unwrap_err(), "s7 param slot=True: expected an integer");
        assert_eq!(p(json!({"max_pdu": 100})).unwrap_err(), "s7 param max_pdu=100: must be 240..960");
        assert_eq!(
            p(json!({"link_type": 4})).unwrap_err(),
            "s7 param link_type=4: expected 1 (PG), 2 (OP), 3 (PC), a name, or null"
        );
        assert_eq!(
            p(json!({"nonnormal_float": "nan"})).unwrap_err(),
            "s7 param nonnormal_float='nan': expected one of ('zero', 'unmodified')"
        );
    }
}
