"""MELSEC MC decoder: words as pymcprotocol returns them (signed int16) to tag values."""

from __future__ import annotations

import math
import struct

import pytest

from opencap.drivers.base import DecodeError
from opencap.drivers.melsec_mc import McParams, decode_block, parse_address, plan_blocks


def _one(address: str, dtype: str, words: list[int], **params):
    blocks, invalid = plan_blocks([("t", parse_address(address, dtype, params))], params)
    assert invalid == {} and len(blocks) == 1
    return decode_block(blocks[0], words, params)["t"]


def _signed(words: list[int]) -> list[int]:
    """pymcprotocol's batchread_wordunits returns signed 16-bit values."""
    return [w - 0x10000 if w & 0x8000 else w for w in words]


def _words_le(fmt: str, value) -> list[int]:
    raw = struct.pack("<" + fmt, value)
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def test_16_bit_values() -> None:
    assert _one("D0", "uint16", [-2]) == 65534
    assert _one("D0", "int16", [-2]) == -2
    assert _one("D0", "uint16", [0x7FFF]) == 0x7FFF
    assert _one("D0", "int16", [0xFFFF]) == -1                # unsigned input is accepted too


@pytest.mark.parametrize(("dtype", "fmt", "value"), [
    ("float32", "f", 12.5), ("float32", "f", -0.15625), ("int32", "i", -123456), ("uint32", "I", 0xDEADBEEF),
    ("float64", "d", 1234.5678), ("int64", "q", -(2 ** 40) - 5), ("uint64", "Q", 2 ** 63 + 7),
])
def test_multi_word_values_first_word_low(dtype: str, fmt: str, value) -> None:
    words = _words_le(fmt, value)                             # low word at the lower device number
    got = _one("D0000002", dtype, _signed(words))
    assert got == pytest.approx(value) if isinstance(value, float) else got == value
    flipped = _one("D0000002", dtype, _signed(list(reversed(words))), first_word_low=False)
    assert flipped == pytest.approx(value) if isinstance(value, float) else flipped == value


def test_first_word_low_matters() -> None:
    words = _words_le("I", 0x00010002)
    assert _one("D0", "uint32", words) == 0x00010002
    assert _one("D0", "uint32", words, first_word_low=False) == 0x00020001


@pytest.mark.parametrize("raw", [0x7FC00000, 0x7F800000, 0xFF800000, 0x00000001])
def test_nonnormal_floats(raw: int) -> None:
    words = [raw & 0xFFFF, raw >> 16]
    assert _one("D0", "float32", words) == 0.0
    got = _one("D0", "float32", words, nonnormal_float="unmodified")
    want = struct.unpack("<f", struct.pack("<I", raw))[0]
    assert (math.isnan(got) and math.isnan(want)) or got == want


def test_bits_in_words() -> None:
    assert _one("D100.05", "bool", [1 << 5]) is True
    assert _one("D100.05", "bool", [~(1 << 5) & 0xFFFF]) is False
    assert _one("D100.15", "bool", [-32768]) is True          # bit 15 = sign bit of the signed reply
    assert _one("D100.17", "bool", [0, 1 << 1]) is True       # bit 17 = bit 1 of the second (high) word
    assert _one("D100.17", "bool", [1 << 1, 0], first_word_low=False) is True


def test_bit_devices_lowest_device_is_bit_0() -> None:
    blocks, _ = plan_blocks([(n, parse_address(a, "bool")) for n, a in (("m0", "M0"), ("m1", "M1"), ("m17", "M17"),
                                                                        ("m31", "M31"))], {})
    out = decode_block(blocks[0], [0b01, -32768 | 0b10], {})
    assert out == {"m0": True, "m1": False, "m17": True, "m31": True}


def test_bit_device_read_as_words() -> None:
    assert _one("M16", "uint16", [-1]) == 0xFFFF
    assert _one("X0010", "int32", _words_le("i", -7)) == -7


def test_arrays() -> None:
    assert _one("D0[3]", "int16", [1, -2, 3]) == [1, -2, 3]
    words = _words_le("f", 1.0) + _words_le("f", 2.0) + _words_le("f", 3.0) + _words_le("f", 4.0)
    assert _one("D0[2][2]", "float32", words) == [[1.0, 2.0], [3.0, 4.0]]


def test_strings() -> None:
    hilo = [(ord("A") << 8) | ord("B"), (ord("C") << 8) | 0, 0x4142]
    assert _one("DSH0.006", "string", hilo) == "ABC"          # cut at the first NUL
    lohi = [ord("A") | (ord("B") << 8), ord("C") | (ord("D") << 8)]
    assert _one("DSL0.004", "string", lohi) == "ABCD"
    assert _one("DSL0.004", "string", [0xE9 | (0x41 << 8), 0]) == "\xe9A"   # latin-1


def test_short_reply_fails_only_the_items_it_cuts() -> None:
    parsed = [("a", parse_address("D0", "uint16")), ("b", parse_address("D2", "float32"))]
    blocks, _ = plan_blocks(parsed, {})
    out = decode_block(blocks[0], [7, 0], {})                  # 2 of 4 words
    assert out["a"] == 7
    assert isinstance(out["b"], DecodeError) and "short reply" in str(out["b"])


@pytest.mark.parametrize("bad", [70000, -40000, 1.5, True, None, "7"])
def test_garbage_words_are_decode_errors(bad) -> None:
    assert isinstance(_one("D0", "uint16", [bad]), DecodeError)


def test_each_name_gets_its_own_copy() -> None:
    parsed = [("a", parse_address("D0[2]", "uint16")), ("b", parse_address("D0[2]", "uint16"))]
    blocks, _ = plan_blocks(parsed, {})
    out = decode_block(blocks[0], [1, 2], McParams())
    assert out["a"] == out["b"] == [1, 2] and out["a"] is not out["b"]
