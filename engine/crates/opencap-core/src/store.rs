//! Tag store and change-event bus ([ADR-0005], `docs/architecture/03-deep-dive.md` §3.2).
//!
//! Layout
//! - One [`DeviceSlot`] per device: the device's tag values in a contiguous `Vec<TagValue>`
//!   indexed by the device-local [`TagIndex`] (enabled tags in config order, see
//!   [`TagLayout`]), a monotonic `version` and `last_good`. Slots live in a `Vec` indexed by
//!   [`DeviceId`]; there is no global lock on the write path.
//! - Exactly one writer per device: [`TagStore::register`] hands out the device's only valid
//!   [`DeviceWriter`]. Registering the device again (config reload) makes every earlier
//!   writer stale: its writes are ignored and publish nothing, so a channel worker that is
//!   still stopping cannot write over its successor.
//! - Readers use [`TagStore::get`] / [`TagStore::snapshot`]; a snapshot is a copy taken
//!   under the device lock. The lock is a `std::sync::Mutex`, so it cannot be held across an
//!   `.await` in a spawned task (the guard is not `Send`).
//!
//! Cache rules (Python `opencap.core.cache.ValueCache`, contract §2)
//! - A good value replaces value, quality and `source_ts`.
//! - Any other quality keeps the last value and the last good `source_ts`; only the quality
//!   changes. A tag never read good has value `None` and `source_ts` `None` (stale).
//! - `version` bumps on every update and register, and on an expire that changed something.
//! - `server_ts` ([ADR-0006]) is set by the store to its clock on every write it accepts
//!   (good, bad, or expired to stale). It is not part of the Python model, so it is not in
//!   the admin API's JSON.
//!
//! Events
//! - After each accepted write the writer publishes `Arc<ChangeBatch>` on a bounded
//!   `tokio::sync::broadcast` channel ([`DEFAULT_BUS_CAPACITY`] batches by default). The
//!   batch holds the tags whose value, quality or `source_ts` changed, with their stored
//!   values; a write that changed nothing publishes nothing. `register` publishes the whole
//!   device (`full = true`) when the slot is new or its tags changed.
//! - Publishing is a synchronous, non-blocking `send` made while the device lock is held,
//!   so batches of one device are on the bus in version order. Writer methods are plain
//!   `fn`s: the writer never awaits a subscriber.
//! - A subscriber that falls more than the capacity behind gets [`Change::Lagged`] and must
//!   resynchronise from snapshots of the devices it serves, then skip batches whose
//!   `version` is not newer than the snapshot's (conflation to the latest value). The
//!   receiver then continues at the newest batch: the batches still queued are older than
//!   those snapshots, so they are dropped with the overwritten ones. (tokio would otherwise
//!   resume at the oldest slot, which the next send overwrites, and a subscriber busy
//!   resynchronising would lag again at once.) Skipped batches are counted in
//!   `north_lagged_total{agent}` and logged as `north.lagged`.
//! - A batch is freed once every subscriber has received it, so the bus holds only unread
//!   batches: memory is bounded by `capacity` × batch size whatever the subscribers do.
//!
//! [ADR-0005]: ../../../../docs/adr/0005-north-plugins-tag-store-event-bus.md
//! [ADR-0006]: ../../../../docs/adr/0006-quality-and-timestamps.md

use std::collections::HashMap;
use std::ops::Index;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, RwLock, RwLockReadGuard, RwLockWriteGuard};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use tokio::sync::broadcast;
use tracing::warn;

use crate::config::Device;
use crate::metrics::METRICS;
use crate::value::{now_utc, Clock, Quality, TagValue, Value};

/// Bus capacity in batches (03 §3.2). tokio rounds it up to a power of two.
pub const DEFAULT_BUS_CAPACITY: usize = 4096;
/// At most one `north.lagged` log line per subscriber per interval (the metric counts all).
pub const LAG_LOG_INTERVAL: Duration = Duration::from_secs(10);

/// Device-local tag index: position in the device's [`TagLayout`].
pub type TagIndex = u32;

/// Store-wide device number. Stable for the life of the store: a device that is
/// unregistered and registered again gets its old id back.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct DeviceId(pub u32);

impl DeviceId {
    pub fn index(self) -> usize {
        self.0 as usize
    }
}

/// The tags of one device in store order: enabled tags in config order.
#[derive(Debug, PartialEq)]
pub struct TagLayout {
    device: String,
    /// Each name is allocated once and shared with `index`.
    names: Vec<Arc<str>>,
    index: HashMap<Arc<str>, TagIndex>,
}

impl TagLayout {
    /// Duplicate names keep their first position.
    pub fn new(device: impl Into<String>, names: impl IntoIterator<Item = String>) -> Self {
        let mut out = Self { device: device.into(), names: Vec::new(), index: HashMap::new() };
        for name in names {
            out.push(name);
        }
        out
    }

    /// The enabled tags of a configured device.
    pub fn of(device: &Device) -> Self {
        Self::new(device.id.clone(), device.tags.iter().filter(|t| t.enabled).map(|t| t.name.clone()))
    }

    fn push(&mut self, name: String) -> TagIndex {
        if let Some(i) = self.index.get(name.as_str()) {
            return *i;
        }
        let i = self.names.len() as TagIndex;
        let name: Arc<str> = name.into();
        self.index.insert(name.clone(), i);
        self.names.push(name);
        i
    }

    /// Device id string (`<channel>/<device>`).
    pub fn device(&self) -> &str {
        &self.device
    }

    pub fn names(&self) -> impl ExactSizeIterator<Item = &str> + '_ {
        self.names.iter().map(|n| &**n)
    }

    pub fn name(&self, i: TagIndex) -> Option<&str> {
        self.names.get(i as usize).map(|n| &**n)
    }

    pub fn index_of(&self, name: &str) -> Option<TagIndex> {
        self.index.get(name).copied()
    }

    pub fn len(&self) -> usize {
        self.names.len()
    }

    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }
}

/// A copy of one device's values (Python `ValueCache.snapshot()`), in tag order.
#[derive(Debug, Clone)]
pub struct Snapshot {
    layout: Arc<TagLayout>,
    values: Vec<TagValue>,
    version: u64,
    last_good: Option<DateTime<Utc>>,
}

