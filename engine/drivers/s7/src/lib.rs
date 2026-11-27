//! Siemens S7 driver (S7-300/400/1200/1500) over ISO-on-TCP, with Kepware "Siemens TCP/IP
//! Ethernet" address semantics. Port of the Python `src/opencap/drivers/s7.py` (D-048 to D-052)
//! with a native S7comm client instead of python-snap7 (D-085).
//!
//! The pure part (address parser, block planner, decoder, frame builders and reply checks)
//! needs no socket. The engine is READ-ONLY: `proto.rs` builds only setup communication, read
//! var and the SZL read, and the connection refuses every other PDU before it reaches the socket.
//!
//! I/O contract (docs/phase1_contracts.md section 1): every request is one attempt bounded by
//! `device.timeout_ms`. Connect failure, reset or peer close -> `ConnectionLost`; no reply ->
//! connection dropped, `Comm`; malformed reply -> connection dropped, `Comm`; an item return
//! code or header error that says the DB/area/offset is wrong -> `BlockConfig`.

pub mod address;
pub mod conn;
pub mod errors;
pub mod pack;
pub mod params;
pub mod plan;
pub mod proto;
pub mod szl;

use std::collections::{HashMap, HashSet};
use std::sync::atomic::Ordering;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{json, Map, Value as Json};
use tracing::{debug, info, warn};

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan, ReadResult};
use opencap_core::value::{now_utc, Quality, TagValue};

pub use address::{parse_s7_address, S7Address};
pub use params::S7Params;
pub use plan::{decode_block, plan_blocks, plan_tags, DecodeError, S7Block, S7Item, SharedPdu};

use conn::{Conn, NetError};
use params::{connection_type_name, READ_OVERHEAD};
use proto::{ReadItem, ReplyError, WL_BYTE, WL_COUNTER, WL_TIMER};

/// Item return codes that mean the DB/area/offset is wrong or not accessible (S7-1200/1500
/// optimized block access, protection): retrying cannot help.
const CONFIG_RETURN_CODES: [u8; 5] = [0x03, 0x05, 0x06, 0x07, 0x0A];
/// Header errors that mean the same for every read of the block.
const CONFIG_HEADER_ERRORS: [(u16, &str); 1] =
    [(0x8104, "function not supported in this context (S7-1200/1500: is PUT/GET access enabled?)")];

pub fn return_code_text(rc: u8) -> String {
    let text = match rc {
        0x00 => "reserved",
        0x01 => "hardware fault",
        0x03 => "accessing the object not allowed",
        0x05 => "address out of range",
        0x06 => "data type not supported",
        0x07 => "data type inconsistent",
        0x0A => "object does not exist",
        0xFF => "success",
        _ => "unknown",
    };
    format!("item return code 0x{rc:02X} ({text})")
}

/// The negotiated PDU of every device, by device id and address. The registry plans a device's blocks with
/// its own driver instance and the worker reads them with another; both share the number, so
/// `wire_requests()` (the per-request read budget) follows the PDU the CPU actually granted.
static NEGOTIATED: LazyLock<Mutex<HashMap<String, SharedPdu>>> = LazyLock::new(Default::default);

fn negotiated_for(device: &Device) -> SharedPdu {
    let key = format!("{}@{}:{}", device.id, device.host, device.port);
    NEGOTIATED.lock().expect("pdu registry").entry(key).or_default().clone()
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Lost,
    Config,
    Comm,
}

#[derive(Clone)]
struct Failure {
    reason: String,
    kind: Kind,
}

pub struct S7Driver {
    device: Device,
    pub params: S7Params,
    port: u16,
    conn: Option<Conn>,
    negotiated: SharedPdu,
    rejected: HashSet<String>,
    pdu_logged: Option<usize>,
}

impl S7Driver {
    /// Errors (bad params) carry the same text as Python's `ValueError`.
    pub fn new(device: &Device) -> Result<Self, String> {
        let params = S7Params::from_map(&device.params)?;
        let port = u16::try_from(device.port).map_err(|_| format!("s7: port {} out of range", device.port))?;
        Ok(Self {
            device: device.clone(),
            params,
            port,
            conn: None,
            negotiated: negotiated_for(device),
            rejected: HashSet::new(),
            pdu_logged: None,
        })
    }

