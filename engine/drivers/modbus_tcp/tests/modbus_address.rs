//! Port of tests/unit/test_modbus_address.py (Kepware Modbus Ethernet semantics).

use opencap_driver_modbus_tcp::{parse_address, ModbusParams};
use serde_json::json;

fn default() -> ModbusParams {
    ModbusParams::default()
}

fn params(v: serde_json::Value) -> ModbusParams {
    ModbusParams::from_map(v.as_object().unwrap()).unwrap()
}

#[test]
fn area_function_code_and_zero_based_address() {
    let cases = [
        ("400045", "float32", "holding", 3, 45, 44),
        ("40002", "float32", "holding", 3, 2, 1),
        ("400001", "uint16", "holding", 3, 1, 0),
        ("465536", "uint16", "holding", 3, 65536, 65535),
        ("300001", "int16", "input", 4, 1, 0),
        ("30010", "uint16", "input", 4, 10, 9),
        ("000139", "bool", "coil", 1, 139, 138),
        ("00205", "bool", "coil", 1, 205, 204),
        ("000001", "bool", "coil", 1, 1, 0),
        ("10001", "bool", "discrete", 2, 1, 0),
        ("100017", "bool", "discrete", 2, 17, 16),
    ];
    for (address, dtype, area, fc, reference, proto) in cases {
        let a = parse_address(address, dtype, &default()).unwrap();
        assert_eq!((a.area, a.fc, a.reference, a.address), (area, fc, reference, proto), "{address}");
        assert_eq!(a.dtype, dtype);
        assert_eq!(a.bit, None);
        assert!(a.dims.is_empty());
    }
}

#[test]
fn zero_based_off_uses_reference_as_protocol_address() {
    let p = ModbusParams { zero_based: false, ..default() };
    assert_eq!(parse_address("400045", "float32", &p).unwrap().address, 45);
    assert_eq!(parse_address("00001", "bool", &p).unwrap().address, 1);
    assert_eq!(parse_address("400000", "uint16", &p).unwrap().address, 0);
}

#[test]
fn params_accepts_a_plain_map_and_ignores_unknown_keys() {
    let p = params(json!({"zero_based": false, "unit_id": 3, "something_else": 1}));
    assert_eq!(parse_address("400045", "float32", &p).unwrap().address, 45);
    assert_eq!(parse_address("400045", "float32", &params(json!({}))).unwrap().address, 44);
    // hand-written YAML may say 0/1 for booleans; null means "default"
    let p = params(json!({"zero_based": 0, "max_holding": null}));
    assert!(!p.zero_based);
    assert_eq!(p.max_holding, 64);
}

#[test]
fn whitespace_is_stripped() {
    assert_eq!(parse_address("  400045 ", "float32", &default()).unwrap().address, 44);
}

#[test]
fn register_count_from_dtype() {
    for (dtype, count) in [
        ("int16", 1),
        ("uint16", 1),
        ("int32", 2),
        ("uint32", 2),
        ("float32", 2),
        ("int64", 4),
        ("uint64", 4),
        ("float64", 4),
    ] {
        assert_eq!(parse_address("400010", dtype, &default()).unwrap().count, count);
        assert_eq!(parse_address("300010", dtype, &default()).unwrap().count, count);
    }
}

#[test]
fn bool_on_coil_or_discrete_is_one_bit() {
    assert_eq!(parse_address("000010", "bool", &default()).unwrap().count, 1);
    assert_eq!(parse_address("100010", "bool", &default()).unwrap().count, 1);
}

#[test]
fn bit_in_register_zero_based_bit() {
    for (address, proto, bit) in
        [("40001.3", 0, 3), ("40001.11", 0, 11), ("400005.0", 4, 0), ("400005.15", 4, 15), ("300002.7", 1, 7)]
    {
        let a = parse_address(address, "bool", &default()).unwrap();
        assert_eq!((a.address, a.count, a.bit), (proto, 1, Some(bit)), "{address}");
    }
}

#[test]
fn bit_one_based_when_zero_based_bit_off() {
    let p = ModbusParams { zero_based_bit: false, ..default() };
    assert_eq!(parse_address("400001.1", "bool", &p).unwrap().bit, Some(0));
    assert_eq!(parse_address("400001.16", "bool", &p).unwrap().bit, Some(15));
    assert!(parse_address("400001.0", "bool", &p).unwrap_err().contains("bit"));
}

#[test]
fn modicon_bit_order_reverses_bits() {
    let p = ModbusParams { modicon_bit_order: true, ..default() };
    assert_eq!(parse_address("400001.0", "bool", &p).unwrap().bit, Some(15));
    assert_eq!(parse_address("400001.15", "bool", &p).unwrap().bit, Some(0));
    let p1 = ModbusParams { modicon_bit_order: true, zero_based_bit: false, ..default() };
    assert_eq!(parse_address("400001.1", "bool", &p1).unwrap().bit, Some(15));
    assert_eq!(parse_address("400001.16", "bool", &p1).unwrap().bit, Some(0));
}

