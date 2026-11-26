//! The Sparkplug B 3.0 edge node (#51, docs/spec/payload-sparkplug.md §8): one north plugin,
//! one MQTT session at a time, built on the codec in this crate.
//!
//! - **Session.** CONNECT with NDEATH as the Will (QoS 1, bdSeq of this CONNECT), subscribe to
//!   NCMD, DCMD and, with a primary host, its STATE topic.
//! - **Births.** NBIRTH and every DBIRTH once connected, and with a primary host only while its
//!   STATE says online. NCMD `Node Control/Rebirth` repeats them. Every other command is rejected
//!   and counted (ADR-0008).
//! - **Data.** Report by exception from the tag store's change batches: a metric is sent when its
//!   quality changes or its value moves beyond the device's `publish.deadband`. DDATA metrics
//!   carry the alias only. A device that goes offline or demoted gets DDEATH, and DBIRTH when it
//!   is back.
//! - **Primary host offline or broker lost.** The changes are kept in the agent's outbox as
//!   historical DDATA (ADR-0011). When the host STATE says offline the session ends (NDEATH,
//!   disconnect) and the node reconnects with the next bdSeq. After the next births the outbox is
//!   drained, in order, with `is_historical` set and new `seq` numbers.

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;
use std::time::Duration;

use async_trait::async_trait;
use chrono::Utc;
use opencap_core::config::{Device, MqttCfg, PollerConfig};
use opencap_core::health::State;
use opencap_core::metrics::METRICS;
use opencap_core::store::{Change, Snapshot};
use opencap_core::value::{Quality, TagValue, Value};
use opencap_north::mqtt::client_options;
use opencap_north::plugin::{NorthContext, NorthError, NorthPlugin, Outbox};
use prometheus::{IntCounterVec, Opts};
use rumqttc::{AsyncClient, Event, EventLoop, LastWill, Packet, QoS};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::{info, warn};

use crate::command::classify;
use crate::datatype::datatype_for;
use crate::message::{
    birth_metric, data_metric, dbirth, ddata, ddeath, decode_payload, encode_payload, metric_name, nbirth, ndeath,
    AliasAllocator, BdSeq, NodeAliases, Sample, Seq, TagMeta,
};
use crate::proto::payload::Metric;
use crate::proto::{DataType, Payload};
use crate::state::HostState;
use crate::topic::{MessageType, Topic};

/// `NorthPlugin::kind` and the agent name.
pub const KIND: &str = "sparkplug";
/// The node metric with the software version (alias right after the two required ones).
pub const VERSION_METRIC: &str = "Node Info/Version";

struct Metrics {
    /// agent, type (NBIRTH, DDATA, ..., `historical` for drained DDATA)
    messages: IntCounterVec,
    /// agent, reason (`read_only`, `not_true`)
    rejected: IntCounterVec,
}

static M: LazyLock<Metrics> = LazyLock::new(|| {
    let counter = |name: &str, help: &str, labels: &[&str]| {
        let c = IntCounterVec::new(Opts::new(name, help), labels).expect("metric");
        METRICS.registry.register(Box::new(c.clone())).expect("register");
        c
    };
    Metrics {
        messages: counter("sparkplug_messages_total", "Sparkplug messages published by type", &["agent", "type"]),
        rejected: counter(
            "sparkplug_commands_rejected_total",
            "NCMD/DCMD metrics not executed (read-only, ADR-0008)",
            &["agent", "reason"],
        ),
    }
});

/// One tag as a metric of its device.
#[derive(Debug, Clone)]
pub struct NodeMetric {
    pub tag: String,
    pub meta: TagMeta,
}

/// One opencap device as a Sparkplug device.
#[derive(Debug, Clone)]
pub struct NodeDevice {
    pub id: String,
    pub sparkplug_id: String,
    pub deadband: f64,
    pub metrics: Vec<NodeMetric>,
}

