//! The driver against an in-process S7 server: connect (TSAP, PDU negotiation), reads split by
//! the negotiated PDU, item return codes and header errors, a mute CPU, a CPU that goes away, a
//! reply arriving in pieces, SZL identity, and what the server ever receives. The full
//! simulator suite (`tests/helpers/s7_sim.py`) runs against the binary in
//! `tests/integration/test_rust_engine_s7.py`.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Driver, DriverError};
use opencap_core::value::Value;
use opencap_driver_s7::S7Driver;

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Normal,
    Mute,
    /// Answer read var with this item return code.
    ItemError(u8),
    /// Answer read var with this header error.
    HeaderError(u16),
    /// Reply in two writes 50 ms apart.
    Split,
    Hangup,
    /// Reply with a PDU reference one ahead.
    WrongSequence,
}

struct Plc {
    mode: Mutex<Mode>,
    max_pdu: usize,
    /// (rack/slot TSAP bytes) of every connection.
    tsaps: Mutex<Vec<(u8, u8)>>,
    /// (pdu type, function) of every S7 request.
    seen: Mutex<Vec<(u8, u8)>>,
    /// Read sizes in bytes.
    reads: Mutex<Vec<usize>>,
}

fn frame(s7: &[u8]) -> Vec<u8> {
    let mut f = vec![3, 0];
    f.extend_from_slice(&((s7.len() + 7) as u16).to_be_bytes());
    f.extend_from_slice(&[2, 0xF0, 0x80]);
    f.extend_from_slice(s7);
    f
}

fn ack(seq: u16, params: &[u8], data: &[u8], error: u16) -> Vec<u8> {
    let mut h = vec![0x32, 3, 0, 0];
    h.extend_from_slice(&seq.to_be_bytes());
    h.extend_from_slice(&(params.len() as u16).to_be_bytes());
    h.extend_from_slice(&(data.len() as u16).to_be_bytes());
    h.extend_from_slice(&error.to_be_bytes());
    h.extend_from_slice(params);
    h.extend_from_slice(data);
    h
}

impl Plc {
    fn reply(&self, pdu: &[u8]) -> Option<Vec<u8>> {
        let seq = u16::from_be_bytes([pdu[4], pdu[5]]);
        self.seen.lock().unwrap().push((pdu[1], pdu[10.min(pdu.len() - 1)]));
        let mode = *self.mode.lock().unwrap();
        if mode == Mode::Mute {
            return None;
        }
        let reply_seq = if mode == Mode::WrongSequence { seq + 1 } else { seq };
        if pdu[1] == 0x07 {
            return Some(self.szl(reply_seq, pdu));
        }
        match pdu[10] {
            0xF0 => {
                let want = usize::from(u16::from_be_bytes([pdu[16], pdu[17]]));
                let neg = want.min(self.max_pdu) as u16;
                let mut params = vec![0xF0, 0, 0, 1, 0, 1];
                params.extend_from_slice(&neg.to_be_bytes());
                Some(ack(reply_seq, &params, &[], 0))
            }
            0x04 => {
                if let Mode::HeaderError(e) = mode {
                    return Some(ack(reply_seq, &[], &[], e));
                }
                let word_len = pdu[15];
                let count = usize::from(u16::from_be_bytes([pdu[16], pdu[17]]));
                let start = u32::from_be_bytes([0, pdu[21], pdu[22], pdu[23]]) as usize / 8;
                let width = if word_len == 0x02 { 1 } else { 2 };
                let n = count * width;
                self.reads.lock().unwrap().push(n);
                if 18 + n > self.max_pdu {
                    return Some(ack(reply_seq, &[], &[], 0x8500));
                }
                if let Mode::ItemError(rc) = mode {
                    return Some(ack(reply_seq, &[4, 1], &[rc, 0, 0, 0], 0));
                }
                let data: Vec<u8> = (0..n).map(|i| ((start + i) & 0xFF) as u8).collect();
                let mut item = vec![0xFF, 0x04];
                item.extend_from_slice(&((n * 8) as u16).to_be_bytes());
                item.extend_from_slice(&data);
                Some(ack(reply_seq, &[4, 1], &item, 0))
            }
            _ => Some(ack(reply_seq, &[], &[], 0x8104)),
        }
    }

