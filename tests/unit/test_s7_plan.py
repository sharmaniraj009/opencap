"""S7 block planner: merge by area + DB, gap <= max_gap bytes, span <= PDU - 18 (PLAN §5.1)."""

from __future__ import annotations

import random

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers.s7 import (
    READ_OVERHEAD,
    S7Block,
    S7Driver,
    S7Params,
    parse_s7_address,
    plan_blocks,
)


def parsed(*specs: tuple[str, str, str]) -> list:
    return [(name, parse_s7_address(addr, dtype)) for name, addr, dtype in specs]


def layout(blocks: list[S7Block]) -> list[tuple]:
    return [(b.area, b.db, b.start, b.count) for b in blocks]


def plan(specs, **params):
    return plan_blocks(parsed(*specs), S7Params.from_mapping(params))


def test_read_overhead_is_18_bytes() -> None:
    assert READ_OVERHEAD == 18          # 12 header + 2 parameter + 4 data item header


def test_same_db_within_gap_merges() -> None:
    blocks, invalid = plan([("a", "DB100,D0", "float32"), ("b", "DB100,D20", "float32")])   # gap 16
    assert invalid == {} and layout(blocks) == [("DB", 100, 0, 24)]
    blocks, _ = plan([("a", "DB100,D0", "float32"), ("b", "DB100,D21", "float32")])         # gap 17
    assert layout(blocks) == [("DB", 100, 0, 4), ("DB", 100, 21, 4)]


def test_max_gap_param() -> None:
    blocks, _ = plan([("a", "DB1,W0", "uint16"), ("b", "DB1,W10", "uint16")], max_gap=0)
    assert len(blocks) == 2
    blocks, _ = plan([("a", "DB1,W0", "uint16"), ("b", "DB1,W2", "uint16")], max_gap=0)   # adjacent
    assert layout(blocks) == [("DB", 1, 0, 4)]


def test_areas_and_dbs_never_merge() -> None:
    specs = [("m", "MD0", "float32"), ("db1", "DB1,D0", "float32"), ("db2", "DB2,D0", "float32"),
             ("i", "ID0", "float32"), ("q", "QD0", "float32"), ("c", "C0", "uint16"), ("t", "T0", "uint32")]
    blocks, invalid = plan(specs)
    assert invalid == {}
    assert sorted(layout(blocks)) == sorted([("M", 0, 0, 4), ("DB", 1, 0, 4), ("DB", 2, 0, 4), ("I", 0, 0, 4),
                                              ("Q", 0, 0, 4), ("C", 0, 0, 1), ("T", 0, 0, 1)])


def test_overlapping_and_bit_items_share_a_block() -> None:
    specs = [("w", "MW10", "uint16"), ("hi", "MB10", "uint16"), ("bit", "M11.2", "bool"),
             ("d", "MD8", "float32"), ("wbit", "MW10.15", "bool")]
    blocks, _ = plan(specs)
    assert layout(blocks) == [("M", 0, 8, 4)]
    assert sorted(blocks[0].tags) == ["bit", "d", "hi", "w", "wbit"]


@pytest.mark.parametrize(("pdu", "span"), [(240, 222), (480, 462), (960, 942)])
def test_span_is_pdu_minus_18(pdu: int, span: int) -> None:
    fits = [("a", "DB1,B0", "uint16"), ("b", f"DB1,B{span - 1}", "uint16")]
    blocks, _ = plan(fits, max_pdu=pdu, max_gap=1000)
    assert layout(blocks) == [("DB", 1, 0, span)]
    assert blocks[0].nbytes == span
    too_far = [("a", "DB1,B0", "uint16"), ("b", f"DB1,B{span}", "uint16")]
    blocks, _ = plan(too_far, max_pdu=pdu, max_gap=1000)
    assert layout(blocks) == [("DB", 1, 0, 1), ("DB", 1, span, 1)]


def test_dense_tags_split_at_the_span() -> None:
    specs = [(f"r{i}", f"DB5,REAL{4 * i}", "float32") for i in range(200)]       # 800 bytes
    blocks, invalid = plan(specs, max_pdu=480)
    assert invalid == {}
    assert [b.count for b in blocks] == [460, 340]                                 # 115 REALs, then 85
    assert all(b.nbytes <= 480 - 18 for b in blocks)
    assert sum(len(b.tags) for b in blocks) == 200


