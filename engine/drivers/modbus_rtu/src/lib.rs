//! Modbus RTU driver: a local serial port, or RTU frames on a TCP socket (a serial-over-TCP
//! gateway in transparent mode / RTU over TCP). Python `src/opencap/drivers/modbus_rtu.py`,
//! docs/decisions.md D-033, D-036.
//!
//! Everything above the wire is `modbus_tcp`'s: the Kepware address parser, the block
//! planner, the register decoder and every `ModbusParams` key (`unit_id`, word order, block
//! sizes, ...), so an RTU device plans and decodes exactly like a Modbus TCP device with the
//! same tags. The wire is tokio-modbus' RTU client over an `opencap-serial` byte stream (TCP or
//! a local port). Only FC01-04 and FC43/14 are ever sent; there is no write path.
//!
//! Serial-line specifics
//! - Every unit id on one line shares one connection ([`opencap_serial::LinkPool`]): a serial
//!   port opens once, many gateways accept one TCP client. An error that closes the line closes
//!   it for every unit on it; they reconnect on their next request.
//! - RTU has no transaction id: a reply arriving after its request timed out could be taken for
//!   the next request's. `close_on_timeout` (default true, as for Modbus TCP) closes the line
//!   after a timeout, throwing such late bytes away. A reply from another unit id, or with
//!   another function code, also closes it then (the line is out of step).
//! - CRC errors: tokio-modbus drops bytes until a frame with a valid CRC arrives; a corrupted
//!   reply is therefore silence, and the request ends in the timeout (CommError), as in Python.
//! - Inter-frame gap: before each request the driver waits until `inter_frame_ms` have passed
//!   since the last request/reply on the line (default 3.5 characters at `baudrate`, at least
//!   1.75 ms, Modbus over serial line V1.02 2.5.1.1).
//! - `local_echo: true` drops the echo of our own request that some 2-wire RS-485 adapters
//!   return (serial transport only, as pymodbus).

use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{Map, Value as Json};
use tokio_modbus::client::{Context, Reader};
use tokio_modbus::prelude::{ReadCode, SlaveContext};
use tokio_modbus::Slave;
use tracing::debug;

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan, ReadResult};
use opencap_core::pyfmt::py_repr_json;
use opencap_driver_modbus_tcp::{
    exception_text, identity_from_objects, plan_tags, values_from_reply, ModbusBlock, ModbusParams, Reply,
    CONFIG_EXCEPTIONS,
};
use opencap_serial::{
    endpoint_for, new_owner_id, open_stream, Endpoint, Kind, Lease, LinkPool, LocalEcho, SerialSettings,
};

/// Transports a Modbus RTU line can use (RFC 2217 is not offered, as in Python).
pub const RTU_TRANSPORTS: [Kind; 2] = [Kind::Tcp, Kind::Serial];
/// Modbus over serial line V1.02, 2.5.1.1: fixed above 19200 baud.
pub const MIN_INTER_FRAME_S: f64 = 0.00175;

/// The gap the driver keeps between frames on the line (Python `inter_frame_s`).
pub fn inter_frame_s(settings: &SerialSettings, inter_frame_ms: Option<&Json>) -> Result<f64, String> {
    let ms = match inter_frame_ms {
        None | Some(Json::Null) => return Ok((3.5 * settings.char_time_s()).max(MIN_INTER_FRAME_S)),
        Some(v @ Json::Number(n)) => (v, n.as_f64().unwrap_or(f64::NAN)),
        Some(v) => return Err(format!("modbus_rtu param inter_frame_ms={}: expected a number", py_repr_json(v))),
    };
    if !(0.0..=10_000.0).contains(&ms.1) {
        return Err(format!("modbus_rtu param inter_frame_ms={}: must be 0..10000", py_repr_json(ms.0)));
    }
    Ok(ms.1 / 1000.0)
}

/// One line's client, shared by its units.
pub struct RtuLine {
    ctx: Context,
    /// end of the last request/reply on the line
    last_io: Option<Instant>,
}

static LINES: LazyLock<LinkPool<RtuLine>> = LazyLock::new(LinkPool::new);

enum Data {
    Registers(Vec<u16>),
    Bits(Vec<bool>),
}

pub struct ModbusRtuDriver {
    device: Device,
    pub params: ModbusParams,
    pub settings: SerialSettings,
    /// seconds of silence kept between frames on the line
    pub gap_s: f64,
    pub local_echo: bool,
    owner: u64,
    lease: Option<Lease<RtuLine>>,
    endpoint: Option<Endpoint>,
}

