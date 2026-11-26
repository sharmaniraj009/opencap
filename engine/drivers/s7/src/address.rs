//! Kepware "Siemens TCP/IP Ethernet" address parser (Python `parse_s7_address`, D-049).
//!
//! ```text
//! <area><type><byte>[.bit | .len][[rows][cols]]   area I/E, Q/A, M/F
//! DB<n>,<type><byte>[...]                         DB100,D04  DB1,REAL8  DB1,X5.6  DB1,STRING0.20
//! DB<n>,DBX<byte>.<bit> / DBB / DBW / DBD         Step 7 form after the comma
//! DB<n>.DBX<byte>.<bit> / DBB / DBW / DBD         Siemens-native form
//! T<n> (S5TIME -> milliseconds),  C<n> / Z<n> (BCD counter value)
//! ```

use std::sync::OnceLock;

use regex::{Captures, Regex};

use opencap_core::config::DType;
use opencap_core::pyfmt::py_repr_str;

use crate::params::S7Params;

pub const ADDRESS_SPACE: u128 = 65536;
pub const MAX_DB: u128 = 65535;
pub const MAX_TC: u128 = 65535;
pub const MAX_STRING: u128 = 254;
pub const MAX_RAW_STRING: u128 = 932;

/// Kind of value (Python `S7Address.kind`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Kind {
    Bit,
    Byte,
    Word,
    Dword,
    Real,
    Lreal,
    Lword,
    String,
    RawString,
    Timer,
    Counter,
}

impl Kind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Kind::Bit => "bit",
            Kind::Byte => "byte",
            Kind::Word => "word",
            Kind::Dword => "dword",
            Kind::Real => "real",
            Kind::Lreal => "lreal",
            Kind::Lword => "lword",
            Kind::String => "string",
            Kind::RawString => "rawstring",
            Kind::Timer => "timer",
            Kind::Counter => "counter",
        }
    }
}

/// Memory area. The derived order is the Python string order of the area names
/// (`C < DB < I < M < Q < T`), which the planner relies on.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Area {
    C,
    Db,
    I,
    M,
    Q,
    T,
}

impl Area {
    pub fn as_str(&self) -> &'static str {
        match self {
            Area::C => "C",
            Area::Db => "DB",
            Area::I => "I",
            Area::M => "M",
            Area::Q => "Q",
            Area::T => "T",
        }
    }

    /// S7 area code on the wire.
    pub fn code(&self) -> u8 {
        match self {
            Area::I => 0x81,
            Area::Q => 0x82,
            Area::M => 0x83,
            Area::Db => 0x84,
            Area::C => 0x1C,
            Area::T => 0x1D,
        }
    }

    pub fn is_tc(&self) -> bool {
        matches!(self, Area::T | Area::C)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct S7Address {
    pub text: String,
    pub area: Area,
    pub db: u128,
    /// Byte offset; timer/counter number for T/C.
    pub start: u128,
    pub kind: Kind,
    /// Bytes per element (strings: the whole string).
    pub size: u128,
    pub dtype: DType,
    pub count: u128,
    pub bit: Option<u32>,
    pub dims: Vec<u128>,
    pub str_len: Option<u128>,
}

pub type AddrKey = (Area, u128, u128, &'static str, u128, u128, &'static str, i64, Vec<u128>, u128);

impl S7Address {
    pub fn nbytes(&self) -> u128 {
        self.size.saturating_mul(self.count)
    }

    /// Size on the wire in request units: timers/counters for T/C, bytes otherwise.
    pub fn units(&self) -> u128 {
        if self.area.is_tc() {
            self.count
        } else {
            self.nbytes()
        }
    }

    /// Exclusive end, in request units.
    pub fn end(&self) -> u128 {
        self.start.saturating_add(self.units())
    }

    /// Identity of the data read (ignores the spelling). Tuple order as in Python, so planning
    /// is identical (`Area` orders like the Python area names; the kind is its name).
    pub fn key(&self) -> AddrKey {
        (
            self.area,
            self.db,
            self.start,
            self.kind.as_str(),
            self.size,
            self.count,
            self.dtype.as_str(),
            self.bit.map_or(-1, i64::from),
            self.dims.clone(),
            self.str_len.unwrap_or(0),
        )
    }
}

fn re(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("static regex"))
}

