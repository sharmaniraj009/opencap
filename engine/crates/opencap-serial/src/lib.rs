//! Serial lines for the serial PLC drivers (Modbus RTU, later Allen-Bradley DF1), the Rust
//! counterpart of Python `src/opencap/drivers/_serial.py` (docs/decisions.md D-033):
//!
//! - [`SerialSettings`]: baud rate, data bits, parity, stop bits from `Device.params`, with the
//!   Python validation messages and character times;
//! - [`Endpoint`] / [`endpoint_for`]: where a line is reached (`params.transport`: `tcp` = raw
//!   TCP to a serial-over-TCP gateway, `serial` = a local port named in `Device.host`, `rfc2217`,
//!   `unmapped`);
//! - [`LinkPool`]: every device on one physical line shares one open connection (a local port
//!   opens once, many gateways accept one TCP client); an error drops it for all of them;
//! - [`open_stream`]: opens a TCP or local serial byte stream for an endpoint.

pub mod echo;
pub mod endpoint;
pub mod link;
pub mod settings;
pub mod transport;

pub use echo::LocalEcho;
pub use endpoint::{endpoint_for, serial_device_path, Endpoint, EndpointError, Kind, TRANSPORTS};
pub use link::{new_owner_id, IoGuard, Lease, LinkKey, LinkPool};
pub use settings::{Parity, SerialSettings};
pub use transport::{open_stream, ByteStream};

use serde_json::Value as Json;

/// Python `str(value)` of a JSON value (`'COM4'` -> `COM4`, `true` -> `True`, `null` -> `None`).
pub(crate) fn py_str(value: &Json) -> String {
    match value {
        Json::String(s) => s.clone(),
        other => opencap_core::pyfmt::py_repr_json(other),
    }
}

/// Python truthiness of a JSON value (`x or default`).
pub(crate) fn py_truthy(value: Option<&Json>) -> bool {
    match value {
        None | Some(Json::Null) => false,
        Some(Json::Bool(b)) => *b,
        Some(Json::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Json::String(s)) => !s.is_empty(),
        Some(Json::Array(a)) => !a.is_empty(),
        Some(Json::Object(o)) => !o.is_empty(),
    }
}
