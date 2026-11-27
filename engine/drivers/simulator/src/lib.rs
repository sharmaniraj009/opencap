//! Simulator driver: computed values for demos, the UI, desktop tests and benchmarks (#124,
//! the Rust port of #12; docs/driver_params.md, "simulator", decision D-071).
//!
//! No socket and no protocol library. `connect()` succeeds unless `params.fail_connect`, and
//! `read()` computes each tag's value from its address and the injected clock, so a run is
//! deterministic for a given clock, `params.epoch` and seed. Read-only by construction: the
//! `Driver` trait has no write and memory registers only return their configured value.
//!
//! Time is whole milliseconds since `params.epoch` (default the Unix epoch), floor division and
//! Euclidean modulo. The formulas and the address grammar are pinned by the shared vectors in
//! `tests/conformance/simulator/`, run by `tests/vectors.rs`.

use std::any::Any;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::f64::consts::PI;
use std::fmt;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use regex::Regex;
use serde_json::{Map, Value as Json};

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Block, BlockRef, Driver, DriverError, Plan, ReadResult};
use opencap_core::pyfmt::{py_float_repr, py_repr_str};
use opencap_core::value::{iso_utc, now_utc, Quality, TagValue, Value};

const GOLDEN_GAMMA: u64 = 0x9E37_79B9_7F4A_7C15;
const RAMP_EPSILON: f64 = 1e-9;
const MAX_REGISTER: i128 = 65535;
const MAX_PERIOD_MS: i128 = 86_400_000 * 366;
const MASK64: i128 = (1 << 64) - 1;
const F32_MAX: f64 = 3.402_823_466_385_288_6e38;

static CALL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)^\s*([A-Za-z]+)\s*\((.*)\)\s*$").expect("regex"));
static REGISTER_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*[Kk]([0-9]+)\s*$").expect("regex"));
static NUMBER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$").expect("regex"));
static INT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[+-]?[0-9]+$").expect("regex"));

pub type SimClock = Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>;

// --- numbers and literals -------------------------------------------------------------------

/// A number as written: integer literals stay exact (they decide the RAMP arithmetic).
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Num {
    Int(i128),
    Float(f64),
}

impl Num {
    pub fn as_f64(self) -> f64 {
        match self {
            Num::Int(i) => i as f64,
            Num::Float(f) => f,
        }
    }
}

impl fmt::Display for Num {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Num::Int(i) => write!(f, "{i}"),
            Num::Float(x) => f.write_str(&py_float_repr(*x)),
        }
    }
}

/// `a < b` the way Python compares an int with a float.
fn lt(a: Num, b: Num) -> bool {
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => x < y,
        _ => a.as_f64() < b.as_f64(),
    }
}

fn is_zero(n: Num) -> bool {
    n.as_f64() == 0.0
}

/// A CONST argument or a memory register's value.
#[derive(Debug, Clone, PartialEq)]
pub enum Lit {
    Bool(bool),
    Num(Num),
    Str(String),
}

impl fmt::Display for Lit {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Lit::Bool(b) => f.write_str(if *b { "True" } else { "False" }),
            Lit::Num(n) => write!(f, "{n}"),
            Lit::Str(s) => f.write_str(&py_repr_str(s)),
        }
    }
}

fn number(text: &str, what: &str) -> Result<Num, String> {
    let t = text.trim();
    if !NUMBER_RE.is_match(t) {
        return Err(format!("{what} {} is not a number", py_repr_str(t)));
    }
    if INT_RE.is_match(t) {
        return t.parse::<i128>().map(Num::Int).map_err(|_| format!("{what} {} is out of range", py_repr_str(t)));
    }
    let v: f64 = t.parse().map_err(|_| format!("{what} {} is not a number", py_repr_str(t)))?;
    if !v.is_finite() {
        return Err(format!("{what} {} is not finite", py_repr_str(t)));
    }
    Ok(Num::Float(v))
}