impl Default for Snapshot {
    fn default() -> Self {
        Self::empty("")
    }
}

impl Snapshot {
    /// No tags (an unknown device).
    pub fn empty(device: &str) -> Self {
        Self { layout: Arc::new(TagLayout::new(device, [])), values: Vec::new(), version: 0, last_good: None }
    }

    /// A snapshot built from `(name, value)` pairs (tests, fixtures). A repeated name keeps
    /// its first position and its last value, like an `IndexMap`.
    pub fn from_pairs<I, K>(device: &str, pairs: I) -> Self
    where
        I: IntoIterator<Item = (K, TagValue)>,
        K: Into<String>,
    {
        let mut layout = TagLayout::new(device, []);
        let mut values: Vec<TagValue> = Vec::new();
        for (name, tv) in pairs {
            let i = layout.push(name.into()) as usize;
            if i == values.len() {
                values.push(tv);
            } else {
                values[i] = tv;
            }
        }
        Self { layout: Arc::new(layout), values, version: 0, last_good: None }
    }

    pub fn layout(&self) -> &Arc<TagLayout> {
        &self.layout
    }

    /// The device's version when the snapshot was taken.
    pub fn version(&self) -> u64 {
        self.version
    }

    /// Newest good `source_ts` seen on the device when the snapshot was taken.
    pub fn last_good(&self) -> Option<DateTime<Utc>> {
        self.last_good
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn get(&self, name: &str) -> Option<&TagValue> {
        self.layout.index_of(name).and_then(|i| self.values.get(i as usize))
    }

    pub fn get_index(&self, i: TagIndex) -> Option<&TagValue> {
        self.values.get(i as usize)
    }

    pub fn iter(&self) -> impl ExactSizeIterator<Item = (&str, &TagValue)> + '_ {
        self.layout.names().zip(self.values.iter())
    }

    pub fn keys(&self) -> impl ExactSizeIterator<Item = &str> + '_ {
        self.layout.names()
    }

    pub fn values(&self) -> std::slice::Iter<'_, TagValue> {
        self.values.iter()
    }
}

/// Like [`Snapshot::from_pairs`] with no device name.
impl<K: Into<String>> FromIterator<(K, TagValue)> for Snapshot {
    fn from_iter<I: IntoIterator<Item = (K, TagValue)>>(pairs: I) -> Self {
        Self::from_pairs("", pairs)
    }
}

impl Index<&str> for Snapshot {
    type Output = TagValue;

    fn index(&self, name: &str) -> &TagValue {
        self.get(name).unwrap_or_else(|| panic!("no tag {name:?} in the snapshot of {}", self.layout.device))
    }
}

/// What one accepted write changed on one device, as published on the bus.
#[derive(Debug)]
pub struct ChangeBatch {
    pub device: DeviceId,
    /// The layout `changes` refers to.
    pub layout: Arc<TagLayout>,
    /// The device's version after the write.
    pub version: u64,
    /// True when `changes` holds every tag of the device (register, new tag layout): a
    /// subscriber replaces what it holds for the device.
    pub full: bool,
    /// Changed tags with their stored values (after the cache rules).
    pub changes: Vec<(TagIndex, TagValue)>,
}

impl ChangeBatch {
    pub fn device_name(&self) -> &str {
        self.layout.device()
    }

    /// `(tag name, stored value)` for every change.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &TagValue)> + '_ {
        self.changes.iter().filter_map(|(i, tv)| self.layout.name(*i).map(|n| (n, tv)))
    }
}

struct SlotState {
    values: Vec<TagValue>,
    version: u64,
    last_good: Option<DateTime<Utc>>,
    /// Generation of the only writer allowed to write.
    writer: u64,
    /// Replaced (new layout) or unregistered: nobody may write any more.
    retired: bool,
}

/// One device's values. Created by [`TagStore::register`].
pub struct DeviceSlot {
    id: DeviceId,
    layout: Arc<TagLayout>,
    state: Mutex<SlotState>,
}

impl DeviceSlot {
    fn lock(&self) -> MutexGuard<'_, SlotState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn snapshot(&self) -> Snapshot {
        let st = self.lock();
        Snapshot {
            layout: self.layout.clone(),
            values: st.values.clone(),
            version: st.version,
            last_good: st.last_good,
        }
    }

    pub fn id(&self) -> DeviceId {
        self.id
    }

    pub fn layout(&self) -> &Arc<TagLayout> {
        &self.layout
    }
}

/// Shared by the store and its writers. Receivers hold only the subscriber counter, so the
/// channel closes once the store and every writer are gone.
struct Bus {
    tx: broadcast::Sender<Arc<ChangeBatch>>,
    subscribers: Arc<AtomicUsize>,
    clock: Clock,
}

#[derive(Default)]
struct Directory {
    ids: HashMap<String, DeviceId>,
    slots: Vec<Option<Arc<DeviceSlot>>>,
}

impl Directory {
    fn intern(&mut self, name: &str) -> DeviceId {
        if let Some(id) = self.ids.get(name) {
            return *id;
        }
        let id = DeviceId(self.slots.len() as u32);
        self.ids.insert(name.to_string(), id);
        self.slots.push(None);
        id
    }

    fn slot(&self, name: &str) -> Option<&Arc<DeviceSlot>> {
        self.ids.get(name).and_then(|id| self.slots[id.index()].as_ref())
    }
}

/// The tag store. Shared as `Arc<TagStore>`; north plugins get a [`TagStoreReader`].
pub struct TagStore {
    dir: RwLock<Directory>,
    bus: Arc<Bus>,
    capacity: usize,
}

impl Default for TagStore {
    fn default() -> Self {
        Self::new()
    }
}

impl TagStore {
    pub fn new() -> Self {
        Self::with_options(DEFAULT_BUS_CAPACITY, None)
    }

