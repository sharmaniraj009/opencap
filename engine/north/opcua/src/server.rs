//! The OPC UA north plugin: builds the server from `opcua_server`, the address space from the
//! devices, and copies tag-store changes into it.

use std::collections::HashMap;
use std::time::Duration;

use async_trait::async_trait;
use opcua::server::address_space::{AccessLevel, VariableBuilder};
use opcua::server::node_manager::memory::{simple_node_manager, SimpleNodeManager};
use opcua::server::{diagnostics::NamespaceMetadata, ServerBuilder, ServerEndpoint, ServerUserToken};
use opcua::types::{DataTypeId, NodeId, ObjectId, QualifiedName};
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

use opencap_core::config::{Device, OpcUaServerCfg};
use opencap_core::store::Change;
use opencap_core::value::TagValue;
use opencap_north::plugin::{NorthContext, NorthError, NorthPlugin};

use crate::address::{plan_device, DevicePlan};
use crate::config::{policy_of, split_endpoint, users_of, USER_ANONYMOUS};
use crate::values::{data_value, default_variant};

/// Where a tag's variable lives.
#[derive(Debug, Clone)]
struct Var {
    node: NodeId,
    data_type: DataTypeId,
}

pub struct OpcUaNorth {
    cfg: OpcUaServerCfg,
    devices: Vec<Device>,
    users: Vec<(String, String)>,
}

impl OpcUaNorth {
    /// `env` resolves the user and password variable names (the values are never in a file).
    pub fn new(
        cfg: OpcUaServerCfg,
        devices: Vec<Device>,
        env: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self, String> {
        cfg.validate()?;
        let users = users_of(&cfg, env)?;
        Ok(Self { cfg, devices, users })
    }

    /// The first user token ids offered on an endpoint.
    fn token_ids(&self) -> Vec<String> {
        let mut ids = Vec::new();
        if self.cfg.anonymous {
            ids.push(USER_ANONYMOUS.to_string());
        }
        ids.extend((0..self.users.len()).map(|i| format!("user{i}")));
        ids
    }

    fn build(&self, token: CancellationToken) -> Result<(opcua::server::Server, opcua::server::ServerHandle), String> {
        let cfg = &self.cfg;
        let (host, port, path) = split_endpoint(&cfg.endpoint)?;
        let mut b = ServerBuilder::new()
            .application_name(cfg.server_name.clone())
            .application_uri(cfg.application_uri.clone())
            .product_uri(cfg.application_uri.clone())
            .host(host)
            .discovery_urls(vec![cfg.advertised_endpoint.clone().unwrap_or_else(|| cfg.endpoint.clone())])
            .port(port)
            .max_sessions(usize::try_from(cfg.max_sessions).unwrap_or(100))
            .with_node_manager(simple_node_manager(
                NamespaceMetadata { namespace_uri: cfg.namespace_uri.clone(), ..Default::default() },
                "opencap",
            ))
            .token(token);
        {
            let l = b.limits_mut();
            l.subscriptions.max_subscriptions_per_session =
                usize::try_from(cfg.max_subscriptions_per_session).unwrap_or(50);
            l.subscriptions.min_publishing_interval_ms = cfg.min_publishing_interval_ms.max(0) as f64;
            l.subscriptions.min_sampling_interval_ms = cfg.min_sampling_interval_ms.max(0) as f64;
            l.subscriptions.max_monitored_items_per_sub = usize::try_from(cfg.max_monitored_items).unwrap_or(100_000);
        }
        if let Some(dir) = &cfg.pki_dir {
            b = b.pki_dir(dir.clone());
        }
        if let (Some(cert), Some(key)) = (&cfg.certificate, &cfg.private_key) {
            b = b.certificate_path(cert.clone()).private_key_path(key.clone());
        } else if cfg.security_policies.iter().any(|p| p != "None") {
            b = b.create_sample_keypair(true);
        }
        // fail closed: a client certificate is accepted only when it sits in the trusted store
        b = b.trust_client_certs(false).check_cert_time(true);
        let ids = self.token_ids();
        for (i, (user, pass)) in self.users.iter().enumerate() {
            b = b.add_user_token(format!("user{i}"), ServerUserToken::user_pass(user.clone(), pass.clone()));
        }
        for name in &cfg.security_policies {
            let (policy, mode) = policy_of(name).ok_or_else(|| format!("unknown security policy {name}"))?;
            b = b.add_endpoint(name.clone(), ServerEndpoint::new(path.clone(), policy, mode, &ids));
        }
        b.build()
    }
}

fn add_device(
    nm: &SimpleNodeManager,
    ns: u16,
    plan: &DevicePlan,
    channel: &str,
    dev: &Device,
    channels: &mut std::collections::HashSet<String>,
) -> HashMap<String, Var> {
    let mut vars = HashMap::new();
    let mut sp = nm.address_space().write();
    let objects = NodeId::from(ObjectId::ObjectsFolder);
    if channels.insert(channel.to_string()) {
        sp.add_folder(&NodeId::new(ns, channel.to_string()), QualifiedName::new(ns, channel), channel, &objects);
    }
    let dev_node = NodeId::new(ns, plan.base.clone());
    sp.add_folder(
        &dev_node,
        QualifiedName::new(ns, dev.name.as_str()),
        dev.name.as_str(),
        &NodeId::new(ns, channel.to_string()),
    );
    for f in &plan.folders {
        sp.add_folder(
            &NodeId::new(ns, f.path.clone()),
            QualifiedName::new(ns, f.name.as_str()),
            f.name.as_str(),
            &NodeId::new(ns, f.parent.clone()),
        );
    }
    for v in &plan.vars {
        let id = NodeId::new(ns, v.path.clone());
        let mut b = VariableBuilder::new(&id, QualifiedName::new(ns, v.name.as_str()), v.name.as_str())
            .data_type(v.data_type)
            .value(default_variant(v.data_type))
            .access_level(AccessLevel::CURRENT_READ)
            .user_access_level(AccessLevel::CURRENT_READ)
            .historizing(false)
            .minimum_sampling_interval(0.0)
            .component_of(NodeId::new(ns, v.parent.clone()));
        if let Some(d) = &v.description {
            b = b.description(d.as_str());
        }
        if v.analog {
            b = b.has_type_definition(opcua::types::VariableTypeId::AnalogItemType);
        }
        b.insert(&mut *sp);
        vars.insert(v.tag.clone(), Var { node: id, data_type: v.data_type });
    }
    vars
}

#[async_trait]
impl NorthPlugin for OpcUaNorth {
    fn kind(&self) -> &'static str {
        "opcua"
    }

