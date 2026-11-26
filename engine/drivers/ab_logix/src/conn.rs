//! One EtherNet/IP session with one CIP connection to a controller, over a TCP socket.
//!
//! Every request is one write and one reply read, bounded by the device timeout. Any failure
//! other than a CIP status leaves the socket in an unknown state, so callers drop the
//! connection ([`NetError`]); the worker reconnects. Nothing here sends a write service
//! (see `cip.rs`).

use std::time::Duration;

use opencap_core::value::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;

use crate::cip::{self, CipReply, Identity, Typed};
use crate::params::{Hop, STANDARD_FORWARD_OPEN_MAX};
use crate::plan::MULTISERVICE_READ_OVERHEAD;
use crate::tags::Resolved;

/// A failure of the transport or of the framing: the connection must be dropped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetError {
    /// No reply within the device timeout.
    Timeout,
    /// The peer closed or reset the connection, or the socket failed.
    Lost(String),
    /// The encapsulation layer refused the command (status code).
    Encap(u32),
    /// A reply that cannot be parsed or does not match the request.
    Protocol(String),
}

impl std::fmt::Display for NetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NetError::Timeout => write!(f, "no reply within the timeout"),
            NetError::Lost(e) => write!(f, "{e}"),
            NetError::Encap(s) => write!(f, "encapsulation status {s:#06x}"),
            NetError::Protocol(e) => write!(f, "{e}"),
        }
    }
}

fn io_err(e: std::io::Error) -> NetError {
    NetError::Lost(match e.kind() {
        std::io::ErrorKind::UnexpectedEof => "connection closed by the peer".to_string(),
        _ => format!("{e}"),
    })
}

/// A request for one name inside or outside a Multiple Service Packet got its own answer.
#[derive(Debug, Clone, PartialEq)]
pub struct NameResult {
    pub value: Option<Value>,
    pub error: Option<String>,
    /// A request for this name went on the wire.
    pub sent: bool,
    /// CIP general status of its reply.
    pub status: Option<u8>,
    pub ext_status: Option<u16>,
}

impl NameResult {
    pub fn ok(&self) -> bool {
        self.error.is_none() && self.value.is_some()
    }
}

/// A read that failed as a whole.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadFail {
    /// The reply is unusable as a whole (a Multiple Service Packet refused or malformed).
    Whole {
        reason: String,
        multi: bool,
    },
    Net(NetError),
}

impl From<NetError> for ReadFail {
    fn from(e: NetError) -> Self {
        ReadFail::Net(e)
    }
}

pub struct Conn {
    stream: TcpStream,
    session: u32,
    ot_id: u32,
    seq: u16,
    timeout: Duration,
    context: u64,
    /// Negotiated connection size in bytes (0 before Forward Open).
    pub connection_size: usize,
    conn_serial: u16,
    orig_serial: u32,
    path: Vec<u8>,
    open: bool,
}

impl Conn {
    /// TCP connect and RegisterSession.
    pub async fn open(host: &str, port: u16, per_request: Duration) -> Result<Self, NetError> {
        let stream = timeout(per_request, TcpStream::connect((host, port)))
            .await
            .map_err(|_| NetError::Timeout)?
            .map_err(io_err)?;
        let _ = stream.set_nodelay(true);
        let mut c = Conn {
            stream,
            session: 0,
            ot_id: 0,
            seq: 0,
            timeout: per_request,
            context: 0,
            connection_size: 0,
            conn_serial: rand::random(),
            orig_serial: rand::random(),
            path: Vec::new(),
            open: false,
        };
        let (h, _) = c.roundtrip(cip::CMD_REGISTER_SESSION, &cip::register_session_payload()).await?;
        c.session = h.session;
        Ok(c)
    }

    fn next_context(&mut self) -> [u8; 8] {
        self.context = self.context.wrapping_add(1);
        self.context.to_le_bytes()
    }

    /// Send one encapsulation command and read its reply.
    async fn roundtrip(&mut self, command: u16, payload: &[u8]) -> Result<(cip::EncapHeader, Vec<u8>), NetError> {
        let ctx = self.next_context();
        let frame = cip::encap(command, self.session, ctx, payload);
        let budget = self.timeout;
        let exchange = async {
            self.stream.write_all(&frame).await.map_err(io_err)?;
            let mut head = [0u8; cip::ENCAP_HEADER_LEN];
            self.stream.read_exact(&mut head).await.map_err(io_err)?;
            let h = cip::parse_encap_header(&head).map_err(NetError::Protocol)?;
            let mut body = vec![0u8; h.length as usize];
            self.stream.read_exact(&mut body).await.map_err(io_err)?;
            Ok::<_, NetError>((h, body))
        };
        let (h, body) = timeout(budget, exchange).await.map_err(|_| NetError::Timeout)??;
        if h.status != 0 {
            return Err(NetError::Encap(h.status));
        }
        if h.command != command {
            return Err(NetError::Protocol(format!("reply to command {:#06x} for {command:#06x}", h.command)));
        }
        Ok((h, body))
    }

