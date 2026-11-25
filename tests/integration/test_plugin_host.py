"""Python driver host contract (#49, proto/opencap/plugin/v1/driver.proto): the real host process
serving the real `ab_pccc` driver against the PCCC simulator, called through gRPC exactly as the
Rust engine calls it. The host must return what the in-process driver returns."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest

pytest.importorskip("grpc")
pccc_sim = pytest.importorskip("tests.helpers.pccc_sim")

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.drivers.ab_pccc import AbPcccDriver  # noqa: E402
from opencap.plugin.v1 import driver_pb2 as pb  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402
from tests.helpers.plugin_host import plugin_host  # noqa: E402
from tests.integration.test_ab_pccc_sim import SYSTEM_FILES, TAGS  # noqa: E402

PcccSim = pccc_sim.PcccSim
tables_for = pccc_sim.tables_for


def device(sim, tags: list[Tag] = TAGS, **params) -> Device:  # noqa: ANN001
    return Device(id="SIM/SLC", channel="SIM", name="SLC", driver="ab_pccc", host=sim.host, port=sim.port,
                  params=params, tags=tags, timeout_ms=1500)


def value(v: pb.Variant):  # noqa: ANN201
    kind = v.WhichOneof("kind")
    if kind == "list_value":
        return [value(i) for i in v.list_value.items]
    return getattr(v, kind)


async def read_all(host, dev: Device, plan: pb.PlanReply) -> dict[str, pb.TagValue]:  # noqa: ANN001
    out: dict[str, pb.TagValue] = {}
    for block in plan.blocks:
        reply = await host.call("Read", pb.ReadRequest(device_id=dev.id, block=block.payload))
        assert not reply.HasField("error"), reply.error
        out.update(reply.values)
    return out


async def test_plan_open_connect_read_identity_match_the_in_process_driver() -> None:
    sim = await PcccSim.start({**SYSTEM_FILES, **tables_for(TAGS, seed=3)})
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        dev = device(sim)
        local = AbPcccDriver(dev, executor)
        local_plan = local.plan(dev.tags)
        await local.connect()
        want = {}
        for b in local_plan.blocks:
            want.update(await local.read(b))
        await local.close()

        async with plugin_host("ab_pccc") as host:
            hello = await host.call("Hello", pb.HelloRequest(protocol="opencap.plugin.v1", engine="test"))
            assert hello.protocol == "opencap.plugin.v1" and list(hello.drivers) == ["ab_pccc"]

            plan = await host.plan(dev)
            assert not plan.HasField("error")
            assert [list(b.tags) for b in plan.blocks] == [b.tags for b in local_plan.blocks]
            assert [b.describe for b in plan.blocks] == [b.describe() for b in local_plan.blocks]
            assert [b.wire_requests for b in plan.blocks] == [b.wire_requests() for b in local_plan.blocks]
            assert dict(plan.invalid) == local_plan.invalid
            fresh = AbPcccDriver(dev, executor)
            assert plan.info.reports_connection and plan.info.HasField("connect_timeout_ms")
            assert plan.info.connect_timeout_ms == round(fresh.connect_timeout_s() * 1000)

            assert not (await host.open(dev)).HasField("error")
            status = await host.call("Connect", pb.DeviceRef(device_id=dev.id))
            assert not status.HasField("error") and status.connected
            # The connect budget shrinks once the file directory is cached: Connect reports it.
            assert status.info.connect_timeout_ms == round(local.connect_timeout_s() * 1000)
            assert status.info.connect_timeout_ms < plan.info.connect_timeout_ms
            got = await read_all(host, dev, plan)
            assert set(got) == set(want)
            for name, tv in want.items():
                assert got[name].quality == pb.GOOD, name
                assert value(got[name].value) == pytest.approx(tv.value), name
                assert got[name].HasField("source_ts_us")

            ident = json.loads((await host.call("Identity", pb.DeviceRef(device_id=dev.id))).json)
            assert ident and "product_name" in json.dumps(ident)
            health = await host.call("Health", pb.HealthRequest())
            assert health.open_devices == 1 and health.reads == len(plan.blocks)
            closed = await host.call("Close", pb.DeviceRef(device_id=dev.id))
            assert not closed.connected
    finally:
        executor.shutdown(wait=True)
        await sim.stop()


async def test_driver_errors_keep_their_class() -> None:
    sim = await PcccSim.start({**SYSTEM_FILES, **tables_for(TAGS, seed=3)}, port=stable_port())
    try:
        dev = device(sim)
        async with plugin_host("ab_pccc") as host:
            plan = await host.plan(dev)
            ref = pb.DeviceRef(device_id=dev.id)
            # Not open yet (e.g. the host restarted): NOT_OPEN, so the engine re-opens.
            assert (await host.call("Connect", ref)).error.kind == pb.DriverError.NOT_OPEN
            await host.open(dev)
            await host.call("Connect", ref)
            await sim.stop()                          # PLC powered off mid-run
            reply = await host.call("Read", pb.ReadRequest(device_id=dev.id, block=plan.blocks[0].payload))
            assert reply.error.kind == pb.DriverError.CONNECTION_LOST, reply.error
            assert not reply.connected
            status = await host.call("Connect", ref)
            assert status.error.kind == pb.DriverError.CONNECTION_LOST and "connect failed" in status.error.message
            await sim.restart()
            assert not (await host.call("Connect", ref)).HasField("error")
            assert len(await read_all(host, dev, plan)) == len(TAGS)
    finally:
        await sim.stop()


async def test_missing_file_is_block_config_and_bad_params_cannot_be_planned() -> None:
    tags = [Tag(name="ok", address="N7:0", dtype="int16"), Tag(name="gone", address="N99:0", dtype="int16")]
    sim = await PcccSim.start({**SYSTEM_FILES, "N7": [5]})
    try:
        dev = device(sim, tags)
        async with plugin_host("ab_pccc") as host:
            plan = await host.plan(dev)
            await host.open(dev)
            await host.call("Connect", pb.DeviceRef(device_id=dev.id))
            kinds = []
            for b in plan.blocks:
                r = await host.call("Read", pb.ReadRequest(device_id=dev.id, block=b.payload))
                kinds.append((list(b.tags), r.error.kind if r.HasField("error") else "ok"))
            assert (["gone"], pb.DriverError.BLOCK_CONFIG) in kinds and (["ok"], "ok") in kinds

            bad = await host.plan(device(sim, tags, path="1,0,2"))     # a CIP route needs pairs
            assert bad.HasField("error") and bad.error.kind == pb.DriverError.OTHER
            unserved = Device(id="X/Y", channel="X", name="Y", driver="modbus_tcp", host="192.0.2.1", port=502,
                              tags=[Tag(name="a", address="400001", dtype="uint16")])
            refused = await host.plan(unserved)
            assert refused.error.kind == pb.DriverError.UNSUPPORTED and "not served" in refused.error.message
    finally:
        await sim.stop()


async def test_payloads_and_calls_are_authenticated() -> None:
    sim = await PcccSim.start({**SYSTEM_FILES, **tables_for(TAGS, seed=3)})
    try:
        dev = device(sim)
        async with plugin_host("ab_pccc") as host:
            plan = await host.plan(dev)
            await host.open(dev)
            payload = bytearray(plan.blocks[0].payload)
            payload[-1] ^= 0xFF                       # tampered pickle: refused before unpickling
            r = await host.call("Read", pb.ReadRequest(device_id=dev.id, block=bytes(payload)))
            assert r.error.kind == pb.DriverError.BLOCK_CONFIG and "signature" in r.error.message
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await host.stub.Health(pb.HealthRequest(), metadata=(("authorization", "Bearer nope"),))
            assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
            with pytest.raises(grpc.aio.AioRpcError) as err:
                async for _ in host.stub.Subscribe(pb.SubscribeRequest(device_id=dev.id)):
                    pass
            assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
            notes = [n async for n in host.stub.Subscribe(pb.SubscribeRequest(device_id=dev.id),
                                                          metadata=host.metadata)]
            assert [n.error.kind for n in notes] == [pb.DriverError.UNSUPPORTED]
        # A host started by another engine run (other token) refuses this run's payloads.
        async with plugin_host("ab_pccc") as other:
            await other.open(dev)
            r = await other.call("Read", pb.ReadRequest(device_id=dev.id, block=plan.blocks[0].payload))
            assert r.error.kind == pb.DriverError.BLOCK_CONFIG
    finally:
        await sim.stop()


async def test_tcp_transport_and_exit_when_the_engine_goes_away() -> None:
    sim = await PcccSim.start({**SYSTEM_FILES, **tables_for(TAGS, seed=3)})
    try:
        dev = device(sim)
        async with plugin_host("ab_pccc", tcp=True) as host:
            assert host.address.startswith("127.0.0.1:")
            plan = await host.plan(dev)
            await host.open(dev)
            await host.call("Connect", pb.DeviceRef(device_id=dev.id))
            assert len(await read_all(host, dev, plan)) == len(TAGS)
            assert host.proc.stdin is not None
            host.proc.stdin.close()                   # the engine died: the host must not linger
            assert await asyncio.wait_for(host.proc.wait(), 10) == 0
    finally:
        await sim.stop()


async def test_tcp_without_a_token_is_refused() -> None:
    import os
    import sys

    env = {k: v for k, v in os.environ.items() if k != "OPENCAP_PLUGIN_TOKEN"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "opencap.plugin_host", "--listen", "127.0.0.1:0", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == 2 and b"OPENCAP_PLUGIN_TOKEN" in err
