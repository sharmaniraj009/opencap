//! `check`: validate config, print the block plan and estimated cycle time per site.
//! Same output as Python's `opencap check` (src/opencap/core/check.py), line for line.

use std::collections::BTreeMap;
use std::path::Path;

use opencap_core::config::{load_config, ConfigError};
use opencap_core::driver::DriverFactory;
use opencap_core::pyfmt::{py_fixed, py_percent0};
use opencap_core::registry::{DevicePlan, Registry};

const MAX_WARNINGS_SHOWN: usize = 15;

fn fmt_rates(dp: &DevicePlan) -> String {
    let parts: Vec<String> = dp
        .groups
        .iter()
        .map(|g| if g.rate_ms % 1000 == 0 { format!("{}s", g.rate_ms / 1000) } else { format!("{}ms", g.rate_ms) })
        .collect();
    if parts.is_empty() {
        "-".into()
    } else {
        parts.join(",")
    }
}

fn trunc(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Returns the process exit code: 0 = config valid, 1 = config invalid. Lines go to `out`,
/// errors to `err`.
pub fn run_check(
    config_dir: &Path,
    site: Option<&str>,
    verbose: bool,
    factory: &DriverFactory,
    out: &mut dyn FnMut(String),
    err: &mut dyn FnMut(String),
) -> i32 {
    let loaded = match load_config(config_dir) {
        Ok(l) => l,
        Err(ConfigError::NotFound(e)) => {
            err(format!("config error: {e}"));
            return 1;
        }
        Err(ConfigError::Invalid(e)) => {
            err(format!("config invalid:\n{e}"));
            return 1;
        }
    };
    let mut channels = loaded.channels.clone();
    if let Some(site) = site {
        channels.retain(|c| c.name == site);
        if channels.is_empty() {
            err(format!("no channel named {}", opencap_core::pyfmt::py_repr_str(site)));
            return 1;
        }
    }
    let rtt = loaded.poller.check.est_rtt_ms;
    let registry = Registry::build(&channels, factory);

    let header = format!(
        "{:<44} {:<13} {:>5} {:>4} {:<9} {:>4} {:>4} {:>8} {:>6}",
        "SITE / DEVICE", "DRIVER", "TAGS", "BAD", "RATES", "BLK", "REQ", "CYCLE", "LOAD"
    );
    out(header.clone());
    out("-".repeat(header.chars().count()));

    let mut overloaded: Vec<String> = Vec::new();
    for channel in &channels {
        let mut site_req = 0usize;
        let mut site_load = 0.0f64;
        let mut site_cost_ms = 0.0f64;
        let shared = channel.devices.iter().filter(|d| d.enabled).count() > 1;
        if !channel.enabled {
            out(format!("{}  (channel disabled)", channel.name));
            continue;
        }
        out(channel.name.clone());
        for device in &channel.devices {
            let dp = &registry.plans[&device.id];
            let label = format!("  {}{}", device.name, if device.enabled { "" } else { " (disabled)" });
            if let Some(e) = &dp.error {
                out(format!("{:<44} {:<13} {:>5}  -- no plan: {e}", label, device.driver, device.tags.len()));
                continue;
            }
            let delay = dp.request_delay_ms(channel.inter_device_delay_ms, shared);
            let load = dp.est_load(rtt, delay);
            if device.enabled {
                site_req += dp.wire_requests();
                site_load += load;
                site_cost_ms += dp.est_cycle_ms(rtt, delay);
            }
            out(format!(
                "{:<44} {:<13} {:>5} {:>4} {:<9} {:>4} {:>4} {:>6}ms {:>6}",
                trunc(&label, 44),
                device.driver,
                dp.enabled_tags(),
                dp.invalid().len(),
                fmt_rates(dp),
                dp.blocks(),
                dp.wire_requests(),
                py_fixed(dp.est_cycle_ms(rtt, delay), 0),
                py_percent0(load)
            ));
            if verbose {
                for g in &dp.groups {
                    for block in &g.plan.blocks {
                        out(format!("      [{}ms] {}  <- {}", g.rate_ms, block.describe(), block.tags().join(", ")));
                    }
                }
                for (name, reason) in dp.invalid() {
                    out(format!("      bad_config {name}: {reason}"));
                }
            }
        }
        if channel.devices.len() > 1 {
            out(format!(
                "{:<44} {:<13} {:>5} {:>4} {:<9} {:>4} {:>4} {:>6}ms {:>6}",
                "  = site total",
                "",
                "",
                "",
                "",
                "",
                site_req,
                py_fixed(site_cost_ms, 0),
                py_percent0(site_load)
            ));
        }
        if site_load > 1.0 {
            overloaded.push(channel.name.clone());
        }
    }

    let plans: Vec<&DevicePlan> = registry.plans.values().map(|p| p.as_ref()).collect();
    let mut by_driver: BTreeMap<&str, usize> = BTreeMap::new();
    let mut planned: BTreeMap<&str, usize> = BTreeMap::new();
    for dp in &plans {
        *by_driver.entry(dp.device.driver.as_str()).or_default() += 1;
        if dp.ok() {
            *planned.entry(dp.device.driver.as_str()).or_default() += 1;
        }
    }
    out(String::new());
    out(format!(
        "channels: {}   devices: {}   excluded at import: {}",
        channels.len(),
        plans.len(),
        loaded.excluded.len()
    ));
    out(format!("assumed round trip per request: {} ms (poller.yaml check.est_rtt_ms)", py_fixed(rtt, 0)));
    if channels.iter().any(|c| c.inter_device_delay_ms != 0 || c.devices.iter().any(|d| d.inter_request_delay_ms != 0))
    {
        out("cycle and load include the inter-request delay of each device and, on a channel with several \
             devices, the inter-device delay for every request (an upper bound)"
            .to_string());
    }
    for (driver, n) in &by_driver {
        out(format!(
            "  {:<13} devices {:>3}   with block plan {:>3}",
            driver,
            n,
            planned.get(driver).copied().unwrap_or(0)
        ));
    }
    out(format!(
        "blocks: {}   wire requests per full pass: {}   bad_config tags: {}",
        plans.iter().map(|dp| dp.blocks()).sum::<usize>(),
        plans.iter().map(|dp| dp.wire_requests()).sum::<usize>(),
        plans.iter().map(|dp| dp.invalid().len()).sum::<usize>()
    ));
    if !overloaded.is_empty() {
        out(format!("OVERLOAD (estimated load > 100%, cycles will overrun): {}", overloaded.join(", ")));
    }
    if !loaded.warnings.is_empty() {
        out(format!("\n{} config warning(s):", loaded.warnings.len()));
        for w in loaded.warnings.iter().take(MAX_WARNINGS_SHOWN) {
            out(format!("  - {w}"));
        }
        if loaded.warnings.len() > MAX_WARNINGS_SHOWN {
            out(format!("  … {} more", loaded.warnings.len() - MAX_WARNINGS_SHOWN));
        }
    }
    0
}
