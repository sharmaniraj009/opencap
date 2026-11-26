//! The Rust engine against the shared conformance vectors (ADR-0014, #40): Modbus planning and
//! decoding, tag scaling and the SubDevice JSON payload. Each adapter turns one vector's
//! `input` into a result shaped like `expected`; `run_area` compares and reports every
//! failing vector with its key and description. A driver port adds its areas here (or in its
//! own test file) the same way.

use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value as Json};

use opencap_conformance::{decode_value, encode_decode_error, encode_value, run_area};
use opencap_core::config::{Device, PayloadCfg, Scale, Tag};
use opencap_core::driver::Plan;
use opencap_core::store::Snapshot;
use opencap_core::value::{Quality, TagValue};
use opencap_core::worker::scale_value;
use opencap_core::FIRM_VERSION;
use opencap_driver_modbus_tcp::{decode_block, plan_tags, ModbusBlock, ModbusParams, Reply};
use opencap_north::payload::{build_payload, encode};

fn tags_of(input: &Json) -> Result<Vec<Tag>, String> {
    serde_json::from_value(input["tags"].clone()).map_err(|e| format!("tags: {e}"))
}

fn params_of(input: &Json) -> Map<String, Json> {
    input.get("params").and_then(Json::as_object).cloned().unwrap_or_default()
}

fn modbus_plan_of(input: &Json) -> Result<Result<(ModbusParams, Plan), String>, String> {
    let tags = tags_of(input)?;
    Ok(ModbusParams::from_map(&params_of(input)).map(|p| {
        let plan = plan_tags(&p, &tags);
        (p, plan)
    }))
}

fn block_of(plan: &Plan, i: usize) -> &ModbusBlock {
    plan.blocks[i].as_any().downcast_ref::<ModbusBlock>().expect("a Modbus block")
}

#[test]
fn modbus_plan() {
    run_area("modbus/plan", |input| {
        let (_, plan) = match modbus_plan_of(input)? {
            Ok(x) => x,
            Err(reason) => return Ok(json!({"error": reason})),
        };
        let blocks: Vec<Json> = (0..plan.blocks.len())
            .map(|i| {
                let b = block_of(&plan, i);
                json!({"fc": b.fc, "start": b.start, "count": b.count, "tags": b.tags})
            })
            .collect();
        Ok(json!({"blocks": blocks, "invalid": plan.invalid}))
    });
}

#[test]
fn modbus_decode() {
    run_area("modbus/decode", |input| {
        let (params, plan) = modbus_plan_of(input)?.map_err(|e| format!("params refused: {e}"))?;
        if plan.blocks.len() != 1 || !plan.invalid.is_empty() {
            return Err(format!("decode vector must plan to one block, got {}", plan.blocks.len()));
        }
        let block = block_of(&plan, 0);
        let reply = &input["reply"];
        let (registers, bits);
        let data = if block.is_bits() {
            bits = reply["bits"].as_array().ok_or("no bits")?.iter().map(|b| b.as_bool().unwrap()).collect::<Vec<_>>();
            Reply::Bits(&bits)
        } else {
            registers = reply["registers"]
                .as_array()
                .ok_or("no registers")?
                .iter()
                .map(|r| r.as_u64().unwrap() as u16)
                .collect::<Vec<_>>();
            Reply::Registers(&registers)
        };
        let decoded = decode_block(block, data, &params);
        let values: Map<String, Json> = decoded
            .iter()
            .map(|(name, r)| {
                let enc = match r {
                    Ok(v) => encode_value(Some(v)),
                    Err(e) => encode_decode_error(&e.0),
                };
                (name.clone(), enc)
            })
            .collect();
        Ok(json!({"values": values}))
    });
}

#[test]
fn scaling() {
    run_area("scaling", |input| {
        let scale: Scale = serde_json::from_value(input["scale"].clone()).map_err(|e| format!("scale: {e}"))?;
        // A missing value is not scaled (the worker only scales decoded values): none passes through.
        let Some(raw) = decode_value(&input["raw"])? else {
            return Ok(json!({"value": encode_value(None)}));
        };
        Ok(match scale_value(&scale, &raw) {
            Ok(v) => json!({"value": encode_value(Some(&v))}),
            Err(reason) => json!({"error": reason}),
        })
    });
}

fn quality(s: &str) -> Result<Quality, String> {
    Ok(match s {
        "good" => Quality::Good,
        "bad_comm" => Quality::BadComm,
        "bad_config" => Quality::BadConfig,
        "bad_decode" => Quality::BadDecode,
        "stale" => Quality::Stale,
        other => return Err(format!("quality {other}")),
    })
}

fn ts(v: &Json) -> Result<Option<DateTime<Utc>>, String> {
    match v.as_str() {
        None => Ok(None),
        Some(s) => {
            DateTime::parse_from_rfc3339(s).map(|d| Some(d.with_timezone(&Utc))).map_err(|e| format!("{s}: {e}"))
        }
    }
}

#[test]
fn payload() {
    run_area("payload", |input| {
        let device: Device = serde_json::from_value(input["device"].clone()).map_err(|e| format!("device: {e}"))?;
        let cfg: PayloadCfg = serde_json::from_value(input["cfg"].clone()).map_err(|e| format!("cfg: {e}"))?;
        let mut pairs = Vec::new();
        for (name, tv) in input["values"].as_object().ok_or("no values")? {
            let value = decode_value(&tv["value"])?;
            pairs.push((
                name.clone(),
                TagValue::new(value, quality(tv["quality"].as_str().unwrap_or(""))?, ts(&tv["source_ts"])?),
            ));
        }
        let snapshot = Snapshot::from_pairs(&device.id, pairs);
        let built = build_payload(
            &device,
            &snapshot,
            &cfg,
            input["is_secure"].as_bool().ok_or("no is_secure")?,
            ts(&input["now"])?,
        )?;
        let text = String::from_utf8(encode(&built)).map_err(|e| e.to_string())?;
        // Same input, same bytes; FirmVersion follows each engine's own release number.
        let py_version = input["firm_version"].as_str().ok_or("no firm_version")?;
        let text =
            text.replace(&format!("\"FirmVersion\":\"{FIRM_VERSION}\""), &format!("\"FirmVersion\":\"{py_version}\""));
        Ok(json!({"bytes": text}))
    });
}
