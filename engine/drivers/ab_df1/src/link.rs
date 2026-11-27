//! One DF1 full-duplex link (one serial line), shared by every station on it. Received frames
//! are always answered (ACK/NAK) at once, whether or not they are the awaited reply.

use std::collections::{BTreeMap, VecDeque};
use std::time::Duration;

use opencap_driver_ab_pccc::status::READ_COMMANDS;
use opencap_serial::ByteStream;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::time::Instant;
use tracing::debug;

use crate::frame::{encode_frame, Check, Df1Message, EventKind, FrameDecoder, LinkEvent, DLE_ACK, DLE_ENQ, DLE_NAK};

pub const REPLY_BIT: u8 = 0x40;

#[derive(Debug, Default, Clone)]
pub struct LinkStats {
    pub frames_sent: u64,
    pub retransmissions: u64,
    pub enqs_sent: u64,
    pub acks_received: u64,
    pub naks_received: u64,
    pub acks_sent: u64,
    pub naks_sent: u64,
    pub enqs_answered: u64,
    pub duplicates: u64,
    /// good frames that were not the reply we waited for
    pub unmatched: u64,
    /// replies from another station number than the one addressed
    pub source_mismatch: u64,
    pub bad_frames: u64,
    /// ACK/NAK left over from an earlier transaction, not counted
    pub stale_dropped: u64,
    pub commands: BTreeMap<(u8, u8), u64>,
}

