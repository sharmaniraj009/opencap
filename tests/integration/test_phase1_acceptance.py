"""PLAN.md Phase 1 acceptance, the whole poller in one process:

- 30 simulated Modbus devices (pymodbus servers) with ~1,600 tags;
- every device publishes every interval, with correct SubDevice JSON payloads;
- killing one simulator demotes only that device;
- a network outage between the poller and the broker loses no messages
  (OPENCAP_OUTAGE_S, default 8 s; the plan's 10-minute run is
  `OPENCAP_OUTAGE_S=600 pytest -m acceptance`).

`--engine=rust` (or OPENCAP_ENGINE=rust) runs the same scenario against the Rust engine
binary instead of the Python `Poller` (conformance; tests/helpers/engine.py).

The outage is a cut TCP path (`NetCut`), not a broker shutdown: amqtt PUBACKs a message
before it routes it, so stopping amqtt itself can drop messages it has already
acknowledged. That is a property of the test broker, not of opencap (see
docs/decisions.md D-018). The broker keeps running, as on a real IPsec link failure.
"""

from __future__ import annotations

import asyncio
import math
import os
import re

import httpx
import pytest

from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import free_port, make_sim_devices, start_sims, stop_sims
from tests.helpers.netcut import NetCut

pytestmark = pytest.mark.acceptance

N_DEVICES = 30
TAGS_PER_DEVICE = 53
INTERVAL_MS = 2000
SCAN_MS = 1000                               # production-like: min_scan_ms is 1000 in poller.yaml
OUTAGE_S = float(os.environ.get("OPENCAP_OUTAGE_S", "8"))
VICTIM = 7                                   # index of the simulator we kill
DATETIME_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

API_TOKEN = "phase1-acceptance-token"


def _close(got, want) -> bool:
    if isinstance(want, float) or isinstance(got, float):
        return math.isclose(float(got), round(float(want), 4), rel_tol=1e-6, abs_tol=1e-4)
    return got == want


def _is_subsequence(needle: list[bytes], haystack: list[bytes]) -> bool:
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


