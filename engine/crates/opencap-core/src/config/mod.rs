//! Configuration: models, loader, topic naming.

pub mod loader;
pub mod models;

pub use loader::{load_config, ConfigError, LoadedConfig};
pub use models::*;

use unicode_normalization::UnicodeNormalization;

/// Config schema contract version (Python `opencap.config.schema.SCHEMA_VERSION`; ADR-0003).
pub const SCHEMA_VERSION: u32 = 1;

/// The files a config revision covers, in hash order (Python `config.store.CONFIG_FILES`).
pub const CONFIG_FILES: [&str; 4] = ["poller.yaml", "devices.json", "devices_map.yaml", "overrides.yaml"];

/// Python `ConfigStore.revision()`: the first 16 hex digits of SHA-256 over every config file
/// (`name NUL (0x01 bytes | 0x00) NUL`, a missing file as 0x00), so both engines report the
/// same revision for the same directory.
pub fn config_revision(dir: &std::path::Path) -> String {
    let mut ctx = ring::digest::Context::new(&ring::digest::SHA256);
    for name in CONFIG_FILES {
        ctx.update(name.as_bytes());
        ctx.update(b"\0");
        match std::fs::read(dir.join(name)) {
            Ok(bytes) => {
                ctx.update(b"\x01");
                ctx.update(&bytes);
            }
            Err(_) => ctx.update(b"\0"),
        }
        ctx.update(b"\0");
    }
    ctx.finish().as_ref().iter().take(8).map(|b| format!("{b:02x}")).collect()
}

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

#[cfg(test)]
mod revision_tests {
    use super::config_revision;

    #[test]
    fn revision_is_pythons() {
        let dir = std::env::temp_dir().join(format!("opencap-rev-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        // Python: ConfigStore(dir).revision() for a dir holding only poller.yaml = "a: 1\n"
        std::fs::write(dir.join("poller.yaml"), "a: 1\n").unwrap();
        let rev = config_revision(&dir);
        assert_eq!(rev, "9793c09bc90c3f74", "the Python store's revision of the same directory");
        std::fs::write(dir.join("devices.json"), "").unwrap();
        assert_ne!(config_revision(&dir), rev, "an empty file differs from a missing one");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
