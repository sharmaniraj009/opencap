//! Port of tests/unit/test_modbus_decode.py: byte order, first word low, first dword low.
//!
//! Two Python cases are not ported because the type system rules them out: a register
//! value > 0xFFFF and a non-bool "bit" (the reply types are `&[u16]` / `&[bool]`).

use opencap_core::config::{DType, Device, Scale, Tag};
use opencap_core::value::Value;
use opencap_drivers::modbus_tcp::{decode_block, DecodeError, ModbusBlock, ModbusParams, ModbusTcpDriver, Reply};
use serde_json::{json, Value as Json};

fn one_block(tags: &[Tag], params: &Json) -> (ModbusBlock, ModbusParams) {
    let mut dev = Device::new("SITE/PLC", "SITE", "PLC", "modbus_tcp", "127.0.0.1", 502, tags.to_vec());
    dev.params = params.as_object().unwrap().clone();
    let d = ModbusTcpDriver::new(&dev).unwrap();
    let p = d.plan_tags(tags);
    assert!(p.invalid.is_empty(), "{:?}", p.invalid);
    assert_eq!(p.blocks.len(), 1);
    (p.blocks[0].as_any().downcast_ref::<ModbusBlock>().unwrap().clone(), d.params.clone())
}

fn regs(address: &str, dtype: DType, data: &[u16], params: Json) -> Result<Value, DecodeError> {
    let (b, p) = one_block(&[Tag::new("X", address, dtype)], &params);
    decode_block(&b, Reply::Registers(data), &p)["X"].clone()
}

fn reg(address: &str, dtype: DType, data: &[u16], params: Json) -> Value {
    regs(address, dtype, data, params).unwrap()
}

fn f(v: f64) -> Value {
    Value::Float(v)
}

fn i(v: i64) -> Value {
    Value::Int(v)
}

#[test]
fn uint16_and_int16() {
    assert_eq!(reg("400001", DType::Uint16, &[0xFFFF], json!({})), i(65535));
    assert_eq!(reg("400001", DType::Int16, &[0xFFFF], json!({})), i(-1));
    assert_eq!(reg("400001", DType::Int16, &[0x7FFF], json!({})), i(32767));
    assert_eq!(reg("300001", DType::Uint16, &[0x1234], json!({})), i(0x1234));
}

#[test]
fn byte_order_little_swaps_bytes_in_each_register() {
    assert_eq!(reg("400001", DType::Uint16, &[0x3412], json!({"byte_order_big": false})), i(0x1234));
    assert_eq!(reg("400001", DType::Int16, &[0xFFFE], json!({"byte_order_big": false})), i(0xFEFF - 0x10000));
}

#[test]
fn float32_word_orders() {
    assert_eq!(reg("400001", DType::Float32, &[0x0000, 0x3F80], json!({})), f(1.0));
    assert_eq!(reg("400001", DType::Float32, &[0x3F80, 0x0000], json!({"first_word_low": false})), f(1.0));
    assert_eq!(reg("400001", DType::Float32, &[0x0000, 0x803F], json!({"byte_order_big": false})), f(1.0));
}

#[test]
fn uint32_int32() {
    for (fwl, r) in [(true, [0x5678u16, 0x1234]), (false, [0x1234, 0x5678])] {
        assert_eq!(reg("400001", DType::Uint32, &r, json!({"first_word_low": fwl})), i(0x12345678));
        let neg = if fwl { [0xFFFE, 0xFFFF] } else { [0xFFFF, 0xFFFE] };
        assert_eq!(reg("400001", DType::Int32, &neg, json!({"first_word_low": fwl})), i(-2));
    }
}

#[test]
fn float32_real_value_roundtrip() {
    let raw = (123.4f32).to_bits();
    let got = reg("400045", DType::Float32, &[(raw & 0xFFFF) as u16, (raw >> 16) as u16], json!({}));
    assert_eq!(got, f(123.4f32 as f64));
}

#[test]
fn uint64_word_and_dword_order() {
    let v64: u64 = 0x1122334455667788;
    for (fwl, fdl, r) in [
        (true, true, [0x7788u16, 0x5566, 0x3344, 0x1122]),
        (false, false, [0x1122, 0x3344, 0x5566, 0x7788]),
        (true, false, [0x3344, 0x1122, 0x7788, 0x5566]),
        (false, true, [0x5566, 0x7788, 0x1122, 0x3344]),
    ] {
        let got = reg("400001", DType::Uint64, &r, json!({"first_word_low": fwl, "first_dword_low": fdl}));
        assert_eq!(got, Value::from_u64(v64), "{fwl} {fdl}");
    }
    assert_eq!(reg("400001", DType::Uint64, &[0xFFFF; 4], json!({})), Value::UInt(u64::MAX));
}

