"""Smoke test of the WinSW service definition on Windows (#35, D-031). CI only; needs an elevated prompt.

    python deploy/windows/service_smoke.py --winsw path\\to\\WinSW-x64.exe

Run from the repository root with opencap and its test helpers installed (`pip install -e '.[dev,all]'`).

1. Starts a Modbus TCP simulator and the MQTT test broker from `tests/helpers` on 127.0.0.1.
2. Writes a config directory for them, and a copy of `deploy/windows/opencap-service.xml` in which
   only the four paths change (this interpreter instead of the venv's python.exe, a temporary
   config, working and log directory). Everything else in the XML is used as shipped.
3. `install`, `start`, then waits for `/health`: MQTT connected and the device polled (`ok`).
4. `stop` (WinSW sends Ctrl+C and waits), then checks for a clean shutdown: `poller_stopped` in
   the service's stdout log, and WinSW's own record of the exit code, "Child process [...]
   terminated with 0", in the Application event log.
5. `uninstall`, always. On failure it prints the service logs.

Nothing here touches a real device: the simulator and the broker are local and ephemeral.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
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

SERVICE_ID = "opencap"
XML = ROOT / "deploy" / "windows" / "opencap-service.xml"
# The paths in the shipped XML, and what each becomes for the test.
SHIPPED = {
    "venv_python": r"<executable>C:\opencap\venv\Scripts\python.exe</executable>",
    "config": r'--config "C:\ProgramData\opencap\config"',
    "workdir": r"<workingdirectory>C:\ProgramData\opencap</workingdirectory>",
    "logs": r"<logpath>C:\ProgramData\opencap\logs</logpath>",
}


def log(msg: str) -> None:
    print(f"[service-smoke] {msg}", flush=True)


def service_xml(config_dir: Path, workdir: Path, logdir: Path) -> str:
    text = XML.read_text(encoding="utf-8")
    for key, shipped in SHIPPED.items():
        if text.count(shipped) != 1:
            raise SystemExit(f"{XML.name}: expected {shipped!r} exactly once; update {Path(__file__).name}")
    return (text
            .replace(SHIPPED["venv_python"], f"<executable>{sys.executable}</executable>")
            .replace(SHIPPED["config"], f'--config "{config_dir}"')
            .replace(SHIPPED["workdir"], f"<workingdirectory>{workdir}</workingdirectory>")
            .replace(SHIPPED["logs"], f"<logpath>{logdir}</logpath>"))


def winsw(exe: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    log(f"{exe.name} {' '.join(args)} -> exit {r.returncode}: {out}")
    if check and r.returncode != 0:
        raise SystemExit(f"WinSW {' '.join(args)} failed with exit code {r.returncode}")
    return out


def get_health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return json.loads(resp.read())
    except OSError:
        return None


async def wait_for(what: str, probe, timeout: float):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await asyncio.to_thread(probe)
        if last:
            return last
        await asyncio.sleep(1)
    raise SystemExit(f"timed out after {timeout:.0f} s waiting for {what} (last: {last!r})")


def exit_events(since: float) -> list[str]:
    """WinSW's 'Child process [...] terminated|finished with N' entries since `since` (epoch s)."""
    ps = ("Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='" + SERVICE_ID + "'} "
          "-MaxEvents 50 -ErrorAction SilentlyContinue | "
          f"Where-Object {{ $_.TimeCreated -ge [DateTimeOffset]::FromUnixTimeSeconds({int(since) - 5}).LocalDateTime }} | "
          "ForEach-Object { $_.Message }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
    return [line.strip() for line in r.stdout.splitlines() if "Child process" in line]


def dump_logs(logdir: Path) -> None:
    for f in sorted(logdir.glob("*.log")):
        log(f"--- {f.name} (last 60 lines)")
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-60:]:
            print(line, flush=True)


async def main(winsw_exe: Path) -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows only: this installs a Windows service")
    work = Path(tempfile.mkdtemp(prefix="opencap-svc-"))
    service_dir, logdir = work / "service", work / "logs"
    service_dir.mkdir()
    logdir.mkdir()
    exe = service_dir / "opencap-service.exe"          # WinSW finds its XML by the executable's base name
    shutil.copy2(winsw_exe, exe)

    async with TestBroker() as broker:
        sims = await start_sims(make_sim_devices(1, 12, scan_ms=500, timeout_ms=1000))
        try:
            api_port = free_port()
            config_dir = write_config(work, [s.device for s in sims], mqtt_port=broker.port, api_port=api_port,
                                      poller={"log_level": "INFO"})
            (service_dir / "opencap-service.xml").write_text(service_xml(config_dir, work, logdir), encoding="utf-8")
            log(f"work dir {work}, broker :{broker.port}, simulator :{sims[0].port}, API :{api_port}")

            installed = False
            try:
                winsw(exe, "install")
                installed = True
                started_at = time.time()
                winsw(exe, "start")
                health = await wait_for(
                    "/health with MQTT connected and the device ok",
                    lambda: (h := get_health(api_port)) and h.get("mqtt_connected") and h["devices"].get("ok") == 1 and h,
                    timeout=90)
                log(f"/health: {health}")
                assert winsw(exe, "status") == "Started"

                winsw(exe, "stop")                       # Ctrl+C to python.exe, waits for Stopped
                assert winsw(exe, "status") == "Stopped"
                out_log = (logdir / f"{SERVICE_ID}.out.log").read_text(encoding="utf-8", errors="replace")
                stopped = [json.loads(line) for line in out_log.splitlines()
                           if line.startswith("{") and '"poller_stopped"' in line]
                if not stopped:
                    raise SystemExit("no poller_stopped event in the service's stdout log")
                log(f"poller_stopped: {stopped[-1]}")
                events = await wait_for("WinSW's child-process exit event", lambda: exit_events(started_at), timeout=30)
                log(f"exit events: {events}")
                if not any(e.endswith("terminated with 0") for e in events):
                    raise SystemExit(f"opencap did not exit cleanly with 0 on stop: {events}")
                log("OK: installed, started, healthy, stopped cleanly (poller_stopped, exit 0)")
            except BaseException:
                dump_logs(logdir)
                raise
            finally:
                if installed:
                    winsw(exe, "stop", check=False)
                    winsw(exe, "uninstall", check=False)
        finally:
            await stop_sims(sims)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--winsw", type=Path, required=True, help="WinSW v2 x64 executable")
    asyncio.run(main(ap.parse_args().winsw))
