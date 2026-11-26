//! The Sparkplug topic namespace (spec chapter 4):
//! `spBv1.0/<group_id>/<NBIRTH|NDEATH|NDATA|NCMD>/<edge_node_id>`,
//! `spBv1.0/<group_id>/<DBIRTH|DDEATH|DDATA|DCMD>/<edge_node_id>/<device_id>` and
//! `spBv1.0/STATE/<host_id>`.

use std::fmt;

use crate::CodecError;

/// The namespace of Sparkplug B 3.0 (and 2.x) topics.
pub const NAMESPACE: &str = "spBv1.0";

/// The message type element of a topic.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MessageType {
    NBirth,
    NDeath,
    NData,
    NCmd,
    DBirth,
    DDeath,
    DData,
    DCmd,
}

impl MessageType {
    pub const ALL: [MessageType; 8] = [
        MessageType::NBirth,
        MessageType::NDeath,
        MessageType::NData,
        MessageType::NCmd,
        MessageType::DBirth,
        MessageType::DDeath,
        MessageType::DData,
        MessageType::DCmd,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            MessageType::NBirth => "NBIRTH",
            MessageType::NDeath => "NDEATH",
            MessageType::NData => "NDATA",
            MessageType::NCmd => "NCMD",
            MessageType::DBirth => "DBIRTH",
            MessageType::DDeath => "DDEATH",
            MessageType::DData => "DDATA",
            MessageType::DCmd => "DCMD",
        }
    }

    /// True for the types whose topic ends with a device id.
    pub fn is_device(self) -> bool {
        matches!(self, MessageType::DBirth | MessageType::DDeath | MessageType::DData | MessageType::DCmd)
    }

    /// `"NBIRTH"` -> `NBirth`, ...
    pub fn parse(s: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|t| t.as_str() == s)
    }

    /// The MQTT QoS an edge node uses for this type: 1 for NDEATH (registered as the Will),
    /// 0 for births and data (spec chapter 6). Edge node messages are never retained.
    /// NCMD/DCMD are published by host applications.
    pub fn qos(self) -> u8 {
        u8::from(self == MessageType::NDeath)
    }
}

/// A parsed or built Sparkplug topic.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Topic {
    Node { group: String, kind: MessageType, edge_node: String },
    Device { group: String, kind: MessageType, edge_node: String, device: String },
    State { host_id: String },
}

/// A group, edge node, device or host id: a non-empty UTF-8 string without the reserved
/// characters `+`, `/` and `#` (spec 4.1.1), and without NUL (not allowed in MQTT topics).
pub fn validate_id(what: &str, id: &str) -> Result<(), CodecError> {
    if id.is_empty() {
        return Err(CodecError::new(format!("{what} must not be empty")));
    }
    if let Some(c) = id.chars().find(|c| matches!(c, '+' | '/' | '#' | '\0')) {
        return Err(CodecError::new(format!("{what} {id:?} contains the reserved character {c:?}")));
    }
    Ok(())
}

impl Topic {
    /// An edge node topic (NBIRTH, NDEATH, NDATA, NCMD).
    pub fn node(group: &str, kind: MessageType, edge_node: &str) -> Result<Self, CodecError> {
        if kind.is_device() {
            return Err(CodecError::new(format!("{} needs a device id", kind.as_str())));
        }
        validate_id("group id", group)?;
        validate_id("edge node id", edge_node)?;
        Ok(Topic::Node { group: group.into(), kind, edge_node: edge_node.into() })
    }

    /// A device topic (DBIRTH, DDEATH, DDATA, DCMD).
    pub fn device(group: &str, kind: MessageType, edge_node: &str, device: &str) -> Result<Self, CodecError> {
        if !kind.is_device() {
            return Err(CodecError::new(format!("{} takes no device id", kind.as_str())));
        }
        validate_id("group id", group)?;
        validate_id("edge node id", edge_node)?;
        validate_id("device id", device)?;
        Ok(Topic::Device { group: group.into(), kind, edge_node: edge_node.into(), device: device.into() })
    }

    /// The primary host's state topic `spBv1.0/STATE/<host_id>`.
    pub fn state(host_id: &str) -> Result<Self, CodecError> {
        validate_id("host id", host_id)?;
        Ok(Topic::State { host_id: host_id.into() })
    }

    /// The message type, or None for STATE.
    pub fn kind(&self) -> Option<MessageType> {
        match self {
            Topic::Node { kind, .. } | Topic::Device { kind, .. } => Some(*kind),
            Topic::State { .. } => None,
        }
    }

    /// Parse a topic in the Sparkplug B namespace.
    pub fn parse(topic: &str) -> Result<Self, CodecError> {
        let bad = |why: &str| CodecError::new(format!("not a Sparkplug B topic ({why}): {topic:?}"));
        let parts: Vec<&str> = topic.split('/').collect();
        if parts.first() != Some(&NAMESPACE) {
            return Err(bad("namespace"));
        }
        match parts.as_slice() {
            [_, "STATE", host] => Topic::state(host),
            [_, group, kind, rest @ ..] => {
                let kind = MessageType::parse(kind).ok_or_else(|| bad("message type"))?;
                match (kind.is_device(), rest) {
                    (false, [edge]) => Topic::node(group, kind, edge),
                    (true, [edge, device]) => Topic::device(group, kind, edge, device),
                    _ => Err(bad("number of levels")),
                }
            }
            _ => Err(bad("number of levels")),
        }
    }
}

impl fmt::Display for Topic {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Topic::Node { group, kind, edge_node } => write!(f, "{NAMESPACE}/{group}/{}/{edge_node}", kind.as_str()),
            Topic::Device { group, kind, edge_node, device } => {
                write!(f, "{NAMESPACE}/{group}/{}/{edge_node}/{device}", kind.as_str())
            }
            Topic::State { host_id } => write!(f, "{NAMESPACE}/STATE/{host_id}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips() {
        for t in [
            Topic::node("Plant A", MessageType::NBirth, "opencap-1").unwrap(),
            Topic::device("Plant A", MessageType::DData, "opencap-1", "Line 1.PLC1").unwrap(),
            Topic::state("ignition-primary").unwrap(),
        ] {
            assert_eq!(Topic::parse(&t.to_string()).unwrap(), t);
        }
        assert_eq!(Topic::device("G", MessageType::DBirth, "E", "D").unwrap().to_string(), "spBv1.0/G/DBIRTH/E/D");
    }

    #[test]
    fn rejects_bad_topics_and_ids() {
        for t in [
            "spAv1.0/G/NBIRTH/E",
            "spBv1.0/G/NBIRTH",
            "spBv1.0/G/NBIRTH/E/D",
            "spBv1.0/G/DDATA/E",
            "spBv1.0/G/NOPE/E",
            "spBv1.0/STATE",
            "spBv1.0//NBIRTH/E",
            "spBv1.0/G/NCMD/E+",
        ] {
            assert!(Topic::parse(t).is_err(), "{t}");
        }
        assert!(Topic::node("G", MessageType::DData, "E").is_err());
        assert!(Topic::device("G", MessageType::NData, "E", "D").is_err());
        assert!(validate_id("device id", "a/b").is_err());
        assert!(validate_id("device id", "a#").is_err());
        assert!(validate_id("device id", "").is_err());
        assert!(validate_id("device id", "Pumpe Süd 1").is_ok());
    }
}
