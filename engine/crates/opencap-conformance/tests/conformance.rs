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
use opencap_north::payload_iotgw::build_iotgw;

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

/// IoT Gateway JSON (`publish.format: iotgw_json`, #36, #170): the same device/values/cfg input as
/// the SubDevice payload, without `firm_version` and `is_secure`.
#[test]
fn payload_iotgw() {
    let n = run_area("payload/iotgw", |input| {
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
        let now = ts(&input["now"])?.ok_or("no now")?;
        let bytes = build_iotgw(&device, &snapshot, &cfg, now);
        Ok(json!({"bytes": String::from_utf8(bytes).map_err(|e| e.to_string())?}))
    });
    assert!(n >= 10, "{n}");
}

// --- Allen-Bradley Logix / Micro800 (#43) ---------------------------------------------------------

mod ab_logix {
    use super::*;
    use opencap_core::value::Value;
    use opencap_driver_ab_logix::{decode_block, AbLogixDriver, LogixBlock};

    fn driver(input: &Json) -> Result<Result<(AbLogixDriver, Vec<Tag>), String>, String> {
        let tags = tags_of(input)?;
        let mut dev = Device::new("SITE/PLC", "SITE", "PLC", "ab_logix", "192.0.2.1", 44818, vec![]);
        dev.params = params_of(input);
        Ok(AbLogixDriver::new(&dev).map(|d| (d, tags)))
    }

    fn block_of(plan: &Plan, i: usize) -> &LogixBlock {
        plan.blocks[i].as_any().downcast_ref::<LogixBlock>().expect("a Logix block")
    }

    #[test]
    fn plan() {
        run_area("ab_logix/plan", |input| {
            let (d, tags) = match driver(input)? {
                Ok(x) => x,
                Err(reason) => return Ok(json!({"error": reason})),
            };
            let plan = d.plan_tags(&tags);
            let blocks: Vec<Json> = (0..plan.blocks.len())
                .map(|i| {
                    let b = block_of(&plan, i);
                    json!({
                        "reads": b.reads.iter().map(|r| json!({"name": r.name, "data_size": r.data_size, "path_size": r.path_size})).collect::<Vec<_>>(),
                        "items": b.items.iter().map(|it| json!({"tag": it.tag, "index": it.index, "bit": it.bit})).collect::<Vec<_>>(),
                        "tags": b.tags,
                        "micro800": b.micro800,
                        "connection_size": b.connection_size,
                        "wire_requests": b.estimate_wire_requests(),
                    })
                })
                .collect();
            Ok(json!({"blocks": blocks, "invalid": plan.invalid}))
        });
    }

    #[test]
    fn decode() {
        run_area("ab_logix/decode", |input| {
            let (d, tags) = driver(input)?.map_err(|e| format!("params refused: {e}"))?;
            let plan = d.plan_tags(&tags);
            if plan.blocks.len() != 1 || !plan.invalid.is_empty() {
                return Err(format!("decode vector must plan to one block, got {}", plan.blocks.len()));
            }
            // `values` is `none` (no reply) or a list whose elements may be `none`
            let values: Option<Vec<Option<Value>>> = match input["values"]["t"].as_str() {
                Some("none") => None,
                Some("list") => Some(
                    input["values"]["v"]
                        .as_array()
                        .ok_or("bad list")?
                        .iter()
                        .map(decode_value)
                        .collect::<Result<_, _>>()?,
                ),
                _ => return Err("values must be a list or none".into()),
            };
            let decoded = decode_block(block_of(&plan, 0), values.as_deref());
            let out: Map<String, Json> = decoded
                .iter()
                .map(|(n, r)| {
                    let enc = match r {
                        Ok(v) => encode_value(Some(v)),
                        Err(e) => encode_decode_error(&e.0),
                    };
                    (n.clone(), enc)
                })
                .collect();
            Ok(json!({"values": out}))
        });
    }
}

mod melsec_mc {
    use super::*;
    use opencap_driver_melsec_mc::frame::{
        batch_read_words, end_code_text, is_config_end_code, monitoring_timer, parse_reply, read_cpu_model,
        reply_words, Reply,
    };
    use opencap_driver_melsec_mc::{address::device_spec, decode_block, McBlock, McParams, MelsecMcDriver};

    fn driver(input: &Json) -> Result<Result<(MelsecMcDriver, Vec<Tag>), String>, String> {
        let tags = tags_of(input)?;
        let mut dev = Device::new("SITE/PLC", "SITE", "PLC", "melsec_mc", "192.0.2.1", 5000, vec![]);
        dev.params = params_of(input);
        Ok(MelsecMcDriver::new(&dev).map(|d| (d, tags)))
    }

    fn block_of(plan: &Plan, i: usize) -> &McBlock {
        plan.blocks[i].as_any().downcast_ref::<McBlock>().expect("an MC block")
    }

