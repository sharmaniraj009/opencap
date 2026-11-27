//! OPC UA client driver (#46, D-124): reads the configured nodes with the Read service on
//! async-opcua, one request per block of up to `max_items` distinct nodes.
//!
//! Polled, not subscribed: the worker's scan period drives the reads, so staleness, the store and the
//! publishers behave as for every other driver. (The Python driver subscribes; monitored items are a
//! follow-up, D-124.)
//!
//! Read-only (ADR-0008): this crate calls `Session::read` only. `tests/read_only.rs` fails if its
//! sources name `write`, `call`, `history_update` or a node-management service.

pub mod params;
pub mod values;

use std::any::Any;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use opcua::client::{ClientBuilder, IdentityToken, Session};
use opcua::types::{AttributeId, NodeId, ReadValueId, TimestampsToReturn};
use serde_json::{Map, Value as Json};
use tokio::task::JoinHandle;
use tracing::debug;

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Block, BlockRef, Driver, DriverError, Plan, ReadResult};
use opencap_core::value::{Quality, TagValue};

pub use params::OpcUaParams;
use values::data_value_to_tag;

/// Resolves an environment variable name (the user name and password are never in a file).
pub type EnvFn = Arc<dyn Fn(&str) -> Option<String> + Send + Sync>;

/// One node and every tag served by it.
#[derive(Debug, Clone)]
pub struct OpcUaItem {
    pub node: NodeId,
    /// canonical text, the dedupe key
    pub key: String,
    pub tags: Vec<(String, DType)>,
}

#[derive(Debug)]
pub struct OpcUaBlock {
    pub items: Vec<OpcUaItem>,
    tags: Vec<String>,
}

impl Block for OpcUaBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        format!("UA read {} nodes", self.items.len())
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

/// `ns=2;s=Name`, `i=2258`, `ns=4;g=<guid>`: async-opcua's NodeId grammar.
pub fn parse_node_id(text: &str) -> Result<NodeId, String> {
    let t = text.trim();
    if t.is_empty() {
        return Err("empty NodeId".into());
    }
    t.parse::<NodeId>()
        .map_err(|_| format!("NodeId {text:?}: expected a form like 'ns=7;s=NAME', 'i=2258', 'ns=4;g=<guid>'"))
}

/// Pure: distinct nodes in first-seen order, `max_items` per block; unparsable addresses go to `invalid`.
pub fn plan_tags<'a>(params: &OpcUaParams, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
    let mut plan = Plan::default();
    let mut items: Vec<OpcUaItem> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    for tag in tags.into_iter().filter(|t| t.enabled) {
        match parse_node_id(&tag.address) {
            Err(e) => {
                plan.invalid.insert(tag.name.clone(), e);
            }
            Ok(node) => {
                let key = node.to_string();
                let i = *index.entry(key.clone()).or_insert_with(|| {
                    items.push(OpcUaItem { node, key, tags: Vec::new() });
                    items.len() - 1
                });
                items[i].tags.push((tag.name.clone(), tag.dtype));
            }
        }
    }
    for chunk in items.chunks(params.max_items) {
        let mut names: Vec<String> = chunk.iter().flat_map(|i| i.tags.iter().map(|(n, _)| n.clone())).collect();
        names.sort();
        let block: BlockRef = Arc::new(OpcUaBlock { items: chunk.to_vec(), tags: names });
        plan.blocks.push(block);
    }
    plan
}

pub struct OpcUaClientDriver {
    device: Device,
    pub params: OpcUaParams,
    env: EnvFn,
    session: Option<Arc<Session>>,
    event_loop: Option<JoinHandle<opcua::types::StatusCode>>,
}

impl OpcUaClientDriver {
    pub fn new(device: &Device) -> Result<Self, String> {
        Self::with_env(device, Arc::new(|n| std::env::var(n).ok()))
    }

    /// `env` resolves the user name and password variable names.
    pub fn with_env(device: &Device, env: EnvFn) -> Result<Self, String> {
        Ok(Self {
            device: device.clone(),
            params: OpcUaParams::from_device(device)?,
            env,
            session: None,
            event_loop: None,
        })
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        format!("{} ({})", self.device.id, self.params.endpoint)
    }

    fn identity_token(&self) -> Result<IdentityToken, String> {
        match (&self.params.username_env, &self.params.password_env) {
            (None, None) => Ok(IdentityToken::Anonymous),
            (Some(u), Some(p)) => {
                let user = (self.env)(u)
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| format!("environment variable {u} is not set"))?;
                let pass = (self.env)(p)
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| format!("environment variable {p} is not set"))?;
                Ok(IdentityToken::UserName(user, pass.into()))
            }
            _ => Err("set both username_env and password_env, or neither".into()),
        }
    }
}

