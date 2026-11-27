//! Tag values as OPC UA variants and status codes (Python `variant_type`, `coerce`, `status_code`).

use chrono::{DateTime as ChronoDateTime, Utc};
use opcua::types::{DataTypeId, DataValue, DateTime, StatusCode, Variant};

use opencap_core::config::{DType, Tag};
use opencap_core::value::{Quality, TagValue, Value};

/// OPC UA built-in type of a tag's variable. Scaled values are always Double.
pub fn data_type_id(tag: &Tag) -> DataTypeId {
    if tag.scale.is_some() {
        return DataTypeId::Double;
    }
    match tag.dtype {
        DType::Bool => DataTypeId::Boolean,
        DType::Int16 => DataTypeId::Int16,
        DType::Uint16 => DataTypeId::UInt16,
        DType::Int32 => DataTypeId::Int32,
        DType::Uint32 => DataTypeId::UInt32,
        DType::Int64 => DataTypeId::Int64,
        DType::Uint64 => DataTypeId::UInt64,
        DType::Float32 => DataTypeId::Float,
        DType::Float64 => DataTypeId::Double,
        DType::String => DataTypeId::String,
    }
}

/// The default (never read) value of a variable of this type.
pub fn default_variant(t: DataTypeId) -> Variant {
    match t {
        DataTypeId::Boolean => Variant::from(false),
        DataTypeId::Int16 => Variant::from(0i16),
        DataTypeId::UInt16 => Variant::from(0u16),
        DataTypeId::Int32 => Variant::from(0i32),
        DataTypeId::UInt32 => Variant::from(0u32),
        DataTypeId::Int64 => Variant::from(0i64),
        DataTypeId::UInt64 => Variant::from(0u64),
        DataTypeId::Float => Variant::from(0f32),
        DataTypeId::Double => Variant::from(0f64),
        _ => Variant::from(String::new()),
    }
}

fn as_f64(v: &Value) -> Result<f64, String> {
    match v {
        Value::Bool(b) => Ok(f64::from(u8::from(*b))),
        Value::Int(i) => Ok(*i as f64),
        Value::UInt(u) => Ok(*u as f64),
        Value::Float(f) => Ok(*f),
        Value::Str(_) => Err("str as a number".into()),
        Value::List(_) => Err("list as a number".into()),
    }
}

fn as_i128(v: &Value, to: &str) -> Result<i128, String> {
    match v {
        Value::Bool(b) => Ok(i128::from(*b)),
        Value::Int(i) => Ok(i128::from(*i)),
        Value::UInt(u) => Ok(i128::from(*u)),
        Value::Float(f) if f.is_finite() => Ok(f.round() as i128),
        Value::Float(f) => Err(format!("{f} as {to}")),
        Value::Str(_) => Err("str as an integer".into()),
        Value::List(_) => Err("list as an integer".into()),
    }
}

/// A store value as a Variant of type `t`. `Err` when it does not fit (the variable is then
/// sent as bad_decode).
pub fn coerce(t: DataTypeId, v: &Value) -> Result<Variant, String> {
    macro_rules! int {
        ($ty:ty, $name:expr) => {{
            let n = as_i128(v, $name)?;
            <$ty>::try_from(n).map(Variant::from).map_err(|_| format!("{n} out of range for {}", $name))
        }};
    }
    match t {
        DataTypeId::Boolean => match v {
            Value::Str(_) | Value::List(_) => Err("not a boolean".into()),
            Value::Float(f) => Ok(Variant::from(*f != 0.0)),
            other => Ok(Variant::from(as_i128(other, "Boolean")? != 0)),
        },
        DataTypeId::Float => Ok(Variant::from(as_f64(v)? as f32)),
        DataTypeId::Double => Ok(Variant::from(as_f64(v)?)),
        DataTypeId::String => Ok(Variant::from(match v {
            Value::Str(s) => s.clone(),
            other => format!("{other:?}"),
        })),
        DataTypeId::Int16 => int!(i16, "Int16"),
        DataTypeId::UInt16 => int!(u16, "UInt16"),
        DataTypeId::Int32 => int!(i32, "Int32"),
        DataTypeId::UInt32 => int!(u32, "UInt32"),
        DataTypeId::Int64 => int!(i64, "Int64"),
        DataTypeId::UInt64 => int!(u64, "UInt64"),
        other => Err(format!("unsupported variable type {other:?}")),
    }
}

/// StatusCode of a store entry (ADR-0006, `tests/conformance/quality/map.json`). The store keeps the
/// last value on every non-good quality, so a tag without a value was never read good.
pub fn status_code(tv: &TagValue) -> StatusCode {
    match (tv.quality, tv.value.is_some()) {
        (Quality::Good, _) => StatusCode::Good,
        (Quality::BadComm, _) => StatusCode::BadCommunicationError,
        (Quality::BadConfig, _) => StatusCode::BadConfigurationError,
        (Quality::BadDecode, _) => StatusCode::BadDataEncodingInvalid,
        (Quality::Stale, true) => StatusCode::UncertainLastUsableValue,
        (Quality::Stale, false) => StatusCode::BadWaitingForInitialData,
    }
}

fn to_opc_time(ts: Option<ChronoDateTime<Utc>>) -> Option<DateTime> {
    ts.map(DateTime::from)
}

/// The DataValue for a store entry: the value (or the type's default when it does not fit or there
/// is none), the mapped status, the source timestamp.
pub fn data_value(t: DataTypeId, tv: &TagValue) -> DataValue {
    let mut status = status_code(tv);
    let value = match &tv.value {
        None => default_variant(t),
        Some(v) => match coerce(t, v) {
            Ok(variant) => variant,
            Err(_) => {
                status = StatusCode::BadDataEncodingInvalid;
                default_variant(t)
            }
        },
    };
    DataValue {
        value: Some(value),
        status: Some(status),
        source_timestamp: to_opc_time(tv.source_ts),
        source_picoseconds: None,
        server_timestamp: to_opc_time(tv.server_ts),
        server_picoseconds: None,
    }
}
