//! `opencap-engine`: the Rust engine binary.
//!
//! ```text
//! opencap-engine check [--config DIR] [--site NAME] [-v]   # block plan + estimated cycle time
//! opencap-engine read "Site 008/Device1" [--tag FLOW_1]    # one read, print values, no MQTT
//! opencap-engine identity "Site 008/Device1"               # vendor/model if the device reports it
//! opencap-engine run [--config DIR] [--site NAME ...]      # poll, publish, serve the API
//! opencap-engine drivers                                   # drivers this build implements (JSON)
//! ```
//!
//! Reads the same config directory as the Python `opencap` CLI.

mod check;
mod runtime;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{bail, Context as _, Result};
use clap::{Parser, Subcommand};
use tracing::info;

use opencap_api::EngineView as _;
use opencap_core::config::{load_config, Channel, Device, LoadedConfig};
use opencap_core::registry::plan_device;
use opencap_core::store::TagStore;
use opencap_core::worker::{ChannelWorker, WorkerOptions};

#[derive(Parser)]
#[command(name = "opencap-engine", version, about = "opencap Rust engine: read-only PLC poller -> MQTT")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Validate config, print the block plan and estimated cycle time per site.
    Check {
        #[arg(short, long, default_value = "config")]
        config: PathBuf,
        /// Only this channel/site
        #[arg(long)]
        site: Option<String>,
        /// Print every block
        #[arg(short, long)]
        verbose: bool,
    },
    /// One read of a device (all its tags), print values. No MQTT.
    Read {
        /// '<channel>/<device>', e.g. 'Site 008/Device1'
        device_id: String,
        /// Only print this tag
        #[arg(long)]
        tag: Option<String>,
        #[arg(short, long, default_value = "config")]
        config: PathBuf,
    },
    /// Vendor/model/firmware of a device, if its protocol reports it.
    Identity {
        device_id: String,
        #[arg(short, long, default_value = "config")]
        config: PathBuf,
    },
    /// Run the poller: poll PLCs, publish to MQTT, serve the admin API.
    Run {
        #[arg(short, long, default_value = "config")]
        config: PathBuf,
        /// Poll only this channel/site (repeatable); for per-site cut-over
        #[arg(long)]
        site: Vec<String>,
    },
    /// Print, as JSON, which of the drivers the config schema knows this build implements.
    /// The acceptance harness (tests/helpers/engine.py) skips suites for drivers not ported yet.
    Drivers,
}

fn configure_logging(level: &str) {
    let lvl = match level.to_ascii_uppercase().as_str() {
        "DEBUG" => "debug",
        "WARNING" | "WARN" => "warn",
        "ERROR" | "CRITICAL" => "error",
        _ => "info",
    };
    // Library chatter stays at WARN unless DEBUG is asked for (Python logsetup does the same).
    let libs = if lvl == "debug" { "debug" } else { "warn" };
    let filter = format!("{lvl},rumqttc={libs},tokio_modbus={libs},hyper={libs},axum={libs}");
    let _ = tracing_subscriber::fmt()
        .json()
        .flatten_event(true)
        .with_current_span(false)
        .with_span_list(false)
        .with_target(true)
        .with_writer(std::io::stdout)
        .with_env_filter(tracing_subscriber::EnvFilter::new(filter))
        .try_init();
}

fn find_device(config_dir: &Path, device_id: &str) -> Result<(LoadedConfig, Channel, Device)> {
    let loaded = load_config(config_dir).map_err(|e| anyhow::anyhow!("{e}"))?;
    for channel in &loaded.channels {
        if let Some(device) = channel.devices.iter().find(|d| d.id == device_id) {
            let (c, d) = (channel.clone(), device.clone());
            return Ok((loaded, c, d));
        }
    }
    bail!("unknown device {} (ids look like '<channel>/<device>')", opencap_core::pyfmt::py_repr_str(device_id))
}

