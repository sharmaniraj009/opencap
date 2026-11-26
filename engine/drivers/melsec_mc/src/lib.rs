//! Mitsubishi MELSEC driver: MC protocol 3E frame, binary, over TCP or UDP, with Kepware
//! "Mitsubishi Ethernet" (Q Series) address semantics. Port of the Python
//! `src/opencap/drivers/melsec_mc.py` (D-053 to D-057) with a native client instead of
//! pymcprotocol (D-077).
//!
//! The pure part (address parser, block planner, word decoder, frame builders and reply checks)
//! needs no socket. The engine is READ-ONLY: `frame.rs` can build exactly two requests, batch
//! read in word units (0401/0000, 0401/0002 for iQ-R) and read CPU model (0101/0000), and the
//! connection refuses to send anything else.
//!
//! I/O contract (docs/phase1_contracts.md section 1): every request is one attempt bounded by
//! `device.timeout_ms`. Connect failure, refused, reset or peer close -> `ConnectionLost`; no
//! reply -> socket dropped, `Comm`; malformed reply -> socket dropped, `Comm`; an end code that
//! says the device, range or command is wrong -> `BlockConfig`, any other end code -> `Comm`
//! (the socket stays open for an end code).

pub mod address;
pub mod conn;
pub mod frame;
pub mod params;
pub mod plan;

use std::collections::HashSet;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{json, Map, Value as Json};
use tracing::{debug, warn};

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan, ReadResult};
use opencap_core::value::{now_utc, Quality, TagValue};

pub use address::{parse_address, McAddress};
pub use params::McParams;
pub use plan::{decode_block, plan_blocks, plan_tags, DecodeError, McBlock, McItem};

use conn::{Conn, NetError};
use frame::{
    batch_read_words, end_code_text, is_config_end_code, monitoring_timer, parse_reply, read_cpu_model, reply_words,
    Reply,
};

/// Why a request did not give data. `kind` decides the error class of the read.
struct Failure {
    reason: String,
    kind: Kind,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Lost,
    Config,
    Comm,
}

pub struct MelsecMcDriver {
    device: Device,
    pub params: McParams,
    port: u16,
    conn: Option<Conn>,
    /// Blocks already logged as rejected.
    rejected: HashSet<String>,
}

impl MelsecMcDriver {
    /// Errors (bad params) carry the same text as Python's `ValueError`.
    pub fn new(device: &Device) -> Result<Self, String> {
        let params = McParams::from_map(&device.params)?;
        let port = u16::try_from(device.port).map_err(|_| format!("melsec_mc: port {} out of range", device.port))?;
        Ok(Self { device: device.clone(), params, port, conn: None, rejected: HashSet::new() })
    }

    /// Pure planning (also used by tests).
    pub fn plan_tags<'a>(&self, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
        plan_tags(&self.params, tags)
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        format!("{} ({}:{}/{})", self.device.id, self.device.host, self.device.port, self.params.protocol)
    }

    /// One request/reply. Drops the socket when it can no longer be trusted.
    async fn call(&mut self, request: &[u8]) -> Result<Reply, Failure> {
        let Some(conn) = self.conn.as_mut() else {
            return Err(Failure { reason: "connection closed".into(), kind: Kind::Lost });
        };
        let timeout_ms = self.device.timeout_ms;
        let frame = match conn.exchange(request).await {
            Ok(f) => f,
            Err(NetError::ReadOnly(why)) => {
                // not reachable: the driver only builds reads
                return Err(Failure { reason: format!("refused by the read-only guard: {why}"), kind: Kind::Comm });
            }
            Err(e) => {
                self.conn = None;
                return Err(match e {
                    NetError::Timeout => Failure {
                        reason: format!(
                            "no reply within {timeout_ms} ms; socket closed (3E replies carry no sequence number)"
                        ),
                        kind: Kind::Comm,
                    },
                    NetError::Frame(f) => {
                        Failure { reason: format!("bad reply: {f}; socket closed"), kind: Kind::Comm }
                    }
                    NetError::Lost(l) => Failure { reason: format!("connection lost: {l}"), kind: Kind::Lost },
                    NetError::ReadOnly(_) => unreachable!("handled above"),
                });
            }
        };
        match parse_reply(&frame) {
            Ok(r) => Ok(r),
            Err(f) => {
                self.conn = None;
                Err(Failure { reason: format!("bad reply: {f}; socket closed"), kind: Kind::Comm })
            }
        }
    }

    /// Words of one batch read; the reply must carry exactly `count` of them.
    async fn read_words(&mut self, block: &McBlock) -> Result<Vec<u16>, Failure> {
        let spec = address::device_spec(block.device).expect("planned device");
        let timer = monitoring_timer(self.device.timeout_ms);
        let request = batch_read_words(&self.params, timer, spec.code, block.head_number() as u32, block.count as u16);
        match self.call(&request).await? {
            Reply::EndCode(code) => Err(Failure {
                reason: end_code_text(code),
                kind: if is_config_end_code(code) { Kind::Config } else { Kind::Comm },
            }),
            Reply::Data(data) => match reply_words(&data, block.count) {
                Ok(w) => Ok(w),
                Err(e) => {
                    self.conn = None;
                    Err(Failure { reason: format!("bad reply: {e}; socket closed"), kind: Kind::Comm })
                }
            },
        }
    }

    fn log_rejected(&mut self, block: &McBlock, reason: &str) {
        let what = block.describe();
        if !self.rejected.insert(what.clone()) {
            return;
        }
        warn!(
            device = %self.device.id, block = %what, tags = ?&block.tags[..block.tags.len().min(20)],
            count = block.tags.len(), reason, quality = "bad_config", "melsec.block_rejected"
        );
    }
}