impl ModbusRtuDriver {
    /// Errors (bad params) carry the same text as Python's ValueError, checked in the same order.
    pub fn new(device: &Device) -> Result<Self, String> {
        let params = ModbusParams::from_map(&device.params)?;
        let settings = SerialSettings::from_params(&device.params)?;
        let gap_s = inter_frame_s(&settings, device.params.get("inter_frame_ms"))?;
        let local_echo = match device.params.get("local_echo") {
            None => false,
            Some(Json::Bool(b)) => *b,
            Some(v) => return Err(format!("modbus_rtu param local_echo={}: expected true or false", py_repr_json(v))),
        };
        Ok(Self {
            device: device.clone(),
            params,
            settings,
            gap_s,
            local_echo,
            owner: new_owner_id(),
            lease: None,
            endpoint: None,
        })
    }

    pub fn plan_tags<'a>(&self, tags: impl IntoIterator<Item = &'a Tag>) -> Plan {
        plan_tags(&self.params, tags)
    }

    /// True when this driver and `other` use the same open connection (one line).
    pub fn shares_line_with(&self, other: &ModbusRtuDriver) -> bool {
        matches!((&self.lease, &other.lease), (Some(a), Some(b)) if a.same_connection(b))
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        let ep = match &self.endpoint {
            Some(ep) => ep.describe(),
            None => format!("{}:{}", self.device.host, self.device.port),
        };
        format!("{} (unit {}, {ep})", self.device.id, self.params.unit_id)
    }

    fn not_connected(&self) -> DriverError {
        DriverError::ConnectionLost(format!("{}: not connected", self.where_()))
    }

    async fn request(&mut self, block: &ModbusBlock) -> Result<Data, DriverError> {
        let where_ = self.where_();
        let what = block.describe();
        // The planner emits FC01-04 only; an unknown code must never silently become a read of
        // another table (ADR-0004 section 5).
        if !(1..=4).contains(&block.fc) {
            return Err(DriverError::BlockConfig(format!(
                "{where_} {what}: unsupported Modbus function code {}",
                block.fc
            )));
        }
        let (timeout, timeout_ms, unit) = (self.timeout(), self.device.timeout_ms, self.params.unit_id);
        let Some(lease) = self.lease.as_ref() else { return Err(self.not_connected()) };
        let Some(mut io) = lease.io().await else { return Err(self.not_connected()) };
        let line = io.conn();
        if let Some(last) = line.last_io {
            let ready_at = last + Duration::from_secs_f64(self.gap_s);
            tokio::time::sleep_until(ready_at.into()).await;
        }
        line.ctx.set_slave(Slave(unit as u8));
        let started = Instant::now();
        let (addr, cnt) = (block.start as u16, block.count as u16);
        let ctx = &mut line.ctx;
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
        line.last_io = Some(Instant::now());
        let ms = (started.elapsed().as_secs_f64() * 10_000.0).round() / 10.0;
        let dev = self.device.id.as_str();
        let close = self.params.close_on_timeout;
        match res {
            Err(_) => {
                debug!(device = %dev, request = %what, unit, result = "timeout", closed = close, ms, "modbus_rtu request");
                if close {
                    io.drop_line();
                }
                Err(DriverError::Comm(format!(
                    "{where_} {what}: no reply within {timeout_ms} ms{}",
                    if close { "; socket closed (close_on_timeout)" } else { "" }
                )))
            }
            Ok(Err(tokio_modbus::Error::Transport(e))) => {
                debug!(device = %dev, request = %what, unit, result = "conn_lost", error = %e, ms, "modbus_rtu request");
                io.drop_line();
                Err(DriverError::ConnectionLost(format!("{where_} {what}: connection closed ({e})")))
            }
            Ok(Err(tokio_modbus::Error::Protocol(e))) => {
                debug!(device = %dev, request = %what, unit, result = "error", error = %e, closed = close, ms, "modbus_rtu request");
                if close {
                    io.drop_line(); // out of step: no transaction id to resynchronise on
                }
                Err(DriverError::Comm(format!("{where_} {what}: {e}")))
            }
            Ok(Ok(Err(exc))) => {
                let code: u8 = exc.into();
                debug!(device = %dev, request = %what, unit, result = "exception", code, ms, "modbus_rtu request");
                let msg = format!("{where_} {what}: {}", exception_text(code));
                if CONFIG_EXCEPTIONS.contains(&code) {
                    Err(DriverError::BlockConfig(msg))
                } else {
                    Err(DriverError::Comm(msg))
                }
            }
            Ok(Ok(Ok(data))) => {
                debug!(device = %dev, request = %what, unit, result = "ok", ms, "modbus_rtu request");
                Ok(data)
            }
        }
    }
}

