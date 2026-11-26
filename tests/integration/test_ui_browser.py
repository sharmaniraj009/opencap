"""The web UI in a real browser: tests/ui/browser_smoke.js against a live server.

Needs Node.js and the `playwright` Node package with Chromium; skipped without them, unless
`OPENCAP_REQUIRE_BROWSER` is set (the Linux acceptance jobs in CI set it, so a missing browser fails
instead of skipping, #234). `OPENCAP_UI_SHOTS` names a directory for the script's screenshots (CI
uploads them when the job fails). Marked `acceptance` (about 10 s). The script fails on any
assertion, script error or CSP violation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from opencap.api.server import create_app
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ENV, SAMPLE, api_cfg, make_config

pytestmark = pytest.mark.acceptance
SCRIPT = Path(__file__).parents[1] / "ui" / "browser_smoke.js"
REQUIRE_ENV = "OPENCAP_REQUIRE_BROWSER"
SHOTS_ENV = "OPENCAP_UI_SHOTS"


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
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set, but Node.js with the playwright package is not installed")
        pytest.skip("Node.js with the playwright package is not installed")
    poller = ConfigPoller(make_config(tmp_path))
    poller.identities["SITE MODBUS/Device1"] = {"vendor": "Example Vendor", "revision": "1.2"}
    poller.states["SITE M800/8 PUMP"] = "demoted"
    app = create_app(poller, api_cfg(tmp_path), environ=ENV)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # the Quick Client must see FLOW_1 change without a reload: a poll every 300 ms, as it were
    stop = threading.Event()

    def polling() -> None:
        n = 0
        while not stop.wait(0.3):
            n += 1
            poller.set_tag("SITE MODBUS/Device1", "FLOW_1", 100.0 + n, source_ts=datetime.now(UTC).isoformat())

    ticker = threading.Thread(target=polling, daemon=True)
    ticker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        args = ["node", str(SCRIPT), str(port), ENV["T_ADMIN"], ENV["T_VIEW"], str(SAMPLE)]
        if os.environ.get(SHOTS_ENV):
            Path(os.environ[SHOTS_ENV]).mkdir(parents=True, exist_ok=True)
            args.append(os.environ[SHOTS_ENV])
        result = subprocess.run(args, env=node_env, capture_output=True, text=True, timeout=240)
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
        assert "all checks passed" in result.stdout
    finally:
        stop.set()
        ticker.join(5)
        server.should_exit = True
        thread.join(10)
