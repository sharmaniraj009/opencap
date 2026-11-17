//! opencap engine drivers. Read-only by construction: the `Driver` trait has no write.
//!
//! Implemented: `modbus_tcp`. Every other driver name the Python engine knows is reported
//! as "not implemented yet in the Rust engine" (the device is skipped and listed, never
//! polled), exactly how the Python engine treats a driver whose module is missing.
//! The roadmap for the others is in docs/rfc/0001-rust-engine.md.

pub mod modbus_tcp;

use std::sync::Arc;

use opencap_core::config::{Device, KNOWN_DRIVERS};
use opencap_core::driver::{Driver, DriverFactory};
use opencap_core::pyfmt::py_repr_str;

/// Driver names this build implements.
pub const IMPLEMENTED: [&str; 1] = ["modbus_tcp"];

pub fn create_driver(device: &Device) -> Result<Box<dyn Driver>, String> {
    match device.driver.as_str() {
        "modbus_tcp" => Ok(Box::new(modbus_tcp::ModbusTcpDriver::new(device)?)),
        name if KNOWN_DRIVERS.contains(&name) => {
            Err(format!("driver {} is not implemented yet in the Rust engine", py_repr_str(name)))
        }
        name => Err(format!("unknown driver {}", py_repr_str(name))),
    }
}

/// The factory the engine uses for every channel.
pub fn default_factory() -> DriverFactory {
    Arc::new(create_driver)
}
