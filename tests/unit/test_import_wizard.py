"""Staged project imports, the KEPServerEX import wizard's API (#33): upload -> report -> apply or
discard, roles, expiry, and the privacy rules (staging under the config directory only, 0700/0600,
no credential from the export in any response, log line or written file)."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient

from opencap.api import imports
from opencap.api.imports import STAGING_DIR, Staging
from opencap.api.server import create_app
from opencap.config.store import ConfigStore
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ADMIN, ENV, SAMPLE, V1, VIEWER, api_cfg, audit_lines, make_config

JSON = {"Content-Type": "application/json"}
LEAK = "OPENCAP-LEAK-"
POSIX = sys.platform != "win32"


@pytest.fixture
def env(tmp_path):
    config_dir = make_config(tmp_path)
    poller = ConfigPoller(config_dir)
    app = create_app(poller, api_cfg(tmp_path), environ=ENV)
    return TestClient(app), poller, config_dir, tmp_path


def stage(c: TestClient, body: bytes, name: str = "project.json", headers=None):
    return c.post(f"{V1}/project/import?stage=true&filename={name}", content=body,
                  headers={**(ADMIN if headers is None else headers), **JSON})


def with_new_device(project: dict) -> dict:
    """The sample with one more Modbus device, so an import changes the config."""
    doc = json.loads(json.dumps(project))
    for ch in doc["project"]["channels"]:
        devices = ch.get("devices") or []
        if devices and ch.get("servermain.MULTIPLE_TYPES_DEVICE_DRIVER") == "Modbus TCP/IP Ethernet":
            clone = json.loads(json.dumps(devices[0]))
            clone["common.ALLTYPES_NAME"] = "Device9"
            devices.append(clone)
            return doc
    raise AssertionError("the sample has no Modbus channel")


def sample() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8-sig"))


def staging_dir(config_dir: Path) -> Path:
    return config_dir / STAGING_DIR


# --- the flow --------------------------------------------------------------------------------------

def test_upload_report_apply(env):
    c, poller, config_dir, tmp = env
    before = (config_dir / "devices.json").read_bytes()
    r = stage(c, json.dumps(with_new_device(sample())).encode())
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["staged"] is True and rep["applied"] is False and len(rep["import_id"]) == 32
    assert rep["kind"] == "kepserver" and rep["expires_at"] > rep["created_at"]
    # what the wizard shows: devices per driver, exclusions with reasons, same-address report, stubs
    inc, exc = rep["import_summary"]["included"], rep["import_summary"]["excluded"]
    assert inc["by_driver"] and all({"devices", "tags"} <= set(v) for v in inc["by_driver"].values())
    assert "by_reason" in exc
    assert len(rep["excluded_devices"]) == exc["devices"] and all(e["reason"] for e in rep["excluded_devices"])
    assert rep["same_address"] and {"device_id", "address", "tags", "classification", "reason"} <= set(rep["same_address"][0])
    added = rep["devices_json"]["summary"]["devices"]["added"]
    assert added == 1 and rep["devices_map"]["stubs_added"]
    assert (config_dir / "devices.json").read_bytes() == before            # nothing written yet

    iid = rep["import_id"]
    again = c.get(f"{V1}/project/import/{iid}", headers=ADMIN).json()
    assert again["import_id"] == iid and again["devices_json"] == rep["devices_json"]

    # a stale If-Match is refused and the staged import stays
    r = c.post(f"{V1}/project/import/{iid}/apply", headers={**ADMIN, "If-Match": '"0000000000000000"'})
    assert r.status_code == 412
    assert c.get(f"{V1}/project/import/{iid}", headers=ADMIN).status_code == 200
    assert c.post(f"{V1}/project/import/{iid}/apply", headers=ADMIN).status_code == 428

    r = c.post(f"{V1}/project/import/{iid}/apply?apply=true", headers={**ADMIN, "If-Match": f'"{rep["revision"]}"'})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["applied"] is True and done["revision"] != rep["revision"] and "devices.json" in done["files"]
    assert done["reload"]["restarted"] and poller.reloads == 1                 # ?apply=true reloads
    assert (config_dir / "devices.json").read_bytes() != before
    assert [e["action"] for e in audit_lines(tmp)] == ["config.import_kep", "reload"]
    assert audit_lines(tmp)[0]["who"] == "alice"
    assert not (staging_dir(config_dir) / iid).exists()                       # staging removed
    assert c.post(f"{V1}/project/import/{iid}/apply", headers={**ADMIN, "If-Match": f'"{done["revision"]}"'}).status_code == 404


def test_staged_apply_writes_what_a_direct_import_writes(tmp_path):
    raw = json.dumps(with_new_device(sample())).encode()
    a, b = ConfigStore(make_config(tmp_path / "a")), ConfigStore(make_config(tmp_path / "b"))
    direct = a.import_config(raw, dry_run=False, filename="project.json", if_match=a.revision())
    work = tmp_path / "b" / "work"
    work.mkdir()
    report, staged = b.stage_import(raw, filename="project.json", work_dir=work)
    applied = b.apply_staged(json.loads(json.dumps(staged)), if_match=report["revision"])      # via JSON, as staged
    assert direct["files"] == applied["files"]

    def same(name: str, text: str) -> object:            # minus the import's own time stamps
        if name == "devices.json":
            doc = json.loads(text)
            doc.pop("generated_at", None)
            return doc
        return re.sub(r"import on \d{4}-\d{2}-\d{2}", "import on <date>", text)

    for name in direct["files"]:
        assert same(name, (a.dir / name).read_text()) == same(name, (b.dir / name).read_text()), name
    assert list(work.iterdir()) == []                                          # the upload copy is gone


def test_discard_and_ids(env):
    c, _, config_dir, _ = env
    iid = stage(c, SAMPLE.read_bytes()).json()["import_id"]
    assert c.delete(f"{V1}/project/import/{iid}", headers=VIEWER).status_code == 403
    assert c.delete(f"{V1}/project/import/{iid}", headers=ADMIN).json() == {"discarded": iid}
    assert not (staging_dir(config_dir) / iid).exists()
    assert c.delete(f"{V1}/project/import/{iid}", headers=ADMIN).status_code == 404
    assert c.get(f"{V1}/project/import/{iid}", headers=ADMIN).status_code == 404
    for bad in ("0" * 31, "Z" * 32, "%2e%2e%2fdevices.json", "%2e%2e"):     # (a bare ".." never reaches the route)
        assert c.get(f"{V1}/project/import/{bad}", headers=ADMIN).status_code == 404, bad


def test_roles(env):
    c, _, _, _ = env
    assert stage(c, SAMPLE.read_bytes(), headers=VIEWER).status_code == 403
    assert stage(c, SAMPLE.read_bytes(), headers={}).status_code == 401
    iid = stage(c, SAMPLE.read_bytes()).json()["import_id"]
    r = c.get(f"{V1}/project/import/{iid}", headers=VIEWER)
    assert r.status_code == 403 and "admin" in r.json()["detail"]
    assert c.post(f"{V1}/project/import/{iid}/apply", headers={**VIEWER, "If-Match": '"x"'}).status_code == 403


def test_expiry(env, monkeypatch):
    c, _, config_dir, _ = env
    monkeypatch.setattr(imports, "TTL_S", 0)                 # everything expires at once
    iid = stage(c, SAMPLE.read_bytes()).json()["import_id"]
    assert c.get(f"{V1}/project/import/{iid}", headers=ADMIN).status_code == 404
    assert list(staging_dir(config_dir).iterdir()) == []


def test_sweep_by_age_and_leftovers(tmp_path):
    store = ConfigStore(make_config(tmp_path))
    now = [1_000_000.0]
    staging = Staging(store, ttl_s=3600, clock=lambda: now[0])
    first = staging.create(SAMPLE.read_bytes(), filename=None, who="t")["import_id"]
    now[0] += 1800
    second = staging.create(SAMPLE.read_bytes(), filename=None, who="t")["import_id"]
    leftover = staging.root / ("f" * 32)                     # a crash before staged.json was written
    leftover.mkdir()
    os.utime(leftover, (now[0] - 7200, now[0] - 7200))
    now[0] += 1800
    assert sorted(staging.sweep()) == sorted([first, leftover.name])
    assert staging.report(second)["import_id"] == second
    now[0] += 1800
    assert staging.sweep() == [second]


def test_a_read_only_config_dir_cannot_stage(env, monkeypatch):
    c, _, config_dir, _ = env
    monkeypatch.setattr(ConfigStore, "writable", lambda self: {"writable": False, "reason": "mounted :ro",
                                                                "config_dir": str(self.dir)})
    r = stage(c, SAMPLE.read_bytes())
    assert r.status_code == 503 and "read-only" in r.json()["detail"]
    assert not staging_dir(config_dir).exists()


def test_opencap_export_can_be_staged_too(env):
    c, _, config_dir, _ = env
    export = c.get(f"{V1}/project", headers=VIEWER).json()
    export["files"]["devices_map.yaml"] += "# a comment\n"
    rep = stage(c, json.dumps(export).encode()).json()
    assert rep["kind"] == "opencap-export" and rep["files"] == ["devices_map.yaml"]
    r = c.post(f"{V1}/project/import/{rep['import_id']}/apply", headers={**ADMIN, "If-Match": f'"{rep["revision"]}"'})
    assert r.status_code == 200 and r.json()["applied"] is True
    assert (config_dir / "devices_map.yaml").read_text().endswith("# a comment\n")


def test_bad_uploads(env):
    c, _, config_dir, _ = env
    assert stage(c, b"not json").status_code == 422
    assert stage(c, b'{"hello": 1}').status_code == 422
    r = c.post(f"{V1}/project/import?stage=true", content=SAMPLE.read_bytes(), headers={**ADMIN, "Content-Type": "text/plain"})
    assert r.status_code == 422
    r = c.post(f"{V1}/project/import?stage=true", content=b"{}", headers={**ADMIN, **JSON, "Content-Length": str(65 * 2**20)})
    assert r.status_code == 413
    assert [p for p in staging_dir(config_dir).iterdir()] == []        # failed uploads leave nothing


# --- privacy -----------------------------------------------------------------------------------------

def _plant_credentials(node, found: list[str], n=[0]):
    """Replace every username/password value in the export with a unique marker."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and any(w in key.upper() for w in ("PASSWORD", "USERNAME")) and value:
                n[0] += 1
                node[key] = f"{LEAK}{n[0]}-{key.rsplit('.', 1)[-1].lower()}"
                found.append(node[key])
            elif isinstance(value, str) and key.endswith("ENDPOINT_URL") and value.startswith("opc.tcp://"):
                node[key] = value.replace("opc.tcp://", f"opc.tcp://{LEAK}url-user:{LEAK}url-pass@")
                found += [f"{LEAK}url-user", f"{LEAK}url-pass"]
            else:
                _plant_credentials(value, found, n)
    elif isinstance(node, list):
        for item in node:
            _plant_credentials(item, found, n)


