"""S7 decoder: block bytes -> tag values. Expected values are encoded here with `struct`,
independently of the driver (big-endian S7 default, `byte_order_big=false` = every byte of a
16/32/64-bit value reversed, Kepware "Little Endian")."""

from __future__ import annotations

import math
import struct

import pytest

from opencap.drivers.base import DecodeError
from opencap.drivers.s7 import S7Params, bcd_to_int, decode_block, parse_s7_address, plan_blocks, s5time_to_ms


def one_block(specs, **params):
    p = S7Params.from_mapping(params)
    blocks, invalid = plan_blocks([(n, parse_s7_address(a, d, p)) for n, a, d in specs], p)
    assert invalid == {} and len(blocks) == 1, (invalid, blocks)
    return blocks[0], p


def decode(specs, data: bytes, **params):
    block, p = one_block(specs, **params)
    return decode_block(block, data, p)


def test_big_endian_numbers() -> None:
    data = (struct.pack(">f", 12.5) + struct.pack(">I", 0xDEADBEEF) + struct.pack(">i", -7)
            + struct.pack(">H", 0xFFFE) + struct.pack(">h", -2) + bytes([0xFF, 0x80])
            + struct.pack(">d", -1.25) + struct.pack(">q", -(2**40)) + struct.pack(">Q", 2**63 + 5))
    specs = [("real", "DB1,REAL0", "float32"), ("dword_f", "DB1,D0", "float32"),
             ("dword", "DB1,D4", "uint32"), ("dint", "DB1,DI8", "int32"),
             ("word", "DB1,W12", "uint16"), ("int", "DB1,I14", "int16"),
             ("byte", "DB1,B16", "uint16"), ("char", "DB1,C16", "int16"), ("byte80_signed", "DB1,B17", "int16"),
             ("lreal", "DB1,LREAL18", "float64"), ("lint", "DB1,LINT26", "int64"), ("lword", "DB1,LWORD34", "uint64")]
    out = decode(specs, data)
    assert out == {"real": 12.5, "dword_f": 12.5, "dword": 0xDEADBEEF, "dint": -7, "word": 0xFFFE, "int": -2,
                   "byte": 255, "char": -1, "byte80_signed": -128, "lreal": -1.25, "lint": -(2**40),
                   "lword": 2**63 + 5}
    assert type(out["word"]) is int and type(out["real"]) is float


def test_little_endian_reverses_every_byte_of_a_value() -> None:
    data = (struct.pack("<f", 3.75) + struct.pack("<I", 0x01020304) + struct.pack("<H", 0x0A0B)
            + struct.pack("<h", -300) + struct.pack("<d", 6.5) + bytes([0x7F]))
    specs = [("real", "MREAL0", "float32"), ("dword", "MD4", "uint32"), ("word", "MW8", "uint16"),
             ("int", "MI10", "int16"), ("lreal", "MLREAL12", "float64"), ("byte", "MB20", "uint16")]
    out = decode(specs, data, byte_order_big=False)
    assert out == {"real": 3.75, "dword": 0x01020304, "word": 0x0A0B, "int": -300, "lreal": 6.5, "byte": 0x7F}
    big = decode(specs, data, byte_order_big=True)
    assert big["dword"] == 0x04030201 and big["word"] == 0x0B0A


def test_bits() -> None:
    # byte 0 = 0b1000_0001, word at 2 = 0x8001, dword at 4 = 0x8000_0001
    data = bytes([0x81, 0x00]) + struct.pack(">H", 0x8001) + struct.pack(">I", 0x80000001)
    specs = [("x0_0", "DB1,X0.0", "bool"), ("x0_7", "DB1,X0.7", "bool"), ("x0_1", "DB1,DBX0.1", "bool"),
             ("b0_7", "DB1,B0.7", "bool"), ("x1_0", "DB1,X1.0", "bool"),
             ("w15", "DB1,W2.15", "bool"), ("w0", "DB1,W2.0", "bool"), ("w8", "DB1,W2.8", "bool"),
             ("d31", "DB1,D4.31", "bool"), ("d0", "DB1,D4.0", "bool"), ("d16", "DB1,D4.16", "bool")]
    out = decode(specs, data)
    assert out == {"x0_0": True, "x0_7": True, "x0_1": False, "b0_7": True, "x1_0": False,
                   "w15": True, "w0": True, "w8": False, "d31": True, "d0": True, "d16": False}
    assert all(type(v) is bool for v in out.values())


def test_bit_of_a_word_follows_the_byte_order() -> None:
    data = bytes([0x00, 0x80])                     # little endian: word value 0x8000 -> bit 15
    out = decode([("w15", "MW0.15", "bool"), ("w7", "MW0.7", "bool")], data, byte_order_big=False)
    assert out == {"w15": True, "w7": False}
    out = decode([("w15", "MW0.15", "bool"), ("w7", "MW0.7", "bool")], data, byte_order_big=True)
    assert out == {"w15": False, "w7": True}       # big endian: 0x0080 -> bit 7


