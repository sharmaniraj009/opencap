"""#68 load-time checks for serial lines: one setting per shared port or gateway, and DF1 ACK/ENQ
timing that can actually reach its ENQ retries."""

from __future__ import annotations

import pytest

from opencap.config.loader import SERIAL_DRIVERS, load_config
from opencap.config.models import Channel, Device, DevicesFile, Tag
from opencap.drivers._serial import check_shared_lines
from opencap.drivers.ab_df1 import Df1Params, timing_warning


def rtu(name: str, *, host: str = "192.0.2.50", port: int = 4001, enabled: bool = True, **params) -> Device:
    return Device(id=f"Line/{name}", channel="Line", name=name, driver="modbus_rtu", host=host, port=port,
                  enabled=enabled, params={"transport": "tcp", "unit_id": 1, **params},
                  tags=[Tag(name="T", address="400001", dtype="uint16")])


def df1(name: str, *, timeout_ms: int = 1000, **params) -> Device:
    return Device(id=f"Line/{name}", channel="Line", name=name, driver="ab_df1", host="192.0.2.50", port=4001,
                  timeout_ms=timeout_ms, params={"transport": "tcp", **params},
                  tags=[Tag(name="N", address="N7:0", dtype="int16")])


def test_same_settings_on_one_line_are_fine():
    check_shared_lines([rtu("A", baudrate=9600), rtu("B", baudrate=9600, unit_id=2)], SERIAL_DRIVERS)
    check_shared_lines([rtu("A", baudrate=9600), rtu("B", baudrate=19200, port=4002)], SERIAL_DRIVERS)


@pytest.mark.parametrize("a,b,what", [
    ({"baudrate": 9600}, {"baudrate": 19200}, "line settings"),
    ({"parity": "E"}, {"parity": "N"}, "line settings"),
    ({"bytesize": 7}, {"bytesize": 8}, "line settings"),
    ({"stopbits": 2}, {"stopbits": 1}, "line settings"),
])
def test_different_line_settings_on_one_line_are_a_config_error(a, b, what):
    with pytest.raises(ValueError, match=f"Line/A and Line/B share the line tcp 192.0.2.50:4001 but differ in {what}"):
        check_shared_lines([rtu("A", **a), rtu("B", **b)], SERIAL_DRIVERS)


def test_df1_error_check_and_mixed_drivers_on_one_line():
    with pytest.raises(ValueError, match=r"differ in error_check \(crc vs bcc\)"):
        check_shared_lines([df1("A", error_check="crc", station=1), df1("B", error_check="bcc", station=2)],
                           SERIAL_DRIVERS)
    with pytest.raises(ValueError, match=r"differ in driver \(modbus_rtu vs ab_df1\)"):
        check_shared_lines([rtu("A"), df1("B")], SERIAL_DRIVERS)


def test_local_ports_compare_by_path_and_gateways_without_case():
    with pytest.raises(ValueError, match="share the line serial /dev/ttyUSB0"):
        check_shared_lines([rtu("A", host="/dev/ttyUSB0", port=0, transport="serial", baudrate=9600),
                            rtu("B", host="/dev/ttyUSB0", port=0, transport="serial", baudrate=19200)], SERIAL_DRIVERS)
    with pytest.raises(ValueError, match="differ in line settings"):
        check_shared_lines([rtu("A", host="GW.example", baudrate=9600), rtu("B", host="gw.example", baudrate=1200)],
                           SERIAL_DRIVERS)


def test_the_loader_refuses_a_mismatch_but_ignores_devices_that_do_not_run(tmp_path):
    def write(devices: list[Device]) -> None:
        df = DevicesFile(channels=[Channel(name="Line", devices=devices)])
        (tmp_path / "devices.json").write_text(df.model_dump_json(), encoding="utf-8")

    write([rtu("A", baudrate=9600), rtu("B", baudrate=19200)])
    with pytest.raises(ValueError, match="one line must use the same line settings"):
        load_config(tmp_path)
    write([rtu("A", baudrate=9600), rtu("B", baudrate=19200, enabled=False)])
    load_config(tmp_path)                                         # a disabled device does not use the line


def test_a_project_v2_gets_the_same_line_checks(tmp_path):
    from opencap.config.models import ProjectV2

    def write(devices: list[Device]) -> None:
        project = ProjectV2(channels=[Channel(name="Line", devices=devices)])
        (tmp_path / "project.json").write_text(project.model_dump_json(), encoding="utf-8")

    write([rtu("A", baudrate=9600), rtu("B", baudrate=19200)])
    with pytest.raises(ValueError, match="one line must use the same line settings"):
        load_config(tmp_path)
    write([df1("A", timeout_ms=1000, ack_timeout_ms=900)])
    assert any("ack_timeout_ms" in w for w in load_config(tmp_path).warnings)


def test_df1_auto_ack_timeout_leaves_room_for_every_enq():
    p = Df1Params.from_mapping({})
    assert p.ack_timeout_ms is None
    assert p.effective_ack_timeout_ms(1000) == 250                # 1 ACK wait + 3 ENQ waits fit in 1 s
    assert p.effective_ack_timeout_ms(10_000) == 1000             # capped at the old default
    assert Df1Params.from_mapping({"enq_retries": 0}).effective_ack_timeout_ms(1000) == 1000
    assert Df1Params.from_mapping({"ack_timeout_ms": 400}).effective_ack_timeout_ms(1000) == 400


def test_df1_timing_warning_for_an_explicit_ack_timeout():
    assert timing_warning(df1("A")) is None                       # auto
    assert timing_warning(df1("A", ack_timeout_ms=250)) is None   # 4 x 250 = 1000: fits
    assert timing_warning(df1("A", ack_timeout_ms=1000, enq_retries=0)) is None
    w = timing_warning(df1("A", ack_timeout_ms=1000))
    assert w == ("Line/A: ack_timeout_ms=1000 with timeout_ms=1000: only 0 of enq_retries=3 ENQ(s) fit in one "
                 "attempt, so a lost frame fails early; use ack_timeout_ms <= 250 or leave it unset (auto)")
    assert "only 2 of" in timing_warning(df1("A", ack_timeout_ms=1000, timeout_ms=3000))


def test_the_loader_reports_the_timing_warning(tmp_path):
    df = DevicesFile(channels=[Channel(name="Line", devices=[df1("A", ack_timeout_ms=1000)])])
    (tmp_path / "devices.json").write_text(df.model_dump_json(), encoding="utf-8")
    assert any("only 0 of enq_retries=3" in w for w in load_config(tmp_path).warnings)
