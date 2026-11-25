//! Out-of-process driver plugins (ADR-0004 §3, 03-deep-dive §3.3): the `opencap.plugin.v1`
//! gRPC client, the plugin process supervisor and the `Driver` implementation the engine's
//! workers use for devices with `plugin: python`.
//!
//! - [`proto`]: the protocol, compiled from `proto/opencap/plugin/v1/driver.proto` at build
//!   time (compatibility rules and launch contract in `proto/README.md`).
//! - [`PluginHost`]: one supervised plugin process (start, restart with backoff, stop).
//! - [`PluginDriver`]: `Driver` over the protocol; a dead plugin is `ConnectionLost`.
//! - [`PluginRegistry`]: routes `plugin: python` devices, prepares their plans and wraps the
//!   engine's driver factory.
//!
//! Read-only like every driver: the protocol has no write call (ADR-0008).

pub mod convert;
pub mod driver;
pub mod host;
pub mod registry;

pub mod proto {
    //! Generated from `proto/opencap/plugin/v1/driver.proto`.
    #![allow(clippy::all, clippy::pedantic, missing_docs)]
    tonic::include_proto!("opencap.plugin.v1");
}

pub use driver::{DevicePlans, PluginBlock, PluginDriver};
pub use host::{Launched, Launcher, PluginError, PluginHost, ProcessLauncher, Transport};
pub use registry::{routed, PluginRegistry};
