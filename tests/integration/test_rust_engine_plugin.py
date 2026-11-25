"""Rust engine with `plugin: python` devices (#49): the `ab_pccc` driver, which has no Rust port,
runs in the Python driver host the engine starts and supervises (proto/README.md).

- the device polls through the host and its values match the PCCC simulator;
- killing the host process mid-run turns the device bad_comm (last values kept), and it recovers
  on its own once the engine has restarted the host;
- `check`, `read` and `identity` work through the host;
- the added latency of a read through the host is measured (`plugin_read_latency` example).

Needs the Rust binary (OPENCAP_ENGINE_BIN or engine/target/*) and `opencap[plugin]`.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("grpc")
pccc_sim = pytest.importorskip("tests.helpers.pccc_sim")

from opencap.config.models import Device, Tag  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.engine import ROOT, RustEngine, rust_binary  # noqa: E402
from tests.helpers.modbus_sim import free_port  # noqa: E402
from tests.integration.test_ab_pccc_sim import SYSTEM_FILES, TAGS, expected  # noqa: E402

BIN = rust_binary()
pytestmark = pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine binary not built")
PLUGINS = {"python": {"command": [sys.executable, "-m", "opencap.plugin_host"]}}
API_TOKEN = "plugin-test-token"


def plugin_device(sim, **kw) -> Device:  # noqa: ANN001
    return Device(id="SLC_SITE/PLC", channel="SLC_SITE", name="PLC", driver="ab_pccc", plugin="python",
                  host=sim.host, port=sim.port, tags=TAGS, scan_ms=200, min_scan_ms=200, timeout_ms=1000,
                  retries=0, demote_after=50, demote_for_ms=1000, **kw)


def plugin_pids(engine_pid: int) -> list[int]:
    """Plugin host processes started by the engine (Linux /proc)."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        if ppid == engine_pid and b"opencap.plugin_host" in cmdline:
            out.append(int(entry.name))
    return out


async def _wait(pred, timeout: float, what: str) -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, f"timed out after {timeout}s waiting for {what}"
        await asyncio.sleep(0.1)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="finds the plugin process through /proc")
async def test_polls_through_the_host_and_recovers_when_it_is_killed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCAP_API_TOKEN", API_TOKEN)
    sim = await pccc_sim.PcccSim.start({**SYSTEM_FILES, **pccc_sim.tables_for(TAGS, seed=3)})
    dev = plugin_device(sim)
    config = write_config(tmp_path, [dev], mqtt_port=free_port(), api_port=free_port(), poller={"plugins": PLUGINS})
    engine = RustEngine(config, BIN, tmp_path / "engine")
    await engine.start()
    try:
        def values() -> dict:
            return engine.device_values(dev.id)

        def all_good() -> bool:
            v = values()
            return len(v) == len(TAGS) and all(tv["quality"] == "good" for tv in v.values())

        await _wait(all_good, 30, "every tag good through the plugin host")
        got = values()
        for tag in TAGS:
            want = expected(sim, tag)
            assert got[tag.name]["value"] == pytest.approx(want), tag.name
        detail = engine.device_detail(dev.id)
        assert detail["driver"] == "ab_pccc" and detail["plan_error"] is None and detail["state"] == "ok"
        pids = plugin_pids(engine.proc.pid)
        assert len(pids) == 1, pids

        sim.set("N7:2", 4321)
        await _wait(lambda: values()["signed"]["value"] == 4321, 10, "a new value through the host")

        os.kill(pids[0], signal.SIGKILL)                      # the plugin dies mid-run
        await _wait(lambda: values()["flow"]["quality"] == "bad_comm", 10, "bad_comm after the kill")
        assert values()["flow"]["value"] == pytest.approx(expected(sim, TAGS[0])), "last value kept"
        await _wait(all_good, 30, "recovered after the host restart")
        new = plugin_pids(engine.proc.pid)
        assert len(new) == 1 and new != pids
        sim.set("N7:2", 77)
        await _wait(lambda: values()["signed"]["value"] == 77, 10, "fresh values after the restart")
        assert "plugin.lost" in engine.log_path.read_text()
    finally:
        await engine.stop()
        await sim.stop()
    await _wait(lambda: not plugin_pids(os.getpid()), 5, "no plugin left behind")


async def test_check_read_and_identity_through_the_host(tmp_path) -> None:
    sim = await pccc_sim.PcccSim.start({**SYSTEM_FILES, **pccc_sim.tables_for(TAGS, seed=3)})
    try:
        dev = plugin_device(sim)
        config = write_config(tmp_path, [dev], poller={"plugins": PLUGINS})

        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([str(BIN), *args, "--config", str(config)], capture_output=True, text=True,
                                  timeout=120)

        check = await asyncio.to_thread(run, "check", "-v")
        assert check.returncode == 0, check.stderr
        assert "F8:0{2}" in check.stdout and "SLC_SITE" in check.stdout, check.stdout
        read = await asyncio.to_thread(run, "read", dev.id)
        assert read.returncode == 0, read.stdout + read.stderr
        lines = {line.split()[0]: line.split() for line in read.stdout.splitlines() if line.strip()}
        assert lines["signed"][1] == "good" and int(lines["signed"][2]) == expected(sim, TAGS[4])
        ident = await asyncio.to_thread(run, "identity", dev.id)
        assert ident.returncode == 0, ident.stderr
        assert "product_name" in json.loads(ident.stdout)

        broken = write_config(tmp_path / "broken", [plugin_device(sim, params={"path": "1,0,2"})],
                              poller={"plugins": PLUGINS})
        bad = await asyncio.to_thread(subprocess.run, [str(BIN), "read", dev.id, "--config", str(broken)],
                                      capture_output=True, text=True, timeout=120)
        assert bad.returncode == 1 and "port/link pairs" in bad.stderr, bad.stderr
    finally:
        await sim.stop()


def latency_example() -> Path | None:
    """The newest build of the example (a stale one from before a config change would fail)."""
    name = "plugin_read_latency.exe" if os.name == "nt" else "plugin_read_latency"
    found = [p for p in (ROOT / "engine" / "target" / prof / "examples" / name for prof in ("release", "debug"))
             if p.exists()]
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


@pytest.mark.skipif(latency_example() is None, reason="cargo build -p opencap-plugin --example plugin_read_latency")
async def test_added_latency_per_read(tmp_path) -> None:
    """Round trip of a Read through the host minus the driver's own time in the host. The target
    is < 0.5 ms p99 on loopback (#49); CI machines are shared, so the gate here is 5 ms and the
    measured numbers are printed (and recorded in docs/architecture/04-scale-reliability.md)."""
    sim = await pccc_sim.PcccSim.start({**SYSTEM_FILES, **pccc_sim.tables_for(TAGS, seed=3)})
    try:
        dev = plugin_device(sim)
        config = write_config(tmp_path, [dev], poller={"plugins": PLUGINS})
        out = await asyncio.to_thread(subprocess.run, [str(latency_example()), str(config), dev.id, "300"],
                                      capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, out.stdout + out.stderr
        stats = json.loads(out.stdout.strip().splitlines()[-1])
        print(f"\n[plugin latency] {stats}")
        assert stats["reads"] >= 300
        assert stats["overhead_ms"]["p99"] < 5.0, stats
    finally:
        await sim.stop()
