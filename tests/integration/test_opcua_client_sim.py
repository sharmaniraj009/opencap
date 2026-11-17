"""opcua_client driver against a real asyncua server (tests/helpers/opcua_sim.py).

Real asyncua on both ends of the wire: subscriptions, every Variant type the importer maps,
StatusCode -> quality, power-off and power-on (resubscribe), user name/password from the
environment, Basic256Sha256 Sign and SignAndEncrypt, and the whole ChannelWorker path.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("asyncua")

from opencap.config.models import Channel, Device, Tag  # noqa: E402
from opencap.core.cache import ValueCache  # noqa: E402
from opencap.core.channel_worker import ChannelWorker  # noqa: E402
from opencap.core.registry import plan_device  # noqa: E402
from opencap.drivers.base import ConnectionLost  # noqa: E402
from opencap.drivers.opcua_client import OpcUaBlock, OpcUaClientDriver  # noqa: E402
from tests.helpers.modbus_sim import free_port  # noqa: E402
from tests.helpers.opcua_sim import OpcUaSim  # noqa: E402

NODES: dict[str, tuple[str, Any]] = {
    "LTI002_PV": ("Double", 1.5),
    "LTI002_QV": ("Float", 2.25),
    "COUNT_I16": ("Int16", -7),
    "COUNT_U16": ("UInt16", 65000),
    "TOTAL_I32": ("Int32", -100000),
    "TOTAL_U32": ("UInt32", 4000000000),
    "TOTAL_I64": ("Int64", -2**40),
    "TOTAL_U64": ("UInt64", 2**40),
    "RUN": ("Boolean", True),
    "MODE": ("String", "AUTO"),
    "LEVELS": ("Double[]", [1.0, 2.0, 3.5]),
}
DTYPES = {"Double": "float64", "Float": "float32", "Int16": "int16", "UInt16": "uint16", "Int32": "int32",
          "UInt32": "uint32", "Int64": "int64", "UInt64": "uint64", "Boolean": "bool", "String": "string"}


def ua_tags(sim: OpcUaSim, names: list[str] | None = None) -> list[Tag]:
    """One tag per sim node, named with a KEPServer-style group path."""
    out = []
    for name in names or list(NODES):
        spec = sim.nodes[name].vtype.name
        out.append(Tag(name=f"GRP.{name}", address=sim.node_id(name), dtype=DTYPES[spec]))
    return out


def make_device(sim: OpcUaSim | None, tags: list[Tag], *, scan_ms: int = 200, timeout_ms: int = 1000,
                endpoint: str | None = None, **params: Any) -> Device:
    p = {"endpoint": endpoint or sim.endpoint, **params}
    return Device(id="UA SITE/E+H", channel="UA SITE", name="E+H", driver="opcua_client", host="127.0.0.1",
                  port=4840, params=p, scan_ms=scan_ms, min_scan_ms=1, timeout_ms=timeout_ms, retries=0,
                  demote_after=2, demote_for_ms=500, tags=tags)


def blocks_of(device: Device) -> list[OpcUaBlock]:
    plan = plan_device(device)
    return [b for g in plan.groups for b in g.plan.blocks]  # type: ignore[misc]


async def read_all(drv: OpcUaClientDriver, device: Device) -> dict:
    out: dict = {}
    for block in blocks_of(device):
        out.update(await drv.read(block))
    return out


async def wait_for(pred, timeout: float = 5.0, what: str = "condition") -> None:
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


@pytest.fixture
async def sim():
    s = await OpcUaSim.start(dict(NODES))
    try:
        yield s
    finally:
        await s.close()
    assert s.write_attempts == 0, "the driver must never write"


@pytest.fixture
async def driver_for():
    made: list[OpcUaClientDriver] = []

    def make(device: Device) -> OpcUaClientDriver:
        drv = OpcUaClientDriver(device)
        made.append(drv)
        return drv

    yield make
    for drv in made:
        await drv.close()


# --- values -------------------------------------------------------------------------------------

async def test_reads_every_type_with_the_server_source_timestamp(sim, driver_for):
    ts = datetime(2026, 11, 1, 8, 30, 0, 123000, tzinfo=UTC)
    await sim.set("LTI002_PV", 1.5, source_ts=ts)
    dev = make_device(sim, ua_tags(sim))
    drv = driver_for(dev)
    await drv.connect()
    assert drv.connected
    out = await read_all(drv, dev)
    assert {n: tv.quality for n, tv in out.items() if tv.quality != "good"} == {}
    for name, (spec, value) in NODES.items():
        got = out[f"GRP.{name}"].value
        if spec == "Float":
            assert got == pytest.approx(value) and type(got) is float
        else:
            assert got == value and type(got) is type(value), name
    assert out["GRP.LTI002_PV"].source_ts == ts            # SourceTimestamp, not the read time
    assert all(tv.source_ts.tzinfo is not None for tv in out.values())


async def test_subscription_pushes_changes_without_polling(sim, driver_for):
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV", "RUN"]), scan_ms=100)
    drv = driver_for(dev)
    await drv.connect()
    assert sim.monitored_items == 2 and sim.reads == 1      # subscribe, then one Read for initial values
    sim.reset_counters()
    for i in range(5):
        await sim.set("LTI002_PV", 10.0 + i)
        await sim.set("RUN", i % 2 == 0)
        await wait_for(lambda: read_all(drv, dev), 3)
        await wait_for(lambda i=i: _value(drv, dev, "GRP.LTI002_PV", 10.0 + i), 3, f"value {10.0 + i}")
        assert (await read_all(drv, dev))["GRP.RUN"].value is (i % 2 == 0)
    assert sim.tag_reads == 0 and sim.monitored_items == 0   # nothing polled, nothing re-subscribed


async def _value(drv, dev, name, want) -> bool:
    return (await read_all(drv, dev))[name].value == want


async def test_same_node_twice_is_one_monitored_item(sim, driver_for):
    tags = [Tag(name="PV", address=sim.node_id("LTI002_PV"), dtype="float64"),
            Tag(name="PV_ALIAS", address=f"  {sim.node_id('LTI002_PV')} ", dtype="float32"),
            Tag(name="SLOW_PV", address=sim.node_id("LTI002_PV"), dtype="float64", scan_ms=1000)]
    dev = make_device(sim, tags)
    drv = driver_for(dev)
    await drv.connect()
    assert sim.monitored_items == 1
    out = await read_all(drv, dev)
    assert {n: tv.value for n, tv in out.items()} == {"PV": 1.5, "PV_ALIAS": 1.5, "SLOW_PV": 1.5}


async def test_namespace_uri_addresses_are_resolved(sim, driver_for):
    async with _client(sim) as c:
        uri = (await c.get_namespace_array())[sim.namespace]
    tags = [Tag(name="PV", address=f"nsu={uri};s=LTI002_PV", dtype="float64"),
            Tag(name="NOPE", address="nsu=urn:not:there;s=LTI002_PV", dtype="float64")]
    dev = make_device(sim, tags)
    drv = driver_for(dev)
    await drv.connect()
    out = await read_all(drv, dev)
    assert (out["PV"].quality, out["PV"].value) == ("good", 1.5)
    assert out["NOPE"].quality == "bad_config"


def _client(sim: OpcUaSim):
    from asyncua import Client

    return Client(sim.endpoint, timeout=2)


# --- quality ------------------------------------------------------------------------------------

async def test_unknown_node_is_bad_config_and_the_rest_reads(sim, driver_for):
    tags = ua_tags(sim, ["LTI002_PV"]) + [Tag(name="GRP.MISSING", address="ns=7;s=NOT_THERE", dtype="float64"),
                                          Tag(name="GRP.BADADDR", address="NOT_A_NODEID", dtype="float64")]
    dev = make_device(sim, tags)
    plan = plan_device(dev)
    assert set(plan.invalid) == {"GRP.BADADDR"}              # never sent: bad_config at plan time
    drv = driver_for(dev)
    await drv.connect()
    out = await read_all(drv, dev)
    assert out["GRP.MISSING"].quality == "bad_config"
    assert (out["GRP.LTI002_PV"].quality, out["GRP.LTI002_PV"].value) == ("good", 1.5)
    ident = await drv.identity()
    assert ident["refused"] == {"ns=7;s=NOT_THERE": "BadNodeIdUnknown"}


async def test_status_codes_map_to_quality(sim, driver_for):
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV", "COUNT_I16"]), scan_ms=100)
    drv = driver_for(dev)
    await drv.connect()

    async def qualities():
        out = await read_all(drv, dev)
        return out["GRP.LTI002_PV"].quality, out["GRP.COUNT_I16"].quality

    await sim.set("LTI002_PV", 3.0, status="UncertainSensorNotAccurate")
    await sim.set("COUNT_I16", status="BadCommunicationError")
    await wait_for(lambda: _eq(qualities(), ("stale", "bad_comm")), 3, "uncertain -> stale, bad -> bad_comm")
    out = await read_all(drv, dev)
    assert out["GRP.LTI002_PV"].value is None                 # the uncertain value is not passed on
    await sim.set("COUNT_I16", status="BadNodeIdUnknown")
    await wait_for(lambda: _eq(qualities(), ("stale", "bad_config")), 3, "BadNodeIdUnknown -> bad_config")
    await sim.set("LTI002_PV", 4.0)
    await sim.set("COUNT_I16", 5)
    await wait_for(lambda: _eq(qualities(), ("good", "good")), 3, "back to good")
    out = await read_all(drv, dev)
    assert (out["GRP.LTI002_PV"].value, out["GRP.COUNT_I16"].value) == (4.0, 5)


async def _eq(coro, want) -> bool:
    return (await coro) == want


async def test_node_gone_after_a_server_restart_is_bad_config(sim, driver_for):
    """A node removed from the server (e.g. a project download) is refused when the driver
    subscribes again. (asyncua 2.1's server does not notify a deletion to live monitored items:
    its callback coroutine is never awaited. The live path is the BadNodeIdUnknown status above.)"""
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV", "MODE"]), scan_ms=100)
    drv = driver_for(dev)
    await drv.connect()
    sim.nodes.pop("MODE")
    await sim.restart()
    await wait_for(lambda: not drv.connected, 3, "session lost")
    await drv.connect()
    out = await read_all(drv, dev)
    assert out["GRP.MODE"].quality == "bad_config"
    assert (out["GRP.LTI002_PV"].quality, out["GRP.LTI002_PV"].value) == ("good", 1.5)


async def _q(drv, dev, name, want) -> bool:
    return (await read_all(drv, dev))[name].quality == want


# --- connection ---------------------------------------------------------------------------------

async def test_power_off_is_connection_lost_and_power_on_resubscribes(sim, driver_for):
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV"]), scan_ms=100)
    drv = driver_for(dev)
    await drv.connect()
    block = blocks_of(dev)[0]
    await sim.stop()
    await wait_for(lambda: not drv.connected, 3, "connected turns False")
    with pytest.raises(ConnectionLost, match="session lost|BadShutdown"):
        await drv.read(block)
    with pytest.raises(ConnectionLost):
        await drv.connect()                                   # nothing listening
    await sim.restart()
    sim.reset_counters()
    await sim.set("LTI002_PV", 42.0)
    await drv.connect()
    assert sim.monitored_items == 1                           # subscribed again
    assert (await drv.read(block))["GRP.LTI002_PV"].value == 42.0
    await sim.set("LTI002_PV", 43.0)
    await wait_for(lambda: _value(drv, dev, "GRP.LTI002_PV", 43.0), 3, "notifications after resubscribe")


async def test_connect_refused_is_connection_lost(driver_for):
    dev = make_device(None, [Tag(name="X", address="ns=2;i=1", dtype="float64")],
                      endpoint=f"opc.tcp://127.0.0.1:{free_port()}/none")
    drv = driver_for(dev)
    t0 = time.monotonic()
    with pytest.raises(ConnectionLost, match="connect failed"):
        await drv.connect()
    assert time.monotonic() - t0 < 2 and not drv.connected
    await drv.close()                                         # idempotent
    await drv.close()


async def test_silent_subscription_is_connection_lost(sim, driver_for):
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV"]))
    drv = driver_for(dev)
    await drv.connect()
    s = drv._session
    assert s is not None and 0 < s.silence_s < 30
    s.sub.last_publish_at = time.monotonic() - s.silence_s - 1   # as if publishes stopped arriving
    assert not drv.connected
    with pytest.raises(ConnectionLost, match="no publish response"):
        await drv.read(blocks_of(dev)[0])


async def test_close_deletes_the_session(sim, driver_for):
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV"]))
    drv = driver_for(dev)
    await drv.connect()
    iserver = sim.server.iserver
    assert len(iserver.subscription_service.subscriptions) == 1
    await drv.close()
    assert not drv.connected
    await wait_for(lambda: not iserver.subscription_service.subscriptions, 2, "server dropped the subscription")
    await drv.close()


async def test_identity(sim, driver_for):
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV"]), publishing_interval_ms=250)
    drv = driver_for(dev)
    assert await drv.identity() == {}
    await drv.connect()
    ident = await drv.identity()
    assert ident["server_state"] == "Running"
    assert (ident["product_name"], ident["manufacturer"]) == ("FreeOpcUa Python Server", "FreeOpcUa")
    assert ident["product_uri"].startswith("urn:")
    assert ident["publishing_interval_ms"] == 250 and ident["monitored_items"] == 1
    assert ident["namespaces"][sim.namespace].startswith("urn:opencap:test")
    assert (ident["security_policy"], ident["refused_items"]) == ("None", 0)


# --- credentials and security -------------------------------------------------------------------

USER_ENV, PASS_ENV = "OPENCAP_OPCUA_TEST_USER", "OPENCAP_OPCUA_TEST_PASSWORD"


async def test_user_and_password_come_from_the_environment(monkeypatch, driver_for):
    sim = await OpcUaSim.start({"PV": ("Double", 1.0)}, users={"operator": "pw-from-env"}, anonymous=False)
    try:
        tags = [Tag(name="PV", address=sim.node_id("PV"), dtype="float64")]
        dev = make_device(sim, tags, username_env=USER_ENV, password_env=PASS_ENV)

        monkeypatch.delenv(USER_ENV, raising=False)
        monkeypatch.delenv(PASS_ENV, raising=False)
        with pytest.raises(ConnectionLost, match=f"{USER_ENV} .*is not set"):
            await driver_for(dev).connect()

        monkeypatch.setenv(USER_ENV, "operator")
        monkeypatch.setenv(PASS_ENV, "wrong-pw")
        with pytest.raises(ConnectionLost) as exc:
            await driver_for(dev).connect()
        assert "wrong-pw" not in str(exc.value) and "BadUserAccessDenied" in str(exc.value)

        monkeypatch.setenv(PASS_ENV, "pw-from-env")
        drv = driver_for(dev)
        await drv.connect()
        assert (await read_all(drv, dev))["PV"].value == 1.0
        assert sim.user_manager.logins == ["operator"]
        assert sim.write_attempts == 0
    finally:
        await sim.close()


@pytest.mark.parametrize("mode, policy", [("Sign", "Basic256Sha256_Sign"),
                                          ("SignAndEncrypt", "Basic256Sha256_SignAndEncrypt")])
async def test_basic256sha256(tmp_path, driver_for, mode, policy):
    sim = await OpcUaSim.start({"PV": ("Double", 7.5)}, policies=[policy], cert_dir=tmp_path / "server")
    try:
        cert, key = tmp_path / "pki" / "client.der", tmp_path / "pki" / "client.pem"
        tags = [Tag(name="PV", address=sim.node_id("PV"), dtype="float64")]
        dev = make_device(sim, tags, security_policy="Basic256Sha256", message_mode=mode,
                          certificate=str(cert), private_key=str(key), timeout_ms=3000)
        drv = driver_for(dev)
        await drv.connect()                                   # the client pair is generated on first use
        assert cert.exists() and key.exists() and (key.stat().st_mode & 0o077) == 0
        assert (await read_all(drv, dev))["PV"].value == 7.5
        await drv.close()

        none_dev = make_device(sim, tags)                     # the server offers no None endpoint
        with pytest.raises(ConnectionLost):
            await driver_for(none_dev).connect()

        pinned = make_device(sim, tags, security_policy="Basic256Sha256", message_mode=mode,
                             certificate=str(cert), private_key=str(key), server_certificate=str(sim.cert),
                             timeout_ms=3000)
        drv = driver_for(pinned)
        await drv.connect()
        assert (await read_all(drv, pinned))["PV"].value == 7.5
    finally:
        await sim.close()


async def test_half_a_key_pair_is_an_error(tmp_path, sim, driver_for):
    cert = tmp_path / "client.der"
    cert.write_bytes(b"not really a certificate")
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV"]), security_policy="Basic256Sha256", message_mode="Sign",
                      certificate=str(cert), private_key=str(tmp_path / "client.pem"))
    with pytest.raises(ConnectionLost, match="client.pem does not exist"):
        await driver_for(dev).connect()


# --- through the ChannelWorker ------------------------------------------------------------------

async def test_worker_keeps_unchanged_values_good_and_recovers_after_power_cycle(sim):
    dev = make_device(sim, ua_tags(sim, ["LTI002_PV", "MODE"]), scan_ms=100, timeout_ms=500)
    ch = Channel(name=dev.channel, devices=[dev])
    cache = ValueCache()
    worker = ChannelWorker(ch, {dev.id: plan_device(dev)}, cache, start_jitter=0.0, backoff_base_s=0.1,
                           backoff_max_s=0.2)
    old = datetime.now(UTC) - timedelta(hours=2)
    await sim.set("LTI002_PV", 1.5, source_ts=old)            # unchanged for two hours
    task = asyncio.create_task(worker.run())
    try:
        await wait_for(lambda: worker.health(dev.id).state == "ok", 5, "device ok")
        seen = set()
        deadline = time.monotonic() + 1.0                     # 10 x the rate, values never change
        while time.monotonic() < deadline:
            seen.add(cache.get(dev.id, "GRP.LTI002_PV").quality)
            await asyncio.sleep(0.01)
        assert seen == {"good"}                               # D-028: not stale although SourceTimestamp is old
        assert cache.get(dev.id, "GRP.LTI002_PV").source_ts == old
        assert worker.device_status(dev.id)["connected"] is True

        await sim.stop()
        await wait_for(lambda: cache.get(dev.id, "GRP.LTI002_PV").quality == "bad_comm", 5, "bad_comm")
        await wait_for(lambda: worker.health(dev.id).state in ("offline", "demoted"), 5, "device down")
        await sim.restart()
        await sim.set("MODE", "MANUAL")
        await wait_for(lambda: worker.health(dev.id).state == "ok", 10, "device ok again")
        await wait_for(lambda: cache.get(dev.id, "GRP.MODE").value == "MANUAL", 5, "new value after restart")
        assert cache.get(dev.id, "GRP.LTI002_PV").quality == "good"
    finally:
        await worker.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert sim.write_attempts == 0
