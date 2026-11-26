//! Planning (Python `AbLogixDriver.plan`, `LogixBlock`): distinct names grouped into reads of
//! at most `max_batch` names, and an estimate of the CIP round trips each read costs.

use std::any::Any;
use std::collections::BTreeMap;
use std::sync::Arc;

use opencap_core::config::{DType, Tag};
use opencap_core::driver::{Block, BlockRef, Plan};

use crate::address::{epath_size, parse_logix_address, LogixAddress};
use crate::params::AbParams;

/// A Multiple Service Packet starts with the service header, path and count: pycomm3's 10.
pub const MULTISERVICE_READ_OVERHEAD: usize = 10;
/// Sequence + reply header + type code, per fragment (estimate).
pub const FRAGMENT_REPLY_OVERHEAD: usize = 8;
/// A bit is read through its host integer: assume a DINT.
const BIT_HOST_SIZE: usize = 4;

/// Reply data bytes per value, from the configured dtype (CIP atomic sizes; STRING is the
/// 88-byte Logix STRING structure).
fn dtype_size(dtype: DType) -> usize {
    match dtype {
        DType::Bool => 1,
        DType::Int16 | DType::Uint16 => 2,
        DType::Int32 | DType::Uint32 | DType::Float32 => 4,
        DType::Int64 | DType::Uint64 | DType::Float64 => 8,
        DType::String => 88,
    }
}

/// One distinct name in a batch, with the sizes the packer uses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogixRead {
    pub name: String,
    /// Estimated reply data bytes.
    pub data_size: usize,
    /// Request EPATH bytes.
    pub path_size: usize,
}

impl LogixRead {
    /// Sequence (2) + service (1) + path + element count (2).
    pub fn message_size(&self) -> usize {
        2 + 1 + self.path_size + 2
    }

    pub fn fragments(&self, connection_size: usize) -> usize {
        let per_fragment = connection_size.saturating_sub(FRAGMENT_REPLY_OVERHEAD).max(1);
        self.data_size.div_ceil(per_fragment).max(1)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogixItem {
    pub tag: String,
    /// Position of its name in `LogixBlock::reads`.
    pub index: usize,
    pub dtype: DType,
    pub bit: Option<u32>,
}

/// One `read(*names)`: the names go out as Multiple Service Packets or single reads.
#[derive(Debug, Clone)]
pub struct LogixBlock {
    pub tags: Vec<String>,
    pub reads: Vec<LogixRead>,
    pub items: Vec<LogixItem>,
    pub micro800: bool,
    pub connection_size: usize,
}

impl LogixBlock {
    pub fn names(&self) -> Vec<&str> {
        self.reads.iter().map(|r| r.name.as_str()).collect()
    }

    /// Tags served by each name, in item order.
    pub fn name_to_tags(&self) -> BTreeMap<&str, Vec<&str>> {
        let mut out: BTreeMap<&str, Vec<&str>> = self.reads.iter().map(|r| (r.name.as_str(), vec![])).collect();
        for item in &self.items {
            out.entry(self.reads[item.index].name.as_str()).or_default().push(&item.tag);
        }
        out
    }

    /// Estimated CIP round trips (Python `wire_requests`).
    pub fn estimate_wire_requests(&self) -> usize {
        let conn = self.connection_size;
        if self.micro800 || self.reads.len() == 1 {
            return self
                .reads
                .iter()
                .map(|r| if r.data_size + r.message_size() > conn { r.fragments(conn) } else { 1 })
                .sum();
        }
        let (mut total, mut groups, mut in_group, mut current) = (0, 0, 0, MULTISERVICE_READ_OVERHEAD);
        for r in &self.reads {
            let size = r.data_size + r.message_size() + 2;
            if size > conn {
                total += r.fragments(conn);
                continue;
            }
            if current + size > conn {
                groups += usize::from(in_group > 0);
                (in_group, current) = (0, MULTISERVICE_READ_OVERHEAD);
            }
            in_group += 1;
            current += size;
        }
        groups += usize::from(in_group > 0);
        total + groups
    }
}

impl Block for LogixBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        let kind = if self.micro800 { "Micro800" } else { "Logix" };
        let w = self.estimate_wire_requests();
        format!(
            "{kind} read {} names / {} tags ({w} request{} @ {} B)",
            self.reads.len(),
            self.items.len(),
            if w == 1 { "" } else { "s" },
            self.connection_size
        )
    }

    fn wire_requests(&self) -> usize {
        self.estimate_wire_requests()
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

fn data_size(entries: &[(&Tag, LogixAddress)]) -> usize {
    entries.iter().map(|(t, a)| if a.bit.is_some() { BIT_HOST_SIZE } else { dtype_size(t.dtype) }).max().unwrap_or(0)
}

/// Group the enabled tags by read name and cut them into blocks.
pub fn plan_tags<'a>(params: &AbParams, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
    let mut invalid = BTreeMap::new();
    let mut by_name: BTreeMap<String, Vec<(&Tag, LogixAddress)>> = BTreeMap::new();
    for tag in tags {
        if !tag.enabled {
            continue;
        }
        match parse_logix_address(&tag.address, params.micro800) {
            Ok(a) if a.bit.is_some() && tag.dtype == DType::String => {
                invalid.insert(
                    tag.name.clone(),
                    format!("dtype string is not supported on a bit address ({})", tag.address),
                );
            }
            Ok(a) => by_name.entry(a.read_name.clone()).or_default().push((tag, a)),
            Err(e) => {
                invalid.insert(tag.name.clone(), e);
            }
        }
    }
    let names: Vec<&String> = by_name.keys().collect();
    let conn = params.plan_connection_size();
    let mut blocks: Vec<BlockRef> = Vec::new();
    for chunk in names.chunks(params.max_batch) {
        let reads: Vec<LogixRead> = chunk
            .iter()
            .map(|n| LogixRead { name: (*n).clone(), data_size: data_size(&by_name[*n]), path_size: epath_size(n) })
            .collect();
        let mut items: Vec<LogixItem> = chunk
            .iter()
            .enumerate()
            .flat_map(|(i, n)| {
                by_name[*n].iter().map(move |(t, a)| LogixItem {
                    tag: t.name.clone(),
                    index: i,
                    dtype: t.dtype,
                    bit: a.bit,
                })
            })
            .collect();
        items.sort_by(|a, b| (a.index, &a.tag).cmp(&(b.index, &b.tag)));
        blocks.push(Arc::new(LogixBlock {
            tags: items.iter().map(|i| i.tag.clone()).collect(),
            reads,
            items,
            micro800: params.micro800,
            connection_size: conn,
        }));
    }
    Plan { blocks, invalid }
}
