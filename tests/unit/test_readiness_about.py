"""`/health/ready`, `/health/live`, `GET /api/v1/about`, `GET /api/v1/support` and `opencap info` (#123)."""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from opencap import __version__
from opencap.__main__ import app as cli
from opencap.api.guard import OPEN_PATHS
from opencap.api.server import create_app
from opencap.config.schema import SCHEMA_VERSION
from tests.helpers.configdir import write_config
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_api import FakePoller
from tests.unit.test_config_api import ENV, V1, VIEWER, api_cfg, make_config
from tests.unit.test_no_private_data import IP_RE


class ReadyPoller(FakePoller):
    def __init__(self, waiting=()):
        super().__init__()
        self.waiting = list(waiting)

    def readiness(self):
        return {"ready": not self.waiting, "waiting": list(self.waiting)}


def open_client(tmp_path, poller):
    return TestClient(create_app(poller, api_cfg(tmp_path), environ=ENV))


def test_new_routes_are_in_the_guards_open_list():
    assert {"/health/ready", "/health/live", "/api/v1/about"} <= OPEN_PATHS


def test_ready_503_lists_waiting_devices_then_200(tmp_path):
    poller = ReadyPoller(["Plant A/PLC1", "Plant A/PLC2"])
    c = open_client(tmp_path, poller)
    r = c.get("/health/ready")                       # no token
    assert r.status_code == 503
    assert r.json() == {"ready": False, "waiting": ["Plant A/PLC1", "Plant A/PLC2"]}
    poller.waiting = ["Plant A/PLC2"]
    assert c.get("/health/ready").json()["waiting"] == ["Plant A/PLC2"]
    poller.waiting = []
    r = c.get("/health/ready")
    assert r.status_code == 200 and r.json() == {"ready": True, "waiting": []}


def test_live_is_200_without_a_token_and_does_not_ask_the_poller(tmp_path):
    c = open_client(tmp_path, object())              # a poller without any method
    r = c.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "live"}


def test_about_open_and_complete(tmp_path):
    c = open_client(tmp_path, ReadyPoller())
    r = c.get(f"{V1}/about")
    assert r.status_code == 200
    major, minor, patch = (int(x) for x in re.match(r"(\d+)\.(\d+)\.(\d+)", __version__).groups())
    assert r.json() == {"product": "opencap", "version": __version__, "version_major": major,
                        "version_minor": minor, "version_patch": patch, "engine": "python",
                        "api_versions": ["v1"], "config_schema_version": SCHEMA_VERSION}


def test_support_needs_a_token_and_is_viewer_readable(tmp_path):
    poller = ConfigPoller(make_config(tmp_path))
    c = open_client(tmp_path, poller)
    assert c.get(f"{V1}/support").status_code == 401
    r = c.get(f"{V1}/support", headers=VIEWER)
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "python" and body["config_revision"]
    assert {"mqtt", "api"} <= set(body["north_interfaces"])
    assert {d["name"] for d in body["drivers"]} >= {"modbus_tcp", "s7", "ab_logix"}


def test_info_has_no_ip_host_or_secret(tmp_path, monkeypatch):
    """`opencap info` for a config full of addresses, host names and a secret reveals none of them."""
    from tests.helpers.modbus_sim import make_sim_devices

    secret = "s3cr3t-token-value"
    monkeypatch.setenv("OPENCAP_MQTT_PASSWORD", secret)
    monkeypatch.setenv("OPENCAP_API_TOKEN", secret)
    devices = make_sim_devices(1, 2, seed=1)
    devices[0] = devices[0].model_copy(update={"host": "192.0.2.17"})
    config_dir = write_config(tmp_path, devices, poller={
        "mqtt": {"host": "broker.plant-a.example.invalid", "username": "operator-bob"},
        "opcua_server": {"enabled": True, "endpoint": "opc.tcp://198.51.100.9:4840/opencap",
                         "username_env": "SECRET_ENV_NAME", "password_env": "SECRET_PW_ENV", "allow_insecure_remote": True},
    })
    for args in ([], ["--json"]):
        result = CliRunner().invoke(cli, ["info", "-c", str(config_dir), *args])
        assert result.exit_code == 0, result.output
        out = result.output
        assert not IP_RE.findall(out), out
        for forbidden in ("192.0.2.17", "198.51.100.9", "plant-a", "broker", "operator-bob", secret,
                          "SECRET_ENV_NAME", "OPENCAP_MQTT_PASSWORD", str(tmp_path)):
            assert forbidden not in out, (forbidden, out)
        assert "opcua_server" in out and __version__ in out
        assert re.search(r"[0-9a-f]{16}", out)       # the config revision
    info = json.loads(CliRunner().invoke(cli, ["info", "-c", str(config_dir), "--json"]).output)
    assert info["engine"] == "python" and info["north_interfaces"] == ["mqtt", "api", "opcua_server"]


def test_info_without_a_config_still_describes_the_build(tmp_path):
    result = CliRunner().invoke(cli, ["info", "-c", str(tmp_path / "nowhere")])
    assert result.exit_code == 0
    assert "config revision: unknown" in result.output and __version__ in result.output
