"""Modbus block planner (PLAN.md §5.1): merge by FC, max_gap, per-FC span, protocol caps."""

from __future__ import annotations

import random

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers.base import Plan
from opencap.drivers.modbus_tcp import ModbusBlock, ModbusTcpDriver


def T(name: str, address: str, dtype: str = "uint16", **kw) -> Tag:
    return Tag(name=name, address=address, dtype=dtype, **kw)


def make_driver(tags: list[Tag] | None = None, **params) -> ModbusTcpDriver:
    dev = Device(
        id="SITE/PLC", channel="SITE", name="PLC", driver="modbus_tcp",
        host="127.0.0.1", port=502, params=params, tags=tags or [],
    )
    return ModbusTcpDriver(dev)


def plan(tags: list[Tag], **params) -> Plan:
    return make_driver(tags, **params).plan(tags)


def spans(p: Plan) -> list[tuple[int, int, int]]:
    return [(b.fc, b.start, b.count) for b in p.blocks]


# --- driver surface -------------------------------------------------------------

def test_driver_identity_and_phase0_surface():
    drv = make_driver()
    assert ModbusTcpDriver.name == "modbus_tcp"
    assert ModbusTcpDriver.is_async is True
    assert isinstance(drv.plan([]), Plan)
    assert drv.plan([]).blocks == [] and drv.plan([]).invalid == {}


async def test_read_without_connect_and_never_writes():
    from opencap.drivers.base import ConnectionLost

    drv = make_driver()
    assert drv.connected is False
    with pytest.raises(ConnectionLost):
        await drv.read(ModbusBlock(fc=3, start=0, count=1))
    with pytest.raises(NotImplementedError, match="read-only"):
        await drv.write("FLOW_1", 1.0)


def test_bad_params_raise_at_construction():
    with pytest.raises(ValueError, match="max_holding"):
        make_driver(max_holding=0)
    with pytest.raises(ValueError, match="max_gap"):
        make_driver(max_gap=-1)


# --- single blocks and describe() -------------------------------------------------

def test_single_float_tag():
    p = plan([T("FLOW_1", "400045", "float32")])
    assert spans(p) == [(3, 44, 2)]
    b = p.blocks[0]
    assert isinstance(b, ModbusBlock)
    assert b.tags == ["FLOW_1"]
    assert b.describe() == "FC03 44..45 (2 regs)"
    assert p.wire_requests == 1


def test_describe_for_bits():
    p = plan([T("C1", "000001", "bool"), T("C2", "000032", "bool")], max_coils=32)
    assert p.blocks[0].describe() == "FC01 0..31 (32 bits)"


# --- merging ----------------------------------------------------------------------

def test_contiguous_tags_merge():
    p = plan([T("A", "400001", "float32"), T("B", "400003", "float32"), T("C", "400005")])
    assert spans(p) == [(3, 0, 5)]
    assert p.blocks[0].tags == ["A", "B", "C"]


def test_gap_up_to_max_gap_merges_and_beyond_splits():
    # A ends at reg 1 (exclusive end 2); B at 6 -> gap 4 -> merge; C at 12 -> gap 5 -> split
    p = plan([T("A", "400001", "float32"), T("B", "400007"), T("C", "400013")])
    assert spans(p) == [(3, 0, 7), (3, 12, 1)]


def test_max_gap_param():
    tags = [T("A", "400001"), T("B", "400003")]          # gap of 1 register
    assert spans(plan(tags, max_gap=0)) == [(3, 0, 1), (3, 2, 1)]
    assert spans(plan(tags, max_gap=1)) == [(3, 0, 3)]


def test_span_capped_by_device_max_holding():
    tags = [T(f"R{i}", f"4{i + 1:05d}") for i in range(40)]   # 40 contiguous registers
    p = plan(tags, max_holding=32)
    assert spans(p) == [(3, 0, 32), (3, 32, 8)]
    assert all(b.count <= 32 for b in p.blocks)


