"""Modbus address parser (PLAN.md §5.1, Kepware Modbus Ethernet semantics)."""

from __future__ import annotations

import pytest

from opencap.drivers.modbus_tcp import ModbusAddress, ModbusParams, parse_address

DEFAULT = ModbusParams()


# --- areas and function codes ------------------------------------------------

@pytest.mark.parametrize(
    "address, dtype, area, fc, ref, proto",
    [
        ("400045", "float32", "holding", 3, 45, 44),     # 6-digit holding
        ("40002", "float32", "holding", 3, 2, 1),        # 5-digit holding
        ("400001", "uint16", "holding", 3, 1, 0),
        ("465536", "uint16", "holding", 3, 65536, 65535),
        ("300001", "int16", "input", 4, 1, 0),           # 6-digit input register
        ("30010", "uint16", "input", 4, 10, 9),          # 5-digit input register
        ("000139", "bool", "coil", 1, 139, 138),         # 6-digit coil
        ("00205", "bool", "coil", 1, 205, 204),          # 5-digit coil
        ("000001", "bool", "coil", 1, 1, 0),
        ("10001", "bool", "discrete", 2, 1, 0),          # 5-digit discrete input
        ("100017", "bool", "discrete", 2, 17, 16),       # 6-digit discrete input
    ],
)
def test_area_function_code_and_zero_based_address(address, dtype, area, fc, ref, proto):
    a = parse_address(address, dtype, DEFAULT)
    assert isinstance(a, ModbusAddress)
    assert (a.area, a.fc, a.ref, a.address) == (area, fc, ref, proto)
    assert a.dtype == dtype
    assert a.bit is None
    assert a.dims == ()


def test_zero_based_off_uses_reference_as_protocol_address():
    p = ModbusParams(zero_based=False)
    assert parse_address("400045", "float32", p).address == 45
    assert parse_address("00001", "bool", p).address == 1
    # with zero_based OFF, reference 0 is a valid protocol address 0
    assert parse_address("400000", "uint16", p).address == 0


def test_params_accepts_a_plain_dict_and_ignores_unknown_keys():
    a = parse_address("400045", "float32", {"zero_based": False, "unit_id": 3, "something_else": 1})
    assert a.address == 45
    assert parse_address("400045", "float32", {}).address == 44   # documented defaults


def test_whitespace_is_stripped():
    assert parse_address("  400045 ", "float32", DEFAULT).address == 44


# --- register counts from dtype -------------------------------------------------

@pytest.mark.parametrize(
    "dtype, count",
    [
        ("int16", 1), ("uint16", 1),
        ("int32", 2), ("uint32", 2), ("float32", 2),
        ("int64", 4), ("uint64", 4), ("float64", 4),
    ],
)
def test_register_count_from_dtype(dtype, count):
    assert parse_address("400010", dtype, DEFAULT).count == count
    assert parse_address("300010", dtype, DEFAULT).count == count


def test_bool_on_coil_or_discrete_is_one_bit():
    assert parse_address("000010", "bool", DEFAULT).count == 1
    assert parse_address("100010", "bool", DEFAULT).count == 1


# --- bit in register (.b) ----------------------------------------------------

@pytest.mark.parametrize(
    "address, proto, bit",
    [
        ("40001.3", 0, 3),       # 5-digit, one-digit bit
        ("40001.11", 0, 11),     # 5-digit, two-digit bit
        ("400005.0", 4, 0),      # 6-digit
        ("400005.15", 4, 15),
        ("300002.7", 1, 7),      # bit in an input register
    ],
)
def test_bit_in_register_zero_based_bit(address, proto, bit):
    a = parse_address(address, "bool", DEFAULT)
    assert a.address == proto
    assert a.count == 1
    assert a.bit == bit


