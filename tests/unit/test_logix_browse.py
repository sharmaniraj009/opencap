"""Automatic tag generation for ab_logix (#11): pycomm3's uploaded tag list -> opencap tags.

The entries below have the shape pycomm3 1.2.16 builds from the tag-list upload (checked against
tests/helpers/cip_sim.py in tests/integration/test_logix_browse_sim.py). Multi-dimensional arrays,
BOOL arrays, nested UDTs, arrays of UDTs and program-scoped tags are not simulated (D-024), so they
are covered here only.
"""

from __future__ import annotations

from typing import Any

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers.ab_logix import MAX_EXPANDED_ELEMENTS, AbLogixDriver, browse_tag_list
from opencap.drivers.base import BrowseResult, ConnectionLost, Driver


def atomic(type_name: str, *dims: int, access: str = "Read/Write", **extra: Any) -> dict[str, Any]:
    d = [*dims, 0, 0, 0][:3]
    return {"tag_type": "atomic", "data_type": type_name, "data_type_name": type_name, "dim": len(dims),
            "dimensions": d, "external_access": access, "alias": False, **extra}


def member(type_name: str, *, array: int = 0, struct: dict | None = None, bit: int | None = None) -> dict:
    m: dict[str, Any] = {"offset": 0, "tag_type": "struct" if struct else "atomic",
                         "data_type": struct or type_name, "data_type_name": type_name, "array": array}
    if bit is not None:
        m["bit"] = bit
    return m


def struct(type_name: str, members: dict[str, dict], *dims: int, string: int | None = None,
           hidden: tuple[str, ...] = ()) -> dict[str, Any]:
    dt: dict[str, Any] = {"name": type_name, "internal_tags": {**members, **{h: member("SINT") for h in hidden}},
                          "attributes": list(members)}
    if string is not None:
        dt["string"] = string
    d = [*dims, 0, 0, 0][:3]
    return {"tag_type": "struct", "data_type": dt, "data_type_name": type_name, "dim": len(dims),
            "dimensions": d, "external_access": "Read/Write", "alias": False}


STRING = struct("STRING", {"LEN": member("DINT"), "DATA": member("SINT", array=82)}, string=82)


def names(result: BrowseResult) -> list[str]:
    return [t.name for t in result.tags]


def test_the_default_browse_is_not_supported():
    class NoBrowse(Driver):
        name = "x"

        def plan(self, tags):  # pragma: no cover - not used
            raise AssertionError

    dev = Device(id="A/B", channel="A", name="B", driver="modbus_tcp", host="192.0.2.1", port=502, tags=[])
    with pytest.raises(NotImplementedError, match="browse not supported"):
        import asyncio
        asyncio.run(NoBrowse(dev).browse())


@pytest.mark.parametrize("type_name,dtype", [
    ("BOOL", "bool"), ("SINT", "int16"), ("USINT", "uint16"), ("INT", "int16"), ("UINT", "uint16"),
    ("DINT", "int32"), ("UDINT", "uint32"), ("LINT", "int64"), ("ULINT", "uint64"), ("REAL", "float32"),
    ("LREAL", "float64"), ("SHORT_STRING", "string"),
])
def test_atomic_types(type_name, dtype):
    out = browse_tag_list({"T": atomic(type_name)})
    assert out.tags == [Tag(name="T", address="T", dtype=dtype, description=type_name)] and out.skipped == {}


def test_logix_string_structures_are_strings():
    out = browse_tag_list({"S": STRING, "S20": struct("STRING20", {"LEN": member("DINT"), "DATA": member("SINT", array=20)},
                                                      string=20)})
    assert [(t.name, t.dtype, t.description) for t in out.tags] == [("S", "string", "STRING"), ("S20", "string", "STRING20")]


@pytest.mark.parametrize("type_name", ["BYTE", "WORD", "DWORD", "LWORD", "TIME", "MESSAGE"])
def test_unsupported_atomic_types_are_skipped_with_a_reason(type_name):
    out = browse_tag_list({"X": atomic(type_name)})
    assert out.tags == [] and out.skipped == {"X": f"type {type_name} is not supported by ab_logix (read it by hand "
                                                    "if it is atomic)"}


def test_arrays_are_skipped_unless_expanded():
    tl = {"ARR": atomic("INT", 3)}
    assert browse_tag_list(tl).skipped == {
        "ARR": "array INT[3]: ab_logix reads single elements only; use --expand-arrays for one tag per element"}
    out = browse_tag_list(tl, expand_arrays=True)
    assert [(t.name, t.address, t.dtype, t.description) for t in out.tags] == [
        ("ARR[0]", "ARR[0]", "int16", "INT[3] element"), ("ARR[1]", "ARR[1]", "int16", "INT[3] element"),
        ("ARR[2]", "ARR[2]", "int16", "INT[3] element")]


