"""The CIP simulator against stock pycomm3 1.2.16 `LogixDriver` (unmodified, run in a thread)."""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct
import time

import pytest
from pycomm3 import CommError, LogixDriver, PycommError
from pycomm3.exceptions import DataError
from pycomm3.packets import MultiServiceRequestPacket, ReadTagRequestPacket

from tests.helpers.cip_sim import (
    LOGIX_PRODUCT_NAME,
    MICRO800_PRODUCT_NAME,
    SIM_UDT_NAME,
    CipSim,
    make_logix_tags,
)
from tests.helpers.netcut import stable_port

MSP, READ, READ_FRAG, TAG_LIST, WRITE = 0x0A, 0x4C, 0x52, 0x55, 0x4D
LARGE_FO, FO, UNCONNECTED_SEND = 0x5B, 0x54, 0x52


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


@pytest.fixture
async def start_sim():
    sims: list[CipSim] = []

    async def _start(tags, **kw) -> CipSim:
        sim = await CipSim.start(tags, **kw)
        sims.append(sim)
        return sim

    yield _start
    for sim in sims:
        await sim.stop()


@pytest.fixture
async def connect(start_sim):
    """Open a pycomm3 LogixDriver on a sim; every driver is closed (on the loop) at teardown."""
    drivers: list[LogixDriver] = []

    async def _connect(sim_or_path, *, timeout: float = 2.0, **kw) -> LogixDriver:
        path = sim_or_path if isinstance(sim_or_path, str) else sim_or_path.path
        plc = LogixDriver(path, **kw)
        plc.socket_timeout = timeout
        drivers.append(plc)
        await asyncio.to_thread(plc.open)
        return plc

    yield _connect
    for plc in drivers:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(plc.close)


async def read(plc: LogixDriver, *names: str):
    return await asyncio.to_thread(plc.read, *names)


def with_dints(n: int = 5, **kw) -> dict:
    """Synthetic tags plus two DINTs with known names."""
    return {**make_logix_tags(n, **kw), "DI_A": ("DINT", 1111), "DI_B": ("DINT", -2222)}


def expected(sim: CipSim, name: str):
    """What pycomm3 returns for a plain name: arrays read with 1 element give element 0."""
    v = sim.value(name)
    return v[0] if isinstance(v, list) else v


# --- open / tag list -------------------------------------------------------------------------------

async def test_open_logix_uploads_tags_and_udt(start_sim, connect):
    tags = make_logix_tags(40)
    sim = await start_sim(tags)
    plc = await connect(sim)

    assert plc.info["product_name"] == LOGIX_PRODUCT_NAME
    assert plc.info["vendor"] == "Rockwell Automation/Allen-Bradley"
    assert plc.info["revision"] == {"major": 20, "minor": 11}
    assert plc.info["keyswitch"] == "REMOTE RUN"
    assert plc.info["name"] == "SIM_PLC"                       # program name, class 0x64
    assert plc.info["programs"] == {"MainProgram": {"instance_id": 0x08, "routines": ["MainRoutine"]}}
    assert plc._micro800 is False
    assert plc._cfg["use_instance_ids"] is False                # revision 20 < 21: symbolic paths
    # system symbols (Program:, Task:, Map:, __x, system bit) are listed but filtered by pycomm3
    assert sorted(plc.tags) == sorted(tags) == sorted(sim.tag_names)
    udt = plc.data_types[SIM_UDT_NAME]
    assert udt["attributes"] == ["TOTAL", "COUNT", "RUN", "FAULT", "MODE", "HIST"]
    assert plc.tags["UDT_0000"]["data_type_name"] == SIM_UDT_NAME
    assert plc.tags["ARR_REAL_0010"]["dimensions"] == [10, 0, 0]
    assert plc.connection_size == 4000 and sim.connection_sizes == [4000]
    assert sim.cm_counts[LARGE_FO] == 1 and sim.cm_counts[FO] == 0
    assert sim.cm_counts[UNCONNECTED_SEND] == 1                 # identity, routed to the slot
    assert sim.open_connections == 1


async def test_open_micro800_has_no_routing_and_no_program_name(start_sim, connect):
    tags = make_logix_tags(25)
    sim = await start_sim(tags, kind="micro800")
    plc = await connect(sim)

    assert plc.info["product_name"] == MICRO800_PRODUCT_NAME
    assert plc._micro800 is True
    assert "name" not in plc.info
    assert plc._cfg["cip_path"] == []                           # backplane segment stripped
    assert plc._cfg["use_instance_ids"] is False
    assert sorted(plc.tags) == sorted(tags)
    assert SIM_UDT_NAME in plc.data_types
    assert sim.cm_counts[UNCONNECTED_SEND] == 0
    assert sim.service_counts[0x01] == 1                        # identity only, no program name
    assert sim.addressing["instance"] == 0