    pub async fn list_identity(&mut self) -> Result<Identity, NetError> {
        let (_, body) = self.roundtrip(cip::CMD_LIST_IDENTITY, &[]).await?;
        cip::parse_list_identity(&body).map_err(NetError::Protocol)
    }

    /// Unconnected request (Forward Open/Close).
    async fn unconnected(&mut self, cip_request: &[u8]) -> Result<CipReply, NetError> {
        let (_, body) = self.roundtrip(cip::CMD_SEND_RR_DATA, &cip::send_rr_data_payload(cip_request)).await?;
        cip::parse_reply(&cip::parse_rr_reply(&body).map_err(NetError::Protocol)?).map_err(NetError::Protocol)
    }

    /// Forward Open. A Large Forward Open (connection up to 4000 bytes) first unless the
    /// configured size fits a standard one; a refusal falls back to a standard one of 500
    /// bytes (or the configured size). `Ok(Err(text))` is a refusal by the controller.
    pub async fn forward_open(
        &mut self,
        route: &[Hop],
        configured: Option<usize>,
    ) -> Result<Result<(), String>, NetError> {
        self.path = cip::connection_path(route);
        let try_large = configured.is_none_or(|c| c > STANDARD_FORWARD_OPEN_MAX);
        let mut attempts = Vec::new();
        if try_large {
            attempts.push((true, configured.unwrap_or(crate::params::CONNECTION_SIZE_LARGE)));
        }
        let standard =
            configured.filter(|c| *c <= STANDARD_FORWARD_OPEN_MAX).unwrap_or(crate::params::CONNECTION_SIZE_STANDARD);
        attempts.push((false, standard));
        let mut last = String::new();
        for (large, size) in attempts {
            let to_id: u32 = rand::random();
            let req = cip::forward_open_request(&cip::ForwardOpenParams {
                large,
                size,
                connection_serial: self.conn_serial,
                originator_serial: self.orig_serial,
                to_connection_id: to_id,
                path: self.path.clone(),
            });
            let reply = self.unconnected(&req).await?;
            if reply.status == cip::STATUS_OK && reply.data.len() >= 4 {
                self.ot_id = u32::from_le_bytes(reply.data[..4].try_into().unwrap());
                self.connection_size = size;
                self.open = true;
                return Ok(Ok(()));
            }
            last = format!(
                "{} Forward Open refused: {}{}",
                if large { "Large" } else { "Standard" },
                cip::status_description(reply.status),
                reply.first_ext().map(|e| format!(", extended status {e:#06x}")).unwrap_or_default()
            );
        }
        Ok(Err(last))
    }

    /// One connected request/reply.
    pub async fn send_connected(&mut self, request: &[u8]) -> Result<CipReply, NetError> {
        // last line of defence: no builder makes these, but nothing may ever carry one
        if cip::WRITE_SERVICES.contains(&request[0]) {
            return Err(NetError::Protocol(format!("opencap is read-only: CIP service {:#04x} refused", request[0])));
        }
        self.seq = self.seq.wrapping_add(1);
        let seq = self.seq;
        let (_, body) =
            self.roundtrip(cip::CMD_SEND_UNIT_DATA, &cip::send_unit_data_payload(self.ot_id, seq, request)).await?;
        let (got, reply) = cip::parse_unit_reply(&body).map_err(NetError::Protocol)?;
        if got != seq {
            return Err(NetError::Protocol(format!("reply sequence {got} for request {seq}")));
        }
        cip::parse_reply(&reply).map_err(NetError::Protocol)
    }

    /// Forward Close and UnRegisterSession, then drop the socket. Never fails.
    pub async fn close(mut self, graceful: bool) {
        if graceful {
            if self.open {
                let req = cip::forward_close_request(self.conn_serial, self.orig_serial, &self.path);
                let _ = self.unconnected(&req).await;
            }
            let ctx = self.next_context();
            let frame = cip::encap(cip::CMD_UNREGISTER_SESSION, self.session, ctx, &[]);
            let _ = timeout(self.timeout, self.stream.write_all(&frame)).await;
            let _ = timeout(self.timeout, self.stream.shutdown()).await;
        }
        // dropping the stream closes the socket
    }

    // --- reads -------------------------------------------------------------------------------

