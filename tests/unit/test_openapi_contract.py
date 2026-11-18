"""The committed OpenAPI contract (api/openapi.json, issue #32): it matches the app (drift), and
the app's real answers validate against it (every route, success and error)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from tests.unit.test_config_api import ADMIN, DEV, DEV_PATH, ENDPOINTS, NO_IF_MATCH, SAMPLE, V1, VIEWER, admin_w, env  # noqa: F401

ROOT = Path(__file__).parents[2]
SPEC_PATH = ROOT / "api" / "openapi.json"
REGEN = "python scripts/gen_openapi.py"


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def test_committed_contract_is_up_to_date():
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_openapi.py"), "--check"],
                          capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, f"api/openapi.json is out of date; regenerate it with: {REGEN}\n{done.stderr}"


def test_generation_is_deterministic():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import gen_openapi
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    assert gen_openapi.render(gen_openapi.build_schema()) == gen_openapi.render(gen_openapi.build_schema())


def test_every_operation_is_typed(spec):
    """No untyped answers: each operation has a 2xx response with a schema (or text for /metrics), and
    an error model for 401 where it needs a token."""
    open_paths = {"/health", "/metrics", "/api/v1/auth-info"}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            ok = [c for c in op["responses"] if c.startswith("2")]
            assert ok, (method, path)
            content = op["responses"][ok[0]].get("content", {})
            assert content, (method, path)
            for media in content.values():
                schema = media["schema"]
                assert schema and schema != {}, (method, path)
                assert schema != {"type": "object"} or path == "/api/v1/project/validate", (method, path, "bare object")
            if path not in open_paths:
                assert "401" in op["responses"], (method, path)
            assert op["operationId"], (method, path)


def _template(spec: dict, method: str, url: str) -> tuple[str, dict]:
    """The OpenAPI path (and operation) a concrete URL belongs to; `{id}` segments may hold `/`."""
    path = url.split("?", 1)[0]
    best = None
    for tpl, ops in spec["paths"].items():
        if method not in ops:
            continue
        rx = "^" + re.sub(r"\\\{[^}]+\\\}", "(.+)", re.escape(tpl)) + "$"
        if re.match(rx, path):
            # prefer the template with the most literal characters (specific before generic)
            lit = len(re.sub(r"\{[^}]+\}", "", tpl))
            if best is None or lit > best[0]:
                best = (lit, tpl, ops[method])
    assert best, f"{method.upper()} {url} is not in api/openapi.json"
    return best[1], best[2]


def _validate(spec: dict, method: str, url: str, response) -> str:
    tpl, op = _template(spec, method, url)
    status = str(response.status_code)
    assert status in op["responses"], f"{method.upper()} {tpl}: status {status} is not documented ({list(op['responses'])})"
    content = op["responses"][status].get("content")
    if content and "application/json" in content and response.content:
        schema = {**content["application/json"]["schema"], "components": spec["components"]}
        jsonschema.validate(response.json(), schema)
    return f"{method} {tpl}"


def test_every_route_answers_as_documented(env, spec):  # noqa: F811
    c, _, _, _ = env
    seen: set[str] = set()

    def go(method, url, body=None, headers=None, **kw):
        kwargs = {"headers": headers if headers is not None else ADMIN, **kw}
        if body is not None:
            kwargs["json"] = body
        r = c.request(method.upper(), url, **kwargs)
        seen.add(_validate(spec, method, url, r))
        return r

    # the table of test_config_api.py: every config route, as viewer (reads) and as admin
    for method, url, body in ENDPOINTS:
        if method == "get":
            assert go(method, url, headers=VIEWER).status_code == 200, url
        headers = ADMIN if method == "get" or url in NO_IF_MATCH else admin_w(c)
        assert go(method, url, body, headers).status_code in (200, 201), (method, url)

    # the rest: open and legacy routes, import, and the error answers
    go("get", "/health", headers={})
    go("get", f"{V1}/auth-info", headers={})
    r = c.get("/metrics")
    seen.add(_validate(spec, "get", "/metrics", r))
    go("get", "/channels")
    go("get", f"/devices/{DEV}")
    go("get", f"/devices/{DEV}/values")
    go("post", f"/devices/{DEV}/read-once")
    go("post", "/reload")
    exported = go("get", f"{V1}/project").json()
    assert go("post", f"{V1}/project/import", exported).json()["applied"] is False
    r = go("post", f"{V1}/project/import?dry_run=false", exported, {**ADMIN, "If-Match": exported["revision"]})
    assert r.status_code in (200, 412)
    go("post", f"{V1}/project/import?dry_run=false&filename=project.json",
       headers={**ADMIN, "Content-Type": "application/json", "If-Match": "0000000000000000"},
       content=SAMPLE.read_bytes())

    # verbs the shared table does not use
    device = c.get(DEV_PATH, headers=VIEWER).json()["device"]
    tag = c.get(f"{DEV_PATH}/tags/FLOW_1", headers=VIEWER).json()["tag"]
    assert go("put", DEV_PATH, device, admin_w(c)).status_code == 200
    assert go("put", f"{DEV_PATH}/tags/FLOW_1", tag, admin_w(c)).status_code == 200
    assert go("put", f"{V1}/map/devices/{DEV}", {"sub_device_id": "SUB_R"}, admin_w(c)).status_code == 200
    assert go("patch", f"{V1}/map/devices/{DEV}", {"sub_device_id": "SUB_P"}, admin_w(c)).status_code == 200
    assert go("put", f"{V1}/overrides/devices/{DEV}", {"retries": 0}, admin_w(c)).status_code == 200
    assert go("patch", f"{V1}/overrides/devices/{DEV}", {"retries": 1}, admin_w(c)).status_code == 200

    assert go("get", f"{V1}/channels", headers={}).status_code == 401
    assert go("patch", DEV_PATH, {"retries": 1}, VIEWER).status_code == 403
    assert go("get", f"{V1}/channels/NOPE").status_code == 404
    assert go("get", f"{DEV_PATH}/tags/NOPE").status_code == 404
    assert go("get", "/devices/NOPE/NOPE").status_code == 404
    assert go("patch", DEV_PATH, {"retries": 1}, ADMIN).status_code == 428
    assert go("patch", DEV_PATH, {"retries": 1}, {**ADMIN, "If-Match": "0000000000000000"}).status_code == 412
    assert go("patch", DEV_PATH, {"port": "x"}, admin_w(c)).status_code == 422
    assert go("post", f"{V1}/channels", {"name": "SITE MODBUS"}, admin_w(c)).status_code == 409

    documented = {f"{m} {p}" for p, ops in spec["paths"].items() for m in ops}
    missing = documented - seen
    assert not missing, f"routes without a contract test: {sorted(missing)}"
