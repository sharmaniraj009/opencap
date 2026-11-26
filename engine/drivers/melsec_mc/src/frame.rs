//! MC protocol 3E binary frames: the read requests the driver builds, the reply check, end
//! codes and the read-only guard (Python `check_read_only`, `check_reply_frame`, D-053, D-056).
//!
//! The engine is READ-ONLY. The only requests this module can build are the batch read in word
//! units (0401/0000, 0401/0002 for iQ-R) and read CPU model (0101/0000); `check_read_only`
//! refuses every other command before a frame reaches a socket.
//!
//! Request (little endian unless noted):
//! `50 00` subheader (big endian) | network | PC | module I/O (2) | station | length (2) |
//! monitoring timer (2) | command (2) | subcommand (2) | request data. The length counts
//! everything after itself. Reply: `D0 00` | network | PC | module I/O (2) | station |
//! length (2) | end code (2) | data (or, with an error end code, the 9-byte error information).

use crate::params::McParams;

pub const SUBHEADER_REQUEST: [u8; 2] = [0x50, 0x00];
pub const SUBHEADER_REPLY: [u8; 2] = [0xD0, 0x00];
/// subheader 2, network 1, PC 1, module I/O 2, station 1, data length 2
pub const REPLY_HEADER_LEN: usize = 9;
pub const END_CODE_LEN: usize = 2;

pub const CMD_BATCH_READ: u16 = 0x0401;
pub const CMD_READ_CPU_MODEL: u16 = 0x0101;
/// The only (command, subcommand) pairs the driver sends.
pub const READ_COMMANDS: [(u16, u16); 3] =
    [(CMD_BATCH_READ, 0x0000), (CMD_BATCH_READ, 0x0002), (CMD_READ_CPU_MODEL, 0x0000)];
/// Write and remote-control commands, listed only to prove they are refused (tests): device
/// writes, remote run/stop/pause/reset, clear error, password lock, memory/file writes.
pub const WRITE_COMMANDS: [u16; 13] =
    [0x1401, 0x1402, 0x1403, 0x1406, 0x1001, 0x1002, 0x1003, 0x1005, 0x1006, 0x1630, 0x1631, 0x1617, 0x1618];

/// End-code texts, paraphrased from the MELSEC communication protocol reference (QnA-compatible
/// 3E frame / Q CPU error codes). Not confirmed on hardware.
pub fn end_code_description(code: u16) -> &'static str {
    match code {
        0xC050 => "ASCII data that cannot be converted to binary",
        0xC051..=0xC054 => "number of read/write points out of range",
        0xC055 => "number of points exceeds the file data range",
        0xC056 => "read/write request exceeds the maximum address",
        0xC058 => "request data length does not match the character count",
        0xC059 => "command/subcommand wrong or not supported by the target",
        0xC05B => "the CPU cannot read/write the specified device",
        0xC05C => "error in the request content (e.g. bit device in word units off a 16-point boundary)",
        0xC05F => "request cannot be executed on the target CPU",
        0xC060 => "error in the request content (bit device specification)",
        0xC061 => "request data length does not match the number of data",
        0xC06F => "communication data code (binary/ASCII) does not match the module setting",
        0x4030 => "the specified device cannot be accessed (device does not exist)",
        0x4031 => "the specified device number is out of range",
        _ => "unknown error",
    }
}

/// The PLC permanently rejects this request: the block (or `plc_type`) is wrong, retrying cannot
/// help (-> `BlockConfigError`). Every other end code is a `CommError`.
pub fn is_config_end_code(code: u16) -> bool {
    matches!(code, 0xC051..=0xC054 | 0xC056 | 0xC059 | 0xC05B | 0xC05C | 0xC060 | 0x4030 | 0x4031)
}

/// `MC end code 0xC056 (read/write request exceeds the maximum address)`.
pub fn end_code_text(code: u16) -> String {
    format!("MC end code {code:#06X} ({})", end_code_description(code)).replacen("0X", "0x", 1)
}

/// 3E monitoring timer (units of 250 ms) for a request timeout.
pub fn monitoring_timer(timeout_ms: i64) -> u16 {
    let t = (timeout_ms.max(0) + 249) / 250;
    t.clamp(1, 0xFFFF) as u16
}

