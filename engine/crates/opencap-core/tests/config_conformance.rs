//! The shared config conformance fixtures (`tests/conformance/config/`, ADR-0003), run
//! against the Rust models: every valid document loads, every invalid one is refused with
//! the same error the Python models give (`tests/unit/test_config_schema.py` is the Python
//! side). serde words two errors differently from pydantic; [`serde_words`] maps them.

use std::path::{Path, PathBuf};

use opencap_core::config::loader::from_json;
use opencap_core::config::{DeviceMap, DevicesFile, Overrides, PollerConfig};
use serde_json::Value;

fn fixtures(kind: &str) -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/conformance/config").join(kind);
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    files
}

fn load(path: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// What the Rust engine says about `data` as `document` (the same checks `load_config` runs).
fn check(document: &str, data: Value) -> Result<(), String> {
    match document {
        "devices" => from_json::<DevicesFile>(data)?.validate(),
        "devices_map" => from_json::<DeviceMap>(data).map(drop),
        "overrides" => from_json::<Overrides>(data)?.validate(),
        "poller" => {
            let p = from_json::<PollerConfig>(data)?;
            p.mqtt.validate().map_err(|e| format!("mqtt: {e}"))?;
            p.api.validate().map_err(|e| format!("api: {e}"))?;
            p.payload.validate()?;
            p.opcua_server.validate()?;
            p.plugins.python.validate()?;
            p.sparkplug.validate()
        }
        other => panic!("fixture document {other:?} has no Rust model: add it here"),
    }
}

/// pydantic's wording -> serde's, for the errors where they differ.
fn serde_words(expected: &str) -> &str {
    match expected {
        "Extra inputs are not permitted" => "unknown field",
        other => other,
    }
}

#[test]
fn there_are_fixtures() {
    assert!(fixtures("valid").len() >= 5 && fixtures("invalid").len() >= 5);
}

#[test]
fn valid_fixtures_load() {
    let mut failed = Vec::new();
    for path in fixtures("valid") {
        let fx = load(&path);
        if let Err(e) = check(fx["document"].as_str().unwrap(), fx["data"].clone()) {
            failed.push(format!("{}: {e}", path.file_name().unwrap().to_string_lossy()));
        }
    }
    assert!(failed.is_empty(), "valid fixtures refused by the Rust models:\n{}", failed.join("\n"));
}

#[test]
fn invalid_fixtures_are_refused_with_the_expected_error() {
    let mut failed = Vec::new();
    for path in fixtures("invalid") {
        let fx = load(&path);
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        let expected = serde_words(fx["expected_error"].as_str().expect("expected_error"));
        match check(fx["document"].as_str().unwrap(), fx["data"].clone()) {
            Ok(()) => failed.push(format!("{name}: accepted")),
            Err(e) if !e.contains(expected) => failed.push(format!("{name}: {e:?} does not contain {expected:?}")),
            Err(_) => {}
        }
    }
    assert!(failed.is_empty(), "invalid fixtures:\n{}", failed.join("\n"));
}

#[test]
fn errors_name_the_field_like_pydantic() {
    let fx = load(&fixtures("invalid").into_iter().find(|p| p.ends_with("devices_port_not_int.json")).unwrap());
    let e = check("devices", fx["data"].clone()).unwrap_err();
    assert!(e.starts_with("channels.0.devices.0.port: invalid type"), "{e}");
}

/// The shared zone fixture (`tests/conformance/timezones/zones.json`, LOG-21): both engines accept
/// and refuse the same `payload.timezone` names.
#[test]
fn timezone_fixture_is_accepted_and_refused_alike() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/conformance/timezones/zones.json");
    let fx = load(&path);
    for (key, ok) in [("valid", true), ("invalid", false)] {
        for name in fx[key].as_array().unwrap() {
            let cfg =
                opencap_core::config::PayloadCfg { timezone: name.as_str().unwrap().into(), ..Default::default() };
            assert_eq!(cfg.validate().is_ok(), ok, "{name}");
        }
    }
}