async def test_tag_list_upload_is_paged(start_sim, connect):
    tags = {f"TAG_WITH_A_LONGER_NAME_{i:03d}": ("REAL", i) for i in range(300)}
    sim = await start_sim(tags, large_forward_open=False)
    plc = await connect(sim)
    assert sim.service_counts[TAG_LIST] > 5                     # 0x06 "more" pages at 500 bytes
    assert sorted(plc.tags) == sorted(tags)
    assert sim.max_reply_bytes <= 500


# --- reads -----------------------------------------------------------------------------------------

ATOMIC_CASES = {
    "B": ("BOOL", True, "BOOL"),
    "SI": ("SINT", -128, "SINT"),
    "I": ("INT", -32768, "INT"),
    "DI": ("DINT", 2**31 - 1, "DINT"),
    "LI": ("LINT", -(2**63), "LINT"),
    "USI": ("USINT", 255, "USINT"),
    "UI": ("UINT", 65535, "UINT"),
    "UDI": ("UDINT", 2**32 - 1, "UDINT"),
    "ULI": ("ULINT", 2**64 - 1, "ULINT"),
    "R": ("REAL", 1.1, "REAL"),
    "LR": ("LREAL", 1.1, "LREAL"),
    "S": ("STRING", "Pump 1 OK", None),
}


@pytest.mark.parametrize("kind", ["logix", "micro800"])
async def test_single_read_of_each_atomic_type(start_sim, connect, kind):
    sim = await start_sim({k: (t, v) for k, (t, v, _) in ATOMIC_CASES.items()}, kind=kind)
    plc = await connect(sim)
    for name, (_t, value, type_name) in ATOMIC_CASES.items():
        tag = await read(plc, name)
        assert tag.error is None, (name, tag.error)
        want = f32(value) if name == "R" else value
        assert tag.value == want and type(tag.value) is type(want), name
        assert tag.value == sim.value(name)
        if type_name:
            assert tag.type == type_name
    assert sim.value("R") != 1.1                                # REAL holds float32
    assert (await read(plc, "S")).type == ("STRING" if kind == "logix" else "SHORT_STRING")
    sim.set("B", False)
    assert (await read(plc, "B")).value is False
    assert sim.service_counts[MSP] == 0                         # one name: plain Read Tag


@pytest.mark.parametrize("kind", ["logix", "micro800"])
async def test_array_element_range_and_fragmented_whole_array(start_sim, connect, kind):
    big = [i * 0.25 for i in range(1500)]                       # 6000 bytes > 4000 connection
    sim = await start_sim({"ARR": ("DINT[20]", list(range(100, 120))), "BIG": ("REAL[1500]", big)}, kind=kind)
    plc = await connect(sim)
    sim.reset_counters()

    assert (await read(plc, "ARR[3]")).value == 103
    assert (await read(plc, "ARR[2]{3}")).value == [102, 103, 104]
    assert (await read(plc, "ARR{20}")).value == list(range(100, 120))
    assert sim.service_counts[READ_FRAG] == 0

    tag = await read(plc, "BIG{1500}")
    assert tag.error is None and tag.value == big and tag.type == "REAL[1500]"
    assert sim.service_counts[READ_FRAG] == 2                   # 3992 + 2008 bytes, element-aligned
    assert sim.max_reply_bytes <= 4000
    sim.set("BIG[1499]", -1.5)
    assert (await read(plc, "BIG[1499]")).value == -1.5


