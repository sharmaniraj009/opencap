//! [`PluginRegistry`]: which devices run in a plugin, their hosts and their plans.
//!
//! A device with `plugin: python` runs its driver in the Python driver host. Each channel
//! with such devices gets one host process (one channel executor, like the Python engine,
//! D-015), started on first use and supervised by its [`PluginHost`].
//!
//! The registry keeps `Driver::plan()` pure: [`PluginRegistry::prepare`] runs before the
//! engine builds its registry (start, reload, `check`/`read`/`identity`) and asks each host
//! to `Plan` every rate group of every routed device whose config changed. The driver
//! factory then hands out [`PluginDriver`]s that answer `plan()` from those plans. A device
//! that cannot be planned (bad params, no Python, host does not start) gets a factory error,
//! so it is listed and not polled, like a driver that does not exist.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use tracing::{info, warn};

use opencap_core::config::{Channel, Device, PythonPluginCfg};
use opencap_core::driver::{BlockRef, Driver, DriverFactory, Plan};

use crate::convert::Info;
use crate::driver::{DevicePlans, PluginBlock, PluginDriver};
use crate::host::{LaunchSpec, Launcher, PluginHost, ProcessLauncher, Transport};
use crate::proto;

pub const PYTHON: &str = "python";
const PLAN_TIMEOUT: Duration = Duration::from_secs(30);

/// Devices (with their config fingerprint) to plan on one host.
type PlanWork = (Arc<PluginHost>, Vec<(Device, String)>);

struct Prepared {
    fingerprint: String,
    host: Arc<PluginHost>,
    result: Result<Arc<DevicePlans>, String>,
}

#[derive(Default)]
struct State {
    cfg: Option<PythonPluginCfg>,
    hosts: HashMap<String, Arc<PluginHost>>,
    prepared: HashMap<String, Prepared>,
}

pub struct PluginRegistry {
    launcher: Arc<dyn Launcher>,
    /// New for every engine run: authenticates calls, and the Python host signs block
    /// payloads with it (proto/README.md).
    token: String,
    state: StdMutex<State>,
}

/// True when the device's driver runs in the Python driver host.
pub fn routed(device: &Device) -> bool {
    device.plugin.as_deref() == Some(PYTHON)
}

