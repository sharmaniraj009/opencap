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
    open_paths = {"/health", "/health/ready", "/health/live", "/metrics", "/api/v1/auth-info", "/api/v1/about"}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if "x-websocket" in op:             # an upgrade: 101, then the messages of x-websocket
                assert "101" in op["responses"] and "401" in op["responses"], (method, path)
                for key in ("client_messages", "server_messages"):
                    for ref in op["x-websocket"][key]["oneOf"]:
                        assert ref["$ref"].split("/")[-1] in spec["components"]["schemas"], (path, ref)
                assert op["operationId"], (method, path)
                continue
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


def test_every_write_documents_its_body(spec):
    """#89: each POST/PUT/PATCH that takes a body has a typed `requestBody` (not FastAPI's untyped
    `Body`); the ones that take none (reload, read-once) have none."""
    from opencap.api.schemas import NO_BODY_WRITES

    writes = [(m, p, op) for p, ops in spec["paths"].items() for m, op in ops.items() if m in ("post", "put", "patch")]
    assert len(writes) >= 18
    for method, path, op in writes:
        if (method, path) in NO_BODY_WRITES:
            assert "requestBody" not in op, (method, path)
            continue
        body = op.get("requestBody")
        assert body and body.get("required") is True, f"{method.upper()} {path} has no requestBody"
        schema = body["content"]["application/json"]["schema"]
        assert schema not in ({}, {"title": "Body"}), f"{method.upper()} {path}: untyped body"


def _resolve(spec: dict, schema: dict) -> dict:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
    return schema