    /// Pure planning (also used by tests).
    pub fn plan_tags<'a>(&self, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
        plan_tags(&self.params, &self.negotiated, tags)
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        format!("{} ({}:{})", self.device.id, self.device.host, self.device.port)
    }

    fn drop_conn(&mut self) {
        if let Some(c) = self.conn.take() {
            c.close();
        }
        self.negotiated.store(0, Ordering::Relaxed);
    }

    /// Map a failed request to a `Failure`, dropping the connection when it can no longer be
    /// trusted.
    fn failure(&mut self, e: NetError) -> Failure {
        match e {
            NetError::Timeout => {
                self.drop_conn();
                Failure {
                    reason: format!("no reply within {} ms; connection closed", self.device.timeout_ms),
                    kind: Kind::Comm,
                }
            }
            NetError::Lost(l) => {
                self.drop_conn();
                Failure { reason: format!("connection lost: {l}"), kind: Kind::Lost }
            }
            NetError::Frame(f) => {
                self.drop_conn();
                Failure { reason: format!("bad reply: {f}; connection closed"), kind: Kind::Comm }
            }
            NetError::ReadOnly(why) => {
                Failure { reason: format!("refused by the read-only guard: {why}"), kind: Kind::Comm }
            }
            NetError::Reply(ReplyError::Header { code, text }) => {
                if let Some((_, why)) = CONFIG_HEADER_ERRORS.iter().find(|(c, _)| *c == code) {
                    Failure { reason: format!("S7 error 0x{code:04X}: {why}"), kind: Kind::Config }
                } else {
                    Failure { reason: text, kind: Kind::Comm }
                }
            }
            NetError::Reply(ReplyError::Malformed(m)) => {
                self.drop_conn();
                Failure { reason: format!("bad reply: {m}; connection closed"), kind: Kind::Comm }
            }
        }
    }

    async fn read_block(&mut self, block: &S7Block) -> Result<Vec<u8>, Failure> {
        let pdu = self.conn.as_ref().map_or(self.params.max_pdu, |c| c.pdu);
        let (word_len, area) = block_item_kind(block);
        let mut data = Vec::with_capacity(block.nbytes());
        for (start, count) in block.parts(pdu) {
            let item =
                ReadItem { area, db: block.db.min(0xFFFF) as u16, start: start as u32, word_len, count: count as u16 };
            let Some(conn) = self.conn.as_mut() else {
                return Err(Failure { reason: "connection closed".into(), kind: Kind::Lost });
            };
            let replies = match conn.read_var(&[item]).await {
                Ok(r) => r,
                Err(e) => return Err(self.failure(e)),
            };
            let reply = &replies[0];
            if reply.return_code != 0xFF {
                let kind = if CONFIG_RETURN_CODES.contains(&reply.return_code) { Kind::Config } else { Kind::Comm };
                return Err(Failure { reason: return_code_text(reply.return_code), kind });
            }
            data.extend_from_slice(&reply.data);
        }
        Ok(data)
    }

    /// The engine error for a failed block read; a rejected block is logged once.
    fn block_error(&mut self, block: &S7Block, f: Failure) -> DriverError {
        let text = format!("{} {}: {}", self.where_(), block.describe(), f.reason);
        match f.kind {
            Kind::Config => {
                self.log_rejected(block, &f.reason);
                DriverError::BlockConfig(text)
            }
            Kind::Lost => DriverError::ConnectionLost(text),
            Kind::Comm => DriverError::Comm(text),
        }
    }

    /// Decode a block's bytes into tag values (`BadDecode` per tag that does not fit).
    fn finish_block(&self, block: &S7Block, data: &[u8], started: Instant) -> HashMap<String, TagValue> {
        let ts = now_utc();
        debug!(device = %self.device.id, block = %block.describe(), requests = block.wire_requests(), ms = started.elapsed().as_millis() as u64, "s7 read");
        let decoded = decode_block(block, data, &self.params);
        let mut out = HashMap::with_capacity(block.tags.len());
        for name in &block.tags {
            match decoded.get(name) {
                Some(Ok(v)) => {
                    out.insert(name.clone(), TagValue::good(v.clone(), ts));
                }
                other => {
                    debug!(device = %self.device.id, tag = %name, reason = ?other, "s7 decode error");
                    out.insert(name.clone(), TagValue::bad(Quality::BadDecode, Some(ts)));
                }
            }
        }
        out
    }

    fn log_rejected(&mut self, block: &S7Block, reason: &str) {
        let what = block.describe();
        if !self.rejected.insert(what.clone()) {
            return;
        }
        warn!(
            device = %self.device.id, block = %what, tags = ?&block.tags[..block.tags.len().min(20)],
            count = block.tags.len(), reason, quality = "bad_config", "s7.block_rejected"
        );
    }
}

