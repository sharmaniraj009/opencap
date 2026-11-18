//! Port of tests/unit/test_modbus_plan.py: merge by FC, max_gap, per-FC span, protocol caps.

use opencap_core::config::{DType, Device, Tag};
use opencap_core::driver::{Driver, DriverError, Plan};
use opencap_driver_modbus_tcp::{ModbusBlock, ModbusTcpDriver};
use rand::rngs::StdRng;
use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use serde_json::{json, Value};

fn t(name: &str, address: &str, dtype: DType) -> Tag {
    Tag::new(name, address, dtype)
}

fn u16t(name: &str, address: &str) -> Tag {
    t(name, address, DType::Uint16)
}

fn driver(params: Value) -> ModbusTcpDriver {
    let mut dev = Device::new("SITE/PLC", "SITE", "PLC", "modbus_tcp", "127.0.0.1", 502, vec![]);
    dev.params = params.as_object().unwrap().clone();
    ModbusTcpDriver::new(&dev).unwrap()
}

fn plan(tags: &[Tag], params: Value) -> Plan {
    driver(params).plan_tags(tags)
}

fn mb(p: &Plan, i: usize) -> &ModbusBlock {
    p.blocks[i].as_any().downcast_ref::<ModbusBlock>().unwrap()
}

fn spans(p: &Plan) -> Vec<(u8, u32, u32)> {
    (0..p.blocks.len()).map(|i| (mb(p, i).fc, mb(p, i).start, mb(p, i).count)).collect()
}

#[test]
fn driver_surface() {
    let d = driver(json!({}));
    assert_eq!(d.name(), "modbus_tcp");
    let p = d.plan(&[]);
    assert!(p.blocks.is_empty() && p.invalid.is_empty());
}

#[tokio::test]
async fn read_without_connect_is_connection_lost() {
    let mut d = driver(json!({}));
    assert!(!d.connected());
    let block = ModbusBlock { fc: 3, start: 0, count: 1, items: vec![], tags: vec![] };
    assert!(matches!(d.read(&block).await, Err(DriverError::ConnectionLost(_))));
}

#[test]
fn bad_params_fail_at_construction() {
    let mut dev = Device::new("SITE/PLC", "SITE", "PLC", "modbus_tcp", "127.0.0.1", 502, vec![]);
    dev.params = json!({"max_holding": 0}).as_object().unwrap().clone();
    assert!(ModbusTcpDriver::new(&dev).err().unwrap().contains("max_holding"));
    dev.params = json!({"max_gap": -1}).as_object().unwrap().clone();
    assert!(ModbusTcpDriver::new(&dev).err().unwrap().contains("max_gap"));
}

#[test]
fn single_float_tag_and_describe() {
    let p = plan(&[t("FLOW_1", "400045", DType::Float32)], json!({}));
    assert_eq!(spans(&p), vec![(3, 44, 2)]);
    assert_eq!(mb(&p, 0).tags, vec!["FLOW_1"]);
    assert_eq!(p.blocks[0].describe(), "FC03 44..45 (2 regs)");
    assert_eq!(p.wire_requests(), 1);
    let p = plan(&[t("C1", "000001", DType::Bool), t("C2", "000032", DType::Bool)], json!({"max_coils": 32}));
    assert_eq!(p.blocks[0].describe(), "FC01 0..31 (32 bits)");
}

#[test]
fn contiguous_tags_merge() {
    let p = plan(&[t("A", "400001", DType::Float32), t("B", "400003", DType::Float32), u16t("C", "400005")], json!({}));
    assert_eq!(spans(&p), vec![(3, 0, 5)]);
    assert_eq!(mb(&p, 0).tags, vec!["A", "B", "C"]);
}

#[test]
fn gap_up_to_max_gap_merges_and_beyond_splits() {
    let p = plan(&[t("A", "400001", DType::Float32), u16t("B", "400007"), u16t("C", "400013")], json!({}));
    assert_eq!(spans(&p), vec![(3, 0, 7), (3, 12, 1)]);
}

#[test]
fn max_gap_param() {
    let tags = [u16t("A", "400001"), u16t("B", "400003")];
    assert_eq!(spans(&plan(&tags, json!({"max_gap": 0}))), vec![(3, 0, 1), (3, 2, 1)]);
    assert_eq!(spans(&plan(&tags, json!({"max_gap": 1}))), vec![(3, 0, 3)]);
}

#[test]
fn span_capped_by_device_max_holding() {
    let tags: Vec<Tag> = (0..40).map(|i| u16t(&format!("R{i}"), &format!("4{:05}", i + 1))).collect();
    let p = plan(&tags, json!({"max_holding": 32}));
    assert_eq!(spans(&p), vec![(3, 0, 32), (3, 32, 8)]);
}

#[test]
fn a_32bit_tag_never_straddles_a_block_limit() {
    let tags: Vec<Tag> = (0..20).map(|i| t(&format!("F{i}"), &format!("4{:05}", 2 * i + 1), DType::Float32)).collect();
    assert_eq!(spans(&plan(&tags, json!({"max_holding": 31}))), vec![(3, 0, 30), (3, 30, 10)]);
}