def test_a_32bit_tag_never_straddles_a_block_limit():
    tags = [T(f"F{i}", f"4{2 * i + 1:05d}", "float32") for i in range(20)]  # regs 0..39
    p = plan(tags, max_holding=31)
    assert spans(p) == [(3, 0, 30), (3, 30, 10)]


def test_default_span_is_64():
    tags = [T(f"R{i}", f"4{i + 1:05d}") for i in range(100)]
    assert spans(plan(tags)) == [(3, 0, 64), (3, 64, 36)]


def test_params_are_capped_at_protocol_limits():
    regs = [T(f"R{i}", f"4{i + 1:05d}") for i in range(300)]
    p = plan(regs, max_holding=1000)
    assert [b.count for b in p.blocks] == [125, 125, 50]
    coils = [T(f"C{i}", f"0{i + 1:05d}", "bool") for i in range(2500)]
    p = plan(coils, max_coils=5000)
    assert [b.count for b in p.blocks] == [2000, 500]


def test_input_registers_use_max_input():
    tags = [T(f"I{i}", f"3{i + 1:05d}") for i in range(10)]
    assert spans(plan(tags, max_input=4, max_holding=64)) == [(4, 0, 4), (4, 4, 4), (4, 8, 2)]


def test_function_codes_never_merge_and_blocks_are_sorted():
    tags = [
        T("H", "400001"), T("I", "300001"), T("C", "000001", "bool"), T("D", "100001", "bool"),
    ]
    assert spans(plan(tags)) == [(1, 0, 1), (2, 0, 1), (3, 0, 1), (4, 0, 1)]


# --- shared addresses, overlap, bits -------------------------------------------------

def test_same_address_tags_are_read_once_and_served_to_all_names():
    tags = [T("PUMP_1_CURRENT_Y", "400133", "float32"), T("PUMP_1_CURRENT_B", "400133", "float32")]
    p = plan(tags)
    assert spans(p) == [(3, 132, 2)]
    b = p.blocks[0]
    assert b.tags == ["PUMP_1_CURRENT_B", "PUMP_1_CURRENT_Y"]
    assert len(b.items) == 1
    assert b.items[0].names == ("PUMP_1_CURRENT_B", "PUMP_1_CURRENT_Y")
    assert b.items[0].offset == 0


def test_5_and_6_digit_forms_of_one_address_share_an_item():
    p = plan([T("A", "40002", "float32"), T("B", "400002", "float32")])
    assert len(p.blocks[0].items) == 1


def test_overlapping_tags_of_different_types():
    p = plan([T("F", "400001", "float32"), T("W", "400002", "uint16")])
    assert spans(p) == [(3, 0, 2)]
    offsets = {n: it.offset for it in p.blocks[0].items for n in it.names}
    assert offsets == {"F": 0, "W": 1}


def test_bits_and_word_of_one_register():
    tags = [T("W", "400010"), T("B0", "400010.0", "bool"), T("B3", "400010.3", "bool")]
    p = plan(tags)
    assert spans(p) == [(3, 9, 1)]
    assert p.blocks[0].tags == ["B0", "B3", "W"]
    assert len(p.blocks[0].items) == 3


def test_coil_gap_is_max_gap_times_16_bits():
    # default max_gap 4 -> 64 unused bits allowed between coils
    near = plan([T("C1", "000001", "bool"), T("C2", "000066", "bool")], max_coils=2000)
    assert spans(near) == [(1, 0, 66)]                     # gap 64 -> merge
    far = plan([T("C1", "000001", "bool"), T("C2", "000067", "bool")], max_coils=2000)
    assert spans(far) == [(1, 0, 1), (1, 66, 1)]           # gap 65 -> split
    tight = plan([T("C1", "000001", "bool"), T("C2", "000003", "bool")], max_gap_bits=0)
    assert spans(tight) == [(1, 0, 1), (1, 2, 1)]


