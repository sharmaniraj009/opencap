"""Store-and-forward outbox in SQLite (PLAN.md section 7.3).

**Outbox-always design.** Every outgoing message (data and status) is written here first.
The MQTT link drains the table strictly in ``id`` order and deletes a row only after the
broker has acknowledged it (QoS 1 PUBACK). This gives, with one mechanism:

- ordering: replay after an outage is in the original order, and new messages queue behind
  the backlog instead of overtaking it;
- no loss across broker outages (nothing is deleted before the PUBACK);
- survival across a process restart or crash: the file is reopened and draining continues.

Durability: WAL journal with ``synchronous=NORMAL``. A committed ``put()`` survives a crash
of the process (kill -9). After an OS crash or power cut, the last transactions since the
latest WAL checkpoint can be lost; that is the price of not calling fsync per message.

Size cap: ``max_bytes`` limits the sum of ``len(topic) + len(payload)`` (UTF-8 bytes) of the
queued messages. When a ``put()`` goes over it, the **oldest** rows are dropped and counted
in ``SAF_DROPPED``. The SQLite file itself is somewhat larger than this logical size (row
and page overhead, WAL); free pages are reused, the file does not shrink.

Not thread-safe: one owner (the event loop thread). Each call is a short local SQLite
transaction (tens of microseconds), so it is called directly from async code.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import structlog

from opencap.metrics import SAF_QUEUE, count_saf_dropped

log = structlog.get_logger("opencap.north.saf")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,   -- AUTOINCREMENT: ids never go back, even when empty
    topic   TEXT    NOT NULL,
    payload BLOB    NOT NULL,
    qos     INTEGER NOT NULL,
    retain  INTEGER NOT NULL,
    size    INTEGER NOT NULL,                    -- len(topic utf-8) + len(payload)
    created REAL    NOT NULL                     -- unix time of put()
)
"""
_CHUNK = 500          # max ids per IN (...) statement
_DROP_LOG_EVERY_S = 60.0


@dataclass(frozen=True, slots=True)
class OutMsg:
    id: int
    topic: str
    payload: bytes
    qos: int
    retain: bool


def message_size(topic: str, payload: bytes) -> int:
    return len(topic.encode("utf-8")) + len(payload)


class StoreAndForward:
    def __init__(self, path: str | Path, max_bytes: int) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self._db: sqlite3.Connection | None = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            log.warning("saf_not_wal", path=str(self.path), journal_mode=mode)
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(_SCHEMA)
        count, size = self._db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM outbox").fetchone()
        self._count = int(count)
        self._bytes = int(size)
        self._dropped_unlogged = 0
        self._drop_logged_at = float("-inf")
        if self._count:
            log.info("saf_reopened", path=str(self.path), messages=self._count, bytes=self._bytes)
        self._trim()                                    # max_bytes may have been lowered
        SAF_QUEUE.set(self._count)

    # ------------------------------------------------------------------ helpers
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("StoreAndForward is closed")
        return self._db

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def _trim(self) -> None:
        """Drop the oldest rows until the queue fits in max_bytes."""
        excess = self._bytes - self.max_bytes
        if excess <= 0:
            return
        dropped = freed = 0
        cutoff = None
        with self._tx() as db:
            while freed < excess:
                rows = db.execute(
                    "SELECT id, size FROM outbox WHERE id > ? ORDER BY id LIMIT 256", (cutoff or 0,)
                ).fetchall()
                if not rows:
                    break
                for row_id, size in rows:
                    cutoff = row_id
                    freed += size
                    dropped += 1
                    if freed >= excess:
                        break
            if cutoff is not None:
                db.execute("DELETE FROM outbox WHERE id <= ?", (cutoff,))
        self._count -= dropped
        self._bytes -= freed
        count_saf_dropped(dropped)
        # At the cap every put() drops a row or two: log a summary at most once a minute.
        self._dropped_unlogged += dropped
        now = time.monotonic()
        if now - self._drop_logged_at >= _DROP_LOG_EVERY_S:
            log.warning("saf_dropped_oldest", dropped=self._dropped_unlogged, max_bytes=self.max_bytes,
                        up_to_id=cutoff, queued=self._count)
            self._dropped_unlogged = 0
            self._drop_logged_at = now

    # ------------------------------------------------------------------ API
    def put(self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False) -> int:
        """Append a message; returns its id. May drop the oldest rows to respect max_bytes
        (a single message larger than max_bytes is dropped right away, and counted)."""
        payload = bytes(payload)
        size = message_size(topic, payload)
        cur = self.db.execute(
            "INSERT INTO outbox (topic, payload, qos, retain, size, created) VALUES (?, ?, ?, ?, ?, ?)",
            (topic, payload, int(qos), int(bool(retain)), size, time.time()),
        )
        self._count += 1
        self._bytes += size
        self._trim()
        SAF_QUEUE.set(self._count)
        return int(cur.lastrowid)

    def peek(self, limit: int = 100) -> list[OutMsg]:
        """The oldest ``limit`` messages, oldest first. Nothing is removed."""
        rows = self.db.execute(
            "SELECT id, topic, payload, qos, retain FROM outbox ORDER BY id LIMIT ?", (int(limit),)
        ).fetchall()
        return [OutMsg(r[0], r[1], bytes(r[2]), r[3], bool(r[4])) for r in rows]

    def ack(self, ids: Iterable[int]) -> None:
        """Delete acknowledged messages. Unknown ids (already acked or dropped) are ignored."""
        ids = list(dict.fromkeys(int(i) for i in ids))
        if not ids:
            return
        removed = freed = 0
        with self._tx() as db:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i:i + _CHUNK]
                marks = ",".join("?" * len(chunk))
                n, size = db.execute(
                    f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM outbox WHERE id IN ({marks})", chunk
                ).fetchone()
                db.execute(f"DELETE FROM outbox WHERE id IN ({marks})", chunk)
                removed += n
                freed += size
        self._count -= removed
        self._bytes -= freed
        SAF_QUEUE.set(self._count)

    def count(self) -> int:
        return self._count

    def size_bytes(self) -> int:
        """Logical queue size: sum of topic + payload bytes (what max_bytes limits)."""
        return self._bytes

    def close(self) -> None:
        """Close the file. Idempotent. Queued messages stay on disk for the next start."""
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    @property
    def closed(self) -> bool:
        return self._db is None
