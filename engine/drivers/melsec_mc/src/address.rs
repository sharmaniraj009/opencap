//! Kepware "Mitsubishi Ethernet" (Q Series) address parser (Python `parse_address`, D-054).
//!
//! ```text
//! D0000002          word device, 1/2/4 words by dtype (16/32/64-bit)
//! B0000, M10        one point of a bit device (bool)
//! M16 (uint16)      16 points of a bit device as a word; needs a 16-point boundary
//! D100.05           bit 0..15 of a word, 16..31 of the 2-word value (bool)
//! D100[4], D0[2][3] arrays on word devices (not bool, not strings)
//! DSH100.020        string of 20 bytes, first char in the high byte (DSL: low byte);
//!                   also WSH/WSL, RSH/RSL, ZRSH/ZRSL; length 2..128, even; '.' or ':'
//! ```

use std::sync::OnceLock;

use regex::Regex;

use opencap_core::config::DType;
use opencap_core::pyfmt::py_repr_str;

pub const MAX_STRING_LEN: u128 = 128;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Bit,
    Word,
}

#[derive(Debug, Clone, Copy)]
pub struct DeviceSpec {
    /// Kepware / GX Works name.
    pub name: &'static str,
    pub kind: Kind,
    /// 10 or 16: how the device number is written.
    pub base: u32,
    /// Last device number in Kepware's Q Series table.
    pub last: u128,
    /// Binary device code of the MC protocol.
    pub code: u8,
    /// Word devices: `D100.05` bit addresses.
    pub bit_access: bool,
    /// Word devices: `DSH100.020` string addresses.
    pub strings: bool,
    /// Word devices: 32/64-bit values allowed.
    pub wide: bool,
}

const fn bit(name: &'static str, base: u32, last: u128, code: u8) -> DeviceSpec {
    DeviceSpec { name, kind: Kind::Bit, base, last, code, bit_access: false, strings: false, wide: true }
}

const fn word(name: &'static str, base: u32, last: u128, code: u8, strings: bool) -> DeviceSpec {
    DeviceSpec { name, kind: Kind::Word, base, last, code, bit_access: true, strings, wide: true }
}

const fn narrow(name: &'static str, base: u32, last: u128, code: u8) -> DeviceSpec {
    DeviceSpec { name, kind: Kind::Word, base, last, code, bit_access: false, strings: false, wide: false }
}

/// Kepware "Mitsubishi Ethernet" Q Series Address Descriptions (ranges and radix), in the
/// Python table's order; device codes from the MELSEC communication protocol reference.
pub static DEVICES: [DeviceSpec; 26] = [
    bit("X", 16, 0x3FFF, 0x9C),
    bit("DX", 16, 0x3FFF, 0xA2),
    bit("Y", 16, 0x3FFF, 0x9D),
    bit("DY", 16, 0x3FFF, 0xA3),
    bit("B", 16, 0xEA60, 0xA0),
    bit("SB", 16, 0x7D00, 0xA1),
    bit("M", 10, 60000, 0x90),
    bit("SM", 10, 2047, 0x91),
    bit("L", 10, 32000, 0x92),
    bit("F", 10, 32000, 0x93),
    bit("V", 10, 32000, 0x94),
    bit("TS", 10, 32000, 0xC1),
    bit("TC", 10, 32000, 0xC0),
    bit("SS", 10, 2047, 0xC7),
    bit("SC", 10, 2047, 0xC6),
    bit("CS", 10, 32000, 0xC4),
    bit("CC", 10, 32000, 0xC3),
    word("D", 10, 4184063, 0xA8, true),
    word("SD", 10, 2047, 0xA9, false),
    word("W", 16, 0x3FD7FF, 0xB4, true),
    word("SW", 16, 0x7D00, 0xB5, false),
    word("R", 10, 32767, 0xAF, true),
    word("ZR", 16, 0x3FD7FF, 0xB0, true),
    narrow("TN", 10, 32000, 0xC2),
    narrow("SN", 10, 2047, 0xC8),
    narrow("CN", 10, 32000, 0xC5),
];

pub fn device_spec(name: &str) -> Option<&'static DeviceSpec> {
    DEVICES.iter().find(|d| d.name == name)
}