    /// `capacity` batches on the bus (at least 1); `clock` sets `server_ts` (default: UTC now,
    /// microsecond precision).
    pub fn with_options(capacity: usize, clock: Option<Clock>) -> Self {
        let capacity = capacity.max(1);
        let (tx, _) = broadcast::channel(capacity);
        let clock = clock.unwrap_or_else(|| Arc::new(now_utc));
        Self {
            dir: RwLock::new(Directory::default()),
            bus: Arc::new(Bus { tx, subscribers: Arc::new(AtomicUsize::new(0)), clock }),
            capacity,
        }
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    fn read_dir(&self) -> RwLockReadGuard<'_, Directory> {
        self.dir.read().unwrap_or_else(|e| e.into_inner())
    }

    fn write_dir(&self) -> RwLockWriteGuard<'_, Directory> {
        self.dir.write().unwrap_or_else(|e| e.into_inner())
    }

    fn slot(&self, device: &str) -> Option<Arc<DeviceSlot>> {
        self.read_dir().slot(device).cloned()
    }

    fn slot_by_id(&self, id: DeviceId) -> Option<Arc<DeviceSlot>> {
        self.read_dir().slots.get(id.index()).cloned().flatten()
    }

    // --- writes -----------------------------------------------------------------------

    /// Create or update the device's slot and return its only valid writer. Every enabled
    /// tag starts as `TagValue(None, stale, None)`; on a second call (reload) tags that still
    /// exist keep their value and quality, new tags are added as stale and removed tags are
    /// dropped. Earlier writers of the device become stale.
    pub fn register(&self, device: &Device) -> DeviceWriter {
        self.register_layout(TagLayout::of(device))
    }

    /// [`register`](Self::register) for a layout built by hand (virtual devices, benchmarks).
    pub fn register_layout(&self, layout: TagLayout) -> DeviceWriter {
        let mut dir = self.write_dir();
        let id = dir.intern(&layout.device);
        let old = dir.slots[id.index()].clone();
        if let Some(old) = old.as_ref().filter(|o| o.layout.names == layout.names) {
            let mut st = old.lock();
            st.writer += 1;
            st.version += 1;
            return DeviceWriter { slot: old.clone(), gen: st.writer, bus: self.bus.clone() };
        }
        let (mut prev, version, last_good, writer) = match old {
            Some(old) => {
                let mut st = old.lock();
                st.retired = true;
                let prev: HashMap<Arc<str>, TagValue> =
                    old.layout.names.iter().cloned().zip(std::mem::take(&mut st.values)).collect();
                (prev, st.version, st.last_good, st.writer)
            }
            None => (HashMap::new(), 0, None, 0),
        };
        let values: Vec<TagValue> = layout
            .names
            .iter()
            .map(|n| prev.remove(&**n).unwrap_or_else(|| TagValue::bad(Quality::Stale, None)))
            .collect();
        let state = SlotState { values, version: version + 1, last_good, writer: writer + 1, retired: false };
        let slot = Arc::new(DeviceSlot { id, layout: Arc::new(layout), state: Mutex::new(state) });
        {
            let st = slot.lock();
            if self.bus.subscribers.load(Ordering::Acquire) > 0 {
                let changes = st.values.iter().cloned().enumerate().map(|(i, tv)| (i as TagIndex, tv)).collect();
                let batch =
                    ChangeBatch { device: id, layout: slot.layout.clone(), version: st.version, full: true, changes };
                let _ = self.bus.tx.send(Arc::new(batch));
            }
        }
        dir.slots[id.index()] = Some(slot.clone());
        DeviceWriter { slot, gen: writer + 1, bus: self.bus.clone() }
    }

    /// Drop the device: its writer becomes stale and reads see an unknown device. A later
    /// `register` starts again at version 1.
    pub fn unregister(&self, device: &str) {
        let mut dir = self.write_dir();
        let Some(id) = dir.ids.get(device).copied() else { return };
        if let Some(old) = dir.slots[id.index()].take() {
            old.lock().retired = true;
        }
    }

    // --- reads ------------------------------------------------------------------------

    /// Read-only handle for north plugins.
    pub fn reader(self: &Arc<Self>) -> TagStoreReader {
        TagStoreReader(self.clone())
    }

    /// Subscribe to the change-event bus. Subscribe first, then take the snapshots you
    /// start from: every write is either in a snapshot or published to you afterwards.
    pub fn subscribe(&self, agent: &str) -> ChangeReceiver {
        self.bus.subscribers.fetch_add(1, Ordering::AcqRel);
        ChangeReceiver {
            rx: self.bus.tx.subscribe(),
            subscribers: self.bus.subscribers.clone(),
            agent: agent.to_string(),
            lagged_total: 0,
            unlogged: 0,
            last_log: None,
        }
    }

    /// Id of a device that has been registered at least once.
    pub fn lookup(&self, device: &str) -> Option<DeviceId> {
        self.read_dir().ids.get(device).copied()
    }

    /// Id for a device name, allocated if needed (the device need not be registered yet).
    /// Subscribers use it to map `ChangeBatch::device` to the devices they serve.
    pub fn intern(&self, device: &str) -> DeviceId {
        if let Some(id) = self.lookup(device) {
            return id;
        }
        self.write_dir().intern(device)
    }

    /// Copy of every registered tag of the device, in tag order (empty if not registered).
    pub fn snapshot(&self, device: &str) -> Snapshot {
        self.slot(device).map(|s| s.snapshot()).unwrap_or_else(|| Snapshot::empty(device))
    }

    /// Snapshot by id; `None` if the device is not registered.
    pub fn snapshot_by_id(&self, id: DeviceId) -> Option<Snapshot> {
        self.slot_by_id(id).map(|s| s.snapshot())
    }

    pub fn get(&self, device: &str, tag: &str) -> Option<TagValue> {
        let slot = self.slot(device)?;
        let i = slot.layout.index_of(tag)?;
        let tv = slot.lock().values.get(i as usize).cloned();
        tv
    }

    pub fn version(&self, device: &str) -> u64 {
        self.slot(device).map(|s| s.lock().version).unwrap_or(0)
    }

    /// Newest good `source_ts` seen on the device.
    pub fn last_good(&self, device: &str) -> Option<DateTime<Utc>> {
        self.slot(device).and_then(|s| s.lock().last_good)
    }

    pub fn layout(&self, device: &str) -> Option<Arc<TagLayout>> {
        self.slot(device).map(|s| s.layout.clone())
    }

    /// Registered devices, in the order they were first registered.
    pub fn devices(&self) -> Vec<String> {
        let dir = self.read_dir();
        dir.slots.iter().flatten().map(|s| s.layout.device.clone()).collect()
    }

    pub fn contains(&self, device: &str) -> bool {
        self.read_dir().slot(device).is_some()
    }

    /// Registered devices and tags (memory reports).
    pub fn counts(&self) -> (usize, usize) {
        let dir = self.read_dir();
        dir.slots.iter().flatten().fold((0, 0), |(d, t), s| (d + 1, t + s.layout.len()))
    }
}

