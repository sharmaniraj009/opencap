//! Packed reads (#227, D-125): `read_many` puts scattered blocks into multi-item read var requests
//! up to the negotiated PDU, fails only the block whose item the CPU refuses, and sends nothing but reads.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan};
use opencap_core::value::Value;
use opencap_driver_s7::S7Driver;

struct Plc {
    max_pdu: usize,
    mute: Mutex<bool>,
    /// items per read var request
    requests: Mutex<Vec<usize>>,
    /// (pdu type, function) of everything received
    seen: Mutex<Vec<(u8, u8)>>,
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

/// DB 99 does not exist (return code 0x0A); every other byte at offset `o` of any DB reads `o & 0xFF`.
fn item_reply(db: u16, start: usize, n: usize) -> (u8, Vec<u8>) {
    if db == 99 {
        return (0x0A, vec![]);
    }
    (0xFF, (0..n).map(|i| ((start + i) & 0xFF) as u8).collect())
}

impl Plc {
    fn reply(&self, pdu: &[u8]) -> Option<Vec<u8>> {
        let seq = u16::from_be_bytes([pdu[4], pdu[5]]);
        self.seen.lock().unwrap().push((pdu[1], pdu[10]));
        if *self.mute.lock().unwrap() && pdu[10] == 0x04 {
            return None;
        }
        match pdu[10] {
            0xF0 => {
                let want = usize::from(u16::from_be_bytes([pdu[16], pdu[17]]));
                let mut params = vec![0xF0, 0, 0, 1, 0, 1];
                params.extend_from_slice(&(want.min(self.max_pdu) as u16).to_be_bytes());
                Some(ack(seq, &params, &[], 0))
            }
            0x04 => {
                let count = usize::from(pdu[11]);
                self.requests.lock().unwrap().push(count);
                let mut data = Vec::new();
                for k in 0..count {
                    let it = &pdu[12 + 12 * k..24 + 12 * k];
                    let width = if it[3] == 0x02 { 1 } else { 2 };
                    let n = usize::from(u16::from_be_bytes([it[4], it[5]])) * width;
                    let db = u16::from_be_bytes([it[6], it[7]]);
                    let start = u32::from_be_bytes([0, it[9], it[10], it[11]]) as usize / 8;
                    let (rc, bytes) = item_reply(db, start, n);
                    data.push(rc);
                    data.push(0x04);
                    data.extend_from_slice(&((bytes.len() * 8) as u16).to_be_bytes());
                    data.extend_from_slice(&bytes);
                    if bytes.len() % 2 == 1 && k + 1 < count {
                        data.push(0);
                    }
                }
                Some(ack(seq, &[4, count as u8], &data, 0))
            }
            _ => Some(ack(seq, &[], &[], 0x8104)),
        }
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
                if s.write_all(&[3u8, 0, 0, 11, 6, 0xD0, 0, 1, 0, 9, 0]).await.is_err() {
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
                        return;
                    }
                    let Some(reply) = plc.reply(&body[3..]) else { continue };
                    if s.write_all(&frame(&reply)).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    port
}

fn plc(max_pdu: usize) -> Arc<Plc> {
    Arc::new(Plc { max_pdu, mute: Mutex::new(false), requests: Mutex::new(vec![]), seen: Mutex::new(vec![]) })
}

/// Eight scattered 2-byte tags in DBs 1..8, one in the missing DB 99.
fn device(port: u16, extra: Vec<Tag>) -> Device {
    let mut tags: Vec<Tag> =
        (1..=8).map(|n| Tag::new(&format!("T{n}"), &format!("DB{n},W{}", n * 10), DType::Uint16)).collect();
    tags.extend(extra);
    let mut d = Device::new("SITE/PLC", "SITE", "PLC", "s7", "127.0.0.1", i64::from(port), tags);
    d.timeout_ms = 500;
    d
}

fn plan_of(drv: &S7Driver, dev: &Device) -> Plan {
    let tags: Vec<&Tag> = dev.tags.iter().collect();
    drv.plan(&tags)
}

fn expect_value(n: i64) -> Value {
    // two bytes at offset o read o, o+1: big-endian uint16 = o * 256 + o + 1
    Value::Int(n * 256 + n + 1)
}

#[tokio::test]
async fn scattered_blocks_share_one_request_and_decode_as_before() {
    let p = plc(480);
    let dev = device(serve(p.clone()).await, vec![]);
    let mut drv = S7Driver::new(&dev).unwrap();
    assert!(drv.supports_batch());
    drv.connect().await.unwrap();
    let plan = plan_of(&drv, &dev);
    assert_eq!(plan.blocks.len(), 8, "one block per DB");
    let refs: Vec<&dyn Block> = plan.blocks.iter().map(|b| b.as_ref()).collect();
    let results = drv.read_many(&refs).await;
    assert_eq!(*p.requests.lock().unwrap(), vec![8], "eight blocks, one request of eight items");
    for (i, r) in results.into_iter().enumerate() {
        let got = r.unwrap();
        let name = &plan.blocks[i].tags()[0];
        let n: i64 = name[1..].parse::<i64>().unwrap() * 10;
        assert_eq!(got[name].value, Some(expect_value(n)), "{name}");
    }
    // only setup communication and read var reached the CPU
    assert!(p.seen.lock().unwrap().iter().all(|(t, f)| *t == 1 && (*f == 0xF0 || *f == 0x04)));
}

#[tokio::test]
async fn a_small_pdu_splits_the_blocks_over_several_requests() {
    // five 100-byte items: 104 reply bytes each, a 240-byte PDU holds two per request
    let p = plc(240);
    let extra: Vec<Tag> =
        (11..16).map(|n| Tag::new(&format!("W{n}"), &format!("DB{n},W0[50]"), DType::Uint16)).collect();
    let mut dev = device(serve(p.clone()).await, vec![]);
    dev.tags = extra;
    let mut drv = S7Driver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let plan = plan_of(&drv, &dev);
    let refs: Vec<&dyn Block> = plan.blocks.iter().map(|b| b.as_ref()).collect();
    let results = drv.read_many(&refs).await;
    assert!(results.iter().all(Result::is_ok));
    let requests = p.requests.lock().unwrap().clone();
    assert_eq!(requests, vec![2, 2, 1]);
}

#[tokio::test]
async fn a_refused_item_fails_only_its_own_block() {
    let p = plc(480);
    let dev = device(serve(p.clone()).await, vec![Tag::new("GONE", "DB99,W0", DType::Uint16)]);
    let mut drv = S7Driver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    let plan = plan_of(&drv, &dev);
    assert_eq!(plan.blocks.len(), 9);
    let refs: Vec<&dyn Block> = plan.blocks.iter().map(|b| b.as_ref()).collect();
    let results = drv.read_many(&refs).await;
    let mut bad = 0;
    for (b, r) in plan.blocks.iter().zip(results) {
        if b.tags().contains(&"GONE".to_string()) {
            assert!(matches!(&r, Err(DriverError::BlockConfig(m)) if m.contains("0x0A")), "{r:?}");
            bad += 1;
        } else {
            assert!(r.is_ok(), "{:?}: {r:?}", b.tags());
        }
    }
    assert_eq!(bad, 1);
    assert!(drv.connected(), "a refused item does not cost the connection");
}

#[tokio::test]
async fn a_mute_cpu_fails_every_block_as_a_comm_error_and_closes_the_connection() {
    let p = plc(480);
    let dev = device(serve(p.clone()).await, vec![]);
    let mut drv = S7Driver::new(&dev).unwrap();
    drv.connect().await.unwrap();
    *p.mute.lock().unwrap() = true;
    let plan = plan_of(&drv, &dev);
    let refs: Vec<&dyn Block> = plan.blocks.iter().map(|b| b.as_ref()).collect();
    let started = std::time::Instant::now();
    let results = drv.read_many(&refs).await;
    assert!(results.iter().all(|r| matches!(r, Err(DriverError::Comm(_)))), "{results:?}");
    assert!(started.elapsed() < Duration::from_secs(5));
    assert!(!drv.connected());
}

#[tokio::test]
async fn not_connected_is_connection_lost_for_every_block() {
    let dev = device(1, vec![]);
    let mut drv = S7Driver::new(&dev).unwrap();
    let plan = plan_of(&drv, &dev);
    let refs: Vec<&dyn Block> = plan.blocks.iter().map(|b| b.as_ref()).collect();
    let results = drv.read_many(&refs).await;
    assert!(results.iter().all(|r| matches!(r, Err(DriverError::ConnectionLost(_)))));
}

#[tokio::test]
async fn the_negotiated_pdu_reaches_blocks_planned_by_another_instance() {
    // the registry plans with its own driver; the worker's driver connects. Both see one PDU.
    let p = plc(240);
    let dev = device(serve(p.clone()).await, vec![Tag::new("BIG", "DB300,B0[400]", DType::Uint16)]);
    let planner = S7Driver::new(&dev).unwrap();
    let plan = plan_of(&planner, &dev);
    let big = plan.blocks.iter().find(|b| b.tags().contains(&"BIG".to_string())).unwrap();
    let before = big.wire_requests();
    let mut worker_driver = S7Driver::new(&dev).unwrap();
    worker_driver.connect().await.unwrap();
    let after = big.wire_requests();
    assert!(
        after >= before && after > 1,
        "a 400-byte block over a 240-byte PDU needs several requests: {before} -> {after}"
    );
}
