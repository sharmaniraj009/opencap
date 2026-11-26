//! EtherNet/IP encapsulation and CIP message codec: the pure byte layer of the driver (no
//! socket). Everything here is a builder or a parser over byte slices, so it is unit tested
//! against hand-made frames and against the simulator's wire format.
//!
//! READ-ONLY BY CONSTRUCTION. The only way to make an object-level CIP request is
//! [`ObjectService`], an enum of read services (Read Tag, Read Tag Fragmented, Multiple
//! Service Packet of reads, Get Attributes All, Get Attribute List, Get Instance Attribute
//! List). There is no builder for Write Tag (0x4D), Write Tag Fragmented (0x53), Read-Modify-
//! Write (0x4E on the Message Router) or Set Attribute (0x02/0x04/0x10), and [`object_request`]
//! refuses a service code in [`WRITE_SERVICES`] even if the enum were extended by mistake. The
//! connection services (Forward Open/Close, RegisterSession) cannot touch tag data.

use opencap_core::value::Value;

pub const ENCAP_HEADER_LEN: usize = 24;

pub const CMD_LIST_IDENTITY: u16 = 0x0063;
pub const CMD_REGISTER_SESSION: u16 = 0x0065;
pub const CMD_UNREGISTER_SESSION: u16 = 0x0066;
pub const CMD_SEND_RR_DATA: u16 = 0x006F;
pub const CMD_SEND_UNIT_DATA: u16 = 0x0070;

const ITEM_NULL: u16 = 0x0000;
const ITEM_CONNECTED_ADDRESS: u16 = 0x00A1;
const ITEM_CONNECTED_DATA: u16 = 0x00B1;
const ITEM_UNCONNECTED_DATA: u16 = 0x00B2;
const ITEM_IDENTITY: u16 = 0x000C;

/// General status codes the driver reacts to.
pub const STATUS_OK: u8 = 0x00;
pub const STATUS_PARTIAL: u8 = 0x06;
pub const STATUS_SERVICE_NOT_SUPPORTED: u8 = 0x08;
pub const STATUS_EMBEDDED_ERROR: u8 = 0x1E;
pub const STATUS_GENERAL: u8 = 0xFF;

/// CIP services that write or change something. Never built, never sent (Python
/// `WRITE_SERVICES`; 0x4E is Forward Close on the Connection Manager, so it is guarded by
/// there being no Message Router builder for it).
pub const WRITE_SERVICES: [u8; 6] = [0x02, 0x04, 0x10, 0x4D, 0x4E, 0x53];

const SVC_GET_ATTRIBUTES_ALL: u8 = 0x01;
const SVC_GET_ATTRIBUTE_LIST: u8 = 0x03;
const SVC_MULTIPLE: u8 = 0x0A;
const SVC_GET_INSTANCE_ATTRIBUTE_LIST: u8 = 0x55;
const SVC_READ_TAG: u8 = 0x4C;
const SVC_READ_FRAGMENTED: u8 = 0x52;
const CM_FORWARD_CLOSE: u8 = 0x4E;
const CM_FORWARD_OPEN: u8 = 0x54;
const CM_LARGE_FORWARD_OPEN: u8 = 0x5B;

pub const CLASS_IDENTITY: u16 = 0x01;
pub const CLASS_MESSAGE_ROUTER: u16 = 0x02;
pub const CLASS_CONNECTION_MANAGER: u16 = 0x06;
pub const CLASS_PROGRAM_NAME: u16 = 0x64;
pub const CLASS_SYMBOL: u16 = 0x6B;
pub const CLASS_TEMPLATE: u16 = 0x6C;

/// Vendor id this client puts in its Forward Open (Rockwell's is 1; any id is accepted).
const ORIGINATOR_VENDOR: u16 = 0x4F43;

// --- encapsulation ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncapHeader {
    pub command: u16,
    pub length: u16,
    pub session: u32,
    pub status: u32,
    pub context: [u8; 8],
}

pub fn encap(command: u16, session: u32, context: [u8; 8], payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(ENCAP_HEADER_LEN + payload.len());
    out.extend_from_slice(&command.to_le_bytes());
    out.extend_from_slice(&(payload.len() as u16).to_le_bytes());
    out.extend_from_slice(&session.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&context);
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(payload);
    out
}