#[async_trait]
impl Driver for OpcUaClientDriver {
    fn name(&self) -> &'static str {
        "opcua_client"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        plan_tags(&self.params, tags.iter().copied())
    }

    fn connected(&self) -> bool {
        self.session.is_some() && self.event_loop.as_ref().is_some_and(|h| !h.is_finished())
    }

    fn reports_connection(&self) -> bool {
        true
    }

    fn connect_timeout(&self) -> Option<Duration> {
        Some(self.timeout() * 2 + Duration::from_secs(2))
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let lost =
            |this: &Self, why: String| DriverError::ConnectionLost(format!("{}: connect failed: {why}", this.where_()));
        let identity = self.identity_token().map_err(|e| lost(self, e))?;
        let p = &self.params;
        let pki = p
            .pki_dir
            .clone()
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::env::temp_dir().join("opencap-opcua-client-pki"));
        let mut b = ClientBuilder::new()
            .application_name("opencap")
            .application_uri(p.application_uri.clone())
            .product_uri(p.application_uri.clone())
            .pki_dir(pki)
            .trust_server_certs(p.trust_server_certs)
            .session_retry_limit(0)
            .session_timeout(p.session_timeout_ms);
        match (&p.certificate, &p.private_key) {
            (Some(c), Some(k)) => b = b.certificate_path(c.clone()).private_key_path(k.clone()),
            _ => b = b.create_sample_keypair(p.policy != opcua::crypto::SecurityPolicy::None),
        }
        let mut client = b.client().map_err(|e| lost(self, e.join("; ")))?;
        let policy = p.policy.to_str();
        let started = tokio::time::timeout(
            self.timeout(),
            client.connect_to_matching_endpoint((p.endpoint.as_str(), policy, p.mode), identity),
        )
        .await;
        let (session, event_loop) = match started {
            Err(_) => return Err(lost(self, format!("no answer within {} ms", self.device.timeout_ms))),
            Ok(Err(e)) => return Err(lost(self, e.to_string())),
            Ok(Ok(x)) => x,
        };
        let handle = event_loop.spawn();
        match tokio::time::timeout(self.timeout(), session.wait_for_connection()).await {
            Ok(true) => {
                self.session = Some(session);
                self.event_loop = Some(handle);
                Ok(())
            }
            other => {
                // the event loop ends with the reason (certificate refused, bad user, ...)
                let why = match tokio::time::timeout(Duration::from_millis(500), &mut { handle }).await {
                    Ok(Ok(status)) => format!("{status}"),
                    _ => match other {
                        Err(_) => format!("no session within {} ms", self.device.timeout_ms),
                        _ => "the session did not open".into(),
                    },
                };
                Err(lost(self, why))
            }
        }
    }

    async fn close(&mut self) {
        if let Some(h) = self.event_loop.take() {
            h.abort();
        }
        self.session = None;
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let Some(block) = block.as_any().downcast_ref::<OpcUaBlock>() else {
            return Err(DriverError::Other("opcua_client cannot read a non-OPC UA block".into()));
        };
        let where_ = self.where_();
        let Some(session) = self.session.clone().filter(|_| self.connected()) else {
            return Err(DriverError::ConnectionLost(format!("{where_}: not connected")));
        };
        let nodes: Vec<ReadValueId> = block
            .items
            .iter()
            .map(|i| ReadValueId {
                node_id: i.node.clone(),
                attribute_id: AttributeId::Value as u32,
                ..Default::default()
            })
            .collect();
        let res = tokio::time::timeout(self.timeout(), session.read(&nodes, TimestampsToReturn::Both, 0.0)).await;
        let values = match res {
            Err(_) => {
                return Err(DriverError::Comm(format!(
                    "{where_} {}: no reply within {} ms",
                    block.describe(),
                    self.device.timeout_ms
                )))
            }
            Ok(Err(e)) => {
                debug!(device = %self.device.id, error = %e, "opcua_client read failed");
                let gone = self.event_loop.as_ref().is_none_or(|h| h.is_finished());
                return Err(if gone {
                    DriverError::ConnectionLost(format!("{where_}: {e}"))
                } else {
                    DriverError::Comm(format!("{where_} {}: {e}", block.describe()))
                });
            }
            Ok(Ok(v)) => v,
        };
        if values.len() != block.items.len() {
            return Err(DriverError::Comm(format!(
                "{where_}: asked for {} nodes, got {} values",
                block.items.len(),
                values.len()
            )));
        }
        let mut out: HashMap<String, TagValue> = HashMap::new();
        for (item, dv) in block.items.iter().zip(&values) {
            for (name, dtype) in &item.tags {
                out.insert(name.clone(), data_value_to_tag(dv, *dtype));
            }
        }
        let _ = Quality::Good;
        Ok(out)
    }

    async fn identity(&mut self) -> Map<String, Json> {
        Map::new()
    }
}
