"""SQLite store-and-forward outbox (PLAN 7.3)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from opencap.metrics import REGISTRY
from opencap.north.saf import OutMsg, StoreAndForward, message_size

SRC = Path(__file__).resolve().parents[2] / "src"


def dropped_total() -> float:
    return REGISTRY.get_sample_value("saf_dropped_total") or 0.0


def queue_gauge() -> float:
    return REGISTRY.get_sample_value("saf_queue_messages")


@pytest.fixture
def saf(tmp_path):
    s = StoreAndForward(tmp_path / "saf.sqlite", max_bytes=10_000_000)
    yield s
    s.close()


def test_put_peek_in_order(saf):
    ids = [saf.put(f"t/{i}", f"p{i}".encode()) for i in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5
    saf.put("s/status", b'{"state":"ok"}', qos=1, retain=True)
    msgs = saf.peek(10)
    assert [m.topic for m in msgs] == ["t/0", "t/1", "t/2", "t/3", "t/4", "s/status"]
    assert msgs[0] == OutMsg(ids[0], "t/0", b"p0", 1, False)
    assert msgs[-1].retain is True and msgs[-1].qos == 1
    assert [m.id for m in saf.peek(2)] == ids[:2]           # limit, oldest first, nothing removed
    assert saf.count() == 6
    assert queue_gauge() == 6


def test_ack_deletes_only_acked(saf):
    ids = [saf.put("t", bytes([i]) * 10) for i in range(6)]
    assert saf.size_bytes() == 6 * (1 + 10)
    saf.ack([ids[1], ids[3]])
    assert [m.id for m in saf.peek()] == [ids[0], ids[2], ids[4], ids[5]]
    assert saf.count() == 4 and saf.size_bytes() == 4 * 11
    saf.ack([ids[1], 999_999])                                # already acked / unknown: ignored
    assert saf.count() == 4 and saf.size_bytes() == 44
    saf.ack(i for i in ids)                                   # any iterable
    assert saf.count() == 0 and saf.size_bytes() == 0 and saf.peek() == []
    assert queue_gauge() == 0
    saf.ack([])


def test_ack_many_ids(saf):
    ids = [saf.put("t", b"x") for _ in range(1234)]
    saf.ack(ids[:-1])
    assert [m.id for m in saf.peek()] == [ids[-1]]
    assert saf.count() == 1


def test_wal_and_synchronous_normal(saf):
    db = sqlite3.connect(saf.path)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        db.close()
    assert saf.db.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL


def test_cap_drops_oldest_and_counts(tmp_path):
    topic, payload = "op/s/d/data", b"x" * 89                          # 100 bytes per message
    assert message_size(topic, payload) == 100
    s = StoreAndForward(tmp_path / "cap.sqlite", max_bytes=1000)
    before = dropped_total()
    ids = [s.put(topic, payload) for _ in range(15)]
    assert s.count() == 10 and s.size_bytes() == 1000
    assert [m.id for m in s.peek(100)] == ids[5:]                     # the 5 oldest went
    assert dropped_total() - before == 5
    assert queue_gauge() == 10
    # a bigger message pushes out as many old ones as needed
    s.put(topic, b"y" * 289)                                           # 300 bytes
    assert s.count() == 8 and s.size_bytes() == 1000
    assert [m.id for m in s.peek(100)][0] == ids[8]
    assert dropped_total() - before == 8
    s.close()


def test_message_larger_than_cap_is_dropped(tmp_path):
    s = StoreAndForward(tmp_path / "cap.sqlite", max_bytes=50)
    before = dropped_total()
    s.put("t", b"a" * 10)
    s.put("t", b"b" * 100)
    assert s.count() == 0 and s.size_bytes() == 0
    assert dropped_total() - before == 2
    s.close()


def test_reopen_continues(tmp_path):
    path = tmp_path / "saf.sqlite"
    s = StoreAndForward(path, max_bytes=1_000_000)
    ids = [s.put("t", f"{i}".encode(), retain=(i == 2)) for i in range(4)]
    s.ack([ids[0], ids[3]])                                 # the newest id is acked too
    s.close()
    s.close()                                               # idempotent
    with pytest.raises(RuntimeError):
        s.peek()

    s2 = StoreAndForward(path, max_bytes=1_000_000)
    assert s2.count() == 2 and s2.size_bytes() == 2 * 2
    assert s2.peek() == [OutMsg(ids[1], "t", b"1", 1, False), OutMsg(ids[2], "t", b"2", 1, True)]
    new = s2.put("t", b"4")
    assert new > ids[3]                                      # ids never go back (AUTOINCREMENT)
    assert [m.id for m in s2.peek()] == [ids[1], ids[2], new]
    s2.close()


def test_reopen_with_smaller_cap_trims(tmp_path):
    path = tmp_path / "saf.sqlite"
    s = StoreAndForward(path, max_bytes=1_000_000)
    ids = [s.put("t", b"x" * 9) for _ in range(10)]         # 10 bytes each
    s.close()
    before = dropped_total()
    s2 = StoreAndForward(path, max_bytes=35)
    assert [m.id for m in s2.peek()] == ids[7:]
    assert dropped_total() - before == 7
    s2.close()


def test_survives_process_kill(tmp_path):
    """Committed puts survive kill -9 of the process (WAL, synchronous=NORMAL)."""
    path = tmp_path / "saf.sqlite"
    script = textwrap.dedent(f"""
        import os, signal, sys
        sys.path.insert(0, {str(SRC)!r})
        from opencap.north.saf import StoreAndForward
        s = StoreAndForward({str(path)!r}, max_bytes=10_000_000)
        ids = [s.put("op/s/d/data", b"%d" % i) for i in range(200)]
        s.ack(ids[:50])
        os.kill(os.getpid(), signal.SIGKILL)
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert proc.returncode == -9, proc.stderr.decode()
    s = StoreAndForward(path, max_bytes=10_000_000)
    msgs = s.peek(1000)
    assert [m.payload for m in msgs] == [b"%d" % i for i in range(50, 200)]
    s.close()
