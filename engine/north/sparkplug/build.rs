//! Generate the Sparkplug B protobuf types with prost from the vendored, unmodified
//! `proto/third_party/sparkplug_b.proto` (Eclipse Tahu v1.0.19, EPL-2.0; see its README there).
//! protox compiles the .proto in Rust, so no system `protoc` is needed. The descriptor set is
//! also written to OUT_DIR: `tests/vectors.rs` checks that the copy the Python conformance
//! generator uses (`tests/conformance/sparkplug/sparkplug_b.desc`) is identical.

use std::path::PathBuf;

use prost::Message as _;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_dir = manifest.join("../../../proto/third_party").canonicalize()?;
    let proto = proto_dir.join("sparkplug_b.proto");
    println!("cargo:rerun-if-changed={}", proto.display());

    let fds = protox::compile([&proto], [&proto_dir])?;
    // The copy for the conformance check carries no source info (comments, line spans), so it
    // does not depend on line endings or comment edits: only on the schema.
    let mut schema = fds.clone();
    for file in &mut schema.file {
        file.source_code_info = None;
    }
    let out = PathBuf::from(std::env::var("OUT_DIR")?);
    std::fs::write(out.join("sparkplug_b.desc"), schema.encode_to_vec())?;
    prost_build::Config::new().compile_fds(fds)?;
    Ok(())
}
