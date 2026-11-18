"""Write (or check) api/openapi.json, the committed contract of the admin + config API (#32).

    python scripts/gen_openapi.py           # regenerate api/openapi.json
    python scripts/gen_openapi.py --check   # exit 1 if the file is out of date

The schema comes straight from the FastAPI app, offline: no server, no sockets. The running
server keeps `openapi_url=None` (D-019); only this script and the drift test ask the app.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "api" / "openapi.json"
REGEN = "python scripts/gen_openapi.py"


class _NoPoller:
    """create_app() only stores the poller; the schema never calls it."""


def build_schema() -> dict:
    from opencap.api.server import create_app
    from opencap.config.models import ApiCfg

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(_NoPoller(), ApiCfg(audit_log=str(Path(tmp) / "audit.jsonl")),  # type: ignore[arg-type]
                         config_dir=Path(tmp), environ={})
        return app.openapi()


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render(build_schema())
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
            print(f"{TARGET.relative_to(ROOT)} is out of date; run: {REGEN}", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main(sys.argv[1:]))
