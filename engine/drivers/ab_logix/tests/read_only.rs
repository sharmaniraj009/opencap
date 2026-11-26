//! Read-only guard (ADR-0004 section 5, ADR-0008, CONTRIBUTING rule 1): no CIP write service can
//! be built or sent by this driver. Mirrors the Python `WRITE_SERVICES` refusal tests.

use opencap_driver_ab_logix::cip::{self, ObjectService, WRITE_SERVICES};

#[test]
fn every_buildable_service_is_a_read() {
    for s in ObjectService::ALL {
        assert!(!WRITE_SERVICES.contains(&s.code()), "{s:?} is a write service");
    }
    let path = cip::symbolic_path("TAG").unwrap();
    let requests = [
        cip::read_tag_request(&path, 1).unwrap(),
        cip::read_fragmented_request(&path, 1, 0).unwrap(),
        cip::get_attribute_list_request(&path, &[1]).unwrap(),
        cip::get_instance_attribute_list_request(&path, &[1]).unwrap(),
        cip::get_attributes_all_request(1, 1).unwrap(),
        cip::read_template_request(0x10, 0, 10).unwrap(),
    ];
    for r in &requests {
        assert!(!WRITE_SERVICES.contains(&r[0]), "{:#04x}", r[0]);
    }
    let msp = cip::multiple_service_request(&requests).unwrap();
    assert_eq!(msp[0], 0x0A);
}

#[test]
fn a_write_service_is_refused_even_inside_a_multiple_service_packet() {
    for code in WRITE_SERVICES {
        assert!(cip::multiple_service_request(&[vec![code, 0]]).is_err(), "{code:#04x}");
    }
}

#[test]
fn driver_sources_never_name_a_write_service() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = 0;
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_some_and(|e| e == "rs") {
            files += 1;
            // cip.rs names the codes once, in the refusal list
            if path.file_name().is_some_and(|n| n == "cip.rs") {
                continue;
            }
            let text = std::fs::read_to_string(&path).unwrap();
            for needle in ["0x4D", "0x4d", "0x53", "WriteTag", "write_tag", "SetAttribute", "set_attribute"] {
                assert!(!text.contains(needle), "{} mentions {needle:?}", path.display());
            }
        }
    }
    assert!(files >= 8);
}
