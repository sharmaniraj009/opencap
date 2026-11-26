//! Loader for the shared conformance vectors (`tests/conformance`, ADR-0014) plus the value
//! codec and comparison every area needs. Nothing here knows an area: a driver port adds an
//! adapter (`input` JSON -> result JSON in the shape of `expected`) and calls [`run_area`].
//!
//! Format and rules: `tests/conformance/README.md`. Failures are collected and reported
//! once, each with its key, description and the paths that differ.

use std::collections::HashSet;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use serde_json::{json, Value as Json};

use opencap_core::pyfmt::py_float_repr;
use opencap_core::value::Value;

/// The vector file format this loader reads (`"format": 1`); any other is refused.
pub const FORMAT: u64 = 1;

/// `tests/conformance` at the repository root.
pub fn conformance_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/conformance")
}

#[derive(Debug, Clone)]
pub struct Vector {
    pub area: String,
    /// Path relative to the root, `modbus/plan/gaps.json`.
    pub file: String,
    pub id: String,
    pub description: String,
    pub input: Json,
    pub expected: Json,
}

impl Vector {
    /// `<file without .json>/<id>`, unique across all vectors.
    pub fn key(&self) -> String {
        format!("{}/{}", self.file.trim_end_matches(".json"), self.id)
    }
}

fn valid_id(id: &str) -> bool {
    let mut chars = id.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c.is_ascii_digit())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '_' | '.' | '-'))
}

/// Parse one vector file's text. `rel` is its path relative to the root (for messages and the
/// area check).
pub fn parse_file(rel: &str, text: &str) -> Result<Vec<Vector>, String> {
    let doc: Json = serde_json::from_str(text).map_err(|e| format!("{rel}: {e}"))?;
    match doc["format"].as_u64() {
        Some(FORMAT) => {}
        other => return Err(format!("{rel}: format {other:?}, this loader reads format {FORMAT}")),
    }
    let area = doc["area"].as_str().ok_or_else(|| format!("{rel}: no area"))?;
    if rel.rsplit_once('/').map(|(d, _)| d) != Some(area) {
        return Err(format!("{rel}: area {area:?} does not match the file's directory"));
    }
    let raw = doc["vectors"].as_array().ok_or_else(|| format!("{rel}: no vectors"))?;
    let (mut out, mut seen) = (Vec::new(), HashSet::new());
    for (i, v) in raw.iter().enumerate() {
        let id = v["id"].as_str().unwrap_or_default();
        if !valid_id(id) || !seen.insert(id.to_string()) {
            return Err(format!("{rel}: vector {i} has a bad or duplicate id {id:?}"));
        }
        let description = v["description"].as_str().unwrap_or_default();
        if description.trim().is_empty() {
            return Err(format!("{rel}: vector {id:?} has no description"));
        }
        out.push(Vector {
            area: area.to_string(),
            file: rel.to_string(),
            id: id.to_string(),
            description: description.to_string(),
            input: v["input"].clone(),
            expected: v["expected"].clone(),
        });
    }
    Ok(out)
}

/// Every vector of `area` (`modbus/plan`, `scaling`, ...), in sorted file order.
pub fn load_area(area: &str) -> Result<Vec<Vector>, String> {
    let dir = conformance_root().join(area);
    let mut paths: Vec<PathBuf> = std::fs::read_dir(&dir)
        .map_err(|e| format!("{}: {e}", dir.display()))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    let mut out = Vec::new();
    for path in paths {
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or_default();
        let text = std::fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        out.extend(parse_file(&format!("{area}/{name}"), &text)?);
    }
    Ok(out)
}

// --- values ---------------------------------------------------------------------------------

/// Decode a tagged value. `{"t":"none"}` is `None`.
pub fn decode_value(v: &Json) -> Result<Option<Value>, String> {
    let x = &v["v"];
    let bad = || format!("bad conformance value {v}");
    Ok(Some(match v["t"].as_str().ok_or_else(bad)? {
        "none" => return Ok(None),
        "bool" => Value::Bool(x.as_bool().ok_or_else(bad)?),
        "int" => {
            let s = x.as_str().ok_or_else(bad)?;
            match s.parse::<i64>() {
                Ok(i) => Value::Int(i),
                Err(_) => Value::UInt(s.parse::<u64>().map_err(|_| bad())?),
            }
        }
        "float" => Value::Float(parse_py_float(x.as_str().ok_or_else(bad)?).ok_or_else(bad)?),
        "str" => Value::Str(x.as_str().ok_or_else(bad)?.to_string()),
        "list" => Value::List(
            x.as_array()
                .ok_or_else(bad)?
                .iter()
                .map(|e| decode_value(e).and_then(|o| o.ok_or_else(bad)))
                .collect::<Result<_, _>>()?,
        ),
        _ => return Err(bad()),
    }))
}

/// Python `repr(float)` text back to a double: `nan`, `inf`, `-inf`, `-0.0`, `1e+16`.
fn parse_py_float(s: &str) -> Option<f64> {
    match s {
        "nan" => Some(f64::NAN),
        "inf" => Some(f64::INFINITY),
        "-inf" => Some(f64::NEG_INFINITY),
        _ => s.parse().ok(),
    }
}

