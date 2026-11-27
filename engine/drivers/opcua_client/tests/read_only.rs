//! ADR-0008: the OPC UA client driver calls no service that changes the server.

#[test]
fn the_driver_sources_name_no_write_service() {
    let src = std::fs::read_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/src")).unwrap();
    let mut files = 0;
    for e in src.flatten() {
        let text = std::fs::read_to_string(e.path()).unwrap();
        let code: String = text.lines().filter(|l| !l.trim_start().starts_with("//")).collect::<Vec<_>>().join("\n");
        files += 1;
        for bad in [
            ".write(",
            ".write_attributes(",
            ".call(",
            ".history_update(",
            ".add_nodes(",
            ".delete_nodes(",
            ".add_references(",
            ".delete_references(",
            "WriteValue",
            "CallMethodRequest",
        ] {
            assert!(!code.contains(bad), "{} uses {bad}: a write path (ADR-0008)", e.path().display());
        }
    }
    assert!(files >= 3, "the scan found {files} files");
}
