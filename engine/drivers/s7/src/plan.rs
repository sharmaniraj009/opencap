//! Block planner and decoder (Python `plan_blocks`, `decode_block`, D-051).
//!
//! One block is one S7 "read var" request of contiguous bytes (or timers/counters) in one
//! area/DB, planned for `params.max_pdu`. If the CPU negotiates a smaller PDU, `read()` splits a
//! block that does not fit; `wire_requests()` counts that runtime split once the driver knows
//! the negotiated PDU (D-085).

use std::any::Any;
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use opencap_core::config::{DType, Tag};
use opencap_core::driver::{Block, BlockRef, Plan};
use opencap_core::pyfmt::py_repr_str;
use opencap_core::value::Value;

use crate::address::{parse_s7_address, AddrKey, Area, Kind, S7Address};
use crate::params::{S7Params, READ_OVERHEAD};

/// The PDU length the CPU negotiated, shared by a driver and the blocks it planned
/// (0 = not connected yet: the planned PDU is assumed).
pub type SharedPdu = Arc<AtomicUsize>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct S7Item {
    pub names: Vec<String>,
    /// Byte offset of the item in the block's data.
    pub offset: usize,
    pub addr: S7Address,
}

#[derive(Debug, Clone)]
pub struct S7Block {
    pub area: Area,
    pub db: u128,
    /// Byte offset, or first timer/counter.
    pub start: usize,
    /// Bytes, or timers/counters.
    pub count: usize,
    pub items: Vec<S7Item>,
    pub tags: Vec<String>,
    /// The PDU the block was planned for.
    pub planned_pdu: usize,
    pub negotiated: SharedPdu,
}

impl S7Block {
    pub fn unit(&self) -> &'static str {
        match self.area {
            Area::T => "timer",
            Area::C => "counter",
            _ => "byte",
        }
    }

    /// Bytes per request unit.
    pub fn unit_bytes(&self) -> usize {
        if self.area.is_tc() {
            2
        } else {
            1
        }
    }

    pub fn nbytes(&self) -> usize {
        self.count * self.unit_bytes()
    }

    /// Request units per read var request for a PDU of `pdu` bytes.
    pub fn chunk(&self, pdu: usize) -> usize {
        (pdu.saturating_sub(READ_OVERHEAD) / self.unit_bytes()).max(1)
    }

    /// The `(start, count)` requests that read the block with a PDU of `pdu` bytes.
    pub fn parts(&self, pdu: usize) -> Vec<(usize, usize)> {
        let chunk = self.chunk(pdu);
        let (mut start, mut left) = (self.start, self.count);
        let mut out = Vec::new();
        while left > 0 {
            let n = left.min(chunk);
            out.push((start, n));
            start += n;
            left -= n;
        }
        out
    }
}

