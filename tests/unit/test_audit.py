"""Audit log: JSON lines, newest first, filters, rotation."""

from __future__ import annotations

import json

from opencap.api import audit as audit_mod
from opencap.api.audit import AuditLog
from opencap.api.auth import Principal

ALICE = Principal("alice", "admin", "api")


def test_record_and_tail_newest_first(tmp_path):
    log = AuditLog(tmp_path / "sub" / "audit.jsonl")
    for i in range(300):                        # crosses the 64 KiB read blocks
        log.record(ALICE, "device.update" if i % 2 else "map.put", f"S/D{i}", revision=f"r{i}",
                   files=["devices.json"], changes=[{"path": "x" * 300, "old": i, "new": i + 1}])
    entries = log.tail(5)
    assert [e["target"] for e in entries] == ["S/D299", "S/D298", "S/D297", "S/D296", "S/D295"]
    assert entries[0]["who"] == "alice" and entries[0]["ts"].endswith("Z")
    assert [e["target"] for e in log.tail(2, 1, action="device")] == ["S/D297", "S/D295"]
    assert log.tail(1, target="D29")[0]["target"] == "S/D299"
    assert log.tail(1000, who="bob") == []
    assert len(log.tail(1000)) == 300
    lines = (tmp_path / "sub" / "audit.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["target"] == "S/D0"


def test_rotation_keeps_one_old_file_and_tail_reads_both(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "MAX_BYTES", 2000)
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(40):
        log.record(ALICE, "reload", f"t{i}")
    assert (tmp_path / "audit.jsonl.1").exists()
    targets = [e["target"] for e in log.tail(1000)]
    assert targets[0] == "t39" and targets == sorted(targets, key=lambda t: -int(t[1:]))


def test_disabled_log_records_nothing(tmp_path):
    log = AuditLog(None)
    assert log.record(ALICE, "reload", "poller")["action"] == "reload"
    assert log.tail() == []
