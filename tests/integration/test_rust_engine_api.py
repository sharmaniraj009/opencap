"""The Rust engine's admin API and reload, on the real binary (src/opencap/api/server.py and
Poller.reload() are the reference; tests/unit/test_api.py and test_runtime_reload.py).

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import os
import signal

import httpx
import pytest
import yaml

from tests.helpers.configdir import write_config
from tests.helpers.engine import RustEngine, rust_binary
from tests.helpers.modbus_sim import free_port, make_sim_devices

BIN = rust_binary()
pytestmark = pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)")


async def _wait(pred, timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.05)


def _devices():
    # two channels; nothing listens on the ports (reload does not need live PLCs)
    return [d.model_copy(update={"port": free_port()}) for d in make_sim_devices(4, 3, per_channel=2)]


async def test_reload_restarts_only_the_changed_channels(tmp_path):
    devices = _devices()
    config_dir = write_config(tmp_path, devices, mqtt_port=free_port(), api_port=free_port())
    engine = RustEngine(config_dir, BIN, tmp_path / "rust")
    await engine.start()
    try:
        base = engine.base_url
        async with httpx.AsyncClient(base_url=base, timeout=10) as http:
            assert (await http.post("/reload")).json() == {
                "started": [], "restarted": [], "stopped": [], "unchanged": ["SIM_SITE_01", "SIM_SITE_02"]}

            ov_path = config_dir / "overrides.yaml"
            ov = (yaml.safe_load(ov_path.read_text()) if ov_path.exists() else None) or {}
            ov.setdefault("devices", {})[devices[0].id] = {"host": "192.0.2.77"}
            ov_path.write_text(yaml.safe_dump(ov))
            assert (await http.post("/reload")).json() == {
                "started": [], "restarted": ["SIM_SITE_01"], "stopped": [], "unchanged": ["SIM_SITE_02"]}
            assert (await http.get(f"/devices/{devices[0].id}")).json()["host"] == "192.0.2.77"

            ov["channels"] = {"SIM_SITE_02": {"enabled": False}}
            ov_path.write_text(yaml.safe_dump(ov))
            engine.proc.send_signal(signal.SIGHUP)                          # same path as POST /reload
            await _wait(lambda: engine.health()["channels"] == 1, 10, "SIGHUP reload")
            assert (await http.get(f"/devices/{devices[2].id}")).status_code == 404

            (config_dir / "poller.yaml").write_text("mqtt: {hots: typo}\n")  # invalid: old config kept
            r = await http.post("/reload")
            assert r.status_code == 400 and r.json()["detail"].startswith("reload failed, old config kept: ")
            assert engine.health()["channels"] == 1
    finally:
        await engine.stop()


async def test_bearer_token_and_open_monitoring_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAP_API_TOKEN", "s3cret-for-tests")
    devices = _devices()[:1]
    config_dir = write_config(tmp_path, devices, mqtt_port=free_port(), api_port=free_port())
    engine = RustEngine(config_dir, BIN, tmp_path / "rust")
    await engine.start()
    try:
        async with httpx.AsyncClient(base_url=engine.base_url, timeout=10) as http:
            assert (await http.get("/health")).status_code == 200
            metrics = await http.get("/metrics")
            assert metrics.status_code == 200 and metrics.headers["content-type"].startswith("text/plain")
            dev = devices[0].id
            for method, path in (("GET", "/channels"), ("GET", f"/devices/{dev}"), ("GET", f"/devices/{dev}/values"),
                                 ("POST", f"/devices/{dev}/read-once"), ("POST", "/reload")):
                r = await http.request(method, path)
                assert r.status_code == 401 and r.json() == {"detail": "missing or bad bearer token"}, (method, path)
                r = await http.request(method, path, headers={"Authorization": "Bearer wrong"})
                assert r.status_code == 401
            ok = {"Authorization": "Bearer s3cret-for-tests"}
            assert (await http.get("/channels", headers=ok)).json()[0]["devices"][0]["id"] == dev
            r = await http.get("/devices/NOPE/X", headers=ok)
            assert r.status_code == 404 and r.json() == {"detail": "unknown device 'NOPE/X'"}
            once = (await http.post(f"/devices/{dev}/read-once", headers=ok)).json()  # PLC unreachable
            assert once and {v["quality"] for v in once.values()} == {"bad_comm"}
    finally:
        await engine.stop()


async def test_refuses_a_public_bind_without_a_token(tmp_path):
    os.environ.pop("OPENCAP_NO_SUCH_TOKEN", None)
    config_dir = write_config(tmp_path, _devices()[:1], poller={"api": {"bind": "192.0.2.10", "token_env": "OPENCAP_NO_SUCH_TOKEN"}})
    proc = await asyncio.create_subprocess_exec(str(BIN), "run", "--config", str(config_dir), cwd=str(tmp_path),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode == 1
    assert "API bound to 192.0.2.10 without a token: set OPENCAP_NO_SUCH_TOKEN or bind to 127.0.0.1" in err.decode()
