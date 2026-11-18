//! Print what the Rust config models make of the shared conformance fixtures, as JSON, for
//! `tests/conformance/config/compare.py`, which checks it against the Python models (#39,
//! ADR-0003).
//!
//!     cargo run -q -p opencap-core --example config_dump -- tests/conformance/config/*/*.json
//!
//! Per fixture file name: `{"ok": bool, "model": <the document with defaults filled in>,
//! "error": <message>}`; a `devices` document is also loaded as a config directory on its
//! own (`load_config`), giving `"resolved": {"channels": [...], "warnings": [...]}`.

use std::path::Path;
use std::process::ExitCode;

use opencap_core::config::loader::{from_json, load_config, DEVICES_FILE};
use opencap_core::config::{DeviceMap, DevicesFile, Overrides, PollerConfig};
use serde_json::{json, Map, Value};

fn model(document: &str, data: Value) -> Result<Value, String> {
    let dump = |v: Result<Value, serde_json::Error>| v.map_err(|e| e.to_string());
    match document {
        "devices" => {
            let file = from_json::<DevicesFile>(data)?;
            file.validate()?;
            dump(serde_json::to_value(file))
        }
        "devices_map" => dump(serde_json::to_value(from_json::<DeviceMap>(data)?)),
        "overrides" => dump(serde_json::to_value(from_json::<Overrides>(data)?)),
        "poller" => {
            let p = from_json::<PollerConfig>(data)?;
            p.mqtt.validate().map_err(|e| format!("mqtt: {e}"))?;
            p.api.validate().map_err(|e| format!("api: {e}"))?;
            p.opcua_server.validate()?;
            dump(serde_json::to_value(p))
        }
        other => Err(format!("no Rust model for document {other:?}")),
    }
}

/// `devices.json` alone in a config directory, through the loader (layering, default topics).
fn resolved(name: &str, data: &Value) -> Value {
    let dir = std::env::temp_dir().join(format!("opencap-config-dump-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("temp dir");
    std::fs::write(dir.join(DEVICES_FILE), data.to_string()).expect("write devices.json");
    let out = match load_config(&dir) {
        Ok(cfg) => json!({"channels": cfg.channels, "warnings": cfg.warnings}),
        Err(e) => json!({"error": e.to_string()}),
    };
    let _ = std::fs::remove_dir_all(&dir);
    out
}

fn main() -> ExitCode {
    let mut out = Map::new();
    for arg in std::env::args().skip(1) {
        let path = Path::new(&arg);
        let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or(arg.clone());
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("{arg}: {e}");
                return ExitCode::FAILURE;
            }
        };
        let fx: Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("{arg}: {e}");
                return ExitCode::FAILURE;
            }
        };
        let document = fx["document"].as_str().unwrap_or_default();
        let mut entry = match model(document, fx["data"].clone()) {
            Ok(dump) => json!({"ok": true, "model": dump}),
            Err(e) => json!({"ok": false, "error": e}),
        };
        if document == "devices" && entry["ok"] == true {
            entry["resolved"] = resolved(&name, &fx["data"]);
        }
        out.insert(name, entry);
    }
    println!("{}", serde_json::to_string_pretty(&Value::Object(out)).expect("serializes"));
    ExitCode::SUCCESS
}
