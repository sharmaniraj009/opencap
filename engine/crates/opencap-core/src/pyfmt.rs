//! Python-compatible text formatting.
//!
//! The Rust engine must produce the same bytes as the Python reference engine wherever
//! output is compared: the SubDevice JSON payload (`json.dumps` of floats uses
//! `float.__repr__`, rounding uses `round(x, n)`), `opencap check` output and config
//! warnings (`{name!r}`). This module holds those few rules in one place.

use serde_json::Value as Json;

/// `repr(float)` as CPython writes it (also what `json.dumps` writes for a float).
///
/// Shortest round-trip digits (Rust's `{:e}` gives the same digits as CPython's
/// `float_repr_style = 'short'`), switched to exponent notation when the decimal point
/// position is `<= -4` or `> 16`, with at least two exponent digits and a sign.
pub fn py_float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    let sci = format!("{:e}", x.abs());
    let (mant, exp) = sci.split_once('e').expect("{:e} always has an exponent");
    let mut exp: i32 = exp.parse().expect("exponent is an integer");
    let mut digits: String = mant.chars().filter(|c| *c != '.').collect();
    if let Some((d, e)) = tie_to_even(x.abs(), &digits, exp) {
        (digits, exp) = (d, e);
    }
    let n = digits.len() as i32;
    let decpt = exp + 1; // value = 0.DIGITS x 10^decpt
    let body = if decpt <= -4 || decpt > 16 {
        let mut m = digits[..1].to_string();
        if n > 1 {
            m.push('.');
            m.push_str(&digits[1..]);
        }
        format!("{}e{}{:02}", m, if exp < 0 { '-' } else { '+' }, exp.abs())
    } else if decpt <= 0 {
        format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
    } else if decpt >= n {
        format!("{}{}.0", digits, "0".repeat((decpt - n) as usize))
    } else {
        format!("{}.{}", &digits[..decpt as usize], &digits[decpt as usize..])
    };
    if x < 0.0 {
        format!("-{body}")
    } else {
        body
    }
}

/// Rust and CPython agree on the shortest digits except when the exact binary value lies
/// exactly halfway between two candidates of that length (common for float32 values widened
/// to a double, e.g. 819.40802001953125): Rust takes the upper one, CPython the even one.
/// Returns the even candidate (digits, exponent) when that is the case.
fn tie_to_even(x: f64, digits: &str, exp: i32) -> Option<(String, i32)> {
    let n = digits.len();
    let last = digits.as_bytes()[n - 1] - b'0';
    if last % 2 == 0 {
        return None;
    }
    // exact expansion: n significant digits, then "5" and only zeros means a tie
    let exact = format!("{:.*e}", n + 40, x);
    let (m, e) = exact.split_once('e')?;
    let all: String = m.chars().filter(|c| *c != '.').collect();
    if e.parse::<i32>().ok()? != exp || all.len() < n + 2 {
        return None;
    }
    let (head, tail) = all.split_at(n);
    if !tail.starts_with('5') || !tail[1..].bytes().all(|b| b == b'0') {
        return None;
    }
    // head is the truncation: the candidates are head and head + 1 (one ulp of the last digit);
    // Rust chose the upper one when `digits` > head
    if digits <= head {
        return None;
    }
    let even = head.to_string();
    if even.as_bytes()[n - 1] % 2 != 0 {
        return None;
    }
    let candidate: f64 = format!("{}.{}e{}", &even[..1], &even[1..], exp).parse().ok()?;
    (candidate == x).then_some((even, exp))
}

/// `round(x, ndigits)` for a Python float: correctly rounded from the exact binary value,
/// ties to even. Rust's fixed-precision formatting is exact with the same tie rule, so
/// formatting and parsing back gives CPython's result.
pub fn py_round(x: f64, ndigits: i64) -> f64 {
    // CPython floatobject.c: NDIGITS_MAX / NDIGITS_MIN for IEEE doubles.
    const NDIGITS_MAX: i64 = 323;
    const NDIGITS_MIN: i64 = -308;
    if !x.is_finite() || x == 0.0 || ndigits > NDIGITS_MAX {
        return x;
    }
    if ndigits < NDIGITS_MIN {
        return 0.0 * x;
    }
    if ndigits >= 0 {
        return format!("{:.*}", ndigits as usize, x).parse().expect("formatted float parses");
    }
    // Negative ndigits: round the exact decimal expansion at 10^k, ties to even.
    let k = (-ndigits) as usize;
    let exact = format!("{:.1074}", x.abs());
    let (int_part, frac) = exact.split_once('.').unwrap_or((exact.as_str(), ""));
    let mut int_digits = int_part.to_string();
    if int_digits.len() < k + 1 {
        int_digits = format!("{}{}", "0".repeat(k + 1 - int_digits.len()), int_digits);
    }
    let split = int_digits.len() - k;
    let (head, tail_int) = int_digits.split_at(split);
    let tail: Vec<u8> = tail_int.bytes().chain(frac.bytes()).collect();
    let first = tail.first().copied().unwrap_or(b'0');
    let rest_nonzero = tail.iter().skip(1).any(|&d| d != b'0');
    let mut head: Vec<u8> = head.bytes().collect();
    let last_odd = head.last().map(|d| (d - b'0') % 2 == 1).unwrap_or(false);
    let up = first > b'5' || (first == b'5' && (rest_nonzero || last_odd));
    if up {
        let mut i = head.len();
        loop {
            if i == 0 {
                head.insert(0, b'1');
                break;
            }
            i -= 1;
            if head[i] == b'9' {
                head[i] = b'0';
            } else {
                head[i] += 1;
                break;
            }
        }
    }
    let mut text = String::from_utf8(head).expect("ascii digits");
    text.push_str(&"0".repeat(k));
    let value: f64 = text.parse().expect("digits parse");
    if x < 0.0 {
        -value
    } else {
        value
    }
}