#[async_trait]
impl Driver for MelsecMcDriver {
    fn name(&self) -> &'static str {
        "melsec_mc"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        self.plan_tags(tags.iter().copied())
    }

    /// True while the socket is believed usable. A TCP peer close is noticed on the next
    /// request (`ConnectionLost`); UDP has no connection, a dead PLC shows as timeouts.
    fn connected(&self) -> bool {
        self.conn.is_some()
    }

    fn reports_connection(&self) -> bool {
        true
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let started = Instant::now();
        match Conn::open(&self.device.host, self.port, self.params.protocol == "udp", self.timeout()).await {
            Ok(c) => {
                self.conn = Some(c);
                debug!(device = %self.device.id, plc_type = %self.params.plc_type, ms = started.elapsed().as_millis() as u64, "melsec connect ok");
                Ok(())
            }
            Err(reason) => {
                debug!(device = %self.device.id, reason, "melsec connect failed");
                Err(DriverError::ConnectionLost(format!("{}: connect failed: {reason}", self.where_())))
            }
        }
    }

    async fn close(&mut self) {
        self.conn = None;
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let block = block
            .as_any()
            .downcast_ref::<McBlock>()
            .ok_or_else(|| DriverError::Other("melsec_mc cannot read a block of another driver".into()))?;
        let where_ = self.where_();
        if self.conn.is_none() {
            return Err(DriverError::ConnectionLost(format!("{where_}: not connected")));
        }
        let what = block.describe();
        let started = Instant::now();
        let words = match self.read_words(block).await {
            Ok(w) => w,
            Err(f) => {
                let text = format!("{where_} {what}: {}", f.reason);
                return Err(match f.kind {
                    Kind::Config => {
                        self.log_rejected(block, &f.reason);
                        DriverError::BlockConfig(text)
                    }
                    Kind::Lost => DriverError::ConnectionLost(text),
                    Kind::Comm => DriverError::Comm(text),
                });
            }
        };
        let ts = now_utc();
        debug!(device = %self.device.id, block = %what, ms = started.elapsed().as_millis() as u64, "melsec read");
        let decoded = decode_block(block, &words, &self.params);
        let mut out = std::collections::HashMap::with_capacity(block.tags.len());
        for name in &block.tags {
            match decoded.get(name) {
                Some(Ok(v)) => {
                    out.insert(name.clone(), TagValue::good(v.clone(), ts));
                }
                Some(Err(e)) => {
                    debug!(device = %self.device.id, tag = %name, reason = %e.0, "melsec decode error");
                    out.insert(name.clone(), TagValue::bad(Quality::BadDecode, Some(ts)));
                }
                None => {
                    out.insert(name.clone(), TagValue::bad(Quality::BadDecode, Some(ts)));
                }
            }
        }
        Ok(out)
    }

    /// `{plc_type, protocol, frame, commtype, cpu_type, cpu_code}` from "read CPU model" (0101);
    /// on an error end code or no answer: what is known plus `cpu_type_error`. Empty when not
    /// connected. Never fails (a timeout closes the socket, as for a read).
    async fn identity(&mut self) -> Map<String, Json> {
        let mut out = Map::new();
        if self.conn.is_none() {
            return out;
        }
        out.insert("plc_type".into(), json!(self.params.plc_type));
        out.insert("protocol".into(), json!(self.params.protocol));
        out.insert("frame".into(), json!(self.params.frame));
        out.insert("commtype".into(), json!("binary"));
        let request = read_cpu_model(&self.params, monitoring_timer(self.device.timeout_ms));
        match self.call(&request).await {
            Err(f) => {
                out.insert("cpu_type_error".into(), json!(f.reason));
            }
            Ok(Reply::EndCode(code)) => {
                out.insert("cpu_type_error".into(), json!(end_code_text(code)));
            }
            Ok(Reply::Data(data)) => {
                if data.len() < 16 {
                    out.insert("cpu_type_error".into(), json!(format!("short CPU model reply: {} bytes", data.len())));
                } else {
                    let name = String::from_utf8_lossy(&data[..16]).replace(' ', "");
                    let code = data[16..].iter().rev().fold(0u64, |acc, b| (acc << 8) | u64::from(*b));
                    out.insert("cpu_type".into(), json!(name.trim()));
                    out.insert("cpu_code".into(), json!(format!("{code:04x}")));
                }
            }
        }
        out
    }
}