#[test]
fn int64_and_float64() {
    assert_eq!(reg("400001", DType::Int64, &[0xFFFE, 0xFFFF, 0xFFFF, 0xFFFF], json!({})), i(-2));
    assert_eq!(reg("400001", DType::Float64, &[0, 0, 0, 0x3FF0], json!({})), f(1.0));
    assert_eq!(
        reg("400001", DType::Float64, &[0x3FF0, 0, 0, 0], json!({"first_word_low": false, "first_dword_low": false})),
        f(1.0)
    );
}

#[test]
fn non_normalized_floats_replaced_with_zero() {
    for raw in [0x7FC00000u32, 0x7F800000, 0xFF800000, 0x00000001] {
        let r = [(raw & 0xFFFF) as u16, (raw >> 16) as u16];
        assert_eq!(reg("400001", DType::Float32, &r, json!({})), f(0.0));
        let Value::Float(kept) = reg("400001", DType::Float32, &r, json!({"nonnormal_float": "unmodified"})) else {
            panic!()
        };
        assert!(kept.is_nan() || kept.is_infinite() || (kept != 0.0 && kept.abs() < 1.2e-38));
    }
    assert_eq!(reg("400001", DType::Float64, &[0, 0, 0, 0x7FF8], json!({})), f(0.0));
    let Value::Float(z) = reg("400001", DType::Float32, &[0, 0x8000], json!({})) else { panic!() };
    assert!(z == 0.0 && z.is_sign_negative()); // -0.0 is a normal zero (the payload writes 0.0)
}

#[test]
fn bits_in_register() {
    let tags: Vec<Tag> =
        [0, 3, 4, 15].iter().map(|b| Tag::new(&format!("B{b}"), &format!("400001.{b}"), DType::Bool)).collect();
    let (b, p) = one_block(&tags, &json!({}));
    let out = decode_block(&b, Reply::Registers(&[0b1000_0000_0000_1001]), &p);
    let got: Vec<(String, Value)> = out.into_iter().map(|(k, v)| (k, v.unwrap())).collect();
    let mut got = got;
    got.sort_by(|a, b| a.0.cmp(&b.0));
    assert_eq!(
        got,
        vec![
            ("B0".into(), Value::Bool(true)),
            ("B15".into(), Value::Bool(true)),
            ("B3".into(), Value::Bool(true)),
            ("B4".into(), Value::Bool(false)),
        ]
    );
}

#[test]
fn bits_with_modicon_order_and_one_based() {
    let tags = [Tag::new("M0", "400001.0", DType::Bool), Tag::new("M15", "400001.15", DType::Bool)];
    let (b, p) = one_block(&tags, &json!({"modicon_bit_order": true}));
    let out = decode_block(&b, Reply::Registers(&[0x8000]), &p);
    assert_eq!(out["M0"], Ok(Value::Bool(true)));
    assert_eq!(out["M15"], Ok(Value::Bool(false)));
    let tags = [Tag::new("B1", "400001.1", DType::Bool), Tag::new("B16", "400001.16", DType::Bool)];
    let (b, p) = one_block(&tags, &json!({"zero_based_bit": false}));
    let out = decode_block(&b, Reply::Registers(&[0x0001]), &p);
    assert_eq!((out["B1"].clone(), out["B16"].clone()), (Ok(Value::Bool(true)), Ok(Value::Bool(false))));
}

#[test]
fn bit_uses_byte_order() {
    let (b, p) = one_block(&[Tag::new("B0", "400001.0", DType::Bool)], &json!({"byte_order_big": false}));
    assert_eq!(decode_block(&b, Reply::Registers(&[0x0100]), &p)["B0"], Ok(Value::Bool(true)));
}

