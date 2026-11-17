"""`mqtt.topic_prefix` end to end: poller.yaml -> Poller -> real MQTT broker.

Data, device status and the poller's LWT/online topic all start with the configured prefix,
and nothing is published under the default ``opencap/`` prefix.
"""

from __future__ import annotations

from opencap.core.runtime import Poller
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims


async def test_poller_publishes_everything_under_the_prefix(tmp_path):
    configure_logging("WARNING")
    async with TestBroker() as broker, broker.collector("#") as col:
        sims = await start_sims(make_sim_devices(1, 5, seed=3, scan_ms=200, timeout_ms=500))
        try:
            dev = sims[0].device
            config_dir = write_config(
                tmp_path, [dev], mqtt_port=broker.port,
                poller={"instance": "edge 1", "mqtt": {"topic_prefix": "acme/plant-1"}},
                publish={"interval_ms": 200},
            )
            poller = Poller(config_dir, serve_api=False)
            await poller.start()
            try:
                loaded_dev = poller.loaded.channels[0].devices[0]
                data_topic = loaded_dev.publish.topic
                assert data_topic.startswith("acme/plant-1/") and data_topic.endswith("/data")
                status_topic = data_topic.rsplit("/", 1)[0] + "/status"
                await col.wait_payload("acme/plant-1/poller/edge_1/status", b"online", timeout=10)
                await col.wait_count(data_topic, 2, timeout=10)
                await col.wait_for(lambda: col.last(status_topic) is not None, timeout=10, what="status")
            finally:
                await poller.stop()
            await col.wait_payload("acme/plant-1/poller/edge_1/status", b"offline", timeout=10)
        finally:
            await stop_sims(sims)
        topics = {m.topic for m in col.messages}
        assert all(t.startswith("acme/plant-1/") for t in topics), sorted(topics)


async def test_reload_moves_data_and_status_topics_and_warns(tmp_path):
    """A changed prefix moves data and status topics at reload; the LWT needs a restart."""
    import yaml
    from structlog.testing import capture_logs

    configure_logging("WARNING")
    async with TestBroker() as broker, broker.collector("#") as col:
        sims = await start_sims(make_sim_devices(1, 5, seed=4, scan_ms=200, timeout_ms=500))
        try:
            dev = sims[0].device
            config_dir = write_config(tmp_path, [dev], mqtt_port=broker.port, poller={"instance": "edge"},
                                      publish={"interval_ms": 200})
            poller = Poller(config_dir, serve_api=False)
            await poller.start()
            try:
                old = poller.loaded.channels[0].devices[0].publish.topic
                assert old.startswith("opencap/")
                await col.wait_count(old, 1, timeout=10)
                poller_yaml = config_dir / "poller.yaml"
                cfg = yaml.safe_load(poller_yaml.read_text())
                cfg["mqtt"]["topic_prefix"] = "acme"
                poller_yaml.write_text(yaml.safe_dump(cfg))
                with capture_logs() as logs:
                    await poller.reload()
                assert any(e["event"] == "mqtt_settings_changed" for e in logs)
                new = poller.loaded.channels[0].devices[0].publish.topic
                assert new == "acme/" + old.split("/", 1)[1]
                await col.wait_count(new, 1, timeout=10)
                await col.wait_for(lambda: col.last(new.rsplit("/", 1)[0] + "/status") is not None,
                                   timeout=10, what="status under the new prefix")
                assert poller.link.lwt_topic == "opencap/poller/edge/status"   # unchanged until restart
            finally:
                await poller.stop()
        finally:
            await stop_sims(sims)
