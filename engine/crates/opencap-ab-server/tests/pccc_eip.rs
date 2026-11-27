//! The `ab_pccc` driver (libplctag, FFI) against libplctag's own `ab_server` simulator in SLC 500
//! mode: seeded values of N, F, L, B and ST files, a missing file, a dead and a restarted gateway,
//! and a short soak. The simulator supports only the files B3, N7, F8, ST18 and L19.
//!
//! The simulator starts with zeroed files, so the test seeds it with libplctag's write calls
//! declared here, in a test of the simulator crate only. The engine crates cannot do that:
//! `opencap-plctag-sys` declares no write function and its test scans the engine sources.

use std::ffi::CString;
use std::os::raw::{c_char, c_int};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Driver, DriverError};
use opencap_core::value::Value;
use opencap_driver_ab_pccc_eip::AbPcccEipDriver;

extern "C" {
    fn plc_tag_create(attrib_str: *const c_char, timeout: c_int) -> i32;
    fn plc_tag_destroy(tag: i32) -> c_int;
    fn plc_tag_write(tag: i32, timeout: c_int) -> c_int;
    fn plc_tag_set_int16(tag: i32, offset: c_int, v: i16) -> c_int;
    fn plc_tag_set_int32(tag: i32, offset: c_int, v: i32) -> c_int;
    fn plc_tag_set_float32(tag: i32, offset: c_int, v: f32) -> c_int;
}

struct Sim(Child);