/// Everything the edge node needs, resolved from the config.
#[derive(Debug, Clone)]
pub struct NodeConfig {
    pub group: String,
    pub edge: String,
    pub primary_host: Option<String>,
    pub client_id: String,
    pub mqtt: MqttCfg,
    pub version: String,
    pub aliases: NodeAliases,
    pub version_alias: u64,
    pub devices: Vec<NodeDevice>,
    pub reconnect_min: Duration,
    pub reconnect_max: Duration,
    /// Retry delay after the session ended because the primary host is offline.
    pub host_retry: Duration,
}

impl NodeConfig {
    /// From a validated config and the enabled devices, in config order (aliases follow it).
    pub fn build(poller: &PollerConfig, devices: &[Device], version: &str) -> Result<Self, String> {
        let sp = &poller.sparkplug;
        let mut alloc = AliasAllocator::default();
        let aliases = alloc.node();
        let version_alias = alloc.alloc();
        let mut out = Vec::new();
        for d in devices.iter().filter(|d| d.enabled) {
            let mut metrics = Vec::new();
            for t in d.tags.iter().filter(|t| t.enabled) {
                let datatype = datatype_for(t.dtype.as_str(), t.scale.is_some(), false)
                    .map_err(|e| format!("{} {}: {e}", d.id, t.name))?;
                let meta = TagMeta {
                    name: metric_name(&t.name),
                    alias: alloc.alloc(),
                    datatype,
                    units: t.scale.as_ref().and_then(|s| s.units.clone()),
                    eng_range: t.scale.as_ref().map(|s| (s.eng_lo, s.eng_hi)),
                };
                metrics.push(NodeMetric { tag: t.name.clone(), meta });
            }
            out.push(NodeDevice {
                id: d.id.clone(),
                sparkplug_id: sp.device_id(d),
                deadband: d.publish.deadband,
                metrics,
            });
        }
        let client_id = sp.client_id.clone().unwrap_or_else(|| {
            format!("{}-sparkplug", poller.mqtt.client_id.clone().unwrap_or_else(|| poller.instance.clone()))
        });
        Ok(NodeConfig {
            group: sp.group_id.clone(),
            edge: sp.edge_node_id.clone().unwrap_or_else(|| poller.instance.clone()),
            primary_host: sp.primary_host_id.clone(),
            client_id,
            mqtt: poller.mqtt.clone(),
            version: version.to_string(),
            aliases,
            version_alias,
            devices: out,
            reconnect_min: Duration::from_secs(1),
            reconnect_max: Duration::from_secs(30),
            host_retry: Duration::from_secs(2),
        })
    }

    fn node_topic(&self, kind: MessageType) -> String {
        Topic::node(&self.group, kind, &self.edge).expect("validated ids").to_string()
    }

    fn device_topic(&self, kind: MessageType, dev: &NodeDevice) -> String {
        Topic::device(&self.group, kind, &self.edge, &dev.sparkplug_id).expect("validated ids").to_string()
    }
}

/// The plugin.
pub struct SparkplugNode {
    cfg: NodeConfig,
}

impl SparkplugNode {
    pub fn new(cfg: NodeConfig) -> Self {
        SparkplugNode { cfg }
    }
}

fn now_ms() -> u64 {
    Utc::now().timestamp_millis().max(0) as u64
}

fn ts_ms(tv: &TagValue) -> u64 {
    tv.source_ts.or(tv.server_ts).map(|t| t.timestamp_millis().max(0) as u64).unwrap_or_else(now_ms)
}

/// The value last reported for a metric (the report-by-exception baseline).
#[derive(Debug, Clone, PartialEq)]
struct Reported {
    value: Option<Value>,
    quality: Quality,
}

