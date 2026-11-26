//! Allen-Bradley ControlLogix / CompactLogix / Micro800 driver, symbolic tags over native
//! CIP (EtherNet/IP). Port of the Python `src/opencap/drivers/ab_logix.py` (D-008, D-024,
//! D-025) with its own client instead of pycomm3 (D-076).
//!
//! The pure part (address check, planner, decoder, tag-list lookups, CIP codec) needs no
//! socket. The engine is READ-ONLY: `cip.rs` has builders for read services only, so a write
//! cannot be constructed, let alone sent.
//!
//! I/O contract (docs/phase1_contracts.md section 1): every request is one attempt bounded by
//! `device.timeout_ms`. Connect failure, reset, peer close or encapsulation error ->
//! `ConnectionLost`; reply timeout -> socket dropped, `Comm`; unusable batched reply -> `Comm`;
//! per-tag CIP errors -> `bad_config` / `bad_comm`; unparseable value -> `bad_decode`.

pub mod address;
pub mod cip;
pub mod conn;
pub mod decode;
pub mod params;
pub mod plan;
pub mod tags;
pub mod upload;

use std::collections::HashMap;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{json, Map, Value as Json};
use tracing::{debug, info, warn};

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan, ReadResult};
use opencap_core::pyfmt::py_repr_str;
use opencap_core::value::{now_utc, Quality, TagValue, Value};

pub use address::{epath_size, parse_logix_address, LogixAddress};
pub use decode::{coerce_value, decode_block, DecodeError};
pub use params::{AbParams, Batching};
pub use plan::{plan_tags, LogixBlock, LogixItem, LogixRead};
pub use tags::{probe_names, resolve_read_name, Resolved, TagList};

use conn::{Conn, NameResult, NetError, ReadFail};
use upload::UploadError;

/// Probes in a row with no usable answer after which batching stays off without probing.
const PROBE_GIVE_UP: u32 = 2;
/// Per-tag CIP statuses that mean "this name/path is wrong" (permanent) -> bad_config.
const CONFIG_STATUSES: [u8; 4] = [0x04, 0x05, 0x16, 0x26];
/// Logix extended statuses (with general status 0xFF): index out of range, beyond the end,
/// data type mismatch.
const CONFIG_EXTENDED: [u16; 3] = [0x2104, 0x2105, 0x2107];

/// Quality of a name that did not give a value (`None` when it has one).
pub fn result_quality(r: &NameResult) -> Option<Quality> {
    if r.ok() {
        return None;
    }
    if !r.sent {
        return Some(Quality::BadConfig);
    }
    Some(match r.status {
        Some(s) if s != cip::STATUS_OK => {
            if CONFIG_STATUSES.contains(&s)
                || (s == cip::STATUS_GENERAL && r.ext_status.is_some_and(|e| CONFIG_EXTENDED.contains(&e)))
            {
                Quality::BadConfig
            } else {
                Quality::BadComm
            }
        }
        Some(_) => Quality::BadDecode,
        None => Quality::BadComm,
    })
}

type IdentityKey = (String, u16, u32, String);

/// A tag list uploaded from one controller, kept across reconnects of one driver instance.
struct Cache {
    key: IdentityKey,
    list: TagList,
    resolved: HashMap<String, Result<Resolved, String>>,
}

struct Session {
    conn: Conn,
    micro800: bool,
    batching: bool,
    batching_reason: String,
    identity: Map<String, Json>,
}

pub struct AbLogixDriver {
    device: Device,
    pub params: AbParams,
    session: Option<Session>,
    cache: Option<Cache>,
    probe_misses: u32,
    batching_off: Option<String>,
    last_batching: Option<(bool, String)>,
    warned_kind: bool,
}