    /// Get Attributes All on the Program Name object (Logix): the controller's program name.
    pub async fn program_name(&mut self) -> Result<Option<String>, NetError> {
        let req = cip::get_attributes_all_request(cip::CLASS_PROGRAM_NAME, 1).map_err(NetError::Protocol)?;
        let r = self.send_connected(&req).await?;
        if r.status != cip::STATUS_OK || r.data.len() < 2 {
            return Ok(None);
        }
        let n = u16::from_le_bytes([r.data[0], r.data[1]]) as usize;
        Ok(r.data.get(2..2 + n).map(|b| b.iter().map(|&c| c as char).collect()))
    }

    /// Read every name; `batching` sends them as Multiple Service Packets packed greedily by
    /// reply size (pycomm3's rule), otherwise one request per name.
    pub async fn read_names(&mut self, names: &[Resolved], batching: bool) -> Result<Vec<NameResult>, ReadFail> {
        let mut out: Vec<Option<NameResult>> = vec![None; names.len()];
        if !batching || names.len() == 1 {
            for (i, r) in names.iter().enumerate() {
                out[i] = Some(self.read_single(r).await?);
            }
        } else {
            for group in pack_groups(names, self.connection_size) {
                if let [i] = group[..] {
                    out[i] = Some(self.read_single(&names[i]).await?);
                    continue;
                }
                let members: Vec<&Resolved> = group.iter().map(|&i| &names[i]).collect();
                let replies = self.read_multi(&members).await?;
                for (&i, result) in group.iter().zip(replies) {
                    out[i] = Some(match result {
                        Some(r) => r,
                        // did not fit in the packet: read it alone (fragmented if need be)
                        None => self.read_single(&names[i]).await?,
                    });
                }
            }
        }
        Ok(out.into_iter().map(|r| r.expect("every name answered")).collect())
    }

    /// One Multiple Service Packet; `None` for a member whose reply did not fit (status 0x06).
    async fn read_multi(&mut self, members: &[&Resolved]) -> Result<Vec<Option<NameResult>>, ReadFail> {
        let mut requests = Vec::with_capacity(members.len());
        for r in members {
            let path = cip::symbolic_path(&r.name).map_err(NetError::Protocol)?;
            requests.push(cip::read_tag_request(&path, 1).map_err(NetError::Protocol)?);
        }
        let packet = cip::multiple_service_request(&requests).map_err(NetError::Protocol)?;
        let reply = self.send_connected(&packet).await?;
        if reply.status != cip::STATUS_OK && reply.status != cip::STATUS_EMBEDDED_ERROR {
            return Err(ReadFail::Whole {
                reason: format!("Multiple Service Packet rejected: {}", cip::status_description(reply.status)),
                multi: true,
            });
        }
        let replies = cip::parse_multiple_reply(&reply.data).map_err(|e| ReadFail::Whole {
            reason: format!("unreadable Multiple Service Packet reply, {}: {e}", cip::status_description(reply.status)),
            multi: true,
        })?;
        if replies.len() != members.len() {
            return Err(ReadFail::Whole {
                reason: format!(
                    "Multiple Service Packet reply has {} replies for {} requests",
                    replies.len(),
                    members.len()
                ),
                multi: true,
            });
        }
        Ok(replies
            .iter()
            .zip(members)
            .map(|(r, res)| if r.status == cip::STATUS_PARTIAL { None } else { Some(name_result(r, res)) })
            .collect())
    }

    /// One name on its own: Read Tag, or Read Tag Fragmented when the value does not fit one
    /// reply.
    async fn read_single(&mut self, r: &Resolved) -> Result<NameResult, NetError> {
        let path = cip::symbolic_path(&r.name).map_err(NetError::Protocol)?;
        let message = 2 + 1 + path.len() + 1 + 2;
        let fragmented = r.data_size + message > self.connection_size;
        let first =
            if fragmented { cip::read_fragmented_request(&path, 1, 0) } else { cip::read_tag_request(&path, 1) }
                .map_err(NetError::Protocol)?;
        let mut reply = self.send_connected(&first).await?;
        if reply.status == cip::STATUS_PARTIAL {
            let mut typed: Option<Typed> = None;
            let mut bytes: Vec<u8> = Vec::new();
            let mut guard = 0;
            loop {
                match cip::parse_typed(&reply.data) {
                    Ok(Typed::Atomic { code, bytes: b }) => {
                        typed.get_or_insert(Typed::Atomic { code, bytes: vec![] });
                        bytes.extend_from_slice(&b);
                    }
                    Ok(Typed::Structure { handle, bytes: b }) => {
                        typed.get_or_insert(Typed::Structure { handle, bytes: vec![] });
                        bytes.extend_from_slice(&b);
                    }
                    Err(e) => return Err(NetError::Protocol(e)),
                }
                if reply.status != cip::STATUS_PARTIAL {
                    break;
                }
                guard += 1;
                if guard > 1000 || bytes.is_empty() {
                    return Err(NetError::Protocol("fragmented read does not end".into()));
                }
                let next = cip::read_fragmented_request(&path, 1, bytes.len() as u32).map_err(NetError::Protocol)?;
                reply = self.send_connected(&next).await?;
                if reply.status != cip::STATUS_OK && reply.status != cip::STATUS_PARTIAL {
                    return Ok(failed(&reply));
                }
            }
            let joined = match typed.expect("at least one fragment") {
                Typed::Atomic { code, .. } => Typed::Atomic { code, bytes },
                Typed::Structure { handle, .. } => Typed::Structure { handle, bytes },
            };
            return Ok(value_result(joined, r));
        }
        Ok(name_result(&reply, r))
    }
}

