//! Live values over a WebSocket: `GET /api/v1/ws`, the Quick Client feed (#164, the Rust port of
//! `src/opencap/api/live.py`, #31).
//!
//! Protocol `opencap.v1`, JSON text frames:
//!
//! ```text
//! client  {"subscribe": {"devices": ["<channel>/<device>", ...],
//!                        "tags": [{"device": "<channel>/<device>", "tag": "<name>"}, ...]}}
//!         each subscribe replaces the previous one; an empty one stops everything
//! server  {"type":"subscribed","devices","tags","conflate_ms"}      the ack, then per device:
//!         {"type":"snapshot","device","version","changes":[{tag,value,quality,source_ts}]}
//!         {"type":"changes","device","version","changes":[...]}     only what changed
//!         {"type":"unavailable","device","detail"}                  not polled; stays subscribed
//!         {"type":"error","detail"}                                 a message it could not use
//! ```
//!
//! Authentication is the REST API's (tokens, roles, open mode), checked before the upgrade: an
//! `Authorization: Bearer` header, or, for browsers, the subprotocol `opencap.bearer.<base64url>`
//! offered next to `opencap.v1`. A browser `Origin` must be the API's own or one of `cors_origins`
//! (D-067). One [`LiveHub`] ticks every 50 ms: a device whose change counter moved, and whose last
//! message left at least 250 ms ago, is diffed once against the state last sent and the batch is
//! offered to every subscriber. A connection has a bounded queue; one that falls behind loses it and
//! gets fresh snapshots instead of a backlog. Nothing here waits on PLC I/O.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::extract::ws::{CloseFrame, Message, WebSocket};
use serde_json::{json, Map, Value};
use tokio::sync::Notify;
use tokio::task::JoinHandle;
use tracing::{debug, info};

use crate::EngineView;

pub const PROTOCOL: &str = "opencap.v1";
pub const BEARER_PREFIX: &str = "opencap.bearer.";
pub const PATH: &str = "/api/v1/ws";
pub const MAX_MESSAGE: usize = 1024 * 1024;
const MAX_SUBSCRIBE_ITEMS: usize = 100_000;
const NOT_POLLED: &str = "not polled: unknown device, its channel is disabled or not selected, \
                          or the change is not reloaded yet";
const SUBSCRIBE_HELP: &str =
    r#"{"subscribe": {"devices": ["<channel>/<device>"], "tags": [{"device": "...", "tag": "..."}]}}"#;

#[derive(Debug, Clone)]
pub struct LiveCfg {
    /// At most one message per device and connection in this time.
    pub conflate: Duration,
    /// How often the hub looks at the change counters.
    pub tick: Duration,
    pub max_queue_bytes: usize,
    pub max_connections: usize,
}

impl Default for LiveCfg {
    fn default() -> Self {
        Self {
            conflate: Duration::from_millis(250),
            tick: Duration::from_millis(50),
            max_queue_bytes: 4 * 1024 * 1024,
            max_connections: 256,
        }
    }
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// `{"tag": name, "value", "quality", "source_ts"}`.
fn change(name: &str, tv: &Value) -> Value {
    let mut m = Map::new();
    m.insert("tag".into(), name.into());
    if let Value::Object(o) = tv {
        m.extend(o.iter().map(|(k, v)| (k.clone(), v.clone())));
    }
    Value::Object(m)
}

fn encode(v: &Value) -> String {
    v.to_string()
}

// --- auth helpers ---------------------------------------------------------------------------

/// The token of an `opencap.bearer.<base64url>` subprotocol; `Some("")` if it does not decode.
pub fn bearer_from_protocols(protocols: &[String]) -> Option<String> {
    let raw = protocols.iter().find_map(|p| p.strip_prefix(BEARER_PREFIX))?;
    let decoded = base64url_decode(raw).and_then(|b| String::from_utf8(b).ok());
    Some(decoded.unwrap_or_default())
}

fn base64url_decode(text: &str) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let (mut acc, mut bits) = (0u32, 0u32);
    for c in text.trim_end_matches('=').bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'-' | b'+' => 62,
            b'_' | b'/' => 63,
            _ => return None,
        };
        acc = (acc << 6) | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