#[test]
fn coils_and_discrete_inputs() {
    let (b, p) = one_block(&[Tag::new("C1", "000001", DType::Bool), Tag::new("C3", "000003", DType::Bool)], &json!({}));
    let bits = [true, true, false, false, false, false, false, false]; // padded to 8, as on the wire
    let out = decode_block(&b, Reply::Bits(&bits), &p);
    assert_eq!((out["C1"].clone(), out["C3"].clone()), (Ok(Value::Bool(true)), Ok(Value::Bool(false))));
    let (b, p) = one_block(&[Tag::new("D", "10001", DType::Bool)], &json!({}));
    assert_eq!(decode_block(&b, Reply::Bits(&[true]), &p)["D"], Ok(Value::Bool(true)));
}

#[test]
fn strings_hilo_lohi_nul_and_odd_length() {
    let s = |a: &str, d: &[u16]| reg(a, DType::String, d, json!({}));
    assert_eq!(s("400001.004H", &[0x4142, 0x4344]), Value::Str("ABCD".into()));
    assert_eq!(s("400001.004L", &[0x4142, 0x4344]), Value::Str("BADC".into()));
    assert_eq!(s("400001.004H", &[0x4142, 0x0043]), Value::Str("AB".into()));
    assert_eq!(s("400001.003H", &[0x4142, 0x4344]), Value::Str("ABC".into()));
    assert_eq!(s("400001.002H", &[0xB041]), Value::Str("°A".into())); // latin-1
}

#[test]
fn arrays() {
    let l = |v: Vec<Value>| Value::List(v);
    assert_eq!(reg("400001[3]", DType::Uint16, &[1, 2, 3], json!({})), l(vec![i(1), i(2), i(3)]));
    assert_eq!(reg("400001[2]", DType::Float32, &[0, 0x3F80, 0, 0x4000], json!({})), l(vec![f(1.0), f(2.0)]));
    assert_eq!(
        reg("400001[2][2]", DType::Int16, &[1, 2, 3, 0xFFFF], json!({})),
        l(vec![l(vec![i(1), i(2)]), l(vec![i(3), i(-1)])])
    );
    let (b, p) = one_block(&[Tag::new("X", "000001[4]", DType::Bool)], &json!({}));
    let bits = [true, false, true, true, false, false, false, false];
    let bv = |x| Value::Bool(x);
    assert_eq!(decode_block(&b, Reply::Bits(&bits), &p)["X"], Ok(l(vec![bv(true), bv(false), bv(true), bv(true)])));
}

#[test]
fn aliases_get_the_same_value_and_offsets_work() {
    let tags = [
        Tag::new("Y", "400133", DType::Float32),
        Tag::new("B", "400133", DType::Float32),
        Tag::new("W", "400136", DType::Uint16),
    ];
    let (b, p) = one_block(&tags, &json!({}));
    assert_eq!((b.start, b.count), (132, 4));
    let out = decode_block(&b, Reply::Registers(&[0, 0x3F80, 0, 7]), &p);
    assert_eq!((out["Y"].clone(), out["B"].clone(), out["W"].clone()), (Ok(f(1.0)), Ok(f(1.0)), Ok(i(7))));
}

#[test]
fn no_scaling_is_applied() {
    let tag = Tag {
        scale: Some(Scale { raw_hi: 10.0, eng_hi: 100.0, ..Default::default() }),
        ..Tag::new("S", "400001", DType::Uint16)
    };
    let (b, p) = one_block(&[tag], &json!({}));
    assert_eq!(decode_block(&b, Reply::Registers(&[5]), &p)["S"], Ok(i(5)));
}

#[test]
fn short_reply_fails_only_the_tags_past_the_end() {
    let tags = [Tag::new("A", "400001", DType::Uint16), Tag::new("F", "400002", DType::Float32)];
    let (b, p) = one_block(&tags, &json!({}));
    let out = decode_block(&b, Reply::Registers(&[42, 0]), &p);
    assert_eq!(out["A"], Ok(i(42)));
    let err = out["F"].clone().unwrap_err();
    assert!(err.0.contains("short"), "{}", err.0);
    assert_eq!(err.0, "400002 (float32): short reply: need registers up to offset 2, got 2");
}

#[test]
fn decode_keys_are_exactly_the_block_tags() {
    let tags: Vec<Tag> = (0..5).map(|n| Tag::new(&format!("R{n}"), &format!("4{:05}", n + 1), DType::Uint16)).collect();
    let (b, p) = one_block(&tags, &json!({}));
    let mut keys: Vec<String> = decode_block(&b, Reply::Registers(&[0; 5]), &p).keys().cloned().collect();
    keys.sort();
    assert_eq!(keys, b.tags);
}
