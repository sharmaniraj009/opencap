"""The throwaway BACnet/IP client prototype against a bacpypes3 device (spike #15).

    pip install bacpypes3==0.1.0 pytest pytest-asyncio
    pytest spikes/bacnet -q

bacpypes3 0.1.0 (MIT per its pyproject; no LICENSE file is shipped, see docs/spikes/bacnet.md) runs
the device in the same event loop; its own client reads the same device, which validates the
device script independently of the prototype. Unicast on 127.0.0.1 with ephemeral ports: no
broadcast needed (bacpypes3 answers a unicast Who-Is with a unicast I-Am).

Not collected by CI (`testpaths = ["tests"]`): a spike, not product code.
"""

from __future__ import annotations

import asyncio

import pytest

bacpypes3 = pytest.importorskip("bacpypes3")

from proto_client import (  # noqa: E402
    CONFIRMED_ALLOWED,
    OBJECT_TYPES as T,
    PROPERTIES as P,
    BACnetError,
    BitString,
    Client,
    confirmed_request,
    parse_address,
    quality_of,
    read_property_request,
)
from sim_device import DEVICE_INSTANCE, free_udp_port, make_device  # noqa: E402


@pytest.fixture
async def sim():
    device = make_device(extra_values=60)
    try:
        yield device
    finally:
        device.close()


# --- pure encoding ------------------------------------------------------------------------------------

def test_encodings() -> None:
    # ReadProperty analog-input 1 present-value: [0] object id (type 0 << 22 | 1), [1] property 85
    assert read_property_request(0, 1, 85) == bytes.fromhex("0C 00000001 19 55")
    # confirmed request header: SA bit, max APDU 1476 (code 5), invoke 7, service 12
    assert confirmed_request(7, 12, b"")[:4] == bytes.fromhex("02 05 07 0C")
    assert CONFIRMED_ALLOWED == {5, 12, 14}
    for service in (15, 16, 17, 20, 27):                 # WriteProperty, WPM, DCC, Reinitialize, ...
        with pytest.raises(ValueError):
            confirmed_request(1, service, b"")


def test_addresses_and_quality() -> None:
    a = parse_address("AnalogInput.1.PresentValue")
    assert (a.obj_type, a.instance, a.prop, a.index, a.cov) == (0, 1, 85, None, False)
    assert parse_address("AnalogValue.100.OutOfService COV").cov
    assert parse_address("AnalogValue.100.PriorityArray [4]").index == 4
    assert parse_address("2.100.85") == parse_address("AnalogValue.100.PresentValue")   # numeric form
    for bad in ("AnalogValue.100.PresentValue PRIORITY", "AnalogValue.100.PresentValue RELINQUISH",
                "Bogus.1.PresentValue", "AnalogInput.4194304.PresentValue", "AnalogInput.1"):
        with pytest.raises(ValueError):
            parse_address(bad)
    ok = BitString((False, False, False, False))
    assert quality_of(ok) == "good"
    assert quality_of(BitString((True, False, True, False))) == "good"        # in alarm, overridden
    assert quality_of(BitString((False, True, False, False)), 1) == "bad_decode"   # fault, no-sensor
    assert quality_of(BitString((False, True, False, False)), 12) == "bad_comm"    # communication-failure
    assert quality_of(BitString((False, True, False, False)), 10) == "bad_config"  # configuration-error
    assert quality_of(BitString((False, False, False, True))) == "stale"      # out of service


# --- against the bacpypes3 device ---------------------------------------------------------------------

async def test_who_is_and_read_property(sim) -> None:
    async with Client() as c:
        [i_am] = await c.who_is(sim.addr, DEVICE_INSTANCE, DEVICE_INSTANCE)
        assert i_am.device == (8, DEVICE_INSTANCE) and i_am.source == sim.addr
        assert await c.read_property(sim.addr, T["AnalogInput"], 1, P["PresentValue"]) == [21.5]
        assert await c.read_property(sim.addr, T["BinaryInput"], 1, P["PresentValue"]) == [1]      # active
        assert await c.read_property(sim.addr, T["MultiStateValue"], 1, P["PresentValue"]) == [3]
        assert await c.read_property(sim.addr, T["AnalogInput"], 1, P["Units"]) == [62]            # degrees-Celsius
        with pytest.raises(BACnetError):
            await c.read_property(sim.addr, T["AnalogInput"], 99, P["PresentValue"])              # unknown object
    assert sim.write_attempts == 0


async def test_status_flags_and_reliability_map_to_quality(sim) -> None:
    async with Client() as c:
        [sf] = await c.read_property(sim.addr, T["AnalogInput"], 2, P["StatusFlags"])
        [rel] = await c.read_property(sim.addr, T["AnalogInput"], 2, P["Reliability"])
        assert sf.bits[1] and rel == 1 and quality_of(sf, rel) == "bad_decode"   # fault, no-sensor
        [sf] = await c.read_property(sim.addr, T["AnalogValue"], 1, P["StatusFlags"])
        assert sf.bits[3] and quality_of(sf) == "stale"                         # out of service
        [sf] = await c.read_property(sim.addr, T["AnalogInput"], 1, P["StatusFlags"])
        assert quality_of(sf) == "good"


