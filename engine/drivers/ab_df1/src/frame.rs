//! DF1 full-duplex framing (DF1 Protocol and Command Set Reference Manual 1770-6.5.16, ch. "Data-link
//! layer: full-duplex"): DLE stuffing, BCC / CRC-16, the incremental receiver. Pure.

pub const DLE: u8 = 0x10;
pub const STX: u8 = 0x02;
pub const ETX: u8 = 0x03;
pub const ENQ: u8 = 0x05;
pub const ACK: u8 = 0x06;
pub const NAK: u8 = 0x15;
pub const DLE_ACK: [u8; 2] = [DLE, ACK];
pub const DLE_NAK: [u8; 2] = [DLE, NAK];
pub const DLE_ENQ: [u8; 2] = [DLE, ENQ];
/// Longest application message accepted (a typed read reply is <= 261).
pub const MAX_APP_BYTES: usize = 512;
/// DST SRC CMD STS TNS TNS
pub const MIN_APP_BYTES: usize = 6;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Check {
    Crc,
    Bcc,
}

impl Check {
    pub fn parse(s: &str) -> Result<Self, String> {
        match s {
            "crc" => Ok(Check::Crc),
            "bcc" => Ok(Check::Bcc),
            other => Err(format!("unknown DF1 error check {} (crc or bcc)", opencap_core::pyfmt::py_repr_str(other))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Check::Crc => "crc",
            Check::Bcc => "bcc",
        }
    }
}

/// CRC-16/ARC (reflected polynomial 0xA001, initial value 0).
pub fn crc16(data: &[u8], mut crc: u16) -> u16 {
    for &b in data {
        crc ^= u16::from(b);
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xA001 } else { crc >> 1 };
        }
    }
    crc
}

/// Block check character: two's complement of the 8-bit sum.
pub fn bcc(data: &[u8]) -> u8 {
    data.iter().fold(0u8, |a, b| a.wrapping_add(*b)).wrapping_neg()
}

/// BCC (1 byte) or CRC-16 over the application bytes and the ETX byte, low byte first.
pub fn check_bytes(app: &[u8], check: Check) -> Vec<u8> {
    match check {
        Check::Crc => {
            let mut v = app.to_vec();
            v.push(ETX);
            crc16(&v, 0).to_le_bytes().to_vec()
        }
        Check::Bcc => vec![bcc(app)],
    }
}

pub fn stuff(app: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(app.len() + 4);
    for &b in app {
        out.push(b);
        if b == DLE {
            out.push(DLE);
        }
    }
    out
}

