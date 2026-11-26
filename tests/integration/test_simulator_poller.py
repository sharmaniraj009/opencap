"""The Phase 1 acceptance shape with the simulator driver (#12): 50 simulator devices through the
whole Python poller (workers, cache, publisher, MQTT link, admin API), and no socket except the
ones to the broker and the API.

- every device publishes valid SubDevice payloads, about one per interval;
- every value the API reports is exactly what the documented formula gives at that value's
  `source_ts` (the driver stamps the value with the instant it computed it);
- a fault channel shows the other qualities: `bad_config` (rejected address), `bad_decode`
  (`bad_decode_tags`) and `bad_comm` / demotion (`fail_every_n_reads: 1`).

The Python engine only: the socket check patches this process, and the Rust engine has no
simulator yet (follow-up issue linked from #12).
"""

from __future__ import annotations

import asyncio
import socket
import threading
from datetime import datetime

import httpx
import pytest

from opencap.config.models import Device, Tag
from opencap.drivers.simulator import SimParams, compute, parse_address
from opencap.logsetup import configure_logging
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.engine import PythonEngine
from tests.helpers.modbus_sim import free_port

N_DEVICES = 50
CHANNELS = 5
INTERVAL_MS = 1000
SCAN_MS = 250
API_TOKEN = "simulator-integration-token"

# (name, address, dtype); K1 is set per device through params.memory
TAGS = [
    ("Ramp", "RAMP(250, 0, 1000, 5)", "int32"),
    ("RampDown", "RAMP(500, -10, 10, -0.5)", "float64"),
    ("Sine", "SINE(10000, -50, 50, 30)", "float64"),
    ("SineInt", "SINE(5000, 0, 4000)", "uint16"),
    ("Random", "RANDOM(250, 1, 6, {seed})", "int16"),
    ("Noise", "RANDOM(250, -1.5, 1.5, {seed})", "float32"),
    ("Square", "SQUARE(1000, 0, 1)", "bool"),
    ("Setpoint", "CONST(72.5)", "float32"),
    ("Label", 'CONST("Line {n}")', "string"),
    ("Memory", "K1", "int32"),
    ("Spare", "K2", "uint16"),
    ("Clock", "TIME", "string"),
    ("Count", "COUNTER", "uint32"),
]


def sim_device(i: int) -> Device:
    channel = f"Sim {i % CHANNELS}"
    tags = [Tag(name=name, address=addr.format(seed=i, n=i), dtype=dtype) for name, addr, dtype in TAGS]
    return Device(id=f"{channel}/Device {i:02d}", channel=channel, name=f"Device {i:02d}", driver="simulator",
                  host="sim", port=0, scan_ms=SCAN_MS, min_scan_ms=SCAN_MS, timeout_ms=500, retries=1,
                  params={"memory": {"K1": 1000 + i}}, tags=tags)


def fault_devices() -> list[Device]:
    bad = Device(id="Faults/Tags", channel="Faults", name="Tags", driver="simulator", host="sim", port=0,
                 scan_ms=SCAN_MS, min_scan_ms=SCAN_MS, timeout_ms=500,
                 params={"bad_decode_tags": ["Broken"]},
                 tags=[Tag(name="Fine", address="CONST(1)", dtype="int16"),
                       Tag(name="Broken", address="CONST(2)", dtype="int16"),
                       Tag(name="Rejected", address="RAMP(1000, 10, 0)", dtype="int16")])
    dead = Device(id="Faults/Comm", channel="Faults", name="Comm", driver="simulator", host="sim", port=0,
                  scan_ms=SCAN_MS, min_scan_ms=SCAN_MS, timeout_ms=500, retries=1, demote_after=2,
                  params={"fail_every_n_reads": 1},
                  tags=[Tag(name="Value", address="CONST(5)", dtype="int16")])
    return [bad, dead]


async def _wait(pred, timeout: float, what: str, step: float = 0.05) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(step)


