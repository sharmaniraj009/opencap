//! Poller runtime (Python `opencap.core.runtime.Poller`): wires config, registry, channel
//! workers, the tag store, the MQTT north plugin and the API. SIGHUP / `POST /reload` re-read the config
//! and restart only the channels that changed.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex as StdMutex, OnceLock, RwLock, Weak};
use std::time::Duration;

use anyhow::{Context as _, Result};
use async_trait::async_trait;
use indexmap::IndexMap;
use serde_json::{json, Value};
use tokio::task::JoinHandle;
use tracing::{error, info, warn};

use opencap_api::{EngineView, ReadOnceError};
use opencap_core::config::{load_config, Channel, LoadedConfig};
use opencap_core::driver::DriverFactory;
use opencap_core::registry::Registry;
use opencap_core::store::TagStore;
use opencap_core::worker::{ChannelWorker, WorkerOptions};
use opencap_core::FIRM_VERSION;
use opencap_north::mqtt::{LinkOptions, MqttLink};
use opencap_north::plugin::{self, DeviceStatusReader, NorthContext, NorthHandle};
use opencap_north::publisher::{Publisher, PublisherOptions, Sink};
use opencap_north::saf::StoreAndForward;
use opencap_plugin::PluginRegistry;

/// Agent name of the MQTT link's publisher (`north_lagged_total{agent}`).
pub const MQTT_AGENT: &str = "mqtt";
/// How long a stopping north plugin may take before it is aborted.
const NORTH_STOP_GRACE: Duration = Duration::from_secs(5);

fn lock<T>(m: &StdMutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

struct RtState {
    registry: Registry,
    workers: IndexMap<String, Arc<ChannelWorker>>,
    channel_cfg: HashMap<String, String>,
}

pub struct Poller {
    config_dir: PathBuf,
    only: Option<HashSet<String>>,
    factory: DriverFactory,
    /// Hosts and plans of `plugin: python` devices; `factory` routes those devices to it.
    plugins: Arc<PluginRegistry>,
    loaded: RwLock<LoadedConfig>,
    store: Arc<TagStore>,
    state: RwLock<RtState>,
    link: StdMutex<Option<Arc<MqttLink>>>,
    link_task: StdMutex<Option<JoinHandle<()>>>,
    north: tokio::sync::Mutex<Option<NorthHandle>>,
    /// What the Sparkplug edge node was started from, and the node (#51).
    sparkplug: tokio::sync::Mutex<(Option<Value>, Option<NorthHandle>)>,
    opcua: tokio::sync::Mutex<(Option<Value>, Option<NorthHandle>)>,
    reload_lock: tokio::sync::Mutex<()>,
    me: Weak<Poller>,
    /// Readiness (D-075): process start, end of `start()` (north side up), and the latched
    /// initial-scan duration (set once, never cleared).
    t0: std::time::Instant,
    started_at: OnceLock<std::time::Instant>,
    ready_ms: OnceLock<u64>,
}

fn rlock<T>(l: &RwLock<T>) -> std::sync::RwLockReadGuard<'_, T> {
    l.read().unwrap_or_else(|e| e.into_inner())
}

fn wlock<T>(l: &RwLock<T>) -> std::sync::RwLockWriteGuard<'_, T> {
    l.write().unwrap_or_else(|e| e.into_inner())
}

impl Poller {
    pub fn new(
        config_dir: PathBuf,
        loaded: LoadedConfig,
        only: Option<Vec<String>>,
        factory: DriverFactory,
        plugins: Arc<PluginRegistry>,
    ) -> Arc<Self> {
        Arc::new_cyclic(|me| Self {
            config_dir,
            only: only.map(|v| v.into_iter().collect()),
            state: RwLock::new(RtState {
                registry: Registry::build(&[], &factory),
                workers: IndexMap::new(),
                channel_cfg: HashMap::new(),
            }),
            factory: plugins.factory(factory),
            plugins,
            loaded: RwLock::new(loaded),
            store: Arc::new(TagStore::new()),
            link: StdMutex::new(None),
            link_task: StdMutex::new(None),
            north: tokio::sync::Mutex::new(None),
            sparkplug: tokio::sync::Mutex::new((None, None)),
            opcua: tokio::sync::Mutex::new((None, None)),
            reload_lock: tokio::sync::Mutex::new(()),
            t0: std::time::Instant::now(),
            started_at: OnceLock::new(),
            ready_ms: OnceLock::new(),
            me: me.clone(),
        })
    }

    fn link(&self) -> Option<Arc<MqttLink>> {
        lock(&self.link).clone()
    }

