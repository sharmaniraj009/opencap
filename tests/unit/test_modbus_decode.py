"""Modbus register decode (PLAN.md §5.1): byte order, first word low, first dword low."""

from __future__ import annotations

import math
import struct

import pytest

from opencap.config.models import Device, Scale, Tag
from opencap.drivers.modbus_tcp import DecodeError, ModbusBlock, ModbusTcpDriver, decode_block


def one_block(tags: list[Tag], **params) -> ModbusBlock:
    dev = Device(
        id="SITE/PLC", channel="SITE", name="PLC", driver="modbus_tcp",
        host="127.0.0.1", port=502, params=params, tags=tags,
    )
    p = ModbusTcpDriver(dev).plan(tags)
    assert not p.invalid, p.invalid
    assert len(p.blocks) == 1, [b.describe() for b in p.blocks]
    return p.blocks[0]


def decode_one(address: str, dtype: str, data, **params):
    block = one_block([Tag(name="X", address=address, dtype=dtype)], **params)
    return decode_block(block, data, params)["X"]


def swap(w: int) -> int:
    return ((w & 0xFF) << 8) | (w >> 8)


# --- 16-bit ------------------------------------------------------------------------

def test_uint16_and_int16():
    assert decode_one("400001", "uint16", [0xFFFF]) == 65535
    assert decode_one("400001", "int16", [0xFFFF]) == -1
    assert decode_one("400001", "int16", [0x7FFF]) == 32767
    assert decode_one("300001", "uint16", [0x1234]) == 0x1234


def test_byte_order_little_swaps_bytes_in_each_register():
    assert decode_one("400001", "uint16", [0x3412], byte_order_big=False) == 0x1234
    assert decode_one("400001", "int16", [0xFFFE], byte_order_big=False) == swap(0xFFFE) - 0x10000


# --- 32-bit: first_word_low ----------------------------------------------------------

def test_float32_first_word_low_default():
    # 1.0f = 0x3F800000; Kepware default: first register holds the low word
    assert decode_one("400001", "float32", [0x0000, 0x3F80]) == 1.0
    assert decode_one("400001", "float32", [0x0000, 0x3F80], first_word_low=True) == 1.0


def test_float32_first_word_high():
    assert decode_one("400001", "float32", [0x3F80, 0x0000], first_word_low=False) == 1.0


def test_float32_byte_swapped():
    assert decode_one("400001", "float32", [0x0000, 0x803F], byte_order_big=False) == 1.0


@pytest.mark.parametrize("fwl, regs", [(True, [0x5678, 0x1234]), (False, [0x1234, 0x5678])])
def test_uint32_int32(fwl, regs):
    assert decode_one("400001", "uint32", regs, first_word_low=fwl) == 0x12345678
    neg = [0xFFFE, 0xFFFF]                          # -2 is symmetric enough to test sign only
    assert decode_one("400001", "int32", neg if fwl else neg[::-1], first_word_low=fwl) == -2


def test_float32_real_value_roundtrip():
    raw = struct.unpack(">I", struct.pack(">f", 123.4))[0]
    lo, hi = raw & 0xFFFF, raw >> 16
    got = decode_one("400045", "float32", [lo, hi])
    assert got == struct.unpack(">f", struct.pack(">f", 123.4))[0]
    assert isinstance(got, float)


# --- 64-bit: first_dword_low × first_word_low ---------------------------------------------

V64 = 0x1122334455667788


@pytest.mark.parametrize(
    "fwl, fdl, regs",
    [
        (True, True, [0x7788, 0x5566, 0x3344, 0x1122]),
        (False, False, [0x1122, 0x3344, 0x5566, 0x7788]),
        (True, False, [0x3344, 0x1122, 0x7788, 0x5566]),   # high dword first, low word first in each
        (False, True, [0x5566, 0x7788, 0x1122, 0x3344]),   # low dword first, high word first in each
    ],
)
def test_uint64_word_and_dword_order(fwl, fdl, regs):
    assert decode_one("400001", "uint64", regs, first_word_low=fwl, first_dword_low=fdl) == V64


def test_int64_and_float64():
    assert decode_one("400001", "int64", [0xFFFE, 0xFFFF, 0xFFFF, 0xFFFF]) == -2
    # 1.0 = 0x3FF0000000000000, default first word low + first dword low
    assert decode_one("400001", "float64", [0, 0, 0, 0x3FF0]) == 1.0
    assert decode_one("400001", "float64", [0x3FF0, 0, 0, 0], first_word_low=False, first_dword_low=False) == 1.0


def test_matches_pymodbus_converter_for_big_and_fully_reversed_orders():
    mixin = pytest.importorskip("pymodbus.client.mixin").ModbusClientMixin
    DT = mixin.DATATYPE
    cases = [("float32", DT.FLOAT32, 2), ("int32", DT.INT32, 2), ("uint32", DT.UINT32, 2),
             ("float64", DT.FLOAT64, 4), ("int64", DT.INT64, 4), ("uint64", DT.UINT64, 4)]
    regs4 = [0x4093, 0x4A45, 0x8000, 0x1234]
    for dtype, dt, n in cases:
        regs = regs4[:n]
        big = mixin.convert_from_registers(regs, dt, word_order="big")
        little = mixin.convert_from_registers(regs, dt, word_order="little")
        assert decode_one("400001", dtype, regs, first_word_low=False, first_dword_low=False) == big
        assert decode_one("400001", dtype, regs, first_word_low=True, first_dword_low=True) == little


# --- non-normalized floats (Kepware channel default: Replace with Zero) ----------------------