pub fn parse_encap_header(buf: &[u8]) -> Result<EncapHeader, String> {
    if buf.len() < ENCAP_HEADER_LEN {
        return Err("short encapsulation header".into());
    }
    Ok(EncapHeader {
        command: u16::from_le_bytes([buf[0], buf[1]]),
        length: u16::from_le_bytes([buf[2], buf[3]]),
        session: u32::from_le_bytes(buf[4..8].try_into().unwrap()),
        status: u32::from_le_bytes(buf[8..12].try_into().unwrap()),
        context: buf[12..20].try_into().unwrap(),
    })
}

pub fn register_session_payload() -> Vec<u8> {
    vec![1, 0, 0, 0] // protocol version 1, options 0
}

/// Common Packet Format: interface handle, timeout, item count, items.
fn cpf(items: &[(u16, &[u8])]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&(items.len() as u16).to_le_bytes());
    for (ty, data) in items {
        out.extend_from_slice(&ty.to_le_bytes());
        out.extend_from_slice(&(data.len() as u16).to_le_bytes());
        out.extend_from_slice(data);
    }
    out
}

pub fn parse_cpf(body: &[u8]) -> Result<Vec<(u16, Vec<u8>)>, String> {
    if body.len() < 8 {
        return Err("short common packet".into());
    }
    let count = u16::from_le_bytes([body[6], body[7]]) as usize;
    let (mut off, mut items) = (8, Vec::with_capacity(count));
    for _ in 0..count {
        if off + 4 > body.len() {
            return Err("truncated common packet item".into());
        }
        let ty = u16::from_le_bytes([body[off], body[off + 1]]);
        let len = u16::from_le_bytes([body[off + 2], body[off + 3]]) as usize;
        off += 4;
        if off + len > body.len() {
            return Err("truncated common packet item".into());
        }
        items.push((ty, body[off..off + len].to_vec()));
        off += len;
    }
    Ok(items)
}

/// SendRRData body (unconnected): null address item + the CIP message.
pub fn send_rr_data_payload(cip: &[u8]) -> Vec<u8> {
    cpf(&[(ITEM_NULL, &[]), (ITEM_UNCONNECTED_DATA, cip)])
}

/// SendUnitData body (connected): the connection id + sequence count and CIP message.
pub fn send_unit_data_payload(ot_connection_id: u32, seq: u16, cip: &[u8]) -> Vec<u8> {
    let mut data = seq.to_le_bytes().to_vec();
    data.extend_from_slice(cip);
    cpf(&[(ITEM_CONNECTED_ADDRESS, &ot_connection_id.to_le_bytes()), (ITEM_CONNECTED_DATA, &data)])
}

/// The CIP message of an unconnected reply.
pub fn parse_rr_reply(body: &[u8]) -> Result<Vec<u8>, String> {
    parse_cpf(body)?
        .into_iter()
        .find(|(t, _)| *t == ITEM_UNCONNECTED_DATA)
        .map(|(_, d)| d)
        .ok_or_else(|| "no unconnected data item in the reply".into())
}

/// (sequence count, CIP message) of a connected reply.
pub fn parse_unit_reply(body: &[u8]) -> Result<(u16, Vec<u8>), String> {
    let (_, data) = parse_cpf(body)?
        .into_iter()
        .find(|(t, _)| *t == ITEM_CONNECTED_DATA)
        .ok_or("no connected data item in the reply")?;
    if data.len() < 2 {
        return Err("connected reply without a sequence count".into());
    }
    Ok((u16::from_le_bytes([data[0], data[1]]), data[2..].to_vec()))
}

// --- identity --------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub vendor: u16,
    pub device_type: u16,
    pub product_code: u16,
    pub major: u8,
    pub minor: u8,
    pub status: u16,
    pub serial: u32,
    pub product_name: String,
}

impl Identity {
    pub fn revision(&self) -> String {
        format!("{}.{}", self.major, self.minor)
    }

    pub fn vendor_name(&self) -> String {
        match self.vendor {
            1 => "Rockwell Automation/Allen-Bradley".into(),
            n => format!("vendor {n}"),
        }
    }

    pub fn product_type(&self) -> Option<&'static str> {
        (self.device_type == 0x0E).then_some("Programmable Logic Controller")
    }

    /// The controller family (Micro800 catalog numbers start with 2080).
    pub fn is_micro800(&self) -> bool {
        self.product_name.starts_with("2080")
    }
}