def test_body_schemas_are_the_project_schema(spec, env):  # noqa: F811
    """The `Config*` components are the definitions `GET /api/v1/project/schema` serves; the PATCH
    variants keep every property, with no `required` and `null` allowed (merge patch)."""
    c, _, _, _ = env
    served = c.get(f"{V1}/project/schema", headers=VIEWER).json()
    comps = spec["components"]["schemas"]
    names = {"channel": "Channel", "device": "Device", "tag": "Tag", "map_entry": "DeviceMapEntry",
             "map_defaults": "PublishCfg", "device_override": "DeviceOverride"}

    def norm(schema, defs_prefix):          # point both documents' refs at the same names
        return json.loads(json.dumps(schema).replace(defs_prefix, "#/REF/"))

    for key, name in names.items():
        mine = norm(comps[f"Config{name}"], "#/components/schemas/Config")
        theirs = norm({k: v for k, v in served[key].items() if k != "$defs"}, "#/$defs/")
        assert mine == theirs, name
        for sub, sub_schema in served[key].get("$defs", {}).items():
            assert norm(comps[f"Config{sub}"], "#/components/schemas/Config") == norm(sub_schema, "#/$defs/"), sub
    for name in ("Device", "Tag", "DeviceMapEntry", "DeviceOverride"):
        patch, full = comps[f"Config{name}Patch"], comps[f"Config{name}"]
        assert "required" not in patch and set(patch["properties"]) == set(full["properties"]), name
        assert all({"type": "null"} in v["anyOf"] for v in patch["properties"].values()), name


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

    bodies: set[str] = set()

    def go(method, url, body=None, headers=None, **kw):
        kwargs = {"headers": headers if headers is not None else ADMIN, **kw}
        if body is not None:
            kwargs["json"] = body
        r = c.request(method.upper(), url, **kwargs)
        seen.add(_validate(spec, method, url, r))
        sent = body if body is not None else (json.loads(kw["content"]) if "content" in kw else None)
        if sent is not None and r.status_code < 300:            # #89: what the server took is documented
            tpl, op = _template(spec, method, url)
            schema = op["requestBody"]["content"]["application/json"]["schema"]
            jsonschema.validate(sent, {**schema, "components": spec["components"]})
            bodies.add(f"{method} {tpl}")
        return r

    # the table of test_config_api.py: every config route, as viewer (reads) and as admin
    for method, url, body in ENDPOINTS:
        if method == "get":
            assert go(method, url, headers=VIEWER).status_code == 200, url
        headers = ADMIN if method == "get" or url in NO_IF_MATCH else admin_w(c)
        assert go(method, url, body, headers).status_code in (200, 201), (method, url)

    # the rest: open and legacy routes, import, and the error answers
    go("get", "/health", headers={})
    go("get", "/health/live", headers={})
    assert go("get", "/health/ready", headers={}).status_code == 200
    c.app.state.ctx.poller.waiting = {DEV}
    assert go("get", "/health/ready", headers={}).status_code == 503
    c.app.state.ctx.poller.waiting = set()
    go("get", f"{V1}/about", headers={})
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
    # staged imports (#33): stage, read, refuse a viewer, apply, discard, unknown id
    json_admin = {**ADMIN, "Content-Type": "application/json"}
    staged = go("post", f"{V1}/project/import?stage=true&filename=project.json", headers=json_admin,
                content=SAMPLE.read_bytes()).json()
    iid = staged["import_id"]
    assert go("get", f"{V1}/project/import/{iid}").status_code == 200
    assert go("get", f"{V1}/project/import/{iid}", headers=VIEWER).status_code == 403
    assert go("post", f"{V1}/project/import/{iid}/apply", headers=ADMIN).status_code == 428
    assert go("post", f"{V1}/project/import/{iid}/apply",
              headers={**ADMIN, "If-Match": f'"{c.get(f"{V1}/project/status", headers=VIEWER).json()["revision"]}"'}).status_code == 200
    assert go("post", f"{V1}/project/import/{iid}/apply", headers={**ADMIN, "If-Match": '"x"'}).status_code == 404
    other = go("post", f"{V1}/project/import?stage=true", headers=json_admin, content=SAMPLE.read_bytes()).json()["import_id"]
    assert go("delete", f"{V1}/project/import/{other}").status_code == 200
    assert go("delete", f"{V1}/project/import/{other}").status_code == 404

    # verbs the shared table does not use
    device = c.get(DEV_PATH, headers=VIEWER).json()["device"]
    tag = c.get(f"{DEV_PATH}/tags/FLOW_1", headers=VIEWER).json()["tag"]
    assert go("put", DEV_PATH, device, admin_w(c)).status_code == 200
    assert go("put", f"{DEV_PATH}/tags/FLOW_1", tag, admin_w(c)).status_code == 200
    assert go("put", f"{V1}/map/devices/{DEV}", {"sub_device_id": "SUB_R"}, admin_w(c)).status_code == 200
    assert go("patch", f"{V1}/map/devices/{DEV}", {"sub_device_id": "SUB_P"}, admin_w(c)).status_code == 200
    assert go("put", f"{V1}/overrides/devices/{DEV}", {"retries": 0}, admin_w(c)).status_code == 200
    assert go("patch", f"{V1}/overrides/devices/{DEV}", {"retries": 1}, admin_w(c)).status_code == 200

    # the driver catalogue and the paged lists (#129)
    assert go("get", f"{V1}/drivers", headers=VIEWER).status_code == 200
    assert go("get", f"{V1}/drivers/modbus_tcp", headers=VIEWER).status_code == 200
    assert go("get", f"{V1}/drivers/nope", headers=VIEWER).status_code == 404
    assert go("get", f"{V1}/channels?q=site&sort=name&order=desc&limit=1&offset=0", headers=VIEWER).status_code == 200
    assert go("get", f"{V1}/devices?sort=port&limit=5", headers=VIEWER).status_code == 200
    assert go("get", f"{DEV_PATH}/tags?sort=name&limit=2&offset=1", headers=VIEWER).status_code == 200
    assert go("get", f"{V1}/channels?sort=colour", headers=VIEWER).status_code == 422

    assert go("get", f"{V1}/channels", headers={}).status_code == 401
    assert go("patch", DEV_PATH, {"retries": 1}, VIEWER).status_code == 403
    assert go("get", f"{V1}/channels/NOPE").status_code == 404
    assert go("get", f"{DEV_PATH}/tags/NOPE").status_code == 404
    assert go("get", "/devices/NOPE/NOPE").status_code == 404
    assert go("patch", DEV_PATH, {"retries": 1}, ADMIN).status_code == 428
    assert go("patch", DEV_PATH, {"retries": 1}, {**ADMIN, "If-Match": "0000000000000000"}).status_code == 412
    assert go("patch", DEV_PATH, {"port": "x"}, admin_w(c)).status_code == 422
    # the documented schema refuses it too (the server's 422 keeps its own `loc`s, D-064)
    _, op = _template(spec, "patch", DEV_PATH)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"port": "x"}, {**op["requestBody"]["content"]["application/json"]["schema"],
                                            "components": spec["components"]})
    assert go("post", f"{V1}/channels", {"name": "SITE MODBUS"}, admin_w(c)).status_code == 409

    # the live feed: refused upgrades answer as documented, an accepted one speaks x-websocket
    import base64

    from starlette.testclient import WebSocketDenialResponse

    xws = spec["paths"][f"{V1}/ws"]["get"]["x-websocket"]
    server_messages = {**xws["server_messages"], "components": spec["components"]}
    for kw in ({}, {"subprotocols": ["other"]}, {"headers": {**VIEWER, "Origin": "http://evil.example"}}):
        try:
            with c.websocket_connect(f"{V1}/ws", **kw):
                raise AssertionError(f"upgrade accepted: {kw}")
        except WebSocketDenialResponse as refused:
            seen.add(_validate(spec, "get", f"{V1}/ws", refused))
    token = base64.urlsafe_b64encode(VIEWER["Authorization"].split()[1].encode()).decode().rstrip("=")
    with c.websocket_connect(f"{V1}/ws", subprotocols=["opencap.v1", "opencap.bearer." + token]) as ws:
        ws.send_text(json.dumps({"subscribe": {"devices": [DEV]}}))
        for _ in range(2):
            jsonschema.validate(json.loads(ws.receive_text()), server_messages)

    class _Upgraded:
        status_code, content = 101, b""

    seen.add(_validate(spec, "get", f"{V1}/ws", _Upgraded()))

    with_body = {f"{m} {p}" for p, ops in spec["paths"].items() for m, op in ops.items() if "requestBody" in op}
    assert with_body - bodies == set(), f"request bodies never sent by this test: {sorted(with_body - bodies)}"

    documented = {f"{m} {p}" for p, ops in spec["paths"].items() for m in ops}
    missing = documented - seen
    assert not missing, f"routes without a contract test: {sorted(missing)}"
