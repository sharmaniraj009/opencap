"""MELSEC MC conformance vectors (tests/conformance/melsec_mc, #45, ADR-0014).

`tests/unit/test_conformance_vectors.py` already re-runs the Python reference against every
vector file and checks the files are what `scripts/gen_conformance.py` writes. This module
checks the MC vectors are complete and sharp: every enabled tag is planned or rejected exactly
once, and mutants of the word order, the non-normal float rule, the end-code classification,
the device ranges and the monitoring timer each fail vectors.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable

import pytest

pytest.importorskip("pymcprotocol")        # the melsec_mc/frame vectors are made with it

from opencap.drivers import melsec_mc  # noqa: E402
from tests.conformance import reference  # noqa: E402
from tests.conformance.vectors import Vector, load, same  # noqa: E402

MC_AREAS = ("melsec_mc/plan", "melsec_mc/decode", "melsec_mc/frame", "melsec_mc/reply")


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


@pytest.mark.parametrize("area", MC_AREAS)
def test_every_mc_area_has_vectors_and_the_reference_reproduces_them(area: str):
    assert len(load(area)) >= 20, area
    assert _failing(area) == []


def _plan_vectors() -> list[Vector]:
    return [v for v in load("melsec_mc/plan") if "blocks" in v.expected]


def test_plans_account_for_every_enabled_tag_exactly_once():
    for v in _plan_vectors():
        enabled = sorted(t["name"] for t in v.input["tags"] if t.get("enabled", True))
        planned = [n for b in v.expected["blocks"] for it in b["items"] for n in it["names"]]
        assert sorted(planned + list(v.expected["invalid"])) == enabled, v.key
        for b in v.expected["blocks"]:
            assert sorted(b["tags"]) == sorted(n for it in b["items"] for n in it["names"]), v.key
            assert b["count"] <= 960, v.key


def test_every_device_of_the_table_appears_in_a_plan_vector():
    planned = {b["device"] for v in _plan_vectors() for b in v.expected["blocks"]}
    assert set(melsec_mc.DEVICES) <= planned, set(melsec_mc.DEVICES) - planned


def _combine_ignoring_order(words, first_word_low):
    return sum(w << (16 * i) for i, w in enumerate(words))


def _float_always_unmodified(u: int, width: int, p: melsec_mc.McParams) -> float:
    import struct
    return struct.unpack(">f" if width == 32 else ">d", u.to_bytes(width // 8, "big"))[0]


def _mutants() -> dict[str, tuple[str, Callable[[pytest.MonkeyPatch], None]]]:
    """name -> (area whose vectors must catch it, how to break the reference)."""
    d_spec = melsec_mc.DEVICES["D"]
    return {
        "first_word_low ignored": (
            "melsec_mc/decode", lambda mp: mp.setattr(melsec_mc, "combine_words", _combine_ignoring_order)),
        "non-normal floats always kept": (
            "melsec_mc/decode", lambda mp: mp.setattr(melsec_mc, "_float", _float_always_unmodified)),
        "end code 0xC059 a comm error": (
            "melsec_mc/reply", lambda mp: mp.setattr(
                melsec_mc, "CONFIG_END_CODES", melsec_mc.CONFIG_END_CODES - {0xC059})),
        "D range shortened": (
            "melsec_mc/plan", lambda mp: mp.setitem(melsec_mc.DEVICES, "D", dataclasses.replace(d_spec, last=1000))),
        "monitoring timer rounds down": (
            "melsec_mc/frame", lambda mp: mp.setattr(
                melsec_mc, "monitoring_timer", lambda ms: max(1, min(0xFFFF, math.floor(ms / 250))))),
        "reply length not checked": (
            "melsec_mc/reply", lambda mp: mp.setattr(
                melsec_mc, "check_reply_frame", lambda frame: int.from_bytes(frame[7:9], "little"))),
    }


@pytest.mark.parametrize("name", list(_mutants()))
def test_a_mutant_of_the_reference_fails_vectors(name: str, monkeypatch: pytest.MonkeyPatch):
    area, patch = _mutants()[name]
    patch(monkeypatch)
    assert _failing(area), f"mutant {name!r} passes every {area} vector: the vectors are too weak"
