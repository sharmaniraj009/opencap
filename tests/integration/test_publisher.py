"""Publisher (PLAN 7.2): interval, on_change, deadband, heartbeat and status topics.

Most tests use a fake cache and a fake link (fast, exact); the last one runs the real
MqttLink against the in-process broker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from opencap.config.loader import default_topic
from opencap.config.models import Device, PayloadCfg, PublishCfg, Tag
from opencap.core.cache import TagValue
from opencap.north.mqtt import MqttLink
from opencap.north.payload import build_payload, encode
from opencap.north.publisher import Publisher, changed
from opencap.north.saf import StoreAndForward
from tests.helpers.broker import mqtt_broker  # noqa: F401

# interval timing bounds (#69): no automatic GC pause inside a test (tests/conftest.py no_gc)
pytestmark = pytest.mark.usefixtures("no_gc")

T0 = datetime(2026, 11, 16, 10, 15, 1, tzinfo=UTC)
CFG = PayloadCfg()


def make_device(name="PLC 1", channel="SITE A", **publish) -> Device:
    publish.setdefault("sub_device_id", f"OL_{name.replace(' ', '_')}")
    tags = [Tag(name="FLOW_1", address="400001", dtype="float32"),
            Tag(name="PUMP_RUN", address="000001", dtype="bool"),
            Tag(name="SPARE", address="400003", dtype="uint16", enabled=False)]
    return Device(id=f"{channel}/{name}", channel=channel, name=name, driver="modbus_tcp", host="192.0.2.1",
                  port=502, tags=tags, publish=PublishCfg(**publish))


def vals(flow=10.0, run=True, q_flow="good", ts=T0) -> dict[str, TagValue]:
    return {"FLOW_1": TagValue(flow, q_flow, ts), "PUMP_RUN": TagValue(run, "good", ts)}


NEVER = {"FLOW_1": TagValue(None, "stale", None), "PUMP_RUN": TagValue(None, "stale", None)}


class FakeCache:
    def __init__(self) -> None:
        self.snaps: dict[str, dict[str, TagValue]] = {}
        self.versions: dict[str, int] = {}

    def set(self, device_id: str, values: dict[str, TagValue]) -> None:
        self.snaps[device_id] = dict(values)
        self.versions[device_id] = self.versions.get(device_id, 0) + 1

    def snapshot(self, device_id: str) -> dict[str, TagValue]:
        return dict(self.snaps.get(device_id, {}))

    def version(self, device_id: str) -> int:
        return self.versions.get(device_id, 0)


@dataclass
class Sent:
    t: float
    topic: str
    payload: bytes
    retain: bool


class FakeLink:
    def __init__(self, is_secure: bool = False) -> None:
        self.is_secure = is_secure
        self.sent: list[Sent] = []

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        self.sent.append(Sent(time.monotonic(), topic, payload, retain))

    def on(self, topic: str) -> list[Sent]:
        return [s for s in self.sent if s.topic == topic]

    def data(self, topic: str) -> list[dict]:
        return [json.loads(s.payload) for s in self.on(topic)]


class Status:
    """Mutable status_fn."""

    def __init__(self, state="ok"):
        self.by_dev: dict[str, dict] = {}
        self.default = {"state": state, "last_good": "2026-11-16T10:15:01+00:00", "errors": 0}

    def __call__(self, device_id: str) -> dict:
        return self.by_dev.get(device_id, self.default)


@contextlib.asynccontextmanager
async def running(pub: Publisher):
    task = asyncio.create_task(pub.run())
    try:
        yield pub
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def topic_of(dev: Device) -> str:
    return dev.publish.topic or default_topic(dev)


async def settle(link: FakeLink, topic: str, n: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while len(link.on(topic)) < n:
        if time.monotonic() > deadline:
            raise AssertionError(f"expected {n} messages on {topic}, got {len(link.on(topic))}")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------- interval

async def test_interval_publishes_full_snapshot_every_interval():
    dev = make_device(interval_ms=100)
    custom = make_device("PLC 2", interval_ms=100, topic="acme/custom/plc2")
    cache, link = FakeCache(), FakeLink()
    cache.set(dev.id, vals())
    cache.set(custom.id, vals(flow=5.0))
    t_start = time.monotonic()                      # == loop.time(): no later than the publisher's start
    async with running(Publisher([dev, custom], cache, link, CFG, Status())):
        await asyncio.sleep(0.56)
    msgs = link.on(topic_of(dev))
    assert topic_of(dev) == "opencap/site_a/plc_1/data"
    assert 4 <= len(msgs) <= 6
    assert all(m.payload == encode(build_payload(dev, vals(), CFG, is_secure=False)) for m in msgs)
    assert not any(m.retain for m in msgs)
    # Fixed rate: message k goes out on its own slot t_start + (k+1) * interval, never early.
    # Checking gaps between messages instead would count one late wake-up twice (a long gap,
    # then a short one back onto the grid), so lateness is measured against the grid.
    # A shared CI runner can delay the first wake-up (cold start: 73 ms seen), so the first
    # message gets a looser bound (150 ms) than the rest (50 ms). Drift is caught separately:
    # the median gap (robust to one late message) must equal the interval within 8 ms.
    # asyncio may run a timer up to one clock resolution early: ~1 ns on Linux, ~15.6 ms for
    # time.monotonic on Windows with Python 3.12. `res` widens every bound by that much.
    res = time.get_clock_info("monotonic").resolution
    lateness = [m.t - (t_start + (k + 1) * 0.1) for k, m in enumerate(msgs)]
    assert -0.005 - res < lateness[0] < 0.15 + res, lateness
    assert all(-0.005 - res < late < 0.05 + res for late in lateness[1:]), lateness
    gaps = sorted(b.t - a.t for a, b in zip(msgs, msgs[1:]))
    assert abs(gaps[len(gaps) // 2] - 0.1) < 0.008 + res, gaps
    assert msgs[0].t - link.sent[0].t >= 0.08 - res  # status at start, first data one interval later
    assert 4 <= len(link.on("acme/custom/plc2")) <= 6


async def test_interval_follows_the_cache():
    dev = make_device(interval_ms=60)
    cache, link = FakeCache(), FakeLink(is_secure=True)
    cache.set(dev.id, vals(flow=1.0))
    async with running(Publisher([dev], cache, link, CFG, Status())):
        await settle(link, topic_of(dev), 1)
        cache.set(dev.id, vals(flow=2.0, q_flow="bad_comm"))
        n = len(link.on(topic_of(dev)))
        await settle(link, topic_of(dev), n + 1)
    first, last = link.data(topic_of(dev))[0], link.data(topic_of(dev))[-1]
    assert first["Data"] == {"FLOW_1": 1.0, "PUMP_RUN": True} and first["isSecure"] is True
    assert last["Data"] == {"PUMP_RUN": True} and last["DataValid"] is False and last["BadTags"] == ["FLOW_1"]


async def test_never_read_device_gets_status_but_no_data():
    dev = make_device(interval_ms=50)
    cache, link = FakeCache(), FakeLink()
    cache.set(dev.id, NEVER)
    status = Status("offline")
    status.default = {"state": "offline", "last_good": None, "errors": 4}
    async with running(Publisher([dev], cache, link, CFG, status, status_check_s=0.02)):
        await asyncio.sleep(0.3)
        assert link.on(topic_of(dev)) == []
        st = link.on("opencap/site_a/plc_1/status")
        assert len(st) == 1 and st[0].retain
        assert json.loads(st[0].payload) == {"state": "offline", "last_good": None, "errors": 4}
        cache.set(dev.id, vals())                                   # first good read
        await settle(link, topic_of(dev), 1)


async def test_after_a_good_read_device_publishes_even_when_all_bad():
    dev = make_device(interval_ms=50)
    cache, link = FakeCache(), FakeLink()
    cache.set(dev.id, {"FLOW_1": TagValue(3.0, "bad_comm", T0), "PUMP_RUN": TagValue(True, "stale", T0)})
    async with running(Publisher([dev], cache, link, CFG, Status())):
        await settle(link, topic_of(dev), 2)
    p = link.data(topic_of(dev))[0]
    assert p["Data"] == {} and p["DataValid"] is False and p["BadTags"] == ["FLOW_1", "PUMP_RUN"]
    assert p["DateTime"] != "2026-11-16 15:45:01"                     # no good tag: DateTime is "now"


async def test_disabled_devices_are_skipped():
    on, off = make_device(interval_ms=50), make_device("PLC 9", interval_ms=50)
    off = off.model_copy(update={"enabled": False})
    cache, link = FakeCache(), FakeLink()
    cache.set(on.id, vals())
    cache.set(off.id, vals())
    pub = Publisher([on, off], cache, link, CFG, Status(), status_check_s=0.02)
    assert [d.id for d in pub.devices] == [on.id]
    async with running(pub):
        await settle(link, topic_of(on), 2)
    assert not any("plc_9" in s.topic for s in link.sent)


# ---------------------------------------------------------------------- on_change

async def test_on_change_deadband_and_quality():
    dev = make_device(mode="on_change", deadband=0.5, heartbeat_ms=60_000)
    cache, link = FakeCache(), FakeLink()
    topic = topic_of(dev)
    pub = Publisher([dev], cache, link, CFG, Status(), on_change_poll_s=5.0)   # only notify_cycle wakes it

    async def cycle(values, expect_publish: bool):
        n = len(link.on(topic))
        cache.set(dev.id, values)
        t0 = time.monotonic()
        pub.notify_cycle(dev.id)
        if expect_publish:
            await settle(link, topic, n + 1, timeout=1.0)
            assert time.monotonic() - t0 < 0.2                     # driven by notify_cycle, not the poll
        else:
            await asyncio.sleep(0.05)
            assert len(link.on(topic)) == n

    async with running(pub):
        await asyncio.sleep(0.01)
        await cycle(vals(flow=10.0), True)                         # first good read
        await cycle(vals(flow=10.3), False)                        # 0.3 <= deadband
        await cycle(vals(flow=10.2), False)
        await cycle(vals(flow=10.6), True)                         # 0.6 from the last *published* 10.0
        await cycle(vals(flow=10.6, q_flow="bad_comm"), True)      # good -> bad
        await cycle(vals(flow=10.6, q_flow="stale"), False)        # bad -> bad: payload would not change
        await cycle(vals(flow=10.9, q_flow="good"), True)          # bad -> good
        await cycle(vals(flow=10.9, run=False), True)              # bool flip
        await cycle(vals(flow=10.9, run=False, ts=T0 + timedelta(seconds=5)), False)  # only the ts moved
    flows = [p["Data"].get("FLOW_1") for p in link.data(topic)]
    assert flows == [10.0, 10.6, None, 10.9, 10.9]
    assert link.data(topic)[2]["BadTags"] == ["FLOW_1"]


async def test_on_change_deadband_zero_ignores_rounding_noise():
    dev = make_device(mode="on_change", deadband=0.0, heartbeat_ms=60_000)
    cache, link = FakeCache(), FakeLink()
    pub = Publisher([dev], cache, link, CFG, Status(), on_change_poll_s=5.0)
    async with running(pub):
        await asyncio.sleep(0.01)
        for flow in (1.0, 1.00000001, 1.00004, 1.0001, 1.0001):
            cache.set(dev.id, vals(flow=flow))
            pub.notify_cycle(dev.id)
            await asyncio.sleep(0.03)
    assert [p["Data"]["FLOW_1"] for p in link.data(topic_of(dev))] == [1.0, 1.0001]


async def test_on_change_heartbeat_sends_full_snapshot():
    dev = make_device(mode="on_change", deadband=100.0, heartbeat_ms=200)
    cache, link = FakeCache(), FakeLink()
    cache.set(dev.id, vals())
    async with running(Publisher([dev], cache, link, CFG, Status(), on_change_poll_s=0.05)):
        await asyncio.sleep(0.75)
    msgs = link.on(topic_of(dev))
    assert 3 <= len(msgs) <= 5                                     # first good read + ~3 heartbeats
    gaps = [b.t - a.t for a, b in zip(msgs, msgs[1:])]
    assert all(0.18 < g < 0.3 for g in gaps), gaps
    assert all(json.loads(m.payload)["Data"] == {"FLOW_1": 10.0, "PUMP_RUN": True} for m in msgs)


async def test_on_change_falls_back_to_polling_without_notify():
    dev = make_device(mode="on_change", heartbeat_ms=60_000)
    cache, link = FakeCache(), FakeLink()
    cache.set(dev.id, vals(flow=1.0))
    async with running(Publisher([dev], cache, link, CFG, Status(), on_change_poll_s=0.05)):
        await settle(link, topic_of(dev), 1)
        cache.set(dev.id, vals(flow=2.0))                          # nobody calls notify_cycle
        await settle(link, topic_of(dev), 2, timeout=0.5)


def test_changed_rule():
    dev = make_device(deadband=1.0)
    assert not changed(dev, vals(flow=1.0), vals(flow=2.0), 4)       # exactly the deadband: no
    assert changed(dev, vals(flow=1.0), vals(flow=2.01), 4)
    assert changed(dev, vals(flow=1.0), vals(flow=-0.5), 4)
    assert changed(dev, vals(), {"PUMP_RUN": vals()["PUMP_RUN"]}, 4)  # tag disappeared = not good
    assert changed(dev, vals(flow=1.0), vals(flow=float("nan")), 4)  # NaN is bad in the payload
    nan = vals(flow=float("nan"))
    assert not changed(dev, nan, vals(flow=float("inf")), 4)


# ---------------------------------------------------------------------- status

async def test_status_on_state_change_and_periodic_refresh():
    a, b = make_device("A", interval_ms=60_000), make_device("B", interval_ms=60_000)
    cache, link = FakeCache(), FakeLink()
    status = Status()
    pub = Publisher([a, b], cache, link, CFG, status, status_check_s=0.02, status_refresh_s=0.4)
    ta, tb = "opencap/site_a/a/status", "opencap/site_a/b/status"
    async with running(pub):
        await settle(link, ta, 1)
        await settle(link, tb, 1)
        assert link.on(ta)[0].retain
        assert json.loads(link.on(ta)[0].payload) == status.default
        status.by_dev[a.id] = {"state": "ok", "last_good": "2026-11-16T10:15:01+00:00", "errors": 5,
                               "cycle_ms": 40}                     # errors only: no new message yet
        await asyncio.sleep(0.1)
        assert len(link.on(ta)) == 1
        status.by_dev[a.id] = {"state": "demoted", "last_good": "2026-11-16T10:15:01+00:00", "errors": 6}
        t0 = time.monotonic()
        await settle(link, ta, 2)
        assert time.monotonic() - t0 < 0.15                        # published as soon as the state changes
        assert json.loads(link.on(ta)[1].payload) == {"state": "demoted",
                                                      "last_good": "2026-11-16T10:15:01+00:00", "errors": 6}
        await settle(link, tb, 2, timeout=1.0)                     # periodic refresh of an unchanged device
    gaps = [y.t - x.t for x, y in zip(link.on(tb), link.on(tb)[1:])]
    assert all(g >= 0.38 for g in gaps), gaps


async def test_status_fn_errors_do_not_stop_others():
    a, b = make_device("A", interval_ms=60_000), make_device("B", interval_ms=60_000)
    link = FakeLink()

    def status_fn(device_id: str) -> dict:
        if device_id == a.id:
            raise KeyError(device_id)
        return {"state": "ok", "last_good": None, "errors": 0}

    async with running(Publisher([a, b], FakeCache(), link, CFG, status_fn, status_check_s=0.02)):
        await settle(link, "opencap/site_a/b/status", 1)
        await asyncio.sleep(0.1)
    assert link.on("opencap/site_a/a/status") == []


# ---------------------------------------------------------------------- end to end

async def test_publisher_through_real_link_and_broker(mqtt_broker, tmp_path):
    dev = make_device(interval_ms=100)
    cache = FakeCache()
    cache.set(dev.id, vals(flow=123.40000152587891))
    saf = StoreAndForward(tmp_path / "saf.sqlite", 10_000_000)
    link = MqttLink(mqtt_broker.mqtt_cfg(), "e2e", saf)
    async with mqtt_broker.collector("opencap/#") as col:
        link_task = asyncio.create_task(link.run())
        try:
            async with running(Publisher([dev], cache, link, CFG, Status(), status_check_s=0.05)):
                data = await col.wait_count(topic_of(dev), 3)
        finally:
            await link.stop()
            await asyncio.gather(link_task, return_exceptions=True)
            saf.close()
    p = data[0].json()
    assert list(p) == ["SubDeviceId", "FirmVersion", "Env", "isSecure", "DataValid", "Data", "DateTime"]
    assert p["SubDeviceId"] == "OL_PLC_1" and p["isSecure"] is False and p["DataValid"] is True
    assert p["Data"] == {"FLOW_1": 123.4, "PUMP_RUN": True}
    assert p["DateTime"] == "2026-11-16 15:45:01"
    status = col.last("opencap/site_a/plc_1/status")
    assert status is not None and status.json() == Status().default
    assert col.last("opencap/poller/e2e/status").payload == b"offline"


@pytest.mark.parametrize("mode", ["interval", "on_change"])
async def test_cancel_is_clean(mode):
    dev = make_device(mode=mode, interval_ms=50, heartbeat_ms=50)
    cache, link = FakeCache(), FakeLink()
    cache.set(dev.id, vals())
    task = asyncio.create_task(Publisher([dev], cache, link, CFG, Status()).run())
    await asyncio.sleep(0.12)
    task.cancel()
    res = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(res[0], asyncio.CancelledError)


# ---------------------------------------------------------------------- iotgw_json (#36)

def _iotgw_ids(msg: dict) -> dict[str, tuple]:
    return {e["id"]: (e["v"], e["q"]) for e in msg["values"]}


async def test_iotgw_interval_publishes_the_standard_template():
    dev = make_device(interval_ms=100, format="iotgw_json")
    plain = make_device("PLC 2", interval_ms=100)
    cache, link = FakeCache(), FakeLink()
    cache.set(dev.id, vals(flow=7.5, q_flow="bad_comm"))
    cache.set(plain.id, vals())
    async with running(Publisher([dev, plain], cache, link, CFG, Status())):
        await settle(link, topic_of(dev), 2)
        await settle(link, topic_of(plain), 1)
    msg = link.data(topic_of(dev))[0]
    assert list(msg) == ["timestamp", "values"]
    assert _iotgw_ids(msg) == {"SITE A.PLC 1.FLOW_1": (7.5, False), "SITE A.PLC 1.PUMP_RUN": (True, True)}
    assert msg["values"][1]["t"] == 1794824101000                    # T0 in ms; SPARE (disabled) is not listed
    assert "SubDeviceId" in link.data(topic_of(plain))[0]            # the other device keeps SubDevice JSON
    assert not any(m.retain for m in link.on(topic_of(dev)))
    status = link.data("opencap/site_a/plc_1/status")[0]             # the status topic is unchanged
    assert status == Status().default


async def test_iotgw_on_change_rules_are_unchanged():
    dev = make_device(mode="on_change", deadband=0.5, heartbeat_ms=60_000, format="iotgw_json")
    cache, link = FakeCache(), FakeLink()
    topic = topic_of(dev)
    pub = Publisher([dev], cache, link, CFG, Status(), on_change_poll_s=5.0)

    async def cycle(values, expect_publish: bool):
        n = len(link.on(topic))
        cache.set(dev.id, values)
        pub.notify_cycle(dev.id)
        if expect_publish:
            await settle(link, topic, n + 1, timeout=1.0)
        else:
            await asyncio.sleep(0.05)
            assert len(link.on(topic)) == n

    async with running(pub):
        await asyncio.sleep(0.01)
        await cycle(vals(flow=10.0), True)
        await cycle(vals(flow=10.3), False)                          # inside the deadband
        await cycle(vals(flow=10.6), True)
        await cycle(vals(flow=10.6, q_flow="bad_comm"), True)        # good -> bad
    flows = [(e["v"], e["q"]) for m in link.data(topic) for e in m["values"] if e["id"].endswith("FLOW_1")]
    assert flows == [(10.0, True), (10.6, True), (10.6, False)]


async def test_iotgw_through_real_link_and_broker_with_outbox(mqtt_broker, tmp_path):
    dev = make_device(interval_ms=100, format="iotgw_json")
    cache = FakeCache()
    cache.set(dev.id, vals(flow=123.40000152587891))
    saf = StoreAndForward(tmp_path / "saf.sqlite", 10_000_000)
    link = MqttLink(mqtt_broker.mqtt_cfg(), "e2e-iotgw", saf)
    async with mqtt_broker.collector("opencap/#") as col:
        link_task = asyncio.create_task(link.run())
        try:
            async with running(Publisher([dev], cache, link, CFG, Status(), status_check_s=0.05)):
                data = await col.wait_count(topic_of(dev), 2)
        finally:
            await link.stop()
            await asyncio.gather(link_task, return_exceptions=True)
            saf.close()
    p = data[0].json()
    assert list(p) == ["timestamp", "values"] and isinstance(p["timestamp"], int)
    assert p["values"] == [
        {"id": "SITE A.PLC 1.FLOW_1", "v": 123.4, "q": True, "t": 1794824101000},
        {"id": "SITE A.PLC 1.PUMP_RUN", "v": True, "q": True, "t": 1794824101000},
    ]
