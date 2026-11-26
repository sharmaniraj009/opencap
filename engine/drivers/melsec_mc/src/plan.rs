//! Block planner and word decoder (Python `plan_blocks`, `decode_block`, D-055).
//!
//! Every block is one "batch read in word units" (command 0401, subcommand 0000, or 0002 for
//! iQ-R): bit devices are read 16 points per word with the head on a 16-point boundary. A block
//! spans at most `max_words` (MC 3E limit: 960 words per request).

use std::any::Any;
use std::collections::BTreeMap;
use std::sync::Arc;

use opencap_core::config::{DType, Tag};
use opencap_core::driver::{Block, BlockRef, Plan};
use opencap_core::value::Value;

use crate::address::{device_spec, format_number, parse_address, AddrKey, Kind, McAddress};
use crate::params::{McParams, MAX_WORDS};

/// One distinct piece of data inside a block, served to every tag name that reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McItem {
    pub names: Vec<String>,
    /// First word of the item, relative to the block start.
    pub offset: usize,
    pub addr: McAddress,
}

#[derive(Debug, Clone)]
pub struct McBlock {
    /// Kepware device name.
    pub device: &'static str,
    /// First word: device number (word devices) or point / 16 (bit devices).
    pub start: usize,
    /// Words.
    pub count: usize,
    pub items: Vec<McItem>,
    /// Names of every tag the block serves, sorted.
    pub tags: Vec<String>,
}

impl McBlock {
    pub fn kind(&self) -> Kind {
        device_spec(self.device).map_or(Kind::Word, |s| s.kind)
    }

    /// Device number of the first point read (bit devices: a multiple of 16).
    pub fn head_number(&self) -> usize {
        if self.kind() == Kind::Bit {
            self.start * 16
        } else {
            self.start
        }
    }

    pub fn last_number(&self) -> usize {
        if self.kind() == Kind::Bit {
            (self.start + self.count) * 16 - 1
        } else {
            self.start + self.count - 1
        }
    }
}

impl Block for McBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        let d = self.device;
        format!(
            "{d}{}..{d}{} ({} words)",
            format_number(d, self.head_number() as u128),
            format_number(d, self.last_number() as u128),
            self.count
        )
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

/// Merge parsed tags into wire blocks. Returns `(blocks, invalid)`: invalid = tags that fit no
/// block. Greedy over items sorted by (device, start word): an item joins the open block when it
/// is on the same device, starts at most `max_gap` words after the block end, and the merged
/// span stays within `max_words`. The output is sorted and independent of input order.
pub fn plan_blocks(parsed: &[(String, McAddress)], p: &McParams) -> (Vec<McBlock>, BTreeMap<String, String>) {
    let mut invalid = BTreeMap::new();
    let mut names_by_key: BTreeMap<AddrKey, Vec<String>> = BTreeMap::new();
    let mut addr_by_key: BTreeMap<AddrKey, &McAddress> = BTreeMap::new();
    for (name, a) in parsed {
        if a.words > p.max_words as u128 {
            invalid.insert(
                name.clone(),
                format!(
                    "melsec address {} ({}): needs {} words in one request, more than max_words={} (MC 3E limit {MAX_WORDS})",
                    opencap_core::pyfmt::py_repr_str(&a.text),
                    a.dtype.as_str(),
                    a.words,
                    p.max_words
                ),
            );
            continue;
        }
        names_by_key.entry(a.key()).or_default().push(name.clone());
        addr_by_key.entry(a.key()).or_insert(a);
    }

    let mut blocks: Vec<McBlock> = Vec::new();
    let mut group: Vec<(&McAddress, Vec<String>)> = Vec::new();
    let mut end: u128 = 0;

    fn close(group: &mut Vec<(&McAddress, Vec<String>)>, end: u128, blocks: &mut Vec<McBlock>) {
        let start = group[0].0.start;
        let items: Vec<McItem> = group
            .iter()
            .map(|(a, names)| McItem { names: names.clone(), offset: (a.start - start) as usize, addr: (*a).clone() })
            .collect();
        let mut tags: Vec<String> = items.iter().flat_map(|it| it.names.iter().cloned()).collect();
        tags.sort();
        blocks.push(McBlock {
            device: group[0].0.device,
            start: start as usize,
            count: (end - start) as usize,
            items,
            tags,
        });
        group.clear();
    }

    for (key, a) in &addr_by_key {
        let mut names = names_by_key[key].clone();
        names.sort();
        if let Some((first, _)) = group.first() {
            if a.device == first.device
                && a.start.saturating_sub(end) <= p.max_gap as u128
                && end.max(a.end()) - first.start <= p.max_words as u128
            {
                end = end.max(a.end());
                group.push((a, names));
                continue;
            }
            close(&mut group, end, &mut blocks);
        }
        end = a.end();
        group.push((a, names));
    }
    if !group.is_empty() {
        close(&mut group, end, &mut blocks);
    }
    (blocks, invalid)
}

