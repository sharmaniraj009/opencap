//! `opencap-engine`: the Rust engine binary.
//!
//! ```text
//! opencap-engine check [--config DIR] [--site NAME] [-v]   # block plan + estimated cycle time
//! opencap-engine read "Site 008/Device1" [--tag FLOW_1]    # one read, print values, no MQTT
//! opencap-engine identity "Site 008/Device1"               # vendor/model if the device reports it
//! opencap-engine run [--config DIR] [--site NAME ...]      # poll, publish, serve the API
//! opencap-engine drivers                                   # drivers this build implements (JSON)
//! opencap-engine service install|uninstall|run [--config]  # Windows: a native service (#57)
//! ```
//!
//! Under systemd (`deploy/opencapd.service`, `Type=notify`) `run` reports readiness once the
//! config is loaded and the API listens, and reload/stop progress (`sdnotify`).
//!
//! Reads the same config directory as the Python `opencap` CLI.

mod check;
mod logging;
mod runtime;
#[cfg(unix)]
mod sdnotify;
mod support;
mod systags;
#[cfg(windows)]
mod winsvc;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};
use tracing::{error, info};

use opencap_api::EngineView as _;
use opencap_core::config::{load_config, Channel, Device, LoadedConfig};
use opencap_core::driver::DriverFactory;
use opencap_core::registry::plan_device;
use opencap_core::store::TagStore;
use opencap_core::worker::{ChannelWorker, WorkerOptions};
use opencap_plugin::PluginRegistry;

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
    /// Support summary, safe to paste into a public issue: versions, platform, drivers, north
    /// interfaces and the config revision. Never an IP address, host name or secret.
    Info {
        #[arg(short, long, default_value = "config")]
        config: PathBuf,
        /// Print JSON instead of text
        #[arg(long)]
        json: bool,
    },
    /// The engine as a native Windows service (docs/deployment.md, "Rust engine on Windows").
    #[cfg(windows)]
    Service {
        #[command(subcommand)]
        action: winsvc::ServiceCmd,
    },
}

/// Where `configure_logging` writes: stdout, or this file (a Windows service has no console).
static LOG_FILE: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();

fn configure_logging(level: &str, instance: Option<&str>) {
    let lvl = match level.to_ascii_uppercase().as_str() {
        "DEBUG" => "debug",
        "WARNING" | "WARN" => "warn",
        "ERROR" | "CRITICAL" => "error",
        _ => "info",
    };
    // Library chatter stays at WARN unless DEBUG is asked for (Python logsetup does the same).
    let libs = if lvl == "debug" { "debug" } else { "warn" };
    let filter = format!("{lvl},rumqttc={libs},tokio_modbus={libs},hyper={libs},axum={libs}");
    if let Some(name) = instance {
        logging::set_instance(name);
    }
    let builder = tracing_subscriber::fmt()
        .event_format(logging::LineFormat::new())
        .with_env_filter(tracing_subscriber::EnvFilter::new(filter));
    let file = LOG_FILE.get().and_then(|path| std::fs::OpenOptions::new().create(true).append(true).open(path).ok());
    let _ = match file {
        Some(f) => builder.with_writer(std::sync::Mutex::new(f)).try_init(),
        None => builder.with_writer(std::io::stdout).try_init(),
    };
}

