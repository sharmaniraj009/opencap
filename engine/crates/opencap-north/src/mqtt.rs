//! MQTT link to the broker (Python `opencap.north.mqtt`), on rumqttc.
//!
//! `publish()` only appends to the SQLite outbox and wakes the drainer: it never waits on
//! the network. `run()` is the connect loop:
//!
//! 1. connect (backoff 1 s, 2 s, 4 s ... 30 s between failed attempts; reset after a
//!    successful connect), with the LWT `<topic_prefix>/poller/<slug(instance)>/status` =
//!    `offline` (retained, QoS 1);
//! 2. publish `online` (retained, QoS 1) directly, not through the outbox;
//! 3. drain the outbox strictly in id order: a window of up to `window` messages is sent
//!    (rumqttc writes requests in order), the rows the broker acknowledged are deleted,
//!    then the next window;
//! 4. on any connection problem the session ends and the loop goes back to 1.
//!    Unacknowledged rows stay in the outbox and are sent again (QoS 1 = at least once).
//!
//! Each connection uses a NEW rumqttc client and event loop (clean session): in-flight
//! state is never reused; the outbox is the only source of truth. PUBACKs are matched
//! to rows through the packet ids rumqttc reports for each outgoing publish, in order.
//!
//! `mqtt_published_total` counts outbox messages acknowledged by the broker; the link's
//! own `online`/`offline` messages are not counted.

use std::collections::{HashMap, VecDeque};
use std::io::Write as _;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use base64::Engine as _;
use rumqttc::{
    AsyncClient, ClientError, Event, LastWill, MqttOptions, Outgoing, Packet, QoS, TlsConfiguration, Transport,
};
use tokio::sync::{mpsc, Notify};
use tokio::time::Instant;
use tracing::{debug, error, info, warn};

use opencap_core::config::{slug, MqttCfg};
use opencap_core::metrics::METRICS;

use crate::saf::{OutMsg, StoreAndForward};

pub const ONLINE: &[u8] = b"online";
pub const OFFLINE: &[u8] = b"offline";

/// `<prefix>/poller/<slug(instance)>/status`; `prefix` is `mqtt.topic_prefix` (D-028).
pub fn lwt_topic(instance: &str, prefix: &str) -> String {
    format!("{prefix}/poller/{}/status", slug(instance))
}

/// Connection options for `cfg`'s broker under `client_id`: keep-alive, clean session,
/// credentials (password from `password_env`) and TLS. Each agent adds its own Will and limits
/// (the MQTT link here, the Sparkplug edge node in `engine/north/sparkplug`).
pub fn client_options(cfg: &MqttCfg, client_id: &str) -> Result<MqttOptions, String> {
    let port = u16::try_from(cfg.port).map_err(|_| format!("bad MQTT port {}", cfg.port))?;
    let mut mo = MqttOptions::new(client_id.to_string(), cfg.host.clone(), port);
    mo.set_keep_alive(Duration::from_secs(cfg.keepalive_s.max(1) as u64));
    mo.set_clean_session(true);
    if let Some(user) = &cfg.username {
        let password = cfg.password_env.as_ref().and_then(|e| std::env::var(e).ok()).unwrap_or_default();
        mo.set_credentials(user.clone(), password);
    }
    if cfg.tls {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let tls = match &cfg.ca_file {
            Some(path) => {
                let ca = std::fs::read(path).map_err(|e| format!("cannot read ca_file {path}: {e}"))?;
                TlsConfiguration::Simple { ca, alpn: None, client_auth: None }
            }
            None => TlsConfiguration::default(),
        };
        mo.set_transport(Transport::Tls(tls));
    }
    Ok(mo)
}

/// Reject topics a broker would refuse; such a row would block the outbox forever.
pub fn check_topic(topic: &str) -> Result<(), String> {
    if topic.is_empty() {
        return Err("MQTT topic must be a non-empty string".into());
    }
    if topic.contains('+') || topic.contains('#') || topic.contains('\0') {
        return Err(format!("invalid MQTT publish topic {topic:?} (wildcard or NUL)"));
    }
    if topic.starts_with('$') {
        return Err(format!("invalid MQTT publish topic {topic:?} (leading '$' is reserved)"));
    }
    if topic.len() > 65535 {
        return Err("MQTT topic longer than 65535 bytes".into());
    }
    Ok(())
}

