//! Sparkplug payloads for the opencap edge node (docs/spec/payload-sparkplug.md §4–§6):
//! NBIRTH, NDEATH, DBIRTH, DDATA and DDEATH, the metrics in them, aliases, `seq` and `bdSeq`.
//!
//! Spec rules these builders apply (Sparkplug 3.0 chapter 6): every NBIRTH/DBIRTH metric has a
//! name, an alias, a timestamp and a datatype; DDATA metrics have the alias and a timestamp
//! only (no name, no datatype); NBIRTH has `seq` 0, a `bdSeq` metric (Int64) and
//! `Node Control/Rebirth` = false; NDEATH has no `seq` and the `bdSeq` metric only.

use opencap_core::value::{Quality, Value};
use prost::Message as _;

use crate::datatype::encode;
use crate::proto::payload::{property_value, Metric, PropertySet, PropertyValue};
use crate::proto::{DataType, Payload};
use crate::quality::{quality_code, withholds_value, QUALITY_PROPERTY};
use crate::CodecError;

/// The birth/death sequence metric of NBIRTH and NDEATH.
pub const BDSEQ: &str = "bdSeq";
/// The node control metric a host writes `true` to request a new birth sequence.
pub const REBIRTH: &str = "Node Control/Rebirth";
/// Ignition MQTT Engine's engineering-unit and range properties (§3.3; *verify* on L2).
pub const ENG_UNIT: &str = "engUnit";
pub const ENG_LOW: &str = "engLow";
pub const ENG_HIGH: &str = "engHigh";

/// The metric name of an opencap tag: its path below the device, with `/` as the folder
/// separator (a tag name's dots are its group path, as in KEPServerEX: `LTI002.Pv` ->
/// `LTI002/Pv`).
pub fn metric_name(tag_name: &str) -> String {
    tag_name.replace('.', "/")
}

/// What a tag's birth metric says about it besides its value.
#[derive(Debug, Clone, PartialEq)]
pub struct TagMeta {
    pub name: String,
    pub alias: u64,
    pub datatype: DataType,
    pub units: Option<String>,
    /// (eng_lo, eng_hi) of a scaled tag.
    pub eng_range: Option<(f64, f64)>,
}

/// One value of a tag as the store holds it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sample<'a> {
    pub value: Option<&'a Value>,
    pub quality: Quality,
    /// source timestamp, milliseconds since the epoch (UTC)
    pub ts_ms: u64,
}

fn prop(dt: DataType, value: property_value::Value) -> PropertyValue {
    PropertyValue { r#type: Some(dt as u32), is_null: None, value: Some(value) }
}

fn properties(quality: Option<Quality>, meta: Option<&TagMeta>) -> Option<PropertySet> {
    let mut set = PropertySet::default();
    let mut add = |k: &str, v: PropertyValue| {
        set.keys.push(k.to_string());
        set.values.push(v);
    };
    if let Some(q) = quality {
        add(QUALITY_PROPERTY, prop(DataType::Int32, property_value::Value::IntValue(quality_code(q))));
    }
    if let Some(meta) = meta {
        if let Some(units) = &meta.units {
            add(ENG_UNIT, prop(DataType::String, property_value::Value::StringValue(units.clone())));
        }
        if let Some((lo, hi)) = meta.eng_range {
            add(ENG_LOW, prop(DataType::Double, property_value::Value::DoubleValue(lo)));
            add(ENG_HIGH, prop(DataType::Double, property_value::Value::DoubleValue(hi)));
        }
    }
    (!set.keys.is_empty()).then_some(set)
}

fn set_value(m: &mut Metric, dt: DataType, s: &Sample) -> Result<(), CodecError> {
    match s.value {
        Some(v) if !withholds_value(s.quality) => m.value = Some(encode(dt, v)?),
        _ => m.is_null = Some(true),
    }
    Ok(())
}

/// A tag's metric in a DBIRTH: name, alias, timestamp, datatype, value (or null) and the
/// properties `Quality`, then `engUnit`, `engLow`, `engHigh` when configured.
pub fn birth_metric(meta: &TagMeta, s: &Sample) -> Result<Metric, CodecError> {
    let mut m = Metric {
        name: Some(meta.name.clone()),
        alias: Some(meta.alias),
        timestamp: Some(s.ts_ms),
        datatype: Some(meta.datatype as u32),
        properties: properties(Some(s.quality), Some(meta)),
        ..Default::default()
    };
    set_value(&mut m, meta.datatype, s)?;
    Ok(m)
}

/// A tag's metric in a DDATA: alias, timestamp, value (or null), and the `Quality` property
/// when `with_quality` (the edge node sends it when the quality changed, §5.2).
pub fn data_metric(alias: u64, datatype: DataType, s: &Sample, with_quality: bool) -> Result<Metric, CodecError> {
    let mut m = Metric {
        alias: Some(alias),
        timestamp: Some(s.ts_ms),
        properties: properties(with_quality.then_some(s.quality), None),
        ..Default::default()
    };
    set_value(&mut m, datatype, s)?;
    Ok(m)
}

fn bdseq_metric(bd_seq: u8, alias: Option<u64>, ts_ms: u64) -> Metric {
    Metric {
        name: Some(BDSEQ.into()),
        alias,
        timestamp: Some(ts_ms),
        datatype: Some(DataType::Int64 as u32),
        value: Some(crate::proto::payload::metric::Value::LongValue(u64::from(bd_seq))),
        ..Default::default()
    }
}

/// The aliases of the two metrics every NBIRTH has.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NodeAliases {
    pub bd_seq: u64,
    pub rebirth: u64,
}