#[test]
fn default_span_is_64() {
    let tags: Vec<Tag> = (0..100).map(|i| u16t(&format!("R{i}"), &format!("4{:05}", i + 1))).collect();
    assert_eq!(spans(&plan(&tags, json!({}))), vec![(3, 0, 64), (3, 64, 36)]);
}

#[test]
fn params_are_capped_at_protocol_limits() {
    let regs: Vec<Tag> = (0..300).map(|i| u16t(&format!("R{i}"), &format!("4{:05}", i + 1))).collect();
    let p = plan(&regs, json!({"max_holding": 1000}));
    assert_eq!(spans(&p).iter().map(|s| s.2).collect::<Vec<_>>(), vec![125, 125, 50]);
    let coils: Vec<Tag> = (0..2500).map(|i| t(&format!("C{i}"), &format!("0{:05}", i + 1), DType::Bool)).collect();
    let p = plan(&coils, json!({"max_coils": 5000}));
    assert_eq!(spans(&p).iter().map(|s| s.2).collect::<Vec<_>>(), vec![2000, 500]);
}

#[test]
fn input_registers_use_max_input() {
    let tags: Vec<Tag> = (0..10).map(|i| u16t(&format!("I{i}"), &format!("3{:05}", i + 1))).collect();
    assert_eq!(spans(&plan(&tags, json!({"max_input": 4, "max_holding": 64}))), vec![(4, 0, 4), (4, 4, 4), (4, 8, 2)]);
}

#[test]
fn function_codes_never_merge_and_blocks_are_sorted() {
    let tags = [u16t("H", "400001"), u16t("I", "300001"), t("C", "000001", DType::Bool), t("D", "100001", DType::Bool)];
    assert_eq!(spans(&plan(&tags, json!({}))), vec![(1, 0, 1), (2, 0, 1), (3, 0, 1), (4, 0, 1)]);
}

#[test]
fn same_address_tags_are_read_once_and_served_to_all_names() {
    let tags = [t("PUMP_1_CURRENT_Y", "400133", DType::Float32), t("PUMP_1_CURRENT_B", "400133", DType::Float32)];
    let p = plan(&tags, json!({}));
    assert_eq!(spans(&p), vec![(3, 132, 2)]);
    let b = mb(&p, 0);
    assert_eq!(b.tags, vec!["PUMP_1_CURRENT_B", "PUMP_1_CURRENT_Y"]);
    assert_eq!(b.items.len(), 1);
    assert_eq!(b.items[0].names, vec!["PUMP_1_CURRENT_B", "PUMP_1_CURRENT_Y"]);
    assert_eq!(b.items[0].offset, 0);
}

#[test]
fn five_and_six_digit_forms_of_one_address_share_an_item() {
    let p = plan(&[t("A", "40002", DType::Float32), t("B", "400002", DType::Float32)], json!({}));
    assert_eq!(mb(&p, 0).items.len(), 1);
}

#[test]
fn overlapping_tags_of_different_types() {
    let p = plan(&[t("F", "400001", DType::Float32), u16t("W", "400002")], json!({}));
    assert_eq!(spans(&p), vec![(3, 0, 2)]);
    let offsets: Vec<(String, u32)> =
        mb(&p, 0).items.iter().flat_map(|it| it.names.iter().map(move |n| (n.clone(), it.offset))).collect();
    assert_eq!(offsets, vec![("F".into(), 0), ("W".into(), 1)]);
}

#[test]
fn bits_and_word_of_one_register() {
    let tags = [u16t("W", "400010"), t("B0", "400010.0", DType::Bool), t("B3", "400010.3", DType::Bool)];
    let p = plan(&tags, json!({}));
    assert_eq!(spans(&p), vec![(3, 9, 1)]);
    assert_eq!(mb(&p, 0).tags, vec!["B0", "B3", "W"]);
    assert_eq!(mb(&p, 0).items.len(), 3);
}

#[test]
fn coil_gap_is_max_gap_times_16_bits() {
    let near = plan(&[t("C1", "000001", DType::Bool), t("C2", "000066", DType::Bool)], json!({"max_coils": 2000}));
    assert_eq!(spans(&near), vec![(1, 0, 66)]);
    let far = plan(&[t("C1", "000001", DType::Bool), t("C2", "000067", DType::Bool)], json!({"max_coils": 2000}));
    assert_eq!(spans(&far), vec![(1, 0, 1), (1, 66, 1)]);
    let tight = plan(&[t("C1", "000001", DType::Bool), t("C2", "000003", DType::Bool)], json!({"max_gap_bits": 0}));
    assert_eq!(spans(&tight), vec![(1, 0, 1), (1, 2, 1)]);
}

#[test]
fn item_offsets_are_relative_to_block_start() {
    let p = plan(&[t("A", "400045", DType::Float32), u16t("B", "400048")], json!({}));
    let b = mb(&p, 0);
    assert_eq!((b.start, b.count), (44, 4));
    assert_eq!(b.items.iter().map(|i| i.offset).collect::<Vec<_>>(), vec![0, 3]);
}