fn literal(text: &str) -> Result<Lit, String> {
    let t = text.trim();
    if t.len() >= 2 && t.starts_with('"') && t.ends_with('"') {
        let body: Vec<char> = t[1..t.len() - 1].chars().collect();
        let (mut out, mut i) = (String::new(), 0);
        while i < body.len() {
            match body[i] {
                '\\' => {
                    if i + 1 >= body.len() || !matches!(body[i + 1], '"' | '\\') {
                        return Err(format!("CONST string {t}: only \\\" and \\\\ escapes are allowed"));
                    }
                    out.push(body[i + 1]);
                    i += 2;
                }
                '"' => return Err(format!("CONST string {t}: escape an inner quote as \\\"")),
                c => {
                    out.push(c);
                    i += 1;
                }
            }
        }
        return Ok(Lit::Str(out));
    }
    match t.to_ascii_lowercase().as_str() {
        "true" => Ok(Lit::Bool(true)),
        "false" => Ok(Lit::Bool(false)),
        _ => number(t, "CONST value").map(Lit::Num),
    }
}

// --- dtypes ---------------------------------------------------------------------------------

fn int_range(d: DType) -> Option<(i128, i128)> {
    Some(match d {
        DType::Int16 => (i16::MIN.into(), i16::MAX.into()),
        DType::Uint16 => (0, u16::MAX.into()),
        DType::Int32 => (i32::MIN.into(), i32::MAX.into()),
        DType::Uint32 => (0, u32::MAX.into()),
        DType::Int64 => (i64::MIN.into(), i64::MAX.into()),
        DType::Uint64 => (0, u64::MAX.into()),
        _ => return None,
    })
}

fn is_numeric(d: DType) -> bool {
    int_range(d).is_some() || matches!(d, DType::Float32 | DType::Float64)
}

fn check_int_fits(value: Num, dtype: DType, what: &str) -> Result<(), String> {
    let (lo, hi) = int_range(dtype).expect("integer dtype");
    let whole = match value {
        Num::Int(_) => true,
        Num::Float(f) => f.is_finite() && f.fract() == 0.0,
    };
    if !whole {
        return Err(format!("{what} {value} is not a whole number (tag is {})", dtype.as_str()));
    }
    let n = match value {
        Num::Int(i) => i,
        Num::Float(f) => f as i128,
    };
    if !(lo..=hi).contains(&n) {
        return Err(format!("{what} {value} is out of range for {} ({lo}..{hi})", dtype.as_str()));
    }
    Ok(())
}

/// A CONST or memory value must be representable in the tag's dtype.
fn check_value_for_dtype(value: &Lit, dtype: DType, what: &str) -> Result<(), String> {
    let d = dtype.as_str();
    match dtype {
        DType::String => {
            if !matches!(value, Lit::Str(_)) {
                return Err(format!("{what} {value} is not a string (tag is string)"));
            }
        }
        DType::Bool => {
            let ok = match value {
                Lit::Bool(_) => true,
                Lit::Num(n) => n.as_f64() == 0.0 || n.as_f64() == 1.0,
                Lit::Str(_) => false,
            };
            if !ok {
                return Err(format!("{what} {value} is not true/false or 0/1 (tag is bool)"));
            }
        }
        _ => {
            let Lit::Num(n) = value else {
                return Err(format!("{what} {value} is not a number (tag is {d})"));
            };
            if int_range(dtype).is_some() {
                check_int_fits(*n, dtype, what)?;
            } else if dtype == DType::Float32 && n.as_f64().abs() > F32_MAX {
                return Err(format!("{what} {value} is out of range for float32"));
            }
        }
    }
    Ok(())
}

// --- params ---------------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct SimParams {
    /// Time zero of every waveform, ms since the Unix epoch.
    pub epoch_ms: i64,
    /// Register number -> initial value.
    pub memory: BTreeMap<u32, Lit>,
    pub fail_connect: bool,
    /// 0 = never.
    pub fail_every_n_reads: u64,
    pub latency_ms: u64,
    pub bad_decode_tags: HashSet<String>,
}