async fn cmd_read(config_dir: &Path, device_id: &str, tag: Option<&str>) -> Result<i32> {
    configure_logging("WARNING");
    let (_, channel, device) = find_device(config_dir, device_id)?;
    let factory = opencap_drivers::default_factory();
    let plan = plan_device(&device, &factory);
    if let Some(e) = &plan.error {
        bail!("{device_id}: {e}");
    }
    let single = Channel { devices: vec![device.clone()], ..channel };
    let store = Arc::new(TagStore::new());
    let plans = [(device.id.clone(), Arc::new(plan))].into_iter().collect();
    let worker = ChannelWorker::new(&single, &plans, store, factory, WorkerOptions::default());
    let result = worker.read_once(&device.id).await;
    worker.stop().await;
    let values = result.map_err(|e| anyhow::anyhow!("{e}"))?;
    let mut names: Vec<String> = match tag {
        Some(t) => vec![t.to_string()],
        None => values.keys().map(str::to_string).collect(),
    };
    if tag.is_none() {
        names.sort();
    }
    for name in &names {
        match values.get(name) {
            None => println!("{name:<40} (no such tag)"),
            Some(tv) => {
                let repr = tv.value.as_ref().map(|v| v.py_repr()).unwrap_or_else(|| "None".into());
                println!("{:<40} {:<10} {}", name, tv.quality.as_str(), repr);
            }
        }
    }
    let bad = values.values().filter(|tv| tv.quality != opencap_core::value::Quality::Good).count();
    Ok(if bad == 0 { 0 } else { 3 })
}

async fn cmd_identity(config_dir: &Path, device_id: &str) -> Result<i32> {
    configure_logging("WARNING");
    let (_, _, device) = find_device(config_dir, device_id)?;
    let mut driver = opencap_drivers::create_driver(&device).map_err(anyhow::Error::msg)?;
    driver.connect().await.map_err(|e| anyhow::anyhow!("{}", e.describe()))?;
    let identity = driver.identity().await;
    driver.close().await;
    println!("{}", serde_json::to_string_pretty(&identity)?);
    Ok(0)
}

/// Which of the schema's drivers (`KNOWN_DRIVERS`, in its order) this build can poll:
/// `implemented` (compiled in), `not_compiled_in` (a crate exists, its Cargo feature is off),
/// `not_implemented` (no Rust crate yet).
fn drivers_json() -> serde_json::Value {
    let compiled = opencap_drivers::compiled_in();
    let has_crate = |d: &str| opencap_drivers::DRIVERS.iter().any(|e| e.name == d);
    let pick = |f: &dyn Fn(&str) -> bool| -> Vec<&str> {
        opencap_core::config::KNOWN_DRIVERS.iter().copied().filter(|d| f(d)).collect()
    };
    serde_json::json!({
        "implemented": pick(&|d| compiled.contains(&d)),
        "not_compiled_in": pick(&|d| has_crate(d) && !compiled.contains(&d)),
        "not_implemented": pick(&|d| !has_crate(d)),
    })
}

async fn shutdown_signal() -> &'static str {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate()).expect("SIGTERM handler");
        let mut int = signal(SignalKind::interrupt()).expect("SIGINT handler");
        tokio::select! {
            _ = term.recv() => "SIGTERM",
            _ = int.recv() => "SIGINT",
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
        "ctrl-c"
    }
}

