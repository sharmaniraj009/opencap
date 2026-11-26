//! Symbolic Logix/Micro800 addresses (Python `parse_logix_address`, `epath_size`): a light
//! syntax check, the `.n` bit split and the request path size the planner packs with. The
//! controller's tag list decides existence at run time.

use std::sync::LazyLock;

use regex::Regex;

use opencap_core::pyfmt::py_repr_str;

pub const MAX_BIT: u32 = 63;

const IDENT: &str = r"[A-Za-z_][A-Za-z0-9_]*";
const INDEX: &str = r"\[\d+(?:,\d+){0,2}\]";

static SYMBOL_RE: LazyLock<Regex> = LazyLock::new(|| {
    let seg = format!("{IDENT}(?:{INDEX})?");
    Regex::new(&format!("^(?:Program:{IDENT}\\.)?{seg}(?:\\.{seg})*$")).unwrap()
});
static BIT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?s)(?P<name>.+)\.(?P<bit>\d+)$").unwrap());

/// A validated symbolic address: the name to read and, for `Tag.n`, the bit taken from the
/// integer that was read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogixAddress {
    pub read_name: String,
    pub bit: Option<u32>,
}

/// Validate a symbolic address (`Tag`, `A.B`, `Program:X.Y`, `T[1,2]`, `T.5`). Errors carry
/// the Python text.
pub fn parse_logix_address(address: &str, micro800: bool) -> Result<LogixAddress, String> {
    let text = address.trim();
    if text.is_empty() {
        return Err("empty address".into());
    }
    if text.contains('{') || text.contains('}') {
        return Err(format!("array block reads ({{n}}) are not supported in tag addresses: {}", py_repr_str(text)));
    }
    if pccc_accepts(text) {
        return Err("PCCC data-table address on a symbolic (Logix/Micro800) device".into());
    }
    let (mut name, mut bit) = (text.to_string(), None);
    if let Some(m) = BIT_RE.captures(text) {
        if SYMBOL_RE.is_match(&m["name"]) {
            let n: u32 = m["bit"].parse().unwrap_or(u32::MAX);
            if n > MAX_BIT {
                return Err(format!("bit {} out of range (0-{MAX_BIT}) in {}", &m["bit"], py_repr_str(text)));
            }
            name = m["name"].to_string();
            bit = Some(n);
        }
    }
    if !SYMBOL_RE.is_match(&name) {
        return Err(format!("illegal symbolic address {}", py_repr_str(text)));
    }
    if micro800 && name.starts_with("Program:") {
        return Err(format!("program-scoped tags are not readable on Micro800 (globals only): {}", py_repr_str(text)));
    }
    Ok(LogixAddress { read_name: name, bit })
}

/// Padded logical member segment: 8-bit 2 bytes, 16-bit 4 bytes, 32-bit 6 bytes.
fn member_size(index: u64) -> usize {
    if index <= 0xFF {
        2
    } else if index <= 0xFFFF {
        4
    } else {
        6
    }
}

/// Bytes of the symbolic request path including its length byte (Python `epath_size`): one
/// ANSI symbol segment per part, plus a member segment per array index.
pub fn epath_size(read_name: &str) -> usize {
    let mut size = 1;
    for part in read_name.split('.') {
        let (ident, idx) = part.split_once('[').map_or((part, None), |(a, b)| (a, Some(b)));
        let n = ident.len();
        size += 2 + n + n % 2;
        if let Some(idx) = idx {
            size += idx.trim_end_matches(']').split(',').map(|i| member_size(i.parse().unwrap_or(0))).sum::<usize>();
        }
    }
    size
}

// --- PCCC acceptance ---------------------------------------------------------------------------
//
// `parse_logix_address` first asks whether the text is a valid PCCC data-table address
// (Python `ab_pccc.parse_pccc_address`): such an address on a symbolic device gets its own
// message. Only the yes/no answer matters here, so this is the accepting side of that parser:
// the same regexes and range checks.

static DATA_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?P<ft>[NFLB])(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?:/(?P<bit>\d{1,3}))?$").unwrap());
static BITNUM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^B(?P<fn>\d{1,3})/(?P<bitnum>\d{1,5})$").unwrap());
static TC_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?P<ft>[TC])(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?:[./](?P<sub>[A-Z]+))?$").unwrap());
static S_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^S(?P<fn>\d{1,3})?:(?P<el>\d{1,4})(?:/(?P<bit>\d{1,3}))?$").unwrap());
static IO_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?P<ft>[IO])(?P<fn>\d{1,3})?:(?P<slot>\d{1,4})(?:\.(?P<word>\d{1,4}))?(?:/(?P<bit>\d{1,4}))?$")
        .unwrap()
});
static ST_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^ST(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?P<bit>/\d+)?$").unwrap());

