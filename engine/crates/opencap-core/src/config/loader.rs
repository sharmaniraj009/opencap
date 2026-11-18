//! Config loader: a line-by-line port of `src/opencap/config/loader.py`.
//!
//! Reads `poller.yaml` (optional), `devices.json` (required), `overrides.yaml` (optional)
//! and `devices_map.yaml` (optional) and applies them in the same order:
//!
//! 1. `devices.json`
//! 2. `overrides.yaml` device fields, `params` (dict merge) and per-tag fields
//! 3. publish settings, lowest priority first: devices.json `publish` ->
//!    `devices_map.yaml` `defaults` -> `overrides.yaml` `publish` -> the device's
//!    `devices_map.yaml` entry (`sub_device_id`, `topic`, `env`)
//! 4. `publish.topic` still unset -> `default_topic(device, poller.mqtt.topic_prefix)`
//!
//! Warnings have the same text as Python's (they are printed by `check`).

use std::fmt;
use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;
use serde_json::{Map, Value as Json};

use super::models::*;
use super::{default_topic, ids_text};
use crate::pyfmt::py_repr_str as r;

pub const POLLER_FILE: &str = "poller.yaml";
pub const DEVICES_FILE: &str = "devices.json";
pub const MAP_FILE: &str = "devices_map.yaml";
pub const OVERRIDES_FILE: &str = "overrides.yaml";

const DEVICE_SCALARS: [&str; 9] =
    ["enabled", "host", "port", "scan_ms", "min_scan_ms", "timeout_ms", "retries", "demote_after", "demote_for_ms"];
const TAG_FIELDS: [&str; 5] = ["enabled", "address", "dtype", "scan_ms", "scale"];

#[derive(Debug, Clone)]
pub struct LoadedConfig {
    pub poller: PollerConfig,
    pub channels: Vec<Channel>,
    pub excluded: Vec<ExcludedDevice>,
    pub warnings: Vec<String>,
}

impl LoadedConfig {
    pub fn devices(&self) -> impl Iterator<Item = &Device> {
        self.channels.iter().flat_map(|c| c.devices.iter())
    }
}

#[derive(Debug)]
pub enum ConfigError {
    /// `devices.json` is missing (Python: FileNotFoundError).
    NotFound(String),
    /// Unreadable file, bad JSON/YAML, or a value the schema rejects (Python: ValueError /
    /// pydantic ValidationError).
    Invalid(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::NotFound(m) | ConfigError::Invalid(m) => f.write_str(m),
        }
    }
}

impl std::error::Error for ConfigError {}

fn invalid(msg: impl Into<String>) -> ConfigError {
    ConfigError::Invalid(msg.into())
}

/// Missing file -> `{}`; empty file -> `{}`; the top level must be a mapping.
pub fn read_yaml(path: &Path) -> Result<Map<String, Json>, ConfigError> {
    if !path.exists() {
        return Ok(Map::new());
    }
    let text = std::fs::read_to_string(path).map_err(|e| invalid(format!("{}: cannot read: {e}", path.display())))?;
    let has_content =
        text.lines().any(|l| !l.trim().is_empty() && !l.trim_start().starts_with('#') && l.trim() != "---");
    if !has_content {
        return Ok(Map::new());
    }
    let data: Json =
        serde_yaml_ng::from_str(&text).map_err(|e| invalid(format!("{}: invalid YAML: {e}", path.display())))?;
    match data {
        Json::Null => Ok(Map::new()),
        Json::Object(m) => Ok(m),
        other => Err(invalid(format!("{}: top level must be a mapping, got {}", path.display(), py_type_name(&other)))),
    }
}

fn py_type_name(v: &Json) -> &'static str {
    match v {
        Json::Null => "NoneType",
        Json::Bool(_) => "bool",
        Json::Number(n) if n.is_f64() => "float",
        Json::Number(_) => "int",
        Json::String(_) => "str",
        Json::Array(_) => "list",
        Json::Object(_) => "dict",
    }
}

