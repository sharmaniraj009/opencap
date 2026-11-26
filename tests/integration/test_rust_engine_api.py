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
        # exactly 127.0.0.1, localhost and ::1 (Python's LOOPBACK_HOSTS), not all of 127/8
        r = await http.get("/channels", headers={"Host": "127.0.0.2:8080"})
        assert r.status_code == 403 and r.json() == {"detail": LOOPBACK_ONLY}
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
        # over the cap by length: a clean 413, not a connection reset (Rust drains the refused body,
        # D-062), repeated because a reset is a race
        for size in (1024 * 1024 + 1, 4 * 1024 * 1024) * 3:
            r = await http.post("/reload", content=b"x" * size, headers=OPS)
            assert r.status_code == 413 and r.json() == {"detail": "request body larger than 1048576 bytes"}, size


async def test_rust_refuses_chunked_and_huge_bodies_with_a_clean_413(tmp_path, no_token_env):
    # Rust reads every body up front, so a chunked overflow is 413 on any route; Python counts
    # only the bodies a route reads (D-062)
    async def chunks(n):
        for _ in range(n):
            yield b"x" * 65536

    async with _api("rust", tmp_path, tokens=True) as http:
        for n in (17, 64, 17):
            r = await http.post("/reload", content=chunks(n), headers=OPS)
            assert r.status_code == 413 and r.json() == {"detail": "request body larger than 1048576 bytes"}, n
        # open paths take no token, so they take no body either: refused unread (Python ignores it)
        r = await http.request("GET", "/health", content=b"hello")
        assert r.status_code == 413 and r.json() == {"detail": "this request takes no body"}
        assert (await http.get("/health")).status_code == 200
        # declared larger than the drain limit (8 MiB): refused at once, before a body byte is sent
        host, port = http.base_url.host, http.base_url.port
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(f"POST /reload HTTP/1.1\r\nHost: {host}:{port}\r\nAuthorization: {OPS['Authorization']}\r\n"
                     f"Content-Length: {64 * 1024 * 1024}\r\n\r\n".encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        assert status.startswith(b"HTTP/1.1 413"), status


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


# --- readiness, about, support, info (#194, the Rust half of #123; D-075) -------------------------

async def test_about_matches_python_except_engine(tmp_path):
    from opencap.core import support

    config_dir = write_config(tmp_path, _devices(), mqtt_port=free_port(), api_port=free_port())
    engine = RustEngine(config_dir, BIN, tmp_path / "rust")
    await engine.start()
    try:
        async with httpx.AsyncClient(base_url=engine.base_url, timeout=10) as http:
            rust = (await http.get("/api/v1/about")).json()               # open: no token
            live = await http.get("/health/live")
            support_resp = await http.get("/api/v1/support")
        py = support.about()
        assert rust.pop("engine") == "rust" and py.pop("engine") == "python"
        assert rust == py
        assert live.status_code == 200 and live.json() == {"status": "live"}
        # open mode is read-only on loopback, and the support summary is viewer-readable there
        assert support_resp.status_code == 200
        from opencap.config.store import ConfigStore
        assert support_resp.json()["config_revision"] == ConfigStore(config_dir).revision()
    finally:
        await engine.stop()


async def test_ready_is_503_until_the_muted_device_is_offline(tmp_path):
    """The Python twin is tests/integration/test_readiness.py: same devices, same answers."""
    from tests.helpers.modbus_sim import start_sims, stop_sims

    devices = make_sim_devices(2, 4, seed=5, scan_ms=100, timeout_ms=300)
    devices = [d.model_copy(update={"retries": 0}) for d in devices]
    sims = await start_sims(devices)
    sims[1].mute = True                       # accepts the connection, never answers
    muted = sims[1].device.id
    try:
        # A long connect timeout on the muted one is not needed: a mute Modbus device times out per read.
        config_dir = write_config(tmp_path, [s.device for s in sims], mqtt_port=free_port(), api_port=free_port())
        engine = RustEngine(config_dir, BIN, tmp_path / "rust")
        await engine.start()
        try:
            async with httpx.AsyncClient(base_url=engine.base_url, timeout=10) as http:
                seen = []
                deadline = asyncio.get_running_loop().time() + 20
                while True:
                    r = await http.get("/health/ready")
                    seen.append((r.status_code, r.json()))
                    if r.status_code == 200:
                        break
                    assert r.status_code == 503 and r.json()["ready"] is False, r.json()
                    assert asyncio.get_running_loop().time() < deadline, seen[-1]
                    await asyncio.sleep(0.02)
                health = (await http.get("/health")).json()
        finally:
            await engine.stop()
    finally:
        await stop_sims(sims)
    assert any(code == 503 and body["waiting"] == [muted] for code, body in seen), seen[:5]
    assert seen[-1][1] == {"ready": True, "waiting": []}
    assert health["initial_scan_complete"] is True and health["initial_scan_ms"] > 0


async def test_info_has_no_ip_host_or_secret(tmp_path, monkeypatch):
    import json
    import re
    import subprocess

    from tests.unit.test_no_private_data import IP_RE

    secret = "s3cr3t-token-value"
    monkeypatch.setenv("OPENCAP_MQTT_PASSWORD", secret)
    devices = make_sim_devices(1, 2, seed=1)
    devices[0] = devices[0].model_copy(update={"host": "192.0.2.17"})
    config_dir = write_config(tmp_path, devices, poller={
        "mqtt": {"host": "broker.plant-a.example.invalid", "username": "operator-bob"},
        "opcua_server": {"enabled": True, "endpoint": "opc.tcp://198.51.100.9:4840/opencap",
                         "username_env": "SECRET_ENV_NAME", "password_env": "SECRET_PW_ENV",
                         "allow_insecure_remote": True},
    })
    for args in ([], ["--json"]):
        out = subprocess.run([str(BIN), "info", "--config", str(config_dir), *args], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        text = out.stdout
        assert not IP_RE.findall(text), text
        for forbidden in ("192.0.2.17", "198.51.100.9", "plant-a", "broker", "operator-bob", secret,
                          "SECRET_ENV_NAME", "OPENCAP_MQTT_PASSWORD", str(tmp_path)):
            assert forbidden not in text, (forbidden, text)
        assert re.search(r"[0-9a-f]{16}", text), text                 # the config revision
    info = json.loads(out.stdout)
    from opencap.config.store import ConfigStore
    assert info["engine"] == "rust" and info["config_revision"] == ConfigStore(config_dir).revision()
    assert info["toolchain"]["rust"] and info["north_interfaces"] == ["mqtt", "api"]   # no OPC UA server in Rust yet
    broken = subprocess.run([str(BIN), "info", "--config", str(tmp_path / "missing")], capture_output=True,
                            text=True, timeout=60)
    assert broken.returncode == 0 and "config revision: unknown" in broken.stdout
