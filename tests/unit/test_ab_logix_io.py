"""ab_logix driver I/O (docs/phase2_contracts.md §1).

Three layers:
1. pure helpers (path, batching param, tag existence, probe choice, error mapping);
2. the pycomm3 `LogixDriver` subclass, driven through real pycomm3 1.2.16 code with crafted
   reply bytes on a fake socket: this pins the private methods it overrides;
3. `AbLogixDriver` with a fake pycomm3 (scripted controller) on a real single-thread executor.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import structlog
from pycomm3.const import BASE_TAG_BIT
from pycomm3.exceptions import CommError as PycommCommError
from pycomm3.exceptions import RequestError, ResponseError

from opencap.config.models import Device, Tag
from opencap.drivers import ab_logix
from opencap.drivers.ab_logix import (
    AbLogixDriver,
    LogixBlock,
    NameResult,
    WholeReplyError,
    connection_path,
    describe_exc,
    is_comm_error,
    is_config_status,
    is_timeout,
    logix_driver_class,
    parse_batching,
    probe_names,
    resolve_read_name,
    result_quality,
)
from opencap.drivers.base import CommError, ConnectionLost

EXEC_PREFIX = "ab-logix-test-exec"


# --- helpers ------------------------------------------------------------------------------

def make_device(tags: list[Tag] | None = None, *, micro800: bool = True, timeout_ms: int = 1000,
                host: str = "192.0.2.14", port: int = 44818, **params: Any) -> Device:
    return Device(
        id="SITE/PLC", channel="SITE", name="PLC", driver="ab_logix", host=host, port=port,
        params={"micro800": micro800, **params}, tags=tags or [], timeout_ms=timeout_ms,
    )


def T(address: str, dtype: str = "int32", name: str | None = None) -> Tag:
    return Tag(name=name or f"t_{address}", address=address, dtype=dtype)


def atomic(dt: str = "DINT", *, dim: int = 0, access: str = "Read/Write") -> dict:
    return {"tag_type": "atomic", "data_type": dt, "data_type_name": dt, "dim": dim, "external_access": access}


def struct_tag(type_name: str, members: dict[str, dict]) -> dict:
    return {"tag_type": "struct", "data_type": {"name": type_name, "internal_tags": members},
            "data_type_name": type_name, "dim": 0, "external_access": "Read/Write"}


def comm_error(cause: BaseException) -> PycommCommError:
    """A pycomm3 CommError the way its socket layer raises it (`raise ... from err`)."""
    exc = PycommCommError("socket connection broken")
    exc.__cause__ = cause
    return exc


def single_block(drv: AbLogixDriver) -> LogixBlock:
    plan = drv.plan(drv.device.tags)
    assert len(plan.blocks) == 1, plan.blocks
    block = plan.blocks[0]
    assert isinstance(block, LogixBlock)
    return block


# --- 1. pure helpers --------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "mode"), [
    (None, "auto"), ("auto", "auto"), ("ON", "on"), (" off ", "off"), (True, "on"), (False, "off"),
])
def test_parse_batching(value: Any, mode: str) -> None:
    assert parse_batching(value) == mode


def test_parse_batching_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="batching"):
        parse_batching("sometimes")
    with pytest.raises(ValueError, match="batching"):
        AbLogixDriver(make_device(batching="maybe"))


@pytest.mark.parametrize(("micro800", "port", "params", "path"), [
    (True, 44818, {"path": "1,0", "slot": 0}, "192.0.2.14:44818"),          # Micro800 ignores slot/path
    (True, 2222, {}, "192.0.2.14:2222"),
    (False, 44818, {"path": "1,0", "slot": 0}, "192.0.2.14:44818/0"),        # <ip>,1,0 -> slot 0
    (False, 44818, {"path": "1,3", "slot": None}, "192.0.2.14:44818/3"),
    (False, 44818, {"slot": 2}, "192.0.2.14:44818/2"),
    (False, 44818, {}, "192.0.2.14:44818"),                                  # pycomm3 adds bp/0
    (False, 44818, {"path": "1,0,2,192.0.2.13,1,0", "slot": 0}, "192.0.2.14:44818/1/0/2/192.0.2.13/1/0"),
    (False, 44818, {"path": "2,192.0.2.13"}, "192.0.2.14:44818/2/192.0.2.13"),
])
def test_connection_path(micro800: bool, port: int, params: dict, path: str) -> None:
    assert connection_path(make_device(micro800=micro800, port=port, **params)) == path


def test_connection_path_parses_with_pycomm3() -> None:
    from pycomm3.cip_driver import parse_connection_path
    ip, port, route = parse_connection_path("192.0.2.14:44818/3", auto_slot=True)
    assert (ip, port, len(route)) == ("192.0.2.14", 44818, 1)


@pytest.mark.parametrize("params", [{"path": "1,0,2"}, {"slot": "x"}, {"slot": 300}, {"connection_size": 0},
                                    {"connection_size": "500"}])
def test_bad_params_raise_value_error(params: dict) -> None:
    with pytest.raises(ValueError):
        AbLogixDriver(make_device(micro800=False, **params))


TAGS = {
    "FLOW": atomic("REAL"),
    "Arr": atomic("DINT", dim=1),
    "Hidden": atomic("DINT", access="None"),
    "Pump": struct_tag("PUMP_T", {
        "Run": {"tag_type": "atomic", "data_type": "BOOL", "data_type_name": "BOOL"},
        "Motor": {"tag_type": "struct", "data_type": {"name": "MOTOR_T", "internal_tags": {
            "Amps": {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"}}}},
    }),
    "Program:Main.Local": atomic("INT"),
}
FOLDED = {k.casefold(): k for k in TAGS}


@pytest.mark.parametrize(("name", "plc_name"), [
    ("FLOW", "FLOW"),
    ("flow", "FLOW"),                                # Logix names are case-insensitive
    ("Arr[3]", "Arr[3]"),
    ("Pump.Run", "Pump.Run"),
    ("pump.motor.AMPS", "Pump.Motor.Amps"),
    ("Program:Main.Local", "Program:Main.Local"),
])
def test_resolve_existing(name: str, plc_name: str) -> None:
    assert resolve_read_name(name, TAGS, FOLDED) == (plc_name, None)


@pytest.mark.parametrize(("name", "reason"), [
    ("NOPE", "not in the controller's tag list"),
    ("Pump.Stop", "has no member 'Stop'"),
    ("FLOW.X", "is not a structure"),
    ("Pump.Run.X", "is not a structure"),
    ("Hidden", "External Access None"),
    ("Program:Main", "program name without a tag"),
    ("Program:Other.Local", "not in the controller's tag list"),
])
def test_resolve_missing(name: str, reason: str) -> None:
    plc_name, why = resolve_read_name(name, TAGS, FOLDED)
    assert plc_name is None and reason in why


def test_resolve_is_case_sensitive_without_index() -> None:
    assert resolve_read_name("flow", TAGS)[0] is None


def test_probe_names() -> None:
    assert probe_names(TAGS) == ["FLOW", "FLOW"]     # only one readable scalar atomic controller tag
    tags = {"b": atomic(), "a": atomic("REAL"), "c": atomic(), "s": struct_tag("X", {})}
    assert probe_names(tags) == ["a", "b"]
    assert probe_names({"s": struct_tag("X", {}), "arr": atomic(dim=1)}) == []


@pytest.mark.parametrize(("result", "quality"), [
    (NameResult(1, None, True, 0), None),
    (NameResult(False, None, True, 0), None),                               # a False BOOL is a value
    (NameResult(None, "Tag doesn't exist - X", False), "bad_config"),        # never sent
    (NameResult(None, "Destination unknown", True, 0x05), "bad_config"),
    (NameResult(None, "IOI syntax", True, 0x04), "bad_config"),
    (NameResult(None, "Object does not exist", True, 0x16), "bad_config"),
    (NameResult(None, "General error", True, 0xFF, 0x2105), "bad_config"),   # index beyond the tag
    (NameResult(None, "General error", True, 0xFF, 0x0001), "bad_comm"),
    (NameResult(None, "Permission denied", True, 0x0F), "bad_comm"),
    (NameResult(None, "Insufficient resource", True, 0x02), "bad_comm"),
    (NameResult(None, "One or more fragment responses failed", True, None), "bad_comm"),
    (NameResult(None, "Failed to parse reply", True, 0), "bad_decode"),
])
def test_result_quality(result: NameResult, quality: str | None) -> None:
    assert result_quality(result) == quality


def test_error_chain_helpers() -> None:
    timeout = comm_error(TimeoutError("timed out"))
    reset = comm_error(ConnectionResetError("reset by peer"))
    assert is_timeout(timeout) and not is_timeout(reset)
    wrapped = ResponseError("failed to get attribute list")
    wrapped.__cause__ = timeout
    assert is_comm_error(wrapped) and is_timeout(wrapped)
    assert not is_comm_error(ResponseError("x"))
    assert describe_exc(reset) == "socket connection broken: reset by peer"
    assert is_config_status(0x05) and not is_config_status(0x08)


# --- 2. the pycomm3 subclass, on real pycomm3 code with crafted replies -------------------------

def _cip_reply(service: int, status: int, data: bytes = b"", ext: tuple[int, ...] = (),
               *, command_status: int = 0) -> bytes:
    """A SendUnitData reply exactly as a controller frames it."""
    cip = bytes([service | 0x80, 0, status, len(ext)]) + b"".join(struct.pack("<H", e) for e in ext) + data
    payload = struct.pack("<H", 1) + cip                                 # connected sequence count
    cpf = (struct.pack("<IHH", 0, 0, 2) + struct.pack("<HH", 0xA1, 4) + b"\x01\x02\x03\x04"
           + struct.pack("<HH", 0xB1, len(payload)) + payload)
    header = struct.pack("<HHII", 0x70, len(cpf), 1, command_status) + b"_pycomm_" + struct.pack("<I", 0)
    return header + cpf


def _read_reply(status: int, type_code: int = 0, value: bytes = b"") -> bytes:
    """One embedded Read Tag (0x4C) reply."""
    body = struct.pack("<H", type_code) + value if status == 0 else b""
    return bytes([0xCC, 0, status, 0]) + body


def _msp(replies: list[bytes], count: int | None = None) -> bytes:
    n = len(replies) if count is None else count
    offsets, pos = [], 2 + 2 * n
    for r in replies:
        offsets.append(struct.pack("<H", pos))
        pos += len(r)
    return struct.pack("<H", n) + b"".join(offsets[:n]) + b"".join(replies)


DINT, REAL = 0xC4, 0xCA
MSP_REQUEST = b"\x0a\x02\x20\x02\x24\x01"       # service 0x0A to Message Router class 2 instance 1


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


@pytest.fixture
def plc_factory():
    cls = logix_driver_class()

    def make(replies: list[bytes | BaseException], *, micro800: bool = True):
        plc = cls("192.0.2.14:44818", init_tags=False)
        raw = {"instance_id": 1, "symbol_address": 0, "symbol_object_address": 0,
               "software_control": BASE_TAG_BIT, "external_access": "Read/Write", "dimensions": [0, 0, 0]}
        plc._tags = {"A": plc._create_tag("A", {**raw, "symbol_type": DINT}),
                     "B": plc._create_tag("B", {**raw, "instance_id": 2, "symbol_type": REAL})}
        plc._micro800 = micro800
        plc._session = 1
        plc._target_is_connected = True
        plc._connection_opened = True
        plc._cfg["connection_size"] = 500
        plc.oo_symbolic()
        plc._sock = FakeSock(replies)
        return plc
    return make


def test_pycomm3_alone_never_batches_micro800(plc_factory) -> None:
    """Pins the pycomm3 1.2.16 behaviour this driver works around (oo_batching=None)."""
    plc = plc_factory([_cip_reply(0x4C, 0, struct.pack("<Hi", DINT, 7)),
                       _cip_reply(0x4C, 0, struct.pack("<Hf", REAL, 1.5))])
    res = plc.oo_read(["A", "B"])
    assert [r.value for r in res] == [7, 1.5]
    assert len(plc._sock.sent) == 2 and not any(MSP_REQUEST in m for m in plc._sock.sent)


def test_micro800_batching_uses_multiple_service_packet(plc_factory) -> None:
    reply = _cip_reply(0x0A, 0, _msp([_read_reply(0, DINT, struct.pack("<i", -3)),
                                      _read_reply(0, REAL, struct.pack("<f", 2.5))]))
    plc = plc_factory([reply])
    plc.oo_batching = True
    res = plc.oo_read(["A", "B"])
    assert [(r.value, r.ok, r.sent, r.status) for r in res] == [(-3, True, True, 0), (2.5, True, True, 0)]
    assert len(plc._sock.sent) == 1 and MSP_REQUEST in plc._sock.sent[0]
    assert b"\x91\x01A\x00" in plc._sock.sent[0]       # symbolic segment, not a Symbol instance id


def test_batching_off_sends_one_read_per_name_even_on_logix(plc_factory) -> None:
    plc = plc_factory([_cip_reply(0x4C, 0, struct.pack("<Hi", DINT, 1)),
                       _cip_reply(0x4C, 0, struct.pack("<Hf", REAL, 2.0))], micro800=False)
    plc.oo_batching = False
    assert [r.value for r in plc.oo_read(["A", "B"])] == [1, 2.0]
    assert len(plc._sock.sent) == 2


def test_logix_default_is_multiple_service_packet(plc_factory) -> None:
    reply = _cip_reply(0x0A, 0, _msp([_read_reply(0, DINT, struct.pack("<i", 1)),
                                      _read_reply(0, REAL, struct.pack("<f", 1.0))]))
    plc = plc_factory([reply], micro800=False)
    assert plc.oo_batching is None
    plc.oo_read(["A", "B"])
    assert len(plc._sock.sent) == 1 and MSP_REQUEST in plc._sock.sent[0]


def test_msp_status_0x08_without_data_is_whole_reply_error(plc_factory) -> None:
    """pycomm3's own parser raises BufferEmptyError on this reply; the hook keeps the status."""
    plc = plc_factory([_cip_reply(0x0A, 0x08)])
    plc.oo_batching = True
    with pytest.raises(WholeReplyError) as info:
        plc.oo_read(["A", "B"])
    assert info.value.status == 0x08 and info.value.multi and not info.value.encapsulation


