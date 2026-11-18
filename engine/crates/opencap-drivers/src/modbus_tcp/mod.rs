//! Modbus TCP driver, following Kepware "Modbus TCP/IP Ethernet" semantics (Python
//! `src/opencap/drivers/modbus_tcp.py`).
//!
//! The pure part (address parser, block planner, register decoder) needs no socket. The
//! I/O part uses tokio-modbus with one TCP connection per device. The engine is read-only:
//! only FC01/02/03/04 and FC43/14 are ever sent; there is no write path.
//!
//! I/O contract (docs/phase1_contracts.md §1): every call makes one attempt bounded by
//! `device.timeout_ms`. Timeout -> close the socket when `close_on_timeout`, then
//! `CommError`. Socket gone -> `ConnectionLost`. Exceptions 01/02/03 -> `BlockConfigError`;
//! other exception codes -> `CommError`. Retries, backoff and scaling belong to the worker.

pub mod address;
pub mod decode;
pub mod params;
pub mod plan;

use std::collections::{BTreeMap, HashMap};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{Map, Value as Json};
use tokio_modbus::client::{Context, Reader};
use tokio_modbus::prelude::ReadCode;
use tokio_modbus::Slave;
use tracing::debug;

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, BlockRef, Driver, DriverError, Plan, ReadResult};
use opencap_core::value::{now_utc, Quality, TagValue};

pub use address::{parse_address, ModbusAddress};
pub use decode::{decode_block, DecodeError, Reply};
pub use params::ModbusParams;
pub use plan::{plan_blocks, ModbusBlock, ModbusItem};

/// The device permanently rejects the request: the block is wrong, retrying cannot help.
pub const CONFIG_EXCEPTIONS: [u8; 3] = [1, 2, 3];

pub fn exception_name(code: u8) -> &'static str {
    match code {
        1 => "illegal function",
        2 => "illegal data address",
        3 => "illegal data value",
        4 => "server device failure",
        5 => "acknowledge",
        6 => "server device busy",
        7 => "negative acknowledge",
        8 => "memory parity error",
        10 => "gateway path unavailable",
        11 => "gateway target device failed to respond",
        _ => "unknown",
    }
}

pub fn exception_text(code: u8) -> String {
    format!("Modbus exception {code:02} ({})", exception_name(code))
}

/// FC43/14 object ids (Modbus Application Protocol v1.1b3, 6.21).
fn identity_object(id: u8) -> String {
    match id {
        0x00 => "vendor".into(),
        0x01 => "product_code".into(),
        0x02 => "revision".into(),
        0x03 => "vendor_url".into(),
        0x04 => "product_name".into(),
        0x05 => "model_name".into(),
        0x06 => "user_application_name".into(),
        other => format!("object_{other:#04x}"),
    }
}

pub struct ModbusTcpDriver {
    device: Device,
    pub params: ModbusParams,
    ctx: Option<Context>,
}

enum Data {
    Registers(Vec<u16>),
    Bits(Vec<bool>),
}

impl ModbusTcpDriver {
    /// Errors (bad params) carry the same text as Python's ValueError.
    pub fn new(device: &Device) -> Result<Self, String> {
        Ok(Self { params: ModbusParams::from_map(&device.params)?, device: device.clone(), ctx: None })
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        format!("{} ({}:{})", self.device.id, self.device.host, self.device.port)
    }

    /// Pure planning on tags (also used by tests).
    pub fn plan_tags<'a>(&self, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
        let mut parsed = Vec::new();
        let mut invalid = BTreeMap::new();
        for tag in tags {
            if !tag.enabled {
                continue;
            }
            match parse_address(&tag.address, tag.dtype.as_str(), &self.params) {
                Ok(a) => parsed.push((tag.name.clone(), a)),
                Err(e) => {
                    invalid.insert(tag.name.clone(), e);
                }
            }
        }
        let (blocks, too_big) = plan_blocks(&parsed, &self.params);
        invalid.extend(too_big);
        Plan { blocks: blocks.into_iter().map(|b| std::sync::Arc::new(b) as BlockRef).collect(), invalid }
    }

    async fn request(&mut self, block: &ModbusBlock) -> Result<Data, DriverError> {
        let where_ = self.where_();
        let what = block.describe();
        let timeout = self.timeout();
        let timeout_ms = self.device.timeout_ms;
        let unit = self.params.unit_id;
        let ctx = self.ctx.as_mut().ok_or_else(|| DriverError::ConnectionLost(format!("{where_}: not connected")))?;
        let started = Instant::now();
        let (addr, cnt) = (block.start as u16, block.count as u16);
        // The planner emits FC01-04 only; an unknown code must never silently become a read of
        // another table (ADR-0004 section 5).
        if !(1..=4).contains(&block.fc) {
            return Err(DriverError::BlockConfig(format!(
                "{where_} {what}: unsupported Modbus function code {}",
                block.fc
            )));
        }
        let fut = async {
            match block.fc {
                1 => ctx.read_coils(addr, cnt).await.map(|r| r.map(Data::Bits)),
                2 => ctx.read_discrete_inputs(addr, cnt).await.map(|r| r.map(Data::Bits)),
                3 => ctx.read_holding_registers(addr, cnt).await.map(|r| r.map(Data::Registers)),
                4 => ctx.read_input_registers(addr, cnt).await.map(|r| r.map(Data::Registers)),
                _ => unreachable!("function code checked above"),
            }
        };
        let res = tokio::time::timeout(timeout, fut).await;
        let ms = (started.elapsed().as_secs_f64() * 10_000.0).round() / 10.0;
        let dev = self.device.id.clone();
        match res {
            Err(_) => {
                let close = self.params.close_on_timeout;
                debug!(device = %dev, request = %what, unit, result = "timeout", closed = close, ms, "modbus request");
                if close {
                    self.ctx = None;
                }
                Err(DriverError::Comm(format!(
                    "{where_} {what}: no reply within {timeout_ms} ms{}",
                    if close { "; socket closed (close_on_timeout)" } else { "" }
                )))
            }
            Ok(Err(tokio_modbus::Error::Transport(e))) => {
                debug!(device = %dev, request = %what, unit, result = "conn_lost", error = %e, ms, "modbus request");
                self.ctx = None;
                Err(DriverError::ConnectionLost(format!("{where_} {what}: connection closed ({e})")))
            }
            Ok(Err(tokio_modbus::Error::Protocol(e))) => {
                debug!(device = %dev, request = %what, unit, result = "error", error = %e, ms, "modbus request");
                Err(DriverError::Comm(format!("{where_} {what}: {e}")))
            }
            Ok(Ok(Err(exc))) => {
                let code: u8 = exc.into();
                debug!(device = %dev, request = %what, unit, result = "exception", code, ms, "modbus request");
                let msg = format!("{where_} {what}: {}", exception_text(code));
                if CONFIG_EXCEPTIONS.contains(&code) {
                    Err(DriverError::BlockConfig(msg))
                } else {
                    Err(DriverError::Comm(msg))
                }
            }
            Ok(Ok(Ok(data))) => {
                debug!(device = %dev, request = %what, unit, result = "ok", ms, "modbus request");
                Ok(data)
            }
        }
    }
}

