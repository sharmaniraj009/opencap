//! System and diagnostic tags (`docs/spec/system-tags.md`, #56): the pure function
//! *engine state -> system tags*.
//!
//! The functions are split by level (node, north agent, channel, device) so the runtime task
//! can recompute one device and its channel on a device event without touching the rest
//! (spec section 2.1, D-095). [`evaluate`] runs them all; the conformance vectors in
//! `tests/conformance/system_tags/` run against it (`opencap-conformance`).
//!
//! Nothing here reads the wire, a clock or the metrics registry: the state carries every input,
//! so the same function serves the vectors and the running engine.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use chrono::{DateTime, Datelike, TimeZone, Timelike, Utc};
use chrono_tz::Tz;
use serde::Deserialize;
use serde_json::{Map, Value as Json};

use crate::store::{DeviceWriter, TagLayout, TagStore};
use crate::value::{iso_utc, Quality, TagValue, Value};

const U32: u64 = 1 << 32;
const SERIAL_DRIVERS: [&str; 2] = ["modbus_rtu", "ab_df1"];
const CLIENT_KINDS: [&str; 3] = ["mqtt", "sparkplug", "rest_client"];
const SERVER_KINDS: [&str; 3] = ["opcua_server", "modbus_server", "opcda_server"];
const OPC_SERVER_KINDS: [&str; 2] = ["opcua_server", "opcda_server"];

// --- values --------------------------------------------------------------------------------

/// The value of a system tag. The dtype follows from the variant (spec section 4).
#[derive(Debug, Clone, PartialEq)]
pub enum SysValue {
    Bool(bool),
    U32(u32),
    U64(u64),
    F64(f64),
    Str(String),
    /// ISO-8601 UTC text with milliseconds and `Z`, or `None` for the placeholder.
    DateTime(Option<String>),
    StrArray(Vec<String>),
}

impl SysValue {
    pub fn dtype(&self) -> &'static str {
        match self {
            SysValue::Bool(_) => "bool",
            SysValue::U32(_) => "uint32",
            SysValue::U64(_) => "uint64",
            SysValue::F64(_) => "float64",
            SysValue::Str(_) => "string",
            SysValue::DateTime(_) => "datetime",
            SysValue::StrArray(_) => "string_array",
        }
    }
}

fn s(v: impl Into<String>) -> SysValue {
    SysValue::Str(v.into())
}

/// Saturating conversion of a configured number to the `uint32` of the tag.
fn u32_of(v: i64) -> SysValue {
    SysValue::U32(v.clamp(0, i64::from(u32::MAX)) as u32)
}

// --- the state (tests/conformance/system_tags/README.md) -----------------------------------

