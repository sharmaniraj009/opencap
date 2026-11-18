//! Register decoder (Python `decode_block`): byte order, first word low, first dword low,
//! bits in registers, strings, arrays, non-normal floats. No scaling here.

use indexmap::IndexMap;

use opencap_core::value::Value;

use super::address::{regs_per_dtype, ModbusAddress};
use super::params::ModbusParams;
use super::plan::{ModbusBlock, ModbusItem};

/// Reply data: `registers` (u16, wire bytes high-first) for FC03/04, `bits` for FC01/02
/// (may be padded to a multiple of 8).
#[derive(Debug, Clone, Copy)]
pub enum Reply<'a> {
    Registers(&'a [u16]),
    Bits(&'a [bool]),
}

impl Reply<'_> {
    fn len(&self) -> usize {
        match self {
            Reply::Registers(r) => r.len(),
            Reply::Bits(b) => b.len(),
        }
    }
}

/// Per-tag decode failure (Python `DecodeError`); the core maps it to `bad_decode`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError(pub String);

/// Decode one block reply into {tag name: value or DecodeError}.
pub fn decode_block(
    block: &ModbusBlock,
    data: Reply<'_>,
    p: &ModbusParams,
) -> IndexMap<String, Result<Value, DecodeError>> {
    let mut out = IndexMap::new();
    for item in &block.items {
        let value = decode_item(item, data, p)
            .map_err(|e| DecodeError(format!("{} ({}): {e}", item.addr.text, item.addr.dtype)));
        for name in &item.names {
            out.insert(name.clone(), value.clone());
        }
    }
    out
}

fn decode_item(item: &ModbusItem, data: Reply<'_>, p: &ModbusParams) -> Result<Value, String> {
    let a = &item.addr;
    let (lo, hi) = (item.offset as usize, (item.offset + a.count) as usize);
    if data.len() < hi {
        let unit = if a.is_bits() { "bits" } else { "registers" };
        return Err(format!("short reply: need {unit} up to offset {}, got {}", hi - 1, data.len()));
    }
    let words: Vec<u16> = match data {
        Reply::Bits(bits) => {
            let values = bits[lo..hi].iter().map(|b| Value::Bool(*b)).collect();
            return Ok(shape(values, &a.dims));
        }
        Reply::Registers(regs) => {
            if a.is_bits() {
                return Err("register reply for a bit block".into());
            }
            regs[lo..hi].to_vec()
        }
    };
    if let Some(len) = a.str_len {
        // byte order comes only from the H/L suffix
        return Ok(Value::Str(decode_string(&words, len as usize, a.str_order.unwrap_or("H"))));
    }
    let words: Vec<u16> = if p.byte_order_big { words } else { words.iter().map(|w| w.swap_bytes()).collect() };
    if let Some(bit) = a.bit {
        return Ok(Value::Bool((words[0] >> bit) & 1 == 1));
    }
    let n = regs_per_dtype(a.dtype) as usize;
    let values = words.chunks(n).map(|c| decode_scalar(c, a, p)).collect();
    Ok(shape(values, &a.dims))
}

fn shape(values: Vec<Value>, dims: &[u32]) -> Value {
    match dims.len() {
        0 => values.into_iter().next().expect("one element"),
        1 => Value::List(values),
        _ => {
            let cols = dims[1] as usize;
            Value::List(values.chunks(cols).take(dims[0] as usize).map(|r| Value::List(r.to_vec())).collect())
        }
    }
}

fn dword(first: u16, second: u16, first_word_low: bool) -> u64 {
    if first_word_low {
        ((second as u64) << 16) | first as u64
    } else {
        ((first as u64) << 16) | second as u64
    }
}

fn decode_scalar(words: &[u16], a: &ModbusAddress, p: &ModbusParams) -> Value {
    match words.len() {
        1 => {
            let w = words[0];
            if a.dtype == "int16" {
                Value::Int(w as i16 as i64)
            } else {
                Value::Int(w as i64)
            }
        }
        2 => {
            let u = dword(words[0], words[1], p.first_word_low) as u32;
            match a.dtype {
                "uint32" => Value::Int(u as i64),
                "int32" => Value::Int(u as i32 as i64),
                _ => Value::Float(float32(u, p)),
            }
        }
        _ => {
            let d0 = dword(words[0], words[1], p.first_word_low);
            let d1 = dword(words[2], words[3], p.first_word_low);
            let u = if p.first_dword_low { (d1 << 32) | d0 } else { (d0 << 32) | d1 };
            match a.dtype {
                "uint64" => Value::from_u64(u),
                "int64" => Value::Int(u as i64),
                _ => Value::Float(float64(u, p)),
            }
        }
    }
}

fn float32(u: u32, p: &ModbusParams) -> f64 {
    let (exp, mant) = ((u >> 23) & 0xFF, u & 0x7F_FFFF);
    let nonnormal = exp == 0xFF || (exp == 0 && mant != 0);
    if nonnormal && p.nonnormal_float == "zero" {
        return 0.0;
    }
    f32::from_bits(u) as f64
}

fn float64(u: u64, p: &ModbusParams) -> f64 {
    let (exp, mant) = ((u >> 52) & 0x7FF, u & ((1u64 << 52) - 1));
    let nonnormal = exp == 0x7FF || (exp == 0 && mant != 0);
    if nonnormal && p.nonnormal_float == "zero" {
        return 0.0;
    }
    f64::from_bits(u)
}

/// Bytes in H (high first) or L order, cut at `length` and at the first NUL, latin-1.
fn decode_string(words: &[u16], length: usize, order: &str) -> String {
    let mut raw: Vec<u8> = Vec::with_capacity(words.len() * 2);
    for w in words {
        let (hi, lo) = ((w >> 8) as u8, (w & 0xFF) as u8);
        if order == "H" {
            raw.extend([hi, lo]);
        } else {
            raw.extend([lo, hi]);
        }
    }
    raw.truncate(length);
    if let Some(nul) = raw.iter().position(|&b| b == 0) {
        raw.truncate(nul);
    }
    raw.iter().map(|&b| b as char).collect()
}