@pytest.mark.parametrize("revision", [(20, 11), (33, 11)], ids=["symbolic", "instance-id"])
async def test_udt_whole_member_bool_and_array_member(start_sim, connect, revision):
    sim = await start_sim(make_logix_tags(5), revision=revision)
    plc = await connect(sim)
    assert plc._cfg["use_instance_ids"] is (revision[0] >= 21)

    whole = await read(plc, "UDT_0000")
    assert whole.error is None and whole.type == SIM_UDT_NAME
    assert whole.value == sim.value("UDT_0000")
    assert (await read(plc, "UDT_0000.TOTAL")).value == sim.value("UDT_0000.TOTAL")
    assert (await read(plc, "UDT_0000.RUN")).value is True
    assert (await read(plc, "UDT_0000.FAULT")).value is False
    assert (await read(plc, "UDT_0000.HIST[2]")).value == sim.value("UDT_0000.HIST[2]")
    assert (await read(plc, "UDT_0000.HIST{4}")).value == sim.value("UDT_0000.HIST")
    sim.set("UDT_0000", {"RUN": False, "FAULT": True})
    assert (await read(plc, "UDT_0000.RUN", "UDT_0000.FAULT"))[0].value is False
    assert (await read(plc, "UDT_0000.FAULT")).value is True
    # pycomm3 builds "TAG.MEMBER" paths from the member's tag info, which has no instance id,
    # so even with instance addressing on only the whole-UDT read uses class 0x6B + instance
    assert sim.addressing["instance"] == (1 if revision[0] >= 21 else 0)
    assert sum(sim.addressing.values()) == 9                    # 9 tag paths resolved in total


async def test_logix_multi_tag_read_is_one_msp(start_sim, connect):
    tags = make_logix_tags(30)
    sim = await start_sim(tags)
    plc = await connect(sim)
    sim.reset_counters()

    results = await read(plc, *tags)
    assert [r.tag for r in results] == list(tags)
    for r in results:
        assert r.error is None and r.value == expected(sim, r.tag), r
    assert sim.request_count == 1
    assert sim.service_counts[MSP] == 1 and sim.service_counts[READ] == 30


async def test_micro800_stock_pycomm3_sends_one_request_per_tag(start_sim, connect):
    tags = make_logix_tags(10, udt=False)
    sim = await start_sim(tags, kind="micro800")
    plc = await connect(sim)
    sim.reset_counters()

    results = await read(plc, *tags)
    assert all(r.error is None and r.value == expected(sim, r.tag) for r in results)
    assert sim.request_count == 10
    assert sim.service_counts[READ] == 10 and sim.service_counts[MSP] == 0


def _msp_request_data(plc: LogixDriver, names: list[str]) -> bytes:
    reqs = [ReadTagRequestPacket(plc._sequence, n, 1, plc.tags[n], i, False) for i, n in enumerate(names)]
    for r in reqs:
        r.build_message()                                       # sets the request path
    msgs = [r.tag_only_message() for r in reqs]
    offsets, at = [], 2 + 2 * len(msgs)
    for m in msgs:
        offsets.append(at)
        at += len(m)
    return struct.pack(f"<H{len(msgs)}H", len(msgs), *offsets) + b"".join(msgs)


def _msp_packet(plc: LogixDriver, names: list[str]) -> MultiServiceRequestPacket:
    reqs = [ReadTagRequestPacket(plc._sequence, n, 1, plc.tags[n], i, False) for i, n in enumerate(names)]
    for r in reqs:
        r.build_message()
    return MultiServiceRequestPacket(plc._sequence, reqs)


async def test_msp_probe_refused_with_0x08_when_batching_off(start_sim, connect):
    tags = make_logix_tags(6, udt=False)
    names = list(tags)[:2]
    sim = await start_sim(tags, kind="micro800", batching=False)
    plc = await connect(sim)
    sim.reset_counters()

    probe = await asyncio.to_thread(plc.generic_message, service=MSP, class_code=0x02, instance=1,
                                    request_data=_msp_request_data(plc, names), connected=True, name="msp_probe")
    assert not probe and probe.error == "Service not supported"
    # pycomm3's own MSP response parser cannot handle the body-less refusal: it raises
    with pytest.raises(DataError):
        await asyncio.to_thread(plc.send, _msp_packet(plc, names))
    assert sim.service_counts[MSP] == 2 and sim.service_counts[READ] == 0
    assert (await read(plc, names[0])).value == expected(sim, names[0])   # connection still fine


async def test_msp_accepted_by_micro800_when_batching_on(start_sim, connect):
    tags = make_logix_tags(6, udt=False)
    names = list(tags)[:4]
    sim = await start_sim(tags, kind="micro800")
    plc = await connect(sim)
    sim.reset_counters()

    probe = await asyncio.to_thread(plc.generic_message, service=MSP, class_code=0x02, instance=1,
                                    request_data=_msp_request_data(plc, names), connected=True)
    assert probe.error is None
    response = await asyncio.to_thread(plc.send, _msp_packet(plc, names))
    assert [r.value for r in response.responses] == [expected(sim, n) for n in names]
    assert sim.service_counts[MSP] == 2 and sim.service_counts[READ] == 8


