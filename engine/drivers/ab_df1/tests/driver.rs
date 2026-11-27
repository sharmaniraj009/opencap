//! The DF1 driver against an in-process fake SLC on a TCP socket (a serial-over-TCP gateway):
//! typed reads of every file type, link faults (NAK, lost ACK, bad reply check, duplicate and stale
//! replies, silence), PLC error statuses, the read-only guard and a short soak. No wall-clock
//! bounds tighter than a few hundred ms (Windows timers tick at ~15.6 ms).

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Driver, DriverError};
use opencap_core::value::{Quality, Value};
use opencap_driver_ab_df1::frame::{encode_frame, Check, Df1Message, EventKind, FrameDecoder, DLE_ACK, DLE_NAK};
use opencap_driver_ab_df1::AbDf1Driver;

#[derive(Default)]
struct Faults {
    nak_next: u32,
    lose_next: u32,
    drop_acks: u32,
    no_reply: bool,
    corrupt_replies: u32,
    duplicate_replies: bool,
    stale_reply: bool,
    mute: bool,
}

#[derive(Default)]
struct Plc {
    /// (PCCC type code, file) -> data table bytes
    files: HashMap<(u8, u32), Vec<u8>>,
    faults: Faults,
    commands: Vec<(u8, u8)>,
}

type Shared = Arc<Mutex<Plc>>;

fn word_bytes(v: &[i16]) -> Vec<u8> {
    v.iter().flat_map(|w| w.to_le_bytes()).collect()
}

fn field(d: &[u8], p: &mut usize) -> u32 {
    if d[*p] == 0xFF {
        let v = u32::from(u16::from_le_bytes([d[*p + 1], d[*p + 2]]));
        *p += 3;
        v
    } else {
        *p += 1;
        u32::from(d[*p - 1])
    }
}

fn elem_size(code: u8) -> usize {
    match code {
        0x8A | 0x91 => 4,
        0x86 | 0x87 => 6,
        0x8D => 84,
        _ => 2,
    }
}

fn reply_for(plc: &Plc, req: &Df1Message) -> Df1Message {
    let mut r = Df1Message { dst: req.src, src: req.dst, cmd: req.cmd | 0x40, sts: 0, tns: req.tns, data: vec![] };
    if req.cmd == 0x0F {
        let fnc = req.data[0];
        let size = usize::from(req.data[1]);
        let mut p = 2;
        let file = field(&req.data, &mut p);
        let code = req.data[p];
        p += 1;
        let element = field(&req.data, &mut p) as usize;
        let sub = if fnc == 0xA2 { field(&req.data, &mut p) as usize } else { 0 };
        let es = elem_size(code);
        match plc.files.get(&(code, file)) {
            Some(table) if (element * es + sub * 2 + size) <= table.len() => {
                let at = element * es + sub * 2;
                r.data = table[at..at + size].to_vec();
            }
            _ => r.sts = 0x10,
        }
    } else if req.cmd == 0x06 {
        let mut d = vec![0u8; 5];
        d.extend(b"1747-L542  "); // 11 chars of catalog text
        r.data = d;
    } else {
        r.sts = 0x60; // any write: command protection
    }
    r
}