/// (word length, area code) of a block's read items.
fn block_item_kind(block: &S7Block) -> (u8, u8) {
    match block.area {
        address::Area::T => (WL_TIMER, block.area.code()),
        address::Area::C => (WL_COUNTER, block.area.code()),
        a => (WL_BYTE, a.code()),
    }
}

fn text(raw: &[u8]) -> Json {
    let end = raw.iter().position(|b| *b == 0).unwrap_or(raw.len());
    let s: String = raw[..end].iter().map(|c| char::from(*c)).collect();
    let s = s.trim();
    if s.is_empty() {
        Json::Null
    } else {
        Json::String(s.to_string())
    }
}

#[async_trait]
impl Driver for S7Driver {
    fn name(&self) -> &'static str {
        "s7"
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

    /// TCP connect, COTP connection request and setup communication: three waits.
    fn connect_timeout(&self) -> Option<Duration> {
        Some(self.timeout() * 3 + Duration::from_secs(1))
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let started = Instant::now();
        let p = &self.params;
        let tsap = proto::remote_tsap(p.connection_type, p.rack, p.slot);
        match Conn::open(&self.device.host, self.port, tsap, p.max_pdu as u16, self.timeout()).await {
            Ok(c) => {
                let pdu = c.pdu;
                debug!(device = %self.device.id, pdu, ms = started.elapsed().as_millis() as u64, "s7 connect ok");
                self.negotiated.store(pdu, Ordering::Relaxed);
                self.conn = Some(c);
                if pdu < self.params.max_pdu && self.pdu_logged != Some(pdu) {
                    self.pdu_logged = Some(pdu);
                    info!(
                        device = %self.device.id, requested = self.params.max_pdu, negotiated = pdu,
                        "s7.pdu_negotiated: blocks above {} bytes are read in several requests; set params.max_pdu={pdu} to plan blocks that fit",
                        pdu - READ_OVERHEAD
                    );
                }
                Ok(())
            }
            Err(reason) => {
                debug!(device = %self.device.id, reason, "s7 connect failed");
                Err(DriverError::ConnectionLost(format!("{}: connect failed: {reason}", self.where_())))
            }
        }
    }

    async fn close(&mut self) {
        self.drop_conn();
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let block = block
            .as_any()
            .downcast_ref::<S7Block>()
            .ok_or_else(|| DriverError::Other("s7 cannot read a block of another driver".into()))?;
        if self.conn.is_none() {
            return Err(DriverError::ConnectionLost(format!("{}: not connected", self.where_())));
        }
        let started = Instant::now();
        match self.read_block(block).await {
            Ok(data) => Ok(self.finish_block(block, &data, started)),
            Err(f) => Err(self.block_error(block, f)),
        }
    }

    fn supports_batch(&self) -> bool {
        true
    }

    /// Several blocks in as few read var requests as the negotiated PDU allows (`pack`). A request
    /// that fails (timeout, lost connection, header error) fails the blocks that had an item in
    /// it; an item return code fails only its own block; the others are decoded as usual.
    async fn read_many(&mut self, blocks: &[&dyn Block]) -> Vec<ReadResult> {
        let mut s7: Vec<Option<&S7Block>> = blocks.iter().map(|b| b.as_any().downcast_ref::<S7Block>()).collect();
        let Some(pdu) = self.conn.as_ref().map(|c| c.pdu) else {
            let e = DriverError::ConnectionLost(format!("{}: not connected", self.where_()));
            return blocks.iter().map(|_| Err(e.clone())).collect();
        };
        let started = Instant::now();
        // every part of every block, in block order
        let mut items: Vec<ReadItem> = Vec::new();
        let mut owner: Vec<(usize, usize)> = Vec::new(); // (block, part)
        let mut parts: Vec<Vec<Option<Vec<u8>>>> = vec![Vec::new(); blocks.len()];
        for (bi, b) in s7.iter().enumerate() {
            let Some(block) = b else { continue };
            let (word_len, area) = block_item_kind(block);
            let list = block.parts(pdu);
            parts[bi] = vec![None; list.len()];
            for (pi, (start, count)) in list.into_iter().enumerate() {
                items.push(ReadItem {
                    area,
                    db: block.db.min(0xFFFF) as u16,
                    start: start as u32,
                    word_len,
                    count: count as u16,
                });
                owner.push((bi, pi));
            }
        }
        let mut failed: Vec<Option<Failure>> = vec![None; blocks.len()];
        let requests = pack::pack(&items, pdu);
        for group in &requests {
            let chunk: Vec<ReadItem> = group.iter().map(|&i| items[i]).collect();
            let Some(conn) = self.conn.as_mut() else {
                for &i in group {
                    failed[owner[i].0].get_or_insert(Failure { reason: "connection closed".into(), kind: Kind::Lost });
                }
                continue;
            };
            match conn.read_var(&chunk).await {
                Err(e) => {
                    let f = self.failure(e);
                    for &i in group {
                        failed[owner[i].0].get_or_insert(f.clone());
                    }
                }
                Ok(replies) => {
                    for (&i, reply) in group.iter().zip(replies) {
                        let (bi, pi) = owner[i];
                        if reply.return_code != 0xFF {
                            let kind = if CONFIG_RETURN_CODES.contains(&reply.return_code) {
                                Kind::Config
                            } else {
                                Kind::Comm
                            };
                            failed[bi].get_or_insert(Failure { reason: return_code_text(reply.return_code), kind });
                        } else {
                            parts[bi][pi] = Some(reply.data);
                        }
                    }
                }
            }
        }
        debug!(device = %self.device.id, blocks = blocks.len(), items = items.len(), requests = requests.len(), ms = started.elapsed().as_millis() as u64, "s7 read_many");
        let mut out = Vec::with_capacity(blocks.len());
        for bi in 0..blocks.len() {
            let Some(block) = s7[bi].take() else {
                out.push(Err(DriverError::Other("s7 cannot read a block of another driver".into())));
                continue;
            };
            if let Some(f) = failed[bi].take() {
                out.push(Err(self.block_error(block, f)));
                continue;
            }
            let mut data = Vec::with_capacity(block.nbytes());
            for p in std::mem::take(&mut parts[bi]) {
                data.extend(p.unwrap_or_default());
            }
            out.push(Ok(self.finish_block(block, &data, started)));
        }
        out
    }

