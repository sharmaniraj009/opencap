//! One ISO-on-TCP connection to a CPU: COTP connect with rack/slot TSAPs, setup communication
//! (PDU negotiation), then one S7 request in flight. Every request is checked by
//! `proto::check_read_only` before it reaches the socket.
//!
//! A timeout drops the connection (the caller does): S7 replies are matched by a PDU reference,
//! but a late reply to an abandoned request must never be taken for another one.

use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;

use crate::proto::{self, ItemReply, ParsedPdu, ReadItem, ReplyError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetError {
    /// No complete reply within the timeout.
    Timeout,
    /// Refused, reset, closed by the peer.
    Lost(String),
    /// A reply that is malformed or carries an error.
    Reply(ReplyError),
    /// COTP/TPKT framing the peer got wrong.
    Frame(String),
    /// The read-only guard refused the PDU (not reachable: the driver only builds reads).
    ReadOnly(String),
}

impl std::fmt::Display for NetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NetError::Timeout => f.write_str("no reply within the timeout"),
            NetError::Reply(r) => f.write_str(r.message()),
            NetError::Lost(s) | NetError::Frame(s) | NetError::ReadOnly(s) => f.write_str(s),
        }
    }
}

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

#[derive(Debug)]
pub struct Conn {
    stream: TcpStream,
    timeout: Duration,
    seq: u16,
    dst_ref: u16,
    /// The negotiated PDU length.
    pub pdu: usize,
}

async fn recv_frame(stream: &mut TcpStream) -> Result<Vec<u8>, NetError> {
    let lost = |e: std::io::Error| {
        if e.kind() == std::io::ErrorKind::UnexpectedEof {
            NetError::Lost("ConnectionResetError: connection closed by the PLC".into())
        } else {
            NetError::Lost(describe_io(&e))
        }
    };
    let mut head = [0u8; 4];
    stream.read_exact(&mut head).await.map_err(lost)?;
    if head[0] != proto::TPKT_VERSION {
        return Err(NetError::Frame(format!("Invalid TPKT version: {}", head[0])));
    }
    let length = usize::from(u16::from_be_bytes([head[2], head[3]]));
    if length < 7 {
        return Err(NetError::Frame("Invalid TPKT length".into()));
    }
    let mut payload = vec![0u8; length - 4];
    stream.read_exact(&mut payload).await.map_err(lost)?;
    Ok(payload)
}

impl Conn {
    /// TCP connect, COTP connection request and setup communication asking for `pdu_request`
    /// bytes; each step bounded by `timeout_d`. The error is the reason text.
    pub async fn open(
        host: &str,
        port: u16,
        remote_tsap: u16,
        pdu_request: u16,
        timeout_d: Duration,
    ) -> Result<Self, String> {
        let ms = timeout_d.as_millis();
        let mut stream = match timeout(timeout_d, TcpStream::connect((host, port))).await {
            Err(_) => return Err(format!("no answer within {ms} ms")),
            Ok(Err(e)) => return Err(describe_io(&e)),
            Ok(Ok(s)) => s,
        };
        let _ = stream.set_nodelay(true);
        let cr = proto::connection_request(proto::LOCAL_TSAP, remote_tsap);
        let confirm = async {
            stream.write_all(&cr).await.map_err(|e| NetError::Lost(describe_io(&e)))?;
            recv_frame(&mut stream).await
        };
        let payload = match timeout(timeout_d, confirm).await {
            Err(_) => return Err(format!("no connection confirm within {ms} ms (check rack, slot and link type)")),
            Ok(Err(e)) => return Err(e.to_string()),
            Ok(Ok(p)) => p,
        };
        let dst_ref = proto::parse_connection_confirm(&payload)?;
        let mut conn = Self { stream, timeout: timeout_d, seq: 0, dst_ref, pdu: usize::from(pdu_request) };
        let seq = conn.next_seq();
        let reply = conn.request(&proto::setup_communication(seq, pdu_request), seq).await.map_err(|e| match e {
            NetError::Timeout => format!("no setup communication reply within {ms} ms"),
            other => other.to_string(),
        })?;
        conn.pdu = proto::parse_setup_reply(&reply).map_err(|e| e.message().to_string())?;
        Ok(conn)
    }

    /// The next PDU reference (1.. wrapping at 16 bits, like python-snap7).
    pub fn next_seq(&mut self) -> u16 {
        self.seq = self.seq.wrapping_add(1);
        self.seq
    }

    /// Send one request PDU (reference `seq`) and return the matching reply. A reply with an
    /// older reference is skipped (up to 3); a newer one is a lost packet.
    pub async fn request(&mut self, pdu: &[u8], seq: u16) -> Result<ParsedPdu, NetError> {
        if let Some(problem) = proto::check_read_only(pdu) {
            return Err(NetError::ReadOnly(problem));
        }
        let limit = self.timeout;
        match timeout(limit, self.request_inner(pdu, seq)).await {
            Err(_) => Err(NetError::Timeout),
            Ok(r) => r,
        }
    }

    async fn request_inner(&mut self, pdu: &[u8], seq: u16) -> Result<ParsedPdu, NetError> {
        self.stream.write_all(&proto::data_frame(pdu)).await.map_err(|e| NetError::Lost(describe_io(&e)))?;
        for _ in 0..4 {
            let payload = recv_frame(&mut self.stream).await?;
            let s7 = proto::parse_data_payload(&payload).map_err(NetError::Frame)?;
            let parsed = proto::parse_pdu(s7).map_err(NetError::Reply)?;
            if parsed.sequence < seq {
                continue; // stale
            }
            if parsed.sequence > seq {
                return Err(NetError::Reply(ReplyError::Malformed(format!(
                    "Packet lost: expected sequence {seq}, got {}",
                    parsed.sequence
                ))));
            }
            return Ok(parsed);
        }
        Err(NetError::Reply(ReplyError::Malformed("Max stale packet retries (3) exceeded".into())))
    }

    /// One read var request with `items`; one reply item per request item.
    pub async fn read_var(&mut self, items: &[ReadItem]) -> Result<Vec<ItemReply>, NetError> {
        let seq = self.next_seq();
        let reply = self.request(&proto::read_var(seq, items), seq).await?;
        proto::parse_read_reply(&reply, items).map_err(NetError::Reply)
    }

    /// Read SZL `id`/`index`: the record data after the SZL id and index, follow-up fragments
    /// included.
    pub async fn read_szl(&mut self, id: u16, index: u16) -> Result<Vec<u8>, NetError> {
        let seq = self.next_seq();
        let reply = self.request(&proto::read_szl(seq, id, index), seq).await?;
        let mut frag = proto::parse_szl_fragment(&reply, true).map_err(NetError::Reply)?;
        let mut data = std::mem::take(&mut frag.data);
        for _ in 0..100 {
            if !frag.more {
                break;
            }
            let seq = self.next_seq();
            let reply = self.request(&proto::read_szl_followup(seq, frag.sequence_number), seq).await?;
            frag = proto::parse_szl_fragment(&reply, false).map_err(NetError::Reply)?;
            data.append(&mut frag.data);
        }
        Ok(data)
    }

    /// Best-effort COTP disconnect, then drop the socket.
    pub fn close(self) {
        let _ = self.stream.try_write(&proto::disconnect_request(self.dst_ref));
    }
}
