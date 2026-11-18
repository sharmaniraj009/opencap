//! Block planner (Python `plan_blocks`): greedy over items sorted by their key; an item
//! joins the open block when it has the same FC, starts at most `max_gap_for(fc)` after
//! the block end, and the merged span stays within `max_span(fc)`. Tags at the same
//! address are one wire item served to every name. Output is independent of input order.

use std::any::Any;
use std::collections::{BTreeMap, HashMap};

use opencap_core::driver::Block;

use super::address::{AddrKey, ModbusAddress};
use super::params::{protocol_max, ModbusParams};

/// One distinct piece of data inside a block, served to every tag name that reads it.
#[derive(Debug, Clone, PartialEq)]
pub struct ModbusItem {
    pub names: Vec<String>,
    /// first register/bit of the item, relative to the block start
    pub offset: u32,
    pub addr: ModbusAddress,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ModbusBlock {
    pub fc: u8,
    /// protocol start address
    pub start: u32,
    /// registers or bits
    pub count: u32,
    pub items: Vec<ModbusItem>,
    /// names of every tag the block serves, sorted
    pub tags: Vec<String>,
}

impl ModbusBlock {
    pub fn is_bits(&self) -> bool {
        self.fc == 1 || self.fc == 2
    }
}

impl Block for ModbusBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        let unit = if self.is_bits() { "bits" } else { "regs" };
        format!("FC{:02} {}..{} ({} {unit})", self.fc, self.start, self.start + self.count - 1, self.count)
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

/// Merge parsed tags into wire blocks. Returns (blocks, invalid): invalid = tags that fit no block.
pub fn plan_blocks(
    parsed: &[(String, ModbusAddress)],
    p: &ModbusParams,
) -> (Vec<ModbusBlock>, BTreeMap<String, String>) {
    let mut invalid = BTreeMap::new();
    let mut names_by_key: HashMap<AddrKey, Vec<String>> = HashMap::new();
    let mut addr_by_key: BTreeMap<AddrKey, ModbusAddress> = BTreeMap::new();
    for (name, a) in parsed {
        let limit = p.max_span(a.fc);
        if a.count > limit {
            let unit = if a.is_bits() { "bits" } else { "registers" };
            let param = ModbusParams::limit_param(a.fc);
            invalid.insert(
                name.clone(),
                format!(
                    "modbus address {} ({}): needs {} {unit} in one request, more than the limit {limit} ({param}={}, protocol max {})",
                    opencap_core::pyfmt::py_repr_str(&a.text),
                    a.dtype,
                    a.count,
                    p.limit_value(a.fc),
                    protocol_max(a.fc)
                ),
            );
            continue;
        }
        let key = a.key();
        names_by_key.entry(key.clone()).or_default().push(name.clone());
        addr_by_key.entry(key).or_insert_with(|| a.clone());
    }

    let mut blocks = Vec::new();
    let mut group: Vec<(ModbusAddress, Vec<String>)> = Vec::new();
    let mut end: u32 = 0;

    fn close(blocks: &mut Vec<ModbusBlock>, group: &[(ModbusAddress, Vec<String>)], end: u32) {
        let start = group[0].0.address;
        let items: Vec<ModbusItem> = group
            .iter()
            .map(|(a, names)| ModbusItem { names: names.clone(), offset: a.address - start, addr: a.clone() })
            .collect();
        let mut tags: Vec<String> = items.iter().flat_map(|it| it.names.iter().cloned()).collect();
        tags.sort();
        blocks.push(ModbusBlock { fc: group[0].0.fc, start, count: end - start, items, tags });
    }

    for (key, a) in &addr_by_key {
        let mut names = names_by_key[key].clone();
        names.sort();
        if let Some((first, _)) = group.first() {
            let gap = a.address as i64 - end as i64;
            if a.fc == first.fc && gap <= p.max_gap_for(a.fc) && end.max(a.end()) - first.address <= p.max_span(a.fc) {
                end = end.max(a.end());
                group.push((a.clone(), names));
                continue;
            }
            close(&mut blocks, &group, end);
        }
        end = a.end();
        group = vec![(a.clone(), names)];
    }
    if !group.is_empty() {
        close(&mut blocks, &group, end);
    }
    (blocks, invalid)
}