    /// A USERDATA SZL reply with a record per list id.
    fn szl(&self, seq: u16, pdu: &[u8]) -> Vec<u8> {
        let id = u16::from_be_bytes([pdu[22], pdu[23]]);
        let rec = match id {
            0x001C => {
                // the partial list header (4 bytes), then the record: data offsets as in the Python parser
                let mut d = vec![0u8; 212];
                d[1] = 34;
                d[3] = 1;
                d[6..12].copy_from_slice(b"SIM-AS");
                d[176..186].copy_from_slice(b"CPU 1511-1");
                d
            }
            0x0011 => {
                let mut d = vec![0u8; 32];
                d[1] = 28;
                d[3] = 1;
                d[5] = 1;
                d[6..26].copy_from_slice(b"6ES7 511-1AK02-0AB0 ");
                d[27..30].copy_from_slice(&[2, 9, 2]);
                d
            }
            _ => vec![0, 0, 0, 0, 0, 0, 0, 8],
        };
        self.szl_reply(seq, id, &rec)
    }

    fn szl_reply(&self, seq: u16, id: u16, body: &[u8]) -> Vec<u8> {
        let mut data = vec![0xFF, 0x09];
        data.extend_from_slice(&((body.len() + 4) as u16).to_be_bytes());
        data.extend_from_slice(&id.to_be_bytes());
        data.extend_from_slice(&[0, 0]);
        data.extend_from_slice(body);
        let params = [0x00, 0x01, 0x12, 0x08, 0x12, 0x84, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00];
        let mut f = vec![0x32, 7, 0, 0];
        f.extend_from_slice(&seq.to_be_bytes());
        f.extend_from_slice(&12u16.to_be_bytes());
        f.extend_from_slice(&(data.len() as u16).to_be_bytes());
        f.extend_from_slice(&params);
        f.extend_from_slice(&data);
        f
    }
}

async fn serve(plc: Arc<Plc>) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let Ok((mut s, _)) = listener.accept().await else { return };
            let plc = plc.clone();
            tokio::spawn(async move {
                let mut head = [0u8; 4];
                if s.read_exact(&mut head).await.is_err() {
                    return;
                }
                let mut cr = vec![0u8; usize::from(u16::from_be_bytes([head[2], head[3]])) - 4];
                if s.read_exact(&mut cr).await.is_err() {
                    return;
                }
                // parameters start at byte 7: C1 02 <local> C2 02 <remote> C0 01 0A
                plc.tsaps.lock().unwrap().push((cr[13], cr[14]));
                let cc = [3u8, 0, 0, 11, 6, 0xD0, 0, 1, 0, 9, 0];
                if s.write_all(&cc).await.is_err() {
                    return;
                }
                loop {
                    let mut head = [0u8; 4];
                    if s.read_exact(&mut head).await.is_err() {
                        return;
                    }
                    let mut body = vec![0u8; usize::from(u16::from_be_bytes([head[2], head[3]])) - 4];
                    if s.read_exact(&mut body).await.is_err() {
                        return;
                    }
                    if body.len() < 3 || body[1] != 0xF0 {
                        return; // COTP disconnect
                    }
                    if *plc.mode.lock().unwrap() == Mode::Hangup && body[3 + 10] == 0x04 {
                        return;
                    }
                    let Some(reply) = plc.reply(&body[3..]) else { continue };
                    let out = frame(&reply);
                    if *plc.mode.lock().unwrap() == Mode::Split {
                        let cut = out.len() / 2;
                        s.write_all(&out[..cut]).await.unwrap();
                        s.flush().await.unwrap();
                        tokio::time::sleep(Duration::from_millis(50)).await;
                        s.write_all(&out[cut..]).await.unwrap();
                    } else {
                        s.write_all(&out).await.unwrap();
                    }
                }
            });
        }
    });
    port
}