fn tc_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^(?P<tc>[TCZ])(?P<num>[0-9]+)(?P<suffix>\.[0-9]+)?(?P<dims>(?:\[\s*[0-9]+\s*\])*)$")
}
fn db_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^DB(?P<db>[0-9]+)(?P<sep>[,.]?)(?P<rest>.*)$")
}
fn area_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^(?P<area>PI|PE|PQ|PA|I|E|Q|A|M|F)(?P<rest>.*)$")
}
fn rest_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^(?P<type>[A-Z]*)(?P<off>[0-9]+)(?:\.(?P<suffix>[0-9]+))?(?P<dims>(?:\[\s*[0-9]+\s*\])*)$")
}
fn dim_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"\[\s*([0-9]+)\s*\]")
}

fn num(text: &str) -> u128 {
    text.parse().unwrap_or(u128::MAX)
}

/// S7 data type -> (kind, bytes per element). STRING's size depends on the length suffix.
fn s7_type(name: &str) -> Option<(Kind, u128)> {
    Some(match name {
        "X" | "DBX" => (Kind::Bit, 1),
        "B" | "BYTE" | "DBB" | "C" | "CHAR" => (Kind::Byte, 1),
        "W" | "WORD" | "DBW" | "I" | "INT" => (Kind::Word, 2),
        "D" | "DWORD" | "DBD" | "DI" | "DINT" => (Kind::Dword, 4),
        "REAL" => (Kind::Real, 4),
        "LREAL" => (Kind::Lreal, 8),
        "LINT" | "LWORD" => (Kind::Lword, 8),
        "STRING" => (Kind::String, 0),
        _ => return None,
    })
}

fn unsupported_type(name: &str) -> Option<&'static str> {
    Some(match name {
        "DT" => "DATE_AND_TIME",
        "DATE" => "DATE",
        "T" | "TIME" => "TIME",
        "TOD" => "TIME_OF_DAY",
        "S5TIME" => "S5TIME",
        "HEXSTRING" => "HEXSTRING",
        "WSTRING" => "WSTRING",
        "DTL" => "DTL",
        _ => return None,
    })
}

fn is_step7(name: &str) -> bool {
    matches!(name, "DBX" | "DBB" | "DBW" | "DBD")
}

fn bit_range(kind: Kind) -> Option<u128> {
    Some(match kind {
        Kind::Bit | Kind::Byte => 7,
        Kind::Word => 15,
        Kind::Dword => 31,
        Kind::Lword => 63,
        _ => return None,
    })
}

fn is_int(d: DType) -> bool {
    matches!(d, DType::Int16 | DType::Uint16 | DType::Int32 | DType::Uint32 | DType::Int64 | DType::Uint64)
}

