//! PCCC address parsing, block planning and decoding against `tests/conformance/ab_pccc` (D-008, D-027).

use serde_json::{json, Map, Value as Json};

use opencap_conformance::{decode_value, encode_decode_error, encode_value, run_area};
use opencap_core::config::Tag;
use opencap_core::driver::Plan;
use opencap_driver_ab_pccc::{decode_block, plan_pccc, PcccBlock, PcccParams};

fn plan_of(input: &Json) -> Result<Result<Plan, String>, String> {
    let tags: Vec<Tag> = serde_json::from_value(input["tags"].clone()).map_err(|e| format!("tags: {e}"))?;
    let params: Map<String, Json> = input.get("params").and_then(Json::as_object).cloned().unwrap_or_default();
    Ok(PcccParams::from_map(&params, "ab_pccc").map(|p| plan_pccc(&p, &tags)))
}

pub fn block_json(b: &PcccBlock) -> Json {
    use opencap_core::driver::Block;
    json!({
        "address": b.address, "file_type": b.file_type, "file_number": b.file_number, "slot": b.slot,
        "field": b.field, "start": b.start, "count": b.count, "elem_size": b.elem_size,
        "tags": b.tags(),
        "items": b.items.iter().map(|i| json!({"tag": i.tag, "offset": i.offset, "bit": i.bit})).collect::<Vec<_>>(),
    })
}

fn pccc_block(plan: &Plan, i: usize) -> &PcccBlock {
    plan.blocks[i].as_any().downcast_ref::<PcccBlock>().expect("a PCCC block")
}

#[test]
fn ab_pccc_plan() {
    run_area("ab_pccc/plan", |input| {
        let plan = match plan_of(input)? {
            Ok(p) => p,
            Err(reason) => return Ok(json!({"error": reason})),
        };
        let blocks: Vec<Json> = (0..plan.blocks.len()).map(|i| block_json(pccc_block(&plan, i))).collect();
        Ok(json!({"blocks": blocks, "invalid": plan.invalid}))
    });
}

#[test]
fn ab_pccc_decode() {
    run_area("ab_pccc/decode", |input| {
        let plan = plan_of(input)?.map_err(|e| format!("params refused: {e}"))?;
        if plan.blocks.len() != 1 || !plan.invalid.is_empty() {
            return Err(format!("decode vector must plan to one block, got {}", plan.blocks.len()));
        }
        let seq: Option<Vec<Option<opencap_core::value::Value>>> = match input["value"]["t"].as_str() {
            Some("none") => None,
            Some("list") => {
                Some(input["value"]["v"].as_array().ok_or("list")?.iter().map(decode_value).collect::<Result<_, _>>()?)
            }
            _ => Some(vec![decode_value(&input["value"])?]),
        };
        let decoded = decode_block(pccc_block(&plan, 0), seq.as_deref());
        let values: Map<String, Json> = decoded
            .iter()
            .map(|(k, r)| {
                (
                    k.clone(),
                    match r {
                        Ok(v) => encode_value(Some(v)),
                        Err(e) => encode_decode_error(&e.0),
                    },
                )
            })
            .collect();
        Ok(json!({"values": values}))
    });
}