impl AbLogixDriver {
    /// Errors (bad params) carry the same text as Python's ValueError.
    pub fn new(device: &Device) -> Result<Self, String> {
        Ok(Self {
            params: AbParams::from_device(device)?,
            device: device.clone(),
            session: None,
            cache: None,
            probe_misses: 0,
            batching_off: None,
            last_batching: None,
            warned_kind: false,
        })
    }

    /// Pure planning (also used by tests).
    pub fn plan_tags<'a>(&self, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
        plan_tags(&self.params, tags)
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        format!("{} ({})", self.device.id, self.params.path)
    }

    async fn establish(&mut self, mut conn: Conn) -> Result<Session, String> {
        let identity = conn.list_identity().await.map_err(|e| e.to_string())?;
        let micro800 = identity.is_micro800();
        if micro800 != self.params.micro800 && !self.warned_kind {
            self.warned_kind = true;
            warn!(
                device = %self.device.id, product_name = %identity.product_name,
                configured_micro800 = self.params.micro800, detected_micro800 = micro800,
                "ab_logix.kind_mismatch: the controller's own kind is followed; check params.micro800"
            );
        }
        let route = if micro800 { vec![] } else { self.params.route.clone() };
        match conn.forward_open(&route, self.params.connection_size).await.map_err(|e| e.to_string())? {
            Ok(()) => {}
            Err(refused) => return Err(refused),
        }
        let program = if micro800 { None } else { conn.program_name().await.ok().flatten() };

        let key: IdentityKey =
            (identity.product_name.clone(), identity.product_code, identity.serial, identity.revision());
        if self.cache.as_ref().is_some_and(|c| c.key == key) {
            debug!(device = %self.device.id, source = "reused", count = self.cache.as_ref().map_or(0, |c| c.list.len()), "ab_logix.tag_list");
        } else {
            if let Some(old) = &self.cache {
                info!(device = %self.device.id, before = ?old.key, after = ?key, "ab_logix.identity_changed: re-uploading the tag list");
            }
            let started = Instant::now();
            let list = conn.upload_tag_list(identity.major, !micro800).await.map_err(|e| match e {
                UploadError::Net(n) => n.to_string(),
                UploadError::Refused(r) => r,
            })?;
            info!(
                device = %self.device.id, source = "uploaded", count = list.len(), data_types = list.templates.len(),
                ms = started.elapsed().as_millis() as u64, "ab_logix.tag_list"
            );
            // kept even if this attempt is abandoned later: the next one reuses it
            self.cache = Some(Cache { key, list, resolved: HashMap::new() });
        }

        let (batching, reason) = self.decide_batching(&mut conn, micro800).await?;
        let mut ident = Map::new();
        ident.insert("vendor".into(), json!(identity.vendor_name()));
        ident.insert("product_name".into(), json!(identity.product_name));
        ident.insert("product_code".into(), json!(identity.product_code));
        ident.insert("revision".into(), json!(identity.revision()));
        ident.insert("serial".into(), json!(identity.serial));
        if let Some(t) = identity.product_type() {
            ident.insert("product_type".into(), json!(t));
        }
        if let Some(p) = program {
            ident.insert("program_name".into(), json!(p));
        }
        ident.insert("connection_size".into(), json!(conn.connection_size));
        Ok(Session { conn, micro800, batching, batching_reason: reason, identity: ident })
    }

    async fn decide_batching(&mut self, conn: &mut Conn, micro800: bool) -> Result<(bool, String), String> {
        let mode = self.params.batching;
        let decision = if !micro800 {
            if mode != Batching::Off {
                (true, "Logix: Multiple Service Packet".to_string())
            } else {
                (false, "off by params.batching".to_string())
            }
        } else {
            match mode {
                Batching::On => (true, "on by params.batching (no probe)".to_string()),
                Batching::Off => (false, "off by params.batching".to_string()),
                Batching::Auto => match &self.batching_off {
                    Some(why) => (false, why.clone()),
                    None => self.probe(conn).await?,
                },
            }
        };
        if self.last_batching.as_ref() != Some(&decision) {
            info!(device = %self.device.id, micro800, batching = decision.0, reason = %decision.1, "ab_logix.batching");
        } else {
            debug!(device = %self.device.id, micro800, batching = decision.0, reason = %decision.1, "ab_logix.batching");
        }
        self.last_batching = Some(decision.clone());
        Ok(decision)
    }

    /// Micro800 + batching auto: one 2-name Multiple Service Packet read of existing tags.
    /// Accepted (every probe name read) -> on. Rejected (e.g. status 0x08), malformed reply or a
    /// failing probe name -> off for this connection. No reply or a dropped connection fails the
    /// connect; after `PROBE_GIVE_UP` such probes in a row batching stays off without probing.
    async fn probe(&mut self, conn: &mut Conn) -> Result<(bool, String), String> {
        let cache = self.cache.as_ref().expect("tag list loaded");
        let names = probe_names(&cache.list, 2);
        if names.is_empty() {
            return Ok((false, "probe impossible: no readable scalar controller tag".into()));
        }
        let resolved: Vec<Resolved> = names.iter().filter_map(|n| resolve_read_name(n, &cache.list).ok()).collect();
        if resolved.len() != names.len() {
            return Ok((false, "probe impossible: no readable scalar controller tag".into()));
        }
        match conn.read_names(&resolved, true).await {
            Err(ReadFail::Whole { reason, .. }) => {
                self.probe_misses = 0;
                Ok((false, format!("probe rejected: {reason}")))
            }
            Err(ReadFail::Net(NetError::Encap(s))) => Err(NetError::Encap(s).to_string()),
            Err(ReadFail::Net(NetError::Protocol(e))) => {
                self.probe_misses = 0;
                Ok((false, format!("probe reply not usable: {e}")))
            }
            Err(ReadFail::Net(e)) => {
                self.probe_misses += 1;
                if self.probe_misses >= PROBE_GIVE_UP {
                    let why = format!(
                        "off without probing: {} probes in a row got no usable answer (last: {e})",
                        self.probe_misses
                    );
                    warn!(device = %self.device.id, reason = %why, "ab_logix.batching_probe_given_up");
                    self.batching_off = Some(why);
                } else {
                    warn!(device = %self.device.id, error = %e, "ab_logix.batching_probe_no_answer: connect fails; the next connect probes again");
                }
                Err(e.to_string())
            }
            Ok(results) => {
                self.probe_misses = 0;
                match results.iter().zip(&names).find(|(r, _)| !r.ok()) {
                    Some((r, name)) => Ok((
                        false,
                        format!(
                            "probe read of {} failed: {}",
                            py_repr_str(name),
                            r.error.clone().unwrap_or_else(|| "no value".into())
                        ),
                    )),
                    None => Ok((true, format!("probe ok: {}-name Multiple Service Packet accepted", names.len()))),
                }
            }
        }
    }

    /// Split block names into `{name: resolved}` and `{name: reason missing}`. Resolutions are
    /// cached with the tag list; each problem is logged once.
    fn resolve(&mut self, block: &LogixBlock) -> (HashMap<String, Resolved>, HashMap<String, String>) {
        let cache = self.cache.as_mut().expect("tag list loaded");
        let (mut found, mut missing) = (HashMap::new(), HashMap::new());
        let (mut new_missing, mut recased) = (Vec::new(), Vec::new());
        for name in block.names() {
            if !cache.resolved.contains_key(name) {
                let hit = resolve_read_name(name, &cache.list);
                match &hit {
                    Err(_) => new_missing.push(name.to_string()),
                    Ok(r) if r.name != name => recased.push(format!("{name} -> {}", r.name)),
                    Ok(_) => {}
                }
                cache.resolved.insert(name.to_string(), hit);
            }
            match &cache.resolved[name] {
                Ok(r) => {
                    found.insert(name.to_string(), r.clone());
                }
                Err(e) => {
                    missing.insert(name.to_string(), e.clone());
                }
            }
        }
        if !new_missing.is_empty() {
            let tags_of = block.name_to_tags();
            let tags: Vec<&str> = new_missing
                .iter()
                .take(20)
                .flat_map(|n| tags_of.get(n.as_str()).cloned().unwrap_or_default())
                .take(20)
                .collect();
            let reasons: Vec<&String> = new_missing.iter().take(5).map(|n| &missing[n]).collect();
            warn!(
                device = %self.device.id, count = new_missing.len(), names = ?&new_missing[..new_missing.len().min(20)],
                tags = ?tags, reasons = ?reasons, quality = "bad_config", "ab_logix.tags_missing"
            );
        }
        if !recased.is_empty() {
            info!(
                device = %self.device.id, count = recased.len(), names = ?&recased[..recased.len().min(20)],
                "ab_logix.tags_recased: address spelling differs from the controller's; read with the controller's"
            );
        }
        (found, missing)
    }
}