fn request(p: &McParams, timer: u16, command: u16, sub: u16, data: &[u8]) -> Vec<u8> {
    let mut f = Vec::with_capacity(15 + data.len());
    f.extend_from_slice(&SUBHEADER_REQUEST);
    f.push(p.network);
    f.push(p.pc);
    f.extend_from_slice(&p.dest_moduleio.to_le_bytes());
    f.push(p.dest_modulesta);
    f.extend_from_slice(&((2 + 4 + data.len()) as u16).to_le_bytes());
    f.extend_from_slice(&timer.to_le_bytes());
    f.extend_from_slice(&command.to_le_bytes());
    f.extend_from_slice(&sub.to_le_bytes());
    f.extend_from_slice(data);
    f
}

/// Batch read in word units of `points` words from device `code`, number `head`.
pub fn batch_read_words(p: &McParams, timer: u16, code: u8, head: u32, points: u16) -> Vec<u8> {
    let mut data = Vec::with_capacity(8);
    if p.iqr() {
        data.extend_from_slice(&head.to_le_bytes());
        data.extend_from_slice(&u16::from(code).to_le_bytes());
    } else {
        data.extend_from_slice(&head.to_le_bytes()[..3]);
        data.push(code);
    }
    data.extend_from_slice(&points.to_le_bytes());
    request(p, timer, CMD_BATCH_READ, if p.iqr() { 0x0002 } else { 0x0000 }, &data)
}

/// Read CPU model name (0101/0000).
pub fn read_cpu_model(p: &McParams, timer: u16) -> Vec<u8> {
    request(p, timer, CMD_READ_CPU_MODEL, 0x0000, &[])
}

/// (command, subcommand) of a binary 3E request frame, or `None` if it is not one.
pub fn request_command(frame: &[u8]) -> Option<(u16, u16)> {
    if frame.len() < 15 || frame[..2] != SUBHEADER_REQUEST {
        return None;
    }
    Some((u16::from_le_bytes([frame[11], frame[12]]), u16::from_le_bytes([frame[13], frame[14]])))
}

/// `None` when `frame` is a request the driver may send, else why it is refused.
pub fn check_read_only(frame: &[u8]) -> Option<String> {
    let Some(cmd) = request_command(frame) else {
        return Some("not a binary 3E request frame (the driver sends binary 3E frames only)".into());
    };
    if !READ_COMMANDS.contains(&cmd) {
        return Some(format!(
            "command {:#06x} subcommand {:#06x} is not a read the driver uses (read-only: only 0401/0000, 0401/0002 and 0101/0000 are sent)",
            cmd.0, cmd.1
        ));
    }
    None
}

/// Validate a binary 3E reply header against the frame length. Returns the announced data length
/// (end code + data, in bytes).
pub fn check_reply_frame(frame: &[u8]) -> Result<usize, String> {
    if frame.len() < REPLY_HEADER_LEN + END_CODE_LEN {
        return Err(format!("reply of {} bytes is shorter than a 3E reply header", frame.len()));
    }
    if frame[..2] != SUBHEADER_REPLY {
        return Err(format!("unexpected reply subheader {} (expected d000)", hex2(&frame[..2])));
    }
    let length = usize::from(u16::from_le_bytes([frame[7], frame[8]]));
    if REPLY_HEADER_LEN + length != frame.len() {
        return Err(format!(
            "reply announces {length} bytes after the header, carries {}",
            frame.len() - REPLY_HEADER_LEN
        ));
    }
    Ok(length)
}

pub fn hex2(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// A checked reply to a read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reply {
    /// End code 0: the data after the end code.
    Data(Vec<u8>),
    /// A non-zero end code.
    EndCode(u16),
}

/// Split a whole reply frame: header check, then the end code. A frame whose subheader or
/// length does not match is a frame error.
pub fn parse_reply(frame: &[u8]) -> Result<Reply, String> {
    check_reply_frame(frame)?;
    let code = u16::from_le_bytes([frame[REPLY_HEADER_LEN], frame[REPLY_HEADER_LEN + 1]]);
    if code != 0 {
        return Ok(Reply::EndCode(code));
    }
    Ok(Reply::Data(frame[REPLY_HEADER_LEN + END_CODE_LEN..].to_vec()))
}

