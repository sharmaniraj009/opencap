"""Config loader: poller.yaml + devices.json + overrides.yaml + devices_map.yaml."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from opencap.config.kep_import import devices_json_text, import_kep, write_outputs
from opencap.config.loader import LoadedConfig, default_topic, load_config, slug
from opencap.config.models import Channel, Device, DevicesFile, PollerConfig, Tag

REPO = Path(__file__).parents[2]
SAMPLE = REPO / "tests" / "fixtures" / "project_sample.json"


def _device(channel: str, name: str, tags: list[Tag] | None = None, **kw) -> Device:
    return Device(
        id=f"{channel}/{name}", channel=channel, name=name, driver="modbus_tcp", host="192.0.2.1", port=502,
        params={"unit_id": 0}, tags=tags if tags is not None else [
            Tag(name="FLOW_1", address="400001", dtype="float32", scan_ms=100),
            Tag(name="LEVEL_1", address="400003", dtype="float32"),
        ], **kw,
    )


def _write(cfg: Path, devices: list[Device], **files: str) -> Path:
    cfg.mkdir(parents=True, exist_ok=True)
    channels: dict[str, list[Device]] = {}
    for d in devices:
        channels.setdefault(d.channel, []).append(d)
    df = DevicesFile(channels=[Channel(name=n, devices=ds) for n, ds in channels.items()])
    (cfg / "devices.json").write_text(devices_json_text(df), encoding="utf-8")
    for name, text in files.items():
        (cfg / name.replace("_yaml", ".yaml")).write_text(text, encoding="utf-8")
    return cfg


def _dev(loaded: LoadedConfig, dev_id: str) -> Device:
    return next(d for c in loaded.channels for d in c.devices if d.id == dev_id)


# --- slug / topic ------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Site 001", "site_001"),
        ("E+H", "e_h"),
        ("a/b#c+d", "a_b_c_d"),
        ("  Site 130 ", "site_130"),
        ("Rüdiger", "rudiger"),
        ("$SYS", "sys"),
        ("Site 032", "site_032"),
        ("+++", "_"),
    ],
)
def test_slug(raw, expected):
    out = slug(raw)
    assert out == expected
    assert not set(out) & set("+#/ $")


def test_default_topic():
    assert default_topic(_device("Site 001", "8 PUMP")) == "opencap/site_001/8_pump/data"


# --- files -------------------------------------------------------------------

def test_missing_devices_json(tmp_path):
    with pytest.raises(FileNotFoundError, match="import-kep"):
        load_config(tmp_path)


def test_only_devices_json(tmp_path):
    cfg = _write(tmp_path, [_device("S", "D")])
    loaded = load_config(cfg)
    assert loaded.poller == PollerConfig()
    d = _dev(loaded, "S/D")
    assert d.publish.topic == "opencap/s/d/data"
    assert d.publish.sub_device_id is None
    assert len(loaded.warnings) == 1 and "devices_map.yaml is missing" in loaded.warnings[0]
    assert loaded.excluded == []


def test_poller_yaml(tmp_path):
    cfg = _write(tmp_path, [_device("S", "D")], poller_yaml="instance: x\nmqtt: {host: broker, tls: true}\n")
    loaded = load_config(cfg)
    assert loaded.poller.instance == "x"
    assert loaded.poller.mqtt.host == "broker" and loaded.poller.mqtt.tls is True
    assert loaded.poller.mqtt.port == 1883


def test_empty_yaml_files_are_defaults(tmp_path):
    cfg = _write(tmp_path, [_device("S", "D")], poller_yaml="", overrides_yaml="# nothing\n", devices_map_yaml="")
    loaded = load_config(cfg)
    assert loaded.poller == PollerConfig()


def test_poller_yaml_unknown_key_is_an_error(tmp_path):
    cfg = _write(tmp_path, [_device("S", "D")], poller_yaml="mqtt: {hots: broker}\n")
    with pytest.raises(ValidationError):
        load_config(cfg)


def test_invalid_yaml_is_value_error(tmp_path):
    cfg = _write(tmp_path, [_device("S", "D")], overrides_yaml="devices: [oops\n")
    with pytest.raises(ValueError, match="overrides.yaml"):
        load_config(cfg)


def test_repo_example_configs_are_valid():
    cfg = REPO / "config"
    poller = PollerConfig.model_validate(yaml.safe_load((cfg / "poller.yaml").read_text(encoding="utf-8")))
    assert poller == PollerConfig()          # the example documents the defaults
    from opencap.config.models import Overrides
    Overrides.model_validate(yaml.safe_load((cfg / "overrides.yaml").read_text(encoding="utf-8")) or {})


# --- overrides ---------------------------------------------------------------

OVERRIDES = """
channels:
  S2: {enabled: false}
  NOPE: {enabled: false}
devices:
  S/D:
    host: 192.0.2.99
    timeout_ms: 1500
    retries: 0
    params: {max_holding: 16, nonnormal_float: unmodified}
    publish: {mode: on_change, deadband: 0.5}
    tags:
      FLOW_1: {enabled: false}
      LEVEL_1: {scan_ms: 5000, address: "400005", dtype: uint32, scale: {raw_lo: 0, raw_hi: 100, eng_lo: 0, eng_hi: 1}}
      GHOST: {enabled: false}
  X/Y: {enabled: false}