def test_arrays() -> None:
    data = struct.pack(">6f", 1, 2, 3, 4, 5, 6)
    out = decode([("flat", "DB2,REAL0[6]", "float32"), ("grid", "DB2,REAL0[2][3]", "float32")], data)
    assert out["flat"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert out["grid"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    out["flat"].append(7.0)                         # each name gets its own list
    out2 = decode([("a", "MW0[3]", "int16"), ("b", "MW0[3]", "int16")], struct.pack(">3h", -1, 0, 1))
    out2["a"][0] = 99
    assert out2["b"] == [-1, 0, 1]


@pytest.mark.parametrize(("word", "ms"), [
    (0x0000, 0), (0x0123, 1230), (0x1123, 12_300), (0x2999, 999_000), (0x3999, 9_990_000),
    (0x3001, 10_000), (0xC123, 1230),              # bits 14-15 are not part of S5TIME
])
def test_s5time(word: int, ms: int) -> None:
    assert s5time_to_ms(word) == ms


@pytest.mark.parametrize("word", [0x00A0, 0x0F00, 0x000B])
def test_s5time_bad_bcd(word: int) -> None:
    with pytest.raises(ValueError, match="BCD"):
        s5time_to_ms(word)


def test_bcd() -> None:
    assert bcd_to_int(0x0999) == 999 and bcd_to_int(0x0042) == 42 and bcd_to_int(0x9999) == 9999
    with pytest.raises(ValueError, match="BCD"):
        bcd_to_int(0x00A1)


def test_timers_and_counters() -> None:
    out = decode([("c0", "C0", "uint16"), ("c1", "C1", "int16"), ("c2", "C2", "uint16")],
                 struct.pack(">3H", 0x0999, 0x0042, 0x00AF))
    assert out["c0"] == 999 and out["c1"] == 42
    assert isinstance(out["c2"], DecodeError) and "BCD" in out["c2"].reason
    out = decode([("t0", "T0", "uint32"), ("t1", "T1", "int32")], struct.pack(">2H", 0x2010, 0x1250))
    assert out == {"t0": 10_000, "t1": 25_000}


def test_timers_ignore_the_byte_order_setting() -> None:
    out = decode([("t0", "T0", "uint32")], struct.pack(">H", 0x2010), byte_order_big=False)
    assert out["t0"] == 10_000
    block, p = one_block([("c0", "C0", "uint16")], byte_order_big=False)
    assert decode_block(block, struct.pack(">H", 0x0123), p) == {"c0": 123}


def test_strings() -> None:
    s7 = bytes([20, 5]) + b"HELLO" + b"OLD TEXT" + bytes(7)          # STRING[20], actual length 5, stale tail
    raw = b"AB\x00CD"
    out = decode([("s", "DB1,STRING0.20", "string"), ("raw", "DB1,B22.5", "string")], s7 + raw)
    assert out == {"s": "HELLO", "raw": "AB"}
    clamp = bytes([4, 9]) + b"ABCD"                                  # actual length > maximum: cut at the maximum
    assert decode([("s", "DB1,STRING0.4", "string")], clamp) == {"s": "ABCD"}
    latin = bytes([3, 3]) + "é°C".encode("latin-1")
    assert decode([("s", "DB1,STRING0.3", "string")], latin) == {"s": "é°C"}


@pytest.mark.parametrize("bits", [0x7FC00000, 0x7F800000, 0xFF800000, 0x00000001])
def test_nonnormal_floats(bits: int) -> None:
    data = struct.pack(">I", bits)
    assert decode([("f", "MD0", "float32")], data) == {"f": 0.0}                    # Kepware "Replace with Zero"
    out = decode([("f", "MD0", "float32")], data, nonnormal_float="unmodified")
    want = struct.unpack(">f", data)[0]
    assert (math.isnan(out["f"]) and math.isnan(want)) or out["f"] == want


def test_nonnormal_double() -> None:
    data = struct.pack(">d", math.inf)
    assert decode([("f", "DB1,LREAL0", "float64")], data) == {"f": 0.0}
    assert decode([("f", "DB1,LREAL0", "float64")], data, nonnormal_float="unmodified") == {"f": math.inf}


def test_short_reply_fails_only_the_items_it_cuts() -> None:
    specs = [("a", "DB1,W0", "uint16"), ("b", "DB1,W10", "uint16")]
    block, p = one_block(specs)
    out = decode_block(block, struct.pack(">H", 7) + bytes(4), p)
    assert out["a"] == 7
    assert isinstance(out["b"], DecodeError) and "short reply" in out["b"].reason
