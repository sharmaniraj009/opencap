"""Naming rules (#112, PRJ-13, API-07, LOG-21): names that make tag paths ambiguous are warnings
for v1 files and 422s for API writes, names are never changed, and an unknown `payload.timezone`
fails at load. Python half; the Rust engine follows (see the PR)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opencap.api.server import create_app
from opencap.config.loader import load_config
from opencap.config.models import DevicesFile, PayloadCfg, name_rule_problems
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ADMIN, ENV, V1, admin_w, api_cfg, make_config


def _devices(channel: str = "S", device: str = "PLC1", tags: list[str] | None = None) -> dict:
    return {"version": 1, "channels": [{"name": channel, "devices": [
        {"id": f"{channel}/{device}", "channel": channel, "name": device, "driver": "modbus_tcp",
         "host": "192.0.2.5", "port": 502,
         "tags": [{"name": t, "address": "400001", "dtype": "int16"} for t in tags or ["T1"]]}]}]}


def _problems(data: dict) -> list[str]:
    return [t for _, t in name_rule_problems(DevicesFile.model_validate(data).channels)]


def test_clean_names_have_no_problems():
    assert _problems(_devices("Plant A", "PLC 1", ["Pumps.P1.Speed", "T-2 (x)"])) == []


@pytest.mark.parametrize(("data", "fragment"), [
    (_devices(channel="A.B"), "channel name 'A.B' contains '.'"),
    (_devices(channel="A/B"), "channel name 'A/B' contains '/'"),
    (_devices(channel=" A"), "channel name ' A' has leading or trailing whitespace"),
    (_devices(channel='A"'), "contains '\"'"),
    (_devices(device="P.1"), "device name 'P.1' contains '.'"),
    (_devices(device="PLC1 "), "device name 'PLC1 ' has leading or trailing whitespace"),
    (_devices(device="tags"), "device name 'tags' is used by the REST paths"),
    (_devices(tags=["A..B"]), "tag segment name is empty (in tag 'A..B')"),
    (_devices(tags=["A."]), "tag segment name is empty (in tag 'A.')"),
    (_devices(tags=["G. X"]), "tag segment name ' X' has leading or trailing whitespace"),
    (_devices(tags=['G"X']), "tag segment name 'G\"X' contains '\"'"),
])
def test_name_rule_problems(data, fragment):
    assert any(fragment in t for t in _problems(data)), _problems(data)


def test_problem_loc_points_at_the_name():
    loc, _ = name_rule_problems(DevicesFile.model_validate(_devices(tags=["T1", "A..B"])).channels)[0]
    assert loc == ("channels", 0, "devices", 0, "tags", 1, "name")


def test_loader_warns_and_keeps_the_names(tmp_path):
    (tmp_path / "devices.json").write_text(json.dumps(_devices("A.B", "PLC1 ", ["G..X"])), encoding="utf-8")
    loaded = load_config(tmp_path)
    assert [c.name for c in loaded.channels] == ["A.B"]                 # never changed
    assert loaded.channels[0].devices[0].name == "PLC1 "
    ws = [w for w in loaded.warnings if w.startswith("devices.json:") and "name" in w]
    assert any("channel name 'A.B' contains '.'" in w for w in ws)
    assert any("leading or trailing whitespace" in w for w in ws)
    assert any("tag segment name is empty" in w for w in ws)


# --- time zone (LOG-21) -----------------------------------------------------------

@pytest.mark.parametrize("name", ["Asia/Kolkata", "UTC", "Europe/Berlin", "America/New_York"])
def test_known_zones_are_accepted(name):
    assert PayloadCfg(timezone=name).timezone == name


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "IST", "../etc/passwd"])
def test_unknown_zone_is_refused_naming_the_key(name):
    with pytest.raises(ValidationError, match="payload.timezone"):
        PayloadCfg(timezone=name)


def test_shared_zone_fixture():
    fx = json.loads((Path(__file__).parents[1] / "conformance" / "timezones" / "zones.json").read_text("utf-8"))
    for name in fx["valid"]:
        assert PayloadCfg(timezone=name).timezone == name
    for name in fx["invalid"]:
        with pytest.raises(ValidationError, match="payload.timezone"):
            PayloadCfg(timezone=name)


# --- API writes -------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    config_dir = make_config(tmp_path)
    return TestClient(create_app(ConfigPoller(config_dir), api_cfg(tmp_path), environ=ENV)), config_dir


DEV_PATH = f"{V1}/devices/SITE MODBUS/Device1"


def test_api_refuses_new_bad_names_with_422_and_writes_nothing(env):
    c, cfg = env
    before = (cfg / "devices.json").read_bytes()
    tag = {"name": "G..X", "address": "400401", "dtype": "uint16"}
    r = c.post(f"{DEV_PATH}/tags", json=tag, headers=admin_w(c))
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"][-1] == "name" and "empty" in r.json()["detail"][0]["msg"]
    r = c.post(f"{DEV_PATH}/tags", json={**tag, "name": 'Say"Hi'}, headers=admin_w(c))
    assert r.status_code == 422 and "'\"'" in r.json()["detail"][0]["msg"]
    r = c.post(f"{V1}/channels", json={"name": "A.B"}, headers=admin_w(c))
    assert r.status_code == 422 and "contains '.'" in r.json()["detail"][0]["msg"]
    r = c.post(f"{V1}/channels", json={"name": "Padded "}, headers=admin_w(c))
    assert r.status_code == 422 and "whitespace" in r.json()["detail"][0]["msg"]
    assert (cfg / "devices.json").read_bytes() == before


def test_api_still_edits_a_file_that_already_has_a_bad_name(env):
    c, cfg = env
    doc = json.loads((cfg / "devices.json").read_text(encoding="utf-8"))
    doc["channels"][0]["devices"][0]["tags"][0]["name"] = "Old..Name"
    (cfg / "devices.json").write_text(json.dumps(doc), encoding="utf-8")
    r = c.post(f"{DEV_PATH}/tags", json={"name": "Fine", "address": "400401", "dtype": "uint16"},
               headers=admin_w(c))
    assert r.status_code in (200, 201), r.text


@pytest.mark.parametrize("name", ["A%B", "A#B", "A?B", "A&B", "A+B"])
def test_names_with_url_characters_round_trip(env, name):
    c, _ = env
    r = c.post(f"{V1}/channels", json={"name": name}, headers=admin_w(c))
    assert r.status_code in (200, 201), r.text
    got = c.get(f"{V1}/channels/{quote(name, safe='')}", headers=ADMIN)
    assert got.status_code == 200, got.text
    assert got.json()["name"] == name
