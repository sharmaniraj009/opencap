//! The driver against an in-process MC 3E server: whole-frame reads (a reply split across TCP
//! segments), end-code mapping, a mute PLC, a PLC that goes away, UDP, and what the server
//! ever receives. The full simulator suite (`tests/helpers/mc_sim.py`) runs against the binary
//! in `tests/integration/test_rust_engine_melsec.py`.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, UdpSocket};

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Driver, DriverError};
use opencap_core::value::{Quality, Value};
use opencap_driver_melsec_mc::MelsecMcDriver;

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Normal,
    /// Send each reply in two writes, 50 ms apart.
    Split,
    /// Answer with this end code.
    EndCode(u16),
    /// Read requests, answer nothing.
    Mute,
    /// Announce one word fewer than asked.
    Short,
    /// Close the connection on the first request.
    Hangup,
}

struct Plc {
    mode: Mutex<Mode>,
    /// (command, subcommand) of every request received.
    seen: Mutex<Vec<(u16, u16)>>,
}

impl Plc {
    fn reply(&self, req: &[u8]) -> Option<Vec<u8>> {
        let cmd = (u16::from_le_bytes([req[11], req[12]]), u16::from_le_bytes([req[13], req[14]]));
        self.seen.lock().unwrap().push(cmd);
        let mode = *self.mode.lock().unwrap();
        let (data, end): (Vec<u8>, u16) = match mode {
            Mode::Mute | Mode::Hangup => return None,
            Mode::EndCode(c) => (req[2..7].iter().chain(&req[11..15]).copied().collect(), c),
            _ if cmd == (0x0101, 0) => (b"Q03UDVCPU       \x66\x03".to_vec(), 0),
            _ => {
                // batch read: device number is memory address, value of word n = number + n
                let iqr = cmd.1 == 2;
                let (head, points) = if iqr {
                    (u32::from_le_bytes([req[15], req[16], req[17], req[18]]), u16::from_le_bytes([req[21], req[22]]))
                } else {
                    (u32::from_le_bytes([req[15], req[16], req[17], 0]), u16::from_le_bytes([req[19], req[20]]))
                };
                let points = if mode == Mode::Short { points - 1 } else { points };
                ((0..points).flat_map(|i| ((head + u32::from(i)) as u16).to_le_bytes()).collect(), 0)
            }
        };
        let mut r = vec![0xD0, 0x00];
        r.extend_from_slice(&req[2..7]);
        r.extend_from_slice(&((data.len() + 2) as u16).to_le_bytes());
        r.extend_from_slice(&end.to_le_bytes());
        r.extend_from_slice(&data);
        Some(r)
    }
}

async fn serve_tcp(plc: Arc<Plc>) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let Ok((mut s, _)) = listener.accept().await else { return };
            let plc = plc.clone();
            tokio::spawn(async move {
                loop {
                    let mut head = [0u8; 9];
                    if s.read_exact(&mut head).await.is_err() {
                        return;
                    }
                    let mut body = vec![0u8; usize::from(u16::from_le_bytes([head[7], head[8]]))];
                    if s.read_exact(&mut body).await.is_err() {
                        return;
                    }
                    let req = [head.to_vec(), body].concat();
                    if *plc.mode.lock().unwrap() == Mode::Hangup {
                        return;
                    }
                    let Some(reply) = plc.reply(&req) else { continue };
                    if *plc.mode.lock().unwrap() == Mode::Split {
                        let cut = reply.len() / 2;
                        s.write_all(&reply[..cut]).await.unwrap();
                        s.flush().await.unwrap();
                        tokio::time::sleep(Duration::from_millis(50)).await;
                        s.write_all(&reply[cut..]).await.unwrap();
                    } else {
                        s.write_all(&reply).await.unwrap();
                    }
                }
            });
        }
    });
    port
}

async fn serve_udp(plc: Arc<Plc>) -> u16 {
    let sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let port = sock.local_addr().unwrap().port();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 65535];
        loop {
            let Ok((n, from)) = sock.recv_from(&mut buf).await else { return };
            if let Some(r) = plc.reply(&buf[..n]) {
                let _ = sock.send_to(&r, from).await;
            }
        }
    });
    port
}

