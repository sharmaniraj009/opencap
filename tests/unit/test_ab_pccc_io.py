"""ab_pccc driver I/O (docs/phase3_contracts.md §1).

Three layers:
1. pure helpers (path, reply status parsing, error mapping, read-only filter, file directory);
2. the pycomm3 `SLCDriver` subclass, driven through real pycomm3 1.2.16 code with crafted
   reply bytes on a fake socket: this pins the methods it overrides and the private pycomm3
   functions it calls;
3. `AbPcccDriver` with a fake pycomm3 (scripted PLC) on a real single-thread executor.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import structlog
from pycomm3 import SLCDriver
from pycomm3.cip_driver import parse_connection_path
from pycomm3.exceptions import CommError as PycommCommError
from pycomm3.exceptions import RequestError, ResponseError

from opencap.config.models import Device, Tag
from opencap.drivers import _cip_common, ab_pccc
from opencap.drivers.ab_pccc import (
    AbPcccDriver,
    DirectoryUnavailable,
    PcccBlock,
    PcccLinkError,
    PcccReply,
    PcccResult,
    check_read_only,
    classify_reply,
    connection_path,
    directory_problem,
    parse_file_check,
    parse_file_directory,
    parse_pccc_reply,
    slc_driver_class,
    verify_targets,
)
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost

EXEC_PREFIX = "ab-pccc-test-exec"


# --- helpers ------------------------------------------------------------------------------

def make_device(tags: list[Tag] | None = None, *, timeout_ms: int = 1000, host: str = "192.0.2.12",
                port: int = 44818, **params: Any) -> Device:
    return Device(id="SITE/SLC", channel="SITE", name="SLC", driver="ab_pccc", host=host, port=port,
                  params=params, tags=tags or [], timeout_ms=timeout_ms)


def T(address: str, dtype: str = "uint16", name: str | None = None) -> Tag:
    return Tag(name=name or f"t_{address}", address=address, dtype=dtype)


def comm_error(cause: BaseException) -> PycommCommError:
    """A pycomm3 CommError the way its socket layer raises it (`raise ... from err`)."""
    exc = PycommCommError("socket connection broken")
    exc.__cause__ = cause
    return exc


def blocks(drv: AbPcccDriver) -> dict[str, PcccBlock]:
    plan = drv.plan(drv.device.tags)
    assert plan.invalid == {}
    return {b.address: b for b in plan.blocks if isinstance(b, PcccBlock)}


GOOD = PcccReply(0, 0, None, 0, None, 7, b"")


def sts_reply(sts: int, ext: int | None = None) -> PcccReply:
    return PcccReply(0, 0, None, sts, ext, 7, b"" if ext is None else bytes([ext]))


# --- 1. pure helpers --------------------------------------------------------------------------

@pytest.mark.parametrize(("port", "params", "path"), [
    (44818, {}, "192.0.2.12:44818"),                                  # KEPServer id string `<ip>`
    (2222, {"path": None}, "192.0.2.12:2222"),
    (44818, {"path": "1,0"}, "192.0.2.12:44818/1/0"),
    (44818, {"path": "3"}, "192.0.2.12:44818/3"),                     # pycomm3 auto-slot: backplane 3
    (44818, {"path": "1,0,2,192.0.2.13"}, "192.0.2.12:44818/1/0/2/192.0.2.13"),
    (44818, {"path": " "}, "192.0.2.12:44818"),
])
def test_connection_path(port: int, params: dict, path: str) -> None:
    assert connection_path(make_device(port=port, **params)) == path


def test_connection_path_parses_with_pycomm3() -> None:
    from pycomm3.cip import PortSegment
    ip, port, route = parse_connection_path(connection_path(make_device()), auto_slot=True)
    assert (ip, port, route) == ("192.0.2.12", 44818, [PortSegment("bp", 0)])   # SLCDriver's own default
    ip, port, route = parse_connection_path(connection_path(make_device(path="1,0,2,192.0.2.13")), True)
    assert route == [PortSegment(1, "0"), PortSegment(2, "192.0.2.13")]


@pytest.mark.parametrize("params", [{"path": "1,0,2"}, {"file_check": "maybe"}])
def test_bad_params_raise(params: dict) -> None:
    with pytest.raises(ValueError):
        AbPcccDriver(make_device(**params))
    with pytest.raises(ValueError, match="empty host"):
        AbPcccDriver(make_device(host=" "))


@pytest.mark.parametrize(("value", "on"), [(None, True), (True, True), ("off", False), (False, False), ("1", True)])
def test_parse_file_check(value: Any, on: bool) -> None:
    assert parse_file_check(value) is on


def _cip_reply(service: int, status: int, data: bytes = b"", ext: tuple[int, ...] = (),
               *, command_status: int = 0) -> bytes:
    """A SendUnitData reply exactly as a controller frames it."""
    cip = bytes([service | 0x80, 0, status, len(ext)]) + b"".join(struct.pack("<H", e) for e in ext) + data
    payload = struct.pack("<H", 1) + cip                                 # connected sequence count
    cpf = (struct.pack("<IHH", 0, 0, 2) + struct.pack("<HH", 0xA1, 4) + b"\x01\x02\x03\x04"
           + struct.pack("<HH", 0xB1, len(payload)) + payload)
    header = struct.pack("<HHII", 0x70, len(cpf), 1, command_status) + b"_pycomm_" + struct.pack("<I", 0)
    return header + cpf


def _rr_reply(service: int, status: int, data: bytes = b"") -> bytes:
    """A SendRRData (unconnected) reply."""
    cip = bytes([service | 0x80, 0, status, 0]) + data
    cpf = struct.pack("<IHH", 0, 0, 2) + struct.pack("<HH", 0, 0) + struct.pack("<HH", 0xB2, len(cip)) + cip
    return struct.pack("<HHII", 0x6F, len(cpf), 1, 0) + b"_pycomm_" + struct.pack("<I", 0) + cpf


def _pccc(sts: int = 0, data: bytes = b"", *, ext: int | None = None, cmd: int = 0x4F, id_len: int = 7) -> bytes:
    """Execute PCCC reply data: requestor ID, CMD|0x40, STS, TNS, [EXT STS], data."""
    rid = bytes([id_len]) + b"\x09\x10" + b"\x00" * (id_len - 3)
    return rid + bytes([cmd, sts]) + b"\x01\x00" + (bytes([ext]) if ext is not None else b"") + data


def pccc_reply(sts: int = 0, data: bytes = b"", **kw: Any) -> bytes:
    return _cip_reply(0x4B, 0, _pccc(sts, data, **kw))


@pytest.mark.parametrize(("raw", "kind", "text"), [
    (pccc_reply(0, b"\x01\x00"), None, None),
    (pccc_reply(0x10), "config", "STS 0x10 (illegal command or format"),
    (pccc_reply(0x50), "config", "addressing problem"),
    (pccc_reply(0xF0, ext=0x06), "config", "EXT STS 0x06 (address does not point to something usable)"),
    (pccc_reply(0xF0, ext=0x07), "config", "file is wrong size"),
    (pccc_reply(0xF0, ext=0x0A), "config", "transaction size plus word address"),
    (pccc_reply(0xF0, ext=0x11), "config", "illegal data type"),
    (pccc_reply(0xF0, ext=0x0B), "comm", "access denied"),
    (pccc_reply(0xF0, ext=0x1F), "comm", "temporary internal problem"),
    (pccc_reply(0xF0), "comm", "EXT STS missing"),
    (pccc_reply(0x60), "comm", "command protection"),
    (pccc_reply(0x70), "comm", "Program mode"),
    (pccc_reply(0x30), "comm", "remote node host is missing"),
    (pccc_reply(0x05), "comm", "local error"),
    (pccc_reply(0, b"\x01\x00", id_len=5), "comm", "requestor ID length 5"),
    (_cip_reply(0x4B, 0x08), "comm", "CIP status 0x08 (Service not supported)"),
    (_cip_reply(0x4B, 0x05, ext=(0x0000,)), "comm", "CIP status 0x05"),
    (_cip_reply(0x4B, 0x01, ext=(0x0107,)), "lost", "extended 0x0107"),
    (_cip_reply(0x4B, 0)[:50], "comm", "truncated"),
    (pccc_reply(0)[:57], "comm", "truncated PCCC reply"),
    (_cip_reply(0x4B, 0, command_status=0x64), "lost", "encapsulation status 0x0064"),
])
def test_reply_status_and_error_mapping(raw: bytes, kind: str | None, text: str | None) -> None:
    problem = classify_reply(parse_pccc_reply(raw))
    if kind is None:
        assert problem is None
    else:
        assert problem is not None and problem[0] == kind and text in problem[1], problem


def test_reply_status_offsets_match_pycomm3() -> None:
    """pycomm3 reads STS at raw byte 58 and data from byte 61 (7-byte requestor ID)."""
    from pycomm3.const import SLC_REPLY_START
    from pycomm3.slc_driver import request_status
    raw = pccc_reply(0xF0, b"\xAA", ext=0x06)
    reply = parse_pccc_reply(raw)
    assert reply is not None and raw[58] == reply.sts == 0xF0 and raw[SLC_REPLY_START] == reply.ext_sts
    assert request_status(raw) == "Error code in EXT STS Byte"
    assert reply.data == raw[SLC_REPLY_START:]
    assert classify_reply(None) == ("comm", "no reply status captured")


def _pccc_msg(cmd: int, fnc: int | None, rest: bytes = b"") -> bytes:
    """An Execute PCCC CIP request as SLCDriver builds it (`_msg_start()` + PCCC command)."""
    head = b"\x4b\x02\x20\x67\x24\x01\x07\x09\x10\x01\x02\x03\x04"
    return head + bytes([cmd, 0]) + b"\x05\x00" + (bytes([fnc]) if fnc is not None else b"") + rest


@pytest.mark.parametrize(("message", "refused"), [
    (_pccc_msg(0x0F, 0xA2, b"\x0c\x08\x8a\x00\x00"), None),           # typed read, 3 address fields
    (_pccc_msg(0x0F, 0xA1, b"\x50\x00\x03\x00"), None),               # file directory read
    (_pccc_msg(0x06, 0x03), None),                                     # diagnostic status
    (_pccc_msg(0x0F, 0xAB, b"\x02\x07\x89\x00\x00\xff\xff\x01\x00"), "PCCC command 0x0f function 0xab"),
    (_pccc_msg(0x0F, 0xAA, b"\x02\x07\x89\x00\x00\x01\x00"), "function 0xaa"),
    (_pccc_msg(0x0F, 0xA9, b"\x02\x07\x89\x00\x01\x00"), "function 0xa9"),
    (_pccc_msg(0x0F, 0x80, b"\x01"), "function 0x80"),                 # change mode
    (_pccc_msg(0x08, None, b"\x00\x00\x01\x00"), "PCCC command 0x08"),  # unprotected write
    (_pccc_msg(0x0F, 0xA2, b"\x6d\x00\xa5\x00\x00"), "data-log queue"),
    (b"\x4b\x02\x20\x67\x24\x01\x07\x09", "unparseable"),
    (b"\x4d\x02\x20\x6b\x24\x01", "CIP service 0x4d refused"),
    (b"\x10\x03\x20\x01\x24\x01\x30\x01", "CIP service 0x10 refused"),
    (b"\x4e\x02\x20\x6b\x24\x01", "CIP service 0x4e refused"),             # Read-Modify-Write
    (b"\x0e\x03\x20\x01\x24\x01\x30\x01", None),                       # Get Attribute Single
])
def test_read_only_filter(message: bytes, refused: str | None) -> None:
    reason = check_read_only(message)
    if refused is None:
        assert reason is None
    else:
        assert reason is not None and refused in reason, reason


# --- file directory (pure) ---

ML1400 = {"file_position": 233, "row_size": 10, "file_type": b"\x03", "size_element": b"\x2b",
          "size_len": b"\x08", "size_const": 19968}
SLC505 = {"file_position": 79, "row_size": 10, "file_type": b"\x01", "size_element": b"\x23", "size_len": b"\x04"}

# number -> (type code, length in bytes); 5 and 6 unused (0x81)
DIR_FILES = {0: (0x82, 2), 1: (0x83, 2), 2: (0x84, 80), 3: (0x85, 20), 4: (0x86, 30),
             5: (0x81, 0), 6: (0x81, 0), 7: (0x89, 20), 8: (0x8A, 12), 9: (0x91, 8), 10: (0x8D, 168)}


def directory_image(sys0: dict, files: dict[int, tuple[int, int]] = DIR_FILES) -> bytes:
    rows = max(files) + 1
    buf = bytearray(sys0["file_position"] + rows * sys0["row_size"])
    buf[52] = sum(1 for c, _ in files.values() if c != 0x81)
    for n in range(rows):
        code, length = files.get(n, (0x81, 0))
        at = sys0["file_position"] + n * sys0["row_size"]
        buf[at] = code
        struct.pack_into("<H", buf, at + 1, length)
    return bytes(buf)


EXPECTED_FILES = {0: ("O", 1), 1: ("I", 1), 2: ("S", 40), 3: ("B", 10), 4: ("T", 5), 7: ("N", 10),
                  8: ("F", 3), 9: ("L", 2), 10: ("ST", 2)}


@pytest.mark.parametrize("sys0", [ML1400, SLC505])
def test_parse_file_directory(sys0: dict) -> None:
    assert parse_file_directory(sys0, directory_image(sys0)) == EXPECTED_FILES


def test_parse_file_directory_agrees_with_pycomm3(capsys: pytest.CaptureFixture[str]) -> None:
    from pycomm3.slc_driver import _parse_file0
    data = directory_image(ML1400)
    theirs = _parse_file0(ML1400, data)
    assert "data files" in capsys.readouterr().out                 # pycomm3 prints; we do not
    ours = parse_file_directory(ML1400, data)
    assert {f"{ft}{n}": el for n, (ft, el) in ours.items()} == {k: v["elements"] for k, v in theirs.items()}


def test_directory_tables_match_pycomm3() -> None:
    from pycomm3.cip import PCCC_DATA_SIZE, PCCC_DATA_TYPE
    theirs = {code[0]: name for code, name in PCCC_DATA_TYPE.items() if isinstance(code, bytes)}
    assert ab_pccc.DIRECTORY_TYPES == theirs
    assert {k: ab_pccc.DIRECTORY_ELEM_SIZE[k] for k in PCCC_DATA_SIZE} == PCCC_DATA_SIZE
    assert {k: PCCC_DATA_SIZE[k] for k in ab_pccc.ELEM_SIZE} == ab_pccc.ELEM_SIZE


@pytest.mark.parametrize(("data", "match"), [
    (directory_image(ML1400, {0: (0x81, 0), 1: (0x83, 2), 2: (0x84, 80), 7: (0x89, 20)}), "O0, I1, S2"),
    (directory_image(ML1400)[:233 + 10 * 7 + 2], "truncated"),
    (b"", "O0, I1, S2"),
])
def test_parse_file_directory_rejects_implausible(data: bytes, match: str) -> None:
    with pytest.raises(DirectoryUnavailable, match=match):
        parse_file_directory(ML1400, data)


def _block(address: str) -> PcccBlock:
    dtype = {"F": "float32", "ST": "string"}
    tags = [T(a.strip(), dtype.get(re.match(r"ST|[A-Z]", a.strip())[0], "uint16")) for a in address.split("+")]
    drv = AbPcccDriver(make_device(tags, max_gap=50))
    (block,) = drv.plan(tags).blocks
    assert isinstance(block, PcccBlock)
    return block


@pytest.mark.parametrize(("addresses", "problem"), [
    ("N7:0 + N7:9", None),
    ("N7:0 + N7:10", "N7 has 10 elements, the block needs 0..10"),
    ("N7:10", "N7 has 10 elements, the block needs 10..10"),
    ("N12:0", "file N12 does not exist"),
    ("F7:0", "file 7 is a N file, not F"),
    ("B3:9/15", None),
    ("B3/160", "B3 has 10 elements"),
    ("T4:4.ACC", None),
    ("T4:5.DN", "T4 has 5 elements"),
    ("ST10:1", None),
    ("I:3.0", None),                                 # I/O and S files are not checked
    ("S:200", None),
])
def test_directory_problem(addresses: str, problem: str | None) -> None:
    got = directory_problem(EXPECTED_FILES, _block(addresses))
    if problem is None:
        assert got is None
    else:
        assert got is not None and problem in got and "processor file directory" in got


def test_verify_targets() -> None:
    assert verify_targets(EXPECTED_FILES) == ("L9:1", "L9:2")         # highest N/F/L/B file
    assert verify_targets({0: ("O", 1), 1: ("I", 1), 2: ("S", 40), 200: ("N", 256)}) == ("N200:255", None)
    assert verify_targets({0: ("O", 1), 1: ("I", 1), 2: ("S", 40), 4: ("T", 3)}) is None


# --- 2. the pycomm3 subclass, on real pycomm3 code with crafted replies -------------------------

class FakeSock:
    def __init__(self, replies: list[bytes | BaseException]) -> None:
        self.replies = list(replies)
        self.sent: list[bytes] = []

    def send(self, msg: bytes, timeout: float = 0) -> int:
        self.sent.append(msg)
        return len(msg)

    def receive(self, timeout: float = 0) -> bytes:
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        pass


def cip_message(sent: bytes) -> bytes:
    """The CIP request inside a SendUnitData message (after the 2-byte sequence count)."""
    return sent[46:]


@pytest.fixture
def plc_factory():
    cls = slc_driver_class()

    def make(replies: list[bytes | BaseException], *, connected: bool = True):
        plc = cls("192.0.2.12:44818")
        plc._session = 1
        plc._target_is_connected = connected
        plc._target_cid = b"\x01\x02\x03\x04" if connected else None    # O->T connection id after Forward Open
        plc._connection_opened = True
        plc._sock = FakeSock(replies)
        return plc
    return make


def test_read_goes_out_as_one_typed_read_of_the_block_address(plc_factory) -> None:
    plc = plc_factory([pccc_reply(0, struct.pack("<22f", *[float(i) for i in range(22)]))])
    res = plc.oo_read("F34:0{22}")
    assert res == PcccResult([float(i) for i in range(22)], None, True, res.reply)
    assert classify_reply(res.reply) is None and plc.oo_sent == 1
    (sent,) = plc._sock.sent
    msg = cip_message(sent)
    assert msg[:6] == b"\x4b\x02\x20\x67\x24\x01" and msg[6] == 7          # Execute PCCC, 7-byte requestor ID
    cmd, sts, _tns, fnc, size, fn, ftype, el, sub = struct.unpack_from("<BBHBBBBBB", msg, 13)
    assert (cmd, sts, fnc, size, fn, ftype, el, sub) == (0x0F, 0, 0xA2, 88, 34, 0x8A, 0, 0)


def test_read_value_shapes(plc_factory) -> None:
    """One element -> scalar, {n} -> list; N/B words come back signed (decode_block masks)."""
    plc = plc_factory([pccc_reply(0, struct.pack("<h", -2)), pccc_reply(0, struct.pack("<2h", -1, 5)),
                       pccc_reply(0, struct.pack("<hhh", 0x2000 - 0x10000 * 0, 100, 42))])
    assert plc.oo_read("N7:1").value == -2
    assert plc.oo_read("B3:0{2}").value == [-1, 5]
    assert plc.oo_read("T4:0.ACC").value == 42                       # pycomm3 picks word 2 of the element
    msg = cip_message(plc._sock.sent[-1])
    assert msg[17:23] == bytes([0xA2, 6, 4, 0x86, 0, 0])                # whole 6-byte element, sub-element 0


def test_read_keeps_status_pycomm3_drops(plc_factory) -> None:
    plc = plc_factory([pccc_reply(0xF0, ext=0x06), _cip_reply(0x4B, 0x08), pccc_reply(0x10)])
    r = plc.oo_read("N7:200")
    assert (r.value, r.error, r.reply.sts, r.reply.ext_sts) == (None, "Error code in EXT STS Byte", 0xF0, 0x06)
    assert classify_reply(r.reply)[0] == "config"
    r = plc.oo_read("N7:0")
    assert r.error == "Unknown Status" and r.reply.cip_status == 0x08     # pycomm3 has no CIP status text here
    assert classify_reply(r.reply)[0] == "comm"
    r = plc.oo_read("N99:0")
    assert r.error.startswith("Illegal Command or Format") and classify_reply(r.reply)[0] == "config"


def test_unparseable_value_is_tag_error_with_good_status(plc_factory) -> None:
    plc = plc_factory([pccc_reply(0, b"")])
    r = plc.oo_read("F8:0")
    assert r.value is None and r.error and classify_reply(r.reply) is None


def test_request_error_before_sending(plc_factory) -> None:
    plc = plc_factory([])
    with pytest.raises(RequestError):
        plc.oo_read("X9:0")
    assert plc.oo_sent == 0 and plc._sock.sent == []


def test_socket_error_propagates_as_pycomm3_comm_error(plc_factory) -> None:
    plc = plc_factory([comm_error(TimeoutError("timed out"))])
    with pytest.raises(PycommCommError) as info:
        plc.oo_read("N7:0")
    assert _cip_common.is_timeout(info.value) and plc.oo_sent == 1


def test_stock_slcdriver_tries_large_forward_open_first() -> None:
    """Pins the pycomm3 1.2.16 behaviour the subclass changes."""
    plc = SLCDriver("192.0.2.12")
    assert plc._cfg["extended forward open"] is True and plc._cfg["connection_size"] == 4000


def test_subclass_uses_standard_forward_open_500(plc_factory) -> None:
    fo_data = struct.pack("<IIHHIIIBB", 0x11223344, 0x27041971, 0x0427, 0x1009, 0x71191009, 0, 0, 0, 0)
    plc = plc_factory([_rr_reply(0x54, 0, fo_data)], connected=False)
    plc.oo_forward_open()
    (sent,) = plc._sock.sent
    assert b"\x54\x02\x20\x06\x24\x01" in sent                         # Forward Open (not 0x5B) to the CM
    assert struct.pack("<H", 500 | 0x4200) in sent                      # 16-bit network parameters, size 500
    assert b"\x01\x00\x20\x02\x24\x01" in sent                          # route: backplane slot 0 + Message Router
    assert plc._target_is_connected and plc.connection_size == 500


def test_forward_open_refused_raises(plc_factory) -> None:
    plc = plc_factory([_rr_reply(0x54, 0x01, b"\x00" * 10)], connected=False)
    with pytest.raises(ResponseError):
        plc.oo_forward_open()
    assert len(plc._sock.sent) == 1                                     # no second attempt


def diag_reply(catalog: bytes = b"1766-L32BXB", sts: int = 0) -> bytes:
    return pccc_reply(sts, bytes([0x06, 0xEE, 0x36, 0x9C, 0x1E]) + catalog.ljust(11) + bytes(8) if sts == 0 else b"")


@pytest.mark.parametrize(("reply", "expected"), [
    (diag_reply(), "1766-L32BXB"),
    (diag_reply(b"1747-L551"), "1747-L551"),
    (diag_reply(sts=0x10), None),
    (_cip_reply(0x4B, 0x08), None),
    (diag_reply(b"\x01\x02\x03"), None),
])
def test_processor_type(plc_factory, reply: bytes, expected: str | None) -> None:
    plc = plc_factory([reply])
    assert plc.oo_processor_type() == expected
    msg = cip_message(plc._sock.sent[0])
    assert (msg[13], msg[17]) == (0x06, 0x03)                            # diagnostic status


def test_processor_type_link_error(plc_factory) -> None:
    plc = plc_factory([_cip_reply(0x4B, 0, command_status=0x64)])
    with pytest.raises(PcccLinkError):
        plc.oo_processor_type()


def directory_replies(sys0: dict, image: bytes, *, chunk_override: dict[int, bytes] | None = None) -> list[bytes]:
    """Size reply + the chunk replies pycomm3's reader will ask for (80 bytes each)."""
    size_word = struct.pack("<H", len(image) + sys0.get("size_const", 0))
    out = [pccc_reply(0, size_word + bytes(int(sys0["size_len"][0]) - 2))]
    pos, i = 0, 0
    while pos < len(image):
        chunk = image[pos:pos + 0x50]
        out.append(pccc_reply(0, (chunk_override or {}).get(i, chunk)))
        pos += len(chunk)
        i += 1
    return out


