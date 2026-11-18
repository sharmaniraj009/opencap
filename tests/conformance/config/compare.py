"""Cross-language config conformance (#39, ADR-0003): the Rust models must make of every shared
fixture exactly what the Python models make of it.

For each `valid/*.json` fixture the document, with every default filled in, must be the same
in both engines (`model_dump(mode="json")` vs serde), and a `devices` document loaded as a
config directory on its own must resolve to the same channels and warnings (`load_config`:
layering and default topics). Every `invalid/*.json` fixture must be refused by both. Error
texts are compared by the per-engine tests (`tests/unit/test_config_schema.py`,
`engine/crates/opencap-core/tests/config_conformance.rs`), not here.

    cd engine && cargo build -p opencap-core --example config_dump && cd ..
    python tests/conformance/config/compare.py --dumper engine/target/debug/examples/config_dump

Exit status 0 = identical, 1 = differences (listed), 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from opencap.config import schema as cfg_schema
from opencap.config.loader import load_config

HERE = Path(__file__).resolve().parent
FIXTURES = sorted(HERE.glob("valid/*.json")) + sorted(HERE.glob("invalid/*.json"))


def python_dump(path: Path) -> dict[str, Any]:
    """What the Python models make of one fixture (same shape as the Rust dumper's entry)."""
    fx = json.loads(path.read_text(encoding="utf-8"))
    _, model = cfg_schema.DOCUMENTS[fx["document"]]
    try:
        dumped = model.model_validate(fx["data"]).model_dump(mode="json")
    except (ValidationError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    entry: dict[str, Any] = {"ok": True, "model": dumped}
    if fx["document"] == "devices":
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "devices.json").write_text(json.dumps(fx["data"]), encoding="utf-8")
            try:
                cfg = load_config(Path(tmp))
            except (ValidationError, ValueError) as exc:
                entry["resolved"] = {"error": str(exc)}
            else:
                entry["resolved"] = {"channels": [c.model_dump(mode="json") for c in cfg.channels],
                                     "warnings": list(cfg.warnings)}
    return entry


def rust_dump(dumper: Path, paths: list[Path]) -> dict[str, Any]:
    out = subprocess.run([str(dumper), *map(str, paths)], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"{dumper} exited with {out.returncode}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def diff(a: Any, b: Any, where: str = "") -> list[str]:
    """Differences between the Python (`a`) and Rust (`b`) JSON values. 1 == 1.0 (JSON has one
    number type; pydantic and serde spell whole floats differently)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = [f"{where}.{k}: only in Python" for k in a.keys() - b.keys()]
        out += [f"{where}.{k}: only in Rust" for k in b.keys() - a.keys()]
        for k in sorted(a.keys() & b.keys()):
            out += diff(a[k], b[k], f"{where}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{where}: {len(a)} items in Python, {len(b)} in Rust"]
        return [d for i, (x, y) in enumerate(zip(a, b, strict=True)) for d in diff(x, y, f"{where}[{i}]")]
    same_number = (isinstance(a, int | float) and isinstance(b, int | float)
                   and not isinstance(a, bool) and not isinstance(b, bool) and a == b)
    if a == b and type(a) is type(b) or same_number:
        return []
    return [f"{where}: Python {a!r}, Rust {b!r}"]


def compare(dumper: Path, paths: list[Path] = FIXTURES) -> list[str]:
    rust = rust_dump(dumper, paths)
    problems: list[str] = []
    for path in paths:
        name, kind = path.name, path.parent.name
        py, rs = python_dump(path), rust.get(path.name)
        if rs is None:
            problems.append(f"{kind}/{name}: missing from the Rust dump")
            continue
        if kind == "invalid":
            for engine, entry in (("Python", py), ("Rust", rs)):
                if entry["ok"]:
                    problems.append(f"invalid/{name}: accepted by {engine}")
            continue
        if not (py["ok"] and rs["ok"]):
            problems.append(f"valid/{name}: Python ok={py['ok']} {py.get('error', '')!s:.200}; "
                            f"Rust ok={rs['ok']} {rs.get('error', '')}")
            continue
        problems += [f"valid/{name}: model{d}" for d in diff(py["model"], rs["model"])]
        if "resolved" in py or "resolved" in rs:
            problems += [f"valid/{name}: resolved{d}" for d in diff(py.get("resolved"), rs.get("resolved"))]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dumper", type=Path, required=True,
                        help="the Rust dumper (cargo build -p opencap-core --example config_dump)")
    args = parser.parse_args(argv)
    if not args.dumper.exists():
        print(f"{args.dumper} not found: cd engine && cargo build -p opencap-core --example config_dump",
              file=sys.stderr)
        return 2
    try:
        problems = compare(args.dumper)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    valid = sum(1 for p in FIXTURES if p.parent.name == "valid")
    if problems:
        print(f"{len(problems)} difference(s) between the Python and Rust config models:")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    print(f"Python and Rust agree on {valid} valid and {len(FIXTURES) - valid} invalid fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
