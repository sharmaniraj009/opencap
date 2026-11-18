"""A `Poller` stand-in for API and UI tests: no sockets, no MQTT, real config.

It loads a real config directory (so the config API, `/check` and "pending reload" see
what a real poller would), builds the real block plans, and serves canned runtime state:
every enabled tag of a polled device reads `good` with value 1.0 unless `values` says
otherwise. `reload()` re-reads the config like `Poller.reload()` and reports what changed.
`set_tag()` changes one value and bumps the device's `live_version` (the WebSocket feed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opencap import FIRM_VERSION
from opencap.config.loader import load_config
from opencap.core.plan_report import device_plan_json
from opencap.core.registry import DevicePlan, Registry, plan_device

TS = "2026-11-17T10:00:00.000Z"


class ConfigPoller:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = Path(config_dir)
        self.loaded = load_config(self.config_dir)
        self._build()
        self.reloads = 0
        self.read_onces: list[str] = []
        self.values: dict[str, dict[str, dict[str, Any]]] = {}
        self.identities: dict[str, dict[str, Any]] = {}
        self.states: dict[str, str] = {}
        self.mqtt_connected = True
        self.saf_queue = 7
        self.live_versions: dict[str, int] = {}

    def _build(self) -> None:
        plans: dict[str, DevicePlan] = {}
        for ch in self.loaded.channels:
            for dev in ch.devices:
                try:
                    plans[dev.id] = plan_device(dev)
                except Exception as exc:  # noqa: BLE001
                    plans[dev.id] = DevicePlan(device=dev, error=str(exc))
        self.registry = Registry(channels=self.loaded.channels, plans=plans)

    def _active(self):
        return [c for c in self.loaded.channels if c.enabled]

    def _check(self, device_id: str) -> DevicePlan:
        for ch in self._active():
            for dev in ch.devices:
                if dev.id == device_id:
                    return self.registry.plans[device_id]
        raise KeyError(device_id)

    def health(self) -> dict[str, Any]:
        states = {"ok": 0, "offline": 0, "demoted": 0}
        for ch in self.channels_summary():
            for d in ch["devices"]:
                states[d["state"]] = states.get(d["state"], 0) + 1
        return {"status": "ok", "version": FIRM_VERSION, "instance": self.loaded.poller.instance,
                "mqtt_connected": self.mqtt_connected, "saf_queue": self.saf_queue,
                "channels": len(self._active()), "devices": states}

    def _status(self, dev_id: str) -> dict[str, Any]:
        dp = self.registry.plans[dev_id]
        state = self.states.get(dev_id, "ok" if dp.ok else "offline")
        return {"state": state, "last_good": TS if state == "ok" else None, "errors": 0 if state == "ok" else 4,
                "cycle_ms": 42.0 if state == "ok" else None,
                "last_error": dp.error if not dp.ok else None, "connected": state == "ok"}

    def channels_summary(self) -> list[dict[str, Any]]:
        out = []
        for ch in self._active():
            out.append({"name": ch.name, "devices": [
                {"id": d.id, "driver": d.driver, **{k: v for k, v in self._status(d.id).items()
                                                    if k in ("state", "last_good", "errors", "cycle_ms")}}
                for d in ch.devices]})
        return out

    def device_detail(self, device_id: str) -> dict[str, Any]:
        dp = self._check(device_id)
        dev = dp.device
        return {"id": dev.id, "channel": dev.channel, "driver": dev.driver, "host": dev.host, "port": dev.port,
                "enabled": dev.enabled, "tags": len(dev.tags), "blocks": dp.blocks,
                "bad_config": sorted(dp.invalid), "plan_error": dp.error,
                "topic": dev.publish.topic, "sub_device_id": dev.publish.sub_device_id,
                **self._status(device_id)}

    def device_values(self, device_id: str) -> dict[str, Any]:
        dp = self._check(device_id)
        if device_id in self.values:
            return self.values[device_id]
        out = {}
        for t in dp.device.tags:
            if not t.enabled:
                continue
            if t.name in dp.invalid:
                out[t.name] = {"value": None, "quality": "bad_config", "source_ts": None}
            else:
                out[t.name] = {"value": 1.0, "quality": "good", "source_ts": TS}
        return out

    def live_version(self, device_id: str) -> int | None:
        try:
            self._check(device_id)
        except KeyError:
            return None
        return self.live_versions.get(device_id, 1)

    def set_tag(self, device_id: str, tag: str, value: Any, quality: str = "good",
                source_ts: str | None = TS) -> None:
        """Change one tag's value (as a poll would) and bump the device's change counter."""
        values = {k: dict(v) for k, v in self.device_values(device_id).items()}
        values[tag] = {"value": value, "quality": quality, "source_ts": source_ts}
        self.values[device_id] = values
        self.live_versions[device_id] = self.live_version(device_id) + 1

    async def read_once(self, device_id: str) -> dict[str, Any]:
        self._check(device_id)
        self.read_onces.append(device_id)
        return self.device_values(device_id)

    def device_plan(self, device_id: str) -> dict[str, Any]:
        return device_plan_json(self._check(device_id), self.loaded.poller.check.est_rtt_ms)

    async def device_identity(self, device_id: str) -> dict[str, Any]:
        self._check(device_id)
        ident = self.identities.get(device_id)
        return {"connected": ident is not None, "identity": ident or {}}

    async def reload(self) -> dict[str, list[str]]:
        loaded = load_config(self.config_dir)            # raises: old config kept
        old = {c.name: c.model_dump_json() for c in self._active()}
        self.loaded = loaded
        self._build()
        new = {c.name: c.model_dump_json() for c in self._active()}
        self.reloads += 1
        return {
            "started": [n for n in new if n not in old],
            "restarted": [n for n in new if n in old and old[n] != new[n]],
            "stopped": [n for n in old if n not in new],
            "unchanged": [n for n in new if n in old and old[n] == new[n]],
        }
