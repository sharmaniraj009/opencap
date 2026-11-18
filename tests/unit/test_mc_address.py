"""MELSEC MC address parser: Kepware "Mitsubishi Ethernet" Q Series syntax (docs/decisions.md D-053).

One test per address form used or allowed by the KEPServer project: word devices (`D0000002`),
bit devices (`B0000`), hex vs decimal numbering, bits in words, bit devices read as words,
arrays, strings, and the rejected forms.
"""

from __future__ import annotations

import pytest

from opencap.drivers.melsec_mc import (
    DEVICES,
    McParams,
    device_string,
    parse_address,
)


def test_word_device_float32_from_the_project() -> None:
    a = parse_address("D0000002", "float32")
    assert (a.device, a.kind, a.number, a.start, a.words, a.bit) == ("D", "word", 2, 2, 2, None)
    assert a.text == "D0000002"


@pytest.mark.parametrize(("dtype", "words"), [
    ("uint16", 1), ("int16", 1), ("int32", 2), ("uint32", 2), ("float32", 2),
    ("int64", 4), ("uint64", 4), ("float64", 4),
])
def test_word_device_dtype_widths(dtype: str, words: int) -> None:
    assert parse_address("D100", dtype).words == words


def test_bit_device_from_the_project_is_hex() -> None:
    a = parse_address("B0000", "bool")
    assert (a.device, a.kind, a.number, a.start, a.words, a.bit) == ("B", "bit", 0, 0, 1, 0)
    b = parse_address("B001F", "bool")                      # hex: B31 = word 1, bit 15
    assert (b.number, b.start, b.bit) == (0x1F, 1, 15)
    c = parse_address("BEA60", "bool")                      # last B in Kepware's Q table
    assert c.number == 0xEA60


