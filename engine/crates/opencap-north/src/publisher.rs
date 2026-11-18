//! Publisher: when to send what (Python `opencap.north.publisher`).
//!
//! Data messages (topic `device.publish.topic`, not retained):
//! - `interval`: the full snapshot every `interval_ms`; the first one interval after start;
//!   missed slots are skipped, never sent in a burst.
//! - `on_change`: a message when a tag goes between good and not good, or a good value
//!   moves by more than `deadband` (absolute, on the rounded value, compared with the last
//!   PUBLISHED value); bools and strings on any difference; plus a heartbeat every
//!   `heartbeat_ms`. Checked on `notify_cycle()` and every `on_change_poll_s`.
//! - A device that never had a good read since start publishes no data, only status.
//!
//! Status messages (`status_topic(device, topic_prefix)`, retained): `{"state","last_good","errors"}`
//! at start, on every state change (checked every `status_check_s`) and every
//! `status_refresh_s`.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Notify;
use tokio::task::JoinSet;
use tokio::time::Instant;
use tracing::{debug, info, warn};

use opencap_core::cache::{Snapshot, ValueCache};
use opencap_core::config::{default_topic, Device, PayloadCfg, PublishMode};
use opencap_core::health::HealthStatus;
use opencap_core::value::{Quality, TagValue, Value};

use crate::payload::{build_payload, clean_value, encode, encode_status, status_topic};

/// Floor for interval_ms / heartbeat_ms (protects against 0 in config).
pub const MIN_PERIOD_MS: i64 = 50;

/// Where messages go (the MQTT link; a fake in tests).
pub trait Sink: Send + Sync {
    fn is_secure(&self) -> bool;
    fn publish(&self, topic: &str, payload: &[u8], retain: bool) -> Result<(), String>;
}

impl Sink for crate::mqtt::MqttLink {
    fn is_secure(&self) -> bool {
        crate::mqtt::MqttLink::is_secure(self)
    }
    fn publish(&self, topic: &str, payload: &[u8], retain: bool) -> Result<(), String> {
        crate::mqtt::MqttLink::publish(self, topic, payload, retain)
    }
}

pub type StatusFn = Arc<dyn Fn(&str) -> Option<HealthStatus> + Send + Sync>;

/// True once any tag of the device has had a good read (the cache keeps the last good ts).
pub fn ever_good(snapshot: &Snapshot) -> bool {
    snapshot.values().any(|tv| tv.source_ts.is_some())
}

fn good_value(tv: Option<&TagValue>, decimals: i64) -> Option<Value> {
    let tv = tv?;
    if tv.quality != Quality::Good {
        return None;
    }
    clean_value(tv.value.as_ref(), decimals)
}

fn finite_number(v: &Value) -> Option<f64> {
    v.as_f64().filter(|f| f.is_finite())
}

/// on_change rule: good <-> not good, or a value move beyond the device's deadband.
pub fn changed(device: &Device, old: &Snapshot, new: &Snapshot, decimals: i64) -> bool {
    let deadband = device.publish.deadband;
    for tag in device.tags.iter().filter(|t| t.enabled) {
        let a = good_value(old.get(&tag.name), decimals);
        let b = good_value(new.get(&tag.name), decimals);
        match (a, b) {
            (None, None) => continue,
            (None, Some(_)) | (Some(_), None) => return true,
            (Some(a), Some(b)) => match (finite_number(&a), finite_number(&b)) {
                (Some(x), Some(y)) => {
                    if (y - x).abs() > deadband {
                        return true;
                    }
                }
                _ => {
                    if !py_eq(&a, &b) {
                        return true;
                    }
                }
            },
        }
    }
    false
}

/// Python `==` between published values (1 == 1.0, True == 1).
fn py_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::List(x), Value::List(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q)),
        (Value::Str(x), Value::Str(y)) => x == y,
        (Value::Bool(x), Value::Bool(y)) => x == y,
        _ => match (num(a), num(b)) {
            (Some(x), Some(y)) => x == y,
            _ => false,
        },
    }
}

fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Bool(b) => Some(*b as i64 as f64),
        other => other.as_f64(),
    }
}

#[derive(Debug, Clone)]
pub struct PublisherOptions {
    pub status_check_s: f64,
    pub status_refresh_s: f64,
    pub on_change_poll_s: f64,
}

impl Default for PublisherOptions {
    fn default() -> Self {
        Self { status_check_s: 1.0, status_refresh_s: 60.0, on_change_poll_s: 1.0 }
    }
}

pub struct Publisher {
    devices: Vec<Device>,
    cache: Arc<ValueCache>,
    sink: Arc<dyn Sink>,
    cfg: PayloadCfg,
    status_fn: StatusFn,
    opts: PublisherOptions,
    data_topic: HashMap<String, String>,
    status_topic: HashMap<String, String>,
    wake: HashMap<String, Notify>,
    data_published: HashMap<String, AtomicU64>,
}