/// No Origin (not a browser), the API's own origin, or a configured CORS origin.
pub fn origin_allowed(origin: Option<&str>, host: Option<&str>, cors_origins: &[String]) -> bool {
    let Some(origin) = origin else { return true };
    let origin = origin.trim().trim_end_matches('/');
    if cors_origins.iter().any(|o| o.trim_end_matches('/') == origin) {
        return true;
    }
    let Some((scheme, rest)) = origin.split_once("://") else { return false };
    matches!(scheme, "http" | "https")
        && !rest.is_empty()
        && !rest.contains('/')
        && rest.eq_ignore_ascii_case(host.unwrap_or("").trim())
}

// --- the subscribe message -------------------------------------------------------------------

/// device -> the tag names asked for on it.
pub type TagRefs = Vec<(String, Vec<String>)>;

type Subs = HashMap<String, Option<HashSet<String>>>;

/// (devices, device -> tag names) of a subscribe message, or the error text.
pub fn parse_subscribe(text: &str) -> Result<(Vec<String>, TagRefs), String> {
    let help = |what: String| format!("{what}; expected {SUBSCRIBE_HELP}");
    let v: Value = serde_json::from_str(text).map_err(|e| help(format!("message: {e}")))?;
    let sub = v.get("subscribe").and_then(Value::as_object).ok_or_else(|| help("subscribe: Field required".into()))?;
    let mut devices: Vec<String> = Vec::new();
    for d in sub.get("devices").map_or(Ok(&Vec::new()), |d| {
        d.as_array().ok_or_else(|| help("subscribe.devices: Input should be a valid list".into()))
    })? {
        let d = d.as_str().ok_or_else(|| help("subscribe.devices: Input should be a valid string".into()))?;
        if !devices.iter().any(|x| x == d) {
            devices.push(d.to_string());
        }
    }
    let mut tags: TagRefs = Vec::new();
    let mut items = devices.len();
    for r in sub.get("tags").map_or(Ok(&Vec::new()), |t| {
        t.as_array().ok_or_else(|| help("subscribe.tags: Input should be a valid list".into()))
    })? {
        let device = r
            .get("device")
            .and_then(Value::as_str)
            .ok_or_else(|| help("subscribe.tags.device: Field required".into()))?;
        let tag =
            r.get("tag").and_then(Value::as_str).ok_or_else(|| help("subscribe.tags.tag: Field required".into()))?;
        items += 1;
        if devices.iter().any(|d| d == device) {
            continue; // the whole device is subscribed already
        }
        match tags.iter_mut().find(|(d, _)| d == device) {
            Some((_, names)) => {
                if !names.iter().any(|n| n == tag) {
                    names.push(tag.to_string());
                }
            }
            None => tags.push((device.to_string(), vec![tag.to_string()])),
        }
    }
    if items > MAX_SUBSCRIBE_ITEMS {
        return Err(format!("at most {MAX_SUBSCRIBE_ITEMS} devices and tags per subscribe"));
    }
    Ok((devices, tags))
}

// --- hub -------------------------------------------------------------------------------------

struct Feed {
    clients: HashSet<u64>,
    seen: Option<u64>,
    version: u64,
    values: Map<String, Value>,
    polled: bool,
    last_sent: Option<Instant>,
}

impl Feed {
    fn new() -> Self {
        Self { clients: HashSet::new(), seen: None, version: 0, values: Map::new(), polled: false, last_sent: None }
    }

    fn snapshot_text(&self, device: &str, names: Option<&HashSet<String>>) -> String {
        let changes: Vec<Value> = self
            .values
            .iter()
            .filter(|(n, _)| names.is_none_or(|s| s.contains(*n)))
            .map(|(n, tv)| change(n, tv))
            .collect();
        let mut msg = json!({"type": "snapshot", "device": device, "version": self.version, "changes": changes});
        if let Some(names) = names {
            let mut missing: Vec<&String> = names.iter().filter(|n| !self.values.contains_key(*n)).collect();
            if !missing.is_empty() {
                missing.sort();
                msg["missing"] = json!(missing);
            }
        }
        encode(&msg)
    }
}