def test_item_offsets_are_relative_to_block_start():
    p = plan([T("A", "400045", "float32"), T("B", "400048")])
    b = p.blocks[0]
    assert (b.start, b.count) == (44, 4)
    assert {n: it.offset for it in b.items for n in it.names} == {"A": 0, "B": 3}


# --- invalid and disabled tags ----------------------------------------------------------

def test_unparseable_address_goes_to_invalid_with_reason():
    p = plan([T("GOOD", "400001"), T("BAD", "500001"), T("BADTYPE", "400002", "bool")])
    assert spans(p) == [(3, 0, 1)]
    assert set(p.invalid) == {"BAD", "BADTYPE"}
    assert "500001" in p.invalid["BAD"]
    assert "bit" in p.invalid["BADTYPE"]


def test_tag_larger_than_device_block_goes_to_invalid():
    p = plan([T("ARR", "400001[40]"), T("OK", "400100")], max_holding=32)
    assert spans(p) == [(3, 99, 1)]
    assert "40" in p.invalid["ARR"] and "max_holding" in p.invalid["ARR"]


def test_disabled_tags_are_not_planned():
    p = plan([T("ON", "400001"), T("OFF", "400002", enabled=False), T("OFFBAD", "9", enabled=False)])
    assert p.blocks[0].tags == ["ON"]
    assert p.invalid == {}


# --- determinism and coverage ----------------------------------------------------------

def _random_tags(rng: random.Random, n: int) -> list[Tag]:
    tags = []
    for i in range(n):
        kind = rng.choice(["h16", "h32", "hbit", "coil", "di", "ir", "bad"])
        ref = rng.randint(1, 400)
        if kind == "h16":
            tags.append(T(f"T{i}", f"4{ref:05d}", "uint16"))
        elif kind == "h32":
            tags.append(T(f"T{i}", f"4{ref:05d}", "float32"))
        elif kind == "hbit":
            tags.append(T(f"T{i}", f"4{ref:05d}.{rng.randint(0, 15)}", "bool"))
        elif kind == "coil":
            tags.append(T(f"T{i}", f"0{ref:05d}", "bool"))
        elif kind == "di":
            tags.append(T(f"T{i}", f"1{ref:04d}", "bool"))
        elif kind == "ir":
            tags.append(T(f"T{i}", f"3{ref:05d}", "int32"))
        else:
            tags.append(T(f"T{i}", f"9{ref:05d}", "uint16"))
    return tags


def _snapshot(p: Plan):
    return (
        [(b.describe(), b.tags, [(it.names, it.offset) for it in b.items]) for b in p.blocks],
        sorted(p.invalid.items()),
    )


def test_plan_is_deterministic_regardless_of_tag_order():
    rng = random.Random(1234)
    tags = _random_tags(rng, 300)
    first = _snapshot(plan(tags, max_holding=32, max_coils=32))
    for _ in range(5):
        rng.shuffle(tags)
        assert _snapshot(plan(tags, max_holding=32, max_coils=32)) == first


@pytest.mark.parametrize("seed", range(5))
def test_every_tag_in_exactly_one_block_or_invalid_and_limits_hold(seed):
    rng = random.Random(seed)
    tags = _random_tags(rng, 250)
    params = {"max_holding": 32, "max_input": 16, "max_coils": 32, "max_discrete": 8}
    p = plan(tags, **params)
    limit = {1: 32, 2: 8, 3: 32, 4: 16}
    seen: list[str] = []
    for b in p.blocks:
        assert 1 <= b.count <= limit[b.fc]
        assert b.tags == sorted(b.tags)
        assert sorted(n for it in b.items for n in it.names) == b.tags
        for it in b.items:
            assert 0 <= it.offset and it.offset + it.addr.count <= b.count
            assert it.addr.address == b.start + it.offset
        seen.extend(b.tags)
    assert len(seen) == len(set(seen))
    assert set(seen) | set(p.invalid) == {t.name for t in tags}
    assert not set(seen) & set(p.invalid)
    starts = [(b.fc, b.start) for b in p.blocks]
    assert starts == sorted(starts)
