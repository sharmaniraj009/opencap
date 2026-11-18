//! Read-only guard (ADR-0004 section 5, ADR-0008, CONTRIBUTING rule 1). Mirrors
//! tests/integration/test_modbus_io.py::test_driver_never_calls_a_modbus_write.

use std::path::Path;

use opencap_core::config::{DType, Device, Tag};
use opencap_drivers::modbus_tcp::{ModbusBlock, ModbusTcpDriver};
use serde_json::json;

#[test]
fn driver_sources_never_name_a_write_api() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("src").join("modbus_tcp");
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
    assert!(files >= 4, "scanned the driver sources");
}

#[test]
fn a_planned_block_only_uses_read_function_codes() {
    let mut dev = Device::new("SITE/PLC", "SITE", "PLC", "modbus_tcp", "127.0.0.1", 502, vec![]);
    dev.params = json!({}).as_object().unwrap().clone();
    let driver = ModbusTcpDriver::new(&dev).unwrap();
    let tags: Vec<Tag> = ["000001", "100001", "300001", "400001", "400100"]
        .iter()
        .enumerate()
        .map(|(i, a)| {
            Tag::new(
                &format!("T{i}"),
                a,
                if a.starts_with('0') || a.starts_with('1') { DType::Bool } else { DType::Uint16 },
            )
        })
        .collect();
    let plan = driver.plan_tags(&tags);
    assert!(!plan.blocks.is_empty());
    for b in &plan.blocks {
        let fc = b.as_any().downcast_ref::<ModbusBlock>().unwrap().fc;
        assert!((1..=4).contains(&fc) || fc == 43, "planned FC {fc}");
    }
}
