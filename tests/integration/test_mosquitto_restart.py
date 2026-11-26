"""A broker restart loses no messages, against Mosquitto with persistence (#8, D-021 follow-up).

The Phase 1 acceptance test proves no loss across a *network* outage (D-021). This one restarts
the broker itself: a real Mosquitto with ``persistence true`` (tests/helpers/mosquitto.py) is
stopped cleanly and started again on the same port and data three times while 10 simulated
Modbus devices publish every second. The subscriber has a persistent session (QoS 1,
``clean_session`` false). It stays connected through the first restart; for the second and
third it is away, so the broker holds the messages it has already acknowledged to the poller
(whose outbox has deleted them) across the restart. That is what persistence must keep.

What is checked is the Phase 1 comparison (D-018): after the last restart and once the outbox
has drained, the set of messages produced so far is frozen (the newest batch is always in
transit, so it is left out), and every frozen message must arrive at the subscriber, in order
per device. Duplicates are allowed (QoS 1 resends after a reconnect) and counted.

Skipped with a reason when neither a ``mosquitto`` binary nor Docker on Linux is available,
unless OPENCAP_REQUIRE_MOSQUITTO is set (CI sets it on its Linux acceptance jobs).
`--engine=rust` runs the same scenario against the Rust engine (tests/helpers/engine.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from opencap.logsetup import configure_logging
from tests.helpers import mosquitto
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import make_sim_devices, start_sims, stop_sims
from tests.helpers.netcut import stable_port
from tests.integration.test_phase1_acceptance import _is_subsequence, _wait

N_DEVICES = 10
TAGS_PER_DEVICE = 10
INTERVAL_MS = 1000
SCAN_MS = 500
RESTARTS = 3
DOWN_S = float(os.environ.get("OPENCAP_MOSQUITTO_DOWN_S", "2"))
BETWEEN_S = 4.0                       # publishing time between restarts

_MODE = mosquitto.available()
pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.drivers("modbus_tcp"),
    pytest.mark.skipif(_MODE is None and not mosquitto.required(),
                       reason="needs Mosquitto: the mosquitto binary, or Docker on Linux (#8)"),
]


async def test_broker_restarts_lose_no_messages(tmp_path, engine_factory):
    configure_logging("WARNING")
    if _MODE is None:
        pytest.fail(f"{mosquitto.REQUIRE_ENV} is set but neither the mosquitto binary nor Docker is usable")
    async with asyncio.timeout(300):
        broker = mosquitto.Mosquitto(tmp_path / "mosquitto", mode=_MODE)
        await broker.start()
        try:
            sims = await start_sims(make_sim_devices(N_DEVICES, TAGS_PER_DEVICE, scan_ms=SCAN_MS, timeout_ms=1000))
            try:
                async with mosquitto.PersistentSubscriber(broker.host, broker.port, ["opencap/+/+/data"],
                                                          client_id="opencap-restart-subscriber") as sub:
                    devices = [s.device for s in sims]
                    config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, api_port=stable_port(),
                                              publish={"interval_ms": INTERVAL_MS})
                    poller = engine_factory(config_dir)
                    await poller.start()
                    try:
                        await _scenario(broker, sub, poller, devices)
                    finally:
                        await poller.stop()
            finally:
                await stop_sims(sims)
        except BaseException:
            print(f"\n[mosquitto] broker log:\n{broker.log_text()[-4000:]}")
            raise
        finally:
            await broker.close()


async def _scenario(broker: mosquitto.Mosquitto, sub: mosquitto.PersistentSubscriber, poller, devices) -> None:
    sent = poller.sent                                   # everything the publisher hands to the link
    loaded = {d.id: d for c in poller.loaded.channels for d in c.devices}
    data_topics = [loaded[d.id].publish.topic for d in devices]
    assert len(set(data_topics)) == N_DEVICES and all(t.endswith("/data") for t in data_topics)
    await _wait(lambda: all(len(sub.payloads(t)) >= 2 for t in data_topics), 30, "2 messages per device")

    def produced() -> int:
        return sum(len(sent[t]) for t in data_topics)

    queued_while_down, held_for_away = [], []
    timeline: list[str] = []                             # for the failure report

    def mark(event: str) -> None:
        timeline.append(f"{time.strftime('%H:%M:%S', time.gmtime())}.{int(time.time() * 1000) % 1000:03d} "
                        f"{event} (produced {produced()})")
    for i in range(1, RESTARTS + 1):
        # Restart 1 with the subscriber connected (in-flight delivery). Restarts 2 and 3 while it
        # is away: the broker acknowledges what the poller publishes (so the outbox deletes it)
        # and queues it for the subscriber's session, then restarts holding it. Without
        # persistence that is exactly what a restart loses.
        away = i > 1
        if away:
            await sub.pause()
            away_from = produced()
            mark(f"subscriber away before restart #{i}")
        await asyncio.sleep(BETWEEN_S)
        mark(f"broker stop #{i}")
        await broker.stop()
        assert broker.db_path.exists(), "Mosquitto wrote no persistence file on a clean stop"
        await _wait(lambda: not poller.link.connected, 15, f"the link notices broker stop #{i}")
        await asyncio.sleep(DOWN_S)
        queued_while_down.append(poller.saf.count())
        await broker.start()
        mark(f"broker started #{i}")
        await _wait(lambda: poller.link.connected and poller.saf.count() == 0, 60, f"outbox drained after restart #{i}")
        mark(f"outbox drained #{i}")
        if away:
            held_for_away.append(produced() - away_from)
            sub.resume()
            mark(f"subscriber back after restart #{i}")
        await sub.wait_connected(i + 1)
    assert sum(queued_while_down) > 0, "the outbox never held a message while the broker was down"
    assert all(n > 0 for n in held_for_away), held_for_away

    # Freeze what is checked: everything produced up to now, from the first message on. The
    # poller keeps publishing every interval, so the newest batch is always in transit.
    checked = {t: list(sent[t]) for t in data_topics}
    total_checked = sum(len(v) for v in checked.values())
    assert total_checked >= N_DEVICES * (RESTARTS * BETWEEN_S - 2) * 1000 / INTERVAL_MS, total_checked

    def arrived(t: str) -> bool:
        return _is_subsequence(checked[t], sub.payloads(t))

    try:
        await _wait(lambda: all(arrived(t) for t in data_topics), 60, "every checked message at the subscriber",
                    step=0.5)
    except AssertionError:
        pass                                             # reported precisely below

    for t in data_topics:
        unknown = set(sub.payloads(t)) - set(sent[t])
        assert not unknown, f"{t}: {len(unknown)} message(s) that were never sent"
    missing = {t: len(set(checked[t]) - set(sub.payloads(t))) for t in data_topics}
    out_of_order = [t for t in data_topics if not missing[t] and not arrived(t)]
    if any(missing.values()):
        got = {t: set(sub.payloads(t)) for t in data_topics}
        detail = [f"{t}: message {k + 1} of {len(checked[t])} (DateTime {json.loads(p).get('DateTime')})"
                  for t in data_topics for k, p in enumerate(checked[t]) if p not in got[t]][:10]
        raise AssertionError("lost across broker restarts: " + "; ".join(detail)
                             + "\ntimeline (UTC):\n  " + "\n  ".join(timeline))
    assert not out_of_order, f"every message arrived, but not in order, on {out_of_order}"

    dupes = {t: len(sub.payloads(t)) - len(set(sub.payloads(t))) for t in data_topics}
    print(f"\n[mosquitto] {broker.mode}: {RESTARTS} restarts ({DOWN_S:g} s down each), "
          f"queued in the outbox while down={queued_while_down}, "
          f"held by the broker for the away subscriber across restarts 2-{RESTARTS}={held_for_away}, "
          f"data messages checked={total_checked}, "
          f"subscriber connects={sub.connects}, all arrived in order, lost=0, "
          f"duplicates={sum(dupes.values())} (per device: {sorted(dupes.values())})")