impl LinkStats {
    pub fn as_json(&self) -> serde_json::Value {
        let mut m = serde_json::json!({
            "frames_sent": self.frames_sent, "retransmissions": self.retransmissions,
            "enqs_sent": self.enqs_sent, "acks_received": self.acks_received,
            "naks_received": self.naks_received, "acks_sent": self.acks_sent, "naks_sent": self.naks_sent,
            "enqs_answered": self.enqs_answered, "duplicates": self.duplicates, "unmatched": self.unmatched,
            "source_mismatch": self.source_mismatch, "bad_frames": self.bad_frames,
            "stale_dropped": self.stale_dropped,
        });
        let cmds: serde_json::Map<String, serde_json::Value> =
            self.commands.iter().map(|((c, f), n)| (format!("{c:#04x}/{f:#04x}"), (*n).into())).collect();
        m["commands"] = cmds.into();
        m
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum LinkError {
    /// no ACK, NAK retries used up, no reply (a comm error)
    Failure(String),
    /// the line is gone (connection lost)
    Closed(String),
    /// not a read command: never sent (ADR-0008)
    Refused(String),
}

#[derive(Debug)]
pub struct Df1Link {
    stream: Box<dyn ByteStream>,
    pub check: Check,
    decoder: FrameDecoder,
    duplicate_detect: bool,
    pub stats: LinkStats,
    /// repeated on DLE ENQ; NAK until a frame was received
    last_response: [u8; 2],
    last_header: Option<Vec<u8>>,
    backlog: VecDeque<LinkEvent>,
    tns: u16,
    warned_source: bool,
}

impl Df1Link {
    pub fn new(stream: Box<dyn ByteStream>, check: Check, duplicate_detect: bool) -> Self {
        Self {
            stream,
            check,
            decoder: FrameDecoder::new(check),
            duplicate_detect,
            stats: LinkStats::default(),
            last_response: DLE_NAK,
            last_header: None,
            backlog: VecDeque::new(),
            tns: rand::random(),
            warned_source: false,
        }
    }

    pub fn next_tns(&mut self) -> u16 {
        self.tns = self.tns.wrapping_add(1);
        self.tns
    }

    async fn send(&mut self, data: &[u8]) -> Result<(), LinkError> {
        let r = async {
            self.stream.write_all(data).await?;
            self.stream.flush().await
        }
        .await;
        r.map_err(|e| LinkError::Closed(format!("write failed ({e})")))
    }

    async fn respond(&mut self, symbol: [u8; 2]) -> Result<(), LinkError> {
        self.last_response = symbol;
        self.send(&symbol).await?;
        if symbol == DLE_ACK {
            self.stats.acks_sent += 1;
        } else {
            self.stats.naks_sent += 1;
        }
        Ok(())
    }

    /// Before a new frame: a DLE ACK or NAK the line delivered after the previous transaction
    /// ended belongs to an older frame and must not count for this one. Late frames and ENQs
    /// stay queued (the loop ACKs a frame so the PLC does not retransmit, and answers an ENQ).
    async fn drop_stale(&mut self) -> Result<(), LinkError> {
        let mut buf = [0u8; 512];
        loop {
            match tokio::time::timeout(Duration::ZERO, self.stream.read(&mut buf)).await {
                Err(_) => break,
                Ok(Ok(0)) => return Err(LinkError::Closed("connection closed by the remote end".into())),
                Ok(Ok(n)) => {
                    let events = self.decoder.feed(&buf[..n]);
                    self.backlog.extend(events);
                }
                Ok(Err(e)) => return Err(LinkError::Closed(format!("read failed ({e})"))),
            }
        }
        let before = self.backlog.len();
        self.backlog.retain(|e| !matches!(e.kind, EventKind::Ack | EventKind::Nak));
        let dropped = before - self.backlog.len();
        if dropped > 0 {
            self.stats.stale_dropped += dropped as u64;
            debug!(count = dropped, "df1 stale ACK/NAK dropped");
        }
        Ok(())
    }

    /// Send `request`, see it ACKed, and return the matching reply.
    pub async fn transact(
        &mut self,
        request: &Df1Message,
        timeout: Duration,
        ack_timeout: Duration,
        nak_retries: u32,
        enq_retries: u32,
    ) -> Result<Df1Message, LinkError> {
        let fnc = request.data.first().copied().unwrap_or(0);
        if !READ_COMMANDS.contains(&(request.cmd, fnc)) {
            return Err(LinkError::Refused("opencap is read-only: only PCCC read commands are sent".into()));
        }
        let deadline = Instant::now() + timeout;
        self.drop_stale().await?;
        let frame = encode_frame(&request.encode(), self.check);
        let expect = (request.cmd | REPLY_BIT, request.tns);
        *self.stats.commands.entry((request.cmd, fnc)).or_default() += 1;
        self.send(&frame).await?;
        self.stats.frames_sent += 1;
        let mut acked = false;
        let (mut naks, mut enqs) = (0u32, 0u32);
        let mut ack_due = Instant::now() + ack_timeout;
        let mut buf = [0u8; 512];
        loop {
            let Some(ev) = self.backlog.pop_front() else {
                let now = Instant::now();
                if now >= deadline {
                    let what = if acked { "reply" } else { "ACK" };
                    return Err(LinkError::Failure(format!(
                        "no {what} within {:.0} ms (NAKs {naks}, ENQs {enqs})",
                        timeout.as_secs_f64() * 1000.0
                    )));
                }
                let until = if acked { deadline } else { deadline.min(ack_due) };
                match tokio::time::timeout_at(until, self.stream.read(&mut buf)).await {
                    Err(_) => {
                        let now = Instant::now();
                        if !acked && now >= ack_due && now < deadline {
                            if enqs >= enq_retries {
                                return Err(LinkError::Failure(format!(
                                    "no ACK after {enqs} ENQ(s) ({:.0} ms each)",
                                    ack_timeout.as_secs_f64() * 1000.0
                                )));
                            }
                            enqs += 1;
                            self.stats.enqs_sent += 1;
                            self.send(&DLE_ENQ).await?;
                            ack_due = Instant::now() + ack_timeout;
                        }
                    }
                    Ok(Ok(0)) => return Err(LinkError::Closed("connection closed by the remote end".into())),
                    Ok(Ok(n)) => {
                        let events = self.decoder.feed(&buf[..n]);
                        self.backlog.extend(events);
                    }
                    Ok(Err(e)) => return Err(LinkError::Closed(format!("read failed ({e})"))),
                }
                continue;
            };
            match ev.kind {
                EventKind::Ack => {
                    self.stats.acks_received += 1;
                    acked = true;
                }
                EventKind::Nak => {
                    self.stats.naks_received += 1;
                    if acked {
                        continue; // a stray NAK after the ACK: nothing to resend
                    }
                    if naks >= nak_retries {
                        return Err(LinkError::Failure(format!(
                            "message NAKed {} time(s) (nak_retries={nak_retries})",
                            naks + 1
                        )));
                    }
                    naks += 1;
                    self.stats.retransmissions += 1;
                    self.send(&frame).await?;
                    ack_due = Instant::now() + ack_timeout;
                }
                EventKind::Enq => {
                    self.stats.enqs_answered += 1;
                    let r = self.last_response;
                    self.send(&r).await?;
                }
                EventKind::Bad => {
                    self.stats.bad_frames += 1;
                    debug!(reason = %ev.reason, "df1 bad frame");
                    self.respond(DLE_NAK).await?;
                }
                EventKind::Frame => {
                    self.respond(DLE_ACK).await?;
                    let header = ev.data[1..6].to_vec();
                    if self.duplicate_detect && self.last_header.as_deref() == Some(header.as_slice()) {
                        self.stats.duplicates += 1;
                        continue;
                    }
                    self.last_header = Some(header);
                    let Ok(msg) = Df1Message::parse(&ev.data) else { continue };
                    if (msg.cmd, msg.tns) == expect {
                        if msg.src != request.dst {
                            self.stats.source_mismatch += 1;
                            if !self.warned_source {
                                self.warned_source = true;
                                tracing::info!(
                                    addressed = request.dst,
                                    answered = msg.src,
                                    "df1 reply from another station"
                                );
                            }
                        }
                        return Ok(msg); // a reply implies the request was received
                    }
                    self.stats.unmatched += 1;
                    debug!(src = msg.src, dst = msg.dst, cmd = msg.cmd, tns = msg.tns, "df1 unmatched frame");
                }
            }
        }
    }
}
