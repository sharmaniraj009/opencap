//! opencap engine core: configuration (same files and schema as the Python engine),
//! value model, tag store and change-event bus, health, scheduler, driver trait and the
//! channel worker.
//!
//! See `docs/rfc/0001-rust-engine.md` for the architecture and `docs/decisions.md` for
//! every behaviour rule this crate ports from the Python reference implementation.

pub mod config;
pub mod device_time;
pub mod driver;
pub mod health;
pub mod metrics;
pub mod pyfmt;
pub mod registry;
pub mod scheduler;
pub mod store;
pub mod system_tags;
pub mod value;
pub mod worker;

/// Payload `FirmVersion`: `opencap-<version>`, identical to the Python engine's for the
/// same release.
pub const FIRM_VERSION: &str = concat!("opencap-", env!("CARGO_PKG_VERSION"));