/// The read side of the store: what a north plugin may do with it.
#[derive(Clone)]
pub struct TagStoreReader(Arc<TagStore>);

impl TagStoreReader {
    pub fn subscribe(&self, agent: &str) -> ChangeReceiver {
        self.0.subscribe(agent)
    }
    pub fn lookup(&self, device: &str) -> Option<DeviceId> {
        self.0.lookup(device)
    }
    pub fn intern(&self, device: &str) -> DeviceId {
        self.0.intern(device)
    }
    pub fn snapshot(&self, device: &str) -> Snapshot {
        self.0.snapshot(device)
    }
    pub fn snapshot_by_id(&self, id: DeviceId) -> Option<Snapshot> {
        self.0.snapshot_by_id(id)
    }
    pub fn get(&self, device: &str, tag: &str) -> Option<TagValue> {
        self.0.get(device, tag)
    }
    pub fn version(&self, device: &str) -> u64 {
        self.0.version(device)
    }
    pub fn last_good(&self, device: &str) -> Option<DateTime<Utc>> {
        self.0.last_good(device)
    }
    pub fn layout(&self, device: &str) -> Option<Arc<TagLayout>> {
        self.0.layout(device)
    }
    pub fn devices(&self) -> Vec<String> {
        self.0.devices()
    }
    pub fn contains(&self, device: &str) -> bool {
        self.0.contains(device)
    }
}

// --- writer ---------------------------------------------------------------------------

/// The only handle that may write one device's values (single writer per device). Not
/// `Clone`: the channel worker that owns the device owns its writer.
pub struct DeviceWriter {
    slot: Arc<DeviceSlot>,
    gen: u64,
    bus: Arc<Bus>,
}

/// One write under the device lock.
struct Txn<'a> {
    st: MutexGuard<'a, SlotState>,
    now: DateTime<Utc>,
    newest: Option<DateTime<Utc>>,
    /// `None` when nobody listens: no batch is built.
    changes: Option<Vec<(TagIndex, TagValue)>>,
}

impl Txn<'_> {
    fn good(&mut self, i: usize, value: Option<Value>, source_ts: Option<DateTime<Utc>>) {
        let e = &mut self.st.values[i];
        let changed = e.quality != Quality::Good || e.source_ts != source_ts || e.value != value;
        e.value = value;
        e.quality = Quality::Good;
        e.source_ts = source_ts;
        e.server_ts = Some(self.now);
        if let Some(ts) = source_ts {
            if self.newest.is_none_or(|n| ts > n) {
                self.newest = Some(ts);
            }
        }
        if changed {
            if let Some(c) = self.changes.as_mut() {
                c.push((i as TagIndex, e.clone()));
            }
        }
    }

    /// Keep the last value and the last good `source_ts`; only the quality changes.
    fn bad(&mut self, i: usize, quality: Quality) {
        let e = &mut self.st.values[i];
        let changed = e.quality != quality;
        e.quality = quality;
        e.server_ts = Some(self.now);
        if changed {
            if let Some(c) = self.changes.as_mut() {
                c.push((i as TagIndex, e.clone()));
            }
        }
    }

    fn put(&mut self, i: usize, tv: &TagValue) {
        if tv.quality == Quality::Good {
            self.good(i, tv.value.clone(), tv.source_ts);
        } else {
            self.bad(i, tv.quality);
        }
    }
}

impl DeviceWriter {
    pub fn device(&self) -> DeviceId {
        self.slot.id
    }

    pub fn layout(&self) -> &Arc<TagLayout> {
        &self.slot.layout
    }

    pub fn index_of(&self, name: &str) -> Option<TagIndex> {
        self.slot.layout.index_of(name)
    }

    /// False once the device was registered again or unregistered: writes are ignored.
    pub fn is_current(&self) -> bool {
        let st = self.slot.lock();
        !st.retired && st.writer == self.gen
    }

    /// The device's current values.
    pub fn snapshot(&self) -> Snapshot {
        self.slot.snapshot()
    }