fn plc(mode: Mode, max_pdu: usize) -> Arc<Plc> {
    Arc::new(Plc {
        mode: Mutex::new(mode),
        max_pdu,
        tsaps: Mutex::new(vec![]),
        seen: Mutex::new(vec![]),
        reads: Mutex::new(vec![]),
    })
}

fn device(port: u16, params: serde_json::Value) -> Device {
    let tags = vec![
        Tag::new("A", "DB1,B0", DType::Uint16),
        Tag::new("B", "DB1,W2", DType::Uint16),
        Tag::new("BIG", "DB300,B0[800]", DType::Uint16),
    ];
    let mut d = Device::new("SITE/PLC", "SITE", "PLC", "s7", "127.0.0.1", i64::from(port), tags);
    d.timeout_ms = 400;
    d.params = params.as_object().unwrap().clone();
    d
}

async fn read_block(
    drv: &mut S7Driver,
    dev: &Device,
    which: usize,
) -> Result<Vec<(String, Option<Value>)>, DriverError> {
    let tags: Vec<&Tag> = dev.tags.iter().collect();
    let plan = drv.plan(&tags);
    let mut got: Vec<_> = drv.read(plan.blocks[which].as_ref()).await?.into_iter().map(|(n, v)| (n, v.value)).collect();
    got.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(got)
}

#[tokio::test]
async fn connects_with_the_right_tsap_reads_and_sends_nothing_but_reads() {
    let p = plc(Mode::Normal, 480);
    let port = serve(p.clone()).await;
    let dev = device(port, serde_json::json!({"rack": 0, "slot": 2, "link_type": "PG"}));
    let mut drv = S7Driver::new(&dev).unwrap();
    assert!(!drv.connected() && drv.identity().await.is_empty());
    drv.connect().await.unwrap();
    assert_eq!(*p.tsaps.lock().unwrap(), vec![(0x01, 0x02)]); // PG, rack 0, slot 2
    let got = read_block(&mut drv, &dev, 0).await.unwrap();
    // memory byte n holds n: A = DB1,B0 = 0, B = DB1,W2 = bytes 2, 3
    assert_eq!(got, vec![("A".to_string(), Some(Value::Int(0))), ("B".to_string(), Some(Value::Int(0x0203)))]);
    let ident = drv.identity().await;
    assert_eq!(ident["pdu_negotiated"], 480);
    assert_eq!(ident["connection_type"], "PG");
    assert_eq!(ident["module_type_name"], "CPU 1511-1");
    assert_eq!(ident["order_code"], "6ES7 511-1AK02-0AB0");
    assert_eq!(ident["firmware"], "V2.9.2");
    assert_eq!(ident["cpu_state"], "run");
    let seen = p.seen.lock().unwrap().clone();
    assert!(seen.iter().all(|(t, f)| (*t == 1 && (*f == 0xF0 || *f == 0x04)) || *t == 7), "{seen:?}");
}

#[tokio::test]
async fn a_block_larger_than_the_negotiated_pdu_is_read_in_several_requests() {
    let p = plc(Mode::Normal, 480);
    let port = serve(p.clone()).await;
    let dev = device(port, serde_json::json!({"max_pdu": 960}));
    let mut drv = S7Driver::new(&dev).unwrap();
    let tags: Vec<&Tag> = dev.tags.iter().collect();
    let plan = drv.plan(&tags);
    assert_eq!(plan.wire_requests(), 2); // planned for 960: one request per block
    drv.connect().await.unwrap();
    assert_eq!(plan.wire_requests(), 3); // the CPU said 480: the 800-byte block takes 462 + 338
    p.reads.lock().unwrap().clear();
    let got = read_block(&mut drv, &dev, 1).await.unwrap();
    let Some(Value::List(v)) = &got[0].1 else { panic!("{got:?}") };
    assert_eq!((v.len(), v[0].clone(), v[799].clone()), (800, Value::Int(0), Value::Int(799 & 0xFF)));
    assert_eq!(*p.reads.lock().unwrap(), vec![462, 338]);
}

