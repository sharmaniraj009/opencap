"""Configuration REST API under /api/v1: every endpoint, roles, If-Match, validation errors,
import dry run, audit (contract: docs/architecture/03-deep-dive.md §3.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from opencap.api.server import create_app
from opencap.config.kep_import import import_kep, write_outputs
from opencap.config.models import ApiCfg, ApiToken
from tests.helpers.fake_poller import ConfigPoller

SAMPLE = Path(__file__).parents[1] / "fixtures" / "project_sample.json"
ADMIN = {"Authorization": "Bearer adm1n-secret"}
VIEWER = {"Authorization": "Bearer v1ewer-secret"}
LEGACY = {"Authorization": "Bearer legacy-secret"}
ENV = {"T_ADMIN": "adm1n-secret", "T_VIEW": "v1ewer-secret", "OPENCAP_API_TOKEN": "legacy-secret"}
V1 = "/api/v1"
DEV = "SITE MODBUS/Device1"
DEV_PATH = f"{V1}/devices/{DEV}"


def make_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    write_outputs(import_kep(SAMPLE), config_dir=config_dir, reports_dir=tmp_path / "reports")
    return config_dir


def api_cfg(tmp_path: Path, **kw) -> ApiCfg:
    return ApiCfg(tokens=[ApiToken(name="alice", role="admin", token_env="T_ADMIN"),
                          ApiToken(name="grafana", role="viewer", token_env="T_VIEW")],
                  audit_log=str(tmp_path / "audit.jsonl"), **kw)


@pytest.fixture
def env(tmp_path):
    config_dir = make_config(tmp_path)
    poller = ConfigPoller(config_dir)
    app = create_app(poller, api_cfg(tmp_path), environ=ENV)
    return TestClient(app), poller, config_dir, tmp_path


def audit_lines(tmp_path: Path) -> list[dict]:
    p = tmp_path / "audit.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def rev(c: TestClient) -> str:
    return c.get(f"{V1}/project/status", headers=VIEWER).json()["revision"]


def admin_w(c: TestClient) -> dict:
    """Admin headers with If-Match set to the current revision."""
    return {**ADMIN, "If-Match": f'"{rev(c)}"'}


# --- roles -----------------------------------------------------------------------------

ENDPOINTS = [
    ("get", f"{V1}/whoami", None), ("get", f"{V1}/status", None),
    ("get", f"{V1}/project", None), ("get", f"{V1}/project/status", None), ("get", f"{V1}/project/pending", None),
    ("get", f"{V1}/project/schema", None), ("get", f"{V1}/project/validate", None),
    ("get", f"{V1}/project/revisions", None), ("get", f"{V1}/audit", None),
    ("get", f"{V1}/channels", None), ("post", f"{V1}/channels", {"name": "R SITE"}),
    ("get", f"{V1}/channels/SITE MODBUS", None), ("patch", f"{V1}/channels/SITE MODBUS", {"enabled": True}),
    ("get", f"{V1}/channels/SITE MODBUS/devices", None),
    ("post", f"{V1}/channels/SITE MODBUS/devices", {"name": "R2", "driver": "modbus_tcp", "host": "192.0.2.3", "port": 502}),
    ("get", f"{V1}/devices", None), ("get", DEV_PATH, None), ("patch", DEV_PATH, {"retries": 1}),
    ("get", f"{DEV_PATH}/tags", None), ("post", f"{DEV_PATH}/tags", {"name": "R", "address": "400401", "dtype": "uint16"}),
    ("get", f"{DEV_PATH}/tags/FLOW_1", None), ("patch", f"{DEV_PATH}/tags/FLOW_1", {"scan_ms": 500}),
    ("get", f"{DEV_PATH}/status", None), ("get", f"{DEV_PATH}/values", None),
    ("get", f"{DEV_PATH}/plan", None), ("get", f"{DEV_PATH}/identity", None),
    ("get", f"{V1}/map", None), ("put", f"{V1}/map/defaults", {"env": "prod"}),
    ("put", f"{V1}/map/devices/{DEV}", {"sub_device_id": "SUB_R"}), ("get", f"{V1}/map/devices/{DEV}", None),
    ("get", f"{V1}/overrides", None),
    ("put", f"{V1}/overrides/devices/{DEV}", {"retries": 0}), ("get", f"{V1}/overrides/devices/{DEV}", None),
    ("put", f"{V1}/overrides/channels/SITE MODBUS", {"enabled": True}),
    ("get", f"{V1}/overrides/channels/SITE MODBUS", None),
    ("delete", f"{V1}/overrides/channels/SITE MODBUS", None),
    ("delete", f"{V1}/overrides/devices/{DEV}", None), ("delete", f"{V1}/map/devices/{DEV}", None),
    ("delete", f"{DEV_PATH}/tags/R", None), ("delete", f"{V1}/devices/SITE MODBUS/R2", None),
    ("delete", f"{V1}/channels/R SITE", None),
    ("post", f"{DEV_PATH}/read-once", None), ("post", f"{V1}/reload", None),
]
NO_IF_MATCH = {f"{DEV_PATH}/read-once", f"{V1}/reload"}


def call(c: TestClient, method: str, path: str, body, headers):
    kw = {"headers": headers}
    if body is not None:
        kw["json"] = body
    return c.request(method.upper(), path, **kw)


def test_roles_on_every_endpoint(env):
    c, _, _, _ = env
    for method, path, body in ENDPOINTS:
        assert call(c, method, path, body, {}).status_code == 401, (method, path)
        assert call(c, method, path, body, {"Authorization": "Bearer nope"}).status_code == 401, (method, path)
        r = call(c, method, path, body, VIEWER)
        if method == "get":
            assert r.status_code == 200, (method, path, r.text)
        else:
            assert r.status_code == 403, (method, path, r.text)
            assert "viewer" in r.json()["detail"]
        headers = ADMIN if method == "get" or path in NO_IF_MATCH else admin_w(c)
        r = call(c, method, path, body, headers)
        assert r.status_code in (200, 201), (method, path, r.text)


def test_open_endpoints(env):
    c, _, _, _ = env
    assert c.get(f"{V1}/auth-info").json() == {"open": False}
    assert c.get("/health").status_code == 200
    me = c.get(f"{V1}/whoami", headers=VIEWER).json()
    assert {k: me[k] for k in ("name", "role", "open")} == {"name": "grafana", "role": "viewer", "open": False}


def test_401_has_www_authenticate(env):
    c, _, _, _ = env
    assert c.get(f"{V1}/channels").headers["www-authenticate"] == "Bearer"


def test_legacy_token_is_admin_named_default(env):
    c, _, _, tmp = env
    assert c.patch(DEV_PATH, json={"retries": 1}, headers={**LEGACY, "If-Match": rev(c)}).status_code == 200
    assert audit_lines(tmp)[-1]["who"] == "default"


def test_listed_token_without_env_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError, match="T_ADMIN"):
        create_app(ConfigPoller(make_config(tmp_path)), api_cfg(tmp_path), environ={"T_VIEW": "x"})


def test_two_tokens_with_one_secret_refuse_to_start(tmp_path):
    with pytest.raises(RuntimeError, match="same secret"):
        create_app(ConfigPoller(make_config(tmp_path)), api_cfg(tmp_path), environ={"T_ADMIN": "s", "T_VIEW": "s"})


def test_token_names_unique_and_default_reserved():
    with pytest.raises(ValueError):
        ApiCfg(tokens=[ApiToken(name="a", token_env="X"), ApiToken(name="a", token_env="Y")])
    with pytest.raises(ValueError):
        ApiCfg(tokens=[ApiToken(name="default", token_env="X")])


def test_open_mode_is_read_only_and_answers_only_localhost(tmp_path):
    """No token configured (loopback bind): reads work for localhost, every write is refused,
    and a DNS-rebinding request (a foreign host name pointed at 127.0.0.1) gets nothing."""
    poller = ConfigPoller(make_config(tmp_path))
    app = create_app(poller, ApiCfg(audit_log=str(tmp_path / "a.jsonl")), environ={})
    c = TestClient(app, base_url="http://127.0.0.1:8080")
    assert c.get(f"{V1}/auth-info").json() == {"open": True}
    me = c.get(f"{V1}/whoami").json()
    assert {k: me[k] for k in ("name", "role", "open")} == {"name": "anonymous", "role": "viewer", "open": True}
    assert c.get(f"{V1}/channels").status_code == 200
    for host in ("localhost:8080", "[::1]:8080", "127.0.0.1"):
        assert c.get(f"{V1}/whoami", headers={"Host": host}).status_code == 200, host
    r = c.patch(DEV_PATH, json={"host": "198.51.100.66"}, headers={"If-Match": rev(c)})
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert c.post(f"{V1}/reload").status_code == 403 and c.post("/reload").status_code == 403
    assert c.post(f"{DEV_PATH}/read-once").status_code == 403 and poller.read_onces == []
    rebind = {"Host": "attacker.example:8080", "Origin": "http://attacker.example:8080",
              "Sec-Fetch-Site": "same-origin"}
    assert c.get(f"{V1}/project", headers=rebind).status_code == 403          # no config leaves
    assert c.patch(DEV_PATH, json={"host": "198.51.100.66"}, headers={**rebind, "If-Match": "x"}).status_code == 403
    assert not (tmp_path / "a.jsonl").exists()                                  # nothing changed


def test_unauthenticated_requests_are_refused_before_the_body_is_read(env):
    c, _, _, _ = env
    r = c.patch(DEV_PATH, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 401                                                 # not 422
    r = c.patch(DEV_PATH, content=b"{not json", headers={**VIEWER, "Content-Type": "application/json"})
    assert r.status_code == 403


async def test_guard_never_reads_the_body_of_a_refused_request(env):
    c = env[0]
    called = []

    async def receive():
        called.append(1)
        raise AssertionError("the body was read")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "PATCH", "path": DEV_PATH, "raw_path": DEV_PATH.encode(),
             "query_string": b"", "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                                               (b"content-length", b"999999999")],
             "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1), "root_path": "",
             "http_version": "1.1", "asgi": {"version": "3.0"}}
    await c.app(scope, receive, send)
    assert called == [] and sent[0]["status"] == 401


def test_body_size_cap(env):
    c, _, cfg, _ = env
    big = b'{"description": "' + b"x" * (1024 * 1024 + 10) + b'"}'
    r = c.patch(f"{DEV_PATH}/tags/FLOW_1", content=big, headers={**admin_w(c), "Content-Type": "application/json"})
    assert r.status_code == 413

    def chunks():                                   # no Content-Length: counted while read
        for _ in range(20):
            yield b"x" * 100_000

    r = c.patch(f"{DEV_PATH}/tags/FLOW_1", content=chunks(), headers={**admin_w(c), "Content-Type": "application/json"})
    assert r.status_code == 413
    # the import route accepts up to 64 MiB: a 2 MiB document is parsed (and refused as unknown)
    doc = json.dumps({"junk": "x" * (2 * 1024 * 1024)}).encode()
    r = c.post(f"{V1}/project/import", content=doc, headers={**ADMIN, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_cors_only_for_configured_origins(tmp_path):
    poller = ConfigPoller(make_config(tmp_path))
    c = TestClient(create_app(poller, api_cfg(tmp_path, cors_origins=["tauri://localhost"]), environ=ENV))
    pre = {"Origin": "tauri://localhost", "Access-Control-Request-Method": "PATCH",
           "Access-Control-Request-Headers": "authorization,if-match,content-type"}
    r = c.options(DEV_PATH, headers=pre)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "tauri://localhost"
    r = c.get(f"{V1}/whoami", headers={**VIEWER, "Origin": "tauri://localhost"})
    assert r.headers["access-control-allow-origin"] == "tauri://localhost"
    evil = c.get(f"{V1}/whoami", headers={**VIEWER, "Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in evil.headers
    c2 = TestClient(create_app(poller, api_cfg(tmp_path), environ=ENV))          # none by default
    r = c2.get(f"{V1}/whoami", headers={**VIEWER, "Origin": "tauri://localhost"})
    assert "access-control-allow-origin" not in r.headers


def test_config_api_without_config_dir_is_503(tmp_path):
    class Bare:
        def health(self):
            return {}

    c = TestClient(create_app(Bare(), api_cfg(tmp_path), environ=ENV))
    assert c.get(f"{V1}/channels", headers=ADMIN).status_code == 503


# --- reads -----------------------------------------------------------------------------

def test_project_status_and_etag(env):
    c, _, _, _ = env
    r = c.get(f"{V1}/project/status", headers=VIEWER)
    body = r.json()
    assert body["valid"] and body["counts"]["devices"] == 10
    assert r.headers["etag"] == f'"{body["revision"]}"'
    assert body["status"]["available"] and body["status"]["pending_reload"] is False
    assert c.get(f"{V1}/project/pending", headers=VIEWER).json()["pending_reload"] is False
    assert c.get(f"{V1}/project/schema", headers=VIEWER).json()["tag"]["properties"]["dtype"]


def test_runtime_status(env):
    c, _, _, _ = env
    body = c.get(f"{V1}/status", headers=VIEWER).json()
    assert body["health"]["mqtt_connected"] is True and body["health"]["saf_queue"] == 7
    assert [ch["name"] for ch in body["channels"]][0] == "SITE MODBUS"
    assert body["config"]["pending_reload"] is False
    assert c.get(f"{DEV_PATH}/status", headers=VIEWER).json()["state"] == "ok"
    assert c.get(f"{DEV_PATH}/values", headers=VIEWER).json()["FLOW_1"]["quality"] == "good"


def test_device_and_tag_reads(env):
    c, _, _, _ = env
    dev = c.get(DEV_PATH, headers=VIEWER).json()
    assert dev["device"]["id"] == DEV and dev["map_entry"]["stub"] is True
    tags = c.get(f"{DEV_PATH}/tags", headers=VIEWER)
    assert len(tags.json()["tags"]) == 9 and tags.headers["etag"] == f'"{tags.json()["revision"]}"'
    assert c.get(f"{DEV_PATH}/tags/FLOW_1", headers=VIEWER).json()["tag"]["address"] == "400045"
    assert [d["id"] for d in c.get(f"{V1}/channels/SITE MODBUS/devices", headers=VIEWER).json()["devices"]] == [DEV]
    devices = c.get(f"{V1}/devices", headers=VIEWER)
    assert len(devices.json()["devices"]) == 10 and devices.headers["etag"] == f'"{rev(c)}"'
    assert c.get(f"{V1}/devices?channel=S7 MULTI", headers=VIEWER).json()["devices"][1]["name"] == "OUT-2"
    channels = c.get(f"{V1}/channels", headers=VIEWER)
    assert channels.json()["channels"][0]["name"] == "SITE MODBUS" and channels.json()["revision"] == rev(c)
    for path in (f"{V1}/channels/NOPE", f"{V1}/channels/NOPE/devices", f"{V1}/devices/SITE MODBUS/NOPE",
                 f"{DEV_PATH}/tags/NOPE", f"{V1}/map/devices/NOPE/X", f"{V1}/overrides/devices/{DEV}",
                 f"{V1}/overrides/channels/SITE MODBUS", f"{V1}/devices/NOPE/X/values"):
        assert c.get(path, headers=VIEWER).status_code == 404, path


# --- writes ----------------------------------------------------------------------------

def test_writes_need_if_match(env):
    c, _, cfg, _ = env
    before = (cfg / "devices.json").read_bytes()
    r = c.patch(DEV_PATH, json={"retries": 1}, headers=ADMIN)
    assert r.status_code == 428 and "If-Match" in r.json()["detail"]
    r = c.patch(DEV_PATH, json={"retries": 1}, headers={**ADMIN, "If-Match": "0000000000000000"})
    assert r.status_code == 412 and "changed since" in r.json()["detail"]
    assert (cfg / "devices.json").read_bytes() == before
    current = rev(c)
    assert c.patch(DEV_PATH, json={"retries": 1}, headers={**ADMIN, "If-Match": f'"{current}"'}).status_code == 200
    r = c.patch(DEV_PATH, json={"retries": 0}, headers={**ADMIN, "If-Match": f'"{current}"'})
    assert r.status_code == 412                     # two editors never silently overwrite each other


def test_change_response_audit_and_pending_reload(env):
    c, poller, cfg, tmp = env
    r = c.patch(DEV_PATH, json={"timeout_ms": 1500}, headers=admin_w(c))
    body = r.json()
    assert body["ok"] and body["files"] == ["devices.json"] and body["pending_reload"] is True
    assert body["changes"] == [{"path": "timeout_ms", "old": 1000, "new": 1500}]
    assert r.headers["etag"] == f'"{body["revision"]}"' and body["revision"] == rev(c)
    entry = audit_lines(tmp)[-1]
    assert entry["who"] == "alice" and entry["role"] == "admin" and entry["via"] == "api"
    assert entry["action"] == "device.update" and entry["target"] == DEV
    assert entry["changes"] == body["changes"] and entry["revision"] == body["revision"]
    status = c.get(f"{V1}/project/pending", headers=VIEWER).json()
    assert status["channels"]["changed"] == ["SITE MODBUS"]
    r = c.post(f"{V1}/reload", headers=ADMIN)
    assert r.json()["restarted"] == ["SITE MODBUS"] and poller.reloads == 1
    assert audit_lines(tmp)[-1]["action"] == "reload"
    assert audit_lines(tmp)[-1]["detail"] == {"restarted": ["SITE MODBUS"]}
    assert c.get(f"{V1}/project/pending", headers=VIEWER).json()["pending_reload"] is False
    revisions = c.get(f"{V1}/project/revisions", headers=VIEWER).json()
    assert revisions["current"] == body["revision"] and revisions["revisions"][0]["revision"] == body["revision"]


def test_apply_reloads_right_after_the_write(env):
    c, poller, _, tmp = env
    r = c.patch(f"{DEV_PATH}?apply=true", json={"retries": 0}, headers=admin_w(c))
    body = r.json()
    assert body["reload"]["restarted"] == ["SITE MODBUS"] and poller.reloads == 1
    assert body["pending_reload"] is False
    assert [e["action"] for e in audit_lines(tmp)] == ["device.update", "reload"]


def test_validation_errors_are_422_with_locations(env):
    c, _, cfg, tmp = env
    before = (cfg / "devices.json").read_bytes()
    r = c.patch(DEV_PATH, json={"port": "x", "timeout_ms": -1.5, "nope": 1}, headers=admin_w(c))
    assert r.status_code == 422
    locs = {tuple(e["loc"]) for e in r.json()["detail"]}
    # relative to the resource, as ConfigStore reports them: never FastAPI's ("body", ...) (#89, D-064)
    assert {("port",), ("timeout_ms",), ("nope",)} <= locs
    assert all(loc[0] != "body" for loc in locs)
    r = c.post(f"{DEV_PATH}/tags", json={"name": "X", "address": "999999", "dtype": "float32"}, headers=admin_w(c))
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["address"]
    r = c.post(f"{DEV_PATH}/tags", json={"name": "X", "address": "400001", "dtype": "float99"}, headers=admin_w(c))
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["dtype"]
    r = c.patch(DEV_PATH, json={"params": {"max_holding": 0}}, headers=admin_w(c))
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["params"]
    r = c.post(f"{V1}/channels/SITE MODBUS/devices", json={"name": "tags", "driver": "modbus_tcp", "host": "192.0.2.1",
                                                            "port": 502}, headers=admin_w(c))
    assert r.status_code == 422 and "reserved" in r.json()["detail"][0]["msg"]
    assert (cfg / "devices.json").read_bytes() == before
    assert audit_lines(tmp) == []                         # rejected changes are not audited


def test_non_json_body_is_refused(env):
    c, _, _, _ = env
    r = c.patch(DEV_PATH, content=b'{"retries": 1}', headers={**admin_w(c), "Content-Type": "text/plain"})
    assert r.status_code == 422
    r = c.post(f"{V1}/project/import", content=SAMPLE.read_bytes(), headers={**ADMIN, "Content-Type": "text/plain"})
    assert r.status_code == 422


def test_conflicts(env):
    c, _, _, _ = env
    assert c.post(f"{V1}/channels", json={"name": "SITE MODBUS"}, headers=admin_w(c)).status_code == 409
    r = c.post(f"{DEV_PATH}/tags", json={"name": "FLOW_1", "address": "400001", "dtype": "uint16"}, headers=admin_w(c))
    assert r.status_code == 409


def test_channel_device_tag_lifecycle(env):
    c, _, cfg, _ = env
    assert c.post(f"{V1}/channels", json={"name": "NEW"}, headers=admin_w(c)).status_code == 201
    r = c.post(f"{V1}/channels/NEW/devices", json={
        "name": "PLC", "driver": "modbus_tcp", "host": "192.0.2.9", "port": 502,
        "tags": [{"name": "A", "address": "400001", "dtype": "uint16"}]}, headers=admin_w(c))
    assert r.status_code == 201 and r.json()["target"] == "NEW/PLC"
    r = c.post(f"{V1}/devices/NEW/PLC/tags", json=[{"name": "B", "address": "400002", "dtype": "uint16"},
                                                  {"name": "C", "address": "400003", "dtype": "uint16"}],
               headers=admin_w(c))
    assert r.status_code == 201
    dev = c.get(f"{V1}/devices/NEW/PLC", headers=ADMIN).json()["device"]
    assert c.put(f"{V1}/devices/NEW/PLC", json={**dev, "host": "192.0.2.10"}, headers=admin_w(c)).status_code == 200
    r = c.put(f"{V1}/devices/NEW/PLC/tags/A", json={"address": "400010", "dtype": "int16"}, headers=admin_w(c))
    assert r.status_code == 200 and r.json()["data"] == {"name": "A", "address": "400010", "dtype": "int16",
                                                        "enabled": True}
    r = c.patch(f"{V1}/devices/NEW/PLC", json={"name": "PLC2"}, headers=admin_w(c))
    assert r.json()["data"]["id"] == "NEW/PLC2"
    assert c.patch(f"{V1}/channels/NEW", json={"name": "NEWER", "enabled": False}, headers=admin_w(c)).status_code == 200
    assert c.get(f"{V1}/devices/NEWER/PLC2", headers=ADMIN).json()["device"]["host"] == "192.0.2.10"
    assert c.delete(f"{V1}/devices/NEWER/PLC2/tags/B", headers=admin_w(c)).status_code == 200
    assert c.delete(f"{V1}/devices/NEWER/PLC2", headers=admin_w(c)).status_code == 200
    assert c.delete(f"{V1}/channels/NEWER", headers=admin_w(c)).status_code == 200
    assert c.delete(f"{V1}/channels/NEWER", headers=admin_w(c)).status_code == 404


def test_map_and_overrides_endpoints(env):
    c, _, cfg, _ = env
    r = c.put(f"{V1}/map/devices/{DEV}", json={"sub_device_id": "SUB_REAL"}, headers=admin_w(c))
    assert r.json()["files"] == ["devices_map.yaml"]
    assert c.patch(f"{V1}/map/devices/{DEV}", json={"env": "test"}, headers=admin_w(c)).status_code == 200
    assert c.get(f"{V1}/map/devices/{DEV}", headers=VIEWER).json()["entry"] == {"sub_device_id": "SUB_REAL", "env": "test"}
    assert c.put(f"{V1}/map/defaults", json={"bogus": 1}, headers=admin_w(c)).status_code == 422
    assert c.put(f"{V1}/map/defaults", json={"env": "prod"}, headers=admin_w(c)).json()["data"] == {"env": "prod"}
    assert c.get(f"{V1}/map", headers=VIEWER).json()["defaults"] == {"env": "prod"}
    r = c.put(f"{V1}/overrides/devices/{DEV}", json={"tags": {"FLOW_1": {"enabled": False}}}, headers=admin_w(c))
    assert r.json()["files"] == ["overrides.yaml"]
    assert c.patch(f"{V1}/overrides/devices/{DEV}", json={"retries": 0}, headers=admin_w(c)).status_code == 200
    assert c.get(f"{V1}/overrides", headers=VIEWER).json()["devices"][DEV] == {
        "tags": {"FLOW_1": {"enabled": False}}, "retries": 0}
    assert c.put(f"{V1}/overrides/channels/SITE MODBUS", json={"enabled": "x"}, headers=admin_w(c)).status_code == 422
    assert c.put(f"{V1}/overrides/channels/SITE MODBUS", json={"enabled": False}, headers=admin_w(c)).status_code == 200
    assert c.delete(f"{V1}/overrides/channels/SITE MODBUS", headers=admin_w(c)).status_code == 200
    assert c.delete(f"{V1}/overrides/devices/{DEV}", headers=admin_w(c)).status_code == 200
    assert c.delete(f"{V1}/map/devices/{DEV}", headers=admin_w(c)).status_code == 200
    assert c.delete(f"{V1}/map/devices/{DEV}", headers=admin_w(c)).status_code == 404
    assert c.put(f"{V1}/map/devices/NOPE/X", json={"sub_device_id": "S"}, headers=admin_w(c)).status_code == 404


# --- project: export / import / validate / audit ---------------------------------------

def device_retries(cfg: Path) -> int:
    doc = json.loads((cfg / "devices.json").read_text())
    return next(d for ch in doc["channels"] for d in ch["devices"] if d["id"] == DEV)["retries"]


def test_export_and_import_round_trip(env):
    c, _, cfg, tmp = env
    r = c.get(f"{V1}/project?download=true", headers=VIEWER)
    exported = r.json()
    assert "attachment" in r.headers["content-disposition"]
    assert set(exported["files"]) == {"devices.json", "devices_map.yaml", "overrides.yaml"}
    c.patch(DEV_PATH, json={"retries": 0}, headers=admin_w(c))
    r = c.post(f"{V1}/project/import", json=exported, headers=ADMIN)                  # dry run by default
    body = r.json()
    assert body["applied"] is False and body["files"] == ["devices.json"] and body["kind"] == "opencap-export"
    assert device_retries(cfg) == 0
    assert c.post(f"{V1}/project/import?dry_run=false", json=exported, headers=ADMIN).status_code == 428
    r = c.post(f"{V1}/project/import?dry_run=false", json=exported, headers={**ADMIN, "If-Match": body["revision"]})
    assert r.json()["applied"] is True and device_retries(cfg) == 2
    assert audit_lines(tmp)[-1]["action"] == "config.import"


def test_import_kepserver_dry_run_then_apply(env):
    c, _, cfg, tmp = env
    c.delete(f"{V1}/channels/SITE PCCC", headers=admin_w(c))
    before = {p.name: p.read_bytes() for p in cfg.iterdir()}
    r = c.post(f"{V1}/project/import?filename=project.json", content=SAMPLE.read_bytes(),
               headers={**ADMIN, "Content-Type": "application/json"})
    body = r.json()
    assert r.status_code == 200 and body["kind"] == "kepserver" and body["applied"] is False
    assert body["devices_json"]["summary"]["devices"]["added"] == 1
    assert [d["id"] for d in body["devices_json"]["devices_added"]] == ["SITE PCCC/7 PUMP"]
    assert body["devices_map"]["stubs_added"] == ["SITE PCCC/7 PUMP"]
    assert body["import_summary"]["source"]["file"] == "project.json"
    assert {p.name: p.read_bytes() for p in cfg.iterdir()} == before           # dry run wrote nothing
    stale = {**ADMIN, "Content-Type": "application/json", "If-Match": "0000000000000000"}
    assert c.post(f"{V1}/project/import?dry_run=false", content=SAMPLE.read_bytes(), headers=stale).status_code == 412
    r = c.post(f"{V1}/project/import?dry_run=false&filename=project.json", content=SAMPLE.read_bytes(),
               headers={**ADMIN, "Content-Type": "application/json", "If-Match": body["revision"]})
    assert r.json()["applied"] is True
    assert "SITE PCCC/7 PUMP" in yaml.safe_load((cfg / "devices_map.yaml").read_text())["devices"]
    entry = audit_lines(tmp)[-1]
    assert entry["action"] == "config.import_kep" and entry["detail"]["kind"] == "kepserver"


def test_import_rejects_unknown_documents(env):
    c, _, _, _ = env
    assert c.post(f"{V1}/project/import", json={"hello": 1}, headers=ADMIN).status_code == 422


def test_validate_endpoint(env, monkeypatch):
    c, _, _, _ = env
    from opencap import drivers
    # a driver whose module is not there (yet): it must show as a plan error, not break the report
    monkeypatch.setitem(drivers.DRIVERS, "opcua_client", "opencap.drivers._not_built_yet:Nope")
    report = c.get(f"{V1}/project/validate", headers=VIEWER).json()
    assert report["rtt_ms"] == 60.0 and report["totals"]["devices"] == 10
    modbus = next(ch for ch in report["channels"] if ch["name"] == "SITE MODBUS")["devices"][0]
    assert modbus["blocks"] >= 1 and modbus["wire_requests"] >= 1 and "block_plan" not in modbus
    s7 = next(ch for ch in report["channels"] if ch["name"] == "SITE S7")["devices"][0]
    assert s7["plan_error"] is None and s7["blocks"] >= 1          # the s7 driver plans (#60)
    opc = next(ch for ch in report["channels"] if ch["name"] == "Test OPC")["devices"][0]
    assert "not implemented" in opc["plan_error"]
    verbose = c.get(f"{V1}/project/validate?site=SITE MODBUS&verbose=true", headers=VIEWER).json()
    assert [ch["name"] for ch in verbose["channels"]] == ["SITE MODBUS"]
    assert verbose["channels"][0]["devices"][0]["block_plan"][0]["describe"].startswith("FC")
    assert c.get(f"{V1}/project/validate?site=NOPE", headers=VIEWER).json()["ok"] is False


def test_validate_reports_an_invalid_config(env):
    c, _, cfg, _ = env
    (cfg / "overrides.yaml").write_text("devices: {X/Y: {timeout_ms: soon}}\n")
    report = c.get(f"{V1}/project/validate", headers=VIEWER).json()
    assert report["ok"] is False and "Overrides" in report["error"]


def test_audit_endpoint_filters(env):
    c, _, _, _ = env
    c.patch(DEV_PATH, json={"retries": 1}, headers=admin_w(c))
    c.put(f"{V1}/map/devices/{DEV}", json={"sub_device_id": "S"}, headers=admin_w(c))
    entries = c.get(f"{V1}/audit", headers=VIEWER).json()["entries"]
    assert [e["action"] for e in entries] == ["map.put", "device.update"]           # newest first
    assert [e["action"] for e in c.get(f"{V1}/audit?action=device", headers=VIEWER).json()["entries"]] == ["device.update"]
    assert len(c.get(f"{V1}/audit?limit=1", headers=VIEWER).json()["entries"]) == 1
    assert c.get(f"{V1}/audit?who=bob", headers=VIEWER).json()["entries"] == []
    ui = c.patch(DEV_PATH, json={"retries": 2}, headers={**admin_w(c), "X-Opencap-Client": "ui"})
    assert ui.status_code == 200 and c.get(f"{V1}/audit?limit=1", headers=VIEWER).json()["entries"][0]["via"] == "ui"


# --- runtime additions -----------------------------------------------------------------

def test_device_plan_identity_and_read_once(env):
    c, poller, _, _ = env
    plan = c.get(f"{DEV_PATH}/plan", headers=VIEWER).json()
    assert plan["id"] == DEV and plan["block_plan"] and "invalid" in plan
    assert c.get(f"{DEV_PATH}/identity", headers=VIEWER).json() == {"connected": False, "identity": {}, "cached": False}
    poller.identities[DEV] = {"vendor": "Example"}
    first = c.get(f"{DEV_PATH}/identity", headers=VIEWER).json()
    assert first["identity"] == {"vendor": "Example"} and first["cached"] is False
    poller.identities[DEV] = {"vendor": "Changed"}                 # cached: no new request
    again = c.get(f"{DEV_PATH}/identity", headers=VIEWER).json()
    assert again["identity"] == {"vendor": "Example"} and again["cached"] is True
    assert c.get(f"{DEV_PATH}/identity?refresh=true", headers=VIEWER).status_code == 403
    assert c.get(f"{DEV_PATH}/identity?refresh=true", headers=ADMIN).json()["identity"] == {"vendor": "Changed"}
    assert c.post(f"{DEV_PATH}/read-once", headers=ADMIN).json()["FLOW_1"]["quality"] == "good"
    assert poller.read_onces == [DEV]
    for path in ("plan", "identity", "status"):
        assert c.get(f"{V1}/devices/NOPE/X/{path}", headers=VIEWER).status_code == 404


def test_reload_failure_keeps_running_and_is_not_audited(env):
    c, poller, cfg, tmp = env
    (cfg / "devices.json").write_text("{ broken")
    r = c.post(f"{V1}/reload", headers=ADMIN)
    assert r.status_code == 400 and "old config kept" in r.json()["detail"]
    assert poller.reloads == 0 and audit_lines(tmp) == []
    assert c.get(f"{V1}/project/pending", headers=VIEWER).json()["disk_valid"] is False


async def test_reload_waits_for_a_config_write_in_progress(tmp_path):
    import asyncio

    from opencap.api.auth import Principal

    poller = ConfigPoller(make_config(tmp_path))
    app = create_app(poller, api_cfg(tmp_path), environ=ENV)
    ctx = app.state.ctx
    ctx.store.lock.acquire()                                    # a multi-file write is under way
    task = asyncio.create_task(ctx.reload(Principal("alice", "admin")))
    await asyncio.sleep(0.1)
    assert not task.done() and poller.reloads == 0
    ctx.store.lock.release()
    await asyncio.wait_for(task, 5)
    assert poller.reloads == 1 and not ctx.store.lock.locked()
    # a cancelled reload never leaves the lock taken
    ctx.store.lock.acquire()
    task = asyncio.create_task(ctx.reload(Principal("alice", "admin")))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    ctx.store.lock.release()
    assert not ctx.store.lock.locked()


def test_read_only_config_is_reported_and_writes_answer_503(env, monkeypatch):
    import errno
    import os

    from opencap.config import store as store_mod

    c, _, cfg, tmp = env
    me = c.get(f"{V1}/whoami", headers=ADMIN).json()
    assert me["config_writable"] is True and me["config_writable_reason"] is None
    monkeypatch.setattr(store_mod.os, "access", lambda path, mode: False)
    assert c.get(f"{V1}/project/status", headers=VIEWER).json()["writable"] is False
    assert c.get(f"{V1}/whoami", headers=ADMIN).json()["config_writable"] is False
    monkeypatch.undo()

    def ro(*_a, **_k):
        raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(cfg / "devices.json"))

    monkeypatch.setattr(store_mod.os, "replace", ro)
    r = c.patch(DEV_PATH, json={"retries": 0}, headers=admin_w(c))
    assert r.status_code == 503 and str(cfg) in r.json()["detail"] and "nothing was changed" in r.json()["detail"]
    assert audit_lines(tmp) == []


def test_unwritable_audit_log_rolls_the_change_back(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "audit-dir").mkdir()
    app = create_app(ConfigPoller(cfg), api_cfg(tmp_path).model_copy(update={"audit_log": str(tmp_path / "audit-dir")}),
                     environ=ENV)
    c = TestClient(app)
    before = (cfg / "devices.json").read_bytes()
    r = c.patch(DEV_PATH, json={"retries": 0}, headers=admin_w(c))
    assert r.status_code == 503 and "rolled back" in r.json()["detail"]
    assert (cfg / "devices.json").read_bytes() == before