const MAX_FILE: u64 = 255;
const MAX_ELEMENT: u64 = 255;

fn num(m: &regex::Captures, name: &str) -> u64 {
    m.name(name).map_or(0, |g| g.as_str().parse().unwrap_or(u64::MAX))
}

fn within(v: u64, lo: u64, hi: u64) -> bool {
    (lo..=hi).contains(&v)
}

fn tc_fields(ft: &str) -> &'static [&'static str] {
    match ft {
        "T" => &["ACC", "PRE", "EN", "TT", "DN"],
        _ => &["ACC", "PRE", "CU", "CD", "DN", "OV", "UN", "UA"],
    }
}

/// True when `address` is a valid PCCC data-table address (SLC 500 / MicroLogix / PLC-5).
pub fn pccc_accepts(address: &str) -> bool {
    let text = address.trim();
    if text.is_empty() || text.contains('{') || text.contains('}') {
        return false;
    }
    let u = text.to_uppercase();
    if u.starts_with("PROGRAM:") || !u.contains([':', '/']) {
        return false;
    }
    if let Some(m) = ST_RE.captures(&u) {
        return m.name("bit").is_none() && within(num(&m, "fn"), 1, MAX_FILE) && within(num(&m, "el"), 0, MAX_ELEMENT);
    }
    if let Some(m) = DATA_RE.captures(&u) {
        if !(within(num(&m, "fn"), 1, MAX_FILE) && within(num(&m, "el"), 0, MAX_ELEMENT)) {
            return false;
        }
        return match m.name("bit") {
            None => true,
            Some(_) => &m["ft"] != "F" && within(num(&m, "bit"), 0, if &m["ft"] == "L" { 31 } else { 15 }),
        };
    }
    if let Some(m) = BITNUM_RE.captures(&u) {
        return within(num(&m, "fn"), 1, MAX_FILE) && within(num(&m, "bitnum"), 0, (MAX_ELEMENT + 1) * 16 - 1);
    }
    if let Some(m) = TC_RE.captures(&u) {
        return within(num(&m, "fn"), 1, MAX_FILE)
            && within(num(&m, "el"), 0, MAX_ELEMENT)
            && m.name("sub").is_some_and(|s| tc_fields(&m["ft"]).contains(&s.as_str()));
    }
    if let Some(m) = S_RE.captures(&u) {
        if m.name("fn").is_some() && num(&m, "fn") != 2 {
            return false;
        }
        return within(num(&m, "el"), 0, MAX_ELEMENT) && (m.name("bit").is_none() || within(num(&m, "bit"), 0, 15));
    }
    if let Some(m) = IO_RE.captures(&u) {
        let expected = if &m["ft"] == "I" { 1 } else { 0 };
        if m.name("fn").is_some() && num(&m, "fn") != expected {
            return false;
        }
        if !within(num(&m, "slot"), 0, MAX_ELEMENT) {
            return false;
        }
        let mut word = num(&m, "word");
        if m.name("bit").is_some() {
            word += num(&m, "bit") / 16;
        }
        return within(word, 0, MAX_ELEMENT);
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pccc_addresses_are_recognised() {
        for ok in
            ["N7:0", "F28:0", "B3:0/14", "B3/50", "T4:1.ACC", "C5:2/DN", "S:1/5", "I:0.1/3", "O:1", "ST9:2", "L9:3/31"]
        {
            assert!(pccc_accepts(ok), "{ok}");
        }
        for no in [
            "N7:0/16",
            "F8:0/1",
            "N7:300",
            "FT_01",
            "A:B",
            "T4:1",
            "T4:1.XX",
            "S2:300",
            "I:300",
            "ST9:1/2",
            "Program:Main",
        ] {
            assert!(!pccc_accepts(no), "{no}");
        }
    }

    #[test]
    fn addresses_split_bits_and_paths() {
        let a = parse_logix_address("DINT_TAG.5", false).unwrap();
        assert_eq!((a.read_name.as_str(), a.bit), ("DINT_TAG", Some(5)));
        assert_eq!(epath_size("FT_01"), 9);
        assert_eq!(epath_size("ARRAY_REAL[4]"), 15);
        assert_eq!(epath_size("UDT[2].Member[3].X"), 23);
        assert!(parse_logix_address("X.99", false).unwrap_err().starts_with("bit 99 out of range"));
        assert!(parse_logix_address("Program:Main.X", true).is_err());
    }
}