fn random_token() -> String {
    let bytes: [u8; 32] = rand::random();
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

impl PluginRegistry {
    pub fn new() -> Arc<Self> {
        Self::with_launcher(Arc::new(ProcessLauncher))
    }

    pub fn with_launcher(launcher: Arc<dyn Launcher>) -> Arc<Self> {
        Arc::new(Self { launcher, token: random_token(), state: StdMutex::new(State::default()) })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// The current host of a channel.
    pub fn host(&self, channel: &str) -> Option<Arc<PluginHost>> {
        self.lock().hosts.get(channel).cloned()
    }

    /// Bring hosts and plans in line with `channels` (the channels the engine will run):
    /// start or replace hosts as needed, stop the ones no channel needs, and plan every
    /// routed device that is new, changed, or failed before.
    pub async fn prepare(&self, cfg: &PythonPluginCfg, channels: &[Channel]) {
        let mut wanted: BTreeMap<&str, Vec<&Device>> = BTreeMap::new();
        for ch in channels {
            for d in ch.devices.iter().filter(|d| routed(d)) {
                wanted.entry(ch.name.as_str()).or_default().push(d);
            }
        }
        let (stop, work) = {
            let mut st = self.lock();
            let mut stop: Vec<Arc<PluginHost>> = Vec::new();
            if st.cfg.as_ref() != Some(cfg) {
                stop.extend(st.hosts.drain().map(|(_, h)| h));
                st.prepared.clear();
                st.cfg = Some(cfg.clone());
            }
            for (ch, devs) in &wanted {
                let drivers: Vec<String> =
                    devs.iter().map(|d| d.driver.clone()).collect::<BTreeSet<_>>().into_iter().collect();
                if st.hosts.get(*ch).is_some_and(|h| h.spec().drivers == drivers) {
                    continue;
                }
                stop.extend(st.hosts.remove(*ch));
                let spec = LaunchSpec {
                    label: ch.to_string(),
                    command: cfg.command_or_default(),
                    drivers,
                    transport: Transport::from_config(&cfg.transport),
                    start_timeout: Duration::from_millis(cfg.start_timeout_ms.max(1000) as u64),
                    token: self.token.clone(),
                };
                st.hosts.insert(ch.to_string(), PluginHost::new(spec, self.launcher.clone()));
            }
            let gone: Vec<String> = st.hosts.keys().filter(|k| !wanted.contains_key(k.as_str())).cloned().collect();
            for k in gone {
                stop.extend(st.hosts.remove(&k));
            }
            let ids: HashSet<&str> = wanted.values().flatten().map(|d| d.id.as_str()).collect();
            st.prepared.retain(|id, _| ids.contains(id.as_str()));
            let mut work: Vec<PlanWork> = Vec::new();
            for (ch, devs) in &wanted {
                let host = st.hosts[*ch].clone();
                let todo: Vec<(Device, String)> =
                    devs.iter()
                        .filter_map(|d| {
                            let fp = serde_json::to_string(d).expect("a device serializes");
                            let fresh = st.prepared.get(&d.id).is_some_and(|p| {
                                p.fingerprint == fp && p.result.is_ok() && Arc::ptr_eq(&p.host, &host)
                            });
                            (!fresh).then(|| ((*d).clone(), fp))
                        })
                        .collect();
                if !todo.is_empty() {
                    work.push((host, todo));
                }
            }
            (stop, work)
        };
        for host in stop {
            host.shutdown().await;
        }
        // Hosts start and plan in parallel; the devices of one host one after another.
        let done = futures_util::future::join_all(work.into_iter().map(|(host, devices)| async move {
            let mut out = Vec::new();
            for (device, fp) in devices {
                let result = plan_device(&host, &device, &fp).await.map(Arc::new);
                match &result {
                    Ok(p) => {
                        info!(device = %device.id, plugin = PYTHON, groups = p.groups.len(), "plugin.device_planned")
                    }
                    Err(e) => warn!(device = %device.id, plugin = PYTHON, error = %e, "plugin.plan_failed"),
                }
                out.push((device.id, Prepared { fingerprint: fp, host: host.clone(), result }));
            }
            out
        }))
        .await;
        let mut st = self.lock();
        for (id, p) in done.into_iter().flatten() {
            st.prepared.insert(id, p);
        }
    }

    /// `base` for every device, a [`PluginDriver`] for routed ones.
    pub fn factory(self: &Arc<Self>, base: DriverFactory) -> DriverFactory {
        let reg = self.clone();
        Arc::new(move |device: &Device| {
            if !routed(device) {
                return base(device);
            }
            reg.driver(device).map(|d| Box::new(d) as Box<dyn Driver>)
        })
    }

    fn driver(self: &Arc<Self>, device: &Device) -> Result<PluginDriver, String> {
        let json = serde_json::to_string(device).map_err(|e| e.to_string())?;
        let plans = {
            let st = self.lock();
            let p = st
                .prepared
                .get(&device.id)
                .filter(|p| p.fingerprint == json)
                .ok_or_else(|| format!("{PYTHON} plugin: the device was not prepared for this config"))?;
            p.result.clone().map_err(|e| format!("{PYTHON} plugin: {e}"))?
        };
        Ok(PluginDriver::new(device, json, self.clone(), plans))
    }

    /// Stop every host (engine stop, CLI exit).
    pub async fn shutdown(&self) {
        let hosts: Vec<Arc<PluginHost>> = {
            let mut st = self.lock();
            st.prepared.clear();
            st.hosts.drain().map(|(_, h)| h).collect()
        };
        for h in hosts {
            h.shutdown().await;
        }
    }
}

/// `Plan` every rate group of one device.
async fn plan_device(host: &PluginHost, device: &Device, json: &str) -> Result<DevicePlans, String> {
    let (mut client, generation) = host.ensure().await.map_err(|e| format!("plugin host {e}"))?;
    let mut plans = DevicePlans::default();
    for (_rate, tags) in device.rate_groups() {
        let names: Vec<String> = tags.iter().map(|t| t.name.clone()).collect();
        let req =
            proto::PlanRequest { device: Some(proto::DeviceConfig { json: json.to_string() }), tags: names.clone() };
        let reply = match tokio::time::timeout(PLAN_TIMEOUT, client.plan(req)).await {
            Err(_) => return Err("Plan timed out".into()),
            Ok(Err(status)) => {
                host.failed(generation, status.message()).await;
                return Err(format!("plugin host failed: {:?}: {}", status.code(), status.message()));
            }
            Ok(Ok(r)) => r.into_inner(),
        };
        if let Some(e) = reply.error {
            return Err(e.message);
        }
        if let Some(info) = &reply.info {
            plans.info = Info::from(info);
        }
        let plan = Plan {
            blocks: reply.blocks.into_iter().map(|b| Arc::new(PluginBlock::from(b)) as BlockRef).collect(),
            invalid: reply.invalid.into_iter().collect(),
        };
        plans.groups.insert(names, plan);
    }
    Ok(plans)
}
