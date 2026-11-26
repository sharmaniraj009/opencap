//! opencap tag dtype -> Sparkplug datatype, and tag values <-> the metric `value` oneof
//! (docs/spec/payload-sparkplug.md §3).
//!
//! Integers: the Sparkplug spec gives Int8/Int16/Int32 the protobuf type `uint32` without
//! saying how a negative value is stored, and the two Eclipse Tahu libraries differ (Java
//! sign-extends to 32 bits, Python adds 2^width). opencap writes the value modulo 2^width
//! (Tahu Python), which Tahu Java's decoder (`(short) getIntValue()`) reads back as the same
//! number, and reads either form. UInt32 goes into `int_value`, the spec's field; Tahu Java
//! writes `long_value` and its decoder accepts both, so the decoder here does too.

use opencap_core::value::Value;

use crate::proto::payload::metric::Value as MValue;
use crate::proto::DataType;
use crate::CodecError;

/// The datatype a tag is born with: its dtype, Double when the tag is scaled (a scaled value
/// is always a float, as in the Python engine), the array type when the tag is an array.
pub fn datatype_for(dtype: &str, scaled: bool, array: bool) -> Result<DataType, CodecError> {
    use DataType as D;
    if scaled && matches!(dtype, "bool" | "string") {
        return Err(CodecError::new(format!("a {dtype} tag cannot be scaled")));
    }
    let (scalar, vector) = match (dtype, scaled) {
        (_, true) => (D::Double, D::DoubleArray),
        ("bool", _) => (D::Boolean, D::BooleanArray),
        ("int16", _) => (D::Int16, D::Int16Array),
        ("uint16", _) => (D::UInt16, D::UInt16Array),
        ("int32", _) => (D::Int32, D::Int32Array),
        ("uint32", _) => (D::UInt32, D::UInt32Array),
        ("int64", _) => (D::Int64, D::Int64Array),
        ("uint64", _) => (D::UInt64, D::UInt64Array),
        ("float32", _) => (D::Float, D::FloatArray),
        ("float64", _) => (D::Double, D::DoubleArray),
        ("string", _) => (D::String, D::StringArray),
        _ => return Err(CodecError::new(format!("unknown dtype {dtype:?}"))),
    };
    Ok(if array { vector } else { scalar })
}

fn int_of(v: &Value, dt: DataType) -> Result<i128, CodecError> {
    match v {
        Value::Int(i) => Ok(i128::from(*i)),
        Value::UInt(u) => Ok(i128::from(*u)),
        other => Err(CodecError::new(format!("{} needs an integer, got {}", dt.as_str_name(), other.py_repr()))),
    }
}

fn int_in(v: &Value, dt: DataType, lo: i128, hi: i128) -> Result<i128, CodecError> {
    let x = int_of(v, dt)?;
    if x < lo || x > hi {
        return Err(CodecError::new(format!("{x} is out of range for {}", dt.as_str_name())));
    }
    Ok(x)
}

fn float_of(v: &Value, dt: DataType) -> Result<f64, CodecError> {
    match v {
        Value::Float(f) => Ok(*f),
        Value::Int(i) => Ok(*i as f64),
        Value::UInt(u) => Ok(*u as f64),
        other => Err(CodecError::new(format!("{} needs a number, got {}", dt.as_str_name(), other.py_repr()))),
    }
}

/// The element type of an array datatype.
pub fn element_of(dt: DataType) -> Option<DataType> {
    use DataType as D;
    Some(match dt {
        D::Int8Array => D::Int8,
        D::Int16Array => D::Int16,
        D::Int32Array => D::Int32,
        D::Int64Array => D::Int64,
        D::UInt8Array => D::UInt8,
        D::UInt16Array => D::UInt16,
        D::UInt32Array => D::UInt32,
        D::UInt64Array => D::UInt64,
        D::FloatArray => D::Float,
        D::DoubleArray => D::Double,
        D::BooleanArray => D::Boolean,
        D::StringArray => D::String,
        D::DateTimeArray => D::DateTime,
        _ => return None,
    })
}