    fn begin(&self) -> Option<Txn<'_>> {
        let st = self.slot.lock();
        if st.retired || st.writer != self.gen {
            return None;
        }
        let listening = self.bus.subscribers.load(Ordering::Acquire) > 0;
        Some(Txn { st, now: (self.bus.clock)(), newest: None, changes: listening.then(Vec::new) })
    }

    /// Bump the version, move `last_good`, publish the changes (still under the lock, so a
    /// device's batches are on the bus in version order).
    fn commit(&self, mut t: Txn<'_>) {
        if let Some(ts) = t.newest {
            if t.st.last_good.is_none_or(|old| ts > old) {
                t.st.last_good = Some(ts);
            }
        }
        t.st.version += 1;
        if let Some(changes) = t.changes.take().filter(|c| !c.is_empty()) {
            let batch = ChangeBatch {
                device: self.slot.id,
                layout: self.slot.layout.clone(),
                version: t.st.version,
                full: false,
                changes,
            };
            let _ = self.bus.tx.send(Arc::new(batch)); // Err only when nobody subscribes
        }
    }

    /// Apply one batch of results by tag name. Names that are not registered are ignored.
    /// Returns false if this writer is stale (nothing was written).
    pub fn update<'v, I, K>(&self, values: I) -> bool
    where
        I: IntoIterator<Item = (K, &'v TagValue)>,
        K: AsRef<str>,
    {
        let layout = &self.slot.layout;
        // Resolve names before taking the lock.
        let resolved: Vec<(usize, &TagValue)> = values
            .into_iter()
            .filter_map(|(name, tv)| layout.index_of(name.as_ref()).map(|i| (i as usize, tv)))
            .collect();
        let Some(mut t) = self.begin() else { return false };
        for (i, tv) in resolved {
            t.put(i, tv);
        }
        self.commit(t);
        true
    }

    /// Apply one batch by tag index (the hot path: no name lookups, values moved in).
    /// Indices outside the layout are ignored. Returns false if this writer is stale.
    pub fn update_indexed<I>(&self, values: I) -> bool
    where
        I: IntoIterator<Item = (TagIndex, TagValue)>,
    {
        let Some(mut t) = self.begin() else { return false };
        let n = t.st.values.len();
        for (i, tv) in values {
            let i = i as usize;
            if i >= n {
                continue;
            }
            if tv.quality == Quality::Good {
                t.good(i, tv.value, tv.source_ts);
            } else {
                t.bad(i, tv.quality);
            }
        }
        self.commit(t);
        true
    }

    /// Good tags whose last good read is older than `max_age_s[name]` become stale. The age
    /// counts from `source_ts`, or from `read_at[name]` when given (drivers whose
    /// `source_ts` is the device's own change time, D-045). Tags missing from `max_age_s`
    /// and tags already bad are left alone. Returns true if anything changed (and the
    /// version bumped).
    pub fn expire(
        &self,
        now: DateTime<Utc>,
        max_age_s: &HashMap<String, f64>,
        read_at: Option<&HashMap<String, DateTime<Utc>>>,
    ) -> bool {
        let layout = &self.slot.layout;
        let ages: Vec<(TagIndex, f64)> =
            max_age_s.iter().filter_map(|(n, a)| layout.index_of(n).map(|i| (i, *a))).collect();
        let read_at: Option<Vec<Option<DateTime<Utc>>>> =
            read_at.map(|r| layout.names().map(|n| r.get(n).copied()).collect());
        self.expire_indexed(now, &ages, read_at.as_deref())
    }

    /// [`expire`](Self::expire) by tag index; `read_at` is indexed like the layout.
    pub fn expire_indexed(
        &self,
        now: DateTime<Utc>,
        max_age_s: &[(TagIndex, f64)],
        read_at: Option<&[Option<DateTime<Utc>>]>,
    ) -> bool {
        let Some(mut t) = self.begin() else { return false };
        let mut changed = false;
        for &(i, max_age) in max_age_s {
            let i = i as usize;
            let Some(e) = t.st.values.get(i) else { continue };
            if e.quality != Quality::Good {
                continue;
            }
            let Some(since) = read_at.and_then(|r| r.get(i).copied().flatten()).or(e.source_ts) else { continue };
            let age = (now - since).num_microseconds().map(|us| us as f64 / 1e6).unwrap_or(f64::MAX);
            if age > max_age {
                t.bad(i, Quality::Stale);
                changed = true;
            }
        }
        if changed {
            self.commit(t);
        }
        changed
    }
}

// --- subscriber -----------------------------------------------------------------------

/// What a subscriber receives.
#[derive(Debug, Clone)]
pub enum Change {
    Batch(Arc<ChangeBatch>),
    /// `n` batches were skipped: resynchronise from snapshots. The receiver already
    /// continues at the newest batch.
    Lagged(u64),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TryRecvError {
    Empty,
    Closed,
}

/// A subscription to the change-event bus.
pub struct ChangeReceiver {
    rx: broadcast::Receiver<Arc<ChangeBatch>>,
    subscribers: Arc<AtomicUsize>,
    agent: String,
    lagged_total: u64,
    unlogged: u64,
    last_log: Option<Instant>,
}

impl ChangeReceiver {
    /// The agent name used in `north_lagged_total{agent}` and in logs.
    pub fn agent(&self) -> &str {
        &self.agent
    }

    /// Batches this receiver has skipped so far.
    pub fn lagged_total(&self) -> u64 {
        self.lagged_total
    }

    /// Next change; `None` once the store and all its writers are gone.
    pub async fn recv(&mut self) -> Option<Change> {
        match self.rx.recv().await {
            Ok(batch) => Some(Change::Batch(batch)),
            Err(broadcast::error::RecvError::Lagged(n)) => Some(self.lagged(n)),
            Err(broadcast::error::RecvError::Closed) => None,
        }
    }

    pub fn try_recv(&mut self) -> Result<Change, TryRecvError> {
        match self.rx.try_recv() {
            Ok(batch) => Ok(Change::Batch(batch)),
            Err(broadcast::error::TryRecvError::Lagged(n)) => Ok(self.lagged(n)),
            Err(broadcast::error::TryRecvError::Empty) => Err(TryRecvError::Empty),
            Err(broadcast::error::TryRecvError::Closed) => Err(TryRecvError::Closed),
        }
    }

    fn lagged(&mut self, missed: u64) -> Change {
        // Skip what is still queued too: it predates the snapshots the subscriber takes now.
        let n = missed + self.rx.len() as u64;
        self.rx = self.rx.resubscribe();
        self.lagged_total += n;
        self.unlogged += n;
        METRICS.north_lagged.with_label_values(&[&self.agent]).inc_by(n);
        let now = Instant::now();
        if self.last_log.is_none_or(|t| now.duration_since(t) >= LAG_LOG_INTERVAL) {
            warn!(agent = %self.agent, skipped = self.unlogged, total = self.lagged_total, "north.lagged");
            self.unlogged = 0;
            self.last_log = Some(now);
        }
        Change::Lagged(n)
    }
}

impl Drop for ChangeReceiver {
    fn drop(&mut self) {
        self.subscribers.fetch_sub(1, Ordering::AcqRel);
    }
}

#[cfg(test)]
mod tests {
    //! The Python `tests/unit/test_cache.py` cases, plus the store's own rules.

    use super::*;
    use crate::config::{DType, Tag};
    use chrono::{Duration as Td, TimeZone};

