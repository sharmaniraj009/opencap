//! S7comm over ISO-on-TCP: the frames the driver builds and parses (TPKT, COTP, S7 PDUs).
//!
//! The engine is READ-ONLY. The only S7 requests this module can build are setup communication
//! (job 0xF0), read var (job 0x04) and the SZL read (USERDATA group 4 subfunction 1, with its
//! follow-up). `check_read_only` refuses every other PDU before it reaches a socket.
//!
//! Layouts (big endian), from the S7comm specification as dissected by Wireshark:
//! - TPKT: `03 00 <length incl. header (2)>`; COTP DT: `02 F0 80`; COTP CR: `<len> E0 0000 <src> 00`
//!   then parameters `C1 02 <local TSAP>`, `C2 02 <remote TSAP>`, `C0 01 0A`.
//! - S7 job header (10 bytes): `32 01 0000 <pdu ref (2)> <param len (2)> <data len (2)>`.
//! - Read var parameters: `04 <items>` then per item `12 0A 10 <transport size> <count (2)>
//!   <DB (2)> <area> <address (3)>` (byte address x 8; timers/counters: the element number).
//! - ACK_DATA header (12 bytes): as a job header with type 03 and `<error class> <error code>`.

use crate::errors::protocol_error_text;

pub const TPKT_VERSION: u8 = 3;
pub const COTP_CR: u8 = 0xE0;
pub const COTP_CC: u8 = 0xD0;
pub const COTP_DR: u8 = 0x80;
pub const COTP_DT: u8 = 0xF0;
pub const LOCAL_TSAP: u16 = 0x0100;

pub const PDU_JOB: u8 = 0x01;
pub const PDU_ACK_DATA: u8 = 0x03;
pub const PDU_USERDATA: u8 = 0x07;
pub const FN_SETUP_COMMUNICATION: u8 = 0xF0;
pub const FN_READ_VAR: u8 = 0x04;
pub const USERDATA_GROUP_SZL: u8 = 0x04;
pub const USERDATA_READ_SZL: u8 = 0x01;

/// Transport sizes of the items of a read: bytes, counters, timers.
pub const WL_BYTE: u8 = 0x02;
pub const WL_COUNTER: u8 = 0x1C;
pub const WL_TIMER: u8 = 0x1D;

/// Job functions that write, load, control or stop: listed only to prove they are refused.
pub const REFUSED_JOB_FUNCTIONS: [u8; 10] = [0x05, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x28, 0x29, 0x2A];

pub fn tpkt(payload: &[u8]) -> Vec<u8> {
    let mut f = vec![TPKT_VERSION, 0];
    f.extend_from_slice(&((payload.len() + 4) as u16).to_be_bytes());
    f.extend_from_slice(payload);
    f
}

/// Remote TSAP: connection type (1 PG, 2 OP, 3 PC), rack and slot.
pub fn remote_tsap(connection_type: u8, rack: u8, slot: u8) -> u16 {
    (u16::from(connection_type) << 8) | (u16::from(rack) << 5) | u16::from(slot)
}

/// COTP connection request in a TPKT frame.
pub fn connection_request(local_tsap: u16, remote_tsap: u16) -> Vec<u8> {
    let mut params = Vec::new();
    params.extend_from_slice(&[0xC1, 2]);
    params.extend_from_slice(&local_tsap.to_be_bytes());
    params.extend_from_slice(&[0xC2, 2]);
    params.extend_from_slice(&remote_tsap.to_be_bytes());
    params.extend_from_slice(&[0xC0, 1, 0x0A]); // TPDU size 1024
    let mut pdu = vec![(6 + params.len()) as u8, COTP_CR, 0, 0, 0, 1, 0];
    pdu.extend_from_slice(&params);
    tpkt(&pdu)
}

/// COTP connection confirm: returns the peer's source reference.
pub fn parse_connection_confirm(payload: &[u8]) -> Result<u16, String> {
    if payload.len() < 7 {
        return Err("Invalid COTP CC: too short".into());
    }
    if payload[1] != COTP_CC {
        return Err(format!("Expected COTP CC, got {:#04x}", payload[1]));
    }
    Ok(u16::from_be_bytes([payload[4], payload[5]]))
}

/// COTP disconnect request in a TPKT frame (best effort on close).
pub fn disconnect_request(dst_ref: u16) -> Vec<u8> {
    let mut pdu = vec![6, COTP_DR, 0, 0, 0, 1, 0, 0];
    pdu[2..4].copy_from_slice(&dst_ref.to_be_bytes());
    tpkt(&pdu)
}

