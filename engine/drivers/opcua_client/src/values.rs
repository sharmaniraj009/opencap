//! StatusCode -> quality and Variant -> tag value (Python `status_quality`, `decode_value`).

use opcua::types::{DataValue, StatusCode, Variant};

use opencap_core::config::DType;
use opencap_core::value::{Quality, TagValue, Value};

/// "This node, as configured, cannot be read": BadNodeIdUnknown, BadNodeIdInvalid,
/// BadAttributeIdInvalid, BadNotReadable, BadUserAccessDenied.
const CONFIG_STATUS: [u32; 5] = [0x8034_0000, 0x8033_0000, 0x8035_0000, 0x803A_0000, 0x801F_0000];

/// Good* -> good; Uncertain* -> stale (the uncertain value is not stored); the config codes ->
/// bad_config; every other Bad -> bad_comm. The low 16 bits are info bits and are ignored.
pub fn status_quality(code: StatusCode) -> Quality {
    let bits = code.bits();
    match bits >> 30 {
        0 => Quality::Good,
        1 => Quality::Stale,
        _ if CONFIG_STATUS.contains(&(bits & 0xFFFF_0000)) => Quality::BadConfig,
        _ => Quality::BadComm,
    }
}

fn number(v: &Variant) -> Option<f64> {
    Some(match v {
        Variant::Boolean(b) => f64::from(u8::from(*b)),
        Variant::SByte(x) => f64::from(*x),
        Variant::Byte(x) => f64::from(*x),
        Variant::Int16(x) => f64::from(*x),
        Variant::UInt16(x) => f64::from(*x),
        Variant::Int32(x) => f64::from(*x),
        Variant::UInt32(x) => f64::from(*x),
        Variant::Int64(x) => *x as f64,
        Variant::UInt64(x) => *x as f64,
        Variant::Float(x) => f64::from(*x),
        Variant::Double(x) => *x,
        _ => return None,
    })
}

fn integer(v: &Variant) -> Option<i128> {
    Some(match v {
        Variant::Boolean(b) => i128::from(*b),
        Variant::SByte(x) => i128::from(*x),
        Variant::Byte(x) => i128::from(*x),
        Variant::Int16(x) => i128::from(*x),
        Variant::UInt16(x) => i128::from(*x),
        Variant::Int32(x) => i128::from(*x),
        Variant::UInt32(x) => i128::from(*x),
        Variant::Int64(x) => i128::from(*x),
        Variant::UInt64(x) => i128::from(*x),
        _ => return None,
    })
}

/// A Variant as the tag's dtype. `Err` is the decode failure (bad_decode).
pub fn decode_value(dtype: DType, v: &Variant) -> Result<Value, String> {
    if dtype == DType::String {
        return match v {
            Variant::String(s) => Ok(Value::Str(s.value().clone().unwrap_or_default())),
            Variant::LocalizedText(t) => Ok(Value::Str(t.text.value().clone().unwrap_or_default())),
            Variant::QualifiedName(n) => Ok(Value::Str(n.name.value().clone().unwrap_or_default())),
            Variant::ByteString(b) => {
                Ok(Value::Str(b.value.clone().unwrap_or_default().iter().map(|&c| c as char).collect()))
            }
            other => match number(other) {
                Some(_) => Ok(Value::Str(match other {
                    Variant::Boolean(b) => if *b { "True" } else { "False" }.to_string(),
                    o => integer(o)
                        .map_or_else(|| number(o).map_or_else(String::new, |f| f.to_string()), |i| i.to_string()),
                })),
                None => Err(format!("cannot read {} as a string", variant_name(other))),
            },
        };
    }
    let Some(f) = number(v) else { return Err(format!("cannot read {} as {}", variant_name(v), dtype.as_str())) };
    match dtype {
        DType::Bool => {
            if f.is_nan() {
                Err("NaN as bool".into())
            } else {
                Ok(Value::Bool(f != 0.0))
            }
        }
        DType::Float32 | DType::Float64 => Ok(Value::Float(f)),
        _ => {
            let n: i128 = match integer(v) {
                Some(i) => i,
                None if f.is_finite() => f.round() as i128,
                None => return Err(format!("{f} as {}", dtype.as_str())),
            };
            let (lo, hi): (i128, i128) = match dtype {
                DType::Int16 => (i128::from(i16::MIN), i128::from(i16::MAX)),
                DType::Uint16 => (0, i128::from(u16::MAX)),
                DType::Int32 => (i128::from(i32::MIN), i128::from(i32::MAX)),
                DType::Uint32 => (0, i128::from(u32::MAX)),
                DType::Int64 => (i128::from(i64::MIN), i128::from(i64::MAX)),
                _ => (0, i128::from(u64::MAX)),
            };
            if !(lo..=hi).contains(&n) {
                return Err(format!("{n} out of range for {}", dtype.as_str()));
            }
            Ok(i64::try_from(n).map(Value::Int).unwrap_or(Value::UInt(n as u64)))
        }
    }
}

fn variant_name(v: &Variant) -> &'static str {
    match v {
        Variant::Empty => "an empty value",
        Variant::Array(_) => "an array",
        _ => "this type",
    }
}

/// One node's DataValue as a tag's store entry.
pub fn data_value_to_tag(dv: &DataValue, dtype: DType) -> TagValue {
    let status = dv.status.unwrap_or(StatusCode::Good);
    let ts = dv.source_timestamp.or(dv.server_timestamp).map(|t| t.as_chrono());
    let quality = status_quality(status);
    if quality != Quality::Good {
        return TagValue::bad(quality, ts);
    }
    match dv.value.as_ref().map(|v| decode_value(dtype, v)) {
        Some(Ok(v)) => TagValue::new(Some(v), Quality::Good, ts),
        _ => TagValue::bad(Quality::BadDecode, ts),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_mapping() {
        assert_eq!(status_quality(StatusCode::Good), Quality::Good);
        assert_eq!(status_quality(StatusCode::UncertainLastUsableValue), Quality::Stale);
        assert_eq!(status_quality(StatusCode::BadNodeIdUnknown), Quality::BadConfig);
        assert_eq!(status_quality(StatusCode::BadUserAccessDenied), Quality::BadConfig);
        assert_eq!(status_quality(StatusCode::BadCommunicationError), Quality::BadComm);
        assert_eq!(status_quality(StatusCode::BadNoCommunication), Quality::BadComm);
    }

    #[test]
    fn decoding_follows_the_dtype() {
        assert_eq!(decode_value(DType::Float64, &Variant::from(3i32)), Ok(Value::Float(3.0)));
        assert_eq!(decode_value(DType::Int16, &Variant::from(2.6f64)), Ok(Value::Int(3)));
        assert_eq!(decode_value(DType::Bool, &Variant::from(2u8)), Ok(Value::Bool(true)));
        assert_eq!(decode_value(DType::Uint16, &Variant::from(-1i32)), Err("-1 out of range for uint16".into()));
        assert_eq!(decode_value(DType::String, &Variant::from("x")), Ok(Value::Str("x".into())));
        assert_eq!(decode_value(DType::String, &Variant::from(true)), Ok(Value::Str("True".into())));
        assert!(decode_value(DType::Int32, &Variant::from("x")).is_err());
        assert!(decode_value(DType::Float64, &Variant::Empty).is_err());
        assert_eq!(decode_value(DType::Uint64, &Variant::from(u64::MAX)), Ok(Value::UInt(u64::MAX)));
    }
}