impl Block for S7Block {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        let name = if self.area == Area::Db { format!("DB{}", self.db) } else { self.area.as_str().to_string() };
        let last = self.start + self.count - 1;
        let at = if last == self.start { self.start.to_string() } else { format!("{}..{last}", self.start) };
        let noun = format!("{}{}", self.unit(), if self.count != 1 { "s" } else { "" });
        format!("{name} {at} ({} {noun})", self.count)
    }

    /// Read var requests this block costs: one, or more when the CPU negotiated a PDU smaller
    /// than the one the block was planned for.
    fn wire_requests(&self) -> usize {
        let negotiated = self.negotiated.load(Ordering::Relaxed);
        let pdu = if negotiated == 0 { self.planned_pdu } else { negotiated };
        self.parts(pdu).len().max(1)
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

/// Merge parsed tags into wire blocks. Returns `(blocks, invalid)`. Per (area, DB), greedy over
/// items sorted by start: an item joins the open block when it starts at most `max_gap` bytes
/// after the block end and the block stays within `max_pdu - 18` bytes (timers/counters: 2
/// bytes each). Sorted and independent of input order.
pub fn plan_blocks(
    parsed: &[(String, S7Address)],
    p: &S7Params,
    negotiated: &SharedPdu,
) -> (Vec<S7Block>, BTreeMap<String, String>) {
    let span_bytes = p.max_span() as u128;
    let mut invalid = BTreeMap::new();
    let mut names_by_key: BTreeMap<AddrKey, Vec<String>> = BTreeMap::new();
    let mut addr_by_key: BTreeMap<AddrKey, &S7Address> = BTreeMap::new();
    for (name, a) in parsed {
        if a.nbytes() > span_bytes {
            invalid.insert(
                name.clone(),
                format!(
                    "s7 address {} ({}): needs {} bytes in one request, more than the {span_bytes} a PDU of {} bytes carries (max_pdu={})",
                    py_repr_str(&a.text),
                    a.dtype.as_str(),
                    a.nbytes(),
                    p.max_pdu,
                    p.max_pdu
                ),
            );
            continue;
        }
        names_by_key.entry(a.key()).or_default().push(name.clone());
        addr_by_key.entry(a.key()).or_insert(a);
    }

    let mut groups: BTreeMap<(Area, u128), Vec<&S7Address>> = BTreeMap::new();
    for a in addr_by_key.values() {
        groups.entry((a.area, a.db)).or_default().push(a);
    }

    let mut blocks = Vec::new();
    for ((area, db), mut addrs) in groups {
        let tc = area.is_tc();
        let span = if tc { span_bytes / 2 } else { span_bytes };
        let gap = if tc { p.max_gap as u128 / 2 } else { p.max_gap as u128 };
        addrs.sort_by_key(|a| (a.start, a.end(), a.key()));
        let mut run: Vec<&S7Address> = Vec::new();
        let mut end: u128 = 0;
        let close = |run: &mut Vec<&S7Address>, end: u128, blocks: &mut Vec<S7Block>| {
            let start = run[0].start;
            let unit_bytes = if tc { 2 } else { 1 };
            let items: Vec<S7Item> = run
                .iter()
                .map(|a| {
                    let mut names = names_by_key[&a.key()].clone();
                    names.sort();
                    S7Item { names, offset: ((a.start - start) as usize) * unit_bytes, addr: (*a).clone() }
                })
                .collect();
            let mut tags: Vec<String> = items.iter().flat_map(|it| it.names.iter().cloned()).collect();
            tags.sort();
            blocks.push(S7Block {
                area,
                db,
                start: start as usize,
                count: (end - start) as usize,
                items,
                tags,
                planned_pdu: p.max_pdu,
                negotiated: negotiated.clone(),
            });
            run.clear();
        };
        for a in addrs {
            if !run.is_empty() && a.start.saturating_sub(end) <= gap && end.max(a.end()) - run[0].start <= span {
                end = end.max(a.end());
                run.push(a);
                continue;
            }
            if !run.is_empty() {
                close(&mut run, end, &mut blocks);
            }
            end = a.end();
            run.push(a);
        }
        if !run.is_empty() {
            close(&mut run, end, &mut blocks);
        }
    }
    (blocks, invalid)
}

/// Pure planning of a device's tags (Python `S7Driver.plan`).
pub fn plan_tags<'a>(p: &S7Params, negotiated: &SharedPdu, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
    let mut parsed: Vec<(String, S7Address)> = Vec::new();
    let mut invalid: BTreeMap<String, String> = BTreeMap::new();
    for tag in tags {
        if !tag.enabled {
            continue;
        }
        match parse_s7_address(&tag.address, tag.dtype, p) {
            Ok(a) => parsed.push((tag.name.clone(), a)),
            Err(reason) => {
                invalid.insert(tag.name.clone(), reason);
            }
        }
    }
    let (blocks, too_big) = plan_blocks(&parsed, p, negotiated);
    invalid.extend(too_big);
    Plan { blocks: blocks.into_iter().map(|b| Arc::new(b) as BlockRef).collect(), invalid }
}

// --- decoder ---------------------------------------------------------------------------------

/// Per-tag decode failure (Python `DecodeError`); the driver maps it to `bad_decode`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError(pub String);

/// Packed BCD (4 digits) to int.
pub fn bcd_to_int(word: u16) -> Result<u32, String> {
    let mut value = 0u32;
    let mut mult = 1u32;
    for shift in [0, 4, 8, 12] {
        let digit = u32::from((word >> shift) & 0xF);
        if digit > 9 {
            return Err(format!("0x{word:04X} is not BCD (digit {digit:X})"));
        }
        value += digit * mult;
        mult *= 10;
    }
    Ok(value)
}

const S5TIME_BASE_MS: [u32; 4] = [10, 100, 1000, 10_000];

