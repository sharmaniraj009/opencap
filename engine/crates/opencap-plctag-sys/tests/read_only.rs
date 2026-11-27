//! ADR-0008 / ADR-0018: no write path to a PLC through libplctag. The FFI crate may declare only
//! the allow-listed read functions, and no engine source may mention a libplctag write or set
//! function. (libplctag's own write code is compiled in but unreachable from Rust.)

use std::path::{Path, PathBuf};

/// Every function the FFI crate may declare.
const ALLOWED: &[&str] = &[
    "plc_tag_create",
    "plc_tag_destroy",
    "plc_tag_read",
    "plc_tag_status",
    "plc_tag_decode_error",
    "plc_tag_get_size",
    "plc_tag_get_bit",
    "plc_tag_get_uint32",
    "plc_tag_get_int32",
    "plc_tag_get_uint16",
    "plc_tag_get_int16",
    "plc_tag_get_float32",
    "plc_tag_get_raw_bytes",
    "plc_tag_get_string_length",
    "plc_tag_get_string",
    "plc_tag_set_debug_level",
    "plc_tag_shutdown",
];

fn engine_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().is_some_and(|x| x == "rs") {
            out.push(p);
        }
    }
}

#[test]
fn the_ffi_crate_declares_only_allow_listed_read_functions() {
    let src = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs")).unwrap();
    let mut declared = 0;
    for line in src.lines() {
        if let Some(rest) = line.trim().strip_prefix("pub fn ") {
            if let Some(name) = rest.split('(').next() {
                if name.starts_with("plc_tag_") {
                    declared += 1;
                    assert!(ALLOWED.contains(&name), "{name} is not on the read-only allow-list (ADR-0008)");
                }
            }
        }
    }
    assert!(declared >= 10, "found only {declared} declarations: the check is not reading the crate");
}

#[test]
fn no_engine_source_binds_or_calls_a_write_function() {
    let mut files = Vec::new();
    let engine = engine_dir();
    rust_files(&engine.join("crates/opencap-plctag-sys/src"), &mut files);
    for entry in std::fs::read_dir(engine.join("drivers")).unwrap().flatten() {
        rust_files(&entry.path().join("src"), &mut files);
    }
    assert!(files.len() > 5, "the scan found only {} files", files.len());
    for f in files {
        let text = std::fs::read_to_string(&f).unwrap();
        let code: String = text.lines().filter(|l| !l.trim_start().starts_with("//")).collect::<Vec<_>>().join("\n");
        let cleaned = code.replace("plc_tag_set_debug_level", "");
        for bad in ["plc_tag_write", "plc_tag_set_", "plc_tag_lock", "plc_tag_unlock", "plc_tag_abort"] {
            assert!(!cleaned.contains(bad), "{} mentions {bad}: a write path (ADR-0008)", f.display());
        }
    }
}
