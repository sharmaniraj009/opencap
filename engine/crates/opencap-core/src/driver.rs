//! Driver interface (Python `opencap.drivers.base`).
//!
//! Every protocol lives behind [`Driver`]. The core only sees `plan()` and `read()`.
//! `plan()` is pure (no I/O) so `check` and unit tests can build block plans without a
//! socket. The engine is READ-ONLY: the trait has no write method at all, so a driver
//! cannot be asked to write to a PLC.
//!
//! Error classes are the Python ones:
//! - [`DriverError::Comm`] (Python `CommError`): one attempt failed (timeout, transient
//!   device error). The worker retries.
//! - [`DriverError::ConnectionLost`] (`ConnectionLost(CommError)`): the connection is gone.
//!   The worker closes the driver and reconnects with backoff.
//! - [`DriverError::BlockConfig`] (`BlockConfigError`): the device permanently rejects
//!   the block. Tags -> `bad_config`, block skipped until reload.
//! - [`DriverError::NotImplemented`]: the driver I/O does not exist (device not polled).
//! - [`DriverError::Other`]: anything else (counted as `error`, retried).

use std::any::Any;
use std::collections::{BTreeMap, HashMap};
use std::fmt::Debug;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{Map, Value as Json};

use crate::config::{Device, Tag};
use crate::value::TagValue;

/// One wire request. Drivers implement this for their own block types.
pub trait Block: Debug + Send + Sync + 'static {
    /// Names of the tags this block serves, sorted.
    fn tags(&self) -> &[String];
    /// Short human-readable form for `check`, e.g. `FC03 44..75 (32 regs)`.
    fn describe(&self) -> String;
    /// Estimated request/reply round trips this block costs.
    fn wire_requests(&self) -> usize {
        1
    }
    fn as_any(&self) -> &dyn Any;
}

pub type BlockRef = Arc<dyn Block>;

/// Result of `Driver::plan()`: blocks to read, plus tags that cannot be read
/// (`invalid`: tag name -> reason; they get `bad_config` and never go on the wire).
#[derive(Debug, Default, Clone)]
pub struct Plan {
    pub blocks: Vec<BlockRef>,
    pub invalid: BTreeMap<String, String>,
}

impl Plan {
    pub fn wire_requests(&self) -> usize {
        self.blocks.iter().map(|b| b.wire_requests()).sum()
    }
}

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum DriverError {
    #[error("{0}")]
    Comm(String),
    #[error("{0}")]
    ConnectionLost(String),
    #[error("{0}")]
    BlockConfig(String),
    #[error("{0}")]
    NotImplemented(String),
    #[error("{0}")]
    Other(String),
}

impl DriverError {
    /// The Python exception class name, for `last_error` texts identical to Python's
    /// `f"{type(exc).__name__}: {exc}"`.
    pub fn class_name(&self) -> &'static str {
        match self {
            DriverError::Comm(_) => "CommError",
            DriverError::ConnectionLost(_) => "ConnectionLost",
            DriverError::BlockConfig(_) => "BlockConfigError",
            DriverError::NotImplemented(_) => "NotImplementedError",
            DriverError::Other(_) => "Exception",
        }
    }

    pub fn describe(&self) -> String {
        let text = self.to_string();
        if text.is_empty() {
            self.class_name().to_string()
        } else {
            format!("{}: {}", self.class_name(), text)
        }
    }

    pub fn is_comm(&self) -> bool {
        matches!(self, DriverError::Comm(_) | DriverError::ConnectionLost(_))
    }
}

pub type ReadResult = Result<HashMap<String, TagValue>, DriverError>;

#[async_trait]
pub trait Driver: Send {
    fn name(&self) -> &'static str;

    /// Pure: parse addresses and merge tags into wire blocks.
    fn plan(&self, tags: &[&Tag]) -> Plan;

    /// True while the driver believes its connection is usable.
    fn connected(&self) -> bool {
        false
    }

    /// True if `connected()` is meaningful (Python: the class overrides `connected`). The
    /// worker then reopens a closed connection before the first attempt of a block.
    fn reports_connection(&self) -> bool {
        false
    }

    /// Outer time budget for `connect()`, or None for the core's per-call safety net.
    fn connect_timeout(&self) -> Option<Duration> {
        None
    }

    /// One connect attempt. `ConnectionLost` on failure.
    async fn connect(&mut self) -> Result<(), DriverError> {
        Err(DriverError::NotImplemented(format!("{}: connect() not implemented yet", self.name())))
    }

    /// Idempotent, never fails.
    async fn close(&mut self) {}

    /// One attempt to read one block: a `TagValue` for every name in `block.tags()`.
    async fn read(&mut self, block: &dyn Block) -> ReadResult;

    /// Vendor/model/firmware if the device reports it; empty otherwise. Never fails.
    async fn identity(&mut self) -> Map<String, Json> {
        Map::new()
    }
}

/// Builds the driver for a device (no I/O). `Err` = the device cannot be planned or read
/// (unknown driver, driver not implemented, bad params).
pub type DriverFactory = Arc<dyn Fn(&Device) -> Result<Box<dyn Driver>, String> + Send + Sync>;
