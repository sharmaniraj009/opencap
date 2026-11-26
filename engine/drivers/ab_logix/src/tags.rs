//! The controller's tag list: Symbol Object records, UDT templates, the existence check
//! (Python `resolve_read_name`) and the batching-probe candidates (`probe_names`).
//!
//! Pure parsing and lookups; the requests that fetch the data are in `client.rs`.

use std::collections::HashMap;

use opencap_core::pyfmt::py_repr_str;

use crate::cip::{atomic_size, status_description};

/// Symbol Object attributes asked for in the tag list upload (pycomm3's set): name, type,
/// symbol address, object address, software control, dimensions; plus external access (10)
/// from Logix revision 18.
pub const SYMBOL_ATTRIBUTES: [u16; 6] = [1, 2, 3, 5, 6, 8];
pub const ATTR_EXTERNAL_ACCESS: u16 = 10;
/// Template Object attributes: definition size (words), structure size (bytes), member count, handle.
pub const TEMPLATE_ATTRIBUTES: [u16; 4] = [4, 5, 2, 1];

const SYMBOL_SYSTEM_BIT: u16 = 0x1000;
/// Estimated reply bytes of a CIP SHORT_STRING (Micro800 STRING): 1 + up to 255 characters.
const SHORT_STRING_SIZE: usize = 256;

/// CIP data type of a tag or member: an atomic type code, or a structure (UDT) template id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Atomic(u8),
    Struct(u16),
}

/// Type and array dimensions (0 = scalar).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Node {
    pub kind: Kind,
    pub dims: u8,
}

