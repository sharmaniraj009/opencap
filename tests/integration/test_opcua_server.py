"""Northbound OPC UA server (north/opcua_server.py) against a real asyncua client.

The client browses the KEPServer-style paths, reads values that match the cache, sees
updates through a subscription, gets the right StatusCode per quality, reads `_System`
diagnostics, and gets an error on every kind of write.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("asyncua")

from asyncua import Client, ua  # noqa: E402
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256  # noqa: E402

from opencap.config.models import Device, OpcUaServerCfg, Scale, Tag  # noqa: E402
from opencap.core.cache import TagValue, ValueCache  # noqa: E402
from opencap.north.opcua_server import OpcUaNorth  # noqa: E402
from tests.helpers.modbus_sim import free_port  # noqa: E402
from tests.helpers.opcua_sim import make_cert, shelf_file  # noqa: E402

T0 = datetime(2026, 11, 17, 6, 30, 0, 250000, tzinfo=UTC)


def site_devices() -> list[Device]:
    plc = Device(id="SITE 001/8 PUMP", channel="SITE 001", name="8 PUMP", driver="ab_logix", host="192.0.2.11",
                 port=44818, tags=[
                     Tag(name="FLOW_1", address="FLOW_1", dtype="float32"),
                     Tag(name="LTI002.Pv", address="A", dtype="float64"),
                     Tag(name="LTI002.Alarm.High", address="B", dtype="bool"),
                     Tag(name="COUNT", address="C", dtype="int16"),
                     Tag(name="TOTAL", address="D", dtype="uint32"),
                     Tag(name="MODE", address="E", dtype="string"),
                     Tag(name="LEVEL_PCT", address="F", dtype="int16", scale=Scale(raw_hi=100, eng_hi=10)),
                     Tag(name="PROFILE", address="G", dtype="float64"),
                     Tag(name="OFF", address="H", dtype="float64", enabled=False),
                 ])
    other = Device(id="SITE 002/Device1", channel="SITE 002", name="Device1", driver="modbus_tcp",
                   host="192.0.2.12", port=502, tags=[Tag(name="P1", address="400001", dtype="uint16")])
    return [plc, other]


GOOD_VALUES = {"FLOW_1": 12.5, "LTI002.Pv": 1.25, "LTI002.Alarm.High": True, "COUNT": -3, "TOTAL": 4000000000,
               "MODE": "AUTO", "LEVEL_PCT": 4.5, "PROFILE": [1.0, 2.0, 3.0]}


class Status:
    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}

    def __call__(self, device_id: str) -> dict[str, Any]:
        return self.by_id.get(device_id, {"state": "offline", "last_good": None, "errors": 0,
                                          "cycle_ms": None, "last_error": None, "connected": False})


async def make_north(devices: list[Device], cache: ValueCache, status: Status, **cfg: Any) -> OpcUaNorth:
    cfg.setdefault("endpoint", f"opc.tcp://127.0.0.1:{free_port()}/opencap")
    cfg.setdefault("anonymous", True)          # the tests' clients connect anonymously (default: off)
    north = OpcUaNorth(OpcUaServerCfg(enabled=True, update_ms=50, **cfg), devices, cache, status,
                       shelf_file=await shelf_file())
    await north.start()
    return north


@pytest.fixture
async def setup():
    devices = site_devices()
    cache = ValueCache()
    for d in devices:
        cache.register(d)
    cache.update("SITE 001/8 PUMP", {k: TagValue(v, "good", T0) for k, v in GOOD_VALUES.items()})
    status = Status()
    status.by_id["SITE 001/8 PUMP"] = {"state": "ok", "last_good": "2026-11-17T06:30:00.250Z", "errors": 2,
                                       "cycle_ms": 41.5, "last_error": None, "connected": True}
    north = await make_north(devices, cache, status)
    client = Client(north.cfg.endpoint, timeout=2)
    await client.connect()
    try:
        yield north, cache, status, client
    finally:
        await client.disconnect()
        await north.stop()


async def wait_for(pred, timeout: float = 3.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = pred()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.02)


def nid(text: str) -> ua.NodeId:
    return ua.NodeId.from_string(text)


async def data_value(client: Client, node: str) -> ua.DataValue:
    return await client.get_node(node).read_data_value(raise_on_bad_status=False)


# --- address space ------------------------------------------------------------------------------

async def test_browse_paths_are_kepserver_style(setup):
    north, _cache, _status, client = setup
    ns = await client.get_namespace_array()
    assert north.namespace_index == 2 and ns[2] == north.cfg.namespace_uri
    objects = client.nodes.objects
    names = {(await c.read_browse_name()).to_string() for c in await objects.get_children()}
    assert {"2:SITE 001", "2:SITE 002"} <= names
    pv = await objects.get_child(["2:SITE 001", "2:8 PUMP", "2:LTI002", "2:Pv"])
    assert pv.nodeid == nid("ns=2;s=SITE 001.8 PUMP.LTI002.Pv")
    high = await objects.get_child(["2:SITE 001", "2:8 PUMP", "2:LTI002", "2:Alarm", "2:High"])
    assert high.nodeid == nid("ns=2;s=SITE 001.8 PUMP.LTI002.Alarm.High")
    assert (await pv.read_display_name()).Text == "Pv"
    device = await objects.get_child(["2:SITE 001", "2:8 PUMP"])
    children = {(await c.read_browse_name()).Name for c in await device.get_children()}
    assert children == {"FLOW_1", "LTI002", "COUNT", "TOTAL", "MODE", "LEVEL_PCT", "PROFILE", "_System"}  # OFF hidden
    assert north.node_id("SITE 001/8 PUMP", "FLOW_1") == "ns=2;s=SITE 001.8 PUMP.FLOW_1"


async def test_values_types_and_timestamps_match_the_cache(setup):
    north, cache, _status, client = setup
    want_types = {"FLOW_1": ua.ObjectIds.Float, "LTI002.Pv": ua.ObjectIds.Double, "LTI002.Alarm.High":
                  ua.ObjectIds.Boolean, "COUNT": ua.ObjectIds.Int16, "TOTAL": ua.ObjectIds.UInt32,
                  "MODE": ua.ObjectIds.String, "LEVEL_PCT": ua.ObjectIds.Double, "PROFILE": ua.ObjectIds.Double}
    for name, want in GOOD_VALUES.items():
        node = north.node_id("SITE 001/8 PUMP", name)
        dv = await data_value(client, node)
        assert dv.StatusCode.is_good(), name
        assert dv.Value.Value == pytest.approx(want) if isinstance(want, float) else dv.Value.Value == want, name
        assert dv.SourceTimestamp == cache.get("SITE 001/8 PUMP", name).source_ts == T0
        assert (await client.get_node(node).read_data_type()) == ua.NodeId(want_types[name]), name
    assert (await client.get_node(north.node_id("SITE 001/8 PUMP", "PROFILE")).read_value_rank()) == 1


async def test_updates_reach_reads_and_subscriptions(setup):
    north, cache, _status, client = setup
    got: list[Any] = []

    class Handler:
        def datachange_notification(self, node, val, data):
            got.append(val)

    sub = await client.create_subscription(50, Handler())
    await sub.subscribe_data_change(client.get_node(north.node_id("SITE 001/8 PUMP", "FLOW_1")))
    await wait_for(lambda: got == [12.5], 2, "initial value")
    ts = T0 + timedelta(seconds=5)
    for v in (13.5, 14.5):
        cache.update("SITE 001/8 PUMP", {"FLOW_1": TagValue(v, "good", ts)})
        north.notify("SITE 001/8 PUMP")
        await wait_for(lambda v=v: got[-1] == v, 2, f"notification {v}")
    dv = await data_value(client, north.node_id("SITE 001/8 PUMP", "FLOW_1"))
    assert dv.Value.Value == 14.5 and dv.SourceTimestamp == ts
    cache.update("SITE 001/8 PUMP", {"COUNT": TagValue(9, "good", ts)})   # no notify: the update_ms tick
    await wait_for(lambda: _value(client, north.node_id("SITE 001/8 PUMP", "COUNT"), 9), 2, "periodic copy")


async def _value(client, node, want) -> bool:
    return (await data_value(client, node)).Value.Value == want


async def test_quality_maps_to_status_codes(setup):
    north, cache, _status, client = setup
    dev = "SITE 001/8 PUMP"
    cache.update(dev, {"FLOW_1": TagValue(None, "bad_comm", None), "COUNT": TagValue(None, "bad_config", None),
                       "TOTAL": TagValue(None, "bad_decode", None)})
    cache.expire(dev, T0 + timedelta(hours=1), {"MODE": 1.0})          # -> stale, last value kept
    north.notify(dev)
    codes = {"FLOW_1": "BadCommunicationError", "COUNT": "BadConfigurationError",
             "TOTAL": "BadDataEncodingInvalid", "MODE": "UncertainLastUsableValue", "LTI002.Pv": "Good"}

    async def all_mapped() -> bool:
        for name, code in codes.items():
            dv = await data_value(client, north.node_id(dev, name))
            if dv.StatusCode.value != getattr(ua.StatusCodes, code):
                return False
        return True

    await wait_for(all_mapped, 2, "status codes")
    assert (await data_value(client, north.node_id(dev, "MODE"))).Value.Value == "AUTO"   # last usable value
    never = await data_value(client, north.node_id("SITE 002/Device1", "P1"))             # never read
    assert never.StatusCode.value == ua.StatusCodes.BadWaitingForInitialData


async def test_every_write_is_refused(setup):
    north, _cache, _status, client = setup
    node = client.get_node(north.node_id("SITE 001/8 PUMP", "FLOW_1"))
    with pytest.raises(ua.UaStatusCodeError, match="BadUserAccessDenied"):
        await node.write_value(ua.DataValue(ua.Variant(99.0, ua.VariantType.Float)))
    with pytest.raises(ua.UaStatusCodeError):
        await node.write_attribute(ua.AttributeIds.DisplayName, ua.DataValue(ua.Variant(ua.LocalizedText("x"))))
    system = client.get_node(north.node_id("SITE 001/8 PUMP", "_System._Enabled"))
    with pytest.raises(ua.UaStatusCodeError):
        await system.write_value(False)
    with pytest.raises(ua.UaStatusCodeError):
        await client.nodes.objects.add_folder(ua.NodeId("ROGUE", 2), ua.QualifiedName("ROGUE", 2))
    with pytest.raises(ua.UaStatusCodeError):
        await client.delete_nodes([node])
    with pytest.raises(ua.UaStatusCodeError):
        await client.nodes.server.call_method(ua.NodeId(ua.ObjectIds.Server_GetMonitoredItems), ua.Variant(1, ua.VariantType.UInt32))
    assert (await node.read_value()) == 12.5
    assert (await node.read_attribute(ua.AttributeIds.UserAccessLevel)).Value.Value == ua.AccessLevel.CurrentRead.mask
    registered = await client.register_nodes([node])          # a read optimisation: allowed
    assert await registered[0].read_value() == 12.5


async def test_system_diagnostics(setup):
    north, _cache, status, client = setup
    dev = "SITE 001/8 PUMP"

    async def system() -> dict[str, Any]:
        out = {}
        for name in ("_State", "_Error", "_NoError", "_AutoDemoted", "_Connected", "_LastGood", "_ErrorCount",
                     "_LastError", "_CycleMs", "_DeviceId", "_Driver", "_Enabled"):
            out[name] = (await data_value(client, north.node_id(dev, f"_System.{name}"))).Value.Value
        return out

    s = await system()
    assert s == {"_State": "ok", "_Error": False, "_NoError": True, "_AutoDemoted": False, "_Connected": True,
                 "_LastGood": T0, "_ErrorCount": 2, "_LastError": "", "_CycleMs": 41.5,
                 "_DeviceId": "192.0.2.11:44818", "_Driver": "ab_logix", "_Enabled": True}
    status.by_id[dev] = {"state": "demoted", "last_good": "2026-11-17T06:30:00.250Z", "errors": 5,
                         "cycle_ms": 3000.0, "last_error": "connect failed: refused", "connected": False}
    await wait_for(lambda: _system_state(client, north, dev, "demoted"), 2, "diagnostics update")
    s = await system()
    assert (s["_Error"], s["_NoError"], s["_AutoDemoted"], s["_ErrorCount"]) == (True, False, True, 5)
    assert s["_LastError"] == "connect failed: refused"


async def _system_state(client, north, dev, want) -> bool:
    return (await data_value(client, north.node_id(dev, "_System._State"))).Value.Value == want


# --- reload -------------------------------------------------------------------------------------

async def test_set_devices_adds_removes_and_rebuilds(setup):
    north, cache, _status, client = setup
    plc, other = site_devices()
    changed_plc = plc.model_copy(update={"tags": [*plc.tags, Tag(name="NEW", address="N", dtype="int32")]})
    third = Device(id="SITE 003/PLC", channel="SITE 003", name="PLC", driver="modbus_tcp", host="192.0.2.13",
                   port=502, tags=[Tag(name="Q", address="400001", dtype="uint16")])
    cache.register(changed_plc)
    cache.register(third)
    cache.update(third.id, {"Q": TagValue(7, "good", T0)})
    changed = await north.set_devices([changed_plc, third])
    assert changed == {"added": [third.id], "removed": [other.id], "rebuilt": [plc.id], "unchanged": []}
    assert (await data_value(client, north.node_id(third.id, "Q"))).Value.Value == 7
    assert (await data_value(client, "ns=2;s=SITE 001.8 PUMP.NEW")).StatusCode.value == \
        ua.StatusCodes.BadWaitingForInitialData
    assert (await data_value(client, "ns=2;s=SITE 001.8 PUMP.FLOW_1")).Value.Value == 12.5
    gone = await data_value(client, "ns=2;s=SITE 002.Device1.P1")
    assert gone.StatusCode.value == ua.StatusCodes.BadNodeIdUnknown
    names = {(await c.read_browse_name()).Name for c in await client.nodes.objects.get_children()}
    assert "SITE 002" not in names and "SITE 003" in names
    assert (await north.set_devices([changed_plc, third]))["unchanged"] == [plc.id, third.id]


# --- security and users -------------------------------------------------------------------------

USER_ENV, PASS_ENV = "OPENCAP_UA_SERVER_TEST_USER", "OPENCAP_UA_SERVER_TEST_PASSWORD"


async def test_secure_endpoint_users_from_env_and_no_anonymous(tmp_path, monkeypatch):
    monkeypatch.setenv(USER_ENV, "scada")
    monkeypatch.setenv(PASS_ENV, "pw-for-test")
    devices = site_devices()
    cache = ValueCache()
    for d in devices:
        cache.register(d)
    cache.update(devices[0].id, {"FLOW_1": TagValue(12.5, "good", T0)})
    north = await make_north(devices, cache, Status(), security_policies=["Basic256Sha256_SignAndEncrypt"],
                             certificate=str(tmp_path / "srv" / "cert.der"), private_key=str(tmp_path / "srv" / "key.pem"),
                             anonymous=False, username_env=USER_ENV, password_env=PASS_ENV)
    try:
        assert (tmp_path / "srv" / "cert.der").exists()                 # generated on first start
        endpoints = await Client(north.cfg.endpoint).connect_and_get_server_endpoints()
        assert {e.SecurityPolicyUri.rsplit("#", 1)[1] for e in endpoints} == {"Basic256Sha256"}
        tokens = {t.TokenType for e in endpoints for t in e.UserIdentityTokens}
        assert tokens == {ua.UserTokenType.UserName}

        cert, key = await make_cert(tmp_path / "cli", "urn:opencap:test:client")

        async def secure_client(user: str | None, password: str | None) -> Client:
            c = Client(north.cfg.endpoint, timeout=3)
            c.application_uri = "urn:opencap:test:client"
            await c.set_security(SecurityPolicyBasic256Sha256, str(cert), str(key), None, None,
                                 ua.MessageSecurityMode.SignAndEncrypt)
            if user:
                c.set_user(user)
                c.set_password(password)
            return c

        for user, password in ((None, None), ("scada", "wrong"), ("nobody", "pw-for-test")):
            c = await secure_client(user, password)
            with pytest.raises(ua.UaStatusCodeError):
                await c.connect()
            await c.disconnect()
        c = await secure_client("scada", "pw-for-test")
        async with c:
            assert await c.get_node(north.node_id(devices[0].id, "FLOW_1")).read_value() == 12.5
            with pytest.raises(ua.UaStatusCodeError):
                await c.get_node(north.node_id(devices[0].id, "FLOW_1")).write_value(
                    ua.DataValue(ua.Variant(1.0, ua.VariantType.Float)))
        plain = Client(north.cfg.endpoint, timeout=2)                    # no None endpoint offered
        with pytest.raises(Exception):
            await plain.connect()
        await plain.disconnect()
    finally:
        await north.stop()


async def test_no_way_to_log_in_refuses_to_start(monkeypatch):
    monkeypatch.delenv(USER_ENV, raising=False)
    north = OpcUaNorth(OpcUaServerCfg(enabled=True, anonymous=False, username_env=USER_ENV, password_env=PASS_ENV,
                                      endpoint=f"opc.tcp://127.0.0.1:{free_port()}/opencap"),
                       site_devices(), ValueCache(), Status(), shelf_file=await shelf_file())
    with pytest.raises(RuntimeError, match="no user"):
        await north.start()
    assert not north.running


async def test_trusted_certs_dir_accepts_only_trusted_clients(tmp_path):
    trusted_dir = tmp_path / "trusted"
    trusted_dir.mkdir()
    good_cert, good_key = await make_cert(tmp_path / "good", "urn:opencap:test:good")
    bad_cert, bad_key = await make_cert(tmp_path / "bad", "urn:opencap:test:bad")
    (trusted_dir / "good.der").write_bytes(good_cert.read_bytes())
    devices = site_devices()
    cache = ValueCache()
    for d in devices:
        cache.register(d)
    north = await make_north(devices, cache, Status(), security_policies=["Basic256Sha256_Sign"],
                             certificate=str(tmp_path / "srv" / "c.der"), private_key=str(tmp_path / "srv" / "k.pem"),
                             trusted_certs_dir=str(trusted_dir))
    try:
        async def connect(cert, key, uri) -> Client:
            c = Client(north.cfg.endpoint, timeout=3)
            c.application_uri = uri
            await c.set_security(SecurityPolicyBasic256Sha256, str(cert), str(key), None, None,
                                 ua.MessageSecurityMode.Sign)
            await c.connect()
            return c

        with pytest.raises(Exception):
            await connect(bad_cert, bad_key, "urn:opencap:test:bad")
        c = await connect(good_cert, good_key, "urn:opencap:test:good")
        try:
            dv = await data_value(c, north.node_id(devices[0].id, "FLOW_1"))
            assert dv.StatusCode.value == ua.StatusCodes.BadWaitingForInitialData
        finally:
            await c.disconnect()
    finally:
        await north.stop()
