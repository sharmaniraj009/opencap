//! Channel worker fault rules, ported from tests/unit/test_channel_worker.py with a
//! scriptable fake driver (no sockets). Real time with short timings, like the Python tests.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use indexmap::IndexMap;

use opencap_core::config::{Channel, DType, Device, Scale, Tag};
use opencap_core::driver::{Block, BlockRef, Driver, DriverError, DriverFactory, Plan, ReadResult};
use opencap_core::health::State;
use opencap_core::registry::{plan_device, DevicePlan};
use opencap_core::store::{Change, TagStore};
use opencap_core::value::{mono, now_utc, Quality, TagValue, Value};
use opencap_core::worker::{ChannelWorker, WorkerOptions};

#[derive(Debug)]
struct FakeBlock {
    key: String,
    tags: Vec<String>,
}

impl Block for FakeBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }
    fn describe(&self) -> String {
        format!("fake {} [{} tags]", self.key, self.tags.len())
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

#[derive(Default)]
struct Script {
    delay: Duration,
    down: bool,
    unsupported: bool,
    hang: bool,
    close_on_timeout: bool,
    timeout_blocks: HashSet<String>,
    lost_blocks: HashSet<String>,
    config_error_blocks: HashSet<String>,
    read_failures: VecDeque<DriverError>,
    constant: HashMap<String, Value>,
    /// the driver packs blocks: `supports_batch` and `read_many`
    batch: bool,
    // records
    connect_attempts: Vec<f64>,
    connects_ok: usize,
    closes: usize,
    reads: Vec<String>,
    batch_calls: usize,
    good_reads: HashMap<String, i64>,
}

impl Script {
    fn reads_of(&self, key: &str) -> usize {
        self.reads.iter().filter(|k| *k == key).count()
    }
}

#[derive(Default)]
struct Probe {
    in_flight: AtomicUsize,
    max: AtomicUsize,
    total: AtomicUsize,
}

impl Probe {
    /// Mark a request as on the wire until the returned guard drops. A request future that
    /// is cancelled (safety-net timeout, `stop()` aborting device tasks one by one) drops the
    /// guard too, as the real wire lock is released then; a manual exit() would leak it.
    fn enter(self: &Arc<Self>) -> InFlight {
        let n = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        self.max.fetch_max(n, Ordering::SeqCst);
        self.total.fetch_add(1, Ordering::SeqCst);
        InFlight(self.clone())
    }
}

struct InFlight(Arc<Probe>);

impl Drop for InFlight {
    fn drop(&mut self) {
        self.0.in_flight.fetch_sub(1, Ordering::SeqCst);
    }
}

struct FakeDriver {
    script: Arc<Mutex<Script>>,
    probes: Vec<Arc<Probe>>,
    connected: bool,
}

fn key_of(address: &str) -> &str {
    address.split('.').next().unwrap_or(address)
}

#[async_trait]
impl Driver for FakeDriver {
    fn name(&self) -> &'static str {
        "fake"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        let mut groups: IndexMap<String, Vec<String>> = IndexMap::new();
        let mut plan = Plan::default();
        for t in tags {
            if t.address.starts_with('!') {
                plan.invalid.insert(t.name.clone(), format!("unusable address {}", t.address));
                continue;
            }
            groups.entry(key_of(&t.address).to_string()).or_default().push(t.name.clone());
        }
        groups.sort_keys();
        for (key, mut names) in groups {
            names.sort();
            plan.blocks.push(Arc::new(FakeBlock { key, tags: names }) as BlockRef);
        }
        plan
    }

    fn connected(&self) -> bool {
        self.connected
    }

    fn reports_connection(&self) -> bool {
        true
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        let _in_flight: Vec<InFlight> = self.probes.iter().map(|p| p.enter()).collect();
        let (down, unsupported, delay) = {
            let mut s = self.script.lock().unwrap();
            s.connect_attempts.push(mono());
            (s.down, s.unsupported, s.delay)
        };
        tokio::time::sleep(delay).await;
        drop(_in_flight);
        if unsupported {
            return Err(DriverError::NotImplemented("fake: no I/O".into()));
        }
        if down {
            self.connected = false;
            return Err(DriverError::ConnectionLost("refused".into()));
        }
        self.connected = true;
        self.script.lock().unwrap().connects_ok += 1;
        Ok(())
    }

    async fn close(&mut self) {
        if self.connected {
            self.script.lock().unwrap().closes += 1;
        }
        self.connected = false;
    }

    fn supports_batch(&self) -> bool {
        self.script.lock().unwrap().batch
    }

    async fn read_many(&mut self, blocks: &[&dyn Block]) -> Vec<ReadResult> {
        self.script.lock().unwrap().batch_calls += 1;
        let mut out = Vec::new();
        for b in blocks {
            out.push(self.read(*b).await);
        }
        out
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let b = block.as_any().downcast_ref::<FakeBlock>().unwrap();
        if !self.connected {
            return Err(DriverError::ConnectionLost("not connected".into()));
        }
        let _in_flight: Vec<InFlight> = self.probes.iter().map(|p| p.enter()).collect();
        let (delay, hang) = {
            let mut s = self.script.lock().unwrap();
            s.reads.push(b.key.clone());
            (s.delay, s.hang)
        };
        if hang {
            tokio::time::sleep(Duration::from_secs(3600)).await;
        }
        tokio::time::sleep(delay).await;
        drop(_in_flight);
        let mut s = self.script.lock().unwrap();
        if let Some(e) = s.read_failures.pop_front() {
            if matches!(e, DriverError::ConnectionLost(_)) {
                self.connected = false;
            }
            return Err(e);
        }
        if s.config_error_blocks.contains(&b.key) {
            return Err(DriverError::BlockConfig("illegal data address".into()));
        }
        if s.lost_blocks.contains(&b.key) {
            self.connected = false;
            return Err(DriverError::ConnectionLost("reset".into()));
        }
        if s.timeout_blocks.contains(&b.key) {
            if s.close_on_timeout {
                self.connected = false;
            }
            return Err(DriverError::Comm("no reply".into()));
        }
        let n = {
            let c = s.good_reads.entry(b.key.clone()).or_insert(0);
            *c += 1;
            *c
        };
        let ts = now_utc();
        Ok(b.tags
            .iter()
            .map(|t| (t.clone(), TagValue::good(s.constant.get(t).cloned().unwrap_or(Value::Int(n)), ts)))
            .collect())
    }
}