/// Encode a tag value for a metric of datatype `dt`.
pub fn encode(dt: DataType, v: &Value) -> Result<MValue, CodecError> {
    use DataType as D;
    let m = |x: i128, bits: u32| (x.rem_euclid(1i128 << bits)) as u64;
    Ok(match dt {
        D::Int8 => MValue::IntValue(m(int_in(v, dt, -128, 127)?, 8) as u32),
        D::Int16 => MValue::IntValue(m(int_in(v, dt, -32_768, 32_767)?, 16) as u32),
        D::Int32 => MValue::IntValue(m(int_in(v, dt, i32::MIN.into(), i32::MAX.into())?, 32) as u32),
        D::Int64 => MValue::LongValue(m(int_in(v, dt, i64::MIN.into(), i64::MAX.into())?, 64)),
        D::UInt8 => MValue::IntValue(int_in(v, dt, 0, 255)? as u32),
        D::UInt16 => MValue::IntValue(int_in(v, dt, 0, 65_535)? as u32),
        D::UInt32 => MValue::IntValue(int_in(v, dt, 0, u32::MAX.into())? as u32),
        D::UInt64 => MValue::LongValue(int_in(v, dt, 0, u64::MAX.into())? as u64),
        D::DateTime => MValue::LongValue(int_in(v, dt, 0, u64::MAX.into())? as u64),
        D::Float => MValue::FloatValue(float_of(v, dt)? as f32),
        D::Double => MValue::DoubleValue(float_of(v, dt)?),
        D::Boolean => match v {
            Value::Bool(b) => MValue::BooleanValue(*b),
            other => return Err(CodecError::new(format!("Boolean needs a bool, got {}", other.py_repr()))),
        },
        D::String | D::Text | D::Uuid => match v {
            Value::Str(s) => MValue::StringValue(s.clone()),
            other => {
                return Err(CodecError::new(format!("{} needs a string, got {}", dt.as_str_name(), other.py_repr())))
            }
        },
        _ if element_of(dt).is_some() => MValue::BytesValue(encode_array(dt, v)?),
        _ => return Err(CodecError::new(format!("opencap does not publish {} metrics", dt.as_str_name()))),
    })
}

/// Packed little-endian bytes of an array value (spec §6.4.16 "Array Types").
fn encode_array(dt: DataType, v: &Value) -> Result<Vec<u8>, CodecError> {
    use DataType as D;
    let Value::List(items) = v else {
        return Err(CodecError::new(format!("{} needs a list, got {}", dt.as_str_name(), v.py_repr())));
    };
    let el = element_of(dt).expect("array datatype");
    let mut out = Vec::new();
    if dt == D::BooleanArray {
        out.extend_from_slice(
            &u32::try_from(items.len()).map_err(|_| CodecError::new("array too long"))?.to_le_bytes(),
        );
        let mut bits = vec![0u8; items.len().div_ceil(8)];
        for (i, item) in items.iter().enumerate() {
            match item {
                Value::Bool(true) => bits[i / 8] |= 0x80 >> (i % 8), // first value = MSB
                Value::Bool(false) => {}
                other => return Err(CodecError::new(format!("BooleanArray needs bools, got {}", other.py_repr()))),
            }
        }
        out.extend_from_slice(&bits);
        return Ok(out);
    }
    for item in items {
        match (el, encode(el, item)?) {
            (D::String, MValue::StringValue(s)) => {
                if s.contains('\0') {
                    return Err(CodecError::new("a StringArray element cannot contain NUL"));
                }
                out.extend_from_slice(s.as_bytes());
                out.push(0);
            }
            (D::Int8 | D::UInt8, MValue::IntValue(x)) => out.push(x as u8),
            (D::Int16 | D::UInt16, MValue::IntValue(x)) => out.extend_from_slice(&(x as u16).to_le_bytes()),
            (D::Int32 | D::UInt32, MValue::IntValue(x)) => out.extend_from_slice(&x.to_le_bytes()),
            (_, MValue::LongValue(x)) => out.extend_from_slice(&x.to_le_bytes()),
            (_, MValue::FloatValue(x)) => out.extend_from_slice(&x.to_le_bytes()),
            (_, MValue::DoubleValue(x)) => out.extend_from_slice(&x.to_le_bytes()),
            (_, other) => unreachable!("{el:?} element encoded as {other:?}"),
        }
    }
    Ok(out)
}

/// A signed value stored in `int_value`: modulo 2^bits, or sign-extended to 32 bits (Tahu Java).
fn signed_from_u32(x: u32, bits: u32, dt: DataType) -> Result<i64, CodecError> {
    let half = 1u64 << (bits - 1);
    let x64 = u64::from(x);
    if bits == 32 {
        return Ok(i64::from(x as i32));
    }
    if x64 < (1 << bits) {
        Ok(if x64 >= half { x64 as i64 - (1i64 << bits) } else { x64 as i64 })
    } else if x64 >= (1u64 << 32) - half {
        Ok(i64::from(x as i32))
    } else {
        Err(CodecError::new(format!("{x} is not a valid {} value", dt.as_str_name())))
    }
}