impl Drop for Sim {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn start(port: u16) -> Sim {
    let child = Command::new(env!("CARGO_BIN_EXE_opencap-ab-server"))
        .args([
            "--plc=SLC500".to_string(),
            format!("--port={port}"),
            "--tag=N7[10]".into(),
            "--tag=F8[10]".into(),
            "--tag=B3[10]".into(),
            "--tag=L19[10]".into(),
            "--tag=ST18[3]".into(),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn ab_server");
    // wait until it accepts connections
    for _ in 0..100 {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return Sim(child);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let mut child = child;
    let _ = child.kill();
    let _ = child.wait();
    panic!("ab_server did not start");
}

fn seed(port: u16, name: &str, count: usize, f: impl Fn(i32)) {
    let attr =
        CString::new(format!("protocol=ab-eip&gateway=127.0.0.1:{port}&cpu=slc500&name={name}&elem_count={count}"))
            .unwrap();
    unsafe {
        let t = plc_tag_create(attr.as_ptr(), 3000);
        assert!(t >= 0, "seed create {name}: {t}");
        f(t);
        assert_eq!(plc_tag_write(t, 3000), 0, "seed write {name}");
        plc_tag_destroy(t);
    }
}

fn seed_all(port: u16) {
    seed(port, "N7:0", 3, |t| unsafe {
        plc_tag_set_int16(t, 0, 1);
        plc_tag_set_int16(t, 2, -2);
        plc_tag_set_int16(t, 4, 300);
    });
    seed(port, "F8:0", 2, |t| unsafe {
        plc_tag_set_float32(t, 0, 1.5);
        plc_tag_set_float32(t, 4, -2.25);
    });
    seed(port, "L19:0", 1, |t| unsafe {
        plc_tag_set_int32(t, 0, -70000);
    });
    seed(port, "B3:0", 1, |t| unsafe {
        plc_tag_set_int16(t, 0, 0x4001);
    });
}

fn device(port: u16, tags: Vec<Tag>, timeout_ms: i64) -> Device {
    let mut d = Device::new("S/PLC", "S", "PLC", "ab_pccc", "127.0.0.1", i64::from(port), tags);
    d.timeout_ms = timeout_ms;
    d
}

fn tags() -> Vec<Tag> {
    vec![
        Tag::new("n0", "N7:0", DType::Int16),
        Tag::new("n1", "N7:1", DType::Int16),
        Tag::new("n1u", "N7:1", DType::Uint16),
        Tag::new("n2", "N7:2", DType::Int16),
        Tag::new("f0", "F8:0", DType::Float32),
        Tag::new("f1", "F8:1", DType::Float64),
        Tag::new("l0", "L19:0", DType::Int32),
        Tag::new("b14", "B3:0/14", DType::Bool),
        Tag::new("b0", "B3:0/0", DType::Bool),
        Tag::new("b1", "B3:0/1", DType::Bool),
    ]
}

async fn read_blocks(
    d: &mut AbPcccEipDriver,
    tags: &[Tag],
) -> Vec<Result<std::collections::HashMap<String, opencap_core::value::TagValue>, DriverError>> {
    let refs: Vec<&Tag> = tags.iter().collect();
    let plan = d.plan(&refs);
    assert!(plan.invalid.is_empty(), "{:?}", plan.invalid);
    let mut out = vec![];
    for b in &plan.blocks {
        out.push(d.read(b.as_ref()).await);
    }
    out
}

#[tokio::test(flavor = "multi_thread")]
async fn reads_seeded_values_errors_restart_and_soak() {
    let port = free_port();
    let sim = start(port);
    seed_all(port);
    let t = tags();
    let mut d = AbPcccEipDriver::new(&device(port, t.clone(), 3000)).unwrap();
    d.connect().await.unwrap();
    assert!(d.connected());

    let mut got = std::collections::HashMap::new();
    for r in read_blocks(&mut d, &t).await {
        for (k, v) in r.unwrap() {
            got.insert(k, v.value.expect("a value"));
        }
    }
    assert_eq!(got["n0"], Value::Int(1));
    assert_eq!(got["n1"], Value::Int(-2));
    assert_eq!(got["n1u"], Value::Int(65534));
    assert_eq!(got["n2"], Value::Int(300));
    assert_eq!(got["f0"], Value::Float(1.5));
    assert_eq!(got["f1"], Value::Float(-2.25));
    assert_eq!(got["l0"], Value::Int(-70000));
    assert_eq!(got["b14"], Value::Bool(true));
    assert_eq!(got["b0"], Value::Bool(true));
    assert_eq!(got["b1"], Value::Bool(false));

    // a file the PLC does not have is a permanent block error, and does not hurt the rest
    let missing = vec![Tag::new("m", "N99:0", DType::Int16)];
    let r = read_blocks(&mut d, &missing).await.pop().unwrap();
    println!("missing file: {r:?}");
    assert!(matches!(r, Err(DriverError::BlockConfig(_)) | Err(DriverError::Comm(_))), "{r:?}");
    assert!(read_blocks(&mut d, &t).await.iter().all(Result::is_ok));

    // soak
    for _ in 0..300 {
        assert!(read_blocks(&mut d, &t).await.iter().all(Result::is_ok));
    }

    // the gateway goes away: the error is a comm or connection error, never a wrong value
    drop(sim);
    let r = read_blocks(&mut d, &t).await.remove(0);
    println!("after kill: {r:?}");
    assert!(matches!(r, Err(DriverError::ConnectionLost(_)) | Err(DriverError::Comm(_))), "{r:?}");
    d.close().await;
    assert!(d.connect().await.is_err(), "connect must fail while the gateway is down");

    // and comes back (a restarted PLC has zeroed data)
    let _sim = start(port);
    d.connect().await.unwrap();
    let r = read_blocks(&mut d, &t).await;
    assert!(r.iter().all(Result::is_ok), "{r:?}");
}

#[tokio::test]
async fn bad_params_and_refused_gateway() {
    let mut dev = device(1, vec![], 500);
    dev.params.insert("plc".into(), serde_json::json!("s7"));
    assert!(AbPcccEipDriver::new(&dev).err().unwrap().contains("must be slc500, micrologix or plc5"));
    let mut d = AbPcccEipDriver::new(&device(free_port(), vec![], 500)).unwrap();
    assert!(matches!(d.connect().await, Err(DriverError::ConnectionLost(_))));
    assert!(!d.connected());
}
