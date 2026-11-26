//! Shared lines (Python `_serial.LinkPool` / `SharedLink` / `Lease`, D-033).
//!
//! Several devices on one physical line (unit ids on RS-485, stations behind a bridge, several
//! KEPServer devices on one COM port) must share one connection: a local serial port opens
//! once and many gateways accept a single TCP client. [`LinkPool::acquire`] hands every driver
//! on the same line the same open connection, [`IoGuard::drop_line`] closes it for all of them
//! when the line fails (each reconnects on its next request), and the last [`Lease`] dropped
//! closes it. A lease is valid for the connection it joined: after a drop, the next one opened
//! on the line is a new generation and old leases report "not connected".
//!
//! The pool is per process (Python: per event loop); the engine runs one tokio runtime.
//! Holding the line's lock ([`Lease::io`]) is the per-line I/O lock: it also serialises devices
//! of different channels that point (by mistake) at the same line.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use tokio::sync::{Mutex as AsyncMutex, OwnedMutexGuard};

use crate::endpoint::Kind;

/// (driver, transport kind, host or device path, port): one physical line per driver protocol.
pub type LinkKey = (&'static str, Kind, String, u16);

struct LinkState<C> {
    conn: Option<C>,
    generation: u64,
}

struct SharedLink<C> {
    state: Arc<AsyncMutex<LinkState<C>>>,
    /// generation of the open connection (mirrors `state` for lock-free `alive()`)
    generation: AtomicU64,
    open: AtomicBool,
    users: Mutex<HashSet<u64>>,
}

impl<C> SharedLink<C> {
    fn new() -> Self {
        Self {
            state: Arc::new(AsyncMutex::new(LinkState { conn: None, generation: 0 })),
            generation: AtomicU64::new(0),
            open: AtomicBool::new(false),
            users: Mutex::new(HashSet::new()),
        }
    }

    fn close(&self, state: &mut LinkState<C>) {
        state.conn = None;
        self.open.store(false, Ordering::SeqCst);
    }
}

/// Shared lines by key. One static pool per driver protocol.
pub struct LinkPool<C> {
    links: Mutex<HashMap<LinkKey, Arc<SharedLink<C>>>>,
}

impl<C> Default for LinkPool<C> {
    fn default() -> Self {
        Self { links: Mutex::new(HashMap::new()) }
    }
}

/// Unique owner ids for leases (one per driver instance).
pub fn new_owner_id() -> u64 {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    NEXT.fetch_add(1, Ordering::Relaxed)
}

impl<C: Send + 'static> LinkPool<C> {
    pub fn new() -> Self {
        Self::default()
    }

    /// The open connection for `key`, opened with `opener()` when nobody has it open. Returns
    /// whatever `opener` fails with (nothing is registered then). Waiting for another user's
    /// open of the same line is part of the wait; other lines are never blocked.
    pub async fn acquire<F, Fut, E>(&self, key: LinkKey, owner: u64, opener: F) -> Result<Lease<C>, E>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = Result<C, E>>,
    {
        let link =
            self.links.lock().expect("pool lock").entry(key).or_insert_with(|| Arc::new(SharedLink::new())).clone();
        let mut state = link.state.clone().lock_owned().await;
        if state.conn.is_none() {
            let conn = opener().await?;
            state.conn = Some(conn);
            state.generation += 1;
            link.generation.store(state.generation, Ordering::SeqCst);
            link.open.store(true, Ordering::SeqCst);
        }
        link.users.lock().expect("users lock").insert(owner);
        Ok(Lease { generation: state.generation, link: link.clone(), owner })
    }

    /// How many drivers use the line now.
    pub fn users(&self, key: &LinkKey) -> usize {
        self.links.lock().expect("pool lock").get(key).map_or(0, |l| l.users.lock().expect("users lock").len())
    }
}

/// A driver's handle on a shared line, valid for the connection (generation) it joined.
/// Dropping it leaves the line; the last one out closes it.
pub struct Lease<C> {
    link: Arc<SharedLink<C>>,
    owner: u64,
    generation: u64,
}

impl<C> Lease<C> {
    /// The connection this lease joined is still open.
    pub fn alive(&self) -> bool {
        self.link.open.load(Ordering::SeqCst) && self.link.generation.load(Ordering::SeqCst) == self.generation
    }

    /// Both leases use the same open connection.
    pub fn same_connection(&self, other: &Lease<C>) -> bool {
        Arc::ptr_eq(&self.link, &other.link) && self.generation == other.generation && self.alive()
    }

