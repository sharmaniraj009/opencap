"""The Rust engine's Sparkplug B edge node (#51, docs/spec/payload-sparkplug.md) against a broker.

A Modbus simulator is polled by the Rust engine; its Sparkplug edge node publishes to the
in-process amqtt broker; this test plays the primary host (STATE, NCMD) and decodes everything
with Google's protobuf runtime (tests/helpers/sparkplug_ref.py):
- no births while the primary host is not online; NBIRTH (seq 0, bdSeq, Rebirth false) and
  DBIRTH (every tag: name, alias, datatype, value, Quality) once its STATE says online;
- DDATA carry aliases only, and only the changes; `seq` runs on without gaps within a session;
- NCMD Node Control/Rebirth repeats the births with the same bdSeq; any other NCMD and every
  DCMD is rejected, counted, and writes nothing;
- host offline: NDEATH, a new session with the next bdSeq that waits; the changes made meanwhile
  arrive after the next births as historical DDATA, all of them, in order (no loss);
- a powered-off PLC gets DDEATH and DBIRTH when it is back; a stopped engine sends NDEATH.

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

pytest.importorskip("google.protobuf")
aiomqtt = pytest.importorskip("aiomqtt")

from tests.helpers import sparkplug_ref as ref  # noqa: E402
from tests.helpers.broker import TestBroker  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.engine import RustEngine, rust_binary  # noqa: E402
from tests.helpers.modbus_sim import ModbusSim, free_port, make_sim_devices  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402

BIN = rust_binary()
pytestmark = pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)")

GROUP, EDGE, HOST = "Plant A", "opencap-1", "scada"
NS = f"spBv1.0/{GROUP}"


def _decode(m):
    return ref.payload_class().FromString(m.payload)


def _kind(m) -> str:
    return m.topic.split("/")[2]


async def _wait(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.05)


async def _publish(broker: TestBroker, topic: str, payload: bytes, *, retain: bool = False) -> None:
    async with aiomqtt.Client(broker.host, broker.port, identifier=f"host-{time.monotonic_ns()}") as c:
        await c.publish(topic, payload, qos=1, retain=retain)


def _state(online: bool, ts: int) -> bytes:
    return json.dumps({"online": online, "timestamp": ts}).encode()


def _cmd(metrics: list[dict]) -> bytes:
    p = ref.payload_class()(timestamp=int(time.time() * 1000))
    for m in metrics:
        p.metrics.add(**m)
    return p.SerializeToString()


def _value(metric):
    field = metric.WhichOneof("value")
    return None if field is None else getattr(metric, field)


def _sessions(msgs) -> list[list]:
    """The edge node's messages grouped by session (from each NBIRTH up to the next); NDEATH, and
    what this test publishes as the host (STATE, NCMD, DCMD), left out."""
    out: list[list] = []
    for m in msgs:
        if _kind(m) == "NBIRTH":
            out.append([])
        if _kind(m) in ("NBIRTH", "NDATA", "DBIRTH", "DDATA", "DDEATH") and out:
            out[-1].append(m)
    return out


async def test_edge_node_session(tmp_path):
    device = make_sim_devices(1, 8, seed=51, scan_ms=200, timeout_ms=300)[0]
    sim = await ModbusSim.start(device.model_copy(update={"port": stable_port()}))
    device = sim.device
    sp_device = f"{device.channel}.{device.name}"
    try:
        async with asyncio.timeout(120), TestBroker() as broker, broker.collector("spBv1.0/#") as col:
            await col.wait_ready()
            config_dir = write_config(tmp_path, [device], mqtt_port=broker.port, api_port=free_port(), poller={
                "sparkplug": {"enabled": True, "group_id": GROUP, "edge_node_id": EDGE, "primary_host_id": HOST,
                              "outbox": str(tmp_path / "spb.sqlite")}})
            engine = RustEngine(config_dir, BIN, tmp_path / "rust")
            await engine.start()
            try:
                await _scenario(broker, col, engine, sim, device, sp_device)
            finally:
                await engine.stop()
    finally:
        await sim.stop()


async def _scenario(broker, col, engine, sim, device, sp_device) -> None:
    msgs = lambda kind: [m for m in list(col.messages) if _kind(m) == kind]  # noqa: E731

    # 1. No births before the primary host is online; then NBIRTH + DBIRTH.
    await _wait(lambda: all(v["quality"] == "good" for v in engine.device_values(device.id).values()), 10,
                "values good")
    await asyncio.sleep(1.5)        # bounded observation: the node is connected and waiting
    assert msgs("NBIRTH") == [] and msgs("DBIRTH") == []
    await _publish(broker, f"spBv1.0/STATE/{HOST}", _state(True, 1000), retain=True)
    await _wait(lambda: msgs("DBIRTH"), 10, "DBIRTH")
    nb, db = _decode(msgs("NBIRTH")[0]), _decode(msgs("DBIRTH")[0])
    assert msgs("NBIRTH")[0].retain is False     # (amqtt delivers at the subscriber's QoS)
    node = {m.name: m for m in nb.metrics}
    assert nb.seq == 0 and node["bdSeq"].long_value == 0 and node["bdSeq"].datatype == ref.dt("Int64")
    assert node["Node Control/Rebirth"].boolean_value is False and node["Node Info/Version"].string_value
    assert msgs("DBIRTH")[0].topic == f"{NS}/DBIRTH/{EDGE}/{sp_device}" and db.seq == 1
    values = engine.device_values(device.id)
    births = {m.name: m for m in db.metrics}
    assert set(births) == {ref.metric_name(t.name) for t in device.tags if t.enabled}
    aliases = {m.alias for m in nb.metrics} | {m.alias for m in db.metrics}
    assert len(aliases) == len(nb.metrics) + len(db.metrics), "aliases unique across the node"
    for t in device.tags:
        m = births[ref.metric_name(t.name)]
        props = dict(zip(m.properties.keys, [_value(v) for v in m.properties.values]))
        assert props["Quality"] == 192, t.name
        want = values[t.name]["value"]
        got = _value(m)
        if t.dtype == "float32":
            assert got == pytest.approx(want, rel=1e-6), t.name
        elif t.dtype in ("int16", "int32") and want < 0:
            assert got == want % (1 << (16 if t.dtype == "int16" else 32)), t.name
        else:
            assert got == want, t.name
    alias_of = {m.name: m.alias for m in db.metrics}

    # 2. A change: DDATA with the alias only.
    num = next(t for t in device.tags if t.dtype == "uint16")
    sim.set_value(num.name, 4321)
    await _wait(lambda: any(_value(m) == 4321 for d in msgs("DDATA") for m in _decode(d).metrics), 10, "DDATA")
    dd = [_decode(d) for d in msgs("DDATA")]
    for p in dd:
        for m in p.metrics:
            assert not m.HasField("name") and not m.HasField("datatype") and m.alias in aliases
    assert any(m.alias == alias_of[ref.metric_name(num.name)] for p in dd for m in p.metrics)

    # 3. Rebirth on request; every other command rejected, nothing written.
    n_births = len(msgs("NBIRTH"))
    before = sim.expected()
    await _publish(broker, f"{NS}/NCMD/{EDGE}", _cmd([{"name": "Node Control/Next Server", "boolean_value": True}]))
    await _publish(broker, f"{NS}/DCMD/{EDGE}/{sp_device}",
                   _cmd([{"alias": alias_of[ref.metric_name(num.name)], "int_value": 7}]))
    await _publish(broker, f"{NS}/NCMD/{EDGE}", _cmd([{"name": "Node Control/Rebirth", "boolean_value": True}]))
    await _wait(lambda: len(msgs("NBIRTH")) == n_births + 1 and len(msgs("DBIRTH")) >= 2, 10, "rebirth")
    again = _decode(msgs("NBIRTH")[-1])
    assert again.seq == 0 and {m.name: m for m in again.metrics}["bdSeq"].long_value == 0, "same bdSeq"
    assert sim.expected() == before, "a command reached the PLC"
    metrics = httpx.get(f"{engine.base_url}/metrics", timeout=5).text
    rejected = sum(float(ln.rsplit(" ", 1)[1]) for ln in metrics.splitlines()
                   if ln.startswith('sparkplug_commands_rejected_total{agent="sparkplug",reason="read_only"}'))
    assert rejected == 2, metrics

    # 4. Host offline: NDEATH, a new waiting session; changes meanwhile are held and replayed.
    await _publish(broker, f"spBv1.0/STATE/{HOST}", _state(False, 2000), retain=True)
    await _wait(lambda: msgs("NDEATH"), 10, "NDEATH")
    death = msgs("NDEATH")[0]
    assert _decode(death).metrics[0].long_value == 0 and not _decode(death).HasField("seq")
    held = []
    for v in (1001, 1002, 1003):
        sim.set_value(num.name, v)
        await _wait(lambda v=v: engine.device_values(device.id)[num.name]["value"] == v, 10, f"engine sees {v}")
        held.append(v)
    n_births = len(msgs("NBIRTH"))
    await _publish(broker, f"spBv1.0/STATE/{HOST}", _state(True, 3000), retain=True)
    await _wait(lambda: len(msgs("NBIRTH")) > n_births, 30, "births after the host is back")
    nb2 = _decode(msgs("NBIRTH")[-1])
    assert {m.name: m for m in nb2.metrics}["bdSeq"].long_value >= 1, "a new CONNECT, a new bdSeq"
    alias = alias_of[ref.metric_name(num.name)]
    def replay() -> list:
        """Historical values of `num` in the session that began with the latest NBIRTH."""
        session = _sessions(list(col.messages))[-1]
        return [_value(m) for d in session if _kind(d) == "DDATA" for m in _decode(d).metrics
                if m.alias == alias and m.is_historical]
    await _wait(lambda: replay() == held, 10, "the held changes, replayed as historical data")

    # 5. seq has no gaps within any session.
    for s in _sessions(list(col.messages)):
        seqs = [_decode(m).seq for m in s]
        assert seqs == [i % 256 for i in range(len(seqs))], seqs

    # 6. A powered-off PLC: DDEATH; back: DBIRTH.
    n_db = len(msgs("DBIRTH"))
    await sim.stop()
    await _wait(lambda: msgs("DDEATH"), 20, "DDEATH")
    await sim.restart()
    await _wait(lambda: len(msgs("DBIRTH")) > n_db, 20, "DBIRTH after power-on")

    # 7. Stopping the engine sends NDEATH for the current session.
    n_death = len(msgs("NDEATH"))
    await engine.stop()
    await _wait(lambda: len(msgs("NDEATH")) > n_death, 10, "NDEATH at shutdown")
    assert not any(m.retain for m in col.messages if not m.topic.startswith("spBv1.0/STATE/"))
