//! The shared simulator vectors (`tests/conformance/simulator/vectors.json`, ADR-0014) through
//! the Rust parser and value functions.

use opencap_core::config::DType;
use opencap_core::value::Value;
use opencap_driver_simulator::{compute, parse_address, SimParams};
use serde_json::Value as Json;

fn dtype(name: &str) -> DType {
    serde_json::from_value(Json::String(name.into())).unwrap_or_else(|_| panic!("dtype {name}"))
}

fn params(v: &Json) -> SimParams {
    SimParams::from_map(v.get("params").and_then(Json::as_object).unwrap_or(&Default::default())).expect("params")
}

fn load() -> Json {
    let path = opencap_conformance::conformance_root().join("simulator/vectors.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("vectors")).expect("json")
}

#[test]
fn valid_vectors() {
    let doc = load();
    let mut failures = Vec::new();
    for (i, v) in doc["valid"].as_array().unwrap().iter().enumerate() {
        let (addr, dt) = (v["address"].as_str().unwrap(), dtype(v["dtype"].as_str().unwrap()));
        let p = params(v);
        let label = format!("#{i} {addr} {} at {} ms", v["dtype"], v["at_ms"]);
        let parsed = match parse_address(addr, dt, &p) {
            Ok(a) => a,
            Err(e) => {
                failures.push(format!("{label}: does not parse: {e}"));
                continue;
            }
        };
        let now = chrono::DateTime::from_timestamp_millis(v["at_ms"].as_i64().unwrap()).unwrap();
        let reads = v["reads"].as_u64().unwrap_or(1);
        let got = compute(&parsed, dt, now, p.epoch_ms, reads);
        let want = &v["expected"];
        let ok = match (&got, want) {
            (Value::Bool(a), Json::Bool(b)) => a == b,
            (Value::Str(a), Json::String(b)) => a == b,
            (Value::Int(a), w) if w.is_i64() => Some(*a) == w.as_i64(),
            (Value::UInt(a), w) if w.is_u64() => Some(*a) == w.as_u64(),
            // a float vector must be a JSON number written with a fraction or exponent
            (Value::Float(a), Json::Number(n)) if !n.is_i64() && !n.is_u64() || dt_is_float(dt) => {
                let b = n.as_f64().unwrap();
                match v.get("tolerance").and_then(Json::as_f64) {
                    Some(tol) => (a - b).abs() <= tol,
                    None => a.to_bits() == b.to_bits() || a == &b,
                }
            }
            _ => false,
        };
        if !ok {
            failures.push(format!("{label}: expected {want}, got {got:?}"));
        }
    }
    assert!(failures.is_empty(), "{} simulator vectors fail:\n{}", failures.len(), failures.join("\n"));
}

fn dt_is_float(d: DType) -> bool {
    matches!(d, DType::Float32 | DType::Float64)
}

#[test]
fn invalid_vectors() {
    let doc = load();
    let mut failures = Vec::new();
    for v in doc["invalid"].as_array().unwrap() {
        let (addr, dt) = (v["address"].as_str().unwrap(), dtype(v["dtype"].as_str().unwrap()));
        match parse_address(addr, dt, &params(v)) {
            Ok(a) => failures.push(format!("{addr:?} {}: parsed as {a:?}", v["dtype"])),
            Err(e) => {
                let want = v["error"].as_str().unwrap();
                if !e.contains(want) {
                    failures.push(format!("{addr:?} {}: error {e:?} does not contain {want:?}", v["dtype"]));
                }
            }
        }
    }
    assert!(failures.is_empty(), "{} invalid vectors fail:\n{}", failures.len(), failures.join("\n"));
}