#[derive(Debug, Clone)]
pub struct LinkOptions {
    pub backoff_min_s: f64,
    pub backoff_max_s: f64,
    pub window: usize,
    pub ack_timeout_s: f64,
    pub connect_timeout_s: f64,
    /// Largest MQTT packet we send or accept (MQTT's own limit is 256 MiB).
    pub max_packet_size: usize,
}

impl Default for LinkOptions {
    fn default() -> Self {
        Self {
            backoff_min_s: 1.0,
            backoff_max_s: 30.0,
            window: 32,
            ack_timeout_s: 30.0,
            connect_timeout_s: 10.0,
            max_packet_size: 268_435_455,
        }
    }
}

#[derive(Debug)]
enum LinkEvent {
    Sent(u16),
    Acked(u16),
    Lost(String),
}

enum Ended {
    Stopped,
}

pub struct MqttLink {
    cfg: MqttCfg,
    pub lwt_topic: String,
    pub client_id: String,
    saf: Arc<StdMutex<StoreAndForward>>,
    opts: LinkOptions,
    wake: Notify,
    stop_signal: Notify,
    connected: AtomicBool,
    stopping: AtomicBool,
    established: AtomicBool,
    /// Successful connections so far.
    pub connects: AtomicU64,
    last_error: StdMutex<Option<String>>,
    tap: Option<StdMutex<std::fs::File>>,
}

