//! Same simulator (`opencap-ab-server`, build it first: `cargo build -p opencap-ab-server` in engine/),
//! same tags, two clients. Run: `cargo test --release --manifest-path spikes/pccc-eval/Cargo.toml -- --nocapture`.
//! The seeding writes go to the simulator only; this crate is an evaluation tool, not engine code.
use std::ffi::CString;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use ciplink::slc::driver::SlcDriver;
use ciplink::Value;
use opencap_plctag_sys as p;

fn server_path() -> String {
    format!("{}/../../target/debug/opencap-ab-server", env!("CARGO_MANIFEST_DIR"))
}

fn start(port: u16, plc: &str) -> Child {
    let c = Command::new(server_path())
        .args([&format!("--plc={plc}"), &format!("--port={port}"), "--tag=N7[10]", "--tag=F8[10]", "--tag=B3[10]", "--tag=L9[10]"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn ab_server (cargo build -p opencap-ab-server first)");
    std::thread::sleep(Duration::from_millis(500));
    c
}

fn cpu_s() -> f64 {
    let s = std::fs::read_to_string("/proc/self/stat").unwrap();
    let f: Vec<&str> = s.rsplit(')').next().unwrap().split_whitespace().collect();
    (f[11].parse::<f64>().unwrap() + f[12].parse::<f64>().unwrap()) / 100.0
}

fn plctag_read(port: u16, name: &str, n: usize) -> Result<Vec<u8>, i32> {
    let attr = CString::new(format!("protocol=ab-eip&gateway=127.0.0.1:{port}&cpu=slc500&name={name}&elem_count={n}")).unwrap();
    unsafe {
        let t = p::plc_tag_create(attr.as_ptr(), 2000);
        if t < 0 {
            return Err(t);
        }
        let rc = p::plc_tag_read(t, 2000);
        if rc != 0 {
            p::plc_tag_destroy(t);
            return Err(rc);
        }
        let size = p::plc_tag_get_size(t);
        let mut buf = vec![0u8; size as usize];
        p::plc_tag_get_raw_bytes(t, 0, buf.as_mut_ptr(), size);
        p::plc_tag_destroy(t);
        Ok(buf)
    }
}

#[test]
fn compare() {
    let port = 44901;
    let mut srv = start(port, "SLC500");
    let plc = SlcDriver::new(&format!("127.0.0.1:{port}"), SlcDriver::default_options()).expect("new");
    println!("open: {:?}", plc.open());
    for (a, v) in [("N7:0", Value::Int(1)), ("N7:1", Value::Int(-2)), ("N7:2", Value::Int(300)), ("F8:0", Value::Real(1.5)), ("F8:1", Value::Real(-2.25)), ("L9:0", Value::DInt(-70000)), ("B3:0", Value::Int(0x4001))] {
        let r = plc.write(&[(a, v)]).unwrap();
        println!("seed {a}: {:?} {:?}", r[0].value, r[0].error);
    }
    for a in ["N7:0", "N7:0{3}", "F8:0{2}", "L9:0", "B3:0", "B3:0/14", "N99:0", "N7:300"] {
        let r = plc.read(&[a]).unwrap();
        println!("ciplink read {a}: {:?} err={:?}", r[0].value, r[0].error);
    }
    println!("libplctag N7:0 x3 = {:?}", plctag_read(port, "N7:0", 3));
    println!("libplctag F8:0 x2 = {:?}", plctag_read(port, "F8:0", 2));
    println!("libplctag L9:0 = {:?}", plctag_read(port, "L9:0", 1));
    println!("libplctag B3:0 = {:?}", plctag_read(port, "B3:0", 1));
    println!("libplctag N99:0 = {:?}", plctag_read(port, "N99:0", 1));

    let n = 2000;
    let (c0, t0) = (cpu_s(), Instant::now());
    let mut lat = vec![];
    for _ in 0..n {
        let t = Instant::now();
        let r = plc.read(&["N7:0{10}"]).unwrap();
        assert!(r[0].value.is_some(), "{:?}", r[0].error);
        lat.push(t.elapsed().as_secs_f64() * 1000.0);
    }
    lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
    println!("ciplink {n} reads: wall {:.2}s cpu {:.3}s p50 {:.3}ms p99 {:.3}ms", t0.elapsed().as_secs_f64(), cpu_s() - c0, lat[n / 2], lat[n * 99 / 100]);

    let attr = CString::new(format!("protocol=ab-eip&gateway=127.0.0.1:{port}&cpu=slc500&name=N7:0&elem_count=10")).unwrap();
    let (c0, t0) = (cpu_s(), Instant::now());
    let mut lat = vec![];
    unsafe {
        let t = p::plc_tag_create(attr.as_ptr(), 2000);
        for _ in 0..n {
            let s = Instant::now();
            assert_eq!(p::plc_tag_read(t, 2000), 0);
            lat.push(s.elapsed().as_secs_f64() * 1000.0);
        }
        p::plc_tag_destroy(t);
    }
    lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
    println!("libplctag {n} reads: wall {:.2}s cpu {:.3}s p50 {:.3}ms p99 {:.3}ms", t0.elapsed().as_secs_f64(), cpu_s() - c0, lat[n / 2], lat[n * 99 / 100]);

    srv.kill().ok();
    srv.wait().ok();
    let t = Instant::now();
    let r = plc.read(&["N7:0"]);
    println!("ciplink after kill: {:?} in {:?}", r.map(|v| v[0].error.clone()), t.elapsed());
    let t = Instant::now();
    println!("libplctag after kill: {:?} in {:?}", plctag_read(port, "N7:0", 1), t.elapsed());
    let mut srv = start(port, "SLC500");
    std::thread::sleep(Duration::from_secs(3));
    let r = plc.read(&["N7:0"]);
    println!("ciplink after restart: {:?}", r.map(|v| (v[0].value.clone(), v[0].error.clone())));
    println!("libplctag after restart: {:?}", plctag_read(port, "N7:0", 1));
    srv.kill().ok();
}