async def test_read_property_multiple_batches_and_per_property_errors(sim) -> None:
    async with Client() as c:
        specs = [(T[t], 1, [P["PresentValue"], P["StatusFlags"]]) for t in
                 ("AnalogInput", "AnalogOutput", "AnalogValue", "BinaryInput", "BinaryOutput", "BinaryValue",
                  "MultiStateInput", "MultiStateOutput", "MultiStateValue")]
        specs[0][2].append(9999)                                                # an unknown property
        r = await c.read_property_multiple(sim.addr, specs)
    assert r[(0, 1)][85] == [21.5] and r[(1, 1)][85] == [40.0] and r[(4, 1)][85] == [0] and r[(14, 1)][85] == [1]
    assert all(isinstance(r[(t, 1)][111][0], BitString) for t, _, _ in specs)
    assert isinstance(r[(0, 1)][9999], BACnetError)                             # error inside the ACK, not for the batch


async def test_segmented_response_is_reassembled(sim) -> None:
    async with Client() as c:
        objects = await c.read_property(sim.addr, T["Device"], DEVICE_INSTANCE, P["ObjectList"], max_apdu=50)
        assert c.segment_acks >= 5                                              # 50-byte APDUs force segmentation
        assert len(objects) == 1 + 1 + 10 + 60                                  # device, network port, 10 + 60 objects
        assert (2, 159) in objects


async def test_cov_subscription_delivers_changes(sim) -> None:
    async with Client() as c:
        await c.subscribe_cov(sim.addr, 7, T["AnalogInput"], 1, lifetime=60)
        first = await asyncio.wait_for(c.cov.get(), 3)                          # initial notification
        assert first.obj == (0, 1) and first.values[85] == [21.5]
        sim.objects["ai1"].presentValue = 30.0                                  # beyond covIncrement 0.5
        change = await asyncio.wait_for(c.cov.get(), 3)
        assert change.values[85] == [30.0] and isinstance(change.values[111][0], BitString)
        assert change.process_id == 7 and change.device == (8, DEVICE_INSTANCE)


async def test_browse_builds_kepserverex_addresses(sim) -> None:
    async with Client() as c:
        tags = dict(await c.browse(sim.addr, DEVICE_INSTANCE))
    assert tags["AnalogInput.1.PresentValue"] == "TT-101"
    assert tags["MultiStateOutput.1.PresentValue"] == "FAN-SPEED"
    assert len(tags) == 10 + 60
    for address in tags:
        parse_address(address)                                                  # browse output parses back


async def test_foreign_device_registration_and_distribute_broadcast() -> None:
    bbmd = make_device(port=free_udp_port(), bbmd=True)
    try:
        async with Client() as c:
            assert await c.register_foreign_device(bbmd.addr, ttl=60) == 0     # BVLC-Result: successful completion
            found = await c.who_is(bbmd.addr, DEVICE_INSTANCE, DEVICE_INSTANCE, via_bbmd=True, wait=2.0)
            assert [i.device for i in found] == [(8, DEVICE_INSTANCE)]
    finally:
        bbmd.close()


async def test_bacpypes3_client_reads_the_same_device(sim) -> None:
    """The device script validated by bacpypes3's own client, independently of the prototype."""
    from bacpypes3.app import Application
    from bacpypes3.argparse import SimpleArgumentParser
    from bacpypes3.pdu import Address
    from bacpypes3.primitivedata import ObjectIdentifier

    args = SimpleArgumentParser().parse_args(["--name", "Peer", "--instance", "999",
                                              "--address", f"127.0.0.1/8:{free_udp_port()}"])
    peer = Application.from_args(args)
    try:
        addr = Address(f"127.0.0.1:{sim.port}")
        assert await peer.read_property(addr, ObjectIdentifier("analog-input,1"), "present-value") == 21.5
        assert await peer.read_property(addr, ObjectIdentifier("multi-state-value,1"), "present-value") == 3
        objects = await peer.read_property(addr, ObjectIdentifier(f"device,{DEVICE_INSTANCE}"), "object-list")
        assert len(objects) == 72
    finally:
        peer.close()


async def test_device_refuses_and_counts_writes(sim) -> None:
    """The simulator side of the read-only guarantee: a write (sent here by bacpypes3, never by the
    prototype, which cannot frame one) is refused and counted."""
    from bacpypes3.apdu import ErrorRejectAbortNack
    from bacpypes3.app import Application
    from bacpypes3.argparse import SimpleArgumentParser
    from bacpypes3.pdu import Address
    from bacpypes3.primitivedata import ObjectIdentifier

    peer = Application.from_args(SimpleArgumentParser().parse_args(
        ["--name", "Writer", "--instance", "998", "--address", f"127.0.0.1/8:{free_udp_port()}"]))
    try:
        with pytest.raises(ErrorRejectAbortNack, match="write-access-denied"):   # a BaseException in bacpypes3
            await peer.write_property(Address(f"127.0.0.1:{sim.port}"), ObjectIdentifier("analog-output,1"),
                                      "present-value", 99.0, priority=8)
    finally:
        peer.close()
    assert sim.write_attempts == 1 and sim.objects["ao1"].presentValue == 40.0
