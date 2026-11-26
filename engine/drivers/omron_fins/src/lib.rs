//! Omron FINS driver for CS1/CJ1/CJ2 CPUs over FINS/UDP and FINS/TCP, written Rust-first
//! (ADR-0002 §4) from docs/protocols/omron-fins.md and accepted against the Python-harness
//! simulator (tests/helpers/fins_sim.py). KEPServerEX "Omron FINS Ethernet" address syntax.
//!
//! Read-only (ADR-0008): only MEMORY AREA READ (0101), MULTIPLE MEMORY AREA READ (0104) and
//! CPU UNIT DATA READ (0501) can be framed (`wire::Command`), and every frame is checked again
//! before it is sent.
//!
//! Errors: end codes per the protocol notes §4 (`wire::end_code_is_config`) -> BlockConfigError
//! or CommError; no reply within `timeout_ms` -> CommError (a late reply is dropped by its
//! SID); a socket error -> ConnectionLost. A 0104 refused with 0401/0402 switches the device to
//! 0101 reads (counted in `wire_requests()` from then on); a 0104 refused with a configuration
//! end code is re-read range by range, so only the ranges the CPU rejects become `bad_config`.

pub mod address;
pub mod conn;
pub mod decode;
pub mod params;
pub mod plan;
pub mod wire;

use std::any::Any;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{Map, Value as Json};
use tracing::{debug, info, warn};

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, BlockRef, Driver, DriverError, Plan, ReadResult};
use opencap_core::value::{now_utc, Quality, TagValue};

pub use address::{parse_fins_address, AddressError, Area, FinsAddress, Kind, Reason};
pub use decode::{decode_range, DecodeError};
pub use params::{FinsParams, Transport};
pub use plan::{plan_requests, FinsItem, FinsRange, Request};

use conn::{Conn, ConnError};
use wire::{Command, Route};

/// Devices whose CPU refused 0104 (by device id), shared by the plan and the driver that reads.
static NO_MULTI: LazyLock<Mutex<HashMap<String, Arc<AtomicBool>>>> = LazyLock::new(Default::default);

fn no_multi_for(device_id: &str) -> Arc<AtomicBool> {
    NO_MULTI.lock().expect("registry").entry(device_id.to_string()).or_default().clone()
}

#[derive(Debug)]
pub struct FinsBlock {
    pub request: Request,
    tags: Vec<String>,
    no_multi: Arc<AtomicBool>,
}

impl Block for FinsBlock {
    fn tags(&self) -> &[String] {
        &self.tags
    }

    fn describe(&self) -> String {
        match &self.request {
            Request::Read(r) => r.describe(),
            Request::Multi(rs) => {
                format!("0104 {}", rs.iter().map(FinsRange::describe).collect::<Vec<_>>().join(" + "))
            }
        }
    }

    fn wire_requests(&self) -> usize {
        match &self.request {
            Request::Multi(rs) if self.no_multi.load(Ordering::Relaxed) => rs.len(),
            _ => 1,
        }
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

pub fn plan_tags<'a>(p: &FinsParams, tags: impl IntoIterator<Item = &'a Tag>, no_multi: Arc<AtomicBool>) -> Plan {
    let mut parsed = Vec::new();
    let mut invalid = BTreeMap::new();
    for tag in tags {
        if !tag.enabled {
            continue;
        }
        let dtype = tag.dtype.as_str();
        match parse_fins_address(&tag.address, Some(dtype), &p.model) {
            Ok(a) => parsed.push((tag.name.clone(), a, dtype.to_string())),
            Err(e) => {
                invalid.insert(tag.name.clone(), e.message);
            }
        }
    }
    let (requests, too_big) = plan_requests(&parsed, p);
    invalid.extend(too_big);
    let blocks = requests
        .into_iter()
        .map(|request| {
            let mut tags: Vec<String> = request.ranges().iter().flat_map(|r| r.tags.iter().cloned()).collect();
            tags.sort();
            Arc::new(FinsBlock { request, tags, no_multi: no_multi.clone() }) as BlockRef
        })
        .collect();
    Plan { blocks, invalid }
}

pub struct FinsDriver {
    device: Device,
    pub params: FinsParams,
    conn: Option<Conn>,
    no_multi: Arc<AtomicBool>,
    /// ranges (by description) the CPU refused for good: never read again
    rejected: HashSet<String>,
    cpu_error_logged: bool,
}

enum Failure {
    Config(String),
    Comm(String),
    Lost(String),
}

impl FinsDriver {
    /// Errors (bad params) are refused when the device is planned, like Python's ValueError.
    pub fn new(device: &Device) -> Result<Self, String> {
        Ok(Self {
            params: FinsParams::from_device(device)?,
            device: device.clone(),
            conn: None,
            no_multi: no_multi_for(&device.id),
            rejected: HashSet::new(),
            cpu_error_logged: false,
        })
    }

    pub fn plan_tags<'a>(&self, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
        plan_tags(&self.params, tags, self.no_multi.clone())
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        let t = if self.params.transport == Transport::Tcp { "tcp" } else { "udp" };
        format!("{} ({t} {}:{})", self.device.id, self.device.host, self.device.port)
    }

