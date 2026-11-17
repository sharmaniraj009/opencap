"""The PCCC simulator against stock pycomm3 1.2.16 `SLCDriver` (unmodified, run in a thread)."""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import struct
import time

import pytest
from pycomm3 import CommError, SLCDriver
from pycomm3.cip import UINT
from pycomm3.exceptions import ResponseError
from pycomm3.packets import SendUnitDataRequestPacket

from opencap.config.models import Device, Tag
from opencap.drivers.ab_pccc import AbPcccDriver, PcccBlock, coerce_value, decode_block, parse_pccc_address
from opencap.drivers.base import DecodeError

from tests.helpers.netcut import stable_port
from tests.helpers.pccc_sim import DEFAULT_PRODUCT_NAME, PcccSim, file_type_conflicts, tables_for

ERR_ADDRESS = "Illegal Command or Format, Address may not exist or not enough elements in data file"
ERR_EXT_STS = "Error code in EXT STS Byte"
ERR_PROTECTED = "Function not allows due to command protection selection"
TYPED_READ, DIR_READ, DIAG_STATUS, MASKED_WRITE = (0x0F, 0xA2), (0x0F, 0xA1), (0x06, 0x03), (0x0F, 0xAB)
LARGE_FO, FO = 0x5B, 0x54


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


TABLES: dict[str, list] = {
    "O": [0x0F0F, 3],
    "I": [[0x00FF, -2], [0x1234]],                     # slot 0: 2 words, slot 1: 1 word
    "S": [0, 0x0040, 7, 8],
    "B3": [0, 0x8001, 0x0002, 0x7FFF],
    "T4": [(0x2000, 100, 42), (0xC000, 5, 1)],          # DN | EN+TT
    "C5": [(0xA000, 7, 3), (0x1C00, 9, 9)],             # CU+DN | OV+UN+UA
    "N7": [-5, 32767, -32768, 65535, *range(16)],       # 65535 is stored as -1
    "F8": [f32(1.5 + i * 0.37) * (-1) ** i for i in range(63)],
    "ST9": ["hello", "", "x" * 82],
    "L10": [-70000, 2**31 - 1, -2**31],
    "N11": [i * 257 - 16000 for i in range(127)],
}


@pytest.fixture
async def start_sim():
    sims: list[PcccSim] = []

    async def _start(tables=None, **kw) -> PcccSim:
        sim = await PcccSim.start(TABLES if tables is None else tables, **kw)
        sims.append(sim)
        return sim

    yield _start
    for sim in sims:
        await sim.stop()


@pytest.fixture
async def connect(start_sim):
    """Open a pycomm3 SLCDriver on a sim; every driver is closed (on the loop) at teardown."""
    drivers: list[SLCDriver] = []

    async def _connect(sim_or_path, *, timeout: float = 2.0) -> SLCDriver:
        path = sim_or_path if isinstance(sim_or_path, str) else sim_or_path.path
        plc = SLCDriver(path)
        plc.socket_timeout = timeout
        drivers.append(plc)
        await asyncio.to_thread(plc.open)
        return plc

    yield _connect
    for plc in drivers:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(plc.close)


async def read(plc: SLCDriver, *addresses: str):
    return await asyncio.to_thread(plc.read, *addresses)


async def until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


def raw_pccc(plc: SLCDriver, cmd: int, fnc: int | None, payload: bytes = b"") -> bytes:
    """Send one hand-built Execute PCCC on the open connection; return the raw reply."""
    msg = plc._msg_start() + bytes([cmd, 0]) + UINT.encode(next(plc._sequence))
    msg += (bytes([fnc]) if fnc is not None else b"") + payload
    request = SendUnitDataRequestPacket(plc._sequence)
    request.add(msg)
    return plc.send(request).raw


# --- open / identity -------------------------------------------------------------------------------

