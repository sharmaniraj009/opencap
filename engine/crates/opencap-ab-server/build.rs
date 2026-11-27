use std::path::PathBuf;
fn main() {
    let dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../vendor/libplctag/src/tools/ab_server");
    let mut b = cc::Build::new();
    let src = dir.join("../..");
    b.warnings(false).opt_level(2).include(&dir).include(&src).define("main", "ab_server_main");
    let mut v: Vec<PathBuf> =
        std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).filter(|p| p.extension().is_some_and(|e| e == "c")).collect();
    v.sort();
    for p in v {
        b.file(p);
    }
    if std::env::var("CARGO_CFG_TARGET_OS").unwrap() == "windows" {
        println!("cargo:rustc-link-lib=ws2_32");
    } else {
        println!("cargo:rustc-link-lib=pthread");
        println!("cargo:rustc-link-lib=m");
    }
    b.compile("ab_server");
    println!("cargo:rerun-if-changed={}", dir.display());
}