    fn route(&self) -> Route {
        let p = &self.params;
        Route {
            gct: p.gateway_count,
            dna: p.dest_network,
            da1: p.dest_node.or_else(|| params::default_node(&self.device.host)).unwrap_or(1),
            da2: p.dest_unit,
            sna: p.src_network,
            sa1: p.src_node.unwrap_or(0),
            sa2: p.src_unit,
        }
    }

    /// One command; its data, or why it failed.
    async fn exchange(&mut self, cmd: &Command) -> Result<Vec<u8>, Failure> {
        let (timeout, ms) = (self.timeout(), self.device.timeout_ms);
        let conn = self.conn.as_mut().ok_or_else(|| Failure::Lost("not connected".into()))?;
        match conn.request(cmd, timeout).await {
            Ok((0, cpu_error, data)) => {
                if cpu_error != 0 && !self.cpu_error_logged {
                    self.cpu_error_logged = true;
                    let kind = if cpu_error & 0x80 != 0 { "fatal" } else { "non-fatal" };
                    warn!(device = %self.device.id, kind, "omron_fins.cpu_error");
                }
                Ok(data)
            }
            Ok((end, _, _)) if wire::end_code_is_config(end) => Err(Failure::Config(wire::end_code_text(end))),
            Ok((end, _, _)) => Err(Failure::Comm(wire::end_code_text(end))),
            Err(ConnError::Timeout) => Err(Failure::Comm(format!("no reply within {ms} ms"))),
            Err(ConnError::Lost(e)) => {
                self.conn = None;
                Err(Failure::Lost(format!("connection lost: {e}")))
            }
            Err(ConnError::Bad(e)) => Err(Failure::Comm(format!("bad reply: {e}"))),
            Err(ConnError::ReadOnly(e)) => Err(Failure::Comm(e)),
        }
    }

    /// 0101 for one range: its bytes.
    async fn read_range(&mut self, r: &FinsRange) -> Result<Vec<u8>, Failure> {
        let data =
            self.exchange(&Command::Read { code: r.code, address: r.address, bit: 0, count: r.count as u16 }).await?;
        if data.len() != r.nbytes() as usize {
            return Err(Failure::Comm(format!("bad reply: {} data bytes for a read of {}", data.len(), r.nbytes())));
        }
        Ok(data)
    }

    /// 0104 for single-element ranges: each range's bytes.
    async fn read_multi(&mut self, rs: &[&FinsRange]) -> Result<Vec<Vec<u8>>, Failure> {
        let items = rs.iter().map(|r| (r.code, r.address, 0u8)).collect();
        let data = self.exchange(&Command::MultiRead(items)).await?;
        let mut out = Vec::with_capacity(rs.len());
        let mut at = 0usize;
        for r in rs {
            let w = r.width as usize;
            if data.get(at) != Some(&r.code) || data.len() < at + 1 + w {
                return Err(Failure::Comm(format!(
                    "bad reply: multiple memory area read item {} missing or out of order",
                    out.len()
                )));
            }
            out.push(data[at + 1..at + 1 + w].to_vec());
            at += 1 + w;
        }
        Ok(out)
    }

    fn error(&self, f: Failure, what: &str) -> DriverError {
        let w = self.where_();
        match f {
            Failure::Config(e) => DriverError::BlockConfig(format!("{w} {what}: {e}")),
            Failure::Comm(e) => DriverError::Comm(format!("{w} {what}: {e}")),
            Failure::Lost(e) => DriverError::ConnectionLost(format!("{w} {what}: {e}")),
        }
    }
}