#[derive(Default)]
struct ClientState {
    /// Acks and errors, sent first.
    control: VecDeque<String>,
    /// Devices whose next message is a snapshot (insertion order).
    pending: Vec<String>,
    /// (device, encoded batch).
    queue: VecDeque<(String, String)>,
    queued_bytes: usize,
}

pub struct Client {
    id: u64,
    name: String,
    subs: Mutex<Subs>,
    st: Mutex<ClientState>,
    wake: Notify,
}

impl Client {
    fn offer(&self, hub: &LiveHub, device: &str, text: String) {
        let subs = lock(&self.subs).len();
        let mut st = lock(&self.st);
        if st.pending.iter().any(|d| d == device) {
            return; // its snapshot is not sent yet and will hold this
        }
        if st.queued_bytes + text.len() > hub.cfg.max_queue_bytes || st.queue.len() >= 16 + 4 * subs {
            // fell behind: drop the backlog, send fresh snapshots instead
            st.queue.clear();
            st.queued_bytes = 0;
            st.pending = lock(&self.subs).keys().cloned().collect();
            hub.resyncs.fetch_add(1, Ordering::Relaxed);
            info!(who = %self.name, devices = subs, "live_client_resync");
        } else {
            st.queued_bytes += text.len();
            st.queue.push_back((device.to_string(), text));
        }
        drop(st);
        self.wake.notify_one();
    }

    /// The device's next message is a snapshot (or `unavailable`); older batches go.
    fn resync(&self, device: &str) {
        let mut st = lock(&self.st);
        st.queue.retain(|(d, _)| d != device);
        st.queued_bytes = st.queue.iter().map(|(_, t)| t.len()).sum();
        if !st.pending.iter().any(|d| d == device) {
            st.pending.push(device.to_string());
        }
        drop(st);
        self.wake.notify_one();
    }

    fn restart(&self) {
        let devices: Vec<String> = lock(&self.subs).keys().cloned().collect();
        let mut st = lock(&self.st);
        st.queue.clear();
        st.queued_bytes = 0;
        st.pending = devices;
        drop(st);
        self.wake.notify_one();
    }

    fn say(&self, msg: &Value) {
        lock(&self.st).control.push_back(encode(msg));
        self.wake.notify_one();
    }

    /// The next frame to send, if any. Never holds the client's lock while asking the hub.
    fn next(&self, hub: &LiveHub) -> Option<String> {
        loop {
            let device = {
                let mut st = lock(&self.st);
                if let Some(t) = st.control.pop_front() {
                    return Some(t);
                }
                if st.pending.is_empty() {
                    return st.queue.pop_front().map(|(_, t)| {
                        st.queued_bytes -= t.len();
                        t
                    });
                }
                st.pending.remove(0)
            };
            if let Some(text) = hub.message_for(self, &device) {
                return Some(text);
            }
        }
    }
}

struct Inner {
    clients: HashMap<u64, Arc<Client>>,
    feeds: HashMap<String, Feed>,
    ticker: Option<JoinHandle<()>>,
}

pub struct LiveHub {
    view: Arc<dyn EngineView>,
    cfg: LiveCfg,
    inner: Mutex<Inner>,
    next_id: AtomicU64,
    /// Connections that fell behind and got snapshots (all time).
    pub resyncs: AtomicU64,
}

impl LiveHub {
    pub fn new(view: Arc<dyn EngineView>, cfg: LiveCfg) -> Arc<Self> {
        Arc::new(Self {
            view,
            cfg,
            inner: Mutex::new(Inner { clients: HashMap::new(), feeds: HashMap::new(), ticker: None }),
            next_id: AtomicU64::new(1),
            resyncs: AtomicU64::new(0),
        })
    }

    pub fn conflate_ms(&self) -> u64 {
        self.cfg.conflate.as_millis() as u64
    }

    pub fn full(&self) -> bool {
        lock(&self.inner).clients.len() >= self.cfg.max_connections
    }

    pub fn max_connections(&self) -> usize {
        self.cfg.max_connections
    }

    pub fn connections(&self) -> usize {
        lock(&self.inner).clients.len()
    }

