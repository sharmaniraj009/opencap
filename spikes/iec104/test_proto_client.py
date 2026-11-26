"""The throwaway IEC 104 client prototype against lib60870-C, through c104 (spike #16).

    pip install c104==2.2.1 pytest pytest-asyncio      # c104 is GPL-3.0: a test-only tool, never vendored
    pytest spikes/iec104 -q

c104 2.2.1 (GPL-3.0, https://github.com/Fraunhofer-FIT-DIEN/iec104-python/blob/v2.2.1/LICENSE) wraps
lib60870-C (GPL-3.0 or commercial), the most widely used open IEC 104 stack. ADR-0007 allows a strong
copyleft tool as a test-only peer installed in CI and never vendored or shipped; this spike installs it
only in a throwaway venv. Not collected by CI (`testpaths = ["tests"]`).
"""

import asyncio
import socket
from datetime import UTC, datetime

import pytest

c104 = pytest.importorskip("c104")

from proto_client import (  # noqa: E402
    C_CI_NA_1,
    C_IC_NA_1,
    COMMANDS_ALLOWED,
    COT_ACT_CON,
    COT_ACT_TERM,
    COT_INROGEN,
    COT_REQCOGEN,
    COT_SPONTANEOUS,
    IV,
    NT,
    OV,
    Client,
    command_asdu,
    cp56time2a,
    encode_cp56time2a,
    i_frame,
    quality_of,
    s_frame,
    u_frame,
)

CA = 47


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- pure framing ----------------------------------------------------------------------------------

def test_frames_and_time() -> None:
    assert u_frame(0x07) == bytes.fromhex("68 04 07 00 00 00")                 # STARTDT act
    assert s_frame(5) == bytes.fromhex("68 04 01 00 0A 00")                    # N(R) = 5
    gi = command_asdu(C_IC_NA_1, CA, 20)
    assert gi == bytes.fromhex("64 01 06 00 2F00 000000 14")                   # C_IC_NA_1, act, CA 47, QOI 20
    assert i_frame(1, 2, gi)[:6] == bytes.fromhex("68 0E 02 00 04 00")
    t = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    assert cp56time2a(encode_cp56time2a(t)) == (t, False)
    assert cp56time2a(bytes.fromhex("00 00 84 00 01 01 1A"))[0] is None       # IV bit in the minutes byte


def test_read_only_commands() -> None:
    assert COMMANDS_ALLOWED == {C_IC_NA_1, C_CI_NA_1}
    for type_id in (45, 46, 47, 48, 49, 50, 51, 58, 63, 103, 105, 110, 113):  # C_SC .. C_BO, C_CS, C_RP, P_ME, P_AC
        with pytest.raises(ValueError):
            command_asdu(type_id, CA, 0)
    for frz in (0x40, 0x80, 0xC0):                                            # freeze / freeze+reset / reset
        with pytest.raises(ValueError):
            command_asdu(C_CI_NA_1, CA, 5 | frz)


def test_quality_mapping() -> None:
    assert quality_of(0) == "good"
    assert quality_of(IV) == "bad_decode"
    assert quality_of(NT) == "stale"
    assert quality_of(OV) == "bad_decode"
    assert quality_of(0x10 | 0x20) == "good"                                   # blocked, substituted
    assert quality_of(0x80, counter=True) == "bad_decode"


# --- against lib60870 (c104) ---------------------------------------------------------------------

@pytest.fixture
def server():
    port = free_port()
    srv = c104.Server(ip="127.0.0.1", port=port)
    station = srv.add_station(common_address=CA)
    points = {
        "sp": station.add_point(io_address=100, type=c104.Type.M_SP_NA_1),
        "dp": station.add_point(io_address=101, type=c104.Type.M_DP_NA_1),
        "nva": station.add_point(io_address=200, type=c104.Type.M_ME_NA_1),
        "sva": station.add_point(io_address=201, type=c104.Type.M_ME_NB_1),
        "flt": station.add_point(io_address=202, type=c104.Type.M_ME_NC_1),
        "flt_t": station.add_point(io_address=203, type=c104.Type.M_ME_TF_1),
        "sp_t": station.add_point(io_address=102, type=c104.Type.M_SP_TB_1),
        "cnt": station.add_point(io_address=300, type=c104.Type.M_IT_NA_1),
        "cmd": station.add_point(io_address=500, type=c104.Type.C_SC_NA_1),
    }
    received_commands = []

    def on_command(point: c104.Point, previous_info: c104.Information,
                   message: c104.IncomingMessage) -> c104.ResponseState:
        received_commands.append(message)                                     # never reached: read-only client
        return c104.ResponseState.FAILURE

    points["cmd"].on_receive(callable=on_command)
    points["sp"].value = True
    points["dp"].value = c104.Double.ON
    points["nva"].value = c104.NormalizedFloat(0.5)
    points["sva"].value = c104.Int16(-1234)
    points["flt"].value = 12.5
    points["flt_t"].value = 3.25
    points["sp_t"].value = False
    points["cnt"].value = 4711
    srv.start()
    try:
        yield port, points, received_commands
    finally:
        srv.stop()