    pub async fn start(&self) -> Result<()> {
        let cfg = rlock(&self.loaded).poller.clone();
        let saf_path = PathBuf::from(&cfg.saf.path); // relative paths resolve from the working directory
        if let Some(parent) = saf_path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).with_context(|| format!("cannot create {}", parent.display()))?;
            }
        }
        let saf = StoreAndForward::open(&saf_path, cfg.saf.max_bytes)
            .with_context(|| format!("cannot open the outbox {}", saf_path.display()))?;
        let link = Arc::new(MqttLink::new(
            cfg.mqtt.clone(),
            &cfg.instance,
            Arc::new(StdMutex::new(saf)),
            LinkOptions::default(),
        ));
        *lock(&self.link_task) = Some(tokio::spawn(link.clone().run()));
        *lock(&self.link) = Some(link);
        let channels = rlock(&self.loaded).channels.clone();
        self.plugins.prepare(&cfg.plugins.python, &self.selected(&channels)).await;
        self.apply_channels(&channels);
        self.restart_publisher().await;
        self.restart_sparkplug(true).await;
        self.restart_opcua(true).await;
        let devices: usize = self.active_channels().iter().map(|c| c.devices.len()).sum();
        info!(version = FIRM_VERSION, channels = rlock(&self.state).workers.len(), devices, "poller_started");
        let _ = self.started_at.set(std::time::Instant::now());
        self.check_ready();
        Ok(())
    }

    pub async fn stop(&self) {
        self.stop_publisher().await;
        if let Some(h) = self.sparkplug.lock().await.1.take() {
            h.stop(NORTH_STOP_GRACE).await;
        }
        if let Some(h) = self.opcua.lock().await.1.take() {
            h.stop(NORTH_STOP_GRACE).await;
        }
        let workers: Vec<Arc<ChannelWorker>> = wlock(&self.state).workers.drain(..).map(|(_, w)| w).collect();
        wlock(&self.state).channel_cfg.clear();
        for w in workers {
            w.stop().await;
        }
        self.plugins.shutdown().await;
        if let Some(link) = self.link() {
            link.stop();
            let task = lock(&self.link_task).take();
            if let Some(mut task) = task {
                if tokio::time::timeout(Duration::from_secs(5), &mut task).await.is_err() {
                    task.abort();
                }
            }
            lock(link.saf()).close();
        }
        info!("poller_stopped");
    }

    // --- channels ---------------------------------------------------------------

    fn selected(&self, channels: &[Channel]) -> Vec<Channel> {
        channels
            .iter()
            .filter(|c| c.enabled && self.only.as_ref().is_none_or(|o| o.contains(&c.name)))
            .cloned()
            .collect()
    }

    fn active_channels(&self) -> Vec<Channel> {
        self.selected(&rlock(&self.loaded).channels)
    }

    /// Start, stop or restart workers so they match `channels`. Returns what changed.
    fn apply_channels(&self, channels: &[Channel]) -> Value {
        let wanted: IndexMap<String, Channel> =
            self.selected(channels).into_iter().map(|c| (c.name.clone(), c)).collect();
        let new_cfg: HashMap<String, String> =
            wanted.iter().map(|(n, c)| (n.clone(), serde_json::to_string(c).expect("channel serializes"))).collect();
        let (mut started, mut restarted, mut stopped, mut unchanged) = (vec![], vec![], vec![], vec![]);
        let stats_interval_s = rlock(&self.loaded).poller.stats_interval_s;
        let mut st = wlock(&self.state);
        st.registry = Registry::build(&wanted.values().cloned().collect::<Vec<_>>(), &self.factory);
        let names: Vec<String> = st.workers.keys().cloned().collect();
        for name in names {
            if !wanted.contains_key(&name) {
                if let Some(w) = st.workers.shift_remove(&name) {
                    tokio::spawn(async move { w.stop().await });
                }
                stopped.push(name);
            }
        }
        for (name, ch) in &wanted {
            if st.workers.contains_key(name) && st.channel_cfg.get(name) == new_cfg.get(name) {
                unchanged.push(name.clone());
                continue;
            }
            if let Some(old) = st.workers.shift_remove(name) {
                tokio::spawn(async move { old.stop().await });
                restarted.push(name.clone());
            } else {
                started.push(name.clone());
            }
            // Every device gets a slot (disabled ones too, so the API can list their tags);
            // the worker registers the devices it polls again and keeps their writers,
            // which makes the previous worker's writers stale (ADR-0005: one writer).
            for dev in &ch.devices {
                self.store.register(dev);
            }
            let plans = ch
                .devices
                .iter()
                .filter_map(|d| st.registry.plans.get(&d.id).map(|p| (d.id.clone(), p.clone())))
                .collect();
            let me = self.me.clone();
            let on_cycle: opencap_core::worker::OnCycle = Arc::new(move |_: &str| {
                if let Some(p) = me.upgrade() {
                    p.check_ready();
                }
            });
            let opts = WorkerOptions { stats_interval_s, on_cycle: Some(on_cycle), ..Default::default() };
            let worker = ChannelWorker::new(ch, &plans, self.store.clone(), self.factory.clone(), opts);
            worker.start();
            st.workers.insert(name.clone(), worker);
        }
        st.channel_cfg = new_cfg;
        json!({"started": started, "restarted": restarted, "stopped": stopped, "unchanged": unchanged})
    }

    // --- publisher --------------------------------------------------------------

    /// (Re)start the `mqtt` north plugin over the active devices; the MQTT link (connection,
    /// outbox) is kept.
    async fn restart_publisher(&self) {
        let mut north = self.north.lock().await;
        if let Some(old) = north.take() {
            old.stop(NORTH_STOP_GRACE).await;
        }
        let Some(link) = self.link() else { return };
        let devices = self.active_channels().into_iter().flat_map(|c| c.devices).filter(|d| d.enabled).collect();
        let me = self.me.clone();
        let status = DeviceStatusReader::new(move |id: &str| {
            me.upgrade().and_then(|p| p.worker_for(id)).and_then(|w| w.health(id))
        });
        let (cfg, prefix) = {
            let l = rlock(&self.loaded);
            (l.poller.payload.clone(), l.poller.mqtt.topic_prefix.clone())
        };
        let outbox = link.saf().clone();
        let sink: Arc<dyn Sink> = link;
        let publisher = Publisher::new(devices, sink, cfg, PublisherOptions::default(), &prefix);
        let ctx = NorthContext::new(MQTT_AGENT, self.store.reader(), status).with_outbox(outbox);
        *north = Some(plugin::spawn(Box::new(publisher), ctx));
    }

    /// (Re)start the Sparkplug edge node (#51) when its config, the broker settings or the
    /// device set changed (its births list every metric), or always with `force`. A new node is a
    /// new MQTT session: NDEATH of the old one, then CONNECT and births.
    async fn restart_sparkplug(&self, force: bool) {
        let (poller, devices) = {
            let l = rlock(&self.loaded);
            let devices: Vec<_> =
                self.active_channels().into_iter().flat_map(|c| c.devices).filter(|d| d.enabled).collect();
            (l.poller.clone(), devices)
        };
        let fingerprint = json!({"sparkplug": poller.sparkplug, "mqtt": poller.mqtt, "instance": poller.instance,
                                 "devices": devices});
        let mut guard = self.sparkplug.lock().await;
        if !force && guard.0.as_ref() == Some(&fingerprint) {
            return;
        }
        if let Some(h) = guard.1.take() {
            h.stop(NORTH_STOP_GRACE).await;
        }
        guard.0 = Some(fingerprint);
        if !poller.sparkplug.enabled {
            return;
        }
        let path = PathBuf::from(&poller.sparkplug.outbox); // relative paths resolve from the working directory
        let started = (|| -> Result<NorthHandle, String> {
            if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
                std::fs::create_dir_all(parent).map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
            }
            let saf = StoreAndForward::open(&path, poller.sparkplug.outbox_max_bytes)
                .map_err(|e| format!("cannot open the Sparkplug outbox {}: {e}", path.display()))?;
            let node = opencap_north_sparkplug::node::NodeConfig::build(&poller, &devices, FIRM_VERSION)?;
            let me = self.me.clone();
            let status = DeviceStatusReader::new(move |id: &str| {
                me.upgrade().and_then(|p| p.worker_for(id)).and_then(|w| w.health(id))
            });
            let ctx = NorthContext::new(opencap_north_sparkplug::node::KIND, self.store.reader(), status)
                .with_outbox(Arc::new(StdMutex::new(saf)));
            Ok(plugin::spawn(Box::new(opencap_north_sparkplug::node::SparkplugNode::new(node)), ctx))
        })();
        match started {
            Ok(h) => guard.1 = Some(h),
            Err(e) => error!(error = %e, "sparkplug_start_failed"),
        }
    }

    /// (Re)start the OPC UA server (#50) when its config or the device set changed (the address space
    /// is built from the devices), or always with `force`. Clients reconnect to the new server.
    async fn restart_opcua(&self, force: bool) {
        let (cfg, devices) = {
            let l = rlock(&self.loaded);
            let devices: Vec<_> =
                self.active_channels().into_iter().flat_map(|c| c.devices).filter(|d| d.enabled).collect();
            (l.poller.opcua_server.clone(), devices)
        };
        let fingerprint = json!({"opcua_server": cfg, "devices": devices});
        let mut guard = self.opcua.lock().await;
        if !force && guard.0.as_ref() == Some(&fingerprint) {
            return;
        }
        if let Some(h) = guard.1.take() {
            h.stop(NORTH_STOP_GRACE).await;
        }
        guard.0 = Some(fingerprint);
        if !cfg.enabled {
            return;
        }
        let endpoint = cfg.endpoint.clone();
        let north = match opencap_north_opcua::OpcUaNorth::new(cfg, devices, &|name| std::env::var(name).ok()) {
            Ok(n) => n,
            Err(e) => {
                error!(endpoint = %endpoint, error = %e, "opcua_server_start_failed");
                return;
            }
        };
        let me = self.me.clone();
        let status = DeviceStatusReader::new(move |id: &str| {
            me.upgrade().and_then(|p| p.worker_for(id)).and_then(|w| w.health(id))
        });
        let ctx = NorthContext::new(opencap_north_opcua::KIND, self.store.reader(), status);
        guard.1 = Some(plugin::spawn(Box::new(north), ctx));
    }

    async fn stop_publisher(&self) {
        if let Some(old) = self.north.lock().await.take() {
            old.stop(NORTH_STOP_GRACE).await;
        }
    }

    // --- reload -----------------------------------------------------------------

    /// SIGHUP / POST /reload: re-read config, restart only the channels that changed.
    pub async fn reload(&self) -> Result<Value, String> {
        let _guard = self.reload_lock.lock().await;
        let loaded = match load_config(&self.config_dir) {
            Ok(l) => l,
            Err(e) => {
                error!(error = %e, "reload_failed");
                return Err(e.to_string());
            }
        };
        if loaded.poller.mqtt != rlock(&self.loaded).poller.mqtt {
            // The link (connection, LWT topic) is built once at start; data and status topics
            // follow the new mqtt.topic_prefix now, everything else at restart (D-028).
            warn!(detail = "restart opencap to apply mqtt connection/LWT settings", "mqtt_settings_changed");
        }
        let channels = loaded.channels.clone();
        self.plugins.prepare(&loaded.poller.plugins.python, &self.selected(&channels)).await;
        *wlock(&self.loaded) = loaded;
        let changed = self.apply_channels(&channels);
        self.restart_publisher().await;
        self.restart_sparkplug(false).await;
        self.restart_opcua(false).await;
        let n = |k: &str| changed[k].as_array().map(|a| a.len()).unwrap_or(0);
        info!(
            started = n("started"),
            restarted = n("restarted"),
            stopped = n("stopped"),
            unchanged = n("unchanged"),
            "config_reloaded"
        );
        Ok(changed)
    }

    // --- readiness (D-075) --------------------------------------------------------

    /// Enabled, polled devices that have not finished one cycle and are not demoted.
    fn waiting_devices(&self) -> Vec<String> {
        let st = rlock(&self.state);
        let mut out = Vec::new();
        for ch in self.active_channels() {
            if let Some(w) = st.workers.get(&ch.name) {
                out.extend(ch.devices.iter().filter(|d| w.awaiting_first_cycle(&d.id)).map(|d| d.id.clone()));
            }
        }
        out
    }

    /// Latch the initial scan once the north side is up and nothing waits. Sticky: a device
    /// added by a later reload never turns a ready process unready.
    fn check_ready(&self) {
        if self.ready_ms.get().is_none() && self.started_at.get().is_some() && self.waiting_devices().is_empty() {
            let _ = self.ready_ms.set(self.t0.elapsed().as_millis() as u64);
        }
    }

    // --- queries ----------------------------------------------------------------

    fn worker_for(&self, device_id: &str) -> Option<Arc<ChannelWorker>> {
        let channel = device_id.split_once('/').map(|(c, _)| c).unwrap_or(device_id);
        let st = rlock(&self.state);
        if !st.registry.plans.contains_key(device_id) {
            return None;
        }
        st.workers.get(channel).filter(|w| w.has_device(device_id)).cloned()
    }

    fn device_status_json(w: &ChannelWorker, id: &str) -> Value {
        match w.device_status(id) {
            Some(s) => json!({
                "state": s.state.as_str(), "last_good": s.last_good, "errors": s.errors,
                "cycle_ms": s.cycle_ms, "last_error": s.last_error, "connected": s.connected,
            }),
            None => json!({"state": "offline"}),
        }
    }
}