    fn attach(self: &Arc<Self>, name: &str) -> Arc<Client> {
        let client = Arc::new(Client {
            id: self.next_id.fetch_add(1, Ordering::Relaxed),
            name: name.to_string(),
            subs: Mutex::new(HashMap::new()),
            st: Mutex::new(ClientState::default()),
            wake: Notify::new(),
        });
        let mut inner = lock(&self.inner);
        inner.clients.insert(client.id, client.clone());
        if inner.ticker.is_none() {
            let hub = self.clone();
            inner.ticker = Some(tokio::spawn(async move { hub.run().await }));
        }
        client
    }

    fn detach(&self, client: &Client) {
        let mut inner = lock(&self.inner);
        inner.clients.remove(&client.id);
        Self::unsubscribe(&mut inner, client);
        if inner.clients.is_empty() {
            if let Some(t) = inner.ticker.take() {
                t.abort(); // the last one left: no tick until the next attach
            }
        }
    }

    fn unsubscribe(inner: &mut Inner, client: &Client) {
        let subs: Vec<String> = std::mem::take(&mut *lock(&client.subs)).into_keys().collect();
        for device in subs {
            if let Some(feed) = inner.feeds.get_mut(&device) {
                feed.clients.remove(&client.id);
                if feed.clients.is_empty() {
                    inner.feeds.remove(&device);
                }
            }
        }
    }

    /// Replace the client's subscription; it then gets a snapshot of each device.
    fn subscribe(&self, client: &Client, devices: &[String], tags: &[(String, Vec<String>)]) {
        let mut inner = lock(&self.inner);
        Self::unsubscribe(&mut inner, client);
        let mut subs: Subs = devices.iter().map(|d| (d.clone(), None)).collect();
        for (d, names) in tags {
            subs.entry(d.clone()).or_insert_with(|| Some(names.iter().cloned().collect()));
        }
        for device in subs.keys() {
            if !inner.feeds.contains_key(device) {
                let mut feed = Feed::new();
                self.load(device, &mut feed);
                inner.feeds.insert(device.clone(), feed);
            }
            if let Some(feed) = inner.feeds.get_mut(device) {
                feed.clients.insert(client.id);
            }
        }
        *lock(&client.subs) = subs;
        client.restart();
    }

    fn read(&self, device: &str) -> Option<(u64, Map<String, Value>)> {
        let version = self.view.live_version(device)?;
        match self.view.device_values(device)? {
            Value::Object(values) => Some((version, values)),
            _ => None,
        }
    }

    fn load(&self, device: &str, feed: &mut Feed) {
        match self.read(device) {
            None => {
                feed.polled = false;
                feed.seen = None;
                feed.values = Map::new();
            }
            Some((version, values)) => {
                feed.polled = true;
                feed.seen = Some(version);
                feed.version = version;
                feed.values = values;
            }
        }
    }

    async fn run(self: Arc<Self>) {
        loop {
            tokio::time::sleep(self.cfg.tick).await;
            self.tick(Instant::now());
        }
    }

    /// One look at the change counters of every subscribed device.
    pub fn tick(&self, now: Instant) {
        let mut inner = lock(&self.inner);
        let Inner { clients, feeds, .. } = &mut *inner;
        for (device, feed) in feeds.iter_mut() {
            let version = self.view.live_version(device);
            let Some(version) = version else {
                if feed.polled {
                    Self::gone(feed, device, clients);
                }
                continue;
            };
            if feed.polled && feed.seen == Some(version) {
                continue;
            }
            if feed.last_sent.is_some_and(|t| now.duration_since(t) < self.cfg.conflate) {
                continue; // conflation: the changes wait for the next slot
            }
            self.flush(device, feed, clients, now);
        }
    }

    fn gone(feed: &mut Feed, device: &str, clients: &HashMap<u64, Arc<Client>>) {
        feed.polled = false;
        feed.seen = None;
        feed.values = Map::new();
        for id in &feed.clients {
            if let Some(c) = clients.get(id) {
                c.resync(device);
            }
        }
    }

