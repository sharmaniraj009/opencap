//! Typed view of `Device.params` for omron_fins (docs/driver_params.md). Unknown keys are
//! ignored; `null` means the default.

use std::net::IpAddr;

use serde_json::{Map, Value as Json};

use opencap_core::config::Device;
use opencap_core::pyfmt::py_repr_json;

pub const MODELS: [&str; 3] = ["CS1", "CJ1", "CJ2"];
/// KEPServerEX "Request Size" choices, bytes.
pub const REQUEST_SIZES: [i64; 7] = [32, 64, 128, 256, 512, 1024, 1984];
/// 0101 over Ethernet: (2012 - 14) / 2 words (W342 5-2-2).
pub const MAX_READ_WORDS: u32 = 999;
/// 0104 items over Ethernet (W342 5-3-5).
pub const MAX_MULTI_ITEMS: usize = 167;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transport {
    Udp,
    Tcp,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FinsParams {
    pub model: String,
    pub transport: Transport,
    /// DNA, DA1, DA2: the CPU. DA1 None = the last octet of the device's IPv4 address
    /// (KEPServerEX's default); over TCP the node the unit reports in the handshake is used.
    pub dest_network: u8,
    pub dest_node: Option<u8>,
    pub dest_unit: u8,
    /// SNA, SA1, SA2: this client. SA1 None = UDP: the last octet of the local address;
    /// TCP: let the unit allocate one (handshake with node 0).
    pub src_network: u8,
    pub src_node: Option<u8>,
    pub src_unit: u8,
    /// gateway count (GCT): 2, or 7 for 8-level networks
    pub gateway_count: u8,
    pub request_size: u32,
    pub max_gap: u32,
    pub first_word_low: bool,
    /// Timer and counter present values are BCD (the CS/CJ default; false = binary mode)
    pub tc_bcd: bool,
    pub multi_read: bool,
    /// UDP: local port to bind; 0 = ephemeral, 9600 for units in static or IP-table mode
    pub local_port: u16,
}

fn get<'a>(p: &'a Map<String, Json>, key: &str) -> Option<&'a Json> {
    p.get(key).filter(|v| !v.is_null())
}

fn int(p: &Map<String, Json>, key: &str, lo: i64, hi: i64) -> Result<Option<i64>, String> {
    match get(p, key) {
        None => Ok(None),
        Some(Json::Number(n)) if n.is_i64() => {
            let v = n.as_i64().expect("i64");
            if (lo..=hi).contains(&v) {
                Ok(Some(v))
            } else {
                Err(format!("omron_fins param {key}={v}: must be {lo}..{hi}"))
            }
        }
        Some(v) => Err(format!("omron_fins param {key}={}: expected an integer", py_repr_json(v))),
    }
}

fn boolean(p: &Map<String, Json>, key: &str, default: bool) -> Result<bool, String> {
    match get(p, key) {
        None => Ok(default),
        Some(Json::Bool(b)) => Ok(*b),
        Some(Json::Number(n)) if n.as_i64() == Some(0) || n.as_i64() == Some(1) => Ok(n.as_i64() == Some(1)),
        Some(v) => Err(format!("omron_fins param {key}={}: expected true or false", py_repr_json(v))),
    }
}

impl FinsParams {
    pub fn from_device(device: &Device) -> Result<Self, String> {
        let p = &device.params;
        let model = match get(p, "model") {
            None => "CJ1".to_string(),
            Some(Json::String(s)) => {
                let m = s.trim().to_uppercase();
                MODELS.iter().find(|x| m == **x).map(|x| x.to_string()).ok_or_else(|| {
                    format!(
                        "omron_fins param model={}: must be CS1, CJ1 or CJ2 (C-mode and CV-mode CPUs are not supported)",
                        py_repr_json(&Json::String(s.clone()))
                    )
                })?
            }
            Some(v) => return Err(format!("omron_fins param model={}: must be CS1, CJ1 or CJ2", py_repr_json(v))),
        };
        let transport = match get(p, "transport") {
            None => Transport::Udp,
            Some(Json::String(s)) if s.trim().eq_ignore_ascii_case("udp") => Transport::Udp,
            Some(Json::String(s)) if s.trim().eq_ignore_ascii_case("tcp") => Transport::Tcp,
            Some(v) => return Err(format!("omron_fins param transport={}: must be udp or tcp", py_repr_json(v))),
        };
        let request_size = match get(p, "request_size") {
            None => 512,
            Some(Json::Number(n)) if n.as_i64().is_some_and(|v| REQUEST_SIZES.contains(&v)) => n.as_i64().expect("i64"),
            Some(v) => {
                return Err(format!(
                    "omron_fins param request_size={}: must be one of 32, 64, 128, 256, 512, 1024, 1984 (bytes)",
                    py_repr_json(v)
                ))
            }
        };
        let gateway_count = int(p, "gateway_count", 0, 7)?.unwrap_or(2) as u8;
        let out = Self {
            model,
            transport,
            dest_network: int(p, "dest_network", 0, 127)?.unwrap_or(0) as u8,
            dest_node: int(p, "dest_node", 1, 254)?.map(|v| v as u8),
            dest_unit: int(p, "dest_unit", 0, 255)?.unwrap_or(0) as u8,
            src_network: int(p, "src_network", 0, 127)?.unwrap_or(0) as u8,
            src_node: int(p, "src_node", 1, 254)?.map(|v| v as u8),
            src_unit: int(p, "src_unit", 0, 255)?.unwrap_or(0) as u8,
            gateway_count,
            request_size: request_size as u32,
            max_gap: int(p, "max_gap", 0, 999)?.unwrap_or(8) as u32,
            first_word_low: boolean(p, "first_word_low", true)?,
            tc_bcd: boolean(p, "tc_bcd", true)?,
            multi_read: boolean(p, "multi_read", true)?,
            local_port: int(p, "local_port", 0, 65535)?.unwrap_or(0) as u16,
        };
        if out.transport == Transport::Udp && out.dest_node.is_none() && default_node(&device.host).is_none() {
            return Err(format!(
                "omron_fins: host {} is not an IPv4 address, so the CPU's FINS node cannot default to its last octet; set params.dest_node",
                py_repr_json(&Json::String(device.host.clone()))
            ));
        }
        Ok(out)
    }

    /// Largest read in bytes: KEPServerEX's Request Size, at most 999 words.
    pub fn max_bytes(&self) -> u32 {
        self.request_size.min(MAX_READ_WORDS * 2)
    }
}

/// KEPServerEX default node number: the last octet of an IPv4 address (1..254).
pub fn default_node(host: &str) -> Option<u8> {
    match host.trim().parse::<IpAddr>().ok()? {
        IpAddr::V4(v4) => Some(v4.octets()[3]).filter(|n| (1..=254).contains(n)),
        IpAddr::V6(_) => None,
    }
}
