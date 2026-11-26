"""Project v2 (#37 part 1, ADR-0013): models, the v2 loader, `opencap config migrate`,
`import-kep --format v2`, and the config API's 409 on a v2 project."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from opencap.__main__ import app
from opencap.api.server import create_app
from opencap.config import migrate as migrate_mod
from opencap.config.kep_import import devices_json_text, import_kep, write_outputs
from opencap.config.loader import (
    DEVICES_FILE,
    MAP_FILE,
    NODE_FILE,
    OVERRIDES_FILE,
    POLLER_FILE,
    PROJECT_FILE,
    is_v2,
    load_config,
)
from opencap.config.migrate import MigrationError, migrate, plan_migration
from opencap.config.models import ApiCfg, ApiToken, Channel, Device, NodeConfig, ProjectV2, Tag
from opencap.config.store import V2_MESSAGE
from tests.helpers.fake_poller import ConfigPoller

SAMPLE = Path(__file__).parents[1] / "fixtures" / "project_sample.json"
runner = CliRunner()


def dev(name: str, channel: str = "Plant A", **kw) -> Device:
    tags = kw.pop("tags", [Tag(name="FLOW", address="400001", dtype="float32"),
                           Tag(name="LEVEL", address="400003", dtype="uint16")])
    return Device(id=f"{channel}/{name}", channel=channel, name=name, driver=kw.pop("driver", "modbus_tcp"),
                  host=kw.pop("host", "192.0.2.10"), port=kw.pop("port", 502), tags=tags, **kw)


def write_v1(root: Path, channels: list[Channel], *, poller: dict | None = None, dmap: dict | None = None,
             overrides: dict | None = None) -> Path:
    cfg = root / "config"
    cfg.mkdir(parents=True)
    from opencap.config.models import DevicesFile

    (cfg / DEVICES_FILE).write_text(devices_json_text(DevicesFile(channels=channels)), encoding="utf-8")
    if poller is not None:
        (cfg / POLLER_FILE).write_text("# the node\n" + yaml.safe_dump(poller), encoding="utf-8")
    if dmap is not None:
        (cfg / MAP_FILE).write_text(yaml.safe_dump(dmap), encoding="utf-8")
    if overrides is not None:
        (cfg / OVERRIDES_FILE).write_text(yaml.safe_dump(overrides), encoding="utf-8")
    return cfg


def layered_v1(tmp_path: Path) -> Path:
    """A v1 directory that uses every layering feature."""
    serial = dev("RTU1", channel="Line 2", driver="modbus_rtu", host="COM5", port=0,
                 params={"transport": "unmapped", "com_port": "COM5", "unit_id": 3})
    lost = dev("RTU2", channel="Line 2", driver="ab_df1", host="COM9", port=0,
               params={"transport": "unmapped", "com_port": "COM9"},
               tags=[Tag(name="N7_0", address="N7:0", dtype="int16")])
    channels = [
        Channel(name="Plant A", devices=[dev("PLC1"), dev("PLC2", publish={"topic": "custom/plc2"}), dev("PLC3")]),
        Channel(name="Line 2", devices=[serial, lost]),
        Channel(name="Spare", devices=[dev("PLC9", channel="Spare")]),
    ]
    return write_v1(
        tmp_path, channels,
        poller={"instance": "node-a", "mqtt": {"host": "192.0.2.5", "topic_prefix": "plant"}},
        dmap={"defaults": {"env": "test", "interval_ms": 5000},
              "devices": {"Plant A/PLC1": {"sub_device_id": "SUB_1", "topic": "map/plc1"},
                          "Plant A/PLC2": {"sub_device_id": "SUB_2", "stub": True},
                          "Line 2/RTU1": {"sub_device_id": "SUB_RTU", "env": "prod"},
                          "Gone/Device": {"sub_device_id": "SUB_X"}}},
        overrides={"channels": {"Spare": {"enabled": False}},
                   "devices": {"Plant A/PLC1": {"scan_ms": 500, "params": {"unit_id": 7},
                                                "tags": {"LEVEL": {"enabled": False, "scale": {"raw_hi": 4095}}},
                                                "publish": {"mode": "on_change", "deadband": 0.5}},
                               "Plant A/PLC3": {"enabled": False}},
                   "serial_ports": {"com5": "192.0.2.50:4001"}},
    )


def devices_of(loaded) -> dict[str, Device]:
    return {d.id: d for c in loaded.channels for d in c.devices}


# --- models ------------------------------------------------------------------------------------

def test_models():
    p = ProjectV2(channels=[Channel(name="A", devices=[dev("X", channel="A")])])
    assert (p.version, p.revision) == (2, 0)
    with pytest.raises(ValidationError):
        ProjectV2(version=1, channels=[])
    with pytest.raises(ValidationError):
        ProjectV2(revision=-1, channels=[])
    with pytest.raises(ValidationError, match="duplicate device id"):
        ProjectV2(channels=[Channel(name="A", devices=[dev("X", channel="A")]),
                            Channel(name="B", devices=[dev("X", channel="A")])])
    assert NodeConfig.model_validate({"instance": "n1", "mqtt": {"topic_prefix": "x"}}).mqtt.topic_prefix == "x"


# --- migration equivalence --------------------------------------------------------------------------

def test_layered_fixture_migrates_to_identical_devices(tmp_path):
    cfg = layered_v1(tmp_path)
    v1 = load_config(cfg)
    m = migrate(cfg)
    assert is_v2(cfg)
    v2 = load_config(cfg)
    assert devices_of(v2) == devices_of(v1)                                   # Device models compare equal
    assert [(c.name, c.enabled) for c in v2.channels] == [(c.name, c.enabled) for c in v1.channels]
    assert v2.poller.model_dump() == v1.poller.model_dump()
    d = devices_of(v2)
    # the layering is now in project.json ...
    plc1 = d["Plant A/PLC1"]
    assert (plc1.scan_ms, plc1.params["unit_id"], plc1.publish.sub_device_id, plc1.publish.topic) == (500, 7, "SUB_1", "map/plc1")
    assert plc1.publish.mode == "on_change" and plc1.publish.env == "test" and plc1.publish.interval_ms == 5000
    assert plc1.tags[1].enabled is False and plc1.tags[1].scale.raw_hi == 4095
    assert d["Plant A/PLC3"].enabled is False and not next(c for c in v2.channels if c.name == "Spare").enabled
    assert (d["Line 2/RTU1"].params["transport"], d["Line 2/RTU1"].host, d["Line 2/RTU1"].port) == ("tcp", "192.0.2.50", 4001)
    # ... and the finalize steps still run at load time
    assert d["Line 2/RTU2"].enabled is False and any("RTU2" in w and PROJECT_FILE in w for w in v2.warnings)
    assert d["Plant A/PLC3"].publish.topic == "plant/plant_a/plc3/data"
    project = json.loads((cfg / PROJECT_FILE).read_text(encoding="utf-8"))
    raw = {x["id"]: x for c in project["channels"] for x in c["devices"]}
    assert "topic" not in raw["Plant A/PLC3"]["publish"]                        # a default topic is not pinned
    assert raw["Line 2/RTU2"]["params"]["transport"] == "unmapped" and raw["Line 2/RTU2"]["enabled"] is True
    assert project["version"] == 2 and project["revision"] == 1
    # node.yaml is poller.yaml, comments kept; the v1 files are moved, not deleted
    assert (cfg / NODE_FILE).read_text(encoding="utf-8").startswith("# the node\n")
    assert sorted(p.name for p in (cfg / "v1-backup").iterdir()) == sorted([DEVICES_FILE, MAP_FILE, OVERRIDES_FILE, POLLER_FILE])
    assert not any((cfg / n).exists() for n in (DEVICES_FILE, MAP_FILE, OVERRIDES_FILE, POLLER_FILE))
    assert any("stub SubDeviceId" in w for w in m.v1.warnings)                  # reported once, at migrate time
    assert m.device_count == 6


def test_sample_project_migrates_to_identical_devices(tmp_path):
    cfg = tmp_path / "config"
    write_outputs(import_kep(SAMPLE), config_dir=cfg, reports_dir=tmp_path / "reports")
    v1 = load_config(cfg)
    migrate(cfg)
    v2 = load_config(cfg)
    assert devices_of(v2) == devices_of(v1) and len(devices_of(v1)) > 5
    assert [e.model_dump() for e in v2.excluded] == [e.model_dump() for e in v1.excluded]


def test_default_topics_follow_the_node_prefix_after_migration(tmp_path):
    cfg = layered_v1(tmp_path)
    migrate(cfg)
    node = yaml.safe_load((cfg / NODE_FILE).read_text(encoding="utf-8"))
    node["mqtt"]["topic_prefix"] = "site9"
    (cfg / NODE_FILE).write_text(yaml.safe_dump(node), encoding="utf-8")
    d = devices_of(load_config(cfg))
    assert d["Plant A/PLC3"].publish.topic == "site9/plant_a/plc3/data"
    assert d["Plant A/PLC1"].publish.topic == "map/plc1"                       # an explicit topic stays


def test_dry_run_writes_nothing(tmp_path):
    cfg = layered_v1(tmp_path)
    before = sorted(p.name for p in cfg.iterdir())
    m = migrate(cfg, dry_run=True)
    assert sorted(p.name for p in cfg.iterdir()) == before and not is_v2(cfg)
    assert json.loads(m.project_text)["version"] == 2


def test_refusals(tmp_path):
    with pytest.raises(MigrationError, match="nothing to migrate"):
        migrate(tmp_path)
    cfg = layered_v1(tmp_path / "a")
    migrate(cfg)
    with pytest.raises(MigrationError, match="already a project v2"):
        migrate(cfg)
    bad = write_v1(tmp_path / "b", [Channel(name="A", devices=[dev("X", channel="A")])],
                   overrides={"devices": {"A/X": {"scan_ms": "fast"}}})
    with pytest.raises(MigrationError, match="does not load"):
        migrate(bad)


def test_a_difference_aborts_before_writing(tmp_path, monkeypatch):
    cfg = layered_v1(tmp_path)
    real = migrate_mod.load_config

    def skewed(path):
        loaded = real(path)
        if is_v2(path):
            loaded.channels[0].devices[0].scan_ms += 1
        return loaded

    monkeypatch.setattr(migrate_mod, "load_config", skewed)
    with pytest.raises(MigrationError, match="device 'Plant A/PLC1' differs"):
        migrate(cfg)
    assert not is_v2(cfg) and (cfg / DEVICES_FILE).exists()


def test_a_crash_while_writing_leaves_v1_loadable(tmp_path, monkeypatch):
    cfg = layered_v1(tmp_path)
    v1 = load_config(cfg)
    from opencap.config import store

    real = store.atomic_write

    def crash_on_project(path, text, **kw):
        if Path(path).name == PROJECT_FILE:
            raise OSError("power cut")
        return real(path, text, **kw)

    monkeypatch.setattr(store, "atomic_write", crash_on_project)
    with pytest.raises(OSError):
        migrate(cfg)
    assert not is_v2(cfg) and (cfg / NODE_FILE).exists()                       # half done: node.yaml only
    assert devices_of(load_config(cfg)) == devices_of(v1)                       # still the v1 project
    monkeypatch.setattr(store, "atomic_write", real)
    migrate(cfg)                                                                # and it can be finished
    assert devices_of(load_config(cfg)) == devices_of(v1)


def test_backup_dir_is_never_overwritten(tmp_path):
    cfg = layered_v1(tmp_path)
    (cfg / "v1-backup").mkdir()
    m = migrate(cfg)
    assert all("v1-backup-2/" in line for line in m.moved)


# --- the v2 loader ----------------------------------------------------------------------------------

def write_v2(cfg: Path, project: ProjectV2, node: dict | None = None) -> Path:
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / PROJECT_FILE).write_text(devices_json_text(project), encoding="utf-8")
    if node is not None:
        (cfg / NODE_FILE).write_text(yaml.safe_dump(node), encoding="utf-8")
    return cfg


def test_v2_loader_finalize_rules_and_warnings(tmp_path):
    project = ProjectV2(channels=[Channel(name="A", devices=[
        dev("X", channel="A", publish={"sub_device_id": "S"}),
        dev("Y", channel="A", publish={"sub_device_id": "S", "topic": "t/y"}),
        dev("Z", channel="A", publish={"topic": "t/y"}),
    ])])
    cfg = write_v2(tmp_path / "config", project, {"mqtt": {"topic_prefix": "p"}})
    (cfg / DEVICES_FILE).write_text("{}", encoding="utf-8")
    text = (cfg / PROJECT_FILE).read_text(encoding="utf-8")
    loaded = load_config(cfg)
    d = devices_of(loaded)
    assert d["A/X"].publish.topic == "p/a/x/data"
    assert isinstance(loaded.poller, NodeConfig)
    assert loaded.warnings == [
        f"{DEVICES_FILE} ignored: {PROJECT_FILE} is the project (v2)",
        f"1 device(s) without SubDeviceId (publish.sub_device_id unset in {PROJECT_FILE}): A/Z",
        "MQTT topic 't/y' is used by 2 devices: A/Y, A/Z",
        "SubDeviceId 'S' is used by 2 devices: A/X, A/Y",
    ]
    assert (cfg / PROJECT_FILE).read_text(encoding="utf-8") == text            # loading never rewrites it
    assert project.channels[0].devices[0].publish.topic is None                  # nor the model it was given


def test_v2_loader_node_file_fallbacks_and_errors(tmp_path):
    project = ProjectV2(channels=[])
    cfg = write_v2(tmp_path / "a", project)
    (cfg / POLLER_FILE).write_text("instance: old-name\n", encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.poller.instance == "old-name" and any("rename it to node.yaml" in w for w in loaded.warnings)
    (cfg / NODE_FILE).write_text("instance: new-name\n", encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.poller.instance == "new-name" and any(f"{POLLER_FILE} ignored" in w for w in loaded.warnings)
    (cfg / PROJECT_FILE).write_text('{"version": 1, "channels": []}', encoding="utf-8")
    with pytest.raises(ValueError, match='needs "version": 2'):
        load_config(cfg)
    (cfg / PROJECT_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_config(cfg)


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_migrate(tmp_path):
    cfg = layered_v1(tmp_path)
    res = runner.invoke(app, ["config", "migrate", "--config", str(cfg), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "6 devices" in res.output and "dry run: would write" in res.output and not is_v2(cfg)
    res = runner.invoke(app, ["config", "migrate", "--config", str(cfg)])
    assert res.exit_code == 0, res.output
    assert is_v2(cfg)
    assert f"wrote {cfg / PROJECT_FILE}" in res.output and "moved devices.json -> v1-backup/devices.json" in res.output
    res = runner.invoke(app, ["config", "migrate", "--config", str(cfg)])
    assert res.exit_code == 1 and "nothing written" in res.output


def test_cli_import_kep_format_v2(tmp_path):
    cfg = tmp_path / "config"
    res = runner.invoke(app, ["import-kep", str(SAMPLE), "--config", str(cfg), "--reports", str(tmp_path / "r"),
                              "--format", "v2"])
    assert res.exit_code == 0, res.output
    assert is_v2(cfg) and (tmp_path / "r" / "import_summary.json").exists()
    reference = tmp_path / "ref"
    write_outputs(import_kep(SAMPLE), config_dir=reference, reports_dir=tmp_path / "r2")
    assert devices_of(load_config(cfg)) == devices_of(load_config(reference))
    res = runner.invoke(app, ["import-kep", str(SAMPLE), "--config", str(cfg), "--format", "v2"])
    assert res.exit_code == 1 and "not supported yet" in res.output
    res = runner.invoke(app, ["import-kep", str(SAMPLE), "--config", str(tmp_path / "x"), "--format", "v3"])
    assert res.exit_code == 2


# --- config API on a v2 project ----------------------------------------------------------------------

def test_config_api_answers_409_on_a_v2_project_and_runtime_routes_work(tmp_path):
    cfg = layered_v1(tmp_path)
    migrate(cfg)
    env = {"T_ADMIN": "adm1n-secret"}
    api = ApiCfg(tokens=[ApiToken(name="alice", role="admin", token_env="T_ADMIN")],
                 audit_log=str(tmp_path / "audit.jsonl"))
    c = TestClient(create_app(ConfigPoller(cfg), api, environ=env))
    h = {"Authorization": "Bearer adm1n-secret"}
    for method, path in (("get", "/api/v1/channels"), ("get", "/api/v1/devices/Plant A/PLC1/tags"),
                         ("post", "/api/v1/channels")):
        extra = {"json": {"name": "N"}, "headers": {**h, "If-Match": '"any"'}} if method == "post" else {"headers": h}
        r = getattr(c, method)(path, **extra)
        assert r.status_code == 409, (path, r.status_code, r.text)
        assert "project v2" in r.json()["detail"] and r.json()["detail"] == V2_MESSAGE
    assert c.get("/api/v1/devices/Plant A/PLC1/status", headers=h).status_code == 200
    assert c.get("/health").status_code == 200


def test_v2_naming_rules_are_errors_and_migrate_refuses_them(tmp_path):
    bad = dev("PLC1", tags=[Tag(name="_Spare", address="400001", dtype="uint16")])
    cfg = write_v2(tmp_path / "v2", ProjectV2(channels=[Channel(name="Plant A", devices=[bad])]))
    with pytest.raises(ValueError, match="reserved for system tags"):
        load_config(cfg)
    v1 = write_v1(tmp_path / "v1", [Channel(name="Plant A", devices=[bad])])
    assert any("reserved for system tags" in w for w in load_config(v1).warnings)    # only a warning in v1
    with pytest.raises(MigrationError, match="does not load"):
        migrate(v1)
    assert not is_v2(v1)


def test_project_name_and_title_reach_the_loaded_config(tmp_path):
    p = ProjectV2(name="Plant A demo", title="Line 1 and 2", channels=[Channel(name="A", devices=[dev("X", channel="A")])])
    loaded = load_config(write_v2(tmp_path / "config", p))
    assert (loaded.project_name, loaded.project_title) == ("Plant A demo", "Line 1 and 2")
    cfg = layered_v1(tmp_path / "m")
    migrate(cfg)
    loaded = load_config(cfg)
    assert (loaded.project_name, loaded.project_title) == (None, "")             # directory name, as in v1