/// An S7 PDU in a COTP DT in a TPKT frame.
pub fn data_frame(s7: &[u8]) -> Vec<u8> {
    let mut p = vec![2, COTP_DT, 0x80];
    p.extend_from_slice(s7);
    tpkt(&p)
}

/// The S7 PDU inside a COTP DT payload (the bytes after the TPKT header).
pub fn parse_data_payload(payload: &[u8]) -> Result<&[u8], String> {
    if payload.len() < 3 {
        return Err("Invalid COTP DT: too short".into());
    }
    if payload[0] != 2 {
        return Err("Invalid COTP DT header length".into());
    }
    if payload[2] & 0x7F != 0 {
        return Err("Invalid Class 0 COTP TPDU number".into());
    }
    if payload[1] != COTP_DT {
        return Err(format!("Expected COTP DT, got {:#04x}", payload[1]));
    }
    Ok(&payload[3..])
}

fn job_header(seq: u16, param_len: usize, data_len: usize) -> Vec<u8> {
    let mut h = vec![0x32, PDU_JOB, 0, 0];
    h.extend_from_slice(&seq.to_be_bytes());
    h.extend_from_slice(&(param_len as u16).to_be_bytes());
    h.extend_from_slice(&(data_len as u16).to_be_bytes());
    h
}

/// Setup communication: ask for a PDU of `pdu` bytes, one job in flight each way.
pub fn setup_communication(seq: u16, pdu: u16) -> Vec<u8> {
    let mut f = job_header(seq, 8, 0);
    f.extend_from_slice(&[FN_SETUP_COMMUNICATION, 0, 0, 1, 0, 1]);
    f.extend_from_slice(&pdu.to_be_bytes());
    f
}

/// One item of a read var request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReadItem {
    pub area: u8,
    pub db: u16,
    /// Byte offset, or the timer/counter number.
    pub start: u32,
    pub word_len: u8,
    pub count: u16,
}

impl ReadItem {
    fn encode(&self, out: &mut Vec<u8>) {
        let address =
            if self.word_len == WL_TIMER || self.word_len == WL_COUNTER { self.start } else { self.start * 8 };
        out.extend_from_slice(&[0x12, 0x0A, 0x10, self.word_len]);
        out.extend_from_slice(&self.count.to_be_bytes());
        out.extend_from_slice(&(if self.area == 0x84 { self.db } else { 0 }).to_be_bytes());
        out.push(self.area);
        out.extend_from_slice(&address.to_be_bytes()[1..]);
    }

    /// Reply data bytes this item asks for.
    pub fn nbytes(&self) -> usize {
        usize::from(self.count) * if self.word_len == WL_BYTE { 1 } else { 2 }
    }
}

/// Read var with one or more items (multi-item reads pack scattered ranges into one request).
pub fn read_var(seq: u16, items: &[ReadItem]) -> Vec<u8> {
    let mut f = job_header(seq, 2 + 12 * items.len(), 0);
    f.extend_from_slice(&[FN_READ_VAR, items.len() as u8]);
    for it in items {
        it.encode(&mut f);
    }
    f
}

/// SZL read (USERDATA, group 4, subfunction 1) for `id` and `index`.
pub fn read_szl(seq: u16, id: u16, index: u16) -> Vec<u8> {
    let params = [0x00, 0x01, 0x12, 0x04, 0x11, 0x44, USERDATA_READ_SZL, 0x00];
    let mut data = vec![0xFF, 0x09, 0x00, 0x04];
    data.extend_from_slice(&id.to_be_bytes());
    data.extend_from_slice(&index.to_be_bytes());
    userdata(seq, &params, &data)
}

/// Follow-up request for the next fragment of a multi-packet SZL reply.
pub fn read_szl_followup(seq: u16, sequence_number: u8) -> Vec<u8> {
    let params =
        [0x00, 0x01, 0x12, 0x08, 0x12, 0x40 | USERDATA_GROUP_SZL, USERDATA_READ_SZL, sequence_number, 0, 0, 0, 0];
    userdata(seq, &params, &[0x0A, 0x00, 0x00, 0x00])
}

