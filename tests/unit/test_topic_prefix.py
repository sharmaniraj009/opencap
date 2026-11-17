"""`mqtt.topic_prefix`: the first topic level(s) of every topic opencap derives itself.

Default topics: ``<prefix>/<slug(channel)>/<slug(device)>/data`` (data),
``<prefix>/<slug(channel)>/<slug(device)>/status`` (device status) and
``<prefix>/poller/<slug(instance)>/status`` (LWT). An explicit ``publish.topic`` is used as is.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opencap.config.kep_import import devices_json_text
from opencap.config.loader import default_topic, load_config
from opencap.config.models import (
    DEFAULT_TOPIC_PREFIX,
    Channel,
    Device,
    DevicesFile,
    MqttCfg,
    PayloadCfg,
    PollerConfig,
    PublishCfg,
    Tag,
)
from opencap.core.cache import TagValue
from opencap.north.mqtt import MqttLink, lwt_topic
from opencap.north.payload import status_topic
from opencap.north.publisher import Publisher
from opencap.north.saf import StoreAndForward


def _device(channel: str = "Site A", name: str = "PLC 1", **publish) -> Device:
    return Device(
        id=f"{channel}/{name}", channel=channel, name=name, driver="modbus_tcp", host="192.0.2.1", port=502,
        tags=[Tag(name="FLOW_1", address="400001", dtype="float32")], publish=PublishCfg(**publish),
    )


# --- model -------------------------------------------------------------------------------------

def test_default_prefix_is_opencap():
    assert DEFAULT_TOPIC_PREFIX == "opencap"
    assert MqttCfg().topic_prefix == "opencap"
    assert PollerConfig().mqtt.topic_prefix == "opencap"


@pytest.mark.parametrize("prefix", ["acme", "acme/plant-1", "Plant_7", "a/b/c", "site.east"])
def test_valid_prefixes_are_kept_verbatim(prefix):
    assert MqttCfg(topic_prefix=prefix).topic_prefix == prefix


@pytest.mark.parametrize(
    "prefix",
    ["", "/", "/acme", "acme/", "acme//plant", "acme/+", "#", "acme/#", "$SYS", "$acme/x", "a\x00b", "  "],
)
def test_invalid_prefixes_are_rejected(prefix):
    with pytest.raises(ValidationError, match="topic_prefix"):
        MqttCfg(topic_prefix=prefix)


# --- topic helpers -----------------------------------------------------------------------------

def test_helpers_default_to_opencap():
    dev = _device()
    assert default_topic(dev) == "opencap/site_a/plc_1/data"
    assert status_topic(dev) == "opencap/site_a/plc_1/status"
    assert lwt_topic("Opencap 1") == "opencap/poller/opencap_1/status"


def test_helpers_use_the_prefix_unslugged():
    dev = _device()
    assert default_topic(dev, "Acme/plant-1") == "Acme/plant-1/site_a/plc_1/data"
    assert status_topic(dev, "Acme/plant-1") == "Acme/plant-1/site_a/plc_1/status"
    assert lwt_topic("Opencap 1", "Acme/plant-1") == "Acme/plant-1/poller/opencap_1/status"


# --- loader ------------------------------------------------------------------------------------

def _write(cfg: Path, devices: list[Device], poller_yaml: str) -> Path:
    cfg.mkdir(parents=True, exist_ok=True)
    df = DevicesFile(channels=[Channel(name=devices[0].channel, devices=devices)])
    (cfg / "devices.json").write_text(devices_json_text(df), encoding="utf-8")
    (cfg / "poller.yaml").write_text(poller_yaml, encoding="utf-8")
    return cfg


def test_loader_applies_prefix_to_default_topics_only(tmp_path):
    cfg = _write(tmp_path, [_device(name="A"), _device(name="B", topic="custom/b/data")],
                 "mqtt: {topic_prefix: acme/east}\n")
    loaded = load_config(cfg)
    topics = {d.name: d.publish.topic for c in loaded.channels for d in c.devices}
    assert topics == {"A": "acme/east/site_a/a/data", "B": "custom/b/data"}


def test_loader_without_prefix_keeps_opencap(tmp_path):
    cfg = _write(tmp_path, [_device(name="A")], "instance: x\n")
    loaded = load_config(cfg)
    assert loaded.channels[0].devices[0].publish.topic == "opencap/site_a/a/data"


def test_loader_rejects_bad_prefix(tmp_path):
    cfg = _write(tmp_path, [_device(name="A")], "mqtt: {topic_prefix: 'acme/#'}\n")
    with pytest.raises(ValidationError, match="topic_prefix"):
        load_config(cfg)


# --- MQTT link (LWT) ---------------------------------------------------------------------------

def test_link_lwt_uses_prefix(tmp_path):
    saf = StoreAndForward(tmp_path / "s.sqlite", 1000)
    try:
        link = MqttLink(MqttCfg(topic_prefix="acme"), "opencap-7", saf)
        assert link.lwt_topic == "acme/poller/opencap-7/status"
        will = link.client_kwargs()["will"]
        assert (will.topic, will.payload, will.retain) == ("acme/poller/opencap-7/status", b"offline", True)
        assert MqttLink(MqttCfg(), "opencap-7", saf).lwt_topic == "opencap/poller/opencap-7/status"
    finally:
        saf.close()


# --- publisher (status topic and data fallback) ------------------------------------------------

class _Cache:
    def __init__(self, values: dict[str, dict[str, TagValue]]) -> None:
        self._values = values

    def snapshot(self, device_id: str) -> dict[str, TagValue]:
        return dict(self._values.get(device_id, {}))

    def version(self, device_id: str) -> int:
        return 1


class _Link:
    is_secure = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bool]] = []

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        self.sent.append((topic, payload, retain))


async def test_publisher_uses_prefix_for_status_and_fallback_data_topic():
    from datetime import UTC, datetime

    dev = _device(interval_ms=50)                 # publish.topic unset: the publisher derives it
    custom = _device(name="PLC 2", interval_ms=50, topic="custom/plc2")
    good = {"FLOW_1": TagValue(1.0, "good", datetime(2026, 11, 16, tzinfo=UTC))}
    link = _Link()
    status = {"state": "ok", "last_good": None, "errors": 0}
    pub = Publisher([dev, custom], _Cache({dev.id: good, custom.id: good}), link, PayloadCfg(),
                    lambda _id: status, topic_prefix="acme")
    task = asyncio.create_task(pub.run())
    try:
        for _ in range(200):
            topics = {t for t, _, _ in link.sent}
            if {"acme/site_a/plc_1/data", "custom/plc2"} <= topics:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    topics = {t for t, _, _ in link.sent}
    assert {"acme/site_a/plc_1/status", "acme/site_a/plc_2/status"} <= topics
    assert {"acme/site_a/plc_1/data", "custom/plc2"} <= topics
    assert not any(t.startswith("opencap/") for t in topics)
    retained = {t for t, _, r in link.sent if r}
    assert retained == {"acme/site_a/plc_1/status", "acme/site_a/plc_2/status"}
    assert json.loads(next(p for t, p, _ in link.sent if t == "acme/site_a/plc_1/status")) == status
