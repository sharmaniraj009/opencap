"""KEPServer "Allen-Bradley DF1" devices are imported as `ab_df1` with their serial settings and
`transport: unmapped`: they are skipped (with a clear warning) until overrides.yaml maps the
COM port to a serial-over-TCP gateway or a local serial device (tests/unit/test_serial_config.py)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from opencap.config import kep_import as ki
from opencap.config.kep_import import import_kep
from opencap.drivers import get_driver_class

SAMPLE = Path(__file__).parents[1] / "fixtures" / "project_sample.json"
SERIAL = "servermain.CHANNEL_SERIAL_COMMUNICATIONS_"


def _sample_df1() -> tuple[dict, dict]:
    project = json.loads(SAMPLE.read_text(encoding="utf-8-sig"))
    ch = next(c for c in project["project"]["channels"] if c["common.ALLTYPES_NAME"] == "LC_DF1")
    return ch, ch["devices"][0]


def _map(ch: dict, dev: dict) -> ki._Mapped:
    return ki._map_df1(ch, dev, "LC_DF1/Device1")


def test_sample_df1_device_is_imported_unmapped():
    res = import_kep(SAMPLE)
    dev = next(d for c in res.devices_file.channels for d in c.devices if d.id == "LC_DF1/Device1")
    assert (dev.driver, dev.host, dev.port) == ("ab_df1", "COM112", 0)
    assert dev.params == {
        "station": 1, "src_station": 0, "error_check": "crc", "transport": "unmapped", "com_port": "COM112",
        "baudrate": 19200, "bytesize": 8, "parity": "N", "stopbits": 1,
        "max_elements": 100, "max_gap": 4, "request_size": 232,
    }
    assert [t.address for t in dev.tags] == ["F19:0", "F19:0", "F19:2"]
    assert (dev.timeout_ms, dev.retries) == (10000, 2)
    assert dev.source["kep_serial"] == {"COM_ID": 112}
    problems = [p for p in res.problems if p.startswith("LC_DF1/Device1")]
    assert any("COM112" in p and "serial_ports" in p and "skipped" in p for p in problems)
    assert any("error checking" in p and "CRC" in p for p in problems)     # not in this export: default, flagged
    assert any("baud rate" in p for p in problems)
    # plans like an ab_pccc device: the same parser and planner
    drv = get_driver_class("ab_df1")(dev)
    plan = drv.plan(dev.tags)
    assert not plan.invalid and [b.address for b in plan.blocks] == ["F19:0{3}"]


def test_serial_settings_and_error_check_are_kept():
    ch, dev = _sample_df1()
    ch = {**ch, SERIAL + "BAUD_RATE": 9600, SERIAL + "DATA_BITS": 7, SERIAL + "PARITY": 69,
          SERIAL + "STOP_BITS": 2, SERIAL + "FLOW_CONTROL": 0}
    dev = {**dev, "allen_bradley_df1.DEVICE_ERROR_CHECKING": "BCC", "servermain.DEVICE_ID_STRING": "3"}
    m = _map(ch, dev)
    assert (m.driver, m.host, m.port) == ("ab_df1", "COM112", 0)
    p = m.params
    assert (p["baudrate"], p["bytesize"], p["parity"], p["stopbits"]) == (9600, 7, "E", 2)
    assert (p["error_check"], p["station"], p["com_port"], p["transport"]) == ("bcc", 3, "COM112", "unmapped")
    assert m.source["kep_serial"]["BAUD_RATE"] == 9600
    assert m.source["kep_df1"] == {"allen_bradley_df1.DEVICE_ERROR_CHECKING": "BCC"}
    assert not any("error checking" in x or "baud rate" in x for x in m.problems)


@pytest.mark.parametrize(("raw", "parity"), [
    (78, "N"), (69, "E"), (79, "O"), ("None", "N"), ("Even", "E"), ("odd", "O"), ("N", "N"),
])
def test_parity_codes(raw, parity):
    ch, dev = _sample_df1()
    m = _map({**ch, SERIAL + "PARITY": raw}, dev)
    assert m.params["parity"] == parity
    assert not any("parity" in x for x in m.problems)


def test_unknown_parity_code_is_reported():
    ch, dev = _sample_df1()
    m = _map({**ch, SERIAL + "PARITY": 3}, dev)
    assert m.params["parity"] == "N"
    assert any("parity" in x and "3" in x for x in m.problems)


@pytest.mark.parametrize(("key", "value", "check", "flagged"), [
    ("allen_bradley_df1.CHANNEL_ERROR_CHECKING", "CRC", "crc", False),
    ("allen_bradley_df1.CHANNEL_ERROR_CHECKING", "BCC", "bcc", False),
    ("allenbradley_df1.DEVICE_ERROR_CHECKING", 1, "crc", True),          # numeric code: mapping unverified
    ("allenbradley_df1.DEVICE_ERROR_CHECKING", 0, "bcc", True),
    ("df1.CHANNEL_CHECKSUM", "BCC (block check)", "bcc", False),
])
def test_error_check_setting(key, value, check, flagged):
    ch, dev = _sample_df1()
    m = _map(ch, {**dev, key: value})
    assert m.params["error_check"] == check
    assert any("error checking" in x for x in m.problems) is flagged


def test_station_ids():
    ch, dev = _sample_df1()
    assert _map(ch, {**dev, "servermain.DEVICE_ID_STRING": " 7 "}).params["station"] == 7
    m = _map(ch, {**dev, "servermain.DEVICE_ID_STRING": "12"})
    assert m.params["station"] == 12
    assert any("station" in x and "decimal" in x for x in m.problems)     # id format matters from 8 up
    with pytest.raises(ki._Exclude, match="DF1 station"):
        _map(ch, {**dev, "servermain.DEVICE_ID_STRING": "PLC-A"})
    with pytest.raises(ki._Exclude, match="DF1 station"):
        _map(ch, {**dev, "servermain.DEVICE_ID_STRING": "300"})


def test_missing_com_port_is_reported():
    ch, dev = _sample_df1()
    ch = {k: v for k, v in ch.items() if k != SERIAL + "COM_ID"}
    m = _map(ch, dev)
    assert m.host == "" and m.params["com_port"] is None and m.params["transport"] == "unmapped"
    assert any("no COM port" in x for x in m.problems)


def test_half_duplex_link_is_flagged():
    ch, dev = _sample_df1()
    m = _map({**ch, "allen_bradley_df1.CHANNEL_LINK_PROTOCOL": "Half Duplex Master"}, dev)
    assert any("half-duplex" in x for x in m.problems)


def test_df1_devices_on_one_com_port_share_it(tmp_path):
    project = json.loads(SAMPLE.read_text(encoding="utf-8-sig"))
    ch = next(c for c in project["project"]["channels"] if c["common.ALLTYPES_NAME"] == "LC_DF1")
    second = copy.deepcopy(ch["devices"][0])
    second["common.ALLTYPES_NAME"] = "Device2"
    second["servermain.DEVICE_ID_STRING"] = "2"
    ch["devices"].append(second)
    path = tmp_path / "project.json"
    path.write_text(json.dumps(project), encoding="utf-8")
    res = import_kep(path)
    devs = {d.name: d for c in res.devices_file.channels for d in c.devices if c.name == "LC_DF1"}
    assert {d.params["com_port"] for d in devs.values()} == {"COM112"}
    assert (devs["Device1"].params["station"], devs["Device2"].params["station"]) == (1, 2)