#[async_trait]
impl Driver for FinsDriver {
    fn name(&self) -> &'static str {
        "omron_fins"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        self.plan_tags(tags.iter().copied())
    }

    fn connected(&self) -> bool {
        self.conn.is_some()
    }

    fn reports_connection(&self) -> bool {
        true
    }

    /// TCP: connect and the node address handshake.
    fn connect_timeout(&self) -> Option<Duration> {
        Some(self.timeout() * 2 + Duration::from_secs(1))
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let port = u16::try_from(self.device.port).unwrap_or(0);
        let p = &self.params;
        let opened = match p.transport {
            Transport::Udp => Conn::udp(&self.device.host, port, p.local_port, self.route(), p.src_node).await,
            Transport::Tcp => Conn::tcp(&self.device.host, port, self.route(), p.src_node, self.timeout()).await,
        };
        match opened {
            Ok(conn) => {
                debug!(device = %self.device.id, da1 = conn.route.da1, sa1 = conn.route.sa1, tcp = conn.is_tcp(), "omron_fins connect");
                self.conn = Some(conn);
                self.cpu_error_logged = false;
                Ok(())
            }
            Err(reason) => {
                debug!(device = %self.device.id, reason = %reason, "omron_fins connect failed");
                Err(DriverError::ConnectionLost(format!("{}: connect failed: {reason}", self.where_())))
            }
        }
    }

    async fn close(&mut self) {
        if self.conn.take().is_some() {
            debug!(device = %self.device.id, "omron_fins close");
        }
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let Some(block) = block.as_any().downcast_ref::<FinsBlock>() else {
            return Err(DriverError::Other("omron_fins cannot read a non-FINS block".into()));
        };
        if self.conn.is_none() {
            return Err(DriverError::ConnectionLost(format!("{}: not connected", self.where_())));
        }
        let what = block.describe();
        let ranges = block.request.ranges();
        let mut data: Vec<Option<Vec<u8>>> = vec![None; ranges.len()];
        let mut failed: HashMap<usize, Failure> = HashMap::new();
        let live: Vec<usize> = (0..ranges.len()).filter(|&i| !self.rejected.contains(&ranges[i].describe())).collect();
        let mut one_by_one = matches!(block.request, Request::Read(_)) || self.no_multi.load(Ordering::Relaxed);
        if !one_by_one && !live.is_empty() {
            let rs: Vec<&FinsRange> = live.iter().map(|&i| &ranges[i]).collect();
            match self.read_multi(&rs).await {
                Ok(parts) => {
                    for (&i, part) in live.iter().zip(parts) {
                        data[i] = Some(part);
                    }
                }
                Err(Failure::Config(e)) if e.contains("0401") || e.contains("0402") => {
                    info!(device = %self.device.id, reason = %e, "omron_fins.multi_read_unsupported; reading with 0101");
                    self.no_multi.store(true, Ordering::Relaxed);
                    one_by_one = true;
                }
                // one item is wrong but the CPU says not which: find it range by range
                Err(Failure::Config(_)) => one_by_one = true,
                Err(f) => return Err(self.error(f, &what)),
            }
        }
        if one_by_one {
            for &i in &live {
                match self.read_range(&ranges[i]).await {
                    Ok(d) => data[i] = Some(d),
                    Err(Failure::Config(e)) => {
                        failed.insert(i, Failure::Config(e));
                    }
                    Err(f) if ranges.len() == 1 => return Err(self.error(f, &what)),
                    Err(Failure::Comm(e)) => {
                        failed.insert(i, Failure::Comm(e));
                    }
                    Err(f) => return Err(self.error(f, &what)),
                }
            }
        }
        if !live.is_empty() && live.iter().all(|i| failed.contains_key(i)) {
            let first = failed.remove(&live[0]).expect("failed");
            if let Failure::Config(_) = first {
                for &i in &live {
                    self.rejected.insert(ranges[i].describe());
                }
                warn!(device = %self.device.id, block = %what, quality = "bad_config", "omron_fins.block_rejected");
            }
            return Err(self.error(first, &what));
        }
        let ts = now_utc();
        let mut out = HashMap::with_capacity(block.tags.len());
        for (i, r) in ranges.iter().enumerate() {
            let quality = match failed.get(&i) {
                Some(Failure::Config(e)) => {
                    self.rejected.insert(r.describe());
                    warn!(device = %self.device.id, block = %r.describe(), reason = %e, quality = "bad_config", "omron_fins.block_rejected");
                    Some(Quality::BadConfig)
                }
                Some(_) => Some(Quality::BadComm),
                None if data[i].is_none() => Some(Quality::BadConfig),
                None => None,
            };
            if let Some(q) = quality {
                for name in &r.tags {
                    out.insert(name.clone(), TagValue::bad(q, None));
                }
                continue;
            }
            let bytes = data[i].take().expect("read");
            for (name, v) in decode_range(r, &bytes, &self.params) {
                out.insert(
                    name.clone(),
                    match v {
                        Ok(v) => TagValue::good(v, ts),
                        Err(e) => {
                            debug!(device = %self.device.id, tag = %name, reason = %e.0, "omron_fins decode error");
                            TagValue::bad(Quality::BadDecode, Some(ts))
                        }
                    },
                );
            }
        }
        Ok(out)
    }

    /// CPU UNIT DATA READ (0501, data 00): model and system version. Empty when not connected
    /// or when the CPU refuses the command; never fails.
    async fn identity(&mut self) -> Map<String, Json> {
        let Some(conn) = self.conn.as_ref() else { return Map::new() };
        let route = conn.route;
        let tcp = conn.is_tcp();
        let Ok(data) = self.exchange(&Command::CpuUnitData).await else { return Map::new() };
        let text = |b: &[u8]| -> String {
            let end = b.iter().position(|&c| c == 0).unwrap_or(b.len());
            b[..end].iter().map(|&c| char::from(c)).collect::<String>().trim().to_string()
        };
        let mut out = Map::new();
        if data.len() >= 40 {
            out.insert("model".into(), Json::from(text(&data[..20])));
            out.insert("version".into(), Json::from(text(&data[20..40])));
        }
        out.insert("transport".into(), Json::from(if tcp { "tcp" } else { "udp" }));
        out.insert("node".into(), Json::from(route.da1));
        out.insert("source_node".into(), Json::from(route.sa1));
        out.insert("model_family".into(), Json::from(self.params.model.clone()));
        info!(device = %self.device.id, model = %out.get("model").and_then(|v| v.as_str()).unwrap_or(""), "omron_fins.identity");
        out
    }
}
