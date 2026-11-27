//! System-tag conformance (spec `docs/spec/system-tags.md`, #56): the `snapshot` and
//! `device_sequence` vectors of `tests/conformance/system_tags/` run through the pure state ->
//! tags function of `opencap_core::system_tags`, and the tag table is checked against
//! `tests/conformance/system_tags_catalogue.json`.

use std::collections::BTreeSet;

use serde_json::{json, Value as Json};

use opencap_conformance::{conformance_root, encode_value, run_area};
use opencap_core::health::DeviceHealth;
use opencap_core::system_tags::{
    device_status_tags, evaluate, is_tick, parse_ts, Counters, DeviceStatusState, State, SysValue, TICK_TAGS,
};
use opencap_core::value::{iso_utc, Value};

fn to_value(v: &SysValue) -> Option<Value> {
    Some(match v {
        SysValue::Bool(b) => Value::Bool(*b),
        SysValue::U32(n) => Value::Int(i64::from(*n)),
        SysValue::U64(n) => Value::from_u64(*n),
        SysValue::F64(f) => Value::Float(*f),
        SysValue::Str(t) => Value::Str(t.clone()),
        SysValue::DateTime(t) => return t.clone().map(Value::Str),
        SysValue::StrArray(a) => Value::List(a.iter().cloned().map(Value::Str).collect()),
    })
}

fn tagged(v: &SysValue) -> Json {
    json!({"dtype": v.dtype(), "value": encode_value(to_value(v).as_ref())})
}

fn snapshot(input: &Json) -> Result<Json, String> {
    let state: State = serde_json::from_value(input["state"].clone()).map_err(|e| format!("state: {e}"))?;
    let got = evaluate(&state)?;
    let mut tags = serde_json::Map::new();
    for p in input["paths"].as_array().ok_or("no paths")? {
        let p = p.as_str().ok_or("path")?;
        let v = got.get(p).ok_or_else(|| format!("{p} missing"))?;
        tags.insert(p.to_string(), tagged(v));
    }
    let absent: Vec<Json> = input["absent"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|a| {
            let a = a.as_str().unwrap_or_default();
            !got.keys().any(|g| g == a || g.starts_with(&format!("{a}.")))
        })
        .collect();
    Ok(json!({"tags": tags, "absent": absent}))
}

fn sequence(input: &Json) -> Result<Json, String> {
    let dev = &input["device"];
    let start = parse_ts(input["start"].as_str().ok_or("start")?)?;
    let mut health =
        DeviceHealth::new(dev["demote_after"].as_i64().unwrap_or(3), dev["demote_for_ms"].as_i64().unwrap_or(30_000));
    let (mut connected, mut last_connect_failed, mut last_error, mut cycle_ms) = (false, false, None::<String>, 0.0);
    let mut steps = serde_json::Map::new();
    for (i, step) in input["steps"].as_array().ok_or("steps")?.iter().enumerate() {
        let at = parse_ts(step["at"].as_str().ok_or("at")?)?;
        let mono = (at - start).num_microseconds().ok_or("range")? as f64 / 1e6;
        let error = step["error"].as_str().map(str::to_string);
        let ms = step["cycle_ms"].as_f64().unwrap_or(0.0);
        match step["event"].as_str().ok_or("event")? {
            "connect_ok" => (connected, last_connect_failed) = (true, false),
            "connect_failed" => (connected, last_connect_failed) = (false, true),
            "cycle_good" => {
                health.record_good(mono, at);
                (last_error, cycle_ms) = (error, ms);
            }
            "cycle_failed" => {
                let ev = health.record_failure(mono, at);
                (last_error, cycle_ms) = (error, ms);
                if matches!(
                    ev,
                    Some(opencap_core::health::HealthEvent::Demoted | opencap_core::health::HealthEvent::Redemoted)
                ) {
                    connected = false; // the worker drops the connection on demotion
                }
            }
            "cycle_neutral" => {
                cycle_ms = ms;
                if error.is_some() {
                    last_error = error;
                }
            }
            "tick" => {}
            other => return Err(format!("step {i}: unknown event {other:?}")),
        }
        let Some(observe) = step["observe"].as_array().filter(|o| !o.is_empty()) else { continue };
        let status = DeviceStatusState {
            state: health.state.as_str().to_string(),
            consecutive_failures: health.consecutive_failures,
            error_since: health.error_since.map(iso_utc),
            last_good: health.last_good.map(iso_utc),
            errors: health.errors,
            last_error: last_error.clone(),
            cycle_ms: Some(cycle_ms),
            connected,
            last_connect_failed,
        };
        let tags = device_status_tags(&Counters::default(), Some(&status), at, false)?;
        let mut out = serde_json::Map::new();
        for name in observe {
            let name = name.as_str().ok_or("observe")?;
            let (_, v) = tags.iter().find(|(n, _)| *n == name).ok_or_else(|| format!("no status tag {name}"))?;
            out.insert(name.to_string(), tagged(v));
        }
        steps.insert(i.to_string(), Json::Object(out));
    }
    Ok(json!({"steps": steps}))
}

#[test]
fn system_tag_vectors() {
    let n = run_area("system_tags", |input| match input["kind"].as_str() {
        Some("snapshot") => snapshot(input),
        Some("device_sequence") => sequence(input),
        other => Err(format!("unknown kind {other:?}")),
    });
    assert!(n >= 20, "only {n} system-tag vectors ran");
}

fn catalogue() -> Json {
    let text = std::fs::read_to_string(conformance_root().join("system_tags_catalogue.json")).expect("catalogue");
    serde_json::from_str(&text).expect("catalogue json")
}

#[test]
fn tick_table_is_the_catalogue() {
    let cat = catalogue();
    let from_catalogue: BTreeSet<&str> = cat["tags"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|t| t["update"] == "tick")
        .map(|t| t["name"].as_str().unwrap())
        .collect();
    let ours: BTreeSet<&str> = TICK_TAGS.iter().copied().collect();
    assert_eq!(ours, from_catalogue);
    assert!(is_tick("_DateTime") && !is_tick("_Error"));
}

/// Every tag a full snapshot produces is in the catalogue with the same dtype, and every
/// catalogue tag name is produced by the vectors' states (checked by the vectors themselves).
#[test]
fn evaluated_dtypes_match_the_catalogue() {
    let cat = catalogue();
    let known: BTreeSet<(String, String)> = cat["tags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| (t["name"].as_str().unwrap().to_string(), t["dtype"].as_str().unwrap().to_string()))
        .collect();
    let vectors = opencap_conformance::load_area("system_tags").unwrap();
    let mut checked = 0;
    for v in vectors.iter().filter(|v| v.input["kind"] == "snapshot") {
        let state: State = serde_json::from_value(v.input["state"].clone()).unwrap();
        for (path, val) in evaluate(&state).unwrap() {
            let name = path.rsplit('.').next().unwrap().to_string();
            assert!(
                known.contains(&(name.clone(), val.dtype().to_string())),
                "{} {path}: {name} {}",
                v.key(),
                val.dtype()
            );
            checked += 1;
        }
    }
    assert!(checked > 100);
}
