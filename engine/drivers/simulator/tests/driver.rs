//! The simulator driver's behaviour: fault switches, COUNTER, memory registers, qualities and
//! the injected clock. No socket is involved, so these run everywhere and fast.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::{DateTime, TimeZone, Utc};
use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Driver, DriverError};
use opencap_core::value::{Quality, Value};
use opencap_driver_simulator::SimulatorDriver;
use serde_json::{json, Value as Json};

fn device(params: Json) -> Device {
    let mut d = Device::new("SIM/D1", "SIM", "D1", "simulator", "sim", 0, Vec::new());
    d.params = params.as_object().cloned().unwrap_or_default();
    d.timeout_ms = 300;
    d
}

type Time = Arc<Mutex<DateTime<Utc>>>;

fn driver(params: Json) -> (SimulatorDriver, Time) {
    let now: Time = Arc::new(Mutex::new(Utc.timestamp_millis_opt(10_000).unwrap()));
    let clock = now.clone();
    let drv = SimulatorDriver::with_clock(&device(params), Arc::new(move || *clock.lock().unwrap())).expect("params");
    (drv, now)
}

fn tags(list: &[(&str, &str, DType)]) -> Vec<Tag> {
    list.iter().map(|(n, a, d)| Tag::new(n, a, *d)).collect()
}

async fn read_all(
    drv: &mut SimulatorDriver,
    tags: &[Tag],
) -> Result<std::collections::HashMap<String, opencap_core::value::TagValue>, DriverError> {
    let refs: Vec<&Tag> = tags.iter().collect();
    let plan = drv.plan(&refs);
    assert!(plan.invalid.is_empty(), "{:?}", plan.invalid);
    drv.read(plan.blocks[0].as_ref()).await
}

#[tokio::test]
async fn reads_computed_values_with_the_injected_clock() {
    let (mut drv, now) = driver(json!({"memory": {"K1": 12.5}}));
    drv.connect().await.unwrap();
    let t = tags(&[
        ("ramp", "RAMP(1000, 0, 5, 2)", DType::Int16),
        ("k", "K1", DType::Float32),
        ("now", "TIME", DType::String),
        ("c", "CONST(\"hi\")", DType::String),
    ]);
    let out = read_all(&mut drv, &t).await.unwrap();
    assert_eq!(out["ramp"].value, Some(Value::Int(2)), "n = 10, 3 steps: 10 mod 3 = 1");
    assert_eq!(out["k"].value, Some(Value::Float(12.5)));
    assert_eq!(out["now"].value, Some(Value::Str("1970-01-01T00:00:10.000Z".into())));
    assert_eq!(out["c"].value, Some(Value::Str("hi".into())));
    assert!(out.values().all(|v| v.quality == Quality::Good && v.source_ts == Some(*now.lock().unwrap())));
    *now.lock().unwrap() = Utc.timestamp_millis_opt(12_000).unwrap();
    assert_eq!(read_all(&mut drv, &t).await.unwrap()["ramp"].value, Some(Value::Int(0)), "n = 12");
}

#[tokio::test]
async fn counter_counts_good_reads_per_tag_and_wraps() {
    let (mut drv, _) = driver(json!({"fail_every_n_reads": 3}));
    drv.connect().await.unwrap();
    let t = tags(&[("c", "COUNTER", DType::Int16), ("d", "COUNTER", DType::Float64)]);
    let mut seen = Vec::new();
    for _ in 0..4 {
        match read_all(&mut drv, &t).await {
            Ok(o) => seen.push((o["c"].value.clone(), o["d"].value.clone())),
            Err(e) => assert!(matches!(e, DriverError::Comm(_)), "{e:?}"),
        }
    }
    // read 3 failed and did not count
    assert_eq!(
        seen,
        vec![
            (Some(Value::Int(1)), Some(Value::Float(1.0))),
            (Some(Value::Int(2)), Some(Value::Float(2.0))),
            (Some(Value::Int(3)), Some(Value::Float(3.0)))
        ]
    );
}

#[tokio::test]
async fn fault_switches() {
    let (mut drv, _) = driver(json!({"fail_connect": true}));
    assert!(matches!(drv.connect().await, Err(DriverError::ConnectionLost(_))));
    assert!(!drv.connected());

    let (mut drv, _) = driver(json!({"fail_every_n_reads": 1}));
    drv.connect().await.unwrap();
    let t = tags(&[("a", "CONST(1)", DType::Int16)]);
    assert!(matches!(read_all(&mut drv, &t).await, Err(DriverError::Comm(_))));

    let (mut drv, _) = driver(json!({"bad_decode_tags": ["a"]}));
    drv.connect().await.unwrap();
    let out =
        read_all(&mut drv, &tags(&[("a", "CONST(1)", DType::Int16), ("b", "CONST(2)", DType::Int16)])).await.unwrap();
    assert_eq!((out["a"].quality, out["a"].value.clone()), (Quality::BadDecode, None));
    assert_eq!(out["b"].quality, Quality::Good);

    let (mut drv, _) = driver(json!({}));
    let t = tags(&[("a", "CONST(1)", DType::Int16)]);
    assert!(matches!(read_all(&mut drv, &t).await, Err(DriverError::ConnectionLost(_))), "not connected");
    assert!(drv.identity().await.is_empty());
    drv.connect().await.unwrap();
    assert_eq!(drv.identity().await["model"], "simulator");
}

#[tokio::test(start_paused = true)]
async fn latency_at_or_above_the_timeout_waits_the_timeout_and_fails() {
    let t = tags(&[("a", "CONST(1)", DType::Int16)]);
    let (mut drv, _) = driver(json!({"latency_ms": 500}));
    drv.connect().await.unwrap();
    let start = tokio::time::Instant::now();
    let err = read_all(&mut drv, &t).await.unwrap_err();
    assert!(matches!(&err, DriverError::Comm(m) if m.contains("no answer within 300 ms")), "{err:?}");
    assert_eq!(start.elapsed(), Duration::from_millis(300), "paused clock: exact");

    let (mut drv, _) = driver(json!({"latency_ms": 100}));
    drv.connect().await.unwrap();
    let start = tokio::time::Instant::now();
    assert!(read_all(&mut drv, &t).await.is_ok());
    assert_eq!(start.elapsed(), Duration::from_millis(100));
}

#[test]
fn bad_params_and_addresses_are_reported() {
    for (p, needle) in [
        (json!({"fail_connect": "yes"}), "fail_connect"),
        (json!({"latency_ms": -1}), "latency_ms"),
        (json!({"epoch": "not a time"}), "epoch"),
        (json!({"memory": {"X1": 1}}), "not a register"),
        (json!({"memory": {"K1": null}}), "memory.K1"),
        (json!({"bad_decode_tags": "a"}), "bad_decode_tags"),
    ] {
        let err = SimulatorDriver::new(&device(p)).err().expect("rejected");
        assert!(err.contains(needle), "{err}");
    }
    let (drv, _) = driver(json!({"epoch": "2026-01-01T00:00:00Z"}));
    assert_eq!(drv.params().epoch_ms, 1_767_225_600_000);
    let (drv, _) = driver(json!({}));
    let bad = tags(&[("x", "NOPE()", DType::Int16), ("y", "K70000", DType::Int16), ("z", "CONST(1)", DType::Int16)]);
    let refs: Vec<&Tag> = bad.iter().collect();
    let plan = drv.plan(&refs);
    assert_eq!(plan.invalid.keys().collect::<Vec<_>>(), ["x", "y"]);
    assert_eq!(plan.blocks[0].tags(), ["z"]);
    assert_eq!(plan.wire_requests(), 0);
}
