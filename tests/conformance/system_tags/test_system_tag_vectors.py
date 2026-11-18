"""System-tag conformance vectors (`docs/spec/system-tags.md` §13).

Checks that the vectors are well formed, agree with the catalogue, and agree with the spec as
evaluated by the test oracle in `reference.py`; and that the spec documents every catalogue name.
The Rust engine (#56) runs the same vectors through its own state -> tags function.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.system_tags import reference

HERE = Path(__file__).resolve().parent
SPEC = HERE.parents[2] / "docs" / "spec" / "system-tags.md"
CATALOGUE = json.loads((HERE / "catalogue.json").read_text(encoding="utf-8"))
VECTOR_FILES = sorted((HERE / "vectors").glob("*.json"))
LEVELS = set(CATALOGUE["levels"])
DTYPES = set(CATALOGUE["dtypes"])
DTYPE_OF = {(t["level"], t["name"]): t["dtype"] for t in CATALOGUE["tags"]}
EVENTS = {"connect_ok", "connect_failed", "cycle_good", "cycle_failed", "cycle_neutral", "tick"}


def _cases() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in VECTOR_FILES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for case in doc["cases"]:
            out.append((f"{path.stem}:{case['name']}", case))
    return out


CASES = _cases()
SNAPSHOTS = [(cid, c) for cid, c in CASES if c["kind"] == "snapshot"]
SEQUENCES = [(cid, c) for cid, c in CASES if c["kind"] == "device_sequence"]


def _type_ok(dtype: str, value: Any) -> bool:
    if dtype == "bool":
        return isinstance(value, bool)
    if dtype in ("uint32", "uint64"):
        limit = 2**32 if dtype == "uint32" else 2**64
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < limit
    if dtype == "float64":
        return isinstance(value, float) and math.isfinite(value)
    if dtype == "string":
        return isinstance(value, str)
    if dtype == "datetime":
        return value is None or (isinstance(value, str)
                                 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value) is not None)
    if dtype == "string_array":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    raise AssertionError(f"unknown dtype {dtype}")


def _level(path: str) -> tuple[str, str]:
    parts = path.split(".")
    name = parts[-1]
    if parts[0] == "_System":
        if parts[1] == "_North":
            return "north", name
        if parts[1] == "_Redundancy":
            return "redundancy", name
        return "node", name
    if parts[1] in ("_System", "_Statistics"):
        return ("channel" if parts[1] == "_System" else "channel_statistics"), name
    return ("device" if parts[2] == "_System" else "device_statistics"), name


# --- catalogue and spec ------------------------------------------------------------------------

def test_catalogue_is_consistent():
    seen = set()
    for t in CATALOGUE["tags"]:
        assert t["level"] in LEVELS, t
        assert t["dtype"] in DTYPES, t
        assert t["update"] in ("event", "tick", "config"), t
        assert t["present"] in CATALOGUE["presence"], t
        assert re.fullmatch(r"_[A-Za-z][A-Za-z0-9_]*", t["name"]), t
        key = (t["level"], t["name"])
        assert key not in seen, f"duplicate {key}"
        seen.add(key)


def test_spec_documents_every_catalogue_tag():
    spec = SPEC.read_text(encoding="utf-8")
    missing = sorted({t["name"] for t in CATALOGUE["tags"] if f"`{t['name']}`" not in spec})
    assert not missing, f"catalogue tags not in docs/spec/system-tags.md: {missing}"


def test_vectors_exist_for_every_kind():
    assert SNAPSHOTS and SEQUENCES


# --- vectors -----------------------------------------------------------------------------------

@pytest.mark.parametrize(("cid", "case"), SNAPSHOTS + SEQUENCES, ids=[c for c, _ in SNAPSHOTS + SEQUENCES])
def test_vector_is_well_formed(cid: str, case: dict[str, Any]):
    expects: list[tuple[str, dict[str, Any]]] = []
    if case["kind"] == "snapshot":
        assert set(case) <= {"kind", "name", "sections", "state", "expect", "absent"}, cid
        expects = [(p, e) for p, e in case["expect"].items()]
        for p in case.get("absent", []):
            assert p not in case["expect"], f"{cid}: {p} both expected and absent"
    else:
        assert set(case) <= {"kind", "name", "sections", "device", "start", "steps"}, cid
        for step in case["steps"]:
            assert step["event"] in EVENTS, f"{cid}: {step}"
            for name, e in step.get("expect", {}).items():
                expects.append((f"X.Y._System.{name}", e))
    assert expects, f"{cid}: asserts nothing"
    for path, e in expects:
        level = _level(path)
        assert level in DTYPE_OF, f"{cid}: {path} is not in the catalogue"
        assert e["dtype"] == DTYPE_OF[level], f"{cid}: {path} dtype {e['dtype']} != catalogue"
        assert _type_ok(e["dtype"], e["value"]), f"{cid}: {path} value {e['value']!r} is not a {e['dtype']}"


@pytest.mark.parametrize(("cid", "case"), SNAPSHOTS, ids=[c for c, _ in SNAPSHOTS])
def test_snapshot_vector_matches_the_spec(cid: str, case: dict[str, Any]):
    got = reference.evaluate(case["state"])
    for path, e in case["expect"].items():
        assert path in got, f"{cid}: {path} missing"
        assert got[path] == (e["dtype"], e["value"]), f"{cid}: {path}"
    for path in case.get("absent", []):
        assert not any(p == path or p.startswith(path + ".") for p in got), f"{cid}: {path} must be absent"


@pytest.mark.parametrize(("cid", "case"), SEQUENCES, ids=[c for c, _ in SEQUENCES])
def test_sequence_vector_matches_the_spec(cid: str, case: dict[str, Any]):
    results = dict(reference.run_sequence(case))
    for index, step in enumerate(case["steps"]):
        for name, e in step.get("expect", {}).items():
            assert results[index][name] == (e["dtype"], e["value"]), f"{cid}: step {index} {name}"


def test_every_snapshot_tag_is_in_the_catalogue():
    """The oracle never produces a tag the catalogue does not list (and with the right dtype)."""
    for cid, case in SNAPSHOTS:
        for path, (dtype, value) in reference.evaluate(case["state"]).items():
            assert DTYPE_OF.get(_level(path)) == dtype, f"{cid}: {path}"
            assert _type_ok(dtype, value), f"{cid}: {path} = {value!r}"


def test_vectors_cover_every_catalogue_tag():
    covered = set()
    for _, case in SNAPSHOTS:
        covered |= {_level(p) for p in case["expect"]}
    for _, case in SEQUENCES:
        covered |= {("device", n) for step in case["steps"] for n in step.get("expect", {})}
    missing = sorted(set(DTYPE_OF) - covered)
    assert not missing, f"no vector asserts: {missing}"