/// ListIdentity reply body: item count, then the identity item.
pub fn parse_list_identity(body: &[u8]) -> Result<Identity, String> {
    let short = || "short ListIdentity reply".to_string();
    if body.len() < 2 {
        return Err(short());
    }
    let mut off = 2;
    let count = u16::from_le_bytes([body[0], body[1]]);
    for _ in 0..count {
        if off + 4 > body.len() {
            return Err(short());
        }
        let ty = u16::from_le_bytes([body[off], body[off + 1]]);
        let len = u16::from_le_bytes([body[off + 2], body[off + 3]]) as usize;
        off += 4;
        let item = body.get(off..off + len).ok_or_else(short)?;
        off += len;
        if ty != ITEM_IDENTITY {
            continue;
        }
        // protocol version (2), socket address (16), then the identity object attributes
        let a = item.get(18..).ok_or_else(short)?;
        if a.len() < 15 {
            return Err(short());
        }
        let name_len = a[14] as usize;
        let name = a.get(15..15 + name_len).ok_or_else(short)?;
        return Ok(Identity {
            vendor: u16::from_le_bytes([a[0], a[1]]),
            device_type: u16::from_le_bytes([a[2], a[3]]),
            product_code: u16::from_le_bytes([a[4], a[5]]),
            major: a[6],
            minor: a[7],
            status: u16::from_le_bytes([a[8], a[9]]),
            serial: u32::from_le_bytes(a[10..14].try_into().unwrap()),
            product_name: name.iter().map(|&b| b as char).collect(),
        });
    }
    Err("no identity item in the ListIdentity reply".into())
}

// --- paths -----------------------------------------------------------------------------------

/// Logical class segment.
fn class_segment(class: u16) -> Vec<u8> {
    if class <= 0xFF {
        vec![0x20, class as u8]
    } else {
        vec![0x21, 0, class as u8, (class >> 8) as u8]
    }
}

/// Logical instance segment.
fn instance_segment(instance: u32) -> Vec<u8> {
    if instance <= 0xFF {
        vec![0x24, instance as u8]
    } else if instance <= 0xFFFF {
        vec![0x25, 0, instance as u8, (instance >> 8) as u8]
    } else {
        let mut v = vec![0x26, 0];
        v.extend_from_slice(&instance.to_le_bytes());
        v
    }
}

pub fn class_instance_path(class: u16, instance: u32) -> Vec<u8> {
    [class_segment(class), instance_segment(instance)].concat()
}

/// ANSI extended symbol segment.
fn symbol_segment(name: &str, out: &mut Vec<u8>) {
    out.push(0x91);
    out.push(name.len() as u8);
    out.extend_from_slice(name.as_bytes());
    if name.len() % 2 == 1 {
        out.push(0);
    }
}

/// Member (array index) segment.
fn member_segment(index: u32, out: &mut Vec<u8>) {
    if index <= 0xFF {
        out.extend_from_slice(&[0x28, index as u8]);
    } else if index <= 0xFFFF {
        out.extend_from_slice(&[0x29, 0, index as u8, (index >> 8) as u8]);
    } else {
        out.extend_from_slice(&[0x2A, 0]);
        out.extend_from_slice(&index.to_le_bytes());
    }
}

/// The symbolic request path of a tag name (`Program:Main.Tag[1,2].Member`): one symbol segment
/// per dotted part (a program scope is a segment of its own), one member segment per array index.
pub fn symbolic_path(name: &str) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    for part in name.split('.') {
        let (ident, idx) = part.split_once('[').map_or((part, None), |(a, b)| (a, Some(b)));
        if ident.is_empty() || ident.len() > 255 {
            return Err(format!("illegal symbol in {name:?}"));
        }
        symbol_segment(ident, &mut out);
        if let Some(idx) = idx {
            for i in idx.trim_end_matches(']').split(',') {
                member_segment(i.parse().map_err(|_| format!("illegal index in {name:?}"))?, &mut out);
            }
        }
    }
    Ok(out)
}

// --- requests --------------------------------------------------------------------------------

/// Object-level services this client can send: all reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObjectService {
    GetAttributesAll,
    GetAttributeList,
    MultipleService,
    GetInstanceAttributeList,
    ReadTag,
    ReadTagFragmented,
}

