"""Poller runtime: wires config, registry, channel workers, cache, publisher, MQTT and the API.

PLAN.md sections 3, 8 (reload) and 9.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from collections.abc import Callable
from concurrent.futures import Executor
from pathlib import Path
from typing import Any

import structlog

from opencap import FIRM_VERSION
from opencap.config.loader import LoadedConfig, load_config
from opencap.config.models import Channel, Device
from opencap.core.cache import TagValue, ValueCache
from opencap.core.channel_worker import ChannelWorker
from opencap.core.registry import Registry
from opencap.drivers.base import Driver
from opencap.north.mqtt import MqttLink
from opencap.north.opcua_server import OpcUaNorth
from opencap.north.publisher import Publisher
from opencap.north.saf import StoreAndForward

log = structlog.get_logger("opencap.runtime")

DriverFactory = Callable[[Device, Executor], Driver]


def event_loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Loop for `opencap run`: aiomqtt (paho) needs `add_reader`, which the default Windows
    ProactorEventLoop lacks, so Windows gets a SelectorEventLoop. None = asyncio's default."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def tag_value_json(tv: TagValue) -> dict[str, Any]:
    return {
        "value": tv.value,
        "quality": tv.quality,
        "source_ts": tv.source_ts.isoformat() if tv.source_ts else None,
    }


class Poller:
    """The whole process. `start()` → running; `stop()` → everything closed."""

    def __init__(
        self,
        config_dir: Path,
        *,
        driver_factory: DriverFactory | None = None,
        serve_api: bool = True,
        loaded: LoadedConfig | None = None,
        only_channels: list[str] | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self._driver_factory = driver_factory
        self._serve_api = serve_api
        self._only = set(only_channels) if only_channels else None   # `opencap run --site`: poll only these
        self.loaded: LoadedConfig = loaded or load_config(self.config_dir)
        self.cache = ValueCache()
        self.registry: Registry | None = None
        self.workers: dict[str, ChannelWorker] = {}
        self._worker_tasks: dict[str, asyncio.Task] = {}
        self._channel_cfg: dict[str, str] = {}   # channel name -> JSON of its config (change detection)
        self.saf: StoreAndForward | None = None
        self.link: MqttLink | None = None
        self.publisher: Publisher | None = None
        self._link_task: asyncio.Task | None = None
        self._pub_task: asyncio.Task | None = None
        self._api_server: Any = None
        self._api_task: asyncio.Task | None = None
        self.opcua: OpcUaNorth | None = None       # northbound OPC UA server (poller.yaml opcua_server)
        self._opcua_cfg: str | None = None
        self._opcua_error: str | None = None      # why the enabled server is not running (start failed)
        self._stop_event = asyncio.Event()
        self._reload_lock = asyncio.Lock()
        self._started_at: float | None = None      # monotonic, end of start(): north servers are up
        self._ready_ms: int | None = None          # initial scan duration; set once, never cleared
        self._t0 = time.monotonic()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        cfg = self.loaded.poller
        saf_path = Path(cfg.saf.path)          # relative paths resolve from the working directory
        saf_path.parent.mkdir(parents=True, exist_ok=True)
        self.saf = StoreAndForward(saf_path, cfg.saf.max_bytes)
        self.link = MqttLink(cfg.mqtt, cfg.instance, self.saf)
        self._link_task = asyncio.create_task(self.link.run(), name="mqtt-link")
        self._apply_channels(self.loaded.channels)
        self._restart_publisher()
        try:
            await self._start_opcua()
        except Exception as exc:     # as on reload: keep polling and publishing without it
            self._opcua_error = str(exc)
            log.error("opcua_server_start_failed", error=str(exc))
        if self._serve_api:
            await self._start_api()
        self._started_at = time.monotonic()
        self._check_ready()
        log.info("poller_started", version=FIRM_VERSION, channels=len(self.workers),
                 devices=sum(len(c.devices) for c in self._active_channels()))

    def _signal_handlers(self) -> list[tuple[int, Callable[[], Any]]]:
        handlers: list[tuple[int, Callable[[], Any]]] = [
            (signal.SIGTERM, self.request_stop), (signal.SIGINT, self.request_stop)]
        sighup = getattr(signal, "SIGHUP", None)        # absent on Windows: use POST /reload there
        if sighup is not None:
            handlers.append((sighup, lambda: asyncio.create_task(self.reload())))
        return handlers

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        for sig, handler in self._signal_handlers():
            try:
                loop.add_signal_handler(sig, handler)
            except (NotImplementedError, RuntimeError):  # non-Unix or not the main thread
                pass
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def stop(self) -> None:
        await self._stop_opcua()
        if self._api_server is not None:
            self._api_server.should_exit = True
        if self._api_task is not None:
            await asyncio.gather(self._api_task, return_exceptions=True)
        await self._stop_publisher()
        for name in list(self.workers):
            await self._stop_worker(name)
        if self.link is not None:
            await self.link.stop()
        if self._link_task is not None:
            self._link_task.cancel()
            await asyncio.gather(self._link_task, return_exceptions=True)
        if self.saf is not None:
            self.saf.close()
        log.info("poller_stopped")

    # ------------------------------------------------------------------ channels
    def _selected(self, channels: list[Channel]) -> list[Channel]:
        return [c for c in channels if c.enabled and (self._only is None or c.name in self._only)]

    def _active_channels(self) -> list[Channel]:
        return self._selected(self.loaded.channels)

    def _apply_channels(self, channels: list[Channel]) -> dict[str, list[str]]:
        """Start, stop or restart workers so they match `channels`. Returns what changed."""
        wanted = {c.name: c for c in self._selected(channels)}
        new_cfg = {name: ch.model_dump_json() for name, ch in wanted.items()}
        changed = {"started": [], "restarted": [], "stopped": [], "unchanged": []}
        self.registry = Registry.build(list(wanted.values()))
        for name in list(self.workers):
            if name not in wanted:
                asyncio.create_task(self._stop_worker(name))
                changed["stopped"].append(name)
        for name, ch in wanted.items():
            if name in self.workers and self._channel_cfg.get(name) == new_cfg[name]:
                changed["unchanged"].append(name)
                continue
            if name in self.workers:
                old = self.workers.pop(name)
                task = self._worker_tasks.pop(name)
                asyncio.create_task(self._stop_old(old, task))
                changed["restarted"].append(name)
            else:
                changed["started"].append(name)
            for dev in ch.devices:
                self.cache.register(dev)
            plans = {d.id: self.registry.plans[d.id] for d in ch.devices}
            worker = ChannelWorker(ch, plans, self.cache, on_cycle=self._on_cycle,
                                   driver_factory=self._driver_factory,
                                   stats_interval_s=self.loaded.poller.stats_interval_s)
            self.workers[name] = worker
            self._worker_tasks[name] = asyncio.create_task(worker.run(), name=f"channel:{name}")
        self._channel_cfg = new_cfg
        return changed

    async def _stop_old(self, worker: ChannelWorker, task: asyncio.Task) -> None:
        await worker.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _stop_worker(self, name: str) -> None:
        worker = self.workers.pop(name, None)
        task = self._worker_tasks.pop(name, None)
        if worker is not None and task is not None:
            await self._stop_old(worker, task)
        self._channel_cfg.pop(name, None)

    def _on_cycle(self, device_id: str) -> None:
        self._check_ready()
        if self.publisher is not None:
            self.publisher.notify_cycle(device_id)
        if self.opcua is not None:
            self.opcua.notify(device_id)

    # ------------------------------------------------------------------ publisher
    def _restart_publisher(self) -> None:
        devices = [d for c in self._active_channels() for d in c.devices if d.enabled]
        self.publisher = Publisher(devices, self.cache, self.link, self.loaded.poller.payload,
                                   status_fn=self.device_health_status,
                                   topic_prefix=self.loaded.poller.mqtt.topic_prefix)
        self._pub_task = asyncio.create_task(self.publisher.run(), name="publisher")

    async def _stop_publisher(self) -> None:
        if self._pub_task is not None:
            self._pub_task.cancel()
            await asyncio.gather(self._pub_task, return_exceptions=True)
            self._pub_task = None

    # ------------------------------------------------------------------ reload
    async def reload(self) -> dict[str, list[str]]:
        """SIGHUP / POST /reload: re-read config, restart only the channels that changed."""
        async with self._reload_lock:
            try:
                loaded = load_config(self.config_dir)
            except Exception as exc:  # keep running on the old config
                log.error("reload_failed", error=str(exc))
                raise
            if loaded.poller.mqtt != self.loaded.poller.mqtt:
                # The link (connection, LWT topic) is built once at start; data and status
                # topics follow the new mqtt.topic_prefix now, everything else at restart.
                log.warning("mqtt_settings_changed", detail="restart opencap to apply mqtt connection/LWT settings")
            self.loaded = loaded
            changed = self._apply_channels(loaded.channels)
            await self._stop_publisher()
            self._restart_publisher()
            await self._reload_opcua()
            log.info("config_reloaded", **{k: len(v) for k, v in changed.items()})
            return changed

    # ------------------------------------------------------------------ OPC UA server (north)
    def _served_devices(self) -> list[Device]:
        """Devices exposed northbound: the same set the publisher publishes."""
        return [d for c in self._active_channels() for d in c.devices if d.enabled]

    async def _start_opcua(self) -> None:
        cfg = self.loaded.poller.opcua_server
        self._opcua_cfg = cfg.model_dump_json()
        self._opcua_error = None
        if not cfg.enabled:
            return
        north = OpcUaNorth(cfg, self._served_devices(), self.cache, self.device_status)
        await north.start()
        self.opcua = north

    async def _stop_opcua(self) -> None:
        north, self.opcua = self.opcua, None
        if north is not None:
            await north.stop()

    async def _reload_opcua(self) -> None:
        """Server settings changed -> restart it; otherwise only sync its devices."""
        if self.loaded.poller.opcua_server.model_dump_json() != self._opcua_cfg:
            await self._stop_opcua()
            try:
                await self._start_opcua()
            except Exception as exc:     # keep polling and publishing without it
                self._opcua_error = str(exc)
                log.error("opcua_server_start_failed", error=str(exc))
            return
        if self.opcua is not None:
            await self.opcua.set_devices(self._served_devices())

    # ------------------------------------------------------------------ queries (API / CLI)
    def _worker_for(self, device_id: str) -> ChannelWorker:
        channel = device_id.split("/", 1)[0]
        worker = self.workers.get(channel)
        if worker is None or self.registry is None or device_id not in self.registry.plans:
            raise KeyError(device_id)
        return worker

    def device_health_status(self, device_id: str) -> dict[str, Any]:
        return self._worker_for(device_id).health(device_id).status()

    def device_status(self, device_id: str) -> dict[str, Any]:
        """Health plus cycle_ms, last_error, connected (OPC UA `_System` diagnostics)."""
        try:
            return self._worker_for(device_id).device_status(device_id)
        except KeyError:
            return {"state": "offline", "last_good": None, "errors": 0, "cycle_ms": None,
                    "last_error": "not polled", "connected": False}

    def device_detail(self, device_id: str) -> dict[str, Any]:
        worker = self._worker_for(device_id)
        dp = self.registry.plans[device_id]
        dev = dp.device
        return {
            "id": dev.id, "channel": dev.channel, "driver": dev.driver, "host": dev.host, "port": dev.port,
            "enabled": dev.enabled, "tags": len(dev.tags), "blocks": dp.blocks,
            "bad_config": sorted(dp.invalid), "plan_error": dp.error,
            "topic": dev.publish.topic, "sub_device_id": dev.publish.sub_device_id,
            **worker.device_status(device_id),
        }

    def device_values(self, device_id: str) -> dict[str, Any]:
        self._worker_for(device_id)
        return {name: tag_value_json(tv) for name, tv in self.cache.snapshot(device_id).items()}

    def live_version(self, device_id: str) -> int | None:
        """Change counter of the device's values, for the live feed `GET /api/v1/ws` (#31): it
        grows with every cache update or expiry. None = the device is not polled."""
        try:
            self._worker_for(device_id)
        except KeyError:
            return None
        return self.cache.version(device_id)

    async def read_once(self, device_id: str) -> dict[str, Any]:
        values = await self._worker_for(device_id).read_once(device_id)
        return {name: tag_value_json(tv) for name, tv in values.items()}

    def device_plan(self, device_id: str) -> dict[str, Any]:
        """Block plan of the running config (`GET /devices/{id}/plan`)."""
        from opencap.core.plan_report import device_plan_json

        self._worker_for(device_id)
        return device_plan_json(self.registry.plans[device_id], self.loaded.poller.check.est_rtt_ms)

    async def device_identity(self, device_id: str) -> dict[str, Any]:
        """`{"connected": bool, "identity": {...}}` over the device's existing connection."""
        identity = await self._worker_for(device_id).identity(device_id)
        return {"connected": identity is not None, "identity": identity or {}}

    def channels_summary(self) -> list[dict[str, Any]]:
        out = []
        for ch in self._active_channels():
            worker = self.workers.get(ch.name)
            devices = []
            for dev in ch.devices:
                st = worker.device_status(dev.id) if worker else {"state": "offline"}
                devices.append({"id": dev.id, "driver": dev.driver, "state": st.get("state"),
                                "last_good": st.get("last_good"), "errors": st.get("errors"),
                                "cycle_ms": st.get("cycle_ms")})
            out.append({"name": ch.name, "devices": devices})
        return out

    def waiting_devices(self) -> list[str]:
        """Enabled, polled devices that have not finished one cycle and are not demoted."""
        out = []
        for ch in self._active_channels():
            worker = self.workers.get(ch.name)
            for dev in ch.devices:
                if worker is not None and worker.awaiting_first_cycle(dev.id):
                    out.append(dev.id)
        return out

    def _check_ready(self) -> None:
        """Latch the initial scan: north servers started and nothing waiting (D-075)."""
        if self._ready_ms is None and self._started_at is not None and not self.waiting_devices():
            self._ready_ms = round((time.monotonic() - self._t0) * 1000)

    def readiness(self) -> dict[str, Any]:
        """`/health/ready`: ready once the config is loaded, the north servers are started and
        every enabled device has finished a cycle (or is offline after one, or demoted). Sticky."""
        self._check_ready()
        waiting = [] if self._ready_ms is not None else self.waiting_devices()
        if self._started_at is None:
            return {"ready": False, "waiting": waiting, "reason": "starting"}
        return {"ready": self._ready_ms is not None, "waiting": waiting}

    def health(self) -> dict[str, Any]:
        states: dict[str, int] = {"ok": 0, "offline": 0, "demoted": 0}
        for ch in self.channels_summary():
            for d in ch["devices"]:
                states[d["state"] or "offline"] = states.get(d["state"] or "offline", 0) + 1
        return {
            "status": "ok",
            "version": FIRM_VERSION,
            "instance": self.loaded.poller.instance,
            "mqtt_connected": bool(self.link and self.link.connected),
            "saf_queue": self.saf.count() if self.saf else 0,
            "channels": len(self.workers),
            "devices": states,
            "initial_scan_complete": self._ready_ms is not None,
            "initial_scan_ms": self._ready_ms,
            **self._opcua_health(),
        }

    def _opcua_health(self) -> dict[str, Any]:
        """`opcua_server` in /health, only when it is enabled (so the default answer is unchanged)."""
        if not self.loaded.poller.opcua_server.enabled:
            return {}
        return {"opcua_server": {"running": self.opcua is not None, "error": self._opcua_error}}

    # ------------------------------------------------------------------ API
    async def _start_api(self) -> None:
        import uvicorn

        from opencap.api.live import UVICORN_WS
        from opencap.api.server import create_app

        api_cfg = self.loaded.poller.api
        app = create_app(self, api_cfg)
        config = uvicorn.Config(app, host=api_cfg.bind, port=api_cfg.port, log_config=None,
                                access_log=False, lifespan="off", **UVICORN_WS)
        self._api_server = uvicorn.Server(config)
        self._api_task = asyncio.create_task(self._api_server.serve(), name="api")