/// `repr(str)` (single quotes unless the text has a single quote and no double quote).
pub fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || (0x7f..=0xa0).contains(&(c as u32)) || c as u32 == 0xad => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `repr()` of a JSON-ish value the way Python prints the equivalent object
/// (`True`, `None`, `'text'`, `[1, 2]`, `{'k': 1.5}`).
pub fn py_repr_json(v: &Json) -> String {
    match v {
        Json::Null => "None".into(),
        Json::Bool(b) => if *b { "True" } else { "False" }.into(),
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else if let Some(u) = n.as_u64() {
                u.to_string()
            } else {
                py_float_repr(n.as_f64().unwrap_or(f64::NAN))
            }
        }
        Json::String(s) => py_repr_str(s),
        Json::Array(a) => format!("[{}]", a.iter().map(py_repr_json).collect::<Vec<_>>().join(", ")),
        Json::Object(o) => format!(
            "{{{}}}",
            o.iter().map(|(k, v)| format!("{}: {}", py_repr_str(k), py_repr_json(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// `repr(list_of_str)`, e.g. `['A', 'B']`.
pub fn py_repr_list(items: &[String]) -> String {
    format!("[{}]", items.iter().map(|s| py_repr_str(s)).collect::<Vec<_>>().join(", "))
}

/// Append `s` as a JSON string exactly as `json.dumps(..., ensure_ascii=False)` writes it.
pub fn write_json_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python `f"{x:.0f}"` / `f"{x:>w.0f}"` rounding (exact, ties to even) — Rust's `{:.0}`
/// behaves the same; kept as a named helper so `check` output stays obviously Python's.
pub fn py_fixed(x: f64, decimals: usize) -> String {
    format!("{:.*}", decimals, x)
}

/// Python `f"{x:.0%}"`.
pub fn py_percent0(x: f64) -> String {
    format!("{:.0}%", x * 100.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_ties_round_to_even_like_cpython() {
        assert_eq!(py_float_repr(f64::from(f32::from_bits(0x444cda1d))), "819.4080200195312");
        assert_eq!(py_float_repr(0.1), "0.1");
        assert_eq!(py_float_repr(5e-324), "5e-324");
        assert_eq!(py_float_repr(1.5), "1.5");
    }

    #[test]
    fn float_repr_matches_cpython() {
        let cases = [
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (1e16, "1e+16"),
            (1234567890123456.0, "1234567890123456.0"),
            (123.4, "123.4"),
            (0.1, "0.1"),
            (1e22, "1e+22"),
            (1.5e-7, "1.5e-07"),
            (2f64.powi(70), "1.1805916207174113e+21"),
            (12.0, "12.0"),
            (-2.5, "-2.5"),
            (1e100, "1e+100"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (100.0, "100.0"),
            (123456.789, "123456.789"),
        ];
        for (x, want) in cases {
            assert_eq!(py_float_repr(x), want, "{x:e}");
        }
    }

    #[test]
    fn round_matches_cpython() {
        assert_eq!(py_round(0.125, 2), 0.12);
        assert_eq!(py_round(0.375, 2), 0.38);
        assert_eq!(py_round(2.675, 2), 2.67);
        assert_eq!(py_round(2.5, 0), 2.0);
        assert_eq!(py_round(1.5, 0), 2.0);
        assert_eq!(py_round(123.456789, 4), 123.4568);
        assert_eq!(py_round(1e300, 4), 1e300);
        assert_eq!(py_round(15.0, -1), 20.0);
        assert_eq!(py_round(25.0, -1), 20.0);
        assert_eq!(py_round(25.000001, -1), 30.0);
        assert_eq!(py_round(1234.5, -2), 1200.0);
        assert_eq!(py_round(-1250.0, -2), -1200.0);
        assert_eq!(py_round(4.0, -1), 0.0);
        assert!(py_round(-4.0, -1).is_sign_negative());
        assert_eq!(py_round(99.0, -2), 100.0);
        assert_eq!(py_round(1e10, -400), 0.0);
    }

    #[test]
    fn repr_str() {
        assert_eq!(py_repr_str("abc"), "'abc'");
        assert_eq!(py_repr_str("it's"), "\"it's\"");
        assert_eq!(py_repr_str("a'b\"c"), "'a\\'b\"c'");
        assert_eq!(py_repr_str("192.0.2.10 "), "'192.0.2.10 '");
        assert_eq!(py_repr_str("tab\there"), "'tab\\there'");
        assert_eq!(py_repr_str("°C"), "'°C'");
        assert_eq!(py_repr_json(&serde_json::json!("maybe")), "'maybe'");
        assert_eq!(py_repr_json(&serde_json::json!(true)), "True");
        assert_eq!(py_repr_json(&serde_json::json!(5.0)), "5.0");
        assert_eq!(py_repr_list(&["A".to_string(), "B".to_string()]), "['A', 'B']");
    }

    #[test]
    fn json_string_escape() {
        let mut out = String::new();
        write_json_str(&mut out, "a\"b\\c\n\u{1}\u{7f}°");
        assert_eq!(out, "\"a\\\"b\\\\c\\n\\u0001\u{7f}°\"");
    }

    #[test]
    fn fixed_and_percent() {
        assert_eq!(py_fixed(0.5, 0), "0");
        assert_eq!(py_fixed(2.5, 0), "2");
        assert_eq!(py_percent0(0.125), "12%");
        assert_eq!(py_percent0(1.0), "100%");
    }
}
