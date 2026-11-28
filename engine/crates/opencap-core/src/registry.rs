//! Registry: devices and their block plans (Python `opencap.core.registry`). Plans are
//! built at startup and on reload; building a plan never touches the network.

use std::collections::BTreeMap;
use std::sync::Arc;

use indexmap::IndexMap;

use crate::config::{Channel, Device};
use crate::driver::{DriverFactory, Plan};

#[derive(Debug, Clone)]
pub struct RateGroupPlan {
    pub rate_ms: i64,
    pub tag_count: usize,
    pub plan: Plan,
}

#[derive(Debug, Clone)]
pub struct DevicePlan {
    pub device: Device,
    pub groups: Vec<RateGroupPlan>,
    /// Set when no plan could be built (e.g. driver not implemented).
    pub error: Option<String>,
}

impl DevicePlan {
    pub fn ok(&self) -> bool {
        self.error.is_none()
    }

    pub fn enabled_tags(&self) -> usize {
        self.groups.iter().map(|g| g.tag_count).sum()
    }

    pub fn blocks(&self) -> usize {
        self.groups.iter().map(|g| g.plan.blocks.len()).sum()
    }

    /// Round trips for one pass over every rate group.
    pub fn wire_requests(&self) -> usize {
        self.groups.iter().map(|g| g.plan.wire_requests()).sum()
    }

    pub fn invalid(&self) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();
        for g in &self.groups {
            out.extend(g.plan.invalid.iter().map(|(k, v)| (k.clone(), v.clone())));
        }
        out
    }

    pub fn est_cycle_ms(&self, rtt_ms: f64) -> f64 {
        self.wire_requests() as f64 * rtt_ms
    }

    /// Fraction of wall time the channel spends on the wire for this device.
    pub fn est_load(&self, rtt_ms: f64) -> f64 {
        self.groups.iter().map(|g| g.plan.wire_requests() as f64 * rtt_ms / g.rate_ms as f64).sum()
    }
}

pub fn plan_device(device: &Device, factory: &DriverFactory) -> DevicePlan {
    let mut result = DevicePlan { device: device.clone(), groups: Vec::new(), error: None };
    let driver = match factory(device) {
        Ok(d) => d,
        Err(e) => {
            result.error = Some(e);
            return result;
        }
    };
    for (rate_ms, tags) in device.rate_groups() {
        let plan = driver.plan(&tags);
        result.groups.push(RateGroupPlan { rate_ms, tag_count: tags.len(), plan });
    }
    result
}

#[derive(Debug, Clone)]
pub struct Registry {
    pub channels: Vec<Channel>,
    /// Key = device id, in config order.
    pub plans: IndexMap<String, Arc<DevicePlan>>,
}

impl Registry {
    pub fn build(channels: &[Channel], factory: &DriverFactory) -> Self {
        let mut plans = IndexMap::new();
        for channel in channels {
            for device in &channel.devices {
                plans.insert(device.id.clone(), Arc::new(plan_device(device, factory)));
            }
        }
        Self { channels: channels.to_vec(), plans }
    }

    pub fn device(&self, device_id: &str) -> Option<&Device> {
        self.plans.get(device_id).map(|p| &p.device)
    }
}

/// `GET /api/v1/devices/{id}/plan`: the block plan of one device as the Python engine's
/// `device_plan_json(…, verbose=True)` writes it. `rtt_ms` is `check.est_rtt_ms`.
pub fn device_plan_json(dp: &DevicePlan, rtt_ms: f64) -> serde_json::Value {
    use serde_json::{json, Map, Value};
    let dev = &dp.device;
    let mut out = Map::new();
    out.insert("id".into(), json!(dev.id));
    out.insert("name".into(), json!(dev.name));
    out.insert("driver".into(), json!(dev.driver));
    out.insert("enabled".into(), json!(dev.enabled));
    out.insert("tags".into(), json!(dev.tags.len()));
    out.insert("plan_error".into(), json!(dp.error));
    if !dp.ok() {
        return Value::Object(out);
    }
    let round = |v: f64, digits: i32| {
        let k = 10f64.powi(digits);
        (v * k).round() / k
    };
    let invalid = dp.invalid();
    out.insert("enabled_tags".into(), json!(dp.enabled_tags()));
    out.insert("bad_config".into(), json!(invalid.len()));
    out.insert("rates_ms".into(), json!(dp.groups.iter().map(|g| g.rate_ms).collect::<Vec<_>>()));
    out.insert(
        "groups".into(),
        Value::Array(
            dp.groups
                .iter()
                .map(|g| {
                    json!({"rate_ms": g.rate_ms, "tags": g.tag_count, "blocks": g.plan.blocks.len(),
                           "wire_requests": g.plan.wire_requests()})
                })
                .collect(),
        ),
    );
    out.insert("blocks".into(), json!(dp.blocks()));
    out.insert("wire_requests".into(), json!(dp.wire_requests()));
    out.insert("est_cycle_ms".into(), json!(round(dp.est_cycle_ms(rtt_ms), 1)));
    out.insert("load".into(), json!(round(dp.est_load(rtt_ms), 4)));
    out.insert(
        "block_plan".into(),
        Value::Array(
            dp.groups
                .iter()
                .flat_map(|g| {
                    g.plan.blocks.iter().map(move |b| {
                        json!({"rate_ms": g.rate_ms, "describe": b.describe(), "tags": b.tags(),
                               "wire_requests": b.wire_requests()})
                    })
                })
                .collect(),
        ),
    );
    out.insert("invalid".into(), json!(invalid));
    Value::Object(out)
}
