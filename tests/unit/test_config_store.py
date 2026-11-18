"""ConfigStore: CRUD on the config files, validation, atomic writes, comment-preserving YAML."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
import yaml

from opencap.config import yamledit
from opencap.config.kep_import import import_kep, write_outputs
from opencap.config.loader import load_config
from opencap.config.store import (
    EXPORT_FORMAT,
    ConfigStore,
    Conflict,
    Invalid,
    NotFound,
    PartialWrite,
    PreconditionFailed,
    StorageError,
    atomic_write,
    merge_patch,
)

SAMPLE = Path(__file__).parents[1] / "fixtures" / "project_sample.json"
DEV = "SITE MODBUS/Device1"

MAP_WITH_COMMENTS = """\
# devices_map.yaml (hand-owned)
defaults: {env: test}   # every device
devices:
  # the pump station
  SITE MODBUS/Device1:
    sub_device_id: SUB_ONE   # confirmed with the backend 2026-11-01
  SITE M800/8 PUMP:
    sub_device_id: SUB_TWO
    stub: true
# end of file
"""

OVERRIDES_WITH_COMMENTS = """\
# overrides.yaml: hand fixes
channels: {}
devices:
  SITE MODBUS/Device1:
    timeout_ms: 2000     # slow VPN at this site
    tags:
      FLOW_1: {scan_ms: 10000}   # nobody needs it faster
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    result = import_kep(SAMPLE)
    config_dir = tmp_path / "config"
    write_outputs(result, config_dir=config_dir, reports_dir=tmp_path / "reports")
    (config_dir / "devices_map.yaml").write_text(MAP_WITH_COMMENTS, encoding="utf-8")
    (config_dir / "overrides.yaml").write_text(OVERRIDES_WITH_COMMENTS, encoding="utf-8")
    return config_dir


@pytest.fixture
def store(cfg: Path) -> ConfigStore:
    return ConfigStore(cfg)


def devices_json(cfg: Path) -> dict:
    return json.loads((cfg / "devices.json").read_text(encoding="utf-8"))


def device(cfg: Path, dev_id: str) -> dict:
    return next(d for c in devices_json(cfg)["channels"] for d in c["devices"] if d["id"] == dev_id)


# --- helpers ---------------------------------------------------------------------------

def test_merge_patch_rfc7386():
    assert merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"b": {"c": None, "e": 4}, "f": [1]}) == \
        {"a": 1, "b": {"d": 3, "e": 4}, "f": [1]}
    assert merge_patch({"a": 1}, {"a": None}) == {}