fn lock<T>(m: &StdMutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn qos_of(q: i64) -> QoS {
    match q {
        0 => QoS::AtMostOnce,
        2 => QoS::ExactlyOnce,
        _ => QoS::AtLeastOnce,
    }
}

impl MqttLink {
    pub fn new(cfg: MqttCfg, instance: &str, saf: Arc<StdMutex<StoreAndForward>>, opts: LinkOptions) -> Self {
        // Test instrumentation (conformance suite): every message handed to the link is also
        // appended to this file, as JSON lines, BEFORE it enters the outbox.
        let tap = std::env::var_os("OPENCAP_PUBLISH_TAP")
            .and_then(|p| std::fs::OpenOptions::new().create(true).append(true).open(p).ok().map(StdMutex::new));
        Self {
            client_id: cfg.client_id.clone().unwrap_or_else(|| instance.to_string()),
            lwt_topic: lwt_topic(instance, &cfg.topic_prefix),
            cfg,
            saf,
            opts,
            wake: Notify::new(),
            stop_signal: Notify::new(),
            connected: AtomicBool::new(false),
            stopping: AtomicBool::new(false),
            established: AtomicBool::new(false),
            connects: AtomicU64::new(0),
            last_error: StdMutex::new(None),
            tap,
        }
    }

    pub fn connected(&self) -> bool {
        self.connected.load(Ordering::SeqCst)
    }

    pub fn is_secure(&self) -> bool {
        self.cfg.tls
    }

    pub fn last_error(&self) -> Option<String> {
        lock(&self.last_error).clone()
    }

    pub fn saf(&self) -> &Arc<StdMutex<StoreAndForward>> {
        &self.saf
    }

    /// Queue a message (outbox first). Never waits on the network.
    pub fn publish(&self, topic: &str, payload: &[u8], retain: bool) -> Result<(), String> {
        check_topic(topic)?;
        if let Some(tap) = &self.tap {
            let line = serde_json::json!({
                "topic": topic,
                "payload_b64": base64::engine::general_purpose::STANDARD.encode(payload),
            });
            let mut f = lock(tap);
            let _ = f.write_all(format!("{line}\n").as_bytes());
        }
        lock(&self.saf).put(topic, payload, self.cfg.qos, retain).map_err(|e| e.to_string())?;
        self.wake.notify_one();
        Ok(())
    }

    /// Connect / reconnect until `stop()`.
    pub async fn run(self: Arc<Self>) {
        let mut backoff = self.opts.backoff_min_s;
        let mut failures = 0u32;
        while !self.stopping.load(Ordering::SeqCst) {
            match self.session().await {
                Ok(Ended::Stopped) => break,
                Err(e) => *lock(&self.last_error) = Some(e),
            }
            if self.stopping.load(Ordering::SeqCst) {
                break;
            }
            let queued = lock(&self.saf).count();
            let err = self.last_error().unwrap_or_default();
            if self.established.load(Ordering::SeqCst) {
                backoff = self.opts.backoff_min_s;
                failures = 0;
                warn!(host = %self.cfg.host, port = self.cfg.port, error = %err, queued, "mqtt_disconnected");
            } else {
                failures += 1;
                if failures == 1 {
                    warn!(host = %self.cfg.host, port = self.cfg.port, error = %err, retry_in_s = backoff,
                          attempt = failures, queued, "mqtt_connect_failed");
                } else {
                    debug!(host = %self.cfg.host, port = self.cfg.port, error = %err, retry_in_s = backoff,
                           attempt = failures, queued, "mqtt_connect_failed");
                }
            }
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs_f64(backoff)) => {}
                _ = self.stop_signal.notified() => {}
            }
            if !self.established.load(Ordering::SeqCst) {
                backoff = (backoff * 2.0).min(self.opts.backoff_max_s);
            }
        }
    }

    /// Publish `offline` (retained) if connected, disconnect cleanly and end `run()`.
    /// Messages still in the outbox stay there for the next start. The caller awaits the
    /// `run()` task afterwards.
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        self.stop_signal.notify_one();
        self.wake.notify_one();
    }

    fn mqtt_options(&self) -> Result<MqttOptions, String> {
        let mut mo = client_options(&self.cfg, &self.client_id)?;
        mo.set_last_will(LastWill::new(self.lwt_topic.clone(), OFFLINE.to_vec(), QoS::AtLeastOnce, true));
        mo.set_inflight(self.opts.window.min(u16::MAX as usize - 1) as u16);
        mo.set_max_packet_size(self.opts.max_packet_size, self.opts.max_packet_size);
        Ok(mo)
    }

    /// One connection, from CONNECT until it breaks.
    async fn session(&self) -> Result<Ended, String> {
        self.established.store(false, Ordering::SeqCst);
        let mo = self.mqtt_options()?;
        let (client, mut el) = AsyncClient::new(mo, self.opts.window + 8);
        let mut net = el.network_options();
        net.set_connection_timeout(self.opts.connect_timeout_s.ceil().max(1.0) as u64);
        el.set_network_options(net);
        let connect_timeout = Duration::from_secs_f64(self.opts.connect_timeout_s + 1.0);
        let connack = tokio::time::timeout(connect_timeout, async {
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
            Err(_) => return Err("Operation timed out".into()),
        }
        self.established.store(true, Ordering::SeqCst);
        self.connected.store(true, Ordering::SeqCst);
        self.connects.fetch_add(1, Ordering::SeqCst);
        *lock(&self.last_error) = None;
        info!(host = %self.cfg.host, port = self.cfg.port, client_id = %self.client_id, tls = self.is_secure(),
              queued = lock(&self.saf).count(), "mqtt_connected");

        let (tx, mut rx) = mpsc::unbounded_channel::<LinkEvent>();
        let poller = tokio::spawn(async move {
            loop {
                let ev = match el.poll().await {
                    Ok(Event::Outgoing(Outgoing::Publish(pkid))) => LinkEvent::Sent(pkid),
                    Ok(Event::Incoming(Packet::PubAck(a))) => LinkEvent::Acked(a.pkid),
                    Ok(Event::Incoming(Packet::PubComp(a))) => LinkEvent::Acked(a.pkid),
                    Ok(_) => continue,
                    Err(e) => {
                        let _ = tx.send(LinkEvent::Lost(e.to_string()));
                        return;
                    }
                };
                if tx.send(ev).is_err() {
                    return;
                }
            }
        });
        let result = self.online_then_drain(&client, &mut rx).await;
        self.connected.store(false, Ordering::SeqCst);
        if matches!(result, Ok(Ended::Stopped)) {
            let _ = client.try_disconnect();
            let _ = tokio::time::timeout(Duration::from_millis(500), async {
                while let Some(ev) = rx.recv().await {
                    if matches!(ev, LinkEvent::Lost(_)) {
                        break;
                    }
                }
            })
            .await;
        }
        poller.abort();
        result
    }

    async fn online_then_drain(
        &self,
        client: &AsyncClient,
        rx: &mut mpsc::UnboundedReceiver<LinkEvent>,
    ) -> Result<Ended, String> {
        let online = OutMsg { id: -1, topic: self.lwt_topic.clone(), payload: ONLINE.to_vec(), qos: 1, retain: true };
        self.send(client, rx, &[online], Duration::from_secs_f64(self.opts.ack_timeout_s)).await?;
        loop {
            if self.stopping.load(Ordering::SeqCst) {
                let offline =
                    OutMsg { id: -1, topic: self.lwt_topic.clone(), payload: OFFLINE.to_vec(), qos: 1, retain: true };
                let _ = self.send(client, rx, &[offline], Duration::from_secs(2)).await;
                return Ok(Ended::Stopped);
            }
            let batch = lock(&self.saf).peek(self.opts.window).map_err(|e| e.to_string())?;
            if batch.is_empty() {
                tokio::select! {
                    _ = self.wake.notified() => {}
                    ev = rx.recv() => match ev {
                        Some(LinkEvent::Lost(e)) => return Err(e),
                        None => return Err("event loop ended".into()),
                        Some(_) => {}
                    },
                }
                continue;
            }
            self.send(client, rx, &batch, Duration::from_secs_f64(self.opts.ack_timeout_s)).await?;
        }
    }

    /// Send one window in id order; delete what the broker acknowledged. Messages with id
    /// < 0 are the link's own (online/offline) and are not in the outbox.
    async fn send(
        &self,
        client: &AsyncClient,
        rx: &mut mpsc::UnboundedReceiver<LinkEvent>,
        batch: &[OutMsg],
        ack_timeout: Duration,
    ) -> Result<(), String> {
        let mut rejected: Vec<i64> = Vec::new();
        let mut order: VecDeque<(i64, bool)> = VecDeque::new(); // (id, qos0) in send order
        for m in batch {
            if m.topic.len() + m.payload.len() + 16 > self.opts.max_packet_size {
                error!(id = m.id, topic = %m.topic, error = "payload too large", "mqtt_message_rejected");
                rejected.push(m.id);
                continue;
            }
            match client.publish(m.topic.clone(), qos_of(m.qos), m.retain, m.payload.clone()).await {
                Ok(()) => order.push_back((m.id, m.qos == 0)),
                Err(ClientError::Request(_)) | Err(ClientError::TryRequest(_)) if !rumqttc::valid_topic(&m.topic) => {
                    // the client refused the message itself (invalid topic): it would block
                    // the outbox forever, so it is dropped
                    error!(id = m.id, topic = %m.topic, error = "rejected by the MQTT client", "mqtt_message_rejected");
                    rejected.push(m.id);
                }
                Err(_) => {
                    // same error for a closed event loop: the connection is gone, keep the row
                    self.ack_outbox(&[], &rejected)?;
                    return Err("connection lost while sending".into());
                }
            }
        }
        let expected = order.len();
        let mut by_pkid: HashMap<u16, i64> = HashMap::new();
        let mut acked: Vec<i64> = Vec::with_capacity(expected);
        let deadline = Instant::now() + ack_timeout;
        let mut failure: Option<String> = None;
        while acked.len() < expected {
            match tokio::time::timeout_at(deadline, rx.recv()).await {
                Ok(Some(LinkEvent::Sent(pkid))) => {
                    if let Some((id, qos0)) = order.pop_front() {
                        if qos0 {
                            acked.push(id);
                        } else {
                            by_pkid.insert(pkid, id);
                        }
                    }
                }
                Ok(Some(LinkEvent::Acked(pkid))) => {
                    if let Some(id) = by_pkid.remove(&pkid) {
                        acked.push(id);
                    }
                }
                Ok(Some(LinkEvent::Lost(e))) => {
                    failure = Some(e);
                    break;
                }
                Ok(None) => {
                    failure = Some("event loop ended".into());
                    break;
                }
                Err(_) => {
                    failure = Some("Operation timed out".into());
                    break;
                }
            }
        }
        self.ack_outbox(&acked, &rejected)?;
        match failure {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// Delete acknowledged (and locally rejected) outbox rows; count the acknowledged ones.
    fn ack_outbox(&self, acked: &[i64], rejected: &[i64]) -> Result<(), String> {
        let outbox_acked: Vec<i64> = acked.iter().copied().filter(|&id| id >= 0).collect();
        let mut all = outbox_acked.clone();
        all.extend(rejected.iter().copied().filter(|&id| id >= 0));
        if !all.is_empty() {
            lock(&self.saf).ack(&all).map_err(|e| e.to_string())?;
            METRICS.mqtt_published.inc_by(outbox_acked.len() as u64);
        }
        Ok(())
    }
}
