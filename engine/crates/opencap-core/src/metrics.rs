//! Prometheus metrics: the same names, labels and buckets as `src/opencap/metrics.py`,
//! in the classic text exposition format (`text/plain; version=0.0.4`).

use std::collections::HashMap;
use std::sync::{Arc, LazyLock, RwLock};

use prometheus::{
    Encoder, Histogram, HistogramOpts, HistogramVec, IntCounter, IntCounterVec, IntGauge, IntGaugeVec, Opts, Registry,
    TextEncoder,
};

pub struct Metrics {
    pub registry: Registry,
    /// result: ok | timeout | conn_error | bad_config | error
    pub poll_requests: IntCounterVec,
    pub poll_cycle: HistogramVec,
    pub poll_overrun: IntCounterVec,
    /// Block reads that ended `bad_comm` after all retries (system tag `_FailedReads`).
    pub poll_failed_reads: IntCounterVec,
    /// 0 = ok, 1 = offline, 2 = demoted
    pub device_state: IntGaugeVec,
    pub mqtt_published: IntCounter,
    pub saf_queue: IntGauge,
    pub saf_dropped: IntCounter,
    /// Device loops restarted after a panic (03 section 3.4).
    pub driver_panics: IntCounterVec,
    /// Change batches a north agent skipped because it lagged behind the bus (ADR-0005).
    pub north_lagged: IntCounterVec,
}

pub static METRICS: LazyLock<Metrics> = LazyLock::new(|| {
    let registry = Registry::new();
    let poll_requests =
        IntCounterVec::new(Opts::new("poll_requests_total", "Wire requests to PLCs by result"), &["device", "result"])
            .expect("metric");
    let poll_cycle = HistogramVec::new(
        HistogramOpts::new("poll_cycle_seconds", "Duration of one rate-group cycle of a device")
            .buckets(vec![0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]),
        &["device"],
    )
    .expect("metric");
    let poll_overrun = IntCounterVec::new(
        Opts::new("poll_overrun_total", "Cycles that took longer than their scan rate"),
        &["device"],
    )
    .expect("metric");
    let poll_failed_reads = IntCounterVec::new(
        Opts::new("poll_failed_reads_total", "Block reads that ended bad_comm after all retries"),
        &["device"],
    )
    .expect("metric");
    let device_state =
        IntGaugeVec::new(Opts::new("device_state", "Device state: 0 = ok, 1 = offline, 2 = demoted"), &["device"])
            .expect("metric");
    let mqtt_published =
        IntCounter::new("mqtt_published_total", "Messages acknowledged by the broker").expect("metric");
    let saf_queue = IntGauge::new("saf_queue_messages", "Messages waiting in store-and-forward").expect("metric");
    let saf_dropped =
        IntCounter::new("saf_dropped_total", "Messages dropped from store-and-forward because of the size cap")
            .expect("metric");
    let driver_panics = IntCounterVec::new(
        Opts::new("driver_panics_total", "Device loops restarted after an internal error (panic)"),
        &["device"],
    )
    .expect("metric");
    let north_lagged = IntCounterVec::new(
        Opts::new(
            "north_lagged_total",
            "Change batches a north agent skipped because it fell behind (resynced from the store)",
        ),
        &["agent"],
    )
    .expect("metric");
    registry.register(Box::new(driver_panics.clone())).expect("register");
    registry.register(Box::new(north_lagged.clone())).expect("register");
    registry.register(Box::new(poll_requests.clone())).expect("register");
    registry.register(Box::new(poll_cycle.clone())).expect("register");
    registry.register(Box::new(poll_overrun.clone())).expect("register");
    registry.register(Box::new(poll_failed_reads.clone())).expect("register");
    registry.register(Box::new(device_state.clone())).expect("register");
    registry.register(Box::new(mqtt_published.clone())).expect("register");
    registry.register(Box::new(saf_queue.clone())).expect("register");
    registry.register(Box::new(saf_dropped.clone())).expect("register");
    Metrics {
        registry,
        poll_requests,
        poll_cycle,
        poll_overrun,
        poll_failed_reads,
        device_state,
        mqtt_published,
        saf_queue,
        saf_dropped,
        driver_panics,
        north_lagged,
    }
});

