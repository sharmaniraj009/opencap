//! Criterion micro-benchmarks of the tag store and change-event bus (#41, ADR-0005).
//!
//! `cargo bench -p opencap-core --bench store`. The paced end-to-end scenario (250k tags,
//! 8 writers, 4 subscribers, one of them slow, p99 write latency) is `--bench store_load`.

use std::hint::black_box;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use criterion::{criterion_group, criterion_main, BatchSize, BenchmarkId, Criterion, Throughput};

use opencap_core::store::{Change, DeviceWriter, TagIndex, TagLayout, TagStore};
use opencap_core::value::{now_utc, TagValue, Value};

const TAGS_PER_DEVICE: usize = 125;

fn layout(device: usize) -> TagLayout {
    TagLayout::new(format!("Channel {device}/PLC"), (0..TAGS_PER_DEVICE).map(|t| format!("TAG_{t:03}")))
}

/// `n` good float values starting at tag `first`, all different from the previous round.
fn batch(round: u64, first: usize, n: usize) -> Vec<(TagIndex, TagValue)> {
    let ts = now_utc();
    (0..n)
        .map(|k| {
            let t = (first + k) % TAGS_PER_DEVICE;
            (t as TagIndex, TagValue::good(Value::Float(round as f64 + t as f64 * 0.001), ts))
        })
        .collect()
}

fn bench_writes(c: &mut Criterion, name: &str, w: &DeviceWriter) {
    let mut g = c.benchmark_group(format!("store/write/{name}"));
    for n in [25usize, 125] {
        g.throughput(Throughput::Elements(n as u64));
        let mut round = 0u64;
        g.bench_with_input(BenchmarkId::new("update_indexed", n), &n, |b, &n| {
            b.iter_batched(
                || {
                    round += 1;
                    batch(round, (round as usize * 25) % TAGS_PER_DEVICE, n)
                },
                |v| black_box(w.update_indexed(v)),
                BatchSize::SmallInput,
            )
        });
        let names: Vec<String> = (0..n).map(|t| format!("TAG_{t:03}")).collect();
        g.bench_with_input(BenchmarkId::new("update_by_name", n), &n, |b, &n| {
            b.iter_batched(
                || {
                    round += 1;
                    batch(round, 0, n)
                },
                |v| black_box(w.update(names.iter().zip(v.iter().map(|(_, tv)| tv)))),
                BatchSize::SmallInput,
            )
        });
    }
    g.finish();
}

/// Write cost without subscribers, with 4 subscribers that never read (the bus is full and
/// every send overwrites), and with 4 live subscribers (3 fast, 1 slow) on other threads.
fn writes(c: &mut Criterion) {
    let store = TagStore::new();
    let w = store.register_layout(layout(0));
    bench_writes(c, "no_subscribers", &w);

    let stalled: Vec<_> = (0..4).map(|i| store.subscribe(&format!("bench-stalled-{i}"))).collect();
    bench_writes(c, "4_stalled_subscribers", &w);
    drop(stalled);

    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    for i in 0..4 {
        let mut rx = store.subscribe(&format!("bench-live-{i}"));
        let stop = stop.clone();
        rt.spawn(async move {
            let mut sum = 0.0f64;
            while !stop.load(Ordering::Relaxed) {
                match tokio::time::timeout(Duration::from_millis(50), rx.recv()).await {
                    Ok(Some(Change::Batch(b))) => {
                        sum += b.changes.iter().filter_map(|(_, tv)| tv.value.as_ref()?.as_f64()).sum::<f64>();
                        if i == 3 {
                            tokio::time::sleep(Duration::from_millis(1)).await; // the slow one
                        }
                    }
                    Ok(Some(Change::Lagged(_))) | Err(_) => {}
                    Ok(None) => break,
                }
            }
            black_box(sum);
        });
    }
    bench_writes(c, "4_live_subscribers_1_slow", &w);
    stop.store(true, Ordering::Relaxed);
    rt.shutdown_timeout(Duration::from_secs(1));
}

fn reads(c: &mut Criterion) {
    let store = TagStore::new();
    let w = store.register_layout(layout(0));
    w.update_indexed(batch(1, 0, TAGS_PER_DEVICE));
    let mut g = c.benchmark_group("store/read");
    g.throughput(Throughput::Elements(TAGS_PER_DEVICE as u64));
    g.bench_function("snapshot/125", |b| b.iter(|| black_box(store.snapshot("Channel 0/PLC"))));
    g.throughput(Throughput::Elements(1));
    g.bench_function("get", |b| b.iter(|| black_box(store.get("Channel 0/PLC", "TAG_042"))));
    g.finish();
}

criterion_group! {
    name = benches;
    config = Criterion::default().measurement_time(Duration::from_secs(3)).warm_up_time(Duration::from_secs(1));
    targets = writes, reads
}
criterion_main!(benches);