fn moved(old: &Reported, new: &TagValue, deadband: f64) -> bool {
    if old.quality != new.quality {
        return true;
    }
    match (&old.value, &new.value) {
        (None, None) => false,
        (Some(a), Some(b)) => match (a.as_f64().filter(|x| x.is_finite()), b.as_f64().filter(|x| x.is_finite())) {
            (Some(x), Some(y)) if !matches!(a, Value::Bool(_)) => (y - x).abs() > deadband,
            _ => a != b,
        },
        _ => true,
    }
}

/// A metric for `tv`; a value the datatype cannot carry is sent as null, bad_decode.
fn metric(meta: &TagMeta, tv: Option<&TagValue>, birth: bool, with_quality: bool) -> Metric {
    let never = TagValue::new(None, Quality::Stale, None);
    let tv = tv.unwrap_or(&never);
    let build = |value: Option<&Value>, quality: Quality| {
        let s = Sample { value, quality, ts_ms: ts_ms(tv) };
        if birth {
            birth_metric(meta, &s)
        } else {
            data_metric(meta.alias, meta.datatype, &s, with_quality)
        }
    };
    build(tv.value.as_ref(), tv.quality).unwrap_or_else(|_| build(None, Quality::BadDecode).expect("null encodes"))
}

enum Incoming {
    Publish(String, Vec<u8>),
    Lost(String),
}

/// One MQTT session.
struct Session {
    client: AsyncClient,
    incoming: mpsc::Receiver<Incoming>,
    pump: JoinHandle<()>,
    bd_seq: u8,
    seq: Seq,
    born: bool,
    host: Option<HostState>,
    alive: HashSet<usize>,
}

async fn pump(mut el: EventLoop, tx: mpsc::Sender<Incoming>) {
    loop {
        match el.poll().await {
            Ok(Event::Incoming(Packet::Publish(p))) => {
                if tx.send(Incoming::Publish(p.topic.clone(), p.payload.to_vec())).await.is_err() {
                    return;
                }
            }
            Ok(_) => {}
            Err(e) => {
                let _ = tx.send(Incoming::Lost(e.to_string())).await;
                return;
            }
        }
    }
}

/// Why a session ended.
enum End {
    Lost(String),
    HostOffline,
}

struct Runner {
    cfg: NodeConfig,
    agent: String,
    store: opencap_core::store::TagStoreReader,
    status: opencap_north::plugin::DeviceStatusReader,
    outbox: Outbox,
    by_device: HashMap<String, usize>,
    by_tag: Vec<HashMap<String, usize>>,
    /// per device, per metric: what the host last got (None: nothing yet this baseline)
    reported: Vec<Vec<Option<Reported>>>,
    bd: BdSeq,
}

impl Runner {
    fn count(&self, kind: &str) {
        M.messages.with_label_values(&[&self.agent, kind]).inc();
    }

