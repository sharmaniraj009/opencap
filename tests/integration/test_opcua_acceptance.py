"""OPC UA end to end in simulation (PLAN Phase 4 opcua_client + Phase 6 OPC UA server).

Two asyncua servers stand in for the project's two OPC UA devices (one anonymous, one with a
user name and password from the environment). The poller reads them through the
`opcua_client` driver (subscriptions), publishes SubDevice JSON to MQTT, and serves the same
values through its own OPC UA server, where a real asyncua client checks them:

- MQTT `Data` and the opencap OPC UA server both equal the simulated values;
- changes arrive through subscriptions (no polling: no tag node is read after connect);
- unchanged values stay good (D-028: no stale tags while nothing changes);
- a NodeId the server does not have is BadTags / BadConfigurationError; an Uncertain status is
  BadTags / UncertainLastUsableValue;
- power-cycling one server takes only that device down (`_System._Error`), and after power-on
  it is resubscribed and its values flow again;
- nothing writes to the simulated servers, and writes to opencap's server are refused.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import pytest

pytest.importorskip("asyncua")

from asyncua import Client, ua  # noqa: E402

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.core.runtime import Poller  # noqa: E402
from opencap.logsetup import configure_logging  # noqa: E402
from tests.helpers.broker import TestBroker  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402
from tests.helpers.opcua_sim import OpcUaSim  # noqa: E402

pytestmark = pytest.mark.acceptance

INTERVAL_MS = 1000
USER_ENV, PASS_ENV = "OPENCAP_OPCUA_UA_SITE_2_USER", "OPENCAP_OPCUA_UA_SITE_2_PASSWORD"
MISSING = "LTI009.Pv"

SPECS: dict[str, tuple[str, str, Any]] = {          # tag name -> (sim node, Variant type, value)
    "LTI002.Pv": ("LTI002_PV", "Double", 2.75),
    "LTI002.Qv": ("LTI002_QV", "Float", 0.5),
    "Tags.Dev 1.Values.Angle": ("ANGLE", "Double", 12.25),
    "Tags.Dev 1.Values.Battery": ("BATTERY", "Double", 98.5),
    "PUMP_1.RUN": ("PUMP_1_RUN", "Boolean", True),
    "PUMP_1.HOURS": ("PUMP_1_HOURS", "UInt32", 41234),
    "PUMP_1.STARTS": ("PUMP_1_STARTS", "Int16", 17),
    "MODE": ("MODE", "String", "AUTO"),
}
DTYPE = {"Double": "float64", "Float": "float32", "Boolean": "bool", "UInt32": "uint32", "Int16": "int16",
         "String": "string"}


def _device(site: str, sim: OpcUaSim, *, missing: bool, auth: bool) -> Device:
    tags = [Tag(name=name, address=sim.node_id(node), dtype=DTYPE[vt]) for name, (node, vt, _v) in SPECS.items()]
    if missing:
        tags.append(Tag(name=MISSING, address=sim.node_id("LTI009_PV"), dtype="float64"))
    params: dict[str, Any] = {           # the keys `opencap import-kep` writes (docs/driver_params.md)
        "endpoint": sim.endpoint, "security_policy": "None", "message_mode": "None",
        "username_env": USER_ENV if auth else None, "password_env": PASS_ENV if auth else None,
        "publishing_interval_ms": 500, "sampling_interval_ms": 250,
    }
    return Device(id=f"{site}/E+H", channel=site, name="E+H", driver="opcua_client", host="127.0.0.1",
                  port=sim.port, params=params, scan_ms=500, min_scan_ms=500, timeout_ms=1000, retries=1,
                  demote_after=2, demote_for_ms=1500, tags=tags)


async def _wait(pred, timeout: float, what: str, step: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = pred()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(step)


def _same(got: Any, want: Any) -> bool:
    if isinstance(want, float):
        return got is not None and math.isclose(float(got), round(want, 4), rel_tol=1e-6, abs_tol=1e-4)
    return got == want


async def test_opcua_end_to_end(tmp_path, monkeypatch):
    configure_logging("WARNING")
    monkeypatch.setenv(USER_ENV, "scada-reader")
    monkeypatch.setenv(PASS_ENV, "pw-only-in-env")
    nodes = {node: (vt, v) for node, vt, v in SPECS.values()}
    sims = [await OpcUaSim.start(dict(nodes)),
            await OpcUaSim.start(dict(nodes), users={"scada-reader": "pw-only-in-env"}, anonymous=False)]
    devices = [_device("UA SITE 1", sims[0], missing=True, auth=False),
               _device("UA SITE 2", sims[1], missing=False, auth=True)]
    endpoint = f"opc.tcp://127.0.0.1:{stable_port()}/opencap"
    try:
        async with asyncio.timeout(120), TestBroker() as broker, broker.collector("opencap/#") as col:
            await col.wait_ready()
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, publish={"interval_ms": INTERVAL_MS},
                                      poller={"opcua_server": {"enabled": True, "endpoint": endpoint,
                                                               "update_ms": 100}})
            poller = Poller(config_dir, serve_api=False)
            await poller.start()
            try:
                async with Client(endpoint, timeout=3) as north:
                    await _scenario(poller, col, sims, devices, north)
            finally:
                await poller.stop()
        assert [s.write_attempts for s in sims] == [0, 0], "opencap must never write to an OPC UA server"
    finally:
        await asyncio.gather(*(s.close() for s in sims), return_exceptions=True)


async def _scenario(poller: Poller, col, sims: list[OpcUaSim], devices: list[Device], north: Client) -> None:
    topics = {d.id: poller.registry.plans[d.id].device.publish.topic for d in devices}
    ids = [d.id for d in devices]

    async def served(dev: Device, name: str) -> ua.DataValue:
        return await north.get_node(poller.opcua.node_id(dev.id, name)).read_data_value(raise_on_bad_status=False)

    def mqtt(dev: Device) -> dict:
        return col.last(topics[dev.id]).json()

    def sim_value(sim: OpcUaSim, name: str) -> Any:
        return sim.value(SPECS[name][0])

    # 1. MQTT and opencap's OPC UA server both carry the simulated values.
    await _wait(lambda: all(len(col.topic(t)) >= 2 for t in topics.values()), 30, "2 messages per device")
    for dev, sim in zip(devices, sims):
        msg = mqtt(dev)
        want_bad = [MISSING] if any(t.name == MISSING for t in dev.tags) else []
        assert msg.get("BadTags", []) == want_bad, (dev.id, msg.get("BadTags"))
        for name in SPECS:
            assert _same(msg["Data"][name], sim_value(sim, name)), (dev.id, name, msg["Data"][name])
            dv = await served(dev, name)
            assert dv.StatusCode.is_good(), (dev.id, name, dv.StatusCode)
            assert _same(dv.Value.Value, sim_value(sim, name)), (dev.id, name, dv.Value.Value)
        assert (await served(dev, "_System._State")).Value.Value == "ok"
    missing = await served(devices[0], MISSING)
    assert missing.StatusCode.value == ua.StatusCodes.BadConfigurationError
    pv = await north.get_node(f"ns=2;s=UA SITE 1.E+H.LTI002.Pv").read_value()      # KEPServer-style path
    assert _same(pv, 2.75)
    assert sims[1].user_manager.logins == ["scada-reader"]

    # 2. Changes arrive through subscriptions; no tag is read after the connect-time Read.
    for s in sims:
        s.reset_counters()
    await sims[0].set("LTI002_PV", 3.5)
    await sims[1].set("PUMP_1_STARTS", 18)
    await _wait(lambda: _same(mqtt(devices[0])["Data"]["LTI002.Pv"], 3.5), 5, "MQTT LTI002.Pv = 3.5")
    await _wait(lambda: mqtt(devices[1])["Data"]["PUMP_1.STARTS"] == 18, 5, "MQTT PUMP_1.STARTS = 18")
    await _wait(lambda: _dv_is(served(devices[0], "LTI002.Pv"), 3.5), 5, "served LTI002.Pv = 3.5")
    assert [s.tag_reads for s in sims] == [0, 0] and [s.monitored_items for s in sims] == [0, 0]

    # 3. Nothing changes for 3 s (6 x the 500 ms rate): every tag stays good (no BadTags).
    col.clear()
    await asyncio.sleep(3.0)
    for dev in devices:
        bad = {tuple(m.json().get("BadTags", [])) for m in col.topic(topics[dev.id])}
        assert bad == {(MISSING,)} if dev is devices[0] else bad == {()}, (dev.id, bad)

    # 4. Uncertain upstream: BadTags in MQTT, UncertainLastUsableValue downstream, last good value kept.
    await sims[1].set("BATTERY", 97.0, status="UncertainSensorNotAccurate")
    await _wait(lambda: "Tags.Dev 1.Values.Battery" in mqtt(devices[1]).get("BadTags", []), 5, "uncertain in BadTags")
    dv = await served(devices[1], "Tags.Dev 1.Values.Battery")
    assert dv.StatusCode.value == ua.StatusCodes.UncertainLastUsableValue and _same(dv.Value.Value, 98.5)
    await sims[1].set("BATTERY", 97.0)
    await _wait(lambda: "BadTags" not in mqtt(devices[1]), 5, "good again")

    # 5. Power-cycle site 1: only it goes down; after power-on it resubscribes and reads again.
    await sims[0].stop()
    await _wait(lambda: poller.device_detail(ids[0])["state"] in ("offline", "demoted"), 10, "site 1 down")
    await _wait(lambda: _dv_is(served(devices[0], "_System._Error"), True), 5, "_System._Error")
    assert poller.device_detail(ids[1])["state"] == "ok"
    assert (await served(devices[0], "LTI002.Qv")).StatusCode.value == ua.StatusCodes.BadCommunicationError
    sims[0].reset_counters()
    await sims[0].restart()
    await sims[0].set("ANGLE", 45.5)
    await _wait(lambda: poller.device_detail(ids[0])["state"] == "ok", 20, "site 1 ok again")
    await _wait(lambda: _same(mqtt(devices[0])["Data"].get("Tags.Dev 1.Values.Angle"), 45.5), 5, "MQTT after restart")
    await _wait(lambda: _dv_is(served(devices[0], "Tags.Dev 1.Values.Angle"), 45.5), 5, "served after restart")
    assert sims[0].monitored_items >= len(SPECS)                     # subscribed again
    assert (await served(devices[0], "_System._Error")).Value.Value is False

    # 6. Read-only northbound.
    with pytest.raises(ua.UaStatusCodeError):
        await north.get_node(poller.opcua.node_id(ids[0], "LTI002.Pv")).write_value(
            ua.DataValue(ua.Variant(0.0, ua.VariantType.Double)))
    print(f"\n[opcua] values verified on MQTT and on {poller.opcua.cfg.endpoint}; "
          f"site 1 resubscribed {sims[0].monitored_items} monitored items after power-on")


async def _dv_is(coro, want) -> bool:
    dv = await coro
    return dv.StatusCode.is_good() and _same(dv.Value.Value, want)