#[test]
fn unparseable_address_goes_to_invalid_with_reason() {
    let p = plan(&[u16t("GOOD", "400001"), u16t("BAD", "500001"), t("BADTYPE", "400002", DType::Bool)], json!({}));
    assert_eq!(spans(&p), vec![(3, 0, 1)]);
    assert_eq!(p.invalid.keys().collect::<Vec<_>>(), vec!["BAD", "BADTYPE"]);
    assert!(p.invalid["BAD"].contains("500001"));
    assert!(p.invalid["BADTYPE"].contains("bit"));
}

#[test]
fn tag_larger_than_device_block_goes_to_invalid() {
    let p = plan(&[u16t("ARR", "400001[40]"), u16t("OK", "400100")], json!({"max_holding": 32}));
    assert_eq!(spans(&p), vec![(3, 99, 1)]);
    assert_eq!(
        p.invalid["ARR"],
        "modbus address '400001[40]' (uint16): needs 40 registers in one request, more than the limit 32 \
         (max_holding=32, protocol max 125)"
    );
}

#[test]
fn disabled_tags_are_not_planned() {
    let off = Tag { enabled: false, ..u16t("OFF", "400002") };
    let offbad = Tag { enabled: false, ..u16t("OFFBAD", "9") };
    let p = plan(&[u16t("ON", "400001"), off, offbad], json!({}));
    assert_eq!(mb(&p, 0).tags, vec!["ON"]);
    assert!(p.invalid.is_empty());
}

fn random_tags(rng: &mut StdRng, n: usize) -> Vec<Tag> {
    (0..n)
        .map(|i| {
            let r = rng.random_range(1..=400);
            let name = format!("T{i}");
            match rng.random_range(0..7) {
                0 => t(&name, &format!("4{r:05}"), DType::Uint16),
                1 => t(&name, &format!("4{r:05}"), DType::Float32),
                2 => t(&name, &format!("4{r:05}.{}", rng.random_range(0..=15)), DType::Bool),
                3 => t(&name, &format!("0{r:05}"), DType::Bool),
                4 => t(&name, &format!("1{r:04}"), DType::Bool),
                5 => t(&name, &format!("3{r:05}"), DType::Int32),
                _ => t(&name, &format!("9{r:05}"), DType::Uint16),
            }
        })
        .collect()
}

type Snap = (Vec<(String, Vec<String>, Vec<(Vec<String>, u32)>)>, Vec<(String, String)>);

fn snapshot(p: &Plan) -> Snap {
    (
        (0..p.blocks.len())
            .map(|i| {
                let b = mb(p, i);
                (
                    p.blocks[i].describe(),
                    b.tags.clone(),
                    b.items.iter().map(|it| (it.names.clone(), it.offset)).collect(),
                )
            })
            .collect(),
        p.invalid.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
    )
}

#[test]
fn plan_is_deterministic_regardless_of_tag_order() {
    let mut rng = StdRng::seed_from_u64(1234);
    let mut tags = random_tags(&mut rng, 300);
    let params = json!({"max_holding": 32, "max_coils": 32});
    let first = snapshot(&plan(&tags, params.clone()));
    for _ in 0..5 {
        tags.shuffle(&mut rng);
        assert_eq!(snapshot(&plan(&tags, params.clone())), first);
    }
}

#[test]
fn every_tag_in_exactly_one_block_or_invalid_and_limits_hold() {
    for seed in 0..5 {
        let mut rng = StdRng::seed_from_u64(seed);
        let tags = random_tags(&mut rng, 250);
        let p = plan(&tags, json!({"max_holding": 32, "max_input": 16, "max_coils": 32, "max_discrete": 8}));
        let limit = |fc: u8| match fc {
            1 => 32,
            2 => 8,
            3 => 32,
            _ => 16,
        };
        let mut seen: Vec<String> = Vec::new();
        for i in 0..p.blocks.len() {
            let b = mb(&p, i);
            assert!(1 <= b.count && b.count <= limit(b.fc));
            let mut sorted = b.tags.clone();
            sorted.sort();
            assert_eq!(b.tags, sorted);
            let mut names: Vec<String> = b.items.iter().flat_map(|it| it.names.clone()).collect();
            names.sort();
            assert_eq!(names, b.tags);
            for it in &b.items {
                assert!(it.offset + it.addr.count <= b.count);
                assert_eq!(it.addr.address, b.start + it.offset);
            }
            seen.extend(b.tags.clone());
        }
        let unique: std::collections::BTreeSet<_> = seen.iter().cloned().collect();
        assert_eq!(unique.len(), seen.len());
        let all: std::collections::BTreeSet<String> = tags.iter().map(|t| t.name.clone()).collect();
        let covered: std::collections::BTreeSet<String> =
            unique.union(&p.invalid.keys().cloned().collect()).cloned().collect();
        assert_eq!(covered, all);
        let starts: Vec<(u8, u32)> = (0..p.blocks.len()).map(|i| (mb(&p, i).fc, mb(&p, i).start)).collect();
        let mut sorted = starts.clone();
        sorted.sort();
        assert_eq!(starts, sorted);
    }
}