#[async_trait]
impl EngineView for Poller {
    fn health(&self) -> Value {
        let mut states: IndexMap<String, u64> =
            [("ok".to_string(), 0), ("offline".to_string(), 0), ("demoted".to_string(), 0)].into();
        if let Some(channels) = self.channels_summary().as_array() {
            for ch in channels {
                for d in ch["devices"].as_array().into_iter().flatten() {
                    let state = d["state"].as_str().unwrap_or("offline").to_string();
                    *states.entry(state).or_insert(0) += 1;
                }
            }
        }
        let link = self.link();
        json!({
            "status": "ok",
            "version": FIRM_VERSION,
            "instance": rlock(&self.loaded).poller.instance,
            "mqtt_connected": link.as_ref().is_some_and(|l| l.connected()),
            "saf_queue": link.as_ref().map(|l| lock(l.saf()).count()).unwrap_or(0),
            "channels": rlock(&self.state).workers.len(),
            "devices": states,
            "initial_scan_complete": self.ready_ms.get().is_some(),
            "initial_scan_ms": self.ready_ms.get(),
        })
    }

    fn readiness(&self) -> Value {
        self.check_ready();
        let ready = self.ready_ms.get().is_some();
        let waiting = if ready { Vec::new() } else { self.waiting_devices() };
        if self.started_at.get().is_none() {
            return json!({"ready": false, "waiting": waiting, "reason": "starting"});
        }
        json!({"ready": ready, "waiting": waiting})
    }

