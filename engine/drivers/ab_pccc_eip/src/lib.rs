//! Allen-Bradley SLC 500 / MicroLogix / PLC-5 data tables over EtherNet/IP (driver `ab_pccc`),
//! read through libplctag 2.7.2 (C, MPL-2.0, vendored and linked statically, D-121).
//!
//! The address parser, planner and decoder are the shared `opencap-driver-ab-pccc` crate (the
//! same vectors as DF1). One block is one libplctag tag handle (name `N7:0`, `elem_count` =
//! the block's element count), read with `plc_tag_read` and decoded from its raw bytes.
//!
//! Read-only (ADR-0008): the FFI crate `opencap-plctag-sys` declares read functions only (no
//! `plc_tag_write`, no `plc_tag_set_*`), and `tests/read_only.rs` there fails if one appears.
//! libplctag's own write code is compiled in but no Rust code can reach it.
//!
//! Errors: a timeout is a comm error; a refused or broken gateway is `ConnectionLost` (every
//! handle is dropped and the worker reconnects); a PLC that rejects the file or element
//! (remote error, not found, out of bounds) is `BlockConfig`.

use std::collections::HashMap;
use std::net::ToSocketAddrs;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{Map, Value as Json};
use tracing::debug;

use opencap_core::config::{Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan, ReadResult};
use opencap_core::value::{now_utc, Quality, TagValue};
use opencap_driver_ab_pccc::values::{as_sequence, pccc_values};
use opencap_driver_ab_pccc::{decode_block, plan_pccc, PcccBlock, PcccParams};
use opencap_plctag_sys as ffi;

pub const DEFAULT_PORT: i64 = 44818;

use ffi::safe::{status_text, Tag as Handle};

/// libplctag status -> engine error class.
pub fn classify(rc: i32, what: &str) -> DriverError {
    let text = format!("{what}: {} ({rc})", status_text(rc));
    match rc {
        -32 => DriverError::Comm(format!("{what}: no reply in time ({})", status_text(rc))),
        // gateway, connection, socket, device: the line to the PLC is unusable
        -3 | -5 | -6 | -26 | -36 | -1 => DriverError::ConnectionLost(text),
        // the PLC says the file, element or type does not exist or cannot be read
        -4 | -7 | -9 | -19 | -20 | -27 | -29 | -33 | -34 | -35 | -18 | -2 => DriverError::BlockConfig(text),
        _ => DriverError::Comm(text),
    }
}

pub struct AbPcccEipDriver {
    device: Device,
    pub params: PcccParams,
    cpu: String,
    handles: HashMap<String, Arc<Handle>>,
    connected: bool,
}

impl AbPcccEipDriver {
    pub fn new(device: &Device) -> Result<Self, String> {
        let params = PcccParams::from_map(&device.params, "ab_pccc")?;
        let cpu = match device.params.get("plc") {
            None | Some(Json::Null) => "slc500".to_string(),
            Some(Json::String(s)) if ["slc500", "micrologix", "plc5"].contains(&s.trim().to_lowercase().as_str()) => {
                s.trim().to_lowercase()
            }
            Some(other) => {
                return Err(format!(
                    "ab_pccc param plc={}: must be slc500, micrologix or plc5",
                    opencap_core::pyfmt::py_repr_json(other)
                ))
            }
        };
        Ok(Self { device: device.clone(), params, cpu, handles: HashMap::new(), connected: false })
    }

    fn gateway(&self) -> String {
        let port = if self.device.port > 0 { self.device.port } else { DEFAULT_PORT };
        format!("{}:{port}", self.device.host)
    }

    fn timeout(&self) -> Duration {
        Duration::from_millis(self.device.timeout_ms.max(1) as u64)
    }

    fn where_(&self) -> String {
        format!("{} ({})", self.device.id, self.gateway())
    }

    fn attributes(&self, block: &PcccBlock) -> (String, String) {
        let name = match (block.field, block.slot) {
            (Some(_), _) => format!("{}{}:{}", block.file_type, block.file_number, block.start),
            (None, Some(slot)) => format!("{}:{slot}.{}", block.file_type, block.start),
            _ if block.file_type == "S" => format!("S:{}", block.start),
            _ => format!("{}{}:{}", block.file_type, block.file_number, block.start),
        };
        let count = if block.field.is_some() { 1 } else { block.count };
        let mut attr = format!(
            "protocol=ab-eip&gateway={}&cpu={}&name={name}&elem_count={count}&allow_packing=0",
            self.gateway(),
            self.cpu
        );
        if !self.params.path.is_empty() {
            attr.push_str(&format!("&path={}", self.params.path.join(",")));
        }
        (attr, name)
    }
}

