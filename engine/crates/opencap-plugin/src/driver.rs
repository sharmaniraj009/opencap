//! [`PluginDriver`]: the engine's `Driver` trait, served by a plugin process.
//!
//! - `plan()` stays pure: plans come from the plugin's `Plan` during
//!   [`PluginRegistry::prepare`](crate::PluginRegistry::prepare), before the registry is built.
//! - `connect()` starts the plugin process if needed, opens the device in it (again after a
//!   restart: a new generation knows no devices) and makes one connect attempt.
//! - `read()` never starts a process. If the process the device was opened in is gone, the
//!   read is `ConnectionLost` and the worker's reconnect brings the plugin back.
//! - A call that fails in transport (process killed, socket gone) ends that generation and is
//!   `ConnectionLost`, so the device goes `bad_comm` and recovers like any lost connection.

use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use serde_json::{Map, Value as Json};
use tonic::{Code, Status};

use opencap_core::config::{Tag, KNOWN_DRIVERS};
use opencap_core::driver::{Block, Driver, DriverError, Plan, ReadResult};

use crate::convert::{driver_error, is_not_open, tag_values, Info};
use crate::host::{Client, PluginError, PluginHost};
use crate::proto;
use crate::registry::PluginRegistry;

const OPEN_TIMEOUT: Duration = Duration::from_secs(10);
const CLOSE_TIMEOUT: Duration = Duration::from_secs(5);

/// One wire block planned by the plugin. The payload is opaque to the engine.
pub struct PluginBlock {
    pub payload: Bytes,
    pub tags: Vec<String>,
    pub wire_requests: usize,
    pub describe: String,
}

impl fmt::Debug for PluginBlock {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PluginBlock")
            .field("describe", &self.describe)
            .field("tags", &self.tags.len())
            .field("payload_bytes", &self.payload.len())
            .finish()
    }
}

impl From<proto::Block> for PluginBlock {
    fn from(b: proto::Block) -> Self {
        Self { payload: b.payload, tags: b.tags, wire_requests: b.wire_requests as usize, describe: b.describe }
    }
}

