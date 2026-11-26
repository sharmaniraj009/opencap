"""Export the configuration JSON Schemas (ADR-0003).

The Pydantic models in :mod:`opencap.config.models` are the single source; the committed files in
``schema/`` are generated from them and drift-tested. Usage::

    python -m opencap.config.schema --out schema/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from opencap.config.models import DeviceMap, DevicesFile, NodeConfig, Overrides, PollerConfig, ProjectV2

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
ID_BASE = "https://github.com/sharmaniraj009/opencap/schema/"
SCHEMA_VERSION = 1   # bump on a non-additive config change (ADR-0003) and ship a migration

# document name -> (file name, model)
DOCUMENTS: dict[str, tuple[str, type[BaseModel]]] = {
    "devices": ("devices.schema.json", DevicesFile),
    "poller": ("poller.schema.json", PollerConfig),
    "devices_map": ("devices_map.schema.json", DeviceMap),
    "overrides": ("overrides.schema.json", Overrides),
    "project": ("project.schema.json", ProjectV2),        # project v2 (ADR-0013)
    "node": ("node.schema.json", NodeConfig),
}

REGEN_HINT = "run `python -m opencap.config.schema --out schema/`"


def build_schema(name: str) -> dict[str, Any]:
    """Return the JSON Schema (2020-12) of one document, with ``$id`` and ``version``."""
    filename, model = DOCUMENTS[name]
    body = model.model_json_schema(mode="validation")
    body.pop("$schema", None)
    return {"$schema": SCHEMA_DIALECT, "$id": ID_BASE + filename, "version": SCHEMA_VERSION, **body}


def render(name: str) -> str:
    """Deterministic text of one schema file (sorted keys, 2-space indent, trailing newline)."""
    return json.dumps(build_schema(name), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_all() -> dict[str, str]:
    """File name -> text for every schema."""
    return {DOCUMENTS[n][0]: render(n) for n in DOCUMENTS}


def write_all(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, text in render_all().items():
        path = out / filename
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m opencap.config.schema", description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("schema"), help="output directory (default: schema/)")
    args = parser.parse_args(argv)
    for path in write_all(args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