fn userdata(seq: u16, params: &[u8], data: &[u8]) -> Vec<u8> {
    let mut f = vec![0x32, PDU_USERDATA, 0, 0];
    f.extend_from_slice(&seq.to_be_bytes());
    f.extend_from_slice(&(params.len() as u16).to_be_bytes());
    f.extend_from_slice(&(data.len() as u16).to_be_bytes());
    f.extend_from_slice(params);
    f.extend_from_slice(data);
    f
}

/// `None` if this S7 request PDU only reads; else why it must not be sent.
pub fn check_read_only(pdu: &[u8]) -> Option<String> {
    if pdu.len() < 11 || pdu[0] != 0x32 {
        return Some("not an S7 request PDU".into());
    }
    match pdu[1] {
        PDU_JOB => {
            let f = pdu[10];
            if f == FN_SETUP_COMMUNICATION || f == FN_READ_VAR {
                return None;
            }
            Some(format!("S7 job 0x{f:02X} ({}) refused: opencap is read-only", job_function_name(f)))
        }
        PDU_USERDATA => {
            if pdu.len() < 17 {
                return Some("short S7 USERDATA PDU".into());
            }
            let (group, sub) = (pdu[15] & 0x0F, pdu[16]);
            if group == USERDATA_GROUP_SZL && sub == USERDATA_READ_SZL {
                return None;
            }
            Some(format!("S7 USERDATA group {group} subfunction 0x{sub:02X} refused: only SZL reads are sent"))
        }
        t => Some(format!("S7 PDU type 0x{t:02X} refused: opencap is read-only")),
    }
}

fn job_function_name(f: u8) -> &'static str {
    match f {
        0x04 => "read var",
        0x05 => "write var",
        0x1A => "request download",
        0x1B => "download block",
        0x1C => "download ended",
        0x1D => "start upload",
        0x1E => "upload",
        0x1F => "end upload",
        0x28 => "PLC control",
        0x29 => "PLC stop",
        0xF0 => "setup communication",
        _ => "unknown",
    }
}

// --- replies ---------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedPdu {
    pub pdu_type: u8,
    pub sequence: u16,
    pub parameters: Vec<u8>,
    pub data: Vec<u8>,
}

/// Why a reply could not be used.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReplyError {
    /// The header carries an error (class << 8 | code).
    Header { code: u16, text: String },
    /// Malformed or unexpected.
    Malformed(String),
}

impl ReplyError {
    pub fn message(&self) -> &str {
        match self {
            ReplyError::Header { text, .. } | ReplyError::Malformed(text) => text,
        }
    }
}

fn bad(s: impl Into<String>) -> ReplyError {
    ReplyError::Malformed(s.into())
}

/// Split an S7 response PDU (ACK_DATA or USERDATA) into header fields, parameters and data.
pub fn parse_pdu(pdu: &[u8]) -> Result<ParsedPdu, ReplyError> {
    if pdu.len() < 10 {
        return Err(bad("PDU too short for S7 response header"));
    }
    let pdu_type = pdu[1];
    let (header_len, class, code) = if pdu_type == PDU_USERDATA {
        (10, 0u8, 0u8)
    } else {
        if pdu.len() < 12 {
            return Err(bad("PDU too short for ACK/ACK_DATA header"));
        }
        (12, pdu[10], pdu[11])
    };
    if pdu[0] != 0x32 {
        return Err(bad(format!("Invalid protocol ID: {:#04x}", pdu[0])));
    }
    if !matches!(pdu_type, 0x02 | PDU_ACK_DATA | PDU_USERDATA) {
        return Err(bad(format!("Expected response PDU, got {pdu_type}")));
    }
    let combined = (u16::from(class) << 8) | u16::from(code);
    if combined != 0 {
        let msg = protocol_error_text(combined)
            .map_or_else(|| format!("Unknown protocol error: {combined:#06x}"), String::from);
        return Err(ReplyError::Header {
            code: combined,
            text: format!("S7 protocol error (class={class:#04x}, code={code:#04x}): {msg}"),
        });
    }
    let sequence = u16::from_be_bytes([pdu[4], pdu[5]]);
    let plen = usize::from(u16::from_be_bytes([pdu[6], pdu[7]]));
    let dlen = usize::from(u16::from_be_bytes([pdu[8], pdu[9]]));
    let mut offset = header_len;
    if offset + plen > pdu.len() {
        return Err(bad("Parameter section extends beyond PDU"));
    }
    let parameters = pdu[offset..offset + plen].to_vec();
    offset += plen;
    if offset + dlen > pdu.len() {
        return Err(bad("Data section extends beyond PDU"));
    }
    let data = pdu[offset..offset + dlen].to_vec();
    offset += dlen;
    if offset != pdu.len() {
        return Err(bad("S7 response contains trailing bytes"));
    }
    Ok(ParsedPdu { pdu_type, sequence, parameters, data })
}

