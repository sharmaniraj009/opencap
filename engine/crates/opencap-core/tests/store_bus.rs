//! Change-event bus under load (ADR-0005): writers on their own threads never wait for a
//! slow subscriber, and a subscriber that lags resynchronises from snapshots and ends with
//! exactly the store's state.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::{TimeZone, Utc};
use tokio::sync::watch;

use opencap_core::metrics::METRICS;
use opencap_core::store::{Change, ChangeReceiver, DeviceId, TagLayout, TagStore};
use opencap_core::value::{Quality, TagValue, Value};

const DEVICES: usize = 40;
const TAGS: usize = 20;
const WRITERS: usize = 4;
const ROUNDS: usize = 400;

/// What a subscriber holds: per device, the last version applied and the tag values.
#[derive(Default)]
struct Mirror {
    devices: HashMap<DeviceId, (u64, Vec<Option<TagValue>>)>,
    resyncs: usize,
    stale_skipped: usize,
}

impl Mirror {
    fn resync(&mut self, store: &TagStore, ids: &[DeviceId]) {
        self.resyncs += 1;
        for id in ids {
            if let Some(snap) = store.snapshot_by_id(*id) {
                self.devices.insert(*id, (snap.version(), snap.values().cloned().map(Some).collect()));
            }
        }
    }

    fn apply(&mut self, change: Change, store: &TagStore, ids: &[DeviceId]) {
        match change {
            Change::Lagged(_) => self.resync(store, ids),
            Change::Batch(b) => {
                let entry = self.devices.entry(b.device).or_insert_with(|| (0, vec![None; b.layout.len()]));
                if b.version <= entry.0 {
                    self.stale_skipped += 1; // already contained in a snapshot
                    return;
                }
                if b.full {
                    entry.1 = vec![None; b.layout.len()];
                }
                for (i, tv) in &b.changes {
                    entry.1[*i as usize] = Some(tv.clone());
                }
                entry.0 = b.version;
            }
        }
    }
}

async fn drain(rx: &mut ChangeReceiver, mirror: &mut Mirror, store: &TagStore, ids: &[DeviceId]) {
    while let Ok(change) = rx.try_recv() {
        mirror.apply(change, store, ids);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn slow_subscriber_lags_resyncs_and_converges() {
    let store = Arc::new(TagStore::with_options(64, None));
    let mut slow = store.subscribe("test-slow");
    let mut fast = store.subscribe("test-fast");
    let writers: Vec<_> = (0..DEVICES)
        .map(|d| store.register_layout(TagLayout::new(format!("S/D{d}"), (0..TAGS).map(|t| format!("T{t}")))))
        .collect();
    let ids: Vec<DeviceId> = writers.iter().map(|w| w.device()).collect();
    let mut slow_mirror = Mirror::default();
    let mut fast_mirror = Mirror::default();
    slow_mirror.resync(&store, &ids);
    fast_mirror.resync(&store, &ids);
    let lagged_before = METRICS.north_lagged.with_label_values(&["test-slow"]).get();

    // Writer threads: each owns DEVICES / WRITERS devices and writes ROUNDS batches to each.
    // `stop` is sent only after the last write, so a subscriber that sees it and then drains
    // with try_recv() has everything that was published.
    let (stop_tx, stop_rx) = watch::channel(false);
    let mut per_thread: Vec<Vec<_>> = (0..WRITERS).map(|_| Vec::new()).collect();
    for (i, w) in writers.into_iter().enumerate() {
        per_thread[i % WRITERS].push(w);
    }
    let ts = Utc.with_ymd_and_hms(2026, 11, 16, 10, 0, 0).unwrap();
    let threads: Vec<_> = per_thread
        .into_iter()
        .enumerate()
        .map(|(n, devs)| {
            std::thread::spawn(move || {
                let mut worst = Duration::ZERO;
                for round in 0..ROUNDS {
                    for w in &devs {
                        let t = Instant::now();
                        let quality = if round % 7 == 3 { Quality::BadComm } else { Quality::Good };
                        let values = (0..TAGS as u32).map(|tag| {
                            let v = Value::Int((round * 1000 + tag as usize + n) as i64);
                            let at = ts + chrono::Duration::milliseconds(round as i64);
                            (tag, TagValue::new(Some(v), quality, Some(at)))
                        });
                        assert!(w.update_indexed(values));
                        worst = worst.max(t.elapsed());
                    }
                    if round % 50 == 49 {
                        std::thread::yield_now();
                    }
                }
                (devs, worst)
            })
        })
        .collect();

    // Fast subscriber keeps up; slow one sleeps between batches and so falls behind.
    let fast_task = {
        let store = store.clone();
        let ids = ids.clone();
        let mut stop = stop_rx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    change = fast.recv() => match change {
                        Some(c) => fast_mirror.apply(c, &store, &ids),
                        None => break,
                    },
                    _ = stop.changed() => break,
                }
            }
            drain(&mut fast, &mut fast_mirror, &store, &ids).await;
            (fast, fast_mirror)
        })
    };
    let slow_task = {
        let store = store.clone();
        let ids = ids.clone();
        let mut stop = stop_rx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    change = slow.recv() => match change {
                        Some(c) => slow_mirror.apply(c, &store, &ids),
                        None => break,
                    },
                    _ = stop.changed() => break,
                }
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
            drain(&mut slow, &mut slow_mirror, &store, &ids).await;
            (slow, slow_mirror)
        })
    };

    let mut writers = Vec::new();
    for t in threads {
        let (devs, worst) = t.join().expect("writer thread");
        // Generous bound: a write is a few microseconds; it must never wait for the slow
        // subscriber (which sleeps 2 ms per batch).
        assert!(worst < Duration::from_millis(500), "a write took {worst:?}");
        writers.extend(devs);
    }
    for w in &writers {
        w.update_indexed([(0, TagValue::good(Value::Int(-1), ts))]);
    }
    stop_tx.send(true).unwrap();
    let (fast, fast_mirror) = tokio::time::timeout(Duration::from_secs(20), fast_task).await.unwrap().unwrap();
    let (slow, slow_mirror) = tokio::time::timeout(Duration::from_secs(20), slow_task).await.unwrap().unwrap();

    assert!(slow.lagged_total() > 0, "the slow subscriber must have lagged");
    assert!(slow_mirror.resyncs > 1);
    assert_eq!(METRICS.north_lagged.with_label_values(&["test-slow"]).get() - lagged_before, slow.lagged_total());
    for (name, mirror) in [("fast", &fast_mirror), ("slow", &slow_mirror)] {
        for id in &ids {
            let snap = store.snapshot_by_id(*id).unwrap();
            let (version, values) = &mirror.devices[id];
            // The mirror may stop at an update that changed nothing (no batch), so compare
            // values, not versions, and require the mirror is not ahead of the store.
            assert!(*version <= snap.version(), "{name}: mirror ahead of the store");
            for (i, tv) in snap.values().enumerate() {
                let got = values[i].as_ref().expect("value");
                assert!(got.same_reading(tv), "{name} {id:?} tag {i}: {got:?} != {tv:?}");
            }
        }
    }
    drop(fast);
}