#[async_trait]
impl Driver for AbPcccEipDriver {
    fn name(&self) -> &'static str {
        "ab_pccc"
    }

    fn plan(&self, tags: &[&Tag]) -> Plan {
        plan_pccc(&self.params, tags.iter().copied())
    }

    fn connected(&self) -> bool {
        self.connected
    }

    fn reports_connection(&self) -> bool {
        true
    }

    /// One attempt: a TCP connect to the gateway, bounded by `timeout_ms`. libplctag opens its
    /// own session when the first tag is created.
    async fn connect(&mut self) -> Result<(), DriverError> {
        self.close().await;
        let gw = self.gateway();
        let addr = tokio::task::spawn_blocking({
            let gw = gw.clone();
            move || gw.to_socket_addrs().map(|mut a| a.next())
        })
        .await
        .map_err(|e| DriverError::ConnectionLost(format!("{}: {e}", self.where_())))?;
        let Ok(Some(addr)) = addr else {
            return Err(DriverError::ConnectionLost(format!("{}: connect failed: cannot resolve {gw}", self.where_())));
        };
        match tokio::time::timeout(self.timeout(), tokio::net::TcpStream::connect(addr)).await {
            Err(_) => Err(DriverError::ConnectionLost(format!(
                "{}: connect failed: no answer within {} ms",
                self.where_(),
                self.device.timeout_ms
            ))),
            Ok(Err(e)) => Err(DriverError::ConnectionLost(format!(
                "{}: connect failed: refused or unreachable ({e})",
                self.where_()
            ))),
            Ok(Ok(_)) => {
                self.connected = true;
                Ok(())
            }
        }
    }

    async fn close(&mut self) {
        self.handles.clear();
        self.connected = false;
    }

    async fn read(&mut self, block: &dyn Block) -> ReadResult {
        let Some(block) = block.as_any().downcast_ref::<PcccBlock>() else {
            return Err(DriverError::Other("ab_pccc cannot read a non-PCCC block".into()));
        };
        if !self.connected {
            return Err(DriverError::ConnectionLost(format!("{}: not connected", self.where_())));
        }
        let (attr, name) = self.attributes(block);
        let what = format!("{} {}", self.where_(), block.describe());
        let timeout_ms = self.device.timeout_ms.clamp(1, i64::from(i32::MAX)) as i32;
        let cached = self.handles.get(&block.address).cloned();
        let res = tokio::task::spawn_blocking(move || -> Result<(Arc<Handle>, Vec<u8>), i32> {
            let handle = match cached {
                Some(h) => h,
                None => Arc::new(Handle::create(&attr, timeout_ms)?),
            };
            let data = handle.read(timeout_ms)?;
            Ok((handle, data))
        })
        .await
        .map_err(|e| DriverError::Other(format!("{what}: {e}")))?;
        let (handle, data) = match res {
            Ok(x) => x,
            Err(rc) => {
                let err = classify(rc, &what);
                debug!(device = %self.device.id, block = %name, rc, "ab_pccc read failed");
                match &err {
                    // a refused file stays refused: keep the handle out so a reload retries clean
                    DriverError::ConnectionLost(_) => {
                        self.handles.clear();
                        self.connected = false;
                    }
                    DriverError::Comm(_) => {
                        self.handles.remove(&block.address);
                    }
                    _ => {}
                }
                return Err(err);
            }
        };
        self.handles.insert(block.address.clone(), handle);
        let ts = now_utc();
        let raw = pccc_values(block, &data);
        let seq = as_sequence(&raw);
        let decoded = decode_block(block, seq.as_deref());
        let mut out = HashMap::new();
        for item in &block.items {
            match decoded.get(&item.tag) {
                Some(Ok(v)) => {
                    out.insert(item.tag.clone(), TagValue::new(Some(v.clone()), Quality::Good, Some(ts)));
                }
                _ => {
                    out.insert(item.tag.clone(), TagValue::new(None, Quality::BadDecode, Some(ts)));
                }
            }
        }
        Ok(out)
    }

    async fn identity(&mut self) -> Map<String, Json> {
        Map::new()
    }
}