def test_msp_status_0x08_with_extended_status(plc_factory) -> None:
    plc = plc_factory([_cip_reply(0x0A, 0x08, ext=(0x0001,))])
    plc.oo_batching = True
    with pytest.raises(WholeReplyError) as info:
        plc.oo_read(["A", "B"])
    assert info.value.status == 0x08


@pytest.mark.parametrize("data", [
    b"\x02",                                                            # truncated count
    _msp([_read_reply(0, DINT, struct.pack("<i", 1))], count=1),       # 1 reply for 2 requests
])
def test_malformed_msp_reply_is_whole_reply_error(plc_factory, data: bytes) -> None:
    plc = plc_factory([_cip_reply(0x0A, 0, data)])
    plc.oo_batching = True
    with pytest.raises(WholeReplyError) as info:
        plc.oo_read(["A", "B"])
    assert info.value.multi


def test_msp_embedded_error_is_per_tag(plc_factory) -> None:
    reply = _cip_reply(0x0A, 0x1E, _msp([_read_reply(0, DINT, struct.pack("<i", 5)), _read_reply(0x05)]))
    plc = plc_factory([reply])
    plc.oo_batching = True
    a, b = plc.oo_read(["A", "B"])
    assert a.ok and a.value == 5
    assert (b.ok, b.sent, b.status) == (False, True, 0x05)
    assert result_quality(b) == "bad_config"


