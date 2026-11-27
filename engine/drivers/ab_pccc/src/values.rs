//! Raw PCCC reply bytes to values (shared by DF1 and EtherNet/IP): what an EtherNet/IP PCCC
//! library returns for the same read. Pure.

use opencap_core::value::Value;

use crate::plan::PcccBlock;

fn words(raw: &[u8]) -> Vec<i64> {
    raw.chunks_exact(2).map(|c| i64::from(i16::from_le_bytes([c[0], c[1]]))).collect()
}

fn tc_bit(field: &str) -> u32 {
    match field {
        "EN" | "CU" => 15,
        "TT" | "CD" => 14,
        "DN" => 13,
        "OV" => 12,
        "UN" => 11,
        _ => 10, // UA
    }
}

/// Raw reply bytes -> the value an EtherNet/IP PCCC library returns for the same read: a scalar
/// for one element, a list for `{n}`; N/B/S/I/O words signed, L signed 32-bit, F float32, ST
/// strings, T/C `.ACC`/`.PRE` the word, T/C bits a bool. Only whole elements are decoded: a
/// short reply gives a shorter list (the decoder reports what is missing).
pub fn pccc_values(block: &PcccBlock, data: &[u8]) -> Option<Value> {
    if let Some(field) = block.field {
        if data.len() < 6 {
            return None;
        }
        let w = words(&data[..6]);
        return Some(match field {
            "PRE" => Value::Int(w[1]),
            "ACC" => Value::Int(w[2]),
            other => Value::Bool(w[0] & (1 << tc_bit(other)) != 0),
        });
    }
    let size = block.elem_size;
    let data = &data[..data.len().min(block.nbytes())];
    let values: Vec<Value> = data
        .chunks_exact(size)
        .map(|r| match block.file_type {
            "F" => Value::Float(f64::from(f32::from_le_bytes([r[0], r[1], r[2], r[3]]))),
            "L" => Value::Int(i64::from(i32::from_le_bytes([r[0], r[1], r[2], r[3]]))),
            "ST" => {
                let n = usize::from(u16::from_le_bytes([r[0], r[1]]));
                let mut text = r[2..2 + 82].to_vec();
                for pair in text.chunks_exact_mut(2) {
                    pair.swap(0, 1);
                }
                Value::Str(text.iter().take(n).map(|&b| b as char).collect())
            }
            _ => Value::Int(i64::from(i16::from_le_bytes([r[0], r[1]]))),
        })
        .collect();
    match values.len() {
        0 => None,
        1 if block.count <= 1 => values.into_iter().next(),
        _ => Some(Value::List(values)),
    }
}

/// The sequence form `decode_block` takes.
pub fn as_sequence(v: &Option<Value>) -> Option<Vec<Option<Value>>> {
    match v {
        None => None,
        Some(Value::List(l)) => Some(l.iter().cloned().map(Some).collect()),
        Some(other) => Some(vec![Some(other.clone())]),
    }
}