#[async_trait]
impl Driver for ModbusTcpDriver {
    fn name(&self) -> &'static str {
        "modbus_tcp"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        self.plan_tags(tags.iter().copied())
    }

    /// True while the TCP connection is open. False after close(), a peer close seen on a
    /// request, or a timeout with `close_on_timeout`.
    fn connected(&self) -> bool {
        self.ctx.is_some()
    }

    fn reports_connection(&self) -> bool {
        true
    }

    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let started = Instant::now();
        let target = (self.device.host.clone(), u16::try_from(self.device.port).unwrap_or(0));
        let res = tokio::time::timeout(self.timeout(), tokio::net::TcpStream::connect(target)).await;
        let ms = (started.elapsed().as_secs_f64() * 10_000.0).round() / 10.0;
        let reason = match res {
            Err(_) => format!("no answer within {} ms", self.device.timeout_ms),
            Ok(Err(e)) => format!("refused or unreachable ({e})"),
            Ok(Ok(stream)) => {
                let _ = stream.set_nodelay(true);
                let unit = Slave(self.params.unit_id as u8);
                self.ctx = Some(tokio_modbus::client::tcp::attach_slave(stream, unit));
                debug!(device = %self.device.id, result = "ok", ms, "modbus connect");
                return Ok(());
            }
        };
        debug!(device = %self.device.id, result = "failed", reason = %reason, ms, "modbus connect");
        Err(DriverError::ConnectionLost(format!("{}: connect failed: {reason}", self.where_())))
    }

    async fn close(&mut self) {
        if self.ctx.take().is_some() {
            debug!(device = %self.device.id, "modbus close");
        }
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let Some(block) = block.as_any().downcast_ref::<ModbusBlock>() else {
            return Err(DriverError::Other("modbus_tcp cannot read a non-Modbus block".into()));
        };
        if self.ctx.is_none() {
            return Err(DriverError::ConnectionLost(format!("{}: not connected", self.where_())));
        }
        let data = self.request(block).await?;
        let ts = now_utc();
        let decoded = match &data {
            Data::Registers(r) => decode_block(block, Reply::Registers(r), &self.params),
            Data::Bits(b) => decode_block(block, Reply::Bits(b), &self.params),
        };
        let mut out = HashMap::with_capacity(block.tags.len());
        for name in &block.tags {
            match decoded.get(name) {
                Some(Ok(v)) => {
                    out.insert(name.clone(), TagValue::good(v.clone(), ts));
                }
                Some(Err(e)) => {
                    debug!(device = %self.device.id, tag = %name, reason = %e.0, "modbus decode error");
                    out.insert(name.clone(), TagValue::bad(Quality::BadDecode, Some(ts)));
                }
                None => {
                    out.insert(name.clone(), TagValue::bad(Quality::BadDecode, Some(ts)));
                }
            }
        }
        Ok(out)
    }

    /// FC43/14 basic device identification. Empty when not connected or the device does
    /// not answer; never fails. A timeout here does not close the socket (many devices
    /// silently ignore FC43).
    async fn identity(&mut self) -> Map<String, Json> {
        let timeout = self.timeout();
        let Some(ctx) = self.ctx.as_mut() else { return Map::new() };
        let res = tokio::time::timeout(timeout, ctx.read_device_identification(ReadCode::Basic, 0x00)).await;
        let mut out = Map::new();
        if let Ok(Ok(Ok(reply))) = res {
            let mut objects = reply.device_id_objects;
            objects.sort_by_key(|o| o.id);
            for o in objects {
                let text: String = o.value.iter().map(|&b| b as char).filter(|&c| c != '\0').collect();
                let text = text.trim();
                if !text.is_empty() {
                    out.insert(identity_object(o.id), Json::String(text.to_string()));
                }
            }
        } else if let Ok(Err(tokio_modbus::Error::Transport(_))) = res {
            self.ctx = None;
        }
        out
    }
}

impl Drop for ModbusTcpDriver {
    fn drop(&mut self) {
        // Dropping the context closes the socket; disconnect() is async and not needed.
        let _ = self.ctx.take();
    }
}
