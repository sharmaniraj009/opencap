"""api.started, api.bind_failed, correlation fields and secrets in the log of a real poller (#135)."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
from structlog.testing import capture_logs
from typer.testing import CliRunner

from opencap.core.runtime import ApiStartError, Poller
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import free_port, make_sim_devices

API_TOKEN = "ev-adm1n-token-0001"
MQTT_PW = "ev-mqtt-broker-pw-0002"
POLLER = {"api": {"tokens": [{"name": "alice", "role": "admin", "token_env": "EV_API_TOKEN"}]},
          "mqtt": {"username": "opencap", "password_env": "EV_MQTT_PW"}}


def names(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e["event"] == event]


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("EV_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("EV_MQTT_PW", MQTT_PW)
    monkeypatch.delenv("OPENCAP_API_TOKEN", raising=False)


async def test_started_reload_and_secrets(tmp_path, secrets):
    devices = make_sim_devices(1, 3, seed=5, scan_ms=200, timeout_ms=500)
    port = free_port()
    async with TestBroker() as broker:
        config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, api_port=port, poller=POLLER)
        poller = Poller(config_dir)
        with capture_logs() as logs:
            await poller.start()
            try:
                started = names(logs, "api.started")
                assert len(started) == 1
                assert (started[0]["bind"], started[0]["port"]) == ("127.0.0.1", port)
                assert started[0]["open_mode"] is False and started[0]["tokens"] == 1
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                    headers = {"Authorization": f"Bearer {API_TOKEN}", "X-Opencap-Client": "ui"}
                    assert (await http.post("/api/v1/reload", headers=headers)).status_code == 200
                    assert (await http.get("/api/v1/channels",
                                           headers={"Authorization": f"Bearer {API_TOKEN}x"})).status_code == 401
                    status = (await http.get("/api/v1/project/status", headers=headers)).json()
                (reloaded,) = names(logs, "config_reloaded")
                assert (reloaded["who"], reloaded["via"], reloaded["revision"]) == ("alice", "ui", status["revision"])
                (up,) = names(logs, "poller_started")
                assert up["engine"] == "python" and up["instance"] == "opencap-test" and up["revision"]
                refused = names(logs, "api.request_refused")
                assert [e["status"] for e in refused] == [401]
            finally:
                await poller.stop()
        text = json.dumps(logs, default=str)
        assert API_TOKEN not in text and MQTT_PW not in text


async def test_sighup_style_reload_has_revision_but_no_who(tmp_path, secrets):
    devices = make_sim_devices(1, 2, seed=6, scan_ms=200, timeout_ms=500)
    async with TestBroker() as broker:
        config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, api_port=free_port(), poller=POLLER)
        poller = Poller(config_dir, serve_api=False)
        await poller.start()
        try:
            with capture_logs() as logs:
                await poller.reload()
        finally:
            await poller.stop()
    (event,) = names(logs, "config_reloaded")
    assert event["revision"] and "who" not in event


async def test_taken_port_logs_bind_failed_and_raises(tmp_path, secrets):
    devices = make_sim_devices(1, 2, seed=7, scan_ms=200, timeout_ms=500)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        async with TestBroker() as broker:
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, api_port=port, poller=POLLER)
            poller = Poller(config_dir)
            with capture_logs() as logs, pytest.raises(ApiStartError) as exc:
                await asyncio.wait_for(poller.run_forever(), 20)
    assert exc.value.reason == "in_use"
    (event,) = names(logs, "api.bind_failed")
    assert (event["bind"], event["port"], event["reason"], event["log_level"]) == ("127.0.0.1", port, "in_use", "error")
    assert not names(logs, "api.started")


def test_run_exits_non_zero_when_the_api_port_is_taken(tmp_path, secrets):
    from opencap.__main__ import app

    devices = make_sim_devices(1, 1, seed=8, scan_ms=500, timeout_ms=500)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        config_dir = write_config(tmp_path, devices, mqtt_port=free_port(), api_port=taken.getsockname()[1],
                                  poller=POLLER)
        result = CliRunner().invoke(app, ["run", "--config", str(config_dir)])
    assert result.exit_code == 1


async def test_open_mode_on_a_non_loopback_bind_is_bind_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCAP_API_TOKEN", raising=False)
    devices = make_sim_devices(1, 1, seed=9, scan_ms=500, timeout_ms=500)
    config_dir = write_config(tmp_path, devices, mqtt_port=free_port(), api_port=free_port(),
                              poller={"api": {"bind": "0.0.0.0"}})
    poller = Poller(config_dir)
    with capture_logs() as logs, pytest.raises(ApiStartError):
        await asyncio.wait_for(poller.run_forever(), 20)
    assert names(logs, "api.bind_failed")[0]["reason"] == "config"
