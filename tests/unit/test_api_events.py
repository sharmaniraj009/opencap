"""Log events of the config API (#135): config.write_failed, config.change_rejected,
api.request_refused (rate limited), and no secret in any log, audit or event line (LOG-R12)."""

from __future__ import annotations

import json
import os
import stat

import pytest
from structlog.testing import capture_logs

from opencap.api.events import RefusalLog
from tests.unit.test_config_api import (  # noqa: F401  (env is a fixture)
    ADMIN,
    DEV,
    DEV_PATH,
    ENV,
    V1,
    VIEWER,
    admin_w,
    audit_lines,
    env,
    rev,
)


def named(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e["event"] == event]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs directory permissions that bind")
def test_read_only_config_dir_is_503_and_logs_write_failed(env):
    c, _, cfg, tmp = env
    headers = admin_w(c)
    mode = cfg.stat().st_mode
    cfg.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with capture_logs() as logs:
            r = c.patch(DEV_PATH, json={"retries": 1}, headers=headers)
    finally:
        cfg.chmod(mode)
    assert r.status_code == 503
    (event,) = named(logs, "config.write_failed")
    assert event["log_level"] == "error"
    assert (event["who"], event["via"], event["status"], event["reason"]) == ("alice", "api", 503, "read_only")
    assert event["action"] == "device.update" and event["files"] == ["devices.json"]
    assert audit_lines(tmp) == []


def test_partial_write_names_the_bak_files_to_restore(env, monkeypatch):
    from opencap.config import store as store_mod

    c, *_ = env

    def boom(directory, texts):
        raise store_mod.PartialWrite("could not restore", ["devices.json"])

    monkeypatch.setattr(store_mod, "commit_files", boom)
    with capture_logs() as logs:
        r = c.patch(DEV_PATH, json={"retries": 1}, headers=admin_w(c))
    assert r.status_code == 500
    (event,) = named(logs, "config.write_failed")
    assert event["reason"] == "partial" and event["status"] == 500 and event["restore"] == ["devices.json.bak"]


def test_disk_full_reason(env, monkeypatch):
    import errno

    from opencap.config import store as store_mod

    c, *_ = env

    def full(directory, texts):
        raise store_mod.StorageError("disk full", full=True, cause=OSError(errno.ENOSPC, "full"))

    monkeypatch.setattr(store_mod, "commit_files", full)
    with capture_logs() as logs:
        assert c.patch(DEV_PATH, json={"retries": 1}, headers=admin_w(c)).status_code == 507
    assert named(logs, "config.write_failed")[0]["reason"] == "disk_full"


def test_duplicate_device_logs_one_change_rejected_and_no_audit_entry(env):
    c, _, _, tmp = env
    channel = DEV.split("/")[0]
    name = DEV.split("/")[1]
    before = audit_lines(tmp)
    with capture_logs() as logs:
        r = c.post(f"{V1}/channels/{channel}/devices", headers=admin_w(c),
                   json={"name": name, "driver": "simulator", "host": "192.0.2.9", "port": 1})
    assert r.status_code == 409
    (event,) = named(logs, "config.change_rejected")
    assert event["log_level"] == "info"
    assert (event["who"], event["via"], event["action"], event["status"]) == ("alice", "api", "device.create", 409)
    assert event["target"] == channel and event["count"] == 1 and event["msg"]
    assert named(logs, "api.request_refused") == []                     # reported once, not twice
    assert audit_lines(tmp) == before                                    # D-043


def test_validation_error_event_has_loc_and_msg_but_no_values(env):
    c, *_ = env
    with capture_logs() as logs:
        r = c.patch(DEV_PATH, json={"port": "not-a-number-xyz"}, headers=admin_w(c))
    assert r.status_code == 422
    (event,) = named(logs, "config.change_rejected")
    assert event["loc"] == ["port"] and event["status"] == 422
    assert "not-a-number-xyz" not in json.dumps(logs, default=str)