/// Parse a Kepware S7 address for a tag of `dtype`. The error is Python's `ValueError` text.
pub fn parse_s7_address(address: &str, dtype: DType, p: &S7Params) -> Result<S7Address, String> {
    let bad = |reason: &str| format!("s7 address {} ({}): {reason}", py_repr_str(address), dtype.as_str());
    let text = address.trim();
    if text.is_empty() {
        return Err(bad("empty address"));
    }
    let up = text.to_uppercase();

    if let Some(m) = tc_re().captures(&up) {
        return parse_tc(text, &m, dtype, &bad);
    }
    if let Some(m) = db_re().captures(&up) {
        if !m["rest"].is_empty() {
            let db = num(&m["db"]);
            if !(1..=MAX_DB).contains(&db) {
                return Err(bad(&format!("DB number {db} out of range 1..{MAX_DB}")));
            }
            let Some(rest) = rest_re().captures(&m["rest"]) else {
                return Err(bad("not an S7 address (expected DB<n>,<type><byte>, e.g. DB1,REAL0, or DB<n>.DBD<byte>)"));
            };
            let tname = &rest["type"];
            if &m["sep"] != "," && !is_step7(tname) {
                let sep = &m["sep"];
                let t = if tname.is_empty() { "W" } else { tname };
                return Err(bad(&format!(
                    "after 'DB{db}{sep}' the Siemens form needs DBX/DBB/DBW/DBD, e.g. DB{db}.DBW0 (Kepware form: DB{db},{t}{})",
                    &rest["off"]
                )));
            }
            return parse_rest(text, Area::Db, db, &rest, dtype, p, &bad);
        }
    }
    if let Some(m) = area_re().captures(&up) {
        if !m["rest"].is_empty() {
            let area = &m["area"];
            if matches!(area, "PI" | "PE" | "PQ" | "PA") {
                return Err(bad(
                    "peripheral I/O (PI/PE/PQ/PA) is not supported by python-snap7; read the process image (I/Q) instead",
                ));
            }
            let Some(rest) = rest_re().captures(&m["rest"]) else {
                return Err(bad("not an S7 address (expected e.g. MD232, MW10, M10.3, IB0, DB1,REAL0)"));
            };
            if is_step7(&rest["type"]) {
                return Err(bad(&format!(
                    "{} is only valid in a data block, e.g. DB1,{}0",
                    &rest["type"], &rest["type"]
                )));
            }
            let a = match area {
                "I" | "E" => Area::I,
                "Q" | "A" => Area::Q,
                _ => Area::M,
            };
            return parse_rest(text, a, 0, &rest, dtype, p, &bad);
        }
    }
    Err(bad("not an S7 address (expected e.g. MD232, M10.3, DB100,D04, DB1.DBD0, C0, T0)"))
}

fn dims_of(text: &str, bad: &dyn Fn(&str) -> String) -> Result<Vec<u128>, String> {
    let dims: Vec<u128> = dim_re().captures_iter(text).map(|c| num(&c[1])).collect();
    if dims.len() > 2 {
        return Err(bad("at most 2 array dimensions ([rows][cols])"));
    }
    if dims.contains(&0) {
        return Err(bad("array dimension must be >= 1"));
    }
    Ok(dims)
}

fn parse_tc(text: &str, m: &Captures, dtype: DType, bad: &dyn Fn(&str) -> String) -> Result<S7Address, String> {
    let (area, what) = if &m["tc"] == "T" { (Area::T, "timer") } else { (Area::C, "counter") };
    let n = num(&m["num"]);
    if m.name("suffix").is_some() {
        return Err(bad(&format!("{what} addresses take no .suffix (T<n>, C<n>)")));
    }
    if !m["dims"].is_empty() {
        return Err(bad(&format!("arrays are not supported on timers and counters ({what})")));
    }
    if n > MAX_TC {
        return Err(bad(&format!("{what} number {n} out of range 0..{MAX_TC}")));
    }
    if area == Area::T && !matches!(dtype, DType::Uint32 | DType::Int32) {
        return Err(bad(
            "a timer reads as milliseconds (S5TIME scaled): use dtype uint32 or int32 (Kepware DWord/Long)",
        ));
    }
    if area == Area::C && !(is_int(dtype) && !matches!(dtype, DType::Int64 | DType::Uint64)) {
        return Err(bad("a counter reads as its BCD-decoded value: use dtype uint16 or int16 (Kepware Word/Short)"));
    }
    Ok(S7Address {
        text: text.to_string(),
        area,
        db: 0,
        start: n,
        kind: if area == Area::T { Kind::Timer } else { Kind::Counter },
        size: 2,
        dtype,
        count: 1,
        bit: None,
        dims: vec![],
        str_len: None,
    })
}