async def _wait(pred, timeout: float, what: str, step: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(step)


async def test_phase1_acceptance(tmp_path, engine_factory, monkeypatch):
    # open mode (no token) is read-only (D-040); read-once below needs the admin token
    monkeypatch.setenv("OPENCAP_API_TOKEN", API_TOKEN)
    configure_logging("WARNING")
    timeout_s = max(120.0, OUTAGE_S * 3 + 120)
    async with asyncio.timeout(timeout_s):
        async with TestBroker() as broker, NetCut(broker.host, broker.port) as netcut:
            sims = await start_sims(make_sim_devices(N_DEVICES, TAGS_PER_DEVICE, scan_ms=SCAN_MS, timeout_ms=1000))
            try:
                await _scenario(tmp_path, broker, netcut, sims, engine_factory)
            finally:
                await stop_sims(sims)


async def _scenario(tmp_path, broker: TestBroker, netcut: NetCut, sims, engine_factory) -> None:
    devices = [s.device for s in sims]
    assert len(devices) == N_DEVICES
    assert 1_550 <= sum(len(d.tags) for d in devices) <= 1_650
    api_port = free_port()
    config_dir = write_config(
        tmp_path, devices, mqtt_port=netcut.port, api_port=api_port,       # poller -> NetCut -> broker
        publish={"interval_ms": INTERVAL_MS},
    )
    async with broker.collector("opencap/#") as col:
        await col.wait_ready()
        poller = engine_factory(config_dir)    # the Python Poller, or the Rust engine (--engine=rust)
        await poller.start()
        sent = poller.sent                     # everything the publisher hands to the link
        try:
            loaded = {d.id: d for c in poller.loaded.channels for d in c.devices}
            topics = {d.id: loaded[d.id].publish.topic for d in devices}
            status_topics = {d.id: topics[d.id].rsplit("/", 1)[0] + "/status" for d in devices}
            await _check_publishing(col, poller, sims, topics)
            await _check_kill_one(col, poller, sims, topics, status_topics)
            await _check_api(api_port, devices)
            await _check_network_outage(col, poller, broker, netcut, sent)
        finally:
            await poller.stop()


async def _check_publishing(col, poller, sims, topics) -> None:
    # Every device publishes at least 3 data messages, about one per interval.
    await _wait(lambda: all(len(col.topic(t)) >= 3 for t in topics.values()), 20, "3 messages per device")
    start_counts = {t: len(col.topic(t)) for t in topics.values()}
    intervals = 3
    window = intervals * INTERVAL_MS / 1000
    await asyncio.sleep(window)
    for dev_id, t in topics.items():
        n = len(col.topic(t)) - start_counts[t]
        assert abs(n - intervals) <= 1, (dev_id, n)
    print(f"\n[acceptance] {len(topics)} devices published; per-device messages in {window:.0f}s window: "
          f"{sorted({len(col.topic(t)) - start_counts[t] for t in topics.values()})}")

    # Payload format and values match each simulator.
    for sim in sims:
        msg = col.last(topics[sim.device.id]).json()
        assert list(msg)[:7] == ["SubDeviceId", "FirmVersion", "Env", "isSecure", "DataValid", "Data", "DateTime"]
        assert msg["SubDeviceId"].startswith("OL_TEST_")
        assert msg["Env"] == "test" and msg["isSecure"] is False
        assert msg["FirmVersion"].startswith("opencap-")
        assert DATETIME_RE.match(msg["DateTime"]), msg["DateTime"]
        if msg["DataValid"] is not True:
            values = poller.device_values(sim.device.id)
            detail = poller.device_detail(sim.device.id)
            bad = {n: values[n]["quality"] for n in msg.get("BadTags", [])}
            raise AssertionError(f"{sim.device.id}: DataValid false; qualities={bad}; "
                                 f"last_error={detail.get('last_error')!r}; state={detail.get('state')}")
        expected = sim.expected()
        assert set(msg["Data"]) == set(expected), sim.device.id
        for name, want in expected.items():
            assert _close(msg["Data"][name], want), (sim.device.id, name, msg["Data"][name], want)


async def _check_kill_one(col, poller, sims, topics, status_topics) -> None:
    victim = sims[VICTIM]
    vid = victim.device.id
    others = [s.device.id for s in sims if s.device.id != vid]
    await victim.stop()
    await _wait(lambda: poller.device_detail(vid)["state"] == "demoted", 15, "victim demoted")
    states = {d: poller.device_detail(d)["state"] for d in others}
    assert set(states.values()) == {"ok"}, {d: s for d, s in states.items() if s != "ok"}

    # The retained status topic says demoted; the others still publish valid data.
    await _wait(lambda: (m := col.last(status_topics[vid])) is not None and m.json()["state"] == "demoted",
                5, "demoted status message")
    counts = {d: len(col.topic(topics[d])) for d in others}
    await asyncio.sleep(1.5 * INTERVAL_MS / 1000)
    for d in others:
        assert len(col.topic(topics[d])) > counts[d], f"{d} stopped publishing"
        assert col.last(topics[d]).json()["DataValid"] is True, d
    last_victim = col.last(topics[vid]).json()
    assert last_victim["DataValid"] is False and last_victim.get("BadTags"), last_victim
    health = poller.health()["devices"]
    print(f"[acceptance] after killing {vid}: {health}")
    assert health["demoted"] == 1 and health["ok"] == N_DEVICES - 1, health

    # Power the PLC back on: it is probed after the demotion period and resumes.
    await victim.restart()
    await _wait(lambda: poller.device_detail(vid)["state"] == "ok", 15, "victim resumed")
    await _wait(lambda: col.last(topics[vid]).json()["DataValid"] is True, 5, "victim valid data again")


async def _check_api(api_port, devices) -> None:
    base = f"http://127.0.0.1:{api_port}"
    async with httpx.AsyncClient(base_url=base, timeout=10,
                                 headers={"Authorization": f"Bearer {API_TOKEN}"}) as http:
        health = (await http.get("/health")).json()
        assert health["mqtt_connected"] is True and health["devices"]["ok"] == N_DEVICES
        dev = devices[0].id
        detail = (await http.get(f"/devices/{dev}")).json()
        assert detail["state"] == "ok" and detail["blocks"] > 0
        values = (await http.get(f"/devices/{dev}/values")).json()
        assert all(v["quality"] == "good" for v in values.values())
        once = (await http.post(f"/devices/{dev}/read-once")).json()
        assert set(once) == set(values)
        metrics = (await http.get("/metrics")).text
        assert "poll_requests_total" in metrics and "mqtt_published_total" in metrics


async def _check_network_outage(col, poller, broker, netcut, sent) -> None:
    data_topics = [t for t in sent if t.endswith("/data")]
    # amqtt PUBACKs on arrival and drops its still-unrouted queue when the publisher's
    # connection breaks (probe: 16/20 cuts lost messages when amqtt was busy, 0 when idle;
    # never a message the broker had not received). Cut only when it has caught up, so the
    # test measures opencap and not amqtt. See docs/decisions.md D-021.
    await _wait(lambda: poller.saf.count() == 0 and broker.idle(), 10, "outbox empty and broker idle")
    before = {t: len(sent[t]) for t in data_topics}

    loop = asyncio.get_running_loop()
    cut_at = loop.time()
    await netcut.cut()
    await _wait(lambda: not poller.link.connected, 10, "link notices the cut")
    await asyncio.sleep(max(0.0, min(3.0, OUTAGE_S / 2) - (loop.time() - cut_at)))
    queued_mid_outage = poller.saf.count()
    assert queued_mid_outage > 0
    await asyncio.sleep(max(0.0, OUTAGE_S - (loop.time() - cut_at)))
    await netcut.restore()
    await _wait(lambda: poller.link.connected and poller.saf.count() == 0, 60 + OUTAGE_S / 5, "outbox drained")

    # Freeze what we check: everything produced up to now. The poller keeps publishing every
    # interval, so the newest batch is always in transit; comparing it would be a false alarm.
    checked = {t: list(sent[t]) for t in data_topics}
    produced_during = sum(len(checked[t]) - before[t] for t in data_topics)
    assert produced_during >= N_DEVICES * (OUTAGE_S - 2) * 1000 / INTERVAL_MS, produced_during

    def at_broker(t: str) -> bool:
        return _is_subsequence(checked[t], broker.accepted_payloads(t))

    def at_subscriber(t: str) -> bool:
        return _is_subsequence(checked[t], col.payloads(t))

    # The broker acknowledges on arrival but forwards more slowly: give it time to deliver
    # every checked message. Real loss never completes and is reported below.
    try:
        await _wait(lambda: all(at_broker(t) and at_subscriber(t) for t in data_topics),
                    60 + OUTAGE_S / 5, "every checked message at the broker and the subscriber", step=0.5)
    except AssertionError:
        pass

    # 1. opencap's guarantee: every message it produced reached the broker, in order.
    not_at_broker = {t: len(set(checked[t]) - set(broker.accepted_payloads(t)))
                     for t in data_topics if not at_broker(t)}
    assert not not_at_broker, f"messages opencap never delivered to the broker: {not_at_broker}"

    # 2. End to end: the subscriber received all of them too, and nothing unknown.
    missing = {t: len(set(checked[t]) - set(col.payloads(t))) for t in data_topics if not at_subscriber(t)}
    for t in data_topics:
        unknown = set(col.payloads(t)) - set(sent[t])
        assert not unknown, f"{t}: {len(unknown)} message(s) that were never sent"
    assert not missing, f"accepted by the broker but not delivered to the subscriber: {missing}"

    total_checked = sum(len(checked[t]) for t in data_topics)
    dupes = sum(len(col.payloads(t)) - len(set(col.payloads(t))) for t in data_topics)
    print(f"[acceptance] outage {OUTAGE_S:.0f}s: queued mid-outage={queued_mid_outage}, "
          f"produced during outage={produced_during}, data messages checked={total_checked}, "
          f"all at broker and subscriber, duplicates={dupes}, lost=0")
