//! FINS frames, pure (docs/protocols/omron-fins.md §1-§2, §4). Only three commands can be
//! framed: MEMORY AREA READ (0101), MULTIPLE MEMORY AREA READ (0104) and CPU UNIT DATA READ
//! (0501); every frame also passes [`check_read_only`] before it reaches a socket (ADR-0008).

pub const CMD_READ: u16 = 0x0101;
pub const CMD_MULTI_READ: u16 = 0x0104;
pub const CMD_CPU_DATA: u16 = 0x0501;
pub const HEADER_LEN: usize = 10;

/// FINS/TCP (W421 7-4)
pub const TCP_MAGIC: &[u8; 4] = b"FINS";
pub const TCP_NODE_REQ: u32 = 0;
pub const TCP_NODE_RESP: u32 = 1;
pub const TCP_FRAME: u32 = 2;
pub const TCP_ERROR_NOTICE: u32 = 3;

/// Destination and source of a frame: (network, node, unit) each.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Route {
    pub gct: u8,
    pub dna: u8,
    pub da1: u8,
    pub da2: u8,
    pub sna: u8,
    pub sa1: u8,
    pub sa2: u8,
}

/// The read commands, the only ones that can be built.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// area code, word address, bit, count
    Read { code: u8, address: u16, bit: u8, count: u16 },
    /// (area code, word address, bit) per item
    MultiRead(Vec<(u8, u16, u8)>),
    /// 0501 with data 00: model, version, system use, area data
    CpuUnitData,
}

impl Command {
    pub fn code(&self) -> u16 {
        match self {
            Command::Read { .. } => CMD_READ,
            Command::MultiRead(_) => CMD_MULTI_READ,
            Command::CpuUnitData => CMD_CPU_DATA,
        }
    }
}

/// A command frame: header (ICF 0x80: command, response required), command code, data.
pub fn frame(route: &Route, sid: u8, cmd: &Command) -> Vec<u8> {
    let mut out = vec![0x80, 0x00, route.gct, route.dna, route.da1, route.da2, route.sna, route.sa1, route.sa2, sid];
    out.extend_from_slice(&cmd.code().to_be_bytes());
    match cmd {
        Command::Read { code, address, bit, count } => {
            out.push(*code);
            out.extend_from_slice(&address.to_be_bytes());
            out.push(*bit);
            out.extend_from_slice(&count.to_be_bytes());
        }
        Command::MultiRead(items) => {
            for (code, address, bit) in items {
                out.push(*code);
                out.extend_from_slice(&address.to_be_bytes());
                out.push(*bit);
            }
        }
        Command::CpuUnitData => out.push(0x00),
    }
    out
}