"""


def test_overrides(tmp_path):
    cfg = _write(tmp_path, [_device("S", "D"), _device("S2", "D")], overrides_yaml=OVERRIDES)
    loaded = load_config(cfg)
    d = _dev(loaded, "S/D")
    assert d.host == "192.0.2.99" and d.timeout_ms == 1500 and d.retries == 0
    assert d.port == 502                                    # untouched
    assert d.params == {"unit_id": 0, "max_holding": 16, "nonnormal_float": "unmodified"}
    assert d.publish.mode == "on_change" and d.publish.deadband == 0.5
    assert d.publish.interval_ms == 10_000                  # merged, not replaced
    tags = {t.name: t for t in d.tags}
    assert tags["FLOW_1"].enabled is False and tags["FLOW_1"].scan_ms == 100
    lvl = tags["LEVEL_1"]
    assert (lvl.scan_ms, lvl.address, lvl.dtype) == (5000, "400005", "uint32")
    assert lvl.scale is not None and lvl.scale.raw_hi == 100
    channels = {c.name: c for c in loaded.channels}
    assert channels["S2"].enabled is False and channels["S"].enabled is True
    text = "\n".join(loaded.warnings)
    assert "unknown channel 'NOPE'" in text
    assert "unknown device id 'X/Y'" in text
    assert "no tag 'GHOST'" in text


def test_override_invalid_value_is_an_error(tmp_path):
    cfg = _write(tmp_path, [_device("S", "D")], overrides_yaml="devices:\n  S/D: {publish: {mode: sometimes}}\n")
    with pytest.raises(ValidationError):
        load_config(cfg)
    cfg = _write(tmp_path, [_device("S", "D")], overrides_yaml="devices:\n  S/D: {tags: {FLOW_1: {dtype: int7}}}\n")
    with pytest.raises(ValidationError):
        load_config(cfg)


# --- devices_map -------------------------------------------------------------

MAP = """
defaults: {env: staging, interval_ms: 5000, colour: red}
devices:
  S/D: {sub_device_id: SUB_S_PLC, topic: custom/topic, env: prod}
  S/E: {sub_device_id: SUB_S_E, stub: true}
  S/F: {sub_device_id: SUB_S_F, stub: true}
  GONE/X: {sub_device_id: SUB_GONE}
"""


def test_devices_map(tmp_path):
    devs = [_device("S", n) for n in ("D", "E", "F", "G")]
    cfg = _write(tmp_path, devs, devices_map_yaml=MAP)
    loaded = load_config(cfg)
    d, e, g = _dev(loaded, "S/D"), _dev(loaded, "S/E"), _dev(loaded, "S/G")
    assert (d.publish.sub_device_id, d.publish.topic, d.publish.env) == ("SUB_S_PLC", "custom/topic", "prod")
    assert d.publish.interval_ms == 5000                     # from defaults
    assert (e.publish.sub_device_id, e.publish.topic, e.publish.env) == ("SUB_S_E", "opencap/s/e/data", "staging")
    assert g.publish.sub_device_id is None and g.publish.topic == "opencap/s/g/data"
    text = "\n".join(loaded.warnings)
    assert "2 device(s) use a stub SubDeviceId" in text      # one aggregate warning
    assert sum("stub SubDeviceId" in w for w in loaded.warnings) == 1
    assert "1 device(s) without SubDeviceId (no entry in devices_map.yaml): S/G" in text
    assert "unknown device id 'GONE/X'" in text
    assert "defaults.colour" in text


def test_map_entry_beats_overrides_publish(tmp_path):
    cfg = _write(
        tmp_path, [_device("S", "D")],
        devices_map_yaml="devices:\n  S/D: {sub_device_id: A, topic: from/map}\n",
        overrides_yaml="devices:\n  S/D: {publish: {topic: from/overrides, env: dev}}\n",
    )
    loaded = load_config(cfg)
    d = _dev(loaded, "S/D")
    assert d.publish.topic == "from/map"
    assert d.publish.env == "dev"                            # map entry has no env: overrides value kept
    assert any("set in both" in w for w in loaded.warnings)


def test_duplicate_topics_and_sub_device_ids_warn(tmp_path):
    devs = [_device("S", "E H"), _device("S", "E+H")]       # both slug to e_h
    cfg = _write(tmp_path, devs, devices_map_yaml="devices:\n  S/E H: {sub_device_id: SAME}\n  S/E+H: {sub_device_id: SAME}\n")
    loaded = load_config(cfg)
    text = "\n".join(loaded.warnings)
    assert "MQTT topic 'opencap/s/e_h/data' is used by 2 devices" in text
    assert "SubDeviceId 'SAME' is used by 2 devices" in text


# --- end to end with the importer ---------------------------------------------

def test_load_after_import(tmp_path):
    cfg = tmp_path / "config"
    write_outputs(import_kep(SAMPLE), cfg, tmp_path / "reports")
    loaded = load_config(cfg)
    devices = [d for c in loaded.channels for d in c.devices]
    assert len(devices) == 10
    assert len(loaded.excluded) == 5
    assert all(d.publish.sub_device_id and d.publish.topic for d in devices)
    assert _dev(loaded, "Test OPC/E+H").publish.topic == "opencap/test_opc/e_h/data"
    assert len(loaded.warnings) == 2
    assert "LC_DF1/Device1: COM112 is not mapped" in loaded.warnings[0]     # DF1: skipped until mapped
    assert _dev(loaded, "LC_DF1/Device1").enabled is False
    assert "10 device(s) use a stub SubDeviceId" in loaded.warnings[1]
    # the loaded devices still round-trip through the models
    DevicesFile.model_validate(json.loads(json.dumps(
        {"channels": [c.model_dump(mode="json") for c in loaded.channels]}
    )))