    #[test]
    fn plan() {
        run_area("melsec_mc/plan", |input| {
            let (d, tags) = match driver(input)? {
                Ok(x) => x,
                Err(reason) => return Ok(json!({"error": reason})),
            };
            let plan = d.plan_tags(&tags);
            let blocks: Vec<Json> = (0..plan.blocks.len())
                .map(|i| {
                    let b = block_of(&plan, i);
                    let items: Vec<Json> = b
                        .items
                        .iter()
                        .map(|it| {
                            json!({
                                "names": it.names, "offset": it.offset, "text": it.addr.text,
                                "dtype": it.addr.dtype.as_str(), "words": it.addr.words, "bit": it.addr.bit,
                                "dims": it.addr.dims, "str_len": it.addr.str_len,
                                "str_order": it.addr.str_order.map(|c| c.to_string()),
                            })
                        })
                        .collect();
                    json!({
                        "device": b.device, "start": b.start, "count": b.count,
                        "head_number": b.head_number(), "last_number": b.last_number(),
                        "describe": opencap_core::driver::Block::describe(b),
                        "tags": b.tags, "items": items,
                    })
                })
                .collect();
            Ok(json!({"blocks": blocks, "invalid": plan.invalid}))
        });
    }

    #[test]
    fn decode() {
        run_area("melsec_mc/decode", |input| {
            let (d, tags) = driver(input)?.map_err(|e| format!("params refused: {e}"))?;
            let plan = d.plan_tags(&tags);
            if plan.blocks.len() != 1 || !plan.invalid.is_empty() {
                return Err(format!("decode vector must plan to one block, got {}", plan.blocks.len()));
            }
            let words: Vec<u16> = input["reply"]["words"]
                .as_array()
                .ok_or("no words")?
                .iter()
                .map(|w| w.as_u64().map(|w| w as u16).ok_or("bad word"))
                .collect::<Result<_, _>>()?;
            let decoded = decode_block(block_of(&plan, 0), &words, &d.params);
            let out: Map<String, Json> = decoded
                .iter()
                .map(|(n, r)| {
                    let enc = match r {
                        Ok(v) => encode_value(Some(v)),
                        Err(e) => encode_decode_error(&e.0),
                    };
                    (n.clone(), enc)
                })
                .collect();
            Ok(json!({"values": out}))
        });
    }

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    #[test]
    fn frame() {
        run_area("melsec_mc/frame", |input| {
            let (d, tags) = driver(input)?.map_err(|e| format!("params refused: {e}"))?;
            let timer = monitoring_timer(input["timeout_ms"].as_i64().ok_or("no timeout_ms")?);
            let plan = d.plan_tags(&tags);
            let p: &McParams = &d.params;
            let requests: Vec<String> = (0..plan.blocks.len())
                .map(|i| {
                    let b = block_of(&plan, i);
                    let code = device_spec(b.device).expect("device").code;
                    hex(&batch_read_words(p, timer, code, b.head_number() as u32, b.count as u16))
                })
                .collect();
            Ok(json!({"requests": requests, "cpu_model": hex(&read_cpu_model(p, timer))}))
        });
    }

    #[test]
    fn reply() {
        run_area("melsec_mc/reply", |input| {
            let text = input["frame"].as_str().ok_or("no frame")?;
            let frame: Vec<u8> = (0..text.len() / 2)
                .map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).map_err(|e| e.to_string()))
                .collect::<Result<_, _>>()?;
            let words = input["words"].as_u64().ok_or("no words")? as usize;
            Ok(match parse_reply(&frame) {
                Err(e) => json!({"frame_error": e}),
                Ok(Reply::EndCode(code)) => json!({
                    "end_code": code,
                    "kind": if is_config_end_code(code) { "config" } else { "comm" },
                    "message": end_code_text(code),
                }),
                Ok(Reply::Data(data)) => match reply_words(&data, words) {
                    Ok(w) => json!({"words": w}),
                    Err(e) => json!({"frame_error": e}),
                },
            })
        });
    }
}

mod s7 {
    use super::*;
    use opencap_core::driver::Block;
    use opencap_driver_s7::address::Area;
    use opencap_driver_s7::proto::{self, ReadItem, ReplyError};
    use opencap_driver_s7::{decode_block, S7Block, S7Driver};

    fn driver(input: &Json) -> Result<Result<(S7Driver, Vec<Tag>), String>, String> {
        let tags = tags_of(input)?;
        let mut dev = Device::new("SITE/PLC", "SITE", "PLC", "s7", "192.0.2.1", 102, vec![]);
        dev.params = params_of(input);
        Ok(S7Driver::new(&dev).map(|d| (d, tags)))
    }

    fn block_of(plan: &Plan, i: usize) -> &S7Block {
        plan.blocks[i].as_any().downcast_ref::<S7Block>().expect("an S7 block")
    }

