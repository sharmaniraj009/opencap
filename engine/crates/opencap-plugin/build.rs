//! Compile `proto/opencap/plugin/v1/driver.proto` (repository root) with protox, a pure-Rust
//! protobuf compiler, so building the engine needs no `protoc`.

use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?).join("../../../proto");
    let file = root.join("opencap/plugin/v1/driver.proto");
    println!("cargo:rerun-if-changed={}", file.display());
    let fds = protox::compile([&file], [&root])?;
    tonic_prost_build::configure()
        .build_server(true) // the in-process fake plugin of the tests
        .build_transport(false) // no generated `DriverClient::connect()`: it would clash with the Connect RPC
        .bytes(".opencap.plugin.v1.Block.payload")
        .bytes(".opencap.plugin.v1.ReadRequest.block")
        .compile_fds(fds)?;
    Ok(())
}