fn flag(p: &Map<String, Json>, key: &str) -> Result<bool, String> {
    match p.get(key) {
        None | Some(Json::Null) => Ok(false),
        Some(Json::Bool(b)) => Ok(*b),
        Some(Json::Number(n)) if n.as_i64().is_some_and(|i| i == 0 || i == 1) => Ok(n.as_i64() == Some(1)),
        Some(v) => Err(format!("simulator param {key}={}: expected true or false", py_json(v))),
    }
}

fn count(p: &Map<String, Json>, key: &str) -> Result<u64, String> {
    match p.get(key) {
        None | Some(Json::Null) => Ok(0),
        Some(Json::Number(n)) if n.is_u64() => Ok(n.as_u64().unwrap_or(0)),
        Some(v) => Err(format!("simulator param {key}={}: expected an integer >= 0", py_json(v))),
    }
}

/// A JSON value as Python's `repr` writes it (for the messages).
fn py_json(v: &Json) -> String {
    match v {
        Json::Null => "None".into(),
        Json::Bool(true) => "True".into(),
        Json::Bool(false) => "False".into(),
        Json::String(s) => py_repr_str(s),
        other => other.to_string(),
    }
}

fn parse_epoch(v: Option<&Json>) -> Result<i64, String> {
    let bad = |v: &Json| {
        format!(
            "simulator param epoch={}: expected ms since 1970 or an ISO 8601 time with a zone, e.g. \"2026-01-01T00:00:00Z\"",
            py_json(v)
        )
    };
    match v {
        None | Some(Json::Null) => Ok(0),
        Some(j @ Json::Bool(_)) => Err(bad(j)),
        Some(j @ Json::Number(n)) => n.as_i64().ok_or_else(|| bad(j)),
        Some(j @ Json::String(s)) => {
            DateTime::parse_from_rfc3339(&s.trim().replace('z', "Z")).map(|t| t.timestamp_millis()).map_err(|_| bad(j))
        }
        Some(j) => Err(bad(j)),
    }
}

fn parse_memory(v: Option<&Json>) -> Result<BTreeMap<u32, Lit>, String> {
    let Some(v) = v.filter(|v| !v.is_null()) else { return Ok(BTreeMap::new()) };
    let Json::Object(map) = v else {
        return Err(format!("simulator param memory={}: expected a mapping like {{\"K0\": 12.5}}", py_json(v)));
    };
    let mut out = BTreeMap::new();
    for (key, val) in map {
        let n = REGISTER_RE
            .captures(key)
            .and_then(|c| c[1].parse::<i128>().ok())
            .filter(|n| *n <= MAX_REGISTER)
            .ok_or_else(|| {
                format!("simulator param memory: key {} is not a register K0..K{MAX_REGISTER}", py_repr_str(key))
            })? as u32;
        let lit = match val {
            Json::Bool(b) => Lit::Bool(*b),
            Json::String(s) => Lit::Str(s.clone()),
            Json::Number(x) => match (x.as_i64(), x.as_u64()) {
                (Some(i), _) => Lit::Num(Num::Int(i.into())),
                (None, Some(u)) => Lit::Num(Num::Int(u.into())),
                _ => Lit::Num(Num::Float(x.as_f64().unwrap_or(f64::NAN))),
            },
            other => {
                return Err(format!(
                    "simulator param memory.{key}={}: expected a number, true/false or a string",
                    py_json(other)
                ))
            }
        };
        if out.insert(n, lit).is_some() {
            return Err(format!("simulator param memory: register K{n} is given twice"));
        }
    }
    Ok(out)
}

impl SimParams {
    /// Validate `Device.params`. The error names the offending key.
    pub fn from_map(p: &Map<String, Json>) -> Result<Self, String> {
        let names = match p.get("bad_decode_tags") {
            None | Some(Json::Null) => HashSet::new(),
            Some(Json::Array(a)) if a.iter().all(Json::is_string) => {
                a.iter().filter_map(|s| s.as_str().map(str::to_string)).collect()
            }
            Some(v) => {
                return Err(format!("simulator param bad_decode_tags={}: expected a list of tag names", py_json(v)))
            }
        };
        Ok(Self {
            epoch_ms: parse_epoch(p.get("epoch"))?,
            memory: parse_memory(p.get("memory"))?,
            fail_connect: flag(p, "fail_connect")?,
            fail_every_n_reads: count(p, "fail_every_n_reads")?,
            latency_ms: count(p, "latency_ms")?,
            bad_decode_tags: names,
        })
    }
}