@pytest.mark.parametrize("raw", [0x7FC00000, 0x7F800000, 0xFF800000, 0x00000001])
def test_non_normalized_float32_replaced_with_zero(raw):
    regs = [raw & 0xFFFF, raw >> 16]
    assert decode_one("400001", "float32", regs) == 0.0
    kept = decode_one("400001", "float32", regs, nonnormal_float="unmodified")
    assert math.isnan(kept) or math.isinf(kept) or 0 < abs(kept) < 1.2e-38


def test_non_normalized_float64_replaced_with_zero():
    assert decode_one("400001", "float64", [0, 0, 0, 0x7FF8]) == 0.0
    assert decode_one("400001", "float32", [0, 0x8000]) == 0.0   # -0.0 is a normal zero


# --- bits ----------------------------------------------------------------------------------

def test_bits_in_register():
    tags = [Tag(name=f"B{b}", address=f"400001.{b}", dtype="bool") for b in (0, 3, 4, 15)]
    block = one_block(tags)
    out = decode_block(block, [0b1000_0000_0000_1001], {})
    assert out == {"B0": True, "B3": True, "B4": False, "B15": True}


def test_bits_with_modicon_order_and_one_based():
    tags = [Tag(name="M0", address="400001.0", dtype="bool"), Tag(name="M15", address="400001.15", dtype="bool")]
    params = {"modicon_bit_order": True}
    out = decode_block(one_block(tags, **params), [0x8000], params)
    assert out == {"M0": True, "M15": False}
    tags = [Tag(name="B1", address="400001.1", dtype="bool"), Tag(name="B16", address="400001.16", dtype="bool")]
    params = {"zero_based_bit": False}
    assert decode_block(one_block(tags, **params), [0x0001], params) == {"B1": True, "B16": False}


def test_bit_uses_byte_order():
    params = {"byte_order_big": False}
    block = one_block([Tag(name="B0", address="400001.0", dtype="bool")], **params)
    assert decode_block(block, [0x0100], params) == {"B0": True}


def test_coils_and_discrete_inputs():
    tags = [Tag(name="C1", address="000001", dtype="bool"), Tag(name="C3", address="000003", dtype="bool")]
    block = one_block(tags)
    # pymodbus pads bit replies to a multiple of 8
    assert decode_block(block, [True, True, False, False, False, False, False, False], {}) == {"C1": True, "C3": False}
    block = one_block([Tag(name="D", address="10001", dtype="bool")])
    assert decode_block(block, [1], {}) == {"D": True}


# --- strings and arrays --------------------------------------------------------------------

def test_strings_hilo_lohi_nul_and_odd_length():
    assert decode_one("400001.004H", "string", [0x4142, 0x4344]) == "ABCD"
    assert decode_one("400001.004L", "string", [0x4142, 0x4344]) == "BADC"
    assert decode_one("400001.004H", "string", [0x4142, 0x0043]) == "AB"
    assert decode_one("400001.003H", "string", [0x4142, 0x4344]) == "ABC"


def test_arrays():
    assert decode_one("400001[3]", "uint16", [1, 2, 3]) == [1, 2, 3]
    assert decode_one("400001[2]", "float32", [0, 0x3F80, 0, 0x4000]) == [1.0, 2.0]
    assert decode_one("400001[2][2]", "int16", [1, 2, 3, 0xFFFF]) == [[1, 2], [3, -1]]
    assert decode_one("000001[4]", "bool", [True, False, True, True, 0, 0, 0, 0]) == [True, False, True, True]


# --- shared addresses, offsets, no scaling ----------------------------------------------------

def test_aliases_get_the_same_value_and_offsets_work():
    tags = [
        Tag(name="Y", address="400133", dtype="float32"),
        Tag(name="B", address="400133", dtype="float32"),
        Tag(name="W", address="400136", dtype="uint16"),
    ]
    block = one_block(tags)
    assert (block.start, block.count) == (132, 4)
    out = decode_block(block, [0, 0x3F80, 0, 7], {})
    assert out == {"Y": 1.0, "B": 1.0, "W": 7}


def test_no_scaling_is_applied():
    tag = Tag(name="S", address="400001", dtype="uint16", scale=Scale(raw_lo=0, raw_hi=10, eng_lo=0, eng_hi=100))
    assert decode_block(one_block([tag]), [5], {}) == {"S": 5}


# --- per-tag failures ------------------------------------------------------------------------

def test_short_reply_fails_only_the_tags_past_the_end():
    tags = [Tag(name="A", address="400001", dtype="uint16"), Tag(name="F", address="400002", dtype="float32")]
    out = decode_block(one_block(tags), [42, 0], {})
    assert out["A"] == 42
    assert isinstance(out["F"], DecodeError)
    assert "short" in out["F"].reason


def test_bad_register_value_fails_only_that_tag():
    tags = [Tag(name="A", address="400001", dtype="uint16"), Tag(name="B", address="400002", dtype="uint16")]
    out = decode_block(one_block(tags), [70000, 5], {})
    assert isinstance(out["A"], DecodeError)
    assert out["B"] == 5


def test_bad_bit_value_fails_only_that_tag():
    tags = [Tag(name="C1", address="000001", dtype="bool"), Tag(name="C2", address="000002", dtype="bool")]
    out = decode_block(one_block(tags), ["x", True], {})
    assert isinstance(out["C1"], DecodeError)
    assert out["C2"] is True


def test_decode_keys_are_exactly_the_block_tags():
    tags = [Tag(name=f"R{i}", address=f"4{i + 1:05d}", dtype="uint16") for i in range(5)]
    block = one_block(tags)
    assert sorted(decode_block(block, [0] * 5, {})) == block.tags
