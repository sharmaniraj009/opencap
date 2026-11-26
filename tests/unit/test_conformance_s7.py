"""S7 conformance vectors (tests/conformance/s7, #44, ADR-0014).

`tests/unit/test_conformance_vectors.py` already re-runs the Python reference against every
vector file and checks the files are what `scripts/gen_conformance.py` writes. This module
checks the S7 vectors are complete and sharp: every enabled tag is planned or rejected exactly
once, no block exceeds the PDU span, and mutants of the byte order, the BCD rule and the PDU span
each fail vectors.
"""

from __future__ import annotations

from collections.abc import Callable

import dataclasses

import pytest

pytest.importorskip("snap7")        # the s7/frame and s7/reply vectors are made with it

from opencap.drivers import s7  # noqa: E402
from tests.conformance import reference  # noqa: E402
from tests.conformance.vectors import Vector, load, same  # noqa: E402

S7_AREAS = ("s7/plan", "s7/decode", "s7/frame", "s7/reply")


def _failing(area: str) -> list[str]:
    out = []
    for v in load(area):
        try:
            actual = reference.run(area, v.input)
        except Exception as exc:
            actual = {"exception": f"{type(exc).__name__}: {exc}"}
        if not same(v.expected, actual):
            out.append(v.key)
    return out


@pytest.mark.parametrize("area", S7_AREAS)
def test_every_s7_area_has_vectors_and_the_reference_reproduces_them(area: str):
    assert len(load(area)) >= 20, area
    assert _failing(area) == []


def _plan_vectors() -> list[Vector]:
    return [v for v in load("s7/plan") if "blocks" in v.expected]


def test_plans_account_for_every_enabled_tag_exactly_once_within_the_pdu():
    for v in _plan_vectors():
        enabled = sorted(t["name"] for t in v.input["tags"] if t.get("enabled", True))
        planned = [n for b in v.expected["blocks"] for it in b["items"] for n in it["names"]]
        assert sorted(planned + list(v.expected["invalid"])) == enabled, v.key
        pdu = v.input["params"].get("max_pdu") or 480
        for b in v.expected["blocks"]:
            unit = 2 if b["area"] in ("T", "C") else 1
            assert b["count"] * unit <= pdu - 18, v.key


def test_the_areas_of_the_address_space_are_all_planned_somewhere():
    areas = {b["area"] for v in _plan_vectors() for b in v.expected["blocks"]}
    assert areas == {"C", "DB", "I", "M", "Q", "T"}


def _mutants() -> dict[str, tuple[str, Callable[[pytest.MonkeyPatch], None]]]:
    """name -> (area whose vectors must catch it, how to break the reference)."""
    from_mapping = s7.S7Params.from_mapping
    return {
        "byte_order_big ignored": (
            "s7/decode", lambda mp: mp.setattr(s7.S7Params, "from_mapping", classmethod(
                lambda cls, params: dataclasses.replace(from_mapping(params), byte_order_big=True)))),
        "BCD digits above 9 accepted": (
            "s7/decode", lambda mp: mp.setattr(s7, "bcd_to_int", lambda w: (w & 0xF) + ((w >> 4) & 0xF) * 10
                                                + ((w >> 8) & 0xF) * 100 + ((w >> 12) & 0xF) * 1000)),
        "span is the whole PDU": (
            "s7/plan", lambda mp: mp.setattr(s7, "READ_OVERHEAD", 12)),
    }


@pytest.mark.parametrize("name", list(_mutants()))
def test_a_mutant_of_the_reference_fails_vectors(name: str, monkeypatch: pytest.MonkeyPatch):
    area, patch = _mutants()[name]
    patch(monkeypatch)
    assert _failing(area), f"mutant {name!r} passes every {area} vector: the vectors are too weak"