/// Pure planning of a device's tags (Python `MelsecMcDriver.plan`): disabled tags are skipped,
/// addresses that do not parse go to `invalid` with their `ValueError` text.
pub fn plan_tags<'a>(p: &McParams, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
    let mut parsed: Vec<(String, McAddress)> = Vec::new();
    let mut invalid: BTreeMap<String, String> = BTreeMap::new();
    for tag in tags {
        if !tag.enabled {
            continue;
        }
        match parse_address(&tag.address, tag.dtype) {
            Ok(a) => parsed.push((tag.name.clone(), a)),
            Err(reason) => {
                invalid.insert(tag.name.clone(), reason);
            }
        }
    }
    let (blocks, too_big) = plan_blocks(&parsed, p);
    invalid.extend(too_big);
    Plan { blocks: blocks.into_iter().map(|b| Arc::new(b) as BlockRef).collect(), invalid }
}

// --- decoder ---------------------------------------------------------------------------------

/// Per-tag decode failure (Python `DecodeError`); the driver maps it to `bad_decode`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError(pub String);

/// Decode one block reply into `{tag name: value or DecodeError}`. `data` are the reply's words
/// (unsigned, lowest device number first). No scaling is applied here.
pub fn decode_block(block: &McBlock, data: &[u16], p: &McParams) -> BTreeMap<String, Result<Value, DecodeError>> {
    let mut out = BTreeMap::new();
    for item in &block.items {
        let value = decode_item(item, data, p)
            .map_err(|reason| DecodeError(format!("{} ({}): {reason}", item.addr.text, item.addr.dtype.as_str())));
        for name in &item.names {
            out.insert(name.clone(), value.clone());
        }
    }
    out
}

/// Words (uint16, lowest device number first) to one unsigned integer.
pub fn combine_words(words: &[u16], first_word_low: bool) -> u64 {
    let mut u = 0u64;
    if first_word_low {
        for (i, w) in words.iter().enumerate() {
            u |= u64::from(*w) << (16 * i);
        }
    } else {
        for (i, w) in words.iter().rev().enumerate() {
            u |= u64::from(*w) << (16 * i);
        }
    }
    u
}

