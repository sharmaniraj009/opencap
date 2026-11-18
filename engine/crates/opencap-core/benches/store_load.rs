//! Tag store and change-event bus under the target load (#41, NFR-1, ADR-0005).
//!
//! 250,000 tags on 2,000 devices (125 tags each), 250,000 tag updates/s from 8 writer tasks
//! (one batch = one 25-tag block, so 10,000 batches/s), and 0 or 4 subscribers, one of which
//! is deliberately slow (it sleeps 1 ms after every batch and resynchronises from snapshots
//! when it lags). Prints the writer's latency percentiles per scenario, CPU time, the
//! subscribers' lag counts and the store's memory per tag.
//!
//! `cargo bench -p opencap-core --bench store_load`
//! (`OPENCAP_BENCH_SECONDS` sets the run time per scenario, default 10).

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::watch;

use opencap_core::store::{Change, DeviceId, DeviceWriter, TagIndex, TagLayout, TagStore};
use opencap_core::value::{now_utc, TagValue, Value};

const DEVICES: usize = 2_000;
const TAGS_PER_DEVICE: usize = 125;
const BLOCK: usize = 25;
const WRITERS: usize = 8;
const UPDATES_PER_S: usize = 250_000;
const RUNTIME_THREADS: usize = 4;

fn layout(device: usize) -> TagLayout {
    TagLayout::new(format!("Channel {device:04}/PLC"), (0..TAGS_PER_DEVICE).map(|t| format!("TAG_{t:03}")))
}

/// Resident set size in bytes (Linux; `None` elsewhere).
fn rss() -> Option<u64> {
    let statm = std::fs::read_to_string("/proc/self/statm").ok()?;
    let pages: u64 = statm.split_whitespace().nth(1)?.parse().ok()?;
    Some(pages * 4096)
}

/// User + system CPU seconds of this process (Linux; USER_HZ is 100 on every Linux ABI).
fn cpu_s() -> Option<f64> {
    let stat = std::fs::read_to_string("/proc/self/stat").ok()?;
    let rest = stat.rsplit_once(')')?.1;
    let f: Vec<&str> = rest.split_whitespace().collect();
    let ticks: u64 = f.get(11)?.parse::<u64>().ok()? + f.get(12)?.parse::<u64>().ok()?;
    Some(ticks as f64 / 100.0)
}

#[derive(Clone, Copy, PartialEq)]
enum Scenario {
    NoSubscribers,
    FourFast,
    ThreeFastOneSlow,
}

impl Scenario {
    fn label(self) -> &'static str {
        match self {
            Scenario::NoSubscribers => "no subscribers",
            Scenario::FourFast => "4 fast subscribers",
            Scenario::ThreeFastOneSlow => "3 fast + 1 slow subscriber",
        }
    }
}

struct SubReport {
    slow: bool,
    batches: u64,
    lagged: u64,
    resyncs: u64,
}

struct Report {
    scenario: Scenario,
    latencies_ns: Vec<u32>,
    writes: u64,
    elapsed: Duration,
    cpu_s: Option<f64>,
    subs: Vec<SubReport>,
}

fn pct(sorted: &[u32], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let i = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[i] as f64 / 1000.0
}

async fn writer_task(
    writers: Vec<DeviceWriter>,
    batches_per_s: f64,
    duration: Duration,
    start: Instant,
) -> (Vec<u32>, u64) {
    let period = Duration::from_secs_f64(1.0 / batches_per_s);
    let blocks = TAGS_PER_DEVICE / BLOCK;
    let mut lat = Vec::with_capacity((batches_per_s * duration.as_secs_f64() * 1.05) as usize);
    let mut k: u64 = 0;
    loop {
        let due = start + period.mul_f64(k as f64);
        if due >= start + duration {
            break;
        }
        if Instant::now() < due {
            tokio::time::sleep_until(due.into()).await;
        }
        let device = (k as usize) % writers.len();
        let block = (k as usize / writers.len()) % blocks;
        let round = k / (writers.len() * blocks) as u64;
        let ts = now_utc();
        let values: Vec<(TagIndex, TagValue)> = (0..BLOCK)
            .map(|j| {
                let t = block * BLOCK + j;
                (t as TagIndex, TagValue::good(Value::Float(round as f64 + t as f64 * 0.001), ts))
            })
            .collect();
        let t0 = Instant::now();
        let ok = writers[device].update_indexed(values);
        lat.push(t0.elapsed().as_nanos().min(u32::MAX as u128) as u32);
        assert!(ok);
        k += 1;
    }
    (lat, k)
}

