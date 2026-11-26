//! Values read from the controller turned into the configured dtype (Python `decode_block`,
//! `ab_pccc.coerce_value`): integers wrap to the dtype's width, `.n` bits are taken from the
//! integer, a missing or unusable value is a per-tag `DecodeError` and nothing else.

use indexmap::IndexMap;

use opencap_core::config::DType;
use opencap_core::pyfmt::py_float_repr;
use opencap_core::value::Value;

use crate::plan::LogixBlock;

/// Per-tag decode failure (Python `DecodeError`); the driver maps it to `bad_decode`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError(pub String);

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Bool(_) => "bool",
        Value::Int(_) | Value::UInt(_) => "int",
        Value::Float(_) => "float",
        Value::Str(_) => "str",
        Value::List(_) => "list",
    }
}

/// An integer (or bool, as in Python) as a 128-bit number.
fn as_int(v: &Value) -> Option<i128> {
    match v {
        Value::Bool(b) => Some(*b as i128),
        Value::Int(i) => Some(*i as i128),
        Value::UInt(u) => Some(*u as i128),
        _ => None,
    }
}

fn int_bits(dtype: DType) -> Option<(u32, bool)> {
    Some(match dtype {
        DType::Int16 => (16, true),
        DType::Uint16 => (16, false),
        DType::Int32 => (32, true),
        DType::Uint32 => (32, false),
        DType::Int64 => (64, true),
        DType::Uint64 => (64, false),
        _ => return None,
    })
}

/// Convert a raw value to the configured dtype (Python `coerce_value`).
pub fn coerce_value(raw: &Value, dtype: DType) -> Result<Value, String> {
    if dtype == DType::String {
        return match raw {
            Value::Str(_) => Ok(raw.clone()),
            other => Err(format!("expected a string, got {}", type_name(other))),
        };
    }
    if matches!(raw, Value::Str(_) | Value::List(_)) {
        return Err(format!("expected a number, got {}", type_name(raw)));
    }
    match dtype {
        DType::Bool => Ok(Value::Bool(match raw {
            Value::Float(f) => *f != 0.0, // NaN is true, as in Python
            other => as_int(other).unwrap_or(0) != 0,
        })),
        DType::Float32 | DType::Float64 => Ok(Value::Float(match raw {
            Value::Float(f) => *f,
            Value::UInt(u) => *u as f64,
            other => as_int(other).unwrap_or(0) as f64,
        })),
        _ => {
            if let Value::Float(f) = raw {
                return Err(format!("float value {} for integer dtype {}", py_float_repr(*f), dtype.as_str()));
            }
            let (bits, signed) = int_bits(dtype).expect("an integer dtype");
            let mut v = as_int(raw).unwrap_or(0) & ((1i128 << bits) - 1);
            if signed && v >= 1i128 << (bits - 1) {
                v -= 1i128 << bits;
            }
            Ok(i64::try_from(v).map(Value::Int).unwrap_or_else(|_| Value::UInt(v as u64)))
        }
    }
}

/// Decode what `plc.read(*names)` returned, aligned with `block.reads`: `values[i]` is the
/// value of name `i`, `None` when the controller gave none (or `values` is `None`). Output is
/// keyed by tag name in name order.
pub fn decode_block(
    block: &LogixBlock,
    values: Option<&[Option<Value>]>,
) -> IndexMap<String, Result<Value, DecodeError>> {
    let seq = values.unwrap_or(&[]);
    let mut out = IndexMap::new();
    for item in &block.items {
        let name = &block.reads[item.index].name;
        let Some(raw) = seq.get(item.index).and_then(|v| v.as_ref()) else {
            out.insert(item.tag.clone(), Err(DecodeError(format!("{name}: no value in reply"))));
            continue;
        };
        let result = (|| {
            let raw = match item.bit {
                Some(bit) => match as_int(raw) {
                    // Python ints are unbounded: a bit above the value's width is its sign bit
                    Some(i) => Value::Bool((i >> bit.min(126)) & 1 == 1),
                    None => return Err(format!("expected an integer for a bit, got {}", type_name(raw))),
                },
                None => raw.clone(),
            };
            coerce_value(&raw, item.dtype)
        })();
        out.insert(item.tag.clone(), result.map_err(|e| DecodeError(format!("{name}: {}: {e}", item.tag))));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integers_wrap_to_the_dtype() {
        assert_eq!(coerce_value(&Value::Int(65535), DType::Int16), Ok(Value::Int(-1)));
        assert_eq!(coerce_value(&Value::Int(-1), DType::Uint64), Ok(Value::UInt(u64::MAX)));
        assert_eq!(coerce_value(&Value::UInt(u64::MAX), DType::Int64), Ok(Value::Int(-1)));
        assert_eq!(coerce_value(&Value::Bool(true), DType::Float32), Ok(Value::Float(1.0)));
        assert!(coerce_value(&Value::Float(1.5), DType::Int16).unwrap_err().contains("float value 1.5"));
    }
}
