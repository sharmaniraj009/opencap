"""The web UI in a real browser: tests/ui/browser_smoke.js against a live server.

Needs Node.js and the `playwright` Node package with Chromium; skipped without them. Marked
`acceptance` (about 15 s). The script fails on any assertion, script error or CSP violation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from opencap.api.server import create_app
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ENV, SAMPLE, api_cfg, make_config

pytestmark = pytest.mark.acceptance
SCRIPT = Path(__file__).parents[1] / "ui" / "browser_smoke.js"


def _node_env() -> dict[str, str] | None:
    """Environment in which `require('playwright')` works, or None."""
    node = shutil.which("node")
    if node is None:
        return None
    env = dict(os.environ)
    probe = [node, "-e", "require.resolve('playwright')"]
    if subprocess.run(probe, env=env, capture_output=True).returncode == 0:
        return env
    npm = shutil.which("npm")
    if npm:
        root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
        env["NODE_PATH"] = root
        if root and subprocess.run(probe, env=env, capture_output=True).returncode == 0:
            return env
    return None


def test_ui_in_a_browser(tmp_path):
    node_env = _node_env()
    if node_env is None:
        pytest.skip("Node.js with the playwright package is not installed")
    poller = ConfigPoller(make_config(tmp_path))
    poller.identities["SITE MODBUS/Device1"] = {"vendor": "Example Vendor", "revision": "1.2"}
    poller.states["SITE M800/8 PUMP"] = "demoted"
    app = create_app(poller, api_cfg(tmp_path), environ=ENV)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        result = subprocess.run(
            ["node", str(SCRIPT), str(port), ENV["T_ADMIN"], ENV["T_VIEW"], str(SAMPLE)],
            env=node_env, capture_output=True, text=True, timeout=240,
        )
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
        assert "all checks passed" in result.stdout
    finally:
        server.should_exit = True
        thread.join(10)