async fn subscriber_task(
    store: Arc<TagStore>,
    ids: Arc<Vec<DeviceId>>,
    agent: String,
    slow: bool,
    mut stop: watch::Receiver<bool>,
) -> SubReport {
    let mut rx = store.subscribe(&agent);
    let mut report = SubReport { slow, batches: 0, lagged: 0, resyncs: 0 };
    let mut sum = 0.0f64;
    loop {
        let change = tokio::select! {
            c = rx.recv() => c,
            _ = stop.changed() => break,
        };
        match change {
            Some(Change::Batch(b)) => {
                report.batches += 1;
                sum += b.changes.iter().filter_map(|(_, tv)| tv.value.as_ref()?.as_f64()).sum::<f64>();
                if slow {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
            }
            Some(Change::Lagged(n)) => {
                report.lagged += n;
                report.resyncs += 1;
                for id in ids.iter() {
                    if let Some(s) = store.snapshot_by_id(*id) {
                        sum += s.len() as f64;
                    }
                }
            }
            None => break,
        }
    }
    std::hint::black_box(sum);
    report
}

fn run(scenario: Scenario, duration: Duration) -> Report {
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(RUNTIME_THREADS).enable_all().build().unwrap();
    rt.block_on(async move {
        let store = Arc::new(TagStore::new());
        let mut writers: Vec<DeviceWriter> = (0..DEVICES).map(|d| store.register_layout(layout(d))).collect();
        let ids = Arc::new(writers.iter().map(|w| w.device()).collect::<Vec<_>>());
        let (stop_tx, stop_rx) = watch::channel(false);
        let subs: Vec<_> = match scenario {
            Scenario::NoSubscribers => vec![],
            Scenario::FourFast => vec![false; 4],
            Scenario::ThreeFastOneSlow => vec![false, false, false, true],
        }
        .into_iter()
        .enumerate()
        .map(|(i, slow)| {
            tokio::spawn(subscriber_task(store.clone(), ids.clone(), format!("bench-{i}"), slow, stop_rx.clone()))
        })
        .collect();
        tokio::time::sleep(Duration::from_millis(100)).await;

        let per_writer = DEVICES / WRITERS;
        let batches_per_s = (UPDATES_PER_S / BLOCK) as f64 / WRITERS as f64;
        let cpu0 = cpu_s();
        let start = Instant::now();
        let tasks: Vec<_> = (0..WRITERS)
            .map(|_| {
                let own: Vec<DeviceWriter> = writers.drain(..per_writer).collect();
                tokio::spawn(writer_task(own, batches_per_s, duration, start))
            })
            .collect();
        let mut latencies_ns = Vec::new();
        let mut writes = 0;
        for t in tasks {
            let (lat, k) = t.await.unwrap();
            latencies_ns.extend(lat);
            writes += k;
        }
        let elapsed = start.elapsed();
        let cpu_s = cpu0.zip(cpu_s()).map(|(a, b)| b - a);
        let _ = stop_tx.send(true);
        let mut reports = Vec::new();
        for s in subs {
            reports.push(s.await.unwrap());
        }
        latencies_ns.sort_unstable();
        Report { scenario, latencies_ns, writes, elapsed, cpu_s, subs: reports }
    })
}

fn memory() {
    let rss0 = rss();
    let layouts: Vec<TagLayout> = (0..DEVICES).map(layout).collect();
    let rss1 = rss();
    let store = TagStore::new();
    let writers: Vec<DeviceWriter> = layouts.into_iter().map(|l| store.register_layout(l)).collect();
    let ts = now_utc();
    for w in &writers {
        w.update_indexed((0..TAGS_PER_DEVICE).map(|t| (t as TagIndex, TagValue::good(Value::Float(t as f64), ts))));
    }
    let rss2 = rss();
    let tags = (DEVICES * TAGS_PER_DEVICE) as f64;
    println!("## Memory ({} devices x {} tags = {} tags, float values)\n", DEVICES, TAGS_PER_DEVICE, tags);
    println!("- size_of::<TagValue>() = {} B (value, quality, source_ts, server_ts)", std::mem::size_of::<TagValue>());
    match (rss0, rss1, rss2) {
        (Some(a), Some(b), Some(c)) => {
            println!(
                "- tag layouts (names + name index): {:.1} MB, {:.0} B/tag",
                (b - a) as f64 / 1e6,
                (b - a) as f64 / tags
            );
            println!(
                "- slots (values, version, lock):    {:.1} MB, {:.0} B/tag",
                (c - b) as f64 / 1e6,
                (c - b) as f64 / tags
            );
        }
        _ => println!("- RSS not available on this platform"),
    }
    let batch_bytes = std::mem::size_of::<(TagIndex, TagValue)>() * BLOCK + 64;
    println!(
        "- bus worst case (a subscriber {} batches behind): {} x ~{} B = {:.1} MB; nothing is retained once every subscriber has read a batch\n",
        store.capacity(),
        store.capacity(),
        batch_bytes,
        (store.capacity() * batch_bytes) as f64 / 1e6
    );
    drop(writers);
}

fn main() {
    let secs: f64 = std::env::var("OPENCAP_BENCH_SECONDS").ok().and_then(|s| s.parse().ok()).unwrap_or(10.0);
    let duration = Duration::from_secs_f64(secs);
    println!("# opencap tag store load benchmark\n");
    println!(
        "{} tags / {} devices, offered {} tag updates/s from {} writer tasks in {}-tag batches, tokio runtime with {} worker threads, {} logical CPUs, {:.0} s per scenario\n",
        DEVICES * TAGS_PER_DEVICE,
        DEVICES,
        UPDATES_PER_S,
        WRITERS,
        BLOCK,
        RUNTIME_THREADS,
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
        secs
    );
    memory();
    println!("## Writer latency (one `update_indexed` of a 25-tag batch)\n");
    println!("| Scenario | updates/s achieved | p50 µs | p99 µs | p99.9 µs | max µs | CPU (cores) | subscribers: batches / lagged (resyncs) |");
    println!("|---|---|---|---|---|---|---|---|");
    for scenario in [Scenario::NoSubscribers, Scenario::FourFast, Scenario::ThreeFastOneSlow] {
        let r = run(scenario, duration);
        let l = &r.latencies_ns;
        let subs: Vec<String> = r
            .subs
            .iter()
            .map(|s| format!("{}{} / {} ({})", if s.slow { "slow " } else { "" }, s.batches, s.lagged, s.resyncs))
            .collect();
        println!(
            "| {} | {:.0} | {:.2} | {:.2} | {:.2} | {:.0} | {} | {} |",
            r.scenario.label(),
            r.writes as f64 * BLOCK as f64 / r.elapsed.as_secs_f64(),
            pct(l, 0.50),
            pct(l, 0.99),
            pct(l, 0.999),
            pct(l, 1.0),
            r.cpu_s.map(|c| format!("{:.2}", c / r.elapsed.as_secs_f64())).unwrap_or_else(|| "n/a".into()),
            if subs.is_empty() { "-".to_string() } else { subs.join("; ") }
        );
    }
}