impl ObjectService {
    pub const ALL: [ObjectService; 6] = [
        ObjectService::GetAttributesAll,
        ObjectService::GetAttributeList,
        ObjectService::MultipleService,
        ObjectService::GetInstanceAttributeList,
        ObjectService::ReadTag,
        ObjectService::ReadTagFragmented,
    ];

    pub fn code(self) -> u8 {
        match self {
            ObjectService::GetAttributesAll => SVC_GET_ATTRIBUTES_ALL,
            ObjectService::GetAttributeList => SVC_GET_ATTRIBUTE_LIST,
            ObjectService::MultipleService => SVC_MULTIPLE,
            ObjectService::GetInstanceAttributeList => SVC_GET_INSTANCE_ATTRIBUTE_LIST,
            ObjectService::ReadTag => SVC_READ_TAG,
            ObjectService::ReadTagFragmented => SVC_READ_FRAGMENTED,
        }
    }
}

fn raw_request(service: u8, path: &[u8], data: &[u8]) -> Vec<u8> {
    debug_assert!(path.len() % 2 == 0, "EPATH is a whole number of words");
    let mut out = vec![service, (path.len() / 2) as u8];
    out.extend_from_slice(path);
    out.extend_from_slice(data);
    out
}

/// Build one object-level request. A write service code is refused with an error.
pub fn object_request(service: ObjectService, path: &[u8], data: &[u8]) -> Result<Vec<u8>, String> {
    if WRITE_SERVICES.contains(&service.code()) {
        return Err(format!("opencap is read-only: CIP service {:#04x} refused", service.code()));
    }
    Ok(raw_request(service.code(), path, data))
}

pub fn read_tag_request(path: &[u8], elements: u16) -> Result<Vec<u8>, String> {
    object_request(ObjectService::ReadTag, path, &elements.to_le_bytes())
}

pub fn read_fragmented_request(path: &[u8], elements: u16, offset: u32) -> Result<Vec<u8>, String> {
    let mut data = elements.to_le_bytes().to_vec();
    data.extend_from_slice(&offset.to_le_bytes());
    object_request(ObjectService::ReadTagFragmented, path, &data)
}

/// Multiple Service Packet of already built (read) requests.
pub fn multiple_service_request(requests: &[Vec<u8>]) -> Result<Vec<u8>, String> {
    for r in requests {
        let code = r.first().copied().unwrap_or(0);
        if WRITE_SERVICES.contains(&code) {
            return Err(format!("opencap is read-only: CIP service {code:#04x} refused"));
        }
    }
    let n = requests.len();
    let mut data = (n as u16).to_le_bytes().to_vec();
    let mut offset = 2 + 2 * n;
    for r in requests {
        data.extend_from_slice(&(offset as u16).to_le_bytes());
        offset += r.len();
    }
    for r in requests {
        data.extend_from_slice(r);
    }
    object_request(ObjectService::MultipleService, &class_instance_path(CLASS_MESSAGE_ROUTER, 1), &data)
}

pub fn get_instance_attribute_list_request(path: &[u8], attrs: &[u16]) -> Result<Vec<u8>, String> {
    let mut data = (attrs.len() as u16).to_le_bytes().to_vec();
    for a in attrs {
        data.extend_from_slice(&a.to_le_bytes());
    }
    object_request(ObjectService::GetInstanceAttributeList, path, &data)
}

pub fn get_attribute_list_request(path: &[u8], attrs: &[u16]) -> Result<Vec<u8>, String> {
    let mut data = (attrs.len() as u16).to_le_bytes().to_vec();
    for a in attrs {
        data.extend_from_slice(&a.to_le_bytes());
    }
    object_request(ObjectService::GetAttributeList, path, &data)
}

pub fn get_attributes_all_request(class: u16, instance: u32) -> Result<Vec<u8>, String> {
    object_request(ObjectService::GetAttributesAll, &class_instance_path(class, instance), &[])
}

/// Read Tag on a template: byte offset (DINT) and byte count.
pub fn read_template_request(template: u16, offset: u32, count: u16) -> Result<Vec<u8>, String> {
    let mut data = offset.to_le_bytes().to_vec();
    data.extend_from_slice(&count.to_le_bytes());
    object_request(ObjectService::ReadTag, &class_instance_path(CLASS_TEMPLATE, template as u32), &data)
}

// --- Connection Manager ------------------------------------------------------------------------

fn cm_path() -> Vec<u8> {
    class_instance_path(CLASS_CONNECTION_MANAGER, 1)
}

