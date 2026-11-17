"""KEPServer importer on the synthetic export tests/fixtures/project_sample.json."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opencap.__main__ import app
from opencap.config import kep_import as ki
from opencap.config.kep_import import import_kep, write_outputs
from opencap.config.models import DeviceDefaults, DeviceMap, DevicesFile, Overrides

SAMPLE = Path(__file__).parents[1] / "fixtures" / "project_sample.json"
SAMPLE_SECRETS = ("dummy-user-x", "dummy-pass-x", "dummy-mqtt-user-x", "dummy-mqtt-pass-x")

# docs/driver_params.md: the importer writes exactly these keys
PARAM_KEYS = {
    "modbus_tcp": {
        "unit_id", "zero_based", "zero_based_bit", "modicon_bit_order", "byte_order_big",
        "first_word_low", "first_dword_low", "max_coils", "max_discrete", "max_input",
        "max_holding", "max_gap", "max_gap_bits", "nonnormal_float", "close_on_timeout",
    },
    "ab_logix": {"micro800", "path", "slot", "max_batch", "connection_size"},
    "ab_pccc": {"path", "max_elements", "max_gap", "request_size"},
    "s7": {"rack", "slot", "max_pdu", "link_type", "byte_order_big"},
    "melsec_mc": {"plc_type", "protocol", "first_word_low", "frame"},
    "opcua_client": {
        "endpoint", "security_policy", "message_mode", "username_env", "password_env",
        "publishing_interval_ms", "sampling_interval_ms",
    },
}


@pytest.fixture(scope="module")
def result():
    return import_kep(SAMPLE)


@pytest.fixture(scope="module")
def devices(result):
    return {d.id: d for c in result.devices_file.channels for d in c.devices}


@pytest.fixture(scope="module")
def excluded(result):
    return {f"{e.channel}/{e.device}": e for e in result.devices_file.excluded}


# --- small parsers -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,host,rest",
    [
        ("192.0.2.1", "192.0.2.1", ""),
        ("192.0.2.1 ", "192.0.2.1", ""),
        ("  <192.0.2.1> ", "192.0.2.1", ""),
        ("<192.0.2.1>.0", "192.0.2.1", ".0"),
        ("<192.0.2.1>.17", "192.0.2.1", ".17"),
        ("<192.0.2.1>,1,0", "192.0.2.1", ",1,0"),
        ("192.0.2.1,1,0", "192.0.2.1", ",1,0"),
        ("192.0.2.1.5", "192.0.2.1", ".5"),
        ("plc-1.example", "plc-1.example", ""),
    ],
)
def test_parse_device_id(raw, host, rest):
    assert ki.parse_device_id(raw) == (host, rest)


@pytest.mark.parametrize(
    "rest,path,slot",
    [("", None, None), (",1,0", "1,0", 0), (",1,3", "1,3", 3), (", 1 , 2 ", "1,2", 2), (".0", None, None)],
)
def test_cip_path(rest, path, slot):
    assert ki._cip_path(rest) == (path, slot)


@pytest.mark.parametrize(
    "channel,device,single,expected",
    [
        ("Site 004", "7 PUMP", True, "SUB_SITE_004_PLC"),
        ("Site 001", "8 PUMP", True, "SUB_SITE_001_PLC"),
        ("S7 MULTI", "OUT-2", False, "SUB_S7_MULTI_OUT_2"),
        ("a -- b", "x__y", False, "SUB_A_B_X_Y"),
        ("Test_OPC1", "E+H", True, "SUB_TEST_OPC1_PLC"),
    ],
)
def test_stub_sub_device_id(channel, device, single, expected):
    assert ki.stub_sub_device_id(channel, device, single) == expected


@pytest.mark.parametrize(
    "a,b,da,db,cls",
    [
        ("PUMP_1_CURRENT_Y", "PUMP_1_CURRENT_B", "float32", "float32", "suspect"),
        ("SEWAGE_LAST_DAY_1", "SEWAGE_LAST_DAY", "float32", "float32", "alias"),
        ("RAW_MAX", "RAW_MAX1", "float32", "float32", "alias"),
        ("EU_MAX", "LEVEL_1_MAX", "float32", "float32", "alias"),
        ("LT_CURRENT_R", "PUMP_6_CURRENT_R", "float32", "float32", "alias"),
        ("PUMP_3_RFB", "PUMP3_RFB", "bool", "bool", "alias"),
        ("LEVEL_1", "LEVEL_2", "float32", "float32", "suspect"),
        ("PUMP_1", "PUMP_10", "uint16", "uint16", "suspect"),
        ("LT_2_STOP", "LT_3_START", "bool", "bool", "suspect"),
        ("PUMP_7_RFB", "RPT_PUMP_7_AVG_CURRENT", "bool", "uint16", "suspect"),
        ("PUMP_1_AVG_VOLTAGE", "PUMP_1_AVG_VOLTAGE_1", "float32", "uint16", "suspect"),
    ],
)
def test_classify_pair(a, b, da, db, cls):
    assert ki.classify_pair(a, b, da, db)[0] == cls
    assert ki.classify_pair(b, a, db, da)[0] == cls


def test_classify_group_is_suspect_if_any_pair_is():
    cls, reason = ki.classify_group(["HT_TFB", "LT_1_TFB", "LT_2_STOP"], ["bool"] * 3)
    assert cls == "suspect" and "STOP" in reason
    assert ki.classify_group(["HT_TFB", "LT_1_TFB", "PUMP_9_TFB"], ["bool"] * 3)[0] == "alias"


# --- import ------------------------------------------------------------------

def test_counts(result):
    s = result.summary
    assert s["source"] == {**s["source"], "channels": 14, "devices": 15, "tags": 46}
    assert s["included"]["devices"] == 9
    assert s["included"]["channels"] == 8
    assert {k: v["devices"] for k, v in s["included"]["by_driver"].items()} == {
        "ab_logix": 2, "ab_pccc": 1, "melsec_mc": 1, "modbus_tcp": 1, "opcua_client": 1, "s7": 3,
    }
    assert s["included"]["micro800"] == {"devices": 1, "tags": 6}
    assert s["excluded"]["channels_without_devices"] == ["Test API"]


def test_exclusions(excluded):
    reasons = {k: e.reason for k, e in excluded.items()}
    assert reasons == {
        "SITE CLX OTHER/PLC": "unmapped ControlLogix model 5",
        "SIMULATOR/Device1": ki.REASON_SIMULATOR,
        "SIMULATOR/ph": ki.REASON_SIMULATOR,
        "MQTT1/Device1": ki.REASON_MQTT,
        "LC_DF1/Device1": ki.REASON_DF1,
        "SITE DISABLED/Device1": ki.REASON_DISABLED,
    }
    assert ki.REASON_DF1 == "DF1 serial: Phase 7"
    assert excluded["LC_DF1/Device1"].tags == 3
    assert excluded["LC_DF1/Device1"].kep_driver == "Allen-Bradley DF1"


def test_ucon_without_devices_is_reported(result):
    assert any("Test API" in p for p in result.problems)


def test_params_keys_match_contract(devices):
    for dev in devices.values():
        assert set(dev.params) == PARAM_KEYS[dev.driver], dev.id


def test_modbus_device(devices):
    d = devices["SITE MODBUS/Device1"]
    assert (d.driver, d.host, d.port) == ("modbus_tcp", "192.0.2.10", 502)  # device port, not channel 4840
    assert d.params["unit_id"] == 1
    assert d.params["max_holding"] == 32 and d.params["max_coils"] == 32 and d.params["max_discrete"] == 32
    assert d.params["modicon_bit_order"] is False
    assert d.params["nonnormal_float"] == "zero"
    assert d.params["max_gap_bits"] is None
    assert (d.timeout_ms, d.retries) == (1000, 2)       # 3 KEPServer attempts = first try + 2 retries
    assert (d.demote_after, d.demote_for_ms) == (DeviceDefaults().demote_after, DeviceDefaults().demote_for_ms)
    assert d.source == {**d.source, "kep_driver": "Modbus TCP/IP Ethernet", "kep_model": 0,
                        "device_id_string": "<192.0.2.10>.1", "kep_retry_attempts": 3}
    tags = {t.name: t for t in d.tags}
    assert tags["FLOW_1"].dtype == "float32" and tags["FLOW_1"].scan_ms == 100
    assert tags["PUMP_1_RFB"].dtype == "bool" and tags["PUMP_1_RFB"].scan_ms == 10
    assert tags["PUMP_1_HOURS"].dtype == "uint32" and tags["PUMP_1_HOURS"].description == "run hours"
    assert tags["FLOW_1"].description is None
    lvl = tags["LEVEL_1"].scale
    assert lvl is not None and lvl.type == "linear"
    assert (lvl.raw_lo, lvl.raw_hi, lvl.eng_lo, lvl.eng_hi) == (0, 4095, 0, 10)
    assert lvl.clamp_lo and lvl.clamp_hi and not lvl.negate and lvl.units == "m"
    assert tags["FLOW_SQRT"].scale.type == "sqrt"
    assert tags["FLOW_1"].scale is None


def test_modbus_nonnormal_float_from_channel(tmp_path):
    raw = json.loads(SAMPLE.read_text(encoding="utf-8-sig"))
    ch = next(c for c in raw["project"]["channels"] if c[ki.K_NAME] == "SITE MODBUS")
    ch["servermain.CHANNEL_NON_NORMALIZED_FLOATING_POINT_HANDLING"] = 1
    path = tmp_path / "p.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    res = import_kep(path)
    dev = next(d for c in res.devices_file.channels for d in c.devices if d.id == "SITE MODBUS/Device1")
    assert dev.params["nonnormal_float"] == "unmodified"


def test_micro800_trailing_space_and_default_type(devices, result):
    d = devices["SITE M800/8 PUMP"]
    assert (d.driver, d.host, d.port) == ("ab_logix", "192.0.2.20", 44818)
    assert d.params == {"micro800": True, "path": None, "slot": None, "max_batch": 40, "connection_size": None}
    assert d.source["device_id_string"] == "192.0.2.20 "
    assert any("SITE M800/8 PUMP" in p and "whitespace" in p for p in result.problems)
    tags = {t.name: t for t in d.tags}
    assert tags["TOTAL_DEFAULT"].dtype == "float32"     # Default -> device default data type (Float)
    assert tags["STATUS_TEXT"].dtype == "string"


def test_logix_path_and_slot(devices, result):
    d = devices["SITE LOGIX/STP"]
    assert d.driver == "ab_logix"
    assert d.params == {"micro800": False, "path": "1,0", "slot": 0, "max_batch": 40, "connection_size": 500}
    dtypes = {t.name: t.dtype for t in d.tags}
    assert dtypes == {"INF_DAY_TOT": "float32", "RUN_SECONDS": "int32", "ENERGY": "float64", "TEMP_RAW": "int16"}
    assert any("TEMP_RAW" in p and "Char" in p for p in result.problems)


def test_pccc_scan_mode_2_demotion_and_duplicate_names(devices, result):
    d = devices["SITE PCCC/7 PUMP"]
    assert d.driver == "ab_pccc"
    assert d.params == {"path": None, "max_elements": 100, "max_gap": 4, "request_size": 232}
    assert d.scan_ms == 5000
    assert all(t.scan_ms is None for t in d.tags)
    assert (d.timeout_ms, d.retries, d.demote_after, d.demote_for_ms) == (1000, 3, 5, 60000)
    names = [t.name for t in d.tags]
    assert names.count("PUMP_1_HOURS") == 1
    assert next(t for t in d.tags if t.name == "PUMP_1_HOURS").address == "N54:17"   # first one kept
    assert any("duplicate tag name 'PUMP_1_HOURS'" in p for p in result.problems)


def test_s7_and_melsec(devices):
    s7 = devices["SITE S7/PLC"]
    assert (s7.host, s7.port) == ("192.0.2.50", 102)
    assert s7.params == {"rack": 0, "slot": 1, "max_pdu": 960, "link_type": 3, "byte_order_big": True}
    mc = devices["SITE MELSEC/STP"]
    assert (mc.host, mc.port, mc.timeout_ms) == ("192.0.2.60", 5001, 250)
    assert mc.params == {"plc_type": "Q", "protocol": "tcp", "first_word_low": True, "frame": "3E"}


def test_opcua_device(devices, result):
    d = devices["Test OPC/E+H"]
    assert (d.driver, d.host, d.port) == ("opcua_client", "192.0.2.70", 4841)
    assert d.params == {
        "endpoint": "opc.tcp://192.0.2.70:4841/Server",
        "security_policy": "None",
        "message_mode": "None",
        "username_env": "OPENCAP_OPCUA_TEST_OPC_USER",
        "password_env": "OPENCAP_OPCUA_TEST_OPC_PASSWORD",
        "publishing_interval_ms": 1000,
        "sampling_interval_ms": 500,
    }
    # nested tag groups are flattened "<group>.<group>.<tag>"; empty groups are fine
    assert [t.name for t in d.tags] == [
        "LTI002.Pv", "LTI002.Qv", "Tags.Dev 1.Values.Angle", "Tags.Dev 1.Values.Battery",
    ]
    assert all(t.dtype == "float64" for t in d.tags)
    assert any("Test OPC/E+H" in p and "test channel" in p for p in result.problems)


def test_opcua_endpoint_userinfo_is_dropped(tmp_path):
    raw = json.loads(SAMPLE.read_text(encoding="utf-8-sig"))
    ch = next(c for c in raw["project"]["channels"] if c[ki.K_NAME] == "Test OPC")
    ch["opcuaclient.CHANNEL_UA_SERVER_ENDPOINT_URL"] = "opc.tcp://dummy-user-x:dummy-pass-x@192.0.2.70/UA"
    ch["opcuaclient.CHANNEL_UA_SERVER_SECURITY_POLICY"] = 3
    ch["opcuaclient.CHANNEL_UA_SERVER_MESSAGE_MODE"] = 3
    del ch["opcuaclient.CHANNEL_AUTHENTICATION_USERNAME"]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    res = import_kep(path)
    dev = next(d for c in res.devices_file.channels for d in c.devices if d.driver == "opcua_client")
    assert dev.params["endpoint"] == "opc.tcp://192.0.2.70/UA"
    assert dev.port == 4840
    assert (dev.params["security_policy"], dev.params["message_mode"]) == ("Basic256Sha256", "SignAndEncrypt")
    assert dev.params["username_env"] is None
    assert dev.params["password_env"] == "OPENCAP_OPCUA_TEST_OPC_PASSWORD"


def test_same_address(result):
    rows = {(r.device_id, r.address): r for r in result.same_address}
    assert set(rows) == {
        ("SITE MODBUS/Device1", "400133"),
        ("SITE MODBUS/Device1", "400201"),
        ("SITE M800/8 PUMP", "EU_MAX"),
        ("SITE PCCC/7 PUMP", "B3:17/15"),
        ("LC_DF1/Device1", "F19:0"),
    }
    conflict = rows[("SITE MODBUS/Device1", "400133")]
    assert conflict.classification == "suspect"
    assert conflict.tags == ["PUMP_1_CURRENT_Y", "PUMP_1_CURRENT_B"]
    assert conflict.n_pairs == 1
    assert rows[("SITE MODBUS/Device1", "400201")].classification == "alias"
    assert rows[("SITE M800/8 PUMP", "EU_MAX")].classification == "alias"
    dtype_clash = rows[("SITE PCCC/7 PUMP", "B3:17/15")]
    assert dtype_clash.classification == "suspect" and dtype_clash.dtypes == ["bool", "float32"]
    assert rows[("LC_DF1/Device1", "F19:0")].included is False
    assert result.summary["same_address"]["pairs"] == 5
    assert result.summary["same_address"]["pairs_included"] == 4


def test_defaults_are_used_when_kepserver_has_no_value(tmp_path):
    custom = DeviceDefaults(scan_ms=2000, min_scan_ms=500, timeout_ms=4000, retries=1, demote_after=7, demote_for_ms=1)
    res = import_kep(SAMPLE, defaults=custom)
    opc = next(d for c in res.devices_file.channels for d in c.devices if d.driver == "opcua_client")
    assert (opc.scan_ms, opc.min_scan_ms, opc.timeout_ms, opc.retries) == (2000, 500, 4000, 1)
    assert (opc.demote_after, opc.demote_for_ms) == (7, 1)


def test_summary_text(result):
    text = result.summary_text()
    assert "14 channels, 15 devices, 46 tags" in text
    assert "modbus_tcp" in text and "DF1 serial: Phase 7" in text
    assert "Micro800: 1 devices" in text


# --- outputs -----------------------------------------------------------------

@pytest.fixture()
def written(tmp_path):
    res = import_kep(SAMPLE)
    cfg, rep = tmp_path / "config", tmp_path / "reports"
    paths = write_outputs(res, cfg, rep)
    return res, cfg, rep, paths


def test_write_outputs_files(written):
    res, cfg, rep, paths = written
    names = {p.name for p in paths}
    assert names == {"devices.json", "devices_map.yaml", "overrides.yaml", "same_address.csv", "import_summary.json"}
    # devices.json round-trips through the model
    loaded = DevicesFile.model_validate_json((cfg / "devices.json").read_text(encoding="utf-8"))
    assert loaded.channels == res.devices_file.channels
    assert loaded.excluded == res.devices_file.excluded
    # template overrides parse as empty Overrides
    assert Overrides.model_validate(yaml.safe_load((cfg / "overrides.yaml").read_text())) == Overrides()
    summary = json.loads((rep / "import_summary.json").read_text(encoding="utf-8"))
    assert summary["source"]["channels"] == 14
    assert summary["problems"] == res.problems
    with (rep / "same_address.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 5
    assert set(rows[0]) == set(ki.SameAddressRow.CSV_FIELDS)
    assert rows[0]["classification"] == "suspect"      # suspects first
    assert sum(int(r["n_pairs"]) for r in rows) == 5


def test_devices_map_stubs(written):
    _, cfg, _, _ = written
    dm = DeviceMap.model_validate(yaml.safe_load((cfg / "devices_map.yaml").read_text(encoding="utf-8")))
    assert dm.devices["SITE MODBUS/Device1"].sub_device_id == "SUB_SITE_MODBUS_PLC"
    assert dm.devices["S7 MULTI/OUT 1"].sub_device_id == "SUB_S7_MULTI_OUT_1"
    assert dm.devices["S7 MULTI/OUT-2"].sub_device_id == "SUB_S7_MULTI_OUT_2"
    assert all(e.stub for e in dm.devices.values())
    assert len(dm.devices) == 9


def test_reimport_never_overwrites_map_or_overrides(written):
    res, cfg, rep, _ = written
    map_path, ov_path = cfg / "devices_map.yaml", cfg / "overrides.yaml"
    # hand edits: confirm one id, drop another entry, add a comment
    text = map_path.read_text(encoding="utf-8")
    text = text.replace("sub_device_id: SUB_SITE_MODBUS_PLC\n    stub: true",
                        "sub_device_id: SUB_REAL_ID  # confirmed\n    topic: x/y")
    text = text.replace("  SITE S7/PLC:\n    sub_device_id: SUB_SITE_S7_PLC\n    stub: true\n", "")
    map_path.write_text(text, encoding="utf-8")
    ov_text = "channels: {}\ndevices:\n  SITE S7/PLC: {enabled: false}  # keep me\n"
    ov_path.write_text(ov_text, encoding="utf-8")

    paths = write_outputs(import_kep(SAMPLE), cfg, rep)

    new_text = map_path.read_text(encoding="utf-8")
    assert new_text.startswith(text)                  # appended, nothing rewritten
    assert "# confirmed" in new_text
    dm = DeviceMap.model_validate(yaml.safe_load(new_text))
    assert dm.devices["SITE MODBUS/Device1"].sub_device_id == "SUB_REAL_ID"
    assert dm.devices["SITE MODBUS/Device1"].stub is False
    assert dm.devices["SITE S7/PLC"].stub is True     # re-added as a stub
    assert len(dm.devices) == 9
    assert ov_path.read_text(encoding="utf-8") == ov_text
    assert ov_path not in paths and map_path in paths

    # a third import with nothing new leaves the map byte-identical
    before = map_path.read_text(encoding="utf-8")
    paths = write_outputs(import_kep(SAMPLE), cfg, rep)
    assert map_path.read_text(encoding="utf-8") == before
    assert map_path not in paths


def test_devices_map_with_unappendable_layout_is_left_alone(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    original = "devices: {}\ndefaults: {env: test}\n"
    (cfg / "devices_map.yaml").write_text(original, encoding="utf-8")
    res = import_kep(SAMPLE)
    write_outputs(res, cfg, tmp_path / "reports")
    assert (cfg / "devices_map.yaml").read_text(encoding="utf-8") == original
    side = DeviceMap.model_validate(yaml.safe_load((cfg / "devices_map.stubs.yaml").read_text(encoding="utf-8")))
    assert len(side.devices) == 9
    assert any("devices_map.yaml not updated" in p for p in res.problems)


def test_devices_map_invalid_is_left_alone(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    original = "devices: [oops\n"
    (cfg / "devices_map.yaml").write_text(original, encoding="utf-8")
    res = import_kep(SAMPLE)
    write_outputs(res, cfg, tmp_path / "reports")
    assert (cfg / "devices_map.yaml").read_text(encoding="utf-8") == original
    assert any("invalid" in p for p in res.problems)


def test_devices_map_without_devices_key_gets_one(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    original = "# my map\ndefaults:\n  env: test\n"
    (cfg / "devices_map.yaml").write_text(original, encoding="utf-8")
    write_outputs(import_kep(SAMPLE), cfg, tmp_path / "reports")
    text = (cfg / "devices_map.yaml").read_text(encoding="utf-8")
    assert text.startswith(original)
    dm = DeviceMap.model_validate(yaml.safe_load(text))
    assert dm.defaults == {"env": "test"} and len(dm.devices) == 9


def test_no_credentials_in_outputs(written):
    _, cfg, rep, _ = written
    for path in [*cfg.iterdir(), *rep.iterdir()]:
        text = path.read_text(encoding="utf-8")
        leaked = [i for i, secret in enumerate(SAMPLE_SECRETS) if secret in text]
        assert leaked == [], f"credential #{leaked} found in {path.name}"


def test_cli_import_kep(tmp_path):
    cfg, rep = tmp_path / "config", tmp_path / "reports"
    res = CliRunner().invoke(app, ["import-kep", str(SAMPLE), "--config", str(cfg), "--reports", str(rep)])
    assert res.exit_code == 0, res.output
    assert "14 channels" in res.output
    assert "wrote" in res.output and "devices.json" in res.output
    assert (cfg / "devices.json").exists() and (rep / "same_address.csv").exists()
    for secret in SAMPLE_SECRETS:
        assert secret not in res.output


def test_not_a_project(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"foo": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        import_kep(path)
