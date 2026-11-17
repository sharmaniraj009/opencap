"""ab_logix symbolic-address validator, batch planner and decoder (pure)."""

from __future__ import annotations

import random
import re
from typing import Any

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers.ab_logix import (
    AbLogixDriver,
    LogixAddress,
    LogixBlock,
    decode_block,
    parse_logix_address,
)
from opencap.drivers.ab_pccc import DecodeError
from opencap.drivers.base import Plan


def make_device(tags: list[Tag], **params: Any) -> Device:
    return Device(
        id="SITE/PLC", channel="SITE", name="PLC", driver="ab_logix",
        host="192.0.2.15", port=44818, params=params, tags=tags,
    )


def T(name: str, address: str, dtype: str = "float32", **kw: Any) -> Tag:
    return Tag(name=name, address=address, dtype=dtype, **kw)


def plan(tags: list[Tag], **params: Any) -> Plan:
    return AbLogixDriver(make_device(tags, **params)).plan(tags)


# --- driver class -----------------------------------------------------------------

def test_driver_class_attributes() -> None:
    assert AbLogixDriver.name == "ab_logix"
    assert AbLogixDriver.is_async is False


async def test_write_stays_read_only() -> None:
    """Phase 2 implements the I/O (tests/unit/test_ab_logix_io.py); writing never exists."""
    for micro800 in (True, False):
        drv = AbLogixDriver(make_device([], micro800=micro800))
        with pytest.raises(NotImplementedError, match="read-only"):
            await drv.write("FT_01", 1.0)
    assert "write" not in AbLogixDriver.__dict__          # the base class's refusing write() is used


# --- validator --------------------------------------------------------------------

@pytest.mark.parametrize(
    ("address", "read_name", "bit"),
    [
        ("FT_01", "FT_01", None),                                   # bare name (Micro800 global)
        ("INF_FLOW_TOTAL.TOTAL", "INF_FLOW_TOTAL.TOTAL", None),     # structure member
        ("RAW_SEWAGE_PUMP_1.RUN_FB", "RAW_SEWAGE_PUMP_1.RUN_FB", None),
        ("ARRAY_REAL[4]", "ARRAY_REAL[4]", None),                   # array element
        ("ARR[1,2]", "ARR[1,2]", None),
        ("ARR[1,2,3]", "ARR[1,2,3]", None),
        ("UDT[2].Member[3].X", "UDT[2].Member[3].X", None),
        ("Program:MainProgram.Tag", "Program:MainProgram.Tag", None),
        ("Program:Main.UDT.Member", "Program:Main.UDT.Member", None),
        ("DINT_TAG.5", "DINT_TAG", 5),                              # bit of an integer
        ("UDT.Word.31", "UDT.Word", 31),
        ("Program:Main.Status.0", "Program:Main.Status", 0),
        ("LINT_TAG.63", "LINT_TAG", 63),
        ("_Hidden", "_Hidden", None),
        ("  FT_01 ", "FT_01", None),
    ],
)
def test_valid_symbolic_addresses(address: str, read_name: str, bit: int | None) -> None:
    assert parse_logix_address(address) == LogixAddress(read_name, bit)


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("", "empty address"),
        ("   ", "empty address"),
        ("F28:0", "PCCC data-table address on a symbolic (Logix/Micro800) device"),
        ("N54:5", "PCCC data-table address on a symbolic (Logix/Micro800) device"),
        ("B3:0/14", "PCCC data-table address on a symbolic (Logix/Micro800) device"),
        ("1ABC", "illegal symbolic address"),
        ("A..B", "illegal symbolic address"),
        ("A.", "illegal symbolic address"),
        (".A", "illegal symbolic address"),
        ("A B", "illegal symbolic address"),
        ("A-B", "illegal symbolic address"),
        ("A:B", "illegal symbolic address"),
        ("A[", "illegal symbolic address"),
        ("A[]", "illegal symbolic address"),
        ("A[x]", "illegal symbolic address"),
        ("A[1][2]", "illegal symbolic address"),
        ("A[1,2,3,4]", "illegal symbolic address"),
        ("A.B/3", "illegal symbolic address"),
        ("Program:.X", "illegal symbolic address"),
        ("Program:Main", "illegal symbolic address"),
        ("A.64", "bit 64 out of range"),
        ("A{10}", "array block reads"),
        ("A[0]{10}", "array block reads"),
    ],
)
def test_invalid_symbolic_addresses(address: str, reason: str) -> None:
    with pytest.raises(ValueError, match=re.escape(reason)):
        parse_logix_address(address)