impl Block for PluginBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }
    fn describe(&self) -> String {
        self.describe.clone()
    }
    fn wire_requests(&self) -> usize {
        self.wire_requests
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// The plans of one device, by rate group (its tag names in order), from `Plan`.
#[derive(Debug, Default)]
pub struct DevicePlans {
    pub info: Info,
    pub groups: HashMap<Vec<String>, Plan>,
}

pub struct PluginDriver {
    device_id: String,
    channel: String,
    json: String,
    name: &'static str,
    /// The channel's host is looked up on every call: a reload that changes `plugins.python`
    /// replaces it without restarting the channel's worker.
    registry: Arc<PluginRegistry>,
    plans: Arc<DevicePlans>,
    info: Info,
    /// Generation of the plugin process the device is open in.
    opened: Option<u64>,
    connected: bool,
}

fn describe(status: &Status) -> String {
    format!("{:?}: {}", status.code(), status.message())
}

impl PluginDriver {
    pub fn new(
        device: &opencap_core::config::Device,
        json: String,
        registry: Arc<PluginRegistry>,
        plans: Arc<DevicePlans>,
    ) -> Self {
        let name = KNOWN_DRIVERS.iter().copied().find(|d| *d == device.driver).unwrap_or("plugin");
        let info = plans.info;
        Self {
            device_id: device.id.clone(),
            channel: device.channel.clone(),
            json,
            name,
            registry,
            plans,
            info,
            opened: None,
            connected: false,
        }
    }

    fn host(&self) -> Result<Arc<PluginHost>, PluginError> {
        self.registry.host(&self.channel).ok_or(PluginError::Stopped)
    }

    fn device_ref(&self) -> proto::DeviceRef {
        proto::DeviceRef { device_id: self.device_id.clone() }
    }

    fn lost_text(&self, what: &str) -> String {
        format!("python plugin host ({}): {what}", self.channel)
    }

    /// A call failed in transport: end that generation (the process is gone or unusable).
    async fn transport_failed(&mut self, generation: u64, status: &Status) -> DriverError {
        self.connected = false;
        self.opened = None;
        if status.code() == Code::Unimplemented {
            return DriverError::NotImplemented(self.lost_text(&describe(status)));
        }
        if let Ok(host) = self.host() {
            host.failed(generation, &describe(status)).await;
        }
        DriverError::ConnectionLost(self.lost_text(&describe(status)))
    }

    fn status(&mut self, reply: &proto::StatusReply) {
        self.connected = reply.connected;
        if let Some(info) = &reply.info {
            self.info = info.into();
        }
    }

    async fn open(&mut self, client: &mut Client, generation: u64) -> Result<(), DriverError> {
        let req = proto::OpenRequest {
            device_id: self.device_id.clone(),
            device: Some(proto::DeviceConfig { json: self.json.clone() }),
        };
        let reply = match tokio::time::timeout(OPEN_TIMEOUT, client.open(req)).await {
            Err(_) => {
                if let Ok(host) = self.host() {
                    host.failed(generation, "Open timed out").await;
                }
                return Err(DriverError::ConnectionLost(self.lost_text("Open timed out")));
            }
            Ok(Err(status)) => return Err(self.transport_failed(generation, &status).await),
            Ok(Ok(r)) => r.into_inner(),
        };
        if let Some(e) = &reply.error {
            return Err(driver_error(e));
        }
        if let Some(info) = &reply.info {
            self.info = info.into();
        }
        self.opened = Some(generation);
        Ok(())
    }

    /// The client of the generation the device is open in, if it is still the live one.
    async fn opened_client(&self) -> Option<(Client, u64)> {
        let opened = self.opened?;
        self.host().ok()?.current().await.filter(|(_, generation)| *generation == opened)
    }
}

#[async_trait]
impl Driver for PluginDriver {
    fn name(&self) -> &'static str {
        self.name
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        let names: Vec<String> = tags.iter().map(|t| t.name.clone()).collect();
        self.plans.groups.get(&names).cloned().unwrap_or_else(|| Plan {
            blocks: Vec::new(),
            invalid: names
                .into_iter()
                .map(|n| (n, "no plugin plan for this tag group (reload the config)".into()))
                .collect(),
        })
    }

    fn connected(&self) -> bool {
        self.connected
    }

    fn reports_connection(&self) -> bool {
        self.info.reports_connection
    }

    fn connect_timeout(&self) -> Option<Duration> {
        self.info.connect_timeout
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        let ensured = match self.host() {
            Ok(host) => host.ensure().await,
            Err(e) => Err(e),
        };
        let (mut client, generation) = match ensured {
            Ok(c) => c,
            Err(e) => {
                self.connected = false;
                return Err(DriverError::ConnectionLost(self.lost_text(&e.to_string())));
            }
        };
        for retry in [false, true] {
            if self.opened != Some(generation) {
                self.open(&mut client, generation).await?;
            }
            let reply = match client.connect(self.device_ref()).await {
                Ok(r) => r.into_inner(),
                Err(status) => return Err(self.transport_failed(generation, &status).await),
            };
            if is_not_open(&reply.error) && !retry {
                self.opened = None; // the plugin lost the device: open it again, once
                continue;
            }
            self.status(&reply);
            return match &reply.error {
                None => Ok(()),
                Some(e) => Err(driver_error(e)),
            };
        }
        Err(DriverError::ConnectionLost(self.lost_text("the device cannot be opened")))
    }

    async fn close(&mut self) {
        self.connected = false;
        let Some((mut client, generation)) = self.opened_client().await else {
            self.opened = None;
            return;
        };
        match tokio::time::timeout(CLOSE_TIMEOUT, client.close(self.device_ref())).await {
            Ok(Ok(reply)) => self.status(&reply.into_inner()),
            Ok(Err(status)) => {
                let _ = self.transport_failed(generation, &status).await;
            }
            Err(_) => {}
        }
        self.connected = false;
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let Some(block) = block.as_any().downcast_ref::<PluginBlock>() else {
            return Err(DriverError::Other("not a block planned by the plugin host".into()));
        };
        let Some((mut client, generation)) = self.opened_client().await else {
            self.connected = false;
            self.opened = None;
            return Err(DriverError::ConnectionLost(self.lost_text("restarted or not running")));
        };
        let req = proto::ReadRequest { device_id: self.device_id.clone(), block: block.payload.clone() };
        let reply = match client.read(req).await {
            Ok(r) => r.into_inner(),
            Err(status) => return Err(self.transport_failed(generation, &status).await),
        };
        self.connected = reply.connected;
        if is_not_open(&reply.error) {
            self.connected = false;
            self.opened = None;
            return Err(DriverError::ConnectionLost(self.lost_text("the device is not open in the plugin")));
        }
        if let Some(e) = &reply.error {
            return Err(driver_error(e));
        }
        Ok(tag_values(&reply.values))
    }

    async fn identity(&mut self) -> Map<String, Json> {
        let Some((mut client, _)) = self.opened_client().await else { return Map::new() };
        match tokio::time::timeout(OPEN_TIMEOUT, client.identity(self.device_ref())).await {
            Ok(Ok(reply)) => serde_json::from_str::<Json>(&reply.into_inner().json)
                .ok()
                .and_then(|j| j.as_object().cloned())
                .unwrap_or_default(),
            _ => Map::new(),
        }
    }
}
