//! Planner and decoder of the omron_fins driver, without a socket.

use opencap_core::config::{DType, Device, Tag};
use opencap_core::value::Value;
use opencap_driver_omron_fins::{decode_range, FinsBlock, FinsDriver, Request};
use serde_json::{json, Value as Json};

fn driver(params: Json) -> FinsDriver {
    let mut d = Device::new("SITE/PLC", "SITE", "PLC", "omron_fins", "192.0.2.10", 9600, vec![]);
    d.params = params.as_object().unwrap().clone();
    FinsDriver::new(&d).unwrap()
}

fn plan(drv: &FinsDriver, tags: &[(&str, &str, DType)]) -> (Vec<Request>, Vec<(String, String)>) {
    let tags: Vec<Tag> = tags.iter().map(|(n, a, d)| Tag::new(n, a, *d)).collect();
    let p = drv.plan_tags(&tags);
    let reqs = p.blocks.iter().map(|b| b.as_any().downcast_ref::<FinsBlock>().unwrap().request.clone()).collect();
    (reqs, p.invalid.into_iter().collect())
}

fn describe(reqs: &[Request]) -> Vec<String> {
    reqs.iter()
        .map(|r| match r {
            Request::Read(r) => r.describe(),
            Request::Multi(rs) => format!("0104 {}", rs.iter().map(|r| r.describe()).collect::<Vec<_>>().join(" + ")),
        })
        .collect()
}

#[test]
fn words_merge_within_the_gap_and_the_request_size() {
    let drv = driver(json!({"max_gap": 2}));
    let (reqs, invalid) = plan(
        &drv,
        &[
            ("A", "D100", DType::Uint16),
            ("B", "D102", DType::Float32),
            ("C", "D106", DType::Uint16),
            ("BIT", "D107.03", DType::Bool),
            ("FAR", "D200", DType::Int32),
        ],
    );
    assert!(invalid.is_empty());
    assert_eq!(describe(&reqs), ["D100..107 (8 words)", "D200..201 (2 words)"], "a bit rides in its word");
    // request_size 32 bytes = 16 words per read
    let drv = driver(json!({"request_size": 32, "max_gap": 100}));
    let (reqs, _) = plan(&drv, &[("A", "D0", DType::Uint16), ("B", "D15", DType::Uint16), ("C", "D16", DType::Uint16)]);
    assert_eq!(describe(&reqs), ["D0..15 (16 words)", "D16 (1 word)"]);
    let (_, invalid) = plan(&drv, &[("S", "D0.40H", DType::String)]);
    assert!(invalid[0].1.contains("more than request_size 32"), "{invalid:?}");
}

#[test]
fn single_elements_share_one_multiple_memory_area_read() {
    let drv = driver(json!({}));
    let tags = [
        ("A", "D0", DType::Uint16),
        ("B", "D500", DType::Uint16),
        ("C", "TS3", DType::Bool),
        ("D", "IR1", DType::Uint32),
        ("E", "D1000", DType::Uint16),
        ("W", "D2000", DType::Float32),
    ];
    let (reqs, _) = plan(&drv, &tags);
    assert_eq!(
        describe(&reqs),
        [
            "D2000..2001 (2 words)",
            "0104 TS3 (1 flag) + D0 (1 word) + D500 (1 word) + D1000 (1 word) + IR1 (1 register)"
        ],
        "items by area code, then address"
    );
    let off = driver(json!({"multi_read": false}));
    assert_eq!(plan(&off, &tags).0.len(), 6, "multi_read false: one 0101 per range");
}

#[test]
fn timers_and_counters_share_an_area_code_but_never_a_read() {
    let drv = driver(json!({"max_gap": 999, "multi_read": false}));
    let (reqs, _) =
        plan(&drv, &[("T", "T4095", DType::Uint16), ("C", "C0", DType::Uint16), ("C1", "C2", DType::Uint16)]);
    assert_eq!(describe(&reqs), ["T4095 (1 word)", "C0..2 (3 words)"]);
}

fn decode(params: Json, address: &str, dtype: DType, words: &[u16]) -> Value {
    let drv = driver(params);
    let (reqs, invalid) = plan(&drv, &[("X", address, dtype)]);
    assert!(invalid.is_empty(), "{invalid:?}");
    let Request::Read(r) = &reqs[0] else { panic!("one range") };
    let data: Vec<u8> = words.iter().flat_map(|w| w.to_be_bytes()).collect();
    decode_range(r, &data, &drv.params).remove(0).1.unwrap()
}