impl Node {
    /// Decode a symbol/member type word: bit 15 structure (template id in the low 12 bits),
    /// bits 14-13 array dimensions, else the atomic code in the low byte.
    pub fn from_type(word: u16) -> Self {
        let dims = ((word >> 13) & 0x03) as u8;
        let kind = if word & 0x8000 != 0 { Kind::Struct(word & 0x0FFF) } else { Kind::Atomic((word & 0xFF) as u8) };
        Self { kind, dims }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TagInfo {
    pub node: Node,
    pub instance: u32,
    /// External Access is "None": not readable from outside the controller.
    pub no_access: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Member {
    pub name: String,
    pub node: Node,
    /// Array size (or, for a BOOL member, its bit number).
    pub info: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Template {
    pub name: String,
    pub handle: u16,
    /// Size of one structure in bytes.
    pub size: u32,
    pub members: Vec<Member>,
}

impl Template {
    /// The Logix STRING layout: a DINT `LEN` and a SINT array `DATA`.
    pub fn is_string(&self) -> bool {
        self.members.len() == 2
            && self.members.iter().any(|m| m.name == "LEN" && m.node == Node { kind: Kind::Atomic(0xC4), dims: 0 })
            && self
                .members
                .iter()
                .any(|m| m.name == "DATA" && matches!(m.node.kind, Kind::Atomic(0xC2)) && m.node.dims > 0)
    }
}

/// A tag list uploaded from one controller (`pycomm3` `plc.tags` / `plc.data_types`).
#[derive(Debug, Default)]
pub struct TagList {
    pub tags: HashMap<String, TagInfo>,
    pub templates: HashMap<u16, Template>,
    /// case-folded name -> the controller's spelling (first wins)
    folded: HashMap<String, String>,
}

impl TagList {
    pub fn new(tags: HashMap<String, TagInfo>, templates: HashMap<u16, Template>) -> Self {
        let mut folded = HashMap::new();
        let mut names: Vec<&String> = tags.keys().collect();
        names.sort();
        for n in names {
            folded.entry(n.to_lowercase()).or_insert_with(|| n.clone());
        }
        Self { tags, templates, folded }
    }

    pub fn len(&self) -> usize {
        self.tags.len()
    }

    pub fn is_empty(&self) -> bool {
        self.tags.is_empty()
    }
}

/// One Symbol Object record of a Get Instance Attribute List reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Symbol {
    pub instance: u32,
    pub name: String,
    pub symbol_type: u16,
    pub dims: [u32; 3],
    pub external_access: Option<u8>,
}

/// Parse the records of one reply (attributes in the order of [`SYMBOL_ATTRIBUTES`], plus
/// external access when `with_access`).
pub fn parse_symbols(data: &[u8], with_access: bool) -> Result<Vec<Symbol>, String> {
    let short = || "truncated Symbol Object record".to_string();
    let take = |off: &mut usize, n: usize| -> Result<&[u8], String> {
        let s = data.get(*off..*off + n).ok_or_else(short)?;
        *off += n;
        Ok(s)
    };
    let mut out = Vec::new();
    let mut off = 0;
    while off < data.len() {
        let instance = u32::from_le_bytes(take(&mut off, 4)?.try_into().unwrap());
        let name_len = u16::from_le_bytes(take(&mut off, 2)?.try_into().unwrap()) as usize;
        let name = take(&mut off, name_len)?.iter().map(|&b| b as char).collect();
        let symbol_type = u16::from_le_bytes(take(&mut off, 2)?.try_into().unwrap());
        take(&mut off, 4 + 4 + 4)?; // symbol address, object address, software control
        let d = take(&mut off, 12)?;
        let dims = [
            u32::from_le_bytes(d[0..4].try_into().unwrap()),
            u32::from_le_bytes(d[4..8].try_into().unwrap()),
            u32::from_le_bytes(d[8..12].try_into().unwrap()),
        ];
        let external_access = if with_access { Some(take(&mut off, 1)?[0]) } else { None };
        out.push(Symbol { instance, name, symbol_type, dims, external_access });
    }
    Ok(out)
}

/// A symbol a user can read: not a system symbol, not hidden (`__`), not a Map/Task/Routine/
/// I-O entry. `Program:X` entries are scopes, not tags (they are uploaded separately).
pub fn is_user_tag(sym: &Symbol) -> bool {
    sym.symbol_type & SYMBOL_SYSTEM_BIT == 0 && !sym.name.starts_with("__") && !sym.name.contains(':')
}

pub fn is_program_scope(sym: &Symbol) -> bool {
    sym.name.starts_with("Program:") && !sym.name.contains('.')
}

pub fn tag_info(sym: &Symbol) -> TagInfo {
    TagInfo {
        node: Node::from_type(sym.symbol_type),
        instance: sym.instance,
        no_access: sym.external_access == Some(2),
    }
}

/// Template attributes reply: (definition size in words, structure size, member count, handle).
pub fn parse_template_attributes(data: &[u8]) -> Result<(u32, u32, u16, u16), String> {
    let short = || "truncated template attribute reply".to_string();
    let n = u16::from_le_bytes(data.get(..2).ok_or_else(short)?.try_into().unwrap()) as usize;
    let mut off = 2;
    let (mut words, mut size, mut count, mut handle) = (0, 0, 0, 0);
    for _ in 0..n {
        let h = data.get(off..off + 4).ok_or_else(short)?;
        let (id, status) = (u16::from_le_bytes([h[0], h[1]]), u16::from_le_bytes([h[2], h[3]]));
        off += 4;
        if status != 0 {
            return Err(format!("template attribute {id}: {}", status_description(status as u8)));
        }
        let width = if id == 4 || id == 5 { 4 } else { 2 };
        let v = data.get(off..off + width).ok_or_else(short)?;
        off += width;
        let n = if width == 4 {
            u32::from_le_bytes(v.try_into().unwrap())
        } else {
            u16::from_le_bytes(v.try_into().unwrap()) as u32
        };
        match id {
            4 => words = n,
            5 => size = n,
            2 => count = n as u16,
            _ => handle = n as u16,
        }
    }
    Ok((words, size, count, handle))
}

/// Parse a template definition: `member_count` 8-byte records (info, type, offset), then the
/// template name (`Name;n`) and the member names, NUL separated. Hidden members (the host
/// bytes of packed BOOLs, `__` and `ZZZZZZZZZZ` names) are dropped.
pub fn parse_template(definition: &[u8], member_count: u16, handle: u16, size: u32) -> Result<Template, String> {
    let infos = member_count as usize * 8;
    if definition.len() < infos {
        return Err("template definition shorter than its member table".into());
    }
    let names: Vec<String> = definition[infos..]
        .split(|&b| b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| s.iter().map(|&b| b as char).collect())
        .collect();
    let (template_name, member_names) = names.split_first().ok_or("template without a name")?;
    let name = template_name.split(';').next().unwrap_or_default().to_string();
    let mut members = Vec::new();
    for i in 0..member_count as usize {
        let rec = &definition[i * 8..i * 8 + 8];
        let info = u16::from_le_bytes([rec[0], rec[1]]);
        let ty = u16::from_le_bytes([rec[2], rec[3]]);
        let member_name = member_names.get(i).cloned().unwrap_or_default();
        if member_name.is_empty() || member_name.starts_with("__") || member_name.starts_with("ZZZZZZZZZZ") {
            continue;
        }
        let mut node = Node::from_type(ty);
        if info > 0 && node.dims == 0 && !matches!(node.kind, Kind::Atomic(0xC1)) {
            node.dims = 1; // an array member whose type word carries no dimension bits
        }
        members.push(Member { name: member_name, node, info });
    }
    Ok(Template { name, handle, size, members })
}

/// Template ids a tag list refers to that are not in `known` yet (tags and, via `known`, nested
/// members are walked by the caller).
pub fn struct_ids_of(members: &[Member]) -> Vec<u16> {
    members.iter().filter_map(|m| if let Kind::Struct(id) = m.node.kind { Some(id) } else { None }).collect()
}

// --- the existence check -----------------------------------------------------------------------

/// A name that exists in the tag list: the controller's spelling, the type of what it reads and
/// the reply bytes to expect for one element.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved {
    pub name: String,
    pub node: Node,
    pub data_size: usize,
    /// The value is a Logix STRING structure.
    pub is_string: bool,
}

fn split_index(part: &str) -> (&str, &str) {
    match part.find('[') {
        Some(i) => (&part[..i], &part[i..]),
        None => (part, ""),
    }
}

fn leaf_size(node: Node, list: &TagList) -> (usize, bool) {
    match node.kind {
        Kind::Atomic(0xDA) => (SHORT_STRING_SIZE, false),
        Kind::Atomic(code) => (atomic_size(code).unwrap_or(4), false),
        Kind::Struct(id) => match list.templates.get(&id) {
            Some(t) => (t.size as usize, t.is_string()),
            None => (4, false),
        },
    }
}

/// Check a read name against the controller's tag list. `Ok` carries the controller's
/// spelling of the base tag and members (Logix names are case-insensitive, the lookup is
/// not); array indexes are left to the controller.
pub fn resolve_read_name(name: &str, list: &TagList) -> Result<Resolved, String> {
    let mut parts = name.split('.');
    let mut base = parts.next().unwrap_or_default().to_string();
    let mut attrs: Vec<&str> = parts.collect();
    if base.starts_with("Program:") {
        if attrs.is_empty() {
            return Err(format!("{}: program name without a tag", py_repr_str(name)));
        }
        base = format!("{base}.{}", attrs.remove(0));
    }
    let (ident, index) = split_index(&base);
    let mut ident = ident.to_string();
    let mut info = list.tags.get(&ident);
    if info.is_none() {
        if let Some(canon) = list.folded.get(&ident.to_lowercase()) {
            ident = canon.clone();
            info = list.tags.get(canon);
        }
    }
    let Some(info) = info else {
        return Err(format!("tag {} is not in the controller's tag list", py_repr_str(&ident)));
    };
    if info.no_access {
        return Err(format!("tag {} has External Access None", py_repr_str(&ident)));
    }
    let mut parts_out = vec![format!("{ident}{index}")];
    let mut node = info.node;
    for attr in attrs {
        let (member, index) = split_index(attr);
        let joined = parts_out.join(".");
        let Kind::Struct(id) = node.kind else {
            return Err(format!("{} is not a structure (no member {})", py_repr_str(&joined), py_repr_str(member)));
        };
        let Some(template) = list.templates.get(&id) else {
            return Err(format!("{} is not a structure (no member {})", py_repr_str(&joined), py_repr_str(member)));
        };
        let found = template
            .members
            .iter()
            .find(|m| m.name == member)
            .or_else(|| template.members.iter().find(|m| m.name.to_lowercase() == member.to_lowercase()));
        let Some(found) = found else {
            return Err(format!(
                "structure {} of {} has no member {}",
                template.name,
                py_repr_str(&joined),
                py_repr_str(member)
            ));
        };
        parts_out.push(format!("{}{index}", found.name));
        node = found.node;
    }
    let (data_size, is_string) = leaf_size(node, list);
    Ok(Resolved { name: parts_out.join("."), node, data_size, is_string })
}

const PROBE_CODES: [u8; 11] = [0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xCB];

/// Readable scalar atomic controller tags for the batching probe (sorted, deterministic);
/// one candidate is used twice, none gives an empty list.
pub fn probe_names(list: &TagList, count: usize) -> Vec<String> {
    let mut names: Vec<&String> = list.tags.keys().collect();
    names.sort();
    let mut picked: Vec<String> = Vec::new();
    for name in names {
        let t = &list.tags[name];
        if matches!(t.node.kind, Kind::Atomic(c) if PROBE_CODES.contains(&c))
            && t.node.dims == 0
            && !t.no_access
            && !name.starts_with("Program:")
        {
            picked.push(name.clone());
            if picked.len() == count {
                break;
            }
        }
    }
    if picked.len() == 1 {
        let only = picked[0].clone();
        picked.resize(count, only);
    }
    picked
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> TagList {
        let atomic = |code: u8, dims: u8| TagInfo {
            node: Node { kind: Kind::Atomic(code), dims },
            instance: 1,
            no_access: false,
        };
        let mut tags = HashMap::new();
        tags.insert("Real_1".to_string(), atomic(0xCA, 0));
        tags.insert("Dint_1".to_string(), atomic(0xC4, 0));
        tags.insert("Arr".to_string(), atomic(0xCA, 1));
        tags.insert("Secret".to_string(), TagInfo { no_access: true, ..atomic(0xC4, 0) });
        tags.insert(
            "Pump".to_string(),
            TagInfo { node: Node { kind: Kind::Struct(0x10), dims: 0 }, instance: 2, no_access: false },
        );
        tags.insert("Program:Main.Count".to_string(), atomic(0xC4, 0));
        let pump = Template {
            name: "PumpUdt".into(),
            handle: 0x1234,
            size: 8,
            members: vec![
                Member { name: "Run".into(), node: Node { kind: Kind::Atomic(0xC1), dims: 0 }, info: 0 },
                Member { name: "Speed".into(), node: Node { kind: Kind::Atomic(0xCA), dims: 0 }, info: 0 },
            ],
        };
        TagList::new(tags, [(0x10, pump)].into_iter().collect())
    }

    #[test]
    fn existing_names_resolve_with_the_controllers_spelling() {
        let l = sample();
        assert_eq!(resolve_read_name("Real_1", &l).unwrap().name, "Real_1");
        assert_eq!(resolve_read_name("REAL_1", &l).unwrap().name, "Real_1");
        assert_eq!(resolve_read_name("pump.speed", &l).unwrap().name, "Pump.Speed");
        assert_eq!(resolve_read_name("Arr[7]", &l).unwrap().name, "Arr[7]");
        assert_eq!(resolve_read_name("Pump.Run", &l).unwrap().data_size, 1);
        assert_eq!(resolve_read_name("Program:Main.Count", &l).unwrap().data_size, 4);
    }

    #[test]
    fn missing_names_say_why() {
        let l = sample();
        assert_eq!(resolve_read_name("Nope", &l).unwrap_err(), "tag 'Nope' is not in the controller's tag list");
        assert_eq!(resolve_read_name("Secret", &l).unwrap_err(), "tag 'Secret' has External Access None");
        assert_eq!(resolve_read_name("Pump.Gone", &l).unwrap_err(), "structure PumpUdt of 'Pump' has no member 'Gone'");
        assert_eq!(resolve_read_name("Real_1.X", &l).unwrap_err(), "'Real_1' is not a structure (no member 'X')");
        assert!(resolve_read_name("Program:Main", &l).unwrap_err().contains("program name without a tag"));
    }

    #[test]
    fn probe_picks_two_sorted_scalars() {
        let l = sample();
        assert_eq!(probe_names(&l, 2), vec!["Dint_1", "Real_1"]);
        let one = TagList::new([("Only".to_string(), l.tags["Real_1"].clone())].into_iter().collect(), HashMap::new());
        assert_eq!(probe_names(&one, 2), vec!["Only", "Only"]);
        assert!(probe_names(&TagList::default(), 2).is_empty());
    }

    #[test]
    fn symbol_records_and_user_filter() {
        let mut rec = Vec::new();
        for (inst, name, ty) in [(0x40u32, "Tag_A", 0x00CAu16), (0x43, "__Hidden", 0xC4), (0x46, "Map:Local", 0x1069)] {
            rec.extend_from_slice(&inst.to_le_bytes());
            rec.extend_from_slice(&(name.len() as u16).to_le_bytes());
            rec.extend_from_slice(name.as_bytes());
            rec.extend_from_slice(&ty.to_le_bytes());
            rec.extend_from_slice(&[0; 12]);
            rec.extend_from_slice(&[0; 12]);
            rec.push(2);
        }
        let syms = parse_symbols(&rec, true).unwrap();
        assert_eq!(syms.len(), 3);
        assert!(is_user_tag(&syms[0]) && !is_user_tag(&syms[1]) && !is_user_tag(&syms[2]));
        assert!(tag_info(&syms[0]).no_access);
        assert!(parse_symbols(&rec[..10], true).is_err());
    }

    #[test]
    fn template_definition_parses() {
        let mut def = Vec::new();
        for (info, ty, off) in [(0u16, 0xCAu16, 0u32), (0, 0xC2, 4), (3, 0xC1, 4), (4, 0xCA, 8)] {
            def.extend_from_slice(&info.to_le_bytes());
            def.extend_from_slice(&ty.to_le_bytes());
            def.extend_from_slice(&off.to_le_bytes());
        }
        def.extend_from_slice(b"SimUdt;n\0TOTAL\0ZZZZZZZZZZSimUdt0\0FAULT\0HIST\0");
        let t = parse_template(&def, 4, 0x77, 24).unwrap();
        assert_eq!(t.name, "SimUdt");
        let names: Vec<&str> = t.members.iter().map(|m| m.name.as_str()).collect();
        assert_eq!(names, vec!["TOTAL", "FAULT", "HIST"]);
        assert_eq!(t.members[2].node.dims, 1);
        assert!(!t.is_string());
    }
}
