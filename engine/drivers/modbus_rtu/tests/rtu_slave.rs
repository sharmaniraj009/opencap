//! modbus_rtu against a small RTU-over-TCP slave written here: its own frame parser, bitwise
//! CRC and reply encoder, independent of tokio-modbus, with fault switches (mute, exception,
//! corrupted CRC) and a record of every function code and request time on the wire. The
//! Python simulator suite (tests/integration/test_rust_engine_rtu.py) is the acceptance test;
//! this one keeps the fault paths and line sharing in `cargo test` (also on Windows).

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Driver, DriverError};
use opencap_core::value::{Quality, Value};
use opencap_driver_modbus_rtu::ModbusRtuDriver;
use serde_json::{json, Value as Json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn crc16(data: &[u8]) -> u16 {
    let mut crc = 0xFFFFu16;
    for &b in data {
        crc ^= u16::from(b);
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xA001 } else { crc >> 1 };
        }
    }
    crc
}

#[derive(Default)]
struct SlaveState {
    units: Vec<u8>,
    mute: bool,
    exception: Option<u8>,
    corrupt_crc: bool,
    connections: usize,
    function_codes: Vec<u8>,
    request_times: Vec<Instant>,
}

#[derive(Clone)]
struct Slave {
    port: u16,
    state: Arc<Mutex<SlaveState>>,
}

impl Slave {
    async fn start(units: &[u8]) -> Slave {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let state = Arc::new(Mutex::new(SlaveState { units: units.to_vec(), ..Default::default() }));
        let st = state.clone();
        tokio::spawn(async move {
            while let Ok((sock, _)) = listener.accept().await {
                st.lock().unwrap().connections += 1;
                tokio::spawn(serve(sock, st.clone()));
            }
        });
        Slave { port, state }
    }

    fn set(&self, f: impl FnOnce(&mut SlaveState)) {
        f(&mut self.state.lock().unwrap());
    }

    fn get<T>(&self, f: impl FnOnce(&SlaveState) -> T) -> T {
        f(&self.state.lock().unwrap())
    }

    /// The slave's accept loop runs on its own task: wait for it to count a connection.
    async fn wait_connections(&self, n: usize) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while self.get(|s| s.connections) < n {
            assert!(Instant::now() < deadline, "slave saw {} connections, want {n}", self.get(|s| s.connections));
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }
}

/// Register value at an address (independent of the driver's decoder).
fn register(unit: u8, addr: u16) -> u16 {
    1000 * u16::from(unit) + addr
}

fn coil(addr: u16) -> bool {
    addr % 2 == 0
}

async fn serve(mut sock: tokio::net::TcpStream, st: Arc<Mutex<SlaveState>>) {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 256];
    loop {
        let n = match sock.read(&mut chunk).await {
            Ok(0) | Err(_) => return,
            Ok(n) => n,
        };
        buf.extend_from_slice(&chunk[..n]);
        while buf.len() >= 2 {
            let len = if buf[1] == 0x2B { 7 } else { 8 };
            if buf.len() < len {
                break;
            }
            let frame: Vec<u8> = buf.drain(..len).collect();
            let (body, crc) = frame.split_at(len - 2);
            assert_eq!(u16::from_le_bytes([crc[0], crc[1]]), crc16(body), "request CRC");
            let reply = {
                let mut s = st.lock().unwrap();
                s.function_codes.push(body[1]);
                s.request_times.push(Instant::now());
                if s.mute || !s.units.contains(&body[0]) {
                    None // silence, like a serial bus
                } else {
                    Some((answer(body, s.exception), s.corrupt_crc))
                }
            };
            if let Some((mut out, corrupt)) = reply {
                let crc = crc16(&out);
                out.extend_from_slice(&crc.to_le_bytes());
                if corrupt {
                    let last = out.len() - 1;
                    out[last] ^= 0xFF;
                }
                if sock.write_all(&out).await.is_err() {
                    return;
                }
            }
        }
    }
}