/// S5TIME (bits 12-13 time base, bits 0-11 three BCD digits) to milliseconds.
pub fn s5time_to_ms(word: u16) -> Result<u32, String> {
    let v = bcd_to_int(word & 0x0FFF).map_err(|_| format!("S5TIME 0x{word:04X}: digits are not BCD"))?;
    Ok(v * S5TIME_BASE_MS[usize::from((word >> 12) & 0x3)])
}

/// Decode one block's bytes into `{tag name: value or DecodeError}`. No scaling.
pub fn decode_block(block: &S7Block, buf: &[u8], p: &S7Params) -> BTreeMap<String, Result<Value, DecodeError>> {
    let mut out = BTreeMap::new();
    for item in &block.items {
        let value = decode_item(item, buf, p)
            .map_err(|reason| DecodeError(format!("{} ({}): {reason}", item.addr.text, item.addr.dtype.as_str())));
        for name in &item.names {
            out.insert(name.clone(), value.clone());
        }
    }
    out
}

fn decode_item(item: &S7Item, buf: &[u8], p: &S7Params) -> Result<Value, String> {
    let a = &item.addr;
    let (lo, hi) = (item.offset, item.offset + a.nbytes() as usize);
    if buf.len() < hi {
        return Err(format!("short reply: need bytes up to {} of the block, got {}", hi - 1, buf.len()));
    }
    let chunk = &buf[lo..hi];
    match a.kind {
        Kind::Bit => return Ok(Value::Bool((chunk[0] >> a.bit.unwrap_or(0)) & 1 == 1)),
        Kind::RawString => {
            let end = chunk.iter().position(|b| *b == 0).unwrap_or(chunk.len());
            return Ok(Value::Str(latin1(&chunk[..end])));
        }
        Kind::String => {
            let length = usize::from(chunk[1]).min(a.str_len.unwrap_or(0) as usize);
            let text = &chunk[2..(2 + length).min(chunk.len())];
            let end = text.iter().position(|b| *b == 0).unwrap_or(text.len());
            return Ok(Value::Str(latin1(&text[..end])));
        }
        Kind::Timer | Kind::Counter => {
            let word = u16::from_be_bytes([chunk[0], chunk[1]]); // S5TIME / BCD: the PLC's format
            return if a.kind == Kind::Timer {
                s5time_to_ms(word).map(|v| Value::Int(i64::from(v)))
            } else {
                bcd_to_int(word).map(|v| Value::Int(i64::from(v)))
            };
        }
        _ => {}
    }
    let size = a.size as usize;
    let mut values = Vec::with_capacity(a.count as usize);
    for i in 0..a.count as usize {
        let mut elem = chunk[i * size..(i + 1) * size].to_vec();
        if !p.byte_order_big {
            elem.reverse();
        }
        let u = elem.iter().fold(0u64, |acc, b| (acc << 8) | u64::from(*b));
        if let Some(bit) = a.bit {
            return Ok(Value::Bool((u >> bit) & 1 == 1));
        }
        values.push(number(u, size, a.dtype, p));
    }
    Ok(shape(values, &a.dims))
}

fn latin1(b: &[u8]) -> String {
    b.iter().map(|c| char::from(*c)).collect()
}

fn number(u: u64, size: usize, dtype: DType, p: &S7Params) -> Value {
    match dtype {
        DType::Float32 => Value::Float(float32(u as u32, p)),
        DType::Float64 => Value::Float(float64(u, p)),
        DType::Int16 | DType::Int32 | DType::Int64 => {
            let bits = size * 8;
            if bits == 64 {
                Value::Int(u as i64)
            } else if u & (1 << (bits - 1)) != 0 {
                Value::Int(u as i64 - (1i64 << bits))
            } else {
                Value::Int(u as i64)
            }
        }
        _ => Value::from_u64(u),
    }
}

fn float32(u: u32, p: &S7Params) -> f64 {
    let (exp, mant) = ((u >> 23) & 0xFF, u & 0x7F_FFFF);
    if (exp == 0xFF || (exp == 0 && mant != 0)) && p.nonnormal_float == "zero" {
        return 0.0;
    }
    f64::from(f32::from_bits(u))
}