/// Encode one route hop (port segment).
pub fn hop_segment(port: u8, link: &crate::params::Link) -> Vec<u8> {
    match link {
        crate::params::Link::Number(n) => vec![port & 0x0F, *n],
        crate::params::Link::Text(s) => {
            let mut v = vec![0x10 | (port & 0x0F), s.len() as u8];
            v.extend_from_slice(s.as_bytes());
            if s.len() % 2 == 1 {
                v.push(0);
            }
            v
        }
    }
}

/// Connection path of a Forward Open: the route to the controller, then its Message Router.
pub fn connection_path(route: &[crate::params::Hop]) -> Vec<u8> {
    let mut out: Vec<u8> = route.iter().flat_map(|h| hop_segment(h.port, &h.link)).collect();
    out.extend(class_instance_path(CLASS_MESSAGE_ROUTER, 1));
    out
}

/// Parameters of one Forward Open.
#[derive(Debug, Clone)]
pub struct ForwardOpenParams {
    pub large: bool,
    /// Connection size in bytes.
    pub size: usize,
    pub connection_serial: u16,
    pub originator_serial: u32,
    pub to_connection_id: u32,
    pub path: Vec<u8>,
}

pub fn forward_open_request(p: &ForwardOpenParams) -> Vec<u8> {
    let mut d = vec![0x0A, 0x0E]; // priority/time tick, time-out ticks
    d.extend_from_slice(&0u32.to_le_bytes()); // O->T connection id: the target chooses
    d.extend_from_slice(&p.to_connection_id.to_le_bytes());
    d.extend_from_slice(&p.connection_serial.to_le_bytes());
    d.extend_from_slice(&ORIGINATOR_VENDOR.to_le_bytes());
    d.extend_from_slice(&p.originator_serial.to_le_bytes());
    d.extend_from_slice(&[7, 0, 0, 0]); // timeout multiplier 7 (x512), reserved
    let rpi: u32 = 2_000_000; // microseconds; with the multiplier the PLC drops an idle connection after ~17 min
    let params_word = |size: usize| -> Vec<u8> {
        if p.large {
            (0x4200_0000u32 | size as u32).to_le_bytes().to_vec()
        } else {
            (0x4200u16 | size as u16).to_le_bytes().to_vec()
        }
    };
    d.extend_from_slice(&rpi.to_le_bytes());
    d.extend(params_word(p.size));
    d.extend_from_slice(&rpi.to_le_bytes());
    d.extend(params_word(p.size));
    d.push(0xA3); // transport class 3, server trigger
    d.push((p.path.len() / 2) as u8);
    d.extend_from_slice(&p.path);
    raw_request(if p.large { CM_LARGE_FORWARD_OPEN } else { CM_FORWARD_OPEN }, &cm_path(), &d)
}

pub fn forward_close_request(connection_serial: u16, originator_serial: u32, path: &[u8]) -> Vec<u8> {
    let mut d = vec![0x0A, 0x0E];
    d.extend_from_slice(&connection_serial.to_le_bytes());
    d.extend_from_slice(&ORIGINATOR_VENDOR.to_le_bytes());
    d.extend_from_slice(&originator_serial.to_le_bytes());
    d.push((path.len() / 2) as u8);
    d.push(0);
    d.extend_from_slice(path);
    raw_request(CM_FORWARD_CLOSE, &cm_path(), &d)
}

// --- replies ---------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CipReply {
    pub service: u8,
    pub status: u8,
    /// Extended status words.
    pub ext: Vec<u16>,
    pub data: Vec<u8>,
}

impl CipReply {
    pub fn first_ext(&self) -> Option<u16> {
        self.ext.first().copied()
    }
}

pub fn parse_reply(buf: &[u8]) -> Result<CipReply, String> {
    if buf.len() < 4 {
        return Err("short CIP reply".into());
    }
    let ext_words = buf[3] as usize;
    let data_at = 4 + 2 * ext_words;
    if buf.len() < data_at {
        return Err("truncated CIP extended status".into());
    }
    let ext = (0..ext_words).map(|i| u16::from_le_bytes([buf[4 + 2 * i], buf[5 + 2 * i]])).collect();
    Ok(CipReply { service: buf[0] & 0x7F, status: buf[2], ext, data: buf[data_at..].to_vec() })
}