// --- addresses ------------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Func {
    Ramp,
    Sine,
    Random,
    Square,
    Const,
    K,
    Time,
    Counter,
}

/// A parsed simulator address; `args` are the waveform's arguments with defaults filled in
/// (period, low, high, then step / phase / seed).
#[derive(Debug, Clone, PartialEq)]
pub struct SimAddress {
    pub func: Func,
    pub args: Vec<Num>,
    /// CONST literal, or the memory register's value.
    pub value: Option<Lit>,
    pub register: Option<u32>,
}

impl SimAddress {
    fn plain(func: Func) -> Self {
        Self { func, args: Vec::new(), value: None, register: None }
    }
}

fn arity(name: &str) -> Option<(Func, usize, usize)> {
    Some(match name {
        "RAMP" => (Func::Ramp, 3, 4),
        "SINE" => (Func::Sine, 3, 4),
        "RANDOM" => (Func::Random, 3, 4),
        "SQUARE" => (Func::Square, 3, 3),
        _ => return None,
    })
}

/// Parse a simulator address for a tag of `dtype`. Pure; the error is the reason.
pub fn parse_address(address: &str, dtype: DType, params: &SimParams) -> Result<SimAddress, String> {
    let text = address.trim();
    let upper = text.to_ascii_uppercase();
    if upper == "TIME" {
        if dtype != DType::String {
            return Err(format!("TIME needs a string tag (tag is {})", dtype.as_str()));
        }
        return Ok(SimAddress::plain(Func::Time));
    }
    if upper == "COUNTER" {
        if !is_numeric(dtype) {
            return Err(format!("COUNTER needs a numeric tag (tag is {})", dtype.as_str()));
        }
        return Ok(SimAddress::plain(Func::Counter));
    }
    if let Some(c) = REGISTER_RE.captures(text) {
        let n: i128 = c[1].parse().unwrap_or(i128::MAX);
        if n > MAX_REGISTER {
            return Err(format!("register K{n} is out of range (K0..K{MAX_REGISTER})"));
        }
        let n = n as u32;
        let value = match params.memory.get(&n) {
            Some(v) => {
                check_value_for_dtype(v, dtype, &format!("memory K{n} ="))?;
                v.clone()
            }
            None if dtype == DType::String => Lit::Str(String::new()),
            None => Lit::Num(Num::Int(0)),
        };
        return Ok(SimAddress { func: Func::K, args: Vec::new(), value: Some(value), register: Some(n) });
    }
    let Some(c) = CALL_RE.captures(text) else {
        return Err(format!(
            "unknown simulator address {}; expected one of RAMP(...), SINE(...), RANDOM(...), SQUARE(...), CONST(...), K<n>, TIME, COUNTER",
            py_repr_str(address)
        ));
    };
    let (name, inner) = (c[1].to_ascii_uppercase(), &c[2]);
    if name == "CONST" {
        let v = literal(inner)?;
        check_value_for_dtype(&v, dtype, "CONST value")?;
        return Ok(SimAddress { func: Func::Const, args: Vec::new(), value: Some(v), register: None });
    }
    let Some((func, lo_n, hi_n)) = arity(&name) else {
        return Err(format!("unknown simulator function {}", py_repr_str(&c[1])));
    };
    if dtype == DType::String {
        return Err(format!("{name} needs a numeric or bool tag (tag is string)"));
    }
    let parts: Vec<&str> = if inner.trim().is_empty() { Vec::new() } else { inner.split(',').collect() };
    if !(lo_n..=hi_n).contains(&parts.len()) {
        let want = if lo_n == hi_n { lo_n.to_string() } else { format!("{lo_n} or {hi_n}") };
        return Err(format!("{name} takes {want} arguments, got {}", parts.len()));
    }
    let period = number(parts[0], &format!("{name} period_ms"))?;
    let Num::Int(p) = period else {
        return Err(format!(
            "{name} period_ms {} must be a whole number 1..{MAX_PERIOD_MS}",
            py_repr_str(parts[0].trim())
        ));
    };
    if !(1..=MAX_PERIOD_MS).contains(&p) {
        return Err(format!(
            "{name} period_ms {} must be a whole number 1..{MAX_PERIOD_MS}",
            py_repr_str(parts[0].trim())
        ));
    }
    let low = number(parts[1], &format!("{name} low"))?;
    let high = number(parts[2], &format!("{name} high"))?;
    if lt(high, low) {
        return Err(format!("{name} low {low} is greater than high {high}"));
    }
    let mut args = vec![period, low, high];
    match func {
        Func::Ramp => {
            let step = if parts.len() > 3 { number(parts[3], "RAMP step")? } else { Num::Int(1) };
            if is_zero(step) {
                return Err("RAMP step must not be 0".into());
            }
            if int_range(dtype).is_some() {
                check_int_fits(step, dtype, "RAMP step")?;
            }
            args.push(step);
        }
        Func::Sine => args.push(if parts.len() > 3 { number(parts[3], "SINE phase_deg")? } else { Num::Int(0) }),
        Func::Random => {
            let seed = if parts.len() > 3 { number(parts[3], "RANDOM seed")? } else { Num::Int(0) };
            match seed {
                Num::Int(s) if (0..=MASK64).contains(&s) => {}
                _ => {
                    return Err(format!(
                        "RANDOM seed {} must be a whole number 0..2^64-1",
                        py_repr_str(parts.get(3).map_or("", |s| s.trim()))
                    ))
                }
            }
            args.push(seed);
        }
        _ => {}
    }
    if int_range(dtype).is_some() {
        check_int_fits(low, dtype, &format!("{name} low"))?;
        check_int_fits(high, dtype, &format!("{name} high"))?;
    } else if dtype == DType::Bool {
        let bit = |n: Num| n.as_f64() == 0.0 || n.as_f64() == 1.0;
        if !bit(low) || !bit(high) {
            return Err(format!("{name} on a bool tag needs low and high of 0 or 1 (got {low}, {high})"));
        }
    } else if dtype == DType::Float32 {
        check_value_for_dtype(&Lit::Num(low), dtype, &format!("{name} low"))?;
        check_value_for_dtype(&Lit::Num(high), dtype, &format!("{name} high"))?;
    }
    Ok(SimAddress { func, args, value: None, register: None })
}

