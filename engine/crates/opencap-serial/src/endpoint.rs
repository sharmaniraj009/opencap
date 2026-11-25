//! Where a serial line is reached (Python `_serial.Endpoint` / `endpoint_for`).

use std::fmt;

use opencap_core::config::Device;
use opencap_core::pyfmt::py_repr_json;
use serde_json::Value as Json;

use crate::settings::SerialSettings;
use crate::{py_str, py_truthy};

/// `params.transport` values, in Python's order.
pub const TRANSPORTS: [&str; 4] = ["tcp", "serial", "rfc2217", "unmapped"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    /// raw TCP to a serial-over-TCP gateway / virtual COM server: the socket carries the line's bytes
    Tcp,
    /// a local serial port; `host` is the device path (`/dev/ttyUSB0`, `COM3`)
    Serial,
    /// a Telnet COM-port-control server (RFC 2217)
    Rfc2217,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Tcp => "tcp",
            Kind::Serial => "serial",
            Kind::Rfc2217 => "rfc2217",
        }
    }

    fn parse(text: &str) -> Option<Kind> {
        match text {
            "tcp" => Some(Kind::Tcp),
            "serial" => Some(Kind::Serial),
            "rfc2217" => Some(Kind::Rfc2217),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Endpoint {
    pub kind: Kind,
    pub host: String,
    pub port: u16,
    pub settings: SerialSettings,
}

impl Endpoint {
    /// Identity of the physical line: settings are not part of it (one line, one setting).
    /// Host names compare without case; device paths as written.
    pub fn key(&self) -> (Kind, String, u16) {
        let host = if self.kind == Kind::Serial { self.host.clone() } else { self.host.to_lowercase() };
        (self.kind, host, self.port)
    }

    /// `serial /dev/ttyUSB0 19200 8N1`, `tcp 192.0.2.50:4001`.
    pub fn describe(&self) -> String {
        match self.kind {
            Kind::Serial => format!("serial {} {}", self.host, self.settings.describe()),
            kind => format!("{} {}:{}", kind.as_str(), self.host, self.port),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EndpointError {
    /// `transport: unmapped` (Python `TransportUnmapped`): the COM port is not mapped yet.
    Unmapped(String),
    /// Anything else wrong with the transport params (Python `ValueError`).
    Invalid(String),
}

impl fmt::Display for EndpointError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EndpointError::Unmapped(m) | EndpointError::Invalid(m) => f.write_str(m),
        }
    }
}

impl std::error::Error for EndpointError {}

/// The endpoint of a serial-line device, checking `transport` against the kinds the driver
/// supports. Same checks and messages as Python `endpoint_for`.
pub fn endpoint_for(device: &Device, allowed: &[Kind]) -> Result<Endpoint, EndpointError> {
    let params = &device.params;
    let raw = params.get("transport");
    let kind_text = if py_truthy(raw) { py_str(raw.expect("truthy")).trim().to_lowercase() } else { "tcp".to_string() };
    if !TRANSPORTS.contains(&kind_text.as_str()) {
        return Err(EndpointError::Invalid(format!(
            "{}: param transport={}: must be one of {}",
            device.id,
            py_repr_json(raw.unwrap_or(&Json::Null)),
            TRANSPORTS.join(", ")
        )));
    }
    if kind_text == "unmapped" {
        let com_raw = params.get("com_port");
        let com = if py_truthy(com_raw) {
            py_str(com_raw.expect("truthy"))
        } else if !device.host.is_empty() {
            device.host.clone()
        } else {
            "the serial port".to_string()
        };
        return Err(EndpointError::Unmapped(format!(
            "{}: {com} is not mapped to a transport; map it in overrides.yaml serial_ports \
             (e.g. {com}: 192.0.2.50:4001 for a serial-over-TCP gateway, or {com}: /dev/ttyUSB0)",
            device.id
        )));
    }
    let kind = Kind::parse(&kind_text).expect("checked against TRANSPORTS");
    if !allowed.contains(&kind) {
        let names: Vec<&str> = allowed.iter().map(|k| k.as_str()).collect();
        return Err(EndpointError::Invalid(format!(
            "{}: transport {kind_text} is not supported by {} (supported: {})",
            device.id,
            device.driver,
            names.join(", ")
        )));
    }
    let settings = SerialSettings::from_params(params).map_err(EndpointError::Invalid)?;
    let host = device.host.trim().to_string();
    if kind == Kind::Serial {
        if host.is_empty() {
            return Err(EndpointError::Invalid(format!(
                "{}: transport serial needs the device path in host (e.g. /dev/ttyUSB0)",
                device.id
            )));
        }
        return Ok(Endpoint { kind, host, port: 0, settings });
    }
    if host.is_empty() {
        return Err(EndpointError::Invalid(format!("{}: transport {kind_text} needs a host", device.id)));
    }
    if !(1..=65535).contains(&device.port) {
        return Err(EndpointError::Invalid(format!(
            "{}: transport {kind_text} needs a port 1..65535, got {} (host {}); map the COM port in \
             overrides.yaml serial_ports",
            device.id,
            device.port,
            opencap_core::pyfmt::py_repr_str(&host)
        )));
    }
    Ok(Endpoint { kind, host, port: device.port as u16, settings })
}

/// The path to open for a local serial port. Windows: the serial library always prefixes the
/// name with `\\.\` itself, so a configured `\\.\COM12` loses its prefix (names stay
/// case-insensitive, as Windows treats them). Elsewhere a `COMn` name cannot exist: the error
/// says to use the device path instead. Anything else is used as written.
pub fn serial_device_path(host: &str, windows: bool) -> Result<String, String> {
    let host = host.trim();
    let is_com =
        |s: &str| s.len() > 3 && s[..3].eq_ignore_ascii_case("com") && s[3..].bytes().all(|b| b.is_ascii_digit());
    if windows {
        let bare = host.strip_prefix(r"\\.\").or_else(|| host.strip_prefix("//./")).unwrap_or(host);
        return Ok(bare.to_string());
    }
    if is_com(host) || host.starts_with(r"\\.\") {
        return Err(format!(
            "{host} is a Windows COM port name; on this host use the device path (e.g. /dev/ttyUSB0) or map \
             the port in overrides.yaml serial_ports"
        ));
    }
    Ok(host.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn dev(host: &str, port: i64, params: Json) -> Device {
        let mut d = Device::new("SITE/PLC", "SITE", "PLC", "modbus_rtu", host, port, vec![]);
        d.params = params.as_object().unwrap().clone();
        d
    }

    const RTU: [Kind; 2] = [Kind::Tcp, Kind::Serial];

    #[test]
    fn tcp_is_the_default_and_hosts_compare_without_case() {
        let ep = endpoint_for(&dev("GW.example", 4001, json!({})), &RTU).unwrap();
        assert_eq!((ep.kind, ep.port), (Kind::Tcp, 4001));
        assert_eq!(ep.describe(), "tcp GW.example:4001");
        assert_eq!(ep.key(), (Kind::Tcp, "gw.example".to_string(), 4001));
        let ep = endpoint_for(&dev("192.0.2.50", 4001, json!({"transport": ""})), &RTU).unwrap();
        assert_eq!(ep.kind, Kind::Tcp);
        let ep =
            endpoint_for(&dev(" /dev/ttyUSB0 ", 0, json!({"transport": " Serial ", "baudrate": 9600})), &RTU).unwrap();
        assert_eq!((ep.kind, ep.host.as_str(), ep.port), (Kind::Serial, "/dev/ttyUSB0", 0));
        assert_eq!(ep.describe(), "serial /dev/ttyUSB0 9600 8N1");
    }

    #[test]
    fn errors_are_pythons() {
        let e = |host: &str, port, params| endpoint_for(&dev(host, port, params), &RTU).unwrap_err();
        assert_eq!(
            e("COM4", 0, json!({"transport": "unmapped", "com_port": "COM4"})),
            EndpointError::Unmapped(
                "SITE/PLC: COM4 is not mapped to a transport; map it in overrides.yaml serial_ports (e.g. COM4: \
                 192.0.2.50:4001 for a serial-over-TCP gateway, or COM4: /dev/ttyUSB0)"
                    .into()
            )
        );
        assert!(e("", 0, json!({"transport": "unmapped"})).to_string().contains("the serial port is not mapped"));
        assert_eq!(
            e("h", 1, json!({"transport": "udp"})).to_string(),
            "SITE/PLC: param transport='udp': must be one of tcp, serial, rfc2217, unmapped"
        );
        assert_eq!(
            e("h", 1, json!({"transport": "rfc2217"})).to_string(),
            "SITE/PLC: transport rfc2217 is not supported by modbus_rtu (supported: tcp, serial)"
        );
        assert_eq!(
            e("", 0, json!({"transport": "serial"})).to_string(),
            "SITE/PLC: transport serial needs the device path in host (e.g. /dev/ttyUSB0)"
        );
        assert_eq!(e("", 4001, json!({})).to_string(), "SITE/PLC: transport tcp needs a host");
        assert_eq!(
            e("COM112", 0, json!({})).to_string(),
            "SITE/PLC: transport tcp needs a port 1..65535, got 0 (host 'COM112'); map the COM port in overrides.yaml \
             serial_ports"
        );
        assert_eq!(e("h", 1, json!({"baudrate": 1})).to_string(), "serial param baudrate=1: must be 50..4000000");
    }

    #[test]
    fn com_port_names_on_windows_and_elsewhere() {
        assert_eq!(serial_device_path("COM3", true).unwrap(), "COM3");
        assert_eq!(serial_device_path(r"\\.\COM12", true).unwrap(), "COM12");
        assert_eq!(serial_device_path("//./com12", true).unwrap(), "com12");
        assert_eq!(serial_device_path(" COM7 ", true).unwrap(), "COM7");
        assert_eq!(serial_device_path("/dev/ttyUSB0", false).unwrap(), "/dev/ttyUSB0");
        assert_eq!(serial_device_path("/dev/pts/3", false).unwrap(), "/dev/pts/3");
        for windows_name in ["COM3", "com12", r"\\.\COM12"] {
            let err = serial_device_path(windows_name, false).unwrap_err();
            assert!(err.contains("is a Windows COM port name"), "{err}");
        }
        assert_eq!(serial_device_path("COMX", false).unwrap(), "COMX");
    }
}
