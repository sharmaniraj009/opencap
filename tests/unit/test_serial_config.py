"""overrides.yaml `serial_ports`: an imported serial device (`transport: unmapped`) is skipped with
a warning until its KEPServer COM port is mapped to a serial-over-TCP gateway, an RFC 2217
server or a local serial device; a per-device override wins; bad mappings are config errors."""

from __future__ import annotations

import pytest
import yaml

from opencap.config.kep_import import OVERRIDES_TEMPLATE, devices_json_text
from opencap.config.loader import load_config
from opencap.config.models import Channel, Device, DevicesFile, Overrides, Tag
from opencap.core.registry import Registry


def _df1(name: str = "PLC", com: str | None = "COM112", station: int = 1, driver: str = "ab_df1") -> Device:
    params = {"station": station, "error_check": "crc", "transport": "unmapped", "com_port": com,
              "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1}
    tags = [Tag(name="FLOW", address="F19:0", dtype="float32")]
    if driver == "modbus_rtu":
        params = {"unit_id": station, "transport": "unmapped", "com_port": com, "baudrate": 9600}
        tags = [Tag(name="FLOW", address="400001", dtype="float32")]
    return Device(id=f"LC_DF1/{name}", channel="LC_DF1", name=name, driver=driver, host=com or "", port=0,
                  params=params, tags=tags)


def _write(tmp_path, devices: list[Device], overrides: dict | None = None):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    channels: dict[str, list[Device]] = {}
    for d in devices:
        channels.setdefault(d.channel, []).append(d)
    df = DevicesFile(channels=[Channel(name=n, devices=ds) for n, ds in channels.items()])
    (cfg / "devices.json").write_text(devices_json_text(df), encoding="utf-8")
    if overrides is not None:
        (cfg / "overrides.yaml").write_text(yaml.safe_dump(overrides), encoding="utf-8")
    return cfg


def _dev(loaded, dev_id):
    return next(d for c in loaded.channels for d in c.devices if d.id == dev_id)


def test_unmapped_device_is_skipped_with_a_warning(tmp_path):
    loaded = load_config(_write(tmp_path, [_df1()]))
    dev = _dev(loaded, "LC_DF1/PLC")
    assert dev.enabled is False and dev.params["transport"] == "unmapped"
    (warning,) = [w for w in loaded.warnings if "COM112" in w]
    assert "not mapped" in warning and "serial_ports" in warning and "skipped" in warning
    plan = Registry.build(loaded.channels).plans[dev.id]
    assert plan.ok and plan.blocks == 1                      # `opencap check` still shows its plan


@pytest.mark.parametrize(("target", "transport", "host", "port"), [
    ("192.0.2.50:4001", "tcp", "192.0.2.50", 4001),
    ("tcp://192.0.2.50:4001", "tcp", "192.0.2.50", 4001),
    ("rfc2217://192.0.2.51:4002", "rfc2217", "192.0.2.51", 4002),
    ("/dev/ttyUSB0", "serial", "/dev/ttyUSB0", 0),
])
def test_serial_ports_maps_the_com_port(tmp_path, target, transport, host, port):
    loaded = load_config(_write(tmp_path, [_df1()], {"serial_ports": {"COM112": target}}))
    dev = _dev(loaded, "LC_DF1/PLC")
    assert dev.enabled is True
    assert (dev.params["transport"], dev.host, dev.port) == (transport, host, port)
    assert dev.params["com_port"] == "COM112" and dev.params["baudrate"] == 9600
    assert not [w for w in loaded.warnings if "COM112" in w]


def test_one_mapping_serves_every_device_on_the_port_case_insensitively(tmp_path):
    devs = [_df1("A", station=1), _df1("B", station=2)]
    loaded = load_config(_write(tmp_path, devs, {"serial_ports": {"com112": "192.0.2.50:4001"}}))
    for name in ("A", "B"):
        d = _dev(loaded, f"LC_DF1/{name}")
        assert (d.enabled, d.host, d.port) == (True, "192.0.2.50", 4001)


def test_device_override_wins_over_serial_ports(tmp_path):
    overrides = {
        "serial_ports": {"COM112": "192.0.2.50:4001"},
        "devices": {"LC_DF1/B": {"host": "/dev/ttyS1", "params": {"transport": "serial"}}},
    }
    loaded = load_config(_write(tmp_path, [_df1("A"), _df1("B")], overrides))
    b = _dev(loaded, "LC_DF1/B")
    assert (b.params["transport"], b.host, b.port) == ("serial", "/dev/ttyS1", 0)
    assert _dev(loaded, "LC_DF1/A").host == "192.0.2.50"


def test_unused_mapping_is_a_warning(tmp_path):
    loaded = load_config(_write(tmp_path, [_df1()], {"serial_ports": {"COM112": "192.0.2.50:4001",
                                                                      "COM9": "192.0.2.59:4001"}}))
    assert any("serial_ports COM9 is not used" in w for w in loaded.warnings)


def test_device_without_com_port(tmp_path):
    loaded = load_config(_write(tmp_path, [_df1(com=None)]))
    assert _dev(loaded, "LC_DF1/PLC").enabled is False
    assert any("its serial port is not mapped" in w for w in loaded.warnings)


@pytest.mark.parametrize(("overrides", "match"), [
    ({"serial_ports": {"COM112": "192.0.2.50"}}, "serial_ports COM112.*port"),
    ({"serial_ports": {"COM112": "udp://192.0.2.50:4001"}}, "scheme"),
    ({"devices": {"LC_DF1/PLC": {"params": {"transport": "tcp"}}}}, "port 1..65535"),
    ({"devices": {"LC_DF1/PLC": {"params": {"transport": "modem"}}}}, "transport"),
    ({"devices": {"LC_DF1/PLC": {"params": {"baudrate": 0}}}, "serial_ports": {"COM112": "/dev/ttyS0"}},
     "baudrate"),
])
def test_bad_serial_config_is_an_error(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        load_config(_write(tmp_path, [_df1()], overrides))


def test_modbus_rtu_rejects_rfc2217(tmp_path):
    cfg = _write(tmp_path, [_df1(driver="modbus_rtu")], {"serial_ports": {"COM112": "rfc2217://192.0.2.51:4002"}})
    with pytest.raises(ValueError, match="rfc2217 is not supported by modbus_rtu"):
        load_config(cfg)
    cfg2 = _write(tmp_path / "b", [_df1(driver="modbus_rtu")], {"serial_ports": {"COM112": "192.0.2.50:4001"}})
    dev = _dev(load_config(cfg2), "LC_DF1/PLC")
    assert (dev.driver, dev.params["transport"], dev.port) == ("modbus_rtu", "tcp", 4001)


def test_overrides_template_mentions_serial_ports_and_is_empty():
    assert "serial_ports" in OVERRIDES_TEMPLATE
    assert Overrides.model_validate(yaml.safe_load(OVERRIDES_TEMPLATE)) == Overrides()