fn parse_rest(
    text: &str,
    area: Area,
    db: u128,
    m: &Captures,
    dtype: DType,
    p: &S7Params,
    bad: &dyn Fn(&str) -> String,
) -> Result<S7Address, String> {
    let tname = &m["type"];
    let offset = num(&m["off"]);
    let suffix = m.name("suffix").map(|s| s.as_str());
    let dims = dims_of(&m["dims"], bad)?;
    if let Some(what) = unsupported_type(tname) {
        return Err(bad(&format!("S7 data type {tname} ({what}) is not supported")));
    }
    let (mut kind, mut size);
    if tname.is_empty() {
        if suffix.is_none() {
            if dtype == DType::Bool {
                return Err(bad(&format!("a bool needs a bit number, e.g. {text}.0")));
            }
            return Err(bad("needs an S7 data type, e.g. MW10, or a bit number, e.g. M10.3"));
        }
        kind = Kind::Bit;
        size = 1;
    } else if let Some((k, s)) = s7_type(tname) {
        kind = k;
        size = s;
    } else {
        return Err(bad(&format!("unknown S7 data type {}", py_repr_str(tname))));
    }

    let mut bit: Option<u32> = None;
    let mut str_len: Option<u128> = None;
    if dtype == DType::Bool {
        if !dims.is_empty() {
            return Err(bad("arrays of bools are not supported (Kepware)"));
        }
        let Some(range) = bit_range(kind) else {
            return Err(bad(&format!("{tname} has no bit access; use X, B, W, D or LWORD with .bit")));
        };
        let Some(suffix) = suffix else {
            return Err(bad(&if kind != Kind::Bit {
                format!("a bool needs a bit number, e.g. {text}.0")
            } else {
                "a bool needs a bit number".to_string()
            }));
        };
        let b = num(suffix);
        if b > range {
            let hint = if kind == Kind::Bit {
                format!("; use byte {} bit {}", offset.saturating_add(b / 8), b % 8)
            } else {
                String::new()
            };
            return Err(bad(&format!("bit {b} out of range 0..{range}{hint}")));
        }
        bit = Some(b as u32);
    } else if dtype == DType::String {
        if !dims.is_empty() {
            return Err(bad("arrays of strings are not supported (Kepware)"));
        }
        if kind == Kind::String {
            let len = match suffix {
                Some(s) => num(s),
                None => {
                    if p.max_pdu >= 480 {
                        MAX_STRING
                    } else {
                        210
                    }
                }
            };
            if !(1..=MAX_STRING).contains(&len) {
                return Err(bad(&format!("string length {len} out of range 1..{MAX_STRING}")));
            }
            str_len = Some(len);
            size = len + 2;
        } else if kind == Kind::Byte {
            let Some(suffix) = suffix else {
                return Err(bad(&format!("a raw string needs a length, e.g. {text}.10")));
            };
            let len = num(suffix);
            if !(1..=MAX_RAW_STRING).contains(&len) {
                return Err(bad(&format!("string length {len} out of range 1..{MAX_RAW_STRING}")));
            }
            str_len = Some(len);
            kind = Kind::RawString;
            size = len;
        } else {
            return Err(bad("a string needs STRING<byte>.<len> (S7 string) or B<byte>.<len> (raw string)"));
        }
    } else {
        if kind == Kind::Bit {
            return Err(bad("a bit address (X or <byte>.<bit>) needs dtype bool"));
        }
        if kind == Kind::String {
            return Err(bad("STRING needs dtype string"));
        }
        if suffix.is_some() {
            return Err(bad("a .n suffix is a bit number (dtype bool) or a string length (dtype string)"));
        }
        let (allowed, reason): (&[DType], &str) = match kind {
            Kind::Byte => (
                &[DType::Int16, DType::Uint16, DType::Int32, DType::Uint32, DType::Int64, DType::Uint64],
                "B/BYTE/C/CHAR is an 8-bit value: use an integer dtype (Kepware Byte/Char)",
            ),
            Kind::Word => (&[DType::Int16, DType::Uint16], "W/WORD/I/INT is a 16-bit value: use dtype int16 or uint16"),
            Kind::Dword => (
                &[DType::Int32, DType::Uint32, DType::Float32],
                "D/DWORD/DI/DINT is a 32-bit value: use dtype int32, uint32 or float32",
            ),
            Kind::Real => (&[DType::Float32], "REAL holds a float: use dtype float32"),
            Kind::Lreal => (&[DType::Float64], "LREAL holds a 64-bit float: use dtype float64"),
            _ => (&[DType::Int64, DType::Uint64], "LINT/LWORD is a 64-bit value: use dtype int64 or uint64"),
        };
        if !allowed.contains(&dtype) {
            return Err(bad(reason));
        }
    }

    let count = dims.iter().fold(1u128, |a, d| a.saturating_mul(*d));
    let nbytes = size.saturating_mul(count);
    if offset.saturating_add(nbytes) > ADDRESS_SPACE {
        return Err(bad(&format!(
            "needs bytes {offset}..{}, past the end of the address space ({})",
            offset.saturating_add(nbytes) - 1,
            ADDRESS_SPACE - 1
        )));
    }
    Ok(S7Address { text: text.to_string(), area, db, start: offset, kind, size, dtype, count, bit, dims, str_len })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(a: &str, d: DType) -> S7Address {
        parse_s7_address(a, d, &S7Params::default()).unwrap_or_else(|e| panic!("{a}: {e}"))
    }

    fn err(a: &str, d: DType) -> String {
        parse_s7_address(a, d, &S7Params::default()).unwrap_err()
    }

    #[test]
    fn forms() {
        let a = ok("DB100,D04", DType::Float32);
        assert_eq!((a.area, a.db, a.start, a.kind, a.size), (Area::Db, 100, 4, Kind::Dword, 4));
        assert_eq!(ok("DB1.DBW6", DType::Uint16).start, 6);
        assert_eq!(ok("db1,x5.6", DType::Bool).bit, Some(6));
        assert_eq!(ok("M10.3", DType::Bool).kind, Kind::Bit);
        assert_eq!(ok("MD232", DType::Float32).area, Area::M);
        assert_eq!(ok("EW4", DType::Int16).area, Area::I);
        let s = ok("DB1,STRING0.20", DType::String);
        assert_eq!((s.size, s.str_len), (22, Some(20)));
        assert_eq!(ok("DB1,B10.8", DType::String).kind, Kind::RawString);
        assert_eq!(ok("Z5", DType::Uint16).area, Area::C);
        assert_eq!(ok("T3", DType::Uint32).kind, Kind::Timer);
        assert_eq!(ok("DB1,REAL0[3][2]", DType::Float32).count, 6);
    }

    #[test]
    fn rejected() {
        assert!(err("PIW0", DType::Uint16).contains("peripheral"));
        assert!(err("DB1.W0", DType::Uint16).contains("Siemens form needs DBX/DBB/DBW/DBD"));
        assert!(err("DBW0", DType::Uint16).contains("not an S7 address"));
        assert!(err("MW10", DType::Float32).contains("use dtype int16 or uint16"));
        assert!(err("M10.8", DType::Bool).contains("bit 8 out of range 0..7; use byte 11 bit 0"));
        assert!(err("T0", DType::Uint16).contains("timer reads as milliseconds"));
        assert!(err("DB1,DT0", DType::Uint16).contains("DATE_AND_TIME"));
        assert!(err("MB65535[2]", DType::Uint16).contains("past the end of the address space (65535)"));
        assert_eq!(err("", DType::Uint16), "s7 address '' (uint16): empty address");
    }

    #[test]
    fn default_string_length_follows_max_pdu() {
        let p = S7Params { max_pdu: 240, ..S7Params::default() };
        assert_eq!(parse_s7_address("DB1,STRING0", DType::String, &p).unwrap().str_len, Some(210));
        assert_eq!(ok("DB1,STRING0", DType::String).str_len, Some(254));
    }
}
