//! Block planner. Tags become reads of one memory area code: bits ride in their word (word
//! code), flags (TS, CS, TK) are one byte each (flag code), index registers four bytes. Per
//! area code, items sorted by address merge when the hole is at most `max_gap` elements and
//! the read stays within `min(request_size, 1998)` bytes (MEMORY AREA READ, 0101). When at
//! least two ranges are single elements, they are read together with MULTIPLE MEMORY AREA READ
//! (0104), up to 167 items per request: scattered tags then cost one request instead of one each.

use std::collections::BTreeMap;

use crate::address::FinsAddress;
use crate::params::{FinsParams, MAX_MULTI_ITEMS};

/// One distinct piece of data inside a range, served to every tag that reads it.
#[derive(Debug, Clone, PartialEq)]
pub struct FinsItem {
    pub names: Vec<String>,
    /// byte offset in the range's data
    pub offset: u32,
    pub addr: FinsAddress,
    pub dtype: String,
}

/// A contiguous read of `count` elements from `address` on, with memory area code `code`.
#[derive(Debug, Clone, PartialEq)]
pub struct FinsRange {
    pub code: u8,
    pub address: u16,
    pub count: u32,
    /// bytes per element: 1 (flag), 2 (word), 4 (index register)
    pub width: u32,
    pub items: Vec<FinsItem>,
    pub tags: Vec<String>,
}

/// KEPServerEX-style name of an area code + FINS address (for `describe`).
pub fn area_label(code: u8, address: u16) -> (String, u32) {
    let a = u32::from(address);
    match code {
        0xB0 => ("CIO".into(), a),
        0xB1 => ("W".into(), a),
        0xB2 => ("H".into(), a),
        0xB3 => ("A".into(), a),
        0x82 => ("D".into(), a),
        0x98 => ("E".into(), a),
        0x89 if a >= 0x8000 => ("C".into(), a - 0x8000),
        0x89 => ("T".into(), a),
        0x09 if a >= 0x8000 => ("CS".into(), a - 0x8000),
        0x09 => ("TS".into(), a),
        0x06 => ("TK".into(), a),
        0xDC => ("IR".into(), a - 0x100),
        0xBC => ("DR".into(), a - 0x200),
        0xA0..=0xAF => (format!("E{:02}:", code - 0xA0), a),
        0x60..=0x68 => (format!("E{:02}:", code - 0x60 + 0x10), a),
        _ => (format!("area 0x{code:02X} "), a),
    }
}

impl FinsRange {
    pub fn nbytes(&self) -> u32 {
        self.count * self.width
    }

    /// `D100..109 (10 words)`, `TS3 (1 flag)`, `IR2 (1 register)`.
    pub fn describe(&self) -> String {
        let (name, first) = area_label(self.code, self.address);
        let noun = match self.width {
            1 => "flag",
            4 => "register",
            _ => "word",
        };
        let at = if self.count == 1 { first.to_string() } else { format!("{first}..{}", first + self.count - 1) };
        format!("{name}{at} ({} {noun}{})", self.count, if self.count == 1 { "" } else { "s" })
    }
}

/// A planned request: one 0101 range, or several single-element ranges in one 0104.
#[derive(Debug, Clone, PartialEq)]
pub enum Request {
    Read(FinsRange),
    Multi(Vec<FinsRange>),
}

impl Request {
    pub fn ranges(&self) -> &[FinsRange] {
        match self {
            Request::Read(r) => std::slice::from_ref(r),
            Request::Multi(rs) => rs,
        }
    }
}

