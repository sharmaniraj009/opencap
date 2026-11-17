"""Write a complete opencap config directory for tests (devices.json, map, poller.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from opencap.config.models import Channel, Device, DevicesFile


def write_config(
    root: Path,
    devices: list[Device],
    *,
    mqtt_port: int = 1883,
    api_port: int = 0,
    poller: dict[str, Any] | None = None,
    publish: dict[str, Any] | None = None,
) -> Path:
    """Create `root/config` for `devices` and return its path. `poller` deep-merges into poller.yaml."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    channels: dict[str, list[Device]] = {}
    for dev in devices:
        channels.setdefault(dev.channel, []).append(dev)
    df = DevicesFile(channels=[Channel(name=name, devices=devs) for name, devs in channels.items()])
    (config_dir / "devices.json").write_text(df.model_dump_json(indent=1), encoding="utf-8")

    entries = {
        d.id: {"sub_device_id": f"OL_TEST_{i:03d}", **({} if publish is None else {})}
        for i, d in enumerate(devices)
    }
    dmap: dict[str, Any] = {"defaults": {"env": "test"}, "devices": entries}
    (config_dir / "devices_map.yaml").write_text(yaml.safe_dump(dmap, sort_keys=False), encoding="utf-8")
    if publish:
        overrides = {"devices": {d.id: {"publish": publish} for d in devices}}
        (config_dir / "overrides.yaml").write_text(yaml.safe_dump(overrides, sort_keys=False), encoding="utf-8")

    cfg: dict[str, Any] = {
        "instance": "opencap-test",
        "log_level": "WARNING",
        "mqtt": {"host": "127.0.0.1", "port": mqtt_port, "client_id": f"opencap-test-{root.name}"},
        "saf": {"path": str(root / "saf.sqlite")},
        "api": {"bind": "127.0.0.1", "port": api_port},
    }
    for key, value in (poller or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    (config_dir / "poller.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return config_dir
