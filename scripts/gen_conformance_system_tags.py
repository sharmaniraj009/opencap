"""System-tag conformance vector inputs (docs/spec/system-tags.md §13, ADR-0014).

Case data only: `scripts/gen_conformance.py` imports `SPECS` from here, computes every
`expected` with `tests/conformance/reference.py` and writes `tests/conformance/system_tags/*.json`.
Run that script, not this one.

The cases themselves are written by hand from the spec in `scripts/conformance_system_tags/*.json`
(an engine state or a device event list, and the values the spec says follow: partial, only the
tags a case is about). They are the independent check on the product code:
`tests/conformance/system_tags/test_system_tag_vectors.py` compares what `evaluate` makes of each
with those hand-written values, so a wrong spec reading cannot be "blessed" by regenerating.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parent / "conformance_system_tags"

Case = tuple[str, str, dict[str, Any]]          # (id, description, input)
FileSpec = tuple[str, str, list[Case]]          # (file stem, file description, cases)


def source_files() -> list[Path]:
    return sorted(SOURCE.glob("*.json"))


def to_input(case: dict[str, Any]) -> dict[str, Any]:
    """The vector `input` of one hand-written case."""
    if case["kind"] == "snapshot":
        return {"kind": "snapshot", "state": case["state"], "paths": list(case["expect"]),
                "absent": list(case.get("absent", []))}
    steps = []
    for step in case["steps"]:
        out = {k: v for k, v in step.items() if k != "expect"}
        if step.get("expect"):
            out["observe"] = list(step["expect"])
        steps.append(out)
    return {"kind": "device_sequence", "device": case["device"], "start": case["start"], "steps": steps}


def load_source() -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """{file stem: (description, hand-written cases)}"""
    out = {}
    for path in source_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        out[path.stem] = (doc["description"], doc["cases"])
    return out


def _cases() -> list[FileSpec]:
    specs: list[FileSpec] = []
    for stem, (description, cases) in load_source().items():
        specs.append((stem, description,
                      [(c["name"].lower(), f"{c['name']}: spec §" + ", §".join(c["sections"]),
                        to_input(c)) for c in cases]))
    return specs


SPECS: dict[str, list[FileSpec]] = {"system_tags": _cases()}