async fn cmd_run(config_dir: &Path, sites: Vec<String>) -> Result<i32> {
    let loaded = load_config(config_dir).map_err(|e| anyhow::anyhow!("{e}"))?;
    let known: Vec<&str> = loaded.channels.iter().map(|c| c.name.as_str()).collect();
    let unknown: Vec<&String> = sites.iter().filter(|s| !known.contains(&s.as_str())).collect();
    if !unknown.is_empty() {
        eprintln!("unknown site(s): {}", unknown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "));
        return Ok(1);
    }
    configure_logging(&loaded.poller.log_level);
    let api_cfg = loaded.poller.api.clone();
    let specs: Vec<opencap_api::TokenSpec> = api_cfg
        .tokens
        .iter()
        .map(|t| opencap_api::TokenSpec {
            name: t.name.clone(),
            role: match t.role {
                opencap_core::config::Role::Admin => opencap_api::Role::Admin,
                opencap_core::config::Role::Viewer => opencap_api::Role::Viewer,
            },
            token_env: t.token_env.clone(),
        })
        .collect();
    let auth = opencap_api::Auth::new(opencap_api::token_from_env(&api_cfg.token_env), &specs, |name| {
        opencap_api::token_from_env(name)
    })
    .map_err(anyhow::Error::msg)?;
    opencap_api::check_bind(&api_cfg.bind, &auth, &api_cfg.token_env).map_err(anyhow::Error::msg)?;
    let port = u16::try_from(api_cfg.port).context("api.port out of range")?;
    let listener = opencap_api::bind(&api_cfg.bind, port)
        .await
        .with_context(|| format!("cannot bind the API to {}:{}", api_cfg.bind, api_cfg.port))?;

    let only = if sites.is_empty() { None } else { Some(sites) };
    let poller = runtime::Poller::new(config_dir.to_path_buf(), loaded, only, opencap_drivers::default_factory());
    poller.start().await?;
    let view: Arc<dyn opencap_api::EngineView> = poller.clone();
    let (api_stop_tx, api_stop_rx) = tokio::sync::oneshot::channel::<()>();
    let api = tokio::spawn(opencap_api::serve(listener, opencap_api::router(view, auth), async move {
        let _ = api_stop_rx.await;
    }));
    info!(bind = %api_cfg.bind, port = api_cfg.port, health = %poller.health()["status"], "api_started");

    #[cfg(unix)]
    let mut hup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;
    let stop = shutdown_signal();
    tokio::pin!(stop);
    loop {
        #[cfg(unix)]
        {
            tokio::select! {
                sig = &mut stop => { info!(signal = sig, "poller_stopping"); break; }
                _ = hup.recv() => { let _ = poller.reload().await; }
            }
        }
        #[cfg(not(unix))]
        {
            let sig = (&mut stop).await;
            info!(signal = sig, "poller_stopping");
            break;
        }
    }
    let _ = api_stop_tx.send(());
    let _ = tokio::time::timeout(std::time::Duration::from_secs(5), api).await;
    poller.stop().await;
    Ok(0)
}

fn main() {
    let cli = Cli::parse();
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().expect("tokio runtime");
    let code = rt.block_on(async {
        match cli.cmd {
            Cmd::Check { config, site, verbose } => Ok(check::run_check(
                &config,
                site.as_deref(),
                verbose,
                &opencap_drivers::default_factory(),
                &mut |line| println!("{line}"),
                &mut |line| eprintln!("{line}"),
            )),
            Cmd::Read { device_id, tag, config } => cmd_read(&config, &device_id, tag.as_deref()).await,
            Cmd::Identity { device_id, config } => cmd_identity(&config, &device_id).await,
            Cmd::Run { config, site } => cmd_run(&config, site).await,
            Cmd::Drivers => {
                println!("{}", drivers_json());
                Ok(0)
            }
        }
    });
    let code = match code {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e:#}");
            1
        }
    };
    rt.shutdown_timeout(std::time::Duration::from_secs(2));
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drivers_json_partitions_the_known_drivers() {
        let v = drivers_json();
        let names = |k: &str| -> Vec<String> {
            v[k].as_array().unwrap().iter().map(|s| s.as_str().unwrap().to_string()).collect()
        };
        let implemented = names("implemented");
        let expected: Vec<String> = opencap_drivers::compiled_in().iter().map(|s| s.to_string()).collect();
        assert_eq!(implemented, expected, "{v}");
        let mut all: Vec<String> =
            implemented.iter().chain(&names("not_compiled_in")).chain(&names("not_implemented")).cloned().collect();
        all.sort();
        let mut known: Vec<String> = opencap_core::config::KNOWN_DRIVERS.iter().map(|s| s.to_string()).collect();
        known.sort();
        assert_eq!(all, known, "every known driver in exactly one list");
        #[cfg(feature = "driver-modbus-tcp")]
        assert!(implemented.contains(&"modbus_tcp".to_string()), "{v}");
    }
}