def test_item_bigger_than_the_span_is_invalid() -> None:
    blocks, invalid = plan([("big", "DB1,REAL0[60]", "float32"), ("ok", "DB1,REAL400", "float32")], max_pdu=240)
    assert layout(blocks) == [("DB", 1, 400, 4)]
    assert "big" in invalid and "needs 240 bytes in one request" in invalid["big"]
    assert "max_pdu=240" in invalid["big"]
    _, invalid = plan([("s", "DB1,STRING0.254", "string")], max_pdu=240)
    assert "needs 256 bytes" in invalid["s"]


def test_timers_and_counters_merge_in_elements() -> None:
    specs = [("c0", "C0", "uint16"), ("c8", "C9", "uint16"), ("c30", "C30", "uint16"),
             ("t1", "T1", "uint32"), ("t2", "T2", "uint32")]
    blocks, _ = plan(specs)                              # gap 16 bytes = 8 elements
    assert sorted(layout(blocks)) == [("C", 0, 0, 10), ("C", 0, 30, 1), ("T", 0, 1, 2)]
    c = next(b for b in blocks if b.area == "C" and b.start == 0)
    assert c.nbytes == 20 and c.unit == "counter"


def test_timer_span_counts_two_bytes_per_element() -> None:
    specs = [(f"t{i}", f"T{i}", "uint32") for i in range(120)]
    blocks, _ = plan(specs, max_pdu=240)                 # 222 bytes -> 111 timers
    assert [b.count for b in blocks] == [111, 9]


def test_same_address_is_read_once_and_served_to_every_name() -> None:
    blocks, _ = plan([("a", "DB1,D4", "float32"), ("b", "DB1,DBD4", "float32"), ("c", "DB1.DBD4", "float32"),
                      ("raw", "DB1,D4", "uint32")])
    assert len(blocks) == 1 and len(blocks[0].items) == 2
    names = sorted(tuple(it.names) for it in blocks[0].items)
    assert names == [("a", "b", "c"), ("raw",)]


def test_plan_is_independent_of_input_order() -> None:
    specs = [(f"t{i}", f"DB{1 + i % 3},D{8 * (i // 3)}", "float32") for i in range(60)]
    specs += [("m1", "M0.1", "bool"), ("m2", "MW2", "int16"), ("c", "C3", "uint16")]
    want = plan(specs)
    for seed in range(5):
        shuffled = specs[:]
        random.Random(seed).shuffle(shuffled)
        got = plan(shuffled)
        assert layout(got[0]) == layout(want[0])
        assert [b.tags for b in got[0]] == [b.tags for b in want[0]]


def test_describe_and_wire_requests() -> None:
    blocks, _ = plan([("a", "DB100,D4", "float32"), ("m", "MD232", "float32"), ("c", "C0", "uint16"),
                      ("t", "T3", "uint32")])
    by_area = {b.area: b for b in blocks}
    assert by_area["DB"].describe() == "DB100 4..7 (4 bytes)"
    assert by_area["M"].describe() == "M 232..235 (4 bytes)"
    assert by_area["C"].describe() == "C 0 (1 counter)"
    assert by_area["T"].describe() == "T 3 (1 timer)"
    assert all(b.wire_requests() == 1 for b in blocks)


def test_driver_plan_reports_invalid_tags_and_skips_disabled() -> None:
    tags = [
        Tag(name="FLOW_1", address="MD232", dtype="float32"),
        Tag(name="COUNT_0", address="C0", dtype="uint16"),
        Tag(name="LEVEL", address="DB100,D04", dtype="float32"),
        Tag(name="TOTAL", address="DB100,D016", dtype="float32"),
        Tag(name="BAD_TYPE", address="MW10", dtype="float32"),
        Tag(name="PERIPHERAL", address="PIW256", dtype="int16"),
        Tag(name="OFF", address="DB0,W0", dtype="uint16", enabled=False),
    ]
    dev = Device(id="SITE S7/PLC", channel="SITE S7", name="PLC", driver="s7", host="192.0.2.50", port=102,
                 params={"rack": 0, "slot": 1, "max_pdu": 960, "link_type": 3, "byte_order_big": True}, tags=tags)
    p = S7Driver(dev).plan(dev.tags)
    assert sorted(p.invalid) == ["BAD_TYPE", "PERIPHERAL"]
    assert sorted(t for b in p.blocks for t in b.tags) == ["COUNT_0", "FLOW_1", "LEVEL", "TOTAL"]
    assert p.wire_requests == 3                          # DB100 4..19, M 232..235, C 0


def test_driver_rejects_bad_params() -> None:
    dev = Device(id="X/Y", channel="X", name="Y", driver="s7", host="192.0.2.50", port=102,
                 params={"max_pdu": 100}, tags=[])
    with pytest.raises(ValueError, match="max_pdu"):
        S7Driver(dev)