/// The per-device counters behind `/metrics` and the system tags (spec section 8.3, D-095).
///
/// Each field is the device's own series of the Prometheus counter, resolved once: the series
/// is itself an atomic, so a system-tag recompute reads `get()` on a handful of atomics and
/// never gathers or walks the registry. Counters live as long as the process (a reload keeps
/// them), like `/metrics`.
pub struct DeviceCounts {
    pub ok: IntCounter,
    pub timeout: IntCounter,
    pub conn_error: IntCounter,
    pub bad_config: IntCounter,
    pub error: IntCounter,
    pub failed_reads: IntCounter,
    pub overruns: IntCounter,
}

impl DeviceCounts {
    fn new(device: &str) -> Self {
        let req = |result: &str| METRICS.poll_requests.with_label_values(&[device, result]);
        Self {
            ok: req("ok"),
            timeout: req("timeout"),
            conn_error: req("conn_error"),
            bad_config: req("bad_config"),
            error: req("error"),
            failed_reads: METRICS.poll_failed_reads.with_label_values(&[device]),
            overruns: METRICS.poll_overrun.with_label_values(&[device]),
        }
    }

    /// One wire request with `result` (ok | timeout | conn_error | bad_config | error).
    pub fn request(&self, result: &str) {
        match result {
            "ok" => &self.ok,
            "timeout" => &self.timeout,
            "conn_error" => &self.conn_error,
            "bad_config" => &self.bad_config,
            _ => &self.error,
        }
        .inc();
    }

    /// The current totals, in the shape the system-tag state takes (bytes are not counted yet, #130).
    pub fn snapshot(&self) -> crate::system_tags::Counters {
        crate::system_tags::Counters {
            ok: self.ok.get(),
            timeout: self.timeout.get(),
            conn_error: self.conn_error.get(),
            bad_config: self.bad_config.get(),
            error: self.error.get(),
            failed_reads: self.failed_reads.get(),
            overruns: self.overruns.get(),
            rx_bytes: 0,
            tx_bytes: 0,
        }
    }
}

static COUNTS: LazyLock<RwLock<HashMap<String, Arc<DeviceCounts>>>> = LazyLock::new(Default::default);

/// The counters of one device (`Plant A/PLC1`), created on first use.
pub fn device_counts(device: &str) -> Arc<DeviceCounts> {
    if let Some(c) = COUNTS.read().unwrap_or_else(|e| e.into_inner()).get(device) {
        return c.clone();
    }
    let mut map = COUNTS.write().unwrap_or_else(|e| e.into_inner());
    map.entry(device.to_string()).or_insert_with(|| Arc::new(DeviceCounts::new(device))).clone()
}

pub fn observe_cycle(device: &str, seconds: f64) {
    let h: Histogram = METRICS.poll_cycle.with_label_values(&[device]);
    h.observe(seconds);
}

/// Text exposition of every metric.
pub fn render() -> String {
    let mut buf = Vec::new();
    TextEncoder::new().encode(&METRICS.registry.gather(), &mut buf).expect("text encoding cannot fail");
    String::from_utf8(buf).expect("prometheus text is UTF-8")
}

pub const CONTENT_TYPE: &str = "text/plain; version=0.0.4";

#[cfg(test)]
mod tests {
    #[test]
    fn device_counts_are_the_metric_series() {
        let c = super::device_counts("S/Counted");
        c.request("ok");
        c.request("ok");
        c.request("timeout");
        c.request("weird");
        c.failed_reads.inc();
        let snap = c.snapshot();
        assert_eq!((snap.ok, snap.timeout, snap.error, snap.failed_reads), (2, 1, 1, 1));
        assert_eq!(super::METRICS.poll_requests.with_label_values(&["S/Counted", "ok"]).get(), 2);
        assert!(std::sync::Arc::ptr_eq(&c, &super::device_counts("S/Counted")));
        assert!(super::render().contains("poll_failed_reads_total{device=\"S/Counted\"} 1"));
    }

    #[test]
    fn names_match_python() {
        super::METRICS.poll_requests.with_label_values(&["S/D", "ok"]).inc();
        super::METRICS.mqtt_published.inc();
        super::METRICS.driver_panics.with_label_values(&["S/D"]).inc();
        super::METRICS.north_lagged.with_label_values(&["mqtt"]).inc_by(0);
        let text = super::render();
        for name in [
            "poll_requests_total",
            "driver_panics_total",
            "north_lagged_total",
            "mqtt_published_total",
            "saf_queue_messages",
            "saf_dropped_total",
        ] {
            assert!(text.contains(name), "{name} missing in\n{text}");
        }
    }
}