async def test_open_registers_a_session_and_the_first_read_opens_a_connection(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    assert plc.connected and sim.clients == 1
    assert sim.open_connections == 0 and not sim.cm_counts      # open() = RegisterSession only

    assert (await read(plc, "N7:0")).value == -5
    # SLC/MicroLogix refuse the Large Forward Open; pycomm3 falls back to the standard one
    assert sim.cm_counts[LARGE_FO] == 1 and sim.cm_counts[FO] == 1
    assert sim.connection_sizes == [500] and plc.connection_size == 500
    assert sim.open_connections == 1

    await asyncio.to_thread(plc.close)
    assert not plc.connected
    await until(lambda: sim.clients == 0)                       # Forward Close + UnRegisterSession
    assert sim.open_connections == 0


async def test_large_forward_open_when_enabled(start_sim, connect):
    sim = await start_sim(large_forward_open=True)
    plc = await connect(sim)
    await read(plc, "N7:0")
    assert sim.connection_sizes == [4000] and sim.cm_counts[FO] == 0


async def test_identity(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    ident = await asyncio.to_thread(plc._list_identity)          # ListIdentity
    assert ident["product_name"] == DEFAULT_PRODUCT_NAME == "1766-L32BXB B/14.00"
    assert ident["vendor"] == "Rockwell Automation/Allen-Bradley"
    assert ident["product_type"] == "Programmable Logic Controller"
    assert ident["revision"] == {"major": 14, "minor": 0}        # from "/14.00"
    module = await asyncio.to_thread(plc.get_module_info, 0)     # Get Attributes All, routed to slot 0
    assert module["product_name"] == DEFAULT_PRODUCT_NAME and module["serial"] == ident["serial"]


@pytest.mark.parametrize(("product_name", "expected"), [
    ("1766-L32BXB B/14.00", "1766-L32BXB"),
    ("1763-L16BWA B/12.00", "1763-L16BWA"),
    ("1747-L552/C C/10.00", "1747-L552/C"),
])
async def test_processor_type(start_sim, connect, product_name, expected):
    sim = await start_sim(product_name=product_name)
    plc = await connect(sim)
    assert await asyncio.to_thread(plc.get_processor_type) == expected
    assert sim.pccc_counts == {DIAG_STATUS: 1}


# --- reads -------------------------------------------------------------------------------------------

SINGLE = {
    "N7:0": -5, "N7:1": 32767, "N7:2": -32768, "N7:3": -1, "N7:0/15": True, "N7:4/0": False,
    "F8:0": f32(1.5), "F8:62": TABLES["F8"][62],
    "B3:1": -32767, "B3:1/15": True, "B3:1/0": True, "B3:2/1": True, "B3:3/15": False,
    "L10:0": -70000, "L10:1": 2**31 - 1, "L10:2": -2**31,
    "S:1": 0x40, "S:1/6": True, "S:3": 8,
    "ST9:0": "hello", "ST9:1": "", "ST9:2": "x" * 82,
    "T4:0.ACC": 42, "T4:0.PRE": 100, "T4:0.DN": True, "T4:0.EN": False, "T4:0.TT": False,
    "T4:1.EN": True, "T4:1.TT": True, "T4:1.DN": False,
    "C5:0.ACC": 3, "C5:0.PRE": 7, "C5:0.CU": True, "C5:0.CD": False, "C5:0.DN": True,
    "C5:1.OV": True, "C5:1.UN": True, "C5:1.UA": True, "C5:1.CU": False,
    "I:0": 0x00FF, "I:0.1": -2, "I:1.0": 0x1234, "I:0/3": True, "I:0/8": False,
    "O:0": 0x0F0F, "O:0.1": 3,
}


async def test_single_element_reads_of_every_file_type(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    tags = await read(plc, *SINGLE)
    for (address, expected), tag in zip(SINGLE.items(), tags, strict=True):
        assert tag.error is None, (address, tag.error)
        assert tag.value == expected == sim.value(address), address
        assert type(tag.value) is type(expected), address
    # one Execute PCCC (typed read, 3 address fields) per address, as pycomm3 sends them
    assert sim.pccc_counts == {TYPED_READ: len(SINGLE)}
    assert sim.service_counts[0x4B] == len(SINGLE)


@pytest.mark.parametrize(("address", "file", "start", "count"), [
    ("F8:0{10}", "F8", 0, 10), ("N7:0{20}", "N7", 0, 20), ("B3:0{4}", "B3", 0, 4),
    ("L10:0{3}", "L10", 0, 3), ("S:0{4}", "S", 0, 4), ("ST9:0{2}", "ST9", 0, 2),
    ("N7:5{3}", "N7", 5, 3), ("O:0.0{2}", "O", 0, 2),
])
async def test_block_reads(start_sim, connect, address, file, start, count):
    sim = await start_sim()
    plc = await connect(sim)
    tag = await read(plc, address)
    assert tag.error is None
    assert tag.value == sim.value(file)[start:start + count]
    assert sim.max_read_bytes == count * {"F": 4, "L": 4, "ST": 84}.get(re.match(r"[A-Z]+", file)[0], 2)


async def test_io_block_runs_on_into_the_next_slot(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    assert (await read(plc, "I:0.0{3}")).value == [0x00FF, -2, 0x1234]
    assert (await read(plc, "I:0.1{2}")).value == [-2, 0x1234]
    assert (await read(plc, "I:0.0{4}")).error == ERR_ADDRESS


async def test_largest_planned_blocks(start_sim, connect):
    """255 data bytes is the PCCC limit: 63 floats (252 B), 127 words (254 B)."""
    sim = await start_sim()
    plc = await connect(sim)
    floats, words = await read(plc, "F8:0{63}", "N11:0{127}")
    assert floats.error is None and floats.value == TABLES["F8"]
    assert all(isinstance(v, float) for v in floats.value)
    assert words.error is None and words.value == TABLES["N11"]
    assert sim.max_read_bytes == 254 and sim.pccc_counts == {TYPED_READ: 2}
    assert sim.max_reply_bytes < sim.connection_sizes[0]


async def test_words_are_signed_on_the_wire(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    sim.set("N7:4", 65535)
    sim.set("B3:0", 0x8000)
    sim.set("T4:0.ACC", 40000)
    assert sim.value("N7:4") == -1 and sim.value("B3:0") == -32768
    n, b, acc = await read(plc, "N7:4", "B3:0", "T4:0.ACC")
    assert (n.value, b.value, acc.value) == (-1, -32768, 40000 - 65536)


async def test_bit_number_form(start_sim, connect):
    """B3/17 is word 1 bit 1. pycomm3 itself decodes it as bit 0 of word 1 (D-008), which is
    why the driver reads whole words; the sim and `parse_pccc_address` agree on the real bit."""
    sim = await start_sim()
    plc = await connect(sim)
    sim.set("B3:1", 0)
    sim.set("B3/17", True)
    assert sim.value("B3:1") == 2 and sim.value("B3:1/1") is True and sim.value("B3/17") is True
    tag = await read(plc, "B3/17")
    assert tag.error is None and tag.value is False                       # pycomm3: bit 0
    assert (await read(plc, "B3:1")).value == 2                           # the driver's way


async def test_reads_see_fixture_changes(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    sim.set("F8:3", 12.3456)
    sim.set("ST9:1", "new text")
    sim.set("T4:0", (0, 1, 2))
    sim.set("T4:0.DN", True)
    f, s, acc, dn = await read(plc, "F8:3", "ST9:1", "T4:0.ACC", "T4:0.DN")
    assert f.value == f32(12.3456) and s.value == "new text" and acc.value == 2 and dn.value is True
    assert sim.value("T4:0") == (0x2000, 1, 2)


# --- errors ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("address", [
    "N99:0",          # file does not exist
    "F7:0",           # file 7 is an N file
    "N7:20",          # element past the end
    "N7:15{6}",       # block reaching past the end
    "T4:2.ACC",       # timer past the end
    "ST9:3",
    "I:2.0",          # no slot 2
    "I:1.1",          # slot 1 has one word
])
async def test_missing_file_or_element_gets_sts_0x10(start_sim, connect, address):
    sim = await start_sim()
    plc = await connect(sim)
    tag = await read(plc, address)
    assert tag.value is None
    assert tag.error == ERR_ADDRESS
    assert sim.pccc_counts == {TYPED_READ: 1}


async def test_injected_file_errors(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    sim.file_errors["F8"] = (0xF0, 0x06)
    sim.file_errors["S"] = (0x20, None)
    sim.file_errors["N99"] = (0xF0, 0x0A)             # applies to missing files too
    f, s, n, ok = await read(plc, "F8:0", "S:0", "N99:0", "N7:0")
    assert (f.error, s.error, n.error, ok.error) == (
        ERR_EXT_STS, "PLC Has a Problem and Will Not Communicate", ERR_EXT_STS, None)
    raw = await asyncio.to_thread(raw_pccc, plc, 0x0F, 0xA2, bytes([4, 8, 0x8A, 0, 0]))
    assert raw[57:59] == bytes([0x4F, 0xF0]) and raw[61] == 0x06      # CMD|0x40, STS, TNS, EXT STS
    del sim.file_errors["F8"]
    assert (await read(plc, "F8:0")).error is None


async def test_removed_file_and_replaced_file(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    sim.remove("N7")
    assert (await read(plc, "N7:0")).error == ERR_ADDRESS
    with pytest.raises(KeyError):
        sim.value("N7:0")
    sim.set("N7", [1, 2, 3])                          # a download with a shorter N7
    assert (await read(plc, "N7:0{3}")).value == [1, 2, 3]
    assert (await read(plc, "N7:3")).error == ERR_ADDRESS
    assert sim.files["N7"] == 3


# --- file directory ----------------------------------------------------------------------------------

@pytest.mark.parametrize("product_name", [
    "1766-L32BXB B/14.00",       # MicroLogix 1400
    "1763-L16BWA B/12.00",       # MicroLogix 1100
    "1747-L552/C C/10.00",       # SLC 5/05
    "1761-L32BWA C/4.00",        # MicroLogix 1000 (pycomm3: untested layout)
])
async def test_file_directory(start_sim, connect, product_name, capsys):
    tables = {**TABLES, "N200": [7] * 3, "F201": [1.0]}     # word offsets past 255 need 0xFF + UINT
    sim = await start_sim(tables, product_name=product_name)
    plc = await connect(sim)
    directory = await asyncio.to_thread(plc.get_file_directory)
    assert directory == sim.directory()
    assert directory["N7"] == {"elements": 20, "length": 40}
    assert directory["T4"] == {"elements": 2, "length": 12}
    assert directory["ST9"] == {"elements": 3, "length": 252}
    assert directory["I1"] == {"elements": 3, "length": 6} and directory["O0"]["elements"] == 2
    assert list(directory)[-2:] == ["N200", "F201"]
    lay = sim.layout
    size = lay.file_position + 202 * lay.row_size
    assert sim.pccc_counts == {DIAG_STATUS: 1, DIR_READ: 1 + math.ceil(size / 80)}
    assert "data files: 13" in capsys.readouterr().out         # pycomm3 prints this


async def test_file_directory_not_readable(start_sim, connect):
    sim = await start_sim(directory=False)
    plc = await connect(sim)
    with pytest.raises(ResponseError, match="Failed to read file directory size"):
        await asyncio.to_thread(plc.get_file_directory)
    assert (await read(plc, "N7:0")).value == -5


# --- connection faults -------------------------------------------------------------------------------

async def test_mute_times_out(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim, timeout=0.3)
    await read(plc, "N7:0")
    sim.mute = True
    t0 = time.monotonic()
    with pytest.raises(CommError):
        await read(plc, "N7:0")
    assert 0.25 <= time.monotonic() - t0 < 2.0
    sim.mute = False


async def test_stop_gives_connection_errors(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim, timeout=0.5)
    await read(plc, "N7:0")
    await sim.stop()
    with pytest.raises(CommError):
        await read(plc, "N7:0")
    with pytest.raises(CommError):
        await connect(sim, timeout=0.5)


async def test_restart_on_the_same_port(start_sim, connect):
    port = stable_port()
    sim = await start_sim(port=port)
    old = await connect(sim, timeout=0.5)
    sim.set("N7:0", 1234)
    await read(old, "N7:0")
    await sim.restart()
    assert sim.port == port
    with pytest.raises(CommError):
        await read(old, "N7:0")                      # the old socket was reset
    new = await connect(sim)
    assert (await read(new, "N7:0")).value == 1234   # data tables survive a power cycle


# --- read-only ---------------------------------------------------------------------------------------

async def test_pccc_writes_are_refused_and_counted(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    tag = await asyncio.to_thread(plc.write, ("N7:0", 99))        # CMD 0x0F FNC 0xAB masked write
    assert tag.error == ERR_PROTECTED
    assert sim.pccc_counts[MASKED_WRITE] == 1 and sim.write_attempts == 1
    data = bytes([2, 7, 0x89, 0, 0]) + struct.pack("<h", 99)
    for cmd, fnc, payload in [
        (0x0F, 0xAA, data),                                          # typed logical write, 3 fields
        (0x0F, 0x67, b"\x00\x00\x01\x00" + data),                    # typed write (PLC-5 style)
        (0x0F, 0x26, b"\x00\x00\xff\xff\x00\x00"),                   # read-modify-write
        (0x08, None, b"\x00\x00\x63\x00"),                           # unprotected write
    ]:
        raw = await asyncio.to_thread(raw_pccc, plc, cmd, fnc, payload)
        assert raw[57:59] == bytes([cmd | 0x40, 0x60]), (cmd, fnc)
    assert sim.write_attempts == 5
    assert sim.value("N7:0") == -5 and (await read(plc, "N7:0")).value == -5
    # CIP-level write service on the connection: 0x08, counted as well
    res = await asyncio.to_thread(plc.generic_message, service=0x4D, class_code=0x67, instance=1,
                                  request_data=b"\x00", connected=True)
    assert res.error and sim.write_attempts == 6


async def test_unsimulated_commands_and_echo(start_sim, connect):
    sim = await start_sim()
    plc = await connect(sim)
    await read(plc, "N7:0")
    raw = await asyncio.to_thread(raw_pccc, plc, 0x01, None, b"\x00\x00\x02")   # unprotected read
    assert raw[57:59] == bytes([0x41, 0x10])
    raw = await asyncio.to_thread(raw_pccc, plc, 0x06, 0x00, b"ping")            # diagnostic echo
    assert raw[57:59] == bytes([0x46, 0x00]) and raw[61:] == b"ping"
    assert sim.write_attempts == 0
    assert sim.pccc_counts[(0x01, None)] == 1 and sim.pccc_counts[(0x06, 0x00)] == 1


# --- fixture API ---------------------------------------------------------------------------------------

async def test_set_and_value_forms_and_validation(start_sim):
    sim = await start_sim()
    sim.set("C5:0.ACC", -3)
    sim.set("I:1/3", True)
    sim.set("O:0.1/15", True)
    sim.set("L10:0/31", False)
    sim.set("S:2/0", False)
    assert sim.value("C5:0.ACC") == -3 and sim.value("C5:0") == (-0x6000, 7, -3)
    assert sim.value("I:1.0") == 0x1234 | 8 and sim.value("O:0.1") == 3 - 32768
    assert sim.value("L10:0") == -70000 + 2**31 and sim.value("S:2") == 6
    assert sim.value("I") == [[0x00FF, -2], [0x1234 | 8]]
    assert sim.files["F8"] == 63 and sim.files["I1"] == 3
    for address, value in [("N7:0", 70000), ("N7:0", 1.5), ("N7:0", "1"), ("F8:0", 1e40),
                           ("ST9:0", "x" * 83), ("T4:0", (1, 2)), ("L10:0", 2**32)]:
        with pytest.raises(ValueError):
            sim.set(address, value)
    for address in ("N8:0", "N7:20", "F7:0", "T4:2.ACC", "I:2.0", "I:1.1", "ST9:3"):
        with pytest.raises(KeyError):
            sim.value(address)
    with pytest.raises(ValueError):
        sim.value("RTC:0.HR")                                        # parse_pccc_address rejects it


@pytest.mark.parametrize("tables", [
    {"N7": []}, {"X3": [1]}, {"I2": [1]}, {"S5": [1]}, {"N2": [1]}, {"N7": [1], "F7": [1.0]},
    {"N7": [0] * 257}, {"T4": [1, 2, 3]}, {"F8": "1.0"},
])
async def test_bad_tables_are_rejected(tables):
    with pytest.raises(ValueError):
        await PcccSim.start(tables)


# --- tables_for ----------------------------------------------------------------------------------------

def _tags(addresses: dict[str, str]) -> list[Tag]:
    return [Tag(name=f"T{i:03d}", address=a, dtype=d) for i, (a, d) in enumerate(addresses.items())]


PATTERN_TAGS = {
    "F34:3": "float32", "F34:0": "float32", "N54:17": "uint16", "N66:10": "float32",
    "B3:17/14": "bool", "B3/0": "bool", "B3/300": "bool", "L9:4": "int32", "L9:2/30": "bool",
    "T47:0.PRE": "uint16", "T47:2.ACC": "uint16", "T47:1.DN": "bool", "C5:3.CU": "bool",
    "ST56:10": "string", "I:0/0": "bool", "I:0/19": "bool", "I:2.1": "uint16", "O:0/5": "bool",
    "S:1/5": "bool", "S:4": "uint16", "RTC:0.HR": "uint16", "GSM_ONFB": "float32",
}


def test_tables_for_covers_every_address_tightly():
    tags = _tags(PATTERN_TAGS)
    tables = tables_for(tags, seed=3)
    assert list(tables) == ["S", "B3", "C5", "L9", "F34", "T47", "N54", "ST56", "N66", "I", "O"]
    sizes = {k: len(v) for k, v in tables.items()}
    assert sizes == {"S": 5, "B3": 19, "C5": 4, "L9": 5, "F34": 4, "T47": 3, "N54": 18, "ST56": 11,
                     "N66": 11, "I": 3, "O": 1}
    assert [len(s) for s in tables["I"]] == [2, 1, 2]                 # slot 1 unused: one word
    assert tables_for(tags, seed=3) == tables != tables_for(tags, seed=4)
    assert {k: len(v) for k, v in tables_for(tags, seed=3, pad=2).items()}["N54"] == 20
    assert all(isinstance(x, float) and x == f32(x) for x in tables["F34"])
    assert all(-32768 <= x <= 32767 for x in tables["N54"] + tables["B3"])


async def test_tables_for_file_number_with_two_types(start_sim, connect):
    """One file number, two types (real project: F54:17 among N54 tags): majority wins, the
    rest fails on the wire like on the PLC. Data files on numbers 0-2 cannot exist either."""
    tags = _tags({"N54:16": "uint16", "N54:17": "uint16", "F54:17": "float32", "N1:0": "uint16",
                  "I:0.0": "uint16"})
    tables = tables_for(tags)
    assert list(tables) == ["N54", "I"] and len(tables["N54"]) == 18
    sim = await start_sim(tables)
    plc = await connect(sim)
    n, f, n1 = await read(plc, "N54:17", "F54:17", "N1:0")
    assert n.error is None and f.error == ERR_ADDRESS and n1.error == ERR_ADDRESS


async def test_tables_for_serves_the_driver_plan(start_sim, connect):
    tags = _tags(PATTERN_TAGS)
    sim = await start_sim(tables_for(tags, seed=5))
    for t in tags:
        with contextlib.suppress(ValueError):
            sim.value(t.address)                                      # KeyError would fail here
    plc = await connect(sim)
    checked = await _read_planned_blocks(sim, plc, tags)
    assert checked == len(tags) - 2                                   # RTC and symbolic are bad_config


async def _read_planned_blocks(sim: PcccSim, plc: SLCDriver, tags: list[Tag]) -> int:
    """Read every block the ab_pccc planner makes through pycomm3; values must match the sim."""
    device = Device(id="SIM/PLC", channel="SIM", name="PLC", driver="ab_pccc", host=sim.host,
                    port=sim.port, tags=tags)
    by_name = {t.name: t for t in tags}
    checked = 0
    for group in device.rate_groups().values():
        plan = AbPcccDriver(device).plan(group)
        blocks = [b for b in plan.blocks if isinstance(b, PcccBlock)]
        replies = await read(plc, *(b.address for b in blocks))
        replies = replies if isinstance(replies, list) else [replies]
        for block, reply in zip(blocks, replies):
            assert reply.error is None, (block.address, reply.error)
            for name, got in decode_block(block, reply.value).items():
                tag = by_name[name]
                assert not isinstance(got, DecodeError), (tag.address, got)
                assert got == coerce_value(sim.value(tag.address), tag.dtype), (tag.address, block.address)
                checked += 1
    return checked


def _real_pccc_devices(kep2poller_output: dict) -> list[tuple[str, list[Tag], list[str]]]:
    """(site/device, tags, PCCC data-table addresses) of the 30 model-23 devices."""
    out = []
    for site in kep2poller_output["sites"]:
        for dev in site["devices"]:
            if dev["driver"] != "Allen-Bradley ControlLogix Ethernet" or dev["model"] != 23:
                continue
            tags = [Tag(name=t["name"], address=t["address"], dtype=t["type"], scan_ms=t["scan_ms"])
                    for t in dev["tags"]]
            addresses = []
            for t in tags:
                try:
                    parse_pccc_address(t.address)
                except ValueError as err:                             # bad_config at plan time
                    assert "symbolic" in str(err) or "RTC" in str(err), (t.address, err)
                    continue
                addresses.append(t.address)
            out.append((f"{site['site']}/{dev['device']}", tags, addresses))
    assert len(out) == 30
    return out


@pytest.mark.real_project
async def test_real_project_pccc_devices_read_through_slcdriver(kep2poller_output, start_sim, connect):
    for device, tags, addresses in _real_pccc_devices(kep2poller_output)[:3]:
        assert len(addresses) > 250, device
        sim = await start_sim(tables_for(tags, seed=11))
        plc = await connect(sim)
        replies = await read(plc, *addresses)
        for address, reply in zip(addresses, replies, strict=True):
            assert reply.error is None, (device, address, reply.error)
            if re.fullmatch(r"B\d+/\d+", address):                    # pycomm3 bug: bit 0 of the word
                a = parse_pccc_address(address)
                word = sim.value(f"B{a.file_number}:{a.element}")
                assert reply.value == bool(word & 1), address
            else:
                assert reply.value == sim.value(address), address
        assert await _read_planned_blocks(sim, plc, tags) == len(addresses)


# A file number has one type: an address that uses it with another type (the real project has
# one F54:17 among 36 N54 tags on a device) cannot read on the PLC. Computed from the data.


@pytest.mark.real_project
async def test_real_project_every_pccc_address_is_served(kep2poller_output):
    failed = set()
    expected_conflicts = set()
    total = 0
    for device, tags, addresses in _real_pccc_devices(kep2poller_output):
        expected_conflicts |= {(device, a) for a in file_type_conflicts(tags)}
        async with await PcccSim.start(tables_for(tags, seed=12)) as sim:
            plc = SLCDriver(sim.path)
            plc.socket_timeout = 2.0
            try:
                await asyncio.to_thread(plc.open)
                replies = await read(plc, *addresses)
            finally:
                await asyncio.to_thread(plc.close)
        for address, reply in zip(addresses, replies, strict=True):
            if reply.error is not None:
                assert reply.error == ERR_ADDRESS, (device, address, reply.error)
                failed.add((device, address))
        total += len(addresses)
    assert failed == expected_conflicts
    assert total > 10_000