fn answer(req: &[u8], exception: Option<u8>) -> Vec<u8> {
    let (unit, fc) = (req[0], req[1]);
    if let Some(code) = exception {
        return vec![unit, fc | 0x80, code];
    }
    if fc == 0x2B {
        let vendor = b"opencap-rs";
        let mut out = vec![unit, 0x2B, 0x0E, 0x01, 0x01, 0x00, 0x00, 1, 0x00, vendor.len() as u8];
        out.extend_from_slice(vendor);
        return out;
    }
    let addr = u16::from_be_bytes([req[2], req[3]]);
    let count = u16::from_be_bytes([req[4], req[5]]);
    match fc {
        3 | 4 => {
            let mut out = vec![unit, fc, (2 * count) as u8];
            for a in addr..addr + count {
                out.extend_from_slice(&register(unit, a).to_be_bytes());
            }
            out
        }
        1 | 2 => {
            let mut bytes = vec![0u8; usize::from(count).div_ceil(8)];
            for i in 0..count {
                if coil(addr + i) {
                    bytes[usize::from(i / 8)] |= 1 << (i % 8);
                }
            }
            let mut out = vec![unit, fc, bytes.len() as u8];
            out.extend(bytes);
            out
        }
        _ => vec![unit, fc | 0x80, 1],
    }
}

fn device(name: &str, port: u16, params: Json) -> Device {
    let tags = vec![
        Tag::new("HR_11", "400011", DType::Uint16),
        Tag::new("HR_12", "400012", DType::Uint16),
        Tag::new("IR_3", "300003", DType::Uint16),
        Tag::new("CO_5", "000005", DType::Bool),
        Tag::new("CO_6", "000006", DType::Bool),
    ];
    let mut d = Device::new(&format!("SITE/{name}"), "SITE", name, "modbus_rtu", "127.0.0.1", i64::from(port), tags);
    let mut p = json!({"transport": "tcp", "close_on_timeout": true});
    p.as_object_mut().unwrap().extend(params.as_object().unwrap().clone());
    d.params = p.as_object().unwrap().clone();
    d.timeout_ms = 300;
    d
}

async fn read_all(drv: &mut ModbusRtuDriver) -> Result<BTreeMap<String, (Quality, Option<Value>)>, DriverError> {
    let tags: Vec<Tag> = drv_tags(drv);
    let refs: Vec<&Tag> = tags.iter().collect();
    let plan = drv.plan(&refs);
    let mut out = BTreeMap::new();
    for block in &plan.blocks {
        for (name, tv) in drv.read(block.as_ref()).await? {
            out.insert(name, (tv.quality, tv.value));
        }
    }
    Ok(out)
}

fn drv_tags(_drv: &ModbusRtuDriver) -> Vec<Tag> {
    device("X", 1, json!({})).tags
}

fn first_block(drv: &ModbusRtuDriver) -> opencap_core::driver::BlockRef {
    let tags = drv_tags(drv);
    let refs: Vec<&Tag> = tags.iter().collect();
    drv.plan(&refs).blocks[0].clone()
}

#[tokio::test]
async fn reads_decode_and_units_on_one_line_share_one_connection() {
    let slave = Slave::start(&[1, 2]).await;
    let mut a = ModbusRtuDriver::new(&device("A", slave.port, json!({"unit_id": 1}))).unwrap();
    let mut b = ModbusRtuDriver::new(&device("B", slave.port, json!({"unit_id": 2}))).unwrap();
    a.connect().await.unwrap();
    b.connect().await.unwrap();
    assert!(a.connected() && b.connected() && a.shares_line_with(&b));
    slave.wait_connections(1).await;
    read_all(&mut a).await.unwrap();
    assert_eq!(slave.get(|s| s.connections), 1, "one connection for the line");
    let va = read_all(&mut a).await.unwrap();
    let vb = read_all(&mut b).await.unwrap();
    // zero_based (default): 400011 -> register 10, 300003 -> input 2, 000005 -> coil 4
    assert_eq!(va["HR_11"], (Quality::Good, Some(Value::Int(1010))));
    assert_eq!(va["HR_12"], (Quality::Good, Some(Value::Int(1011))));
    assert_eq!(va["IR_3"], (Quality::Good, Some(Value::Int(1002))));
    assert_eq!(va["CO_5"], (Quality::Good, Some(Value::Bool(true))));
    assert_eq!(va["CO_6"], (Quality::Good, Some(Value::Bool(false))));
    assert_eq!(vb["HR_11"], (Quality::Good, Some(Value::Int(2010))));
    // one unit leaves: the line stays open for the other
    a.close().await;
    assert!(b.connected());
    assert_eq!(read_all(&mut b).await.unwrap()["IR_3"].1, Some(Value::Int(2002)));
    assert!(slave.get(|s| s.function_codes.iter().all(|fc| [1, 2, 3, 4].contains(fc))));
}