/// The negotiated PDU length of a setup communication reply.
pub fn parse_setup_reply(p: &ParsedPdu) -> Result<usize, ReplyError> {
    if p.parameters.len() < 8 || p.parameters[0] != FN_SETUP_COMMUNICATION {
        return Err(bad("Setup communication response parameters too short"));
    }
    let negotiated = usize::from(u16::from_be_bytes([p.parameters[6], p.parameters[7]]));
    Ok(if negotiated < 64 { 240 } else { negotiated })
}

/// One item of a read var reply: its return code (0xFF = success) and data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ItemReply {
    pub return_code: u8,
    pub data: Vec<u8>,
}

/// Parse the items of a read var reply for `requested` items. A successful item's declared
/// length must match what was asked (bit length for byte/word/dword transport sizes, byte
/// length for the octet-string sizes); items are padded to an even length except the last.
pub fn parse_read_reply(p: &ParsedPdu, requested: &[ReadItem]) -> Result<Vec<ItemReply>, ReplyError> {
    if requested.len() == 1 {
        return parse_single(p, requested[0]).map(|r| vec![r]);
    }
    if p.parameters.len() < 2 || p.parameters[0] != FN_READ_VAR {
        return Err(bad("Unexpected S7 response function"));
    }
    if usize::from(p.parameters[1]) != requested.len() {
        return Err(bad("Unexpected S7 response item count"));
    }
    if p.data.is_empty() {
        return Err(bad("No data in response"));
    }
    let mut out = Vec::with_capacity(requested.len());
    let mut at = 0usize;
    for (i, item) in requested.iter().enumerate() {
        if at + 4 > p.data.len() {
            return Err(bad("Data item is truncated"));
        }
        let (rc, tsize) = (p.data[at], p.data[at + 1]);
        let declared = usize::from(u16::from_be_bytes([p.data[at + 2], p.data[at + 3]]));
        if rc != 0xFF {
            // a refused item carries no data (length 0)
            out.push(ItemReply { return_code: rc, data: Vec::new() });
            at += 4;
            continue;
        }
        if !matches!(tsize, 0x03 | 0x04 | 0x05 | 0x06 | 0x07 | 0x09) {
            return Err(bad("Invalid read response transport size"));
        }
        let bytes = if matches!(tsize, 0x04 | 0x05) { declared / 8 } else { declared };
        at += 4;
        if at + bytes > p.data.len() {
            return Err(bad("Data item is truncated"));
        }
        let expected = item.nbytes();
        if bytes != expected {
            return Err(bad(format!("Read response size mismatch: expected {expected} bytes, received {bytes}")));
        }
        let expected_length = if matches!(tsize, 0x04 | 0x05) { expected * 8 } else { expected };
        if declared != expected_length {
            return Err(bad("Read response declared length does not match requested size"));
        }
        out.push(ItemReply { return_code: rc, data: p.data[at..at + bytes].to_vec() });
        at += bytes;
        if i + 1 < requested.len() && bytes % 2 == 1 {
            at += 1; // fill byte
        }
    }
    if at != p.data.len() {
        return Err(bad("Data item contains trailing bytes"));
    }
    Ok(out)
}

