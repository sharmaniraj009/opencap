//! PCCC data-table addresses (Python `parse_pccc_address`, `check_dtype`).

use std::sync::LazyLock;

use regex::Regex;

use opencap_core::config::DType;
use opencap_core::pyfmt::py_repr_str;

pub const WORD_FILES: [&str; 5] = ["N", "B", "S", "I", "O"];
pub const MAX_FILE: i64 = 255;
pub const MAX_ELEMENT: i64 = 255;
pub const STRING_MAX_COUNT: usize = 2;
pub const PCCC_MAX_BYTES: usize = 255;

/// MicroLogix function files: no address form here.
const FUNCTION_FILES: [&str; 18] = [
    "RTC", "RTA", "HSC", "PTO", "PTOX", "PWM", "PWMX", "STI", "EII", "BHI", "MMI", "DAT", "TPI", "CS", "IOS", "LCD",
    "DLS", "ES",
];

pub fn elem_size(ft: &str) -> usize {
    match ft {
        "L" | "F" => 4,
        "T" | "C" => 6,
        "ST" => 84,
        _ => 2,
    }
}

pub fn tc_fields(ft: &str) -> Option<&'static [&'static str]> {
    match ft {
        "T" => Some(&["ACC", "PRE", "EN", "TT", "DN"]),
        "C" => Some(&["ACC", "PRE", "CU", "CD", "DN", "OV", "UN", "UA"]),
        _ => None,
    }
}

pub fn is_tc_word_field(f: &str) -> bool {
    f == "ACC" || f == "PRE"
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PcccAddress {
    pub file_type: &'static str,
    pub file_number: u32,
    /// element number; for I/O files the slot
    pub element: u32,
    /// I/O: the word within the slot
    pub word: u32,
    pub bit: Option<u32>,
    /// T/C sub-element
    pub field: Option<&'static str>,
}

impl PcccAddress {
    pub fn is_bit(&self) -> bool {
        self.bit.is_some() || self.field.is_some_and(|f| !is_tc_word_field(f))
    }

    pub fn canonical(&self) -> String {
        let bit = self.bit.map(|b| format!("/{b}")).unwrap_or_default();
        let ft = self.file_type;
        match ft {
            "I" | "O" => format!("{ft}:{}.{}{bit}", self.element, self.word),
            "S" => format!("S:{}{bit}", self.element),
            "T" | "C" => format!("{ft}{}:{}.{}", self.file_number, self.element, self.field.unwrap_or("")),
            _ => format!("{ft}{}:{}{bit}", self.file_number, self.element),
        }
    }
}

fn re(p: &str) -> Regex {
    Regex::new(&format!("^(?:{p})$")).expect("regex")
}

static DATA_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?P<ft>[NFLB])(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?:/(?P<bit>\d{1,3}))?"));
static BITNUM_RE: LazyLock<Regex> = LazyLock::new(|| re(r"B(?P<fn>\d{1,3})/(?P<bitnum>\d{1,5})"));
static TC_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?P<ft>[TC])(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?:[./](?P<sub>[A-Z]+))?"));
static S_RE: LazyLock<Regex> = LazyLock::new(|| re(r"S(?P<fn>\d{1,3})?:(?P<el>\d{1,4})(?:/(?P<bit>\d{1,3}))?"));
static IO_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(r"(?P<ft>[IO])(?P<fn>\d{1,3})?:(?P<slot>\d{1,4})(?:\.(?P<word>\d{1,4}))?(?:/(?P<bit>\d{1,4}))?")
});
static ST_RE: LazyLock<Regex> = LazyLock::new(|| re(r"ST(?P<fn>\d{1,3}):(?P<el>\d{1,4})(?P<bit>/\d+)?"));
static FUNC_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?P<ff>[A-Z]{2,4}):\d+(?:\.\w+)?(?:/\d+)?"));
static FILETYPE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?P<ft>[A-Z]{1,3})\d{0,3}:.*"));
static SYMBOL_HINT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[:/]").expect("regex"));

fn num(m: &regex::Captures, name: &str) -> i64 {
    m[name].parse().unwrap_or(i64::MAX)
}

fn check_range(what: &str, value: i64, lo: i64, hi: i64, address: &str) -> Result<u32, String> {
    if !(lo..=hi).contains(&value) {
        return Err(format!("{what} {value} out of range ({lo}-{hi}) in {}", py_repr_str(address)));
    }
    Ok(value as u32)
}