/// None if this FINS frame is a read command the driver may send; else why it must not be sent.
pub fn check_read_only(frame: &[u8]) -> Option<String> {
    if frame.len() < HEADER_LEN + 2 {
        return Some("not a FINS command frame".into());
    }
    if frame[0] & 0x40 != 0 {
        return Some("a FINS response, not a command".into());
    }
    let cmd = u16::from_be_bytes([frame[10], frame[11]]);
    if [CMD_READ, CMD_MULTI_READ, CMD_CPU_DATA].contains(&cmd) {
        None
    } else {
        Some(format!("FINS command {cmd:04X} refused: opencap only reads (0101, 0104, 0501)"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reply {
    /// masked end code, the CPU error bits (SRES bits 7 and 6), response data
    Ok { end: u16, cpu_error: u8, data: Vec<u8> },
    /// a frame that is not the reply to this request (another SID or command, or a command)
    Other,
}

/// Match and split a response frame for the request with `sid` and `command`.
pub fn parse_reply(frame: &[u8], sid: u8, command: u16) -> Result<Reply, String> {
    if frame.len() < HEADER_LEN + 4 {
        return Err(format!("reply of {} bytes is shorter than a FINS response header", frame.len()));
    }
    if frame[0] & 0x40 == 0 || frame[9] != sid || u16::from_be_bytes([frame[10], frame[11]]) != command {
        return Ok(Reply::Other);
    }
    let (mres, sres) = (frame[12], frame[13]);
    let end = u16::from_be_bytes([mres, sres]) & 0x7F3F;
    // MRES bit 7: a network relay error word (network, node) follows the end code
    let skip = if mres & 0x80 != 0 { 2 } else { 0 };
    let data = frame.get(14 + skip..).unwrap_or_default().to_vec();
    Ok(Reply::Ok { end, cpu_error: sres & 0xC0, data })
}

/// The driver error class of a masked end code (protocol notes §4): true = the block can never
/// be read (BlockConfigError), false = CommError.
pub fn end_code_is_config(end: u16) -> bool {
    matches!(end, 0x0401 | 0x0402 | 0x1001..=0x1004 | 0x1101..=0x1104 | 0x1109 | 0x110B | 0x110C | 0x2001..=0x21FF | 0x2301..=0x2303)
}

pub fn end_code_text(end: u16) -> String {
    let what = match end {
        0x0001 => "service cancelled",
        0x0101..=0x0106 => "local node error",
        0x0201..=0x0205 => "destination node error",
        0x0301..=0x0304 => "controller error",
        0x0401 => "undefined command",
        0x0402 => "not supported by the unit model or version",
        0x0501..=0x0504 => "routing table error",
        0x1001 => "command too long",
        0x1002 => "command too short",
        0x1003 => "elements and data do not agree",
        0x1004 => "command format error",
        0x1005 => "header error",
        0x1101 => "area classification missing (no such area, or no EM)",
        0x1102 => "access size wrong",
        0x1103 => "address range designation error",
        0x1104 => "address range exceeded",
        0x1109 => "relational error",
        0x110B => "response too long",
        0x110C => "parameter error",
        0x2001..=0x21FF => "read not possible",
        0x2201..=0x22FF => "wrong CPU mode",
        0x2301 => "the memory does not exist",
        0x2302 => "no file memory",
        0x2303 => "no clock",
        0x2401..=0x25FF => "unit error",
        0x2601..=0x26FF => "command error",
        0x3001 => "access right held by another device",
        0x4001 => "aborted",
        _ => "error",
    };
    format!("FINS end code {end:04X} ({what})")
}

/// A FINS/TCP message: magic, length (8 + payload), command, error code, payload.
pub fn tcp_message(command: u32, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(16 + payload.len());
    out.extend_from_slice(TCP_MAGIC);
    out.extend_from_slice(&(8 + payload.len() as u32).to_be_bytes());
    out.extend_from_slice(&command.to_be_bytes());
    out.extend_from_slice(&0u32.to_be_bytes());
    out.extend_from_slice(payload);
    out
}

pub fn tcp_error_text(code: u32) -> &'static str {
    match code {
        0x01 => "the header is not FINS",
        0x02 => "the data length is too long",
        0x03 => "the command is not supported",
        0x20 => "all connections are in use",
        0x21 => "the requested client node is already connected",
        0x22 => "a protected node was accessed from an unregistered IP address",
        0x23 => "the client node is out of range",
        0x24 => "the client node equals the server's node",
        0x25 => "no node is left to allocate",
        _ => "unknown error",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ROUTE: Route = Route { gct: 2, dna: 0, da1: 10, da2: 0, sna: 0, sa1: 1, sa2: 0 };

    #[test]
    fn memory_area_read_frame_from_the_manual_layout() {
        // W342 5-3-2: header, 01 01, area 82 (DM), address 0064 00 (D100), 0002 items
        let f = frame(&ROUTE, 0x2A, &Command::Read { code: 0x82, address: 100, bit: 0, count: 2 });
        assert_eq!(f, [0x80, 0, 2, 0, 10, 0, 0, 1, 0, 0x2A, 0x01, 0x01, 0x82, 0x00, 0x64, 0x00, 0x00, 0x02]);
        let m = frame(&ROUTE, 1, &Command::MultiRead(vec![(0x82, 1, 0), (0x30, 5, 3)]));
        assert_eq!(&m[10..], [0x01, 0x04, 0x82, 0x00, 0x01, 0x00, 0x30, 0x00, 0x05, 0x03]);
        assert_eq!(&frame(&ROUTE, 1, &Command::CpuUnitData)[10..], [0x05, 0x01, 0x00]);
    }

    #[test]
    fn only_the_three_read_commands_pass_the_guard() {
        for cmd in [
            Command::Read { code: 0x82, address: 0, bit: 0, count: 1 },
            Command::MultiRead(vec![(0x82, 0, 0)]),
            Command::CpuUnitData,
        ] {
            assert_eq!(check_read_only(&frame(&ROUTE, 1, &cmd)), None);
        }
        // every write/control command of W342 5-1-1 (and the CV-mode ones) is refused
        let writes = [
            0x0102u16, 0x0103, 0x0105, 0x0202, 0x0203, 0x0307, 0x0308, 0x0401, 0x0402, 0x0702, 0x0C01, 0x0C02, 0x2101,
            0x2103, 0x2141, 0x2203, 0x2204, 0x2205, 0x2207, 0x2208, 0x220A, 0x220B, 0x220C, 0x2215, 0x2220, 0x2301,
            0x2302, 0x2803, 0x2804, 0x2805, 0x0304, 0x0305, 0x0403,
        ];
        for w in writes {
            let mut f = frame(&ROUTE, 1, &Command::Read { code: 0x82, address: 0, bit: 0, count: 1 });
            f[10..12].copy_from_slice(&w.to_be_bytes());
            assert!(check_read_only(&f).unwrap().contains(&format!("{w:04X}")), "{w:04X}");
        }
        let mut response = frame(&ROUTE, 1, &Command::CpuUnitData);
        response[0] = 0xC0;
        assert!(check_read_only(&response).is_some());
    }

    #[test]
    fn replies_are_matched_by_sid_and_command_and_end_codes_masked() {
        let reply = [0xC0, 0, 2, 0, 1, 0, 0, 10, 0, 7, 0x01, 0x01, 0x00, 0x40, 0x12, 0x34];
        assert_eq!(parse_reply(&reply, 7, CMD_READ), Ok(Reply::Ok { end: 0, cpu_error: 0x40, data: vec![0x12, 0x34] }));
        assert_eq!(parse_reply(&reply, 8, CMD_READ), Ok(Reply::Other), "another SID");
        assert_eq!(parse_reply(&reply, 7, CMD_MULTI_READ), Ok(Reply::Other), "another command");
        let relay = [0xC0, 0, 2, 0, 1, 0, 0, 10, 0, 7, 0x01, 0x01, 0x82, 0x05, 0x01, 0x0A];
        assert_eq!(parse_reply(&relay, 7, CMD_READ), Ok(Reply::Ok { end: 0x0205, cpu_error: 0, data: vec![] }));
        assert!(parse_reply(&reply[..12], 7, CMD_READ).is_err());
        assert!(end_code_is_config(0x1101) && end_code_is_config(0x0401) && end_code_is_config(0x110B));
        assert!(!end_code_is_config(0x0205) && !end_code_is_config(0x2201) && !end_code_is_config(0x1005));
    }
}
