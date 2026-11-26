//! The Sparkplug column of ADR-0006 (`tests/conformance/quality/map.json`, Python
//! `opencap.core.quality.to_sparkplug`): every metric carries its opencap quality as the
//! `Quality` property (an Int32 with the OPC DA quality code Ignition reads), and the qualities
//! whose value must not be used are sent as null.
//!
//! The Rust core has no shared quality module yet; when it gets one, this becomes a re-export.
//! `tests/vectors.rs` checks this module against the shared vectors.

use opencap_core::value::Quality;

/// The metric property that carries the quality code.
pub const QUALITY_PROPERTY: &str = "Quality";

/// The `Quality` code: good 192 (0xC0), bad_comm 0x18, bad_config 0x04, bad_decode 0x00, stale
/// 0x44, or 0x00 for a stale tag that was never read good (`ever_good` false).
pub fn quality_code(q: Quality, ever_good: bool) -> u32 {
    match q {
        Quality::Good => 0xC0,
        Quality::BadComm => 0x18,
        Quality::BadConfig => 0x04,
        Quality::BadDecode => 0x00,
        Quality::Stale if ever_good => 0x44,
        Quality::Stale => 0x00,
    }
}

/// `is_null`: bad_config and bad_decode never carry a value (the last one says nothing); stale
/// and bad_comm carry the last value unless the tag never had one; good carries the value.
pub fn is_null(q: Quality, ever_good: bool) -> bool {
    matches!(q, Quality::BadConfig | Quality::BadDecode) || (q != Quality::Good && !ever_good)
}