def test_file_directory_reads(plc_factory) -> None:
    image = directory_image(ML1400)
    replies = directory_replies(ML1400, image)
    plc = plc_factory(replies)
    assert plc.oo_file_directory("1766-L32BXB") == EXPECTED_FILES
    sent = [cip_message(m) for m in plc._sock.sent]
    assert len(sent) == len(replies) == 1 + -(-len(image) // 0x50)
    assert sent[0][13] == 0x0F and sent[0][17:22] == b"\xa1\x08\x00\x03\x2b"     # size: file 0, word 0x2B
    offsets = [m[21] for m in sent[1:]]
    assert offsets == [0, 40, 80, 120, 160][:len(offsets)]                # word offsets of 80-byte chunks
    assert all(m[17] == 0xA1 and m[19:21] == b"\x00\x03" for m in sent[1:])


def test_file_directory_empty_chunk_does_not_loop(plc_factory) -> None:
    """pycomm3's own reader loops forever on an empty chunk with a good status."""
    image = directory_image(ML1400)
    replies = directory_replies(ML1400, image, chunk_override={1: b""}) + [pccc_reply(0, b"")] * 20
    plc = plc_factory(replies)
    with pytest.raises(DirectoryUnavailable, match="no progress"):
        plc.oo_file_directory("1766-L32BXB")
    assert len(plc._sock.sent) <= 2 + -(-len(image) // 0x50)
    assert plc._oo_budget is None


@pytest.mark.parametrize(("replies", "match"), [
    ([pccc_reply(0x10)], "size not readable: PCCC STS 0x10"),
    ([pccc_reply(0, struct.pack("<H", 19968 + 20000) + bytes(6))], "implausible directory size 20000"),
    ([pccc_reply(0, struct.pack("<H", 100) + bytes(6))], "implausible"),       # below the constant
    ([pccc_reply(0, struct.pack("<H", 19968 + 400) + bytes(6)), pccc_reply(0x10)], "contents not readable"),
])
def test_file_directory_unavailable(plc_factory, replies: list[bytes], match: str) -> None:
    plc = plc_factory(replies)
    with pytest.raises(DirectoryUnavailable, match=match):
        plc.oo_file_directory("1766-L32BXB")


def test_file_directory_link_error(plc_factory) -> None:
    plc = plc_factory([_cip_reply(0x4B, 0, command_status=0x64)])
    with pytest.raises(PcccLinkError):
        plc.oo_file_directory("1766-L32BXB")


def test_subclass_refuses_writes(plc_factory) -> None:
    from pycomm3.packets import SendUnitDataRequestPacket
    plc = plc_factory([])
    with pytest.raises(NotImplementedError):
        plc.write(("N7:0", 1))
    with pytest.raises(NotImplementedError):
        plc._write_tag("N7:0", 1)
    with pytest.raises(NotImplementedError):
        plc.get_datalog_queue(1, 0)
    with pytest.raises(NotImplementedError):
        plc._get_datalog(0)
    for service in (b"\x10", b"\x04", 0x02, b"\x4D", b"\x53"):
        with pytest.raises(NotImplementedError):
            plc.generic_message(service=service, class_code=b"\x67", instance=1)
    # a hand-built PCCC protected typed logical write (what SLCDriver.write sends)
    request = SendUnitDataRequestPacket(plc._sequence)
    request.add(plc._msg_start() + b"\x0f\x00\x05\x00\xaa\x02\x07\x89\x00\x00\x01\x00")
    with pytest.raises(RequestError, match="read-only"):
        plc.send(request)
    assert plc._sock.sent == [] and plc.oo_sent == 0


def test_open_uses_socket_timeout_and_own_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = slc_driver_class()
    assert cls.socket_class is _cip_common.oo_socket_class()            # shared with ab_logix
    plc = cls("127.0.0.1:1")
    plc._cfg["socket_timeout"] = 0.25

    def refuse(self, host, port):
        raise PycommCommError(f"Failed to open socket to {host}:{port}")
    monkeypatch.setattr(cls.socket_class, "connect", refuse)
    with pytest.raises(PycommCommError):
        plc.open()
    assert isinstance(plc._sock, cls.socket_class)
    assert plc._sock.sock.gettimeout() == 0.25
    plc._sock.close()


# --- 3. AbPcccDriver with a fake pycomm3 --------------------------------------------------------

IDENTITY = {"vendor": "Rockwell Automation/Allen-Bradley", "product_type": "Programmable Logic Controller",
            "product_code": 90, "revision": {"major": 14, "minor": 0}, "serial": "40c0ffee",
            "product_name": "1766-L32BXB B/14.00", "state": 3}
_ADDR = re.compile(r"(?P<ft>[NFLBT])(?P<fn>\d+):(?P<el>\d+)(?:\.(?P<sub>ACC|PRE|DN))?(?:\{(?P<n>\d+)\})?")


class FakeController:
    """Scripted SLC shared by every FakePlc one driver creates: data files by number."""

    def __init__(self, files: dict[int, tuple[str, list]] | None = None) -> None:
        self.files = files if files is not None else {
            3: ("B", [0, 0x8001 - 0x10000, 5]), 7: ("N", [1, -2, 3, 4]), 8: ("F", [1.5, 2.5]),
            54: ("N", [10 * i for i in range(18)]), 66: ("N", [7] * 12), 4: ("T", [(0x2000, 100, 42)]),
        }
        self.identity = dict(IDENTITY)
        self.processor_type: str | None = "1766-L32BXB"
        self.directory: dict | BaseException | None = None      # None: built from `files`
        self.replies: dict[str, PcccResult] = {}                  # address -> scripted result
        self.read_raises: list[tuple[BaseException, bool]] = []   # (exception, request sent)
        self.open_result: bool | BaseException = True
        self.fo_exc: BaseException | None = None
        self.identity_exc: BaseException | None = None
        self.close_raises: BaseException | None = None
        self.directory_gate: threading.Event | None = None
        self.plcs: list[FakePlc] = []
        self.threads: list[tuple[str, str]] = []
        self.reads: list[str] = []
        self.directory_reads = 0

    def record(self, what: str) -> None:
        self.threads.append((what, threading.current_thread().name))

    def directory_files(self) -> dict[int, tuple[str, int]]:
        out = {0: ("O", 1), 1: ("I", 1), 2: ("S", 40)}
        out.update({n: (ft, len(v)) for n, (ft, v) in self.files.items()})
        return out

    def answer(self, address: str) -> PcccResult:
        illegal = PcccResult(None, "Illegal Command or Format, Address may not exist or not enough elements "
                                   "in data file", True, sts_reply(0x10))
        m = _ADDR.fullmatch(address)
        assert m, address
        f = self.files.get(int(m["fn"]))
        el, n = int(m["el"]), int(m["n"] or 1)
        if f is None or f[0] != m["ft"] or el + n > len(f[1]):
            return illegal
        values = f[1][el:el + n]
        if m["sub"]:
            ctl, pre, acc = values[0]
            values = [{"ACC": acc, "PRE": pre, "DN": bool(ctl & 0x2000)}[m["sub"]]]
        return PcccResult(values if n > 1 else values[0], None, True, GOOD)


class FakePlc:
    """The `OoSLCDriver` surface AbPcccDriver uses, scripted by a FakeController."""

    def __init__(self, path: str, *, ctl: FakeController) -> None:
        ctl.record("__init__")
        self.ctl = ctl
        self.path = path
        self._cfg = {"socket_timeout": 5.0, "extended forward open": False, "connection_size": 500}
        self.opened_with: dict | None = None
        self.forward_open = False
        self.closed = 0
        self.aborted = 0
        self.oo_sent = 0
        self._sock = None
        ctl.plcs.append(self)

    @property
    def connection_size(self) -> int:
        return self._cfg["connection_size"]

    def open(self) -> bool:
        self.ctl.record("open")
        self.opened_with = dict(self._cfg)
        if isinstance(self.ctl.open_result, BaseException):
            raise self.ctl.open_result
        return self.ctl.open_result

    def oo_list_identity(self) -> dict:
        self.ctl.record("oo_list_identity")
        if self.ctl.identity_exc is not None:
            raise self.ctl.identity_exc
        return dict(self.ctl.identity)

    def oo_forward_open(self) -> None:
        self.ctl.record("oo_forward_open")
        if self.ctl.fo_exc is not None:
            raise self.ctl.fo_exc
        self.forward_open = True

    def oo_processor_type(self) -> str | None:
        self.ctl.record("oo_processor_type")
        return self.ctl.processor_type

    def oo_file_directory(self, plc_type: str) -> dict:
        ctl = self.ctl
        ctl.record("oo_file_directory")
        ctl.directory_reads += 1
        assert plc_type == ctl.processor_type
        if ctl.directory_gate is not None:
            ctl.directory_gate.wait(5)
        if isinstance(ctl.directory, BaseException):
            raise ctl.directory
        return dict(ctl.directory) if ctl.directory is not None else ctl.directory_files()

    def oo_read(self, address: str) -> PcccResult:
        ctl = self.ctl
        ctl.record("oo_read")
        ctl.reads.append(address)
        self.oo_sent = 0
        if ctl.read_raises:
            exc, sent = ctl.read_raises.pop(0)
            self.oo_sent = int(sent)
            raise exc
        self.oo_sent = 1
        if address in ctl.replies:
            return ctl.replies[address]
        return ctl.answer(address)

    def close(self) -> None:
        self.ctl.record("close")
        self.closed += 1
        if self.ctl.close_raises is not None:
            raise self.ctl.close_raises

    def oo_abort(self) -> None:
        self.ctl.record("oo_abort")
        self.aborted += 1


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=EXEC_PREFIX)
    yield ex
    ex.shutdown(wait=True)


@pytest.fixture
def make_driver(monkeypatch: pytest.MonkeyPatch, executor):
    def make(ctl: FakeController, tags: list[Tag], **device_kw: Any) -> AbPcccDriver:
        monkeypatch.setattr(ab_pccc, "slc_driver_class", lambda: functools.partial(FakePlc, ctl=ctl))
        return AbPcccDriver(make_device(tags, **device_kw), executor)
    return make


STD_TAGS = [T("F8:0", "float32"), T("F8:1", "float32"), T("N7:1", "uint16"), T("N7:2", "int16"),
            T("B3:1/15", "bool"), T("B3/16", "bool", name="b3_bitnum_16"), T("B3/17", "bool", name="b3_bitnum_17"),
            T("N66:10", "float32"), T("T4:0.ACC", "uint16")]
STD_VALUES = {"t_F8:0": 1.5, "t_F8:1": 2.5, "t_N7:1": 65534, "t_N7:2": 3, "t_B3:1/15": True,
              "b3_bitnum_16": True, "b3_bitnum_17": False, "t_N66:10": 7.0, "t_T4:0.ACC": 42}


async def read_all(drv: AbPcccDriver) -> dict:
    out: dict = {}
    for block in blocks(drv).values():
        out.update(await drv.read(block))
    return out


async def test_connect_read_good_values(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, STD_TAGS)
    assert not drv.connected
    await drv.connect()
    assert drv.connected
    plc = ctl.plcs[-1]
    assert plc.path == "192.0.2.12:44818" and plc.forward_open
    verify_reads = list(ctl.reads)
    assert verify_reads == ["N66:11", "N66:12"]                       # directory verification
    ctl.reads.clear()
    out = await read_all(drv)
    assert {k: (v.value, v.quality) for k, v in out.items()} == {k: (v, "good") for k, v in STD_VALUES.items()}
    assert all(v.source_ts is not None and v.source_ts.tzinfo is not None for v in out.values())
    assert sorted(ctl.reads) == sorted(blocks(drv))                    # exactly the block addresses, once each
    assert "B3:1" in ctl.reads and "F8:0{2}" in ctl.reads              # bit-number B3/16, B3/17 -> word 1
    await drv.close()
    assert not drv.connected and plc.closed == 1


async def test_values_go_through_decode_block(make_driver, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []
    real = ab_pccc.decode_block

    def spy(block, values):
        seen.append((block.address, values))
        return real(block, values)
    monkeypatch.setattr(ab_pccc, "decode_block", spy)
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0"), T("N7:1", "int16")])
    await drv.connect()
    await read_all(drv)
    assert seen == [("N7:0{2}", [1, -2])]


async def test_timeout_and_path_applied_before_open(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0")], timeout_ms=2500, port=2222, path="1,0")
    await drv.connect()
    plc = ctl.plcs[-1]
    assert plc.path == "192.0.2.12:2222/1/0"
    assert plc.opened_with["socket_timeout"] == 2.5


@pytest.mark.parametrize("reply", [sts_reply(0x10), sts_reply(0x50), sts_reply(0xF0, 0x06), sts_reply(0xF0, 0x0A)])
async def test_pccc_address_errors_are_block_config_errors(make_driver, reply: PcccReply) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0"), T("N7:1")], file_check=False)
    await drv.connect()
    ctl.replies["N7:0{2}"] = PcccResult(None, "pycomm3 text", True, reply)
    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            with pytest.raises(BlockConfigError, match="PCCC STS"):
                await drv.read(blocks(drv)["N7:0{2}"])
    assert drv.connected                                               # a config error keeps the connection
    rejected = [e for e in logs if e["event"] == "ab_pccc.block_rejected"]
    assert len(rejected) == 1 and rejected[0]["source"] == "plc" and rejected[0]["log_level"] == "warning"


@pytest.mark.parametrize(("reply", "text"), [
    (sts_reply(0x60), "command protection"), (sts_reply(0xF0, 0x0B), "access denied"),
    (sts_reply(0x70), "Program mode"), (PcccReply(0, 0x08), "CIP status 0x08"),
    (PcccReply(0, 0, None, 0, None, 9, b""), "requestor ID length 9"), (None, "no reply status"),
])
async def test_other_pccc_and_cip_errors_are_comm_errors(make_driver, reply: PcccReply | None, text: str) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0")])
    await drv.connect()
    ctl.replies["N7:0"] = PcccResult(None, "x", True, reply)
    with pytest.raises(CommError, match=text) as info:
        await drv.read(blocks(drv)["N7:0"])
    assert type(info.value) is CommError
    assert drv.connected and ctl.plcs[-1].aborted == 0


@pytest.mark.parametrize("reply", [PcccReply(0x64), PcccReply(0, 0x01, 0x0107)])
async def test_encapsulation_error_and_cip_connection_failure_are_connection_lost(make_driver, reply) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0")])
    await drv.connect()
    ctl.replies["N7:0"] = PcccResult(None, "Unknown Status", True, reply)
    with pytest.raises(ConnectionLost, match="connection closed"):
        await drv.read(blocks(drv)["N7:0"])
    assert not drv.connected and ctl.plcs[-1].aborted == 1 and ctl.plcs[-1].closed == 0


async def test_reply_timeout_closes_then_comm_error(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0")], timeout_ms=700)
    await drv.connect()
    ctl.read_raises.append((comm_error(TimeoutError("timed out")), True))
    with pytest.raises(CommError) as info:
        await drv.read(blocks(drv)["N7:0"])
    assert type(info.value) is CommError and "no reply within 700 ms" in str(info.value)
    assert not drv.connected
    assert ctl.plcs[-1].aborted == 1 and ctl.plcs[-1].closed == 0      # no Forward Close to a mute PLC
    with pytest.raises(ConnectionLost, match="not connected"):
        await drv.read(blocks(drv)["N7:0"])
    await drv.close()                                                  # idempotent after the drop
    assert ctl.plcs[-1].closed == 0


@pytest.mark.parametrize("cause", [ConnectionResetError("reset"), BrokenPipeError("pipe"),
                                   ConnectionResetError("connection closed by the PLC")])
async def test_connection_errors_are_connection_lost(make_driver, cause: BaseException) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0")])
    await drv.connect()
    ctl.read_raises.append((comm_error(cause), True))
    with pytest.raises(ConnectionLost):
        await drv.read(blocks(drv)["N7:0"])
    assert not drv.connected and ctl.plcs[-1].aborted == 1


async def test_pycomm3_cannot_build_the_request_is_block_config_error(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0")])
    await drv.connect()
    ctl.read_raises.append((RequestError("Error parsing the tag passed to read() - N7:0"), False))
    with pytest.raises(BlockConfigError, match="cannot build a read"):
        await drv.read(blocks(drv)["N7:0"])
    ctl.read_raises.append((ResponseError("Target did not connected. read will not be executed."), True))
    with pytest.raises(CommError, match="pycomm3 read failed") as info:
        await drv.read(blocks(drv)["N7:0"])
    assert type(info.value) is CommError and drv.connected


async def test_unparsed_or_short_reply_is_bad_decode(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("F8:0", "float32"), T("F8:1", "float32")], file_check=False)
    await drv.connect()
    block = blocks(drv)["F8:0{2}"]
    ctl.replies[block.address] = PcccResult(None, "Failed parsing tag read reply", True, GOOD)
    out = await drv.read(block)
    assert {v.quality for v in out.values()} == {"bad_decode"}
    ctl.replies[block.address] = PcccResult(1.25, None, True, GOOD)       # one element instead of two
    out = await drv.read(block)
    assert (out["t_F8:0"].value, out["t_F8:0"].quality) == (1.25, "good")
    assert (out["t_F8:1"].value, out["t_F8:1"].quality) == (None, "bad_decode")


@pytest.mark.parametrize("failure", ["refused", "no_session", "fo_refused", "identity_timeout"])
async def test_connect_failures_are_connection_lost(make_driver, failure: str) -> None:
    ctl = FakeController()
    if failure == "refused":
        exc = PycommCommError("failed to open a connection")
        exc.__cause__ = PycommCommError("Failed to open socket to 192.0.2.12:44818")
        ctl.open_result = exc
    elif failure == "no_session":
        ctl.open_result = False
    elif failure == "fo_refused":
        ctl.fo_exc = ResponseError("Target did not connected. <lambda> will not be executed.")
    else:
        ctl.identity_exc = comm_error(TimeoutError("timed out"))
    drv = make_driver(ctl, STD_TAGS)
    with pytest.raises(ConnectionLost, match="connect failed"):
        await drv.connect()
    assert not drv.connected and await drv.identity() == {}
    plc = ctl.plcs[-1]
    assert plc.closed + plc.aborted == 1                               # never leaks the connection
    if failure == "identity_timeout":
        assert plc.aborted == 1                                        # no Forward Close to a mute PLC
    if failure == "fo_refused":
        assert plc.closed == 1


async def test_close_is_idempotent_and_never_raises(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, STD_TAGS)
    await drv.close()                                                  # never connected
    await drv.connect()
    ctl.close_raises = PycommCommError("forward_close failed")
    await drv.close()
    await drv.close()
    assert not drv.connected
    assert ctl.plcs[-1].closed == 1 and ctl.plcs[-1].aborted == 1      # graceful failed -> socket dropped


async def test_close_without_executor_drops_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    ctl = FakeController()
    monkeypatch.setattr(ab_pccc, "slc_driver_class", lambda: functools.partial(FakePlc, ctl=ctl))
    ex = ThreadPoolExecutor(max_workers=1)
    drv = AbPcccDriver(make_device(STD_TAGS), ex)
    await drv.connect()

    class Sock:
        closed = False

        def close(self) -> None:
            self.closed = True
    sock = ctl.plcs[-1]._sock = Sock()
    ex.shutdown(wait=True)
    await drv.close()                                                  # executor gone: no raise
    assert sock.closed and not drv.connected


async def test_connect_needs_executor_and_closes_previous_connection(make_driver) -> None:
    with pytest.raises(RuntimeError, match="executor"):
        await AbPcccDriver(make_device(STD_TAGS)).connect()
    ctl = FakeController()
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    first = ctl.plcs[-1]
    await drv.connect()
    assert first.closed == 1 and ctl.plcs[-1] is not first and drv.connected


async def test_identity_fields(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, STD_TAGS)
    assert await drv.identity() == {}
    await drv.connect()
    ident = await drv.identity()
    for key in ("vendor", "product_name", "product_code", "revision", "serial", "processor_type"):
        assert ident[key] is not None, key
    assert ident["product_name"] == "1766-L32BXB B/14.00" and ident["revision"] == "14.0"
    assert ident["serial"] == "40c0ffee" and ident["processor_type"] == "1766-L32BXB"
    assert ident["connection_size"] == 500
    assert ident["file_directory"] == "ok: 9 files" and ident["files"]["N54"] == 18 and ident["files"]["S2"] == 40
    await drv.close()
    assert await drv.identity() == {}


async def test_read_rejects_other_blocks_and_needs_connection(make_driver) -> None:
    from opencap.drivers.base import Block
    ctl = FakeController()
    drv = make_driver(ctl, STD_TAGS)
    with pytest.raises(ConnectionLost, match="not connected"):
        await drv.read(blocks(drv)["N7:1{2}"])
    await drv.connect()
    with pytest.raises(TypeError):
        await drv.read(Block(tags=["x"]))


# --- file directory ---

@pytest.mark.parametrize(("tags", "address", "reason"), [
    ([T("N9:0")], "N9:0", "file N9 does not exist"),
    ([T("F54:17", "float32")], "F54:17", "file 54 is a N file, not F"),           # the real-project typo
    ([T("N7:2"), T("N7:4")], "N7:2{3}", "N7 has 4 elements, the block needs 2..4"),
])
async def test_directory_rejects_without_sending(make_driver, tags: list[Tag], address: str, reason: str) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, tags)
    await drv.connect()
    ctl.reads.clear()
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(BlockConfigError, match=re.escape(reason)):
            await drv.read(blocks(drv)[address])
    assert ctl.reads == []                                             # nothing went on the wire
    (event,) = [e for e in logs if e["event"] == "ab_pccc.block_rejected"]
    assert event["source"] == "file_directory" and event["tags"] == [t.name for t in tags]


async def test_directory_read_once_per_instance_and_identity(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, STD_TAGS)
    assert drv.connect_timeout_s() == 60.0
    await drv.connect()
    assert drv.connect_timeout_s() == pytest.approx(7.0)               # 6 x 1 s + 1 s once the directory is known
    await drv.close()
    await drv.connect()
    ctl.read_raises.append((comm_error(ConnectionResetError("reset")), True))
    with pytest.raises(ConnectionLost):
        await drv.read(blocks(drv)["N7:1{2}"])
    await drv.connect()
    assert ctl.directory_reads == 1
    ctl.identity["serial"] = "0badf00d"                                # PLC swapped
    ctl.files[9] = ("N", [0])
    await drv.connect()
    assert ctl.directory_reads == 2 and (await drv.identity())["files"]["N9"] == 1


async def test_directory_unavailable_skips_the_check(make_driver) -> None:
    ctl = FakeController()
    ctl.directory = DirectoryUnavailable("size not readable: PCCC STS 0x10")
    drv = make_driver(ctl, [T("N9:0")])
    with structlog.testing.capture_logs() as logs:
        await drv.connect()
    assert any(e["event"] == "ab_pccc.file_directory" and e["log_level"] == "warning" for e in logs)
    ident = await drv.identity()
    assert ident["file_directory"].startswith("unavailable: size not readable") and "files" not in ident
    ctl.reads.clear()
    with pytest.raises(BlockConfigError, match="STS 0x10"):           # the PLC says so instead
        await drv.read(blocks(drv)["N9:0"])
    assert ctl.reads == ["N9:0"]
    await drv.connect()
    assert ctl.directory_reads == 1                                    # the outcome is kept too


@pytest.mark.parametrize(("setup", "status"), [
    (lambda c: c.replies.__setitem__("N66:12", PcccResult(7, None, True, GOOD)), "N66:12 is readable although"),
    (lambda c: c.replies.__setitem__("N66:11", PcccResult(None, "x", True, sts_reply(0x10))), "N66:11 is listed but"),
    (lambda c: c.replies.__setitem__("N66:12", PcccResult(None, "x", True, sts_reply(0x60))), "expected an address"),
    (lambda c: setattr(c, "processor_type", None), "processor type unknown"),
    (lambda c: setattr(c, "directory", {0: ("O", 1), 1: ("I", 1), 2: ("S", 4), 4: ("T", 2)}), "no N/F/L/B file"),
    (lambda c: setattr(c, "directory", ResponseError("Failed to read file directory size")), "Failed to read"),
])
async def test_directory_not_trusted(make_driver, setup, status: str) -> None:
    ctl = FakeController()
    setup(ctl)
    drv = make_driver(ctl, [T("N9:0")])
    await drv.connect()
    ident = await drv.identity()
    assert ident["file_directory"].startswith("unavailable") and status in ident["file_directory"], ident
    ctl.reads.clear()
    with pytest.raises(BlockConfigError):
        await drv.read(blocks(drv)["N9:0"])
    assert ctl.reads == ["N9:0"]                                       # no directory: the read goes out


async def test_directory_no_answer_fails_connect_then_gives_up(make_driver) -> None:
    ctl = FakeController()
    ctl.directory = comm_error(TimeoutError("timed out"))
    drv = make_driver(ctl, STD_TAGS)
    for _ in range(2):
        with pytest.raises(ConnectionLost, match="connect failed"):
            await drv.connect()
        assert ctl.plcs[-1].aborted == 1
    await drv.connect()                                                # given up: no third read
    assert ctl.directory_reads == 2 and drv.connected
    assert (await drv.identity())["file_directory"].startswith("unavailable: 2 reads in a row")


async def test_directory_link_error_during_verification_fails_connect(make_driver) -> None:
    ctl = FakeController()
    ctl.replies["N66:12"] = PcccResult(None, "Unknown Status", True, PcccReply(0x64))
    drv = make_driver(ctl, STD_TAGS)
    with pytest.raises(ConnectionLost, match="encapsulation status"):
        await drv.connect()
    assert ctl.plcs[-1].aborted == 1 and ctl.plcs[-1].closed == 0


async def test_file_check_off(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N9:0")], file_check=False)
    assert drv.connect_timeout_s() == pytest.approx(7.0)
    await drv.connect()
    assert ctl.directory_reads == 0 and ctl.reads == []
    assert (await drv.identity())["file_directory"] == "off (params.file_check)"


async def test_cancelled_connect_does_not_leak_and_keeps_directory(make_driver) -> None:
    """The worker's safety net cancels a slow connect; the executor job closes what it opened
    and keeps the directory it read for the next attempt."""
    ctl = FakeController()
    ctl.directory_gate = threading.Event()
    drv = make_driver(ctl, STD_TAGS)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(drv.connect(), 0.05)
    first = ctl.plcs[-1]
    ctl.directory_gate.set()
    for _ in range(200):
        if first.closed:
            break
        await asyncio.sleep(0.01)
    assert first.closed == 1 and not drv.connected
    ctl.directory_gate = None
    await drv.connect()
    assert ctl.directory_reads == 1 and drv.connected


# --- concurrency ---

async def test_every_pycomm3_call_runs_on_the_executor(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    await read_all(drv)
    ctl.read_raises.append((comm_error(TimeoutError("timed out")), True))
    with pytest.raises(CommError):
        await drv.read(blocks(drv)["N7:1{2}"])
    await drv.connect()
    await drv.identity()
    await drv.close()
    seen = {what for what, _ in ctl.threads}
    assert {"__init__", "open", "oo_list_identity", "oo_forward_open", "oo_processor_type", "oo_file_directory",
            "oo_read", "close", "oo_abort"} <= seen
    assert all(thread.startswith(EXEC_PREFIX) for _, thread in ctl.threads), ctl.threads
    assert threading.current_thread().name not in {t for _, t in ctl.threads}


async def test_slow_read_does_not_block_the_event_loop(make_driver) -> None:
    ctl = FakeController()
    drv = make_driver(ctl, [T("N7:0")])
    await drv.connect()
    plc = ctl.plcs[-1]
    real = FakePlc.oo_read

    def slow(self, address):
        time.sleep(0.2)
        return real(self, address)
    plc.oo_read = functools.partial(slow, plc)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1
    task = asyncio.create_task(ticker())
    await drv.read(blocks(drv)["N7:0"])
    task.cancel()
    assert ticks >= 5


def test_connect_timeout_budget() -> None:
    drv = AbPcccDriver(make_device(timeout_ms=5000))
    assert drv.connect_timeout_s() == 100.0                            # 20 x timeout before the directory
    assert AbPcccDriver(make_device(timeout_ms=5000, file_check=False)).connect_timeout_s() == 31.0


# --- read-only ---

def test_driver_source_never_writes() -> None:
    """Fails if ab_pccc.py calls a pycomm3 write path, a data-log read or generic_message."""
    src = Path(ab_pccc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden_calls = {"write", "_write_tag", "writeable_value", "get_datalog_queue", "_get_datalog",
                       "generic_message", "set_plc_time"}
    forbidden_names = {"SLC_FNC_WRITE", "WriteTagRequestPacket", "writeable_value"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in forbidden_calls:
                # the only allowed one: the read-only guard forwarding generic_message to pycomm3
                # after refusing write services (`super().generic_message(...)`)
                is_guard_forward = (name == "generic_message" and isinstance(func, ast.Attribute)
                                    and isinstance(func.value, ast.Call)
                                    and getattr(func.value.func, "id", None) == "super")
                if not is_guard_forward:
                    bad.append((node.lineno, name))
        elif isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in forbidden_names:
                bad.append((node.lineno, name))
    assert bad == []
    assert AbPcccDriver.write is ab_pccc.Driver.write                  # base: raises NotImplementedError
    assert ab_pccc.READ_COMMANDS == {(0x0F, 0xA2), (0x0F, 0xA1), (0x06, 0x03)}
