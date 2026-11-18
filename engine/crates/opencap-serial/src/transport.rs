//! Byte streams for an [`Endpoint`]: TCP (serial-over-TCP gateway) or a local serial port.

use std::fmt::Debug;
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncWrite};

use crate::endpoint::{serial_device_path, Endpoint, Kind};
use crate::settings::{Parity, SerialSettings};

/// An open line: the bytes on it are the bytes on the serial wire.
pub trait ByteStream: AsyncRead + AsyncWrite + Unpin + Send + Debug {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send + Debug> ByteStream for T {}

/// Open the endpoint, bounded by `timeout`. The error text says why (no `connect failed:`
/// prefix: the driver adds its own context).
pub async fn open_stream(endpoint: &Endpoint, timeout: Duration) -> Result<Box<dyn ByteStream>, String> {
    let ms = timeout.as_millis();
    match endpoint.kind {
        Kind::Tcp => {
            match tokio::time::timeout(timeout, tokio::net::TcpStream::connect((endpoint.host.as_str(), endpoint.port)))
                .await
            {
                Err(_) => Err(format!("no answer within {ms} ms")),
                Ok(Err(e)) => Err(format!("refused or unreachable ({e})")),
                Ok(Ok(stream)) => {
                    let _ = stream.set_nodelay(true);
                    Ok(Box::new(stream))
                }
            }
        }
        Kind::Serial => {
            let path = serial_device_path(&endpoint.host, cfg!(windows))?;
            let builder = serial_builder(&path, &endpoint.settings)?;
            // Opening a port is a blocking system call; keep it off the reactor threads.
            let open = tokio::task::spawn_blocking(move || tokio_serial::SerialStream::open(&builder));
            match tokio::time::timeout(timeout, open).await {
                Err(_) => Err(format!("no answer within {ms} ms")),
                Ok(Err(e)) => Err(format!("cannot open {path}: {e}")),
                Ok(Ok(Err(e))) => Err(format!("cannot open {path}: {e}")),
                Ok(Ok(Ok(port))) => Ok(Box::new(port)),
            }
        }
        Kind::Rfc2217 => Err("RFC 2217 is not supported by the Rust engine yet".into()),
    }
}

/// The serial library's settings. It knows no mark/space parity and no 1.5 stop bits
/// (both valid in the config, as in Python): such a port cannot be opened from this engine.
fn serial_builder(path: &str, s: &SerialSettings) -> Result<tokio_serial::SerialPortBuilder, String> {
    use tokio_serial::{DataBits, FlowControl, StopBits};
    let data_bits = match s.bytesize {
        5 => DataBits::Five,
        6 => DataBits::Six,
        7 => DataBits::Seven,
        _ => DataBits::Eight,
    };
    let parity = match s.parity {
        Parity::None => tokio_serial::Parity::None,
        Parity::Even => tokio_serial::Parity::Even,
        Parity::Odd => tokio_serial::Parity::Odd,
        Parity::Mark | Parity::Space => {
            return Err(format!("{path}: parity {} is not supported by the serial library", s.parity.letter()))
        }
    };
    let stop_bits = if s.stopbits == 2.0 {
        StopBits::Two
    } else if s.stopbits == 1.0 {
        StopBits::One
    } else {
        return Err(format!("{path}: {} stop bits are not supported by the serial library", s.stopbits));
    };
    Ok(tokio_serial::new(path, s.baudrate)
        .data_bits(data_bits)
        .parity(parity)
        .stop_bits(stop_bits)
        .flow_control(FlowControl::None))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unsupported_line_settings_are_named() {
        let s = |parity, stopbits| SerialSettings { parity, stopbits, ..SerialSettings::default() };
        assert!(serial_builder("/dev/ttyS0", &s(Parity::Even, 2.0)).is_ok());
        assert_eq!(
            serial_builder("/dev/ttyS0", &s(Parity::Mark, 1.0)).unwrap_err(),
            "/dev/ttyS0: parity M is not supported by the serial library"
        );
        assert_eq!(
            serial_builder("/dev/ttyS0", &s(Parity::None, 1.5)).unwrap_err(),
            "/dev/ttyS0: 1.5 stop bits are not supported by the serial library"
        );
    }

    #[tokio::test]
    async fn tcp_open_errors_say_why() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let ep = Endpoint { kind: Kind::Tcp, host: "127.0.0.1".into(), port, settings: SerialSettings::default() };
        let err = open_stream(&ep, Duration::from_millis(500)).await.unwrap_err();
        assert!(err.starts_with("refused or unreachable"), "{err}");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_missing_serial_device_cannot_be_opened() {
        let ep = Endpoint {
            kind: Kind::Serial,
            host: "/dev/opencap-no-such-tty".into(),
            port: 0,
            settings: SerialSettings::default(),
        };
        let err = open_stream(&ep, Duration::from_millis(500)).await.unwrap_err();
        assert!(err.starts_with("cannot open /dev/opencap-no-such-tty"), "{err}");
    }
}
