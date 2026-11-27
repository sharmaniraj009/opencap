//! Block planning (Python `plan_pccc`): merge PCCC addresses into file blocks, one read each.
//! Shared by the EtherNet/IP and DF1 drivers.

use std::any::Any;
use std::collections::BTreeMap;

use serde_json::{Map, Value as Json};

use opencap_core::config::{DType, Tag};
use opencap_core::driver::{Block, BlockRef, Plan};
use opencap_core::pyfmt::py_repr_json;
use std::sync::Arc;

use crate::address::{
    check_dtype, elem_size, parse_pccc_address, tc_fields, PcccAddress, PCCC_MAX_BYTES, STRING_MAX_COUNT,
};

#[derive(Debug, Clone, PartialEq)]
pub struct PcccItem {
    pub tag: String,
    /// element (I/O: word) index relative to the block start
    pub offset: usize,
    pub dtype: DType,
    pub bit: Option<u32>,
    pub field: Option<&'static str>,
}

#[derive(Debug, Clone)]
pub struct PcccBlock {
    pub file_type: &'static str,
    pub file_number: u32,
    pub start: u32,
    pub count: u32,
    pub elem_size: usize,
    /// read string, e.g. `F34:0{22}`
    pub address: String,
    pub items: Vec<PcccItem>,
    pub slot: Option<u32>,
    pub field: Option<&'static str>,
    tags: Vec<String>,
}

impl PcccBlock {
    pub fn nbytes(&self) -> usize {
        self.count as usize * self.elem_size
    }
}

impl Block for PcccBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        let span = if let Some(f) = self.field {
            format!("{}{}:{}.{}", self.file_type, self.file_number, self.start, f)
        } else if let Some(slot) = self.slot {
            format!("{} slot {slot} words {}..{}", self.file_type, self.start, self.start + self.count - 1)
        } else {
            format!("{}{} {}..{}", self.file_type, self.file_number, self.start, self.start + self.count - 1)
        };
        format!("{} ({span}, {} elem, {} B, {} tags)", self.address, self.count, self.nbytes(), self.tags.len())
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

/// `max_elements`, `max_gap`, `request_size` (and the EtherNet/IP-only `file_check`, `path`).
#[derive(Debug, Clone, PartialEq)]
pub struct PcccParams {
    pub max_elements: i64,
    pub max_gap: i64,
    pub max_bytes: usize,
    pub file_check: bool,
    pub path: Vec<String>,
}

