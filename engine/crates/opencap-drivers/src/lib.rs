//! opencap engine driver registry. Read-only by construction: the `Driver` trait has no write.
//!
//! Each driver is its own crate in `engine/drivers/<name>` behind a Cargo feature
//! (`driver-<name>`, ADR-0004 §2, #67), so parallel driver ports never edit the same crate.
//! [`DRIVERS`] is the one static table that maps a config `driver:` name to its crate.
//!
//! A device whose driver is in the table but whose feature is off is reported as "not compiled
//! in"; a driver name the Python engine knows but that has no Rust crate yet is reported as "not
//! implemented yet in the Rust engine". Either way the device is skipped and listed, never
//! polled, exactly how the Python engine treats a driver whose module is missing.
//! The roadmap for the others is in docs/rfc/0001-rust-engine.md.

use std::sync::Arc;

use opencap_core::config::{Device, KNOWN_DRIVERS};
use opencap_core::driver::{Driver, DriverFactory};
use opencap_core::pyfmt::py_repr_str;

/// Builds a driver for one device; the error text is the same as Python's ValueError.
pub type CreateFn = fn(&Device) -> Result<Box<dyn Driver>, String>;

/// One driver crate.
pub struct DriverEntry {
    /// The config `driver:` name (Python `DriverName`).
    pub name: &'static str,
    /// The Cargo feature of `opencap-drivers` (and `opencap-engine`) that compiles it in.
    pub feature: &'static str,
    /// `None` when the feature is off in this build.
    pub create: Option<CreateFn>,
}

/// Every driver crate that exists, compiled in or not. Add one entry per crate.
pub static DRIVERS: &[DriverEntry] = &[
    DriverEntry {
        name: "modbus_tcp",
        feature: "driver-modbus-tcp",
        #[cfg(feature = "driver-modbus-tcp")]
        create: Some(|d| Ok(Box::new(opencap_driver_modbus_tcp::ModbusTcpDriver::new(d)?))),
        #[cfg(not(feature = "driver-modbus-tcp"))]
        create: None,
    },
    DriverEntry {
        name: "modbus_rtu",
        feature: "driver-modbus-rtu",
        #[cfg(feature = "driver-modbus-rtu")]
        create: Some(|d| Ok(Box::new(opencap_driver_modbus_rtu::ModbusRtuDriver::new(d)?))),
        #[cfg(not(feature = "driver-modbus-rtu"))]
        create: None,
    },
    DriverEntry {
        name: "ab_logix",
        feature: "driver-ab-logix",
        #[cfg(feature = "driver-ab-logix")]
        create: Some(|d| Ok(Box::new(opencap_driver_ab_logix::AbLogixDriver::new(d)?))),
        #[cfg(not(feature = "driver-ab-logix"))]
        create: None,
    },
    DriverEntry {
        name: "melsec_mc",
        feature: "driver-melsec-mc",
        #[cfg(feature = "driver-melsec-mc")]
        create: Some(|d| Ok(Box::new(opencap_driver_melsec_mc::MelsecMcDriver::new(d)?))),
        #[cfg(not(feature = "driver-melsec-mc"))]
        create: None,
    },
    DriverEntry {
        name: "s7",
        feature: "driver-s7",
        #[cfg(feature = "driver-s7")]
        create: Some(|d| Ok(Box::new(opencap_driver_s7::S7Driver::new(d)?))),
        #[cfg(not(feature = "driver-s7"))]
        create: None,
    },
    DriverEntry {
        name: "omron_fins",
        feature: "driver-omron-fins",
        #[cfg(feature = "driver-omron-fins")]
        create: Some(|d| Ok(Box::new(opencap_driver_omron_fins::FinsDriver::new(d)?))),
        #[cfg(not(feature = "driver-omron-fins"))]
        create: None,
    },
    DriverEntry {
        name: "simulator",
        feature: "driver-simulator",
        #[cfg(feature = "driver-simulator")]
        create: Some(|d| Ok(Box::new(opencap_driver_simulator::SimulatorDriver::new(d)?))),
        #[cfg(not(feature = "driver-simulator"))]
        create: None,
    },
    DriverEntry {
        name: "ab_df1",
        feature: "driver-ab-df1",
        #[cfg(feature = "driver-ab-df1")]
        create: Some(|d| Ok(Box::new(opencap_driver_ab_df1::AbDf1Driver::new(d)?))),
        #[cfg(not(feature = "driver-ab-df1"))]
        create: None,
    },
];

/// Driver names this build can poll, in table order.
pub fn compiled_in() -> Vec<&'static str> {
    DRIVERS.iter().filter(|e| e.create.is_some()).map(|e| e.name).collect()
}

pub fn create_driver(device: &Device) -> Result<Box<dyn Driver>, String> {
    let name = device.driver.as_str();
    match DRIVERS.iter().find(|e| e.name == name) {
        Some(DriverEntry { create: Some(create), .. }) => create(device),
        Some(DriverEntry { feature, .. }) => {
            Err(format!("driver {} is not compiled in (build with Cargo feature {feature})", py_repr_str(name)))
        }
        None if KNOWN_DRIVERS.contains(&name) => {
            Err(format!("driver {} is not implemented yet in the Rust engine", py_repr_str(name)))
        }
        None => Err(format!("unknown driver {}", py_repr_str(name))),
    }
}

/// The factory the engine uses for every channel.
pub fn default_factory() -> DriverFactory {
    Arc::new(create_driver)
}