def test_stale_and_missing_if_match_are_change_rejected(env):
    c, *_ = env
    with capture_logs() as logs:
        assert c.patch(DEV_PATH, json={"retries": 1}, headers=ADMIN).status_code == 428
        assert c.patch(DEV_PATH, json={"retries": 1}, headers={**ADMIN, "If-Match": "0000000000000000"}).status_code == 412
    assert [e["status"] for e in named(logs, "config.change_rejected")] == [428, 412]


def test_reads_with_4xx_do_not_log_change_rejected(env):
    c, *_ = env
    with capture_logs() as logs:
        assert c.get(f"{V1}/channels/NOPE", headers=VIEWER).status_code == 404
    assert named(logs, "config.change_rejected") == []


def test_refused_requests_emit_one_event_each_status(env):
    c, *_ = env
    with capture_logs() as logs:
        assert c.get(f"{V1}/channels", headers={"Authorization": "Bearer wrong-token-value"}).status_code == 401
        assert c.patch(DEV_PATH, json={"retries": 1}, headers=VIEWER).status_code == 403
        assert c.patch(DEV_PATH, content=b"x", headers={**ADMIN, "Content-Length": str(2 * 1024 * 1024)}).status_code == 413
        assert c.get(f"{V1}/audit?limit=0", headers=VIEWER).status_code == 422
        assert c.get(f"{DEV_PATH}/identity?refresh=true", headers=VIEWER).status_code == 403
    events = named(logs, "api.request_refused")
    by_status = {e["status"]: e for e in events}
    assert set(by_status) == {401, 403, 413, 422}
    assert by_status[401]["who"] == "unknown" and by_status[401]["log_level"] == "warning"
    assert by_status[403]["who"] in ("grafana", "alice") and by_status[413]["who"] == "alice"
    assert by_status[422]["who"] == "grafana" and by_status[422]["method"] == "GET"
    for e in events:
        assert {"method", "path", "status", "who", "client", "via"} <= set(e)
        assert "wrong-token-value" not in json.dumps(e)
    assert by_status[422]["path"] == f"{V1}/audit" and "limit" not in json.dumps(by_status[422])


def test_refusal_log_is_limited_to_one_per_second_per_client_and_status():
    now = [100.0]
    refusals = RefusalLog(clock=lambda: now[0])

    def refuse(client="192.0.2.1", status=401):
        refusals.refused(method="GET", path="/api/v1/x", status=status, who=None, client=client, via="api")

    with capture_logs() as logs:
        for _ in range(5):
            refuse()
        refuse(status=403)                       # another status: its own budget
        refuse(client="192.0.2.2")               # another client: its own budget
        now[0] += 1.5
        refuse()
        now[0] += 0.1
        refuse()
    events = named(logs, "api.request_refused")
    assert len(events) == 4
    assert "suppressed" not in events[0] and events[-1]["suppressed"] == 4


def test_known_secrets_never_reach_a_log_audit_or_event_line(env, tmp_path):
    c, _, cfg, tmp = env
    secrets = ["adm1n-secret", "v1ewer-secret", "legacy-secret", "mqtt-broker-pw-123"]
    with capture_logs() as logs:
        c.get(f"{V1}/channels", headers={"Authorization": "Bearer adm1n-secrett"})        # near miss
        c.get(f"{V1}/channels", headers={"Authorization": "Bearer v1ewer-secret-bad"})
        c.patch(DEV_PATH, json={"retries": 1}, headers=VIEWER)
        c.patch(DEV_PATH, json={"port": "x"}, headers=admin_w(c))
        c.patch(DEV_PATH, json={"retries": 2}, headers=admin_w(c))
        c.post(f"{V1}/reload", headers=ADMIN)
        c.get(f"{V1}/support", headers=VIEWER)
        c.get(f"{V1}/whoami", headers=VIEWER)
    text = json.dumps(logs, default=str) + (tmp / "audit.jsonl").read_text()
    for secret in secrets:
        assert secret not in text
    assert "Authorization" not in text and "Bearer" not in text
    assert rev(c)
    assert ENV["T_ADMIN"] == "adm1n-secret"