def test_program_scope_is_not_readable_on_micro800() -> None:
    assert parse_logix_address("Program:Main.X").read_name == "Program:Main.X"
    with pytest.raises(ValueError, match="program-scoped"):
        parse_logix_address("Program:Main.X", micro800=True)


# --- batching -----------------------------------------------------------------------

def test_names_are_batched_by_max_batch_and_sorted() -> None:
    tags = [T(f"T{i:03}", f"TAG_{i:03}") for i in range(100)]
    p = plan(tags, micro800=True)
    assert [len(b.names) for b in p.blocks] == [40, 40, 20]
    names = [n for b in p.blocks for n in b.names]
    assert names == sorted(names)
    assert all(isinstance(b, LogixBlock) for b in p.blocks)


def test_max_batch_param() -> None:
    tags = [T(f"T{i}", f"TAG_{i}") for i in range(100)]
    assert [len(b.names) for b in plan(tags, max_batch=25).blocks] == [25, 25, 25, 25]


def test_plan_is_deterministic() -> None:
    tags = [T(f"T{i}", f"TAG_{(i * 37) % 97}", ["float32", "uint16", "bool"][i % 3]) for i in range(97)]
    ref = plan(tags)
    for seed in range(5):
        shuffled = tags[:]
        random.Random(seed).shuffle(shuffled)
        p = plan(shuffled)
        assert [b.names for b in p.blocks] == [b.names for b in ref.blocks]
        assert [b.items for b in p.blocks] == [b.items for b in ref.blocks]


def test_duplicate_addresses_are_read_once_and_mapped_to_every_tag() -> None:
    p = plan([T("TEST_A", "TEST"), T("TEST_B", "TEST"), T("FLOW_1", "FT_01")], micro800=True)
    (b,) = p.blocks
    assert b.names == ["FT_01", "TEST"]
    assert b.name_to_tags() == {"FT_01": ["FLOW_1"], "TEST": ["TEST_A", "TEST_B"]}
    assert sorted(b.tags) == ["FLOW_1", "TEST_A", "TEST_B"]
    assert decode_block(b, [1.5, 2.5]) == {"FLOW_1": 1.5, "TEST_A": 2.5, "TEST_B": 2.5}


def test_bits_of_one_integer_are_read_once() -> None:
    p = plan([T("RUN", "STATUS.0", "bool"), T("FAULT", "STATUS.5", "bool"), T("WORD", "STATUS", "uint16")])
    (b,) = p.blocks
    assert b.names == ["STATUS"]
    assert decode_block(b, [33]) == {"RUN": True, "FAULT": True, "WORD": 33}


def test_every_tag_lands_in_exactly_one_block_or_invalid() -> None:
    tags = [T(f"T{i}", f"TAG_{i % 70}") for i in range(150)] + [T("BAD", "F28:0"), T("EMPTY", "")]
    p = plan(tags, micro800=True)
    served = [t for b in p.blocks for t in b.tags]
    assert len(served) == len(set(served))
    assert sorted(served + list(p.invalid)) == sorted(t.name for t in tags)
    assert set(p.invalid) == {"BAD", "EMPTY"}
    assert p.invalid["EMPTY"] == "empty address"
    all_names = [n for b in p.blocks for n in b.names]
    assert len(all_names) == len(set(all_names)) == 70


def test_disabled_tags_are_ignored() -> None:
    p = plan([T("A", "FT_01"), T("B", "FT_02", enabled=False), T("C", "", enabled=False)])
    assert [b.tags for b in p.blocks] == [["A"]]
    assert p.invalid == {}


def test_micro800_program_tag_is_invalid_in_plan() -> None:
    p = plan([T("X", "Program:Main.X")], micro800=True)
    assert p.blocks == [] and "program-scoped" in p.invalid["X"]


# --- wire request estimate ------------------------------------------------------------

def test_micro800_sends_one_request_per_name() -> None:
    p = plan([T(f"T{i}", f"TAG_{i}") for i in range(45)], micro800=True)
    assert [b.wire_requests() for b in p.blocks] == [40, 5]
    assert p.wire_requests == 45


def test_logix_large_connection_packs_a_batch_in_one_packet() -> None:
    p = plan([T(f"T{i}", f"FLOW_TOTALIZER_{i:02}") for i in range(40)])
    assert p.blocks[0].connection_size == 4000
    assert p.wire_requests == 1


def test_logix_small_connection_needs_several_packets() -> None:
    p = plan([T(f"T{i}", f"FLOW_TOTALIZER_{i:02}") for i in range(40)], connection_size=500)
    assert p.blocks[0].connection_size == 500
    assert p.wire_requests > 1