/// `DLE STX <stuffed app> DLE ETX <check>`.
pub fn encode_frame(app: &[u8], check: Check) -> Vec<u8> {
    let mut out = vec![DLE, STX];
    out.extend(stuff(app));
    out.extend([DLE, ETX]);
    out.extend(check_bytes(app, check));
    out
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventKind {
    Ack,
    Nak,
    Enq,
    /// a good frame; `data` = the unstuffed application bytes
    Frame,
    /// a frame to NAK; `reason` says why
    Bad,
}

impl EventKind {
    pub fn as_str(self) -> &'static str {
        match self {
            EventKind::Ack => "ack",
            EventKind::Nak => "nak",
            EventKind::Enq => "enq",
            EventKind::Frame => "frame",
            EventKind::Bad => "bad",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkEvent {
    pub kind: EventKind,
    pub data: Vec<u8>,
    pub reason: String,
}

impl LinkEvent {
    fn symbol(kind: EventKind) -> Self {
        Self { kind, data: Vec::new(), reason: String::new() }
    }

    fn bad(reason: impl Into<String>) -> Self {
        Self { kind: EventKind::Bad, data: Vec::new(), reason: reason.into() }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Idle,
    IdleDle,
    Data,
    DataDle,
    Check,
}

/// Incremental DF1 full-duplex receiver: feed it bytes as they come, get [`LinkEvent`]s.
///
/// Bytes outside a frame other than a DLE response symbol are noise and are skipped. Inside a
/// frame, DLE DLE is one data DLE; DLE ACK / NAK / ENQ are response symbols embedded in the
/// frame (reported, the frame goes on); DLE STX restarts the frame (the partial one is
/// reported bad); any other DLE pair ends the frame as bad.
#[derive(Debug)]
pub struct FrameDecoder {
    check: Check,
    need: usize,
    state: State,
    app: Vec<u8>,
    chk: Vec<u8>,
    /// bytes skipped outside frames
    pub noise: usize,
}

impl FrameDecoder {
    pub fn new(check: Check) -> Self {
        Self {
            check,
            need: if check == Check::Crc { 2 } else { 1 },
            state: State::Idle,
            app: Vec::new(),
            chk: Vec::new(),
            noise: 0,
        }
    }

    pub fn in_frame(&self) -> bool {
        !matches!(self.state, State::Idle | State::IdleDle)
    }

    fn symbol(b: u8) -> Option<EventKind> {
        match b {
            ACK => Some(EventKind::Ack),
            NAK => Some(EventKind::Nak),
            ENQ => Some(EventKind::Enq),
            _ => None,
        }
    }

    pub fn feed(&mut self, data: &[u8]) -> Vec<LinkEvent> {
        let mut out = Vec::new();
        for &b in data {
            match self.state {
                State::Idle => {
                    if b == DLE {
                        self.state = State::IdleDle;
                    } else {
                        self.noise += 1;
                    }
                }
                State::IdleDle => {
                    self.state = State::Idle;
                    if b == STX {
                        self.start();
                    } else if let Some(k) = Self::symbol(b) {
                        out.push(LinkEvent::symbol(k));
                    } else if b == DLE {
                        self.state = State::IdleDle;
                        self.noise += 1;
                    } else {
                        self.noise += 2;
                    }
                }
                State::Data => {
                    if b == DLE {
                        self.state = State::DataDle;
                    } else if self.app.len() >= MAX_APP_BYTES {
                        out.push(LinkEvent::bad(format!("frame longer than {MAX_APP_BYTES} bytes")));
                        self.state = State::Idle;
                    } else {
                        self.app.push(b);
                    }
                }
                State::DataDle => {
                    self.state = State::Data;
                    if b == DLE {
                        self.app.push(DLE);
                    } else if b == ETX {
                        self.chk.clear();
                        self.state = State::Check;
                    } else if let Some(k) = Self::symbol(b) {
                        out.push(LinkEvent::symbol(k));
                    } else if b == STX {
                        out.push(LinkEvent::bad("DLE STX inside a frame (frame restarted)"));
                        self.start();
                    } else {
                        out.push(LinkEvent::bad(format!("invalid DLE sequence 10 {b:02x} inside a frame")));
                        self.state = State::Idle;
                    }
                }
                State::Check => {
                    self.chk.push(b);
                    if self.chk.len() == self.need {
                        out.push(self.finish());
                        self.state = State::Idle;
                    }
                }
            }
        }
        out
    }

    fn start(&mut self) {
        self.app.clear();
        self.state = State::Data;
    }

    fn finish(&mut self) -> LinkEvent {
        let app = std::mem::take(&mut self.app);
        let want = check_bytes(&app, self.check);
        if self.chk != want {
            let name = self.check.as_str().to_uppercase();
            return LinkEvent {
                kind: EventKind::Bad,
                reason: format!("{name} mismatch: got {}, computed {}", hex(&self.chk), hex(&want)),
                data: app,
            };
        }
        if app.len() < MIN_APP_BYTES {
            return LinkEvent {
                kind: EventKind::Bad,
                reason: format!("frame of {} bytes is shorter than a DF1 message header", app.len()),
                data: app,
            };
        }
        LinkEvent { kind: EventKind::Frame, data: app, reason: String::new() }
    }
}

/// Lowercase hex without separators (`8b3d`).
pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// A DF1 application message: `DST SRC CMD STS TNS(2, little-endian)` and the rest.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Df1Message {
    pub dst: u8,
    pub src: u8,
    pub cmd: u8,
    pub sts: u8,
    pub tns: u16,
    /// after TNS: FNC + function data (commands), data (replies)
    pub data: Vec<u8>,
}

impl Df1Message {
    pub fn ext_sts(&self) -> Option<u8> {
        if self.sts == opencap_driver_ab_pccc::status::EXT_STS_FOLLOWS {
            self.data.first().copied()
        } else {
            None
        }
    }

    pub fn reply_data(&self) -> &[u8] {
        if self.sts == opencap_driver_ab_pccc::status::EXT_STS_FOLLOWS && !self.data.is_empty() {
            &self.data[1..]
        } else {
            &self.data
        }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut v = vec![self.dst, self.src, self.cmd, self.sts];
        v.extend(self.tns.to_le_bytes());
        v.extend(&self.data);
        v
    }

    pub fn parse(app: &[u8]) -> Result<Self, String> {
        if app.len() < MIN_APP_BYTES {
            return Err(format!("DF1 message of {} bytes is shorter than its 6-byte header", app.len()));
        }
        Ok(Self {
            dst: app[0],
            src: app[1],
            cmd: app[2],
            sts: app[3],
            tns: u16::from_le_bytes([app[4], app[5]]),
            data: app[6..].to_vec(),
        })
    }
}