    fn about(&self) -> Value {
        crate::support::about()
    }

    fn support(&self) -> Value {
        crate::support::support_info(Some(opencap_core::config::config_revision(&self.config_dir)))
    }

    fn channels_summary(&self) -> Value {
        let st = rlock(&self.state);
        let out: Vec<Value> = self
            .active_channels()
            .iter()
            .map(|ch| {
                let worker = st.workers.get(&ch.name);
                let devices: Vec<Value> = ch
                    .devices
                    .iter()
                    .map(|dev| {
                        let s = worker
                            .map(|w| Self::device_status_json(w, &dev.id))
                            .unwrap_or_else(|| json!({"state": "offline"}));
                        json!({"id": dev.id, "driver": dev.driver, "state": s["state"], "last_good": s.get("last_good"),
                               "errors": s.get("errors"), "cycle_ms": s.get("cycle_ms")})
                    })
                    .collect();
                json!({"name": ch.name, "devices": devices})
            })
            .collect();
        Value::Array(out)
    }

    fn device_detail(&self, device_id: &str) -> Option<Value> {
        let worker = self.worker_for(device_id)?;
        let dp = rlock(&self.state).registry.plans.get(device_id)?.clone();
        let dev = &dp.device;
        let mut out = json!({
            "id": dev.id, "channel": dev.channel, "driver": dev.driver, "host": dev.host, "port": dev.port,
            "enabled": dev.enabled, "tags": dev.tags.len(), "blocks": dp.blocks(),
            "bad_config": dp.invalid().keys().cloned().collect::<Vec<_>>(), "plan_error": dp.error,
            "topic": dev.publish.topic, "sub_device_id": dev.publish.sub_device_id,
        });
        if let (Value::Object(o), Value::Object(s)) = (&mut out, Self::device_status_json(&worker, device_id)) {
            o.extend(s);
        }
        Some(out)
    }

    fn device_values(&self, device_id: &str) -> Option<Value> {
        self.worker_for(device_id)?;
        let snap = self.store.snapshot(device_id);
        Some(Value::Object(snap.iter().map(|(n, tv)| (n.to_string(), tv.to_api_json())).collect()))
    }

    async fn read_once(&self, device_id: &str) -> Result<Value, ReadOnceError> {
        let worker = self.worker_for(device_id).ok_or(ReadOnceError::UnknownDevice)?;
        let snap = worker.read_once(device_id).await.map_err(|e| match e {
            opencap_core::worker::ReadOnceError::UnknownDevice(..) => ReadOnceError::UnknownDevice,
            other => ReadOnceError::Failed(other.to_string()),
        })?;
        Ok(Value::Object(snap.iter().map(|(n, tv)| (n.to_string(), tv.to_api_json())).collect()))
    }

    async fn reload(&self) -> Result<Value, String> {
        Poller::reload(self).await
    }

    fn metrics(&self) -> String {
        opencap_core::metrics::render()
    }
}
