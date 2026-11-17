//! ChannelWorker: polls every device of one channel (Python `opencap.core.channel_worker`).
//!
//! Concurrency
//! - One worker per channel. Each polled device runs its own rate-group loop (a tokio
//!   task), but every wire operation (connect, read, close) holds the channel's FIFO wire
//!   lock (`tokio::sync::Mutex` is fair), so exactly one request is on the wire at a time
//!   across the whole channel and devices interleave request by request.
//! - Every driver call is bounded by a safety net of `timeout_ms * 1.25 + 0.1 s`
//!   (`x block.wire_requests()` for reads). Channels share nothing.
//! - The per-device cycle lock (`Runner::state`) serializes polling with `read_once()`.
//!   Status queries (API, publisher) read a snapshot and never wait on the wire.
//!
//! Faults (same rules as Python; health rules are in `health.rs`)
//! - CommError / safety-net timeout: retry up to `device.retries`, then the block's tags are
//!   `bad_comm` (last value kept). A safety-net timeout also closes the connection. Before
//!   a retry, if the driver reports its connection closed (close_on_timeout), the worker
//!   reconnects at once, inside the same retry budget and without backoff.
//! - ConnectionLost on a read: close, reconnect at once and repeat the same attempt. A
//!   ConnectionLost right after a fresh connect, a failed connect or a pending backoff ends
//!   the cycle: the remaining blocks are `bad_comm` with no request sent.
//! - Connect failures back off 1 s, 2 s, 4 s ... 60 s; reset on a successful connect.
//! - BlockConfigError: tags `bad_config`, block never read again by this worker.
//!   Plan-invalid tags are set to `bad_config` once, when the device is registered.
//! - NotImplemented / a driver that cannot be built: the device stops being polled.
//! - Demotion closes the connection; the probe runs when the period is over, the group is
//!   due and no reconnect backoff is pending; it stops at its first failed block.

use std::collections::HashMap;
use std::panic::AssertUnwindSafe;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use chrono::{DateTime, Utc};
use futures_util::FutureExt;
use indexmap::IndexMap;
use rand::rngs::StdRng;
use rand::SeedableRng;
use tokio::sync::{Mutex, Notify};
use tokio::task::{AbortHandle, JoinHandle};
use tracing::{debug, info, warn};

use crate::cache::{Snapshot, ValueCache};
use crate::config::{Channel, Device, Scale, Tag};
use crate::driver::{BlockRef, Driver, DriverError, DriverFactory};
use crate::health::{Backoff, DeviceHealth, HealthEvent, HealthStatus, State};
use crate::metrics::{observe_cycle, METRICS};
use crate::registry::DevicePlan;
use crate::scheduler::{DeviceSchedule, START_JITTER};
use crate::value::{mono, now_utc, Quality, TagValue, Value};

pub const SAFETY_FACTOR: f64 = 1.25;
pub const SAFETY_MARGIN_S: f64 = 0.1;
/// A tag with no good read for 3 x its effective rate -> stale.
pub const STALE_FACTOR: f64 = 3.0;
pub const LOOP_ERROR_PAUSE_S: f64 = 1.0;
pub const STATS_INTERVAL_S: f64 = 300.0;

/// Outer bound for one driver call; the driver's own `timeout_ms` should fire first.
pub fn safety_timeout_s(timeout_ms: i64) -> f64 {
    timeout_ms.max(0) as f64 / 1000.0 * SAFETY_FACTOR + SAFETY_MARGIN_S
}

/// Apply `scale` to a number, or element-wise to an array. bool and str pass through.
pub fn scale_value(scale: &Scale, value: &Value) -> Result<Value, String> {
    match value {
        Value::List(items) => Ok(Value::List(items.iter().map(|v| scale_value(scale, v)).collect::<Result<_, _>>()?)),
        v if v.is_number() => Ok(Value::Float(scale.apply(v.as_f64().expect("number"))?)),
        v => Ok(v.clone()),
    }
}

pub type OnCycle = Arc<dyn Fn(&str) + Send + Sync>;
pub type Clock = Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>;

#[derive(Clone)]
pub struct WorkerOptions {
    pub start_jitter: f64,
    pub backoff_base_s: f64,
    pub backoff_max_s: f64,
    pub stats_interval_s: f64,
    pub seed: Option<u64>,
    pub on_cycle: Option<OnCycle>,
    pub clock: Option<Clock>,
}

impl Default for WorkerOptions {
    fn default() -> Self {
        Self {
            start_jitter: START_JITTER,
            backoff_base_s: 1.0,
            backoff_max_s: 60.0,
            stats_interval_s: STATS_INTERVAL_S,
            seed: None,
            on_cycle: None,
            clock: None,
        }
    }
}

