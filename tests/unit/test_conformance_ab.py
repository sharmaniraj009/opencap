"""Allen-Bradley conformance vectors (tests/conformance/ab_logix, ab_pccc; #5, ADR-0014).

`tests/unit/test_conformance_vectors.py` re-runs the Python reference against every vector
file, these included, and checks the files are what `scripts/gen_conformance.py` writes.
This module checks the AB vectors are complete and sharp: they cover every AB area, their
plans account for every enabled tag, and mutants of the PCCC block packing, the Logix
response-size estimate and the shared value coercion each fail vectors.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from opencap.drivers import ab_logix, ab_pccc
from tests.conformance import reference
from tests.conformance.vectors import Vector, load, same

AB_AREAS = ("ab_logix/plan", "ab_logix/decode", "ab_pccc/plan", "ab_pccc/decode")


def _failing(area: str) -> list[str]:
    out = []
    for v in load(area):
        try:
            actual: Any = reference.run(area, v.input)
        except Exception as exc:  # a crash is a failure too, reported by name
            actual = {"exception": f"{type(exc).__name__}: {exc}"}
        if not same(v.expected, actual):
            out.append(v.key)
    return out


@pytest.mark.parametrize("area", AB_AREAS)
def test_every_ab_area_has_vectors_and_the_reference_reproduces_them(area: str):
    vectors = load(area)
    assert len(vectors) >= 40, area
    assert _failing(area) == []


def _plan_vectors(area: str) -> list[Vector]:
    return [v for v in load(area) if "blocks" in v.expected]


@pytest.mark.parametrize("area", ["ab_logix/plan", "ab_pccc/plan"])
def test_plans_account_for_every_enabled_tag_exactly_once(area: str):
    for v in _plan_vectors(area):
        enabled = sorted(t["name"] for t in v.input["tags"] if t.get("enabled", True))
        planned = [it["tag"] for b in v.expected["blocks"] for it in b["items"]]
        assert sorted(planned + list(v.expected["invalid"])) == enabled, v.key
        for b in v.expected["blocks"]:
            assert sorted(b["tags"]) == sorted(it["tag"] for it in b["items"]), v.key


def test_pccc_plans_respect_the_255_byte_pccc_limit():
    for v in _plan_vectors("ab_pccc/plan"):
        for b in v.expected["blocks"]:
            assert b["count"] * b["elem_size"] <= ab_pccc.PCCC_MAX_BYTES or b["count"] == 1, v.key


def test_decode_vectors_cover_every_dtype():
    dtypes = {t["dtype"] for area in ("ab_logix/decode", "ab_pccc/decode") for v in load(area) for t in v.input["tags"]}
    assert dtypes == {"bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64", "string"}


# --- sharpness ------------------------------------------------------------------------------

def _no_odd_padding(read_name: str) -> int:
    size = 1
    for part in read_name.split("."):
        ident, _, idx = part.partition("[")
        size += 2 + len(ident.encode())
        if idx:
            size += sum(ab_logix._member_size(int(i)) for i in idx.rstrip("]").split(","))
    return size


def _unsigned_not_masked(coerce: Callable[[Any, str], Any]) -> Callable[[Any, str], Any]:
    def mutant(raw: Any, dtype: str) -> Any:
        if dtype.startswith("uint") and type(raw) is int and raw < 0:
            return raw
        return coerce(raw, dtype)
    return mutant


_decode_item = ab_pccc._decode_item


def _tc_bit_0(block: ab_pccc.PcccBlock, item: ab_pccc.PcccItem, raw: Any) -> Any:
    """pycomm3 already extracted a T/C bit; this mutant wrongly takes bit 0 of it again."""
    if item.field is not None and item.field not in ab_pccc.TC_WORD_FIELDS and type(raw) is int:
        return ab_pccc.coerce_value(bool(raw & 1), item.dtype)
    return _decode_item(block, item, raw)


def _mutants() -> dict[str, tuple[str, Callable[[pytest.MonkeyPatch], None]]]:
    """name -> (area whose vectors must catch it, how to break the reference)."""
    coerce = ab_pccc.coerce_value
    return {
        # PCCC block packing (issue test plan)
        "pccc 255-byte cap dropped": ("ab_pccc/plan", lambda mp: mp.setattr(ab_pccc, "PCCC_MAX_BYTES", 10_000)),
        "pccc L elements sized as words": ("ab_pccc/plan", lambda mp: mp.setitem(ab_pccc.ELEM_SIZE, "L", 2)),
        "pccc ST reads not capped at 2": ("ab_pccc/plan", lambda mp: mp.setattr(ab_pccc, "STRING_MAX_COUNT", 50)),
        "pccc bits not ordered within a word": (
            "ab_pccc/plan", lambda mp: mp.setattr(ab_pccc, "_sort_bit", lambda a: 0)),
        "pccc I/O merged by slot instead of word": (
            "ab_pccc/plan", lambda mp: mp.setattr(ab_pccc, "_index", lambda a: a.element)),
        # Logix response-size estimate (issue test plan)
        "logix packet header not counted": (
            "ab_logix/plan", lambda mp: mp.setattr(ab_logix, "MULTISERVICE_READ_OVERHEAD", 0)),
        "logix fragment overhead not counted": (
            "ab_logix/plan", lambda mp: mp.setattr(ab_logix, "FRAGMENT_REPLY_OVERHEAD", 0)),
        "logix bit host read as a byte": ("ab_logix/plan", lambda mp: mp.setattr(ab_logix, "_BIT_HOST_SIZE", 1)),
        "logix STRING sized 82 bytes": ("ab_logix/plan", lambda mp: mp.setitem(ab_logix._DTYPE_SIZE, "string", 82)),
        "logix path without odd padding": (
            "ab_logix/plan", lambda mp: mp.setattr(ab_logix, "epath_size", _no_odd_padding)),
        # shared coercion
        "logix unsigned not masked": (
            "ab_logix/decode", lambda mp: mp.setattr(ab_logix, "coerce_value", _unsigned_not_masked(coerce))),
        "pccc unsigned not masked": (
            "ab_pccc/decode", lambda mp: mp.setattr(ab_pccc, "coerce_value", _unsigned_not_masked(coerce))),
        "pccc T/C bit taken as bit 0": ("ab_pccc/decode", lambda mp: mp.setattr(ab_pccc, "_decode_item", _tc_bit_0)),
    }


@pytest.mark.parametrize("name", list(_mutants()))
def test_a_mutant_of_the_reference_fails_vectors(name: str, monkeypatch: pytest.MonkeyPatch):
    area, patch = _mutants()[name]
    patch(monkeypatch)
    failing = _failing(area)
    assert failing, f"mutant {name!r} passes every {area} vector: the vectors are too weak"


def test_vectors_are_json_round_trippable():
    for area in AB_AREAS:
        for v in load(area):
            assert json.loads(json.dumps(v.input)) == v.input
