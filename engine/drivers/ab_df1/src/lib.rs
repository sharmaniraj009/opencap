//! Allen-Bradley DF1 full-duplex driver: SLC 500 / MicroLogix data tables over a serial line
//! (local port or a serial-over-TCP gateway). Written natively on `opencap-serial` because the
//! open-source libraries evaluated (libplctag) have no serial DF1 (docs/decisions.md D-120).
//!
//! DF1 carries the same PCCC commands as EtherNet/IP, so the address parser, planner and
//! decoder are `opencap-driver-ab-pccc`'s. Each block is one protected typed logical read
//! (CMD 0x0F FNC 0xA2, or 0xA1). Link layer: DLE STX / DLE ETX framing with BCC or CRC-16,
//! ACK/NAK/ENQ, duplicate detection. Replies are matched by TNS and CMD | 0x40.
//!
//! Read-only (ADR-0008): [`link::Df1Link::transact`] refuses every command except the three
//! read commands in `READ_COMMANDS`, and there is no write function to call. Not supported:
//! half-duplex master/slave, DH+/DH-485 routing, PLC-5 typed reads, unsolicited messages.

pub mod frame;
pub mod link;
pub mod params;
pub mod request;

use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{json, Map, Value as Json};
use tracing::{debug, warn};

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan, ReadResult};
use opencap_core::value::{now_utc, Quality, TagValue};
use opencap_driver_ab_pccc::status::{is_config_status, pccc_status_text};
use opencap_driver_ab_pccc::{decode_block, plan_pccc, PcccBlock, PcccParams};
use opencap_serial::{endpoint_for, new_owner_id, open_stream, Endpoint, Kind, Lease, LinkPool};

use frame::Df1Message;
use link::{Df1Link, LinkError};
use params::Df1Params;
use request::{as_sequence, pccc_values, typed_read_function, CMD_DIAGNOSTIC, CMD_EXTENDED, FNC_DIAGNOSTIC_STATUS};

/// A DF1 line is a serial port or a serial-over-TCP gateway.
pub const DF1_TRANSPORTS: [Kind; 2] = [Kind::Tcp, Kind::Serial];

static LINES: LazyLock<LinkPool<Df1Link>> = LazyLock::new(LinkPool::new);

pub struct AbDf1Driver {
    device: Device,
    pub params: Df1Params,
    pub plan_params: PcccParams,
    owner: u64,
    lease: Option<Lease<Df1Link>>,
    endpoint: Option<Endpoint>,
    rejected: std::collections::HashSet<String>,
}

impl AbDf1Driver {
    /// Errors (bad params) carry the Python `ValueError` text.
    pub fn new(device: &Device) -> Result<Self, String> {
        let params = Df1Params::from_map(&device.params)?;
        let plan_params = PcccParams::from_map(&device.params, "ab_df1")?;
        Ok(Self {
            device: device.clone(),
            params,
            plan_params,
            owner: new_owner_id(),
            lease: None,
            endpoint: None,
            rejected: Default::default(),
        })
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        let ep = match &self.endpoint {
            Some(ep) => ep.describe(),
            None => format!("{}:{}", self.device.host, self.device.port),
        };
        format!("{} (station {}, {ep})", self.device.id, self.params.station)
    }

    fn not_connected(&self) -> DriverError {
        DriverError::ConnectionLost(format!("{}: not connected", self.where_()))
    }

    async fn transact(
        &mut self,
        cmd: u8,
        function: Vec<u8>,
        what: &str,
    ) -> Result<(Df1Message, chrono::DateTime<chrono::Utc>), DriverError> {
        let where_ = self.where_();
        let (timeout, p) = (self.timeout(), self.params.clone());
        let ack = Duration::from_millis(p.effective_ack_timeout_ms(self.device.timeout_ms).max(1) as u64);
        let Some(lease) = self.lease.as_ref() else { return Err(self.not_connected()) };
        let Some(mut io) = lease.io().await else { return Err(self.not_connected()) };
        let link = io.conn();
        let request =
            Df1Message { dst: p.station, src: p.src_station, cmd, sts: 0, tns: link.next_tns(), data: function };
        let started = Instant::now();
        let res = link.transact(&request, timeout, ack, p.nak_retries, p.enq_retries).await;
        let ms = (started.elapsed().as_secs_f64() * 10_000.0).round() / 10.0;
        let dev = self.device.id.clone();
        match res {
            Ok(reply) => {
                debug!(device = %dev, request = %what, tns = request.tns, sts = reply.sts, ms, "df1 request");
                Ok((reply, now_utc()))
            }
            Err(LinkError::Closed(e)) => {
                io.drop_line();
                debug!(device = %dev, request = %what, result = "conn_lost", error = %e, "df1 request");
                Err(DriverError::ConnectionLost(format!("{where_} {what}: {e}")))
            }
            Err(LinkError::Failure(e)) => {
                let close = p.close_on_timeout;
                if close {
                    io.drop_line();
                }
                debug!(device = %dev, request = %what, result = "link_failure", error = %e, ms, "df1 request");
                Err(DriverError::Comm(format!(
                    "{where_} {what}: {e}{}",
                    if close { "; line closed (close_on_timeout)" } else { "" }
                )))
            }
            Err(LinkError::Refused(e)) => Err(DriverError::Other(e)),
        }
    }
}