async def test_logix_stock_read_against_batching_off_raises(start_sim, connect):
    tags = make_logix_tags(6, udt=False)
    sim = await start_sim(tags, batching=False)
    plc = await connect(sim)
    with pytest.raises(DataError):                              # documents pycomm3's behaviour
        await read(plc, *list(tags)[:3])
    assert sim.service_counts[MSP] == 1 and sim.service_counts[READ] == 0
    first = list(tags)[0]
    assert (await read(plc, first)).value == expected(sim, first)  # single reads still work


@pytest.mark.parametrize(("kind", "revision", "wire_error"), [
    ("logix", None, "IOI syntax error"),                        # 0x04 for an unknown symbol
    ("logix", (33, 11), "Destination unknown"),                 # 0x05 for an unknown instance id
    ("micro800", None, "IOI syntax error"),
])
async def test_unknown_tag_fails_alone(start_sim, connect, kind, revision, wire_error):
    tags = with_dints(8, udt=False)
    a, b = "DI_A", "DI_B"
    sim = await start_sim(tags, kind=kind, revision=revision)
    plc = await connect(sim)
    sim.reset_counters()

    ok1, missing, ok2 = await read(plc, a, "NO_SUCH_TAG", b)
    assert missing.value is None and "Tag doesn't exist" in missing.error   # client-side, no wire request
    assert (ok1.value, ok2.value) == (sim.value(a), sim.value(b))
    assert sim.service_counts[READ] == 2

    sim.remove(b)                                               # gone after the tag list upload
    ok1, gone = await read(plc, a, b)
    assert ok1.value == sim.value(a) and ok1.error is None
    assert gone.value is None and wire_error in gone.error


async def test_standard_forward_open_500_bytes_and_batch_split(start_sim, connect):
    tags = {f"R{i:03d}": ("REAL", i / 3) for i in range(120)}
    tags["BIG"] = ("REAL[400]", [i * 0.5 for i in range(400)])
    sim = await start_sim(tags, large_forward_open=False)
    plc = await connect(sim)
    assert plc.connection_size == 500 and sim.connection_sizes == [500]
    assert sim.cm_counts[LARGE_FO] == 1 and sim.cm_counts[FO] == 1

    names = [n for n in tags if n != "BIG"]
    groups = len(plc._read_build_requests(plc._parse_requested_tags(names, "r")))
    sim.reset_counters()
    results = await read(plc, *names)
    assert all(r.error is None and r.value == sim.value(r.tag) for r in results)
    assert groups > 1 and sim.service_counts[MSP] == groups == sim.request_count
    assert sim.max_reply_bytes <= 500

    big = await read(plc, "BIG{400}")
    assert big.value == sim.value("BIG")
    assert sim.service_counts[READ_FRAG] == math.ceil(1600 / 488)   # (500 - 2 - 4 - 2) // 4 * 4
    assert sim.max_reply_bytes <= 500


# --- faults ----------------------------------------------------------------------------------------

async def test_mute_times_out_then_resumes(start_sim, connect):
    sim = await start_sim(with_dints())
    plc = await connect(sim, timeout=0.5)
    sim.mute = True
    t0 = time.monotonic()
    with pytest.raises(CommError):
        await read(plc, "DI_A")
    assert 0.4 <= time.monotonic() - t0 < 2.0
    with pytest.raises(CommError):
        await connect(sim, timeout=0.3)                         # open: RegisterSession unanswered
    sim.mute = False
    assert (await read(plc, "DI_A")).value == sim.value("DI_A")   # muted request was dropped


async def test_stop_mid_session_then_restart_same_port(start_sim, connect):
    port = stable_port()
    sim = await start_sim(with_dints(), port=port)
    plc = await connect(sim)
    assert (await read(plc, "DI_A")).error is None

    await sim.stop()
    assert not sim.running and sim.clients == 0
    with pytest.raises(CommError):
        await read(plc, "DI_A")
    with pytest.raises(CommError):
        await connect(sim)                                      # connection refused

    sim.set("DI_A", 4242)
    await sim.restart()
    assert sim.port == port
    plc2 = await connect(sim)
    assert (await read(plc2, "DI_A")).value == 4242


