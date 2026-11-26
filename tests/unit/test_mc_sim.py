"""The MC 3E simulator against stock pymcprotocol 0.3.0 (unmodified `Type3E`).

Also pins two pymcprotocol behaviours the driver works around (docs/decisions.md D-053):
its single `recv(4096)` decodes a reply split across TCP segments as zeros, and its ASCII mode
writes the numbers of hex devices as decimal digits.
"""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

pymcprotocol = pytest.importorskip("pymcprotocol")

from pymcprotocol.mcprotocolerror import MCProtocolError, UnsupportedComandError  # noqa: E402

from tests.helpers.mc_sim import McSim  # noqa: E402


def _client(sim: McSim, plc_type: str = "Q", commtype: str = "binary",
            timeout: float | None = None) -> "pymcprotocol.Type3E":
    client = pymcprotocol.Type3E(plc_type)
    if commtype != "binary":
        client.setaccessopt(commtype=commtype)
    if timeout is not None:
        client.soc_timeout = timeout
    client.connect(sim.host, sim.port)
    return client


async def _run(fn, *args):
    return await asyncio.to_thread(fn, *args)


async def test_batch_read_words_q_and_iqr() -> None:
    async with await McSim.start() as sim:
        for i in range(10):
            sim.set_word("D", 100 + i, 1000 + i)
        sim.set_word("D", 110, 0xFFFE)
        for plc_type, sub in (("Q", 0x0000), ("iQ-R", 0x0002)):
            client = _client(sim, plc_type)
            try:
                got = await _run(client.batchread_wordunits, "D100", 11)
            finally:
                client.close()
            assert got == [1000 + i for i in range(10)] + [-2]          # pymcprotocol returns signed words
            assert sim.command_counts[(0x0401, sub)] == 1


async def test_word_access_to_bit_devices_lowest_point_is_bit_0() -> None:
    async with await McSim.start() as sim:
        sim.set_bit("M", 16, True)
        sim.set_bit("M", 31, True)
        sim.set_bit("X", 0x1A, True)                                     # X1A: word X10, bit 10
        client = _client(sim)
        try:
            assert await _run(client.batchread_wordunits, "M16", 1) == [-32767]   # 0x8001
            assert await _run(client.batchread_wordunits, "X10", 1) == [1 << 10]
            with pytest.raises(MCProtocolError, match="0xC05C"):          # not on a 16-point boundary
                await _run(client.batchread_wordunits, "M17", 1)
        finally:
            client.close()


async def test_bit_units_and_random_read() -> None:
    async with await McSim.start() as sim:
        for n in (0, 3, 4):
            sim.set_bit("B", n, True)
        sim.set_word("D", 0, 7)
        sim.set("D10", -123456, "int32")
        client = _client(sim)
        try:
            assert await _run(client.batchread_bitunits, "B0", 5) == [1, 0, 0, 1, 1]
            assert await _run(client.randomread, ["D0"], ["D10"]) == ([7], [-123456])
        finally:
            client.close()


async def test_read_cpu_model() -> None:
    async with await McSim.start(cpu_type="Q03UDVCPU", cpu_code=0x0366) as sim:
        client = _client(sim)
        try:
            assert await _run(client.read_cputype) == ("Q03UDVCPU", "0366")
            sim.cpu_model_end_code = 0xC059
            with pytest.raises(UnsupportedComandError):
                await _run(client.read_cputype)
        finally:
            client.close()


async def test_end_codes() -> None:
    async with await McSim.start({"D": 100}) as sim:
        client = _client(sim)
        try:
            with pytest.raises(MCProtocolError, match="0xC056"):
                await _run(client.batchread_wordunits, "D99", 2)          # past the device size
            with pytest.raises(MCProtocolError, match="0xC051"):
                await _run(client.batchread_wordunits, "M0", 961)          # more than 960 words
            sim.end_codes["D"] = 0x4031
            with pytest.raises(MCProtocolError, match="0x4031"):
                await _run(client.batchread_wordunits, "D0", 1)
            del sim.end_codes["D"]
            assert await _run(client.batchread_wordunits, "D0", 1) == [0]  # connection still usable
        finally:
            client.close()


async def test_error_reply_carries_the_error_information_block() -> None:
    async with await McSim.start({"D": 10}) as sim:
        client = _client(sim)
        try:
            data = client._make_commanddata(0x0401, 0) + client._make_devicedata("D20") + client._encode_value(1)
            await _run(client._send, client._make_senddata(data))
            reply = await _run(client._recv)
        finally:
            client.close()
        assert reply[:2] == b"\xd0\x00" and int.from_bytes(reply[7:9], "little") == 11
        assert int.from_bytes(reply[9:11], "little") == 0xC056
        assert reply[11:16] == b"\x00\xff\xff\x03\x00"                    # network, PC, module I/O, station
        assert reply[16:20] == b"\x01\x04\x00\x00"                        # command 0401, subcommand 0000


