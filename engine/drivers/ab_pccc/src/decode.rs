//! Decoding of a block's value (Python `decode_block`): the value is what the PCCC read
//! returns (a scalar for one element, a list for several), exactly the shape the EtherNet/IP
//! libraries give, so the EtherNet/IP and DF1 drivers share this code.

use indexmap::IndexMap;

use opencap_core::value::Value;
use opencap_driver_ab_logix::decode::coerce_value;

use crate::plan::{PcccBlock, PcccItem};

#[derive(Debug, Clone, PartialEq)]
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

fn as_int(v: &Value) -> Result<i64, String> {
    match v {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Int(i) => Ok(*i),
        Value::UInt(u) => Ok(*u as i64),
        other => Err(format!("expected an integer word, got {}", type_name(other))),
    }
}

fn decode_item(block: &PcccBlock, item: &PcccItem, raw: &Option<Value>) -> Result<Value, String> {
    let Some(raw) = raw else { return Err("no value".into()) };
    if block.file_type == "ST" || block.file_type == "F" {
        return coerce_value(raw, item.dtype);
    }
    if let Some(f) = item.field {
        if f != "ACC" && f != "PRE" {
            return coerce_value(&Value::Bool(as_int(raw)? != 0), item.dtype);
        }
    }
    let word = as_int(raw)?;
    if let Some(bit) = item.bit {
        return coerce_value(&Value::Bool((word >> bit) & 1 != 0), item.dtype);
    }
    coerce_value(&Value::Int(word), item.dtype)
}

/// `values` is what the read returned as a sequence (a scalar is a one-element slice, a
/// missing element is `None`); `None` = no value at all. Every tag gets its value or a
/// [`DecodeError`]; one bad tag never hides the others.
pub fn decode_block(
    block: &PcccBlock,
    values: Option<&[Option<Value>]>,
) -> IndexMap<String, Result<Value, DecodeError>> {
    let mut out = IndexMap::new();
    let Some(seq) = values else {
        for item in &block.items {
            out.insert(item.tag.clone(), Err(DecodeError(format!("{}: no value in reply", block.address))));
        }
        return out;
    };
    for item in &block.items {
        let Some(raw) = seq.get(item.offset) else {
            out.insert(
                item.tag.clone(),
                Err(DecodeError(format!(
                    "{}: reply has {} elements, element {} missing",
                    block.address,
                    seq.len(),
                    block.start as usize + item.offset
                ))),
            );
            continue;
        };
        out.insert(
            item.tag.clone(),
            decode_item(block, item, raw).map_err(|e| DecodeError(format!("{}: {}: {e}", block.address, item.tag))),
        );
    }
    out
}