/// The driver factory for one-off commands: devices with `plugin: python` are prepared in
/// their plugin host first. Call `shutdown()` on the registry when done.
async fn factory_for(loaded: &LoadedConfig, channels: &[Channel]) -> (Arc<PluginRegistry>, DriverFactory) {
    let plugins = PluginRegistry::new();
    plugins.prepare(&loaded.poller.plugins.python, channels).await;
    let factory = plugins.factory(opencap_drivers::default_factory());
    (plugins, factory)
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
    configure_logging("WARNING", None);
    let (loaded, channel, device) = find_device(config_dir, device_id)?;
    let (plugins, factory) = factory_for(&loaded, std::slice::from_ref(&channel)).await;
    let plan = plan_device(&device, &factory);
    if let Some(e) = &plan.error {
        plugins.shutdown().await;
        bail!("{device_id}: {e}");
    }
    let single = Channel { devices: vec![device.clone()], ..channel };
    let store = Arc::new(TagStore::new());
    let plans = [(device.id.clone(), Arc::new(plan))].into_iter().collect();
    let worker = ChannelWorker::new(&single, &plans, store, factory, WorkerOptions::default());
    let result = worker.read_once(&device.id).await;
    worker.stop().await;
    plugins.shutdown().await;
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
    configure_logging("WARNING", None);
    let (loaded, channel, device) = find_device(config_dir, device_id)?;
    let (plugins, factory) = factory_for(&loaded, std::slice::from_ref(&channel)).await;
    let result = async {
        let mut driver = factory(&device).map_err(anyhow::Error::msg)?;
        driver.connect().await.map_err(|e| anyhow::anyhow!("{}", e.describe()))?;
        let identity = driver.identity().await;
        driver.close().await;
        Ok::<_, anyhow::Error>(identity)
    }
    .await;
    plugins.shutdown().await;
    let identity = result?;
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
        // Plugin hosts this build can start: a device with `plugin: python` runs any driver the
        // Python driver host serves, whether or not it has a Rust port (#49).
        "plugins": [opencap_plugin::registry::PYTHON],
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

/// `run`: poll, publish and serve the API until `stop` resolves (it names what stopped it).
/// `on_ready` runs once the config is loaded, the poller started and the API listens.
async fn cmd_run(
    config_dir: &Path,
    sites: Vec<String>,
    stop: impl std::future::Future<Output = &'static str>,
    on_ready: impl FnOnce(),
) -> Result<i32> {
    let loaded = load_config(config_dir).map_err(|e| anyhow::anyhow!("{e}"))?;
    let known: Vec<&str> = loaded.channels.iter().map(|c| c.name.as_str()).collect();
    let unknown: Vec<&String> = sites.iter().filter(|s| !known.contains(&s.as_str())).collect();
    if !unknown.is_empty() {
        eprintln!("unknown site(s): {}", unknown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "));
        return Ok(1);
    }
    configure_logging(&loaded.poller.log_level, Some(&loaded.poller.instance));
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
    // The process does not run without its API: every way the API cannot start is one
    // `api.bind_failed` line (D-079) and exit status 1.
    let bind_failed = |reason: &str, error: &str| {
        error!(bind = %api_cfg.bind, port = api_cfg.port, reason, error, "api.bind_failed");
        eprintln!("{error}"); // the operator running `run` by hand sees why it stopped
        Ok(1)
    };
    let auth = match opencap_api::Auth::new(opencap_api::token_from_env(&api_cfg.token_env), &specs, |name| {
        opencap_api::token_from_env(name)
    }) {
        Ok(a) => a,
        Err(e) => return bind_failed("config", &e),
    };
    if let Err(e) = opencap_api::check_bind(&api_cfg.bind, &auth, &api_cfg.token_env) {
        return bind_failed("config", &e);
    }
    let Ok(port) = u16::try_from(api_cfg.port) else { return bind_failed("config", "api.port out of range") };
    let listener = match opencap_api::bind_api(&api_cfg.bind, port).await {
        Ok(l) => l,
        Err(e) => return bind_failed(e.reason, &e.message),
    };

    let (open_mode, tokens) = (auth.is_open(), auth.token_count());
    let listener_port = listener.local_addr().map_or(api_cfg.port, |a| i64::from(a.port()));
    let only = if sites.is_empty() { None } else { Some(sites) };
    let poller = runtime::Poller::new(
        config_dir.to_path_buf(),
        loaded,
        only,
        opencap_drivers::default_factory(),
        PluginRegistry::new(),
    );
    poller.start().await?;
    let view: Arc<dyn opencap_api::EngineView> = poller.clone();
    let (api_stop_tx, api_stop_rx) = tokio::sync::oneshot::channel::<()>();
    let api = tokio::spawn(opencap_api::serve(listener, opencap_api::router(view, auth), async move {
        let _ = api_stop_rx.await;
    }));
    info!(
        bind = %api_cfg.bind,
        port = listener_port,
        open_mode = open_mode,
        tokens = tokens,
        health = %poller.health()["status"],
        "api_started"
    );
    on_ready();

    #[cfg(unix)]
    let mut hup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;
    tokio::pin!(stop);
    loop {
        #[cfg(unix)]
        {
            tokio::select! {
                sig = &mut stop => { info!(signal = sig, "poller_stopping"); break; }
                _ = hup.recv() => {
                    sdnotify::notify("RELOADING=1\nSTATUS=reloading the config");
                    let _ = poller.reload().await;
                    sdnotify::notify("READY=1\nSTATUS=running");
                }
            }
        }
        #[cfg(not(unix))]
        {
            let sig = (&mut stop).await;
            info!(signal = sig, "poller_stopping");
            break;
        }
    }
    #[cfg(unix)]
    sdnotify::notify("STOPPING=1");
    let _ = api_stop_tx.send(());
    let _ = tokio::time::timeout(std::time::Duration::from_secs(5), api).await;
    poller.stop().await;
    Ok(0)
}

fn main() {
    let cli = Cli::parse();
    // The SCM runs `service run` and its dispatcher owns the thread; it starts its own runtime.
    #[cfg(windows)]
    if let Cmd::Service { action } = cli.cmd {
        let code = match winsvc::dispatch(action) {
            Ok(c) => c,
            Err(e) => {
                eprintln!("{e:#}");
                1
            }
        };
        std::process::exit(code);
    }
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().expect("tokio runtime");
    let code = rt.block_on(async {
        match cli.cmd {
            Cmd::Check { config, site, verbose } => {
                // Devices with `plugin: python` are planned by their host; a config that does
                // not load is reported by run_check itself.
                let (plugins, factory) = match load_config(&config) {
                    Ok(loaded) => {
                        let channels: Vec<Channel> = loaded
                            .channels
                            .iter()
                            .filter(|c| site.as_deref().is_none_or(|s| s == c.name))
                            .cloned()
                            .collect();
                        factory_for(&loaded, &channels).await
                    }
                    Err(_) => (PluginRegistry::new(), opencap_drivers::default_factory()),
                };
                let code = check::run_check(
                    &config,
                    site.as_deref(),
                    verbose,
                    &factory,
                    &mut |line| println!("{line}"),
                    &mut |line| eprintln!("{line}"),
                );
                plugins.shutdown().await;
                Ok(code)
            }
            Cmd::Read { device_id, tag, config } => cmd_read(&config, &device_id, tag.as_deref()).await,
            Cmd::Identity { device_id, config } => cmd_identity(&config, &device_id).await,
            Cmd::Run { config, site } => {
                #[cfg(unix)]
                let ready = || sdnotify::notify("READY=1\nSTATUS=running");
                #[cfg(not(unix))]
                let ready = || {};
                cmd_run(&config, site, shutdown_signal(), ready).await
            }
            Cmd::Drivers => {
                println!("{}", drivers_json());
                Ok(0)
            }
            Cmd::Info { config, json } => {
                // A broken or missing config still gets the build information.
                let revision = load_config(&config).ok().map(|_| opencap_core::config::config_revision(&config));
                let info = support::support_info(revision);
                if json {
                    println!("{}", serde_json::to_string_pretty(&info)?);
                } else {
                    println!("{}", support::render_text(&info));
                }
                Ok(0)
            }
            #[cfg(windows)]
            Cmd::Service { .. } => unreachable!("handled before the runtime starts"),
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

    /// The process does not run without its API (D-079): a taken port, and open mode on a
    /// non-loopback bind, end `run` with status 1 before anything is started.
    #[tokio::test]
    async fn run_exits_1_when_the_api_cannot_start() {
        let dir = std::env::temp_dir().join(format!("opencap-bindfail-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("devices.json"), r#"{"channels": []}"#).unwrap();
        let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = taken.local_addr().unwrap().port();
        std::env::remove_var("OPENCAP_API_TOKEN");
        for (api, why) in [
            (format!("{{bind: 127.0.0.1, port: {port}}}"), "port in use"),
            ("{bind: 0.0.0.0, port: 0}".to_string(), "open mode on a public bind"),
            ("{bind: 127.0.0.1, port: 70000}".to_string(), "port out of range"),
        ] {
            std::fs::write(dir.join("poller.yaml"), format!("api: {api}\n")).unwrap();
            let code = cmd_run(&dir, Vec::new(), std::future::pending(), || panic!("must not become ready: {why}"))
                .await
                .unwrap();
            assert_eq!(code, 1, "{why}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn drivers_json_partitions_the_known_drivers() {
        let v = drivers_json();
        let names = |k: &str| -> Vec<String> {
            v[k].as_array().unwrap().iter().map(|s| s.as_str().unwrap().to_string()).collect()
        };
        let implemented = names("implemented");
        // the JSON lists follow KNOWN_DRIVERS order, `compiled_in` the driver table's: compare as sets
        let mut expected: Vec<String> = opencap_drivers::compiled_in().iter().map(|s| s.to_string()).collect();
        expected.sort();
        let mut got = implemented.clone();
        got.sort();
        assert_eq!(got, expected, "{v}");
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