@pytest.mark.parametrize("kind", ["logix", "micro800"])
async def test_writes_are_refused_and_counted(start_sim, connect, kind):
    sim = await start_sim(with_dints(udt=False), kind=kind)
    plc = await connect(sim)
    before = sim.value("DI_A")

    result = await asyncio.to_thread(plc.write, "DI_A", before + 1)
    assert result.error == "Service not supported"
    assert sim.write_attempts == 1 and sim.service_counts[WRITE] == 1
    assert sim.value("DI_A") == before
    assert (await read(plc, "DI_A")).value == before

    results = await asyncio.to_thread(plc.write, ("DI_A", 1), ("DI_B", 2))
    assert all(r.error for r in results)
    assert sim.write_attempts == 3
    assert sim.service_counts[MSP] == (1 if kind == "logix" else 0)


async def test_repeated_sequence_count_gets_the_cached_reply(start_sim, connect):
    sim = await start_sim(with_dints())
    plc = await connect(sim)
    request = ReadTagRequestPacket(4321, "DI_A", 1, plc.tags["DI_A"], 0, False)   # fixed sequence
    first = await asyncio.to_thread(plc.send, request)
    sim.set("DI_A", 5)
    again = await asyncio.to_thread(plc.send, request)          # class 3 duplicate: not re-executed
    assert first.value == again.value == 1111 and sim.duplicate_requests == 1
    assert (await read(plc, "DI_A")).value == 5


# --- routing, sessions, API ------------------------------------------------------------------------

async def test_logix_slot_is_routed(start_sim, connect):
    sim = await start_sim(with_dints(3), slot=3)
    assert sim.path.endswith("/3")
    plc = await connect(sim)
    assert (await read(plc, "DI_A")).error is None
    with pytest.raises(PycommError):                            # identity request routed to slot 1
        await connect(f"{sim.host}:{sim.port}/1")


async def test_concurrent_clients_and_clean_close(start_sim, connect):
    sim = await start_sim(with_dints())
    p1, p2 = await connect(sim), await connect(sim)
    r1, r2 = await asyncio.gather(read(p1, "DI_A"), read(p2, "DI_B"))
    assert (r1.value, r2.value) == (sim.value("DI_A"), sim.value("DI_B"))
    assert sim.clients == 2 and sim.open_connections == 2
    await asyncio.to_thread(p1.close)
    await asyncio.to_thread(p2.close)
    for _ in range(50):
        if sim.clients == 0:
            break
        await asyncio.sleep(0.01)
    assert sim.clients == 0 and sim.open_connections == 0 and sim.cm_counts[0x4E] == 2


async def test_set_and_value_forms(start_sim):
    sim = await start_sim({
        "R": ("REAL", 0), "ARR": ("INT[4]", [1, 2, 3, 4]), "S": ("STRING", "x"),
        "U": ("UDT:Pump", {"KW": ("REAL", 1.5), "ON": ("BOOL", True), "H": ("DINT[3]", [7, 8, 9])}),
    })
    sim.set("R", 0.1)
    assert sim.value("R") == f32(0.1)
    sim.set("ARR[1]", 20)
    sim.set("U.H[2]", 90)
    assert sim.value("ARR") == [1, 20, 3, 4] and sim.value("arr[1]") == 20     # case-insensitive
    sim.set("ARR", [5, 6, 7, 8])
    assert sim.value("ARR[3]") == 8
    assert sim.value("U") == {"KW": 1.5, "ON": True, "H": [7, 8, 90]}
    sim.set("U.ON", False)
    assert sim.value("U.ON") is False
    for bad_name in ("NOPE", "ARR[9]", "U.NOPE", "R.X"):
        with pytest.raises(KeyError):
            sim.value(bad_name)
    with pytest.raises(ValueError):
        sim.set("ARR[0]", 40000)                                # INT range
    with pytest.raises(ValueError):
        sim.set("ARR", [1, 2])
    with pytest.raises(ValueError):
        sim.set("S", "x" * 83)                                  # Logix STRING holds 82
    with pytest.raises(ValueError):
        await CipSim.start({"B": ("BOOL[32]", None)})           # Logix BOOL arrays: not simulated


def test_make_logix_tags_is_deterministic_and_mixed():
    a, b, c = make_logix_tags(60, seed=1), make_logix_tags(60, seed=1), make_logix_tags(60, seed=2)
    assert a == b and a != c and len(a) == 60
    types = {t.split("[")[0] for t, _ in a.values()}
    assert {"REAL", "DINT", "INT", "BOOL", f"UDT:{SIM_UDT_NAME}"} <= types
    assert sum("[" in t for t, _ in a.values()) == 3
    assert not any(t.startswith("UDT:") for t, _ in make_logix_tags(60, udt=False).values())