def test_single_read_cip_error_and_extended_status(plc_factory) -> None:
    plc = plc_factory([_cip_reply(0x4C, 0xFF, ext=(0x2105,))])
    (r,) = plc.oo_read(["A"])
    assert (r.sent, r.status, r.ext_status) == (True, 0xFF, 0x2105)
    assert result_quality(r) == "bad_config"


def test_encapsulation_error_is_whole_reply_error(plc_factory) -> None:
    plc = plc_factory([_cip_reply(0x4C, 0, struct.pack("<Hi", DINT, 1), command_status=0x64)])
    with pytest.raises(WholeReplyError) as info:
        plc.oo_read(["A"])
    assert info.value.encapsulation


def test_unknown_name_is_not_sent(plc_factory) -> None:
    plc = plc_factory([_cip_reply(0x0A, 0, _msp([_read_reply(0, DINT, struct.pack("<i", 9))]))])
    plc.oo_batching = True
    a, missing = plc.oo_read(["A", "Missing"])
    assert a.value == 9 and len(plc._sock.sent) == 1     # a 1-service packet: only "A" went out
    assert (missing.sent, missing.ok) == (False, False) and "doesn't exist" in missing.error
    assert result_quality(missing) == "bad_config"


def test_socket_error_propagates_as_pycomm3_comm_error(plc_factory) -> None:
    plc = plc_factory([comm_error(TimeoutError("timed out"))])
    plc.oo_batching = True
    with pytest.raises(PycommCommError) as info:
        plc.oo_read(["A", "B"])
    assert is_timeout(info.value)


def test_subclass_refuses_writes(plc_factory) -> None:
    from pycomm3.packets import WriteTagRequestPacket
    plc = plc_factory([])
    with pytest.raises(NotImplementedError):
        plc.write("A", 1)
    with pytest.raises(NotImplementedError):
        plc.set_plc_time()
    for service in (b"\x10", b"\x04", 0x02, b"\x4D", b"\x53"):
        with pytest.raises(NotImplementedError):
            plc.generic_message(service=service, class_code=b"\x6b", instance=1)
    packet = WriteTagRequestPacket(plc._sequence, "A", 1, plc._tags["A"], 0, False, b"\x01\x00\x00\x00")
    with pytest.raises(RequestError, match="read-only"):
        plc.send(packet)
    assert plc._sock.sent == []


def test_open_uses_socket_timeout_and_own_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = logix_driver_class()
    plc = cls("127.0.0.1:1", init_tags=False)
    plc._cfg["socket_timeout"] = 0.25

    def refuse(self, host, port):
        raise PycommCommError(f"Failed to open socket to {host}:{port}")
    monkeypatch.setattr(cls.socket_class, "connect", refuse)
    with pytest.raises(PycommCommError):
        plc.open()
    assert isinstance(plc._sock, cls.socket_class)
    assert plc._sock.sock.gettimeout() == 0.25
    plc._sock.close()