fn model<T: DeserializeOwned>(what: &Path, value: Json) -> Result<T, ConfigError> {
    serde_json::from_value(value)
        .map_err(|e| invalid(format!("{}: {}", what.display(), explain_yaml11_bool(e.to_string()))))
}

/// YAML 1.1 spellings PyYAML reads as booleans; serde_yaml_ng (YAML 1.2) reads them as strings.
const YAML11_BOOLS: [&str; 10] = ["yes", "no", "on", "off", "y", "n", "true", "false", "t", "f"];

/// Turn serde's `invalid type: string "yes", expected a boolean` into an explicit message
/// (ADR-0002, D-032: same files as Python, so say what to change instead of a type error).
fn explain_yaml11_bool(msg: String) -> String {
    const HEAD: &str = "invalid type: string \"";
    if let Some(rest) = msg.strip_prefix(HEAD) {
        if let Some((word, tail)) = rest.split_once('"') {
            if tail.contains("expected a boolean") && YAML11_BOOLS.contains(&word.to_ascii_lowercase().as_str()) {
                return format!(
                    "{word:?} is a YAML 1.1 boolean spelling that this engine does not accept; use true or false{}",
                    tail.find(" at line").map(|i| &tail[i..]).unwrap_or("")
                );
            }
        }
    }
    msg
}

pub fn load_devices(path: &Path) -> Result<DevicesFile, ConfigError> {
    if !path.exists() {
        return Err(ConfigError::NotFound(format!(
            "{} not found: run `opencap import-kep project.json` first",
            path.display()
        )));
    }
    let raw = std::fs::read(path).map_err(|e| invalid(format!("{}: cannot read: {e}", path.display())))?;
    let raw = raw.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(&raw); // utf-8-sig
    let data: Json =
        serde_json::from_slice(raw).map_err(|e| invalid(format!("{}: invalid JSON: {e}", path.display())))?;
    let file: DevicesFile = model(path, data)?;
    file.validate().map_err(|e| invalid(format!("{}: {e}", path.display())))?;
    Ok(file)
}

/// Returns the publish dict from the override (applied later); mutates `dev` for the rest.
fn apply_device_override(
    dev: &mut Map<String, Json>,
    ov: &DeviceOverride,
    ov_raw: &Map<String, Json>,
    warnings: &mut Vec<String>,
) -> Map<String, Json> {
    for name in DEVICE_SCALARS {
        if let Some(v) = ov_raw.get(name) {
            if !v.is_null() {
                dev.insert(name.into(), v.clone());
            }
        }
    }
    if !ov.params.is_empty() {
        let params = dev.entry("params").or_insert_with(|| Json::Object(Map::new()));
        if let Json::Object(p) = params {
            for (k, v) in &ov.params {
                p.insert(k.clone(), v.clone());
            }
        }
    }
    if !ov.tags.is_empty() {
        let dev_id = dev.get("id").and_then(Json::as_str).unwrap_or_default().to_string();
        let tags_raw = ov_raw.get("tags").and_then(Json::as_object).cloned().unwrap_or_default();
        let tags = dev.get_mut("tags").and_then(Json::as_array_mut);
        if let Some(tags) = tags {
            for tag_name in ov.tags.keys() {
                let tag = tags
                    .iter_mut()
                    .filter_map(Json::as_object_mut)
                    .find(|t| t.get("name").and_then(Json::as_str) == Some(tag_name.as_str()));
                let Some(tag) = tag else {
                    warnings.push(format!(
                        "{OVERRIDES_FILE}: device {} has no tag {}; override ignored",
                        r(&dev_id),
                        r(tag_name)
                    ));
                    continue;
                };
                let tov = tags_raw.get(tag_name).and_then(Json::as_object).cloned().unwrap_or_default();
                for name in TAG_FIELDS {
                    if let Some(v) = tov.get(name) {
                        if v.is_null() {
                            continue;
                        }
                        let value = if name == "scale" {
                            // model_dump() of the validated Scale: every field, defaults filled in
                            ov.tags[tag_name]
                                .scale
                                .as_ref()
                                .map(|s| serde_json::to_value(s).expect("scale serializes"))
                                .unwrap_or(Json::Null)
                        } else {
                            v.clone()
                        };
                        tag.insert(name.into(), value);
                    }
                }
            }
        }
    }
    ov.publish.clone()
}