impl Publisher {
    pub fn new(
        devices: Vec<Device>,
        cache: Arc<ValueCache>,
        sink: Arc<dyn Sink>,
        cfg: PayloadCfg,
        status_fn: StatusFn,
        opts: PublisherOptions,
        topic_prefix: &str,
    ) -> Arc<Self> {
        let devices: Vec<Device> = devices.into_iter().filter(|d| d.enabled).collect();
        let data_topic = devices
            .iter()
            .map(|d| (d.id.clone(), d.publish.topic.clone().unwrap_or_else(|| default_topic(d, topic_prefix))))
            .collect();
        let status_topic = devices.iter().map(|d| (d.id.clone(), status_topic(d, topic_prefix))).collect();
        let wake = devices.iter().map(|d| (d.id.clone(), Notify::new())).collect();
        let data_published = devices.iter().map(|d| (d.id.clone(), AtomicU64::new(0))).collect();
        Arc::new(Self { devices, cache, sink, cfg, status_fn, opts, data_topic, status_topic, wake, data_published })
    }

    /// ChannelWorker on_cycle hook.
    pub fn notify_cycle(&self, device_id: &str) {
        if let Some(n) = self.wake.get(device_id) {
            n.notify_one();
        }
    }

    pub fn data_published(&self, device_id: &str) -> u64 {
        self.data_published.get(device_id).map(|c| c.load(Ordering::Relaxed)).unwrap_or(0)
    }

    /// Run every device loop and the status loop until the task is aborted.
    pub async fn run(self: Arc<Self>) {
        let mut set = JoinSet::new();
        for i in 0..self.devices.len() {
            let me = self.clone();
            if self.devices[i].publish.mode == PublishMode::OnChange {
                set.spawn(async move { me.on_change_loop(i).await });
            } else {
                set.spawn(async move { me.interval_loop(i).await });
            }
        }
        let me = self.clone();
        set.spawn(async move { me.status_loop().await });
        while set.join_next().await.is_some() {}
    }

    fn publish_data(&self, dev: &Device, snapshot: &Snapshot) {
        let payload = match build_payload(dev, snapshot, &self.cfg, self.sink.is_secure(), None) {
            Ok(p) => p,
            Err(e) => {
                warn!(device = %dev.id, error = %e, "publish_failed");
                return;
            }
        };
        match self.sink.publish(&self.data_topic[&dev.id], &encode(&payload), false) {
            Ok(()) => {
                self.data_published[&dev.id].fetch_add(1, Ordering::Relaxed);
            }
            Err(e) => warn!(device = %dev.id, error = %e, "publish_failed"),
        }
    }

    async fn interval_loop(&self, i: usize) {
        let dev = &self.devices[i];
        let period = Duration::from_millis(dev.publish.interval_ms.max(MIN_PERIOD_MS) as u64);
        let mut next_t = Instant::now() + period;
        loop {
            tokio::time::sleep_until(next_t).await;
            let snap = self.cache.snapshot(&dev.id);
            if ever_good(&snap) {
                self.publish_data(dev, &snap);
            }
            next_t += period;
            let now = Instant::now();
            if next_t <= now {
                next_t = now + period; // fell behind: skip, do not burst
            }
        }
    }

    async fn on_change_loop(&self, i: usize) {
        let dev = &self.devices[i];
        let heartbeat = Duration::from_millis(dev.publish.heartbeat_ms.max(MIN_PERIOD_MS) as u64);
        let poll = Duration::from_secs_f64(self.opts.on_change_poll_s);
        let decimals = self.cfg.float_decimals;
        let wake = &self.wake[&dev.id];
        let mut last_version: Option<u64> = None;
        let mut last_sent: Option<Snapshot> = None;
        let mut last_sent_t = Instant::now();
        loop {
            let mut timeout = poll;
            if last_sent.is_some() {
                timeout = timeout.min((last_sent_t + heartbeat).saturating_duration_since(Instant::now()));
            }
            let _ = tokio::time::timeout(timeout, wake.notified()).await;
            let now = Instant::now();
            let due = last_sent.is_some() && now.duration_since(last_sent_t) >= heartbeat;
            let version = self.cache.version(&dev.id);
            if Some(version) == last_version && !due {
                continue;
            }
            last_version = Some(version);
            let snap = self.cache.snapshot(&dev.id);
            if !ever_good(&snap) {
                continue;
            }
            let send = match &last_sent {
                None => true,
                Some(old) => due || changed(dev, old, &snap, decimals),
            };
            if send {
                self.publish_data(dev, &snap);
                last_sent = Some(snap);
                last_sent_t = now;
            }
        }
    }