def test_no_credential_in_any_response_log_or_file(env, monkeypatch, caplog):
    c, _, config_dir, tmp = env
    system_tmp = tmp / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))            # nothing may land here
    doc = with_new_device(sample())
    secrets_: list[str] = []
    _plant_credentials(doc, secrets_)
    assert len(secrets_) >= 4                                            # OPC UA user/password/URL, MQTT
    raw = json.dumps(doc).encode()
    assert all(s.encode() in raw for s in secrets_)

    caplog.set_level(logging.DEBUG)
    answers: list[str] = []
    with structlog.testing.capture_logs() as logs:
        r = stage(c, raw)
        answers.append(r.text)
        rep = r.json()
        iid = rep["import_id"]
        # while staged: only staged.json, 0600, in a 0700 directory, under the config directory
        files = [p for p in staging_dir(config_dir).rglob("*") if p.is_file()]
        assert [p.name for p in files] == ["staged.json"]
        if POSIX:
            assert stat.S_IMODE(staging_dir(config_dir).stat().st_mode) == 0o700
            assert stat.S_IMODE((staging_dir(config_dir) / iid).stat().st_mode) == 0o700
            assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        staged_text = files[0].read_text()
        assert "OPENCAP_OPCUA_" in staged_text                           # env-var names instead (D-010)
        answers.append(c.get(f"{V1}/project/import/{iid}", headers=ADMIN).text)
        other = stage(c, raw).json()["import_id"]
        # every file while two imports are staged (apply and discard remove them later)
        while_staged = {p: p.read_bytes() for p in tmp.rglob("*") if p.is_file()}
        answers.append(c.delete(f"{V1}/project/import/{other}", headers=ADMIN).text)
        r = c.post(f"{V1}/project/import/{iid}/apply?apply=true", headers={**ADMIN, "If-Match": f'"{rep["revision"]}"'})
        assert r.status_code == 200
        answers.append(r.text)
        answers.append(c.get(f"{V1}/audit", headers=ADMIN).text)
        answers.append(c.get(f"{V1}/project", headers=ADMIN).text)

    written = {p: p.read_bytes() for p in tmp.rglob("*") if p.is_file()}
    assert list(system_tmp.iterdir()) == []
    haystacks = {"responses": "\n".join(answers), "structlog": repr(logs), "logging": caplog.text,
                 **{f"staged file {p.relative_to(tmp)}": b.decode("utf-8", "replace") for p, b in while_staged.items()},
                 **{f"file {p.relative_to(tmp)}": b.decode("utf-8", "replace") for p, b in written.items()}}
    assert any("staged.json" in k for k in haystacks)
    for where, text in haystacks.items():
        assert LEAK not in text, f"a credential from the export appears in {where}"
    assert not (staging_dir(config_dir) / iid).exists()
