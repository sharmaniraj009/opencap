//! Typed view of `Device.params` for modbus_tcp (Python `ModbusParams`), with the
//! documented defaults (docs/driver_params.md) and the same validation messages.

use serde_json::{Map, Value as Json};

use opencap_core::pyfmt::py_repr_json;

/// Protocol limits per request (Modbus spec).
pub fn protocol_max(fc: u8) -> u32 {
    match fc {
        1 | 2 => 2000,
        _ => 125,
    }
}

pub const BITS_PER_REGISTER: u32 = 16;
pub const NONNORMAL_FLOAT_MODES: [&str; 2] = ["zero", "unmodified"];

const BOOL_PARAMS: [&str; 7] = [
    "zero_based",
    "zero_based_bit",
    "modicon_bit_order",
    "byte_order_big",
    "first_word_low",
    "first_dword_low",
    "close_on_timeout",
];
/// Field order of the Python dataclass (validation runs in this order).
const FIELDS: [&str; 15] = [
    "unit_id",
    "zero_based",
    "zero_based_bit",
    "modicon_bit_order",
    "byte_order_big",
    "first_word_low",
    "first_dword_low",
    "max_coils",
    "max_discrete",
    "max_input",
    "max_holding",
    "max_gap",
    "max_gap_bits",
    "close_on_timeout",
    "nonnormal_float",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModbusParams {
    pub unit_id: i64,
    pub zero_based: bool,
    pub zero_based_bit: bool,
    pub modicon_bit_order: bool,
    pub byte_order_big: bool,
    pub first_word_low: bool,
    pub first_dword_low: bool,
    pub max_coils: i64,
    pub max_discrete: i64,
    pub max_input: i64,
    pub max_holding: i64,
    /// registers
    pub max_gap: i64,
    /// coils/discrete; None = max_gap * 16
    pub max_gap_bits: Option<i64>,
    pub close_on_timeout: bool,
    /// NaN/Inf/denormal floats: "zero" (Kepware default) or "unmodified"
    pub nonnormal_float: String,
}

impl Default for ModbusParams {
    fn default() -> Self {
        Self {
            unit_id: 0,
            zero_based: true,
            zero_based_bit: true,
            modicon_bit_order: false,
            byte_order_big: true,
            first_word_low: true,
            first_dword_low: true,
            max_coils: 64,
            max_discrete: 64,
            max_input: 64,
            max_holding: 64,
            max_gap: 4,
            max_gap_bits: None,
            close_on_timeout: true,
            nonnormal_float: "zero".into(),
        }
    }
}

fn param_err(name: &str, value: &Json, what: &str) -> String {
    format!("modbus_tcp param {name}={}: {what}", py_repr_json(value))
}

impl ModbusParams {
    /// Accept a `Device.params` map. Unknown keys are ignored; a null value means "default".
    pub fn from_map(params: &Map<String, Json>) -> Result<Self, String> {
        let mut p = Self::default();
        for name in FIELDS {
            let Some(value) = params.get(name) else { continue };
            if value.is_null() {
                continue;
            }
            if BOOL_PARAMS.contains(&name) {
                let b = match value {
                    Json::Bool(b) => *b,
                    // hand-written YAML may say 0/1
                    Json::Number(n) if n.as_i64() == Some(0) || n.as_i64() == Some(1) => n.as_i64() == Some(1),
                    _ => return Err(param_err(name, value, "expected true or false")),
                };
                match name {
                    "zero_based" => p.zero_based = b,
                    "zero_based_bit" => p.zero_based_bit = b,
                    "modicon_bit_order" => p.modicon_bit_order = b,
                    "byte_order_big" => p.byte_order_big = b,
                    "first_word_low" => p.first_word_low = b,
                    "first_dword_low" => p.first_dword_low = b,
                    _ => p.close_on_timeout = b,
                }
            } else if name == "nonnormal_float" {
                match value.as_str() {
                    Some(s) if NONNORMAL_FLOAT_MODES.contains(&s) => p.nonnormal_float = s.into(),
                    _ => {
                        return Err(param_err(name, value, "expected one of ('zero', 'unmodified')"));
                    }
                }
            } else {
                let Some(i) = value.as_i64().filter(|_| !value.is_f64()) else {
                    return Err(param_err(name, value, "expected an integer"));
                };
                match name {
                    "unit_id" => p.unit_id = i,
                    "max_coils" => p.max_coils = i,
                    "max_discrete" => p.max_discrete = i,
                    "max_input" => p.max_input = i,
                    "max_holding" => p.max_holding = i,
                    "max_gap" => p.max_gap = i,
                    _ => p.max_gap_bits = Some(i),
                }
            }
        }
        for (name, v) in [
            ("max_coils", p.max_coils),
            ("max_discrete", p.max_discrete),
            ("max_holding", p.max_holding),
            ("max_input", p.max_input),
        ] {
            if v < 1 {
                return Err(format!("modbus_tcp param {name}={v}: must be >= 1"));
            }
        }
        if p.max_gap < 0 {
            return Err(format!("modbus_tcp param max_gap={}: must be >= 0", p.max_gap));
        }
        if let Some(bits) = p.max_gap_bits {
            if bits < 0 {
                return Err(format!("modbus_tcp param max_gap_bits={bits}: must be >= 0"));
            }
        }
        if !(0..=255).contains(&p.unit_id) {
            return Err(format!("modbus_tcp param unit_id={}: must be 0..255", p.unit_id));
        }
        Ok(p)
    }

    pub fn limit_param(fc: u8) -> &'static str {
        match fc {
            1 => "max_coils",
            2 => "max_discrete",
            3 => "max_holding",
            _ => "max_input",
        }
    }

    pub fn limit_value(&self, fc: u8) -> i64 {
        match fc {
            1 => self.max_coils,
            2 => self.max_discrete,
            3 => self.max_holding,
            _ => self.max_input,
        }
    }

    /// Largest request for this function code: device setting, capped at the protocol limit.
    pub fn max_span(&self, fc: u8) -> u32 {
        self.limit_value(fc).min(protocol_max(fc) as i64) as u32
    }

    /// Unused registers (FC03/04) or bits (FC01/02) allowed between merged tags.
    pub fn max_gap_for(&self, fc: u8) -> i64 {
        if fc == 1 || fc == 2 {
            self.max_gap_bits.unwrap_or(self.max_gap * BITS_PER_REGISTER as i64)
        } else {
            self.max_gap
        }
    }
}
