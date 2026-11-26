//! Typed view of `Device.params` for melsec_mc (Python `McParams`), with the same validation
//! order and messages. `null` means "default", `0`/`1` are accepted for the boolean, unknown
//! keys are ignored.

use serde_json::{Map, Value as Json};

use opencap_core::pyfmt::py_repr_json;

pub const PLC_TYPES: [&str; 5] = ["Q", "L", "QnA", "iQ-L", "iQ-R"];
pub const PROTOCOLS: [&str; 2] = ["tcp", "udp"];
pub const FRAMES: [&str; 1] = ["3E"];
pub const NONNORMAL_FLOAT_MODES: [&str; 2] = ["zero", "unmodified"];

/// MC 3E per-request limit: batch read in word units.
pub const MAX_WORDS: i64 = 960;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McParams {
    pub plc_type: String,
    pub protocol: String,
    pub first_word_low: bool,
    pub frame: String,
    pub max_words: usize,
    pub max_gap: usize,
    pub nonnormal_float: String,
    pub network: u8,
    pub pc: u8,
    pub dest_moduleio: u16,
    pub dest_modulesta: u8,
}

impl Default for McParams {
    fn default() -> Self {
        Self {
            plc_type: "Q".into(),
            protocol: "tcp".into(),
            first_word_low: true,
            frame: "3E".into(),
            max_words: MAX_WORDS as usize,
            max_gap: 16,
            nonnormal_float: "zero".into(),
            network: 0,
            pc: 0xFF,
            dest_moduleio: 0x3FF,
            dest_modulesta: 0,
        }
    }
}

/// Python tuple repr of a tuple of strings: `('Q', 'L')`, `('3E',)`.
fn tuple_repr(items: &[&str]) -> String {
    let inner: Vec<String> = items.iter().map(|s| format!("'{s}'")).collect();
    if inner.len() == 1 {
        format!("({},)", inner[0])
    } else {
        format!("({})", inner.join(", "))
    }
}

fn string_param(params: &Map<String, Json>, name: &str, allowed: &[&str], default: &str) -> Result<String, String> {
    match params.get(name) {
        None | Some(Json::Null) => Ok(default.to_string()),
        Some(Json::String(s)) if allowed.contains(&s.as_str()) => Ok(s.clone()),
        Some(v) => Err(format!("melsec_mc param {name}={}: expected one of {}", py_repr_json(v), tuple_repr(allowed))),
    }
}

fn int_param(params: &Map<String, Json>, name: &str, lo: i64, hi: i64, default: i64) -> Result<i64, String> {
    match params.get(name) {
        None | Some(Json::Null) => Ok(default),
        Some(Json::Number(n)) if n.as_i64().is_some_and(|i| (lo..=hi).contains(&i)) => Ok(n.as_i64().unwrap()),
        Some(v) => Err(format!("melsec_mc param {name}={}: expected an integer {lo}..{hi}", py_repr_json(v))),
    }
}

impl McParams {
    /// Python `McParams.from_mapping(device.params)`; the error text is Python's `ValueError`.
    pub fn from_map(params: &Map<String, Json>) -> Result<Self, String> {
        let plc_type = string_param(params, "plc_type", &PLC_TYPES, "Q")?;
        let protocol = string_param(params, "protocol", &PROTOCOLS, "tcp")?;
        let first_word_low = match params.get("first_word_low") {
            None | Some(Json::Null) => true,
            Some(Json::Bool(b)) => *b,
            Some(Json::Number(n)) if n.as_i64() == Some(0) || n.as_i64() == Some(1) => n.as_i64() == Some(1),
            Some(v) => {
                return Err(format!("melsec_mc param first_word_low={}: expected true or false", py_repr_json(v)))
            }
        };
        let frame = string_param(params, "frame", &FRAMES, "3E")?;
        let max_words = int_param(params, "max_words", 1, MAX_WORDS, MAX_WORDS)?;
        let max_gap = int_param(params, "max_gap", 0, 1_000_000, 16)?;
        let nonnormal_float = string_param(params, "nonnormal_float", &NONNORMAL_FLOAT_MODES, "zero")?;
        let network = int_param(params, "network", 0, 0xFF, 0)?;
        let pc = int_param(params, "pc", 0, 0xFF, 0xFF)?;
        let dest_moduleio = int_param(params, "dest_moduleio", 0, 0xFFFF, 0x3FF)?;
        let dest_modulesta = int_param(params, "dest_modulesta", 0, 0xFF, 0)?;
        Ok(Self {
            plc_type,
            protocol,
            first_word_low,
            frame,
            max_words: max_words as usize,
            max_gap: max_gap as usize,
            nonnormal_float,
            network: network as u8,
            pc: pc as u8,
            dest_moduleio: dest_moduleio as u16,
            dest_modulesta: dest_modulesta as u8,
        })
    }

    /// iQ-R: 4-byte device numbers, 2-byte device codes, subcommand 0002.
    pub fn iqr(&self) -> bool {
        self.plc_type == "iQ-R"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn p(v: Json) -> Result<McParams, String> {
        McParams::from_map(v.as_object().unwrap())
    }

    #[test]
    fn defaults_and_lenient_forms() {
        assert_eq!(p(json!({})).unwrap(), McParams::default());
        assert!(!p(json!({"first_word_low": 0})).unwrap().first_word_low);
        assert_eq!(p(json!({"unknown_key": 1, "plc_type": null})).unwrap().plc_type, "Q");
    }

    #[test]
    fn refused_with_the_python_text() {
        assert_eq!(
            p(json!({"plc_type": "A"})).unwrap_err(),
            "melsec_mc param plc_type='A': expected one of ('Q', 'L', 'QnA', 'iQ-L', 'iQ-R')"
        );
        assert_eq!(p(json!({"frame": "4E"})).unwrap_err(), "melsec_mc param frame='4E': expected one of ('3E',)");
        assert_eq!(
            p(json!({"max_words": 961})).unwrap_err(),
            "melsec_mc param max_words=961: expected an integer 1..960"
        );
        assert_eq!(
            p(json!({"max_words": true})).unwrap_err(),
            "melsec_mc param max_words=True: expected an integer 1..960"
        );
        assert_eq!(
            p(json!({"first_word_low": "yes"})).unwrap_err(),
            "melsec_mc param first_word_low='yes': expected true or false"
        );
    }
}