async def test_writes_and_remote_control_are_refused_and_counted() -> None:
    async with await McSim.start() as sim:
        sim.set_word("D", 0, 5)
        client = _client(sim)
        try:
            with pytest.raises(UnsupportedComandError):
                await _run(client.batchwrite_wordunits, "D0", [99])
            with pytest.raises(UnsupportedComandError):
                await _run(client.randomwrite, ["D0"], [99], [], [])
            with pytest.raises(UnsupportedComandError):
                await _run(client.remote_stop)
            assert await _run(client.batchread_wordunits, "D0", 1) == [5]
        finally:
            client.close()
        assert sim.write_attempts == 3


async def test_loopback_test() -> None:
    async with await McSim.start() as sim:
        client = _client(sim)
        try:
            assert await _run(client.echo_test, "ABC123") == (6, "ABC123")
        finally:
            client.close()


async def test_ascii_frames_work_for_decimal_devices_but_pymcprotocol_garbles_hex_devices() -> None:
    async with await McSim.start() as sim:
        sim.set_word("D", 100, 0x1234)
        sim.set_word("W", 0x1A, 0x00AA)
        sim.set_word("W", 0x26, 0x00BB)
        client = _client(sim, commtype="ascii")
        try:
            assert await _run(client.batchread_wordunits, "D100", 1) == [0x1234]
            # pymcprotocol sends W1A as the decimal digits "000026"; MC ASCII numbers W in hex,
            # so the PLC reads W26 (hex) instead of W1A.
            assert await _run(client.batchread_wordunits, "W1A", 1) == [0x00BB]
        finally:
            client.close()
        assert sim.frames["ascii"] == 2


async def test_split_tcp_reply_is_decoded_short_by_stock_pymcprotocol() -> None:
    """pymcprotocol reads a reply with ONE recv(4096): the second half of a split reply is left in
    the socket and the missing words decode as 0 (int.from_bytes(b"") == 0)."""
    async with await McSim.start() as sim:
        for i in range(200):
            sim.set_word("D", i, i + 1)
        sim.split_replies = True
        client = _client(sim)
        try:
            got = await _run(client.batchread_wordunits, "D0", 200)
        finally:
            client.close()
        assert got[:10] == list(range(1, 11))
        assert got[-1] == 0 and got != list(range(1, 201))


async def test_udp_frames() -> None:
    async with await McSim.start() as sim:
        sim.set("D2", 12.5, "float32")
        builder = pymcprotocol.Type3E("Q")
        frame = builder._make_senddata(builder._make_commanddata(0x0401, 0) + builder._make_devicedata("D2")
                                       + builder._encode_value(2))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.connect((sim.host, sim.port))
            sock.send(frame)
            reply = await _run(sock.recv, 1024)
        assert reply[:2] == b"\xd0\x00" and reply[9:11] == b"\x00\x00"
        assert struct.unpack("<f", reply[11:15])[0] == 12.5
        assert sim.frames["udp"] == 1


async def test_mute_stop_and_restart() -> None:
    from tests.helpers.netcut import stable_port

    sim = await McSim.start(port=stable_port())
    try:
        sim.set_word("D", 0, 42)
        client = _client(sim)
        client._sock.settimeout(0.3)
        sim.mute = True
        with pytest.raises(socket.timeout):
            await _run(client.batchread_wordunits, "D0", 1)
        client.close()
        sim.mute = False
        port = sim.port
        await sim.stop()
        with pytest.raises(ConnectionRefusedError):
            _client(sim, timeout=10)     # Windows reports a refused connect after ~2 s, pymcprotocol's default
        await sim.restart()
        assert sim.port == port
        client = _client(sim)
        try:
            assert await _run(client.batchread_wordunits, "D0", 1) == [42]   # memory kept
        finally:
            client.close()
    finally:
        await sim.stop()


async def test_own_encoder_round_trips() -> None:
    async with await McSim.start() as sim:
        for dtype, value in (("float32", -0.15625), ("int32", -7), ("uint32", 0xDEADBEEF), ("float64", 1e300),
                             ("int64", -(2 ** 50)), ("uint16", 65535), ("int16", -1)):
            for order in ("low_first", "high_first"):
                sim.set("D200", value, dtype, word_order=order)
                assert sim.value("D200", dtype, word_order=order) == value
        sim.set("D300", 0x00010002, "uint32")
        assert (sim.word("D", 300), sim.word("D", 301)) == (0x0002, 0x0001)   # low word first
        sim.set("D400.17", True, "bool")
        assert sim.word("D", 401) == 0x0002 and sim.value("D400.17", "bool") is True
        sim.set("DSH500.006", "AB", "string")
        assert sim.word("D", 500) == 0x4142 and sim.value("DSH500.006", "string") == "AB"
        sim.set("DSL510:004", "AB", "string")
        assert sim.word("D", 510) == 0x4241
        sim.set("M16", 0x8001, "uint16")
        assert sim.bit("M", 16) and sim.bit("M", 31) and not sim.bit("M", 17)
        sim.set("D600[2][2]", [[1, 2], [3, 4]], "int16")
        assert sim.value("D600[2][2]", "int16") == [[1, 2], [3, 4]]
        assert sim.value("ZR10", "uint16") == sim.word("ZR", 0x10)