async fn serve(listener: TcpListener, plc: Shared) {
    loop {
        let Ok((mut sock, _)) = listener.accept().await else { return };
        let _ = sock.set_nodelay(true);
        let plc = plc.clone();
        tokio::spawn(async move {
            let mut dec = FrameDecoder::new(Check::Crc);
            let mut buf = [0u8; 512];
            let mut last_header: Option<Vec<u8>> = None;
            loop {
                let Ok(n) = sock.read(&mut buf).await else { return };
                if n == 0 {
                    return;
                }
                for ev in dec.feed(&buf[..n]) {
                    match ev.kind {
                        EventKind::Frame => {
                            let req = Df1Message::parse(&ev.data).unwrap();
                            // (nak, lose, ack, reply, corrupt, dup, stale), decided under the lock
                            let (nak, lose, ack, reply, corrupt, dup, stale) = {
                                let mut g = plc.lock().unwrap();
                                if g.faults.mute {
                                    (false, true, false, false, false, false, false)
                                } else if g.faults.nak_next > 0 {
                                    g.faults.nak_next -= 1;
                                    (true, false, false, false, false, false, false)
                                } else if g.faults.lose_next > 0 {
                                    g.faults.lose_next -= 1;
                                    (false, true, false, false, false, false, false)
                                } else {
                                    let ack = if g.faults.drop_acks > 0 {
                                        g.faults.drop_acks -= 1;
                                        false
                                    } else {
                                        true
                                    };
                                    let corrupt = g.faults.corrupt_replies > 0;
                                    if corrupt {
                                        g.faults.corrupt_replies -= 1;
                                    }
                                    let stale = std::mem::take(&mut g.faults.stale_reply);
                                    let (reply, dup) = (!g.faults.no_reply, g.faults.duplicate_replies);
                                    g.commands.push((req.cmd, req.data.first().copied().unwrap_or(0)));
                                    (false, false, ack, reply, corrupt, dup, stale)
                                }
                            };
                            if nak {
                                sock.write_all(&DLE_NAK).await.unwrap();
                                continue;
                            }
                            if lose {
                                continue;
                            }
                            let same = last_header.as_deref() == Some(&ev.data[1..6]);
                            last_header = Some(ev.data[1..6].to_vec());
                            if ack {
                                sock.write_all(&DLE_ACK).await.unwrap();
                            }
                            if same || !reply {
                                continue;
                            }
                            let r = reply_for(&plc.lock().unwrap(), &req);
                            if stale {
                                let mut old = r.clone();
                                old.tns = old.tns.wrapping_sub(7);
                                sock.write_all(&encode_frame(&old.encode(), Check::Crc)).await.unwrap();
                            }
                            let mut frame = encode_frame(&r.encode(), Check::Crc);
                            if corrupt {
                                let k = frame.len() - 1;
                                frame[k] ^= 0xFF;
                            }
                            sock.write_all(&frame).await.unwrap();
                            if dup {
                                sock.write_all(&frame).await.unwrap();
                            }
                        }
                        EventKind::Bad => sock.write_all(&DLE_NAK).await.unwrap(),
                        _ => {}
                    }
                }
            }
        });
    }
}

fn table() -> Plc {
    let mut plc = Plc::default();
    plc.files.insert((0x89, 7), word_bytes(&[1, -2, 300, 4, 5, 6, 7, 8, 9, 10]));
    let f: Vec<u8> = [1.5f32, -2.25, 100.0].iter().flat_map(|x| x.to_le_bytes()).collect();
    plc.files.insert((0x8A, 8), f);
    plc.files.insert((0x91, 9), (-70000i32).to_le_bytes().into_iter().chain(5i32.to_le_bytes()).collect());
    plc.files.insert((0x85, 3), word_bytes(&[0x4001, 0]));
    // T4:0 = control 0x2000 (DN), PRE 500, ACC 1234
    plc.files.insert((0x86, 4), word_bytes(&[0x2000, 500, 1234]));
    let mut st = vec![0u8; 84];
    st[0] = 9;
    let text = b"Pump 1 ok\0";
    for (i, pair) in text.chunks(2).enumerate() {
        st[2 + i * 2] = pair[1];
        st[3 + i * 2] = pair[0];
    }
    plc.files.insert((0x8D, 10), st);
    plc.files.insert((0x83, 1), word_bytes(&[0, 0, 0, 300, 0, 0, 0, 0]));
    plc
}

async fn start(tags: Vec<Tag>, params: serde_json::Value, timeout_ms: i64) -> (Shared, AbDf1Driver) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = i64::from(listener.local_addr().unwrap().port());
    let plc: Shared = Arc::new(Mutex::new(table()));
    tokio::spawn(serve(listener, plc.clone()));
    let mut dev = Device::new("S/PLC", "S", "PLC", "ab_df1", "127.0.0.1", port, tags);
    dev.timeout_ms = timeout_ms;
    dev.params = params.as_object().cloned().unwrap_or_default();
    (plc, AbDf1Driver::new(&dev).unwrap())
}

fn tags() -> Vec<Tag> {
    vec![
        Tag::new("n0", "N7:0", DType::Int16),
        Tag::new("n1", "N7:1", DType::Int16),
        Tag::new("n1u", "N7:1", DType::Uint16),
        Tag::new("n2", "N7:2", DType::Int16),
        Tag::new("f0", "F8:0", DType::Float32),
        Tag::new("f1", "F8:1", DType::Float64),
        Tag::new("l0", "L9:0", DType::Int32),
        Tag::new("b14", "B3:0/14", DType::Bool),
        Tag::new("b0", "B3:0/0", DType::Bool),
        Tag::new("tacc", "T4:0.ACC", DType::Int16),
        Tag::new("tdn", "T4:0.DN", DType::Bool),
        Tag::new("st", "ST10:0", DType::String),
        Tag::new("in", "I:1.2", DType::Int16),
        Tag::new("missing", "N99:0", DType::Int16),
    ]
}

