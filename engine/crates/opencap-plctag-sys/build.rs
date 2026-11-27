//! Builds the vendored libplctag (engine/vendor/libplctag, v2.7.2, unmodified) with the `cc` crate:
//! plain `cargo build`, no system package, same on Linux and Windows.
use std::path::{Path, PathBuf};

fn add_dir(b: &mut cc::Build, dir: &Path) {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "c"))
        .filter(|p| p.file_name().is_some_and(|n| n != "omron_device_tag.c" && n != "handle_system.c")) // not in upstream CMakeLists, does not compile
        .collect();
    v.sort();
    for p in v {
        b.file(p);
    }
}

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../vendor/libplctag/src");
    let lib = root.join("libplctag");
    let mut b = cc::Build::new();
    b.warnings(false).opt_level(2);
    for sub in ["lib", "protocols/ab", "protocols/mb", "protocols/omron", "protocols/system"] {
        add_dir(&mut b, &lib.join(sub));
    }
    add_dir(&mut b, &root.join("utils"));
    let windows = std::env::var("CARGO_CFG_TARGET_OS").unwrap() == "windows";
    let plat = root.join("platform").join(if windows { "windows" } else { "posix" });
    add_dir(&mut b, &plat);
    for inc in [root.clone(), lib.clone(), lib.join("lib"), lib.join("protocols"), lib.join("protocols/ab"), root.join("utils"), plat] {
        b.include(inc);
    }
    b.define("LIBPLCTAG_STATIC", "1");
    if windows {
        b.define("_CRT_SECURE_NO_WARNINGS", None);
        println!("cargo:rustc-link-lib=ws2_32");
        println!("cargo:rustc-link-lib=winmm");
    } else {
        b.flag_if_supported("-pthread");
        println!("cargo:rustc-link-lib=pthread");
        println!("cargo:rustc-link-lib=m");
    }
    b.compile("plctag");
    println!("cargo:rerun-if-changed={}", root.display());
}