#[test]
fn bit_out_of_range() {
    assert!(parse_address("400001.16", "bool", &default()).unwrap_err().contains("bit"));
}

#[test]
fn arrays() {
    let a = parse_address("400001[4]", "uint16", &default()).unwrap();
    assert_eq!((a.address, a.count, a.dims.clone()), (0, 4, vec![4]));
    let a = parse_address("400011[3]", "float32", &default()).unwrap();
    assert_eq!((a.address, a.count, a.dims.clone()), (10, 6, vec![3]));
    let a = parse_address("400001 [2][3]", "float32", &default()).unwrap();
    assert_eq!((a.count, a.dims.clone()), (12, vec![2, 3]));
    let a = parse_address("000001[16]", "bool", &default()).unwrap();
    assert_eq!((a.fc, a.address, a.count, a.dims.clone()), (1, 0, 16, vec![16]));
}

#[test]
fn strings_hilo_and_lohi() {
    let a = parse_address("400001.020H", "string", &default()).unwrap();
    assert_eq!((a.fc, a.address, a.count, a.str_len, a.str_order), (3, 0, 10, Some(20), Some("H")));
    let a = parse_address("300101.005l", "string", &default()).unwrap();
    assert_eq!((a.fc, a.address, a.count, a.str_len, a.str_order), (4, 100, 3, Some(5), Some("L")));
}

#[test]
fn invalid_addresses_raise_clear_errors() {
    let cases = [
        ("", "uint16", "empty"),
        ("abc", "uint16", "not a Modbus address"),
        ("4001", "uint16", "5 or 6 digits"),
        ("4000011", "uint16", "5 or 6 digits"),
        ("500001", "uint16", "prefix"),
        ("200001", "uint16", "prefix"),
        ("400000", "uint16", "reference 0"),
        ("465537", "uint16", "65536"),
        ("465536", "float32", "65535"),
        ("400001", "bcd", "dtype"),
        ("400001", "bool", "bit"),
        ("000001", "uint16", "bool"),
        ("100001", "float32", "bool"),
        ("000001.1", "bool", "register"),
        ("400001.3", "uint16", "bool"),
        ("400001.3[2]", "bool", "array"),
        ("400001[0]", "uint16", "array"),
        ("400001[2]", "bool", "bit"),
        ("400001", "string", "length"),
        ("400001.20", "string", "H or L"),
        ("400001.000H", "string", "length"),
        ("400001.251H", "string", "length"),
        ("400001.020H", "uint16", "string"),
        ("000001.020H", "string", "register"),
        ("400001.020H[2]", "string", "array"),
    ];
    for (address, dtype, needle) in cases {
        let err = parse_address(address, dtype, &default()).unwrap_err();
        assert!(err.contains(needle), "{address:?} {dtype}: {err}");
        if !address.is_empty() {
            assert!(err.contains(address), "message names the address: {err}");
        }
    }
}

#[test]
fn messages_are_pythons() {
    assert_eq!(
        parse_address("500001", "uint16", &default()).unwrap_err(),
        "modbus address '500001' (uint16): unknown area prefix '5' \
         (0 = coil, 1 = discrete input, 3 = input register, 4 = holding register)"
    );
    assert_eq!(
        parse_address("465536", "float32", &default()).unwrap_err(),
        "modbus address '465536' (float32): needs 2 registers from protocol address 65535, past the last address 65535"
    );
}

#[test]
fn bad_params_raise() {
    let err = ModbusParams::from_map(json!({"zero_based": "maybe"}).as_object().unwrap()).unwrap_err();
    assert_eq!(err, "modbus_tcp param zero_based='maybe': expected true or false");
    let err = ModbusParams::from_map(json!({"max_holding": 0}).as_object().unwrap()).unwrap_err();
    assert_eq!(err, "modbus_tcp param max_holding=0: must be >= 1");
    let err = ModbusParams::from_map(json!({"max_gap": -1}).as_object().unwrap()).unwrap_err();
    assert!(err.contains("max_gap"));
    let err = ModbusParams::from_map(json!({"max_input": 5.0}).as_object().unwrap()).unwrap_err();
    assert_eq!(err, "modbus_tcp param max_input=5.0: expected an integer");
    let err = ModbusParams::from_map(json!({"nonnormal_float": "nan"}).as_object().unwrap()).unwrap_err();
    assert_eq!(err, "modbus_tcp param nonnormal_float='nan': expected one of ('zero', 'unmodified')");
    let err = ModbusParams::from_map(json!({"unit_id": 256}).as_object().unwrap()).unwrap_err();
    assert_eq!(err, "modbus_tcp param unit_id=256: must be 0..255");
}