async def test_general_interrogation(server) -> None:
    port, _, commands = server
    async with Client("127.0.0.1", port) as c:
        await c.startdt()
        replies = await c.general_interrogation(CA)
        assert [r.cot for r in replies] == [COT_ACT_CON, COT_ACT_TERM] and not any(r.negative for r in replies)
        p = c.points
        assert p[(CA, 100)].value is True and p[(CA, 100)].cot == COT_INROGEN
        assert p[(CA, 101)].value == 2                                         # DPI on
        assert p[(CA, 200)].value == pytest.approx(0.5, abs=1 / 32768)        # normalized
        assert p[(CA, 201)].value == -1234                                    # scaled
        assert p[(CA, 202)].value == 12.5 and p[(CA, 202)].opencap_quality == "good"
        assert p[(CA, 203)].value == 3.25 and p[(CA, 203)].time is not None   # time-tagged in the GI
        await c.stopdt()
    assert commands == []


async def test_counter_interrogation_reads_without_freeze(server) -> None:
    port, _, _ = server
    async with Client("127.0.0.1", port) as c:
        await c.startdt()
        replies = await c.counter_interrogation(CA)
        assert [r.cot for r in replies] == [COT_ACT_CON, COT_ACT_TERM]
        counter = c.points[(CA, 300)]
        assert counter.value == 4711 and counter.cot == COT_REQCOGEN and counter.opencap_quality == "good"
        assert all(f[6] != C_CI_NA_1 or f[-1] & 0xC0 == 0 for f in c.sent if len(f) > 6)   # FRZ = 0 on the wire


async def test_spontaneous_values_quality_and_time(server) -> None:
    port, points, _ = server
    async with Client("127.0.0.1", port) as c:
        await c.startdt()
        await c.general_interrogation(CA)
        points["flt"].value = 99.0
        points["flt"].quality = c104.Quality.Invalid
        points["flt"].transmit(cause=c104.Cot.SPONTANEOUS)
        points["flt_t"].value = 7.5
        points["flt_t"].quality = c104.Quality.NonTopical
        points["flt_t"].transmit(cause=c104.Cot.SPONTANEOUS)
        for _ in range(100):
            if len(c.events) >= 2:
                break
            await asyncio.sleep(0.02)
        ev = {e.ioa: e for e in c.events}
        assert ev[202].value == 99.0 and ev[202].cot == COT_SPONTANEOUS and ev[202].opencap_quality == "bad_decode"
        assert ev[203].value == 7.5 and ev[203].opencap_quality == "stale"
        assert ev[203].time is not None and abs((datetime.now(UTC) - ev[203].time).total_seconds()) < 60


async def test_w_window_acknowledges_and_testfr(server) -> None:
    port, points, _ = server
    async with Client("127.0.0.1", port, w=4) as c:
        await c.startdt()
        await c.general_interrogation(CA)                                     # 8 points -> several I-frames
        for i in range(10):
            points["sva"].value = c104.Int16(i)
            points["sva"].transmit(cause=c104.Cot.SPONTANEOUS)
        for _ in range(100):
            if len([e for e in c.events if e.ioa == 201]) >= 10:
                break
            await asyncio.sleep(0.02)
        assert c.s_frames_sent >= 2                                           # acknowledged every w = 4 frames
        await c.testfr()                                                      # link test both ways
        assert [e.value for e in c.events if e.ioa == 201] == list(range(10))


async def test_no_command_ever_reaches_the_server(server) -> None:
    """The read-only guarantee on the wire: only U, S and C_IC/C_CI I-frames were sent."""
    port, _, commands = server
    async with Client("127.0.0.1", port) as c:
        await c.startdt()
        await c.general_interrogation(CA)
        await c.counter_interrogation(CA)
        await c.stopdt()
        type_ids = {f[6] for f in c.sent if f[2] & 0x01 == 0}                # I-frames: ASDU type id at byte 6
    assert type_ids <= COMMANDS_ALLOWED and commands == []


# --- a permissive second peer: igs104-server (igs-iec104 0.1.0, Apache-2.0) ---------------------------

def _igs104_server() -> str | None:
    import os
    import shutil

    return os.environ.get("IGS104_SERVER") or shutil.which("igs104-server")


@pytest.mark.skipif(_igs104_server() is None, reason="igs104-server not built (cargo install igs-iec104)")
async def test_against_igs104_server() -> None:
    """The same client against an unrelated, permissively licensed Rust stack, with its demo points."""
    import subprocess

    port = free_port()
    proc = subprocess.Popen([_igs104_server(), "--bind", f"127.0.0.1:{port}", "--common-address", str(CA),
                             "--for", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                c = await Client("127.0.0.1", port).__aenter__()
                break
            except OSError:
                await asyncio.sleep(0.05)
        try:
            await c.startdt()
            replies = await c.general_interrogation(CA)
            assert [r.cot for r in replies] == [COT_ACT_CON, COT_ACT_TERM]
            types = {p.type_id for p in c.points.values() if p.cot == COT_INROGEN}
            assert {1, 3, 9, 11, 13} <= types                                 # SP, DP, ME_NA, ME_NB, ME_NC
            assert [r.cot for r in await c.counter_interrogation(CA)] == [COT_ACT_CON, COT_ACT_TERM]
            for _ in range(150):                                              # the demo changes values
                if any(e.time is not None for e in c.events):
                    break
                await asyncio.sleep(0.02)
            assert any(e.time is not None for e in c.events)                  # a time-tagged spontaneous ASDU
            await c.testfr()
            await c.stopdt()
        finally:
            await c.__aexit__()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
