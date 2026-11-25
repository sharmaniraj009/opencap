//! The Sparkplug column of ADR-0006: every metric carries its opencap quality as the `Quality`
//! property (an Int32 with the OPC DA quality code Ignition reads), and the qualities whose
//! value must not be used are sent as null.
//!
//! The shared mapping module of #30 does not exist yet in either engine; when it lands this
//! module becomes a thin re-export and its vectors replace the table test below.

use opencap_core::value::Quality;

/// The metric property that carries the quality code.
pub const QUALITY_PROPERTY: &str = "Quality";

/// ADR-0006: good 192 (0xC0), bad_comm 0x18, bad_config 0x04, bad_decode 0x00, stale 0x44.
pub fn quality_code(q: Quality) -> u32 {
    match q {
        Quality::Good => 0xC0,
        Quality::BadComm => 0x18,
        Quality::BadConfig => 0x04,
        Quality::BadDecode => 0x00,
        Quality::Stale => 0x44,
    }
}

/// `is_null` even when a last value exists: the address is unusable or the reply could not be
/// converted, so the last value says nothing (ADR-0006). bad_comm and stale keep the last value.
pub fn withholds_value(q: Quality) -> bool {
    matches!(q, Quality::BadConfig | Quality::BadDecode)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adr_0006_table() {
        let table = [
            (Quality::Good, 192, false),
            (Quality::BadComm, 0x18, false),
            (Quality::BadConfig, 0x04, true),
            (Quality::BadDecode, 0x00, true),
            (Quality::Stale, 0x44, false),
        ];
        for (q, code, null) in table {
            assert_eq!((quality_code(q), withholds_value(q)), (code, null), "{q:?}");
        }
    }
}
