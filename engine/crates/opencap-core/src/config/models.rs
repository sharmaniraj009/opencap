//! Configuration models: the SAME schema as `src/opencap/config/models.py` (pydantic,
//! `extra="forbid"`), so `devices.json`, `devices_map.yaml`, `overrides.yaml` and
//! `poller.yaml` are interchangeable between the Python and the Rust engine.
//!
//! Differences that are deliberate (see docs/rfc/0001-rust-engine.md, "Config"):
//! - `Device.driver` is a string checked against `KNOWN_DRIVERS` (Python's `DriverName`
//!   literal): an unknown name is a config error, as in Python. A known driver this build
//!   does not implement yet loads fine and is reported as "not implemented".
//! - Typing is serde's: no pydantic "lax mode" coercion such as `"5"` -> `5`.

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value as Json};

use crate::pyfmt::{py_repr_list, py_repr_str};

/// Each device's diagnostics live under `<channel>.<device>._System` (OPC UA server, D-046), so
/// no tag or tag group may use that name (compared without case). Python `RESERVED_GROUP`.
pub const RESERVED_GROUP: &str = "_System";
/// System-tag folders (docs/spec/system-tags.md 3.2): Python `RESERVED_CHANNEL_NAMES`,
/// `RESERVED_DEVICE_NAMES`, `RESERVED_TAG_GROUPS`. Compared without case.
pub const RESERVED_CHANNEL_NAMES: [&str; 1] = ["_System"];
pub const RESERVED_DEVICE_NAMES: [&str; 2] = ["_System", "_Statistics"];
pub const RESERVED_TAG_GROUPS: [&str; 2] = ["_System", "_Statistics"];