    async fn connect(&mut self) -> Result<Session, String> {
        let bd_seq = self.bd.connect();
        let mut mo = client_options(&self.cfg.mqtt, &self.cfg.client_id)?;
        let will = encode_payload(&ndeath(now_ms(), bd_seq));
        mo.set_last_will(LastWill::new(self.cfg.node_topic(MessageType::NDeath), will, QoS::AtLeastOnce, false));
        let (client, mut el) = AsyncClient::new(mo, 256);
        let connack = tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                match el.poll().await {
                    Ok(Event::Incoming(Packet::ConnAck(_))) => return Ok(()),
                    Ok(_) => continue,
                    Err(e) => return Err(e.to_string()),
                }
            }
        })
        .await;
        match connack {
            Ok(Ok(())) => {}
            Ok(Err(e)) => return Err(e),
            Err(_) => return Err("no CONNACK within 10 s".into()),
        }
        let (tx, rx) = mpsc::channel(256);
        let pump = tokio::spawn(pump(el, tx));
        let mut subs = vec![
            self.cfg.node_topic(MessageType::NCmd),
            format!("{}/{}/DCMD/{}/+", crate::topic::NAMESPACE, self.cfg.group, self.cfg.edge),
        ];
        if let Some(host) = &self.cfg.primary_host {
            subs.push(Topic::state(host).expect("validated").to_string());
        }
        for t in subs {
            client.subscribe(t, QoS::AtLeastOnce).await.map_err(|e| e.to_string())?;
        }
        info!(agent = %self.agent, bd_seq, client_id = %self.cfg.client_id, "sparkplug.session_started");
        Ok(Session {
            client,
            incoming: rx,
            pump,
            bd_seq,
            seq: Seq::default(),
            born: false,
            host: None,
            alive: HashSet::new(),
        })
    }

    async fn publish(&self, s: &Session, topic: String, payload: &Payload, kind: &str) -> Result<(), String> {
        let qos = if kind == "NDEATH" { QoS::AtLeastOnce } else { QoS::AtMostOnce };
        s.client.publish(topic, qos, false, encode_payload(payload)).await.map_err(|e| e.to_string())?;
        self.count(kind);
        Ok(())
    }

    fn snapshot(&self, i: usize) -> Snapshot {
        self.store.snapshot(&self.cfg.devices[i].id)
    }

    async fn dbirth(&mut self, s: &mut Session, i: usize) -> Result<(), String> {
        let snap = self.snapshot(i);
        let dev = &self.cfg.devices[i];
        let mut metrics = Vec::with_capacity(dev.metrics.len());
        for (k, m) in dev.metrics.iter().enumerate() {
            let tv = snap.get(&m.tag);
            metrics.push(metric(&m.meta, tv, true, true));
            self.reported[i][k] = tv.map(|tv| Reported { value: tv.value.clone(), quality: tv.quality });
        }
        let seq = s.seq.next_seq();
        self.publish(s, self.cfg.device_topic(MessageType::DBirth, dev), &dbirth(now_ms(), seq, metrics), "DBIRTH")
            .await?;
        s.alive.insert(i);
        Ok(())
    }

    /// NBIRTH, every DBIRTH, then the held DATA (historical, new seq).
    async fn births(&mut self, s: &mut Session) -> Result<(), String> {
        s.seq.birth();
        let ts = now_ms();
        let version = Metric {
            name: Some(VERSION_METRIC.into()),
            alias: Some(self.cfg.version_alias),
            timestamp: Some(ts),
            datatype: Some(DataType::String as u32),
            value: Some(crate::proto::payload::metric::Value::StringValue(self.cfg.version.clone())),
            ..Default::default()
        };
        let birth = nbirth(ts, s.bd_seq, self.cfg.aliases, vec![version]);
        self.publish(s, self.cfg.node_topic(MessageType::NBirth), &birth, "NBIRTH").await?;
        s.alive.clear();
        for i in 0..self.cfg.devices.len() {
            self.dbirth(s, i).await?;
        }
        s.born = true;
        info!(agent = %self.agent, bd_seq = s.bd_seq, devices = self.cfg.devices.len(), "sparkplug.born");
        self.drain(s).await
    }

    async fn drain(&mut self, s: &mut Session) -> Result<(), String> {
        loop {
            let batch = {
                let ob = self.outbox.lock().unwrap_or_else(|e| e.into_inner());
                ob.peek(100).map_err(|e| e.to_string())?
            };
            if batch.is_empty() {
                return Ok(());
            }
            let mut ids = Vec::with_capacity(batch.len());
            for msg in batch {
                if let Ok(mut p) = decode_payload(&msg.payload) {
                    p.seq = Some(u64::from(s.seq.next_seq()));
                    self.publish(s, msg.topic.clone(), &p, "historical").await?;
                }
                ids.push(msg.id);
            }
            self.outbox.lock().unwrap_or_else(|e| e.into_inner()).ack(&ids).map_err(|e| e.to_string())?;
        }
    }

    /// The changed metrics of device `i` among `values`; updates the baseline.
    fn changes<'a>(
        &mut self,
        i: usize,
        values: impl Iterator<Item = (&'a str, &'a TagValue)>,
        historical: bool,
    ) -> Vec<Metric> {
        let dev = &self.cfg.devices[i];
        let mut out = Vec::new();
        for (tag, tv) in values {
            let Some(&k) = self.by_tag[i].get(tag) else { continue };
            let quality_changed = self.reported[i][k].as_ref().is_none_or(|r| r.quality != tv.quality);
            if self.reported[i][k].as_ref().is_some_and(|r| !moved(r, tv, dev.deadband)) {
                continue;
            }
            let mut m = metric(&dev.metrics[k].meta, Some(tv), false, quality_changed);
            if historical {
                m.is_historical = Some(true);
            }
            out.push(m);
            self.reported[i][k] = Some(Reported { value: tv.value.clone(), quality: tv.quality });
        }
        out
    }

    async fn on_values(
        &mut self,
        s: Option<&mut Session>,
        i: usize,
        snap_or_batch: Vec<(String, TagValue)>,
    ) -> Result<(), String> {
        let online = s.as_ref().is_some_and(|s| s.born && s.alive.contains(&i));
        let offline_buffer = s.as_ref().is_none_or(|s| !s.born);
        if !online && !offline_buffer {
            // device is dead in a live session: its DBIRTH will carry the current values
            for (tag, tv) in &snap_or_batch {
                if let Some(&k) = self.by_tag[i].get(tag.as_str()) {
                    self.reported[i][k] = Some(Reported { value: tv.value.clone(), quality: tv.quality });
                }
            }
            return Ok(());
        }
        let metrics = self.changes(i, snap_or_batch.iter().map(|(t, v)| (t.as_str(), v)), !online);
        if metrics.is_empty() {
            return Ok(());
        }
        let topic = self.cfg.device_topic(MessageType::DData, &self.cfg.devices[i]);
        match s {
            Some(s) if online => {
                let seq = s.seq.next_seq();
                self.publish(s, topic, &ddata(now_ms(), seq, metrics), "DDATA").await
            }
            _ => {
                let p = Payload { timestamp: Some(now_ms()), metrics, seq: None, uuid: None, body: None };
                let mut ob = self.outbox.lock().unwrap_or_else(|e| e.into_inner());
                ob.put(&topic, &encode_payload(&p), 0, false).map(drop).map_err(|e| e.to_string())
            }
        }
    }

    async fn on_change(&mut self, s: Option<&mut Session>, change: Change) -> Result<(), String> {
        match change {
            Change::Batch(b) => {
                let Some(&i) = self.by_device.get(b.device_name()) else { return Ok(()) };
                let values: Vec<(String, TagValue)> = b.iter().map(|(t, v)| (t.to_string(), v.clone())).collect();
                self.on_values(s, i, values).await
            }
            Change::Lagged(_) => {
                let mut s = s;
                for i in 0..self.cfg.devices.len() {
                    let snap = self.snapshot(i);
                    let values: Vec<(String, TagValue)> =
                        snap.iter().map(|(t, v)| (t.to_string(), v.clone())).collect();
                    self.on_values(s.as_deref_mut(), i, values).await?;
                }
                Ok(())
            }
        }
    }

    /// Returns Some(end) when the session must end.
    async fn on_mqtt(&mut self, s: &mut Session, msg: Incoming) -> Result<Option<End>, String> {
        let (topic, payload) = match msg {
            Incoming::Lost(e) => return Ok(Some(End::Lost(e))),
            Incoming::Publish(t, p) => (t, p),
        };
        let Ok(parsed) = Topic::parse(&topic) else { return Ok(None) };
        if let Topic::State { host_id } = &parsed {
            if Some(host_id) != self.cfg.primary_host.as_ref() {
                return Ok(None);
            }
            let Ok(state) = HostState::parse(&payload) else {
                warn!(agent = %self.agent, topic = %topic, "sparkplug.state_invalid");
                return Ok(None);
            };
            if !state.supersedes(s.host.as_ref()) {
                return Ok(None);
            }
            s.host = Some(state);
            if !state.online {
                info!(agent = %self.agent, host = %host_id, "sparkplug.host_offline");
                return Ok(Some(End::HostOffline));
            }
            if !s.born {
                self.births(s).await?;
            }
            return Ok(None);
        }
        match classify(&parsed, &payload, Some(self.cfg.aliases.rebirth)) {
            Ok(d) => {
                for r in &d.rejected {
                    let reason = if r.reason == crate::command::READ_ONLY { "read_only" } else { "not_true" };
                    M.rejected.with_label_values(&[&self.agent, reason]).inc();
                    warn!(agent = %self.agent, topic = %topic, metric = %r.metric, reason = r.reason, "sparkplug.command_rejected");
                }
                if d.rebirth && s.born {
                    info!(agent = %self.agent, "sparkplug.rebirth");
                    self.births(s).await?;
                }
            }
            Err(e) => warn!(agent = %self.agent, topic = %topic, error = %e, "sparkplug.command_invalid"),
        }
        Ok(None)
    }

    /// DDEATH for devices that went offline/demoted, DBIRTH for those that came back.
    async fn on_tick(&mut self, s: &mut Session) -> Result<(), String> {
        if !s.born {
            return Ok(());
        }
        for i in 0..self.cfg.devices.len() {
            let state = self.status.get(&self.cfg.devices[i].id).map(|h| h.state);
            let down = matches!(state, Some(State::Offline | State::Demoted));
            if down && s.alive.remove(&i) {
                let seq = s.seq.next_seq();
                let topic = self.cfg.device_topic(MessageType::DDeath, &self.cfg.devices[i]);
                self.publish(s, topic, &ddeath(now_ms(), seq), "DDEATH").await?;
            } else if !down && !s.alive.contains(&i) {
                self.dbirth(s, i).await?;
            }
        }
        Ok(())
    }

    async fn close(&mut self, s: Session, graceful: bool) {
        if graceful {
            let _ =
                self.publish(&s, self.cfg.node_topic(MessageType::NDeath), &ndeath(now_ms(), s.bd_seq), "NDEATH").await;
            let _ = s.client.disconnect().await;
            let _ = tokio::time::timeout(Duration::from_secs(2), s.pump).await;
        } else {
            s.pump.abort();
        }
    }
}

