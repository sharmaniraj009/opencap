"""MELSEC MC block planner: per-device word blocks within the MC 3E batch-read limit."""

from __future__ import annotations

import random

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers.melsec_mc import MAX_WORDS, McBlock, MelsecMcDriver, parse_address, plan_blocks


def _plan(specs: list[tuple[str, str, str]], **params):
    parsed = [(name, parse_address(addr, dtype, params)) for name, addr, dtype in specs]
    return plan_blocks(parsed, params)


def _shape(blocks: list[McBlock]) -> list[tuple[str, int, int]]:
    return [(b.device, b.start, b.count) for b in blocks]


def test_contiguous_and_small_gaps_merge() -> None:
    blocks, invalid = _plan([
        ("a", "D0", "uint16"), ("b", "D1", "float32"), ("c", "D19", "int16"),    # gap 16 words: merged
        ("d", "D36", "uint16"),                                                  # gap 16 after D19: merged
        ("e", "D53", "uint16"),                                                  # gap 16: merged
        ("f", "D71", "uint16"),                                                  # gap 17: new block
    ])
    assert invalid == {}
    assert _shape(blocks) == [("D", 0, 54), ("D", 71, 1)]
    assert blocks[0].tags == ["a", "b", "c", "d", "e"]
    assert [it.offset for it in blocks[0].items] == [0, 1, 19, 36, 53]


def test_max_gap_param() -> None:
    blocks, _ = _plan([("a", "D0", "uint16"), ("b", "D2", "uint16")], max_gap=0)
    assert _shape(blocks) == [("D", 0, 1), ("D", 2, 1)]
    blocks, _ = _plan([("a", "D0", "uint16"), ("b", "D1", "uint16")], max_gap=0)
    assert _shape(blocks) == [("D", 0, 2)]


def test_span_never_exceeds_max_words() -> None:
    specs = [(f"t{i}", f"D{i * 10}", "float32") for i in range(300)]          # D0..D2991
    blocks, invalid = _plan(specs)
    assert invalid == {}
    assert all(b.count <= MAX_WORDS for b in blocks)
    assert _shape(blocks) == [("D", 0, 952), ("D", 960, 952), ("D", 1920, 952), ("D", 2880, 112)]
    blocks, _ = _plan(specs, max_words=100)
    assert all(b.count <= 100 for b in blocks) and len(blocks) == 30


def test_item_bigger_than_max_words_is_invalid() -> None:
    blocks, invalid = _plan([("big", "D0[961]", "uint16"), ("ok", "D2000[960]", "uint16")])
    assert list(invalid) == ["big"] and "961 words" in invalid["big"] and "max_words=960" in invalid["big"]
    assert _shape(blocks) == [("D", 2000, 960)]
    _, invalid = _plan([("big", "D0[11]", "uint16")], max_words=10)
    assert list(invalid) == ["big"]


def test_bit_devices_are_read_as_aligned_words() -> None:
    blocks, _ = _plan([("m5", "M5", "bool"), ("m40", "M40", "bool"), ("m400", "M400", "bool"),
                       ("w", "M32", "uint16")])
    assert _shape(blocks) == [("M", 0, 3), ("M", 25, 1)]             # M0..M47 and M400..M415
    assert blocks[0].head == "M0" and blocks[1].head == "M400"
    assert blocks[0].describe() == "M0..M47 (3 words)"
    items = {it.names: (it.offset, it.addr.bit) for it in blocks[0].items}
    assert items == {("m5",): (0, 5), ("w",): (2, None), ("m40",): (2, 8)}


def test_hex_device_heads_and_descriptions() -> None:
    blocks, _ = _plan([("x", "X1A", "bool"), ("w", "W1F", "uint16"), ("zr", "ZR10", "uint16")])
    by_dev = {b.device: b for b in blocks}
    assert by_dev["X"].head == "X010" and by_dev["X"].describe() == "X10..X1F (1 words)"
    assert by_dev["W"].head == "W01F" and by_dev["W"].describe() == "W1F..W1F (1 words)"
    assert by_dev["ZR"].head == "ZR010"


def test_devices_never_merge_with_each_other() -> None:
    blocks, _ = _plan([("d", "D0", "uint16"), ("dx", "DX0", "bool"), ("x", "X0", "bool"), ("r", "R0", "uint16"),
                       ("zr", "ZR0", "uint16"), ("sd", "SD0", "uint16")])
    assert sorted(b.device for b in blocks) == ["D", "DX", "R", "SD", "X", "ZR"]


def test_same_address_is_one_item_for_every_name() -> None:
    blocks, _ = _plan([("a", "D0000002", "float32"), ("b", "D2", "float32"), ("c", "D2", "uint16")])
    assert len(blocks) == 1
    items = sorted((it.names, it.addr.dtype) for it in blocks[0].items)
    assert items == [(("a", "b"), "float32"), (("c",), "uint16")]
    assert blocks[0].tags == ["a", "b", "c"]


def test_output_is_independent_of_input_order() -> None:
    specs = [(f"t{i}", f"D{i * 7}", "uint16") for i in range(50)] + [(f"m{i}", f"M{i * 13}", "bool") for i in range(40)]
    ref, _ = _plan(specs)
    shuffled = specs[:]
    random.Random(4).shuffle(shuffled)
    again, _ = _plan(shuffled)
    assert _shape(ref) == _shape(again) and [b.tags for b in ref] == [b.tags for b in again]


def test_driver_plan_reports_bad_addresses_and_skips_disabled_tags() -> None:
    tags = [
        Tag(name="FLOW_1", address="D0000002", dtype="float32"),      # the KEPServer sample device
        Tag(name="PUMP_1_RUN", address="B0000", dtype="bool"),
        Tag(name="BAD", address="Q0", dtype="uint16"),
        Tag(name="OFF", address="D10", dtype="uint16", enabled=False),
    ]
    dev = Device(id="SITE MELSEC/STP", channel="SITE MELSEC", name="STP", driver="melsec_mc", host="192.0.2.60",
                 port=5001, params={"plc_type": "Q", "protocol": "tcp", "first_word_low": True, "frame": "3E"},
                 tags=tags)
    plan = MelsecMcDriver(dev).plan(tags)
    assert list(plan.invalid) == ["BAD"] and "unknown device" in plan.invalid["BAD"]
    assert [(b.describe(), b.tags) for b in plan.blocks] == [
        ("B0..BF (1 words)", ["PUMP_1_RUN"]), ("D2..D3 (2 words)", ["FLOW_1"])]
    assert plan.wire_requests == 2


def test_driver_rejects_bad_params() -> None:
    dev = Device(id="S/P", channel="S", name="P", driver="melsec_mc", host="192.0.2.60", port=5001,
                 params={"plc_type": "FX"}, tags=[])
    with pytest.raises(ValueError, match="plc_type"):
        MelsecMcDriver(dev)