#[tokio::test]
async fn item_return_codes_and_header_errors_map_to_the_python_classes() {
    for (mode, config) in [
        (Mode::ItemError(0x0A), true),
        (Mode::ItemError(0x05), true),
        (Mode::ItemError(0x03), true),
        (Mode::HeaderError(0x8104), true),
        (Mode::ItemError(0x01), false),
        (Mode::HeaderError(0x8500), false),
    ] {
        let p = plc(mode, 480);
        let port = serve(p).await;
        let dev = device(port, serde_json::json!({}));
        let mut drv = S7Driver::new(&dev).unwrap();
        drv.connect().await.unwrap();
        let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
        assert_eq!(matches!(err, DriverError::BlockConfig(_)), config, "{err:?}");
        assert!(matches!(err, DriverError::BlockConfig(_) | DriverError::Comm(_)), "{err:?}");
        assert!(drv.connected(), "an error reply does not close the connection: {err}");
    }
}

#[tokio::test]
async fn text_of_a_refused_item() {
    let p = plc(Mode::ItemError(0x0A), 480);
    let port = serve(p).await;
    let dev = device(port, serde_json::json!({}));
    let mut drv = S7Driver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(err.to_string().contains("DB1 0..3 (4 bytes): item return code 0x0A (object does not exist)"), "{err}");
}

#[tokio::test]
async fn a_mute_cpu_is_a_comm_error_and_closes_the_connection() {
    let p = plc(Mode::Mute, 480);
    let port = serve(p.clone()).await;
    let mut dev = device(port, serde_json::json!({}));
    dev.timeout_ms = 300;
    let mut drv = S7Driver::new(&dev).unwrap();
    // a mute CPU never answers setup communication either
    let err = drv.connect().await.unwrap_err();
    assert!(matches!(err, DriverError::ConnectionLost(_)), "{err:?}");
    *p.mode.lock().unwrap() = Mode::Normal;
    drv.connect().await.unwrap();
    *p.mode.lock().unwrap() = Mode::Mute;
    let started = std::time::Instant::now();
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(
        matches!(err, DriverError::Comm(_)) && err.to_string().contains("no reply within 300 ms; connection closed"),
        "{err}"
    );
    assert!(started.elapsed() < Duration::from_secs(1) && !drv.connected());
    *p.mode.lock().unwrap() = Mode::Normal;
    drv.connect().await.unwrap();
    assert!(read_block(&mut drv, &dev, 0).await.is_ok());
}

#[tokio::test]
async fn a_reply_in_two_tcp_segments_is_read_whole_and_a_wrong_reference_is_refused() {
    let p = plc(Mode::Split, 480);
    let port = serve(p.clone()).await;
    let mut dev = device(port, serde_json::json!({}));
    dev.timeout_ms = 2000;
    let mut drv = S7Driver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    assert_eq!(read_block(&mut drv, &dev, 0).await.unwrap().len(), 2);
    *p.mode.lock().unwrap() = Mode::WrongSequence;
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(matches!(err, DriverError::Comm(_)) && err.to_string().contains("Packet lost"), "{err}");
    assert!(!drv.connected());
}

#[tokio::test]
async fn a_cpu_that_hangs_up_is_connection_lost_and_nothing_listening_fails_the_connect() {
    let p = plc(Mode::Hangup, 480);
    let port = serve(p).await;
    let dev = device(port, serde_json::json!({}));
    let mut drv = S7Driver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(matches!(err, DriverError::ConnectionLost(_)), "{err:?}");
    assert!(!drv.connected());
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(err.to_string().ends_with("not connected"), "{err}");
    let free = TcpListener::bind("127.0.0.1:0").await.unwrap().local_addr().unwrap().port();
    let mut gone = S7Driver::new(&device(free, serde_json::json!({}))).unwrap();
    let err = gone.connect().await.unwrap_err();
    assert!(matches!(err, DriverError::ConnectionLost(_)) && err.to_string().contains("connect failed"), "{err}");
}

#[test]
fn bad_params_are_refused_with_the_python_text() {
    assert_eq!(
        S7Driver::new(&device(1, serde_json::json!({"rack": 8}))).err().unwrap(),
        "s7 param rack=8: must be 0..7"
    );
}