/// Decode a metric value of datatype `dt` back to a tag value.
pub fn decode(dt: DataType, v: &MValue) -> Result<Value, CodecError> {
    use DataType as D;
    let bad = || CodecError::new(format!("{} metric with a {} value field", dt.as_str_name(), field_name(v)));
    let unsigned = |x: u64, max: u64| {
        if x <= max {
            Ok(Value::from_u64(x))
        } else {
            Err(CodecError::new(format!("{x} is out of range for {}", dt.as_str_name())))
        }
    };
    Ok(match (dt, v) {
        (D::Int8, MValue::IntValue(x)) => Value::Int(signed_from_u32(*x, 8, dt)?),
        (D::Int16, MValue::IntValue(x)) => Value::Int(signed_from_u32(*x, 16, dt)?),
        (D::Int32, MValue::IntValue(x)) => Value::Int(signed_from_u32(*x, 32, dt)?),
        (D::Int64, MValue::LongValue(x)) => Value::Int(*x as i64),
        (D::UInt8, MValue::IntValue(x)) => unsigned(u64::from(*x), 255)?,
        (D::UInt16, MValue::IntValue(x)) => unsigned(u64::from(*x), 65_535)?,
        (D::UInt32, MValue::IntValue(x)) => Value::Int(i64::from(*x)),
        (D::UInt32, MValue::LongValue(x)) => unsigned(*x, u64::from(u32::MAX))?,
        (D::UInt64 | D::DateTime, MValue::LongValue(x)) => Value::from_u64(*x),
        (D::Float, MValue::FloatValue(x)) => Value::Float(f64::from(*x)),
        (D::Double, MValue::DoubleValue(x)) => Value::Float(*x),
        (D::Boolean, MValue::BooleanValue(b)) => Value::Bool(*b),
        (D::String | D::Text | D::Uuid, MValue::StringValue(s)) => Value::Str(s.clone()),
        (_, MValue::BytesValue(b)) if element_of(dt).is_some() => decode_array(dt, b)?,
        _ => return Err(bad()),
    })
}

fn decode_array(dt: DataType, b: &[u8]) -> Result<Value, CodecError> {
    use DataType as D;
    let el = element_of(dt).expect("array datatype");
    let short = || CodecError::new(format!("{} of {} bytes is malformed", dt.as_str_name(), b.len()));
    if dt == D::BooleanArray {
        let count = u32::from_le_bytes(b.get(..4).ok_or_else(short)?.try_into().unwrap()) as usize;
        let bits = &b[4..];
        if bits.len() != count.div_ceil(8) {
            return Err(short());
        }
        return Ok(Value::List((0..count).map(|i| Value::Bool(bits[i / 8] & (0x80 >> (i % 8)) != 0)).collect()));
    }
    if dt == D::StringArray {
        if b.is_empty() {
            return Ok(Value::List(vec![]));
        }
        let Some(body) = b.strip_suffix(&[0]) else { return Err(short()) };
        return body
            .split(|c| *c == 0)
            .map(|s| String::from_utf8(s.to_vec()).map(Value::Str).map_err(|_| short()))
            .collect::<Result<Vec<_>, _>>()
            .map(Value::List);
    }
    let size = match el {
        D::Int8 | D::UInt8 => 1,
        D::Int16 | D::UInt16 => 2,
        D::Int32 | D::UInt32 | D::Float => 4,
        _ => 8,
    };
    if b.len() % size != 0 {
        return Err(short());
    }
    let items = b.chunks_exact(size).map(|c| {
        let mut w = [0u8; 8];
        w[..size].copy_from_slice(c);
        let raw = u64::from_le_bytes(w);
        match el {
            D::Int8 => Value::Int(i64::from(raw as u8 as i8)),
            D::Int16 => Value::Int(i64::from(raw as u16 as i16)),
            D::Int32 => Value::Int(i64::from(raw as u32 as i32)),
            D::Int64 => Value::Int(raw as i64),
            D::Float => Value::Float(f64::from(f32::from_bits(raw as u32))),
            D::Double => Value::Float(f64::from_bits(raw)),
            _ => Value::from_u64(raw), // unsigned and DateTime
        }
    });
    Ok(Value::List(items.collect()))
}

