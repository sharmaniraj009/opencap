//! PCCC messages carried by DF1: the protected typed logical read and its reply values. Pure.

use opencap_core::value::Value;
use opencap_driver_ab_pccc::PcccBlock;

pub const CMD_DIAGNOSTIC: u8 = 0x06;
pub const CMD_EXTENDED: u8 = 0x0F;
pub const FNC_DIAGNOSTIC_STATUS: u8 = 0x03;
pub const FNC_TYPED_READ_2: u8 = 0xA1;
pub const FNC_TYPED_READ_3: u8 = 0xA2;

fn file_type_code(ft: &str) -> Option<u8> {
    Some(match ft {
        "O" => 0x82,
        "I" => 0x83,
        "S" => 0x84,
        "B" => 0x85,
        "T" => 0x86,
        "C" => 0x87,
        "N" => 0x89,
        "F" => 0x8A,
        "ST" => 0x8D,
        "L" => 0x91,
        _ => return None,
    })
}

/// PCCC logical address field: one byte below 255, else 0xFF + UINT (little-endian).
pub fn address_field(value: u32) -> Vec<u8> {
    if value < 0xFF {
        vec![value as u8]
    } else {
        let mut v = vec![0xFF];
        v.extend((value as u16).to_le_bytes());
        v
    }
}

/// FNC + function data of the protected typed logical read for one block: byte count, file
/// number, file type, element, sub-element. A T/C sub-element reads the whole 6-byte element;
/// I/O: element = slot, sub-element = word; `a1` leaves out a sub-element of 0.
pub fn typed_read_function(block: &PcccBlock, read_fnc: u8) -> Result<Vec<u8>, String> {
    let size = block.nbytes();
    if !(1..=0xFF).contains(&size) {
        return Err(format!("{}: {size} bytes do not fit the one-byte PCCC size field", block.address));
    }
    let code = file_type_code(block.file_type)
        .ok_or_else(|| format!("{}: file type {} has no PCCC type code", block.address, block.file_type))?;
    let (element, sub) = match block.slot {
        Some(slot) => (slot, block.start),
        None => (block.start, 0),
    };
    let mut fields = vec![size as u8];
    fields.extend(address_field(block.file_number));
    fields.push(code);
    fields.extend(address_field(element));
    let mut out = Vec::new();
    if read_fnc == FNC_TYPED_READ_2 && sub == 0 {
        out.push(FNC_TYPED_READ_2);
        out.extend(fields);
    } else {
        out.push(FNC_TYPED_READ_3);
        out.extend(fields);
        out.extend(address_field(sub));
    }
    Ok(out)
}

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