    fn t0() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 16, 10, 0, 0).unwrap()
    }

    fn secs(s: i64) -> DateTime<Utc> {
        t0() + Td::seconds(s)
    }

    fn dev(names: &[&str], disabled: &[&str]) -> Device {
        let mut tags: Vec<Tag> = names.iter().map(|n| Tag::new(n, "400001", DType::Uint16)).collect();
        tags.extend(disabled.iter().map(|n| Tag { enabled: false, ..Tag::new(n, "400099", DType::Uint16) }));
        Device::new("S/D", "S", "D", "modbus_tcp", "h", 502, tags)
    }

    /// Store whose clock (server_ts) is fixed at `t0() + 1 h`.
    fn store() -> TagStore {
        TagStore::with_options(64, Some(Arc::new(|| t0() + Td::hours(1))))
    }

    fn good(v: Value, ts: DateTime<Utc>) -> TagValue {
        TagValue::good(v, ts)
    }

    fn int(i: i64) -> Value {
        Value::Int(i)
    }

    fn tv(value: Option<Value>, q: Quality, ts: Option<DateTime<Utc>>) -> TagValue {
        TagValue::new(value, q, ts)
    }

    fn upd(w: &DeviceWriter, vals: &[(&str, TagValue)]) {
        assert!(w.update(vals.iter().map(|(k, v)| (*k, v))));
    }

    /// Compare the Python fields only (value, quality, source_ts).
    #[track_caller]
    fn same(got: Option<TagValue>, want: TagValue) {
        let got = got.expect("tag exists");
        assert!(got.same_reading(&want), "got {got:?}, want {want:?}");
    }

    #[test]
    fn register_prefills_enabled_tags_as_stale() {
        let c = store();
        c.register(&dev(&["A", "B"], &["OFF"]));
        let snap = c.snapshot("S/D");
        assert_eq!(snap.keys().collect::<Vec<_>>(), vec!["A", "B"]);
        assert!(snap.values().all(|v| *v == TagValue::bad(Quality::Stale, None)));
        assert_eq!(c.last_good("S/D"), None);
        assert!(c.contains("S/D") && c.devices() == vec!["S/D".to_string()]);
        assert_eq!(c.version("S/D"), 1);
    }

    #[test]
    fn good_update_stores_value_quality_and_ts() {
        let c = store();
        let w = c.register(&dev(&["A", "B"], &[]));
        upd(&w, &[("A", good(Value::Float(1.5), t0()))]);
        let a = c.get("S/D", "A").unwrap();
        same(Some(a.clone()), good(Value::Float(1.5), t0()));
        assert_eq!(a.server_ts, Some(t0() + Td::hours(1)), "server_ts is the store's clock");
        assert_eq!(c.snapshot("S/D")["B"].quality, Quality::Stale);
        assert_eq!(c.snapshot("S/D")["B"].server_ts, None);
        assert_eq!(c.last_good("S/D"), Some(t0()));
    }

    #[test]
    fn bad_update_keeps_last_value_and_last_good_ts() {
        let c = store();
        let w = c.register(&dev(&["A"], &[]));
        upd(&w, &[("A", good(int(7), t0()))]);
        for q in [Quality::BadComm, Quality::BadDecode, Quality::BadConfig, Quality::Stale] {
            upd(&w, &[("A", tv(None, q, Some(secs(5))))]);
            same(c.get("S/D", "A"), tv(Some(int(7)), q, Some(t0())));
        }
        upd(&w, &[("A", good(int(8), secs(9)))]);
        same(c.get("S/D", "A"), good(int(8), secs(9)));
    }

    #[test]
    fn bad_update_on_never_read_tag_keeps_none() {
        let c = store();
        let w = c.register(&dev(&["A"], &[]));
        upd(&w, &[("A", tv(None, Quality::BadComm, None))]);
        same(c.get("S/D", "A"), tv(None, Quality::BadComm, None));
    }

    #[test]
    fn version_bumps_on_every_update() {
        let c = store();
        assert_eq!(c.version("S/D"), 0);
        let w = c.register(&dev(&["A"], &[]));
        let v0 = c.version("S/D");
        upd(&w, &[("A", good(int(1), t0()))]);
        upd(&w, &[("A", good(int(1), t0()))]); // same value still bumps
        upd(&w, &[]);
        assert_eq!(c.version("S/D"), v0 + 3);
    }

    #[test]
    fn snapshot_is_a_copy() {
        let c = store();
        let w = c.register(&dev(&["A", "ARR"], &[]));
        upd(&w, &[("A", good(int(1), t0())), ("ARR", good(Value::List(vec![int(1), int(2), int(3)]), t0()))]);
        let mut snap = c.snapshot("S/D");
        snap.values[0].value = Some(int(99));
        snap.values[0].quality = Quality::BadComm;
        if let Some(Value::List(items)) = snap.values[1].value.as_mut() {
            items.push(int(4));
        }
        let again = c.snapshot("S/D");
        same(again.get("A").cloned(), good(int(1), t0()));
        assert_eq!(again["ARR"].value, Some(Value::List(vec![int(1), int(2), int(3)])));
    }

    #[test]
    fn update_does_not_alias_caller_objects() {
        let c = store();
        let w = c.register(&dev(&["A"], &[]));
        let mut input = good(Value::List(vec![int(1), int(2)]), t0());
        upd(&w, &[("A", input.clone())]);
        if let Some(Value::List(items)) = input.value.as_mut() {
            items.push(int(3));
        }
        input.quality = Quality::BadComm;
        same(c.get("S/D", "A"), good(Value::List(vec![int(1), int(2)]), t0()));
    }

    #[test]
    fn unknown_names_are_ignored_unknown_device_reads_are_lenient() {
        // Python raises KeyError on a write to an unknown device; here a write needs the
        // device's writer, so it cannot be addressed at all.
        let c = store();
        let w = c.register(&dev(&["A"], &[]));
        upd(&w, &[("NOPE", good(int(1), t0()))]);
        assert_eq!(c.snapshot("S/D").keys().collect::<Vec<_>>(), vec!["A"]);
        assert!(c.snapshot("X/Y").is_empty() && c.version("X/Y") == 0 && c.last_good("X/Y").is_none());
        assert!(c.get("X/Y", "A").is_none() && c.get("S/D", "NOPE").is_none());
        assert!(w.update_indexed([(7, good(int(1), t0()))]), "an index outside the layout is ignored");
    }

    #[test]
    fn last_good_is_newest_good_ts() {
        let c = store();
        let w = c.register(&dev(&["A", "B"], &[]));
        upd(&w, &[("A", good(int(1), secs(3))), ("B", good(int(2), secs(1)))]);
        assert_eq!(c.last_good("S/D"), Some(secs(3)));
        upd(&w, &[("B", good(int(2), secs(2)))]); // older than A's
        assert_eq!(c.last_good("S/D"), Some(secs(3)));
        upd(&w, &[("A", tv(None, Quality::BadComm, Some(secs(60))))]);
        assert_eq!(c.last_good("S/D"), Some(secs(3)));
    }

    fn ages(pairs: &[(&str, f64)]) -> HashMap<String, f64> {
        pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
    }

    #[test]
    fn expire_marks_only_old_good_tags_stale() {
        let c = store();
        let w = c.register(&dev(&["OLD", "NEW", "BAD", "NEVER", "NOLIMIT"], &[]));
        upd(
            &w,
            &[
                ("OLD", good(int(1), t0())),
                ("NEW", good(int(2), secs(9))),
                ("BAD", good(int(3), t0())),
                ("NOLIMIT", good(int(4), t0())),
            ],
        );
        upd(&w, &[("BAD", tv(None, Quality::BadComm, None))]);
        let v = c.version("S/D");
        let a = ages(&[("OLD", 3.0), ("NEW", 3.0), ("BAD", 3.0), ("NEVER", 3.0)]);
        assert!(w.expire(secs(10), &a, None));
        let snap = c.snapshot("S/D");
        same(snap.get("OLD").cloned(), tv(Some(int(1)), Quality::Stale, Some(t0()))); // value and ts kept
        assert_eq!(snap["NEW"].quality, Quality::Good);
        assert_eq!(snap["BAD"].quality, Quality::BadComm); // bad stays bad
        same(snap.get("NEVER").cloned(), tv(None, Quality::Stale, None));
        assert_eq!(snap["NOLIMIT"].quality, Quality::Good); // not in max_age_s
        assert_eq!(c.version("S/D"), v + 1);
        assert!(!w.expire(secs(10), &a, None)); // nothing changed: no bump
        assert_eq!(c.version("S/D"), v + 1);
        upd(&w, &[("OLD", good(int(5), secs(11)))]);
        same(c.get("S/D", "OLD"), good(int(5), secs(11)));
    }

    #[test]
    fn expire_boundary_is_strictly_older() {
        let c = store();
        let w = c.register(&dev(&["A"], &[]));
        upd(&w, &[("A", good(int(1), t0()))]);
        w.expire(secs(3), &ages(&[("A", 3.0)]), None);
        assert_eq!(c.get("S/D", "A").unwrap().quality, Quality::Good);
        w.expire(secs(3) + Td::milliseconds(1), &ages(&[("A", 3.0)]), None);
        assert_eq!(c.get("S/D", "A").unwrap().quality, Quality::Stale);
    }

    #[test]
    fn expire_by_read_time_ignores_an_old_source_ts() {
        let c = store();
        let w = c.register(&dev(&["UNCHANGED", "STOPPED", "UNTRACKED"], &[]));
        let hour_ago = t0() - Td::hours(1);
        upd(
            &w,
            &[
                ("UNCHANGED", good(int(1), hour_ago)),
                ("STOPPED", good(int(1), hour_ago)),
                ("UNTRACKED", good(int(1), hour_ago)),
            ],
        );
        let read_at: HashMap<String, DateTime<Utc>> =
            [("UNCHANGED".to_string(), secs(9)), ("STOPPED".to_string(), t0())].into();
        w.expire(secs(10), &ages(&[("UNCHANGED", 3.0), ("STOPPED", 3.0), ("UNTRACKED", 3.0)]), Some(&read_at));
        let snap = c.snapshot("S/D");
        same(snap.get("UNCHANGED").cloned(), good(int(1), hour_ago)); // source_ts itself is untouched
        same(snap.get("STOPPED").cloned(), tv(Some(int(1)), Quality::Stale, Some(hour_ago)));
        assert_eq!(snap["UNTRACKED"].quality, Quality::Stale); // no read time: falls back to source_ts
    }

    #[test]
    fn register_again_keeps_values_adds_and_drops_tags() {
        let c = store();
        let w = c.register(&dev(&["A", "B"], &[]));
        upd(&w, &[("A", good(int(1), t0())), ("B", good(int(2), t0()))]);
        c.register(&dev(&["A", "C"], &[]));
        let snap = c.snapshot("S/D");
        assert_eq!(snap.keys().collect::<Vec<_>>(), vec!["A", "C"]);
        same(snap.get("A").cloned(), good(int(1), t0()));
        assert_eq!(snap["C"], TagValue::bad(Quality::Stale, None));
        assert_eq!(c.last_good("S/D"), Some(t0()));
        c.unregister("S/D");
        assert!(!c.contains("S/D") && c.devices().is_empty());
        c.register(&dev(&["A"], &[]));
        assert_eq!(c.version("S/D"), 1, "unregister forgets the version, as Python does");
        assert_eq!(c.get("S/D", "A").unwrap(), TagValue::bad(Quality::Stale, None));
    }

    // --- store rules beyond the Python cache -----------------------------------------

    #[test]
    fn only_the_newest_writer_writes() {
        let c = store();
        let old = c.register(&dev(&["A", "B"], &[]));
        let same_layout = c.register(&dev(&["A", "B"], &[]));
        assert!(!old.is_current() && same_layout.is_current());
        assert!(!old.update([("A", &good(int(1), t0()))]));
        assert!(!old.expire(secs(99), &ages(&[("A", 0.0)]), None));
        assert_eq!(c.get("S/D", "A").unwrap().value, None);
        let new_layout = c.register(&dev(&["A"], &[]));
        assert!(!same_layout.update([("A", &good(int(2), t0()))]), "writer of a replaced slot");
        assert!(new_layout.update([("A", &good(int(3), t0()))]));
        assert_eq!(c.get("S/D", "A").unwrap().value, Some(int(3)));
        c.unregister("S/D");
        assert!(!new_layout.update([("A", &good(int(4), t0()))]));
    }

    #[test]
    fn device_ids_are_stable() {
        let c = store();
        let a = c.intern("S/A");
        let w = c.register(&dev(&["X"], &[]));
        assert_eq!(c.lookup("S/A"), Some(a));
        assert_eq!(c.lookup("S/D"), Some(w.device()));
        c.unregister("S/D");
        assert_eq!(c.register(&dev(&["X"], &[])).device(), w.device());
        assert!(c.snapshot_by_id(a).is_none() && c.snapshot_by_id(w.device()).is_some());
        assert_eq!(c.counts(), (1, 1));
    }

    fn batch(rx: &mut ChangeReceiver) -> Arc<ChangeBatch> {
        match rx.try_recv() {
            Ok(Change::Batch(b)) => b,
            other => panic!("expected a batch, got {other:?}"),
        }
    }

    #[test]
    fn batches_hold_what_changed_in_version_order() {
        let c = store();
        let mut rx = c.subscribe("test");
        let w = c.register(&dev(&["A", "B", "C"], &[]));
        let b = batch(&mut rx);
        assert!(b.full && b.version == 1 && b.changes.len() == 3 && b.device == w.device());

        upd(&w, &[("A", good(int(1), t0())), ("B", tv(None, Quality::BadComm, None))]);
        let b = batch(&mut rx);
        assert!(!b.full);
        assert_eq!(b.version, 2);
        assert_eq!(b.device_name(), "S/D");
        let got: Vec<(&str, Option<Value>, Quality)> = b.iter().map(|(n, v)| (n, v.value.clone(), v.quality)).collect();
        assert_eq!(got, vec![("A", Some(int(1)), Quality::Good), ("B", None, Quality::BadComm)]);
        assert_eq!(b.changes[0].1.server_ts, Some(t0() + Td::hours(1)));

        // Same bad quality again and a good read with an unchanged value and source_ts:
        // nothing changed, the version bumps but no batch is published.
        upd(&w, &[("A", good(int(1), t0())), ("B", tv(None, Quality::BadComm, None))]);
        assert_eq!(rx.try_recv().unwrap_err(), TryRecvError::Empty);
        assert_eq!(c.version("S/D"), 3);

        // A new source_ts is a change even with the same value; the stored value is published.
        upd(&w, &[("A", good(int(1), secs(1))), ("C", tv(None, Quality::BadDecode, None))]);
        let b = batch(&mut rx);
        assert_eq!(b.version, 4);
        assert_eq!(b.changes.iter().map(|(i, _)| *i).collect::<Vec<_>>(), vec![0, 2]);

        // Expire publishes the tags it made stale, with their last value kept.
        assert!(w.expire(secs(10), &ages(&[("A", 3.0)]), None));
        let b = batch(&mut rx);
        assert_eq!(b.version, 5);
        assert_eq!(b.changes.len(), 1);
        assert!(b.changes[0].1.same_reading(&tv(Some(int(1)), Quality::Stale, Some(secs(1)))));

        // Registering the same layout again publishes nothing; a new layout republishes the device.
        c.register(&dev(&["A", "B", "C"], &[]));
        assert_eq!(rx.try_recv().unwrap_err(), TryRecvError::Empty);
        let w = c.register(&dev(&["C", "A"], &[]));
        let b = batch(&mut rx);
        assert!(b.full && b.version == 7 && b.layout.names().collect::<Vec<_>>() == ["C", "A"]);
        assert_eq!(b.changes[1].1.value, Some(int(1)));
        drop(w);
    }

    #[test]
    fn no_batch_is_built_without_subscribers() {
        let c = store();
        let w = c.register(&dev(&["A"], &[]));
        upd(&w, &[("A", good(int(1), t0()))]);
        let mut rx = c.subscribe("late");
        assert_eq!(rx.try_recv().unwrap_err(), TryRecvError::Empty, "only writes after subscribe");
        upd(&w, &[("A", good(int(2), t0()))]);
        assert_eq!(batch(&mut rx).changes[0].1.value, Some(int(2)));
        drop(rx);
        assert_eq!(c.bus.subscribers.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn a_subscriber_that_never_reads_cannot_block_the_writer() {
        let c = TagStore::with_options(8, None);
        let mut rx = c.subscribe("stuck");
        let w = c.register(&dev(&["A"], &[]));
        for i in 0..10_000 {
            assert!(w.update_indexed([(0, good(int(i), t0()))]));
        }
        let before = METRICS.north_lagged.with_label_values(&["stuck"]).get();
        // 10,000 updates + the register batch: the overwritten ones and the 8 still queued
        // are all skipped, and the receiver continues at the newest batch.
        match rx.try_recv() {
            Ok(Change::Lagged(n)) => assert_eq!(n, 10_001),
            other => panic!("expected Lagged, got {other:?}"),
        }
        assert_eq!(rx.lagged_total(), 10_001);
        assert_eq!(METRICS.north_lagged.with_label_values(&["stuck"]).get() - before, 10_001);
        assert_eq!(rx.try_recv().unwrap_err(), TryRecvError::Empty);
        assert!(w.update_indexed([(0, good(int(-1), t0()))]));
        assert_eq!(batch(&mut rx).version, 10_002);
    }

    #[tokio::test]
    async fn recv_ends_when_the_store_and_writers_are_gone() {
        let c = store();
        let mut rx = c.subscribe("a");
        let w = c.register(&dev(&["A"], &[]));
        drop(c);
        assert!(matches!(rx.recv().await, Some(Change::Batch(_))));
        drop(w);
        assert!(rx.recv().await.is_none());
    }

    #[test]
    fn snapshot_from_pairs_behaves_like_an_index_map() {
        let s = Snapshot::from_pairs(
            "S/D",
            [("B", good(int(1), t0())), ("A", good(int(2), t0())), ("B", good(int(3), t0()))],
        );
        assert_eq!(s.keys().collect::<Vec<_>>(), vec!["B", "A"]);
        assert_eq!(s["B"].value, Some(int(3)));
        assert_eq!(s.iter().len(), 2);
        assert!(Snapshot::default().is_empty());
    }
}