@pytest.fixture
def outbound(monkeypatch):
    """Every address this process connects a socket to (TCP or UDP), from now on."""
    peers: list[object] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_socketpair = socket.socketpair
    pairing = threading.local()

    def socketpair(*args, **kwargs):
        # Windows emulates socketpair() (the event loop's wake-up pipe) with a loopback TCP
        # connect inside this process: not a peer the engine talks to, so it is not recorded.
        pairing.active = True
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            pairing.active = False

    def connect(self, address):
        if not getattr(pairing, "active", False):
            peers.append(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if not getattr(pairing, "active", False):
            peers.append(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    return peers


async def test_fifty_simulator_devices_through_the_poller(tmp_path, monkeypatch, outbound, no_gc):
    monkeypatch.setenv("OPENCAP_API_TOKEN", API_TOKEN)
    configure_logging("WARNING")
    devices = [sim_device(i) for i in range(N_DEVICES)]
    faults = fault_devices()
    api_port = free_port()
    async with asyncio.timeout(90), TestBroker() as broker:
        config_dir = write_config(tmp_path, devices + faults, mqtt_port=broker.port, api_port=api_port,
                                  publish={"interval_ms": INTERVAL_MS})
        async with broker.collector("opencap/#") as col:
            await col.wait_ready()
            engine = PythonEngine(config_dir)
            await engine.start()
            try:
                loaded = {d.id: d for c in engine.loaded.channels for d in c.devices}
                topics = {d.id: loaded[d.id].publish.topic for d in devices}
                await _check_publishing(col, topics)
                await _check_values_match_the_formulas(engine, devices)
                await _check_api_and_faults(engine, api_port, devices)
            finally:
                await engine.stop()
    allowed = {broker.port, api_port}
    stray = [p for p in outbound if not (isinstance(p, tuple) and p[1] in allowed)]
    assert outbound and not stray, f"sockets to something other than the broker or the API: {stray}"


async def _check_publishing(col, topics) -> None:
    await _wait(lambda: all(len(col.topic(t)) >= 2 for t in topics.values()), 20, "2 messages per device")
    start = {t: len(col.topic(t)) for t in topics.values()}
    await asyncio.sleep(3 * INTERVAL_MS / 1000)
    for dev_id, t in topics.items():
        assert abs(len(col.topic(t)) - start[t] - 3) <= 1, (dev_id, len(col.topic(t)) - start[t])
    for i, t in enumerate(topics.values()):
        msg = col.last(t).json()
        assert msg["DataValid"] is True, msg
        data = msg["Data"]
        assert set(data) == {name for name, _, _ in TAGS}
        assert data["Setpoint"] == 72.5 and data["Label"] == f"Line {i}" and data["Memory"] == 1000 + i
        assert data["Spare"] == 0 and isinstance(data["Square"], bool)
        assert 1 <= data["Random"] <= 6 and -1.5 <= data["Noise"] <= 1.5 and -50 <= data["Sine"] <= 50
        assert data["Ramp"] % 5 == 0 and 0 <= data["Ramp"] <= 1000
        assert data["Clock"].endswith("Z") and data["Count"] >= 2
        # a first message can predate the first read and carry no tags; the counter only counts reads
        counts = [m.json()["Data"]["Count"] for m in col.topic(t) if "Count" in m.json().get("Data", {})]
        assert counts == sorted(counts), counts                      # one COUNTER per device, increasing


async def _check_values_match_the_formulas(engine, devices) -> None:
    """The API's value of every tag equals the formula at its `source_ts` (exact, not approximate)."""
    checked = 0
    for dev in devices:
        params = SimParams.from_mapping(dev.params)
        values = engine.device_values(dev.id)
        for tag in dev.tags:
            tv = values[tag.name]
            assert tv["quality"] == "good", (dev.id, tag.name, tv)
            if tag.address == "COUNTER":
                continue
            at = datetime.fromisoformat(tv["source_ts"])
            want = compute(parse_address(tag.address, tag.dtype, params), tag.dtype, at, params.epoch_ms)
            assert tv["value"] == want, (dev.id, tag.name, tv, want)
            checked += 1
    assert checked == N_DEVICES * (len(TAGS) - 1)


async def _check_api_and_faults(engine, api_port, devices) -> None:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", timeout=10,
                                 headers={"Authorization": f"Bearer {API_TOKEN}"}) as http:
        health = (await http.get("/health")).json()
        assert health["mqtt_connected"] is True
        dev = devices[0].id
        detail = (await http.get(f"/api/v1/devices/{dev}/status")).json()
        assert detail["state"] == "ok" and detail["blocks"] == 1
        once = (await http.post(f"/api/v1/devices/{dev}/read-once")).json()
        assert set(once) == {name for name, _, _ in TAGS}
        assert all(v["quality"] == "good" for v in once.values())
        identity = (await http.get(f"/api/v1/devices/{dev}/identity")).json()
        assert identity["connected"] is True
        assert identity["identity"] == {"vendor": "opencap", "model": "simulator", "simulated": True}

        await _wait(lambda: engine.device_detail("Faults/Comm")["state"] == "demoted", 10, "Faults/Comm demoted")
        tags = (await http.get("/api/v1/devices/Faults/Tags/values")).json()
        assert {n: v["quality"] for n, v in tags.items()} == {
            "Fine": "good", "Broken": "bad_decode", "Rejected": "bad_config"}
        comm = (await http.get("/api/v1/devices/Faults/Comm/values")).json()
        assert comm["Value"]["quality"] in ("bad_comm", "stale"), comm
        health = (await http.get("/health")).json()
        assert health["devices"]["ok"] >= N_DEVICES and health["devices"]["demoted"] == 1, health
