"""Reserved system-tag names (docs/spec/system-tags.md 3.2, PRJ-13, LOG-07): errors for the reserved
words, a warning for any other name starting with `_`, a 422 for API writes that would add one,
and importer notes instead of failures. The shared fixtures are in tests/conformance/config/."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opencap.api.server import create_app
from opencap.config import kep_import as ki
from opencap.config.kep_import import import_kep
from opencap.config.loader import load_config
from opencap.config.models import Channel, Device
from tests.unit.test_config_api import ADMIN, ENV, V1, admin_w, api_cfg, make_config
from tests.helpers.fake_poller import ConfigPoller

SAMPLE = Path(__file__).parents[1] / "fixtures" / "project_sample.json"
FIXTURES = Path(__file__).parents[1] / "conformance" / "config" / "valid"


def _device(name: str = "PLC1", tags: list[str] | None = None) -> dict:
    return {"id": f"S/{name}", "channel": "S", "name": name, "driver": "modbus_tcp", "host": "192.0.2.5",
            "port": 502, "tags": [{"name": t, "address": "400001", "dtype": "int16"} for t in tags or ["T1"]]}


@pytest.mark.parametrize("name", ["_System", "_SYSTEM", "_system"])
def test_channel_may_not_be_system(name):
    with pytest.raises(ValidationError, match="channel name .* reserved word '_System'"):
        Channel(name=name, devices=[])
    Channel(name="_Statistics", devices=[])        # only a device or a tag group may not be `_Statistics`


@pytest.mark.parametrize(("name", "word"), [("_System", "_System"), ("_statistics", "_Statistics"),
                                            ("_STATISTICS", "_Statistics")])
def test_device_may_not_be_a_system_folder(name, word):
    with pytest.raises(ValidationError, match=f"device name .* reserved word '{word}'"):
        Device(**_device(name))


@pytest.mark.parametrize(("tag", "word"), [("_Statistics", "_Statistics"), ("_statistics.Reads", "_Statistics"),
                                           ("_SYSTEM.X", "_System")])
def test_tag_group_may_not_be_a_system_folder(tag, word):
    with pytest.raises(ValidationError, match=f"reserved group name '{word}'"):
        Device(**_device(tags=[tag]))
    Device(**_device(tags=["Group._Statistics", "_Statisticsx"]))     # not the first segment / other word


def test_loader_lists_underscore_names(tmp_path):
    fx = json.loads((FIXTURES / "devices_underscore_names.json").read_text(encoding="utf-8"))
    (tmp_path / "devices.json").write_text(json.dumps(fx["data"]), encoding="utf-8")
    warnings = [w for w in load_config(tmp_path).warnings if "'_'" in w]
    assert warnings == [
        "devices.json: channel name '_Spare' starts with '_'; names starting with '_' are reserved for system tags",
        "devices.json: SITE_A/_Backup: device name '_Backup' starts with '_'; "
        "names starting with '_' are reserved for system tags",
        "devices.json: SITE_A/_Backup: tag name 'Pumps._Spare.Speed' has a segment starting with '_'; "
        "names starting with '_' are reserved for system tags",
        "devices.json: SITE_A/_Backup: tag name '_Spare' has a segment starting with '_'; "
        "names starting with '_' are reserved for system tags",
        "devices.json: SITE_A/PLC2: tag name '_System_Like' has a segment starting with '_'; "
        "names starting with '_' are reserved for system tags",
        "devices.json: SITE_A/PLC2: tag name 'Systems._System' has a segment starting with '_'; "
        "names starting with '_' are reserved for system tags",
    ]


# --- API writes (PRJ-13) ---------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    config_dir = make_config(tmp_path)
    app = create_app(ConfigPoller(config_dir), api_cfg(tmp_path), environ=ENV)
    return TestClient(app), config_dir


DEV_PATH = f"{V1}/devices/SITE MODBUS/Device1"


def test_api_refuses_new_underscore_names_with_422(env):
    c, cfg = env
    before = (cfg / "devices.json").read_bytes()
    tag = {"name": "_Spare", "address": "400401", "dtype": "uint16"}
    r = c.post(f"{DEV_PATH}/tags", json=tag, headers=admin_w(c))
    assert r.status_code == 422 and "'_Spare'" in r.json()["detail"][0]["msg"]
    r = c.post(f"{DEV_PATH}/tags", json={**tag, "name": "Group._Spare.X"}, headers=admin_w(c))
    assert r.status_code == 422
    r = c.post(f"{V1}/channels", json={"name": "_Spare"}, headers=admin_w(c))
    assert r.status_code == 422 and "channel name '_Spare'" in r.json()["detail"][0]["msg"]
    r = c.post(f"{V1}/channels/SITE MODBUS/devices", json={"name": "_Spare", "driver": "modbus_tcp",
                                                           "host": "192.0.2.1", "port": 502}, headers=admin_w(c))
    assert r.status_code == 422
    for reserved in ("_System", "_Statistics.X"):
        r = c.post(f"{DEV_PATH}/tags", json={**tag, "name": reserved}, headers=admin_w(c))
        assert r.status_code == 422
    r = c.post(f"{V1}/channels", json={"name": "_system"}, headers=admin_w(c))
    assert r.status_code == 422 and "reserved word" in r.json()["detail"][0]["msg"]
    assert (cfg / "devices.json").read_bytes() == before


def test_api_still_edits_a_file_that_already_has_an_underscore_name(env):
    c, cfg = env
    doc = json.loads((cfg / "devices.json").read_text(encoding="utf-8"))
    doc["channels"][0]["devices"][0]["tags"][0]["name"] = "_Old"
    (cfg / "devices.json").write_text(json.dumps(doc), encoding="utf-8")
    r = c.post(f"{DEV_PATH}/tags", json={"name": "Fine", "address": "400401", "dtype": "uint16"},
               headers=admin_w(c))
    assert r.status_code in (200, 201), r.text
    assert c.get(f"{DEV_PATH}/tags", headers=ADMIN).status_code == 200


# --- KEPServerEX importer (LOG-07) -----------------------------------------------

def _kep_project(tmp_path: Path, channel: str, device: str, tag_names: list[str]) -> Path:
    def tag(name: str) -> dict:
        return {"common.ALLTYPES_NAME": name, "servermain.TAG_ADDRESS": "400001", "servermain.TAG_DATA_TYPE": 4}

    sample = json.loads(SAMPLE.read_text(encoding="utf-8-sig"))
    ch = copy.deepcopy(sample["project"]["channels"][0])
    ch["common.ALLTYPES_NAME"] = channel
    ch["devices"] = [ch["devices"][0]]
    ch["devices"][0]["common.ALLTYPES_NAME"] = device
    ch["devices"][0]["tags"] = [tag(n) for n in tag_names]
    path = tmp_path / f"project_{len(list(tmp_path.iterdir()))}.json"
    path.write_text(json.dumps({"project": {"channels": [ch]}}), encoding="utf-8")
    return path


def test_importer_reports_reserved_and_underscore_tags(tmp_path):
    res = import_kep(_kep_project(tmp_path, "SITE_A", "PLC1", ["_Spare", "Group._Hidden", "_System.X", "_Statistics", "Ok"]))
    tags = [t.name for c in res.devices_file.channels for d in c.devices for t in d.tags]
    assert tags == ["_Spare", "Group._Hidden", "Ok"]
    text = "\n".join(res.problems)
    assert "'_System.X' starts with the reserved word '_System'" in text
    assert "'_Statistics' starts with the reserved word '_Statistics'" in text
    assert "'_Spare' has a segment starting with '_'" in text and "'Group._Hidden' has a segment" in text


def test_importer_reports_reserved_device_and_channel(tmp_path):
    res = import_kep(_kep_project(tmp_path, "SITE_A", "_statistics", ["T1"]))
    assert [(e.device, e.reason) for e in res.devices_file.excluded] == [("_statistics", ki.REASON_RESERVED_NAME)]
    res = import_kep(_kep_project(tmp_path, "_SYSTEM", "PLC1", ["T1"]))
    assert res.devices_file.channels == []
    assert any("channel '_SYSTEM'" in p and "reserved word '_System'" in p for p in res.problems)
    res = import_kep(_kep_project(tmp_path, "_Spare", "_Backup", ["T1"]))
    assert [c.name for c in res.devices_file.channels] == ["_Spare"]
    assert sum("starting with '_'" in p for p in res.problems) == 2
