//! DF1 framing, receiver, typed-read requests and reply values against `tests/conformance/ab_df1` (#68, #257).

use serde_json::{json, Map, Value as Json};

use opencap_conformance::{encode_decode_error, encode_value, run_area};
use opencap_core::config::Tag;
use opencap_core::driver::{Block, Plan};
use opencap_driver_ab_df1::frame::{encode_frame, hex, Check, FrameDecoder};
use opencap_driver_ab_df1::params::Df1Params;
use opencap_driver_ab_df1::request::{as_sequence, pccc_values, typed_read_function};
use opencap_driver_ab_pccc::{decode_block, plan_pccc, PcccBlock, PcccParams};

fn unhex(s: &str) -> Result<Vec<u8>, String> {
    s.split_whitespace().map(|b| u8::from_str_radix(b, 16).map_err(|e| format!("{b}: {e}"))).collect()
}

fn spaced(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect::<Vec<_>>().join(" ")
}

#[test]
fn ab_df1_frame() {
    run_area("ab_df1/frame", |input| {
        let check = match Check::parse(input["check"].as_str().ok_or("check")?) {
            Ok(c) => c,
            Err(e) => return Ok(json!({"error": e})),
        };
        let app = unhex(input["app"].as_str().ok_or("app")?)?;
        Ok(json!({"frame": spaced(&encode_frame(&app, check))}))
    });
}

#[test]
fn ab_df1_receive() {
    run_area("ab_df1/receive", |input| {
        let check = match Check::parse(input["check"].as_str().ok_or("check")?) {
            Ok(c) => c,
            Err(e) => return Ok(json!({"error": e})),
        };
        let mut dec = FrameDecoder::new(check);
        let mut events = Vec::new();
        for chunk in input["chunks"].as_array().ok_or("chunks")? {
            for ev in dec.feed(&unhex(chunk.as_str().ok_or("chunk")?)?) {
                events.push(json!({"kind": ev.kind.as_str(), "data": spaced(&ev.data), "reason": ev.reason}));
            }
        }
        Ok(json!({"events": events, "noise": dec.noise}))
    });
}

fn plan_of(input: &Json) -> Result<Result<Plan, String>, String> {
    let tags: Vec<Tag> = serde_json::from_value(input["tags"].clone()).map_err(|e| format!("tags: {e}"))?;
    let params: Map<String, Json> = input.get("params").and_then(Json::as_object).cloned().unwrap_or_default();
    Ok(Df1Params::from_map(&params).and_then(|_| PcccParams::from_map(&params, "ab_df1")).map(|p| plan_pccc(&p, &tags)))
}

fn block(plan: &Plan, i: usize) -> &PcccBlock {
    plan.blocks[i].as_any().downcast_ref::<PcccBlock>().expect("a PCCC block")
}

#[test]
fn ab_df1_request() {
    run_area("ab_df1/request", |input| {
        let plan = match plan_of(input)? {
            Ok(p) => p,
            Err(e) => return Ok(json!({"error": e})),
        };
        let params: Map<String, Json> = input.get("params").and_then(Json::as_object).cloned().unwrap_or_default();
        let fnc = Df1Params::from_map(&params)?.read_fnc;
        let requests: Vec<Json> = (0..plan.blocks.len())
            .map(|i| {
                let b = block(&plan, i);
                let f = typed_read_function(b, fnc).map_err(|e| e.to_string())?;
                Ok(json!({"address": b.address, "tags": b.tags(), "fnc": spaced(&f)}))
            })
            .collect::<Result<_, String>>()?;
        Ok(json!({"requests": requests, "invalid": plan.invalid}))
    });
}

#[test]
fn ab_df1_values() {
    run_area("ab_df1/values", |input| {
        let plan = plan_of(input)?.map_err(|e| format!("params refused: {e}"))?;
        if plan.blocks.len() != 1 || !plan.invalid.is_empty() {
            return Err(format!("values vector must plan to one block, got {}", plan.blocks.len()));
        }
        let b = block(&plan, 0);
        let data = unhex(input["reply"].as_str().ok_or("reply")?)?;
        let raw = pccc_values(b, &data);
        let seq = as_sequence(&raw);
        let values: Map<String, Json> = decode_block(b, seq.as_deref())
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
        let _ = hex;
        Ok(json!({"raw": encode_value(raw.as_ref()), "values": values}))
    });
}