    fn unhex(text: &str) -> Result<Vec<u8>, String> {
        (0..text.len() / 2)
            .map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).map_err(|e| e.to_string()))
            .collect()
    }

    #[test]
    fn plan() {
        run_area("s7/plan", |input| {
            let (d, tags) = match driver(input)? {
                Ok(x) => x,
                Err(reason) => return Ok(json!({"error": reason})),
            };
            let plan = d.plan_tags(&tags);
            let blocks: Vec<Json> = (0..plan.blocks.len())
                .map(|i| {
                    let b = block_of(&plan, i);
                    let items: Vec<Json> = b
                        .items
                        .iter()
                        .map(|it| {
                            json!({
                                "names": it.names, "offset": it.offset, "text": it.addr.text,
                                "dtype": it.addr.dtype.as_str(), "kind": it.addr.kind.as_str(),
                                "size": it.addr.size, "elements": it.addr.count, "bit": it.addr.bit,
                                "dims": it.addr.dims, "str_len": it.addr.str_len,
                            })
                        })
                        .collect();
                    json!({
                        "area": b.area.as_str(), "db": b.db, "start": b.start, "count": b.count,
                        "describe": b.describe(), "tags": b.tags, "items": items,
                    })
                })
                .collect();
            Ok(json!({"blocks": blocks, "invalid": plan.invalid}))
        });
    }

    #[test]
    fn decode() {
        run_area("s7/decode", |input| {
            let (d, tags) = driver(input)?.map_err(|e| format!("params refused: {e}"))?;
            let plan = d.plan_tags(&tags);
            if plan.blocks.len() != 1 || !plan.invalid.is_empty() {
                return Err(format!("decode vector must plan to one block, got {}", plan.blocks.len()));
            }
            let bytes = unhex(input["reply"]["bytes"].as_str().ok_or("no bytes")?)?;
            let decoded = decode_block(block_of(&plan, 0), &bytes, &d.params);
            let out: Map<String, Json> = decoded
                .iter()
                .map(|(n, r)| {
                    let enc = match r {
                        Ok(v) => encode_value(Some(v)),
                        Err(e) => encode_decode_error(&e.0),
                    };
                    (n.clone(), enc)
                })
                .collect();
            Ok(json!({"values": out}))
        });
    }

    #[test]
    fn frame() {
        run_area("s7/frame", |input| {
            let (d, tags) = driver(input)?.map_err(|e| format!("params refused: {e}"))?;
            let p = &d.params;
            let pdu = input["pdu"].as_u64().map_or(p.max_pdu, |v| v as usize);
            let tsap = proto::remote_tsap(p.connection_type, p.rack, p.slot);
            let plan = d.plan_tags(&tags);
            let requests: Vec<Vec<String>> = (0..plan.blocks.len())
                .map(|i| {
                    let b = block_of(&plan, i);
                    let (word_len, area) = match b.area {
                        Area::T => (proto::WL_TIMER, b.area.code()),
                        Area::C => (proto::WL_COUNTER, b.area.code()),
                        a => (proto::WL_BYTE, a.code()),
                    };
                    b.parts(pdu)
                        .into_iter()
                        .enumerate()
                        .map(|(n, (start, count))| {
                            let item =
                                ReadItem { area, db: b.db as u16, start: start as u32, word_len, count: count as u16 };
                            proto::hex(&proto::read_var(n as u16 + 1, &[item]))
                        })
                        .collect()
                })
                .collect();
            let szl: Vec<String> =
                [0x001Cu16, 0x0011, 0x0424].iter().map(|id| proto::hex(&proto::read_szl(1, *id, 0))).collect();
            Ok(json!({
                "connection_request": proto::hex(&proto::connection_request(proto::LOCAL_TSAP, tsap)),
                "setup": proto::hex(&proto::setup_communication(1, p.max_pdu as u16)),
                "szl": szl,
                "requests": requests,
            }))
        });
    }

    #[test]
    fn reply() {
        run_area("s7/reply", |input| {
            let pdu = unhex(input["pdu"].as_str().ok_or("no pdu")?)?;
            let item = ReadItem {
                area: 0x84,
                db: 1,
                start: 0,
                word_len: input["word_len"].as_u64().ok_or("no word_len")? as u8,
                count: input["count"].as_u64().ok_or("no count")? as u16,
            };
            let parsed = match proto::parse_pdu(&pdu) {
                Ok(p) => p,
                Err(ReplyError::Header { code, text }) => return Ok(json!({"header_error": code, "message": text})),
                Err(ReplyError::Malformed(m)) => return Ok(json!({"bad_reply": m})),
            };
            Ok(match proto::parse_read_reply(&parsed, &[item]) {
                Ok(items) => json!({"return_code": items[0].return_code, "data": proto::hex(&items[0].data)}),
                Err(e) => json!({"bad_reply": e.message()}),
            })
        });
    }
}

/// DEV-13 (#112): device local time -> UTC around DST changes (`tests/conformance/timezones/device_time/`).
#[test]
fn device_time_vectors() {
    use chrono::NaiveDateTime;
    use opencap_core::device_time::DeviceZone;
    let n = run_area("timezones/device_time", |input| {
        let zone = input["zone"].as_str().ok_or("zone")?;
        let local: NaiveDateTime =
            input["local"].as_str().ok_or("local")?.parse().map_err(|e| format!("local: {e}"))?;
        Ok(match DeviceZone::parse(zone) {
            Err(_) => json!({"ok": false}),
            Ok(z) => {
                let (utc, fold) = z.local_to_utc(local);
                json!({"ok": true, "utc": opencap_core::value::iso_utc(utc), "fold": fold.as_str()})
            }
        })
    });
    assert!(n >= 25, "{n}");
}