fn float64(u: u64, p: &S7Params) -> f64 {
    let (exp, mant) = ((u >> 52) & 0x7FF, u & ((1u64 << 52) - 1));
    if (exp == 0x7FF || (exp == 0 && mant != 0)) && p.nonnormal_float == "zero" {
        return 0.0;
    }
    f64::from_bits(u)
}

fn shape(mut values: Vec<Value>, dims: &[u128]) -> Value {
    match dims.len() {
        0 => values.swap_remove(0),
        1 => Value::List(values),
        _ => {
            let cols = dims[1] as usize;
            Value::List(values.chunks(cols).map(|r| Value::List(r.to_vec())).collect())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan(p: &S7Params, tags: &[Tag]) -> Plan {
        plan_tags(p, &SharedPdu::default(), tags)
    }

    fn block(plan: &Plan, i: usize) -> &S7Block {
        plan.blocks[i].as_any().downcast_ref::<S7Block>().unwrap()
    }

    #[test]
    fn merging_and_describe() {
        let tags = [
            Tag::new("a", "DB1,REAL0", DType::Float32),
            Tag::new("b", "DB1,REAL8", DType::Float32),
            Tag::new("c", "DB1,REAL100", DType::Float32),
            Tag::new("m", "M10.3", DType::Bool),
            Tag::new("t", "T1", DType::Uint32),
        ];
        let p = plan(&S7Params::default(), &tags);
        let d: Vec<String> = p.blocks.iter().map(|b| b.describe()).collect();
        assert_eq!(d, ["DB1 0..11 (12 bytes)", "DB1 100..103 (4 bytes)", "M 10 (1 byte)", "T 1 (1 timer)"]);
    }

    #[test]
    fn a_tag_larger_than_the_pdu_is_invalid() {
        let tags = [Tag::new("big", "DB1,B0.470", DType::String)];
        let p = plan(&S7Params::default(), &tags);
        assert!(p.blocks.is_empty());
        assert!(
            p.invalid["big"].contains("needs 470 bytes in one request, more than the 462 a PDU of 480 bytes carries")
        );
    }

    #[test]
    fn wire_requests_follow_the_negotiated_pdu() {
        let pdu = SharedPdu::default();
        let tags = [Tag::new("a", "DB1,REAL0[200]", DType::Float32)];
        let p = S7Params { max_pdu: 960, ..S7Params::default() };
        let plan = plan_tags(&p, &pdu, &tags);
        let b = block(&plan, 0);
        assert_eq!(b.wire_requests(), 1); // 800 bytes fit one 960-byte PDU
        pdu.store(480, Ordering::Relaxed);
        assert_eq!(b.wire_requests(), 2); // 462 + 338
        assert_eq!(b.parts(480), vec![(0, 462), (462, 338)]);
        pdu.store(240, Ordering::Relaxed);
        assert_eq!(b.wire_requests(), 4);
    }

    #[test]
    fn values() {
        let p = S7Params::default();
        let tags = [
            Tag::new("r", "DB1,REAL0", DType::Float32),
            Tag::new("i", "DB1,INT4", DType::Int16),
            Tag::new("b", "DB1,X6.5", DType::Bool),
            Tag::new("s", "DB1,STRING8.6", DType::String),
        ];
        let plan = plan(&p, &tags);
        let b = block(&plan, 0);
        let mut buf = vec![0u8; 16];
        buf[..4].copy_from_slice(&12.5f32.to_be_bytes());
        buf[4..6].copy_from_slice(&(-2i16).to_be_bytes());
        buf[6] = 1 << 5;
        buf[8..13].copy_from_slice(&[6, 3, b'a', b'b', b'c']);
        let out = decode_block(b, &buf, &p);
        assert_eq!(out["r"], Ok(Value::Float(12.5)));
        assert_eq!(out["i"], Ok(Value::Int(-2)));
        assert_eq!(out["b"], Ok(Value::Bool(true)));
        assert_eq!(out["s"], Ok(Value::Str("abc".into())));
        let short = decode_block(b, &buf[..3], &p);
        assert!(short["r"].as_ref().unwrap_err().0.contains("short reply: need bytes up to 3 of the block, got 3"));
    }

    #[test]
    fn timers_and_counters() {
        assert_eq!(s5time_to_ms(0x1123), Ok(12300)); // base 100 ms, 123
        assert_eq!(bcd_to_int(0x0999), Ok(999));
        assert!(bcd_to_int(0x00A0).is_err());
    }
}
