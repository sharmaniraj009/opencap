"""Loader and value codec for the shared conformance vectors (ADR-0014, README.md here).

This module is the Python side of the file format only: it knows nothing about Modbus or
payloads. `reference.py` runs the Python reference on a vector's input, the generator
(`scripts/gen_conformance.py`) writes the files, and `tests/unit/test_conformance_vectors.py`
checks them. Another engine reimplements this ~100-line loader in its own language.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = 1
ROOT = Path(__file__).resolve().parent
GENERATED_BY = "scripts/gen_conformance.py"

# Areas whose vector files this module reads, each a directory under tests/conformance/.
# (`config/` is the older config fixture set with its own format: see config/compare.py.)
AREAS = ("modbus/plan", "modbus/decode", "scaling", "payload", "payload/iotgw",
         "ab_logix/plan", "ab_logix/decode", "ab_pccc/plan", "ab_pccc/decode", "system_tags", "quality")

_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")


@dataclass(frozen=True)
class Vector:
    area: str            # "modbus/plan"
    file: str            # path relative to tests/conformance, "modbus/plan/gaps.json"
    id: str              # unique within the file
    description: str
    input: Any
    expected: Any

    @property
    def key(self) -> str:
        """Unique across all vectors: `<file without .json>/<id>`, e.g. `modbus/plan/gaps/gap-4`."""
        return f"{self.file.removesuffix('.json')}/{self.id}"


# --- values ----------------------------------------------------------------------------------
#
# Tag values keep their exact type and bits: JSON alone cannot tell 1 from 1.0, has no NaN or
# infinity, and many parsers lose precision above 2**53 or in the last bit of a double.
#
#   {"t": "none"}                       no value (Python None)
#   {"t": "bool",  "v": true}
#   {"t": "int",   "v": "-32768"}       decimal text, any size (uint64 included)
#   {"t": "float", "v": "0.1"}          Python repr(): shortest text that parses back to the
#                                        same double; "nan", "inf", "-inf", "-0.0", "1e+16"
#   {"t": "str",   "v": "text"}
#   {"t": "list",  "v": [<value>, ...]} array tags, nested for 2-D
#   {"t": "decode_error", "v": "reason"} a per-tag decode failure (DecodeError)
#
# The same encoding is used by engine/crates/opencap-north/tests/golden/payload_cases.json.

def encode_value(v: Any) -> dict[str, Any]:
    if v is None:
        return {"t": "none"}
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, int):
        return {"t": "int", "v": str(v)}
    if isinstance(v, float):
        return {"t": "float", "v": repr(v)}
    if isinstance(v, str):
        return {"t": "str", "v": v}
    if isinstance(v, (list, tuple)):
        return {"t": "list", "v": [encode_value(x) for x in v]}
    reason = getattr(v, "reason", None)                 # opencap.drivers.base.DecodeError
    if type(v).__name__ == "DecodeError" and isinstance(reason, str):
        return {"t": "decode_error", "v": reason}
    raise TypeError(f"no conformance encoding for {type(v).__name__}: {v!r}")


def decode_value(obj: dict[str, Any]) -> Any:
    """Inverse of `encode_value` (a decode error comes back as `DecodeErrorValue`)."""
    t = obj["t"]
    if t == "none":
        return None
    v = obj["v"]
    if t == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"bad bool value {v!r}")
        return v
    if t == "int":
        return int(v)
    if t == "float":
        return float(v)
    if t == "str":
        return str(v)
    if t == "list":
        return [decode_value(x) for x in v]
    if t == "decode_error":
        return DecodeErrorValue(str(v))
    raise ValueError(f"unknown value tag {t!r}")


@dataclass(frozen=True)
class DecodeErrorValue:
    reason: str


def same(expected: Any, actual: Any) -> bool:
    """Equality of two JSON-shaped results, with NaN equal to NaN. Encoded values compare by
    their text, so 0.0 and -0.0 differ and int 1 never equals float 1.0."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(same(expected[k], actual[k]) for k in expected)
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(same(a, b) for a, b in zip(expected, actual, strict=True))
    if isinstance(expected, float) and isinstance(actual, float) and math.isnan(expected) and math.isnan(actual):
        return True
    return type(expected) is type(actual) and expected == actual


# --- files -----------------------------------------------------------------------------------

def area_dir(area: str) -> Path:
    if area not in AREAS:
        raise ValueError(f"unknown conformance area {area!r} (known: {', '.join(AREAS)})")
    return ROOT / area


def files(area: str) -> list[Path]:
    return sorted(area_dir(area).glob("*.json"))


def load_file(path: Path) -> list[Vector]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    rel = path.relative_to(ROOT).as_posix()
    if doc.get("format") != FORMAT:
        raise ValueError(f"{rel}: format {doc.get('format')!r}, this loader reads format {FORMAT}")
    area = doc["area"]
    if rel.rsplit("/", 1)[0] != area:
        raise ValueError(f"{rel}: area {area!r} does not match the file's directory")
    out: list[Vector] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc["vectors"]):
        vid = raw["id"]
        if not _ID_RE.fullmatch(vid) or vid in seen:
            raise ValueError(f"{rel}: vector {i} has a bad or duplicate id {vid!r}")
        if not raw.get("description", "").strip():
            raise ValueError(f"{rel}: vector {vid!r} has no description")
        seen.add(vid)
        out.append(Vector(area=area, file=rel, id=vid, description=raw["description"],
                          input=raw["input"], expected=raw["expected"]))
    return out


def load(area: str) -> list[Vector]:
    """Every vector of one area, in file then file order."""
    return [v for path in files(area) for v in load_file(path)]


def load_all() -> list[Vector]:
    return [v for area in AREAS for v in load(area)]


def render(area: str, description: str, vectors: list[dict[str, Any]]) -> str:
    """Canonical text of one vector file: a small header, then one vector per line, so a
    behaviour change shows up as a one-line diff per affected vector."""
    one = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)  # noqa: E731
    lines = [
        "{",
        f' "format": {FORMAT},',
        f' "area": {one(area)},',
        f' "description": {one(description)},',
        f' "generated_by": {one(GENERATED_BY)},',
        ' "vectors": [',
    ]
    lines += [f"  {one(v)}{',' if i < len(vectors) - 1 else ''}" for i, v in enumerate(vectors)]
    lines += [" ]", "}"]
    return "\n".join(lines) + "\n"