/// A device number as Kepware writes it: hex (upper case) or decimal.
pub fn format_number(device: &str, number: u128) -> String {
    match device_spec(device) {
        Some(s) if s.base == 16 => format!("{number:X}"),
        _ => number.to_string(),
    }
}

/// Word-unit count of a dtype (`WORDS_PER_DTYPE`); `None` for bool and string.
pub fn words_per_dtype(dtype: DType) -> Option<u128> {
    Some(match dtype {
        DType::Int16 | DType::Uint16 => 1,
        DType::Int32 | DType::Uint32 | DType::Float32 => 2,
        DType::Int64 | DType::Uint64 | DType::Float64 => 4,
        DType::Bool | DType::String => return None,
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McAddress {
    /// Address as written (stripped).
    pub text: String,
    /// Kepware device name, e.g. "D", "M", "X".
    pub device: &'static str,
    pub kind: Kind,
    /// Device number as written (hex already converted).
    pub number: u128,
    pub dtype: DType,
    /// First word read: device number (word devices) or `number / 16` (bit devices).
    pub start: u128,
    /// Words read.
    pub words: u128,
    /// bool: bit of the (1- or 2-word) value, 0 = LSB.
    pub bit: Option<u32>,
    /// Array dimensions, empty = scalar.
    pub dims: Vec<u128>,
    pub str_len: Option<u128>,
    /// "H" (first char in the high byte) or "L".
    pub str_order: Option<char>,
}

impl McAddress {
    /// Exclusive end, in words.
    pub fn end(&self) -> u128 {
        self.start + self.words
    }

    /// Identity of the data read (ignores the spelling): equal keys are read and decoded once.
    /// The order is Python's tuple order, so planning is identical.
    pub fn key(&self) -> AddrKey {
        (
            self.device,
            self.start,
            self.words,
            self.dtype.as_str(),
            self.bit.map_or(-1, i64::from),
            self.dims.clone(),
            self.str_len.unwrap_or(0),
            self.str_order.map(String::from).unwrap_or_default(),
        )
    }
}

pub type AddrKey = (&'static str, u128, u128, &'static str, i64, Vec<u128>, u128, String);

struct Prefix {
    text: String,
    spec: &'static DeviceSpec,
    order: Option<char>,
}

fn prefixes() -> &'static [Prefix] {
    static P: OnceLock<Vec<Prefix>> = OnceLock::new();
    P.get_or_init(|| {
        let mut out: Vec<Prefix> = Vec::new();
        for spec in &DEVICES {
            out.push(Prefix { text: spec.name.to_string(), spec, order: None });
            if spec.strings {
                for order in ['H', 'L'] {
                    out.push(Prefix { text: format!("{}S{order}", spec.name), spec, order: Some(order) });
                }
            }
        }
        // Longest prefix first, so DX wins over D and ZRSH over ZR.
        out.sort_by(|a, b| b.text.len().cmp(&a.text.len()).then_with(|| a.text.cmp(&b.text)));
        out
    })
}

fn rest_re(base: u32) -> &'static Regex {
    static RE: OnceLock<[Regex; 2]> = OnceLock::new();
    let both = RE.get_or_init(|| {
        let make = |digits: &str| {
            Regex::new(&format!(
                r"^(?P<num>{digits})(?P<sfx>[.:][0-9]{{1,3}})?(?P<dims>(?:\s*\[\s*[0-9]+\s*\])*)(?P<long>\s+L)?$"
            ))
            .expect("static regex")
        };
        [make("[0-9]+"), make("[0-9A-F]+")]
    });
    if base == 16 {
        &both[1]
    } else {
        &both[0]
    }
}

fn dim_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\[\s*([0-9]+)\s*\]").expect("static regex"))
}

fn parse_int(text: &str, base: u32) -> u128 {
    u128::from_str_radix(text, base).unwrap_or(u128::MAX)
}

