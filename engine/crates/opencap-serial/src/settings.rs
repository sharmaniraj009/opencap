//! Line settings (Python `_serial.SerialSettings`): same defaults, accepted spellings and
//! error messages, so a config means the same to both engines.

use serde_json::{Map, Value as Json};

use opencap_core::pyfmt::py_repr_json;

use crate::py_str;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Parity {
    None,
    Even,
    Odd,
    Mark,
    Space,
}

impl Parity {
    pub fn letter(self) -> char {
        match self {
            Parity::None => 'N',
            Parity::Even => 'E',
            Parity::Odd => 'O',
            Parity::Mark => 'M',
            Parity::Space => 'S',
        }
    }

    /// Python `PARITY_NAMES` (case-insensitive, surrounding blanks ignored).
    pub fn from_name(name: &str) -> Option<Parity> {
        match name.trim().to_uppercase().as_str() {
            "N" | "NONE" => Some(Parity::None),
            "E" | "EVEN" => Some(Parity::Even),
            "O" | "ODD" => Some(Parity::Odd),
            "M" | "MARK" => Some(Parity::Mark),
            "S" | "SPACE" => Some(Parity::Space),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SerialSettings {
    pub baudrate: u32,
    pub bytesize: u8,
    pub parity: Parity,
    /// 1, 1.5 or 2
    pub stopbits: f64,
}

impl Default for SerialSettings {
    fn default() -> Self {
        Self { baudrate: 19200, bytesize: 8, parity: Parity::None, stopbits: 1.0 }
    }
}

fn int_param(params: &Map<String, Json>, key: &str, default: i64) -> Result<i64, String> {
    let err = |v: &Json| format!("serial param {key}={}: expected an integer", py_repr_json(v));
    match params.get(key) {
        None | Some(Json::Null) => Ok(default),
        Some(v @ Json::Number(n)) => n.as_i64().ok_or_else(|| err(v)),
        // Python int(str): blanks around the digits and one sign are allowed
        Some(v @ Json::String(s)) => s.trim().parse::<i64>().map_err(|_| err(v)),
        Some(v) => Err(err(v)),
    }
}

impl SerialSettings {
    /// Python `SerialSettings.from_params` followed by `__post_init__` (same check order).
    pub fn from_params(params: &Map<String, Json>) -> Result<Self, String> {
        let parity = match params.get("parity") {
            None | Some(Json::Null) => Parity::None,
            Some(raw) => Parity::from_name(&py_str(raw)).ok_or_else(|| {
                format!("serial param parity={}: must be none, even, odd, mark or space", py_repr_json(raw))
            })?,
        };
        let stopbits = match params.get("stopbits") {
            None | Some(Json::Null) => 1.0,
            Some(raw) => {
                let bad = || format!("serial param stopbits={}: must be 1, 1.5 or 2", py_repr_json(raw));
                let value = match raw {
                    Json::Number(n) => n.as_f64().ok_or_else(bad)?,
                    Json::String(s) => s.trim().parse::<f64>().map_err(|_| bad())?,
                    _ => return Err(bad()),
                };
                if ![1.0, 1.5, 2.0].contains(&value) {
                    return Err(bad());
                }
                value
            }
        };
        let baudrate = int_param(params, "baudrate", 19200)?;
        let bytesize = int_param(params, "bytesize", 8)?;
        if !(50..=4_000_000).contains(&baudrate) {
            return Err(format!("serial param baudrate={baudrate}: must be 50..4000000"));
        }
        if !(5..=8).contains(&bytesize) {
            return Err(format!("serial param bytesize={bytesize}: must be one of (5, 6, 7, 8)"));
        }
        Ok(Self { baudrate: baudrate as u32, bytesize: bytesize as u8, parity, stopbits })
    }

    /// Seconds per character on the wire: start bit + data bits + parity + stop bits.
    pub fn char_time_s(&self) -> f64 {
        let parity = if self.parity == Parity::None { 0.0 } else { 1.0 };
        (1.0 + f64::from(self.bytesize) + parity + self.stopbits) / f64::from(self.baudrate)
    }

    /// `19200 8N1`, `9600 7E1.5` (Python `describe()`).
    pub fn describe(&self) -> String {
        format!("{} {}{}{}", self.baudrate, self.bytesize, self.parity.letter(), self.stopbits)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn from(v: Json) -> Result<SerialSettings, String> {
        SerialSettings::from_params(v.as_object().unwrap())
    }

    #[test]
    fn defaults_spellings_and_describe() {
        let s = from(json!({})).unwrap();
        assert_eq!(s, SerialSettings::default());
        assert_eq!(s.describe(), "19200 8N1");
        let s = from(json!({"baudrate": "9600", "bytesize": 7, "parity": " even ", "stopbits": "1.5"})).unwrap();
        assert_eq!((s.baudrate, s.bytesize, s.parity, s.stopbits), (9600, 7, Parity::Even, 1.5));
        assert_eq!(s.describe(), "9600 7E1.5");
        assert_eq!(from(json!({"parity": "o", "stopbits": 2})).unwrap().describe(), "19200 8O2");
        assert_eq!(from(json!({"parity": "SPACE"})).unwrap().parity, Parity::Space);
        assert_eq!(from(json!({"parity": null, "stopbits": null})).unwrap(), SerialSettings::default());
    }

    #[test]
    fn char_time_counts_start_parity_and_stop_bits() {
        assert!((from(json!({"baudrate": 9600})).unwrap().char_time_s() - 10.0 / 9600.0).abs() < 1e-12);
        let s = from(json!({"baudrate": 9600, "parity": "E", "stopbits": 2})).unwrap();
        assert!((s.char_time_s() - 12.0 / 9600.0).abs() < 1e-12);
    }

    #[test]
    fn errors_are_pythons() {
        let cases = [
            (json!({"baudrate": 0}), "serial param baudrate=0: must be 50..4000000"),
            (json!({"baudrate": 19200.5}), "serial param baudrate=19200.5: expected an integer"),
            (json!({"baudrate": true}), "serial param baudrate=True: expected an integer"),
            (json!({"baudrate": "fast"}), "serial param baudrate='fast': expected an integer"),
            (json!({"bytesize": 9}), "serial param bytesize=9: must be one of (5, 6, 7, 8)"),
            (json!({"parity": "X"}), "serial param parity='X': must be none, even, odd, mark or space"),
            (json!({"parity": 1}), "serial param parity=1: must be none, even, odd, mark or space"),
            (json!({"stopbits": 3}), "serial param stopbits=3: must be 1, 1.5 or 2"),
            (json!({"stopbits": true}), "serial param stopbits=True: must be 1, 1.5 or 2"),
            (json!({"stopbits": "one"}), "serial param stopbits='one': must be 1, 1.5 or 2"),
            // parity and stop bits are checked before the integers, as in Python
            (json!({"baudrate": 0, "parity": "X"}), "serial param parity='X': must be none, even, odd, mark or space"),
        ];
        for (params, want) in cases {
            assert_eq!(from(params.clone()).unwrap_err(), want, "{params}");
        }
    }
}
