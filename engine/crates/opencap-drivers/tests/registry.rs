//! The driver registry lists exactly the driver crates compiled into this build (#67).
//! Run once with the default features and once with `--no-default-features`.

use std::collections::BTreeSet;

use opencap_core::config::{Device, KNOWN_DRIVERS};
use opencap_drivers::{compiled_in, create_driver, DRIVERS};

/// `driver-*` features Cargo enabled for this build (see build.rs).
fn enabled_features() -> BTreeSet<&'static str> {
    env!("OPENCAP_DRIVER_FEATURES").split(',').filter(|f| !f.is_empty()).collect()
}

/// (`driver-*` feature names, `default` feature list) from this crate's Cargo.toml.
fn manifest_features() -> (BTreeSet<String>, BTreeSet<String>) {
    let text = include_str!("../Cargo.toml");
    let section = text.split("[features]").nth(1).expect("[features] section");
    let section = section.split("\n[").next().unwrap();
    let declared = section
        .lines()
        .filter_map(|l| l.trim().strip_prefix("driver-").and_then(|r| r.split_once('=')))
        .map(|(name, _)| format!("driver-{}", name.trim()))
        .collect();
    let default = section.split("default").nth(1).expect("default feature").split(']').next().unwrap();
    let default = default.split('"').skip(1).step_by(2).map(str::to_owned).collect();
    (declared, default)
}

fn device(driver: &str) -> Device {
    Device::new("SITE/PLC", "SITE", "PLC", driver, "192.0.2.10", 502, vec![])
}

#[test]
fn the_registry_lists_exactly_the_compiled_in_features() {
    let compiled: BTreeSet<&str> = DRIVERS.iter().filter(|e| e.create.is_some()).map(|e| e.feature).collect();
    assert_eq!(compiled, enabled_features());
    let names: Vec<&str> = DRIVERS.iter().filter(|e| enabled_features().contains(e.feature)).map(|e| e.name).collect();
    assert_eq!(compiled_in(), names);
}

#[test]
fn every_driver_feature_has_one_table_entry_and_is_a_default() {
    let (declared, default) = manifest_features();
    let table: BTreeSet<String> = DRIVERS.iter().map(|e| e.feature.to_owned()).collect();
    assert_eq!(table.len(), DRIVERS.len(), "one entry per feature");
    assert_eq!(declared, table, "Cargo.toml [features] vs the DRIVERS table");
    assert_eq!(default, table, "`default` = every driver that exists");
    let names: BTreeSet<&str> = DRIVERS.iter().map(|e| e.name).collect();
    assert_eq!(names.len(), DRIVERS.len(), "one entry per driver name");
    for e in DRIVERS {
        assert!(KNOWN_DRIVERS.contains(&e.name), "{} is not a config driver name", e.name);
        assert_eq!(e.feature, format!("driver-{}", e.name.replace('_', "-")), "feature naming");
    }
}

#[test]
fn a_driver_whose_feature_is_off_is_reported_not_compiled_in() {
    for e in DRIVERS {
        let res = create_driver(&device(e.name));
        let err = res.as_ref().err().cloned().unwrap_or_default();
        if e.create.is_some() {
            assert!(!err.contains("not compiled in"), "{}: {err}", e.name);
        } else {
            assert_eq!(err, format!("driver '{}' is not compiled in (build with Cargo feature {})", e.name, e.feature));
        }
    }
}

#[test]
fn a_known_driver_without_a_crate_is_not_implemented_yet() {
    for name in KNOWN_DRIVERS.iter().filter(|n| !DRIVERS.iter().any(|e| e.name == **n)) {
        let err = create_driver(&device(name)).err().expect("no crate, no driver");
        assert_eq!(err, format!("driver '{name}' is not implemented yet in the Rust engine"));
    }
    assert_eq!(create_driver(&device("nope")).err().unwrap(), "unknown driver 'nope'");
}