#[derive(Debug, Clone, Deserialize)]
pub struct State {
    pub now: String,
    pub node: NodeState,
    #[serde(default)]
    pub north: Vec<NorthState>,
    #[serde(default)]
    pub channels: Vec<ChannelState>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct NodeState {
    pub product_version: String,
    pub engine: String,
    pub instance: String,
    pub project_name: String,
    #[serde(default)]
    pub project_title: Option<String>,
    pub project_revision: String,
    pub start_time: String,
    pub timezone: String,
    #[serde(default)]
    pub sessions: Vec<Session>,
    #[serde(default)]
    pub redundancy: Option<Redundancy>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Session {
    #[serde(default)]
    pub server: String,
    pub kind: String,
    #[serde(default)]
    pub name: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Redundancy {
    pub mode: String,
    pub role: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct NorthState {
    pub name: String,
    pub kind: String,
    #[serde(default = "yes")]
    pub enabled: bool,
    #[serde(default)]
    pub connected: bool,
    #[serde(default)]
    pub published: u64,
    #[serde(default)]
    pub running: bool,
    #[serde(default)]
    pub clients: u32,
    #[serde(default)]
    pub last_error: Option<String>,
    #[serde(default)]
    pub outbox: Option<Outbox>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Outbox {
    #[serde(default)]
    pub messages: u64,
    #[serde(default)]
    pub bytes: u64,
    #[serde(default)]
    pub oldest: Option<String>,
    #[serde(default)]
    pub max_bytes: u64,
    #[serde(default)]
    pub full: bool,
    #[serde(default)]
    pub disk_free_bytes: Option<u64>,
    #[serde(default)]
    pub disk_low: bool,
    #[serde(default = "yes")]
    pub available: bool,
    #[serde(default)]
    pub dropped: u64,
    #[serde(default)]
    pub forward_mode: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ChannelState {
    pub name: String,
    #[serde(default = "yes")]
    pub enabled: bool,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub in_flight: u32,
    #[serde(default)]
    pub max_in_flight: u32,
    #[serde(default)]
    pub devices: Vec<DeviceState>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DeviceState {
    pub name: String,
    #[serde(default = "yes")]
    pub enabled: bool,
    pub driver: String,
    #[serde(default)]
    pub host: String,
    #[serde(default)]
    pub port: i64,
    #[serde(default)]
    pub params: Map<String, Json>,
    #[serde(default = "d_scan")]
    pub scan_ms: i64,
    #[serde(default = "d_timeout")]
    pub timeout_ms: i64,
    #[serde(default = "d_retries")]
    pub retries: i64,
    #[serde(default = "d_demote_after")]
    pub demote_after: i64,
    #[serde(default = "d_demote_for")]
    pub demote_for_ms: i64,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub tags_enabled: u64,
    #[serde(default)]
    pub tags_scanned: u64,
    #[serde(default)]
    pub counts_bytes: bool,
    #[serde(default)]
    pub status: Option<DeviceStatusState>,
    #[serde(default)]
    pub counters: Counters,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DeviceStatusState {
    pub state: String,
    #[serde(default)]
    pub consecutive_failures: i64,
    #[serde(default)]
    pub error_since: Option<String>,
    #[serde(default)]
    pub last_good: Option<String>,
    #[serde(default)]
    pub errors: u64,
    #[serde(default)]
    pub last_error: Option<String>,
    #[serde(default)]
    pub cycle_ms: Option<f64>,
    #[serde(default)]
    pub connected: bool,
    #[serde(default)]
    pub last_connect_failed: bool,
}

/// Process-lifetime counters of one device (the totals beside `/metrics`, spec section 8.3).
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Counters {
    #[serde(default)]
    pub ok: u64,
    #[serde(default)]
    pub timeout: u64,
    #[serde(default)]
    pub conn_error: u64,
    #[serde(default)]
    pub bad_config: u64,
    #[serde(default)]
    pub error: u64,
    #[serde(default)]
    pub failed_reads: u64,
    #[serde(default)]
    pub overruns: u64,
    #[serde(default)]
    pub rx_bytes: u64,
    #[serde(default)]
    pub tx_bytes: u64,
}

fn yes() -> bool {
    true
}
fn d_scan() -> i64 {
    1000
}
fn d_timeout() -> i64 {
    3000
}
fn d_retries() -> i64 {
    2
}
fn d_demote_after() -> i64 {
    3
}
fn d_demote_for() -> i64 {
    30_000
}

pub type Tags = BTreeMap<String, SysValue>;

pub fn parse_ts(text: &str) -> Result<DateTime<Utc>, String> {
    DateTime::parse_from_rfc3339(text)
        .map(|d| d.with_timezone(&Utc))
        .map_err(|e| format!("bad timestamp {text:?}: {e}"))
}

fn zone(name: &str) -> Result<Tz, String> {
    name.parse::<Tz>().map_err(|_| format!("unknown time zone {name:?}"))
}

/// Whole seconds from `from` to `to`, floored (negative differences are the caller's to clamp).
fn floor_secs(from: DateTime<Utc>, to: DateTime<Utc>) -> i64 {
    (to - from).num_milliseconds().div_euclid(1000)
}

// --- node ----------------------------------------------------------------------------------

/// The clock tags of section 5.2, in `tz`.
pub fn clock_tags(now: DateTime<Utc>, tz: Tz) -> Vec<(&'static str, SysValue)> {
    let local = tz.from_utc_datetime(&now.naive_utc());
    let hour = local.hour();
    vec![
        ("_DateTime", s(iso_utc(now))),
        ("_DateTimeLocal", s(local.format("%Y-%m-%dT%H:%M:%S%.3f%:z").to_string())),
        ("_Date", s(local.format("%Y-%m-%d").to_string())),
        ("_Time", s(local.format("%H:%M:%S").to_string())),
        ("_Date_Day", SysValue::U32(local.day())),
        ("_Date_DayOfWeek", SysValue::U32(local.weekday().num_days_from_sunday())),
        ("_Date_Month", SysValue::U32(local.month())),
        ("_Date_Year2", SysValue::U32(local.year().rem_euclid(100) as u32)),
        ("_Date_Year4", SysValue::U32(local.year().max(0) as u32)),
        ("_Time_Hour", SysValue::U32(if hour % 12 == 0 { 12 } else { hour % 12 })),
        ("_Time_Hour24", SysValue::U32(hour)),
        ("_Time_Minute", SysValue::U32(local.minute())),
        ("_Time_Second", SysValue::U32(local.second().min(59))),
        ("_Time_PM", SysValue::Bool(hour >= 12)),
    ]
}

fn is_active(ch: &ChannelState, dev: &DeviceState) -> bool {
    ch.enabled && dev.enabled
}

/// Largest age of the oldest queued message, in seconds rounded to 3 decimals.
fn outbox_age(now: DateTime<Utc>, ob: &Outbox) -> Result<f64, String> {
    let Some(oldest) = ob.oldest.as_deref() else { return Ok(0.0) };
    if ob.messages == 0 {
        return Ok(0.0);
    }
    let oldest = parse_ts(oldest)?;
    let micros = (now - oldest).num_microseconds().unwrap_or(i64::MAX).max(0);
    Ok(((micros + 500) / 1000) as f64 / 1000.0)
}

fn put(out: &mut Tags, path: &str, name: &str, v: SysValue) {
    out.insert(format!("{path}{name}"), v);
}

/// Node tags (`_System.*`) and the redundancy folder.
pub fn node_tags(state: &State, now: DateTime<Utc>, out: &mut Tags) -> Result<(), String> {
    let node = &state.node;
    let p = "_System.";
    let start = parse_ts(&node.start_time)?;
    let tz = zone(&node.timezone)?;
    put(out, p, "_ProductName", s("opencap"));
    put(out, p, "_ProductVersion", s(&node.product_version));
    put(out, p, "_Version", s(format!("opencap-{}", node.product_version)));
    put(out, p, "_Engine", s(&node.engine));
    put(out, p, "_Instance", s(&node.instance));
    put(out, p, "_ProjectName", s(&node.project_name));
    put(out, p, "_ProjectTitle", s(node.project_title.clone().unwrap_or_default()));
    put(out, p, "_ProjectRevision", s(&node.project_revision));
    put(out, p, "_StartTime", SysValue::DateTime(Some(iso_utc(start))));
    put(out, p, "_UptimeS", SysValue::U64(floor_secs(start, now).max(0) as u64));
    for (name, v) in clock_tags(now, tz) {
        put(out, p, name, v);
    }

    let (mut total, mut active) = (0u64, 0u64);
    let (mut ok, mut offline, mut demoted, mut disabled) = (0u32, 0u32, 0u32, 0u32);
    for ch in &state.channels {
        for dev in &ch.devices {
            if !is_active(ch, dev) {
                disabled += 1;
                continue;
            }
            total += dev.tags_enabled;
            let st = dev.status.as_ref().map(|s| s.state.as_str()).unwrap_or("offline");
            match st {
                "ok" => ok += 1,
                "demoted" => demoted += 1,
                _ => offline += 1,
            }
            if st != "demoted" {
                active += dev.tags_scanned;
            }
        }
    }
    let sessions = &node.sessions;
    let mut names: Vec<String> =
        sessions.iter().filter(|x| OPC_SERVER_KINDS.contains(&x.kind.as_str())).map(|x| x.name.clone()).collect();
    names.sort();
    let mqtt: Vec<&NorthState> = state.north.iter().filter(|a| a.kind == "mqtt" && a.enabled).collect();
    let outboxes: Vec<&Outbox> = state.north.iter().filter_map(|a| a.outbox.as_ref()).collect();
    let mut age = 0.0f64;
    for ob in &outboxes {
        age = age.max(outbox_age(now, ob)?);
    }
    put(out, p, "_TotalTagCount", u32_of(total.min(u64::from(u32::MAX)) as i64));
    put(out, p, "_ActiveTagCount", u32_of(active.min(u64::from(u32::MAX)) as i64));
    put(out, p, "_ClientCount", u32_of(sessions.len() as i64));
    put(out, p, "_OpcClientNames", SysValue::StrArray(names));
    put(out, p, "_DevicesOk", SysValue::U32(ok));
    put(out, p, "_DevicesOffline", SysValue::U32(offline));
    put(out, p, "_DevicesDemoted", SysValue::U32(demoted));
    put(out, p, "_DevicesDisabled", SysValue::U32(disabled));
    put(out, p, "_MqttConnected", SysValue::Bool(!mqtt.is_empty() && mqtt.iter().all(|a| a.connected)));
    put(out, p, "_OutboxMessages", SysValue::U64(outboxes.iter().map(|o| o.messages).sum()));
    put(out, p, "_OutboxAgeS", SysValue::F64(age));
    put(out, p, "_OutboxDropped", SysValue::U64(outboxes.iter().map(|o| o.dropped).sum()));

    let (mode, role) =
        node.redundancy.as_ref().map(|r| (r.mode.as_str(), r.role.as_str())).unwrap_or(("standalone", "active"));
    put(out, "_System._Redundancy.", "_Mode", s(mode));
    put(out, "_System._Redundancy.", "_Role", s(role));
    Ok(())
}

/// One folder per north agent or server (section 6).
pub fn north_tags(agent: &NorthState, now: DateTime<Utc>, out: &mut Tags) -> Result<(), String> {
    let p = format!("_System._North.{}.", agent.name);
    let kind = agent.kind.as_str();
    put(out, &p, "_Kind", s(kind));
    put(out, &p, "_Enabled", SysValue::Bool(agent.enabled));
    put(out, &p, "_LastError", s(agent.last_error.clone().unwrap_or_default()));
    if CLIENT_KINDS.contains(&kind) {
        put(out, &p, "_Connected", SysValue::Bool(agent.connected));
        put(out, &p, "_Published", SysValue::U64(agent.published));
    }
    if SERVER_KINDS.contains(&kind) {
        put(out, &p, "_Running", SysValue::Bool(agent.running));
        put(out, &p, "_Clients", SysValue::U32(agent.clients));
    }
    if let Some(ob) = &agent.outbox {
        put(out, &p, "_OutboxMessages", SysValue::U64(ob.messages));
        put(out, &p, "_OutboxBytes", SysValue::U64(ob.bytes));
        put(out, &p, "_OutboxAgeS", SysValue::F64(outbox_age(now, ob)?));
        put(out, &p, "_OutboxCapacityBytes", SysValue::U64(ob.max_bytes.saturating_sub(ob.bytes)));
        put(out, &p, "_OutboxFull", SysValue::Bool(ob.full));
        put(out, &p, "_OutboxDiskFreeBytes", SysValue::U64(ob.disk_free_bytes.unwrap_or(0)));
        put(out, &p, "_OutboxDiskLow", SysValue::Bool(ob.disk_low));
        put(out, &p, "_OutboxAvailable", SysValue::Bool(ob.available));
        put(out, &p, "_OutboxDropped", SysValue::U64(ob.dropped));
        put(out, &p, "_ForwardMode", s(ob.forward_mode.clone().unwrap_or_else(|| "ordered".into())));
    }
    Ok(())
}

// --- channels and devices --------------------------------------------------------------------

fn transport(dev: &DeviceState) -> Option<String> {
    if !SERIAL_DRIVERS.contains(&dev.driver.as_str()) {
        return None;
    }
    let default = if dev.driver == "modbus_rtu" { "tcp" } else { "unmapped" };
    Some(dev.params.get("transport").and_then(Json::as_str).unwrap_or(default).to_string())
}

fn json_int(v: &Json) -> Option<i64> {
    match v {
        Json::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Json::String(t) => t.trim().parse().ok(),
        Json::Bool(b) => Some(i64::from(*b)),
        _ => None,
    }
}

fn json_float(v: &Json) -> Option<f64> {
    match v {
        Json::Number(n) => n.as_f64(),
        Json::String(t) => t.trim().parse().ok(),
        _ => None,
    }
}

fn serial_int(dev: &DeviceState, key: &str, default: i64) -> i64 {
    dev.params.get(key).and_then(json_int).unwrap_or(default)
}

fn parity(dev: &DeviceState) -> String {
    let text = dev.params.get("parity").and_then(Json::as_str).unwrap_or("N").to_ascii_lowercase();
    match text.as_str() {
        "n" | "none" => "None",
        "e" | "even" => "Even",
        "o" | "odd" => "Odd",
        "m" | "mark" => "Mark",
        "s" | "space" => "Space",
        _ => "",
    }
    .to_string()
}

fn com_id(dev: &DeviceState) -> u32 {
    let text = match dev.params.get("com_port") {
        Some(Json::String(t)) if !t.is_empty() => t.clone(),
        _ => dev.host.clone(),
    };
    let t = text.as_str();
    match t.get(..3) {
        Some(head) if head.eq_ignore_ascii_case("COM") && t.len() > 3 && t[3..].bytes().all(|b| b.is_ascii_digit()) => {
            t[3..].parse().unwrap_or(0)
        }
        _ => 0,
    }
}

/// The value all devices share, else `placeholder`.
fn shared<T: PartialEq + Clone>(values: Vec<T>, placeholder: T) -> T {
    match values.first() {
        Some(first) if values.iter().all(|v| v == first) => first.clone(),
        _ => placeholder,
    }
}

fn wrap32(v: u64) -> SysValue {
    SysValue::U32((v % U32) as u32)
}

/// Channel `_System.*` mirrors and `_Statistics.*` (sections 7.1, 7.2).
pub fn channel_tags(ch: &ChannelState, out: &mut Tags) {
    let devs = &ch.devices;
    let p = format!("{}._System.", ch.name);
    put(out, &p, "_Enabled", SysValue::Bool(ch.enabled));
    put(out, &p, "_Description", s(ch.description.clone().unwrap_or_default()));
    put(out, &p, "_Driver", s(shared(devs.iter().map(|d| d.driver.clone()).collect(), String::new())));
    put(out, &p, "_DeviceCount", u32_of(devs.len() as i64));
    let transports: Vec<Option<String>> = devs.iter().map(transport).collect();
    if !devs.is_empty() && transports.iter().all(|t| t.as_deref() == Some("serial")) {
        put(out, &p, "_ComId", SysValue::U32(shared(devs.iter().map(com_id).collect(), 0)));
        let int =
            |key: &str, default: i64| u32_of(shared(devs.iter().map(|d| serial_int(d, key, default)).collect(), 0));
        put(out, &p, "_BaudRate", int("baudrate", 19200));
        put(out, &p, "_DataBits", int("bytesize", 8));
        put(out, &p, "_Parity", s(shared(devs.iter().map(parity).collect(), String::new())));
        let stop: Vec<u64> =
            devs.iter().map(|d| d.params.get("stopbits").and_then(json_float).unwrap_or(1.0).to_bits()).collect();
        put(out, &p, "_StopBits", SysValue::F64(f64::from_bits(shared(stop, 0.0f64.to_bits()))));
    }
    if !devs.is_empty() && transports.iter().all(|t| t.as_deref() == Some("tcp")) {
        put(out, &p, "_EncapsulationPort", u32_of(shared(devs.iter().map(|d| d.port).collect(), 0)));
        put(out, &p, "_EncapsulationProtocol", s("TCP/IP"));
    }
    let sp = format!("{}._Statistics.", ch.name);
    let total = |f: fn(&Counters) -> u64| devs.iter().map(|d| f(&d.counters)).sum::<u64>();
    put(out, &sp, "_SuccessfulReads", wrap32(total(|c| c.ok)));
    put(out, &sp, "_FailedReads", wrap32(total(|c| c.failed_reads)));
    if !devs.is_empty() && devs.iter().all(|d| d.counts_bytes) {
        put(out, &sp, "_RxBytes", wrap32(total(|c| c.rx_bytes)));
        put(out, &sp, "_TxBytes", wrap32(total(|c| c.tx_bytes)));
    }
    put(out, &sp, "_PendingReads", SysValue::U32(ch.in_flight));
    put(out, &sp, "_MaxPendingReads", SysValue::U32(ch.max_in_flight));
    put(out, &sp, "_RequestsOk", SysValue::U64(total(|c| c.ok)));
    put(out, &sp, "_Timeouts", SysValue::U64(total(|c| c.timeout)));
    put(out, &sp, "_ConnErrors", SysValue::U64(total(|c| c.conn_error)));
    put(out, &sp, "_BadConfig", SysValue::U64(total(|c| c.bad_config)));
    put(out, &sp, "_OtherErrors", SysValue::U64(total(|c| c.error)));
    put(out, &sp, "_Overruns", SysValue::U64(total(|c| c.overruns)));
}

/// The section 8.1 status tags of one device, from its status fields (section 9). `status` is
/// ignored for a disabled device.
pub fn device_status_tags(
    counters: &Counters,
    status: Option<&DeviceStatusState>,
    now: DateTime<Utc>,
    disabled: bool,
) -> Result<Vec<(&'static str, SysValue)>, String> {
    let overruns = SysValue::U64(counters.overruns);
    let Some(status) = status.filter(|_| !disabled) else {
        return Ok(vec![
            ("_State", s("disabled")),
            ("_Error", SysValue::Bool(false)),
            ("_NoError", SysValue::Bool(true)),
            ("_SecondsInError", SysValue::U32(0)),
            ("_AutoDemoted", SysValue::Bool(false)),
            ("_FailedConnection", SysValue::Bool(false)),
            ("_Connected", SysValue::Bool(false)),
            ("_LastGood", SysValue::DateTime(None)),
            ("_LastError", s("")),
            ("_ErrorCount", SysValue::U64(0)),
            ("_CycleMs", SysValue::F64(0.0)),
            ("_Overruns", overruns),
        ]);
    };
    let error = status.consecutive_failures > 0;
    let seconds = match (&status.error_since, error) {
        (Some(since), true) => floor_secs(parse_ts(since)?, now).clamp(0, i64::from(u32::MAX)) as u32,
        _ => 0,
    };
    let last_good = status.last_good.as_deref().map(|t| parse_ts(t).map(iso_utc)).transpose()?;
    Ok(vec![
        ("_State", s(&status.state)),
        ("_Error", SysValue::Bool(error)),
        ("_NoError", SysValue::Bool(!error)),
        ("_SecondsInError", SysValue::U32(seconds)),
        ("_AutoDemoted", SysValue::Bool(status.state == "demoted")),
        ("_FailedConnection", SysValue::Bool(status.last_connect_failed)),
        ("_Connected", SysValue::Bool(status.connected)),
        ("_LastGood", SysValue::DateTime(last_good)),
        ("_LastError", s(status.last_error.clone().unwrap_or_default())),
        ("_ErrorCount", SysValue::U64(status.errors)),
        ("_CycleMs", SysValue::F64(status.cycle_ms.unwrap_or(0.0))),
        ("_Overruns", overruns),
    ])
}

/// A device's `_System.*` and `_Statistics.*` tags (sections 8.1 to 8.3).
pub fn device_tags(ch: &ChannelState, dev: &DeviceState, now: DateTime<Utc>, out: &mut Tags) -> Result<(), String> {
    let base = format!("{}.{}.", ch.name, dev.name);
    let p = format!("{base}_System.");
    for (name, v) in device_status_tags(&dev.counters, dev.status.as_ref(), now, !is_active(ch, dev))? {
        put(out, &p, name, v);
    }
    let tr = transport(dev);
    put(out, &p, "_Enabled", SysValue::Bool(dev.enabled));
    put(out, &p, "_Driver", s(&dev.driver));
    let id = if tr.as_deref() == Some("serial") { dev.host.clone() } else { format!("{}:{}", dev.host, dev.port) };
    put(out, &p, "_DeviceId", s(id));
    put(out, &p, "_Description", s(dev.description.clone().unwrap_or_default()));
    put(out, &p, "_Simulated", SysValue::Bool(dev.driver == "simulator"));
    put(out, &p, "_ScanMode", s("tag_scan_rate"));
    put(out, &p, "_ScanRateMs", u32_of(dev.scan_ms));
    put(out, &p, "_RequestTimeout", u32_of(dev.timeout_ms));
    put(out, &p, "_RequestAttempts", u32_of(dev.retries.max(0) + 1));
    put(out, &p, "_AutoDemotionEnabled", SysValue::Bool(dev.demote_after > 0));
    put(out, &p, "_AutoDemotedFailureCount", u32_of(dev.demote_after.max(0)));
    put(out, &p, "_AutoDemotionIntervalMS", u32_of(dev.demote_for_ms));
    if tr.as_deref() == Some("tcp") {
        put(out, &p, "_EncapsulationIp", s(&dev.host));
        put(out, &p, "_EncapsulationPort", u32_of(dev.port));
        put(out, &p, "_EncapsulationProtocol", s("TCP/IP"));
    }
    let sp = format!("{base}_Statistics.");
    let c = &dev.counters;
    put(out, &sp, "_SuccessfulReads", wrap32(c.ok));
    put(out, &sp, "_FailedReads", wrap32(c.failed_reads));
    if dev.counts_bytes {
        put(out, &sp, "_RxBytes", wrap32(c.rx_bytes));
        put(out, &sp, "_TxBytes", wrap32(c.tx_bytes));
    }
    put(out, &sp, "_RequestsOk", SysValue::U64(c.ok));
    put(out, &sp, "_Timeouts", SysValue::U64(c.timeout));
    put(out, &sp, "_ConnErrors", SysValue::U64(c.conn_error));
    put(out, &sp, "_BadConfig", SysValue::U64(c.bad_config));
    put(out, &sp, "_OtherErrors", SysValue::U64(c.error));
    Ok(())
}

/// Every system tag of a snapshot: `{path: value}`.
pub fn evaluate(state: &State) -> Result<Tags, String> {
    let now = parse_ts(&state.now)?;
    let mut out = Tags::new();
    node_tags(state, now, &mut out)?;
    for agent in &state.north {
        north_tags(agent, now, &mut out)?;
    }
    for ch in &state.channels {
        channel_tags(ch, &mut out);
        for dev in &ch.devices {
            device_tags(ch, dev, now, &mut out)?;
        }
    }
    Ok(out)
}

// --- the store (virtual devices) ---------------------------------------------------------------

impl SysValue {
    /// The tag-store value (`None` is the datetime placeholder: a good tag with no value).
    pub fn to_value(&self) -> Option<Value> {
        Some(match self {
            SysValue::Bool(b) => Value::Bool(*b),
            SysValue::U32(n) => Value::Int(i64::from(*n)),
            SysValue::U64(n) => Value::from_u64(*n),
            SysValue::F64(f) => Value::Float(*f),
            SysValue::Str(t) => Value::Str(t.clone()),
            SysValue::DateTime(t) => return t.clone().map(Value::Str),
            SysValue::StrArray(a) => Value::List(a.iter().cloned().map(Value::Str).collect()),
        })
    }
}

/// The folder (virtual device id) of a system-tag path: everything before the last dot.
pub fn folder_of(path: &str) -> &str {
    path.rsplit_once('.').map(|(f, _)| f).unwrap_or(path)
}

struct Folder {
    writer: DeviceWriter,
    names: Vec<String>,
    last: HashMap<String, SysValue>,
}

/// The only writer of the system-tag virtual devices (spec section 2, ADR-0005). Each folder
/// (`_System`, `Plant A._Statistics`, `Plant A.PLC1._System`, ...) is one virtual device of the
/// tag store. A tag is written only when its value changed, quality always `good`, `source_ts`
/// the computation time.
pub struct SystemTagWriter {
    store: Arc<TagStore>,
    folders: HashMap<String, Folder>,
}

impl SystemTagWriter {
    pub fn new(store: Arc<TagStore>) -> Self {
        Self { store, folders: HashMap::new() }
    }

    /// Write `tags`, which must hold every tag of each folder they touch (the level functions
    /// above produce whole folders). A folder whose tag set changed (a reload) is registered
    /// again. Returns how many tags changed.
    pub fn apply(&mut self, tags: &Tags, now: DateTime<Utc>) -> usize {
        let mut by_folder: BTreeMap<&str, Vec<(&str, &SysValue)>> = BTreeMap::new();
        for (path, v) in tags {
            let folder = folder_of(path);
            by_folder.entry(folder).or_default().push((&path[folder.len() + 1..], v));
        }
        let mut changed = 0;
        for (name, entries) in by_folder {
            let want: Vec<String> = entries.iter().map(|(n, _)| (*n).to_string()).collect();
            let stale = self.folders.get(name).is_none_or(|f| f.names != want || !f.writer.is_current());
            if stale {
                let writer = self.store.register_layout(TagLayout::new(name, want.iter().cloned()));
                self.folders.insert(name.to_string(), Folder { writer, names: want, last: HashMap::new() });
            }
            let folder = self.folders.get_mut(name).expect("folder registered above");
            let mut batch: Vec<(&str, TagValue)> = Vec::new();
            for (tag, v) in entries {
                if folder.last.get(tag) != Some(v) {
                    batch.push((tag, TagValue::new(v.to_value(), Quality::Good, Some(now))));
                    folder.last.insert(tag.to_string(), v.clone());
                }
            }
            if !batch.is_empty() {
                changed += batch.len();
                folder.writer.update(batch.iter().map(|(n, tv)| (*n, tv)));
            }
        }
        changed
    }

    /// Unregister every folder for which `keep` is false (channels and devices a reload removed).
    pub fn retain(&mut self, keep: impl Fn(&str) -> bool) {
        let gone: Vec<String> = self.folders.keys().filter(|f| !keep(f)).cloned().collect();
        for f in gone {
            self.store.unregister(&f);
            self.folders.remove(&f);
        }
    }

    /// `{path, dtype, value, quality, source_ts}` of every tag whose path starts with `prefix`
    /// (the `GET /api/v1/system/tags` items), folders and tags in name order.
    pub fn items(&self, prefix: &str) -> Vec<serde_json::Value> {
        let mut out = Vec::new();
        for folder in self.folders() {
            let f = &self.folders[&folder];
            let snap = self.store.snapshot(&folder);
            for name in &f.names {
                let path = format!("{folder}.{name}");
                let (Some(sv), Some(tv)) = (f.last.get(name), snap.get(name)) else { continue };
                if path.starts_with(prefix) {
                    let mut item = tv.to_api_json();
                    item["path"] = path.into();
                    item["dtype"] = sv.dtype().into();
                    out.push(item);
                }
            }
        }
        out
    }

    /// A device's `_System.*` and `_Statistics.*` tags keyed by their device-relative path
    /// (`_System._Error`), as SubDevice JSON and `values?include_system=true` name them.
    pub fn device_values(&self, base: &str) -> serde_json::Map<String, serde_json::Value> {
        let mut out = serde_json::Map::new();
        for part in ["_System", "_Statistics"] {
            let folder = format!("{base}.{part}");
            let Some(f) = self.folders.get(&folder) else { continue };
            let snap = self.store.snapshot(&folder);
            for name in &f.names {
                if let Some(tv) = snap.get(name) {
                    out.insert(format!("{part}.{name}"), tv.to_api_json());
                }
            }
        }
        out
    }

    /// Folder names (virtual device ids), sorted.
    pub fn folders(&self) -> Vec<String> {
        let mut v: Vec<String> = self.folders.keys().cloned().collect();
        v.sort();
        v
    }

    pub fn contains(&self, folder: &str) -> bool {
        self.folders.contains_key(folder)
    }

    pub fn folder_set(&self) -> HashSet<&str> {
        self.folders.keys().map(String::as_str).collect()
    }
}

// --- the catalogue (spec: tests/conformance/system_tags_catalogue.json) ------------------------

/// Names of the `tick` update class (spec section 2.1): recomputed once per second, not on every
/// device event. A name has the same class at every level (a test compares this with the catalogue).
pub const TICK_TAGS: &[&str] = &[
    "_ActiveTagCount",
    "_ClientCount",
    "_Clients",
    "_Date",
    "_DateTime",
    "_DateTimeLocal",
    "_Date_Day",
    "_Date_DayOfWeek",
    "_Date_Month",
    "_Date_Year2",
    "_Date_Year4",
    "_DevicesDemoted",
    "_DevicesOffline",
    "_DevicesOk",
    "_MaxPendingReads",
    "_MqttConnected",
    "_OpcClientNames",
    "_OutboxAgeS",
    "_OutboxAvailable",
    "_OutboxBytes",
    "_OutboxCapacityBytes",
    "_OutboxDiskFreeBytes",
    "_OutboxDiskLow",
    "_OutboxDropped",
    "_OutboxFull",
    "_OutboxMessages",
    "_PendingReads",
    "_Published",
    "_Time",
    "_Time_Hour",
    "_Time_Hour24",
    "_Time_Minute",
    "_Time_PM",
    "_Time_Second",
    "_UptimeS",
];

pub fn is_tick(name: &str) -> bool {
    TICK_TAGS.contains(&name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writer_publishes_changes_only_and_good_quality() {
        let store = Arc::new(TagStore::new());
        let mut w = SystemTagWriter::new(store.clone());
        let mut tags = Tags::new();
        tags.insert("Plant A.PLC1._System._Error".into(), SysValue::Bool(false));
        tags.insert("Plant A.PLC1._System._LastGood".into(), SysValue::DateTime(None));
        let t0 = parse_ts("2026-11-18T10:00:00.000Z").unwrap();
        assert_eq!(w.apply(&tags, t0), 2);
        assert_eq!(w.apply(&tags, t0 + chrono::Duration::seconds(1)), 0, "unchanged values are not rewritten");
        tags.insert("Plant A.PLC1._System._Error".into(), SysValue::Bool(true));
        let t1 = t0 + chrono::Duration::seconds(2);
        assert_eq!(w.apply(&tags, t1), 1);
        let e = store.get("Plant A.PLC1._System", "_Error").unwrap();
        assert_eq!((e.value, e.quality, e.source_ts), (Some(Value::Bool(true)), Quality::Good, Some(t1)));
        let lg = store.get("Plant A.PLC1._System", "_LastGood").unwrap();
        assert_eq!((lg.value, lg.quality), (None, Quality::Good));
        assert_eq!(w.folders(), vec!["Plant A.PLC1._System"]);
        w.retain(|_| false);
        assert!(store.get("Plant A.PLC1._System", "_Error").is_none());
    }

    #[test]
    fn com_id_parses_com_names_only() {
        let mk = |host: &str| {
            serde_json::from_value::<DeviceState>(
                serde_json::json!({"name": "d", "driver": "modbus_rtu", "host": host}),
            )
            .unwrap()
        };
        assert_eq!(com_id(&mk("COM7")), 7);
        assert_eq!(com_id(&mk("com12")), 12);
        assert_eq!(com_id(&mk("COM")), 0);
        assert_eq!(com_id(&mk("/dev/ttyUSB0")), 0);
    }

    #[test]
    fn outbox_age_rounds_to_milliseconds() {
        let ob = Outbox {
            messages: 1,
            bytes: 1,
            oldest: Some("2026-11-18T10:00:00.000Z".into()),
            max_bytes: 10,
            full: false,
            disk_free_bytes: None,
            disk_low: false,
            available: true,
            dropped: 0,
            forward_mode: None,
        };
        let now = parse_ts("2026-11-18T10:00:01.2346Z").unwrap();
        assert_eq!(outbox_age(now, &ob).unwrap(), 1.235);
    }
}