/// The replies inside a Multiple Service Packet reply (`data` of the outer reply).
pub fn parse_multiple_reply(data: &[u8]) -> Result<Vec<CipReply>, String> {
    if data.len() < 2 {
        return Err("short Multiple Service Packet reply".into());
    }
    let n = u16::from_le_bytes([data[0], data[1]]) as usize;
    if data.len() < 2 + 2 * n {
        return Err("truncated Multiple Service Packet offset table".into());
    }
    let offsets: Vec<usize> = (0..n).map(|i| u16::from_le_bytes([data[2 + 2 * i], data[3 + 2 * i]]) as usize).collect();
    let mut out = Vec::with_capacity(n);
    for (i, &start) in offsets.iter().enumerate() {
        let end = offsets.get(i + 1).copied().unwrap_or(data.len());
        if start > end || end > data.len() {
            return Err("bad Multiple Service Packet offset".into());
        }
        out.push(parse_reply(&data[start..end])?);
    }
    Ok(out)
}

/// Text for a CIP general status (the common ones; Python shows pycomm3's table).
pub fn status_text(status: u8) -> &'static str {
    match status {
        0x00 => "Success",
        0x01 => "Connection failure",
        0x04 => "Path segment error",
        0x05 => "Path destination unknown",
        0x06 => "Partial transfer",
        0x08 => "Service not supported",
        0x0A => "Attribute list error",
        0x11 => "Reply data too large",
        0x13 => "Not enough data",
        0x14 => "Attribute not supported",
        0x16 => "Object does not exist",
        0x1E => "Embedded service error",
        0x26 => "Invalid path size",
        0xFF => "General error",
        _ => "unknown",
    }
}

pub fn status_description(status: u8) -> String {
    format!("CIP status {status:#04x} ({})", status_text(status))
}

// --- values ----------------------------------------------------------------------------------

/// What a Read Tag reply carries: the type and the raw bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Typed {
    Atomic { code: u8, bytes: Vec<u8> },
    Structure { handle: u16, bytes: Vec<u8> },
}

/// The type word(s) and value bytes of a Read Tag / Read Tag Fragmented reply `data`.
pub fn parse_typed(data: &[u8]) -> Result<Typed, String> {
    if data.len() < 2 {
        return Err("reply without a data type".into());
    }
    if data[0] == 0xA0 && data[1] == 0x02 {
        if data.len() < 4 {
            return Err("truncated structure type".into());
        }
        return Ok(Typed::Structure { handle: u16::from_le_bytes([data[2], data[3]]), bytes: data[4..].to_vec() });
    }
    Ok(Typed::Atomic { code: data[0], bytes: data[2..].to_vec() })
}

/// Size in bytes of an atomic CIP type, or `None` for one this client does not read.
pub fn atomic_size(code: u8) -> Option<usize> {
    Some(match code {
        0xC1 | 0xC2 | 0xC6 | 0xD1 => 1,
        0xC3 | 0xC7 | 0xD2 => 2,
        0xC4 | 0xC8 | 0xCA | 0xD3 => 4,
        0xC5 | 0xC9 | 0xCB | 0xD4 => 8,
        _ => return None,
    })
}

/// Python-visible name of an atomic type (as pycomm3 reports it).
pub fn atomic_name(code: u8) -> &'static str {
    match code {
        0xC1 => "BOOL",
        0xC2 => "SINT",
        0xC3 => "INT",
        0xC4 => "DINT",
        0xC5 => "LINT",
        0xC6 => "USINT",
        0xC7 => "UINT",
        0xC8 => "UDINT",
        0xC9 => "ULINT",
        0xCA => "REAL",
        0xCB => "LREAL",
        0xD1 => "BYTE",
        0xD2 => "WORD",
        0xD3 => "DWORD",
        0xD4 => "LWORD",
        0xDA => "STRING",
        _ => "UNKNOWN",
    }
}

