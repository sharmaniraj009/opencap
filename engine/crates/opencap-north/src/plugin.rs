//! North plugin contract (`docs/architecture/03-deep-dive.md` §3.5, ADR-0005).
//!
//! A north plugin (MQTT, Sparkplug, OPC UA server, Modbus/DNP3 servers, data logger) runs
//! as one task until `ctx.shutdown` is cancelled. It reads values only from the tag store
//! (`ctx.store`), learns about changes from the bounded change-event bus (`ctx.events`) and
//! never touches a device directly: a client's request for a fresh device read goes through
//! `ctx.demand`, which serves the store unless the device allows demand reads.
//!
//! On [`Change::Lagged`](opencap_core::store::Change::Lagged) a plugin resynchronises from
//! `ctx.store.snapshot()` of the devices it serves and then ignores batches whose version
//! is not newer than the snapshot's; the receiver has already counted the skipped batches in
//! `north_lagged_total{agent}`.

use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use async_trait::async_trait;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use tracing::{error, warn};

use opencap_core::health::HealthStatus;
use opencap_core::store::{ChangeReceiver, Snapshot, TagStoreReader};

use crate::saf::StoreAndForward;

/// A durable agent's store-and-forward outbox (ADR-0011).
pub type Outbox = Arc<StdMutex<StoreAndForward>>;

#[derive(Debug, thiserror::Error)]
pub enum NorthError {
    #[error("configuration error: {0}")]
    Config(String),
    #[error("{0}")]
    Other(String),
}

#[async_trait]
pub trait NorthPlugin: Send {
    /// `"mqtt"`, `"sparkplug"`, `"opcua"`, ...
    fn kind(&self) -> &'static str;
    /// Serve until `ctx.shutdown` is cancelled.
    async fn run(self: Box<Self>, ctx: NorthContext) -> Result<(), NorthError>;
}

/// Everything a north plugin may use.
pub struct NorthContext {
    /// Agent name (`north_lagged_total{agent}`, logs).
    pub agent: String,
    pub store: TagStoreReader,
    pub events: ChangeReceiver,
    /// Present when the agent's config says durable.
    pub outbox: Option<Outbox>,
    pub status: DeviceStatusReader,
    pub demand: DemandReader,
    pub shutdown: CancellationToken,
}

impl NorthContext {
    /// Subscribes to the bus now: start from snapshots taken after this call.
    pub fn new(agent: &str, store: TagStoreReader, status: DeviceStatusReader) -> Self {
        Self {
            agent: agent.to_string(),
            events: store.subscribe(agent),
            demand: DemandReader::new(store.clone()),
            store,
            outbox: None,
            status,
            shutdown: CancellationToken::new(),
        }
    }

    pub fn with_outbox(mut self, outbox: Outbox) -> Self {
        self.outbox = Some(outbox);
        self
    }
}

type StatusFn = dyn Fn(&str) -> Option<HealthStatus> + Send + Sync;

/// Health per device (status topics, system tags). Never waits on the wire.
#[derive(Clone)]
pub struct DeviceStatusReader(Arc<StatusFn>);

impl DeviceStatusReader {
    pub fn new(f: impl Fn(&str) -> Option<HealthStatus> + Send + Sync + 'static) -> Self {
        Self(Arc::new(f))
    }

    pub fn get(&self, device: &str) -> Option<HealthStatus> {
        (self.0)(device)
    }
}

/// Device reads requested by north clients (OPC UA `Read` with `maxAge = 0`, OPC DA device
/// reads). 03 §3.5: such a read becomes a demand poll only when the device sets
/// `allow_demand_reads`, at most once per device per `min_scan_ms`; otherwise the cached
/// value is returned. No device can opt in yet (the setting arrives with the OPC UA server,
/// #46), so every read is served from the store and PLCs see no extra traffic.
#[derive(Clone)]
pub struct DemandReader {
    store: TagStoreReader,
}

impl DemandReader {
    pub fn new(store: TagStoreReader) -> Self {
        Self { store }
    }

    pub async fn read(&self, device: &str) -> Snapshot {
        self.store.snapshot(device)
    }
}

/// A running north plugin.
pub struct NorthHandle {
    pub kind: &'static str,
    pub agent: String,
    shutdown: CancellationToken,
    task: JoinHandle<Result<(), NorthError>>,
}

/// Run `plugin` on its own task.
pub fn spawn(plugin: Box<dyn NorthPlugin>, ctx: NorthContext) -> NorthHandle {
    let kind = plugin.kind();
    let agent = ctx.agent.clone();
    let shutdown = ctx.shutdown.clone();
    let task = tokio::spawn({
        let agent = agent.clone();
        async move {
            let result = plugin.run(ctx).await;
            if let Err(e) = &result {
                error!(kind, agent = %agent, error = %e, "north.plugin_failed");
            }
            result
        }
    });
    NorthHandle { kind, agent, shutdown, task }
}

impl NorthHandle {
    pub fn is_finished(&self) -> bool {
        self.task.is_finished()
    }

    /// Cancel `shutdown` and wait up to `grace` for the plugin to return; abort it after.
    pub async fn stop(mut self, grace: Duration) {
        self.shutdown.cancel();
        if tokio::time::timeout(grace, &mut self.task).await.is_err() {
            warn!(kind = self.kind, agent = %self.agent, "north.plugin_stop_timeout");
            self.task.abort();
            let _ = self.task.await;
        }
    }
}