/// Encode a value the way the vectors do (exact type and bits; `None` is `none`).
pub fn encode_value(v: Option<&Value>) -> Json {
    match v {
        None => json!({"t": "none"}),
        Some(Value::Bool(b)) => json!({"t": "bool", "v": b}),
        Some(Value::Int(i)) => json!({"t": "int", "v": i.to_string()}),
        Some(Value::UInt(u)) => json!({"t": "int", "v": u.to_string()}),
        Some(Value::Float(f)) => json!({"t": "float", "v": py_float_repr(*f)}),
        Some(Value::Str(s)) => json!({"t": "str", "v": s}),
        Some(Value::List(items)) => {
            json!({"t": "list", "v": items.iter().map(|i| encode_value(Some(i))).collect::<Vec<_>>()})
        }
    }
}

/// A per-tag decode failure as the vectors encode it.
pub fn encode_decode_error(reason: &str) -> Json {
    json!({"t": "decode_error", "v": reason})
}

// --- comparing ------------------------------------------------------------------------------

/// Paths where `expected` and `actual` differ (objects by key, arrays by index and length,
/// leaves exactly; encoded values are objects of text, so `-0.0` differs from `0.0` and an
/// int never equals a float). Empty = equal.
pub fn diff(expected: &Json, actual: &Json) -> Vec<String> {
    let mut out = Vec::new();
    diff_at("$", expected, actual, &mut out);
    out
}

fn diff_at(path: &str, e: &Json, a: &Json, out: &mut Vec<String>) {
    match (e, a) {
        (Json::Object(x), Json::Object(y)) => {
            for (k, xv) in x {
                match y.get(k) {
                    Some(yv) => diff_at(&format!("{path}.{k}"), xv, yv, out),
                    None => out.push(format!("{path}.{k}: missing (expected {xv})")),
                }
            }
            for (k, yv) in y {
                if !x.contains_key(k) {
                    out.push(format!("{path}.{k}: unexpected (got {yv})"));
                }
            }
        }
        (Json::Array(x), Json::Array(y)) => {
            if x.len() != y.len() {
                out.push(format!("{path}: {} items, expected {}", y.len(), x.len()));
            }
            for (i, (xv, yv)) in x.iter().zip(y).enumerate() {
                diff_at(&format!("{path}[{i}]"), xv, yv, out);
            }
        }
        _ if e == a => {}
        _ => out.push(format!("{path}: expected {e}, got {a}")),
    }
}

/// Run `adapter` on every vector of `area` and panic once, listing every mismatch with its
/// key, description and differing paths, if any differ. An adapter `Err` fails that vector
/// too. An area with no vectors also fails, so a moved directory cannot pass vacuously.
/// Returns the number of vectors checked.
pub fn run_area(area: &str, adapter: impl Fn(&Json) -> Result<Json, String>) -> usize {
    let vectors = load_area(area).unwrap_or_else(|e| panic!("cannot load {area}: {e}"));
    assert!(!vectors.is_empty(), "no conformance vectors under tests/conformance/{area}");
    let mut failures = String::new();
    let mut failed = 0;
    for v in &vectors {
        let problem = match adapter(&v.input) {
            Ok(actual) => {
                let d = diff(&v.expected, &actual);
                (!d.is_empty()).then(|| d.iter().take(8).map(|l| format!("  {l}\n")).collect::<String>())
            }
            Err(e) => Some(format!("  adapter error: {e}\n")),
        };
        if let Some(p) = problem {
            failed += 1;
            let _ = write!(failures, "{}\n  {}\n{p}\n", v.key(), v.description);
        }
    }
    assert!(failed == 0, "{failed} of {} {area} vectors fail:\n\n{failures}", vectors.len());
    vectors.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn value_codec_round_trips_the_awkward_values() {
        for text in ["nan", "inf", "-inf", "-0.0", "1e+16", "0.1", "5e-324"] {
            let enc = json!({"t": "float", "v": text});
            let v = decode_value(&enc).unwrap();
            assert_eq!(encode_value(v.as_ref()), enc, "{text}");
        }
        let big = json!({"t": "int", "v": "18446744073709551615"});
        assert_eq!(encode_value(decode_value(&big).unwrap().as_ref()), big);
        let nested = json!({"t": "list", "v": [{"t": "list", "v": [{"t": "bool", "v": true}]}]});
        assert_eq!(encode_value(decode_value(&nested).unwrap().as_ref()), nested);
    }

    #[test]
    fn diff_tells_int_from_float_and_zero_from_negative_zero() {
        let i = json!({"t": "int", "v": "1"});
        let f = json!({"t": "float", "v": "1.0"});
        assert!(diff(&i, &i).is_empty());
        assert_eq!(diff(&i, &f).len(), 2);
        assert_eq!(diff(&json!({"t": "float", "v": "0.0"}), &json!({"t": "float", "v": "-0.0"})).len(), 1);
        assert_eq!(diff(&json!([1, 2]), &json!([1])).len(), 1);
    }

    #[test]
    fn loader_refuses_unknown_format_bad_area_and_duplicate_ids() {
        let ok = r#"{"format":1,"area":"a/b","vectors":[{"id":"x","description":"d","input":{},"expected":{}}]}"#;
        assert_eq!(parse_file("a/b/f.json", ok).unwrap()[0].key(), "a/b/f/x");
        assert!(parse_file("a/b/f.json", &ok.replace("\"format\":1", "\"format\":2")).is_err());
        assert!(parse_file("a/c/f.json", ok).is_err());
        let dup = ok.replace("}]}", "},{\"id\":\"x\",\"description\":\"d\",\"input\":{},\"expected\":{}}]}");
        assert!(parse_file("a/b/f.json", &dup).is_err());
        assert!(parse_file("a/b/f.json", &ok.replace("\"x\"", "\"X\"")).is_err());
    }
}
