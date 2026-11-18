"""s7 driver I/O hooks on python-snap7 3.x, without a socket.

Pins the snap7 internals the `OcS7Client` subclass relies on (so a snap7 upgrade that moves
them fails here, not on site), the read-only guard on crafted and real snap7 PDUs, and that every
snap7 API that writes is stopped before anything reaches the connection.
"""

from __future__ import annotations

import inspect
from datetime import datetime

import pytest

snap7 = pytest.importorskip("snap7")

from snap7.client import Client  # noqa: E402
from snap7.connection import ISOTCPConnection  # noqa: E402
from snap7.datatypes import S7Area, S7WordLen  # noqa: E402
from snap7.s7protocol import S7Protocol  # noqa: E402
from snap7.type import Area, Block  # noqa: E402

from opencap.drivers.s7 import (  # noqa: E402
    CONFIG_HEADER_ERRORS,
    CONFIG_RETURN_CODES,
    S7ReadOnlyViolation,
    check_read_only,
    return_code_text,
    s7_client_class,
)


def params(fn) -> list[str]:
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def test_snap7_internals_the_subclass_relies_on() -> None:
    assert snap7.__version__.split(".")[0] == "3"
    assert params(Client._connect) == ["address", "rack", "slot", "tcp_port"]
    assert params(Client._send_data) == ["conn", "request"]
    assert params(Client._send_receive)[:1] == ["request"]
    assert params(Client._setup_communication) == []
    assert params(ISOTCPConnection.connect) == ["timeout"]
    assert params(ISOTCPConnection.__init__)[:4] == ["host", "port", "local_tsap", "remote_tsap"]
    assert params(S7Protocol.build_read_request) == ["area", "db_number", "start", "word_len", "count"]
    assert params(S7Protocol.extract_read_data) == ["response", "word_len", "count"]
    # Client.connect builds the remote TSAP from the connection type and calls _connect.
    src = inspect.getsource(Client.connect)
    assert "self.connection_type << 8" in src and "self._connect(" in src
    # every request goes through _send_data
    assert "self._send_data(conn, request)" in inspect.getsource(Client._send_receive)


def test_client_subclass_settings() -> None:
    cls = s7_client_class()
    assert issubclass(cls, Client)
    c = cls(0.75, 960, 3)
    assert (c.oc_timeout_s, c.pdu_length, c.connection_type) == (0.75, 960, 3)
    assert c.use_optimizer is False and c._auto_reconnect is False and c._heartbeat_interval == 0


def proto() -> S7Protocol:
    return S7Protocol()


ALLOWED = {
    "setup communication": lambda: proto().build_setup_communication_request(1, 1, 480),
    "read var": lambda: proto().build_read_request(S7Area.DB, 1, 0, S7WordLen.BYTE, 4),
    "read timers": lambda: proto().build_read_request(S7Area.TM, 0, 0, S7WordLen.TIMER, 2),
    "SZL read": lambda: proto().build_read_szl_request(0x001C, 0),
    "SZL follow-up": lambda: proto().build_userdata_followup_request(4, 1, 7),
}
REFUSED = {
    "write var": (lambda: proto().build_write_request(S7Area.DB, 1, 0, S7WordLen.BYTE, b"\x01"), "write var"),
    "PLC stop": (lambda: proto().build_plc_control_request("stop"), "PLC stop"),
    "hot start": (lambda: proto().build_plc_control_request("hot_start"), "PLC control"),
    "cold start": (lambda: proto().build_plc_control_request("cold_start"), "PLC control"),
    "set clock": (lambda: proto().build_set_clock_request(datetime(2026, 1, 1)), "USERDATA group 7"),
    "get clock": (lambda: proto().build_get_clock_request(), "USERDATA group 7"),
    "list blocks": (lambda: proto().build_list_blocks_request(), "USERDATA group 3"),
    "start upload": (lambda: proto().build_start_upload_request(0x41, 1), "start upload"),
    "download": (lambda: proto().build_request_download_request(0x41, 1, b"\x00" * 40)
                 if hasattr(S7Protocol, "build_request_download_request")
                 else proto().build_download_request(0x41, 1, bytes(36 + 40)), "download"),
    "not S7": (lambda: b"\x00" * 20, "not an S7 request PDU"),
    "too short": (lambda: b"\x32\x01", "not an S7 request PDU"),
    "ack pdu": (lambda: b"\x32\x03" + bytes(12), "PDU type 0x03"),
}


