#!/usr/bin/env python3
"""Licence gate for installed Python distributions (ADR-0007).

Usage: check_licenses.py [--allow licenses/python-allow.toml] [--json FILE]
Without --json it runs `pip-licenses` in the current interpreter's environment.
Exit 1 and print package + licence for every unknown or disallowed distribution.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# Free-text / classifier licence names -> SPDX id ("" = ambiguous, never auto-allowed).
NORMALISE = {
    "mit license": "MIT", "mit": "MIT", "mit-0": "MIT-0",
    "bsd license": "BSD-3-Clause",  # classifier is ambiguous; 2/3-clause both allowed
    "bsd": "BSD-3-Clause", "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",  # protobuf's metadata
    "apache software license": "Apache-2.0", "apache-2.0": "Apache-2.0", "apache 2.0": "Apache-2.0",
    "isc license (iscl)": "ISC", "isc": "ISC", "zlib": "Zlib",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0", "mpl-2.0": "MPL-2.0",
    "eclipse public license 2.0": "EPL-2.0", "epl-2.0": "EPL-2.0",
    "psf-2.0": "PSF-2.0", "python software foundation license": "PSF-2.0",
    "the unlicense (unlicense)": "Unlicense", "unlicense": "Unlicense", "0bsd": "0BSD",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "lgpl-3.0-or-later": "LGPL-3.0-or-later", "lgpl-3.0": "LGPL-3.0-or-later",
}
UNKNOWN = {"", "unknown", "unlicensed", "none", "other/proprietary license"}


def norm(token: str) -> str:
    t = token.strip()
    if t.lower() in NORMALISE:
        return NORMALISE[t.lower()]
    t = t.strip("()").strip()
    return NORMALISE.get(t.lower(), t)


def licence_ok(expr: str, allowed: set[str]) -> bool:
    """SPDX-ish: `A OR B` passes if any alternative passes; `A AND B` needs all; `;` is AND."""
    if expr.strip().lower() in UNKNOWN:
        return False
    for conj in re.split(r"\s+OR\s+", expr, flags=re.I):
        parts = [norm(p) for p in re.split(r"\s+AND\s+|;", conj, flags=re.I)]
        if parts and all(p in allowed for p in parts):
            return True
    return False


def evaluate(rows: list[dict], cfg: dict) -> list[str]:
    allowed = set(cfg.get("allow", {}).get("ids", []))
    exceptions = cfg.get("exceptions", {})
    overrides = cfg.get("overrides", {})
    test_only = cfg.get("test_only", {})
    test_names = {p.lower() for p in test_only.get("packages", [])}
    test_allowed = allowed | set(test_only.get("extra_ids", []))
    problems = []
    for row in rows:
        name, lic = row["Name"], row["License"]
        key = name.lower().replace("_", "-")
        if key == "opencap":
            continue
        if any(k.lower().replace("_", "-") == key for k in exceptions):
            continue
        ov = next((v for k, v in overrides.items() if k.lower().replace("_", "-") == key), None)
        if ov:
            lic = ov["licence"]
        pool = test_allowed if key in {n.replace("_", "-") for n in test_names} else allowed
        if not licence_ok(lic, pool):
            problems.append(f"{name} {row.get('Version', '')}: licence {lic!r} is not allowed (ADR-0007)")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow", default=str(Path(__file__).resolve().parent.parent / "licenses/python-allow.toml"))
    ap.add_argument("--json", help="pip-licenses JSON file (default: run pip-licenses)")
    a = ap.parse_args()
    cfg = tomllib.loads(Path(a.allow).read_text())
    if a.json:
        rows = json.loads(Path(a.json).read_text())
    else:
        out = subprocess.run([sys.executable, "-m", "piplicenses", "--format=json", "--from=mixed"],
                             check=True, capture_output=True, text=True).stdout
        rows = json.loads(out)
    problems = evaluate(rows, cfg)
    for p in problems:
        print(f"::error::{p}")
    print(f"checked {len(rows)} distributions, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