#[async_trait]
impl Driver for AbDf1Driver {
    fn name(&self) -> &'static str {
        "ab_df1"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        plan_pccc(&self.plan_params, tags.iter().copied())
    }

    fn connected(&self) -> bool {
        self.lease.as_ref().is_some_and(|l| l.alive())
    }

    fn reports_connection(&self) -> bool {
        true
    }

    /// Open (or join) the line: one attempt, bounded by `device.timeout_ms`.
    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let endpoint =
            endpoint_for(&self.device, &DF1_TRANSPORTS).map_err(|e| DriverError::ConnectionLost(e.to_string()))?;
        self.endpoint = Some(endpoint.clone());
        let started = Instant::now();
        let (timeout, check, dup) = (self.timeout(), self.params.error_check, self.params.duplicate_detect);
        let (kind, host, port) = endpoint.key();
        let opener = || async {
            let stream = open_stream(&endpoint, timeout).await?;
            Ok::<_, String>(Df1Link::new(stream, check, dup))
        };
        match LINES.acquire(("ab_df1", kind, host, port), self.owner, opener).await {
            Ok(lease) => {
                // another device on the same line must use the same frame check
                if let Some(mut io) = lease.io().await {
                    let line_check = io.conn().check;
                    if line_check != check {
                        drop(io);
                        return Err(DriverError::ConnectionLost(format!(
                            "{}: error_check {} differs from {}, used by another device on the same line",
                            self.where_(),
                            check.as_str(),
                            line_check.as_str()
                        )));
                    }
                }
                let ms = (started.elapsed().as_secs_f64() * 10_000.0).round() / 10.0;
                debug!(device = %self.device.id, result = "ok", line = %endpoint.describe(), shared = lease.users() > 1, ms, "df1 connect");
                self.lease = Some(lease);
                Ok(())
            }
            Err(reason) => {
                debug!(device = %self.device.id, result = "failed", reason = %reason, "df1 connect");
                Err(DriverError::ConnectionLost(format!("{}: connect failed: {reason}", self.where_())))
            }
        }
    }

    async fn close(&mut self) {
        if self.lease.take().is_some() {
            debug!(device = %self.device.id, "df1 close");
        }
    }

    /// One block = one protected typed logical read.
    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let Some(block) = block.as_any().downcast_ref::<PcccBlock>() else {
            return Err(DriverError::Other("ab_df1 cannot read a non-PCCC block".into()));
        };
        let function = typed_read_function(block, self.params.read_fnc)
            .map_err(|e| DriverError::BlockConfig(format!("{} {}: {e}", self.where_(), block.describe())))?;
        let (reply, ts) = self.transact(CMD_EXTENDED, function, &block.address).await?;
        if reply.sts != 0 {
            let ext = reply.ext_sts();
            let text = pccc_status_text(reply.sts, ext);
            let msg = format!("{} {}: {text}", self.where_(), block.describe());
            if is_config_status(reply.sts, ext) {
                if self.rejected.insert(block.address.clone()) {
                    warn!(device = %self.device.id, block = %block.address, reason = %text, quality = "bad_config", "ab_df1.block_rejected");
                }
                return Err(DriverError::BlockConfig(msg));
            }
            return Err(DriverError::Comm(msg));
        }
        let data = reply.reply_data();
        if data.len() != block.nbytes() {
            debug!(device = %self.device.id, block = %block.address, got = data.len(), expected = block.nbytes(), "df1 reply size");
        }
        let raw = pccc_values(block, data);
        let seq = as_sequence(&raw);
        let decoded = decode_block(block, seq.as_deref());
        let mut out = HashMap::new();
        for item in &block.items {
            match decoded.get(&item.tag) {
                Some(Ok(v)) => {
                    out.insert(item.tag.clone(), TagValue::new(Some(v.clone()), Quality::Good, Some(ts)));
                }
                other => {
                    debug!(device = %self.device.id, tag = %item.tag, reason = ?other, "df1 decode error");
                    out.insert(item.tag.clone(), TagValue::new(None, Quality::BadDecode, Some(ts)));
                }
            }
        }
        Ok(out)
    }

    /// Diagnostic status (CMD 0x06 FNC 0x03): processor catalog number, plus the link setup and
    /// counters. Empty when not connected or the PLC does not answer. Never fails.
    async fn identity(&mut self) -> Map<String, Json> {
        if !self.connected() {
            return Map::new();
        }
        let Ok((reply, _)) = self.transact(CMD_DIAGNOSTIC, vec![FNC_DIAGNOSTIC_STATUS], "diagnostic status").await
        else {
            return Map::new();
        };
        let mut out = Map::new();
        out.insert("station".into(), json!(self.params.station));
        out.insert("link".into(), self.endpoint.as_ref().map_or(Json::Null, |e| json!(e.describe())));
        out.insert("error_check".into(), json!(self.params.error_check.as_str()));
        let data = reply.reply_data();
        if reply.sts == 0 && data.len() >= 16 {
            let catalog: String = data[5..16]
                .iter()
                .map(|&b| if b.is_ascii_graphic() || b == b' ' { b as char } else { '\u{0}' })
                .collect();
            let catalog = catalog.trim_matches(|c| c == ' ' || c == '\u{0}').to_string();
            if !catalog.is_empty() && !catalog.contains('\u{0}') {
                out.insert("processor_type".into(), json!(catalog));
            }
        } else {
            let text = if reply.sts != 0 { pccc_status_text(reply.sts, reply.ext_sts()) } else { "short reply".into() };
            out.insert("diagnostic_status".into(), json!(text));
        }
        if let Some(lease) = self.lease.as_ref() {
            if let Some(mut io) = lease.io().await {
                out.insert("link_stats".into(), io.conn().stats.as_json());
            }
        }
        out
    }
}
