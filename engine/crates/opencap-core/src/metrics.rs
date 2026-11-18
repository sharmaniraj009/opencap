//! Prometheus metrics: the same names, labels and buckets as `src/opencap/metrics.py`,
//! in the classic text exposition format (`text/plain; version=0.0.4`).

use std::sync::LazyLock;

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
    /// 0 = ok, 1 = offline, 2 = demoted
    pub device_state: IntGaugeVec,
    pub mqtt_published: IntCounter,
    pub saf_queue: IntGauge,
    pub saf_dropped: IntCounter,
    /// Device loops restarted after a panic (03 section 3.4).
    pub driver_panics: IntCounterVec,
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
    registry.register(Box::new(driver_panics.clone())).expect("register");
    registry.register(Box::new(poll_requests.clone())).expect("register");
    registry.register(Box::new(poll_cycle.clone())).expect("register");
    registry.register(Box::new(poll_overrun.clone())).expect("register");
    registry.register(Box::new(device_state.clone())).expect("register");
    registry.register(Box::new(mqtt_published.clone())).expect("register");
    registry.register(Box::new(saf_queue.clone())).expect("register");
    registry.register(Box::new(saf_dropped.clone())).expect("register");
    Metrics {
        registry,
        poll_requests,
        poll_cycle,
        poll_overrun,
        device_state,
        mqtt_published,
        saf_queue,
        saf_dropped,
        driver_panics,
    }
});

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
    fn names_match_python() {
        super::METRICS.poll_requests.with_label_values(&["S/D", "ok"]).inc();
        super::METRICS.mqtt_published.inc();
        super::METRICS.driver_panics.with_label_values(&["S/D"]).inc();
        let text = super::render();
        for name in [
            "poll_requests_total",
            "driver_panics_total",
            "mqtt_published_total",
            "saf_queue_messages",
            "saf_dropped_total",
        ] {
            assert!(text.contains(name), "{name} missing in\n{text}");
        }
    }
}
