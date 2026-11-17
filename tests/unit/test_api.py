from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opencap.api.server import create_app
from opencap.config.models import ApiCfg

DEV = "Site 001/8 PUMP"   # ids contain spaces and a slash


class FakePoller:
    def __init__(self):
        self.reloads = 0

    def health(self):
        return {"status": "ok", "mqtt_connected": True}

    def channels_summary(self):
        return [{"name": "Site 001", "devices": [{"id": DEV, "state": "ok"}]}]

    def _check(self, device_id):
        if device_id != DEV:
            raise KeyError(device_id)

    def device_detail(self, device_id):
        self._check(device_id)
        return {"id": device_id, "state": "ok"}

    def device_values(self, device_id):
        self._check(device_id)
        return {"FLOW_1": {"value": 1.5, "quality": "good", "source_ts": None}}

    async def read_once(self, device_id):
        self._check(device_id)
        return {"FLOW_1": {"value": 2.0, "quality": "good", "source_ts": None}}

    async def reload(self):
        self.reloads += 1
        return {"started": [], "restarted": ["X"], "stopped": [], "unchanged": []}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENCAP_TEST_TOKEN", "s3cret")
    poller = FakePoller()
    app = create_app(poller, ApiCfg(bind="127.0.0.1", token_env="OPENCAP_TEST_TOKEN"))
    return TestClient(app), poller


AUTH = {"Authorization": "Bearer s3cret"}


def test_health_and_metrics_are_open(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "ok"
    r = c.get("/metrics")
    assert r.status_code == 200
    assert "poll_requests_total" in r.text or "# HELP" in r.text


@pytest.mark.parametrize("method,path", [
    ("get", "/channels"), ("get", f"/devices/{DEV}"), ("get", f"/devices/{DEV}/values"),
    ("post", f"/devices/{DEV}/read-once"), ("post", "/reload"),
])
def test_token_required(client, method, path):
    c, _ = client
    assert getattr(c, method)(path).status_code == 401
    assert getattr(c, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert getattr(c, method)(path, headers=AUTH).status_code == 200


def test_device_routes_with_slash_and_spaces(client):
    c, poller = client
    assert c.get(f"/devices/{DEV}", headers=AUTH).json()["id"] == DEV
    assert c.get(f"/devices/{DEV}/values", headers=AUTH).json()["FLOW_1"]["value"] == 1.5
    assert c.post(f"/devices/{DEV}/read-once", headers=AUTH).json()["FLOW_1"]["value"] == 2.0
    assert c.post("/reload", headers=AUTH).json()["restarted"] == ["X"]
    assert poller.reloads == 1


def test_unknown_device_404(client):
    c, _ = client
    assert c.get("/devices/NOPE/X", headers=AUTH).status_code == 404
    assert c.get("/devices/NOPE/X/values", headers=AUTH).status_code == 404
    assert c.post("/devices/NOPE/X/read-once", headers=AUTH).status_code == 404


def test_no_token_on_public_bind_refused(monkeypatch):
    monkeypatch.delenv("OPENCAP_NO_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        create_app(FakePoller(), ApiCfg(bind="192.0.2.11", token_env="OPENCAP_NO_TOKEN"))


def test_no_token_on_loopback_is_open(monkeypatch):
    monkeypatch.delenv("OPENCAP_NO_TOKEN", raising=False)
    c = TestClient(create_app(FakePoller(), ApiCfg(bind="127.0.0.1", token_env="OPENCAP_NO_TOKEN")))
    assert c.get("/channels").status_code == 200
