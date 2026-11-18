"""The OPC UA test simulator (tests/helpers/opcua_sim.py) against a plain asyncua client."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

asyncua = pytest.importorskip("asyncua")

from asyncua import Client, ua  # noqa: E402

from tests.helpers.opcua_sim import OpcUaSim  # noqa: E402


async def test_values_status_and_restart_on_same_port():
    sim = await OpcUaSim.start({"PV": ("Double", 1.5), "ARR": ("Int16[]", [1, 2, 3])})
    try:
        assert sim.node_id("PV") == "ns=7;s=PV"
        async with Client(sim.endpoint, timeout=2) as c:
            assert (await c.get_namespace_array())[7].startswith("urn:opencap:test")
            assert await c.get_node(sim.node_id("PV")).read_value() == 1.5
            assert await c.get_node(sim.node_id("ARR")).read_value() == [1, 2, 3]
            ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
            await sim.set("PV", 2.5, status="UncertainSensorNotAccurate", source_ts=ts)
            dv = await c.get_node(sim.node_id("PV")).read_data_value(raise_on_bad_status=False)
            assert dv.Value.Value == 2.5 and dv.SourceTimestamp == ts
            assert dv.StatusCode.value == ua.StatusCodes.UncertainSensorNotAccurate
            assert sim.reads >= 3 and sim.read_nodes >= 3
            with pytest.raises(ua.UaStatusCodeError):
                await c.get_node(sim.node_id("PV")).write_value(9.0)
            assert sim.write_attempts == 1 and sim.value("PV") == 2.5
        port = sim.port
        await sim.restart()
        assert sim.port == port
        async with Client(sim.endpoint, timeout=2) as c:           # value and status survive a restart
            dv = await c.get_node(sim.node_id("PV")).read_data_value(raise_on_bad_status=False)
            assert dv.Value.Value == 2.5 and dv.StatusCode.value == ua.StatusCodes.UncertainSensorNotAccurate
    finally:
        await sim.close()


async def test_users_and_anonymous_off():
    sim = await OpcUaSim.start({"PV": ("Double", 1.0)}, users={"op": "pw-test"}, anonymous=False)
    try:
        c = Client(sim.endpoint, timeout=2)
        with pytest.raises(ua.UaStatusCodeError):
            await c.connect()
        await c.disconnect()
        c = Client(sim.endpoint, timeout=2)
        c.set_user("op")
        c.set_password("pw-test")
        async with c:
            assert await c.get_node(sim.node_id("PV")).read_value() == 1.0
        assert sim.user_manager.logins == ["op"]
    finally:
        await sim.close()
