"""The S7 simulator against the stock python-snap7 client (no opencap code on the wire)."""

from __future__ import annotations

import asyncio
import struct

import pytest

snap7 = pytest.importorskip("snap7")

from snap7.error import S7ConnectionError, S7ProtocolError  # noqa: E402
from snap7.type import Area  # noqa: E402

from tests.helpers.netcut import stable_port  # noqa: E402
from tests.helpers.s7_sim import S7Sim, ms_to_s5time, to_bcd  # noqa: E402

AREAS = {"M": 256, "I": 16, "Q": 16, "DB1": 64, "DB100": 32, "T": 8, "C": 8}


def stock_client(sim: S7Sim, *, rack: int = 0, slot: int = 1, pdu: int = 480, ctype: int | None = None):
    c = snap7.Client()
    c.pdu_length = pdu
    if ctype is not None:
        c.set_connection_type(ctype)
    c.connect(sim.host, rack, slot, sim.port)
    return c


async def test_stock_client_reads_what_the_sim_encodes() -> None:
    async with await S7Sim.start(AREAS) as sim:
        sim.set("MD200", "float32", 12.5)
        sim.set("DB100,D04", "float32", -3.25)
        sim.set("DB1,W2", "uint16", 0xBEEF)
        sim.set("DB1,DI8", "int32", -123456)
        sim.set("M10.3", "bool", True)
        sim.set("C3", "uint16", 42)
        sim.set("T1", "uint32", 25_000)
        sim.set("DB1,STRING20.10", "string", "PUMP")

        def go():
            c = stock_client(sim)
            try:
                return (c.read_area(Area.MK, 0, 200, 4), c.db_read(100, 4, 4), c.db_read(1, 2, 2),
                        c.db_read(1, 8, 4), c.read_area(Area.MK, 0, 10, 1), c.ct_read(3, 1), c.tm_read(1, 1),
                        c.db_read(1, 20, 12), c.get_pdu_length())
            finally:
                c.disconnect()

        md, d04, w2, di8, m10, c3, t1, s, pdu = await asyncio.to_thread(go)
        assert struct.unpack(">f", md)[0] == 12.5
        assert struct.unpack(">f", d04)[0] == -3.25
        assert struct.unpack(">H", w2)[0] == 0xBEEF
        assert struct.unpack(">i", di8)[0] == -123456
        assert m10[0] & 0x08
        assert struct.unpack(">H", c3)[0] == to_bcd(42) == 0x0042
        assert struct.unpack(">H", t1)[0] == ms_to_s5time(25_000) == 0x1250
        assert s[:6] == bytes([10, 4]) + b"PUMP"
        assert pdu == 480
        assert sim.value("DB100,D04", "float32") == -3.25 and sim.value("T1", "uint32") == 25_000
        assert sim.value("C3", "uint16") == 42 and sim.value("DB1,STRING20.10", "string") == "PUMP"
        assert sim.read_count == 8 and sim.write_attempts == 0
        assert sim.tsaps == [(1, 0, 1)]                                      # snap7's default: PG


async def test_pdu_is_capped_and_oversize_reads_are_refused() -> None:
    async with await S7Sim.start({"DB1": 1000}, max_pdu=240) as sim:
        def go():
            c = stock_client(sim, pdu=960)
            try:
                pdu = c.get_pdu_length()
                req = c.protocol.build_read_request(area=snap7.datatypes.S7Area.DB, db_number=1, start=0,
                                                    word_len=snap7.datatypes.S7WordLen.BYTE, count=300)
                try:
                    c._send_receive(req)
                except S7ProtocolError as exc:
                    return pdu, exc.error_code
                return pdu, None
            finally:
                c.disconnect()

        pdu, err = await asyncio.to_thread(go)
        assert (pdu, err) == (240, 0x8500)
        assert sim.pdu_requests == [960] and sim.oversize_reads == 1


async def test_item_return_codes() -> None:
    async with await S7Sim.start({"DB1": 16}) as sim:
        def go():
            c = stock_client(sim)
            out = []
            try:
                for db, start, size in ((2, 0, 4), (1, 14, 4)):
                    try:
                        c.db_read(db, start, size)
                    except S7ProtocolError as exc:
                        out.append(str(exc))
            finally:
                c.disconnect()
            return out

        missing, past_end = await asyncio.to_thread(go)
        assert "(0x0a)" in missing and "(0x05)" in past_end


async def test_wrong_rack_slot_is_refused() -> None:
    async with await S7Sim.start({"DB1": 16}, rack=0, slot=2) as sim:
        with pytest.raises(S7ConnectionError):
            await asyncio.to_thread(stock_client, sim, slot=1)
        c = await asyncio.to_thread(stock_client, sim, slot=2, ctype=3)
        await asyncio.to_thread(c.disconnect)
        assert sim.tsaps == [(1, 0, 1), (3, 0, 2)] and len(sim.rejected_tsaps) == 1


async def test_writes_are_refused_and_counted() -> None:
    async with await S7Sim.start({"DB1": 16}) as sim:
        sim.set("DB1,W0", "uint16", 7)

        def go():
            c = stock_client(sim)
            try:
                with pytest.raises(Exception):
                    c.db_write(1, 0, bytearray(b"\x00\x09"))
                with pytest.raises(Exception):
                    c.plc_stop()
            finally:
                c.disconnect()

        await asyncio.to_thread(go)
        assert sim.write_attempts == 2 and sim.value("DB1,W0", "uint16") == 7


async def test_identity_szl_and_power_cycle() -> None:
    port = stable_port()
    sim = await S7Sim.start({"DB1": 16}, port=port)
    try:
        c = await asyncio.to_thread(stock_client, sim)
        info = await asyncio.to_thread(c.get_cpu_info)
        assert info.ModuleTypeName and sim.szl_count >= 1
        await sim.stop()
        with pytest.raises(S7ConnectionError):
            await asyncio.to_thread(c.db_read, 1, 0, 2)
        with pytest.raises(S7ConnectionError):
            await asyncio.to_thread(stock_client, sim)
        await sim.restart()
        assert sim.port == port
        c = await asyncio.to_thread(stock_client, sim)
        assert await asyncio.to_thread(c.db_read, 1, 0, 2) == bytearray(2)
        await asyncio.to_thread(c.disconnect)
    finally:
        await sim.stop()