/// Merge parsed tags into requests. Returns (requests in plan order, invalid tag -> reason).
pub fn plan_requests(
    parsed: &[(String, FinsAddress, String)],
    p: &FinsParams,
) -> (Vec<Request>, BTreeMap<String, String>) {
    let max_bytes = p.max_bytes();
    let mut invalid = BTreeMap::new();
    // (code, address, width, elements) -> names, in first-seen order
    let mut keyed: BTreeMap<ItemKey, (FinsAddress, Vec<String>)> = BTreeMap::new();
    for (name, a, dtype) in parsed {
        let (code, address, width) = a.read_codes();
        let elements = a.words(Some(dtype.as_str()).filter(|d| !matches!(*d, "bool" | "string")));
        if elements * width > max_bytes {
            invalid.insert(
                name.clone(),
                format!(
                    "omron_fins address {} ({dtype}): needs {} bytes in one read, more than request_size {} allows",
                    opencap_core::pyfmt::py_repr_str(&a.text),
                    elements * width,
                    p.request_size
                ),
            );
            continue;
        }
        let key = (code, address, width, elements, dtype.clone(), FinsAddressKey::of(a));
        keyed.entry(key).or_insert_with(|| (a.clone(), Vec::new())).1.push(name.clone());
    }

    let mut ranges: Vec<FinsRange> = Vec::new();
    let mut run: Vec<(u16, u32, FinsItemSeed)> = Vec::new();
    let mut current: Option<(u8, u32)> = None; // (code, width)
    let mut end: u32 = 0;
    let flush =
        |run: &mut Vec<(u16, u32, FinsItemSeed)>, code: u8, width: u32, end: u32, ranges: &mut Vec<FinsRange>| {
            if run.is_empty() {
                return;
            }
            let start = run[0].0;
            let items: Vec<FinsItem> = run
                .drain(..)
                .map(|(address, _, seed)| {
                    let mut names = seed.names;
                    names.sort();
                    FinsItem {
                        names,
                        offset: (u32::from(address) - u32::from(start)) * width,
                        addr: seed.addr,
                        dtype: seed.dtype,
                    }
                })
                .collect();
            let mut tags: Vec<String> = items.iter().flat_map(|i| i.names.iter().cloned()).collect();
            tags.sort();
            ranges.push(FinsRange { code, address: start, count: end - u32::from(start), width, items, tags });
        };
    for ((code, address, width, elements, dtype, _), (addr, names)) in keyed {
        let (a_start, a_end) = (u32::from(address), u32::from(address) + elements);
        let same_area = current == Some((code, width));
        if same_area && !run.is_empty() {
            let start = u32::from(run[0].0);
            // area codes 0x89/0x09 hold timers below 0x8000 and counters above: never merged across
            let crosses = (start < 0x8000) != (a_start < 0x8000);
            if !crosses && a_start <= end + p.max_gap && end.max(a_end) - start <= max_bytes / width {
                end = end.max(a_end);
                run.push((address, elements, FinsItemSeed { addr, names, dtype }));
                continue;
            }
        }
        if let Some((c, w)) = current {
            flush(&mut run, c, w, end, &mut ranges);
        }
        current = Some((code, width));
        end = a_end;
        run.push((address, elements, FinsItemSeed { addr, names, dtype }));
    }
    if let Some((c, w)) = current {
        flush(&mut run, c, w, end, &mut ranges);
    }

    let single = |r: &FinsRange| r.count == 1;
    let pool: Vec<FinsRange> = if p.multi_read && ranges.iter().filter(|r| single(r)).count() >= 2 {
        ranges.iter().filter(|r| single(r)).cloned().collect()
    } else {
        Vec::new()
    };
    let mut requests: Vec<Request> =
        ranges.into_iter().filter(|r| !(single(r) && !pool.is_empty())).map(Request::Read).collect();
    let max_reply = p.max_bytes();
    let mut multi: Vec<FinsRange> = Vec::new();
    let mut reply = 0u32;
    for r in pool {
        let item = 1 + r.width;
        if !multi.is_empty() && (multi.len() >= MAX_MULTI_ITEMS || reply + item > max_reply) {
            requests.push(Request::Multi(std::mem::take(&mut multi)));
            reply = 0;
        }
        reply += item;
        multi.push(r);
    }
    match multi.len() {
        0 => {}
        1 => requests.push(Request::Read(multi.pop().expect("one"))),
        _ => requests.push(Request::Multi(multi)),
    }
    (requests, invalid)
}

/// (area code, address, element width, elements, dtype, the rest of the address): sorts by code,
/// then address, and identifies one wire item.
type ItemKey = (u8, u16, u32, u32, String, FinsAddressKey);

struct FinsItemSeed {
    addr: FinsAddress,
    names: Vec<String>,
    dtype: String,
}

/// What makes two tags the same item beyond code/address/size/dtype: bit, kind, string form.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct FinsAddressKey {
    kind: &'static str,
    bit: i16,
    dims: Vec<u32>,
    str_len: u32,
    str_order: char,
    bcd: bool,
}

impl FinsAddressKey {
    fn of(a: &FinsAddress) -> Self {
        Self {
            kind: a.kind.as_str(),
            bit: a.bit.map_or(-1, i16::from),
            dims: a.dims.clone(),
            str_len: a.str_len.unwrap_or(0),
            str_order: a.str_order.unwrap_or(' '),
            bcd: a.bcd,
        }
    }
}