// --- value functions (pure) -----------------------------------------------------------------

/// The n-th output (n = 0, 1, ...) of SplitMix64 seeded with `seed`, computed directly.
pub fn splitmix64(seed: u64, n: u64) -> u64 {
    let mut z = seed.wrapping_add(n.wrapping_add(1).wrapping_mul(GOLDEN_GAMMA));
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

fn int_value(n: i128) -> Value {
    i64::try_from(n).map(Value::Int).unwrap_or_else(|_| Value::UInt(n as u64))
}

/// A computed number in the tag's dtype: integers round half away from zero, float32 rounds to
/// the nearest float32, bool is `round(value) != 0`.
fn coerce(raw: Num, dtype: DType) -> Value {
    let as_int = |raw: Num| match raw {
        Num::Int(i) => i,
        Num::Float(f) => f.round() as i128,
    };
    match dtype {
        DType::Bool => Value::Bool(as_int(raw) != 0),
        DType::Float32 => Value::Float(f64::from(raw.as_f64() as f32)),
        DType::Float64 => Value::Float(raw.as_f64()),
        _ => int_value(as_int(raw)),
    }
}

fn wrap(n: u64, dtype: DType) -> i128 {
    let (lo, hi) = int_range(dtype).expect("integer dtype");
    (i128::from(n) - lo).rem_euclid(hi - lo + 1) + lo
}

/// Value of a RAMP/SINE/RANDOM/SQUARE address `elapsed_ms` after the epoch (may be negative).
fn waveform(addr: &SimAddress, dtype: DType, elapsed_ms: i128) -> Value {
    let Num::Int(period) = addr.args[0] else { unreachable!("period is an integer") };
    let (low, high) = (addr.args[1], addr.args[2]);
    let n = elapsed_ms.div_euclid(period);
    match addr.func {
        Func::Ramp => {
            let step = addr.args[3];
            let steps = match (low, high, step) {
                (Num::Int(l), Num::Int(h), Num::Int(s)) => (h - l).div_euclid(s.abs()) + 1,
                _ => ((high.as_f64() - low.as_f64()) / step.as_f64().abs() + RAMP_EPSILON).floor() as i128 + 1,
            };
            let k = n.rem_euclid(steps);
            let up = !lt(step, Num::Int(0));
            let raw = match (low, high, step) {
                (Num::Int(l), Num::Int(h), Num::Int(s)) => Num::Int(if up { l + s * k } else { h + s * k }),
                _ => {
                    let base = if up { low.as_f64() } else { high.as_f64() };
                    Num::Float(base + step.as_f64() * k as f64)
                }
            };
            coerce(raw, dtype)
        }
        Func::Sine => {
            let frac = elapsed_ms.rem_euclid(period) as f64 / period as f64;
            let (lo, hi) = (low.as_f64(), high.as_f64());
            let (mid, amp) = ((lo + hi) / 2.0, (hi - lo) / 2.0);
            let phase = addr.args[3].as_f64() * (PI / 180.0);
            let raw = mid + amp * (2.0 * PI * frac + phase).sin();
            coerce(Num::Float(raw.clamp(lo, hi)), dtype)
        }
        Func::Random => {
            let Num::Int(seed) = addr.args[3] else { unreachable!("seed is an integer") };
            let u = (splitmix64(seed as u64, n as u64) >> 11) as f64 / (1u64 << 53) as f64;
            match (low, high) {
                (Num::Int(l), Num::Int(h)) if int_range(dtype).is_some() || dtype == DType::Bool => {
                    let span = h - l;
                    coerce(Num::Int(l + ((u * (span + 1) as f64).floor() as i128).min(span)), dtype)
                }
                _ => coerce(Num::Float(low.as_f64() + u * (high.as_f64() - low.as_f64())), dtype),
            }
        }
        _ => {
            let first_half = elapsed_ms.rem_euclid(period) * 2 < period;
            coerce(if first_half { high } else { low }, dtype)
        }
    }
}

fn static_value(addr: &SimAddress, dtype: DType) -> Value {
    match (addr.value.as_ref(), dtype) {
        (Some(Lit::Str(s)), _) => Value::Str(s.clone()),
        (Some(Lit::Bool(b)), DType::Bool) => Value::Bool(*b),
        (Some(Lit::Num(n)), DType::Bool) => Value::Bool(n.as_f64() != 0.0),
        (Some(Lit::Num(n)), d) => coerce(*n, d),
        (Some(Lit::Bool(b)), d) => coerce(Num::Int(i128::from(*b)), d),
        (None, _) => Value::Int(0),
    }
}

/// The value of `addr` at `now`. `reads` is the 1-based read count (COUNTER only).
pub fn compute(addr: &SimAddress, dtype: DType, now: DateTime<Utc>, epoch_ms: i64, reads: u64) -> Value {
    match addr.func {
        Func::Const | Func::K => static_value(addr, dtype),
        Func::Time => Value::Str(iso_utc(now)),
        Func::Counter if int_range(dtype).is_some() => int_value(wrap(reads, dtype)),
        Func::Counter => coerce(Num::Int(i128::from(reads)), dtype),
        _ => waveform(addr, dtype, i128::from(now.timestamp_millis()) - i128::from(epoch_ms)),
    }
}

// --- plan and driver ------------------------------------------------------------------------

/// All simulator tags of one rate group: one "request", no wire.
#[derive(Debug)]
pub struct SimBlock {
    tags: Vec<String>,
    items: HashMap<String, (SimAddress, DType)>,
}

impl Block for SimBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        format!("SIM {} tags", self.tags.len())
    }

    fn wire_requests(&self) -> usize {
        0
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

/// Pure: parse every enabled tag; all valid ones go into one block.
pub fn plan_tags(tags: &[&Tag], params: &SimParams) -> Plan {
    let mut items = HashMap::new();
    let mut invalid = BTreeMap::new();
    for tag in tags.iter().filter(|t| t.enabled) {
        match parse_address(&tag.address, tag.dtype, params) {
            Ok(a) => {
                items.insert(tag.name.clone(), (a, tag.dtype));
            }
            Err(e) => {
                invalid.insert(tag.name.clone(), e);
            }
        }
    }
    let mut names: Vec<String> = items.keys().cloned().collect();
    names.sort();
    let blocks: Vec<BlockRef> =
        if names.is_empty() { Vec::new() } else { vec![Arc::new(SimBlock { tags: names, items })] };
    Plan { blocks, invalid }
}

/// Computed values, no I/O. The clock is injectable so tests are deterministic.
pub struct SimulatorDriver {
    params: SimParams,
    timeout_ms: i64,
    clock: SimClock,
    connected: bool,
    /// `read()` calls, failed ones included (`fail_every_n_reads`).
    reads: u64,
    /// Tag name -> good reads (COUNTER).
    served: HashMap<String, u64>,
}

impl SimulatorDriver {
    pub fn new(device: &Device) -> Result<Self, String> {
        Self::with_clock(device, Arc::new(now_utc))
    }

    pub fn with_clock(device: &Device, clock: SimClock) -> Result<Self, String> {
        Ok(Self {
            params: SimParams::from_map(&device.params)?,
            timeout_ms: device.timeout_ms.max(0),
            clock,
            connected: false,
            reads: 0,
            served: HashMap::new(),
        })
    }

    pub fn params(&self) -> &SimParams {
        &self.params
    }
}

#[async_trait]
impl Driver for SimulatorDriver {
    fn name(&self) -> &'static str {
        "simulator"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        plan_tags(tags, &self.params)
    }

    fn connected(&self) -> bool {
        self.connected
    }

    fn reports_connection(&self) -> bool {
        true
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        if self.params.fail_connect {
            return Err(DriverError::ConnectionLost("simulated connect failure (params.fail_connect)".into()));
        }
        self.connected = true;
        Ok(())
    }

    async fn close(&mut self) {
        self.connected = false;
    }

    async fn identity(&mut self) -> Map<String, Json> {
        if !self.connected {
            return Map::new();
        }
        let mut m = Map::new();
        m.insert("vendor".into(), "opencap".into());
        m.insert("model".into(), "simulator".into());
        m.insert("simulated".into(), true.into());
        m
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        if !self.connected {
            return Err(DriverError::ConnectionLost("not connected".into()));
        }
        let block = block
            .as_any()
            .downcast_ref::<SimBlock>()
            .ok_or_else(|| DriverError::Other("not a simulator block".into()))?;
        self.reads += 1;
        let latency = self.params.latency_ms;
        if latency > 0 {
            let timeout = self.timeout_ms as u64;
            if latency >= timeout {
                tokio::time::sleep(Duration::from_millis(timeout)).await;
                return Err(DriverError::Comm(format!(
                    "no answer within {timeout} ms (simulated latency {latency} ms)"
                )));
            }
            tokio::time::sleep(Duration::from_millis(latency)).await;
        }
        let n = self.params.fail_every_n_reads;
        if n > 0 && self.reads % n == 0 {
            return Err(DriverError::Comm(format!(
                "simulated read failure (read {}, params.fail_every_n_reads={n})",
                self.reads
            )));
        }
        let now = (self.clock)();
        let mut out = HashMap::with_capacity(block.tags.len());
        for name in &block.tags {
            if self.params.bad_decode_tags.contains(name) {
                out.insert(name.clone(), TagValue::new(None, Quality::BadDecode, Some(now)));
                continue;
            }
            let (addr, dtype) = &block.items[name];
            let served = self.served.get(name).copied().unwrap_or(0) + 1;
            if addr.func == Func::Counter {
                self.served.insert(name.clone(), served);
            }
            out.insert(name.clone(), TagValue::good(compute(addr, *dtype, now, self.params.epoch_ms, served), now));
        }
        Ok(out)
    }
}