#[test]
fn values_decode_as_kepserverex() {
    assert_eq!(decode(json!({}), "D0", DType::Int32, &[0x5678, 0x1234]), Value::Int(0x1234_5678), "low word first");
    assert_eq!(
        decode(json!({"first_word_low": false}), "D0", DType::Int32, &[0x1234, 0x5678]),
        Value::Int(0x1234_5678)
    );
    assert_eq!(decode(json!({}), "D0", DType::Float32, &[0x0000, 0x3FC0]), Value::Float(1.5));
    assert_eq!(decode(json!({}), "D0D", DType::Uint16, &[0x1234]), Value::Int(1234), "BCD suffix");
    assert_eq!(decode(json!({}), "D0D", DType::Int16, &[0x8050]), Value::Int(-50), "signed BCD, PTC's example");
    assert_eq!(
        decode(json!({}), "D0D", DType::Uint32, &[0x5678, 0x1234]),
        Value::Int(12_345_678),
        "LBCD low word first"
    );
    assert_eq!(decode(json!({}), "T3", DType::Uint16, &[0x0750]), Value::Int(750), "timer PV in BCD");
    assert_eq!(decode(json!({"tc_bcd": false}), "T3", DType::Uint16, &[0x0750]), Value::Int(0x0750), "binary mode");
    assert_eq!(decode(json!({}), "D0.4H", DType::String, &[0x4142, 0x4300]), Value::Str("ABC".into()));
    assert_eq!(decode(json!({}), "D0.4L", DType::String, &[0x4241, 0x0043]), Value::Str("ABC".into()));
    assert_eq!(decode(json!({}), "D0.3D", DType::String, &[0x4100, 0x4200, 0x4300]), Value::Str("ABC".into()));
    assert_eq!(decode(json!({}), "D0.3E", DType::String, &[0x0041, 0x0042, 0x0043]), Value::Str("ABC".into()));
    assert_eq!(
        decode(json!({}), "D0[2][2]", DType::Int16, &[1, 0xFFFF, 3, 4]),
        Value::List(vec![
            Value::List(vec![Value::Int(1), Value::Int(-1)]),
            Value::List(vec![Value::Int(3), Value::Int(4)])
        ])
    );
    assert_eq!(decode(json!({}), "CIO10.15", DType::Bool, &[0x8000]), Value::Bool(true));
}

#[test]
fn a_bad_bcd_word_is_a_decode_error_and_the_driver_has_no_write() {
    let drv = driver(json!({}));
    let (reqs, _) = plan(&drv, &[("X", "D0D", DType::Uint16)]);
    let Request::Read(r) = &reqs[0] else { panic!() };
    assert!(decode_range(r, &[0x12, 0x3A], &drv.params)[0].1.is_err());
    let src = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/wire.rs")).unwrap();
    // write command codes (0x2301 is also an end code, so it is not in this list)
    for word in ["0x0102", "0x0103", "0x0105", "0x0702"] {
        assert!(!src.split("#[cfg(test)]").next().unwrap().contains(word), "wire.rs (non-test) names {word}");
    }
}

#[test]
fn params_are_checked() {
    let mut d = Device::new("SITE/PLC", "SITE", "PLC", "omron_fins", "plc.example", 9600, vec![]);
    let err = |p: Json, d: &mut Device| {
        d.params = p.as_object().unwrap().clone();
        FinsDriver::new(d).err().unwrap()
    };
    assert!(err(json!({}), &mut d).contains("set params.dest_node"), "a host name has no last octet");
    assert!(err(json!({"dest_node": 1, "model": "CV1000"}), &mut d).contains("CS1, CJ1 or CJ2"));
    assert!(err(json!({"dest_node": 1, "request_size": 100}), &mut d).contains("request_size=100"));
    assert!(err(json!({"dest_node": 255}), &mut d).contains("dest_node=255: must be 1..254"));
    d.params = json!({"transport": "tcp"}).as_object().unwrap().clone();
    assert!(FinsDriver::new(&d).is_ok(), "TCP learns the node in the handshake");
}