struct Rig {
    scripts: HashMap<String, Arc<Mutex<Script>>>,
    channel_probes: HashMap<String, Arc<Probe>>,
    global: Arc<Probe>,
}

impl Rig {
    fn new(devices: &[Device]) -> Arc<Self> {
        Arc::new(Self {
            scripts: devices.iter().map(|d| (d.id.clone(), Arc::new(Mutex::new(Script::default())))).collect(),
            channel_probes: devices.iter().map(|d| (d.channel.clone(), Arc::new(Probe::default()))).collect(),
            global: Arc::new(Probe::default()),
        })
    }

    fn factory(self: &Arc<Self>) -> DriverFactory {
        let me = self.clone();
        Arc::new(move |d: &Device| {
            if d.driver != "fake" {
                return Err(format!("driver '{}' is not implemented yet", d.driver));
            }
            Ok(Box::new(FakeDriver {
                script: me.scripts[&d.id].clone(),
                probes: vec![me.channel_probes[&d.channel].clone(), me.global.clone()],
                connected: false,
            }) as Box<dyn Driver>)
        })
    }

    fn script(&self, id: &str) -> std::sync::MutexGuard<'_, Script> {
        self.scripts[id].lock().unwrap()
    }
}

fn device(id: &str, tags: &[(&str, &str)]) -> Device {
    let (channel, name) = id.split_once('/').unwrap();
    let mut d = Device::new(
        id,
        channel,
        name,
        "fake",
        "192.0.2.1",
        1,
        tags.iter().map(|(n, a)| Tag::new(n, a, DType::Int32)).collect(),
    );
    d.scan_ms = 50;
    d.min_scan_ms = 1;
    d.timeout_ms = 200;
    d.retries = 1;
    d.demote_after = 3;
    d.demote_for_ms = 300;
    d
}