    fn flush(&self, device: &str, feed: &mut Feed, clients: &HashMap<u64, Arc<Client>>, now: Instant) {
        let Some((version, values)) = self.read(device) else {
            if feed.polled {
                Self::gone(feed, device, clients);
            }
            return;
        };
        feed.seen = Some(version);
        let same_tags = values.len() == feed.values.len() && values.keys().all(|k| feed.values.contains_key(k));
        if !feed.polled || !same_tags {
            // back after a gap, or the tag set changed (reload): everyone gets a snapshot
            feed.polled = true;
            feed.version = version;
            feed.values = values;
            feed.last_sent = Some(now);
            for id in &feed.clients {
                if let Some(c) = clients.get(id) {
                    c.resync(device);
                }
            }
            return;
        }
        let changed: Vec<String> =
            values.iter().filter(|(n, tv)| feed.values.get(*n) != Some(tv)).map(|(n, _)| n.clone()).collect();
        feed.values = values;
        feed.version = version;
        if changed.is_empty() {
            return;
        }
        feed.last_sent = Some(now);
        let batch = |names: &mut dyn Iterator<Item = &String>| -> String {
            let changes: Vec<Value> = names.map(|n| change(n, &feed.values[n])).collect();
            encode(&json!({"type": "changes", "device": device, "version": version, "changes": changes}))
        };
        let mut whole: Option<String> = None;
        for id in &feed.clients {
            let Some(client) = clients.get(id) else { continue };
            let wanted = lock(&client.subs).get(device).cloned();
            match wanted {
                Some(None) => {
                    let text = whole.get_or_insert_with(|| batch(&mut changed.iter())).clone();
                    client.offer(self, device, text);
                }
                Some(Some(names)) => {
                    let mut mine = changed.iter().filter(|n| names.contains(*n)).peekable();
                    if mine.peek().is_some() {
                        client.offer(self, device, batch(&mut mine));
                    }
                }
                None => {}
            }
        }
    }

    /// What a client waiting on `device` gets now: its snapshot, or `unavailable`.
    fn message_for(&self, client: &Client, device: &str) -> Option<String> {
        let names = lock(&client.subs).get(device).cloned()?;
        let inner = lock(&self.inner);
        let feed = inner.feeds.get(device)?;
        if !feed.polled {
            return Some(encode(&json!({"type": "unavailable", "device": device, "detail": NOT_POLLED})));
        }
        Some(feed.snapshot_text(device, names.as_ref()))
    }
}

// --- one connection --------------------------------------------------------------------------

/// Serve one upgraded connection until it closes.
pub async fn serve(hub: Arc<LiveHub>, mut socket: WebSocket, who: String) {
    let client = hub.attach(&who);
    debug!(who = %who, "live_connected");
    loop {
        tokio::select! {
            // frames to send: acks, snapshots, batches
            () = client.wake.notified() => {
                while let Some(text) = client.next(&hub) {
                    if socket.send(Message::Text(text.into())).await.is_err() {
                        hub.detach(&client);
                        return;
                    }
                }
            }
            incoming = socket.recv() => {
                let Some(Ok(message)) = incoming else { break };
                match message {
                    Message::Close(_) => break,
                    Message::Text(text) => {
                        if text.len() > MAX_MESSAGE {
                            client.say(&json!({"type": "error", "detail": format!("message larger than {MAX_MESSAGE} bytes")}));
                            continue;
                        }
                        match parse_subscribe(&text) {
                            Ok((devices, tags)) => {
                                hub.subscribe(&client, &devices, &tags);
                                let tag_refs: Vec<Value> = tags
                                    .iter()
                                    .flat_map(|(d, names)| {
                                        let mut names = names.clone();
                                        names.sort();
                                        names.into_iter().map(move |t| json!({"device": d, "tag": t}))
                                    })
                                    .collect();
                                client.say(&json!({"type": "subscribed", "devices": devices, "tags": tag_refs,
                                                   "conflate_ms": hub.conflate_ms()}));
                            }
                            Err(e) => client.say(&json!({"type": "error", "detail": e})),
                        }
                    }
                    Message::Binary(_) => client.say(&json!({"type": "error", "detail": "send JSON text frames"})),
                    _ => {}
                }
            }
        }
    }
    hub.detach(&client);
    let _ = socket.send(Message::Close(Some(CloseFrame { code: 1000, reason: "".into() }))).await;
}