/// A read var reply to one item, checked in the order python-snap7 checks it (so the texts of
/// the conformance vectors match): the data section's own length, the item return code, then
/// function, item count, size, transport size and declared length.
fn parse_single(p: &ParsedPdu, item: ReadItem) -> Result<ItemReply, ReplyError> {
    let func = p.parameters.first().copied();
    let count = p.parameters.get(1).copied();
    let d = &p.data;
    if d.is_empty() {
        return Err(bad("No data in response"));
    }
    let (rc, tsize, declared, payload): (u8, u8, usize, &[u8]) = if func == Some(0x05) || d.len() == 1 {
        (d[0], 0, 0, &[])
    } else if d.len() >= 4 {
        let declared = usize::from(u16::from_be_bytes([d[2], d[3]]));
        let bytes = if matches!(d[1], 0x00 | 0x03 | 0x06 | 0x07 | 0x09) { declared } else { declared / 8 };
        if func == Some(FN_READ_VAR) && count == Some(1) {
            if 4 + bytes > d.len() {
                return Err(bad("Data item is truncated"));
            }
            if 4 + bytes < d.len() {
                return Err(bad("Data item contains trailing bytes"));
            }
        }
        (d[0], d[1], declared, &d[4..(4 + bytes).min(d.len())])
    } else {
        return Err(bad("Data item is truncated"));
    };
    if rc != 0xFF {
        return Ok(ItemReply { return_code: rc, data: Vec::new() });
    }
    if func != Some(FN_READ_VAR) {
        return Err(bad("Unexpected S7 response function"));
    }
    if count != Some(1) {
        return Err(bad("Unexpected S7 response item count"));
    }
    let expected = item.nbytes();
    if payload.len() != expected {
        return Err(bad(format!("Read response size mismatch: expected {expected} bytes, received {}", payload.len())));
    }
    if !matches!(tsize, 0x03 | 0x04 | 0x05 | 0x06 | 0x07 | 0x09) {
        return Err(bad("Invalid read response transport size"));
    }
    let expected_length = if matches!(tsize, 0x04 | 0x05) { expected * 8 } else { expected };
    if declared != expected_length {
        return Err(bad("Read response declared length does not match requested size"));
    }
    Ok(ItemReply { return_code: rc, data: payload.to_vec() })
}

/// One fragment of an SZL reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SzlFragment {
    pub more: bool,
    pub sequence_number: u8,
    /// The record data (the first fragment's starts after the SZL id and index).
    pub data: Vec<u8>,
}

