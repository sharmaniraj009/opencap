//! Product and support information (Python `opencap.core.support`, D-075): `GET /api/v1/about`,
//! `GET /api/v1/support` and `opencap-engine info`.
//!
//! Safe to paste into a public issue: build facts (versions, platform) and the names of what is
//! enabled. Never an IP address, a host name or path from the config, a secret or an environment
//! variable name or value.

use serde_json::{json, Value};

use opencap_core::config::SCHEMA_VERSION;

pub const ENGINE: &str = "rust";
pub const API_VERSIONS: [&str; 1] = ["v1"];
/// The package version (`0.1.0`), not `FIRM_VERSION` (`opencap-0.1.0`).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const RUSTC_VERSION: &str = env!("OPENCAP_RUSTC_VERSION");

/// Python `version_parts`: major, minor, patch; a part's non-digits are dropped, missing parts are 0.
pub fn version_parts(version: &str) -> (u64, u64, u64) {
    let mut nums = version.split('.').take(3).map(|p| {
        let digits: String = p.chars().filter(char::is_ascii_digit).collect();
        digits.parse().unwrap_or(0)
    });
    (nums.next().unwrap_or(0), nums.next().unwrap_or(0), nums.next().unwrap_or(0))
}

/// `GET /api/v1/about`: open, so it carries nothing that identifies a site.
pub fn about() -> Value {
    let (major, minor, patch) = version_parts(VERSION);
    json!({
        "product": "opencap", "version": VERSION, "version_major": major, "version_minor": minor,
        "version_patch": patch, "engine": ENGINE, "api_versions": API_VERSIONS,
        "config_schema_version": SCHEMA_VERSION,
    })
}

/// Python `platform.system()` names.
pub fn os_name() -> &'static str {
    match std::env::consts::OS {
        "linux" => "Linux",
        "windows" => "Windows",
        "macos" => "Darwin",
        other => other,
    }
}

/// The full summary; `config_revision` is None when the config cannot be read.
pub fn support_info(config_revision: Option<String>) -> Value {
    // What this engine actually serves: it has no OPC UA server yet (#46), whatever the config says.
    let north = ["mqtt", "api"];
    let mut drivers: Vec<&str> = opencap_drivers::compiled_in();
    drivers.sort_unstable();
    let drivers: Vec<Value> =
        drivers.iter().map(|d| json!({"name": d, "library": null, "library_version": null})).collect();
    let mut out = about();
    let obj = out.as_object_mut().expect("about is an object");
    obj.insert("os".into(), json!(os_name()));
    obj.insert("arch".into(), json!(std::env::consts::ARCH));
    obj.insert("toolchain".into(), json!({"rust": RUSTC_VERSION}));
    obj.insert("drivers".into(), Value::Array(drivers));
    obj.insert("north_interfaces".into(), json!(north));
    obj.insert("config_revision".into(), json!(config_revision));
    out
}

/// The `info` text (same layout as Python's `render_text`).
pub fn render_text(info: &Value) -> String {
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let list = |v: &Value| v.as_array().map(|a| a.iter().map(&s).collect::<Vec<_>>().join(", ")).unwrap_or_default();
    let mut lines = vec![
        format!("opencap {} ({} engine)", s(&info["version"]), s(&info["engine"])),
        format!("api versions: {}; config schema {}", list(&info["api_versions"]), info["config_schema_version"]),
        format!("os: {} {}", s(&info["os"]), s(&info["arch"])),
        format!("toolchain: rust {}", s(&info["toolchain"]["rust"])),
        "drivers:".into(),
    ];
    for d in info["drivers"].as_array().into_iter().flatten() {
        lines.push(format!("  {:<14} built in", s(&d["name"])));
    }
    lines.push(format!("north interfaces: {}", list(&info["north_interfaces"])));
    let rev = info["config_revision"].as_str().unwrap_or("unknown");
    lines.push(format!("config revision: {rev}"));
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn about_and_support() {
        assert_eq!(version_parts("1.2.3rc1"), (1, 2, 31), "every digit of a part, as Python");
        assert_eq!(version_parts("2"), (2, 0, 0));
        let a = about();
        assert_eq!(a["engine"], "rust");
        assert_eq!(a["version"], VERSION);
        let info = support_info(Some("0123456789abcdef".into()));
        assert_eq!(info["config_revision"], "0123456789abcdef");
        assert!(info["toolchain"]["rust"].as_str().unwrap().starts_with('1'));
        let text = render_text(&info);
        assert!(text.starts_with(&format!("opencap {VERSION} (rust engine)")), "{text}");
        assert!(text.contains("config revision: 0123456789abcdef"));
        assert!(render_text(&support_info(None)).ends_with("config revision: unknown"));
    }
}