/// The protobuf field name of a metric value (for messages and the conformance vectors).
pub fn field_name(v: &MValue) -> &'static str {
    match v {
        MValue::IntValue(_) => "int_value",
        MValue::LongValue(_) => "long_value",
        MValue::FloatValue(_) => "float_value",
        MValue::DoubleValue(_) => "double_value",
        MValue::BooleanValue(_) => "boolean_value",
        MValue::StringValue(_) => "string_value",
        MValue::BytesValue(_) => "bytes_value",
        MValue::DatasetValue(_) => "dataset_value",
        MValue::TemplateValue(_) => "template_value",
        MValue::ExtensionValue(_) => "extension_value",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use DataType as D;

    fn rt(dt: DataType, v: Value) -> Value {
        decode(dt, &encode(dt, &v).unwrap()).unwrap()
    }

    #[test]
    fn negative_narrow_ints_are_stored_modulo_their_width_and_read_both_ways() {
        assert_eq!(encode(D::Int16, &Value::Int(-2)).unwrap(), MValue::IntValue(65_534));
        assert_eq!(encode(D::Int8, &Value::Int(-1)).unwrap(), MValue::IntValue(255));
        assert_eq!(encode(D::Int32, &Value::Int(-1)).unwrap(), MValue::IntValue(u32::MAX));
        assert_eq!(encode(D::Int64, &Value::Int(-1)).unwrap(), MValue::LongValue(u64::MAX));
        // Tahu Java's sign-extended form decodes to the same value
        assert_eq!(decode(D::Int16, &MValue::IntValue(0xFFFF_FFFE)).unwrap(), Value::Int(-2));
        assert_eq!(decode(D::Int8, &MValue::IntValue(0xFFFF_FF80)).unwrap(), Value::Int(-128));
        assert!(decode(D::Int16, &MValue::IntValue(0x0001_0000)).is_err());
        for (dt, v) in [(D::Int16, -32_768), (D::Int16, 32_767), (D::Int32, i64::from(i32::MIN)), (D::Int64, i64::MIN)]
        {
            assert_eq!(rt(dt, Value::Int(v)), Value::Int(v), "{dt:?} {v}");
        }
    }

    #[test]
    fn uint32_uses_int_value_and_reads_long_value_too() {
        assert_eq!(encode(D::UInt32, &Value::Int(4_000_000_000)).unwrap(), MValue::IntValue(4_000_000_000));
        assert_eq!(decode(D::UInt32, &MValue::LongValue(7)).unwrap(), Value::Int(7));
        assert_eq!(rt(D::UInt64, Value::UInt(u64::MAX)), Value::UInt(u64::MAX));
    }

    #[test]
    fn out_of_range_and_wrong_types_are_errors() {
        assert!(encode(D::Int16, &Value::Int(40_000)).is_err());
        assert!(encode(D::UInt16, &Value::Int(-1)).is_err());
        assert!(encode(D::Boolean, &Value::Int(1)).is_err());
        assert!(encode(D::String, &Value::Bool(true)).is_err());
        assert!(encode(D::DataSet, &Value::Int(1)).is_err());
        assert!(decode(D::Boolean, &MValue::IntValue(1)).is_err());
    }

    #[test]
    fn arrays_match_the_spec_examples() {
        let ints = |v: &[i64]| Value::List(v.iter().map(|i| Value::Int(*i)).collect());
        let bytes = |dt, v: Value| match encode(dt, &v).unwrap() {
            MValue::BytesValue(b) => b,
            other => panic!("{other:?}"),
        };
        assert_eq!(bytes(D::Int16Array, ints(&[-30_000, 30_000])), [0xD0, 0x8A, 0x30, 0x75]);
        assert_eq!(bytes(D::Int32Array, ints(&[-1, 315_338_746])), [0xFF, 0xFF, 0xFF, 0xFF, 0xFA, 0xAF, 0xCB, 0x12]);
        assert_eq!(bytes(D::UInt16Array, ints(&[30, 52_360])), [0x1E, 0x00, 0x88, 0xCC]);
        let bools = [false, false, true, true, false, true, false, false, true, true, false, true];
        let b = Value::List(bools.iter().map(|b| Value::Bool(*b)).collect());
        assert_eq!(bytes(D::BooleanArray, b.clone()), [0x0C, 0x00, 0x00, 0x00, 0x34, 0xD0]);
        let s = Value::List(vec![Value::Str("ABC".into()), Value::Str("hello".into())]);
        assert_eq!(bytes(D::StringArray, s.clone()), b"ABC\0hello\0");
        for (dt, v) in [(D::BooleanArray, b), (D::StringArray, s), (D::Int16Array, ints(&[-1, 2, 3]))] {
            assert_eq!(rt(dt, v.clone()), v, "{dt:?}");
        }
        let f = Value::List(vec![Value::Float(1.5), Value::Float(-0.25)]);
        assert_eq!(rt(D::FloatArray, f.clone()), f);
    }

    #[test]
    fn dtype_mapping() {
        assert_eq!(datatype_for("int16", false, false).unwrap(), D::Int16);
        assert_eq!(datatype_for("int16", true, false).unwrap(), D::Double);
        assert_eq!(datatype_for("float32", false, true).unwrap(), D::FloatArray);
        assert_eq!(datatype_for("bool", false, true).unwrap(), D::BooleanArray);
        assert!(datatype_for("bool", true, false).is_err());
        assert!(datatype_for("int8", false, false).is_err());
    }
}