fn opts() -> WorkerOptions {
    WorkerOptions {
        start_jitter: 0.0,
        backoff_base_s: 0.05,
        backoff_max_s: 0.4,
        stats_interval_s: 0.0,
        ..Default::default()
    }
}

struct Setup {
    rig: Arc<Rig>,
    store: Arc<TagStore>,
    workers: Vec<Arc<ChannelWorker>>,
}

fn setup_with(devices: Vec<Device>, channel_enabled: bool, opts: WorkerOptions) -> Setup {
    let rig = Rig::new(&devices);
    let factory = rig.factory();
    let store = Arc::new(TagStore::new());
    let mut channels: IndexMap<String, Vec<Device>> = IndexMap::new();
    for d in devices {
        channels.entry(d.channel.clone()).or_default().push(d);
    }
    let workers = channels
        .into_iter()
        .map(|(name, devs)| {
            let plans: IndexMap<String, Arc<DevicePlan>> =
                devs.iter().map(|d| (d.id.clone(), Arc::new(plan_device(d, &factory)))).collect();
            let ch = Channel { name, enabled: channel_enabled, devices: devs };
            ChannelWorker::new(&ch, &plans, store.clone(), factory.clone(), opts.clone())
        })
        .collect();
    Setup { rig, store, workers }
}

fn setup(devices: Vec<Device>) -> Setup {
    setup_with(devices, true, opts())
}

