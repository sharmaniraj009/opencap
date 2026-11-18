//! Exposes the enabled `driver-*` Cargo features to the registry tests as
//! `OPENCAP_DRIVER_FEATURES` (comma-separated, sorted), so the test checks the static table
//! against what Cargo actually compiled in, with no per-driver line to maintain.

fn main() {
    let mut features: Vec<String> = std::env::vars()
        .filter_map(|(key, _)| key.strip_prefix("CARGO_FEATURE_DRIVER_").map(|n| n.to_lowercase().replace('_', "-")))
        .map(|n| format!("driver-{n}"))
        .collect();
    features.sort();
    println!("cargo:rustc-env=OPENCAP_DRIVER_FEATURES={}", features.join(","));
    println!("cargo:rerun-if-changed=build.rs");
}
