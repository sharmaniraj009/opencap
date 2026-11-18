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