/// NBIRTH: `seq` 0, `bdSeq` (the value of this session's NDEATH), `Node Control/Rebirth` = false,
/// then `extra` node metrics (version, `_System` metrics) built with [`birth_metric`].
pub fn nbirth(ts_ms: u64, bd_seq: u8, aliases: NodeAliases, extra: Vec<Metric>) -> Payload {
    let rebirth = Metric {
        name: Some(REBIRTH.into()),
        alias: Some(aliases.rebirth),
        timestamp: Some(ts_ms),
        datatype: Some(DataType::Boolean as u32),
        value: Some(crate::proto::payload::metric::Value::BooleanValue(false)),
        ..Default::default()
    };
    let mut metrics = vec![bdseq_metric(bd_seq, Some(aliases.bd_seq), ts_ms), rebirth];
    metrics.extend(extra);
    Payload { timestamp: Some(ts_ms), metrics, seq: Some(0), uuid: None, body: None }
}

/// NDEATH, registered as the MQTT Will at CONNECT: the `bdSeq` metric only, no `seq`.
pub fn ndeath(ts_ms: u64, bd_seq: u8) -> Payload {
    Payload {
        timestamp: Some(ts_ms),
        metrics: vec![bdseq_metric(bd_seq, None, ts_ms)],
        seq: None,
        uuid: None,
        body: None,
    }
}

/// DBIRTH: every tag of the device as a [`birth_metric`].
pub fn dbirth(ts_ms: u64, seq: u8, metrics: Vec<Metric>) -> Payload {
    Payload { timestamp: Some(ts_ms), metrics, seq: Some(u64::from(seq)), uuid: None, body: None }
}

/// DDATA: the changed tags as [`data_metric`]s.
pub fn ddata(ts_ms: u64, seq: u8, metrics: Vec<Metric>) -> Payload {
    dbirth(ts_ms, seq, metrics)
}

/// DDEATH: no metrics.
pub fn ddeath(ts_ms: u64, seq: u8) -> Payload {
    dbirth(ts_ms, seq, vec![])
}

/// The protobuf bytes of a payload.
pub fn encode_payload(p: &Payload) -> Vec<u8> {
    p.encode_to_vec()
}

/// Parse protobuf bytes as a Sparkplug payload.
pub fn decode_payload(bytes: &[u8]) -> Result<Payload, CodecError> {
    Payload::decode(bytes).map_err(|e| CodecError::new(format!("not a Sparkplug B payload: {e}")))
}

/// `seq` of one edge node session: NBIRTH carries 0, every later NDATA, DBIRTH, DDATA and
/// DDEATH the next value, 255 wraps to 0. A new NBIRTH (reconnect, rebirth) starts over.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Seq {
    next: u8,
}

impl Seq {
    /// The `seq` of an NBIRTH (always 0); the next message gets 1.
    pub fn birth(&mut self) -> u8 {
        self.next = 1;
        0
    }

    /// The `seq` of the next message after the NBIRTH.
    pub fn next_seq(&mut self) -> u8 {
        let v = self.next;
        self.next = v.wrapping_add(1);
        v
    }
}