    /// Number of drivers on this line.
    pub fn users(&self) -> usize {
        self.link.users.lock().expect("users lock").len()
    }

    /// Exclusive use of the line for one request. `None` when this lease's connection is gone.
    pub async fn io(&self) -> Option<IoGuard<C>> {
        let guard = self.link.state.clone().lock_owned().await;
        if guard.conn.is_none() || guard.generation != self.generation {
            return None;
        }
        Some(IoGuard { guard, link: self.link.clone() })
    }
}

impl<C> Drop for Lease<C> {
    fn drop(&mut self) {
        let mut users = self.link.users.lock().expect("users lock");
        users.remove(&self.owner);
        if users.is_empty() {
            // Nobody else holds the state lock unless a new user is opening the line right
            // now, and then the line is theirs: leave it alone.
            if let Ok(mut state) = self.link.state.try_lock() {
                self.link.close(&mut state);
            }
        }
    }
}

/// The line, locked for one request.
pub struct IoGuard<C> {
    guard: OwnedMutexGuard<LinkState<C>>,
    link: Arc<SharedLink<C>>,
}

impl<C> IoGuard<C> {
    pub fn conn(&mut self) -> &mut C {
        self.guard.conn.as_mut().expect("checked when locked")
    }

    /// The line failed: close it for every user (they reconnect on their next request).
    pub fn drop_line(mut self) {
        self.link.close(&mut self.guard);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(port: u16) -> LinkKey {
        ("test", Kind::Tcp, "192.0.2.50".into(), port)
    }

    #[tokio::test]
    async fn users_share_one_connection_and_the_last_one_out_closes_it() {
        let pool: LinkPool<u32> = LinkPool::new();
        let opens = AtomicU64::new(0);
        let open = || async {
            opens.fetch_add(1, Ordering::SeqCst);
            Ok::<_, String>(7)
        };
        let a = pool.acquire(key(1), 1, open).await.unwrap();
        let b = pool.acquire(key(1), 2, open).await.unwrap();
        let c = pool.acquire(key(2), 3, open).await.unwrap();
        assert_eq!(opens.load(Ordering::SeqCst), 2, "one open per line");
        assert!(a.same_connection(&b) && !a.same_connection(&c));
        assert_eq!((pool.users(&key(1)), a.users()), (2, 2));
        assert_eq!(*a.io().await.unwrap().conn(), 7);
        drop(a);
        assert!(b.alive(), "b still uses the line");
        drop(b);
        assert_eq!(pool.users(&key(1)), 0);
        let d = pool.acquire(key(1), 4, open).await.unwrap();
        assert_eq!(opens.load(Ordering::SeqCst), 3, "reopened after the last user left");
        assert!(d.alive() && c.alive());
    }

    #[tokio::test]
    async fn a_drop_closes_the_line_for_every_user_and_old_leases_stay_dead() {
        let pool: LinkPool<u32> = LinkPool::new();
        let mut n = 0;
        let a = pool.acquire(key(1), 1, || async { Ok::<_, String>(1) }).await.unwrap();
        let b = pool.acquire(key(1), 2, || async { Ok::<_, String>(99) }).await.unwrap();
        a.io().await.unwrap().drop_line();
        assert!(!a.alive() && !b.alive());
        assert!(b.io().await.is_none(), "not connected");
        // a reconnects: a new generation; b's old lease stays dead until b reconnects too
        drop(a);
        let a = pool
            .acquire(key(1), 1, || async {
                n += 1;
                Ok::<_, String>(2)
            })
            .await
            .unwrap();
        assert_eq!(n, 1);
        assert!(a.alive() && !b.alive());
        drop(b);
        let b = pool.acquire(key(1), 2, || async { Ok::<_, String>(3) }).await.unwrap();
        assert!(a.same_connection(&b));
        assert_eq!(*b.io().await.unwrap().conn(), 2);
    }

    #[tokio::test]
    async fn a_failed_open_registers_nothing() {
        let pool: LinkPool<u32> = LinkPool::new();
        let err = pool.acquire(key(1), 1, || async { Err::<u32, _>("refused") }).await.err();
        assert_eq!(err, Some("refused"));
        assert_eq!(pool.users(&key(1)), 0);
        let a = pool.acquire(key(1), 1, || async { Ok::<_, &str>(5) }).await.unwrap();
        assert!(a.alive());
    }
}