@pytest.mark.parametrize("name", list(ALLOWED))
def test_read_only_allows(name: str) -> None:
    assert check_read_only(ALLOWED[name]()) is None


@pytest.mark.parametrize("name", list(REFUSED))
def test_read_only_refuses(name: str) -> None:
    build, reason = REFUSED[name]
    problem = check_read_only(build())
    assert problem is not None and reason in problem


class _FakeConnection:
    """Stands in for ISOTCPConnection: records anything that would reach the socket."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.connected = True

    def send_data(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        raise ConnectionError("fake connection: nothing is answered")

    def receive_data(self) -> bytes:   # pragma: no cover
        raise ConnectionError("fake connection")

    def check_connection(self) -> bool:
        return True

    def disconnect(self) -> None:
        self.connected = False


WRITERS = {
    "db_write": lambda c: c.db_write(1, 0, bytearray(2)),
    "write_area": lambda c: c.write_area(Area.MK, 0, 0, bytearray(2)),
    "mb_write": lambda c: c.mb_write(0, 2, bytearray(2)),
    "eb_write": lambda c: c.eb_write(0, 2, bytearray(2)),
    "ab_write": lambda c: c.ab_write(0, bytearray(2)),
    "tm_write": lambda c: c.tm_write(0, 1, bytearray(2)),
    "ct_write": lambda c: c.ct_write(0, 1, bytearray(2)),
    "db_write_real": lambda c: c.db_write_real(1, 0, 1.5),
    "write_multi_vars": lambda c: c.write_multi_vars(
        [{"area": Area.DB, "db_number": 1, "start": 0, "data": bytearray(2)}]),
    "plc_stop": lambda c: c.plc_stop(),
    "plc_hot_start": lambda c: c.plc_hot_start(),
    "plc_cold_start": lambda c: c.plc_cold_start(),
    "set_plc_datetime": lambda c: c.set_plc_datetime(datetime(2026, 1, 1)),
    "set_plc_system_datetime": lambda c: c.set_plc_system_datetime(),
    "compress": lambda c: c.compress(1000),
    "copy_ram_to_rom": lambda c: c.copy_ram_to_rom(1000),
    "set_session_password": lambda c: c.set_session_password("secret"),
    "delete": lambda c: c.delete(Block.DB, 1),
    "download": lambda c: c.download(bytearray(200), 1),
    "db_fill": lambda c: c.db_fill(1, 0, size=4),
}


@pytest.mark.parametrize("name", list(WRITERS))
def test_snap7_write_apis_never_reach_the_socket(name: str) -> None:
    c = s7_client_class()(1.0, 480, 3)
    fake = _FakeConnection()
    c.connection, c.connected = fake, True
    with pytest.raises(Exception) as info:
        WRITERS[name](c)
    assert fake.sent == [], f"{name} sent {fake.sent[0][:12].hex()}"
    assert isinstance(info.value, S7ReadOnlyViolation), info.value
    assert c.oc_refused == 1 and c.oc_sent == 0
    c.connection = None


def test_error_tables() -> None:
    assert CONFIG_RETURN_CODES == {0x03, 0x05, 0x06, 0x07, 0x0A}
    assert 0x01 not in CONFIG_RETURN_CODES and 0xFF not in CONFIG_RETURN_CODES
    assert set(CONFIG_HEADER_ERRORS) == {0x8104}
    assert return_code_text(0x0A) == "item return code 0x0A (object does not exist)"
    assert return_code_text(0x42) == "item return code 0x42 (unknown)"