async fn read_all(
    d: &mut AbDf1Driver,
    tags: &[Tag],
) -> Vec<Result<HashMap<String, opencap_core::value::TagValue>, DriverError>> {
    let refs: Vec<&Tag> = tags.iter().collect();
    let plan = d.plan(&refs);
    let mut out = vec![];
    for b in &plan.blocks {
        out.push(d.read(b.as_ref()).await);
    }
    out
}

#[tokio::test]
async fn reads_every_file_type() {
    let t = tags();
    let (plc, mut d) = start(t.clone(), json!({}), 2000).await;
    d.connect().await.unwrap();
    assert!(d.connected());
    let mut got: HashMap<String, Value> = HashMap::new();
    let mut config_errors = 0;
    for r in read_all(&mut d, &t).await {
        match r {
            Ok(m) => {
                for (k, v) in m {
                    assert_eq!(v.quality, Quality::Good, "{k}");
                    got.insert(k, v.value.unwrap());
                }
            }
            Err(DriverError::BlockConfig(e)) => {
                assert!(e.contains("0x10"), "{e}");
                config_errors += 1;
            }
            Err(e) => panic!("{e}"),
        }
    }
    assert_eq!(config_errors, 1, "N99 does not exist: STS 0x10 is a config error");
    assert_eq!(got["n0"], Value::Int(1));
    assert_eq!(got["n1"], Value::Int(-2));
    assert_eq!(got["n1u"], Value::Int(65534));
    assert_eq!(got["n2"], Value::Int(300));
    assert_eq!(got["f0"], Value::Float(1.5));
    assert_eq!(got["f1"], Value::Float(-2.25));
    assert_eq!(got["l0"], Value::Int(-70000));
    assert_eq!(got["b14"], Value::Bool(true));
    assert_eq!(got["b0"], Value::Bool(true));
    assert_eq!(got["tacc"], Value::Int(1234));
    assert_eq!(got["tdn"], Value::Bool(true));
    assert_eq!(got["st"], Value::Str("Pump 1 ok".into()));
    assert_eq!(got["in"], Value::Int(300));
    let id = d.identity().await;
    assert_eq!(id["processor_type"], json!("1747-L542"));
    // only read commands reached the PLC
    assert!(plc.lock().unwrap().commands.iter().all(|c| c.0 == 0x0F && (c.1 == 0xA2 || c.1 == 0xA1) || c.0 == 0x06));
}

async fn one(d: &mut AbDf1Driver, t: &Tag) -> Result<Value, DriverError> {
    let plan = d.plan(&[t]);
    let m = d.read(plan.blocks[0].as_ref()).await?;
    Ok(m[&t.name].value.clone().expect("a value"))
}

#[tokio::test]
async fn link_faults_are_retried_inside_one_attempt() {
    let t = Tag::new("n0", "N7:0", DType::Int16);
    let (plc, mut d) = start(vec![t.clone()], json!({}), 3000).await;
    d.connect().await.unwrap();
    // NAKed twice, then accepted
    plc.lock().unwrap().faults.nak_next = 2;
    assert_eq!(one(&mut d, &t).await.unwrap(), Value::Int(1));
    // the first reply has a bad check: we NAK, the PLC is expected to resend; ours never gets it,
    // so this ends as a comm error (the worker retries) and the next read is clean
    plc.lock().unwrap().faults.corrupt_replies = 1;
    assert!(matches!(one(&mut d, &t).await, Err(DriverError::Comm(_))));
    assert_eq!(one(&mut d, &t).await.unwrap(), Value::Int(1));
    // a stale reply (older TNS) and a duplicated reply never answer a newer request
    plc.lock().unwrap().faults.stale_reply = true;
    plc.lock().unwrap().faults.duplicate_replies = true;
    for _ in 0..3 {
        assert_eq!(one(&mut d, &t).await.unwrap(), Value::Int(1));
    }
}

