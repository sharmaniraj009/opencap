"""The Rust engine's admin API and reload, on the real binary (src/opencap/api/server.py and
Poller.reload() are the reference; tests/unit/test_api.py and test_runtime_reload.py).

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import contextlib
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


async def test_reload_restarts_only_the_changed_channels(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAP_API_TOKEN", "s3cret-for-tests")      # with no token the API is read-only
    devices = _devices()
    config_dir = write_config(tmp_path, devices, mqtt_port=free_port(), api_port=free_port())
    engine = RustEngine(config_dir, BIN, tmp_path / "rust")
    await engine.start()
    try:
        base = engine.base_url
        async with httpx.AsyncClient(base_url=base, timeout=10,
                                     headers={"Authorization": "Bearer s3cret-for-tests"}) as http:
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


# --- the same checks against both engines (#82, D-062): Python's ApiGuard/Auth is the reference --

OPEN_READ_ONLY = ("no API token is configured, so the API is read-only: "
                  "set api.tokens (or api.token_env) in poller.yaml to make changes")
LOOPBACK_ONLY = "no API token is configured: only requests to localhost are answered"
TOKENS = [{"name": "ops", "role": "admin", "token_env": "OPENCAP_TEST_OPS"},
          {"name": "wall", "token_env": "OPENCAP_TEST_WALL"}]
OPS = {"Authorization": "Bearer ops-secret-for-tests"}
WALL = {"Authorization": "Bearer wall-secret-for-tests"}


@contextlib.asynccontextmanager
async def _api(engine: str, root, *, tokens: bool):
    """An HTTP client for `engine`'s API on a one-device config (named tokens or open mode)."""
    api = {"audit_log": str(root / "audit.jsonl"), **({"tokens": TOKENS} if tokens else {})}
    config_dir = write_config(root, _devices()[:1], mqtt_port=free_port(), api_port=free_port(), poller={"api": api})
    if engine == "python":
        from opencap.api.server import create_app
        from tests.helpers.fake_poller import ConfigPoller

        poller = ConfigPoller(config_dir)
        cfg = poller.loaded.poller.api
        app = create_app(poller, cfg, environ=dict(os.environ))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{cfg.port}") as http:
            yield http
        return
    rust = RustEngine(config_dir, BIN, root / "rust")
    await rust.start()
    try:
        async with httpx.AsyncClient(base_url=rust.base_url, timeout=10) as http:
            yield http
    finally:
        await rust.stop()


@pytest.fixture
def no_token_env(monkeypatch):
    monkeypatch.delenv("OPENCAP_API_TOKEN", raising=False)
    monkeypatch.setenv("OPENCAP_TEST_OPS", "ops-secret-for-tests")
    monkeypatch.setenv("OPENCAP_TEST_WALL", "wall-secret-for-tests")


@pytest.mark.parametrize("engine", ["python", "rust"])
async def test_open_mode_is_read_only_and_answers_only_loopback_hosts(engine, tmp_path, no_token_env):
    async with _api(engine, tmp_path, tokens=False) as http:
        assert (await http.get("/channels")).status_code == 200            # the viewer "anonymous"
        for path in ("/reload", f"/devices/{_devices()[0].id}/read-once"):
            r = await http.post(path)
            assert r.status_code == 403 and r.json() == {"detail": OPEN_READ_ONLY}, path
        # DNS rebinding: a foreign page reaching the loopback API sends its own host name
        r = await http.get("/channels", headers={"Host": "attacker.example"})
        assert r.status_code == 403 and r.json() == {"detail": LOOPBACK_ONLY}
        assert (await http.get("/channels", headers={"Host": "localhost:8080"})).status_code == 200
        assert (await http.get("/health", headers={"Host": "attacker.example"})).status_code == 200


@pytest.mark.parametrize("engine", ["python", "rust"])
async def test_named_tokens_and_roles(engine, tmp_path, no_token_env):
    async with _api(engine, tmp_path, tokens=True) as http:
        assert (await http.get("/channels", headers=WALL)).status_code == 200
        r = await http.post("/reload", headers=WALL)
        assert r.status_code == 403 and r.json() == {"detail": "token 'wall' has role viewer: read-only"}
        assert (await http.post("/reload", headers=OPS)).status_code == 200
        r = await http.get("/channels")
        assert r.status_code == 401 and r.json() == {"detail": "missing or bad bearer token"}
        assert r.headers["www-authenticate"] == "Bearer"
        # with tokens, any Host is answered: the token is the protection
        assert (await http.get("/channels", headers={**WALL, "Host": "192.0.2.30"})).status_code == 200


@pytest.mark.parametrize("engine", ["python", "rust"])
async def test_auth_before_the_body_and_the_body_cap(engine, tmp_path, no_token_env):
    async with _api(engine, tmp_path, tokens=True) as http:
        # unauthenticated with a malformed body: 401, never 400/422 (the body is not read)
        r = await http.post("/reload", content=b"{not json", headers={"Content-Type": "application/json"})
        assert r.status_code == 401
        r = await http.post("/reload", content=b"x" * (1024 * 1024 + 1), headers=OPS)
        assert r.status_code == 413 and r.json() == {"detail": "request body larger than 1048576 bytes"}


async def test_rust_counts_a_chunked_body_against_the_cap(tmp_path, no_token_env):
    # Rust reads every body up front, so a chunked overflow is 413 on any route; Python counts
    # only the bodies a route reads (D-062)
    async def chunks():
        for _ in range(17):
            yield b"x" * 65536

    async with _api("rust", tmp_path, tokens=True) as http:
        r = await http.post("/reload", content=chunks(), headers=OPS)
        assert r.status_code == 413 and r.json() == {"detail": "request body larger than 1048576 bytes"}


async def test_a_listed_token_without_its_env_var_stops_the_start(tmp_path, no_token_env, monkeypatch):
    monkeypatch.delenv("OPENCAP_TEST_OPS")
    config_dir = write_config(tmp_path, _devices()[:1], mqtt_port=free_port(), api_port=free_port(),
                              poller={"api": {"tokens": TOKENS}})
    proc = await asyncio.create_subprocess_exec(str(BIN), "run", "--config", str(config_dir), cwd=str(tmp_path),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode == 1 and "api.tokens[0] 'ops': env var OPENCAP_TEST_OPS is not set" in err.decode()


async def test_refuses_a_public_bind_without_a_token(tmp_path):
    os.environ.pop("OPENCAP_NO_SUCH_TOKEN", None)
    config_dir = write_config(tmp_path, _devices()[:1], poller={"api": {"bind": "192.0.2.10", "token_env": "OPENCAP_NO_SUCH_TOKEN"}})
    proc = await asyncio.create_subprocess_exec(str(BIN), "run", "--config", str(config_dir), cwd=str(tmp_path),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode == 1
    assert "API bound to 192.0.2.10 without a token: set OPENCAP_NO_SUCH_TOKEN or bind to 127.0.0.1" in err.decode()
