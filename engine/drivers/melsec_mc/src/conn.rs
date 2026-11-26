//! One socket to a PLC: TCP (a stream) or UDP (one frame per datagram), one request in flight.
//!
//! A TCP reply is read as exactly one 3E frame: the 9-byte header, then the number of bytes it
//! announces. A reply split across TCP segments is therefore read whole, never decoded short
//! (stock pymcprotocol reads one `recv(4096)` and fills the missing words with zeros, D-053).
//! 3E replies carry no sequence number, so after a timeout the socket is dropped: a late reply
//! must never be taken as the answer to the next request.

use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{lookup_host, TcpStream, UdpSocket};
use tokio::time::timeout;

use crate::frame::{check_read_only, check_reply_frame, REPLY_HEADER_LEN, SUBHEADER_REPLY};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetError {
    /// No complete reply within the timeout.
    Timeout,
    /// Refused, reset, broken pipe or the PLC closed the connection.
    Lost(String),
    /// Something that is not a well-formed 3E reply.
    Frame(String),
    /// The read-only guard refused the frame (a bug in this driver: it only builds reads).
    ReadOnly(String),
}

impl std::fmt::Display for NetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NetError::Timeout => f.write_str("no reply within the timeout"),
            NetError::Lost(s) | NetError::Frame(s) | NetError::ReadOnly(s) => f.write_str(s),
        }
    }
}

#[derive(Debug)]
enum Transport {
    Tcp(TcpStream),
    Udp(UdpSocket),
}

#[derive(Debug)]
pub struct Conn {
    transport: Transport,
    timeout: Duration,
}

/// Text of an I/O error close to Python's `ExceptionClass: message`.
pub fn describe_io(e: &std::io::Error) -> String {
    use std::io::ErrorKind as K;
    let class = match e.kind() {
        K::ConnectionRefused => "ConnectionRefusedError",
        K::ConnectionReset => "ConnectionResetError",
        K::ConnectionAborted => "ConnectionAbortedError",
        K::BrokenPipe => "BrokenPipeError",
        K::TimedOut => "TimeoutError",
        _ => "OSError",
    };
    format!("{class}: {e}")
}

impl Conn {
    /// Open the socket: a TCP connect, or a connected UDP socket (no traffic). One attempt,
    /// bounded by `timeout`. The error is the reason text.
    pub async fn open(host: &str, port: u16, udp: bool, timeout_d: Duration) -> Result<Self, String> {
        let attempt = async {
            if udp {
                let addr = lookup_host((host, port))
                    .await
                    .map_err(|e| describe_io(&e))?
                    .next()
                    .ok_or_else(|| format!("no address for {host}"))?;
                let bind = if addr.is_ipv4() { "0.0.0.0:0" } else { "[::]:0" };
                let sock = UdpSocket::bind(bind).await.map_err(|e| describe_io(&e))?;
                sock.connect(addr).await.map_err(|e| describe_io(&e))?; // fixes the peer: ICMP errors come back
                Ok(Transport::Udp(sock))
            } else {
                let s = TcpStream::connect((host, port)).await.map_err(|e| describe_io(&e))?;
                let _ = s.set_nodelay(true);
                Ok(Transport::Tcp(s))
            }
        };
        match timeout(timeout_d, attempt).await {
            Err(_) => Err(format!("no answer within {} ms", timeout_d.as_millis())),
            Ok(Err(reason)) => Err(reason),
            Ok(Ok(transport)) => Ok(Self { transport, timeout: timeout_d }),
        }
    }

    /// Send one request frame and return the whole reply frame. Only read requests pass the
    /// guard.
    pub async fn exchange(&mut self, frame: &[u8]) -> Result<Vec<u8>, NetError> {
        if let Some(problem) = check_read_only(frame) {
            return Err(NetError::ReadOnly(problem));
        }
        let limit = self.timeout;
        match timeout(limit, self.exchange_inner(frame)).await {
            Err(_) => Err(NetError::Timeout),
            Ok(r) => r,
        }
    }

    async fn exchange_inner(&mut self, frame: &[u8]) -> Result<Vec<u8>, NetError> {
        let lost = |e: std::io::Error| NetError::Lost(describe_io(&e));
        match &mut self.transport {
            Transport::Udp(sock) => {
                sock.send(frame).await.map_err(lost)?;
                let mut buf = vec![0u8; 65535];
                let n = sock.recv(&mut buf).await.map_err(lost)?;
                buf.truncate(n);
                check_reply_frame(&buf).map_err(NetError::Frame)?;
                Ok(buf)
            }
            Transport::Tcp(s) => {
                s.write_all(frame).await.map_err(lost)?;
                let mut header = [0u8; REPLY_HEADER_LEN];
                read_exact(s, &mut header).await?;
                if header[..2] != SUBHEADER_REPLY {
                    return Err(NetError::Frame(format!(
                        "unexpected reply subheader {} (expected d000)",
                        crate::frame::hex2(&header[..2])
                    )));
                }
                let length = usize::from(u16::from_le_bytes([header[7], header[8]]));
                let mut out = header.to_vec();
                out.resize(REPLY_HEADER_LEN + length, 0);
                read_exact(s, &mut out[REPLY_HEADER_LEN..]).await?;
                check_reply_frame(&out).map_err(NetError::Frame)?;
                Ok(out)
            }
        }
    }
}

async fn read_exact(s: &mut TcpStream, buf: &mut [u8]) -> Result<(), NetError> {
    match s.read_exact(buf).await {
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
            Err(NetError::Lost("ConnectionResetError: connection closed by the PLC".into()))
        }
        Err(e) => Err(NetError::Lost(describe_io(&e))),
    }
}
