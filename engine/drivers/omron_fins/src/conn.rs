//! FINS/UDP and FINS/TCP transports (docs/protocols/omron-fins.md §1). Replies are matched by
//! SID and command code; anything else (a late reply to a request that timed out, another
//! client's traffic) is dropped while waiting. Every frame passes `wire::check_read_only`.

use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpStream, UdpSocket};
use tokio::time::Instant;

use crate::wire::{self, Command, Reply, Route};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConnError {
    Timeout,
    Lost(String),
    Bad(String),
    ReadOnly(String),
}

enum Link {
    Udp(UdpSocket),
    Tcp(TcpStream),
}

pub struct Conn {
    link: Link,
    pub route: Route,
    sid: u8,
    /// frames dropped because their SID or command did not match
    pub dropped: u64,
}

fn lost(e: std::io::Error) -> ConnError {
    ConnError::Lost(e.to_string())
}

async fn read_tcp_message(stream: &mut TcpStream) -> Result<(u32, u32, Vec<u8>), ConnError> {
    let mut head = [0u8; 16];
    stream.read_exact(&mut head).await.map_err(lost)?;
    if &head[..4] != wire::TCP_MAGIC {
        return Err(ConnError::Bad("FINS/TCP message without the FINS header".into()));
    }
    let len = u32::from_be_bytes([head[4], head[5], head[6], head[7]]);
    let command = u32::from_be_bytes([head[8], head[9], head[10], head[11]]);
    let error = u32::from_be_bytes([head[12], head[13], head[14], head[15]]);
    if !(8..=8 + 2012).contains(&len) {
        return Err(ConnError::Bad(format!("FINS/TCP length {len}")));
    }
    let mut payload = vec![0u8; (len - 8) as usize];
    stream.read_exact(&mut payload).await.map_err(lost)?;
    Ok((command, error, payload))
}

impl Conn {
    /// UDP: bind (`local_port`, 0 = ephemeral) and connect the socket to the unit; SA1 defaults to
    /// the last octet of the local address. Nothing is sent: the first read shows whether the CPU
    /// answers.
    pub async fn udp(
        host: &str,
        port: u16,
        local_port: u16,
        mut route: Route,
        src_node: Option<u8>,
    ) -> Result<Conn, String> {
        let target = tokio::net::lookup_host((host, port))
            .await
            .map_err(|e| format!("cannot resolve {host}: {e}"))?
            .next()
            .ok_or_else(|| format!("cannot resolve {host}"))?;
        let bind = if target.is_ipv4() { format!("0.0.0.0:{local_port}") } else { format!("[::]:{local_port}") };
        let socket = UdpSocket::bind(&bind).await.map_err(|e| format!("cannot bind UDP {bind}: {e}"))?;
        socket.connect(target).await.map_err(|e| format!("cannot reach {target}: {e}"))?;
        route.sa1 = match src_node {
            Some(n) => n,
            None => match socket.local_addr().map(|a| a.ip()) {
                Ok(std::net::IpAddr::V4(v4)) => match v4.octets()[3] {
                    0 | 255 => 1,
                    n => n,
                },
                _ => 1,
            },
        };
        Ok(Conn { link: Link::Udp(socket), route, sid: 0, dropped: 0 })
    }

    /// TCP: connect and run the node address handshake. DA1 becomes the node the unit reports,
    /// SA1 the client node it accepted or allocated.
    pub async fn tcp(
        host: &str,
        port: u16,
        mut route: Route,
        src_node: Option<u8>,
        timeout: Duration,
    ) -> Result<Conn, String> {
        let ms = timeout.as_millis();
        let mut stream = match tokio::time::timeout(timeout, TcpStream::connect((host, port))).await {
            Err(_) => return Err(format!("no answer within {ms} ms")),
            Ok(Err(e)) => return Err(format!("refused or unreachable ({e})")),
            Ok(Ok(s)) => s,
        };
        let _ = stream.set_nodelay(true);
        let request = u32::from(src_node.unwrap_or(0)).to_be_bytes();
        let handshake = tokio::time::timeout(timeout, async {
            stream.write_all(&wire::tcp_message(wire::TCP_NODE_REQ, &request)).await.map_err(lost)?;
            read_tcp_message(&mut stream).await
        })
        .await;
        let (command, error, payload) = match handshake {
            Err(_) => return Err(format!("no FINS/TCP node address reply within {ms} ms")),
            Ok(Err(ConnError::Lost(e))) => {
                return Err(format!("the unit closed the connection during the handshake ({e})"))
            }
            Ok(Err(e)) => return Err(format!("FINS/TCP handshake failed: {e:?}")),
            Ok(Ok(m)) => m,
        };
        if error != 0 {
            return Err(format!("FINS/TCP handshake refused: error 0x{error:02X} ({})", wire::tcp_error_text(error)));
        }
        if command != wire::TCP_NODE_RESP || payload.len() < 8 {
            return Err(format!("FINS/TCP handshake: unexpected command {command}"));
        }
        let client = u32::from_be_bytes([payload[0], payload[1], payload[2], payload[3]]);
        let server = u32::from_be_bytes([payload[4], payload[5], payload[6], payload[7]]);
        route.sa1 = client as u8;
        route.da1 = server as u8;
        Ok(Conn { link: Link::Tcp(stream), route, sid: 0, dropped: 0 })
    }

    pub fn is_tcp(&self) -> bool {
        matches!(self.link, Link::Tcp(_))
    }

    /// Send one command and wait for its reply (matched by SID and command) within `timeout`.
    pub async fn request(&mut self, cmd: &Command, timeout: Duration) -> Result<(u16, u8, Vec<u8>), ConnError> {
        self.sid = self.sid.wrapping_add(1);
        let sid = self.sid;
        let frame = wire::frame(&self.route, sid, cmd);
        if let Some(why) = wire::check_read_only(&frame) {
            return Err(ConnError::ReadOnly(why));
        }
        let deadline = Instant::now() + timeout;
        match &mut self.link {
            Link::Udp(s) => {
                s.send(&frame).await.map_err(lost)?;
            }
            Link::Tcp(s) => {
                let msg = wire::tcp_message(wire::TCP_FRAME, &frame);
                tokio::time::timeout_at(deadline, s.write_all(&msg))
                    .await
                    .map_err(|_| ConnError::Timeout)?
                    .map_err(lost)?;
            }
        }
        loop {
            let reply = match &mut self.link {
                Link::Udp(s) => {
                    let mut buf = vec![0u8; 2048];
                    let n = tokio::time::timeout_at(deadline, s.recv(&mut buf))
                        .await
                        .map_err(|_| ConnError::Timeout)?
                        .map_err(lost)?;
                    buf.truncate(n);
                    buf
                }
                Link::Tcp(s) => {
                    let (command, error, payload) = tokio::time::timeout_at(deadline, read_tcp_message(s))
                        .await
                        .map_err(|_| ConnError::Timeout)??;
                    if command == wire::TCP_ERROR_NOTICE || error != 0 {
                        return Err(ConnError::Lost(format!(
                            "FINS/TCP error notification 0x{error:02X} ({})",
                            wire::tcp_error_text(error)
                        )));
                    }
                    if command != wire::TCP_FRAME {
                        self.dropped += 1;
                        continue;
                    }
                    payload
                }
            };
            match wire::parse_reply(&reply, sid, cmd.code()) {
                Ok(Reply::Ok { end, cpu_error, data }) => return Ok((end, cpu_error, data)),
                Ok(Reply::Other) => self.dropped += 1,
                Err(e) => return Err(ConnError::Bad(e)),
            }
        }
    }
}
