//! Added latency of a read through the Python driver host (#49): for every block of one
//! device, the `Read` round trip as the engine sees it, minus the time the driver itself took
//! in the plugin (`ReadReply.driver_us`). Prints one JSON object.
//!
//! ```text
//! cargo run -p opencap-plugin --example plugin_read_latency -- CONFIG_DIR DEVICE_ID [ROUNDS]
//! ```
//!
//! The device must have `plugin: python`; `plugins.python` in poller.yaml says how to start the
//! host. Used by `tests/integration/test_rust_engine_plugin.py`.

use std::time::Instant;

use opencap_core::config::load_config;
use opencap_core::registry::plan_device;
use opencap_plugin::proto;
use opencap_plugin::{PluginBlock, PluginRegistry};

fn pct(sorted: &[f64], p: f64) -> f64 {
    sorted[((sorted.len() as f64 - 1.0) * p).round() as usize]
}

#[tokio::main]
async fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    let (dir, id) = (args.get(1).ok_or("CONFIG_DIR")?, args.get(2).ok_or("DEVICE_ID")?);
    let rounds: usize = args.get(3).map(|r| r.parse().map_err(|e| format!("ROUNDS: {e}"))).transpose()?.unwrap_or(200);
    let loaded = load_config(std::path::Path::new(dir)).map_err(|e| e.to_string())?;
    let channel = loaded.channels.iter().find(|c| c.devices.iter().any(|d| &d.id == id)).ok_or("unknown device")?;
    let device = channel.devices.iter().find(|d| &d.id == id).unwrap();
    let reg = PluginRegistry::new();
    reg.prepare(&loaded.poller.plugins.python, std::slice::from_ref(channel)).await;
    let plan = plan_device(device, &reg.factory(opencap_drivers_none()));
    if let Some(e) = plan.error {
        return Err(e);
    }
    let payloads: Vec<_> = plan
        .groups
        .iter()
        .flat_map(|g| g.plan.blocks.iter())
        .filter_map(|b| b.as_any().downcast_ref::<PluginBlock>().map(|b| b.payload.clone()))
        .collect();
    let (mut client, _) = reg.host(&channel.name).ok_or("no host")?.ensure().await.map_err(|e| e.to_string())?;
    let json = serde_json::to_string(device).unwrap();
    let open = proto::OpenRequest { device_id: id.clone(), device: Some(proto::DeviceConfig { json }) };
    client.open(open).await.map_err(|e| e.to_string())?;
    let connected = client.connect(proto::DeviceRef { device_id: id.clone() }).await.map_err(|e| e.to_string())?;
    if let Some(e) = connected.into_inner().error {
        return Err(format!("connect: {}", e.message));
    }
    let (mut rtt, mut overhead) = (Vec::new(), Vec::new());
    for round in 0..rounds + 10 {
        for payload in &payloads {
            let t0 = Instant::now();
            let reply = client
                .read(proto::ReadRequest { device_id: id.clone(), block: payload.clone() })
                .await
                .map_err(|e| e.to_string())?
                .into_inner();
            let us = t0.elapsed().as_secs_f64() * 1e6;
            if let Some(e) = reply.error {
                return Err(format!("read: {}", e.message));
            }
            if round >= 10 {
                rtt.push(us);
                overhead.push((us - reply.driver_us as f64).max(0.0));
            }
        }
    }
    // The same round trip with no work in the plugin: what gRPC itself costs.
    let mut health = Vec::new();
    for _ in 0..rtt.len() {
        let t0 = Instant::now();
        client.health(proto::HealthRequest {}).await.map_err(|e| e.to_string())?;
        health.push(t0.elapsed().as_secs_f64() * 1e6);
    }
    health.sort_by(f64::total_cmp);
    reg.shutdown().await;
    rtt.sort_by(f64::total_cmp);
    overhead.sort_by(f64::total_cmp);
    let ms = |us: f64| (us).round() / 1000.0; // microseconds -> milliseconds, 3 decimals
    println!(
        "{}",
        serde_json::json!({
            "reads": rtt.len(),
            "blocks": payloads.len(),
            "rtt_ms": {"p50": ms(pct(&rtt, 0.5)), "p99": ms(pct(&rtt, 0.99))},
            "overhead_ms": {"p50": ms(pct(&overhead, 0.5)), "p99": ms(pct(&overhead, 0.99)), "max": ms(pct(&overhead, 1.0))},
            "empty_call_ms": {"p50": ms(pct(&health, 0.5)), "p99": ms(pct(&health, 0.99))},
        })
    );
    Ok(())
}

/// Every device of the latency run is routed through the plugin.
fn opencap_drivers_none() -> opencap_core::driver::DriverFactory {
    std::sync::Arc::new(|d: &opencap_core::config::Device| Err(format!("{} is not routed through the plugin", d.id)))
}