fn plc(mode: Mode) -> Arc<Plc> {
    Arc::new(Plc { mode: Mutex::new(mode), seen: Mutex::new(Vec::new()) })
}

fn device(port: u16, params: serde_json::Value) -> Device {
    let tags = vec![
        Tag::new("A", "D10", DType::Uint16),
        Tag::new("B", "D12", DType::Uint32),
        Tag::new("BIG", "D1000[960]", DType::Uint16),
    ];
    let mut d = Device::new("SITE/PLC", "SITE", "PLC", "melsec_mc", "127.0.0.1", i64::from(port), tags);
    d.timeout_ms = 400;
    d.params = params.as_object().unwrap().clone();
    d
}

async fn read_block(
    drv: &mut MelsecMcDriver,
    device: &Device,
    which: usize,
) -> Result<Vec<(String, Option<Value>, Quality)>, DriverError> {
    let tags: Vec<&Tag> = device.tags.iter().collect();
    let plan = drv.plan(&tags);
    let block = &plan.blocks[which];
    let mut got: Vec<_> = drv.read(block.as_ref()).await?.into_iter().map(|(n, v)| (n, v.value, v.quality)).collect();
    got.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(got)
}

#[tokio::test]
async fn reads_a_block_and_sends_nothing_but_reads() {
    let p = plc(Mode::Normal);
    let port = serve_tcp(p.clone()).await;
    let dev = device(port, serde_json::json!({}));
    let mut drv = MelsecMcDriver::new(&dev).unwrap();
    assert!(!drv.connected());
    assert!(drv.identity().await.is_empty());
    drv.connect().await.unwrap();
    let got = read_block(&mut drv, &dev, 0).await.unwrap();
    // word n of the fake PLC holds n; D12/D13 as uint32, low word first: 13 << 16 | 12
    assert_eq!(
        got,
        vec![
            ("A".to_string(), Some(Value::Int(10)), Quality::Good),
            ("B".to_string(), Some(Value::Int((13 << 16) | 12)), Quality::Good)
        ]
    );
    let ident = drv.identity().await;
    assert_eq!(ident["cpu_type"], "Q03UDVCPU");
    assert_eq!(ident["cpu_code"], "0366");
    assert_eq!(ident["plc_type"], "Q");
    assert!(
        p.seen.lock().unwrap().iter().all(|c| *c == (0x0401, 0) || *c == (0x0101, 0)),
        "{:?}",
        p.seen.lock().unwrap()
    );
}

#[tokio::test]
async fn a_960_word_reply_split_across_tcp_segments_is_read_whole() {
    let p = plc(Mode::Split);
    let port = serve_tcp(p).await;
    let mut dev = device(port, serde_json::json!({}));
    dev.timeout_ms = 2000;
    let mut drv = MelsecMcDriver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let got = read_block(&mut drv, &dev, 1).await.unwrap();
    assert_eq!(got.len(), 1);
    let Some(Value::List(words)) = &got[0].1 else { panic!("{got:?}") };
    assert_eq!(words.len(), 960);
    assert_eq!(words[0], Value::Int(1000));
    assert_eq!(words[959], Value::Int(1959)); // not zero-filled
}

#[tokio::test]
async fn iqr_frames_use_subcommand_2() {
    let p = plc(Mode::Normal);
    let port = serve_tcp(p.clone()).await;
    let dev = device(port, serde_json::json!({"plc_type": "iQ-R"}));
    let mut drv = MelsecMcDriver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let got = read_block(&mut drv, &dev, 0).await.unwrap();
    assert_eq!(got[0].1, Some(Value::Int(10)));
    assert_eq!(*p.seen.lock().unwrap(), vec![(0x0401, 2)]);
}