    /// CPU identity from SZL 0x001C (component identification), 0x0011 (order code and
    /// firmware) and 0x0424 (operating mode), plus the negotiated PDU and connection type.
    /// Empty when not connected. A failed SZL read is reported as `<name>_error`.
    async fn identity(&mut self) -> Map<String, Json> {
        let mut out = Map::new();
        let Some(pdu) = self.conn.as_ref().map(|c| c.pdu) else { return out };
        let p = self.params.clone();
        out.insert("pdu_requested".into(), json!(p.max_pdu));
        out.insert("pdu_negotiated".into(), json!(pdu));
        out.insert("rack".into(), json!(p.rack));
        out.insert("slot".into(), json!(p.slot));
        out.insert("connection_type".into(), json!(connection_type_name(p.connection_type)));
        out.insert("byte_order".into(), json!(if p.byte_order_big { "big" } else { "little" }));
        for key in ["cpu_info", "order_code", "cpu_state"] {
            let (id, index) = match key {
                "cpu_info" => (0x001C, 0),
                "order_code" => (0x0011, 0),
                _ => (0x0424, 0),
            };
            let Some(conn) = self.conn.as_mut() else { break };
            let data = match conn.read_szl(id, index).await {
                Ok(d) => d,
                Err(e) => {
                    let fatal = matches!(e, NetError::Timeout | NetError::Lost(_) | NetError::Frame(_));
                    let reason = match &e {
                        NetError::Timeout => format!("no reply within {} ms", self.device.timeout_ms),
                        other => other.to_string(),
                    };
                    if fatal {
                        self.drop_conn();
                        out.insert(format!("{key}_error"), json!(format!("{reason}; connection closed")));
                        break;
                    }
                    out.insert(format!("{key}_error"), json!(reason));
                    continue;
                }
            };
            match key {
                "cpu_info" => {
                    let i = szl::parse_cpu_info(&data);
                    out.insert("module_type_name".into(), text(&i.module_type_name));
                    out.insert("serial_number".into(), text(&i.serial_number));
                    out.insert("as_name".into(), text(&i.as_name));
                    out.insert("module_name".into(), text(&i.module_name));
                    out.insert("copyright".into(), text(&i.copyright));
                }
                "order_code" => {
                    let oc = szl::parse_order_code(&data);
                    out.insert("order_code".into(), text(&oc.order_code));
                    if oc.version != (0, 0, 0) {
                        out.insert(
                            "firmware".into(),
                            json!(format!("V{}.{}.{}", oc.version.0, oc.version.1, oc.version.2)),
                        );
                    }
                }
                _ => match szl::parse_cpu_state(&data) {
                    Ok(s) => {
                        out.insert("cpu_state".into(), json!(s));
                    }
                    Err(e) => {
                        out.insert("cpu_state_error".into(), json!(e));
                    }
                },
            }
        }
        out
    }
}