async fn wait_for(what: &str, timeout_s: f64, mut pred: impl FnMut() -> bool) {
    let deadline = mono() + timeout_s;
    while !pred() {
        assert!(mono() < deadline, "timed out after {timeout_s}s waiting for {what}");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn one_request_on_the_wire_per_channel_and_channels_in_parallel() {
    let mut devs = Vec::new();
    for ch in ["A", "B"] {
        for k in 0..3 {
            devs.push(device(&format!("{ch}/D{k}"), &[("X", "a.0"), ("Y", "b.0")]));
        }
    }
    let s = setup(devs);
    for id in s.rig.scripts.keys() {
        s.rig.script(id).delay = Duration::from_millis(10);
    }
    s.workers.iter().for_each(|w| w.start());
    tokio::time::sleep(Duration::from_millis(600)).await;
    for w in &s.workers {
        w.stop().await;
    }
    for ch in ["A", "B"] {
        let p = &s.rig.channel_probes[ch];
        assert_eq!(p.max.load(Ordering::SeqCst), 1, "channel {ch} had overlapping requests");
        assert!(p.total.load(Ordering::SeqCst) > 20);
    }
    assert_eq!(s.rig.global.max.load(Ordering::SeqCst), 2, "the two channels poll in parallel");
}

/// Mutation check for the one-request-on-the-wire assertion: the probe really reports an
/// overlap, and a cancelled request does not leak an in-flight count.
#[test]
fn probe_detects_overlap_and_is_drop_safe() {
    let p = Arc::new(Probe::default());
    {
        let _a = p.enter();
        let _b = p.enter();
    }
    assert_eq!(p.max.load(Ordering::SeqCst), 2, "an overlapping driver must be visible");
    assert_eq!(p.in_flight.load(Ordering::SeqCst), 0, "dropped guards release the count");
    let cancelled = p.enter();
    drop(cancelled); // request abandoned mid-flight
    let _next = p.enter();
    assert_eq!(p.in_flight.load(Ordering::SeqCst), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn retries_count_and_timeout_keeps_last_good_value() {
    let s = setup(vec![device("S/D", &[("X", "a.0")])]);
    let w = &s.workers[0];
    w.read_once("S/D").await.unwrap();
    assert_eq!(s.store.get("S/D", "X").unwrap().value, Some(Value::Int(1)));
    s.rig.script("S/D").timeout_blocks.insert("a".into());
    let snap = w.read_once("S/D").await.unwrap();
    assert_eq!(s.rig.script("S/D").reads_of("a"), 1 + 2, "first attempt + retries=1");
    let x = &snap["X"];
    assert_eq!((x.quality, x.value.clone()), (Quality::BadComm, Some(Value::Int(1))));
    let st = w.device_status("S/D").unwrap();
    assert_eq!(st.last_error.as_deref(), Some("fake a [1 tags]: CommError: no reply"));
    assert_eq!(st.last_reason.as_deref(), Some("timeout"), "`no reply` is a timeout (LOG-R6)");
}

#[tokio::test(flavor = "multi_thread")]
async fn last_reason_follows_last_error_and_clears_with_it() {
    let mut d = device("S/D", &[("X", "a.0")]);
    d.retries = 0;
    let s = setup(vec![d]);
    let w = &s.workers[0];
    w.read_once("S/D").await.unwrap();
    assert_eq!(w.device_status("S/D").unwrap().last_reason, None);
    s.rig.script("S/D").read_failures.push_back(DriverError::Comm("Connection refused (os error 111)".into()));
    w.read_once("S/D").await.unwrap();
    let st = w.device_status("S/D").unwrap();
    assert_eq!(st.last_reason.as_deref(), Some("refused"));
    assert!(st.last_error.unwrap().contains("Connection refused"));
}

#[tokio::test(flavor = "multi_thread")]
async fn close_on_timeout_reopens_the_socket_for_each_retry() {
    let mut d = device("S/D", &[("X", "a.0")]);
    d.retries = 2;
    let s = setup(vec![d]);
    {
        let mut sc = s.rig.script("S/D");
        sc.timeout_blocks.insert("a".into());
        sc.close_on_timeout = true;
    }
    s.workers[0].read_once("S/D").await.unwrap();
    let sc = s.rig.script("S/D");
    assert_eq!(sc.reads_of("a"), 3);
    assert_eq!(sc.connect_attempts.len(), 3, "connect, then reopen before each retry");
}

#[tokio::test(flavor = "multi_thread")]
async fn connection_lost_on_idle_socket_reconnects_without_using_a_retry() {
    let mut d = device("S/D", &[("X", "a.0")]);
    d.retries = 0;
    let s = setup(vec![d]);
    let w = &s.workers[0];
    w.read_once("S/D").await.unwrap();
    s.rig.script("S/D").read_failures.push_back(DriverError::ConnectionLost("peer closed".into()));
    let snap = w.read_once("S/D").await.unwrap();
    assert_eq!(snap["X"].quality, Quality::Good, "reconnected and read in the same attempt");
    assert_eq!(s.rig.script("S/D").connects_ok, 2);
}

#[tokio::test(flavor = "multi_thread")]
async fn connection_lost_right_after_connect_ends_the_cycle() {
    let s = setup(vec![device("S/D", &[("X", "a.0"), ("Y", "b.0")])]);
    s.rig.script("S/D").lost_blocks.insert("a".into());
    let snap = s.workers[0].read_once("S/D").await.unwrap();
    assert_eq!(snap["X"].quality, Quality::BadComm);
    assert_eq!(snap["Y"].quality, Quality::BadComm);
    assert_eq!(s.rig.script("S/D").reads_of("b"), 0, "no request for the remaining blocks");
}

#[tokio::test(flavor = "multi_thread")]
async fn safety_net_timeout_is_a_comm_error_and_drops_the_connection() {
    let mut d = device("S/D", &[("X", "a.0")]);
    d.retries = 0;
    d.timeout_ms = 40; // safety net 0.15 s
    let s = setup(vec![d]);
    s.rig.script("S/D").hang = true;
    let t0 = mono();
    let snap = s.workers[0].read_once("S/D").await.unwrap();
    assert!(mono() - t0 < 1.0);
    assert_eq!(snap["X"].quality, Quality::BadComm);
    let st = s.workers[0].device_status("S/D").unwrap();
    assert!(st.last_error.unwrap().contains("no answer within the safety timeout"));
    assert!(!st.connected);
}

#[tokio::test(flavor = "multi_thread")]
async fn reconnect_backoff_doubles_and_caps() {
    let mut d = device("S/D", &[("X", "a.0")]);
    d.scan_ms = 10;
    d.demote_after = 0; // no demotion: only the backoff limits connects
    let s = setup(vec![d]);
    s.rig.script("S/D").down = true;
    s.workers[0].start();
    tokio::time::sleep(Duration::from_millis(1600)).await;
    s.workers[0].stop().await;
    let at = s.rig.script("S/D").connect_attempts.clone();
    let gaps: Vec<f64> = at.windows(2).map(|w| w[1] - w[0]).collect();
    assert!(gaps.len() >= 4, "{gaps:?}");
    let expected = [0.05, 0.1, 0.2, 0.4, 0.4];
    for (g, e) in gaps.iter().zip(expected) {
        assert!(*g >= e * 0.9 && *g < e + 0.12, "gaps {gaps:?}");
    }
    assert_eq!(s.workers[0].device_status("S/D").unwrap().state, State::Offline);
}

#[tokio::test(flavor = "multi_thread")]
async fn demotion_after_failed_cycles_probe_and_resume() {
    let s = setup(vec![device("S/D", &[("X", "a.0")])]);
    let w = &s.workers[0];
    w.start();
    wait_for("ok", 2.0, || w.device_status("S/D").unwrap().state == State::Ok).await;
    s.rig.script("S/D").timeout_blocks.insert("a".into());
    wait_for("demoted", 3.0, || w.device_status("S/D").unwrap().state == State::Demoted).await;
    let reads_at_demotion = s.rig.script("S/D").reads_of("a");
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert_eq!(s.rig.script("S/D").reads_of("a"), reads_at_demotion, "demoted: nothing sent");
    assert!(!w.device_status("S/D").unwrap().connected, "connection closed on demotion");
    s.rig.script("S/D").timeout_blocks.clear();
    wait_for("resumed", 3.0, || w.device_status("S/D").unwrap().state == State::Ok).await;
    assert!(w.device_status("S/D").unwrap().errors >= 3);
    w.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn probe_stops_at_first_failed_block() {
    let mut d = device("S/D", &[("X", "a.0"), ("Y", "b.0"), ("Z", "c.0")]);
    d.retries = 0;
    d.demote_after = 1;
    d.demote_for_ms = 100;
    let s = setup(vec![d]);
    let w = &s.workers[0];
    {
        let mut sc = s.rig.script("S/D");
        sc.timeout_blocks.extend(["a".to_string(), "b".into(), "c".into()]);
    }
    w.start();
    wait_for("demoted", 2.0, || w.device_status("S/D").unwrap().state == State::Demoted).await;
    let before = {
        let sc = s.rig.script("S/D");
        (sc.reads_of("b"), sc.reads_of("c"))
    };
    tokio::time::sleep(Duration::from_millis(500)).await; // several probes
    {
        let sc = s.rig.script("S/D");
        assert!(sc.reads_of("a") >= 3, "probes ran");
        assert_eq!((sc.reads_of("b"), sc.reads_of("c")), before, "a probe stops at its first failed block");
    }
    w.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn block_config_error_is_never_read_again_and_all_rejected_goes_idle() {
    let s = setup(vec![device("S/D", &[("X", "a.0"), ("Y", "b.0")])]);
    let w = &s.workers[0];
    s.rig.script("S/D").config_error_blocks.insert("a".into());
    w.start();
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(s.rig.script("S/D").reads_of("a"), 1);
    assert!(s.rig.script("S/D").reads_of("b") > 3);
    assert_eq!(s.store.get("S/D", "X").unwrap().quality, Quality::BadConfig);
    assert_eq!(w.device_status("S/D").unwrap().state, State::Ok);
    w.stop().await;

    // every block rejected: neutral cycles (health unchanged), then the device goes idle
    let s = setup(vec![device("S/D", &[("X", "a.0")])]);
    s.rig.script("S/D").config_error_blocks.insert("a".into());
    s.workers[0].start();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(s.rig.script("S/D").reads_of("a"), 1);
    let st = s.workers[0].device_status("S/D").unwrap();
    assert_eq!((st.state, st.errors), (State::Offline, 0));
    s.workers[0].stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn plan_invalid_tags_are_bad_config_from_the_start() {
    let s = setup(vec![device("S/D", &[("X", "a.0"), ("BAD", "!x")])]);
    assert_eq!(s.store.get("S/D", "BAD").unwrap().quality, Quality::BadConfig);
    assert_eq!(s.store.get("S/D", "X").unwrap().quality, Quality::Stale);
}

#[tokio::test(flavor = "multi_thread")]
async fn scaling_applied_and_scaling_error_is_bad_decode() {
    let mut d = device("S/D", &[("S", "a.0"), ("Z", "a.1"), ("L", "a.2")]);
    d.tags[0].scale = Some(Scale { raw_hi: 10.0, eng_hi: 100.0, ..Default::default() });
    d.tags[1].scale = Some(Scale { raw_lo: 5.0, raw_hi: 5.0, ..Default::default() });
    d.tags[2].scale = Some(Scale { raw_hi: 10.0, eng_hi: 100.0, ..Default::default() });
    let s = setup(vec![d]);
    {
        let mut sc = s.rig.script("S/D");
        sc.constant.insert("S".into(), Value::Int(5));
        sc.constant.insert("L".into(), Value::List(vec![Value::Int(1), Value::Bool(true), Value::Str("x".into())]));
    }
    let snap = s.workers[0].read_once("S/D").await.unwrap();
    assert_eq!(snap["S"].value, Some(Value::Float(50.0)));
    assert_eq!(snap["Z"].quality, Quality::BadDecode);
    assert_eq!(snap["L"].value, Some(Value::List(vec![Value::Float(10.0), Value::Bool(true), Value::Str("x".into())])));
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_after_three_times_the_rate() {
    let s = setup(vec![device("S/D", &[("X", "a.0")])]);
    let w = &s.workers[0];
    w.start();
    wait_for("good", 2.0, || s.store.get("S/D", "X").unwrap().quality == Quality::Good).await;
    s.rig.script("S/D").hang = true; // reads stop completing; nothing new arrives
    wait_for("stale", 2.0, || s.store.get("S/D", "X").unwrap().quality == Quality::Stale).await;
    w.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn read_once_without_start_on_disabled_device_and_not_polled_reasons() {
    let mut off = device("S/OFF", &[("X", "a.0")]);
    off.enabled = false;
    let mut other = device("S/LOGIX", &[("X", "a.0")]);
    other.driver = "ab_logix".into();
    let s = setup(vec![off, other, device("S/ON", &[("X", "a.0")])]);
    let w = &s.workers[0];
    assert_eq!(w.device_status("S/OFF").unwrap().last_error.as_deref(), Some("device disabled"));
    assert_eq!(
        w.device_status("S/LOGIX").unwrap().last_error.as_deref(),
        Some("driver 'ab_logix' is not implemented yet")
    );
    let snap = w.read_once("S/OFF").await.unwrap(); // works on a disabled device
    assert_eq!(snap["X"].quality, Quality::Good);
    assert!(w.read_once("S/LOGIX").await.is_err());
    assert!(w.read_once("NOPE/X").await.is_err());

    let s2 = setup_with(vec![device("C/D", &[("X", "a.0")])], false, opts());
    assert_eq!(s2.workers[0].device_status("C/D").unwrap().last_error.as_deref(), Some("channel disabled"));
}

#[tokio::test(flavor = "multi_thread")]
async fn driver_without_io_stops_polling_the_device() {
    let s = setup(vec![device("S/D", &[("X", "a.0")])]);
    s.rig.script("S/D").unsupported = true;
    s.workers[0].start();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(s.rig.script("S/D").connect_attempts.len(), 1);
    let st = s.workers[0].device_status("S/D").unwrap();
    assert_eq!(st.last_error.as_deref(), Some("NotImplementedError: fake: no I/O"));
    s.workers[0].stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stop_closes_drivers_and_on_cycle_panics_do_not_stop_polling() {
    let calls = Arc::new(AtomicUsize::new(0));
    let c = calls.clone();
    let on_cycle = Arc::new(move |_: &str| {
        c.fetch_add(1, Ordering::SeqCst);
        panic!("callback bug");
    });
    let s =
        setup_with(vec![device("S/D", &[("X", "a.0")])], true, WorkerOptions { on_cycle: Some(on_cycle), ..opts() });
    s.workers[0].start();
    // wait on the condition with a generous deadline instead of a fixed sleep (CPU load, #78)
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while calls.load(Ordering::SeqCst) < 3 && std::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(calls.load(Ordering::SeqCst) >= 3, "polling continues after a panicking callback");
    s.workers[0].stop().await;
    {
        let sc = s.rig.script("S/D");
        assert_eq!(sc.closes, sc.connects_ok, "every connection closed on stop");
    }
    assert!(s.workers[0].read_once("S/D").await.is_err(), "stopped worker refuses read_once");
}

/// ADR-0005: the worker is its devices' single writer and publishes one change batch per
/// cycle. A worker created later for the same device (what a config reload does) takes
/// over: the previous worker's cycles no longer reach the store or the bus.
#[tokio::test(flavor = "multi_thread")]
async fn worker_publishes_cycles_and_a_restarted_worker_takes_over() {
    let devs = vec![device("S/D", &[("X", "a.0"), ("Y", "b.0")])];
    let s = setup(devs.clone());
    let mut rx = s.store.subscribe("test-worker");
    let old = &s.workers[0];
    old.read_once("S/D").await.unwrap();
    let batch = match rx.try_recv() {
        Ok(Change::Batch(b)) => b,
        other => panic!("expected a batch, got {other:?}"),
    };
    assert_eq!(batch.device_name(), "S/D");
    assert_eq!(batch.iter().map(|(n, _)| n).collect::<Vec<_>>(), vec!["X", "Y"], "both blocks, one write");
    assert!(rx.try_recv().is_err());

    let factory = s.rig.factory();
    let plans: IndexMap<String, Arc<DevicePlan>> =
        devs.iter().map(|d| (d.id.clone(), Arc::new(plan_device(d, &factory)))).collect();
    let ch = Channel { name: "S".into(), enabled: true, devices: devs };
    let new = ChannelWorker::new(&ch, &plans, s.store.clone(), factory, opts());
    let version = s.store.version("S/D");
    old.read_once("S/D").await.unwrap(); // still talks to the device, but its writer is stale
    assert_eq!(s.store.version("S/D"), version);
    assert!(rx.try_recv().is_err(), "a stale writer publishes nothing");
    new.read_once("S/D").await.unwrap();
    assert!(s.store.version("S/D") > version);
    assert!(matches!(rx.try_recv(), Ok(Change::Batch(_))));
    new.stop().await;
    old.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_batching_driver_reads_the_cycle_with_one_read_many() {
    let mut d = device("S/D", &[("X", "a.0"), ("Y", "b.0"), ("Z", "c.0")]);
    d.retries = 1;
    let s = setup(vec![d]);
    s.rig.script("S/D").batch = true;
    let w = &s.workers[0];
    w.start();
    wait_for("several cycles", 3.0, || s.rig.script("S/D").good_reads.get("c").copied().unwrap_or(0) >= 4).await;
    {
        let sc = s.rig.script("S/D");
        // the first cycle connects through the single-block path; every later one is one batch
        assert!(sc.batch_calls >= 3, "batch calls: {}", sc.batch_calls);
        assert!(
            sc.reads_of("a") <= sc.batch_calls + 1,
            "no block is read twice in a cycle: {} reads, {} batches",
            sc.reads_of("a"),
            sc.batch_calls
        );
    }
    w.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_block_the_batch_did_not_return_good_takes_the_single_block_path_alone() {
    let mut d = device("S/D", &[("X", "a.0"), ("Y", "b.0"), ("Z", "c.0")]);
    d.retries = 1;
    let s = setup(vec![d]);
    {
        let mut sc = s.rig.script("S/D");
        sc.batch = true;
        sc.config_error_blocks.insert("b".to_string());
    }
    let w = &s.workers[0];
    w.start();
    wait_for("a and c good, b rejected", 3.0, || {
        let snap = s.store.snapshot("S/D");
        snap.get("X").is_some_and(|t| t.quality == Quality::Good)
            && snap.get("Z").is_some_and(|t| t.quality == Quality::Good)
            && snap.get("Y").is_some_and(|t| t.quality == Quality::BadConfig)
    })
    .await;
    {
        let sc = s.rig.script("S/D");
        // b is rejected once and never read again; a and c keep coming good
        assert_eq!(sc.reads_of("b"), 1, "b is rejected in the first (single-block) cycle and never read again");
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    {
        let sc = s.rig.script("S/D");
        assert!(sc.good_reads["a"] >= 3 && sc.good_reads["c"] >= 3);
    }
    w.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_block_that_fails_inside_a_batch_is_retried_alone_and_the_rest_stay_good() {
    let mut d = device("S/D", &[("X", "a.0"), ("Y", "b.0"), ("Z", "c.0")]);
    d.retries = 1;
    d.demote_after = 100;
    let s = setup(vec![d]);
    s.rig.script("S/D").batch = true;
    let w = &s.workers[0];
    w.start();
    wait_for("batched cycles", 3.0, || s.rig.script("S/D").batch_calls >= 2).await;
    let (a0, c0) = {
        let mut sc = s.rig.script("S/D");
        sc.timeout_blocks.insert("b".to_string());
        (sc.good_reads["a"], sc.good_reads["c"])
    };
    wait_for("b bad_comm while a and c stay good", 3.0, || {
        let snap = s.store.snapshot("S/D");
        snap.get("Y").is_some_and(|t| t.quality == Quality::BadComm)
            && snap.get("X").is_some_and(|t| t.quality == Quality::Good)
            && snap.get("Z").is_some_and(|t| t.quality == Quality::Good)
    })
    .await;
    // from here on every batched cycle has b timing out. The cycle running now is counted in `bc0`
    // (and may still be reading), so judge only the cycles that started after it and finished
    let (b0, bc0) = {
        let sc = s.rig.script("S/D");
        (sc.reads_of("b"), sc.batch_calls)
    };
    wait_for("two more batched cycles", 3.0, || s.rig.script("S/D").batch_calls >= bc0 + 2).await;
    {
        let sc = s.rig.script("S/D");
        // in every batched cycle b costs its batch read plus at least one single-block attempt; a and c one read
        let finished = sc.batch_calls - bc0 - 1;
        assert!(finished >= 1, "no batched cycle ran");
        assert!(
            sc.reads_of("b") - b0 >= 2 * finished,
            "b was retried alone: {} reads in {finished} finished cycles",
            sc.reads_of("b") - b0
        );
        assert!(sc.good_reads["a"] > a0 && sc.good_reads["c"] > c0);
    }
    w.stop().await;
}