pub fn load_config(config_dir: &Path) -> Result<LoadedConfig, ConfigError> {
    let config_dir = PathBuf::from(config_dir);
    let mut warnings: Vec<String> = Vec::new();

    let poller_path = config_dir.join(POLLER_FILE);
    let poller: PollerConfig = model(&poller_path, Json::Object(read_yaml(&poller_path)?))?;
    poller.mqtt.validate().map_err(|e| invalid(format!("{}: mqtt: {e}", poller_path.display())))?;
    poller.api.validate().map_err(|e| invalid(format!("{}: api: {e}", poller_path.display())))?;
    let devices_file = load_devices(&config_dir.join(DEVICES_FILE))?;
    let ov_path = config_dir.join(OVERRIDES_FILE);
    let overrides_raw = read_yaml(&ov_path)?;
    let overrides: Overrides = model(&ov_path, Json::Object(overrides_raw.clone()))?;
    let map_path = config_dir.join(MAP_FILE);
    let device_map: DeviceMap = model(&map_path, Json::Object(read_yaml(&map_path)?))?;
    let overrides_dev_raw: Map<String, Json> =
        overrides_raw.get("devices").and_then(Json::as_object).cloned().unwrap_or_default();

    let known_channels: std::collections::HashSet<&str> =
        devices_file.channels.iter().map(|c| c.name.as_str()).collect();
    let known_devices: std::collections::HashSet<&str> =
        devices_file.channels.iter().flat_map(|c| c.devices.iter().map(|d| d.id.as_str())).collect();

    for (name, settings) in &overrides.channels {
        if !known_channels.contains(name.as_str()) {
            warnings.push(format!("{OVERRIDES_FILE}: unknown channel {}; override ignored", r(name)));
        }
        for key in settings.keys() {
            if key != "enabled" {
                warnings.push(format!("{OVERRIDES_FILE}: channel {}: unknown setting {} ignored", r(name), r(key)));
            }
        }
    }
    for dev_id in overrides.devices.keys() {
        if !known_devices.contains(dev_id.as_str()) {
            warnings.push(format!("{OVERRIDES_FILE}: unknown device id {}; override ignored", r(dev_id)));
        }
    }
    for dev_id in device_map.devices.keys() {
        if !known_devices.contains(dev_id.as_str()) {
            warnings.push(format!("{MAP_FILE}: unknown device id {}; entry ignored", r(dev_id)));
        }
    }

    let mut map_defaults = Map::new();
    for (key, value) in &device_map.defaults {
        if PUBLISH_FIELDS.contains(&key.as_str()) {
            map_defaults.insert(key.clone(), value.clone());
        } else {
            warnings.push(format!("{MAP_FILE}: defaults.{key} is not a publish setting; ignored"));
        }
    }

    let mut channels: Vec<Channel> = Vec::new();
    let mut unmapped: Vec<String> = Vec::new();
    let mut stubs: Vec<String> = Vec::new();
    for ch in &devices_file.channels {
        let mut ch_enabled = ch.enabled;
        if let Some(ch_ov) = overrides.channels.get(&ch.name) {
            if let Some(enabled) = ch_ov.get("enabled") {
                ch_enabled = *enabled;
            }
        }
        let mut devices: Vec<Device> = Vec::new();
        for device in &ch.devices {
            let Json::Object(mut dev) = serde_json::to_value(device).expect("device serializes") else {
                unreachable!("a device serializes to an object")
            };
            let mut publish = dev.get("publish").and_then(Json::as_object).cloned().unwrap_or_default();
            for (k, v) in &map_defaults {
                publish.insert(k.clone(), v.clone());
            }
            let ov = overrides.devices.get(&device.id);
            if let Some(ov) = ov {
                let ov_raw = overrides_dev_raw.get(&device.id).and_then(Json::as_object).cloned().unwrap_or_default();
                let ov_publish = apply_device_override(&mut dev, ov, &ov_raw, &mut warnings);
                for (k, v) in ov_publish {
                    publish.insert(k, v);
                }
            }
            match device_map.devices.get(&device.id) {
                None => unmapped.push(device.id.clone()),
                Some(entry) => {
                    if entry.stub {
                        stubs.push(device.id.clone());
                    }
                    publish.insert("sub_device_id".into(), Json::String(entry.sub_device_id.clone()));
                    for (key, value) in [("topic", &entry.topic), ("env", &entry.env)] {
                        let Some(value) = value else { continue };
                        if let Some(ov) = ov {
                            if let Some(ov_value) = ov.publish.get(key) {
                                if ov_value != &Json::String(value.clone()) {
                                    warnings.push(format!(
                                        "{}: publish.{key} set in both {OVERRIDES_FILE} and {MAP_FILE}; using {MAP_FILE}",
                                        device.id
                                    ));
                                }
                            }
                        }
                        publish.insert(key.into(), Json::String(value.clone()));
                    }
                }
            }
            dev.insert("publish".into(), Json::Object(publish));
            let mut new: Device =
                serde_json::from_value(Json::Object(dev)).map_err(|e| invalid(format!("{}: {e}", device.id)))?;
            new.validate().map_err(invalid)?;
            if new.publish.topic.is_none() {
                new.publish.topic = Some(default_topic(&new, &poller.mqtt.topic_prefix));
            }
            devices.push(new);
        }
        channels.push(Channel { name: ch.name.clone(), enabled: ch_enabled, devices });
    }

    if !unmapped.is_empty() {
        let where_ =
            if !map_path.exists() { format!("{MAP_FILE} is missing") } else { format!("no entry in {MAP_FILE}") };
        warnings.push(format!("{} device(s) without SubDeviceId ({where_}): {}", unmapped.len(), ids_text(&unmapped)));
    }
    if !stubs.is_empty() {
        warnings.push(format!(
            "{} device(s) use a stub SubDeviceId (stub: true in {MAP_FILE}); confirm before go-live: {}",
            stubs.len(),
            ids_text(&stubs)
        ));
    }

    let all: Vec<&Device> = channels.iter().flat_map(|c| c.devices.iter()).collect();
    for (what, attr) in [("MQTT topic", "topic"), ("SubDeviceId", "sub_device_id")] {
        let mut users: indexmap::IndexMap<String, Vec<String>> = Default::default();
        for d in &all {
            let value = if attr == "topic" { &d.publish.topic } else { &d.publish.sub_device_id };
            if let Some(v) = value {
                if !v.is_empty() {
                    users.entry(v.clone()).or_default().push(d.id.clone());
                }
            }
        }
        for (value, ids) in users {
            if ids.len() > 1 {
                warnings.push(format!("{what} {} is used by {} devices: {}", r(&value), ids.len(), ids_text(&ids)));
            }
        }
    }

    let validated = DevicesFile {
        version: devices_file.version,
        generated_from: devices_file.generated_from.clone(),
        generated_at: devices_file.generated_at.clone(),
        channels,
        excluded: devices_file.excluded.clone(),
    };
    validated.validate().map_err(invalid)?;
    Ok(LoadedConfig { poller, channels: validated.channels, excluded: validated.excluded, warnings })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn unknown_driver_is_a_config_error_with_pydantic_shape() {
        let d = dir("unknown-driver");
        let mut j = devices_json();
        j["channels"][0]["devices"][0]["driver"] = json!("profibus");
        std::fs::write(d.join(DEVICES_FILE), j.to_string()).unwrap();
        let e = load_config(&d).unwrap_err().to_string();
        assert!(e.contains("Site 001/8 PUMP: driver: Input should be 'modbus_tcp', 'ab_logix'"), "{e}");
        assert!(e.contains("or 'ab_df1' (got 'profibus')"), "{e}");
    }

    #[test]
    fn known_but_unimplemented_driver_still_loads() {
        let d = dir("known-driver");
        let mut j = devices_json();
        j["channels"][0]["devices"][0]["driver"] = json!("s7");
        std::fs::write(d.join(DEVICES_FILE), j.to_string()).unwrap();
        assert!(load_config(&d).is_ok());
    }

    #[test]
    fn yaml_1_1_boolean_spellings_are_named() {
        for word in ["yes", "No", "on", "off"] {
            let d = dir(&format!("yaml11-{word}"));
            std::fs::write(d.join(DEVICES_FILE), devices_json().to_string()).unwrap();
            std::fs::write(d.join(OVERRIDES_FILE), format!("devices:\n  Site 001/8 PUMP: {{enabled: {word}}}\n"))
                .unwrap();
            let e = load_config(&d).unwrap_err().to_string();
            assert!(e.contains(&format!("{word:?} is a YAML 1.1 boolean spelling")), "{word}: {e}");
            assert!(e.contains("use true or false"), "{e}");
        }
    }

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("opencap-loader-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn devices_json() -> Json {
        json!({"channels": [{"name": "Site 001", "devices": [
            {"id": "Site 001/8 PUMP", "channel": "Site 001", "name": "8 PUMP", "driver": "modbus_tcp",
             "host": "192.0.2.1", "port": 502,
             "tags": [{"name": "FLOW_1", "address": "400045", "dtype": "float32"},
                      {"name": "LEVEL_1", "address": "400047", "dtype": "float32"}]},
            {"id": "Site 001/9 PUMP", "channel": "Site 001", "name": "9 PUMP", "driver": "modbus_tcp",
             "host": "192.0.2.2", "port": 502, "publish": {"env": "test", "topic": "x/y"},
             "tags": [{"name": "FLOW_1", "address": "400045", "dtype": "float32"}]}
        ]}]})
    }

    #[test]
    fn layering_and_warnings_match_python() {
        let d = dir("layering");
        std::fs::write(d.join(DEVICES_FILE), format!("\u{feff}{}", devices_json())).unwrap();
        std::fs::write(
            d.join(OVERRIDES_FILE),
            "channels:\n  Nope: {enabled: false}\n  Site 001: {enabled: true, colour: true}\n\
             devices:\n  Site 001/8 PUMP:\n    timeout_ms: 2000\n    params: {max_holding: 32}\n\
             \x20   publish: {mode: on_change, env: staging}\n    tags:\n      FLOW_1: {scale: {raw_hi: 10}}\n\
             \x20     GHOST: {enabled: false}\n  X/Y: {enabled: false}\n",
        )
        .unwrap();
        std::fs::write(
            d.join(MAP_FILE),
            "defaults: {env: prod, colour: blue}\ndevices:\n  Site 001/8 PUMP: {sub_device_id: SUB_8, env: live}\n  \
             Site 001/9 PUMP: {sub_device_id: SUB_8, stub: true}\n  Gone/Dev: {sub_device_id: G}\n",
        )
        .unwrap();
        let cfg = load_config(&d).unwrap();
        let d8 = &cfg.channels[0].devices[0];
        assert_eq!(d8.timeout_ms, 2000);
        assert_eq!(d8.params["max_holding"], json!(32));
        assert_eq!(d8.publish.mode, PublishMode::OnChange);
        assert_eq!(d8.publish.env, "live");
        assert_eq!(d8.publish.sub_device_id.as_deref(), Some("SUB_8"));
        assert_eq!(d8.publish.topic.as_deref(), Some("opencap/site_001/8_pump/data"));
        assert_eq!(d8.tags[0].scale.as_ref().unwrap().raw_hi, 10.0);
        let d9 = &cfg.channels[0].devices[1];
        assert_eq!(d9.publish.env, "prod"); // map defaults beat devices.json publish
        assert_eq!(d9.publish.topic.as_deref(), Some("x/y"));
        assert_eq!(cfg.warnings, vec![
            "overrides.yaml: unknown channel 'Nope'; override ignored",
            "overrides.yaml: channel 'Site 001': unknown setting 'colour' ignored",
            "overrides.yaml: unknown device id 'X/Y'; override ignored",
            "devices_map.yaml: unknown device id 'Gone/Dev'; entry ignored",
            "devices_map.yaml: defaults.colour is not a publish setting; ignored",
            "overrides.yaml: device 'Site 001/8 PUMP' has no tag 'GHOST'; override ignored",
            "Site 001/8 PUMP: publish.env set in both overrides.yaml and devices_map.yaml; using devices_map.yaml",
            "1 device(s) use a stub SubDeviceId (stub: true in devices_map.yaml); confirm before go-live: Site 001/9 PUMP",
            "SubDeviceId 'SUB_8' is used by 2 devices: Site 001/8 PUMP, Site 001/9 PUMP",
        ]);
    }

    #[test]
    fn missing_files_and_errors() {
        let d = dir("missing");
        assert!(matches!(load_config(&d), Err(ConfigError::NotFound(_))));
        std::fs::write(d.join(DEVICES_FILE), devices_json().to_string()).unwrap();
        let cfg = load_config(&d).unwrap();
        assert_eq!(cfg.poller, PollerConfig::default());
        assert_eq!(cfg.warnings.len(), 1);
        assert!(cfg.warnings[0].starts_with("2 device(s) without SubDeviceId (devices_map.yaml is missing)"));
        std::fs::write(d.join(POLLER_FILE), "# only a comment\n").unwrap();
        assert!(load_config(&d).is_ok());
        std::fs::write(d.join(POLLER_FILE), "- a\n- b\n").unwrap();
        let err = load_config(&d).unwrap_err().to_string();
        assert!(err.contains("top level must be a mapping, got list"), "{err}");
        std::fs::write(d.join(POLLER_FILE), "mqtt: {hots: x}\n").unwrap();
        assert!(matches!(load_config(&d), Err(ConfigError::Invalid(_))));
        std::fs::write(d.join(POLLER_FILE), "").unwrap();
        std::fs::write(d.join(DEVICES_FILE), "{not json").unwrap();
        assert!(load_config(&d).unwrap_err().to_string().contains("invalid JSON"));
    }

    #[test]
    fn topic_prefix_applies_to_default_topics_only() {
        let d = dir("prefix");
        std::fs::write(d.join(DEVICES_FILE), devices_json().to_string()).unwrap();
        std::fs::write(d.join(POLLER_FILE), "mqtt: {topic_prefix: Acme/plant-1}\n").unwrap();
        let cfg = load_config(&d).unwrap();
        assert_eq!(cfg.channels[0].devices[0].publish.topic.as_deref(), Some("Acme/plant-1/site_001/8_pump/data"));
        assert_eq!(cfg.channels[0].devices[1].publish.topic.as_deref(), Some("x/y")); // explicit topic kept
        std::fs::write(d.join(POLLER_FILE), "mqtt: {topic_prefix: acme//x}\n").unwrap();
        let err = load_config(&d).unwrap_err().to_string();
        assert!(err.contains("topic_prefix 'acme//x' must not start or end with '/' or contain '//'"), "{err}");
    }

    #[test]
    fn ids_text_truncates_like_python() {
        let ids: Vec<String> = (0..7).map(|i| format!("D{i}")).collect();
        assert_eq!(ids_text(&ids), "D0, D1, D2, D3, D4, … (+2 more)");
    }
}
