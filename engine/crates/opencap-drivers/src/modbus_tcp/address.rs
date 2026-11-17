//! Kepware Modbus address parser (Python `parse_address`), same rules and messages.
//!
//! ```text
//! 0xxxxx  coil              FC01  bool
//! 1xxxxx  discrete input    FC02  bool
//! 3xxxxx  input register    FC04  16/32/64-bit, bool with .b, string
//! 4xxxxx  holding register  FC03  16/32/64-bit, bool with .b, string
//! 4xxxxx.b        bit b of the register (bool; zero_based_bit, modicon_bit_order)
//! 4xxxxx[n]       1-D array, 4xxxxx[r][c] 2-D array (not for .b or strings)
//! 4xxxxx.lllH     string of lll bytes (1..250), first char in the high byte
//! 4xxxxx.lllL     same, first char in the low byte
//! ```

use std::cmp::Ordering;
use std::sync::LazyLock;

use regex::Regex;

use opencap_core::pyfmt::py_repr_str;

use super::params::{ModbusParams, BITS_PER_REGISTER};

pub const LAST_ADDRESS: u32 = 0xFFFF;
pub const MAX_STRING_LEN: u32 = 250;
pub const DTYPES: [&str; 10] =
    ["int16", "uint16", "int32", "uint32", "float32", "int64", "uint64", "float64", "bool", "string"];

pub fn regs_per_dtype(dtype: &str) -> u32 {
    match dtype {
        "int16" | "uint16" => 1,
        "int32" | "uint32" | "float32" => 2,
        "int64" | "uint64" | "float64" => 4,
        _ => 0,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModbusAddress {
    /// address as written (stripped)
    pub text: String,
    /// coil | discrete | input | holding
    pub area: &'static str,
    /// 1, 2, 4, 3
    pub fc: u8,
    /// 1-based Modicon reference as written
    pub reference: u32,
    /// protocol start address on the wire
    pub address: u32,
    /// registers (FC03/04) or bits (FC01/02) on the wire
    pub count: u32,
    pub dtype: &'static str,
    /// bit inside the register, 0 = LSB (after zero_based_bit / modicon)
    pub bit: Option<u32>,
    /// array dimensions, empty = scalar
    pub dims: Vec<u32>,
    pub str_len: Option<u32>,
    /// "H" (high byte first) or "L"
    pub str_order: Option<&'static str>,
}

/// Identity of the data read (ignores the spelling): equal keys are read and decoded once.
/// Ordered like Python's key tuple `(fc, address, count, dtype, bit or -1, dims, str_len or 0, str_order or "")`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AddrKey {
    pub fc: u8,
    pub address: u32,
    pub count: u32,
    pub dtype: &'static str,
    pub bit: i64,
    pub dims: Vec<u32>,
    pub str_len: u32,
    pub str_order: &'static str,
}

impl Ord for AddrKey {
    fn cmp(&self, o: &Self) -> Ordering {
        (self.fc, self.address, self.count, self.dtype, self.bit, &self.dims, self.str_len, self.str_order).cmp(&(
            o.fc,
            o.address,
            o.count,
            o.dtype,
            o.bit,
            &o.dims,
            o.str_len,
            o.str_order,
        ))
    }
}

impl PartialOrd for AddrKey {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

impl ModbusAddress {
    pub fn is_bits(&self) -> bool {
        self.fc == 1 || self.fc == 2
    }

    /// Exclusive end address.
    pub fn end(&self) -> u32 {
        self.address + self.count
    }

    pub fn key(&self) -> AddrKey {
        AddrKey {
            fc: self.fc,
            address: self.address,
            count: self.count,
            dtype: self.dtype,
            bit: self.bit.map(i64::from).unwrap_or(-1),
            dims: self.dims.clone(),
            str_len: self.str_len.unwrap_or(0),
            str_order: self.str_order.unwrap_or(""),
        }
    }
}

static ADDR_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)^(?P<digits>[0-9]+)(?:\.(?P<suffix>[0-9]+)\s*(?P<order>[HL])?)?(?P<dims>(?:\s*\[\s*[0-9]+\s*\])*)$",
    )
    .expect("valid regex")
});
static DIM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\[\s*([0-9]+)\s*\]").expect("valid regex"));

fn canonical_dtype(dtype: &str) -> Option<&'static str> {
    DTYPES.iter().copied().find(|d| *d == dtype)
}

