"""ab_pccc block planner and decoder (pure: no sockets, no pycomm3 connection)."""

from __future__ import annotations

import random
from typing import Any

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers.ab_pccc import (
    AbPcccDriver,
    DecodeError,
    PcccBlock,
    decode_block,
)
from opencap.drivers.base import Plan


def make_device(tags: list[Tag], **params: Any) -> Device:
    return Device(
        id="SITE/PLC", channel="SITE", name="PLC", driver="ab_pccc",
        host="192.0.2.15", port=44818, params=params, tags=tags,
    )


def T(name: str, address: str, dtype: str = "uint16", **kw: Any) -> Tag:
    return Tag(name=name, address=address, dtype=dtype, **kw)


def plan(tags: list[Tag], **params: Any) -> Plan:
    return AbPcccDriver(make_device(tags, **params)).plan(tags)


def addresses(p: Plan) -> list[str]:
    return [b.address for b in p.blocks]


# --- driver class -----------------------------------------------------------------

def test_driver_class_attributes() -> None:
    assert AbPcccDriver.name == "ab_pccc"
    assert AbPcccDriver.is_async is False


async def test_driver_is_read_only() -> None:
    """Phase 3 implements the I/O (tests/unit/test_ab_pccc_io.py); writes stay refused."""
    drv = AbPcccDriver(make_device([]))
    with pytest.raises(NotImplementedError, match="read-only"):
        await drv.write("N7:0", 1)
    assert not drv.connected
    await drv.close()                    # never connected: a no-op, no executor needed


# --- merging ----------------------------------------------------------------------

def test_contiguous_elements_merge_into_one_block() -> None:
    p = plan([T(f"F{i}", f"F34:{i}", "float32") for i in range(4)])
    assert addresses(p) == ["F34:0{4}"]
    (b,) = p.blocks
    assert isinstance(b, PcccBlock)
    assert (b.file_type, b.file_number, b.start, b.count, b.elem_size) == ("F", 34, 0, 4, 4)
    assert b.tags == ["F0", "F1", "F2", "F3"]
    assert b.wire_requests() == 1
    assert p.invalid == {}


def test_single_element_block_has_no_count_suffix() -> None:
    assert addresses(plan([T("A", "N7:3")])) == ["N7:3"]


def test_gap_up_to_max_gap_merges() -> None:
    p = plan([T("A", "F34:0", "float32"), T("B", "F34:5", "float32")])   # 4 unused elements between
    assert addresses(p) == ["F34:0{6}"]


def test_gap_above_max_gap_splits() -> None:
    p = plan([T("A", "F34:0", "float32"), T("B", "F34:6", "float32")])   # 5 unused elements between
    assert addresses(p) == ["F34:0", "F34:6"]


def test_max_gap_param() -> None:
    tags = [T("A", "N7:0"), T("B", "N7:11")]
    assert addresses(plan(tags, max_gap=10)) == ["N7:0{12}"]
    assert addresses(plan(tags, max_gap=0)) == ["N7:0", "N7:11"]


def test_max_elements_splits_word_files() -> None:
    p = plan([T(f"N{i}", f"N7:{i}") for i in range(150)])
    assert addresses(p) == ["N7:0{100}", "N7:100{50}"]


def test_request_size_splits_float_files() -> None:
    # 232 data bytes / 4 bytes per float = 58 elements per read
    p = plan([T(f"F{i}", f"F8:{i}", "float32") for i in range(100)])
    assert addresses(p) == ["F8:0{58}", "F8:58{42}"]
    assert all(b.count * b.elem_size <= 232 for b in p.blocks)


def test_request_size_param() -> None:
    p = plan([T(f"F{i}", f"F8:{i}", "float32") for i in range(30)], request_size=100)
    assert addresses(p) == ["F8:0{25}", "F8:25{5}"]


def test_request_size_is_capped_by_the_one_byte_pccc_size_field() -> None:
    # pycomm3 encodes the read byte count as a USINT: at most 255 bytes (127 words)
    p = plan([T(f"N{i}", f"N7:{i}") for i in range(200)], request_size=4000, max_elements=1000)
    assert addresses(p) == ["N7:0{127}", "N7:127{73}"]