@pytest.fixture
def sock_pair():
    cls = logix_driver_class()
    ours = cls.socket_class(1.0)
    ours.sock.close()
    a, b = socket.socketpair()
    a.settimeout(1.0)
    ours.sock = a
    yield ours, b
    a.close()
    b.close()


def test_socket_reads_one_message_from_chunks(sock_pair) -> None:
    ours, peer = sock_pair
    msg = _cip_reply(0x4C, 0, struct.pack("<Hi", DINT, 1))

    def feed() -> None:
        for i in range(0, len(msg), 7):
            peer.sendall(msg[i:i + 7])
            time.sleep(0.002)
        peer.sendall(b"NEXT")                  # bytes of a following message are not consumed
    t = threading.Thread(target=feed)
    t.start()
    assert ours.receive() == msg
    t.join()
    assert peer is not None and ours.sock.recv(4) == b"NEXT"


def test_socket_peer_close_mid_reply_fails_fast(sock_pair) -> None:
    """pycomm3's own reader loops forever here (recv returns b'' after the close)."""
    ours, peer = sock_pair
    msg = _cip_reply(0x4C, 0, struct.pack("<Hi", DINT, 1))
    peer.sendall(msg[:30])
    peer.close()
    started = time.monotonic()
    with pytest.raises(PycommCommError) as info:
        ours.receive()
    assert time.monotonic() - started < 0.5
    assert not is_timeout(info.value)
    assert any(isinstance(e, ConnectionResetError) for e in ab_logix._chain(info.value))


def test_socket_timeout_is_a_timeout(sock_pair) -> None:
    ours, _peer = sock_pair
    ours.sock.settimeout(0.05)
    with pytest.raises(PycommCommError) as info:
        ours.receive()
    assert is_timeout(info.value)


# --- 3. AbLogixDriver with a fake pycomm3 --------------------------------------------------------

DEFAULT_INFO = {
    "vendor": "Rockwell Automation/Allen-Bradley", "product_type": "Programmable Logic Controller",
    "product_code": 166, "revision": {"major": 12, "minor": 11}, "serial": "c0ffee01",
    "product_name": "2080-LC50-48QWB", "keyswitch": "REMOTE RUN",
}


class FakeController:
    """Scripted controller shared by every FakePlc one driver creates."""

    def __init__(self, tags: dict[str, dict], values: dict[str, Any], *, micro800: bool = True,
                 msp: str | int = "ok") -> None:
        self.tags = tags
        self.values = values                    # controller name -> value
        self.errors: dict[str, NameResult] = {}
        self.micro800 = micro800
        self.msp = msp                          # "ok" | CIP status | "malformed" | "timeout" | "error"
        self.info = dict(DEFAULT_INFO, product_name="2080-LC50-48QWB" if micro800 else "1756-L61/B LOGIX5561")
        self.open_result: bool | BaseException = True
        self.upload_exc: BaseException | None = None
        self.upload_gate: threading.Event | None = None
        self.read_raises: list[BaseException] = []
        self.hook_effective = True               # False: pycomm3 ignored oo_batching (sent singles)
        self.close_raises: BaseException | None = None
        self.plcs: list[FakePlc] = []
        self.threads: list[tuple[str, str]] = []
        self.reads: list[tuple[tuple[str, ...], bool | None]] = []
        self.uploads = 0

    def record(self, what: str) -> None:
        self.threads.append((what, threading.current_thread().name))


class FakePlc:
    """The `OoLogixDriver` surface AbLogixDriver uses, scripted by a FakeController."""

    def __init__(self, path: str, *, init_tags: bool = True, ctl: FakeController) -> None:
        ctl.record("__init__")
        self.ctl = ctl
        self.path = path
        self.init_tags = init_tags
        self._cfg = {"socket_timeout": 5.0, "connection_size": 4000, "extended forward open": True,
                     "use_instance_ids": True}
        self._tags: dict = {}
        self._data_types: dict = {}
        self.info: dict = {}
        self.oo_batching: bool | None = None
        self.oo_multi_sent = 0
        self.opened_with: dict | None = None
        self.forward_open = False
        self.adopted = False
        self.closed = 0
        self.aborted = 0
        self._sock = None
        ctl.plcs.append(self)

    @property
    def oo_micro800(self) -> bool:
        return self.ctl.micro800

    @property
    def tags(self) -> dict:
        return self._tags

    @property
    def data_types(self) -> dict:
        return self._data_types

    @property
    def connection_size(self) -> int:
        return 500 if self.ctl.micro800 else self._cfg["connection_size"]

    def open(self) -> bool:
        self.ctl.record("open")
        self.opened_with = dict(self._cfg)
        if isinstance(self.ctl.open_result, BaseException):
            raise self.ctl.open_result
        self.info = dict(self.ctl.info)
        return self.ctl.open_result

    def get_tag_list(self, program: str | None = None) -> list:
        self.ctl.record("get_tag_list")
        assert program == "*"
        if self.ctl.upload_gate is not None:
            self.ctl.upload_gate.wait(5)
        if self.ctl.upload_exc is not None:
            raise self.ctl.upload_exc
        self.ctl.uploads += 1
        self._tags = dict(self.ctl.tags)
        self._data_types = {"PUMP_T": {}}
        return list(self._tags.values())

    def oo_adopt_tags(self, tags: dict, data_types: dict) -> None:
        self.ctl.record("oo_adopt_tags")
        self.adopted = True
        self._tags, self._data_types = tags, data_types

    def oo_symbolic(self) -> None:
        self.ctl.record("oo_symbolic")
        self._cfg["use_instance_ids"] = False

    def oo_forward_open(self) -> None:
        self.ctl.record("oo_forward_open")
        self.forward_open = True

    def oo_read(self, names: list[str]) -> list[NameResult]:
        ctl = self.ctl
        ctl.record("oo_read")
        ctl.reads.append((tuple(names), self.oo_batching))
        self.oo_multi_sent = int(bool(self.oo_batching and len(names) > 1 and ctl.hook_effective))
        if ctl.read_raises:
            raise ctl.read_raises.pop(0)
        if self.oo_batching and len(names) > 1 and ctl.msp != "ok":
            if ctl.msp == "timeout":
                raise comm_error(TimeoutError("timed out"))
            if ctl.msp == "error":
                raise ResponseError("weird")
            if ctl.msp == "malformed":
                raise WholeReplyError("unreadable Multiple Service Packet reply", status=0, multi=True)
            raise WholeReplyError(f"Multiple Service Packet rejected: {ctl.msp:#04x}", status=ctl.msp, multi=True)
        out = []
        for n in names:
            if n in ctl.errors:
                out.append(ctl.errors[n])
            elif n in ctl.values:
                out.append(NameResult(ctl.values[n], None, True, 0))
            else:
                out.append(NameResult(None, f"Tag doesn't exist - {n}", False))
        return out

    def close(self) -> None:
        self.ctl.record("close")
        self.closed += 1
        if self.ctl.close_raises is not None:
            raise self.ctl.close_raises

    def oo_abort(self) -> None:
        self.ctl.record("oo_abort")
        self.aborted += 1