/// Group names greedily by reply size against the connection size (pycomm3 `_read_build_multi_requests`):
/// each name costs `data + request message + 2`, a group starts at the packet overhead, a name
/// that does not fit a packet on its own goes alone (and is read fragmented).
pub fn pack_groups(names: &[Resolved], connection_size: usize) -> Vec<Vec<usize>> {
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut current = MULTISERVICE_READ_OVERHEAD;
    let mut open: Vec<usize> = Vec::new();
    for (i, r) in names.iter().enumerate() {
        let path_bytes = crate::address::epath_size(&r.name) - 1;
        let size = r.data_size + (2 + 1 + path_bytes + 1 + 2) + 2;
        if size > connection_size {
            groups.push(vec![i]);
            continue;
        }
        if current + size > connection_size && !open.is_empty() {
            groups.push(std::mem::take(&mut open));
            current = MULTISERVICE_READ_OVERHEAD;
        }
        open.push(i);
        current += size;
    }
    if !open.is_empty() {
        groups.push(open);
    }
    groups
}

fn failed(r: &CipReply) -> NameResult {
    NameResult {
        value: None,
        error: Some(cip::status_description(r.status)),
        sent: true,
        status: Some(r.status),
        ext_status: r.first_ext(),
    }
}

fn value_result(typed: Typed, r: &Resolved) -> NameResult {
    let value = match typed {
        Typed::Atomic { code, bytes } => cip::decode_atomic(code, &bytes),
        Typed::Structure { bytes, .. } if r.is_string => cip::decode_logix_string(&bytes),
        Typed::Structure { handle, .. } => {
            Err(format!("structure {handle:#06x} values are not supported: address a member"))
        }
    };
    match value {
        Ok(v) => NameResult { value: Some(v), error: None, sent: true, status: Some(cip::STATUS_OK), ext_status: None },
        Err(e) => {
            NameResult { value: None, error: Some(e), sent: true, status: Some(cip::STATUS_OK), ext_status: None }
        }
    }
}

fn name_result(reply: &CipReply, r: &Resolved) -> NameResult {
    if reply.status != cip::STATUS_OK {
        return failed(reply);
    }
    match cip::parse_typed(&reply.data) {
        Ok(t) => value_result(t, r),
        Err(e) => {
            NameResult { value: None, error: Some(e), sent: true, status: Some(cip::STATUS_OK), ext_status: None }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tags::{Kind, Node};

    fn res(name: &str, size: usize) -> Resolved {
        Resolved {
            name: name.into(),
            node: Node { kind: Kind::Atomic(0xCA), dims: 0 },
            data_size: size,
            is_string: false,
        }
    }

    #[test]
    fn packing_is_greedy_by_reply_size() {
        // 500-byte connection: each name costs 4 + (2+1+path+1+2) + 2
        let names: Vec<Resolved> = (0..100).map(|i| res(&format!("TAG_{i:03}"), 4)).collect();
        let groups = pack_groups(&names, 500);
        assert!(groups.len() > 1);
        assert_eq!(groups.iter().map(Vec::len).sum::<usize>(), 100);
        let mut next = 0;
        for g in &groups {
            assert_eq!(g[0], next, "order kept");
            next += g.len();
        }
        // a value bigger than the connection is alone
        let big = vec![res("A", 4), res("BIG", 600), res("B", 4)];
        assert_eq!(pack_groups(&big, 500), vec![vec![1], vec![0, 2]]);
    }

    #[test]
    fn partial_member_replies_are_not_results() {
        let r = CipReply { service: 0x4C, status: cip::STATUS_PARTIAL, ext: vec![], data: vec![] };
        assert_eq!(r.status, 6);
        let bad = failed(&CipReply { service: 0x4C, status: 4, ext: vec![0], data: vec![] });
        assert_eq!((bad.status, bad.ext_status, bad.sent), (Some(4), Some(0), true));
        assert!(!bad.ok());
    }
}