/// Python `device_status()`: health status plus cycle_ms, last_error, connected.
#[derive(Debug, Clone, PartialEq)]
pub struct DeviceStatus {
    pub state: State,
    pub last_good: Option<String>,
    pub errors: u64,
    pub cycle_ms: Option<f64>,
    pub last_error: Option<String>,
    pub connected: bool,
}

impl DeviceStatus {
    pub fn health(&self) -> HealthStatus {
        HealthStatus { state: self.state, last_good: self.last_good.clone(), errors: self.errors }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ReadOnceError {
    #[error("device {0} is not in channel {1}")]
    UnknownDevice(String, String),
    #[error("{0}")]
    Unusable(String),
}

/// Cycle-time summary between two `device.stats` log lines.
#[derive(Debug, Default, Clone)]
struct CycleStats {
    cycles: u64,
    total_ms: f64,
    max_ms: f64,
    overruns: u64,
    bad_cycles: u64,
}

impl CycleStats {
    fn add(&mut self, ms: f64, overrun: bool, good: bool) {
        self.cycles += 1;
        self.total_ms += ms;
        self.max_ms = self.max_ms.max(ms);
        self.overruns += overrun as u64;
        self.bad_cycles += (!good) as u64;
    }
}

struct Shared {
    channel: String,
    channel_enabled: bool,
    cache: Arc<ValueCache>,
    factory: DriverFactory,
    wire: Mutex<()>,
    opts: WorkerOptions,
    rng: StdMutex<StdRng>,
}

impl Shared {
    fn clock(&self) -> DateTime<Utc> {
        self.opts.clock.as_ref().map(|c| c()).unwrap_or_else(now_utc)
    }

    fn notify(&self, device_id: &str) {
        if let Some(cb) = &self.opts.on_cycle {
            if std::panic::catch_unwind(AssertUnwindSafe(|| cb(device_id))).is_err() {
                warn!(channel = %self.channel, device = %device_id, "channel.on_cycle_error");
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Ok,
    BadConfig,
    Comm,
    ConnDown,
}

struct BlockResult {
    kind: Kind,
    values: HashMap<String, TagValue>,
    error: Option<String>,
}

impl BlockResult {
    fn err(kind: Kind, error: String) -> Self {
        Self { kind, values: HashMap::new(), error: Some(error) }
    }
}

/// The device cannot be read by this worker at all (driver missing / not implemented).
struct Unsupported(String);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Outcome {
    Good,
    Failed,
    Neutral,
}

struct RunnerState {
    health: DeviceHealth,
    backoff: Backoff,
    driver: Option<Box<dyn Driver>>,
    connected: bool,
    link_up: bool,
    last_error: Option<String>,
    connect_error: Option<String>,
    cycle_ms: Option<f64>,
    groups: Vec<Vec<BlockRef>>,
}

struct Runner {
    id: String,
    device: Device,
    plan: Option<Arc<DevicePlan>>,
    tags: HashMap<String, Tag>,
    max_age_s: HashMap<String, f64>,
    safety_s: f64,
    rates_s: Vec<f64>,
    state: Mutex<RunnerState>,
    status: StdMutex<DeviceStatus>,
    unsupported: StdMutex<Option<String>>,
    stats: StdMutex<CycleStats>,
    registered: AtomicBool,
    wake: Notify,
}

fn lock<T>(m: &StdMutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

impl Runner {
    fn new(w: &Shared, device: Device, plan: Option<Arc<DevicePlan>>) -> Self {
        let tags: HashMap<String, Tag> =
            device.tags.iter().filter(|t| t.enabled).map(|t| (t.name.clone(), t.clone())).collect();
        let max_age_s =
            tags.iter().map(|(n, t)| (n.clone(), STALE_FACTOR * device.effective_rate_ms(t) as f64 / 1000.0)).collect();
        let usable = plan.as_ref().is_some_and(|p| p.ok());
        let groups: Vec<Vec<BlockRef>> = if usable {
            plan.as_ref().unwrap().groups.iter().map(|g| g.plan.blocks.clone()).collect()
        } else {
            Vec::new()
        };
        let rates_s = if usable {
            plan.as_ref().unwrap().groups.iter().map(|g| g.rate_ms as f64 / 1000.0).collect()
        } else {
            Vec::new()
        };
        let state = RunnerState {
            health: DeviceHealth::new(device.demote_after, device.demote_for_ms),
            backoff: Backoff::new(w.opts.backoff_base_s, w.opts.backoff_max_s),
            driver: None,
            connected: false,
            link_up: false,
            last_error: None,
            connect_error: None,
            cycle_ms: None,
            groups,
        };
        let runner = Self {
            id: device.id.clone(),
            safety_s: safety_timeout_s(device.timeout_ms),
            device,
            plan,
            tags,
            max_age_s,
            rates_s,
            status: StdMutex::new(DeviceStatus {
                state: State::Offline,
                last_good: None,
                errors: 0,
                cycle_ms: None,
                last_error: None,
                connected: false,
            }),
            unsupported: StdMutex::new(None),
            stats: StdMutex::new(CycleStats::default()),
            registered: AtomicBool::new(false),
            wake: Notify::new(),
            state: Mutex::new(state),
        };
        // initial snapshot (not_polled_reason for devices that never run)
        {
            let st = runner.state.try_lock().expect("fresh mutex");
            runner.refresh_status(&st, w);
        }
        runner
    }

    // --- static facts -------------------------------------------------------------

    fn broken(&self) -> Option<String> {
        match &self.plan {
            None => Some("no block plan for this device".into()),
            Some(p) if p.error.is_some() => p.error.clone(),
            _ => lock(&self.unsupported).clone(),
        }
    }

    fn not_polled_reason(&self, st: &RunnerState, w: &Shared) -> Option<String> {
        if !w.channel_enabled {
            return Some("channel disabled".into());
        }
        if !self.device.enabled {
            return Some("device disabled".into());
        }
        if let Some(b) = self.broken() {
            return Some(b);
        }
        if st.groups.iter().all(|g| g.is_empty()) {
            return Some("no readable blocks".into());
        }
        None
    }

    fn refresh_status(&self, st: &RunnerState, w: &Shared) {
        let hs = st.health.status();
        let snap = DeviceStatus {
            state: hs.state,
            last_good: hs.last_good,
            errors: hs.errors,
            cycle_ms: st.cycle_ms,
            last_error: st.last_error.clone().or_else(|| self.not_polled_reason(st, w)),
            connected: st.connected && st.driver.as_ref().is_some_and(|d| d.connected()),
        };
        *lock(&self.status) = snap;
    }

    fn read_budget(&self, block: &BlockRef) -> Duration {
        Duration::from_secs_f64(self.safety_s * block.wire_requests().max(1) as f64)
    }

    fn ensure_registered(&self, w: &Shared) {
        if self.registered.swap(true, Ordering::SeqCst) {
            return;
        }
        w.cache.register(&self.device);
        let invalid = self.plan.as_ref().filter(|p| p.ok()).map(|p| p.invalid()).unwrap_or_default();
        if !invalid.is_empty() {
            let values: IndexMap<String, TagValue> =
                invalid.keys().map(|n| (n.clone(), TagValue::bad(Quality::BadConfig, None))).collect();
            w.cache.update(&self.id, &values);
            let first: Vec<&String> = invalid.keys().take(20).collect();
            info!(channel = %w.channel, device = %self.id, count = invalid.len(), tags = ?first, "device.invalid_tags");
        }
    }

    fn disable_block(st: &mut RunnerState, block: &BlockRef) {
        for blocks in st.groups.iter_mut() {
            blocks.retain(|b| !Arc::ptr_eq(b, block));
        }
    }

    // --- connection ---------------------------------------------------------------

    fn ensure_driver<'a>(&self, st: &'a mut RunnerState, w: &Shared) -> Result<&'a mut Box<dyn Driver>, Unsupported> {
        if st.driver.is_none() {
            let drv = (w.factory)(&self.device).map_err(|e| Unsupported(format!("cannot create driver: {e}")))?;
            st.driver = Some(drv);
        }
        Ok(st.driver.as_mut().expect("driver just set"))
    }

    /// One connect attempt. Ok(None) when connected, Ok(Some(reason)) when not.
    async fn connect(&self, st: &mut RunnerState, w: &Shared, force: bool) -> Result<Option<String>, Unsupported> {
        let now = mono();
        if !force && st.backoff.pending(now) {
            return Ok(Some(format!(
                "{} (next attempt in {:.1} s)",
                st.connect_error.as_deref().unwrap_or("connect failed"),
                st.backoff.remaining(now)
            )));
        }
        let safety = self.safety_s;
        let drv = self.ensure_driver(st, w)?;
        let budget = drv.connect_timeout().unwrap_or(Duration::from_secs_f64(safety));
        let t0 = mono();
        let res = {
            let _wire = w.wire.lock().await;
            tokio::time::timeout(budget, drv.connect()).await
        };
        let reason = match res {
            Ok(Ok(())) => None,
            Ok(Err(DriverError::NotImplemented(m))) => {
                return Err(Unsupported(DriverError::NotImplemented(m).describe()));
            }
            Ok(Err(e)) => Some(e.describe()),
            Err(_) => Some("no answer within the safety timeout".to_string()),
        };
        let ms = round1((mono() - t0) * 1000.0);
        if let Some(reason) = reason {
            let err = format!("connect failed: {reason}");
            st.connect_error = Some(err.clone());
            METRICS.poll_requests.with_label_values(&[&self.id, "conn_error"]).inc();
            let delay = st.backoff.failure(mono());
            self.close_driver(st, w).await;
            debug!(channel = %w.channel, device = %self.id, error = %reason, retry_in_s = delay, ms, "device.connect_failed");
            if st.link_up {
                st.link_up = false;
                info!(channel = %w.channel, device = %self.id, error = %reason, "device.disconnected");
            }
            self.refresh_status(st, w);
            return Ok(Some(err));
        }
        st.connected = true;
        st.connect_error = None;
        st.backoff.reset();
        if !st.link_up {
            st.link_up = true;
            info!(channel = %w.channel, device = %self.id, host = %self.device.host, port = self.device.port, ms, "device.connected");
        } else {
            debug!(channel = %w.channel, device = %self.id, ms, "device.reconnected");
        }
        self.refresh_status(st, w);
        Ok(None)
    }

    async fn drop_conn(&self, st: &mut RunnerState, w: &Shared, reason: &str) {
        if st.connected {
            debug!(channel = %w.channel, device = %self.id, reason, "device.connection_dropped");
        }
        st.connected = false;
        self.close_driver(st, w).await;
    }

    async fn close_driver(&self, st: &mut RunnerState, w: &Shared) {
        let Some(drv) = st.driver.as_mut() else { return };
        let _wire = w.wire.lock().await;
        if tokio::time::timeout(Duration::from_secs_f64(self.safety_s), drv.close()).await.is_err() {
            debug!(channel = %w.channel, device = %self.id, "device.close_failed");
        }
    }

    // --- one block ----------------------------------------------------------------

    fn count(&self, w: &Shared, result: &str, block: &BlockRef, t0: f64, attempt: usize, error: Option<&str>) {
        METRICS.poll_requests.with_label_values(&[&self.id, result]).inc();
        debug!(channel = %w.channel, device = %self.id, block = %block.describe(), result, attempt = attempt + 1,
               ms = round1((mono() - t0) * 1000.0), error = error.unwrap_or(""), "device.request");
    }

    async fn read_block(
        &self,
        st: &mut RunnerState,
        w: &Shared,
        block: &BlockRef,
        mut force_connect: bool,
    ) -> Result<BlockResult, Unsupported> {
        let attempts = (self.device.retries.max(0) + 1) as usize;
        let mut attempt = 0usize;
        let mut fresh = false;
        let mut last_error = "no attempt made".to_string();
        while attempt < attempts {
            if let Some(drv) = st.driver.as_ref() {
                if st.connected && !drv.connected() && (attempt > 0 || drv.reports_connection()) {
                    // The driver closed its socket (close_on_timeout, or the peer closed it).
                    // Reopen at once: no retry used, no backoff.
                    st.connected = false;
                    debug!(channel = %w.channel, device = %self.id, block = %block.describe(), attempt = attempt + 1, "device.reopen");
                }
            }
            if !st.connected {
                let err = self.connect(st, w, force_connect).await?;
                force_connect = false;
                if let Some(err) = err {
                    return Ok(BlockResult::err(Kind::ConnDown, err));
                }
                fresh = true;
            }
            let budget = self.read_budget(block);
            let t0 = mono();
            let res = {
                let drv = st.driver.as_mut().expect("connected implies a driver");
                let _wire = w.wire.lock().await;
                tokio::time::timeout(budget, drv.read(block.as_ref())).await
            };
            match res {
                Ok(Ok(values)) => {
                    self.count(w, "ok", block, t0, attempt, None);
                    return Ok(BlockResult { kind: Kind::Ok, values, error: None });
                }
                Ok(Err(e @ DriverError::BlockConfig(_))) => {
                    let text = e.to_string();
                    self.count(w, "bad_config", block, t0, attempt, Some(&text));
                    return Ok(BlockResult::err(
                        Kind::BadConfig,
                        format!("{}: rejected: {}", block.describe(), e.describe()),
                    ));
                }
                Ok(Err(e @ DriverError::ConnectionLost(_))) => {
                    self.count(w, "conn_error", block, t0, attempt, Some(&e.to_string()));
                    last_error = format!("connection lost: {}", e.describe());
                    self.drop_conn(st, w, &last_error).await;
                    if fresh {
                        return Ok(BlockResult::err(Kind::ConnDown, last_error));
                    }
                    continue; // the old connection was dead: reconnect, same attempt
                }
                Ok(Err(e @ DriverError::Comm(_))) => {
                    self.count(w, "timeout", block, t0, attempt, Some(&e.to_string()));
                    last_error = format!("{}: {}", block.describe(), e.describe());
                }
                Ok(Err(e @ DriverError::NotImplemented(_))) => return Err(Unsupported(e.describe())),
                Ok(Err(e @ DriverError::Other(_))) => {
                    self.count(w, "error", block, t0, attempt, Some(&e.describe()));
                    last_error = format!("{}: {}", block.describe(), e.describe());
                }
                Err(_) => {
                    self.count(w, "timeout", block, t0, attempt, Some("safety-net timeout"));
                    last_error = format!("{}: no answer within the safety timeout", block.describe());
                    self.drop_conn(st, w, &last_error).await;
                }
            }
            attempt += 1;
            fresh = false;
        }
        Ok(BlockResult::err(Kind::Comm, last_error))
    }

    /// Driver result -> cache values: scaling, missing tags, default source_ts.
    fn postprocess(
        &self,
        w: &Shared,
        block: &BlockRef,
        result: &HashMap<String, TagValue>,
    ) -> IndexMap<String, TagValue> {
        let mut out = IndexMap::new();
        let mut fallback_ts: Option<DateTime<Utc>> = None;
        for name in block.tags() {
            let Some(tv) = result.get(name) else {
                out.insert(name.clone(), TagValue::bad(Quality::BadDecode, None));
                continue;
            };
            let Some(value) = tv.value.as_ref().filter(|_| tv.quality == Quality::Good) else {
                let quality = if tv.quality == Quality::Good { Quality::BadDecode } else { tv.quality };
                out.insert(name.clone(), TagValue::bad(quality, tv.source_ts));
                continue;
            };
            let mut value = value.clone();
            if let Some(scale) = self.tags.get(name).and_then(|t| t.scale.as_ref()) {
                match scale_value(scale, &value) {
                    Ok(v) => value = v,
                    Err(e) => {
                        debug!(channel = %w.channel, device = %self.id, tag = %name, error = %e, "device.scale_error");
                        out.insert(name.clone(), TagValue::bad(Quality::BadDecode, tv.source_ts));
                        continue;
                    }
                }
            }
            let ts = tv.source_ts.unwrap_or_else(|| *fallback_ts.get_or_insert_with(|| w.clock()));
            out.insert(name.clone(), TagValue::good(value, ts));
        }
        out
    }

    // --- one cycle ----------------------------------------------------------------

    async fn cycle(
        &self,
        st: &mut RunnerState,
        w: &Shared,
        blocks: &[BlockRef],
        probe: bool,
        mut force_connect: bool,
    ) -> Result<Outcome, Unsupported> {
        let mut values: IndexMap<String, TagValue> = IndexMap::new();
        let mut any_ok = false;
        let mut comm_failed = false;
        let mut errors: Vec<String> = Vec::new();
        for (i, block) in blocks.iter().enumerate() {
            let res = self.read_block(st, w, block, force_connect).await?;
            force_connect = false;
            match res.kind {
                Kind::Ok => {
                    any_ok = true;
                    values.extend(self.postprocess(w, block, &res.values));
                }
                Kind::BadConfig => {
                    Self::disable_block(st, block);
                    let error = res.error.unwrap_or_else(|| "block rejected".into());
                    warn!(channel = %w.channel, device = %self.id, block = %block.describe(), error = %error, "device.block_rejected");
                    errors.push(error);
                    for name in block.tags() {
                        values.insert(name.clone(), TagValue::bad(Quality::BadConfig, None));
                    }
                }
                Kind::Comm | Kind::ConnDown => {
                    comm_failed = true;
                    errors.push(res.error.unwrap_or_else(|| "communication failed".into()));
                    let abort = res.kind == Kind::ConnDown || (probe && !any_ok);
                    let failed: &[BlockRef] = if abort { &blocks[i..] } else { std::slice::from_ref(block) };
                    for b in failed {
                        for name in b.tags() {
                            values.insert(name.clone(), TagValue::bad(Quality::BadComm, None));
                        }
                    }
                    if abort {
                        break;
                    }
                }
            }
        }
        if !values.is_empty() {
            w.cache.update(&self.id, &values);
        }
        let now = mono();
        let (outcome, event) = if any_ok {
            (Outcome::Good, st.health.record_good(now, w.clock()))
        } else if comm_failed {
            (Outcome::Failed, st.health.record_failure(now))
        } else {
            (Outcome::Neutral, None)
        };
        if let Some(last) = errors.last() {
            st.last_error = Some(last.clone());
        } else if any_ok {
            st.last_error = None;
        }
        self.on_event(st, w, event).await;
        self.refresh_status(st, w);
        Ok(outcome)
    }

    async fn on_event(&self, st: &mut RunnerState, w: &Shared, event: Option<HealthEvent>) {
        let Some(event) = event else { return };
        METRICS.device_state.with_label_values(&[&self.id]).set(st.health.state.code());
        let err = st.last_error.clone().unwrap_or_default();
        match event {
            HealthEvent::Demoted => info!(channel = %w.channel, device = %self.id,
                after_failed_cycles = st.health.consecutive_failures, for_ms = self.device.demote_for_ms,
                error = %err, "device.demoted"),
            HealthEvent::Redemoted => debug!(channel = %w.channel, device = %self.id,
                for_ms = self.device.demote_for_ms, error = %err, "device.probe_failed"),
            HealthEvent::Resumed => info!(channel = %w.channel, device = %self.id, "device.resumed"),
            HealthEvent::Ok => info!(channel = %w.channel, device = %self.id, "device.ok"),
            HealthEvent::Offline => info!(channel = %w.channel, device = %self.id, error = %err, "device.offline"),
        }
        if matches!(event, HealthEvent::Demoted | HealthEvent::Redemoted) {
            self.drop_conn(st, w, "demoted").await;
        }
        self.wake.notify_one();
    }

    fn mark_unsupported(&self, st: &mut RunnerState, w: &Shared, reason: String) {
        *lock(&self.unsupported) = Some(reason.clone());
        st.last_error = Some(reason.clone());
        st.connected = false;
        warn!(channel = %w.channel, device = %self.id, error = %reason, "device.unsupported");
        self.refresh_status(st, w);
    }

    // --- loops --------------------------------------------------------------------

    async fn sleep(&self, delay: f64) {
        tokio::select! {
            _ = self.wake.notified() => {}
            _ = tokio::time::sleep(Duration::from_secs_f64(delay.max(0.0))) => {}
        }
    }

    async fn poll_loop(self: Arc<Self>, w: Arc<Shared>) {
        let rates: Vec<(usize, f64)> = {
            let st = self.state.lock().await;
            self.rates_s
                .iter()
                .enumerate()
                .filter(|(i, _)| st.groups.get(*i).is_some_and(|g| !g.is_empty()))
                .map(|(i, r)| (i, *r))
                .collect()
        };
        let mut sched = {
            let mut rng = lock(&w.rng);
            DeviceSchedule::new(&rates, mono(), w.opts.start_jitter, &mut *rng)
        };
        while !sched.is_empty() {
            let step = AssertUnwindSafe(self.poll_step(&w, &mut sched)).catch_unwind().await;
            match step {
                Ok(Ok(())) => {}
                Ok(Err(Unsupported(reason))) => {
                    let mut st = self.state.lock().await;
                    self.mark_unsupported(&mut st, &w, reason);
                    return;
                }
                Err(panic) => {
                    let text = panic
                        .downcast_ref::<String>()
                        .cloned()
                        .or_else(|| panic.downcast_ref::<&str>().map(|s| s.to_string()))
                        .unwrap_or_else(|| "panic".into());
                    {
                        let mut st = self.state.lock().await;
                        st.last_error = Some(format!("internal error: {text}"));
                        self.refresh_status(&st, &w);
                    }
                    tracing::error!(channel = %w.channel, device = %self.id, error = %text, "device.loop_error");
                    tokio::time::sleep(Duration::from_secs_f64(LOOP_ERROR_PAUSE_S)).await;
                }
            }
        }
        warn!(channel = %w.channel, device = %self.id, reason = "no readable blocks left", "device.idle");
    }

    async fn poll_step(&self, w: &Shared, sched: &mut DeviceSchedule) -> Result<(), Unsupported> {
        let now = mono();
        let slot = sched.due().expect("schedule not empty");
        let mut wait = slot.next_due - now;
        {
            let st = self.state.lock().await;
            if st.health.state == State::Demoted {
                let backoff = if st.connected { 0.0 } else { st.backoff.remaining(now) };
                wait = wait.max(st.health.demoted_remaining(now)).max(backoff);
            }
        }
        if wait > 0.0 {
            self.sleep(wait).await;
            return Ok(());
        }
        let mut st = self.state.lock().await;
        if st.health.state == State::Demoted && !st.health.probe_due(mono()) {
            return Ok(()); // a read_once() demoted the device while we waited
        }
        let probe = st.health.state == State::Demoted;
        let blocks = st.groups[slot.index].clone();
        let started = mono();
        self.cycle(&mut st, w, &blocks, probe, false).await?;
        let ended = mono();
        let cycle_ms = round1((ended - started) * 1000.0);
        st.cycle_ms = Some(cycle_ms);
        observe_cycle(&self.id, ended - started);
        let overrun = sched.complete(slot.index, started, ended);
        lock(&self.stats).add(cycle_ms, overrun, st.health.state == State::Ok);
        if overrun {
            METRICS.poll_overrun.with_label_values(&[&self.id]).inc();
            debug!(channel = %w.channel, device = %self.id, rate_ms = (slot.rate_s * 1000.0).round(), cycle_ms, "device.overrun");
        }
        if st.groups[slot.index].is_empty() {
            sched.remove(slot.index);
        }
        self.refresh_status(&st, w);
        drop(st);
        w.notify(&self.id);
        Ok(())
    }

    async fn read_once(&self, w: &Shared) -> Result<Snapshot, ReadOnceError> {
        self.ensure_registered(w);
        {
            let mut st = self.state.lock().await;
            let blocks: Vec<BlockRef> = st.groups.iter().flatten().cloned().collect();
            if let Err(Unsupported(reason)) = self.cycle(&mut st, w, &blocks, false, true).await {
                self.mark_unsupported(&mut st, w, reason.clone());
                return Err(ReadOnceError::Unusable(format!("{}: {reason}", self.id)));
            }
        }
        self.wake.notify_one();
        w.notify(&self.id);
        Ok(w.cache.snapshot(&self.id))
    }

    async fn close_for_stop(&self, w: &Shared) {
        let mut st = self.state.lock().await;
        st.connected = false;
        self.close_driver(&mut st, w).await;
        self.refresh_status(&st, w);
    }
}

/// Polls the devices of one channel. See the module docs for the rules.
pub struct ChannelWorker {
    shared: Arc<Shared>,
    runners: IndexMap<String, Arc<Runner>>,
    tasks: StdMutex<Vec<JoinHandle<()>>>,
    oneshots: StdMutex<Vec<AbortHandle>>,
    stopped: AtomicBool,
    started: AtomicBool,
}

impl ChannelWorker {
    pub fn new(
        channel: &Channel,
        plans: &IndexMap<String, Arc<DevicePlan>>,
        cache: Arc<ValueCache>,
        factory: DriverFactory,
        opts: WorkerOptions,
    ) -> Arc<Self> {
        let rng = match opts.seed {
            Some(seed) => StdRng::seed_from_u64(seed),
            None => StdRng::from_os_rng(),
        };
        let shared = Arc::new(Shared {
            channel: channel.name.clone(),
            channel_enabled: channel.enabled,
            cache,
            factory,
            wire: Mutex::new(()),
            opts,
            rng: StdMutex::new(rng),
        });
        let runners: IndexMap<String, Arc<Runner>> = channel
            .devices
            .iter()
            .map(|d| (d.id.clone(), Arc::new(Runner::new(&shared, d.clone(), plans.get(&d.id).cloned()))))
            .collect();
        if channel.enabled {
            for r in runners.values().filter(|r| r.device.enabled) {
                r.ensure_registered(&shared);
            }
        }
        Arc::new(Self {
            shared,
            runners,
            tasks: StdMutex::new(Vec::new()),
            oneshots: StdMutex::new(Vec::new()),
            stopped: AtomicBool::new(false),
            started: AtomicBool::new(false),
        })
    }

    pub fn channel(&self) -> &str {
        &self.shared.channel
    }

    pub fn device_ids(&self) -> Vec<String> {
        self.runners.keys().cloned().collect()
    }

    /// Start polling (one task per polled device, plus the stale/stats housekeeping task).
    pub fn start(self: &Arc<Self>) {
        if self.stopped.load(Ordering::SeqCst) || self.started.swap(true, Ordering::SeqCst) {
            return;
        }
        let w = &self.shared;
        let mut polled = Vec::new();
        for r in self.runners.values() {
            // The state is only locked by a read_once() running before start(); its blocks are
            // still the planned ones then, which is all not_polled_reason() needs.
            let reason = match r.state.try_lock() {
                Ok(st) => r.not_polled_reason(&st, w),
                Err(_) => lock(&r.status).last_error.clone().filter(|_| r.broken().is_some()),
            };
            match reason {
                Some(reason) => info!(channel = %w.channel, device = %r.id, reason = %reason, "device.not_polled"),
                None => polled.push(r.clone()),
            }
        }
        let mut tasks = lock(&self.tasks);
        for r in &polled {
            METRICS.device_state.with_label_values(&[&r.id]).set(State::Offline.code());
            tasks.push(tokio::spawn(r.clone().poll_loop(w.clone())));
        }
        if !polled.is_empty() {
            tasks.push(tokio::spawn(housekeeping(w.clone(), polled.clone())));
        }
        info!(channel = %w.channel, devices = polled.len(), "channel.started");
    }

    /// Stop polling and any `read_once()`, close every driver.
    pub async fn stop(&self) {
        if self.stopped.swap(true, Ordering::SeqCst) {
            return;
        }
        let tasks: Vec<JoinHandle<()>> = std::mem::take(&mut *lock(&self.tasks));
        for t in &tasks {
            t.abort();
        }
        for t in tasks {
            let _ = t.await;
        }
        let oneshots: Vec<AbortHandle> = std::mem::take(&mut *lock(&self.oneshots));
        for o in &oneshots {
            o.abort();
        }
        for r in self.runners.values() {
            r.close_for_stop(&self.shared).await;
        }
        info!(channel = %self.shared.channel, "channel.closed");
    }

    /// One full cycle (every rate group) of the device now; returns its cache snapshot.
    /// Works on disabled devices and without `start()` (CLI), ignores demotion, makes one
    /// connect attempt even during backoff, and feeds health like any cycle.
    pub async fn read_once(&self, device_id: &str) -> Result<Snapshot, ReadOnceError> {
        if self.stopped.load(Ordering::SeqCst) {
            return Err(ReadOnceError::Unusable("channel worker stopped".into()));
        }
        let runner = self
            .runners
            .get(device_id)
            .cloned()
            .ok_or_else(|| ReadOnceError::UnknownDevice(device_id.into(), self.shared.channel.clone()))?;
        if let Some(reason) = runner.broken() {
            return Err(ReadOnceError::Unusable(format!("{device_id}: {reason}")));
        }
        let w = self.shared.clone();
        let handle = tokio::spawn(async move { runner.read_once(&w).await });
        {
            let mut oneshots = lock(&self.oneshots);
            oneshots.retain(|h| !h.is_finished());
            oneshots.push(handle.abort_handle());
        }
        match handle.await {
            Ok(result) => result,
            Err(_) => Err(ReadOnceError::Unusable("channel worker stopped".into())),
        }
    }

    pub fn has_device(&self, device_id: &str) -> bool {
        self.runners.contains_key(device_id)
    }

    /// Health status snapshot (never waits on the wire).
    pub fn health(&self, device_id: &str) -> Option<HealthStatus> {
        self.device_status(device_id).map(|s| s.health())
    }

    pub fn device_status(&self, device_id: &str) -> Option<DeviceStatus> {
        self.runners.get(device_id).map(|r| lock(&r.status).clone())
    }
}

/// Mark good tags stale when their group has not been read for 3 x its rate, and log the
/// periodic `device.stats` line.
async fn housekeeping(w: Arc<Shared>, runners: Vec<Arc<Runner>>) {
    let fastest = runners.iter().flat_map(|r| r.rates_s.iter().copied()).fold(f64::INFINITY, f64::min);
    let fastest = if fastest.is_finite() { fastest } else { 1.0 };
    let tick = (fastest / 2.0).clamp(0.02, 1.0);
    let mut next_stats = mono() + w.opts.stats_interval_s;
    loop {
        tokio::time::sleep(Duration::from_secs_f64(tick)).await;
        let now = w.clock();
        for r in &runners {
            w.cache.expire(&r.id, now, &r.max_age_s);
        }
        if w.opts.stats_interval_s > 0.0 && mono() >= next_stats {
            next_stats = mono() + w.opts.stats_interval_s;
            for r in &runners {
                let s = std::mem::take(&mut *lock(&r.stats));
                if s.cycles == 0 {
                    continue;
                }
                let state = lock(&r.status).state.as_str();
                info!(channel = %w.channel, device = %r.id, state, period_s = w.opts.stats_interval_s.round(),
                      cycles = s.cycles, mean_cycle_ms = round1(s.total_ms / s.cycles as f64),
                      max_cycle_ms = round1(s.max_ms), overruns = s.overruns, bad_cycles = s.bad_cycles,
                      "device.stats");
            }
        }
    }
}