#[tokio::test]
async fn a_timeout_drops_the_line_for_every_unit() {
    let slave = Slave::start(&[1, 2]).await;
    let mut a = ModbusRtuDriver::new(&device("A", slave.port, json!({"unit_id": 1}))).unwrap();
    let mut b = ModbusRtuDriver::new(&device("B", slave.port, json!({"unit_id": 2}))).unwrap();
    a.connect().await.unwrap();
    b.connect().await.unwrap();
    slave.set(|s| s.mute = true);
    let started = Instant::now();
    let err = a.read(first_block(&a).as_ref()).await.unwrap_err();
    assert!(started.elapsed() < Duration::from_millis(900));
    assert!(
        matches!(&err, DriverError::Comm(m) if m.contains("no reply within 300 ms; socket closed (close_on_timeout)")
        && m.starts_with("SITE/A (unit 1, tcp 127.0.0.1:")),
        "{err:?}"
    );
    assert!(!a.connected() && !b.connected(), "one line: closed for every unit");
    slave.set(|s| s.mute = false);
    let err = b.read(first_block(&b).as_ref()).await.unwrap_err();
    assert!(matches!(&err, DriverError::ConnectionLost(m) if m.ends_with("not connected")), "{err:?}");
    b.connect().await.unwrap();
    a.connect().await.unwrap();
    assert!(a.shares_line_with(&b));
    slave.wait_connections(2).await;
    assert_eq!(read_all(&mut a).await.unwrap()["HR_11"].1, Some(Value::Int(1010)));
    assert_eq!(slave.get(|s| s.connections), 2, "one new connection for both units");
    assert_eq!(read_all(&mut a).await.unwrap()["HR_11"].1, Some(Value::Int(1010)));
}

#[tokio::test]
async fn exceptions_map_like_modbus_tcp_and_keep_the_line() {
    let slave = Slave::start(&[1]).await;
    let mut a = ModbusRtuDriver::new(&device("A", slave.port, json!({"unit_id": 1}))).unwrap();
    a.connect().await.unwrap();
    slave.set(|s| s.exception = Some(2));
    let err = a.read(first_block(&a).as_ref()).await.unwrap_err();
    assert!(
        matches!(&err, DriverError::BlockConfig(m) if m.ends_with("Modbus exception 02 (illegal data address)")),
        "{err:?}"
    );
    slave.set(|s| s.exception = Some(6));
    let err = a.read(first_block(&a).as_ref()).await.unwrap_err();
    assert!(matches!(&err, DriverError::Comm(m) if m.ends_with("Modbus exception 06 (server device busy)")), "{err:?}");
    assert!(a.connected());
}

#[tokio::test]
async fn a_corrupted_crc_or_an_unknown_unit_is_silence() {
    let slave = Slave::start(&[1]).await;
    let mut a =
        ModbusRtuDriver::new(&device("A", slave.port, json!({"unit_id": 1, "close_on_timeout": false}))).unwrap();
    a.connect().await.unwrap();
    slave.set(|s| s.corrupt_crc = true);
    let err = a.read(first_block(&a).as_ref()).await.unwrap_err();
    assert!(matches!(&err, DriverError::Comm(m) if m.ends_with("no reply within 300 ms")), "{err:?}");
    assert!(a.connected(), "close_on_timeout false keeps the line");
    slave.set(|s| s.corrupt_crc = false);
    assert_eq!(read_all(&mut a).await.unwrap()["HR_11"].1, Some(Value::Int(1010)));
    let mut stranger = ModbusRtuDriver::new(&device("U9", slave.port, json!({"unit_id": 9}))).unwrap();
    stranger.connect().await.unwrap();
    let err = stranger.read(first_block(&stranger).as_ref()).await.unwrap_err();
    assert!(matches!(&err, DriverError::Comm(m) if m.contains("no reply within 300 ms")), "{err:?}");
}