fn decode_item(item: &McItem, data: &[u16], p: &McParams) -> Result<Value, String> {
    let a = &item.addr;
    let lo = item.offset;
    let hi = item.offset + a.words as usize;
    if data.len() < hi {
        return Err(format!("short reply: need words up to offset {}, got {}", hi - 1, data.len()));
    }
    let words = &data[lo..hi];
    if let Some(len) = a.str_len {
        return Ok(Value::Str(decode_string(words, len as usize, a.str_order.unwrap_or('H'))));
    }
    if let Some(bit) = a.bit {
        return Ok(Value::Bool((combine_words(words, p.first_word_low) >> bit) & 1 == 1));
    }
    let n = match a.dtype {
        DType::Int16 | DType::Uint16 => 1,
        DType::Int32 | DType::Uint32 | DType::Float32 => 2,
        _ => 4,
    };
    let values: Vec<Value> = words.chunks(n).map(|c| decode_scalar(c, a.dtype, p)).collect();
    Ok(shape(values, &a.dims))
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

fn decode_scalar(words: &[u16], dtype: DType, p: &McParams) -> Value {
    let u = combine_words(words, p.first_word_low);
    match dtype {
        DType::Uint16 | DType::Uint32 => Value::Int(u as i64),
        DType::Uint64 => Value::from_u64(u),
        DType::Int16 => Value::Int(i64::from(u as u16 as i16)),
        DType::Int32 => Value::Int(i64::from(u as u32 as i32)),
        DType::Int64 => Value::Int(u as i64),
        DType::Float32 => Value::Float(float32(u as u32, p)),
        _ => Value::Float(float64(u, p)),
    }
}

fn float32(u: u32, p: &McParams) -> f64 {
    let (exp, mant) = ((u >> 23) & 0xFF, u & 0x7F_FFFF);
    let nonnormal = exp == 0xFF || (exp == 0 && mant != 0);
    if nonnormal && p.nonnormal_float == "zero" {
        return 0.0;
    }
    f64::from(f32::from_bits(u))
}

fn float64(u: u64, p: &McParams) -> f64 {
    let (exp, mant) = ((u >> 52) & 0x7FF, u & ((1u64 << 52) - 1));
    let nonnormal = exp == 0x7FF || (exp == 0 && mant != 0);
    if nonnormal && p.nonnormal_float == "zero" {
        return 0.0;
    }
    f64::from_bits(u)
}

fn decode_string(words: &[u16], length: usize, order: char) -> String {
    let mut raw: Vec<u8> = Vec::with_capacity(words.len() * 2);
    for w in words {
        let (hi, lo) = ((w >> 8) as u8, (w & 0xFF) as u8);
        if order == 'H' {
            raw.extend([hi, lo]);
        } else {
            raw.extend([lo, hi]);
        }
    }
    raw.truncate(length);
    if let Some(nul) = raw.iter().position(|b| *b == 0) {
        raw.truncate(nul);
    }
    raw.into_iter().map(char::from).collect() // latin-1
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tag(name: &str, address: &str, dtype: DType) -> Tag {
        Tag::new(name, address, dtype)
    }

    fn one(address: &str, dtype: DType, words: &[u16], p: &McParams) -> Result<Value, DecodeError> {
        let plan = plan_tags(p, &[tag("t", address, dtype)]);
        assert!(plan.invalid.is_empty() && plan.blocks.len() == 1, "{:?}", plan.invalid);
        let block = plan.blocks[0].as_any().downcast_ref::<McBlock>().unwrap();
        decode_block(block, words, p).remove("t").unwrap()
    }

    #[test]
    fn merges_within_the_gap_and_splits_beyond() {
        let p = McParams::default();
        let tags = [
            tag("a", "D0", DType::Uint16),
            tag("b", "D16", DType::Uint16),
            tag("c", "D40", DType::Uint16),
            tag("d", "M0", DType::Bool),
        ];
        let plan = plan_tags(&p, &tags);
        let d: Vec<String> = plan.blocks.iter().map(|b| b.describe()).collect();
        assert_eq!(d, ["D0..D16 (17 words)", "D40..D40 (1 words)", "M0..M15 (1 words)"]);
    }

    #[test]
    fn a_tag_wider_than_max_words_is_invalid() {
        let p = McParams { max_words: 4, ..McParams::default() };
        let plan = plan_tags(&p, &[tag("big", "D0[8]", DType::Uint16)]);
        assert_eq!(
            plan.invalid["big"],
            "melsec address 'D0[8]' (uint16): needs 8 words in one request, more than max_words=4 (MC 3E limit 960)"
        );
        assert!(plan.blocks.is_empty());
    }

    #[test]
    fn values_and_word_order() {
        let p = McParams::default();
        assert_eq!(one("D0", DType::Int16, &[0xFFFE], &p), Ok(Value::Int(-2)));
        assert_eq!(one("D0", DType::Uint32, &[2, 1], &p), Ok(Value::Int(0x0001_0002)));
        let q = McParams { first_word_low: false, ..McParams::default() };
        assert_eq!(one("D0", DType::Uint32, &[1, 2], &q), Ok(Value::Int(0x0001_0002)));
        // 12.5f32 = 0x41480000: low word first
        assert_eq!(one("D2", DType::Float32, &[0x0000, 0x4148], &p), Ok(Value::Float(12.5)));
        assert_eq!(one("D0", DType::Uint64, &[0xFFFF; 4], &p), Ok(Value::UInt(u64::MAX)));
    }

    #[test]
    fn nonnormal_floats_follow_the_param() {
        let nan = [0x0000, 0x7FC0];
        assert_eq!(one("D0", DType::Float32, &nan, &McParams::default()), Ok(Value::Float(0.0)));
        let keep = McParams { nonnormal_float: "unmodified".into(), ..McParams::default() };
        assert!(matches!(one("D0", DType::Float32, &nan, &keep), Ok(Value::Float(f)) if f.is_nan()));
    }

    #[test]
    fn bits_strings_and_arrays() {
        let p = McParams::default();
        assert_eq!(one("D100.05", DType::Bool, &[1 << 5], &p), Ok(Value::Bool(true)));
        assert_eq!(one("D100.17", DType::Bool, &[0, 2], &p), Ok(Value::Bool(true)));
        assert_eq!(
            one("DSH60.010", DType::String, &[0x5055, 0x4D50, 0x2041, 0, 0], &p),
            Ok(Value::Str("PUMP A".into()))
        );
        assert_eq!(one("DSL70:004", DType::String, &[0x6B6F, 0x0000], &p), Ok(Value::Str("ok".into())));
        assert_eq!(
            one("D0[2][2]", DType::Uint16, &[1, 2, 3, 4], &p),
            Ok(Value::List(vec![
                Value::List(vec![Value::Int(1), Value::Int(2)]),
                Value::List(vec![Value::Int(3), Value::Int(4)])
            ]))
        );
    }

    #[test]
    fn a_short_reply_is_a_decode_error_not_zeros() {
        let e = one("D0", DType::Uint32, &[1], &McParams::default()).unwrap_err();
        assert_eq!(e.0, "D0 (uint32): short reply: need words up to offset 1, got 1");
    }
}