/// Parse a PCCC data-table address. `Err` carries the readable reason.
pub fn parse_pccc_address(address: &str) -> Result<PcccAddress, String> {
    let text = address.trim();
    if text.is_empty() {
        return Err("empty address".into());
    }
    let u = text.to_uppercase();
    let q = py_repr_str(text);
    if u.contains('{') || u.contains('}') {
        return Err(format!("element count {{n}} in a tag address is not supported: {q}"));
    }
    if u.starts_with("PROGRAM:") || !SYMBOL_HINT.is_match(&u) {
        return Err("symbolic address on a PCCC device".into());
    }
    let a = |ft: &'static str, fnum: u32, element: u32| PcccAddress {
        file_type: ft,
        file_number: fnum,
        element,
        word: 0,
        bit: None,
        field: None,
    };

    if let Some(m) = ST_RE.captures(&u) {
        if m.name("bit").is_some() {
            return Err(format!("bit access is not supported on a ST file: {q}"));
        }
        let fnum = check_range("file number", num(&m, "fn"), 1, MAX_FILE, text)?;
        let el = check_range("element", num(&m, "el"), 0, MAX_ELEMENT, text)?;
        return Ok(a("ST", fnum, el));
    }
    if let Some(m) = DATA_RE.captures(&u) {
        let ft: &'static str = match &m["ft"] {
            "N" => "N",
            "F" => "F",
            "L" => "L",
            _ => "B",
        };
        let fnum = check_range("file number", num(&m, "fn"), 1, MAX_FILE, text)?;
        let el = check_range("element", num(&m, "el"), 0, MAX_ELEMENT, text)?;
        let mut out = a(ft, fnum, el);
        if m.name("bit").is_some() {
            if ft == "F" {
                return Err(format!("bit access is not supported on a F file: {q}"));
            }
            out.bit = Some(check_range("bit", num(&m, "bit"), 0, if ft == "L" { 31 } else { 15 }, text)?);
        }
        return Ok(out);
    }
    if let Some(m) = BITNUM_RE.captures(&u) {
        let fnum = check_range("file number", num(&m, "fn"), 1, MAX_FILE, text)?;
        let bitnum = check_range("bit number", num(&m, "bitnum"), 0, (MAX_ELEMENT + 1) * 16 - 1, text)?;
        let mut out = a("B", fnum, bitnum / 16);
        out.bit = Some(bitnum % 16);
        return Ok(out);
    }
    if let Some(m) = TC_RE.captures(&u) {
        let ft: &'static str = if &m["ft"] == "T" { "T" } else { "C" };
        let fnum = check_range("file number", num(&m, "fn"), 1, MAX_FILE, text)?;
        let el = check_range("element", num(&m, "el"), 0, MAX_ELEMENT, text)?;
        let fields = tc_fields(ft).expect("T/C");
        let Some(sub) = m.name("sub") else {
            let list = fields.iter().map(|f| format!(".{f}")).collect::<Vec<_>>().join(", ");
            return Err(format!("{ft} address needs a sub-element ({list}): {q}"));
        };
        let Some(field) = fields.iter().find(|f| **f == sub.as_str()) else {
            return Err(format!("sub-element .{} is not valid for a {ft} file: {q}", sub.as_str()));
        };
        let mut out = a(ft, fnum, el);
        out.field = Some(field);
        return Ok(out);
    }
    if let Some(m) = S_RE.captures(&u) {
        if let Some(f) = m.name("fn") {
            if f.as_str().parse::<i64>().unwrap_or(-1) != 2 {
                return Err(format!("file number of the S (status) file must be 2: {q}"));
            }
        }
        let el = check_range("element", num(&m, "el"), 0, MAX_ELEMENT, text)?;
        let mut out = a("S", 2, el);
        if m.name("bit").is_some() {
            out.bit = Some(check_range("bit", num(&m, "bit"), 0, 15, text)?);
        }
        return Ok(out);
    }
    if let Some(m) = IO_RE.captures(&u) {
        let (ft, expected): (&'static str, i64) = if &m["ft"] == "I" { ("I", 1) } else { ("O", 0) };
        if let Some(f) = m.name("fn") {
            if f.as_str().parse::<i64>().unwrap_or(-1) != expected {
                return Err(format!("file number of an {ft} file must be {expected}: {q}"));
            }
        }
        let slot = check_range("slot", num(&m, "slot"), 0, MAX_ELEMENT, text)?;
        let mut word = m.name("word").map_or(0, |w| w.as_str().parse::<i64>().unwrap_or(0));
        let mut bit = None;
        if m.name("bit").is_some() {
            let bitnum = num(&m, "bit");
            word += bitnum / 16;
            bit = Some((bitnum % 16) as u32);
        }
        let word = check_range("word", word, 0, MAX_ELEMENT, text)?;
        let mut out = a(ft, expected as u32, slot);
        out.word = word;
        out.bit = bit;
        return Ok(out);
    }
    if let Some(m) = FUNC_RE.captures(&u) {
        if FUNCTION_FILES.contains(&&m["ff"]) {
            return Err(format!("MicroLogix function file {} is not supported by pycomm3 SLCDriver: {q}", &m["ff"]));
        }
    }
    if let Some(m) = FILETYPE_RE.captures(&u) {
        let ft = &m["ft"];
        let known = ["N", "B", "S", "I", "O", "L", "F", "T", "C", "ST"].contains(&ft);
        if !known && !FUNCTION_FILES.contains(&ft) {
            return Err(format!("file type {ft} is not supported by pycomm3 SLCDriver: {q}"));
        }
    }
    Err(format!("cannot parse PCCC address {q}"))
}

fn dtype_class(addr: &PcccAddress) -> &'static str {
    if addr.is_bit() {
        "bit"
    } else if WORD_FILES.contains(&addr.file_type) || tc_fields(addr.file_type).is_some() {
        "word"
    } else {
        addr.file_type // L, F, ST
    }
}

fn allowed(class: &str) -> &'static [&'static str] {
    match class {
        "bit" => &["bool", "float32", "float64", "int16", "int32", "int64", "uint16", "uint32", "uint64"],
        "word" => &["bool", "float32", "float64", "int16", "uint16"],
        "L" => &["bool", "float32", "float64", "int32", "int64", "uint32", "uint64"],
        "F" => &["float32", "float64"],
        _ => &["string"],
    }
}

/// `Err` when `dtype` cannot be read from `addr` without guessing.
pub fn check_dtype(addr: &PcccAddress, dtype: DType) -> Result<(), String> {
    let cls = dtype_class(addr);
    let list = allowed(cls);
    if list.contains(&dtype.as_str()) {
        return Ok(());
    }
    let what = match cls {
        "bit" => "a bit address".to_string(),
        "word" => "a 16-bit word".to_string(),
        other => format!("a {other} file element"),
    };
    Err(format!(
        "dtype {} is not supported on {what} ({}); allowed: {}",
        dtype.as_str(),
        addr.canonical(),
        list.join(", ")
    ))
}