/// Parse a Kepware Modbus address for a tag of `dtype`. Errors carry a clear reason.
pub fn parse_address(address: &str, dtype: &str, p: &ModbusParams) -> Result<ModbusAddress, String> {
    let bad = |reason: String| format!("modbus address {} ({dtype}): {reason}", py_repr_str(address));
    let text = address.trim();
    if text.is_empty() {
        return Err(bad("empty address".into()));
    }
    let Some(m) = ADDR_RE.captures(text) else {
        return Err(bad(
            "not a Modbus address (expected 0xxxxx, 1xxxxx, 3xxxxx or 4xxxxx, optionally with .bit, .lllH/.lllL or [n])"
                .into(),
        ));
    };
    let digits = &m["digits"];
    if digits.len() != 5 && digits.len() != 6 {
        return Err(bad(format!("expected 5 or 6 digits (area digit + reference), got {}", digits.len())));
    }
    let (area, fc) = match &digits[..1] {
        "0" => ("coil", 1u8),
        "1" => ("discrete", 2),
        "3" => ("input", 4),
        "4" => ("holding", 3),
        other => {
            return Err(bad(format!(
                "unknown area prefix {} (0 = coil, 1 = discrete input, 3 = input register, 4 = holding register)",
                py_repr_str(other)
            )));
        }
    };
    let Some(dtype_c) = canonical_dtype(dtype) else {
        return Err(bad(format!("unknown dtype {}", py_repr_str(dtype))));
    };

    let reference: u32 = digits[1..].parse().expect("ascii digits");
    let max_ref = if digits.len() == 5 { 9999 } else { LAST_ADDRESS + 1 };
    if reference > max_ref {
        return Err(bad(format!("reference {reference} out of range 1..{max_ref}")));
    }
    if reference == 0 && p.zero_based {
        return Err(bad("reference 0 is not valid with zero_based addressing (references start at 1)".into()));
    }

    let dims_text = m.name("dims").map(|d| d.as_str()).unwrap_or("");
    let mut dims: Vec<u32> = Vec::new();
    for c in DIM_RE.captures_iter(dims_text) {
        // absurdly large dimensions are reported as "past the last address" below
        dims.push(c[1].parse::<u64>().unwrap_or(u64::MAX).min(u32::MAX as u64) as u32);
    }
    if dims.len() > 2 {
        return Err(bad("at most 2 array dimensions ([rows][cols])".into()));
    }
    if dims.iter().any(|&d| d < 1) {
        return Err(bad("array dimension must be >= 1".into()));
    }

    let is_bits = fc == 1 || fc == 2;
    let suffix = m.name("suffix").map(|s| s.as_str());
    let order = m.name("order").map(|s| s.as_str());
    let mut bit = None;
    let mut str_len = None;
    let mut str_order = None;
    let count: u64;

    if let Some(order) = order {
        if is_bits {
            return Err(bad("string addresses are only valid on registers (3xxxxx/4xxxxx)".into()));
        }
        if dtype_c != "string" {
            return Err(bad("a .lllH/.lllL string address needs dtype string".into()));
        }
        if !dims.is_empty() {
            return Err(bad("arrays of strings are not supported".into()));
        }
        let len: u64 = suffix.expect("order implies suffix").parse().unwrap_or(u64::MAX);
        if !(1..=MAX_STRING_LEN as u64).contains(&len) {
            return Err(bad(format!("string length {len} out of range 1..{MAX_STRING_LEN}")));
        }
        str_len = Some(len as u32);
        str_order = Some(if order.eq_ignore_ascii_case("h") { "H" } else { "L" });
        count = len.div_ceil(2);
    } else if let Some(suffix) = suffix {
        if dtype_c == "string" {
            return Err(bad(format!("string address needs an H or L byte-order suffix, e.g. {digits}.{suffix}H")));
        }
        if is_bits {
            return Err(bad("a .bit suffix is only valid on registers (3xxxxx/4xxxxx)".into()));
        }
        if dtype_c != "bool" {
            return Err(bad("a .bit address must have dtype bool".into()));
        }
        if !dims.is_empty() {
            return Err(bad("arrays are not supported on bit addresses".into()));
        }
        let written: i64 = suffix.parse::<i64>().unwrap_or(i64::MAX);
        let b0 = if p.zero_based_bit { written } else { written - 1 };
        if !(0..BITS_PER_REGISTER as i64).contains(&b0) {
            let rng = if p.zero_based_bit { "0..15 (zero_based_bit on)" } else { "1..16 (zero_based_bit off)" };
            return Err(bad(format!("bit .{written} out of range {rng}")));
        }
        let b0 = b0 as u32;
        bit = Some(if p.modicon_bit_order { BITS_PER_REGISTER - 1 - b0 } else { b0 });
        count = 1;
    } else {
        if dtype_c == "string" {
            return Err(bad(format!("string needs a length and byte order, e.g. {digits}.020H")));
        }
        if is_bits && dtype_c != "bool" {
            return Err(bad(format!("{area} addresses are bool only")));
        }
        if !is_bits && dtype_c == "bool" {
            return Err(bad(format!("bool on a register needs a .bit suffix, e.g. {digits}.0")));
        }
        let elements: u64 = dims.iter().map(|&d| d as u64).product();
        count = elements * if is_bits { 1 } else { regs_per_dtype(dtype_c) as u64 };
    }

    let proto: u64 = if p.zero_based { reference as u64 - 1 } else { reference as u64 };
    if proto + count - 1 > LAST_ADDRESS as u64 {
        let unit = if is_bits { "bits" } else { "registers" };
        return Err(bad(format!(
            "needs {count} {unit} from protocol address {proto}, past the last address {LAST_ADDRESS}"
        )));
    }
    Ok(ModbusAddress {
        text: text.into(),
        area,
        fc,
        reference,
        address: proto as u32,
        count: count as u32,
        dtype: dtype_c,
        bit,
        dims,
        str_len,
        str_order,
    })
}