def test_multi_dimensional_arrays_expand_row_major():
    out = browse_tag_list({"M": atomic("DINT", 2, 3)}, expand_arrays=True)
    assert names(out) == ["M[0,0]", "M[0,1]", "M[0,2]", "M[1,0]", "M[1,1]", "M[1,2]"]
    assert len(browse_tag_list({"C": atomic("REAL", 2, 2, 2)}, expand_arrays=True).tags) == 8


def test_bool_arrays_and_huge_arrays_are_skipped():
    out = browse_tag_list({"B": atomic("BOOL", 64), "H": atomic("REAL", MAX_EXPANDED_ELEMENTS + 1)}, expand_arrays=True)
    assert out.tags == []
    assert "DWORD-packed" in out.skipped["B"] and f"more than {MAX_EXPANDED_ELEMENTS}" in out.skipped["H"]
    assert len(browse_tag_list({"H": atomic("REAL", MAX_EXPANDED_ELEMENTS)}, expand_arrays=True).tags) == MAX_EXPANDED_ELEMENTS


def test_udt_members_one_level():
    udt = struct("Pump", {"SPEED": member("REAL"), "RUN": member("BOOL", bit=0), "COUNT": member("DINT"),
                          "HIST": member("REAL", array=2), "NAME": member("STRING", struct=STRING["data_type"]),
                          "MOTOR": member("Motor", struct={"name": "Motor", "internal_tags": {}, "attributes": []})},
                 hidden=("ZZZZZZZZZZPump0",))
    out = browse_tag_list({"PUMP_1": udt})
    assert [(t.name, t.address, t.dtype, t.description) for t in out.tags] == [
        ("PUMP_1.SPEED", "PUMP_1.SPEED", "float32", "Pump.SPEED: REAL"),
        ("PUMP_1.RUN", "PUMP_1.RUN", "bool", "Pump.RUN: BOOL"),
        ("PUMP_1.COUNT", "PUMP_1.COUNT", "int32", "Pump.COUNT: DINT"),
    ]
    assert set(out.skipped) == {"PUMP_1.HIST", "PUMP_1.NAME", "PUMP_1.MOTOR"}           # hidden host not listed
    assert "use --expand-arrays" in out.skipped["PUMP_1.HIST"]
    assert "STRING members are not simulated" in out.skipped["PUMP_1.NAME"]
    assert "one level" in out.skipped["PUMP_1.MOTOR"]
    expanded = browse_tag_list({"PUMP_1": udt}, expand_arrays=True)
    assert "PUMP_1.HIST[0]" in names(expanded) and "PUMP_1.HIST[1]" in names(expanded)


def test_arrays_of_udts_program_scope_and_no_external_access_are_skipped():
    tl = {"UDTS": struct("Pump", {"SPEED": member("REAL")}, 4),
          "Program:MainProgram.LOCAL": atomic("DINT"),
          "HIDDEN": atomic("DINT", access="None"),
          "RO": atomic("DINT", access="Read Only"),
          "EMPTY": struct("Empty", {})}
    out = browse_tag_list(tl, expand_arrays=True)
    assert names(out) == ["RO"]
    assert out.skipped == {
        "EMPTY": "structure Empty has no readable members",
        "HIDDEN": "External Access is None: the controller does not serve it",
        "Program:MainProgram.LOCAL": "program-scoped tag: browse lists controller-scoped tags only",
        "UDTS": "array of structures Pump[4]: not supported",
    }


def test_sorted_by_name_without_case():
    out = browse_tag_list({"b": atomic("DINT"), "A": atomic("DINT"), "C": atomic("DINT")})
    assert names(out) == ["A", "b", "C"]


@pytest.mark.parametrize("micro800", [False, True])
def test_every_generated_tag_plans_cleanly(micro800):
    tl = {"R": atomic("REAL"), "S": STRING if not micro800 else atomic("SHORT_STRING"), "A": atomic("INT", 4),
          "M": atomic("DINT", 2, 2), "U": struct("Pump", {"SPEED": member("REAL"), "RUN": member("BOOL", bit=0),
                                                         "HIST": member("REAL", array=3)})}
    out = browse_tag_list(tl, expand_arrays=True)
    dev = Device(id="P/L", channel="P", name="L", driver="ab_logix", host="192.0.2.20", port=44818,
                 params={"micro800": micro800}, tags=out.tags)
    plan = AbLogixDriver(dev).plan(out.tags)
    assert plan.invalid == {} and sum(len(b.tags) for b in plan.blocks) == len(out.tags) == 15


async def test_browse_needs_a_connection():
    dev = Device(id="P/L", channel="P", name="L", driver="ab_logix", host="192.0.2.20", port=44818, tags=[])
    with pytest.raises(ConnectionLost, match="not connected"):
        await AbLogixDriver(dev).browse()