#[tokio::test]
async fn end_codes_map_to_block_config_or_comm_and_keep_the_socket() {
    for (code, config) in [(0xC056, true), (0xC059, true), (0x4031, true), (0xC061, false), (0x4010, false)] {
        let p = plc(Mode::EndCode(code));
        let port = serve_tcp(p).await;
        let dev = device(port, serde_json::json!({}));
        let mut drv = MelsecMcDriver::new(&dev).unwrap();
        drv.connect().await.unwrap();
        let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
        let text = err.to_string();
        assert!(text.contains(&format!("MC end code {code:#06X}")), "{text}");
        assert_eq!(matches!(err, DriverError::BlockConfig(_)), config, "{code:#06x}: {err:?}");
        assert!(matches!(err, DriverError::BlockConfig(_) | DriverError::Comm(_)));
        assert!(drv.connected(), "an end code does not close the socket");
    }
}

#[tokio::test]
async fn a_mute_plc_is_a_comm_error_and_closes_the_socket() {
    let p = plc(Mode::Mute);
    let port = serve_tcp(p.clone()).await;
    let mut dev = device(port, serde_json::json!({}));
    dev.timeout_ms = 300;
    let mut drv = MelsecMcDriver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let started = std::time::Instant::now();
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(matches!(err, DriverError::Comm(_)), "{err:?}");
    assert!(err.to_string().contains("no reply within 300 ms; socket closed"), "{err}");
    assert!(started.elapsed() < Duration::from_secs(1));
    assert!(!drv.connected(), "a late reply must not be taken as the next one");
    *p.mode.lock().unwrap() = Mode::Normal;
    drv.connect().await.unwrap();
    assert!(read_block(&mut drv, &dev, 0).await.is_ok());
}

#[tokio::test]
async fn a_reply_with_the_wrong_word_count_is_an_error_not_zeros() {
    let p = plc(Mode::Short);
    let port = serve_tcp(p).await;
    let dev = device(port, serde_json::json!({}));
    let mut drv = MelsecMcDriver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(matches!(err, DriverError::Comm(_)), "{err:?}");
    assert!(
        err.to_string().contains("reply carries 6 data bytes for 4 words") || err.to_string().contains("bad reply"),
        "{err}"
    );
    assert!(!drv.connected());
}

#[tokio::test]
async fn a_peer_that_closes_is_connection_lost_and_connect_to_nothing_fails() {
    let p = plc(Mode::Hangup);
    let port = serve_tcp(p).await;
    let dev = device(port, serde_json::json!({}));
    let mut drv = MelsecMcDriver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(matches!(err, DriverError::ConnectionLost(_)), "{err:?}");
    assert!(!drv.connected());
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(err.to_string().ends_with("not connected"), "{err}");

    let free = {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        l.local_addr().unwrap().port()
    };
    let mut gone = MelsecMcDriver::new(&device(free, serde_json::json!({}))).unwrap();
    let err = gone.connect().await.unwrap_err();
    assert!(matches!(err, DriverError::ConnectionLost(_)));
    assert!(err.to_string().contains("connect failed"), "{err}");
}

#[tokio::test]
async fn udp_one_frame_per_datagram() {
    let p = plc(Mode::Normal);
    let port = serve_udp(p.clone()).await;
    let dev = device(port, serde_json::json!({"protocol": "udp"}));
    let mut drv = MelsecMcDriver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let got = read_block(&mut drv, &dev, 0).await.unwrap();
    assert_eq!(got[0].1, Some(Value::Int(10)));
    assert_eq!(drv.identity().await["protocol"], "udp");
    *p.mode.lock().unwrap() = Mode::Mute;
    let err = read_block(&mut drv, &dev, 0).await.unwrap_err();
    assert!(matches!(err, DriverError::Comm(_)) && err.to_string().contains("no reply within 400 ms"), "{err}");
    assert!(!drv.connected());
}

#[test]
fn bad_params_are_refused_with_the_python_text() {
    let dev = device(1, serde_json::json!({"plc_type": "A"}));
    assert_eq!(
        MelsecMcDriver::new(&dev).err().unwrap(),
        "melsec_mc param plc_type='A': expected one of ('Q', 'L', 'QnA', 'iQ-L', 'iQ-R')"
    );
}