#[async_trait]
impl Driver for ModbusRtuDriver {
    fn name(&self) -> &'static str {
        "modbus_rtu"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        self.plan_tags(tags.iter().copied())
    }

    /// True while the line this driver joined is open: false after close(), a lost line, or a
    /// timeout with `close_on_timeout` on any unit of the line.
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
            endpoint_for(&self.device, &RTU_TRANSPORTS).map_err(|e| DriverError::ConnectionLost(e.to_string()))?;
        self.endpoint = Some(endpoint.clone());
        let started = Instant::now();
        let (timeout, unit, echo) = (self.timeout(), Slave(self.params.unit_id as u8), self.local_echo);
        let (kind, host, port) = endpoint.key();
        let opener = || async {
            let stream = open_stream(&endpoint, timeout).await?;
            let ctx = if echo && endpoint.kind == Kind::Serial {
                tokio_modbus::client::rtu::attach_slave(LocalEcho::new(stream), unit)
            } else {
                tokio_modbus::client::rtu::attach_slave(stream, unit)
            };
            Ok::<_, String>(RtuLine { ctx, last_io: None })
        };
        match LINES.acquire(("modbus_rtu", kind, host, port), self.owner, opener).await {
            Ok(lease) => {
                let ms = (started.elapsed().as_secs_f64() * 10_000.0).round() / 10.0;
                debug!(device = %self.device.id, result = "ok", line = %endpoint.describe(), shared = lease.users() > 1, ms, "modbus_rtu connect");
                self.lease = Some(lease);
                Ok(())
            }
            Err(reason) => {
                debug!(device = %self.device.id, result = "failed", reason = %reason, "modbus_rtu connect");
                Err(DriverError::ConnectionLost(format!("{}: connect failed: {reason}", self.where_())))
            }
        }
    }

    /// Leave the line (closed when no other unit uses it). Idempotent, never fails.
    async fn close(&mut self) {
        if self.lease.take().is_some() {
            debug!(device = %self.device.id, "modbus_rtu close");
        }
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let Some(block) = block.as_any().downcast_ref::<ModbusBlock>() else {
            return Err(DriverError::Other("modbus_rtu cannot read a non-Modbus block".into()));
        };
        let data = self.request(block).await?;
        let out: HashMap<_, _> = match &data {
            Data::Registers(r) => values_from_reply(block, Reply::Registers(r), &self.params, &self.device.id),
            Data::Bits(b) => values_from_reply(block, Reply::Bits(b), &self.params, &self.device.id),
        };
        Ok(out)
    }

    /// FC43/14 basic device identification. Empty when not connected or the unit does not
    /// answer; never fails. A timeout here does not close the line (many devices ignore FC43).
    async fn identity(&mut self) -> Map<String, Json> {
        let (timeout, unit, gap) = (self.timeout(), self.params.unit_id, self.gap_s);
        let Some(lease) = self.lease.as_ref() else { return Map::new() };
        let Some(mut io) = lease.io().await else { return Map::new() };
        let line = io.conn();
        if let Some(last) = line.last_io {
            tokio::time::sleep_until((last + Duration::from_secs_f64(gap)).into()).await;
        }
        line.ctx.set_slave(Slave(unit as u8));
        let res = tokio::time::timeout(timeout, line.ctx.read_device_identification(ReadCode::Basic, 0x00)).await;
        line.last_io = Some(Instant::now());
        match res {
            Ok(Ok(Ok(reply))) => identity_from_objects(reply.device_id_objects),
            Ok(Err(tokio_modbus::Error::Transport(_))) => {
                io.drop_line();
                Map::new()
            }
            _ => Map::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn settings(baud: u32) -> SerialSettings {
        SerialSettings { baudrate: baud, ..SerialSettings::default() }
    }

    #[test]
    fn inter_frame_gap_defaults_and_validation() {
        assert!((inter_frame_s(&settings(9600), None).unwrap() - 3.5 * 10.0 / 9600.0).abs() < 1e-12);
        assert_eq!(inter_frame_s(&settings(115200), None).unwrap(), MIN_INTER_FRAME_S);
        assert_eq!(inter_frame_s(&settings(19200), Some(&Json::Null)).unwrap(), 3.5 * 10.0 / 19200.0);
        assert_eq!(inter_frame_s(&settings(19200), Some(&json!(50))).unwrap(), 0.05);
        assert_eq!(inter_frame_s(&settings(19200), Some(&json!(0))).unwrap(), 0.0);
        assert_eq!(
            inter_frame_s(&settings(19200), Some(&json!("soon"))).unwrap_err(),
            "modbus_rtu param inter_frame_ms='soon': expected a number"
        );
        assert_eq!(
            inter_frame_s(&settings(19200), Some(&json!(true))).unwrap_err(),
            "modbus_rtu param inter_frame_ms=True: expected a number"
        );
        assert_eq!(
            inter_frame_s(&settings(19200), Some(&json!(10001))).unwrap_err(),
            "modbus_rtu param inter_frame_ms=10001: must be 0..10000"
        );
        assert_eq!(
            inter_frame_s(&settings(19200), Some(&json!(-0.5))).unwrap_err(),
            "modbus_rtu param inter_frame_ms=-0.5: must be 0..10000"
        );
    }
}