M800_TAGS = {
    "FT_01": atomic("REAL"), "LT_01": atomic("REAL"), "RUN": atomic("BOOL"), "STATUS": atomic("DINT"),
    "Pump": struct_tag("PUMP_T", {"Amps": {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"}}),
}
M800_VALUES = {"FT_01": 12.5, "LT_01": 3.25, "RUN": True, "STATUS": 0b1010, "Pump.Amps": 7.0}


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=EXEC_PREFIX)
    yield ex
    ex.shutdown(wait=True)


@pytest.fixture
def make_driver(monkeypatch: pytest.MonkeyPatch, executor):
    def make(ctl: FakeController, tags: list[Tag], **device_kw: Any) -> AbLogixDriver:
        monkeypatch.setattr(ab_logix, "logix_driver_class", lambda: functools.partial(FakePlc, ctl=ctl))
        device_kw.setdefault("micro800", ctl.micro800)
        return AbLogixDriver(make_device(tags, **device_kw), executor)
    return make


def m800(**kw: Any) -> FakeController:
    return FakeController(dict(M800_TAGS), dict(M800_VALUES), **kw)


STD_TAGS = [T("FT_01", "float32"), T("LT_01", "float32"), T("RUN", "bool"), T("STATUS.1", "bool", name="s_bit1"),
            T("STATUS.3", "bool", name="s_bit3"), T("STATUS", "int32"), T("Pump.Amps", "float32")]


async def test_connect_read_good_values(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    assert not drv.connected
    await drv.connect()
    assert drv.connected
    plc = ctl.plcs[-1]
    assert plc.path == "192.0.2.14:44818" and plc.init_tags is False
    assert plc.forward_open and plc._cfg["use_instance_ids"] is False
    out = await drv.read(single_block(drv))
    assert {k: (v.value, v.quality) for k, v in out.items()} == {
        "t_FT_01": (12.5, "good"), "t_LT_01": (3.25, "good"), "t_RUN": (True, "good"),
        "s_bit1": (True, "good"), "s_bit3": (True, "good"), "t_STATUS": (10, "good"),
        "t_Pump.Amps": (7.0, "good"),
    }
    assert all(v.source_ts is not None and v.source_ts.tzinfo is not None for v in out.values())
    names, batching = ctl.reads[-1]
    assert sorted(names) == ["FT_01", "LT_01", "Pump.Amps", "RUN", "STATUS"]   # STATUS read once for 3 tags
    assert batching is True
    await drv.close()
    assert not drv.connected and plc.closed == 1


async def test_timeout_and_connection_size_applied_before_open(make_driver) -> None:
    ctl = FakeController({"A": atomic()}, {"A": 1}, micro800=False)
    drv = make_driver(ctl, [T("A")], timeout_ms=2500, path="1,0", slot=0, connection_size=500)
    await drv.connect()
    plc = ctl.plcs[-1]
    assert plc.path == "192.0.2.14:44818/0"
    assert plc.opened_with["socket_timeout"] == 2.5
    assert plc.opened_with["connection_size"] == 500
    assert plc.opened_with["extended forward open"] is False       # 500 fits a standard Forward Open

    ctl2 = FakeController({"A": atomic()}, {"A": 1}, micro800=False)
    drv2 = make_driver(ctl2, [T("A")], connection_size=4000)
    await drv2.connect()
    assert ctl2.plcs[-1].opened_with["extended forward open"] is True

    ctl3 = m800()
    drv3 = make_driver(ctl3, [T("FT_01")])
    await drv3.connect()
    assert ctl3.plcs[-1].opened_with["connection_size"] == 4000    # untouched: pycomm3 negotiates


async def test_missing_tags_are_bad_config_and_not_sent(make_driver) -> None:
    ctl = m800()
    tags = [T("FT_01", "float32"), T("NO_SUCH", "float32"), T("Pump.Nope", "float32"), T("FT_01.X", "int32")]
    drv = make_driver(ctl, tags)
    await drv.connect()
    block = single_block(drv)
    with structlog.testing.capture_logs() as logs:
        out = await drv.read(block)
        out2 = await drv.read(block)
    assert out["t_FT_01"].quality == "good"
    for name in ("t_NO_SUCH", "t_Pump.Nope", "t_FT_01.X"):
        assert out[name].quality == "bad_config" and out[name].value is None
        assert out2[name].quality == "bad_config"
    sent = {n for names, _ in ctl.reads for n in names}
    assert "NO_SUCH" not in sent and "Pump.Nope" not in sent and "FT_01.X" not in sent
    warnings = [e for e in logs if e["event"] == "ab_logix.tags_missing"]
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
    assert sorted(warnings[0]["names"]) == ["FT_01.X", "NO_SUCH", "Pump.Nope"]


async def test_all_missing_block_does_not_touch_the_wire(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, [T("GONE_1"), T("GONE_2")])
    await drv.connect()
    reads_before = len(ctl.reads)
    out = await drv.read(single_block(drv))
    assert {v.quality for v in out.values()} == {"bad_config"}
    assert len(ctl.reads) == reads_before


async def test_case_insensitive_match_reads_controller_spelling(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, [T("ft_01", "float32"), T("pump.AMPS", "float32")])
    await drv.connect()
    with structlog.testing.capture_logs() as logs:
        out = await drv.read(single_block(drv))
    assert out["t_ft_01"].value == 12.5 and out["t_pump.AMPS"].value == 7.0
    assert sorted(ctl.reads[-1][0]) == ["FT_01", "Pump.Amps"]
    assert any(e["event"] == "ab_logix.tags_recased" for e in logs)


# --- batching probe ---

async def test_probe_success_turns_batching_on(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    with structlog.testing.capture_logs() as logs:
        await drv.connect()
    probe, batching = ctl.reads[0]
    assert probe == ("FT_01", "LT_01") and batching is True       # 2 existing scalar tags, batched
    ident = await drv.identity()
    assert ident["batching"] is True and "probe ok" in ident["batching_reason"]
    info = [e for e in logs if e["event"] == "ab_logix.batching"]
    assert len(info) == 1 and info[0]["log_level"] == "info" and info[0]["batching"] is True
    await drv.read(single_block(drv))
    assert ctl.reads[-1][1] is True


@pytest.mark.parametrize(("msp", "reason"), [
    (0x08, "probe rejected"), ("malformed", "probe rejected"), ("error", "probe reply not usable"),
])
async def test_probe_failure_turns_batching_off(make_driver, msp: Any, reason: str) -> None:
    ctl = m800(msp=msp)
    drv = make_driver(ctl, STD_TAGS)
    with structlog.testing.capture_logs() as logs:
        await drv.connect()
    assert drv.connected
    ident = await drv.identity()
    assert ident["batching"] is False and reason in ident["batching_reason"]
    assert [e["batching"] for e in logs if e["event"] == "ab_logix.batching"] == [False]
    out = await drv.read(single_block(drv))
    assert ctl.reads[-1][1] is False                               # pycomm3's one-request-per-tag path
    assert {v.quality for v in out.values()} == {"good"}
    assert out["t_FT_01"].value == 12.5


async def test_probe_data_error_turns_batching_off(make_driver) -> None:
    """pycomm3 raises BufferEmptyError (a DataError) for an MSP refused with 0x08 and no body;
    the subclass hook converts it, but a raw DataError reaching the probe must also mean off."""
    from pycomm3.exceptions import BufferEmptyError
    ctl = m800()
    ctl.read_raises.append(BufferEmptyError())
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    ident = await drv.identity()
    assert drv.connected and ident["batching"] is False
    assert "probe reply not usable" in ident["batching_reason"]


async def test_probe_read_error_turns_batching_off(make_driver) -> None:
    ctl = m800()
    ctl.errors["FT_01"] = NameResult(None, "Failed to parse reply", True, 0)
    drv = make_driver(ctl, [T("LT_01", "float32")])
    await drv.connect()
    ident = await drv.identity()
    assert ident["batching"] is False and "probe read of 'FT_01' failed" in ident["batching_reason"]


async def test_probe_must_go_out_as_multiple_service_packet(make_driver) -> None:
    """If pycomm3 ever stops calling the hook, the probe reads singles: that is not a pass."""
    ctl = m800()
    ctl.hook_effective = False
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    ident = await drv.identity()
    assert ident["batching"] is False and "did not go out" in ident["batching_reason"]


def test_probe_counts_multiple_service_packets(plc_factory) -> None:
    reply = _cip_reply(0x0A, 0, _msp([_read_reply(0, DINT, struct.pack("<i", 1)),
                                      _read_reply(0, REAL, struct.pack("<f", 1.0))]))
    plc = plc_factory([reply, _cip_reply(0x4C, 0, struct.pack("<Hi", DINT, 1)),
                       _cip_reply(0x4C, 0, struct.pack("<Hf", REAL, 1.0))])
    plc.oo_batching = True
    plc.oo_read(["A", "B"])
    assert plc.oo_multi_sent == 1
    plc.oo_batching = False
    plc.oo_read(["A", "B"])
    assert plc.oo_multi_sent == 0


async def test_probe_without_candidate_tag(make_driver) -> None:
    ctl = FakeController({"Pump": M800_TAGS["Pump"]}, {"Pump.Amps": 1.0})
    drv = make_driver(ctl, [T("Pump.Amps", "float32")])
    await drv.connect()
    assert ctl.reads == []
    assert (await drv.identity())["batching"] is False


async def test_probe_is_redone_on_each_connect_and_logged_on_change(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    with structlog.testing.capture_logs() as logs:
        await drv.connect()
        await drv.connect()
        ctl.msp = 0x08
        await drv.connect()
    assert sum(1 for names, b in ctl.reads if b and len(names) == 2) == 3
    batching_logs = [e for e in logs if e["event"] == "ab_logix.batching"]
    assert [e["batching"] for e in batching_logs if e["log_level"] == "info"] == [True, False]
    assert [e["log_level"] for e in batching_logs] == ["info", "debug", "info"]   # repeat at DEBUG


@pytest.mark.parametrize(("mode", "expected"), [("on", True), ("off", False)])
async def test_batching_override_skips_probe(make_driver, mode: str, expected: bool) -> None:
    ctl = m800(msp=0x08)
    drv = make_driver(ctl, STD_TAGS, batching=mode)
    await drv.connect()
    assert ctl.reads == []                                          # no probe on the wire
    ident = await drv.identity()
    assert ident["batching"] is expected and "params.batching" in ident["batching_reason"]
    ctl.msp = "ok"                       # forced "on" would otherwise fail every read (no demotion)
    await drv.read(single_block(drv))
    assert ctl.reads[-1][1] is expected


@pytest.mark.parametrize(("mode", "expected"), [("auto", True), ("on", True), ("off", False)])
async def test_logix_never_probes(make_driver, mode: str, expected: bool) -> None:
    ctl = FakeController({"A": atomic(), "B": atomic()}, {"A": 1, "B": 2}, micro800=False)
    drv = make_driver(ctl, [T("A"), T("B")], batching=mode, path="1,0", slot=0)
    await drv.connect()
    assert ctl.reads == []
    assert (await drv.identity())["batching"] is expected
    await drv.read(single_block(drv))
    assert ctl.reads[-1][1] is expected


async def test_probe_without_answer_fails_connect_then_gives_up(make_driver) -> None:
    ctl = m800(msp="timeout")
    drv = make_driver(ctl, STD_TAGS)
    with structlog.testing.capture_logs() as logs:
        for _ in range(ab_logix.PROBE_GIVE_UP):
            with pytest.raises(ConnectionLost, match="timed out"):
                await drv.connect()
            assert not drv.connected
            assert ctl.plcs[-1].aborted == 1                         # dropped without Forward Close
        await drv.connect()                                         # no probe any more
    assert drv.connected
    probes = [r for r in ctl.reads if r[1]]
    assert len(probes) == ab_logix.PROBE_GIVE_UP
    ident = await drv.identity()
    assert ident["batching"] is False and "without probing" in ident["batching_reason"]
    assert any(e["event"] == "ab_logix.batching_probe_given_up" and e["log_level"] == "warning" for e in logs)


async def test_runtime_demotion_after_failed_batched_read(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    block = single_block(drv)
    ctl.read_raises.append(WholeReplyError("Multiple Service Packet rejected: 0x11", status=0x11, multi=True))
    with structlog.testing.capture_logs() as logs, pytest.raises(CommError) as info:
        await drv.read(block)
    assert not isinstance(info.value, ConnectionLost)
    assert drv.connected                                            # a framed reply: connection kept
    assert any(e["event"] == "ab_logix.batching_demoted" for e in logs)
    out = await drv.read(block)
    assert ctl.reads[-1][1] is False and out["t_FT_01"].quality == "good"
    await drv.connect()                                             # stays off, no new probe
    assert ctl.reads[-1][1] is False and len(ctl.reads[-1][0]) > 2
    assert (await drv.identity())["batching"] is False


async def test_forced_batching_is_not_demoted(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS, batching="on")
    await drv.connect()
    ctl.read_raises.append(WholeReplyError("rejected", status=0x08, multi=True))
    with pytest.raises(CommError):
        await drv.read(single_block(drv))
    await drv.read(single_block(drv))
    assert ctl.reads[-1][1] is True


# --- error mapping ---

async def test_per_tag_error_mapping(make_driver) -> None:
    ctl = m800()
    ctl.values.update({"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": "x"})
    ctl.tags.update({k: atomic() for k in "ABCDEF"})
    ctl.errors.update({
        "A": NameResult(None, "Destination unknown", True, 0x05),
        "B": NameResult(None, "General Error", True, 0xFF, 0x2105),
        "C": NameResult(None, "Permission denied", True, 0x0F),
        "D": NameResult(None, "Failed to parse reply", True, 0),
        "E": NameResult(None, "Tag doesn't exist - E", False),
    })
    drv = make_driver(ctl, [T("A"), T("B"), T("C"), T("D"), T("E"), T("F"), T("FT_01", "float32")])
    await drv.connect()
    out = await drv.read(single_block(drv))
    assert {k: v.quality for k, v in out.items()} == {
        "t_A": "bad_config", "t_B": "bad_config", "t_C": "bad_comm", "t_D": "bad_decode",
        "t_E": "bad_config", "t_F": "bad_decode",              # a string for an int32 tag
        "t_FT_01": "good",
    }


async def test_whole_reply_failure_is_comm_error(make_driver) -> None:
    ctl = FakeController({"A": atomic(), "B": atomic()}, {"A": 1, "B": 2}, micro800=False)
    drv = make_driver(ctl, [T("A"), T("B")])
    await drv.connect()
    ctl.read_raises.append(WholeReplyError("Multiple Service Packet rejected", status=0x02, multi=True))
    with pytest.raises(CommError) as info:
        await drv.read(single_block(drv))
    assert type(info.value) is CommError
    assert drv.connected and ctl.plcs[-1].aborted == 0


async def test_unexpected_pycomm3_exception_is_comm_error(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, [T("FT_01", "float32")])
    await drv.connect()
    ctl.read_raises.append(ResponseError("Target did not connected"))
    with pytest.raises(CommError, match="pycomm3 read failed"):
        await drv.read(single_block(drv))
    assert drv.connected


async def test_reply_timeout_closes_then_comm_error(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, [T("FT_01", "float32")], timeout_ms=700)
    await drv.connect()
    ctl.read_raises.append(comm_error(TimeoutError("timed out")))
    with pytest.raises(CommError) as info:
        await drv.read(single_block(drv))
    assert type(info.value) is CommError and "no reply within 700 ms" in str(info.value)
    assert not drv.connected
    assert ctl.plcs[-1].aborted == 1 and ctl.plcs[-1].closed == 0    # no Forward Close to a mute PLC
    with pytest.raises(ConnectionLost, match="not connected"):
        await drv.read(single_block(drv))
    await drv.close()                                                 # idempotent after the drop
    assert ctl.plcs[-1].closed == 0


@pytest.mark.parametrize("cause", [ConnectionResetError("reset"), BrokenPipeError("pipe"),
                                   ConnectionResetError("connection closed by the PLC")])
async def test_connection_errors_are_connection_lost(make_driver, cause: BaseException) -> None:
    ctl = m800()
    drv = make_driver(ctl, [T("FT_01", "float32")])
    await drv.connect()
    ctl.read_raises.append(comm_error(cause))
    with pytest.raises(ConnectionLost):
        await drv.read(single_block(drv))
    assert not drv.connected and ctl.plcs[-1].aborted == 1


async def test_encapsulation_error_is_connection_lost(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, [T("FT_01", "float32")])
    await drv.connect()
    ctl.read_raises.append(WholeReplyError("encapsulation status 0x0064", encapsulation=True))
    with pytest.raises(ConnectionLost):
        await drv.read(single_block(drv))
    assert not drv.connected


@pytest.mark.parametrize("failure", ["refused", "no_session", "upload_timeout", "upload_error"])
async def test_connect_failures_are_connection_lost(make_driver, failure: str) -> None:
    ctl = m800()
    if failure == "refused":
        exc = PycommCommError("failed to open a connection")
        exc.__cause__ = PycommCommError("Failed to open socket to 192.0.2.14:44818")
        ctl.open_result = exc
    elif failure == "no_session":
        ctl.open_result = False
    elif failure == "upload_timeout":
        exc = ResponseError("failed to get attribute list")
        exc.__cause__ = comm_error(TimeoutError("timed out"))
        ctl.upload_exc = exc
    else:
        ctl.upload_exc = ResponseError("failed to get attribute list")
    drv = make_driver(ctl, STD_TAGS)
    with pytest.raises(ConnectionLost, match="connect failed"):
        await drv.connect()
    assert not drv.connected
    plc = ctl.plcs[-1]
    assert plc.closed + plc.aborted == 1                              # never leaks the connection
    if failure == "upload_timeout":
        assert plc.aborted == 1                                       # no Forward Close to a mute PLC
    if failure == "upload_error":
        assert plc.closed == 1                                        # graceful close otherwise
    assert await drv.identity() == {}


async def test_close_is_idempotent_and_never_raises(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    await drv.close()                                                 # never connected
    await drv.connect()
    ctl.close_raises = PycommCommError("forward_close failed")
    await drv.close()
    await drv.close()
    assert not drv.connected
    assert ctl.plcs[-1].closed == 1 and ctl.plcs[-1].aborted == 1     # graceful failed -> socket dropped


async def test_close_without_executor_drops_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    ctl = m800()
    monkeypatch.setattr(ab_logix, "logix_driver_class", lambda: functools.partial(FakePlc, ctl=ctl))
    ex = ThreadPoolExecutor(max_workers=1)
    drv = AbLogixDriver(make_device(STD_TAGS), ex)
    await drv.connect()

    class Sock:
        closed = False

        def close(self) -> None:
            self.closed = True
    sock = ctl.plcs[-1]._sock = Sock()
    ex.shutdown(wait=True)
    await drv.close()                                                 # executor gone: no raise
    assert sock.closed and not drv.connected


async def test_connect_closes_previous_connection(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    first = ctl.plcs[-1]
    await drv.connect()
    assert first.closed == 1 and ctl.plcs[-1] is not first and drv.connected


async def test_identity_fields(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    assert await drv.identity() == {}
    await drv.connect()
    ident = await drv.identity()
    for key in ("vendor", "product_name", "product_code", "revision", "serial", "micro800", "batching"):
        assert key in ident
    assert ident["product_name"] == "2080-LC50-48QWB" and ident["revision"] == "12.11"
    assert ident["serial"] == "c0ffee01" and ident["micro800"] is True and ident["batching"] is True
    assert ident["connection_size"] == 500 and ident["tag_count"] == len(M800_TAGS)
    await drv.close()
    assert await drv.identity() == {}


async def test_kind_mismatch_is_warned_once(make_driver) -> None:
    ctl = m800()                                                      # identifies as a Micro800
    drv = make_driver(ctl, [T("FT_01", "float32")], micro800=False, path="1,0", slot=0)
    with structlog.testing.capture_logs() as logs:
        await drv.connect()
        await drv.connect()
    assert sum(1 for e in logs if e["event"] == "ab_logix.kind_mismatch") == 1
    assert (await drv.identity())["micro800"] is True                 # pycomm3 follows the detected kind


async def test_read_rejects_other_blocks_and_needs_connection(make_driver) -> None:
    from opencap.drivers.base import Block
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    with pytest.raises(ConnectionLost):
        await drv.read(single_block(drv))
    await drv.connect()
    with pytest.raises(TypeError):
        await drv.read(Block(tags=["x"]))


# --- tag list reuse ---

async def test_tag_list_reused_across_reconnects(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    await drv.close()
    await drv.connect()
    ctl.read_raises.append(comm_error(ConnectionResetError("reset")))
    with pytest.raises(ConnectionLost):
        await drv.read(single_block(drv))
    await drv.connect()
    assert ctl.uploads == 1
    assert [p.adopted for p in ctl.plcs] == [False, True, True]
    out = await drv.read(single_block(drv))
    assert out["t_Pump.Amps"].value == 7.0


async def test_tag_list_reuploaded_when_identity_changes(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    ctl.info["serial"] = "0badf00d"                                   # controller swapped
    ctl.tags["NEW"] = atomic()
    await drv.connect()
    assert ctl.uploads == 2 and not ctl.plcs[-1].adopted
    assert "NEW" in ctl.plcs[-1].tags


async def test_cancelled_connect_does_not_leak_and_keeps_upload(make_driver) -> None:
    """The worker's safety net cancels a slow connect; the executor job closes what it opened
    and leaves the uploaded tag list for the next attempt."""
    ctl = m800()
    ctl.upload_gate = threading.Event()
    drv = make_driver(ctl, STD_TAGS)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(drv.connect(), 0.05)
    first = ctl.plcs[-1]
    ctl.upload_gate.set()
    for _ in range(200):
        if first.closed:
            break
        await asyncio.sleep(0.01)
    assert first.closed == 1 and not drv.connected
    ctl.upload_gate = None
    await drv.connect()
    assert ctl.uploads == 1 and ctl.plcs[-1].adopted and drv.connected


# --- concurrency ---

async def test_every_pycomm3_call_runs_on_the_executor(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, STD_TAGS)
    await drv.connect()
    await drv.read(single_block(drv))
    ctl.read_raises.append(comm_error(TimeoutError("timed out")))
    with pytest.raises(CommError):
        await drv.read(single_block(drv))
    await drv.connect()
    await drv.identity()
    await drv.close()
    seen = {what for what, _ in ctl.threads}
    assert {"__init__", "open", "get_tag_list", "oo_adopt_tags", "oo_read", "close", "oo_abort",
            "oo_symbolic", "oo_forward_open"} <= seen
    assert all(thread.startswith(EXEC_PREFIX) for _, thread in ctl.threads), ctl.threads
    assert threading.current_thread().name not in {t for _, t in ctl.threads}


async def test_slow_read_does_not_block_the_event_loop(make_driver) -> None:
    ctl = m800()
    drv = make_driver(ctl, [T("FT_01", "float32")])
    await drv.connect()
    real = FakePlc.oo_read

    def slow(self, names):
        time.sleep(0.2)
        return real(self, names)
    ctl.plcs[-1].oo_read = functools.partial(slow, ctl.plcs[-1])
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1
    task = asyncio.create_task(ticker())
    await drv.read(single_block(drv))
    task.cancel()
    assert ticks >= 5


# --- read-only ---

def test_driver_source_never_writes() -> None:
    """Fails if ab_logix.py calls a pycomm3 write path or generic_message at all."""
    src = Path(ab_logix.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden_calls = {"write", "set_plc_time", "_write_build_requests", "_write_build_multi_requests",
                       "_write_build_single_request", "_send_write_fragmented", "generic_message"}
    forbidden_names = {"WriteTagRequestPacket", "WriteTagFragmentedRequestPacket", "ReadModifyWriteRequestPacket",
                       "encode_value"}
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
    assert AbLogixDriver.write is ab_logix.Driver.write                  # base: raises NotImplementedError