/// Parse a Kepware Mitsubishi (Q Series) address for a tag of `dtype`. The error is Python's
/// `ValueError` text.
pub fn parse_address(address: &str, dtype: DType) -> Result<McAddress, String> {
    let bad = |reason: &str| format!("melsec address {} ({}): {reason}", py_repr_str(address), dtype.as_str());

    let text = address.trim();
    if text.is_empty() {
        return Err(bad("empty address"));
    }
    let upper = text.to_uppercase();
    let found = prefixes().iter().find_map(|p| {
        let rest = upper.strip_prefix(p.text.as_str())?;
        rest_re(p.spec.base).captures(rest).map(|c| (p, c))
    });
    let Some((prefix, caps)) = found else {
        return Err(bad(&format!(
            "unknown device or malformed number; supported devices: {} (and DSH/DSL, WSH/WSL, RSH/RSL, ZRSH/ZRSL strings)",
            DEVICES.iter().map(|d| d.name).collect::<Vec<_>>().join(", ")
        )));
    };
    let spec = prefix.spec;
    let order = prefix.order;
    if caps.name("long").is_some() {
        return Err(bad("the ' L' suffix (Kepware long access) is not supported; use dtype int32/uint32"));
    }
    let num_text = caps.name("num").map_or("", |m| m.as_str());
    let number = parse_int(num_text, spec.base);
    let dims: Vec<u128> =
        dim_re().captures_iter(caps.name("dims").map_or("", |m| m.as_str())).map(|c| parse_int(&c[1], 10)).collect();
    if dims.len() > 2 {
        return Err(bad("at most 2 array dimensions ([rows][cols])"));
    }
    if dims.contains(&0) {
        return Err(bad("array dimension must be >= 1"));
    }
    let sfx = caps.name("sfx").map(|m| m.as_str());
    let name = spec.name;

    let mut bit: Option<u32> = None;
    let mut str_len: Option<u128> = None;
    let (start, words, last_point);

    if let Some(order) = order {
        // DSH100.020 string
        if dtype != DType::String {
            return Err(bad(&format!("a {name}S{order} string address needs dtype string")));
        }
        let Some(sfx) = sfx else {
            return Err(bad(&format!("string needs a length, e.g. {name}S{order}{num_text}.020")));
        };
        if !dims.is_empty() {
            return Err(bad("arrays of strings are not supported"));
        }
        let len = parse_int(&sfx[1..], 10);
        if !(2..=MAX_STRING_LEN).contains(&len) || len % 2 != 0 {
            return Err(bad(&format!("string length {len} must be 2..{MAX_STRING_LEN} and even")));
        }
        str_len = Some(len);
        start = number;
        words = len / 2;
        last_point = number.saturating_add(words - 1);
    } else {
        if dtype == DType::String {
            let hint = if spec.strings {
                format!("use the {name}SH/{name}SL form, e.g. {name}SH{num_text}.020")
            } else {
                "strings are only supported on D, W, R and ZR".to_string()
            };
            return Err(bad(&format!("string needs a length and byte order: {hint}")));
        }
        if sfx.is_some_and(|s| s.starts_with(':')) {
            return Err(bad("':' is only valid in string addresses"));
        }
        if spec.kind == Kind::Bit {
            if sfx.is_some() {
                return Err(bad("a .bit suffix is only valid on word devices"));
            }
            if !dims.is_empty() {
                return Err(bad("arrays are not supported on bit devices"));
            }
            if dtype == DType::Bool {
                start = number / 16;
                words = 1;
                bit = Some((number % 16) as u32);
                last_point = number;
            } else {
                if !matches!(dtype, DType::Int16 | DType::Uint16 | DType::Int32 | DType::Uint32) {
                    return Err(bad(&format!(
                        "{name} is a bit device: only bool, or 16/32-bit integers (16 or 32 points read as words)"
                    )));
                }
                if number % 16 != 0 {
                    return Err(bad(&format!(
                        "word access to bit device {name} must start on a 16-point boundary (e.g. {name}{})",
                        format_number(name, number - number % 16)
                    )));
                }
                start = number / 16;
                words = words_per_dtype(dtype).expect("integer dtype");
                last_point = number.saturating_add(16 * words - 1);
            }
        } else {
            if let Some(sfx) = sfx {
                // D100.05 bit in a word
                if !spec.bit_access {
                    return Err(bad(&format!("bit access is not supported on {name}")));
                }
                if dtype != DType::Bool {
                    return Err(bad("a .bit address must have dtype bool"));
                }
                if !dims.is_empty() {
                    return Err(bad("bool arrays are not supported"));
                }
                let b = parse_int(&sfx[1..], 10);
                if b > 31 {
                    return Err(bad(&format!("bit .{b} out of range 0..31")));
                }
                bit = Some(b as u32);
                words = if b < 16 { 1 } else { 2 };
            } else {
                if dtype == DType::Bool {
                    return Err(bad(&format!(
                        "bool on word device {name} needs a .bit suffix, e.g. {name}{num_text}.00"
                    )));
                }
                let per = words_per_dtype(dtype).expect("numeric dtype");
                if !spec.wide && per > 1 {
                    return Err(bad(&format!("{name} holds 16-bit values only (int16/uint16)")));
                }
                let count = dims.iter().fold(1u128, |acc, d| acc.saturating_mul(*d));
                words = count.saturating_mul(per);
            }
            start = number;
            last_point = number.saturating_add(words - 1);
        }
    }

    if last_point > spec.last {
        return Err(bad(&format!(
            "needs {name}{}..{}, past the last {name} device {} (Kepware Q Series range)",
            format_number(name, number),
            format_number(name, last_point),
            format_number(name, spec.last)
        )));
    }
    Ok(McAddress {
        text: text.to_string(),
        device: spec.name,
        kind: spec.kind,
        number,
        dtype,
        start,
        words,
        bit,
        dims,
        str_len,
        str_order: order,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(a: &str, d: DType) -> McAddress {
        parse_address(a, d).unwrap_or_else(|e| panic!("{a}: {e}"))
    }

    #[test]
    fn word_and_bit_devices() {
        let a = ok("D0000002", DType::Float32);
        assert_eq!((a.device, a.number, a.start, a.words, a.bit), ("D", 2, 2, 2, None));
        let b = ok("B001F", DType::Bool);
        assert_eq!((b.number, b.start, b.bit), (0x1F, 1, Some(15)));
        let w = ok("W1F", DType::Uint16);
        assert_eq!((w.device, w.number), ("W", 0x1F));
        assert_eq!(ok("  d0000002 ", DType::Float32).text, "d0000002");
    }

    #[test]
    fn bits_in_words_and_words_of_bits() {
        assert_eq!(ok("D100.5", DType::Bool).key(), ok("D100.05", DType::Bool).key());
        let d = ok("D100.17", DType::Bool);
        assert_eq!((d.words, d.bit), (2, Some(17)));
        let m = ok("M16", DType::Uint16);
        assert_eq!((m.start, m.words, m.bit), (1, 1, None));
        assert!(parse_address("M17", DType::Uint16).unwrap_err().contains("16-point boundary"));
    }

    #[test]
    fn arrays_and_strings() {
        assert_eq!(ok("D100 [2][3]", DType::Float32).words, 12);
        let s = ok("DSL100:020", DType::String);
        assert_eq!((s.start, s.words, s.str_len, s.str_order), (100, 10, Some(20), Some('L')));
        assert_eq!(ok("WSH1F.004", DType::String).number, 0x1F);
        assert_eq!(ok("ZRSH10.128", DType::String).device, "ZR");
    }

    #[test]
    fn rejected_forms_name_the_reason() {
        let cases: [(&str, DType, &str); 8] = [
            ("D100", DType::Bool, "needs a .bit suffix"),
            ("D100.32", DType::Bool, "bit .32 out of range 0..31"),
            ("TN0", DType::Float32, "16-bit values only"),
            ("CS0000 L", DType::Int32, "' L' suffix"),
            ("D4184064", DType::Uint16, "past the last D device"),
            ("DSH100.003", DType::String, "2..128 and even"),
            ("D100", DType::String, "DSH100.020"),
            ("K4M0", DType::Uint16, "unknown device"),
        ];
        for (a, d, reason) in cases {
            let e = parse_address(a, d).unwrap_err();
            assert!(e.contains(reason), "{a}: {e}");
        }
    }

    #[test]
    fn error_text_quotes_the_address_as_written() {
        assert_eq!(parse_address("", DType::Uint16).unwrap_err(), "melsec address '' (uint16): empty address");
        assert_eq!(
            parse_address("D100", DType::Bool).unwrap_err(),
            "melsec address 'D100' (bool): bool on word device D needs a .bit suffix, e.g. D100.00"
        );
    }

    #[test]
    fn every_device_parses() {
        for d in &DEVICES {
            let dtype = if d.kind == Kind::Bit { DType::Bool } else { DType::Uint16 };
            let a = ok(&format!("{}10", d.name), dtype);
            assert_eq!((a.device, a.number), (d.name, u128::from_str_radix("10", d.base).unwrap()));
        }
    }
}