def test_single_name_is_one_request() -> None:
    assert plan([T("A", "FT_01")]).wire_requests == 1
    assert plan([T("A", "FT_01")], micro800=True).wire_requests == 1


def test_oversized_tag_needs_fragmented_reads() -> None:
    p = plan([T("TXT", "LONG_STRING", "string"), T("A", "FT_01")], connection_size=60)
    assert p.wire_requests >= 3       # >= 2 fragments for the string + 1 packet for FT_01


def test_connection_size_defaults() -> None:
    assert plan([T("A", "X")]).blocks[0].connection_size == 4000
    assert plan([T("A", "X")], micro800=True).blocks[0].connection_size == 500
    assert plan([T("A", "X")], micro800=True, connection_size=None).blocks[0].connection_size == 500


# Calibration against pycomm3's own request builder (no socket is opened).

_PYCOMM3_TYPE = {"float32": "REAL", "uint16": "UINT", "int16": "INT", "bool": "BOOL",
                 "uint32": "UDINT", "int32": "DINT"}


def _pycomm3_requests(tags: list[Tag], micro800: bool, connection_size: int) -> int:
    pytest.importorskip("pycomm3")
    from pycomm3 import LogixDriver

    plc = LogixDriver("192.0.2.15", init_tags=False)
    plc._micro800 = micro800
    plc._cfg["connection_size"] = connection_size
    plc._cfg["use_instance_ids"] = False     # Micro800 and pre-v21 Logix; our estimate's worst case
    for t in tags:
        base = t.address.split(".")[0].split("[")[0]
        atomic = {"tag_type": "atomic", "data_type": _PYCOMM3_TYPE[t.dtype],
                  "data_type_name": _PYCOMM3_TYPE[t.dtype], "dim": 0}
        if "." in t.address:
            member = t.address.split(".")[1].split("[")[0]
            plc._tags[base] = {"tag_type": "struct", "data_type_name": "UDT",
                               "data_type": {"internal_tags": {member: atomic},
                                             "template": {"structure_size": 64}}}
        else:
            plc._tags[base] = atomic
    names = sorted({t.address for t in tags})
    parsed = plc._parse_requested_tags(names, "r")
    assert not any(v.get("error") for v in parsed.values())
    return len(plc._read_build_requests(parsed))


@pytest.mark.parametrize("connection_size", [500, 4000])
@pytest.mark.parametrize("micro800", [False, True])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_wire_estimate_matches_pycomm3_packing(connection_size: int, micro800: bool, seed: int) -> None:
    rng = random.Random(seed)
    dtypes = list(_PYCOMM3_TYPE)
    tags = []
    for i in range(40):
        name = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ_") for _ in range(rng.randint(3, 30)))
        address = f"{name}{i}"
        kind = rng.random()
        if kind < 0.2:
            address += f"[{rng.choice([3, 300, 70000])}]"
        elif kind < 0.4:
            address += f".M{rng.randint(1, 9999)}"
        tags.append(T(f"T{i}", address, rng.choice(dtypes)))
    p = plan(tags, micro800=micro800, connection_size=connection_size, max_batch=40)
    (b,) = p.blocks
    assert b.wire_requests() == _pycomm3_requests(tags, micro800, connection_size)


# --- decode -------------------------------------------------------------------------

def test_decode_coerces_to_the_configured_dtype() -> None:
    p = plan([T("U", "U16", "uint16"), T("F", "F32", "float32"), T("B", "B1", "bool"),
              T("S", "S16", "int16"), T("D", "U32", "uint32")], micro800=True)
    (b,) = p.blocks
    assert b.names == ["B1", "F32", "S16", "U16", "U32"]
    out = decode_block(b, [1, 3, 65535, -1, -1])
    assert out == {"B": True, "F": 3.0, "S": -1, "U": 65535, "D": 4294967295}
    assert isinstance(out["F"], float)


def test_decode_errors() -> None:
    (b,) = plan([T("A", "A1"), T("B", "B1"), T("C", "C1")]).blocks
    out = decode_block(b, [1.0, None])
    assert out["A"] == 1.0
    assert isinstance(out["B"], DecodeError)      # pycomm3 gave no value
    assert isinstance(out["C"], DecodeError)      # reply shorter than the batch
    assert isinstance(decode_block(b, [1.0, "x", 2.0])["B"], DecodeError)


def test_describe() -> None:
    (b,) = plan([T(f"T{i}", f"TAG_{i}") for i in range(3)], micro800=True).blocks
    text = b.describe()
    assert "3 names" in text and "Micro800" in text and "3 requests" in text
