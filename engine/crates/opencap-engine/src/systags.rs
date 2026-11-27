//! The system-tag task (spec `docs/spec/system-tags.md` section 2, #56): the one writer of the
//! system and diagnostic virtual devices in the tag store.
//!
//! Cost rules (D-095, #238): a device event recomputes that device's tags and its channel's and
//! nothing else; a once-a-second `tick` recomputes the node, north-agent and channel tags and the
//! devices that are in error (`_SecondsInError` counts). Counters are read from the per-device
//! atomics beside the metrics (`opencap_core::metrics::device_counts`), never from the registry.
//! Nothing here causes a wire request and nothing is writable (ADR-0008).

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex as StdMutex, Weak};
use std::time::Duration;

use chrono::{DateTime, Utc};
use tokio::sync::Notify;

use opencap_core::store::TagStore;
use opencap_core::system_tags::{
    channel_tags, device_tags, folder_of, node_tags, north_tags, ChannelState, DeviceState, NodeState, NorthState,
    State, SystemTagWriter, Tags,
};
use opencap_core::value::{now_utc, Clock};

/// Event updates that arrive together are computed once.
const COALESCE: Duration = Duration::from_millis(50);

fn lock<T>(m: &StdMutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// What the task needs of the running engine. The static parts (names, settings, tag counts) are
/// read once per config load; the dynamic parts are refreshed in place.
pub trait Source: Send + Sync {
    /// The state with everything the config fixes: names, effective device settings, tag counts.
    fn static_state(&self) -> State;
    /// Fill the dynamic fields of one device: status, counters, scanned tags.
    fn refresh_device(&self, channel: &ChannelState, dev: &mut DeviceState);
    /// Fill the dynamic fields of one channel (requests in flight).
    fn refresh_channel(&self, ch: &mut ChannelState);
    /// Fill the dynamic fields of the node and the north agents (sessions, link state, outbox).
    fn refresh_node(&self, node: &mut NodeState, north: &mut Vec<NorthState>);
}

struct Inner {
    state: State,
    /// Device id (`Plant A/PLC1`) -> (channel index, device index).
    devices: HashMap<String, (usize, usize)>,
    writer: SystemTagWriter,
}

pub struct SystemTags {
    source: Weak<dyn Source>,
    inner: StdMutex<Inner>,
    dirty: StdMutex<HashSet<String>>,
    wake: Notify,
    clock: Clock,
}

impl SystemTags {
    pub fn new(store: Arc<TagStore>, source: Weak<dyn Source>, clock: Option<Clock>) -> Arc<Self> {
        let state = State {
            now: String::new(),
            node: serde_json::from_value(serde_json::json!({
                "product_version": "", "engine": "rust", "instance": "", "project_name": "",
                "project_revision": "", "start_time": "1970-01-01T00:00:00.000Z", "timezone": "UTC"
            }))
            .expect("placeholder node state"),
            north: Vec::new(),
            channels: Vec::new(),
        };
        Arc::new(Self {
            source,
            inner: StdMutex::new(Inner { state, devices: HashMap::new(), writer: SystemTagWriter::new(store) }),
            dirty: StdMutex::new(HashSet::new()),
            wake: Notify::new(),
            clock: clock.unwrap_or_else(|| Arc::new(now_utc)),
        })
    }

    /// A device finished a cycle or changed health: recompute it (and its channel) soon.
    pub fn mark(&self, device_id: &str) {
        lock(&self.dirty).insert(device_id.to_string());
        self.wake.notify_one();
    }

    /// Re-read the static state (start, config reload) and write every tag; folders that no
    /// longer exist are removed.
    pub fn rebuild(&self) {
        let Some(source) = self.source.upgrade() else { return };
        let now = (self.clock)();
        let mut inner = lock(&self.inner);
        let mut state = source.static_state();
        state.now = opencap_core::value::iso_utc(now);
        inner.devices.clear();
        for (ci, ch) in state.channels.iter().enumerate() {
            for (di, d) in ch.devices.iter().enumerate() {
                inner.devices.insert(format!("{}/{}", ch.name, d.name), (ci, di));
            }
        }
        inner.state = state;
        let Inner { state, writer, .. } = &mut *inner;
        source.refresh_node(&mut state.node, &mut state.north);
        let mut tags = Tags::new();
        for ci in 0..state.channels.len() {
            Self::refresh_channel_devices(&*source, state, ci);
        }
        if let Err(e) = Self::all_tags(state, now, &mut tags) {
            tracing::warn!(error = %e, "system_tags_failed");
            return;
        }
        let folders: HashSet<String> = tags.keys().map(|p| folder_of(p).to_string()).collect();
        writer.apply(&tags, now);
        writer.retain(|f| folders.contains(f));
    }

    fn refresh_channel_devices(source: &dyn Source, state: &mut State, ci: usize) {
        let mut ch = std::mem::replace(&mut state.channels[ci], placeholder_channel());
        source.refresh_channel(&mut ch);
        let snapshot = ch.clone();
        for dev in ch.devices.iter_mut() {
            source.refresh_device(&snapshot, dev);
        }
        state.channels[ci] = ch;
    }

    fn all_tags(state: &State, now: DateTime<Utc>, out: &mut Tags) -> Result<(), String> {
        node_tags(state, now, out)?;
        for agent in &state.north {
            north_tags(agent, now, out)?;
        }
        for ch in &state.channels {
            channel_tags(ch, out);
            for dev in &ch.devices {
                device_tags(ch, dev, now, out)?;
            }
        }
        Ok(())
    }

    /// The once-a-second pass: node, north agents, channels, and devices that are in error.
    pub fn tick(&self) {
        let Some(source) = self.source.upgrade() else { return };
        let now = (self.clock)();
        let mut inner = lock(&self.inner);
        let Inner { state, writer, .. } = &mut *inner;
        state.now = opencap_core::value::iso_utc(now);
        for ci in 0..state.channels.len() {
            Self::refresh_channel_devices(&*source, state, ci);
        }
        source.refresh_node(&mut state.node, &mut state.north);
        let mut tags = Tags::new();
        let result = (|| -> Result<(), String> {
            node_tags(state, now, &mut tags)?;
            for agent in &state.north {
                north_tags(agent, now, &mut tags)?;
            }
            for ch in &state.channels {
                channel_tags(ch, &mut tags);
                for dev in ch.devices.iter().filter(|d| d.status.as_ref().is_some_and(|s| s.consecutive_failures > 0)) {
                    device_tags(ch, dev, now, &mut tags)?;
                }
            }
            Ok(())
        })();
        match result {
            Ok(()) => {
                writer.apply(&tags, now);
            }
            Err(e) => tracing::warn!(error = %e, "system_tags_failed"),
        }
    }

    /// Recompute the devices marked since the last call, each with its channel.
    pub fn flush_events(&self) {
        let ids: Vec<String> = lock(&self.dirty).drain().collect();
        if ids.is_empty() {
            return;
        }
        let Some(source) = self.source.upgrade() else { return };
        let now = (self.clock)();
        let mut inner = lock(&self.inner);
        let Inner { state, devices, writer } = &mut *inner;
        state.now = opencap_core::value::iso_utc(now);
        let mut channels: Vec<usize> = ids.iter().filter_map(|id| devices.get(id).map(|(c, _)| *c)).collect();
        channels.sort_unstable();
        channels.dedup();
        let mut tags = Tags::new();
        let result = (|| -> Result<(), String> {
            for &ci in &channels {
                Self::refresh_channel_devices(&*source, state, ci);
                let ch = &state.channels[ci];
                channel_tags(ch, &mut tags);
                for id in &ids {
                    if let Some(&(c, di)) = devices.get(id).filter(|(c, _)| *c == ci) {
                        debug_assert_eq!(c, ci);
                        device_tags(ch, &ch.devices[di], now, &mut tags)?;
                    }
                }
            }
            Ok(())
        })();
        match result {
            Ok(()) => {
                writer.apply(&tags, now);
            }
            Err(e) => tracing::warn!(error = %e, "system_tags_failed"),
        }
    }

    /// Items of `GET /api/v1/system/tags`.
    pub fn items(&self, prefix: &str) -> Vec<serde_json::Value> {
        lock(&self.inner).writer.items(prefix)
    }

    /// A device's system and statistics tags keyed by device-relative path.
    pub fn device_values(&self, channel: &str, device: &str) -> serde_json::Map<String, serde_json::Value> {
        lock(&self.inner).writer.device_values(&format!("{channel}.{device}"))
    }

    /// Folder names (virtual device ids) currently in the store, sorted.
    #[cfg(test)]
    pub fn folders(&self) -> Vec<String> {
        lock(&self.inner).writer.folders()
    }

    /// The task: event updates (coalesced) and the tick at each UTC second.
    pub async fn run(self: Arc<Self>) {
        loop {
            let to_tick = self.until_next_second();
            tokio::select! {
                _ = self.wake.notified() => {
                    tokio::time::sleep(COALESCE).await;
                    self.flush_events();
                }
                _ = tokio::time::sleep(to_tick) => {
                    // A timer may fire a clock resolution early (Windows, ~15.6 ms): wait out the rest
                    // so the clock tags show the second they are aligned to (spec section 2.1).
                    for _ in 0..5 {
                        let left = self.until_next_second();
                        if left > Duration::from_millis(250) {
                            break; // the boundary just passed
                        }
                        tokio::time::sleep(left).await;
                    }
                    self.flush_events();
                    self.tick();
                }
            }
        }
    }

    fn until_next_second(&self) -> Duration {
        let ms = u64::from((self.clock)().timestamp_subsec_millis());
        Duration::from_millis(1000 - ms)
    }
}

fn placeholder_channel() -> ChannelState {
    serde_json::from_value(serde_json::json!({"name": ""})).expect("placeholder channel")
}

#[cfg(test)]
mod tests {
    use super::*;
    use opencap_core::system_tags::{Counters, DeviceStatusState};

    struct Fake {
        failures: StdMutex<i64>,
    }

    impl Source for Fake {
        fn static_state(&self) -> State {
            serde_json::from_value(serde_json::json!({
                "now": "2026-11-18T10:00:00.000Z",
                "node": {"product_version": "0.1.0", "engine": "rust", "instance": "t", "project_name": "p",
                         "project_revision": "r", "start_time": "2026-11-18T09:00:00.000Z", "timezone": "UTC"},
                "channels": [{"name": "Plant A", "devices": [
                    {"name": "PLC1", "driver": "modbus_tcp", "host": "192.0.2.10", "port": 502, "tags_enabled": 4},
                    {"name": "PLC2", "driver": "modbus_tcp", "host": "192.0.2.11", "port": 502}]}]
            }))
            .unwrap()
        }
        fn refresh_device(&self, _: &ChannelState, dev: &mut DeviceState) {
            let failures = *lock(&self.failures);
            dev.status = Some(DeviceStatusState {
                state: if failures > 0 { "offline" } else { "ok" }.into(),
                consecutive_failures: if dev.name == "PLC1" { failures } else { 0 },
                error_since: (failures > 0).then(|| "2026-11-18T10:00:00.000Z".into()),
                last_good: None,
                errors: 0,
                last_error: None,
                cycle_ms: None,
                connected: true,
                last_connect_failed: false,
            });
            dev.counters = Counters::default();
        }
        fn refresh_channel(&self, _: &mut ChannelState) {}
        fn refresh_node(&self, _: &mut NodeState, _: &mut Vec<NorthState>) {}
    }

    #[test]
    fn events_update_the_device_and_the_tick_the_node() {
        let store = Arc::new(TagStore::new());
        let fake: Arc<Fake> = Arc::new(Fake { failures: StdMutex::new(0) });
        let source: Arc<dyn Source> = fake.clone();
        let tags = SystemTags::new(store.clone(), Arc::downgrade(&source), None);
        tags.rebuild();
        let get = |folder: &str, name: &str| store.get(folder, name).map(|t| t.value);
        assert_eq!(get("Plant A.PLC1._System", "_Error"), Some(Some(opencap_core::value::Value::Bool(false))));
        assert_eq!(get("_System", "_DevicesOk"), Some(Some(opencap_core::value::Value::Int(2))));
        assert!(tags.folders().contains(&"Plant A._Statistics".to_string()));

        *lock(&fake.failures) = 1;
        tags.mark("Plant A/PLC1");
        tags.flush_events();
        assert_eq!(get("Plant A.PLC1._System", "_Error"), Some(Some(opencap_core::value::Value::Bool(true))));
        // the node count follows at the next tick, not at the event (spec section 2.1)
        assert_eq!(get("_System", "_DevicesOk"), Some(Some(opencap_core::value::Value::Int(2))));
        tags.tick();
        assert_eq!(get("_System", "_DevicesOk"), Some(Some(opencap_core::value::Value::Int(0))));
        assert_eq!(get("Plant A.PLC2._System", "_Error"), Some(Some(opencap_core::value::Value::Bool(false))));
    }
}