async fn next_incoming(s: &mut Option<Session>) -> Option<Incoming> {
    match s {
        Some(s) => s.incoming.recv().await,
        None => std::future::pending().await,
    }
}

#[async_trait]
impl NorthPlugin for SparkplugNode {
    fn kind(&self) -> &'static str {
        KIND
    }

    async fn run(self: Box<Self>, ctx: NorthContext) -> Result<(), NorthError> {
        let NorthContext { agent, store, mut events, outbox, status, shutdown, .. } = ctx;
        let outbox = outbox.ok_or_else(|| NorthError::Config("the Sparkplug edge node needs its outbox".into()))?;
        let cfg = self.cfg;
        let by_device = cfg.devices.iter().enumerate().map(|(i, d)| (d.id.clone(), i)).collect();
        let by_tag = cfg
            .devices
            .iter()
            .map(|d| d.metrics.iter().enumerate().map(|(k, m)| (m.tag.clone(), k)).collect())
            .collect();
        let reported = cfg.devices.iter().map(|d| vec![None; d.metrics.len()]).collect();
        let mut r = Runner { cfg, agent, store, status, outbox, by_device, by_tag, reported, bd: BdSeq::default() };
        let mut session: Option<Session> = None;
        let mut retry_at = tokio::time::Instant::now();
        let mut backoff = r.cfg.reconnect_min;
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        loop {
            if session.is_none() && tokio::time::Instant::now() >= retry_at {
                match r.connect().await {
                    Ok(mut s) => {
                        backoff = r.cfg.reconnect_min;
                        if r.cfg.primary_host.is_none() {
                            if let Err(e) = r.births(&mut s).await {
                                warn!(agent = %r.agent, error = %e, "sparkplug.birth_failed");
                            }
                        }
                        session = Some(s);
                    }
                    Err(e) => {
                        warn!(agent = %r.agent, error = %e, retry_s = backoff.as_secs_f64(), "sparkplug.connect_failed");
                        retry_at = tokio::time::Instant::now() + backoff;
                        backoff = (backoff * 2).min(r.cfg.reconnect_max);
                    }
                }
            }
            let mut end: Option<End> = None;
            tokio::select! {
                _ = shutdown.cancelled() => {
                    if let Some(s) = session.take() {
                        r.close(s, true).await;
                    }
                    return Ok(());
                }
                change = events.recv() => match change {
                    Some(c) => if let Err(e) = r.on_change(session.as_mut(), c).await {
                        end = Some(End::Lost(e));
                    },
                    None => return Ok(()),
                },
                msg = next_incoming(&mut session) => match msg {
                    Some(m) => match r.on_mqtt(session.as_mut().expect("a session"), m).await {
                        Ok(e) => end = e,
                        Err(e) => end = Some(End::Lost(e)),
                    },
                    None => end = Some(End::Lost("event loop stopped".into())),
                },
                _ = tick.tick() => if let Some(s) = session.as_mut() {
                    if let Err(e) = r.on_tick(s).await {
                        end = Some(End::Lost(e));
                    }
                },
            }
            // only take the session when it ended (Option::zip would take it on every turn)
            if let Some(why) = end {
                if let Some(s) = session.take() {
                    match why {
                        End::HostOffline => {
                            r.close(s, true).await;
                            retry_at = tokio::time::Instant::now() + r.cfg.host_retry;
                        }
                        End::Lost(e) => {
                            warn!(agent = %r.agent, error = %e, "sparkplug.session_lost");
                            r.close(s, false).await;
                            retry_at = tokio::time::Instant::now() + backoff;
                            backoff = (backoff * 2).min(r.cfg.reconnect_max);
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tv(v: Value, q: Quality) -> TagValue {
        TagValue::new(Some(v), q, Some(Utc::now()))
    }

    #[test]
    fn report_by_exception() {
        let r = |v: Value, q| Reported { value: Some(v), quality: q };
        assert!(!moved(&r(Value::Float(1.0), Quality::Good), &tv(Value::Float(1.4), Quality::Good), 0.5));
        assert!(moved(&r(Value::Float(1.0), Quality::Good), &tv(Value::Float(1.6), Quality::Good), 0.5));
        assert!(moved(&r(Value::Float(1.0), Quality::Good), &tv(Value::Float(1.0), Quality::BadComm), 0.5));
        assert!(moved(&r(Value::Bool(true), Quality::Good), &tv(Value::Bool(false), Quality::Good), 5.0));
        assert!(!moved(&r(Value::Str("a".into()), Quality::Good), &tv(Value::Str("a".into()), Quality::Good), 0.0));
        assert!(moved(&r(Value::Int(1), Quality::Good), &tv(Value::Int(2), Quality::Good), 0.0));
    }

    #[test]
    fn a_value_the_datatype_cannot_carry_is_null_bad_decode() {
        let meta = TagMeta { name: "N".into(), alias: 9, datatype: DataType::Int16, units: None, eng_range: None };
        let m = metric(&meta, Some(&tv(Value::Int(70_000), Quality::Good)), true, true);
        assert_eq!(m.is_null, Some(true));
        let q = m.properties.unwrap().values[0].value.clone();
        assert_eq!(q, Some(crate::proto::payload::property_value::Value::IntValue(0)));
    }
}