def test_different_files_never_merge() -> None:
    p = plan([T("A", "N7:0"), T("B", "N9:1"), T("C", "N7:1"), T("D", "B7:2", "bool")])
    assert addresses(p) == ["B7:2", "N7:0{2}", "N9:1"]


def test_blocks_are_sorted_and_plan_is_deterministic() -> None:
    tags = [T(f"N{i}", f"N{7 + i % 3}:{(i * 7) % 50}") for i in range(60)]
    tags += [T(f"F{i}", f"F{8 + i % 2}:{(i * 5) % 70}", "float32") for i in range(40)]
    tags += [T(f"B{i}", f"B3:{i % 9}/{i % 16}", "bool") for i in range(30)]
    ref = plan(tags)
    for seed in range(5):
        shuffled = tags[:]
        random.Random(seed).shuffle(shuffled)
        p = plan(shuffled)
        assert [b.describe() for b in p.blocks] == [b.describe() for b in ref.blocks]
        assert [b.items for b in p.blocks] == [b.items for b in ref.blocks]
    keys = [(b.file_type, b.file_number, b.slot or 0, b.start) for b in ref.blocks]
    assert keys == sorted(keys)


def test_every_tag_lands_in_exactly_one_block() -> None:
    tags = [T(f"N{i}", f"N7:{i * 3}") for i in range(80)]
    p = plan(tags)
    served = [name for b in p.blocks for name in b.tags]
    assert sorted(served) == sorted(t.name for t in tags)


def test_disabled_tags_are_ignored() -> None:
    p = plan([T("A", "N7:0"), T("B", "N7:1", enabled=False), T("C", "GSM", enabled=False)])
    assert [b.tags for b in p.blocks] == [["A"]]
    assert p.invalid == {}


# --- shared addresses and bits ---------------------------------------------------

def test_tags_sharing_an_address_are_read_once() -> None:
    p = plan([T("PUMP_1_CURRENT_Y", "N7:5"), T("PUMP_1_CURRENT_B", "N7:5")])
    (b,) = p.blocks
    assert b.address == "N7:5"
    assert sorted(b.tags) == ["PUMP_1_CURRENT_B", "PUMP_1_CURRENT_Y"]
    assert decode_block(b, 17) == {"PUMP_1_CURRENT_B": 17, "PUMP_1_CURRENT_Y": 17}


def test_bits_are_read_as_b_words_and_decoded_locally() -> None:
    p = plan([
        T("B0_0", "B3:0/0", "bool"),
        T("B0_5", "B3:0/5", "bool"),
        T("B1_1", "B3:1/1", "bool"),
        T("B1_1_BITNUM", "B3/17", "bool"),    # same bit, bit-number form
        T("B0_15", "B3/15", "bool"),
    ])
    (b,) = p.blocks
    assert b.address == "B3:0{2}"
    values = [0b1000_0000_0010_0001 - 0x10000, 0b10]   # pycomm3 returns B words as signed INT
    assert decode_block(b, values) == {
        "B0_0": True, "B0_5": True, "B0_15": True, "B1_1": True, "B1_1_BITNUM": True,
    }
    assert decode_block(b, [0, 0]) == dict.fromkeys(["B0_0", "B0_5", "B0_15", "B1_1", "B1_1_BITNUM"], False)


def test_element_bit_on_n_word() -> None:
    (b,) = plan([T("RUN", "N7:0/3", "bool"), T("WORD", "N7:0")]).blocks
    assert b.address == "N7:0"
    assert decode_block(b, 0b1000) == {"RUN": True, "WORD": 8}


def test_bit_on_l_word_up_to_31() -> None:
    (b,) = plan([T("HI", "L9:0/31", "bool"), T("V", "L9:1", "int32")]).blocks
    assert b.address == "L9:0{2}"
    assert decode_block(b, [-(2**31), 7]) == {"HI": True, "V": 7}


# --- dtype conversion --------------------------------------------------------------

def test_unsigned_dtype_on_n_word_masks_to_16_bits() -> None:
    (b,) = plan([T("U", "N7:0", "uint16"), T("S", "N7:0", "int16")]).blocks
    assert decode_block(b, -1) == {"U": 65535, "S": -1}