@pytest.mark.parametrize(("address", "device", "number"), [
    ("X1A", "X", 0x1A), ("XFF", "X", 0xFF), ("X0010", "X", 0x10), ("Y3FFF", "Y", 0x3FFF),
    ("DX10", "DX", 0x10), ("DY7", "DY", 7), ("SB10", "SB", 0x10),
])
def test_hex_bit_devices(address: str, device: str, number: int) -> None:
    a = parse_address(address, "bool")
    assert (a.device, a.kind, a.number, a.start, a.bit) == (device, "bit", number, number // 16, number % 16)


@pytest.mark.parametrize(("address", "device"), [
    ("M10", "M"), ("SM400", "SM"), ("L5", "L"), ("F3", "F"), ("V2", "V"),
    ("TS1", "TS"), ("TC1", "TC"), ("SS1", "SS"), ("SC1", "SC"), ("CS1", "CS"), ("CC1", "CC"),
])
def test_decimal_bit_devices(address: str, device: str) -> None:
    a = parse_address(address, "bool")
    assert a.device == device and a.kind == "bit"
    assert a.number == int(address[len(device):])


@pytest.mark.parametrize(("address", "device", "number"), [
    ("D4184063", "D", 4184063), ("SD100", "SD", 100), ("R32767", "R", 32767),
    ("TN5", "TN", 5), ("SN5", "SN", 5), ("CN5", "CN", 5),
    ("W1F", "W", 0x1F), ("SW10", "SW", 0x10), ("ZR10", "ZR", 0x10),   # W, SW and ZR are hex (Kepware)
])
def test_word_devices_and_their_radix(address: str, device: str, number: int) -> None:
    a = parse_address(address, "uint16")
    assert (a.device, a.kind, a.number, a.start, a.words) == (device, "word", number, number, 1)


def test_lowercase_and_spaces_are_accepted() -> None:
    a = parse_address("  d0000002 ", "float32")
    assert (a.device, a.number) == ("D", 2)
    assert a.text == "d0000002"


def test_bit_in_a_word_device() -> None:
    a = parse_address("D100.05", "bool")
    assert (a.start, a.words, a.bit) == (100, 1, 5)
    b = parse_address("D100.5", "bool")
    assert a.key == b.key                                   # same data, different spelling
    c = parse_address("W1F.15", "bool")
    assert (c.number, c.words, c.bit) == (0x1F, 1, 15)
    d = parse_address("D100.17", "bool")                    # bits 16..31: bit of the 2-word value
    assert (d.words, d.bit) == (2, 17)
    for addr in ("R0.31", "ZR0.0", "SD10.3", "SW0.2"):
        assert parse_address(addr, "bool").kind == "word"


def test_bit_device_read_as_words_on_a_16_point_boundary() -> None:
    a = parse_address("M16", "uint16")
    assert (a.kind, a.start, a.words, a.bit) == ("bit", 1, 1, None)
    b = parse_address("X0010", "int32")                     # X10 hex = 16: words 1..2
    assert (b.start, b.words) == (1, 2)
    with pytest.raises(ValueError, match="16-point boundary"):
        parse_address("M17", "uint16")
    with pytest.raises(ValueError, match="16-point boundary"):
        parse_address("X0011", "uint16")


def test_arrays_on_word_devices() -> None:
    a = parse_address("D100[4]", "int16")
    assert (a.dims, a.words) == ((4,), 4)
    b = parse_address("D100 [2][3]", "float32")
    assert (b.dims, b.words) == ((2, 3), 12)
    with pytest.raises(ValueError, match="bool arrays"):
        parse_address("D100.1[4]", "bool")
    with pytest.raises(ValueError, match="arrays are not supported on bit devices"):
        parse_address("M0[4]", "uint16")
    with pytest.raises(ValueError, match=">= 1"):
        parse_address("D100[0]", "int16")


def test_strings_hilo_and_lohi() -> None:
    a = parse_address("DSH100.020", "string")
    assert (a.device, a.start, a.words, a.str_len, a.str_order) == ("D", 100, 10, 20, "H")
    b = parse_address("DSL100:020", "string")              # Kepware allows ':' for the length
    assert (b.str_order, b.str_len) == ("L", 20)
    for addr, dev in (("WSH1F.004", "W"), ("RSL0.002", "R"), ("ZRSH10.128", "ZR")):
        s = parse_address(addr, "string")
        assert s.device == dev
    assert parse_address("WSH1F.004", "string").number == 0x1F
    with pytest.raises(ValueError, match="2..128 and even"):
        parse_address("DSH100.003", "string")
    with pytest.raises(ValueError, match="2..128 and even"):
        parse_address("DSH100.130", "string")
    with pytest.raises(ValueError, match="needs a length"):
        parse_address("DSH100", "string")
    with pytest.raises(ValueError, match="needs dtype string"):
        parse_address("DSH100.020", "uint16")
    with pytest.raises(ValueError, match="DSH100.020"):
        parse_address("D100", "string")                     # hint at the string form


@pytest.mark.parametrize(("address", "dtype", "reason"), [
    ("D100", "bool", "needs a .bit suffix"),
    ("D100.32", "bool", "bit .32 out of range 0..31"),
    ("D100.5", "uint16", "must have dtype bool"),
    ("M10.1", "bool", "only valid on word devices"),
    ("M16", "float32", "16/32-bit integers"),
    ("TN0", "float32", "16-bit values only"),
    ("TN0.1", "bool", "bit access is not supported on TN"),
    ("CS0000 L", "int32", "' L' suffix"),
    ("S0", "bool", "unknown device"),
    ("K4M0", "uint16", "unknown device"),
    ("Z0", "uint16", "unknown device"),
    ("D", "uint16", "unknown device"),
    ("D1G", "uint16", "unknown device"),
    ("D4184064", "uint16", "past the last D device"),
    ("D4184063", "float32", "past the last D device"),
    ("X4000", "bool", "past the last X device"),
    ("M59985", "uint16", "16-point boundary"),
    ("M60000", "uint16", "past the last M device"),
    ("D100:5", "bool", "':' is only valid in string addresses"),
    ("", "uint16", "empty address"),
    ("D100", "char", "unknown dtype"),
])
def test_rejected_addresses(address: str, dtype: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_address(address, dtype)


def test_every_device_in_the_table_parses() -> None:
    for name, spec in DEVICES.items():
        dtype = "bool" if spec.kind == "bit" else "uint16"
        digits = "10"
        a = parse_address(f"{name}{digits}", dtype)
        assert a.device == name and a.number == int(digits, spec.base)


def test_pymcprotocol_device_strings() -> None:
    assert device_string("D", 2) == "D2"
    assert device_string("X", 0x1A) == "X01A"               # hex devices: leading 0 for pymcprotocol
    assert device_string("B", 0xEA60) == "B0EA60"
    assert device_string("ZR", 16) == "ZR010"
    assert device_string("SS", 5) == "STS5"                 # Kepware SS/SC/SN = pymcprotocol STS/STC/STN
    assert device_string("SC", 5) == "STC5"
    assert device_string("SN", 5) == "STN5"


def test_pymcprotocol_parses_our_device_strings() -> None:
    """The strings we hand to pymcprotocol decode to the number we parsed (binary 3E, Q and iQ-R)."""
    pymcprotocol = pytest.importorskip("pymcprotocol")
    for plc_type in ("Q", "iQ-R"):
        client = pymcprotocol.Type3E(plc_type)
        for name, spec in DEVICES.items():
            for number in (0, 0x1A if spec.base == 16 else 26, 4000):
                data = client._make_devicedata(device_string(name, number))
                width = 4 if plc_type == "iQ-R" else 3
                assert int.from_bytes(data[:width], "little") == number, (plc_type, name, number)


def test_params_validation() -> None:
    assert McParams.from_mapping({"plc_type": "Q", "protocol": "tcp", "first_word_low": True, "frame": "3E"}) == McParams()
    assert McParams.from_mapping({"first_word_low": 0}).first_word_low is False
    assert McParams.from_mapping({"unknown_key": 1, "plc_type": None}).plc_type == "Q"
    for bad in ({"plc_type": "A"}, {"protocol": "serial"}, {"frame": "4E"}, {"max_words": 961},
                {"max_words": 0}, {"max_gap": -1}, {"first_word_low": "yes"}, {"nonnormal_float": "nan"},
                {"network": 256}, {"pc": -1}, {"dest_moduleio": 0x10000}, {"max_words": True}):
        with pytest.raises(ValueError):
            McParams.from_mapping(bad)
