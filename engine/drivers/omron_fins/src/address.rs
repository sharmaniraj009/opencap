//! KEPServerEX Omron FINS address parser (CS1/CJ1/CJ2), docs/protocols/omron-fins.md §3 and §5.
//! Same grammar, limits and error categories as the simulator's own parser, which the
//! `omron_fins/addresses` conformance vectors were generated from.

use std::sync::LazyLock;

use regex::Regex;

use opencap_core::pyfmt::py_repr_str;

/// Why an address is refused (the vectors' `error`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// not the grammar
    Syntax,
    /// a number, bank, bit, string length or extent outside what the model has
    Range,
    /// a combination KEPServerEX does not allow
    Unsupported,
}

impl Reason {
    pub fn as_str(self) -> &'static str {
        match self {
            Reason::Syntax => "syntax",
            Reason::Range => "range",
            Reason::Unsupported => "unsupported",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AddressError {
    pub reason: Reason,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Area {
    Cio,
    W,
    H,
    A,
    D,
    /// EM bank 0..24 (CJ2 above 12)
    Em(u8),
    /// EM current bank
    E,
    T,
    C,
    Tk,
    Ir,
    Dr,
}

impl Area {
    pub fn name(self) -> String {
        match self {
            Area::Cio => "CIO".into(),
            Area::W => "W".into(),
            Area::H => "H".into(),
            Area::A => "A".into(),
            Area::D => "D".into(),
            Area::Em(b) => format!("E{b:02}:"),
            Area::E => "E".into(),
            Area::T => "T".into(),
            Area::C => "C".into(),
            Area::Tk => "TK".into(),
            Area::Ir => "IR".into(),
            Area::Dr => "DR".into(),
        }
    }

    /// (word code, bit/flag code) and the FINS address of element 0.
    fn codes(self) -> (Option<u8>, Option<u8>, u16) {
        match self {
            Area::Cio => (Some(0xB0), Some(0x30), 0),
            Area::W => (Some(0xB1), Some(0x31), 0),
            Area::H => (Some(0xB2), Some(0x32), 0),
            Area::A => (Some(0xB3), Some(0x33), 0),
            Area::D => (Some(0x82), Some(0x02), 0),
            Area::Em(b) if b <= 0x0F => (Some(0xA0 + b), Some(0x20 + b), 0),
            Area::Em(b) => (Some(0x60 + b - 0x10), Some(0xE0 + b - 0x10), 0),
            Area::E => (Some(0x98), Some(0x0A), 0),
            Area::T => (Some(0x89), Some(0x09), 0),
            Area::C => (Some(0x89), Some(0x09), 0x8000),
            Area::Tk => (None, Some(0x06), 0),
            Area::Ir => (Some(0xDC), None, 0x0100),
            Area::Dr => (Some(0xBC), None, 0x0200),
        }
    }

    /// Bytes per element read with the word code (index registers are 32-bit).
    pub fn word_width(self) -> u32 {
        if self == Area::Ir {
            4
        } else {
            2
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    Word,
    Bit,
    /// completion flag (TS, CS) or task flag (TK): one byte per flag
    Flag,
    String,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Word => "word",
            Kind::Bit => "bit",
            Kind::Flag => "flag",
            Kind::String => "string",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct FinsAddress {
    pub text: String,
    pub area: Area,
    /// element number in KEPServerEX terms (timer 3 = 3)
    pub word: u32,
    pub bit: Option<u8>,
    pub kind: Kind,
    pub dims: Vec<u32>,
    pub str_len: Option<u32>,
    /// H/L: two characters per word (high/low byte first); D/E: one per word (high/low byte)
    pub str_order: Option<char>,
    /// 'D' suffix: the Word/DWord is unsigned BCD, Short/Long signed BCD
    pub bcd: bool,
}

/// Words a value of `dtype` covers (bcd16/bcd32 are the KEPServerEX BCD/LBCD types).
pub fn words_of(dtype: &str) -> u32 {
    match dtype {
        "int32" | "uint32" | "float32" | "bcd32" => 2,
        "int64" | "uint64" | "float64" => 4,
        _ => 1,
    }
}

impl FinsAddress {
    pub fn rows(&self) -> u32 {
        self.dims.first().copied().unwrap_or(1)
    }

    pub fn cols(&self) -> u32 {
        self.dims.get(1).copied().unwrap_or(1)
    }

    /// KEPServerEX's default data type at this address.
    pub fn default_dtype(&self) -> &'static str {
        match (self.kind, self.area) {
            (Kind::Bit | Kind::Flag, _) => "bool",
            (Kind::String, _) => "string",
            (_, Area::T | Area::C) => "bcd16",
            (_, Area::Ir) => "uint32",
            _ => "uint16",
        }
    }

    /// (FINS memory area code, FINS word address) of the first element, as the vectors give it:
    /// a bit address uses the area's bit code.
    pub fn codes(&self) -> (u8, u16) {
        let (word, bit, offset) = self.area.codes();
        let code = if matches!(self.kind, Kind::Bit | Kind::Flag) { bit } else { word };
        (code.expect("checked when parsed"), self.word as u16 + offset)
    }

    /// The code and address a read uses: bits are read as part of their word (word code),
    /// flags with the flag code. Returns (code, address, element width in bytes).
    pub fn read_codes(&self) -> (u8, u16, u32) {
        let (word, bit, offset) = self.area.codes();
        match self.kind {
            Kind::Flag => (bit.expect("flag code"), self.word as u16 + offset, 1),
            _ => (word.expect("word code"), self.word as u16 + offset, self.area.word_width()),
        }
    }

    /// Items a read of this tag covers: words, or bits/flags (1), or IR elements.
    pub fn words(&self, dtype: Option<&str>) -> u32 {
        match self.kind {
            Kind::Bit | Kind::Flag => 1,
            Kind::String => {
                let n = self.str_len.unwrap_or(0);
                if matches!(self.str_order, Some('D' | 'E')) {
                    n
                } else {
                    n.div_ceil(2)
                }
            }
            Kind::Word if self.area == Area::Ir => 1,
            Kind::Word => words_of(dtype.unwrap_or(self.default_dtype())) * self.rows() * self.cols(),
        }
    }
}

static ADDR: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^(?P<area>CIO|DR|IR|TK|TS|CS|T|C|W|H|A|D|E)(?:(?P<bank>\d{1,2}):)?(?P<num>\d+)(?:\.(?P<sfx>\d+)(?P<order>[HLDE])?|(?P<bcd>D))?(?P<dims>(?:\s*\[\s*\d+\s*\])*)$",
    )
    .expect("regex")
});
static DIGITS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\d+").expect("regex"));

fn num(s: &str) -> u64 {
    s.chars().filter_map(|c| c.to_digit(10)).fold(0u64, |a, d| a.saturating_mul(10).saturating_add(u64::from(d)))
}

/// Element numbers KEPServerEX accepts for an area on a model (PTC manual CS1/CJ1/CJ2 tables),
/// as half-open (start, end) ranges.
fn limits(area: Area, model: &str) -> &'static [(u64, u64)] {
    let cj2 = model == "CJ2";
    match area {
        Area::Cio => &[(0, 6144)],
        Area::W => &[(0, 512)],
        Area::H => &[(0, 1536)],
        Area::D | Area::Em(_) | Area::E => &[(0, 32768)],
        Area::A if cj2 => &[(0, 1472), (10000, 11536)],
        Area::A => &[(0, 960)],
        Area::T | Area::C => &[(0, 4096)],
        Area::Tk if cj2 => &[(0, 128)],
        Area::Tk => &[(0, 32)],
        Area::Ir | Area::Dr => &[(0, 16)],
    }
}

/// Parse a KEPServerEX Omron FINS address for a tag of `dtype` (None = the address's default)
/// on `model` (CS1, CJ1, CJ2).
pub fn parse_fins_address(address: &str, dtype: Option<&str>, model: &str) -> Result<FinsAddress, AddressError> {
    let err = |reason: Reason, why: String| AddressError {
        reason,
        message: format!(
            "omron_fins address {}{}: {why}",
            py_repr_str(address),
            dtype.map(|d| format!(" ({d})")).unwrap_or_default()
        ),
    };
    let text = address.trim().to_uppercase();
    let Some(m) = ADDR.captures(&text) else {
        return Err(err(
            Reason::Syntax,
            "not a KEPServerEX Omron FINS address (e.g. D100, CIO10.05, E00:00100, T5)".into(),
        ));
    };
    let raw = &m["area"];
    let number = num(&m["num"]);
    let dims: Vec<u64> = DIGITS.find_iter(&m["dims"]).map(|d| num(d.as_str())).collect();
    let sfx = m.name("sfx").map(|s| num(s.as_str()));
    let order = m.name("order").map(|o| o.as_str().chars().next().expect("one char"));
    let bcd = m.name("bcd").is_some();
    let area = if let Some(bank) = m.name("bank") {
        if raw != "E" {
            return Err(err(Reason::Syntax, "only EM takes a bank (E<bank>:<word>)".into()));
        }
        let bank = num(bank.as_str());
        let max = if model == "CJ2" { 24 } else { 12 };
        if bank > max {
            return Err(err(Reason::Range, format!("no EM bank {bank} on {model} (banks 0..{max})")));
        }
        Area::Em(bank as u8)
    } else {
        match raw {
            "CIO" => Area::Cio,
            "DR" => Area::Dr,
            "IR" => Area::Ir,
            "TK" | "TS" => {
                if raw == "TK" {
                    Area::Tk
                } else {
                    Area::T
                }
            }
            "CS" => Area::C,
            "T" => Area::T,
            "C" => Area::C,
            "W" => Area::W,
            "H" => Area::H,
            "A" => Area::A,
            "D" => Area::D,
            _ => Area::E,
        }
    };
    if !dims.is_empty() && (dims.len() > 2 || dims.contains(&0)) {
        return Err(err(Reason::Unsupported, "arrays have 1 or 2 dimensions, each at least 1".into()));
    }
    let mk = |kind, bit: Option<u64>, str_len: Option<u64>, str_order: Option<char>, bcd| FinsAddress {
        text: address.trim().to_string(),
        area,
        word: number as u32,
        bit: bit.map(|b| b as u8),
        kind,
        dims: dims.iter().map(|&d| d as u32).collect(),
        str_len: str_len.map(|n| n as u32),
        str_order,
        bcd,
    };
    let check = |a: &FinsAddress, extent: u64| -> Result<(), AddressError> {
        let last = number + extent.max(1) - 1;
        if limits(area, model).iter().any(|&(lo, hi)| (lo..hi).contains(&number) && (lo..hi).contains(&last)) {
            Ok(())
        } else {
            Err(err(Reason::Range, format!("outside {} on {model}", a.area.name().trim_end_matches(':'))))
        }
    };
    if matches!(raw, "TS" | "CS" | "TK") {
        if sfx.is_some() || bcd || !dims.is_empty() {
            return Err(err(Reason::Unsupported, "a flag has no bit, suffix or array".into()));
        }
        let a = mk(Kind::Flag, None, None, None, false);
        check(&a, 1)?;
        return Ok(a);
    }
    if let Some(order) = order {
        let n = sfx.expect("the grammar puts the length before the order");
        let two_per_word = matches!(order, 'H' | 'L');
        if !dims.is_empty() || matches!(raw, "T" | "C" | "IR" | "DR") || (two_per_word && n % 2 == 1) {
            return Err(err(
                Reason::Unsupported,
                "a string is <word>.<length>H|L|D|E on a word area, H/L with an even length".into(),
            ));
        }
        if !(if two_per_word { (2..=512).contains(&n) } else { (1..=256).contains(&n) }) {
            return Err(err(Reason::Range, format!("string length {n} out of range")));
        }
        let a = mk(Kind::String, None, Some(n), Some(order), false);
        check(&a, u64::from(a.words(None)))?;
        return Ok(a);
    }
    if let Some(bit) = sfx {
        if !dims.is_empty() || matches!(raw, "T" | "C" | "IR" | "DR") {
            return Err(err(Reason::Unsupported, "no bit access on this area or on an array".into()));
        }
        if bit > 15 {
            return Err(err(Reason::Range, format!("bit {bit} above 15")));
        }
        let a = mk(Kind::Bit, Some(bit), None, None, false);
        check(&a, 1)?;
        return Ok(a);
    }
    if matches!(dtype, Some("bool" | "string")) {
        return Err(err(Reason::Unsupported, format!("{} needs a bit or string address", dtype.expect("matched"))));
    }
    if bcd && (matches!(raw, "T" | "C") || matches!(dtype, Some("bcd16" | "bcd32" | "float32"))) {
        return Err(err(Reason::Unsupported, "the 'D' (BCD) suffix is not allowed here".into()));
    }
    if raw == "IR" && matches!(dtype, Some("uint16" | "int16" | "bcd16")) {
        return Err(err(Reason::Unsupported, "index registers are 32-bit".into()));
    }
    let a = mk(Kind::Word, None, None, None, bcd);
    check(&a, u64::from(a.words(dtype)))?;
    Ok(a)
}