def test_float_dtype_on_n_word_converts_int_to_float() -> None:
    (b,) = plan([T("LEVEL_VAL_DL", "N66:10", "float32")]).blocks
    out = decode_block(b, 42)
    assert out == {"LEVEL_VAL_DL": 42.0}
    assert isinstance(out["LEVEL_VAL_DL"], float)


def test_float_file_values() -> None:
    (b,) = plan([T("A", "F8:0", "float32"), T("B", "F8:1", "float64")]).blocks
    assert decode_block(b, [1.5, -2.25]) == {"A": 1.5, "B": -2.25}


def test_bool_dtype_on_word_is_nonzero() -> None:
    (b,) = plan([T("ON", "N7:0", "bool")]).blocks
    assert decode_block(b, 4) == {"ON": True}
    assert decode_block(b, 0) == {"ON": False}


@pytest.mark.parametrize(
    ("address", "dtype", "reason"),
    [
        ("N7:0", "string", "dtype string"),
        ("N7:0", "int32", "dtype int32"),
        ("N7:0", "uint32", "dtype uint32"),
        ("B3:0", "int64", "dtype int64"),
        ("F8:0", "int16", "dtype int16"),
        ("F8:0", "bool", "dtype bool"),
        ("L9:0", "int16", "dtype int16"),
        ("ST9:0", "float32", "dtype float32"),
        ("B3:0/1", "string", "dtype string"),
    ],
)
def test_incompatible_dtype_is_invalid(address: str, dtype: str, reason: str) -> None:
    p = plan([T("X", address, dtype)])
    assert p.blocks == []
    assert reason in p.invalid["X"]


# --- invalid addresses -------------------------------------------------------------

def test_symbolic_and_function_file_addresses_are_invalid() -> None:
    p = plan([
        T("GSM", "GSM_ONFB", "float32"),
        T("HOUR", "RTC:0.HR"),
        T("MIN", "RTC:0.MIN"),
        T("OK", "N7:0"),
        T("EMPTY", ""),
    ])
    assert addresses(p) == ["N7:0"]
    assert p.invalid["GSM"] == "symbolic address on a PCCC device"
    assert "function file RTC" in p.invalid["HOUR"]
    assert "pycomm3" in p.invalid["HOUR"]
    assert "function file RTC" in p.invalid["MIN"]
    assert p.invalid["EMPTY"] == "empty address"
    assert list(p.invalid) == sorted(p.invalid)


# --- other file types -------------------------------------------------------------

def test_io_words_merge_only_within_one_slot() -> None:
    p = plan([
        T("I0", "I:0/0", "bool"),
        T("I19", "I:0/19", "bool"),       # slot 0, word 1, bit 3
        T("I1", "I:1/0", "bool"),
        T("O2", "O:0/2", "bool"),
    ])
    assert addresses(p) == ["I:0.0{2}", "I:1.0", "O:0.0"]
    b = p.blocks[0]
    assert (b.file_type, b.file_number, b.slot, b.start, b.count) == ("I", 1, 0, 0, 2)
    assert decode_block(b, [0b1, 0b1000]) == {"I0": True, "I19": True}


def test_status_file() -> None:
    p = plan([T("FIRST_SCAN", "S:1/15", "bool"), T("S2", "S:2")])
    assert addresses(p) == ["S:1{2}"]
    assert decode_block(p.blocks[0], [-32768, 5]) == {"FIRST_SCAN": True, "S2": 5}


def test_timer_and_counter_sub_elements_are_separate_reads() -> None:
    p = plan([
        T("ACC", "T4:0.ACC"), T("ACC2", "T4:0.ACC"), T("PRE", "T4:0.PRE"),
        T("DN", "T4:0.DN", "bool"), T("ACC1", "T4:1.ACC"), T("CNT", "C5:0.ACC"),
    ])
    assert addresses(p) == ["C5:0.ACC", "T4:0.ACC", "T4:0.DN", "T4:0.PRE", "T4:1.ACC"]
    assert all(b.count == 1 and b.elem_size == 6 for b in p.blocks)
    by_addr = {b.address: b for b in p.blocks}
    assert decode_block(by_addr["T4:0.ACC"], 1200) == {"ACC": 1200, "ACC2": 1200}
    assert decode_block(by_addr["T4:0.DN"], True) == {"DN": True}
    assert decode_block(by_addr["T4:0.PRE"], -1) == {"PRE": 65535}


