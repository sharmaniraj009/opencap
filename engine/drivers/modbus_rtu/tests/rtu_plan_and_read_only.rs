//! RTU plans exactly like Modbus TCP (same planner, so the Modbus conformance vectors cover
//! both transports), and stays read-only (ADR-0004 section 5, ADR-0008).

use std::path::Path;
use std::sync::Arc;

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Block, Driver, DriverError, Plan};
use opencap_driver_modbus_rtu::ModbusRtuDriver;
use opencap_driver_modbus_tcp::{ModbusBlock, ModbusTcpDriver};
use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};
use serde_json::{json, Value as Json};

fn device(driver: &str, params: Json) -> Device {
    let mut d = Device::new("SITE/PLC", "SITE", "PLC", driver, "192.0.2.10", 4001, vec![]);
    d.params = params.as_object().unwrap().clone();
    d
}

fn random_tags(rng: &mut StdRng, n: usize) -> Vec<Tag> {
    (0..n)
        .map(|i| {
            let r = rng.random_range(1..=400);
            let name = format!("T{i}");
            let (address, dtype) = match rng.random_range(0..8) {
                0 => (format!("4{r:05}"), DType::Uint16),
                1 => (format!("4{r:05}"), DType::Float32),
                2 => (format!("4{r:05}.{}", rng.random_range(0..=15)), DType::Bool),
                3 => (format!("0{r:05}"), DType::Bool),
                4 => (format!("1{r:04}"), DType::Bool),
                5 => (format!("3{r:05}"), DType::Int32),
                6 => (format!("4{r:05}[3]"), DType::Int16),
                _ => (format!("9{r:05}"), DType::Uint16),
            };
            let mut t = Tag::new(&name, &address, dtype);
            t.enabled = rng.random_range(0..10) != 0;
            t
        })
        .collect()
}

type Snap = (Vec<(String, Vec<String>)>, Vec<(String, String)>);

fn snapshot(p: &Plan) -> Snap {
    (
        p.blocks.iter().map(|b| (b.describe(), b.tags().to_vec())).collect(),
        p.invalid.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
    )
}

#[test]
fn rtu_plans_exactly_like_modbus_tcp() {
    let mut rng = StdRng::seed_from_u64(47);
    for params in [
        json!({}),
        json!({"max_holding": 16, "max_coils": 32, "max_gap": 0, "zero_based": false}),
        json!({"unit_id": 7, "max_gap": 20, "modicon_bit_order": true, "transport": "serial", "baudrate": 9600}),
    ] {
        let tcp = ModbusTcpDriver::new(&device("modbus_tcp", params.clone())).unwrap();
        let rtu = ModbusRtuDriver::new(&device("modbus_rtu", params.clone())).unwrap();
        for _ in 0..20 {
            let tags = random_tags(&mut rng, 120);
            let refs: Vec<&Tag> = tags.iter().collect();
            let (a, b) = (Driver::plan(&tcp, &refs), Driver::plan(&rtu, &refs));
            assert!(!a.blocks.is_empty());
            assert_eq!(snapshot(&a), snapshot(&b), "{params}");
        }
    }
}

#[test]
fn driver_sources_never_name_a_write_api() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = 0;
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_some_and(|e| e == "rs") {
            files += 1;
            let text = std::fs::read_to_string(&path).unwrap();
            for needle in ["Writer", "write_", "masked_write", "Client::call", "Request::Write", "ReadWrite"] {
                assert!(!text.contains(needle), "{} mentions {needle:?}", path.display());
            }
        }
    }
    assert!(files >= 1, "scanned the driver sources");
}

#[tokio::test]
async fn a_block_with_another_function_code_is_never_sent() {
    let mut rtu = ModbusRtuDriver::new(&device("modbus_rtu", json!({}))).unwrap();
    let tags = [Tag::new("HR", "400001", DType::Uint16)];
    let plan = rtu.plan_tags(&tags);
    let mut block = plan.blocks[0].as_any().downcast_ref::<ModbusBlock>().unwrap().clone();
    assert!((1..=4).contains(&block.fc));
    for fc in [5u8, 6, 15, 16, 22, 23] {
        block.fc = fc;
        let b: Arc<dyn Block> = Arc::new(block.clone());
        let err = rtu.read(b.as_ref()).await.unwrap_err();
        assert!(
            matches!(&err, DriverError::BlockConfig(m) if m.contains("unsupported Modbus function code")),
            "{err:?}"
        );
    }
}
