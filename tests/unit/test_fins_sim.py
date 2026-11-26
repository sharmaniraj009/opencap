"""The Omron FINS simulator (#13), validated two independent ways:

1. hand-built frames: an encoder written here from the manual's frame layouts (byte literals, no
   simulator code), over raw UDP and TCP sockets;
2. stock fins-driver 0.3.1 (MIT, https://github.com/indrarudianto/fins-driver/blob/v0.3.1/LICENSE),
   unmodified, over FINS/UDP. Its "tcp" mode writes bare FINS frames on a TCP socket without the
   FINS/TCP header, so it cannot talk to a real Ethernet unit either; the test below pins that the
   simulator refuses it, and FINS/TCP is proven with the hand-built frames.

Also checks the address-parsing vectors in tests/conformance/omron_fins/ against the simulator's
own parser.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from pathlib import Path

import pytest

from tests.conformance.vectors import load as load_vectors
from tests.helpers.fins_sim import (
    END_ADDRESS,
    END_AREA,
    END_RANGE,
    END_RESPONSE_TOO_LONG,
    END_TOO_LONG,
    END_TOO_SHORT,
    END_UNSUPPORTED,
    MAX_READ_WORDS,
    AddressError,
    FinsSim,
    parse_address,
)
from tests.helpers.netcut import stable_port

NODE = 10            # the simulator's FINS node (DA1)
CLIENT = 0xEF        # our source node (SA1) on UDP


# --- an independent FINS encoder (from the manual's layouts; shares nothing with the simulator) -------

def header(*, sid: int = 0x01, da1: int = NODE, sa1: int = CLIENT, icf: int = 0x80) -> bytes:
    # ICF RSV GCT DNA DA1 DA2 SNA SA1 SA2 SID
    return bytes((icf, 0x00, 0x02, 0x00, da1, 0x00, 0x00, sa1, 0x00, sid))


def read_cmd(area: int, word: int, bit: int, count: int, **kw: int) -> bytes:
    return header(**kw) + b"\x01\x01" + bytes((area,)) + word.to_bytes(2, "big") + bytes((bit,)) \
        + count.to_bytes(2, "big")


def split_reply(frame: bytes) -> tuple[bytes, int, int, bytes]:
    """FINS response -> (header, command, end code, data)."""
    return frame[:10], int.from_bytes(frame[10:12], "big"), int.from_bytes(frame[12:14], "big"), frame[14:]


def udp_exchange(sim: FinsSim, frame: bytes, *, timeout: float = 2.0) -> bytes | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(frame, (sim.host, sim.port))
        try:
            return s.recv(4096)
        except TimeoutError:
            return None
        except ConnectionResetError:     # Windows: ICMP port unreachable (nothing listens) shows up here
            return None


def tcp_msg(command: int, payload: bytes = b"", error: int = 0) -> bytes:
    return b"FINS" + struct.pack(">III", 8 + len(payload), command, error) + payload


def tcp_recv(s: socket.socket) -> tuple[int, int, bytes]:
    """One FINS/TCP message -> (command, error code, payload); reads by the length field."""
    head = _recv_exactly(s, 16)
    assert head[:4] == b"FINS"
    length, command, error = struct.unpack(">III", head[4:16])
    return command, error, _recv_exactly(s, length - 8)


def _recv_exactly(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"closed after {len(buf)} of {n} bytes")
        buf += chunk
    return buf


def tcp_connect(sim: FinsSim, client_node: int = 0) -> tuple[socket.socket, int, int]:
    s = socket.create_connection((sim.host, sim.port), timeout=2.0)
    s.sendall(tcp_msg(0, client_node.to_bytes(4, "big")))
    command, error, payload = tcp_recv(s)
    assert (command, error) == (1, 0), (command, error)
    return s, int.from_bytes(payload[:4], "big"), int.from_bytes(payload[4:8], "big")


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# --- golden frames over UDP ---------------------------------------------------------------------------

async def test_memory_area_read_golden_frame_udp() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("D", 100, 0x1234)
        sim.set_word("D", 101, 0xABCD)
        # MEMORY AREA READ, DM (0x82) word 100 (0x0064), bit 00, 2 items
        reply = await _run(udp_exchange, sim, bytes.fromhex("80 00 02 00 0A 00 00 EF 00 2A 0101 82 0064 00 0002"))
        # response: ICF C0, source/destination swapped, SID echoed, MRC/SRC, end code 0000, data
        assert reply == bytes.fromhex("C0 00 02 00 EF 00 00 0A 00 2A 0101 0000 1234 ABCD")
        assert sim.request_count == 1 and sim.command_counts[0x0101] == 1 and sim.frames["udp"] == 1


async def test_bit_reads_are_one_byte_per_bit() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_bit("CIO", 10, 5, True)
        sim.set_bit("CIO", 10, 7, True)
        sim.set_bit("CIO", 11, 0, True)
        # CIO bit (0x30) 10.05, 4 bits: 10.05, 10.06, 10.07, 10.08
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x30, 10, 5, 4)))
        assert (end, data) == (0, b"\x01\x00\x01\x00")
        # bits run on into the next word: 10.15, 11.00
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x30, 10, 15, 2)))
        assert (end, data) == (0, b"\x00\x01")


@pytest.mark.parametrize(("area", "word_code", "bit_code"), [
    ("CIO", 0xB0, 0x30), ("W", 0xB1, 0x31), ("H", 0xB2, 0x32), ("A", 0xB3, 0x33), ("D", 0x82, 0x02),
    ("E0", 0xA0, 0x20), ("E2", 0xA2, 0x22),
])
async def test_area_codes(area: str, word_code: int, bit_code: int) -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word(area, 7, 0x8001)
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(word_code, 7, 0, 1)))
        assert (end, data) == (0, b"\x80\x01")
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(bit_code, 7, 15, 1)))
        assert (end, data) == (0, b"\x01")


async def test_em_banks_beyond_c_and_the_current_bank() -> None:
    async with await FinsSim.start(node=NODE, em_banks=(0, 0x0D, 0x10, 0x18)) as sim:
        sim.set_word("ED", 1, 0x00D1)
        sim.set_word("E10", 1, 0x0101)
        sim.set_word("E18", 1, 0x0181)
        sim.set_word("E0", 1, 0x0001)
        for code, want in ((0xAD, 0x00D1), (0x60, 0x0101), (0x68, 0x0181), (0x98, 0x0001)):
            _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(code, 1, 0, 1)))
            assert (end, data) == (0, want.to_bytes(2, "big")), hex(code)
        sim.current_bank = 0x10
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x98, 1, 0, 1)))
        assert (end, data) == (0, b"\x01\x01")
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0xE0, 1, 0, 1)))   # bank 10 bit
        assert (end, data) == (0, b"\x01")
        # bank 1 is not fitted: the area does not exist
        _, _, end, _ = split_reply(await _run(udp_exchange, sim, read_cmd(0xA1, 0, 0, 1)))
        assert end == END_AREA


async def test_timers_and_counters() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("T", 3, 0x0150)
        sim.set_word("C", 3, 0x0042)
        sim.set_flag("T", 3, True)
        sim.set_flag("C", 4, True)
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x89, 3, 0, 1)))
        assert (end, data) == (0, b"\x01\x50")                          # timer PV T0003
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x89, 0x8003, 0, 1)))
        assert (end, data) == (0, b"\x00\x42")                          # counter PV C0003 = 8003 hex
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x09, 3, 0, 2)))
        assert (end, data) == (0, b"\x01\x00")                          # timer flags T0003, T0004
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x09, 0x8003, 0, 2)))
        assert (end, data) == (0, b"\x00\x01")                          # counter flags C0003, C0004


async def test_multiple_memory_area_read_golden_frame() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("D", 0, 0x0102)
        sim.set_bit("CIO", 1, 2, True)
        sim.set_word("H", 511, 0xFFFF)
        sim.set_flag("C", 0, True)
        frame = header(sid=7) + bytes.fromhex("0104" "82000000" "30000102" "B201FF00" "09800000")
        hdr, cmd, end, data = split_reply(await _run(udp_exchange, sim, frame))
        assert (cmd, end, hdr[9]) == (0x0104, 0, 7)
        # each item: its area code, then 2 bytes (word) or 1 byte (bit / flag)
        assert data == bytes.fromhex("82 0102" "30 01" "B2 FFFF" "09 01")


async def test_controller_data_read_gives_the_model() -> None:
    async with await FinsSim.start(node=NODE, model="CJ2M-CPU33", version="02.00") as sim:
        # W342 5-3-15: no data = everything (159 bytes), 00 = model..area data (92), 01 = units (67)
        for request, size in ((b"", 159), (b"\x00", 92), (b"\x01", 67)):
            _, cmd, end, data = split_reply(await _run(udp_exchange, sim, header() + b"\x05\x01" + request))
            assert (cmd, end, len(data)) == (0x0501, 0, size)
            if request != b"\x01":
                assert data[:20] == b"CJ2M-CPU33".ljust(20)                     # space padded
                assert data[20:40] == b"02.00".ljust(20, b"\x00")              # null padded
        _, _, end, _ = split_reply(await _run(udp_exchange, sim, header() + b"\x05\x01\x02"))
        assert end == 0x110C
        sim.cpu_data_end_code = 0x2201
        _, _, end, _ = split_reply(await _run(udp_exchange, sim, header() + b"\x05\x01"))
        assert end == 0x2201


async def test_registers_task_flags_and_em_bank_number() -> None:
    async with await FinsSim.start(node=NODE, em_banks=(0, 1, 2)) as sim:
        sim.set("IR3", 0x12345678)
        sim.set("DR15", 0xBEEF)
        sim.set("TK05", True)
        sim.current_bank = 2
        # IR: code DC, addresses 0100-010F hex, 4 bytes per element (W342 5-2-2)
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0xDC, 0x0103, 0, 1)))
        assert (end, data) == (0, b"\x12\x34\x56\x78")
        # DR: code BC, addresses 0200-020F hex
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0xBC, 0x020F, 0, 1)))
        assert (end, data) == (0, b"\xbe\xef")
        # task flags: code 06, one byte per flag
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x06, 4, 0, 3)))
        assert (end, data) == (0, b"\x00\x01\x00")
        # current EM bank number: code BC, address 0F0000
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0xBC, 0x0F00, 0, 1)))
        assert (end, data) == (0, b"\x00\x02")
        frame = header() + bytes.fromhex("0104" "DC010300" "BC020F00" "06000500")
        _, _, end, data = split_reply(await _run(udp_exchange, sim, frame))
        assert (end, data) == (0, bytes.fromhex("DC 12345678" "BC BEEF" "06 01"))


async def test_zero_items_is_a_normal_completion() -> None:
    """W342 5-3-2 note 1: a count of 0000 reads nothing and returns a normal response."""
    async with await FinsSim.start(node=NODE) as sim:
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 0)))
        assert (end, data) == (0, b"")


async def test_cpu_error_bits_ride_on_a_normal_completion() -> None:
    """SRES bit 6 (non-fatal) / bit 7 (fatal CPU error) with valid data: a driver masks them."""
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("D", 0, 0x0102)
        sim.cpu_error_bits = 0x40
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1)))
        assert (end, data) == (0x0040, b"\x01\x02")
        sim.cpu_error_bits = 0x80
        _, _, end, _ = split_reply(await _run(udp_exchange, sim, read_cmd(0x99, 0, 0, 1)))
        assert end == END_AREA | 0x80 and end & 0x7F3F == END_AREA


# --- end codes --------------------------------------------------------------------------------------

@pytest.mark.parametrize(("frame", "end"), [
    (read_cmd(0x99, 0, 0, 1), END_AREA),                          # no such area code
    (read_cmd(0x82, 32768, 0, 1), END_ADDRESS),                   # first address outside DM
    (read_cmd(0x82, 32767, 0, 2), END_RANGE),                     # runs past the end of DM
    (read_cmd(0x82, 0, 0, MAX_READ_WORDS + 1), END_RESPONSE_TOO_LONG),
    (read_cmd(0x82, 0, 3, 1), END_ADDRESS),                       # word access with a bit number
    (read_cmd(0x02, 0, 16, 1), END_ADDRESS),                      # bit number above 15
    (read_cmd(0x31, 511, 15, 2), END_RANGE),                      # bits past W511.15
    (header() + b"\x01\x01\x82\x00\x00\x00\x00", END_TOO_SHORT),
    (header() + b"\x01\x01\x82\x00\x00\x00\x00\x01\x00", END_TOO_LONG),
    (header() + b"\x01\x04\x82\x00\x00", END_TOO_SHORT),          # partial 0104 item
    (header() + b"\x01\x04" + b"\x82\x00\x00\x00" * 168, END_TOO_LONG),
    (header() + b"\x06\x20", END_UNSUPPORTED),                     # a command the simulator lacks
], ids=["area", "first-address", "range-end", "too-many-words", "word-with-bit", "bit-16",
        "bits-past-end", "short-0101", "long-0101", "partial-0104", "too-many-0104", "unsupported"])
async def test_end_codes(frame: bytes, end: int) -> None:
    async with await FinsSim.start(node=NODE) as sim:
        _, _, got, data = split_reply(await _run(udp_exchange, sim, frame))
        assert (got, data) == (end, b"")
        assert sim.error_replies[end] == 1


async def test_max_words_per_read_is_accepted() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("D", MAX_READ_WORDS - 1, 0x7777)
        reply = await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, MAX_READ_WORDS))
        _, _, end, data = split_reply(reply)
        assert end == 0 and len(data) == 2 * MAX_READ_WORDS and data[-2:] == b"\x77\x77"
        assert len(reply) == 2012                                        # the Ethernet maximum frame


async def test_end_code_switches_per_area_and_global() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.area_end_codes["H"] = 0x2002                                 # protected
        _, _, end, _ = split_reply(await _run(udp_exchange, sim, read_cmd(0xB2, 0, 0, 1)))
        assert end == 0x2002
        _, _, end, _ = split_reply(await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1)))
        assert end == 0
        frame = header() + bytes.fromhex("0104" "82000000" "B2000000")
        _, _, end, data = split_reply(await _run(udp_exchange, sim, frame))
        assert (end, data) == (0x2002, b"")
        sim.end_code = 0x0205                                            # response time-out
        _, _, end, _ = split_reply(await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1)))
        assert end == 0x0205


async def test_write_commands_are_refused_and_counted() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("D", 0, 0x1111)
        for frame in (
            header() + bytes.fromhex("0102 82 0000 00 0001 BEEF"),         # memory area write
            header() + bytes.fromhex("0103 82 0000 00 0001 BEEF"),         # memory area fill
            header() + bytes.fromhex("0105 82 0000 00 82 0010 00 0001"),   # memory area transfer
            header() + bytes.fromhex("0401 FFFF 04"),                      # run
            header() + bytes.fromhex("0402 FFFF"),                         # stop
            header() + bytes.fromhex("2301 0001 0001 30 0000 00"),         # forced set
        ):
            _, _, end, _ = split_reply(await _run(udp_exchange, sim, frame))
            assert end == END_UNSUPPORTED
        assert sim.write_attempts == 6
        assert sim.word("D", 0) == 0x1111 and sim.word("D", 16) == 0


# --- fault switches ---------------------------------------------------------------------------------

async def test_mute_counts_but_does_not_answer() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.mute = True
        assert await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1), timeout=0.3) is None
        assert sim.request_count == 1
        sim.mute = False
        assert await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1)) is not None


async def test_short_reply_sid_offset_and_no_response_required() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.short_reply = True
        reply = await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 2))
        assert len(reply) == 14 + 4 - 1
        sim.short_reply = False
        sim.sid_offset = 1
        hdr, _, _, _ = split_reply(await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1, sid=0x41)))
        assert hdr[9] == 0x42
        sim.sid_offset = 0
        # ICF bit 0: response not required
        assert await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1, icf=0x81), timeout=0.3) is None
        assert sim.request_count == 3


async def test_strict_node_drops_frames_for_another_node() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        assert await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1, da1=NODE + 1), timeout=0.3) is None
        assert sim.misaddressed == 1 and sim.request_count == 0
        sim.strict_node = False
        hdr, _, end, _ = split_reply(await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1, da1=0)))
        assert end == 0 and hdr[7] == NODE                              # SA1 is the real node


async def test_restart_keeps_port_and_memory() -> None:
    async with await FinsSim.start(node=NODE, port=stable_port()) as sim:
        sim.set_word("D", 5, 0x0505)
        port = sim.port
        await sim.stop()
        assert not sim.running
        assert await _run(udp_exchange, sim, read_cmd(0x82, 5, 0, 1), timeout=0.3) is None
        await sim.restart()
        assert sim.port == port
        _, _, end, data = split_reply(await _run(udp_exchange, sim, read_cmd(0x82, 5, 0, 1)))
        assert (end, data) == (0, b"\x05\x05")


async def test_reply_delay(no_gc) -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.reply_delay = 0.4
        assert await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1), timeout=0.1) is None
        assert await _run(udp_exchange, sim, read_cmd(0x82, 0, 0, 1), timeout=3.0) is not None


# --- FINS/TCP ---------------------------------------------------------------------------------------

async def test_tcp_handshake_allocates_a_node_and_carries_frames() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("D", 100, 0x0BAD)

        def go():
            s, client, server = tcp_connect(sim, 0)
            try:
                frame = read_cmd(0x82, 100, 0, 1, sa1=client, sid=9)
                s.sendall(tcp_msg(2, frame))
                return client, server, tcp_recv(s)
            finally:
                s.close()

        client, server, (command, error, payload) = await _run(go)
        assert server == NODE and 239 <= client <= 254
        assert (command, error) == (2, 0)
        hdr, cmd, end, data = split_reply(payload)
        assert (cmd, end, data) == (0x0101, 0, b"\x0b\xad")
        assert hdr[4] == client and hdr[7] == NODE and hdr[9] == 9
        assert sim.handshakes == [client] and sim.frames["tcp"] == 1


async def test_tcp_handshake_errors() -> None:
    async with await FinsSim.start(node=NODE) as sim:

        def attempt(node: int) -> tuple[int, int]:
            with socket.create_connection((sim.host, sim.port), timeout=2.0) as s:
                s.sendall(tcp_msg(0, node.to_bytes(4, "big")))
                command, error, _ = tcp_recv(s)
                assert s.recv(16) == b""                                # closed after the error
                return command, error

        def go():
            first, client, _ = tcp_connect(sim, 5)
            try:
                return client, attempt(5), attempt(NODE), attempt(300)
            finally:
                first.close()

        client, in_use, same, out_of_range = await _run(go)
        assert client == 5
        assert in_use == (1, 0x21) and same == (1, 0x24) and out_of_range == (1, 0x23)


async def test_tcp_rejects_bad_magic_and_frames_before_the_handshake() -> None:
    async with await FinsSim.start(node=NODE) as sim:

        def go(first: bytes) -> tuple[int, int]:
            with socket.create_connection((sim.host, sim.port), timeout=2.0) as s:
                s.sendall(first)
                command, error, _ = tcp_recv(s)
                assert s.recv(16) == b""
                return command, error

        assert await _run(go, b"SNIF" + bytes(12)) == (3, 0x01)
        assert await _run(go, tcp_msg(2, read_cmd(0x82, 0, 0, 1))) == (3, 0x03)
        assert await _run(go, tcp_msg(0, bytes(4))[:4] + (8 + 4000).to_bytes(4, "big") + bytes(8)) == (3, 0x02)
        assert sim.request_count == 0


async def test_tcp_split_replies_and_two_clients() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.split_replies = True
        sim.set_word("W", 0, 0x0102)

        def go():
            a, na, _ = tcp_connect(sim)
            b, nb, _ = tcp_connect(sim)
            try:
                a.sendall(tcp_msg(2, read_cmd(0xB1, 0, 0, 1, sa1=na)))
                b.sendall(tcp_msg(2, read_cmd(0xB1, 0, 0, 1, sa1=nb)))
                return na, nb, tcp_recv(a), tcp_recv(b)
            finally:
                a.close()
                b.close()

        na, nb, ra, rb = await _run(go)
        assert na != nb
        for _, _, payload in (ra, rb):
            assert split_reply(payload)[2:] == (0, b"\x01\x02")


# --- stock fins-driver (MIT), unmodified, over FINS/UDP ---------------------------------------------

try:                                     # fins-driver's import name is `fins`
    from fins import client as fins_client
except ImportError:                      # pragma: no cover - the dev extra installs it
    fins_client = None
needs_fins_driver = pytest.mark.skipif(fins_client is None or not hasattr(fins_client, "FinsClient"),
                                       reason="fins-driver (dev extra) is not installed")


def stock_client(sim: FinsSim, mode: str = "udp"):
    c = fins_client.FinsClient(host=sim.host, port=sim.port, mode=mode, timeout=2)
    c.da1 = sim.node            # configuration, not a modification: its default DA1 is 0
    c.sa1 = CLIENT
    c.connect()
    return c


@needs_fins_driver
async def test_stock_client_reads_words_bits_and_multiple_areas() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set("D100", 12.5, "float32")                                # low word first (Omron native)
        sim.set("D102", -123456, "int32")
        sim.set("H20", 0xBEEF)
        sim.set("CIO10.05", True, "bool")
        sim.set("W3", 1234, "bcd16")
        sim.set("A448.15", True, "bool")
        sim.set("E01:00100", 0x0E01)

        def go():
            c = stock_client(sim)
            try:
                return (c.memory_area_read("D100", 4), c.memory_area_read("H20"),
                        c.memory_area_read("CIO10.05"), c.memory_area_read("W3"),
                        c.memory_area_read("A448.15"),
                        c.memory_area_read(b"\xa1\x00\x64\x00"),             # E1_100: area code A1
                        c.multiple_memory_area_read("D100", "CIO10.05", "H20", "CIO10.04"))
            finally:
                c.close()

        d100, h20, bit, w3, a448, em, multi = await _run(go)
        for r in (d100, h20, bit, w3, a448, em, multi):
            assert r.ok, r.status_text
        # 12.5 = 0x41480000: D100 holds the low word 0000, D101 the high word 4148
        assert d100.data == bytes.fromhex("0000 4148") + struct.pack(">i", -123456)[2:] + struct.pack(">i", -123456)[:2]
        assert h20.data == b"\xbe\xef" and bit.data == b"\x01" and w3.data == b"\x12\x34" and a448.data == b"\x01"
        assert em.data == b"\x0e\x01"
        assert multi.data == [b"\x00\x00", b"\x01", b"\xbe\xef", b"\x00"]
        assert sim.command_counts[0x0101] == 6 and sim.command_counts[0x0104] == 1
        assert sim.last_header[7] == CLIENT


@needs_fins_driver
async def test_stock_client_sees_end_codes_and_write_refusal() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set_word("D", 0, 7)

        def go():
            c = stock_client(sim)
            try:
                return (c.memory_area_read("D32767", 2), c.memory_area_read("D32768"),
                        c.memory_area_write("D0", b"\x00\x08"), c.memory_area_fill("D0", b"\x00\x09", 4),
                        c.memory_area_transfer("D0", "D10", 1), c.stop(), c.memory_area_read("D0"))
            finally:
                c.close()

        past_end, outside, write, fill, transfer, stop, d0 = await _run(go)
        assert past_end.code == b"\x11\x04" and outside.code == b"\x11\x03"
        for r in (write, fill, transfer, stop):
            assert r.code == b"\x04\x01"
        assert sim.write_attempts == 4
        assert d0.ok and d0.data == b"\x00\x07" and sim.word("D", 10) == 0


@needs_fins_driver
async def test_stock_client_tcp_mode_is_not_fins_tcp() -> None:
    """fins-driver's "tcp" mode sends a bare FINS frame on the socket; a real Ethernet unit
    expects the FINS/TCP header and node handshake first. The simulator answers error 1 (not
    "FINS") and closes, like the unit, and executes nothing."""
    async with await FinsSim.start(node=NODE) as sim:

        def go():
            c = stock_client(sim, mode="tcp")
            try:
                c.memory_area_read("D0")
            except OSError:
                pass
            finally:
                c.close()

        await _run(go)
        assert sim.tcp_errors[0x01] == 1 and sim.request_count == 0


# --- the simulator's value encoder and address parser ------------------------------------------------

async def test_value_round_trips_and_word_order() -> None:
    async with await FinsSim.start(node=NODE) as sim:
        sim.set("D20", 0x11223344, "uint32", word_order="high_first")
        assert (sim.word("D", 20), sim.word("D", 21)) == (0x1122, 0x3344)
        sim.set("D30", 0x11223344, "uint32")
        assert (sim.word("D", 30), sim.word("D", 31)) == (0x3344, 0x1122)       # Omron native: low word first
        sim.set("D32", -2.5, "float32")
        assert sim.value("D32", "float32") == -2.5 and sim.word("D", 33) == 0xC020
        sim.set("D40", 12345678, "bcd32")
        assert (sim.word("D", 40), sim.word("D", 41)) == (0x5678, 0x1234)
        assert sim.value("D40", "bcd32") == 12345678
        sim.set("T3", 150)                                                    # timer default type: BCD
        assert sim.word("T", 3) == 0x0150 and sim.value("T3") == 150
        # the 'D' suffix: Word as unsigned BCD, Short as signed BCD (PTC manual's example: -50 -> 8050)
        sim.set("D50D", -50, "int16")
        assert sim.word("D", 50) == 0x8050 and sim.value("D50D", "int16") == -50
        assert sim.value("D50D", "uint16") == 8050 and sim.value("D50", "bcd16") == 8050
        sim.set("D60[2][3]", [[1, 2, 3], [4, 5, 6]], "int16")
        assert sim.value("D60[2][3]", "int16") == [[1, 2, 3], [4, 5, 6]] and sim.word("D", 65) == 6
        sim.set("D70.006H", "PUMP01")
        assert sim.word("D", 70) == 0x5055 and sim.value("D70.006H") == "PUMP01"
        sim.set("D80.006L", "VALVE1")
        assert sim.word("D", 80) == 0x4156 and sim.value("D80.006L") == "VALVE1"
        sim.set("D90.003D", "ABC")
        assert [sim.word("D", 90 + i) for i in range(3)] == [0x4100, 0x4200, 0x4300]
        sim.set("D95.002E", "XY")
        assert [sim.word("D", 95 + i) for i in range(2)] == [0x0058, 0x0059] and sim.value("D95.002E") == "XY"
        sim.set("TS7", True)
        assert sim.flag("T", 7) and sim.value("TS7")
        sim.set("IR0", -1.5, "float32")
        assert sim.word("IR", 0) == 0xBFC00000 and sim.value("IR0", "float32") == -1.5
        sim.current_bank = 2
        sim.set("E00009", 0x0909)                                             # current bank
        sim.set("E01:00009", 0x0109)
        assert sim.word("E2", 9) == 0x0909 and sim.word("E1", 9) == 0x0109
        with pytest.raises(ValueError):
            sim.set("E05:0", 1)                                               # bank 5 is not fitted


def _hand_written() -> dict[str, list[tuple[dict, dict]]]:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "gen_conformance_fins.py"
    spec = importlib.util.spec_from_file_location("gen_conformance_fins", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.hand_written()


def _vectors(stem: str) -> list:
    return [v for v in load_vectors("omron_fins/addresses") if v.file.endswith(f"/{stem}.json")]


@pytest.mark.parametrize("stem", ["valid", "invalid"])
def test_vectors_equal_the_hand_written_table(stem: str) -> None:
    """The format-1 files (generated from the simulator's parser) must equal the expectations written
    by hand from the PTC manual and W342 in scripts/gen_conformance_fins.py: a parser bug cannot
    silently become a vector."""
    files = [(v.input, v.expected) for v in _vectors(stem)]
    assert files == _hand_written()[stem]


@pytest.mark.parametrize("vector", _vectors("invalid"), ids=lambda v: v.key)
def test_invalid_vectors_raise_address_error(vector) -> None:
    with pytest.raises(AddressError) as exc:
        parse_address(vector.input["address"], vector.input.get("dtype"), model=vector.input.get("model", "CJ1"))
    assert exc.value.reason == vector.expected["error"]


def test_vectors_cover_every_area_and_kind() -> None:
    valid = [v.expected for v in _vectors("valid")]
    codes = {e["area_code"] for e in valid}
    assert {"0xB0", "0x30", "0xB1", "0x31", "0xB2", "0x32", "0xB3", "0x33", "0x82", "0x02", "0xA0", "0x20",
            "0x60", "0xE0", "0x98", "0x0A", "0x89", "0x09", "0x06", "0xDC", "0xBC"} <= codes
    assert {e["kind"] for e in valid} == {"word", "bit", "flag", "string"}
    assert {e["str_order"] for e in valid} >= {"H", "L", "D", "E"}
    assert {v.expected["error"] for v in _vectors("invalid")} == {"syntax", "range", "unsupported"}