#[tokio::test]
async fn the_inter_frame_gap_is_kept_between_frames() {
    let slave = Slave::start(&[1]).await;
    let mut a = ModbusRtuDriver::new(&device("A", slave.port, json!({"unit_id": 1, "inter_frame_ms": 120}))).unwrap();
    a.connect().await.unwrap();
    let block = first_block(&a);
    a.read(block.as_ref()).await.unwrap();
    let started = Instant::now();
    for _ in 0..3 {
        a.read(block.as_ref()).await.unwrap();
    }
    assert!(started.elapsed() >= Duration::from_millis(3 * 120 - 10), "{:?}", started.elapsed());
    let times = slave.get(|s| s.request_times.clone());
    for pair in times.windows(2) {
        assert!(pair[1] - pair[0] >= Duration::from_millis(115), "{:?}", pair[1] - pair[0]);
    }
}

#[tokio::test]
async fn identity_over_rtu() {
    let slave = Slave::start(&[1]).await;
    let mut a = ModbusRtuDriver::new(&device("A", slave.port, json!({"unit_id": 1}))).unwrap();
    assert!(a.identity().await.is_empty(), "not connected: empty");
    a.connect().await.unwrap();
    let ident = a.identity().await;
    assert_eq!(ident.get("vendor"), Some(&json!("opencap-rs")));
    slave.set(|s| s.mute = true);
    assert!(a.identity().await.is_empty());
    assert!(a.connected(), "an identity timeout does not close the line");
    assert!(slave.get(|s| s.function_codes.iter().all(|fc| [1, 2, 3, 4, 0x2B].contains(fc))));
}

#[tokio::test]
async fn connect_errors_say_why() {
    let free = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let mut refused = ModbusRtuDriver::new(&device("A", free, json!({}))).unwrap();
    let err = refused.connect().await.unwrap_err();
    assert!(
        matches!(&err, DriverError::ConnectionLost(m) if m.contains("connect failed: refused or unreachable")),
        "{err:?}"
    );
    let mut rfc = ModbusRtuDriver::new(&device("A", 4001, json!({"transport": "rfc2217"}))).unwrap();
    assert_eq!(
        rfc.connect().await.unwrap_err(),
        DriverError::ConnectionLost(
            "SITE/A: transport rfc2217 is not supported by modbus_rtu (supported: tcp, serial)".into()
        )
    );
    let mut dev = device("A", 0, json!({"transport": "unmapped", "com_port": "COM4"}));
    dev.host = "COM4".into();
    let err = ModbusRtuDriver::new(&dev).unwrap().connect().await.unwrap_err();
    assert!(matches!(&err, DriverError::ConnectionLost(m) if m.starts_with("SITE/A: COM4 is not mapped")), "{err:?}");
    let mut dev = device("A", 0, json!({"transport": "serial"}));
    dev.host = if cfg!(windows) { "COM250".into() } else { "/dev/opencap-no-such-tty".into() };
    let err = ModbusRtuDriver::new(&dev).unwrap().connect().await.unwrap_err();
    assert!(matches!(&err, DriverError::ConnectionLost(m) if m.contains("connect failed: cannot open")), "{err:?}");
}

#[test]
fn bad_params_are_refused_with_pythons_messages() {
    let err = |p: Json| ModbusRtuDriver::new(&device("A", 1, p)).err().unwrap();
    assert_eq!(err(json!({"baudrate": 0})), "serial param baudrate=0: must be 50..4000000");
    assert_eq!(
        err(json!({"local_echo": "sometimes"})),
        "modbus_rtu param local_echo='sometimes': expected true or false"
    );
    assert_eq!(err(json!({"local_echo": null})), "modbus_rtu param local_echo=None: expected true or false");
    assert_eq!(err(json!({"inter_frame_ms": "soon"})), "modbus_rtu param inter_frame_ms='soon': expected a number");
    let ok = ModbusRtuDriver::new(&device("A", 1, json!({"baudrate": 9600, "local_echo": true}))).unwrap();
    assert!(ok.local_echo && (ok.gap_s - 3.5 * 10.0 / 9600.0).abs() < 1e-12);
}