/// Decode one atomic value (little endian). A CIP SHORT_STRING (0xDA, the Micro800 STRING) is
/// one length byte and the characters.
pub fn decode_atomic(code: u8, bytes: &[u8]) -> Result<Value, String> {
    if code == 0xDA {
        let len = *bytes.first().ok_or("empty SHORT_STRING")? as usize;
        let text = bytes.get(1..1 + len).ok_or("truncated SHORT_STRING")?;
        return Ok(Value::Str(text.iter().map(|&b| b as char).collect()));
    }
    let size = atomic_size(code).ok_or_else(|| format!("unsupported CIP data type {code:#04x}"))?;
    let b = bytes.get(..size).ok_or_else(|| format!("short {} value", atomic_name(code)))?;
    Ok(match code {
        0xC1 => Value::Bool(b[0] != 0),
        0xC2 => Value::Int(b[0] as i8 as i64),
        0xC3 => Value::Int(i16::from_le_bytes(b.try_into().unwrap()) as i64),
        0xC4 => Value::Int(i32::from_le_bytes(b.try_into().unwrap()) as i64),
        0xC5 => Value::Int(i64::from_le_bytes(b.try_into().unwrap())),
        0xC6 | 0xD1 => Value::Int(b[0] as i64),
        0xC7 | 0xD2 => Value::Int(u16::from_le_bytes(b.try_into().unwrap()) as i64),
        0xC8 | 0xD3 => Value::Int(u32::from_le_bytes(b.try_into().unwrap()) as i64),
        0xC9 | 0xD4 => Value::from_u64(u64::from_le_bytes(b.try_into().unwrap())),
        0xCA => Value::Float(f32::from_le_bytes(b.try_into().unwrap()) as f64),
        _ => Value::Float(f64::from_le_bytes(b.try_into().unwrap())),
    })
}

