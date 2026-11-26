//! NCMD and DCMD handling (docs/spec/payload-sparkplug.md §6): opencap is read-only
//! (ADR-0008), so the only command it honours is `Node Control/Rebirth` = true in an NCMD.
//! Every other metric of an NCMD, and every metric of a DCMD, is rejected; the edge node logs
//! and counts each rejection and writes nothing.

use crate::message::{decode_payload, REBIRTH};
use crate::proto::payload::metric::Value as MValue;
use crate::topic::{MessageType, Topic};
use crate::CodecError;

/// Why a command metric was not executed.
pub const READ_ONLY: &str = "opencap is read-only (ADR-0008): only Node Control/Rebirth is accepted";
pub const NOT_TRUE: &str = "Node Control/Rebirth needs the boolean value true";

/// One metric of a command that opencap did not execute.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rejected {
    /// The metric name, or `alias <n>` when the host sent only an alias.
    pub metric: String,
    pub reason: &'static str,
}

/// What to do with one NCMD/DCMD message.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Decision {
    /// Publish a new NBIRTH and every DBIRTH.
    pub rebirth: bool,
    pub rejected: Vec<Rejected>,
}

/// Classify a command. `rebirth_alias` is the alias `Node Control/Rebirth` had in the current
/// NBIRTH (hosts may address it by alias, spec 6.4.6). Errors: not a command topic, or a
/// payload that is not Sparkplug B protobuf.
pub fn classify(topic: &Topic, payload: &[u8], rebirth_alias: Option<u64>) -> Result<Decision, CodecError> {
    let kind = topic.kind();
    if !matches!(kind, Some(MessageType::NCmd | MessageType::DCmd)) {
        return Err(CodecError::new(format!("{topic} is not a command topic")));
    }
    let payload = decode_payload(payload)?;
    let mut out = Decision::default();
    for m in payload.metrics {
        let is_rebirth =
            m.name.as_deref() == Some(REBIRTH) || (m.name.is_none() && m.alias.is_some() && m.alias == rebirth_alias);
        let label = match (&m.name, m.alias) {
            (Some(n), _) => n.clone(),
            (None, Some(a)) => format!("alias {a}"),
            (None, None) => "(unnamed)".to_string(),
        };
        if kind == Some(MessageType::NCmd) && is_rebirth {
            if m.value == Some(MValue::BooleanValue(true)) {
                out.rebirth = true;
            } else {
                out.rejected.push(Rejected { metric: label, reason: NOT_TRUE });
            }
        } else {
            out.rejected.push(Rejected { metric: label, reason: READ_ONLY });
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::message::encode_payload;
    use crate::proto::payload::Metric;
    use crate::proto::Payload;

    fn cmd(metrics: Vec<Metric>) -> Vec<u8> {
        encode_payload(&Payload { timestamp: Some(1), metrics, seq: None, uuid: None, body: None })
    }

    fn metric(name: Option<&str>, alias: Option<u64>, value: MValue) -> Metric {
        Metric { name: name.map(Into::into), alias, value: Some(value), ..Default::default() }
    }

    #[test]
    fn only_rebirth_true_in_an_ncmd_is_honoured() {
        let ncmd = Topic::node("G", MessageType::NCmd, "E").unwrap();
        let dcmd = Topic::device("G", MessageType::DCmd, "E", "D").unwrap();
        let rebirth = metric(Some(REBIRTH), None, MValue::BooleanValue(true));
        let d = classify(&ncmd, &cmd(vec![rebirth.clone()]), Some(2)).unwrap();
        assert_eq!(d, Decision { rebirth: true, rejected: vec![] });
        let by_alias = metric(None, Some(2), MValue::BooleanValue(true));
        assert!(classify(&ncmd, &cmd(vec![by_alias.clone()]), Some(2)).unwrap().rebirth);

        let write = metric(Some("Node Control/Reboot"), None, MValue::BooleanValue(true));
        let d = classify(&ncmd, &cmd(vec![write, rebirth.clone()]), Some(2)).unwrap();
        assert!(d.rebirth);
        assert_eq!(d.rejected, [Rejected { metric: "Node Control/Reboot".into(), reason: READ_ONLY }]);

        let d = classify(&ncmd, &cmd(vec![metric(Some(REBIRTH), None, MValue::BooleanValue(false))]), Some(2)).unwrap();
        assert_eq!((d.rebirth, d.rejected[0].reason), (false, NOT_TRUE));

        // a DCMD is never executed, not even a metric named like the node control
        let d = classify(&dcmd, &cmd(vec![rebirth, by_alias, metric(None, Some(9), MValue::IntValue(1))]), Some(2))
            .unwrap();
        assert!(!d.rebirth);
        assert_eq!(d.rejected.iter().map(|r| r.metric.as_str()).collect::<Vec<_>>(), [REBIRTH, "alias 2", "alias 9"]);

        assert!(classify(&Topic::node("G", MessageType::NData, "E").unwrap(), &cmd(vec![]), None).is_err());
        assert!(classify(&ncmd, b"\x0a\xff", None).is_err());
    }
}