#[async_trait]
impl Driver for AbLogixDriver {
    fn name(&self) -> &'static str {
        "ab_logix"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        self.plan_tags(tags.iter().copied())
    }

    fn connected(&self) -> bool {
        self.session.is_some()
    }

    fn reports_connection(&self) -> bool {
        true
    }

    /// Connect = Forward Open + identity + batching probe, plus a tag-list upload the first
    /// time. Give it room.
    fn connect_timeout(&self) -> Option<Duration> {
        let per = self.timeout();
        Some(if self.cache.is_none() {
            Duration::from_secs(60).max(per * 20)
        } else {
            per * 5 + Duration::from_secs(1)
        })
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let started = Instant::now();
        let lost = |this: &Self, reason: String| {
            DriverError::ConnectionLost(format!("{}: connect failed: {reason}", this.where_()))
        };
        let conn = match Conn::open(&self.params.host, self.params.port, self.timeout()).await {
            Ok(c) => c,
            Err(e) => {
                let err = lost(self, e.to_string());
                debug!(device = %self.device.id, reason = %e, "ab_logix connect failed");
                return Err(err);
            }
        };
        match self.establish(conn).await {
            Ok(session) => {
                debug!(
                    device = %self.device.id, micro800 = session.micro800, batching = session.batching,
                    ms = started.elapsed().as_millis() as u64, "ab_logix connect ok"
                );
                self.session = Some(session);
                Ok(())
            }
            Err(reason) => Err(lost(self, reason)),
        }
    }

    async fn close(&mut self) {
        if let Some(s) = self.session.take() {
            s.conn.close(true).await;
        }
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let block = block
            .as_any()
            .downcast_ref::<LogixBlock>()
            .ok_or_else(|| DriverError::Other("ab_logix cannot read a block of another driver".into()))?;
        let where_ = self.where_();
        if self.session.is_none() {
            return Err(DriverError::ConnectionLost(format!("{where_}: not connected")));
        }
        let (found, missing) = self.resolve(block);
        // distinct controller names, in block order
        let mut wire: Vec<Resolved> = Vec::new();
        let mut wire_index: HashMap<String, usize> = HashMap::new();
        for n in block.names() {
            if let Some(r) = found.get(n) {
                let i = *wire_index.entry(r.name.clone()).or_insert_with(|| {
                    wire.push(r.clone());
                    wire.len() - 1
                });
                let _ = i;
            }
        }
        let mut results: HashMap<String, NameResult> = HashMap::new();
        if !wire.is_empty() {
            let mut session = self.session.take().expect("checked above");
            let started = Instant::now();
            let outcome = session.conn.read_names(&wire, session.batching).await;
            match outcome {
                Ok(replies) => {
                    debug!(
                        device = %self.device.id, names = wire.len(), batching = session.batching,
                        failed = replies.iter().filter(|r| !r.ok()).count(), ms = started.elapsed().as_millis() as u64, "ab_logix read"
                    );
                    for n in block.names() {
                        if let Some(r) = found.get(n) {
                            results.insert(n.to_string(), replies[wire_index[&r.name]].clone());
                        }
                    }
                    self.session = Some(session);
                }
                Err(ReadFail::Whole { reason, multi }) => {
                    if multi && session.micro800 && session.batching && self.params.batching == Batching::Auto {
                        let why = format!("off after a failed batched read: {reason}");
                        session.batching = false;
                        session.batching_reason = why.clone();
                        self.batching_off = Some(why.clone());
                        self.last_batching = Some((false, why.clone()));
                        warn!(device = %self.device.id, reason = %why, "ab_logix.batching_demoted");
                    }
                    self.session = Some(session);
                    return Err(DriverError::Comm(format!("{where_} {}: {reason}", block.describe())));
                }
                Err(ReadFail::Net(e)) => {
                    session.conn.close(false).await;
                    let d = block.describe();
                    return Err(match e {
                        NetError::Timeout => DriverError::Comm(format!(
                            "{where_} {d}: no reply within {} ms; connection closed",
                            self.device.timeout_ms
                        )),
                        NetError::Protocol(p) => DriverError::Comm(format!("{where_} {d}: {p}; connection closed")),
                        NetError::Encap(_) => {
                            DriverError::ConnectionLost(format!("{where_} {d}: {e}; connection closed"))
                        }
                        NetError::Lost(_) => DriverError::ConnectionLost(format!("{where_} {d}: connection lost: {e}")),
                    });
                }
            }
        }
        Ok(self.values(block, &results, &missing))
    }

    async fn identity(&mut self) -> Map<String, Json> {
        let Some(s) = &self.session else { return Map::new() };
        let mut out = s.identity.clone();
        out.insert("micro800".into(), json!(s.micro800));
        out.insert("batching".into(), json!(s.batching));
        out.insert("batching_reason".into(), json!(s.batching_reason));
        out.insert("tag_count".into(), json!(self.cache.as_ref().map_or(0, |c| c.list.len())));
        out
    }
}

