//! Read-only guard (ADR-0004 section 5, ADR-0008, CONTRIBUTING rule 1): no MC write or
//! remote-control command can be built or sent by this driver. Mirrors the Python
//! `check_read_only` tests.

use opencap_driver_melsec_mc::frame::{
    batch_read_words, check_read_only, read_cpu_model, request_command, READ_COMMANDS, WRITE_COMMANDS,
};
use opencap_driver_melsec_mc::McParams;

#[test]
fn every_buildable_request_is_a_read() {
    for plc_type in ["Q", "L", "QnA", "iQ-L", "iQ-R"] {
        let p = McParams { plc_type: plc_type.into(), ..McParams::default() };
        let frames =
            [batch_read_words(&p, 4, 0xA8, 0, 1), batch_read_words(&p, 4, 0x90, 16, 960), read_cpu_model(&p, 4)];
        for f in frames {
            let cmd = request_command(&f).expect("a 3E request");
            assert!(READ_COMMANDS.contains(&cmd), "{plc_type}: {cmd:?}");
            assert!(!WRITE_COMMANDS.contains(&cmd.0));
            assert_eq!(check_read_only(&f), None);
        }
    }
}

#[test]
fn a_write_or_control_frame_is_refused_for_every_subcommand() {
    let p = McParams::default();
    for cmd in WRITE_COMMANDS {
        for sub in [0x0000u16, 0x0001, 0x0002, 0x0003] {
            let mut f = read_cpu_model(&p, 4);
            f[11..13].copy_from_slice(&cmd.to_le_bytes());
            f[13..15].copy_from_slice(&sub.to_le_bytes());
            assert!(check_read_only(&f).is_some(), "{cmd:#06x}/{sub:#06x}");
        }
    }
    // device write, remote RUN, remote STOP are in the list
    for must in [0x1401u16, 0x1402, 0x1001, 0x1002] {
        assert!(WRITE_COMMANDS.contains(&must));
    }
}

#[test]
fn driver_sources_never_name_a_write_command() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = 0;
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_some_and(|e| e == "rs") {
            files += 1;
            // frame.rs names the codes once, in the refusal list
            if path.file_name().is_some_and(|n| n == "frame.rs") {
                continue;
            }
            let text = std::fs::read_to_string(&path).unwrap();
            for needle in
                ["0x1401", "0x1402", "0x1001", "0x1002", "0x1003", "0x1005", "0x1006", "write_words", "batchwrite"]
            {
                assert!(!text.contains(needle), "{} mentions {needle:?}", path.display());
            }
        }
    }
    assert!(files >= 5);
}