#[tokio::test]
async fn lost_ack_is_recovered_with_enq() {
    let t = Tag::new("n0", "N7:0", DType::Int16);
    let (plc, mut d) = start(vec![t.clone()], json!({"ack_timeout_ms": 150}), 3000).await;
    d.connect().await.unwrap();
    plc.lock().unwrap().faults.drop_acks = 1;
    // no ACK, but the reply arrives: a reply implies the request was received
    assert_eq!(one(&mut d, &t).await.unwrap(), Value::Int(1));
    // the frame is lost on the way: ENQ, then the same frame is not resent by us: comm error
    plc.lock().unwrap().faults.lose_next = 1;
    assert!(matches!(one(&mut d, &t).await, Err(DriverError::Comm(m)) if m.contains("ENQ")));
}

#[tokio::test]
async fn silence_and_missing_reply_are_comm_errors_and_the_line_survives() {
    let t = Tag::new("n0", "N7:0", DType::Int16);
    let (plc, mut d) = start(vec![t.clone()], json!({"enq_retries": 1}), 600).await;
    d.connect().await.unwrap();
    plc.lock().unwrap().faults.no_reply = true;
    let e = one(&mut d, &t).await.unwrap_err();
    assert!(matches!(&e, DriverError::Comm(m) if m.contains("no reply within")), "{e:?}");
    plc.lock().unwrap().faults.no_reply = false;
    plc.lock().unwrap().faults.mute = true;
    let e = one(&mut d, &t).await.unwrap_err();
    assert!(matches!(&e, DriverError::Comm(m) if m.contains("no ACK")), "{e:?}");
    plc.lock().unwrap().faults.mute = false;
    assert!(d.connected(), "a timeout does not close the line by default");
    assert_eq!(one(&mut d, &t).await.unwrap(), Value::Int(1));
}

#[tokio::test]
async fn close_on_timeout_closes_and_reconnect_works() {
    let t = Tag::new("n0", "N7:0", DType::Int16);
    let (plc, mut d) = start(vec![t.clone()], json!({"close_on_timeout": true, "enq_retries": 0}), 400).await;
    d.connect().await.unwrap();
    plc.lock().unwrap().faults.mute = true;
    assert!(matches!(one(&mut d, &t).await, Err(DriverError::Comm(m)) if m.contains("close_on_timeout")));
    assert!(!d.connected());
    assert!(matches!(one(&mut d, &t).await, Err(DriverError::ConnectionLost(_))));
    plc.lock().unwrap().faults.mute = false;
    d.connect().await.unwrap();
    assert_eq!(one(&mut d, &t).await.unwrap(), Value::Int(1));
}

#[tokio::test]
async fn connect_refused_is_connection_lost() {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = i64::from(l.local_addr().unwrap().port());
    drop(l);
    let dev = Device::new("S/PLC", "S", "PLC", "ab_df1", "127.0.0.1", port, vec![]);
    let mut d = AbDf1Driver::new(&dev).unwrap();
    assert!(matches!(d.connect().await, Err(DriverError::ConnectionLost(_))));
    assert!(!d.connected());
}

#[tokio::test]
async fn only_read_commands_can_be_sent() {
    use opencap_driver_ab_df1::link::{Df1Link, LinkError};
    let (a, _b) = tokio::io::duplex(1024);
    let mut link = Df1Link::new(Box::new(a), Check::Crc, true);
    // CMD 0x0F FNC 0xAA = protected typed logical write: refused before it reaches the wire
    for (cmd, fnc) in [(0x0F, 0xAA), (0x0F, 0xAB), (0x0F, 0x67), (0x0F, 0x00), (0x08, 0x00)] {
        let req = Df1Message { dst: 1, src: 0, cmd, sts: 0, tns: 1, data: vec![fnc, 2, 7, 0x89, 0, 0] };
        let r = link.transact(&req, Duration::from_millis(50), Duration::from_millis(20), 0, 0).await;
        assert!(matches!(r, Err(LinkError::Refused(_))), "{cmd:02x}/{fnc:02x}");
    }
    assert_eq!(link.stats.frames_sent, 0);
}

#[tokio::test]
async fn short_soak_with_noise() {
    let t = tags();
    let (plc, mut d) = start(t.clone(), json!({}), 2000).await;
    d.connect().await.unwrap();
    let n = AtomicU32::new(0);
    for i in 0..300u32 {
        if i % 50 == 10 {
            plc.lock().unwrap().faults.nak_next = 1;
        }
        for r in read_all(&mut d, &t).await {
            match r {
                Ok(_) | Err(DriverError::BlockConfig(_)) => {
                    n.fetch_add(1, Ordering::Relaxed);
                }
                Err(e) => panic!("round {i}: {e}"),
            }
        }
    }
    assert!(n.load(Ordering::Relaxed) >= 300 * 8);
}