fn int_param(p: &Map<String, Json>, key: &str, default: i64, min: i64, who: &str) -> Result<i64, String> {
    let v = match p.get(key) {
        None | Some(Json::Null) => return Ok(default),
        Some(Json::Bool(b)) => i64::from(*b),
        Some(Json::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(default),
        Some(Json::String(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| format!("invalid literal for int() with base 10: {}", opencap_core::pyfmt::py_repr_str(s)))?,
        Some(other) => return Err(format!("{who} param {key}={}: expected an integer", py_repr_json(other))),
    };
    if v < min {
        return Err(format!("{who} param {key}={v} must be >= {min}"));
    }
    Ok(v)
}

impl PcccParams {
    pub fn from_map(p: &Map<String, Json>, who: &str) -> Result<Self, String> {
        let file_check = match p.get("file_check") {
            None | Some(Json::Null) => true,
            Some(Json::Bool(b)) => *b,
            Some(v) => {
                let text = match v {
                    Json::String(s) => s.trim().to_lowercase(),
                    other => py_repr_json(other).to_lowercase(),
                };
                match text.as_str() {
                    "on" | "true" | "yes" | "1" => true,
                    "off" | "false" | "no" | "0" => false,
                    _ => return Err(format!("{who} param file_check={} must be true or false", py_repr_json(v))),
                }
            }
        };
        let path = match p.get("path") {
            None | Some(Json::Null) => Vec::new(),
            Some(route) => {
                let text = match route {
                    Json::String(s) => s.clone(),
                    other => py_repr_json(other),
                };
                let segs: Vec<String> =
                    text.split([',', '/', '\\']).map(str::trim).filter(|s| !s.is_empty()).map(String::from).collect();
                if segs.len() > 1 && segs.len() % 2 == 1 {
                    return Err(format!("{who} param path={}: a CIP route needs port/link pairs", py_repr_json(route)));
                }
                segs
            }
        };
        let max_elements = int_param(p, "max_elements", 100, 1, who)?;
        let max_gap = int_param(p, "max_gap", 4, 0, who)?;
        let max_bytes = int_param(p, "request_size", 232, 1, who)?.min(PCCC_MAX_BYTES as i64) as usize;
        Ok(Self { max_elements, max_gap, max_bytes, file_check, path })
    }
}

type Key = (String, u32, i64, i64, String);

fn group_key(a: &PcccAddress) -> Key {
    let ft = a.file_type.to_string();
    if a.file_type == "I" || a.file_type == "O" {
        (ft, a.file_number, i64::from(a.element), -1, String::new())
    } else if tc_fields(a.file_type).is_some() {
        (ft, a.file_number, -1, i64::from(a.element), a.field.unwrap_or("").to_string())
    } else {
        (ft, a.file_number, -1, -1, String::new())
    }
}

fn index(a: &PcccAddress) -> u32 {
    if a.file_type == "I" || a.file_type == "O" {
        a.word
    } else {
        a.element
    }
}

fn max_count(ft: &str, max_elements: i64, max_bytes: usize) -> u32 {
    if tc_fields(ft).is_some() {
        return 1;
    }
    let mut cap = (max_elements as usize).min(max_bytes / elem_size(ft));
    if ft == "ST" {
        cap = cap.min(STRING_MAX_COUNT);
    }
    cap.max(1) as u32
}

fn read_string(ft: &str, fnum: u32, slot: Option<u32>, start: u32, count: u32, field: Option<&str>) -> String {
    let suffix = if count > 1 { format!("{{{count}}}") } else { String::new() };
    if let Some(f) = field {
        return format!("{ft}{fnum}:{start}.{f}");
    }
    match ft {
        "I" | "O" => format!("{ft}:{}.{start}{suffix}", slot.unwrap_or(0)),
        "S" => format!("S:{start}{suffix}"),
        _ => format!("{ft}{fnum}:{start}{suffix}"),
    }
}

fn make_block(key: &Key, start: u32, last: u32, run: &[(PcccAddress, &Tag)]) -> PcccBlock {
    let ft: &'static str = run[0].0.file_type;
    let slot = (key.2 >= 0).then_some(key.2 as u32);
    let field: Option<&'static str> = if key.4.is_empty() { None } else { run[0].0.field };
    let count = last - start + 1;
    let items: Vec<PcccItem> = run
        .iter()
        .map(|(a, t)| PcccItem {
            tag: t.name.clone(),
            offset: (index(a) - start) as usize,
            dtype: t.dtype,
            bit: a.bit,
            field: a.field,
        })
        .collect();
    PcccBlock {
        file_type: ft,
        file_number: key.1,
        start,
        count,
        elem_size: elem_size(ft),
        address: read_string(ft, key.1, slot, start, count, field),
        tags: items.iter().map(|i| i.tag.clone()).collect(),
        items,
        slot,
        field,
    }
}

/// Parse PCCC addresses and merge them into file blocks (one PCCC read each). Pure.
pub fn plan_pccc<'a>(params: &PcccParams, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
    let mut invalid: BTreeMap<String, String> = BTreeMap::new();
    let mut groups: BTreeMap<Key, Vec<(PcccAddress, &Tag)>> = BTreeMap::new();
    for tag in tags {
        if !tag.enabled {
            continue;
        }
        let parsed = parse_pccc_address(&tag.address).and_then(|a| check_dtype(&a, tag.dtype).map(|()| a));
        match parsed {
            Ok(a) => groups.entry(group_key(&a)).or_default().push((a, tag)),
            Err(e) => {
                invalid.insert(tag.name.clone(), e);
            }
        }
    }
    let mut blocks: Vec<BlockRef> = Vec::new();
    for (key, mut entries) in groups {
        entries.sort_by(|x, y| {
            (index(&x.0), x.0.bit.map_or(-1, i64::from), &x.1.name).cmp(&(
                index(&y.0),
                y.0.bit.map_or(-1, i64::from),
                &y.1.name,
            ))
        });
        let cap = max_count(&key.0, params.max_elements, params.max_bytes);
        let mut run: Vec<(PcccAddress, &Tag)> = Vec::new();
        let (mut start, mut last) = (0u32, 0u32);
        for (a, tag) in entries {
            let idx = index(&a);
            if !run.is_empty() && i64::from(idx) - i64::from(last) - 1 <= params.max_gap && idx - start < cap {
                run.push((a, tag));
                last = idx;
                continue;
            }
            if !run.is_empty() {
                blocks.push(Arc::new(make_block(&key, start, last, &run)));
            }
            run = vec![(a, tag)];
            start = idx;
            last = idx;
        }
        if !run.is_empty() {
            blocks.push(Arc::new(make_block(&key, start, last, &run)));
        }
    }
    Plan { blocks, invalid }
}
