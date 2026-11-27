//! PCCC STS / EXT STS texts and their classification (DF1 manual 1770-6.5.16; Python `ab_pccc`).

pub const EXT_STS_FOLLOWS: u8 = 0xF0;

/// The only PCCC commands the engine may send, as (CMD, FNC): protected typed logical read
/// with 3 and 2 address fields, and diagnostic status. Everything else is refused (ADR-0008).
pub const READ_COMMANDS: [(u8, u8); 3] = [(0x0F, 0xA2), (0x0F, 0xA1), (0x06, 0x03)];

fn sts_text(high_nibble: u8) -> Option<&'static str> {
    Some(match high_nibble {
        0x10 => "illegal command or format (address may not exist or not enough elements in the file)",
        0x20 => "host has a problem and will not communicate",
        0x30 => "remote node host is missing, disconnected or shut down",
        0x40 => "host could not complete the function due to a hardware fault",
        0x50 => "addressing problem or memory protect rungs",
        0x60 => "function not allowed due to command protection selection",
        0x70 => "processor is in Program mode",
        0x80 => "compatibility mode file missing or communication zone problem",
        0x90 => "remote node cannot buffer the command",
        0xB0 => "remote node problem due to download",
        0xF0 => "error code in the EXT STS byte",
        _ => return None,
    })
}

fn ext_sts_text(code: u8) -> &'static str {
    match code {
        0x01 => "a field has an illegal value",
        0x02 => "fewer levels specified in the address than the minimum for any address",
        0x03 => "more levels specified in the address than the system supports",
        0x04 => "symbol not found",
        0x05 => "symbol is of improper format",
        0x06 => "address does not point to something usable",
        0x07 => "file is wrong size",
        0x08 => "cannot complete request, situation has changed since the start of the command",
        0x09 => "data or file is too large",
        0x0A => "transaction size plus word address is too large",
        0x0B => "access denied, improper privilege",
        0x0C => "condition cannot be generated, resource is not available",
        0x0D => "condition already exists, resource is already available",
        0x0E => "command cannot be executed",
        0x0F => "histogram overflow",
        0x10 => "no access",
        0x11 => "illegal data type",
        0x12 => "invalid parameter or invalid data",
        0x13 => "address reference exists to deleted area",
        0x14 => "command execution failure for unknown reason",
        0x15 => "data conversion error",
        0x17 => "type mismatch",
        0x1A => "file is open; another node owns it",
        0x1B => "another node is the program owner",
        0x1E => "data table element protection violation",
        0x1F => "temporary internal problem",
        0x23 => "timeout",
        0x24 => "unknown error",
        _ => "unknown",
    }
}

pub fn pccc_status_text(sts: u8, ext_sts: Option<u8>) -> String {
    // high nibble: remote error (from the PLC); low nibble: local error (from the link)
    let meaning = sts_text(sts & 0xF0).unwrap_or(if sts & 0x0F != 0 { "local error" } else { "unknown" });
    let text = format!("PCCC STS {sts:#04x} ({meaning})");
    if sts == EXT_STS_FOLLOWS {
        return match ext_sts {
            None => format!("{text}, EXT STS missing"),
            Some(e) => format!("{text}, EXT STS {e:#04x} ({})", ext_sts_text(e)),
        };
    }
    text
}

/// Statuses that mean the file/element does not exist or the address is illegal for the
/// processor (permanent for the block): `BlockConfig`. Everything else is a comm error.
pub fn is_config_status(sts: u8, ext_sts: Option<u8>) -> bool {
    if sts == EXT_STS_FOLLOWS {
        return ext_sts.is_some_and(|e| {
            matches!(e, 0x01 | 0x02 | 0x03 | 0x04 | 0x05 | 0x06 | 0x07 | 0x09 | 0x0A | 0x11 | 0x12 | 0x13 | 0x17)
        });
    }
    matches!(sts, 0x10 | 0x50)
}
