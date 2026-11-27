//! The stack benchmark gate (docs/architecture/04-scale-reliability.md section 4.1): address-space
//! memory per node and build time for 250k tags, and the cost of copying a full update into it.
//! `#[ignore]`d (it takes seconds and 100s of MB):
//! `cargo test --release -p opencap-north-opcua --test bench -- --ignored --nocapture`.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio_util::sync::CancellationToken;

use opencap_core::config::{DType, Device, OpcUaServerCfg, Tag};
use opencap_core::health::{HealthStatus, State};
use opencap_core::store::TagStore;
use opencap_core::value::{now_utc, Quality, TagValue, Value};
use opencap_north::plugin::{DeviceStatusReader, NorthContext, NorthPlugin};
use opencap_north_opcua::OpcUaNorth;

fn rss_mb() -> f64 {
    let s = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    s.lines()
        .find(|l| l.starts_with("VmRSS:"))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|k| k.parse::<f64>().ok())
        .unwrap_or(0.0)
        / 1024.0
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn address_space_memory_and_update_cost_at_250k_tags() {
    let (devices, per_device) = (250usize, 1000usize);
    let store = Arc::new(TagStore::with_options(512, None));
    let now = now_utc();
    let mut devs = vec![];
    let mut writers = vec![];
    for d in 0..devices {
        let tags: Vec<Tag> =
            (0..per_device).map(|i| Tag::new(&format!("G{}.T{i}", i % 10), "40001", DType::Float64)).collect();
        let dev = Device::new(&format!("S/D{d}"), "S", &format!("D{d}"), "modbus_tcp", "192.0.2.1", 502, tags);
        let w = store.register(&dev);
        let tv = TagValue::new(Some(Value::Float(1.0)), Quality::Good, Some(now));
        let names: Vec<String> = dev.tags.iter().map(|t| t.name.clone()).collect();
        w.update(names.iter().map(|n| (n.as_str(), &tv)));
        writers.push((w, names));
        devs.push(dev);
    }
    let before = rss_mb();
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let cfg = OpcUaServerCfg {
        enabled: true,
        endpoint: format!("opc.tcp://127.0.0.1:{port}/opencap"),
        anonymous: true,
        update_ms: 250,
        ..Default::default()
    };
    let north = OpcUaNorth::new(cfg, devs, &|_| None).unwrap();
    let status = DeviceStatusReader::new(|_| Some(HealthStatus { state: State::Ok, last_good: None, errors: 0 }));
    let mut ctx = NorthContext::new("opcua", store.reader(), status);
    let shutdown = CancellationToken::new();
    ctx.shutdown = shutdown.clone();
    let started = Instant::now();
    let task = tokio::spawn(async move { Box::new(north).run(ctx).await.unwrap() });
    let mut up = None;
    for _ in 0..600 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
            up = Some(started.elapsed());
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let up = up.expect("server did not start");
    tokio::time::sleep(Duration::from_secs(2)).await; // initial value sync
    let after = rss_mb();
    let nodes = devices * (per_device + 12); // variables + group folders + device folder
    println!(
        "250k tags: server up in {:.2}s, RSS +{:.0} MB = {:.2} KB per variable",
        up.as_secs_f64(),
        after - before,
        (after - before) * 1024.0 / (devices * per_device) as f64
    );
    let _ = nodes;

    // a full change of every tag, copied into the address space
    let tv = TagValue::new(Some(Value::Float(2.0)), Quality::Good, Some(now_utc()));
    let t = Instant::now();
    for (w, names) in &writers {
        w.update(names.iter().map(|n| (n.as_str(), &tv)));
    }
    println!("store update of 250k tags: {:.0} ms", t.elapsed().as_secs_f64() * 1000.0);
    tokio::time::sleep(Duration::from_secs(3)).await;
    println!("RSS after a full update: +{:.0} MB", rss_mb() - before);
    shutdown.cancel();
    let _ = task.await;
}
