//! Decoder: one range's bytes -> {tag: value or DecodeError} (docs/protocols/omron-fins.md §3.1).
//! Words are big-endian; 32/64-bit values take consecutive words, low word first unless
//! `first_word_low` is false; index registers are one 4-byte element, most significant byte
//! first; BCD as KEPServerEX (the `D` suffix: Word/DWord unsigned, Short/Long signed with the
//! most significant bit as sign; timer/counter present values when `tc_bcd`).

use opencap_core::value::Value;

use crate::address::{Area, Kind};
use crate::params::FinsParams;
use crate::plan::{FinsItem, FinsRange};

#[derive(Debug, Clone, PartialEq)]
pub struct DecodeError(pub String);

/// BCD digits -> value; an error on a nibble above 9.
pub fn from_bcd(raw: u64, digits: u32) -> Result<u64, String> {
    let mut value = 0u64;
    let mut mult = 1u64;
    for i in 0..digits {
        let d = (raw >> (4 * i)) & 0xF;
        if d > 9 {
            return Err(format!("0x{raw:X} is not BCD"));
        }
        value += d * mult;
        mult *= 10;
    }
    Ok(value)
}

pub fn decode_range(range: &FinsRange, data: &[u8], p: &FinsParams) -> Vec<(String, Result<Value, DecodeError>)> {
    let mut out = Vec::new();
    for item in &range.items {
        let v =
            decode_item(item, data, p).map_err(|e| DecodeError(format!("{} ({}): {e}", item.addr.text, item.dtype)));
        for name in &item.names {
            out.push((name.clone(), v.clone()));
        }
    }
    out
}

fn decode_item(item: &FinsItem, data: &[u8], p: &FinsParams) -> Result<Value, String> {
    let a = &item.addr;
    let lo = item.offset as usize;
    let need = |n: usize| -> Result<&[u8], String> {
        data.get(lo..lo + n).ok_or_else(|| format!("short reply: need {n} bytes at {lo}, got {}", data.len()))
    };
    match a.kind {
        Kind::Flag => return Ok(Value::Bool(need(1)?[0] != 0)),
        Kind::Bit => {
            let w = need(2)?;
            return Ok(Value::Bool((u16::from_be_bytes([w[0], w[1]]) >> a.bit.unwrap_or(0)) & 1 == 1));
        }
        Kind::String => {
            let n = a.str_len.unwrap_or(0) as usize;
            let order = a.str_order.unwrap_or('H');
            let words = if matches!(order, 'D' | 'E') { n } else { n.div_ceil(2) };
            let raw = need(words * 2)?;
            let mut bytes = Vec::with_capacity(words * 2);
            for w in raw.chunks(2) {
                match order {
                    'H' => bytes.extend_from_slice(&[w[0], w[1]]),
                    'L' => bytes.extend_from_slice(&[w[1], w[0]]),
                    'D' => bytes.push(w[0]),
                    _ => bytes.push(w[1]),
                }
            }
            bytes.truncate(n);
            let end = bytes.iter().position(|&b| b == 0).unwrap_or(bytes.len());
            return Ok(Value::Str(bytes[..end].iter().map(|&b| char::from(b)).collect()));
        }
        Kind::Word => {}
    }
    let dtype = item.dtype.as_str();
    if a.area == Area::Ir {
        let b = need(4)?;
        let u = u32::from_be_bytes([b[0], b[1], b[2], b[3]]);
        return Ok(match dtype {
            "int32" => Value::Int(i64::from(u as i32)),
            "float32" => Value::Float(f64::from(f32::from_bits(u))),
            _ => Value::Int(i64::from(u)),
        });
    }
    let n = crate::address::words_of(dtype) as usize;
    let elements = if a.dims.is_empty() { 1 } else { (a.rows() * a.cols()) as usize };
    let raw = need(n * 2 * elements)?;
    let tc_bcd = matches!(a.area, Area::T | Area::C) && p.tc_bcd;
    let mut values = Vec::with_capacity(elements);
    for e in 0..elements {
        let mut words: Vec<u64> =
            (0..n).map(|i| u64::from(u16::from_be_bytes([raw[(e * n + i) * 2], raw[(e * n + i) * 2 + 1]]))).collect();
        if !p.first_word_low {
            words.reverse();
        }
        // low word first from here on
        let u = words.iter().enumerate().fold(0u64, |acc, (i, w)| acc | (w << (16 * i)));
        let bits = 16 * n as u32;
        values.push(if a.bcd || tc_bcd || dtype.starts_with("bcd") {
            let signed = a.bcd && dtype.starts_with("int");
            if signed {
                let sign = 1u64 << (bits - 1);
                let v = from_bcd(u & !sign, bits / 4)? as i64;
                Value::Int(if u & sign != 0 { -v } else { v })
            } else {
                Value::from_u64(from_bcd(u, bits / 4)?)
            }
        } else {
            match dtype {
                "float32" => Value::Float(f64::from(f32::from_bits(u as u32))),
                "float64" => Value::Float(f64::from_bits(u)),
                d if d.starts_with("int") => {
                    let shift = 64 - bits;
                    Value::Int(((u << shift) as i64) >> shift)
                }
                _ => Value::from_u64(u),
            }
        });
    }
    Ok(match a.dims.len() {
        0 => values.remove(0),
        1 => Value::List(values),
        _ => {
            let cols = a.cols() as usize;
            Value::List(values.chunks(cols).map(|c| Value::List(c.to_vec())).collect())
        }
    })
}
