"""Smoke test of the Rust engine's native Windows service (#57). CI only; needs an elevated prompt.

    python deploy/windows/engine_service_smoke.py --engine engine\\target\\release\\opencap-engine.exe

Run from the repository root with opencap and its test helpers installed (`pip install -e '.[dev]'`).

1. Starts a Modbus TCP simulator and the MQTT test broker from `tests/helpers` on 127.0.0.1, and
   writes a config directory for them.
2. `opencap-engine service install --config <dir>`, then checks what the service control
   manager registered: automatic start, the `service run --config` command line, and the
   recovery actions (restart on failure).
3. `sc start`, then waits for `/health`: MQTT connected and the device polled (`ok`).
4. `sc stop`, then waits for STOPPED and checks for a clean stop: exit code 0 in `sc query`,
   `poller_stopped` in `%ProgramData%\\opencap\\logs\\opencap-engine.log`, and the service's
   "started" and "stopped" entries in the Application event log (source `opencap-engine`).
5. `opencap-engine service uninstall`, always; then checks the service is gone. On failure it
   prints the engine log.

Nothing here touches a real device: the simulator and the broker are local and ephemeral.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.helpers.broker import TestBroker  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.modbus_sim import free_port, make_sim_devices, start_sims, stop_sims  # noqa: E402

SERVICE = "opencap-engine"
PROGRAM_DATA = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "opencap"
LOG = PROGRAM_DATA / "logs" / "opencap-engine.log"


def log(msg: str) -> None:
    print(f"[engine-service-smoke] {msg}", flush=True)


def run(*args: str, check: bool = True) -> str:
    done = subprocess.run(list(args), capture_output=True, text=True, timeout=120)
    out = (done.stdout + done.stderr).strip()
    log(f"{' '.join(args[:3])} -> exit {done.returncode}: {out[:600]}")
    if check and done.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed ({done.returncode})")
    return out


def sc_state(name: str = SERVICE) -> tuple[str, str]:
    """(STATE, WIN32_EXIT_CODE) from `sc query`, ("MISSING", "") if the service does not exist."""
    out = subprocess.run(["sc", "query", name], capture_output=True, text=True, timeout=30).stdout
    state = exit_code = ""
    for line in out.splitlines():
        key, _, value = line.strip().partition(":")
        if key.strip() == "STATE":
            state = value.split()[-1]
        elif key.strip() == "WIN32_EXIT_CODE":
            exit_code = value.split()[0]
    return (state or "MISSING", exit_code)


def get_health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 - not up yet
        return None


async def wait_for(what: str, probe, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        await asyncio.sleep(0.5)
    raise SystemExit(f"timed out after {timeout}s waiting for {what}")


def events(since: float) -> list[str]:
    ps = ("Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='opencap-engine'} "
          "-MaxEvents 20 -ErrorAction SilentlyContinue | ForEach-Object { $_.TimeCreated.ToUniversalTime()"
          ".ToString('o') + ' ' + $_.Message }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         timeout=60).stdout
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since - 5))
    return [line for line in out.splitlines() if line[:19] >= cutoff]


async def main(engine: Path) -> None:
    started_at = time.time()
    sims = await start_sims(make_sim_devices(1, 5))
    async with TestBroker() as broker:
        api_port = free_port()
        tmp = Path(tempfile.mkdtemp(prefix="opencap-engine-svc-"))
        config_dir = write_config(tmp, [s.device for s in sims], mqtt_port=broker.port, api_port=api_port,
                                  poller={"log_level": "INFO"})
        log(f"config {config_dir}, broker :{broker.port}, simulator :{sims[0].port}, API :{api_port}")
        installed = False
        try:
            run(str(engine), "service", "install", "--config", str(config_dir))
            installed = True
            qc = run("sc", "qc", SERVICE)
            assert "AUTO_START" in qc, qc
            assert f'service run --config {config_dir}' in qc.replace('"', ""), qc
            failure = run("sc", "qfailure", SERVICE)
            assert failure.upper().count("RESTART") >= 3, failure

            run("sc", "start", SERVICE)
            health = await wait_for(
                "/health with MQTT connected and the device ok",
                lambda: (h := get_health(api_port)) and h.get("mqtt_connected") and h["devices"]["ok"] == 1 and h,
                90)
            log(f"/health: {health}")
            assert sc_state()[0] == "RUNNING", sc_state()

            run("sc", "stop", SERVICE)
            await wait_for("STOPPED", lambda: sc_state()[0] == "STOPPED", 60)
            state, exit_code = sc_state()
            log(f"sc query: {state}, WIN32_EXIT_CODE {exit_code}")
            assert exit_code == "0", f"the service stopped with exit code {exit_code}"
            text = LOG.read_text(encoding="utf-8", errors="replace")
            stopped = [json.loads(line) for line in text.splitlines() if '"poller_stopped"' in line]
            assert stopped, f"no poller_stopped in {LOG}"
            log(f"poller_stopped: {stopped[-1]}")
            found = events(started_at)
            log(f"event log: {found}")
            assert any("opencap-engine started" in e for e in found), "no 'started' event"
            assert any(e.rstrip().endswith("opencap-engine stopped") for e in found), "no 'stopped' event"
            log("OK: installed, started, healthy, stopped cleanly (exit 0, poller_stopped, events)")
        except BaseException:
            if LOG.exists():
                log(f"engine log:\n{LOG.read_text(encoding='utf-8', errors='replace')[-4000:]}")
            raise
        finally:
            if installed:
                run(str(engine), "service", "uninstall", check=False)
                assert sc_state()[0] == "MISSING", sc_state()
            await stop_sims(sims)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.engine.resolve()))