    async fn status_loop(&self) {
        let mut last_state: HashMap<String, &'static str> = HashMap::new();
        let mut last_sent: HashMap<String, Instant> = HashMap::new();
        let refresh = Duration::from_secs_f64(self.opts.status_refresh_s);
        loop {
            let now = Instant::now();
            for dev in &self.devices {
                let Some(status) = (self.status_fn)(&dev.id) else {
                    debug!(device = %dev.id, "status_unavailable");
                    continue;
                };
                let state = status.state.as_str();
                let state_changed = last_state.get(&dev.id) != Some(&state);
                let stale = last_sent.get(&dev.id).is_none_or(|t| now.duration_since(*t) >= refresh);
                if !(state_changed || stale) {
                    continue;
                }
                if let Err(e) = self.sink.publish(&self.status_topic[&dev.id], &encode_status(&status), true) {
                    warn!(device = %dev.id, error = %e, "status_publish_failed");
                    continue;
                }
                if state_changed {
                    if let Some(old) = last_state.get(&dev.id) {
                        info!(device = %dev.id, old = %old, new = %state, "device_status_changed");
                    }
                }
                last_state.insert(dev.id.clone(), state);
                last_sent.insert(dev.id.clone(), now);
            }
            tokio::time::sleep(Duration::from_secs_f64(self.opts.status_check_s)).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{TimeZone, Utc};
    use opencap_core::config::{DType, Tag};
    use opencap_core::health::State;
    use std::sync::Mutex;

    #[derive(Default)]
    struct FakeSink {
        sent: Mutex<Vec<(String, Vec<u8>, bool)>>,
    }

    impl Sink for FakeSink {
        fn is_secure(&self) -> bool {
            false
        }
        fn publish(&self, topic: &str, payload: &[u8], retain: bool) -> Result<(), String> {
            self.sent.lock().unwrap().push((topic.into(), payload.to_vec(), retain));
            Ok(())
        }
    }

    fn dev(mode: PublishMode, deadband: f64) -> Device {
        let mut d = Device::new(
            "S/D",
            "S",
            "D",
            "modbus_tcp",
            "h",
            1,
            vec![Tag::new("A", "400001", DType::Float32), Tag::new("B", "400003", DType::Bool)],
        );
        d.publish.mode = mode;
        d.publish.deadband = deadband;
        d.publish.interval_ms = 100;
        d.publish.heartbeat_ms = 10_000;
        d
    }

    fn snap(a: Option<f64>, b: bool) -> Snapshot {
        let ts = Utc.with_ymd_and_hms(2026, 11, 16, 10, 0, 0).unwrap();
        let mut s = Snapshot::new();
        s.insert(
            "A".into(),
            match a {
                Some(v) => TagValue::good(Value::Float(v), ts),
                None => TagValue::bad(Quality::BadComm, Some(ts)),
            },
        );
        s.insert("B".into(), TagValue::good(Value::Bool(b), ts));
        s
    }

    #[test]
    fn change_rule() {
        let d = dev(PublishMode::OnChange, 0.5);
        assert!(!changed(&d, &snap(Some(1.0), true), &snap(Some(1.4), true), 4));
        assert!(changed(&d, &snap(Some(1.0), true), &snap(Some(1.6), true), 4));
        assert!(changed(&d, &snap(Some(1.0), true), &snap(None, true), 4));
        assert!(changed(&d, &snap(Some(1.0), true), &snap(Some(1.0), false), 4));
        assert!(!changed(&d, &snap(None, true), &snap(None, true), 4));
    }

    #[tokio::test(start_paused = true)]
    async fn interval_and_status_messages() {
        let cache = Arc::new(ValueCache::new());
        let d = dev(PublishMode::Interval, 0.0);
        cache.register(&d);
        let sink = Arc::new(FakeSink::default());
        let status: StatusFn = Arc::new(|_| Some(HealthStatus { state: State::Ok, last_good: None, errors: 0 }));
        let p = Publisher::new(
            vec![d.clone()],
            cache.clone(),
            sink.clone(),
            PayloadCfg::default(),
            status,
            PublisherOptions::default(),
            "opencap",
        );
        let task = tokio::spawn(p.clone().run());
        tokio::time::sleep(Duration::from_millis(350)).await;
        // never read good: no data, but a retained status message
        {
            let sent = sink.sent.lock().unwrap();
            assert_eq!(sent.len(), 1);
            assert_eq!(sent[0].0, "opencap/s/d/status");
            assert!(sent[0].2);
        }
        cache.update("S/D", &snap(Some(2.5), true));
        tokio::time::sleep(Duration::from_millis(300)).await;
        let data: Vec<String> = sink
            .sent
            .lock()
            .unwrap()
            .iter()
            .filter(|m| m.0.ends_with("/data"))
            .map(|m| String::from_utf8(m.1.clone()).unwrap())
            .collect();
        assert_eq!(data.len(), 3);
        assert!(data[0].contains("\"Data\":{\"A\":2.5,\"B\":true}"), "{}", data[0]);
        assert_eq!(p.data_published("S/D"), 3);
        task.abort();
    }
}
