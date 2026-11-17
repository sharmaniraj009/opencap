"""Engine adapters for the conformance suite: the same acceptance test, simulators, broker and
assertions drive either the Python reference `Poller` or the Rust engine binary (`engine/`).

    pytest -m acceptance tests/integration/test_phase1_acceptance.py --engine=rust
    OPENCAP_ENGINE=rust pytest -m acceptance ...          # same, via the environment
    OPENCAP_ENGINE_BIN=/path/to/opencap-engine           # binary to run (default: engine/target/{release,debug})

Both adapters expose the part of `opencap.core.runtime.Poller` the acceptance test uses:

    engine = factory(config_dir); await engine.start(); ...; await engine.stop()
    engine.sent                    # {topic: [payload bytes]} handed by the publisher to the link
    engine.loaded.channels         # the config as loaded (same files, Python loader)
    engine.health(), engine.device_detail(id), engine.device_values(id)
    engine.link.connected, engine.saf.count()

The Rust adapter answers these from the engine's admin API (`/health`, `/devices/...`) and
from a publish tap: with `OPENCAP_PUBLISH_TAP=<file>` the engine appends every message the
publisher hands to the MQTT link to that file (JSON lines) BEFORE it enters the outbox, so
`sent` is always a superset of what can reach the broker, as with the Python spy.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from opencap.config.loader import load_config

ROOT = Path(__file__).resolve().parents[2]
ENGINES = ("python", "rust")


def rust_binary() -> Path | None:
    """The Rust engine binary: $OPENCAP_ENGINE_BIN, else engine/target/release, else debug."""
    env = os.environ.get("OPENCAP_ENGINE_BIN")
    if env:
        return Path(env).resolve()          # the engine runs in its own working directory
    for profile in ("release", "debug"):
        exe = ROOT / "engine" / "target" / profile / ("opencap-engine.exe" if os.name == "nt" else "opencap-engine")
        if exe.exists():
            return exe
    return None


class PythonEngine:
    """The reference implementation, in-process (exactly what the test did before)."""

    def __init__(self, config_dir: Path) -> None:
        from opencap.core.runtime import Poller

        self.poller = Poller(config_dir)
        self.sent: dict[str, list[bytes]] = defaultdict(list)

    async def start(self) -> None:
        await self.poller.start()
        # Spy on everything the publisher hands to the link (right after start, before any data).
        real_publish = self.poller.link.publish

        async def spy(topic: str, payload: bytes, *, retain: bool = False) -> None:
            self.sent[topic].append(payload)
            await real_publish(topic, payload, retain=retain)

        self.poller.link.publish = spy

    async def stop(self) -> None:
        await self.poller.stop()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.poller, name)


class TapLog(Mapping):
    """{topic: [payload, ...]} read from the engine's publish tap; re-read on every access, so a
    message is visible here as soon as the engine has handed it to its link."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._data: dict[str, list[bytes]] = defaultdict(list)

    def _refresh(self) -> None:
        try:
            with self._path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except FileNotFoundError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        for line in chunk[:end].split(b"\n"):
            if line:
                rec = json.loads(line)
                self._data[rec["topic"]].append(base64.b64decode(rec["payload_b64"]))
        self._offset += end + 1

    def __getitem__(self, topic: str) -> list[bytes]:
        self._refresh()
        return list(self._data.get(topic, []))

    def __iter__(self) -> Iterator[str]:
        self._refresh()
        return iter(list(self._data))

    def __len__(self) -> int:
        self._refresh()
        return len(self._data)


class _Link:
    def __init__(self, engine: RustEngine) -> None:
        self._e = engine

    @property
    def connected(self) -> bool:
        return bool(self._e.health()["mqtt_connected"])


class _Saf:
    def __init__(self, engine: RustEngine) -> None:
        self._e = engine

    def count(self) -> int:
        return int(self._e.health()["saf_queue"])


class RustEngine:
    """The Rust engine as a child process, driven through its admin API."""

    def __init__(self, config_dir: Path, binary: Path, workdir: Path) -> None:
        self.config_dir = Path(config_dir).resolve()
        self.binary = Path(binary).resolve()
        self.workdir = workdir
        self.loaded = load_config(self.config_dir)
        api = self.loaded.poller.api
        self.base_url = f"http://{api.bind}:{api.port}"
        token = os.environ.get(api.token_env)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.tap_path = workdir / "publish_tap.jsonl"
        self.log_path = workdir / "engine.log"
        self.sent = TapLog(self.tap_path)
        self.link = _Link(self)
        self.saf = _Saf(self)
        self.proc: asyncio.subprocess.Process | None = None
        self._http: httpx.Client | None = None
        self._log = None

    async def start(self, timeout: float = 30.0) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.tap_path.unlink(missing_ok=True)
        env = {**os.environ, "OPENCAP_PUBLISH_TAP": str(self.tap_path)}
        self._log = self.log_path.open("wb")
        self.proc = await asyncio.create_subprocess_exec(
            str(self.binary), "run", "--config", str(self.config_dir),
            stdout=self._log, stderr=asyncio.subprocess.STDOUT, env=env, cwd=str(self.workdir))
        self._http = httpx.Client(base_url=self.base_url, timeout=10, headers=self._headers)
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(base_url=self.base_url, timeout=2, headers=self._headers) as http:
            while True:
                if self.proc.returncode is not None:
                    raise RuntimeError(f"opencap-engine exited with {self.proc.returncode}:\n{self.log_tail()}")
                with contextlib.suppress(httpx.HTTPError):
                    if (await http.get("/health")).status_code == 200:
                        return
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError(f"opencap-engine API not up after {timeout}s:\n{self.log_tail()}")
                await asyncio.sleep(0.05)

    async def stop(self, timeout: float = 15.0) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"opencap-engine did not stop within {timeout}s:\n{self.log_tail()}") from None
        if self._log is not None:
            self._log.close()
            self._log = None

    def log_tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no engine log)"

    # --- the Poller query surface, over HTTP ----------------------------------------------
    def _get(self, path: str) -> Any:
        assert self._http is not None, "engine not started"
        r = self._http.get(path)
        if r.status_code == 404:
            raise KeyError(path)
        r.raise_for_status()
        return r.json()

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def device_detail(self, device_id: str) -> dict[str, Any]:
        return self._get(f"/devices/{device_id}")

    def device_values(self, device_id: str) -> dict[str, Any]:
        return self._get(f"/devices/{device_id}/values")


def make_engine_factory(kind: str, workdir: Path) -> Callable[[Path], Any]:
    """`factory(config_dir) -> engine` for `--engine=python|rust`."""
    if kind == "python":
        return PythonEngine
    if kind != "rust":
        raise ValueError(f"unknown engine {kind!r}; expected one of {ENGINES}")
    binary = rust_binary()
    if binary is None or not binary.exists():
        raise FileNotFoundError(
            "Rust engine binary not found: build it with `cd engine && cargo build --release` "
            "or point OPENCAP_ENGINE_BIN at it")
    return lambda config_dir: RustEngine(config_dir, binary, workdir / "rust-engine")