def test_string_file_reads_at_most_two_elements() -> None:
    p = plan([T(f"S{i}", f"ST9:{i}", "string") for i in range(5)])
    assert addresses(p) == ["ST9:0{2}", "ST9:2{2}", "ST9:4"]
    assert decode_block(p.blocks[0], ["abc", "de"]) == {"S0": "abc", "S1": "de"}
    assert decode_block(p.blocks[2], "xyz") == {"S4": "xyz"}


def test_long_file_element_size() -> None:
    p = plan([T(f"L{i}", f"L9:{i}", "int32") for i in range(60)])
    assert addresses(p) == ["L9:0{58}", "L9:58{2}"]


# --- decode errors ----------------------------------------------------------------

def test_short_reply_marks_missing_tags_as_decode_error() -> None:
    (b,) = plan([T("A", "N7:0"), T("B", "N7:1"), T("C", "N7:2")]).blocks
    out = decode_block(b, [1, 2])
    assert out["A"] == 1 and out["B"] == 2
    assert isinstance(out["C"], DecodeError)


def test_missing_value_marks_every_tag_as_decode_error() -> None:
    (b,) = plan([T("A", "N7:0"), T("B", "N7:1")]).blocks
    out = decode_block(b, None)
    assert all(isinstance(v, DecodeError) for v in out.values())
    assert set(out) == {"A", "B"}


def test_wrong_value_type_is_decode_error() -> None:
    (b,) = plan([T("A", "N7:0")]).blocks
    assert isinstance(decode_block(b, "oops")["A"], DecodeError)


# --- describe and pycomm3 compatibility ------------------------------------------

def test_describe() -> None:
    (b,) = plan([T(f"F{i}", f"F34:{i}", "float32") for i in range(22)]).blocks
    text = b.describe()
    assert "F34:0{22}" in text and "22 tags" in text and "88 B" in text


def _pycomm3_parse(address: str) -> dict:
    slc = pytest.importorskip("pycomm3.slc_driver")
    parsed = slc.parse_tag(address)
    assert parsed is not None, f"pycomm3 SLCDriver cannot parse {address!r}"
    return parsed


def test_pycomm3_accepts_every_read_string() -> None:
    tags = [
        T("N", "N7:0"), T("N2", "N7:9"), T("F", "F8:3", "float32"), T("L", "L9:0", "int32"),
        T("B", "B3:4/2", "bool"), T("BB", "B3/80", "bool"), T("S", "S:1/15", "bool"),
        T("I", "I:0/19", "bool"), T("I2", "I:2.1/0", "bool"), T("O", "O:1/3", "bool"),
        T("ST", "ST9:3", "string"), T("ST2", "ST9:4", "string"),
        T("TA", "T4:0.ACC"), T("TD", "T4:0.DN", "bool"), T("CP", "C5:2.PRE"),
    ]
    p = plan(tags)
    assert p.invalid == {}
    for b in p.blocks:
        parsed = _pycomm3_parse(b.address)
        assert parsed["file_type"] == b.file_type, b.address
        assert int(parsed["file_number"]) == b.file_number, b.address
        assert int(parsed["element_count"]) == b.count, b.address
        if b.file_type in ("I", "O"):
            assert int(parsed["element_number"]) == b.slot, b.address
            assert int(parsed["pos_number"]) == b.start, b.address
        else:
            assert int(parsed["element_number"]) == b.start, b.address
        # bits are decoded locally, never by pycomm3 (except T/C named sub-elements)
        if b.file_type not in ("T", "C"):
            assert parsed.get("address_field") == 2, b.address


def test_pycomm3_bit_number_form_is_why_we_never_send_it() -> None:
    """pycomm3 1.2.16 parses `B3/17` with float division and always reads bit 0.

    This documents the library behaviour that made us read B words and decode bits
    locally (docs/decisions.md). If it ever starts failing, pycomm3 fixed the bug.
    """
    parsed = _pycomm3_parse("B3/17")
    assert int(parsed["sub_element"] or 0) == 0     # should be bit 1 of word 1
