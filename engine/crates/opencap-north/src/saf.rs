//! Store-and-forward outbox in SQLite (Python `opencap.north.saf`), same schema and file
//! format, so an outbox written by one engine is drained by the other.
//!
//! Outbox-always: every outgoing message (data and status) is written here first; the MQTT
//! link drains it strictly in `id` order and deletes a row only after the broker's PUBACK.
//! WAL journal with `synchronous=NORMAL`. `max_bytes` caps `len(topic) + len(payload)` of
//! the queued messages; the oldest rows are dropped first and counted.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, params_from_iter, Connection};
use tracing::{info, warn};

use opencap_core::metrics::METRICS;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS outbox (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,   -- AUTOINCREMENT: ids never go back, even when empty
    topic   TEXT    NOT NULL,
    payload BLOB    NOT NULL,
    qos     INTEGER NOT NULL,
    retain  INTEGER NOT NULL,
    size    INTEGER NOT NULL,                    -- len(topic utf-8) + len(payload)
    created REAL    NOT NULL                     -- unix time of put()
)
";
const CHUNK: usize = 500;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutMsg {
    pub id: i64,
    pub topic: String,
    pub payload: Vec<u8>,
    pub qos: i64,
    pub retain: bool,
}

pub fn message_size(topic: &str, payload: &[u8]) -> i64 {
    (topic.len() + payload.len()) as i64
}