/// `bdSeq`: 0 on the first MQTT CONNECT, one more on every later CONNECT, 255 wraps to 0.
/// The same value goes into that CONNECT's Will (NDEATH) and the NBIRTHs of the session.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct BdSeq {
    next: u8,
}

impl BdSeq {
    pub fn starting_at(first: u8) -> Self {
        BdSeq { next: first }
    }

    /// The value for a new MQTT CONNECT.
    pub fn connect(&mut self) -> u8 {
        let v = self.next;
        self.next = v.wrapping_add(1);
        v
    }
}

/// Aliases of one edge node, unique across the node and all its devices: handed out from 1 in
/// birth order (`bdSeq`, `Node Control/Rebirth`, other node metrics, then each device's tags in
/// config order), so the same configuration always gets the same aliases.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AliasAllocator {
    next: u64,
}

impl Default for AliasAllocator {
    fn default() -> Self {
        AliasAllocator { next: 1 }
    }
}

impl AliasAllocator {
    pub fn alloc(&mut self) -> u64 {
        let v = self.next;
        self.next += 1;
        v
    }

    /// The two node aliases every NBIRTH needs (call first).
    pub fn node(&mut self) -> NodeAliases {
        NodeAliases { bd_seq: self.alloc(), rebirth: self.alloc() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::payload::metric::Value as MValue;

    #[test]
    fn seq_wraps_and_restarts_at_birth() {
        let mut s = Seq::default();
        assert_eq!(s.birth(), 0);
        let got: Vec<u8> = (0..257).map(|_| s.next_seq()).collect();
        assert_eq!((got[0], got[254], got[255], got[256]), (1, 255, 0, 1));
        assert_eq!(s.birth(), 0);
        assert_eq!(s.next_seq(), 1);
        let mut b = BdSeq::starting_at(254);
        assert_eq!((b.connect(), b.connect(), b.connect()), (254, 255, 0));
    }

    #[test]
    fn birth_and_data_metrics() {
        let meta = TagMeta {
            name: metric_name("LTI002.Pv"),
            alias: 7,
            datatype: DataType::Double,
            units: Some("m".into()),
            eng_range: Some((0.0, 10.0)),
        };
        let v = Value::Float(2.5);
        let m = birth_metric(&meta, &Sample { value: Some(&v), quality: Quality::Good, ts_ms: 5 }).unwrap();
        assert_eq!(m.name.as_deref(), Some("LTI002/Pv"));
        assert_eq!((m.alias, m.timestamp, m.datatype, m.is_null), (Some(7), Some(5), Some(10), None));
        assert_eq!(m.value, Some(MValue::DoubleValue(2.5)));
        assert_eq!(m.properties.unwrap().keys, ["Quality", "engUnit", "engLow", "engHigh"]);

        let d =
            data_metric(7, DataType::Double, &Sample { value: Some(&v), quality: Quality::BadConfig, ts_ms: 6 }, true)
                .unwrap();
        assert_eq!((d.name, d.datatype, d.is_null, d.value), (None, None, Some(true), None));
        let p = d.properties.unwrap();
        assert_eq!(
            (p.keys, p.values[0].value.clone()),
            (vec!["Quality".to_string()], Some(property_value::Value::IntValue(4)))
        );
        let d =
            data_metric(7, DataType::Double, &Sample { value: Some(&v), quality: Quality::BadComm, ts_ms: 6 }, false)
                .unwrap();
        assert_eq!((d.value, d.is_null, d.properties), (Some(MValue::DoubleValue(2.5)), None, None));
    }

    #[test]
    fn node_birth_and_death() {
        let mut aliases = AliasAllocator::default();
        let node = aliases.node();
        assert_eq!((node.bd_seq, node.rebirth, aliases.alloc()), (1, 2, 3));
        let b = nbirth(100, 3, node, vec![]);
        assert_eq!(b.seq, Some(0));
        assert_eq!(b.metrics.iter().map(|m| m.name.clone().unwrap()).collect::<Vec<_>>(), [BDSEQ, REBIRTH]);
        assert_eq!(b.metrics[0].value, Some(MValue::LongValue(3)));
        assert_eq!(b.metrics[1].value, Some(MValue::BooleanValue(false)));
        let d = ndeath(100, 3);
        assert_eq!((d.seq, d.metrics.len(), d.metrics[0].alias), (None, 1, None));
        assert_eq!(decode_payload(&encode_payload(&d)).unwrap(), d);
        assert!(decode_payload(b"\xff\xff").is_err());
    }
}