/// The words of a batch-read reply: exactly `count` of them, little endian. A reply carrying
/// another number of bytes (a reply cut short, a PLC that answers something else) is an error
/// rather than zeros (D-053).
pub fn reply_words(data: &[u8], count: usize) -> Result<Vec<u16>, String> {
    if data.len() != 2 * count {
        return Err(format!("reply carries {} data bytes for {count} words", data.len()));
    }
    Ok(data.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p() -> McParams {
        McParams::default()
    }

    #[test]
    fn the_manual_example_d100_five_points() {
        // MELSEC communication protocol reference, 3E binary: batch read D100.., 5 points,
        // monitoring timer 0x0010: 50 00 00 FF FF 03 00 0C 00 10 00 01 04 00 00 64 00 00 A8 05 00
        let frame = batch_read_words(&p(), 0x0010, 0xA8, 100, 5);
        let want = [
            0x50, 0x00, 0x00, 0xFF, 0xFF, 0x03, 0x00, 0x0C, 0x00, 0x10, 0x00, 0x01, 0x04, 0x00, 0x00, 0x64, 0x00, 0x00,
            0xA8, 0x05, 0x00,
        ];
        assert_eq!(frame, want);
        assert_eq!(check_read_only(&frame), None);
    }

    #[test]
    fn iqr_uses_four_byte_numbers_two_byte_codes_and_subcommand_2() {
        let q = McParams { plc_type: "iQ-R".into(), ..p() };
        let f = batch_read_words(&q, 4, 0xA8, 100, 5);
        assert_eq!(&f[11..15], &[0x01, 0x04, 0x02, 0x00]);
        assert_eq!(&f[15..], &[0x64, 0, 0, 0, 0xA8, 0, 0x05, 0]);
        assert_eq!(usize::from(u16::from_le_bytes([f[7], f[8]])), f.len() - 9);
    }

    #[test]
    fn route_params_go_into_the_header() {
        let q = McParams { network: 1, pc: 2, dest_moduleio: 0x3E1, dest_modulesta: 3, ..p() };
        let f = read_cpu_model(&q, 4);
        assert_eq!(&f[..9], &[0x50, 0x00, 1, 2, 0xE1, 0x03, 3, 0x06, 0x00]);
        assert_eq!(request_command(&f), Some((0x0101, 0)));
    }

    #[test]
    fn write_and_control_commands_are_refused() {
        for cmd in WRITE_COMMANDS {
            let mut f = read_cpu_model(&p(), 4);
            f[11..13].copy_from_slice(&cmd.to_le_bytes());
            assert!(check_read_only(&f).is_some(), "{cmd:#06x}");
        }
        let mut f = batch_read_words(&p(), 4, 0xA8, 0, 1);
        f[13] = 0x01; // batch read in bit units is not used either
        assert!(check_read_only(&f).is_some());
        assert!(check_read_only(b"5000").unwrap().contains("binary 3E"));
    }

    #[test]
    fn reply_checks() {
        let ok = [0xD0, 0, 0, 0xFF, 0xFF, 3, 0, 6, 0, 0, 0, 0x34, 0x12, 0x78, 0x56];
        assert_eq!(parse_reply(&ok), Ok(Reply::Data(vec![0x34, 0x12, 0x78, 0x56])));
        assert_eq!(reply_words(&[0x34, 0x12, 0x78, 0x56], 2), Ok(vec![0x1234, 0x5678]));
        assert_eq!(reply_words(&[0x34, 0x12], 2).unwrap_err(), "reply carries 2 data bytes for 2 words");
        let mut bad = ok.to_vec();
        bad[0] = 0xD1;
        assert_eq!(parse_reply(&bad).unwrap_err(), "unexpected reply subheader d100 (expected d000)");
        bad[0] = 0xD0;
        bad[7] = 9;
        assert_eq!(parse_reply(&bad).unwrap_err(), "reply announces 9 bytes after the header, carries 6");
        assert_eq!(parse_reply(&ok[..10]).unwrap_err(), "reply of 10 bytes is shorter than a 3E reply header");
    }

    #[test]
    fn end_codes() {
        assert_eq!(end_code_text(0xC056), "MC end code 0xC056 (read/write request exceeds the maximum address)");
        assert_eq!(end_code_text(0x4010), "MC end code 0x4010 (unknown error)");
        assert!(is_config_end_code(0xC059) && is_config_end_code(0x4031));
        assert!(!is_config_end_code(0xC061) && !is_config_end_code(0x4010));
    }

    #[test]
    fn monitoring_timer_is_in_250_ms_units() {
        assert_eq!(monitoring_timer(1000), 4);
        assert_eq!(monitoring_timer(1), 1);
        assert_eq!(monitoring_timer(300), 2);
        assert_eq!(monitoring_timer(100_000_000), 0xFFFF);
    }
}