#[derive(Debug, thiserror::Error)]
pub enum SafError {
    #[error("StoreAndForward is closed")]
    Closed,
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

pub struct StoreAndForward {
    pub path: PathBuf,
    pub max_bytes: i64,
    db: Option<Connection>,
    count: i64,
    bytes: i64,
}

impl StoreAndForward {
    pub fn open(path: impl AsRef<Path>, max_bytes: i64) -> Result<Self, SafError> {
        let path = path.as_ref().to_path_buf();
        let db = Connection::open(&path)?;
        let mode: String = db.query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))?;
        if !mode.eq_ignore_ascii_case("wal") {
            warn!(path = %path.display(), journal_mode = %mode, "saf_not_wal");
        }
        db.execute_batch("PRAGMA synchronous=NORMAL")?;
        db.execute_batch(SCHEMA)?;
        let (count, size): (i64, i64) =
            db.query_row("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM outbox", [], |r| Ok((r.get(0)?, r.get(1)?)))?;
        let mut s = Self { path, max_bytes, db: Some(db), count, bytes: size };
        if count > 0 {
            info!(path = %s.path.display(), messages = count, bytes = size, "saf_reopened");
        }
        s.trim()?; // max_bytes may have been lowered
        METRICS.saf_queue.set(s.count);
        Ok(s)
    }

    fn db(&self) -> Result<&Connection, SafError> {
        self.db.as_ref().ok_or(SafError::Closed)
    }

    /// Drop the oldest rows until the queue fits in max_bytes.
    fn trim(&mut self) -> Result<(), SafError> {
        let excess = self.bytes - self.max_bytes;
        if excess <= 0 {
            return Ok(());
        }
        let (mut dropped, mut freed) = (0i64, 0i64);
        let mut cutoff: Option<i64> = None;
        {
            let db = self.db()?;
            db.execute_batch("BEGIN IMMEDIATE")?;
            let result = (|| -> Result<(), rusqlite::Error> {
                let mut stmt = db.prepare("SELECT id, size FROM outbox WHERE id > ? ORDER BY id LIMIT 256")?;
                'outer: while freed < excess {
                    let rows: Vec<(i64, i64)> = stmt
                        .query_map([cutoff.unwrap_or(0)], |r| Ok((r.get(0)?, r.get(1)?)))?
                        .collect::<Result<_, _>>()?;
                    if rows.is_empty() {
                        break;
                    }
                    for (id, size) in rows {
                        cutoff = Some(id);
                        freed += size;
                        dropped += 1;
                        if freed >= excess {
                            break 'outer;
                        }
                    }
                }
                if let Some(c) = cutoff {
                    db.execute("DELETE FROM outbox WHERE id <= ?", [c])?;
                }
                Ok(())
            })();
            match result {
                Ok(()) => db.execute_batch("COMMIT")?,
                Err(e) => {
                    let _ = db.execute_batch("ROLLBACK");
                    return Err(e.into());
                }
            }
        }
        self.count -= dropped;
        self.bytes -= freed;
        METRICS.saf_dropped.inc_by(dropped as u64);
        // At the cap every put() drops a row or two: one line each, which the engine's flood
        // control (LOG-R7) turns into the first line and a summary with `suppressed` a minute.
        warn!(
            dropped = dropped as u64,
            max_bytes = self.max_bytes,
            up_to_id = cutoff.unwrap_or(0),
            queued = self.count,
            "saf_dropped_oldest"
        );
        Ok(())
    }

    /// Append a message; returns its id. May drop the oldest rows to respect max_bytes (a
    /// single message larger than max_bytes is dropped right away, and counted).
    pub fn put(&mut self, topic: &str, payload: &[u8], qos: i64, retain: bool) -> Result<i64, SafError> {
        let size = message_size(topic, payload);
        let created = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        let id = {
            let db = self.db()?;
            db.execute(
                "INSERT INTO outbox (topic, payload, qos, retain, size, created) VALUES (?, ?, ?, ?, ?, ?)",
                params![topic, payload, qos, retain as i64, size, created],
            )?;
            db.last_insert_rowid()
        };
        self.count += 1;
        self.bytes += size;
        self.trim()?;
        METRICS.saf_queue.set(self.count);
        Ok(id)
    }

    /// The oldest `limit` messages, oldest first. Nothing is removed.
    pub fn peek(&self, limit: usize) -> Result<Vec<OutMsg>, SafError> {
        let db = self.db()?;
        let mut stmt = db.prepare_cached("SELECT id, topic, payload, qos, retain FROM outbox ORDER BY id LIMIT ?")?;
        let rows = stmt
            .query_map([limit as i64], |r| {
                Ok(OutMsg {
                    id: r.get(0)?,
                    topic: r.get(1)?,
                    payload: r.get(2)?,
                    qos: r.get(3)?,
                    retain: r.get::<_, i64>(4)? != 0,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// Delete acknowledged messages. Unknown ids (already acked or dropped) are ignored.
    pub fn ack(&mut self, ids: &[i64]) -> Result<(), SafError> {
        let mut unique: Vec<i64> = Vec::with_capacity(ids.len());
        let mut seen = std::collections::HashSet::new();
        for &i in ids {
            if seen.insert(i) {
                unique.push(i);
            }
        }
        if unique.is_empty() {
            return Ok(());
        }
        let (mut removed, mut freed) = (0i64, 0i64);
        {
            let db = self.db()?;
            db.execute_batch("BEGIN IMMEDIATE")?;
            let result = (|| -> Result<(), rusqlite::Error> {
                for chunk in unique.chunks(CHUNK) {
                    let marks = vec!["?"; chunk.len()].join(",");
                    let (n, size): (i64, i64) = db.query_row(
                        &format!("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM outbox WHERE id IN ({marks})"),
                        params_from_iter(chunk.iter()),
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )?;
                    db.execute(&format!("DELETE FROM outbox WHERE id IN ({marks})"), params_from_iter(chunk.iter()))?;
                    removed += n;
                    freed += size;
                }
                Ok(())
            })();
            match result {
                Ok(()) => db.execute_batch("COMMIT")?,
                Err(e) => {
                    let _ = db.execute_batch("ROLLBACK");
                    return Err(e.into());
                }
            }
        }
        self.count -= removed;
        self.bytes -= freed;
        METRICS.saf_queue.set(self.count);
        Ok(())
    }

    pub fn count(&self) -> i64 {
        self.count
    }

    /// Logical queue size: sum of topic + payload bytes (what max_bytes limits).
    pub fn size_bytes(&self) -> i64 {
        self.bytes
    }

    /// Close the file. Idempotent. Queued messages stay on disk for the next start.
    pub fn close(&mut self) {
        self.db = None;
    }

    pub fn closed(&self) -> bool {
        self.db.is_none()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("opencap-saf-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d.join("saf.sqlite")
    }

    #[test]
    fn put_peek_ack_in_order() {
        let mut s = StoreAndForward::open(tmp("order"), 10_000_000).unwrap();
        let ids: Vec<i64> =
            (0..5).map(|i| s.put(&format!("t/{i}"), format!("p{i}").as_bytes(), 1, false).unwrap()).collect();
        s.put("s/status", b"{\"state\":\"ok\"}", 1, true).unwrap();
        let msgs = s.peek(10).unwrap();
        assert_eq!(
            msgs.iter().map(|m| m.topic.as_str()).collect::<Vec<_>>(),
            ["t/0", "t/1", "t/2", "t/3", "t/4", "s/status"]
        );
        assert_eq!(msgs[0], OutMsg { id: ids[0], topic: "t/0".into(), payload: b"p0".to_vec(), qos: 1, retain: false });
        assert!(msgs[5].retain);
        s.ack(&[ids[1], ids[3], 999_999, ids[1]]).unwrap();
        assert_eq!(s.count(), 4);
        assert_eq!(s.peek(2).unwrap().iter().map(|m| m.id).collect::<Vec<_>>(), [ids[0], ids[2]]);
        s.ack(&[]).unwrap();
    }

    #[test]
    fn ack_many_ids() {
        let mut s = StoreAndForward::open(tmp("many"), 10_000_000).unwrap();
        let ids: Vec<i64> = (0..1234).map(|_| s.put("t", b"x", 1, false).unwrap()).collect();
        s.ack(&ids[..ids.len() - 1]).unwrap();
        assert_eq!(s.peek(10).unwrap().iter().map(|m| m.id).collect::<Vec<_>>(), [ids[1233]]);
    }

    #[test]
    fn cap_drops_oldest() {
        let mut s = StoreAndForward::open(tmp("cap"), 1000).unwrap();
        let payload = vec![b'x'; 89];
        assert_eq!(message_size("op/s/d/data", &payload), 100);
        let ids: Vec<i64> = (0..15).map(|_| s.put("op/s/d/data", &payload, 1, false).unwrap()).collect();
        assert_eq!((s.count(), s.size_bytes()), (10, 1000));
        assert_eq!(s.peek(100).unwrap().iter().map(|m| m.id).collect::<Vec<_>>(), ids[5..]);
        s.put("op/s/d/data", &[b'y'; 289], 1, false).unwrap();
        assert_eq!((s.count(), s.size_bytes()), (8, 1000));
        assert_eq!(s.peek(1).unwrap()[0].id, ids[8]);
        let mut small = StoreAndForward::open(tmp("small"), 50).unwrap();
        small.put("t", &[b'a'; 10], 1, false).unwrap();
        small.put("t", &[b'b'; 100], 1, false).unwrap();
        assert_eq!((small.count(), small.size_bytes()), (0, 0));
    }

    #[test]
    fn reopen_continues_and_ids_never_go_back() {
        let path = tmp("reopen");
        let mut s = StoreAndForward::open(&path, 1_000_000).unwrap();
        let ids: Vec<i64> = (0..4).map(|i| s.put("t", i.to_string().as_bytes(), 1, i == 2).unwrap()).collect();
        s.ack(&[ids[0], ids[3]]).unwrap();
        s.close();
        s.close();
        assert!(matches!(s.peek(1), Err(SafError::Closed)));
        let mut s2 = StoreAndForward::open(&path, 1_000_000).unwrap();
        assert_eq!((s2.count(), s2.size_bytes()), (2, 4));
        let new = s2.put("t", b"4", 1, false).unwrap();
        assert!(new > ids[3]);
        let journal: String = s2.db().unwrap().query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
        assert_eq!(journal, "wal");
        let sync: i64 = s2.db().unwrap().query_row("PRAGMA synchronous", [], |r| r.get(0)).unwrap();
        assert_eq!(sync, 1);
        s2.close();
        let s3 = StoreAndForward::open(&path, 3).unwrap(); // smaller cap trims on open
        assert_eq!(s3.count(), 1);
    }
}