/// Logix STRING structure: DINT length then the characters.
pub fn decode_logix_string(bytes: &[u8]) -> Result<Value, String> {
    let len = i32::from_le_bytes(bytes.get(..4).ok_or("short STRING structure")?.try_into().unwrap());
    let len = usize::try_from(len).map_err(|_| "negative STRING length")?;
    let text = bytes.get(4..4 + len).ok_or("STRING length beyond its data")?;
    Ok(Value::Str(text.iter().map(|&b| b as char).collect()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::params::{Hop, Link};

    #[test]
    fn no_object_service_is_a_write_service() {
        for s in ObjectService::ALL {
            assert!(!WRITE_SERVICES.contains(&s.code()), "{s:?}");
        }
        assert!(multiple_service_request(&[vec![0x4D, 0, 0]]).is_err());
        assert!(multiple_service_request(&[vec![0x53, 0, 0]]).is_err());
        assert!(multiple_service_request(&[read_tag_request(&[0x91, 2, b'A', b'B'], 1).unwrap()]).is_ok());
    }

    #[test]
    fn symbolic_paths_match_the_planner_estimate() {
        for name in ["FT_01", "ARRAY_REAL[4]", "UDT[2].Member[3].X", "Program:MainProgram.Tag", "ARR[1,2,300]", "A.B"] {
            let p = symbolic_path(name).unwrap();
            assert_eq!(p.len() + 1, crate::address::epath_size(name), "{name}");
            assert_eq!(p.len() % 2, 0);
        }
        assert_eq!(symbolic_path("A[300]").unwrap(), vec![0x91, 1, b'A', 0, 0x29, 0, 0x2C, 0x01]);
        assert_eq!(symbolic_path("Program:Main.T").unwrap()[..2], [0x91, 12]);
    }

    #[test]
    fn read_tag_wire_bytes() {
        let path = symbolic_path("AB").unwrap();
        assert_eq!(read_tag_request(&path, 1).unwrap(), vec![0x4C, 2, 0x91, 2, b'A', b'B', 1, 0]);
        assert_eq!(read_fragmented_request(&path, 1, 8).unwrap(), vec![0x52, 2, 0x91, 2, b'A', b'B', 1, 0, 8, 0, 0, 0]);
    }

    #[test]
    fn multiple_service_packet_round_trips() {
        let a = read_tag_request(&symbolic_path("A").unwrap(), 1).unwrap();
        let b = read_tag_request(&symbolic_path("BB").unwrap(), 1).unwrap();
        let msp = multiple_service_request(&[a.clone(), b.clone()]).unwrap();
        assert_eq!(&msp[..6], &[0x0A, 2, 0x20, 0x02, 0x24, 0x01]);
        let data = &msp[6..];
        assert_eq!(u16::from_le_bytes([data[0], data[1]]), 2);
        assert_eq!(u16::from_le_bytes([data[2], data[3]]) as usize, 6);
        assert_eq!(u16::from_le_bytes([data[4], data[5]]) as usize, 6 + a.len());
        // a reply with two members
        let r1 = [0xCC, 0, 0, 0, 0xC4, 0, 7, 0, 0, 0];
        let r2 = [0xCC, 0, 4, 1, 0, 0];
        let mut reply = vec![2, 0, 6, 0, 6 + r1.len() as u8, 0];
        reply.extend_from_slice(&r1);
        reply.extend_from_slice(&r2);
        let parsed = parse_multiple_reply(&reply).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].status, 0);
        assert_eq!(
            decode_atomic(
                0xC4,
                &parse_typed(&parsed[0].data)
                    .map(|t| match t {
                        Typed::Atomic { bytes, .. } => bytes,
                        Typed::Structure { bytes, .. } => bytes,
                    })
                    .unwrap()
            ),
            Ok(Value::Int(7))
        );
        assert_eq!((parsed[1].status, parsed[1].first_ext()), (4, Some(0)));
    }

    #[test]
    fn forward_open_layout() {
        let p = ForwardOpenParams {
            large: true,
            size: 4000,
            connection_serial: 7,
            originator_serial: 9,
            to_connection_id: 0x1122_3344,
            path: connection_path(&[Hop { port: 1, link: Link::Number(2) }]),
        };
        let r = forward_open_request(&p);
        assert_eq!(&r[..6], &[0x5B, 2, 0x20, 0x06, 0x24, 0x01]);
        assert_eq!(r[6], 0x0A);
        // O->T connection parameters: point to point, variable size, 4000 bytes
        let params_at = 6 + 2 + 4 + 4 + 2 + 2 + 4 + 4 + 4;
        assert_eq!(u32::from_le_bytes(r[params_at..params_at + 4].try_into().unwrap()), 0x4200_0000 | 4000);
        let std = forward_open_request(&ForwardOpenParams { large: false, size: 500, ..p.clone() });
        assert_eq!(std[0], 0x54);
        let params_at = 6 + 2 + 4 + 4 + 2 + 2 + 4 + 4 + 4;
        assert_eq!(u16::from_le_bytes(std[params_at..params_at + 2].try_into().unwrap()), 0x4200 | 500);
        // route: port 1 link 2, then the message router
        assert_eq!(p.path, vec![1, 2, 0x20, 0x02, 0x24, 0x01]);
    }

    #[test]
    fn extended_link_hop_is_padded() {
        assert_eq!(hop_segment(2, &Link::Text("192.0.2.7".into())), {
            let mut v = vec![0x12, 9];
            v.extend_from_slice(b"192.0.2.7");
            v.push(0);
            v
        });
    }

    #[test]
    fn atomic_values_decode() {
        assert_eq!(decode_atomic(0xC3, &[0xFF, 0xFF]), Ok(Value::Int(-1)));
        assert_eq!(decode_atomic(0xC7, &[0xFF, 0xFF]), Ok(Value::Int(65535)));
        assert_eq!(decode_atomic(0xCA, &1.5f32.to_le_bytes()), Ok(Value::Float(1.5)));
        assert_eq!(decode_atomic(0xC9, &u64::MAX.to_le_bytes()), Ok(Value::UInt(u64::MAX)));
        assert_eq!(decode_atomic(0xC1, &[0xFF]), Ok(Value::Bool(true)));
        assert_eq!(decode_atomic(0xDA, &[2, b'o', b'k']), Ok(Value::Str("ok".into())));
        assert!(decode_atomic(0xC4, &[1, 2]).is_err());
        assert_eq!(decode_logix_string(&[2, 0, 0, 0, b'h', b'i', 0]), Ok(Value::Str("hi".into())));
    }

    #[test]
    fn list_identity_parses() {
        let mut item = vec![1, 0];
        item.extend_from_slice(&[0; 16]);
        item.extend_from_slice(&[1, 0, 0x0E, 0, 54, 0, 20, 11, 0x60, 0x30, 0xEE, 0xFF, 0xC0, 0x60, 4]);
        item.extend_from_slice(b"1756");
        item.push(3);
        let mut body = vec![1, 0, 0x0C, 0];
        body.extend_from_slice(&(item.len() as u16).to_le_bytes());
        body.extend_from_slice(&item);
        let id = parse_list_identity(&body).unwrap();
        assert_eq!((id.vendor, id.product_code, id.revision().as_str(), id.serial), (1, 54, "20.11", 0x60C0_FFEE));
        assert_eq!(id.product_name, "1756");
        assert!(!id.is_micro800());
    }
}
