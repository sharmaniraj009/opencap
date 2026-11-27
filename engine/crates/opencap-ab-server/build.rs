use std::path::PathBuf;
fn main() {
    let dir =
        PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../vendor/libplctag/src/tools/ab_server");
    let mut b = cc::Build::new();
    let src = dir.join("../..");
    // C11: ATOMIC_VAR_INIT is not a constant initializer under C23, the default of newer GCC
    b.warnings(false).opt_level(2).include(&dir).include(&src).define("main", "ab_server_main");
    // The simulator has its own mem/mutex/socket/thread helpers with the names libplctag's platform
    // layer exports. The integration test links both, so the simulator's are renamed.
    for sym in [
        "mem_alloc",
        "mem_cmp",
        "mem_copy",
        "mem_free",
        "mem_move",
        "mem_realloc",
        "mem_set",
        "mutex_create",
        "mutex_destroy",
        "mutex_lock_impl",
        "mutex_try_lock_impl",
        "mutex_unlock_impl",
        "random_u64",
        "socket_close",
        "socket_read",
        "socket_write",
        "thread_create",
        "thread_destroy",
        "thread_detach",
        "thread_join",
        "thread_kill",
        "thread_stop",
    ] {
        b.define(sym, format!("absim_{sym}").as_str());
    }
    let mut v: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "c"))
        .collect();
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
    if std::env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default() != "msvc" {
        b.std("gnu11");
    }
    b.compile("ab_server");
    println!("cargo:rerun-if-changed={}", dir.display());
}