/// The reserved word `name` equals without regard to case (Python `reserved_word`).
pub fn reserved_word(name: &str, reserved: &[&'static str]) -> Option<&'static str> {
    reserved.iter().copied().find(|w| w.eq_ignore_ascii_case(name))
}

/// Names with a segment starting with `_` (the warning of spec 3.2), in file order. Python
/// `underscore_names`.
pub fn underscore_names(channels: &[Channel]) -> Vec<String> {
    let mut out = Vec::new();
    for ch in channels {
        if ch.name.starts_with('_') {
            out.push(format!("channel name {} starts with '_'", py_repr_str(&ch.name)));
        }
        for dev in &ch.devices {
            if dev.name.starts_with('_') {
                out.push(format!("{}: device name {} starts with '_'", dev.id, py_repr_str(&dev.name)));
            }
            for tag in &dev.tags {
                if tag.name.split('.').any(|seg| seg.starts_with('_')) {
                    out.push(format!(
                        "{}: tag name {} has a segment starting with '_'",
                        dev.id,
                        py_repr_str(&tag.name)
                    ));
                }
            }
        }
    }
    out
}

/// Device names the REST paths use as sub-resources. Python `API_DEVICE_NAMES`.
pub const API_DEVICE_NAMES: [&str; 6] = ["tags", "values", "plan", "identity", "status", "read-once"];

/// Rule violations of one name (PRJ-13). Python `_name_faults`.
fn name_faults(name: &str, what: &str, dotted: bool) -> Vec<String> {
    if name.is_empty() {
        return vec![format!("{what} name is empty")];
    }
    let mut out = Vec::new();
    // Python `str.strip()` trims Unicode whitespace, as `str::trim` does.
    if name != name.trim() {
        out.push(format!("{what} name {} has leading or trailing whitespace", py_repr_str(name)));
    }
    if name.contains('"') {
        out.push(format!("{what} name {} contains '\"'", py_repr_str(name)));
    }
    if dotted {
        for ch in [".", "/"] {
            if name.contains(ch) {
                out.push(format!("{what} name {} contains {}", py_repr_str(name), py_repr_str(ch)));
            }
        }
    }
    out
}

/// Names that break the naming rules other than the `_` one (PRJ-13), in file order, with the
/// same texts as Python `name_rule_problems` (the `loc` is Python's and is not kept here).
pub fn name_rule_problems(channels: &[Channel]) -> Vec<String> {
    let mut out = Vec::new();
    for ch in channels {
        out.extend(name_faults(&ch.name, "channel", true));
        for dev in &ch.devices {
            out.extend(name_faults(&dev.name, "device", true).into_iter().map(|t| format!("{}: {t}", dev.id)));
            if API_DEVICE_NAMES.contains(&dev.name.as_str()) {
                out.push(format!("{}: device name {} is used by the REST paths", dev.id, py_repr_str(&dev.name)));
            }
            for tag in &dev.tags {
                if tag.name.is_empty() {
                    out.push(format!("{}: tag name is empty", dev.id));
                    continue;
                }
                for seg in tag.name.split('.') {
                    out.extend(
                        name_faults(seg, "tag segment", false)
                            .into_iter()
                            .map(|t| format!("{}: {t} (in tag {})", dev.id, py_repr_str(&tag.name))),
                    );
                }
            }
        }
    }
    out
}

/// An IANA zone name the payload can use. Python `is_known_zone`.
pub fn is_known_zone(name: &str) -> bool {
    name.parse::<chrono_tz::Tz>().is_ok()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DType {
    Bool,
    Int16,
    Uint16,
    Int32,
    Uint32,
    Int64,
    Uint64,
    Float32,
    Float64,
    String,
}

impl DType {
    pub fn as_str(&self) -> &'static str {
        match self {
            DType::Bool => "bool",
            DType::Int16 => "int16",
            DType::Uint16 => "uint16",
            DType::Int32 => "int32",
            DType::Uint32 => "uint32",
            DType::Int64 => "int64",
            DType::Uint64 => "uint64",
            DType::Float32 => "float32",
            DType::Float64 => "float64",
            DType::String => "string",
        }
    }
}

fn d_true() -> bool {
    true
}
fn d_raw_hi() -> f64 {
    1000.0
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ScaleType {
    #[default]
    Linear,
    Sqrt,
}

/// Kepware-style tag scaling. Defaults match Kepware's defaults.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scale {
    #[serde(default, rename = "type")]
    pub kind: ScaleType,
    #[serde(default)]
    pub raw_lo: f64,
    #[serde(default = "d_raw_hi")]
    pub raw_hi: f64,
    #[serde(default)]
    pub eng_lo: f64,
    #[serde(default = "d_raw_hi")]
    pub eng_hi: f64,
    #[serde(default)]
    pub clamp_lo: bool,
    #[serde(default)]
    pub clamp_hi: bool,
    #[serde(default)]
    pub negate: bool,
    #[serde(default)]
    pub units: Option<String>,
}

impl Default for Scale {
    fn default() -> Self {
        Self {
            kind: ScaleType::Linear,
            raw_lo: 0.0,
            raw_hi: 1000.0,
            eng_lo: 0.0,
            eng_hi: 1000.0,
            clamp_lo: false,
            clamp_hi: false,
            negate: false,
            units: None,
        }
    }
}

impl Scale {
    /// Same arithmetic as `Scale.apply` in Python. Errors where Python raises ValueError.
    pub fn apply(&self, raw: f64) -> Result<f64, String> {
        let span = self.raw_hi - self.raw_lo;
        if span == 0.0 {
            return Err("scale raw_hi == raw_lo".into());
        }
        let mut frac = (raw - self.raw_lo) / span;
        if self.kind == ScaleType::Sqrt {
            frac = if frac > 0.0 { frac.sqrt() } else { 0.0 };
        }
        let mut value = frac * (self.eng_hi - self.eng_lo) + self.eng_lo;
        let (lo, hi) = if self.eng_lo <= self.eng_hi { (self.eng_lo, self.eng_hi) } else { (self.eng_hi, self.eng_lo) };
        if self.clamp_lo && value < lo {
            value = lo;
        }
        if self.clamp_hi && value > hi {
            value = hi;
        }
        Ok(if self.negate { -value } else { value })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tag {
    pub name: String,
    pub address: String,
    pub dtype: DType,
    #[serde(default)]
    pub scan_ms: Option<i64>,
    #[serde(default)]
    pub scale: Option<Scale>,
    #[serde(default = "d_true")]
    pub enabled: bool,
    #[serde(default)]
    pub description: Option<String>,
}

impl Tag {
    pub fn new(name: &str, address: &str, dtype: DType) -> Self {
        Self {
            name: name.into(),
            address: address.into(),
            dtype,
            scan_ms: None,
            scale: None,
            enabled: true,
            description: None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishMode {
    #[default]
    Interval,
    OnChange,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishFormat {
    #[default]
    SubdeviceJson,
    IotgwJson,
}

fn d_env() -> String {
    "prod".into()
}
fn d_interval() -> i64 {
    10_000
}
fn d_heartbeat() -> i64 {
    300_000
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishCfg {
    #[serde(default)]
    pub sub_device_id: Option<String>,
    #[serde(default)]
    pub topic: Option<String>,
    #[serde(default = "d_env")]
    pub env: String,
    #[serde(default)]
    pub mode: PublishMode,
    #[serde(default = "d_interval")]
    pub interval_ms: i64,
    #[serde(default = "d_heartbeat")]
    pub heartbeat_ms: i64,
    #[serde(default)]
    pub deadband: f64,
    /// Message body format (D-072). Accepted and kept; the MQTT plugin does not dispatch on it yet (#170).
    #[serde(default)]
    pub format: PublishFormat,
}

impl Default for PublishCfg {
    fn default() -> Self {
        Self {
            sub_device_id: None,
            topic: None,
            env: d_env(),
            mode: PublishMode::Interval,
            interval_ms: d_interval(),
            heartbeat_ms: d_heartbeat(),
            deadband: 0.0,
            format: PublishFormat::SubdeviceJson,
        }
    }
}

/// The publish settings' field names (Python `PublishCfg.model_fields`).
pub const PUBLISH_FIELDS: [&str; 8] =
    ["sub_device_id", "topic", "env", "mode", "interval_ms", "heartbeat_ms", "deadband", "format"];

fn d_1000() -> i64 {
    1000
}
fn d_3000() -> i64 {
    3000
}
fn d_2() -> i64 {
    2
}
fn d_3() -> i64 {
    3
}
fn d_30000() -> i64 {
    30_000
}

/// Drivers the Python reference engine knows (its `DriverName` literal, same order).
pub const KNOWN_DRIVERS: [&str; 10] = [
    "modbus_tcp",
    "ab_logix",
    "ab_pccc",
    "s7",
    "melsec_mc",
    "opcua_client",
    "modbus_rtu",
    "ab_df1",
    "simulator",
    "omron_fins",
];

/// pydantic's message shape for the `DriverName` literal.
pub fn driver_name_error(value: &str) -> String {
    use crate::pyfmt::py_repr_str as q;
    let quoted: Vec<String> = KNOWN_DRIVERS.iter().map(|d| q(d)).collect();
    let (last, head) = quoted.split_last().expect("non-empty");
    format!("driver: Input should be {} or {} (got {})", head.join(", "), last, q(value))
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Device {
    pub id: String,
    pub channel: String,
    pub name: String,
    pub driver: String,
    pub host: String,
    pub port: i64,
    #[serde(default)]
    pub params: Map<String, Json>,
    #[serde(default = "d_1000")]
    pub scan_ms: i64,
    #[serde(default = "d_1000")]
    pub min_scan_ms: i64,
    #[serde(default = "d_3000")]
    pub timeout_ms: i64,
    #[serde(default = "d_2")]
    pub retries: i64,
    #[serde(default = "d_3")]
    pub demote_after: i64,
    #[serde(default = "d_30000")]
    pub demote_for_ms: i64,
    #[serde(default = "d_true")]
    pub enabled: bool,
    #[serde(default)]
    pub publish: PublishCfg,
    pub tags: Vec<Tag>,
    #[serde(default)]
    pub source: Map<String, Json>,
}

impl Device {
    /// A device with the model defaults (for tests and tools).
    pub fn new(id: &str, channel: &str, name: &str, driver: &str, host: &str, port: i64, tags: Vec<Tag>) -> Self {
        Self {
            id: id.into(),
            channel: channel.into(),
            name: name.into(),
            driver: driver.into(),
            host: host.into(),
            port,
            params: Map::new(),
            scan_ms: 1000,
            min_scan_ms: 1000,
            timeout_ms: 3000,
            retries: 2,
            demote_after: 3,
            demote_for_ms: 30_000,
            enabled: true,
            publish: PublishCfg::default(),
            tags,
            source: Map::new(),
        }
    }

    /// pydantic's `_unique_tag_names` validator (duplicates, then the reserved `_System` group).
    pub fn validate(&self) -> Result<(), String> {
        if !KNOWN_DRIVERS.contains(&self.driver.as_str()) {
            return Err(format!("{}: {}", self.id, driver_name_error(&self.driver)));
        }
        let mut seen = std::collections::HashSet::new();
        let mut dupes: Vec<String> =
            self.tags.iter().filter(|t| !seen.insert(t.name.as_str())).map(|t| t.name.clone()).collect();
        dupes.sort();
        dupes.dedup();
        if !dupes.is_empty() {
            return Err(format!("{}: duplicate tag names {}", self.id, py_repr_list(&dupes)));
        }
        if let Some(word) = reserved_word(&self.name, &RESERVED_DEVICE_NAMES) {
            return Err(format!(
                "{}: device name {} is the reserved word {} (system-tag folder, docs/spec/system-tags.md 3.2)",
                self.id,
                py_repr_str(&self.name),
                py_repr_str(word)
            ));
        }
        for word in RESERVED_TAG_GROUPS {
            let mut reserved: Vec<String> = self
                .tags
                .iter()
                .filter(|t| t.name.split('.').next().unwrap_or("").eq_ignore_ascii_case(word))
                .map(|t| t.name.clone())
                .collect();
            reserved.sort();
            if !reserved.is_empty() {
                let why = if word == RESERVED_GROUP {
                    "the per-device diagnostics folder of the OPC UA server, D-046"
                } else {
                    "system-tag folder, docs/spec/system-tags.md 3.2"
                };
                return Err(format!(
                    "{}: tag names {} use the reserved group name {} ({why})",
                    self.id,
                    py_repr_list(&reserved),
                    py_repr_str(word)
                ));
            }
        }
        Ok(())
    }

    /// `max(tag.scan_ms or device.scan_ms, device.min_scan_ms)` (Python `or`: 0 means unset).
    pub fn effective_rate_ms(&self, tag: &Tag) -> i64 {
        let rate = match tag.scan_ms {
            Some(v) if v != 0 => v,
            _ => self.scan_ms,
        };
        rate.max(self.min_scan_ms)
    }

    /// Enabled tags grouped by effective scan rate, fastest first.
    pub fn rate_groups(&self) -> Vec<(i64, Vec<&Tag>)> {
        let mut groups: std::collections::BTreeMap<i64, Vec<&Tag>> = Default::default();
        for tag in self.tags.iter().filter(|t| t.enabled) {
            groups.entry(self.effective_rate_ms(tag)).or_default().push(tag);
        }
        groups.into_iter().collect()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Channel {
    pub name: String,
    #[serde(default = "d_true")]
    pub enabled: bool,
    pub devices: Vec<Device>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExcludedDevice {
    pub channel: String,
    pub device: String,
    pub kep_driver: String,
    #[serde(default)]
    pub tags: i64,
    pub reason: String,
}

fn d_version() -> i64 {
    1
}

/// Schema of `config/devices.json`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DevicesFile {
    #[serde(default = "d_version")]
    pub version: i64,
    #[serde(default)]
    pub generated_from: Option<String>,
    #[serde(default)]
    pub generated_at: Option<String>,
    pub channels: Vec<Channel>,
    #[serde(default)]
    pub excluded: Vec<ExcludedDevice>,
}

impl DevicesFile {
    pub fn validate(&self) -> Result<(), String> {
        let mut seen = std::collections::HashSet::new();
        for ch in &self.channels {
            if let Some(word) = reserved_word(&ch.name, &RESERVED_CHANNEL_NAMES) {
                return Err(format!(
                    "channel name {} is the reserved word {} (system-tag folder, docs/spec/system-tags.md 3.2)",
                    py_repr_str(&ch.name),
                    py_repr_str(word)
                ));
            }
            for dev in &ch.devices {
                dev.validate()?;
                if !seen.insert(dev.id.as_str()) {
                    return Err(format!("duplicate device id {}", crate::pyfmt::py_repr_str(&dev.id)));
                }
            }
        }
        Ok(())
    }
}

// --- devices_map.yaml -------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceMapEntry {
    pub sub_device_id: String,
    #[serde(default)]
    pub topic: Option<String>,
    #[serde(default)]
    pub env: Option<String>,
    #[serde(default)]
    pub stub: bool,
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceMap {
    #[serde(default)]
    pub defaults: IndexMap<String, Json>,
    #[serde(default)]
    pub devices: IndexMap<String, DeviceMapEntry>,
}

// --- overrides.yaml ---------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TagOverride {
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub address: Option<String>,
    #[serde(default)]
    pub dtype: Option<DType>,
    #[serde(default)]
    pub scan_ms: Option<i64>,
    #[serde(default)]
    pub scale: Option<Scale>,
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceOverride {
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub host: Option<String>,
    #[serde(default)]
    pub port: Option<i64>,
    #[serde(default)]
    pub scan_ms: Option<i64>,
    #[serde(default)]
    pub min_scan_ms: Option<i64>,
    #[serde(default)]
    pub timeout_ms: Option<i64>,
    #[serde(default)]
    pub retries: Option<i64>,
    #[serde(default)]
    pub demote_after: Option<i64>,
    #[serde(default)]
    pub demote_for_ms: Option<i64>,
    #[serde(default)]
    pub params: Map<String, Json>,
    #[serde(default)]
    pub publish: Map<String, Json>,
    #[serde(default)]
    pub tags: IndexMap<String, TagOverride>,
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Overrides {
    #[serde(default)]
    pub channels: IndexMap<String, IndexMap<String, bool>>,
    #[serde(default)]
    pub devices: IndexMap<String, DeviceOverride>,
    /// KEPServer COM port -> where opencap reaches that line (serial drivers with
    /// `transport: unmapped`): `"COM112": "192.0.2.50:4001"` (raw TCP gateway),
    /// `"rfc2217://host:port"`, `"/dev/ttyUSB0"`.
    #[serde(default)]
    pub serial_ports: IndexMap<String, String>,
}

// --- poller.yaml ------------------------------------------------------------------

fn d_localhost() -> String {
    "localhost".into()
}
fn d_1883() -> i64 {
    1883
}
fn d_pw_env() -> Option<String> {
    Some("OPENCAP_MQTT_PASSWORD".into())
}
fn d_qos() -> i64 {
    1
}
fn d_keepalive() -> i64 {
    30
}
/// `poller.yaml` `mqtt.topic_prefix` default.
pub const DEFAULT_TOPIC_PREFIX: &str = "opencap";
fn d_topic_prefix() -> String {
    DEFAULT_TOPIC_PREFIX.into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MqttCfg {
    #[serde(default = "d_localhost")]
    pub host: String,
    #[serde(default = "d_1883")]
    pub port: i64,
    #[serde(default)]
    pub tls: bool,
    #[serde(default)]
    pub ca_file: Option<String>,
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default = "d_pw_env")]
    pub password_env: Option<String>,
    #[serde(default)]
    pub client_id: Option<String>,
    #[serde(default = "d_qos")]
    pub qos: i64,
    #[serde(default = "d_keepalive")]
    pub keepalive_s: i64,
    /// First level(s) of every topic opencap derives (D-028): `<prefix>/<channel>/<device>/data|status`
    /// and the LWT `<prefix>/poller/<instance>/status`. Used verbatim (not slugged).
    #[serde(default = "d_topic_prefix")]
    pub topic_prefix: String,
}

impl MqttCfg {
    /// pydantic's `_check_topic_prefix` validator, same messages.
    pub fn validate(&self) -> Result<(), String> {
        let v = &self.topic_prefix;
        let r = crate::pyfmt::py_repr_str(v);
        if v.trim().is_empty() {
            return Err("topic_prefix must not be empty".into());
        }
        if v.contains('+') || v.contains('#') || v.contains('\0') {
            return Err(format!("topic_prefix {r} must not contain '+', '#' or NUL"));
        }
        if v.starts_with('$') {
            return Err(format!("topic_prefix {r} must not start with '$' (reserved by brokers)"));
        }
        if v.split('/').any(str::is_empty) {
            return Err(format!("topic_prefix {r} must not start or end with '/' or contain '//'"));
        }
        Ok(())
    }
}

impl Default for MqttCfg {
    fn default() -> Self {
        serde_json::from_value(Json::Object(Map::new())).expect("defaults")
    }
}

fn d_saf_path() -> String {
    "data/saf.sqlite".into()
}
fn d_saf_max() -> i64 {
    2 * 1024 * 1024 * 1024
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SafCfg {
    #[serde(default = "d_saf_path")]
    pub path: String,
    #[serde(default = "d_saf_max")]
    pub max_bytes: i64,
}

fn d_bind() -> String {
    "127.0.0.1".into()
}
fn d_8080() -> i64 {
    8080
}
fn d_token_env() -> String {
    "OPENCAP_API_TOKEN".into()
}

fn d_audit_log() -> String {
    "data/audit.jsonl".into()
}

/// What a token may do: `viewer` = safe methods only (GET/HEAD), `admin` = also changes
/// (reload, read-once, config writes). Python `ApiToken.role`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    #[default]
    Viewer,
    Admin,
}

impl Role {
    pub fn as_str(&self) -> &'static str {
        match self {
            Role::Viewer => "viewer",
            Role::Admin => "admin",
        }
    }
}

/// A named API token. The secret lives in the env var `token_env`, never in yaml.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApiToken {
    /// Shown in the audit log as "who".
    pub name: String,
    #[serde(default)]
    pub role: Role,
    pub token_env: String,
}

/// The legacy `api.token_env` token's name (it has role admin).
pub const LEGACY_TOKEN_NAME: &str = "default";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApiCfg {
    #[serde(default = "d_bind")]
    pub bind: String,
    #[serde(default = "d_8080")]
    pub port: i64,
    /// Legacy single token: name "default", role admin.
    #[serde(default = "d_token_env")]
    pub token_env: String,
    #[serde(default)]
    pub tokens: Vec<ApiToken>,
    /// Config-change audit log (JSON lines). The Rust engine has no config API yet, so it
    /// accepts the setting and does not write the file.
    #[serde(default = "d_audit_log")]
    pub audit_log: String,
    /// Other web origins allowed to call the API (desktop app). Accepted for parity; the Rust
    /// engine sends no CORS headers yet.
    #[serde(default)]
    pub cors_origins: Vec<String>,
}

impl ApiCfg {
    /// pydantic's `min_length=1` on `ApiToken` fields and the `_unique_token_names` validator,
    /// same messages.
    pub fn validate(&self) -> Result<(), String> {
        for (i, t) in self.tokens.iter().enumerate() {
            for (field, value) in [("name", &t.name), ("token_env", &t.token_env)] {
                if value.is_empty() {
                    return Err(format!("tokens.{i}.{field}: String should have at least 1 character"));
                }
            }
        }
        let names: Vec<&str> = self.tokens.iter().map(|t| t.name.as_str()).collect();
        if names.contains(&LEGACY_TOKEN_NAME) {
            return Err("api.tokens: the name 'default' is reserved for api.token_env".into());
        }
        let mut dupes: Vec<String> =
            names.iter().filter(|n| names.iter().filter(|m| m == n).count() > 1).map(|n| n.to_string()).collect();
        dupes.sort();
        dupes.dedup();
        if dupes.is_empty() {
            Ok(())
        } else {
            Err(format!("api.tokens: duplicate names {}", py_repr_list(&dupes)))
        }
    }
}

fn d_decimals() -> i64 {
    4
}
fn d_tz() -> String {
    "Asia/Kolkata".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PayloadCfg {
    #[serde(default = "d_decimals")]
    pub float_decimals: i64,
    #[serde(default = "d_true")]
    pub bad_tags_field: bool,
    #[serde(default = "d_tz")]
    pub timezone: String,
}

impl PayloadCfg {
    /// Python's `PayloadCfg._known_zone` validator, same message.
    pub fn validate(&self) -> Result<(), String> {
        if !is_known_zone(&self.timezone) {
            return Err(format!("payload.timezone {} is not a known IANA time zone name", py_repr_str(&self.timezone)));
        }
        Ok(())
    }
}

impl Default for PayloadCfg {
    fn default() -> Self {
        Self { float_decimals: 4, bad_tags_field: true, timezone: d_tz() }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceDefaults {
    #[serde(default = "d_1000")]
    pub scan_ms: i64,
    #[serde(default = "d_1000")]
    pub min_scan_ms: i64,
    #[serde(default = "d_3000")]
    pub timeout_ms: i64,
    #[serde(default = "d_2")]
    pub retries: i64,
    #[serde(default = "d_3")]
    pub demote_after: i64,
    #[serde(default = "d_30000")]
    pub demote_for_ms: i64,
}

fn d_rtt() -> f64 {
    60.0
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckCfg {
    #[serde(default = "d_rtt")]
    pub est_rtt_ms: f64,
}

/// Python `OpcUaServerPolicy`.
pub const OPCUA_SERVER_POLICIES: [&str; 7] = [
    "None",
    "Basic256Sha256_Sign",
    "Basic256Sha256_SignAndEncrypt",
    "Aes128_Sha256_RsaOaep_Sign",
    "Aes128_Sha256_RsaOaep_SignAndEncrypt",
    "Aes256_Sha256_RsaPss_Sign",
    "Aes256_Sha256_RsaPss_SignAndEncrypt",
];

fn d_ua_endpoint() -> String {
    "opc.tcp://127.0.0.1:4840/opencap".into()
}
fn d_ua_server_name() -> String {
    "opencap".into()
}
fn d_ua_application_uri() -> String {
    "urn:opencap:server".into()
}
fn d_ua_namespace_uri() -> String {
    "urn:opencap".into()
}
fn d_ua_policies() -> Vec<String> {
    vec!["None".into()]
}
fn d_250() -> i64 {
    250
}
fn d_ua_max_sessions() -> i64 {
    100
}
fn d_ua_max_subs() -> i64 {
    50
}
fn d_ua_max_items() -> i64 {
    100_000
}
fn d_ua_min_interval() -> i64 {
    100
}
fn d_ua_max_refs() -> i64 {
    1000
}

/// One OPC UA user (Python `OpcUaUserCfg`): both names are environment variables.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OpcUaUserCfg {
    pub username_env: String,
    pub password_env: String,
}

/// `poller.yaml` `opcua_server` (Python `OpcUaServerCfg`, D-046, D-088). The Rust engine has no OPC UA
/// server yet (#46): it reads and validates the section like Python, so the same file loads in
/// both engines, and warns at start when `enabled` is set.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OpcUaServerCfg {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "d_ua_endpoint")]
    pub endpoint: String,
    #[serde(default)]
    pub advertised_endpoint: Option<String>,
    #[serde(default = "d_ua_server_name")]
    pub server_name: String,
    #[serde(default = "d_ua_application_uri")]
    pub application_uri: String,
    #[serde(default = "d_ua_namespace_uri")]
    pub namespace_uri: String,
    #[serde(default = "d_true")]
    pub kepserverex_namespace_index: bool,
    #[serde(default = "d_ua_policies")]
    pub security_policies: Vec<String>,
    #[serde(default)]
    pub pki_dir: Option<String>,
    #[serde(default)]
    pub certificate: Option<String>,
    #[serde(default)]
    pub private_key: Option<String>,
    #[serde(default)]
    pub trusted_certs_dir: Option<String>,
    #[serde(default)]
    pub anonymous: bool,
    #[serde(default)]
    pub username_env: Option<String>,
    #[serde(default)]
    pub password_env: Option<String>,
    #[serde(default)]
    pub users: Vec<OpcUaUserCfg>,
    #[serde(default)]
    pub x509_users: bool,
    #[serde(default = "d_250")]
    pub update_ms: i64,
    #[serde(default)]
    pub allow_insecure_remote: bool,
    #[serde(default = "d_ua_max_sessions")]
    pub max_sessions: i64,
    #[serde(default = "d_ua_max_subs")]
    pub max_subscriptions_per_session: i64,
    #[serde(default = "d_ua_max_items")]
    pub max_monitored_items: i64,
    #[serde(default = "d_ua_min_interval")]
    pub min_publishing_interval_ms: i64,
    #[serde(default = "d_ua_min_interval")]
    pub min_sampling_interval_ms: i64,
    #[serde(default = "d_ua_max_refs")]
    pub max_references_per_browse: i64,
}

impl Default for OpcUaServerCfg {
    fn default() -> Self {
        d_obj()
    }
}

fn non_empty(v: &Option<String>) -> bool {
    v.as_deref().is_some_and(|s| !s.is_empty())
}

impl OpcUaServerCfg {
    /// Python's `Literal` check and `_check` validator, same order and messages.
    pub fn validate(&self) -> Result<(), String> {
        for (i, p) in self.security_policies.iter().enumerate() {
            if !OPCUA_SERVER_POLICIES.contains(&p.as_str()) {
                let quoted: Vec<String> = OPCUA_SERVER_POLICIES.iter().map(|x| py_repr_str(x)).collect();
                let (last, rest) = quoted.split_last().expect("not empty");
                return Err(format!(
                    "opcua_server.security_policies.{i}: Input should be {} or {last}",
                    rest.join(", ")
                ));
            }
        }
        if !self.endpoint.starts_with("opc.tcp://") {
            return Err(format!("opcua_server.endpoint {} must start with opc.tcp://", py_repr_str(&self.endpoint)));
        }
        if let Some(adv) = &self.advertised_endpoint {
            if !adv.starts_with("opc.tcp://") {
                return Err(format!(
                    "opcua_server.advertised_endpoint {} must start with opc.tcp://",
                    py_repr_str(adv)
                ));
            }
        }
        if self.security_policies.is_empty() {
            return Err("opcua_server.security_policies must not be empty".into());
        }
        let secure = self.security_policies.iter().any(|p| p != "None");
        if secure && !(non_empty(&self.certificate) && non_empty(&self.private_key)) && !non_empty(&self.pki_dir) {
            return Err(
                "opcua_server: secure security_policies need certificate and private_key paths (or pki_dir)".into()
            );
        }
        if non_empty(&self.pki_dir) && non_empty(&self.trusted_certs_dir) {
            return Err("opcua_server: set pki_dir or trusted_certs_dir, not both (pki_dir has trusted/)".into());
        }
        if non_empty(&self.username_env) != non_empty(&self.password_env) {
            return Err("opcua_server: set both username_env and password_env, or neither".into());
        }
        if self.x509_users && !(non_empty(&self.pki_dir) && secure) {
            return Err("opcua_server: x509_users needs pki_dir (users/) and a Sign or SignAndEncrypt policy".into());
        }
        if self.update_ms < 10 {
            return Err("opcua_server.update_ms must be >= 10".into());
        }
        for (name, value) in [
            ("max_sessions", self.max_sessions),
            ("max_subscriptions_per_session", self.max_subscriptions_per_session),
            ("max_monitored_items", self.max_monitored_items),
            ("max_references_per_browse", self.max_references_per_browse),
        ] {
            if value < 1 {
                return Err(format!("opcua_server.{name} must be >= 1"));
            }
        }
        for (name, value) in [
            ("min_publishing_interval_ms", self.min_publishing_interval_ms),
            ("min_sampling_interval_ms", self.min_sampling_interval_ms),
        ] {
            if value < 0 {
                return Err(format!("opcua_server.{name} must be >= 0"));
            }
        }
        if self.security_policies.iter().any(|p| p == "None")
            && !self.allow_insecure_remote
            && !is_loopback_host(&self.endpoint_host())
        {
            return Err(format!(
                "opcua_server: security policy None on {} would serve plaintext beyond this computer: offer only \
                 Sign/SignAndEncrypt policies, bind to 127.0.0.1, or set allow_insecure_remote: true",
                py_repr_str(&self.endpoint)
            ));
        }
        if self.enabled
            && !self.anonymous
            && !non_empty(&self.username_env)
            && self.users.is_empty()
            && !self.x509_users
        {
            return Err("opcua_server: anonymous is off and no user is configured: set username_env/password_env, \
                        users, x509_users, or anonymous: true"
                .into());
        }
        Ok(())
    }

    /// Host part of `endpoint` (`opc.tcp://host:port/path`), without brackets (Python
    /// `endpoint_host`).
    pub fn endpoint_host(&self) -> String {
        let rest = self.endpoint.strip_prefix("opc.tcp://").unwrap_or(&self.endpoint);
        let rest = rest.split('/').next().unwrap_or("");
        if let Some(inner) = rest.strip_prefix('[') {
            return inner.split(']').next().unwrap_or("").to_string();
        }
        if rest.matches(':').count() == 1 {
            rest.rsplit_once(':').map(|(h, _)| h).unwrap_or(rest).to_string()
        } else {
            rest.to_string()
        }
    }
}

/// `localhost` or a loopback IP (all of 127/8, ::1): Python `_is_loopback_host`.
fn is_loopback_host(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost") || host.parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

fn d_instance() -> String {
    "opencap-1".into()
}
fn d_log_level() -> String {
    "INFO".into()
}
fn d_stats() -> f64 {
    300.0
}
fn d_obj<T: for<'de> Deserialize<'de>>() -> T {
    serde_json::from_value(Json::Object(Map::new())).expect("all fields have defaults")
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PollerConfig {
    #[serde(default = "d_instance")]
    pub instance: String,
    #[serde(default = "d_log_level")]
    pub log_level: String,
    #[serde(default = "d_stats")]
    pub stats_interval_s: f64,
    #[serde(default = "d_obj")]
    pub mqtt: MqttCfg,
    #[serde(default = "d_obj")]
    pub saf: SafCfg,
    #[serde(default = "d_obj")]
    pub api: ApiCfg,
    #[serde(default = "d_obj")]
    pub payload: PayloadCfg,
    #[serde(default = "d_obj")]
    pub defaults: DeviceDefaults,
    #[serde(default = "d_obj")]
    pub check: CheckCfg,
    #[serde(default = "d_obj")]
    pub opcua_server: OpcUaServerCfg,
}

impl Default for PollerConfig {
    fn default() -> Self {
        d_obj()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python_models() {
        let p = PollerConfig::default();
        assert_eq!(p.instance, "opencap-1");
        assert_eq!(p.mqtt.port, 1883);
        assert_eq!(p.mqtt.password_env.as_deref(), Some("OPENCAP_MQTT_PASSWORD"));
        assert_eq!(p.saf.max_bytes, 2 * 1024 * 1024 * 1024);
        assert_eq!(p.api.bind, "127.0.0.1");
        assert_eq!(p.payload.timezone, "Asia/Kolkata");
        assert_eq!(p.defaults.retries, 2);
        assert_eq!(p.check.est_rtt_ms, 60.0);
        assert_eq!(p.mqtt.topic_prefix, "opencap");
    }

    #[test]
    fn topic_prefix_validation_matches_python() {
        let cfg = |p: &str| MqttCfg { topic_prefix: p.into(), ..Default::default() };
        for ok in ["acme", "acme/plant-1", "Plant_7", "a/b/c", "site.east"] {
            assert!(cfg(ok).validate().is_ok(), "{ok}");
        }
        for bad in ["", "/", "/acme", "acme/", "acme//plant", "acme/+", "#", "acme/#", "$SYS", "$acme/x", "a\0b", "  "]
        {
            let e = cfg(bad).validate().unwrap_err();
            assert!(e.contains("topic_prefix"), "{bad:?}: {e}");
        }
        assert_eq!(
            cfg("a//b").validate().unwrap_err(),
            "topic_prefix 'a//b' must not start or end with '/' or contain '//'"
        );
    }

    #[test]
    fn api_tokens_match_python() {
        let api = |v: Json| serde_json::from_value::<ApiCfg>(v).unwrap();
        let a = api(serde_json::json!({"tokens": [
            {"name": "ops", "role": "admin", "token_env": "OPS_TOKEN"},
            {"name": "screen", "token_env": "SCREEN_TOKEN"},
        ], "cors_origins": ["tauri://localhost"]}));
        assert!(a.validate().is_ok());
        assert_eq!((a.tokens[0].role, a.tokens[1].role), (Role::Admin, Role::Viewer));
        assert_eq!(a.audit_log, "data/audit.jsonl");
        let dup = api(serde_json::json!({"tokens": [
            {"name": "b", "token_env": "X"}, {"name": "a", "token_env": "Y"},
            {"name": "b", "token_env": "Z"}, {"name": "a", "token_env": "W"},
        ]}));
        assert_eq!(dup.validate().unwrap_err(), "api.tokens: duplicate names ['a', 'b']");
        let reserved = api(serde_json::json!({"tokens": [{"name": "default", "token_env": "X"}]}));
        assert_eq!(reserved.validate().unwrap_err(), "api.tokens: the name 'default' is reserved for api.token_env");
        let empty = api(serde_json::json!({"tokens": [{"name": "", "token_env": "X"}]}));
        assert_eq!(empty.validate().unwrap_err(), "tokens.0.name: String should have at least 1 character");
        assert!(serde_json::from_value::<ApiCfg>(
            serde_json::json!({"tokens": [{"name": "x", "token_env": "X", "role": "root"}]})
        )
        .is_err());
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let err = serde_json::from_value::<PollerConfig>(serde_json::json!({"mqtt": {"hots": "x"}}));
        assert!(err.is_err());
    }

    #[test]
    fn scale_matches_python() {
        let s = Scale { raw_lo: 0.0, raw_hi: 10.0, eng_lo: 0.0, eng_hi: 100.0, ..Default::default() };
        assert_eq!(s.apply(5.0).unwrap(), 50.0);
        let sq = Scale { kind: ScaleType::Sqrt, raw_hi: 100.0, eng_hi: 10.0, ..Default::default() };
        assert_eq!(sq.apply(25.0).unwrap(), 5.0);
        assert_eq!(sq.apply(-1.0).unwrap(), 0.0);
        let c = Scale { clamp_hi: true, negate: true, ..Default::default() };
        assert_eq!(c.apply(2000.0).unwrap(), -1000.0);
        assert!(Scale { raw_hi: 0.0, ..Default::default() }.apply(1.0).is_err());
    }

    #[test]
    fn rate_groups_and_effective_rate() {
        let mut d = Device::new(
            "S/D",
            "S",
            "D",
            "modbus_tcp",
            "192.0.2.1",
            502,
            vec![
                Tag { scan_ms: Some(100), ..Tag::new("A", "400001", DType::Uint16) },
                Tag { scan_ms: Some(0), ..Tag::new("B", "400002", DType::Uint16) },
                Tag { scan_ms: Some(5000), ..Tag::new("C", "400003", DType::Uint16) },
                Tag { enabled: false, ..Tag::new("D", "400004", DType::Uint16) },
            ],
        );
        d.min_scan_ms = 1000;
        d.scan_ms = 2000;
        let groups: Vec<(i64, Vec<String>)> =
            d.rate_groups().into_iter().map(|(r, t)| (r, t.iter().map(|t| t.name.clone()).collect())).collect();
        assert_eq!(
            groups,
            vec![(1000, vec!["A".to_string()]), (2000, vec!["B".to_string()]), (5000, vec!["C".to_string()]),]
        );
    }

    #[test]
    fn duplicate_tag_names() {
        let d = Device::new(
            "S/D",
            "S",
            "D",
            "modbus_tcp",
            "h",
            1,
            vec![Tag::new("A", "400001", DType::Uint16), Tag::new("A", "400002", DType::Uint16)],
        );
        assert_eq!(d.validate().unwrap_err(), "S/D: duplicate tag names ['A']");
    }
}