def test_bit_one_based_when_zero_based_bit_off():
    p = ModbusParams(zero_based_bit=False)
    assert parse_address("400001.1", "bool", p).bit == 0
    assert parse_address("400001.16", "bool", p).bit == 15
    with pytest.raises(ValueError, match="bit"):
        parse_address("400001.0", "bool", p)


def test_modicon_bit_order_reverses_bits():
    p = ModbusParams(modicon_bit_order=True)
    assert parse_address("400001.0", "bool", p).bit == 15
    assert parse_address("400001.15", "bool", p).bit == 0
    p1 = ModbusParams(modicon_bit_order=True, zero_based_bit=False)
    assert parse_address("400001.1", "bool", p1).bit == 15
    assert parse_address("400001.16", "bool", p1).bit == 0


def test_bit_out_of_range():
    with pytest.raises(ValueError, match="bit"):
        parse_address("400001.16", "bool", DEFAULT)


# --- arrays ------------------------------------------------------------------

def test_one_dimensional_array():
    a = parse_address("400001[4]", "uint16", DEFAULT)
    assert (a.address, a.count, a.dims) == (0, 4, (4,))
    a = parse_address("400011[3]", "float32", DEFAULT)
    assert (a.address, a.count, a.dims) == (10, 6, (3,))


def test_two_dimensional_array_and_spaces():
    a = parse_address("400001 [2][3]", "float32", DEFAULT)
    assert (a.count, a.dims) == (12, (2, 3))


def test_bool_array_on_coils():
    a = parse_address("000001[16]", "bool", DEFAULT)
    assert (a.fc, a.address, a.count, a.dims) == (1, 0, 16, (16,))


# --- strings (Kepware 4xxxxx.LLLH / .LLLL) -----------------------------------

def test_string_hilo_and_lohi():
    a = parse_address("400001.020H", "string", DEFAULT)
    assert (a.fc, a.address, a.count, a.str_len, a.str_order) == (3, 0, 10, 20, "H")
    a = parse_address("300101.005l", "string", DEFAULT)      # odd length rounds up, case-insensitive
    assert (a.fc, a.address, a.count, a.str_len, a.str_order) == (4, 100, 3, 5, "L")


# --- invalid input: clear ValueError ------------------------------------------

@pytest.mark.parametrize(
    "address, dtype, match",
    [
        ("", "uint16", "empty"),
        ("abc", "uint16", "not a Modbus address"),
        ("4001", "uint16", "5 or 6 digits"),
        ("4000011", "uint16", "5 or 6 digits"),
        ("500001", "uint16", "prefix"),
        ("200001", "uint16", "prefix"),
        ("400000", "uint16", "reference 0"),             # reference 0 with zero-based ON
        ("465537", "uint16", "65536"),
        ("465536", "float32", "65535"),                 # second register past the end
        ("400001", "bcd", "dtype"),
        ("400001", "bool", "bit"),                       # bool on a register needs .b
        ("000001", "uint16", "bool"),                    # coils are bool only
        ("100001", "float32", "bool"),
        ("000001.1", "bool", "register"),                # no bits inside a coil
        ("400001.3", "uint16", "bool"),                  # .b is bool only
        ("400001.3[2]", "bool", "array"),
        ("400001[0]", "uint16", "array"),
        ("400001[2]", "bool", "bit"),                    # bool array on registers not allowed
        ("400001", "string", "length"),
        ("400001.20", "string", "H or L"),
        ("400001.000H", "string", "length"),
        ("400001.251H", "string", "length"),
        ("400001.020H", "uint16", "string"),
        ("000001.020H", "string", "register"),
        ("400001.020H[2]", "string", "array"),
    ],
)
def test_invalid_addresses_raise_clear_value_error(address, dtype, match):
    with pytest.raises(ValueError, match=match) as info:
        parse_address(address, dtype, DEFAULT)
    if address:
        assert address in str(info.value)   # the message names the bad address


def test_bad_params_raise():
    with pytest.raises(ValueError, match="zero_based"):
        parse_address("400001", "uint16", {"zero_based": "maybe"})
