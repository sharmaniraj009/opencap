//! Configuration: models, loader, topic naming.

pub mod loader;
pub mod models;

pub use loader::{load_config, ConfigError, LoadedConfig};
pub use models::*;

use unicode_normalization::UnicodeNormalization;

/// MQTT-safe topic level: lowercase ASCII, runs of anything but `[a-z0-9._-]` -> `_`
/// (Python `slug()`: NFKD, drop non-ASCII, lowercase, collapse, strip `_`). Never empty.
pub fn slug(s: &str) -> String {
    let ascii: String = s.nfkd().filter(char::is_ascii).collect::<String>().to_ascii_lowercase();
    let mut out = String::with_capacity(ascii.len());
    let mut in_run = false;
    for c in ascii.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-') {
            out.push(c);
            in_run = false;
        } else if !in_run {
            out.push('_');
            in_run = true;
        }
    }
    let trimmed = out.trim_matches('_');
    if trimmed.is_empty() {
        "_".into()
    } else {
        trimmed.into()
    }
}

/// `<prefix>/<slug(channel)>/<slug(device)>/data`; `prefix` is poller.yaml `mqtt.topic_prefix`
/// (default `opencap`, D-028), used verbatim.
pub fn default_topic(device: &Device, prefix: &str) -> String {
    format!("{prefix}/{}/{}/data", slug(&device.channel), slug(&device.name))
}

/// Device-id list for aggregate warnings: the first five, then `… (+N more)`.
pub fn ids_text(ids: &[String]) -> String {
    const LIMIT: usize = 5;
    let shown = ids.iter().take(LIMIT).cloned().collect::<Vec<_>>().join(", ");
    if ids.len() > LIMIT {
        format!("{shown}, … (+{} more)", ids.len() - LIMIT)
    } else {
        shown
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slug_matches_python() {
        assert_eq!(slug("Site 001"), "site_001");
        assert_eq!(slug("E+H"), "e_h");
        assert_eq!(slug("8 PUMP"), "8_pump");
        assert_eq!(slug("  #+/  "), "_");
        assert_eq!(slug("Café Ünit"), "cafe_unit");
        assert_eq!(slug("$SYS"), "sys");
        assert_eq!(slug("a__b"), "a__b"); // '_' is kept, only other runs collapse
        assert_eq!(slug("a-.b"), "a-.b");
        assert_eq!(slug("x°y"), "xy"); // non-ASCII is dropped, not replaced
    }
}