def test_atomic_write_keeps_bak_and_mode(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("old\n")
    os.chmod(p, 0o640)
    atomic_write(p, "new\n")
    assert p.read_text() == "new\n"
    assert (tmp_path / "f.yaml.bak").read_text() == "old\n"
    assert oct(p.stat().st_mode & 0o777) == oct(0o640)
    atomic_write(p, "newer\n")
    assert (tmp_path / "f.yaml.bak").read_text() == "new\n"     # one generation
    assert sorted(x.name for x in tmp_path.iterdir()) == ["f.yaml", "f.yaml.bak"]   # no temp files left


def test_atomic_write_failure_leaves_the_file_alone(tmp_path, monkeypatch):
    p = tmp_path / "f.yaml"
    p.write_text("old\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, "new\n")
    monkeypatch.undo()
    assert p.read_text() == "old\n"
    assert [x.name for x in tmp_path.iterdir()] == ["f.yaml"]   # temp file cleaned up


# --- reads -----------------------------------------------------------------------------

def test_summary_and_reads(store: ConfigStore):
    s = store.summary()
    assert s["valid"] and s["counts"]["devices"] == 10 and s["counts"]["channels"] == 9
    assert "modbus_tcp" in s["drivers"] and "float32" in s["dtypes"]
    assert len(s["revision"]) == 16
    assert [c["name"] for c in store.list_channels()["channels"]][0] == "SITE MODBUS"
    assert store.list_devices()["revision"] == s["revision"]
    dev = store.get_device(DEV)
    assert dev["map_entry"] == {"sub_device_id": "SUB_ONE"}
    assert dev["override"]["timeout_ms"] == 2000
    assert store.get_tag(DEV, "FLOW_1")["override"] == {"scan_ms": 10000}
    with pytest.raises(NotFound):
        store.get_device("NOPE/X")
    with pytest.raises(NotFound):
        store.get_tag(DEV, "NOPE")


def test_revision_changes_with_any_file(store: ConfigStore, cfg: Path):
    r1 = store.revision()
    (cfg / "poller.yaml").write_text("instance: x\n")
    r2 = store.revision()
    assert r1 != r2
    (cfg / "poller.yaml").unlink()
    assert store.revision() == r1          # same bytes, same revision


# --- devices.json CRUD -----------------------------------------------------------------

def test_update_device_writes_devices_json_atomically(store: ConfigStore, cfg: Path):
    before = (cfg / "devices.json").read_text()
    res = store.update_device(DEV, {"timeout_ms": 1500}, partial=True)
    assert res.files == ["devices.json"]
    assert res.changes == [{"path": "timeout_ms", "old": 1000, "new": 1500}]
    assert device(cfg, DEV)["timeout_ms"] == 1500
    assert (cfg / "devices.json.bak").read_text() == before
    assert res.revision == store.revision()
    load_config(cfg)                                        # still loads


def test_validation_error_has_field_location_and_writes_nothing(store: ConfigStore, cfg: Path):
    before = {p.name: p.read_bytes() for p in cfg.iterdir()}
    with pytest.raises(Invalid) as exc:
        store.update_device(DEV, {"timeout_ms": "soon", "bogus": 1}, partial=True)
    locs = {tuple(e["loc"]) for e in exc.value.errors}
    assert ("timeout_ms",) in locs and ("bogus",) in locs
    assert {p.name: p.read_bytes() for p in cfg.iterdir()} == before


def test_driver_rejects_params_and_addresses(store: ConfigStore):
    with pytest.raises(Invalid) as exc:
        store.update_device(DEV, {"params": {"unit_id": 999}}, partial=True)
    assert exc.value.errors[0]["loc"] == ["params"] and "0..255" in exc.value.errors[0]["msg"]
    with pytest.raises(Invalid) as exc:
        store.create_tags(DEV, {"name": "X", "address": "999999", "dtype": "float32"})
    assert exc.value.errors[0]["loc"] == ["address"] and exc.value.errors[0]["type"] == "bad_config"
    # a disabled tag is not planned, so it may hold an address the driver cannot read yet
    store.create_tags(DEV, {"name": "X", "address": "999999", "dtype": "float32", "enabled": False})


def test_unimplemented_driver_is_a_warning(store: ConfigStore, monkeypatch):
    from opencap import drivers
    # pretend the s7 driver module is not there: its devices are kept, warned about, not polled
    monkeypatch.setitem(drivers.DRIVERS, "s7", "opencap.drivers._not_built_yet:Nope")
    res = store.update_device("SITE S7/PLC", {"timeout_ms": 1000}, partial=True)
    assert any("SITE S7/PLC" in w and "not polled" in w for w in res.warnings)
    monkeypatch.undo()
    res = store.update_device("SITE S7/PLC", {"timeout_ms": 1500}, partial=True)
    assert not any("SITE S7/PLC" in w and "not polled" in w for w in res.warnings)   # s7 exists (#60)


def test_put_device_replaces_and_requires_tags(store: ConfigStore, cfg: Path):
    dev = store.get_device(DEV)["device"]
    with pytest.raises(Invalid) as exc:
        store.update_device(DEV, {k: v for k, v in dev.items() if k not in ("tags", "id")}, partial=False)
    assert ["tags"] in [e["loc"] for e in exc.value.errors]
    body = {**dev, "retries": 0, "tags": dev["tags"][:2]}
    store.update_device(DEV, body, partial=False)
    assert device(cfg, DEV)["retries"] == 0 and len(device(cfg, DEV)["tags"]) == 2


def test_id_is_derived_from_channel_and_name(store: ConfigStore):
    with pytest.raises(Invalid) as exc:
        store.update_device(DEV, {"id": "SITE MODBUS/Other"}, partial=True)
    assert exc.value.errors[0]["loc"] == ["id"]
    with pytest.raises(Invalid):
        store.create_device("SITE MODBUS", {"name": "a/b", "driver": "modbus_tcp", "host": "192.0.2.1", "port": 502})


def test_create_and_delete_channel_and_device(store: ConfigStore, cfg: Path):
    store.create_channel({"name": "NEW SITE"})
    with pytest.raises(Conflict):
        store.create_channel({"name": "NEW SITE"})
    res = store.create_device("NEW SITE", {"name": "PLC", "driver": "modbus_tcp", "host": "192.0.2.5", "port": 502,
                                           "tags": [{"name": "A", "address": "400001", "dtype": "uint16"}]})
    assert res.target == "NEW SITE/PLC"
    assert any("without SubDeviceId" in w for w in res.warnings)
    with pytest.raises(Conflict):
        store.create_device("NEW SITE", {"name": "PLC", "driver": "modbus_tcp", "host": "192.0.2.5", "port": 502})
    with pytest.raises(NotFound):
        store.create_device("NO SUCH", {"name": "PLC", "driver": "modbus_tcp", "host": "192.0.2.5", "port": 502})
    store.put_map_entry("NEW SITE/PLC", {"sub_device_id": "SUB_NEW"}, partial=False)
    res = store.delete_channel("NEW SITE")
    assert set(res.files) == {"devices.json", "devices_map.yaml"}       # cascade
    assert "NEW SITE/PLC" not in yaml.safe_load((cfg / "devices_map.yaml").read_text())["devices"]
    assert res.changes[0]["old"]["refs"]["NEW SITE/PLC"]["map_entry"] == {"sub_device_id": "SUB_NEW"}


def test_rename_device_moves_map_and_override_keeping_comments(store: ConfigStore, cfg: Path):
    res = store.update_device(DEV, {"name": "Device9"}, partial=True)
    assert set(res.files) == {"devices.json", "devices_map.yaml", "overrides.yaml"}
    new_id = "SITE MODBUS/Device9"
    assert device(cfg, new_id)["name"] == "Device9"
    map_text = (cfg / "devices_map.yaml").read_text()
    assert "SITE MODBUS/Device9:\n    sub_device_id: SUB_ONE   # confirmed with the backend 2026-11-01" in map_text
    assert "# the pump station" in map_text and "# end of file" in map_text
    assert map_text.index("Device9") < map_text.index("8 PUMP")             # position kept
    ov_text = (cfg / "overrides.yaml").read_text()
    assert "timeout_ms: 2000     # slow VPN at this site" in ov_text
    loaded = load_config(cfg)
    dev = next(d for c in loaded.channels for d in c.devices if d.id == new_id)
    assert dev.publish.sub_device_id == "SUB_ONE" and dev.timeout_ms == 2000


def test_move_device_to_another_channel(store: ConfigStore, cfg: Path):
    store.update_device(DEV, {"channel": "SITE LOGIX"}, partial=True)
    assert device(cfg, "SITE LOGIX/Device1")["channel"] == "SITE LOGIX"
    with pytest.raises(Invalid):
        store.update_device("SITE LOGIX/Device1", {"channel": "NOWHERE"}, partial=True)


def test_rename_channel_renames_every_device_id(store: ConfigStore, cfg: Path):
    store.put_channel_override("SITE MODBUS", {"enabled": False})
    store.update_channel("SITE MODBUS", {"name": "SITE MB"})
    assert device(cfg, "SITE MB/Device1")["channel"] == "SITE MB"
    ov = yaml.safe_load((cfg / "overrides.yaml").read_text())
    assert ov["channels"] == {"SITE MB": {"enabled": False}} and "SITE MB/Device1" in ov["devices"]
    with pytest.raises(Invalid):
        store.update_channel("SITE MB", {"devices": []})


def test_tags_crud_carries_tag_overrides(store: ConfigStore, cfg: Path):
    store.create_tags(DEV, [{"name": "T1", "address": "400101", "dtype": "uint16"},
                            {"name": "T2", "address": "400102", "dtype": "uint16"}])
    with pytest.raises(Conflict):
        store.create_tags(DEV, {"name": "T1", "address": "400103", "dtype": "uint16"})
    with pytest.raises(Invalid):
        store.create_tags(DEV, [{"name": "D", "address": "400103", "dtype": "uint16"}] * 2)
    store.update_tag(DEV, "FLOW_1", {"name": "FLOW_MAIN"}, partial=True)   # has a tag override
    ov = yaml.safe_load((cfg / "overrides.yaml").read_text())["devices"][DEV]["tags"]
    assert ov == {"FLOW_MAIN": {"scan_ms": 10000}}
    text = (cfg / "overrides.yaml").read_text()
    assert "FLOW_MAIN: {scan_ms: 10000}" in text and "# nobody needs it faster" in text   # comment kept
    res = store.update_tag(DEV, "T1", {"scan_ms": 5000, "description": "x"}, partial=True)
    assert {c["path"] for c in res.changes} == {"scan_ms", "description"}
    store.delete_tag(DEV, "FLOW_MAIN")
    assert "tags" not in yaml.safe_load((cfg / "overrides.yaml").read_text())["devices"][DEV] or \
        yaml.safe_load((cfg / "overrides.yaml").read_text())["devices"][DEV]["tags"] == {}
    with pytest.raises(NotFound):
        store.delete_tag(DEV, "FLOW_MAIN")


# --- devices_map.yaml / overrides.yaml -------------------------------------------------

def test_map_entry_crud_preserves_comments(store: ConfigStore, cfg: Path):
    res = store.put_map_entry("SITE M800/8 PUMP", {"sub_device_id": "SUB_TWO", "topic": "a/b"}, partial=False)
    assert res.files == ["devices_map.yaml"] and not res.comments_lost
    text = (cfg / "devices_map.yaml").read_text()
    assert "# devices_map.yaml (hand-owned)" in text and "# confirmed with the backend 2026-11-01" in text
    assert "stub" not in yaml.safe_load(text)["devices"]["SITE M800/8 PUMP"]     # PUT replaces
    store.put_map_entry("SITE M800/8 PUMP", {"env": "staging"}, partial=True)
    assert yaml.safe_load((cfg / "devices_map.yaml").read_text())["devices"]["SITE M800/8 PUMP"] == \
        {"sub_device_id": "SUB_TWO", "topic": "a/b", "env": "staging"}
    with pytest.raises(Invalid):
        store.put_map_entry("SITE M800/8 PUMP", {"topic": "x"}, partial=False)      # sub_device_id required
    with pytest.raises(NotFound):
        store.put_map_entry("NOPE/X", {"sub_device_id": "S"}, partial=False)
    store.delete_map_entry("SITE M800/8 PUMP")
    with pytest.raises(NotFound):
        store.delete_map_entry("SITE M800/8 PUMP")


def test_map_defaults_validated_against_publish_settings(store: ConfigStore, cfg: Path):
    store.put_map_defaults({"env": "prod", "interval_ms": "5000"})
    assert yaml.safe_load((cfg / "devices_map.yaml").read_text())["defaults"] == {"env": "prod", "interval_ms": 5000}
    with pytest.raises(Invalid) as exc:
        store.put_map_defaults({"sub_device": "x", "mode": "sometimes"})
    assert {tuple(e["loc"]) for e in exc.value.errors} == {("sub_device",), ("mode",)}


def test_override_crud(store: ConfigStore, cfg: Path):
    with pytest.raises(Invalid):
        store.put_device_override(DEV, {"timeout_ms": "x"}, partial=False)
    with pytest.raises(Invalid) as exc:   # the override changes an address to one the driver rejects
        store.put_device_override(DEV, {"tags": {"FLOW_1": {"address": "999999"}}}, partial=True)
    assert exc.value.errors[0]["loc"] == ["address"]
    store.put_device_override(DEV, {"retries": 0}, partial=True)
    ov = yaml.safe_load((cfg / "overrides.yaml").read_text())["devices"][DEV]
    assert ov == {"timeout_ms": 2000, "tags": {"FLOW_1": {"scan_ms": 10000}}, "retries": 0}
    store.put_device_override(DEV, {"tags": {"FLOW_1": None}}, partial=True)        # merge patch delete
    assert yaml.safe_load((cfg / "overrides.yaml").read_text())["devices"][DEV]["tags"] == {}
    store.delete_device_override(DEV)
    assert DEV not in (yaml.safe_load((cfg / "overrides.yaml").read_text())["devices"] or {})
    assert "# overrides.yaml: hand fixes" in (cfg / "overrides.yaml").read_text()
    with pytest.raises(NotFound):
        store.put_device_override(DEV, {"retries": 1}, partial=True)                # PATCH needs one


def test_channel_override(store: ConfigStore, cfg: Path):
    with pytest.raises(Invalid):
        store.put_channel_override("SITE MODBUS", {"enabled": "maybe"})
    with pytest.raises(Invalid):
        store.put_channel_override("SITE MODBUS", {"enabled": True, "x": 1})
    store.put_channel_override("SITE MODBUS", {"enabled": False})
    assert not next(c for c in load_config(cfg).channels if c.name == "SITE MODBUS").enabled
    store.delete_channel_override("SITE MODBUS")
    with pytest.raises(NotFound):
        store.put_channel_override("NOPE", {"enabled": True})


def test_missing_yaml_files_are_created_from_templates(store: ConfigStore, cfg: Path):
    (cfg / "overrides.yaml").unlink()
    store.put_device_override(DEV, {"retries": 0}, partial=False)
    text = (cfg / "overrides.yaml").read_text()
    assert "hand fixes applied on top of devices.json" in text            # template comments kept
    doc = yaml.safe_load(text)
    assert doc["channels"] == {} and doc["devices"] == {DEV: {"retries": 0}}


def test_if_match(store: ConfigStore):
    rev = store.revision()
    store.update_device(DEV, {"retries": 1}, partial=True, if_match=f'"{rev}"')
    with pytest.raises(PreconditionFailed):
        store.update_device(DEV, {"retries": 0}, partial=True, if_match=rev)    # stale now
    with pytest.raises(Invalid, match="reserved"):
        store.create_device("SITE MODBUS", {"name": "tags", "driver": "modbus_tcp", "host": "192.0.2.1", "port": 502})


def test_broken_files_block_changes(store: ConfigStore, cfg: Path):
    (cfg / "overrides.yaml").write_text("devices: [oops\n")
    with pytest.raises(Conflict, match="overrides.yaml"):
        store.update_device(DEV, {"retries": 1}, partial=True)
    assert store.summary()["valid"] is False


def test_comment_fallback_is_reported(store: ConfigStore, cfg: Path, monkeypatch):
    monkeypatch.setattr(yamledit, "YAML", None)        # as if ruamel.yaml could not keep them
    res = store.put_map_entry(DEV, {"sub_device_id": "SUB_X"}, partial=False)
    assert res.comments_lost == ["devices_map.yaml"]
    text = (cfg / "devices_map.yaml").read_text()
    assert text.startswith("# devices_map.yaml (hand-owned)\n# Rewritten by the opencap config API")
    assert yaml.safe_load(text)["devices"][DEV] == {"sub_device_id": "SUB_X"}


def test_yamledit_quotes_strings_pyyaml_would_misread():
    text, preserved = yamledit.render("devices: {}\n", {"devices": {"A/B": {"sub_device_id": "off", "env": "1e3"}}},
                                      file_name="m.yaml")
    assert preserved and yaml.safe_load(text) == {"devices": {"A/B": {"sub_device_id": "off", "env": "1e3"}}}


# --- import / export -------------------------------------------------------------------

def test_import_kep_dry_run_writes_nothing(store: ConfigStore, cfg: Path):
    store.update_device(DEV, {"name": "Renamed"}, partial=True)
    before = {p.name: p.read_bytes() for p in cfg.iterdir()}
    report = store.import_config(SAMPLE.read_bytes(), dry_run=True, filename="project.json")
    assert report["kind"] == "kepserver" and report["dry_run"]
    dj = report["devices_json"]
    assert [d["id"] for d in dj["devices_added"]] == [DEV]
    assert [d["id"] for d in dj["devices_removed"]] == ["SITE MODBUS/Renamed"]
    stubs = report["devices_map"]["stubs_added"]
    assert DEV in stubs and "SITE M800/8 PUMP" not in stubs and len(stubs) == 9
    assert set(report["files"]) == {"devices.json", "devices_map.yaml"}
    assert report["import_summary"]["included"]["devices"] == 10
    assert {p.name: p.read_bytes() for p in cfg.iterdir()} == before


def test_import_kep_apply(store: ConfigStore, cfg: Path):
    store.delete_device("SITE PCCC/7 PUMP")
    rev = store.revision()
    report = store.import_config(SAMPLE.read_bytes(), dry_run=False, filename="project.json", if_match=rev)
    res = report["result"]
    assert "devices.json" in res.files and res.revision == store.revision()
    assert device(cfg, "SITE PCCC/7 PUMP")["driver"] == "ab_pccc"
    map_doc = yaml.safe_load((cfg / "devices_map.yaml").read_text())
    assert map_doc["devices"]["SITE PCCC/7 PUMP"]["stub"] is True
    assert map_doc["devices"][DEV] == {"sub_device_id": "SUB_ONE"}               # kept
    assert "# confirmed with the backend 2026-11-01" in (cfg / "devices_map.yaml").read_text()
    with pytest.raises(PreconditionFailed):
        store.import_config(SAMPLE.read_bytes(), dry_run=False, if_match=rev)


def test_import_rejects_junk(store: ConfigStore):
    with pytest.raises(Invalid):
        store.import_config(b"not json")
    with pytest.raises(Invalid):
        store.import_config(b'{"something": 1}')
    with pytest.raises(Invalid):
        store.import_config(json.dumps({"format": EXPORT_FORMAT, "files": {"../evil": "x"}}).encode())


def test_export_import_round_trip(store: ConfigStore, cfg: Path):
    exported = store.export()
    assert exported["format"] == EXPORT_FORMAT
    assert set(exported["files"]) == {"devices.json", "devices_map.yaml", "overrides.yaml"}
    store.update_device(DEV, {"retries": 0}, partial=True)
    report = store.import_config(json.dumps(exported).encode(), dry_run=True)
    assert report["kind"] == "opencap-export" and report["files"] == ["devices.json"]
    assert report["devices_json"]["devices_changed"][0]["fields"] == [{"path": "retries", "old": 0, "new": 2}]
    report = store.import_config(json.dumps(exported).encode(), dry_run=False)
    assert store.revision() == exported["revision"]                         # byte-for-byte restore


def test_export_import_validates(store: ConfigStore, cfg: Path):
    bad = {"format": EXPORT_FORMAT, "files": {"overrides.yaml": "devices:\n  X/Y: {timeout_ms: soon}\n"}}
    with pytest.raises(Invalid) as exc:
        store.import_config(json.dumps(bad).encode(), dry_run=True)
    assert exc.value.errors[0]["loc"][0] == "overrides.yaml"


def test_export_restores_over_a_broken_file(store: ConfigStore, cfg: Path):
    exported = store.export()
    (cfg / "devices.json").write_text("{ broken")
    store.import_config(json.dumps(exported).encode(), dry_run=False)
    assert store.summary()["valid"]


def test_empty_names_hosts_and_addresses_are_rejected(store: ConfigStore):
    for body in ({"name": "", "address": "400001", "dtype": "uint16"}, {"name": "X", "address": " ", "dtype": "uint16"}):
        with pytest.raises(Invalid):
            store.create_tags(DEV, body)
    with pytest.raises(Invalid) as exc:
        store.update_device(DEV, {"host": ""}, partial=True)
    assert exc.value.errors[0]["loc"] == ["host"]
    with pytest.raises(Invalid):
        store.create_channel({"name": "  "})


# --- storage failures: all or nothing (review of #63) ----------------------------------

def _snapshot(cfg: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in cfg.iterdir() if not p.name.endswith(".bak")}


def test_a_failed_multi_file_write_changes_nothing(store: ConfigStore, cfg: Path, monkeypatch):
    """Device rename = devices.json + devices_map.yaml + overrides.yaml. The second rename fails:
    the first file gets its old bytes back, nothing is audited, and the caller gets a 503."""
    from opencap.config import store as store_mod

    before = _snapshot(cfg)
    audited = []
    store.on_commit = lambda result, who, detail: audited.append(result)
    real_replace = os.replace

    def flaky_replace(src, dst):
        if Path(dst).name == "devices_map.yaml" and Path(src).name.startswith(".devices_map.yaml."):
            raise OSError(errno.EIO, "Input/output error", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", flaky_replace)
    with pytest.raises(StorageError) as exc:
        store.update_device(DEV, {"name": "Device9"}, partial=True)
    assert exc.value.status == 503 and "nothing was changed" in exc.value.message
    assert "devices_map.yaml" in exc.value.message
    monkeypatch.setattr(store_mod.os, "replace", real_replace)
    assert _snapshot(cfg) == before                        # byte for byte, no temp files left
    assert audited == []
    load_config(cfg)


def test_a_failed_rollback_is_reported_and_audited_as_partial(store: ConfigStore, cfg: Path, monkeypatch):
    from opencap.config import store as store_mod

    audited = []
    store.on_commit = lambda result, who, detail: audited.append((result, detail))
    real_replace = os.replace
    renamed = []

    def broken_disk(src, dst):
        name = Path(dst).name
        if name == "devices_map.yaml" and Path(src).name.startswith(".devices_map.yaml."):
            raise OSError(errno.EIO, "Input/output error", str(dst))
        if name == "devices.json" and "devices.json" in renamed:        # the rollback write
            raise OSError(errno.EIO, "Input/output error", str(dst))
        real_replace(src, dst)
        if name == "devices.json":
            renamed.append(name)

    monkeypatch.setattr(store_mod.os, "replace", broken_disk)
    with pytest.raises(PartialWrite) as exc:
        store.update_device(DEV, {"name": "Device9"}, partial=True)
    assert exc.value.status == 500 and exc.value.files == ["devices.json"]
    assert audited and audited[0][0].action == "device.update.partial"
    assert audited[0][0].files == ["devices.json"]


def test_read_only_and_full_disks_are_503_and_507(store: ConfigStore, cfg: Path, monkeypatch):
    from opencap.config import store as store_mod

    before = _snapshot(cfg)
    for code, status in ((errno.EROFS, 503), (errno.EACCES, 503), (errno.ENOSPC, 507)):
        def fail(*_a, code=code, **_k):
            raise OSError(code, os.strerror(code), str(cfg / "devices.json"))

        monkeypatch.setattr(store_mod.os, "replace", fail)
        with pytest.raises(StorageError) as exc:
            store.update_device(DEV, {"retries": 0}, partial=True)
        assert exc.value.status == status and str(cfg) in exc.value.message
        monkeypatch.undo()
    assert _snapshot(cfg) == before


def test_an_audit_failure_rolls_the_change_back(store: ConfigStore, cfg: Path):
    before = _snapshot(cfg)

    def audit_down(result, who, detail):
        raise OSError(errno.EROFS, "Read-only file system", "data/audit.jsonl")

    store.on_commit = audit_down
    with pytest.raises(StorageError) as exc:
        store.update_device(DEV, {"name": "Device9"}, partial=True)
    assert "rolled back" in exc.value.message
    assert _snapshot(cfg) == before


def test_writable_reports_why_not(store: ConfigStore, cfg: Path, monkeypatch):
    from opencap.config import store as store_mod

    assert store.writable()["writable"] is True
    monkeypatch.setattr(store_mod.os, "access", lambda path, mode: Path(path) != cfg)
    w = store.writable()
    assert w["writable"] is False and str(cfg) in w["reason"]
    monkeypatch.undo()

    class RO:
        f_flag = os.ST_RDONLY

    monkeypatch.setattr(store_mod.os, "statvfs", lambda path: RO())
    assert "read-only filesystem" in store.writable()["reason"]


def test_kep_import_appends_stubs_like_the_cli_or_writes_a_side_file(store: ConfigStore, cfg: Path):
    store.delete_device("SITE PCCC/7 PUMP")
    before = (cfg / "devices_map.yaml").read_text()
    report = store.import_config(SAMPLE.read_bytes(), dry_run=False, filename="project.json")
    text = (cfg / "devices_map.yaml").read_text()
    assert text.startswith(before)                               # appended, never rewritten
    assert "# stubs added by the opencap config API import on" in text
    assert report["devices_map"]["status"].startswith("appended")
    # a layout that cannot be appended to safely: flow-style devices mapping
    (cfg / "devices_map.yaml").write_text("defaults: {}\ndevices: {SITE MODBUS/Device1: {sub_device_id: S1}}\n")
    store.delete_device("SITE LOGIX/STP")
    report = store.import_config(SAMPLE.read_bytes(), dry_run=False, filename="project.json")
    assert (cfg / "devices_map.yaml").read_text() == "defaults: {}\ndevices: {SITE MODBUS/Device1: {sub_device_id: S1}}\n"
    side = yaml.safe_load((cfg / "devices_map.stubs.yaml").read_text())
    assert "SITE LOGIX/STP" in side["devices"] and report["devices_map"]["side_file"] == "devices_map.stubs.yaml"
    assert any("devices_map.stubs.yaml" in w for w in report["result"].warnings)


def test_export_import_audits_text_diffs(store: ConfigStore, cfg: Path):
    exported = store.export()
    (cfg / "poller.yaml").write_text("instance: before\n")
    exported["files"]["poller.yaml"] = "instance: after\n"
    audited = []
    store.on_commit = lambda result, who, detail: audited.append((result, detail))
    store.import_config(json.dumps(exported).encode(), dry_run=False)
    result, detail = audited[0]
    assert detail == {"kind": "opencap-export"}
    note = next(c for c in result.changes if c["path"] == "poller.yaml")
    assert "-instance: before" in note["diff"] and "+instance: after" in note["diff"]