    async fn run(self: Box<Self>, mut ctx: NorthContext) -> Result<(), NorthError> {
        let token = ctx.shutdown.child_token();
        let (server, handle) = self.build(token.clone()).map_err(NorthError::Config)?;
        let nm = handle
            .node_managers()
            .get_of_type::<SimpleNodeManager>()
            .ok_or_else(|| NorthError::Other("opcua: the simple node manager is missing".into()))?;
        let ns = handle
            .get_namespace_index(&self.cfg.namespace_uri)
            .ok_or_else(|| NorthError::Other("opcua: the tag namespace was not registered".into()))?;
        info!(namespace = ns, endpoint = %self.cfg.endpoint, "opcua: address space");

        // device id -> tag -> variable
        let mut map: HashMap<String, HashMap<String, Var>> = HashMap::new();
        let mut channels = std::collections::HashSet::new();
        for dev in &self.devices {
            let plan = plan_device(dev);
            for (tag, why) in &plan.skipped {
                warn!(device = %dev.id, tag = %tag, error = %why, "opcua_server.tag_skipped");
            }
            map.insert(dev.id.clone(), add_device(&nm, ns, &plan, &dev.channel, dev, &mut channels));
        }

        let server_task = tokio::spawn(server.run());
        let update = Duration::from_millis(u64::try_from(self.cfg.update_ms).unwrap_or(250).max(10));
        let subs = handle.subscriptions().clone();
        let mut pending: HashMap<NodeId, (DataTypeId, TagValue)> = HashMap::new();
        let flush = |pending: &mut HashMap<NodeId, (DataTypeId, TagValue)>| {
            if pending.is_empty() {
                return;
            }
            let batch: Vec<(NodeId, opcua::types::DataValue)> =
                pending.drain().map(|(id, (t, tv))| (id, data_value(t, &tv))).collect();
            let _ = nm.set_values(&subs, batch.iter().map(|(id, dv)| (id, None, dv.clone())));
        };
        let load = |pending: &mut HashMap<NodeId, (DataTypeId, TagValue)>,
                    device: &str,
                    store: &opencap_core::store::TagStoreReader| {
            if let Some(vars) = map.get(device) {
                let snap = store.snapshot(device);
                for (name, tv) in snap.iter() {
                    if let Some(v) = vars.get(name) {
                        pending.insert(v.node.clone(), (v.data_type, tv.clone()));
                    }
                }
            }
        };
        for dev in &self.devices {
            load(&mut pending, &dev.id, &ctx.store);
        }
        flush(&mut pending);

        let rejected_dir = self.cfg.pki_dir.as_ref().map(|d| std::path::Path::new(d).join("rejected"));
        let mut prune_tick = tokio::time::interval(Duration::from_secs(30));
        let mut tick = tokio::time::interval(update);
        loop {
            tokio::select! {
                _ = ctx.shutdown.cancelled() => break,
                _ = tick.tick() => flush(&mut pending),
                _ = prune_tick.tick() => {
                    if let Some(dir) = &rejected_dir {
                        let n = crate::pki::prune_rejected(dir, crate::pki::MAX_REJECTED);
                        if n > 0 {
                            warn!(removed = n, keep = crate::pki::MAX_REJECTED, "opcua_server.rejected_certificates_pruned");
                        }
                    }
                }
                change = ctx.events.recv() => match change {
                    None => break,
                    Some(Change::Batch(b)) => {
                        if let Some(vars) = map.get(b.device_name()) {
                            for (name, tv) in b.iter() {
                                if let Some(v) = vars.get(name) {
                                    pending.insert(v.node.clone(), (v.data_type, tv.clone()));
                                }
                            }
                        }
                    }
                    Some(Change::Lagged(_)) => {
                        for dev in &self.devices {
                            load(&mut pending, &dev.id, &ctx.store);
                        }
                    }
                },
            }
        }
        token.cancel();
        let _ = server_task.await;
        Ok(())
    }
}
