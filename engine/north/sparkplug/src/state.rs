//! The primary host application's state (Sparkplug 3.0 §4.2.2): JSON on the retained topic
//! `spBv1.0/STATE/<host_id>`, `{"online": true|false, "timestamp": <ms since epoch>}`.
//! An edge node configured with a primary host publishes its births only while that host is
//! online, and keeps DATA in its outbox otherwise (ADR-0011).
//!
//! Sparkplug 2.x hosts (older Ignition MQTT Engine) publish the plain text `ONLINE`/`OFFLINE` on
//! `STATE/<host_id>`, outside the namespace; that form is not supported (spec doc §7).

use crate::CodecError;

/// A parsed STATE message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HostState {
    pub online: bool,
    pub timestamp: u64,
}

impl HostState {
    /// Parse the JSON payload. Both keys are required; other keys are ignored.
    pub fn parse(payload: &[u8]) -> Result<Self, CodecError> {
        let bad = |why: &str| CodecError::new(format!("invalid Sparkplug STATE payload ({why})"));
        let v: serde_json::Value = serde_json::from_slice(payload).map_err(|_| bad("not JSON"))?;
        let online = v.get("online").and_then(serde_json::Value::as_bool).ok_or_else(|| bad("online"))?;
        let timestamp = v.get("timestamp").and_then(serde_json::Value::as_u64).ok_or_else(|| bad("timestamp"))?;
        Ok(HostState { online, timestamp })
    }

    /// True when `self` supersedes `previous`: STATE messages can arrive out of order across
    /// reconnects, so an older timestamp never overrides a newer one (spec 4.2.2.5).
    pub fn supersedes(&self, previous: Option<&HostState>) -> bool {
        previous.is_none_or(|p| self.timestamp >= p.timestamp)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_3_0_payload() {
        let s = HostState::parse(br#"{"online": true, "timestamp": 1700000000123}"#).unwrap();
        assert_eq!(s, HostState { online: true, timestamp: 1_700_000_000_123 });
        for bad in [
            &b"ONLINE"[..],
            br#"{"online": "true", "timestamp": 1}"#,
            br#"{"online": true}"#,
            br#"{"online": true, "timestamp": -1}"#,
        ] {
            assert!(HostState::parse(bad).is_err(), "{}", String::from_utf8_lossy(bad));
        }
        let old = HostState { online: false, timestamp: 5 };
        assert!(s.supersedes(Some(&old)) && !old.supersedes(Some(&s)) && old.supersedes(None));
    }
}
