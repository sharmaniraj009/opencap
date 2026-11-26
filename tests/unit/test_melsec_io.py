"""melsec_mc I/O hooks, pinned with crafted replies (no simulator).

The pymcprotocol subclass (`mc_client_class()`) is driven over a socketpair: framed replies,
peer close, bad frames, integer end codes, the reply-length check and the read-only guard.
The driver's error mapping (`_call`) is checked with a fake client.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("pymcprotocol")

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.drivers.base import ConnectionLost  # noqa: E402
from opencap.drivers.melsec_mc import (  # noqa: E402
    CONFIG_END_CODES,
    McEndCode,
    McFrameError,
    McReadOnlyError,
    MelsecMcDriver,
    _ReadFailure,
    _Session,
    check_read_only,
    check_reply_frame,
    end_code_text,
    mc_client_class,
    monitoring_timer,
    request_command,
)

ROUTE = b"\x00\xff\xff\x03\x00"


def reply(data: bytes = b"", end: int = 0) -> bytes:
    body = end.to_bytes(2, "little") + data
    return b"\xd0\x00" + ROUTE + len(body).to_bytes(2, "little") + body


def words(*values: int) -> bytes:
    return b"".join(v.to_bytes(2, "little") for v in values)


@pytest.fixture
def pair():
    """(client, peer): an OoType3E wired to one end of a socketpair."""
    a, b = socket.socketpair()
    a.settimeout(2)
    b.settimeout(2)
    client = mc_client_class()("Q")
    client._sock, client._is_connected = a, True
    yield client, b
    a.close()
    b.close()


def test_one_frame_per_recv_even_when_two_arrive_together(pair) -> None:
    client, peer = pair
    peer.sendall(reply(words(1, 2)) + reply(words(3)))
    assert client.batchread_wordunits("D0", 2) == [1, 2]
    assert client.batchread_wordunits("D0", 1) == [3]                   # the second frame is not lost


def test_frame_split_into_pieces_is_reassembled(pair) -> None:
    client, peer = pair
    frame = reply(words(*range(1, 201)))

    def trickle() -> None:
        for i in range(0, len(frame), 97):
            peer.sendall(frame[i:i + 97])
            time.sleep(0.01)

    t = threading.Thread(target=trickle)
    t.start()
    assert client.oo_read_words("D0", 200) == list(range(1, 201))
    t.join()


def test_peer_close_mid_frame_is_a_connection_error(pair) -> None:
    client, peer = pair
    peer.sendall(reply(words(1, 2))[:12])
    peer.shutdown(socket.SHUT_WR)
    with pytest.raises(ConnectionResetError, match="closed by the PLC"):
        client.batchread_wordunits("D0", 2)


def test_bad_subheader_is_a_frame_error(pair) -> None:
    client, peer = pair
    peer.sendall(b"\xd4\x00" + reply(words(1))[2:])
    with pytest.raises(McFrameError, match="subheader d400"):
        client.batchread_wordunits("D0", 1)


def test_end_code_is_raised_with_the_integer(pair) -> None:
    client, peer = pair
    peer.sendall(reply(ROUTE + b"\x01\x04\x00\x00", end=0xC059))
    with pytest.raises(McEndCode) as info:
        client.batchread_wordunits("D0", 1)
    assert info.value.code == 0xC059                                   # stock pymcprotocol drops the code
    assert "0xC059" in str(info.value) and "not supported" in str(info.value)


def test_reply_must_carry_exactly_the_requested_words(pair) -> None:
    client, peer = pair
    peer.sendall(reply(words(1, 2)))                                    # 2 words for a 3-word request
    with pytest.raises(McFrameError, match="4 data bytes for 3 words"):
        client.oo_read_words("D0", 3)


def test_request_frame_is_q_format_with_our_route_and_timer(pair) -> None:
    client, peer = pair
    client.oo_configure(protocol="tcp", timeout_s=1, timer=8, network=1, pc=2, dest_moduleio=0x3E0, dest_modulesta=3)
    peer.sendall(reply(words(5)))
    client.oo_read_words("ZR010", 1)
    sent = peer.recv(100)
    assert sent[:2] == b"\x50\x00" and sent[2:7] == b"\x01\x02\xe0\x03\x03"
    assert sent[9:11] == (8).to_bytes(2, "little")                      # monitoring timer
    assert request_command(sent) == (0x0401, 0x0000)
    assert sent[15:19] == b"\x10\x00\x00\xb0"                           # ZR 0x10, device code 0xB0
    assert sent[19:21] == b"\x01\x00"


def test_iqr_frames_use_subcommand_2_and_4_byte_numbers() -> None:
    a, b = socket.socketpair()
    try:
        client = mc_client_class()("iQ-R")
        client._sock, client._is_connected = a, True
        b.sendall(reply(words(5)))
        client.oo_read_words("D2", 1)
        sent = b.recv(100)
        assert request_command(sent) == (0x0401, 0x0002)
        assert sent[15:21] == b"\x02\x00\x00\x00\xa8\x00"
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("call", [
    lambda c: c.batchwrite_wordunits("D0", [1]),
    lambda c: c.batchwrite_bitunits("M0", [1]),
    lambda c: c.randomwrite(["D0"], [1], [], []),
    lambda c: c.randomwrite_bitunits(["M0"], [1]),
    lambda c: c.remote_run(clear_mode=0),
    lambda c: c.remote_stop(),
    lambda c: c.remote_reset(),
    lambda c: c.remote_latchclear(),
    lambda c: c.remote_unlock("1234"),
    lambda c: c.batchread_bitunits("M0", 1),                           # a read, but not one we use
    lambda c: c.randomread(["D0"], []),
    lambda c: c.echo_test("A"),
])
def test_read_only_guard_refuses_before_sending(pair, call) -> None:
    client, peer = pair
    with pytest.raises(McReadOnlyError, match="read-only"):
        call(client)
    peer.setblocking(False)
    with pytest.raises(BlockingIOError):
        peer.recv(1)                                                    # nothing reached the wire


def test_read_only_guard_on_raw_frames() -> None:
    c = mc_client_class()("Q")
    ok = c._make_senddata(c._make_commanddata(0x0401, 0x0000) + c._make_devicedata("D0") + c._encode_value(1))
    assert check_read_only(ok) is None
    assert check_read_only(c._make_senddata(c._make_commanddata(0x0101, 0x0000))) is None
    assert "not a read" in check_read_only(c._make_senddata(c._make_commanddata(0x1401, 0x0000)))
    assert "binary 3E" in check_read_only(b"500000FF03FF00")
    assert check_read_only(b"\x50\x00") is not None


def test_reply_frame_check() -> None:
    assert check_reply_frame(reply(words(1))) == 4
    for bad in (b"", b"\xd0\x00" + ROUTE + b"\x02", reply(words(1))[:-1], reply(words(1)) + b"\x00",
                b"\xd4\x00" + reply()[2:]):
        with pytest.raises(McFrameError):
            check_reply_frame(bad)


def test_small_helpers() -> None:
    assert [monitoring_timer(ms) for ms in (1, 250, 251, 3000, 10 ** 9)] == [1, 1, 2, 12, 0xFFFF]
    assert end_code_text(0xC056) == "MC end code 0xC056 (read/write request exceeds the maximum address)"
    assert end_code_text(0x1234) == "MC end code 0x1234 (unknown error)"
    assert {0xC056, 0xC059, 0x4031} <= CONFIG_END_CODES and 0xC061 not in CONFIG_END_CODES


# --- driver error mapping (fake client) ----------------------------------------------------------------

class FakeClient:
    def __init__(self, exc: BaseException | None = None, *, sent: bool = True) -> None:
        self.exc, self.oo_sent, self.closed = exc, sent, False

    def oo_read_words(self, head: str, count: int) -> list[int]:
        if self.exc is not None:
            raise self.exc
        return [0] * count

    def close(self) -> None:
        self.closed = True


def _driver() -> MelsecMcDriver:
    dev = Device(id="S/P", channel="S", name="P", driver="melsec_mc", host="127.0.0.1", port=5001,
                 timeout_ms=300, tags=[Tag(name="t", address="D0", dtype="uint16")])
    return MelsecMcDriver(dev)


@pytest.mark.parametrize(("exc", "kind", "closed"), [
    (McEndCode(0xC056), "config", False),
    (McEndCode(0x4031), "config", False),
    (McEndCode(0xC059), "config", False),
    (McEndCode(0xC061), "comm", False),
    (McEndCode(0x0055), "comm", False),
    (socket.timeout("timed out"), "comm", True),
    (McFrameError("short"), "comm", True),
    (ConnectionResetError("closed by the PLC"), "lost", True),
    (ConnectionRefusedError(111, "refused"), "lost", True),
    (BrokenPipeError(32, "broken"), "lost", True),
])
def test_call_maps_failures(exc: BaseException, kind: str, closed: bool) -> None:
    drv = _driver()
    client = FakeClient(exc)
    session = _Session(client)
    with pytest.raises(_ReadFailure) as info:
        drv._call(session, client.oo_read_words, "D0", 1)
    assert info.value.kind == kind
    assert client.closed is closed and session.dead is closed
    if isinstance(exc, socket.timeout):
        assert "no reply within 300 ms; socket closed" in str(info.value)


def test_call_library_error_before_sending_is_config() -> None:
    drv = _driver()
    client = FakeClient(ValueError("Exceeeded Device value range"), sent=False)
    with pytest.raises(_ReadFailure) as info:
        drv._call(_Session(client), client.oo_read_words, "D0", 1)
    assert info.value.kind == "config" and not client.closed
    client = FakeClient(ValueError("decode"), sent=True)
    with pytest.raises(_ReadFailure) as info:
        drv._call(_Session(client), client.oo_read_words, "D0", 1)
    assert info.value.kind == "comm" and client.closed


async def test_not_connected_and_identity_without_a_session() -> None:
    drv = _driver()
    assert not drv.connected and await drv.identity() == {}
    plan = drv.plan(drv.device.tags)
    with pytest.raises(ConnectionLost, match="not connected"):
        await drv.read(plan.blocks[0])
    with pytest.raises(TypeError):
        await drv.read(object())                                        # type: ignore[arg-type]
    await drv.close()                                                   # idempotent without a session


async def test_connect_refused_is_connection_lost() -> None:
    from tests.helpers.modbus_sim import free_port

    # Windows takes 1-2 s to report a refused connect (it retries the SYN), longer than 500 ms
    dev = Device(id="S/P", channel="S", name="P", driver="melsec_mc", host="127.0.0.1", port=free_port(),
                 timeout_ms=5000 if sys.platform == "win32" else 500, tags=[])
    with ThreadPoolExecutor(max_workers=1) as ex:
        drv = MelsecMcDriver(dev, ex)
        with pytest.raises(ConnectionLost, match="connect failed: .*refused"):
            await drv.connect()
        assert not drv.connected