impl AbLogixDriver {
    fn values(
        &self,
        block: &LogixBlock,
        results: &HashMap<String, NameResult>,
        missing: &HashMap<String, String>,
    ) -> HashMap<String, TagValue> {
        let ts = now_utc();
        let mut failed: HashMap<&str, Quality> = HashMap::new();
        let mut values: Vec<Option<Value>> = Vec::with_capacity(block.reads.len());
        for r in &block.reads {
            let name = r.name.as_str();
            if missing.contains_key(name) {
                failed.insert(name, Quality::BadConfig);
                values.push(None);
                continue;
            }
            let Some(res) = results.get(name) else {
                failed.insert(name, Quality::BadComm);
                values.push(None);
                continue;
            };
            match result_quality(res) {
                None => values.push(res.value.clone()),
                Some(q) => {
                    failed.insert(name, q);
                    values.push(None);
                    debug!(device = %self.device.id, name, quality = %q, status = ?res.status, ext_status = ?res.ext_status, error = ?res.error, "ab_logix tag error");
                }
            }
        }
        let decoded = decode_block(block, Some(&values));
        let mut out = HashMap::with_capacity(block.items.len());
        for item in &block.items {
            let name = block.reads[item.index].name.as_str();
            if let Some(q) = failed.get(name) {
                out.insert(item.tag.clone(), TagValue::bad(*q, Some(ts)));
                continue;
            }
            match decoded.get(&item.tag) {
                Some(Ok(v)) => {
                    out.insert(item.tag.clone(), TagValue::good(v.clone(), ts));
                }
                other => {
                    debug!(device = %self.device.id, tag = %item.tag, reason = ?other, "ab_logix decode error");
                    out.insert(item.tag.clone(), TagValue::bad(Quality::BadDecode, Some(ts)));
                }
            }
        }
        out
    }
}
