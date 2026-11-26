//! Read-only guard (ADR-0004 section 5, ADR-0008, CONTRIBUTING rule 1): no write var, PLC
//! control, stop, download or upload PDU can be built or sent by this driver. Mirrors the
//! Python `check_read_only` tests.

use opencap_driver_s7::proto::{
    check_read_only, read_szl, read_szl_followup, read_var, setup_communication, ReadItem, REFUSED_JOB_FUNCTIONS,
    WL_BYTE,
};

#[test]
fn every_buildable_request_is_a_read() {
    let item = ReadItem { area: 0x84, db: 7, start: 100, word_len: WL_BYTE, count: 4 };
    let pdus = [
        setup_communication(1, 960),
        read_var(2, &[item]),
        read_var(3, &[item, item, item]),
        read_szl(4, 0x001C, 0),
        read_szl(5, 0x0424, 0),
        read_szl_followup(6, 1),
    ];
    for pdu in pdus {
        assert_eq!(check_read_only(&pdu), None);
        assert!(!REFUSED_JOB_FUNCTIONS.contains(&pdu[10]) || pdu[1] == 0x07);
    }
}

#[test]
fn write_var_plc_control_and_block_services_are_refused() {
    let item = ReadItem { area: 0x84, db: 7, start: 100, word_len: WL_BYTE, count: 4 };
    // write var (0x05), request download .. end upload (0x1A..0x1F), PLC control (0x28), stop (0x29)
    for f in REFUSED_JOB_FUNCTIONS {
        let mut pdu = read_var(1, &[item]);
        pdu[10] = f;
        let why = check_read_only(&pdu).unwrap_or_else(|| panic!("{f:#04x} passed the guard"));
        assert!(why.contains("refused"), "{why}");
    }
    assert!(
        REFUSED_JOB_FUNCTIONS.contains(&0x05)
            && REFUSED_JOB_FUNCTIONS.contains(&0x28)
            && REFUSED_JOB_FUNCTIONS.contains(&0x29)
    );
    // another USERDATA group (block functions, 3; cyclic data, 2; time functions, 7) or subfunction
    for group in [0x42u8, 0x43, 0x47, 0x41] {
        let mut pdu = read_szl(1, 0x001C, 0);
        pdu[15] = group;
        assert!(check_read_only(&pdu).is_some(), "{group:#04x}");
    }
    // something that is not an S7 job at all
    assert!(check_read_only(&[0u8; 20]).is_some());
    assert!(check_read_only(&[0x32, 0x01]).is_some());
}

#[test]
fn driver_sources_never_build_a_write() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = 0;
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_some_and(|e| e == "rs") {
            files += 1;
            // proto.rs names the refused function codes once, in the refusal list; errors.rs is a
            // table of error texts
            if path.file_name().is_some_and(|n| n == "proto.rs" || n == "errors.rs") {
                continue;
            }
            let text = std::fs::read_to_string(&path).unwrap();
            for needle in ["write_var", "build_write", "plc_control", "plc_stop", "download", "upload", "0x28", "0x29"]
            {
                assert!(!text.contains(needle), "{} mentions {needle:?}", path.display());
            }
        }
    }
    assert!(files >= 7);
}