/// Parse one USERDATA reply of an SZL read. `first` strips the 4-byte SZL id and index.
pub fn parse_szl_fragment(p: &ParsedPdu, first: bool) -> Result<SzlFragment, ReplyError> {
    if p.pdu_type != PDU_USERDATA {
        return Err(bad("Missing USERDATA response parameters"));
    }
    let pr = &p.parameters;
    if pr.len() != 12 {
        return Err(bad("USERDATA response parameters must contain exactly 12 bytes"));
    }
    let error = u16::from_be_bytes([pr[10], pr[11]]);
    if error != 0 {
        let msg =
            protocol_error_text(error).map_or_else(|| format!("Unknown protocol error: {error:#06x}"), String::from);
        return Err(bad(format!("USERDATA request failed: {msg} ({error:#06x})")));
    }
    if pr[9] > 1 {
        return Err(bad(format!("Invalid USERDATA last data unit value 0x{:02x}", pr[9])));
    }
    if pr[5] & 0x0F != USERDATA_GROUP_SZL {
        return Err(bad("Unexpected USERDATA response function group"));
    }
    if pr[6] != USERDATA_READ_SZL {
        return Err(bad("Unexpected USERDATA response subfunction"));
    }
    if p.data.len() < 4 {
        return Err(bad("Missing USERDATA response data"));
    }
    let rc = p.data[0];
    if rc != 0xFF {
        return Err(bad(format!("Read SZL failed: item return code 0x{rc:02x}")));
    }
    let len = usize::from(u16::from_be_bytes([p.data[2], p.data[3]]));
    if 4 + len != p.data.len() {
        return Err(bad("Data item is truncated or contains trailing bytes"));
    }
    let raw = &p.data[4..];
    let data = if first {
        if raw.len() < 4 {
            Vec::new()
        } else {
            raw[4..].to_vec()
        }
    } else {
        raw.to_vec()
    };
    Ok(SzlFragment { more: pr[9] == 1, sequence_number: pr[7], data })
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Spec-derived: read var of 2 bytes at DB1.DBB0 (S7comm layout: TPKT 03 00 001F, COTP 02 F0 80,
    /// S7 job header, function 04 with one S7-ANY item), and its reply with the two bytes 12 34.
    #[test]
    fn read_var_request_and_reply_from_the_spec_layout() {
        let item = ReadItem { area: 0x84, db: 1, start: 0, word_len: WL_BYTE, count: 2 };
        let request = data_frame(&read_var(1, &[item]));
        let want = "0300001f02f080320100000001000e00000401120a10020002000184000000";
        assert_eq!(hex(&request), want);

        // ACK_DATA: header, params `04 01`, data item rc FF, transport size 04 (byte), 0x0010 bits
        let reply = hex2(&["320300000001000200060000", "0401", "ff040010", "1234"]);
        let parsed = parse_pdu(&reply).unwrap();
        assert_eq!(parsed.sequence, 1);
        let items = parse_read_reply(&parsed, &[item]).unwrap();
        assert_eq!(items, vec![ItemReply { return_code: 0xFF, data: vec![0x12, 0x34] }]);
    }

    fn hex2(parts: &[&str]) -> Vec<u8> {
        let s: String = parts.concat();
        (0..s.len() / 2).map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap()).collect()
    }

    #[test]
    fn connection_request_carries_the_tsaps() {
        let f = connection_request(LOCAL_TSAP, remote_tsap(3, 0, 1));
        assert_eq!(hex(&f), "0300001611e00000000100c1020100c2020301c0010a");
        assert_eq!(remote_tsap(1, 0, 2), 0x0102);
        assert_eq!(remote_tsap(3, 1, 2), 0x0322);
    }

    #[test]
    fn multi_item_reads_are_packed_and_split() {
        let a = ReadItem { area: 0x83, db: 0, start: 10, word_len: WL_BYTE, count: 3 };
        let b = ReadItem { area: 0x1D, db: 0, start: 4, word_len: WL_TIMER, count: 1 };
        let req = read_var(7, &[a, b]);
        assert_eq!(u16::from_be_bytes([req[6], req[7]]), 26);
        assert_eq!(&req[10..12], &[0x04, 2]);
        // timer element number is not multiplied by 8; byte address is
        assert_eq!(&req[21..24], &[0, 0, 80]);
        assert_eq!(&req[33..36], &[0, 0, 4]);
        // item 1: 3 bytes (24 bits) + 1 fill byte; item 2: 2 bytes (octet string); data length 14
        let reply = hex2(&["3203000000070002000e0000", "0402", "ff040018", "010203", "00", "ff090002", "0a0b"]);
        let items = parse_read_reply(&parse_pdu(&reply).unwrap(), &[a, b]).unwrap();
        assert_eq!(items[0].data, vec![1, 2, 3]);
        assert_eq!(items[1].data, vec![0x0A, 0x0B]);
    }

    #[test]
    fn replies_are_checked_not_trusted() {
        let item = ReadItem { area: 0x84, db: 1, start: 0, word_len: WL_BYTE, count: 2 };
        let short = hex2(&["320300000001000200050000", "0401", "ff040008", "12"]);
        let e = parse_read_reply(&parse_pdu(&short).unwrap(), &[item]).unwrap_err();
        assert_eq!(e.message(), "Read response size mismatch: expected 2 bytes, received 1");
        let refused = hex2(&["320300000001000200040000", "0401", "0a000000"]);
        let r = parse_read_reply(&parse_pdu(&refused).unwrap(), &[item]).unwrap();
        assert_eq!(r[0].return_code, 0x0A);
        let header_error = hex2(&["320300000001000000008104"]);
        assert_eq!(
            parse_pdu(&header_error).unwrap_err(),
            ReplyError::Header {
                code: 0x8104,
                text: "S7 protocol error (class=0x81, code=0x04): This service is not implemented on the module or a frame error was reported".into()
            }
        );
        assert_eq!(parse_pdu(&hex2(&["32030000"])).unwrap_err().message(), "PDU too short for S7 response header");
    }

    #[test]
    fn setup_reply_gives_the_negotiated_pdu() {
        let s = setup_communication(1, 960);
        assert_eq!(hex(&s), "32010000000100080000f0000001000103c0");
        let reply = hex2(&["320300000001000800000000", "f0000001000101e0"]);
        assert_eq!(parse_setup_reply(&parse_pdu(&reply).unwrap()), Ok(480));
    }

    #[test]
    fn only_reads_pass_the_guard() {
        let item = ReadItem { area: 0x83, db: 0, start: 0, word_len: WL_BYTE, count: 1 };
        for pdu in [setup_communication(1, 480), read_var(2, &[item]), read_szl(3, 0x1C, 0), read_szl_followup(4, 1)] {
            assert_eq!(check_read_only(&pdu), None);
        }
        for f in REFUSED_JOB_FUNCTIONS {
            let mut pdu = read_var(2, &[item]);
            pdu[10] = f;
            assert!(check_read_only(&pdu).is_some(), "{f:#04x}");
        }
        let mut other = read_szl(3, 0x1C, 0);
        other[16] = 0x02; // another subfunction of the SZL group
        assert!(check_read_only(&other).is_some());
        other[15] = 0x43; // cyclic data group
        assert!(check_read_only(&other).is_some());
        assert!(check_read_only(&[0x32, 0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0]).unwrap().contains("type 0x02"));
    }
}
