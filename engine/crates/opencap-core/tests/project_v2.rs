//! Project v2 (`project.json` + `node.yaml`, ADR-0013, #186): the loader takes the project as
//! written and only runs the finalize steps; v1 files beside it are ignored with a warning.

use std::path::{Path, PathBuf};

use opencap_core::config::loader::{NODE_FILE, PROJECT_FILE};
use opencap_core::config::{config_revision, is_v2, load_config, ConfigError};
use serde_json::{json, Value};

fn dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("opencap-v2-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn project() -> Value {
    json!({"version": 2, "revision": 4, "name": "plant-a", "title": "Plant A", "channels": [
    {"name": "Site 001", "devices": [
        {"id": "Site 001/8 PUMP", "channel": "Site 001", "name": "8 PUMP", "driver": "modbus_tcp",
         "host": "192.0.2.1", "port": 502, "publish": {"sub_device_id": "SUB_8"},
         "tags": [{"name": "FLOW_1", "address": "400045", "dtype": "float32"}]},
        {"id": "Site 001/9 PUMP", "channel": "Site 001", "name": "9 PUMP", "driver": "modbus_tcp",
         "host": "192.0.2.2", "port": 502, "publish": {"topic": "x/y", "sub_device_id": "SUB_8"}, "tags": []},
        {"id": "Site 001/COM", "channel": "Site 001", "name": "COM", "driver": "modbus_rtu",
         "host": "COM3", "port": 0, "params": {"transport": "unmapped", "com_port": "COM3"}, "tags": []}
    ]}]})
}

fn write(d: &Path, name: &str, text: impl ToString) {
    std::fs::write(d.join(name), text.to_string()).unwrap();
}

#[test]
fn a_project_is_read_as_written_and_finalized() {
    let d = dir("load");
    write(&d, PROJECT_FILE, project());
    write(&d, NODE_FILE, "instance: edge-7\nmqtt: {topic_prefix: Acme/plant-1}\n");
    // v1 files beside it are ignored: this override would disable the pump if it were applied
    write(&d, "devices.json", json!({"channels": []}));
    write(&d, "overrides.yaml", "devices:\n  Site 001/8 PUMP: {enabled: false}\n");
    write(&d, "poller.yaml", "instance: old\n");
    assert!(is_v2(&d));
    let cfg = load_config(&d).unwrap();
    assert_eq!(cfg.poller.instance, "edge-7");
    assert_eq!((cfg.project_name.as_deref(), cfg.project_title.as_str()), (Some("plant-a"), "Plant A"));
    let devs = &cfg.channels[0].devices;
    assert!(devs[0].enabled, "overrides.yaml is not applied to a project v2");
    assert_eq!(devs[0].publish.topic.as_deref(), Some("Acme/plant-1/site_001/8_pump/data"));
    assert_eq!(devs[1].publish.topic.as_deref(), Some("x/y"), "a topic set in the project is kept");
    assert!(!devs[2].enabled, "an unmapped serial line is disabled");
    assert_eq!(
        cfg.warnings,
        [
            "poller.yaml ignored: node.yaml holds the node settings of a project v2",
            "devices.json ignored: project.json is the project (v2)",
            "overrides.yaml ignored: project.json is the project (v2)",
            "Site 001/COM: COM3 is not mapped to a transport (transport: unmapped); device skipped. Set params.transport, host and port in project.json",
            "1 device(s) without SubDeviceId (publish.sub_device_id unset in project.json): Site 001/COM",
            "SubDeviceId 'SUB_8' is used by 2 devices: Site 001/8 PUMP, Site 001/9 PUMP",
        ]
    );
}

#[test]
fn without_node_yaml_poller_yaml_is_read_with_a_warning() {
    let d = dir("nonode");
    write(&d, PROJECT_FILE, project());
    write(&d, "poller.yaml", "instance: from-poller\n");
    let cfg = load_config(&d).unwrap();
    assert_eq!(cfg.poller.instance, "from-poller");
    assert_eq!(cfg.warnings[0], "node.yaml is missing, read poller.yaml instead; rename it to node.yaml");
    std::fs::remove_file(d.join("poller.yaml")).unwrap();
    let cfg = load_config(&d).unwrap();
    assert_eq!(cfg.poller.instance, opencap_core::config::PollerConfig::default().instance);
    assert!(cfg.warnings.iter().all(|w| !w.contains("node.yaml")), "{:?}", cfg.warnings);
}

#[test]
fn naming_rules_are_errors_in_a_project() {
    let d = dir("names");
    let mut p = project();
    p["channels"][0]["name"] = json!("_Spare");
    write(&d, PROJECT_FILE, &p);
    let ConfigError::Invalid(e) = load_config(&d).unwrap_err() else { panic!("not Invalid") };
    assert!(e.starts_with("project.json: channel name '_Spare' starts with '_'"), "{e}");
    assert!(e.ends_with("names starting with '_' are reserved for system tags"), "{e}");
}

#[test]
fn bad_versions_and_documents_are_refused_with_the_path() {
    let d = dir("bad");
    for (doc, want) in [
        (json!({"version": 1, "channels": []}), "version 1, a project v2 needs \"version\": 2"),
        (json!({"channels": []}), "version None, a project v2 needs \"version\": 2"),
        (json!({"version": 2, "channels": [], "colour": "x"}), "unknown field"),
        (json!({"version": 2, "revision": -1, "channels": []}), "greater than or equal to 0"),
    ] {
        write(&d, PROJECT_FILE, doc);
        let e = load_config(&d).unwrap_err().to_string();
        assert!(e.contains("project.json") && e.contains(want), "{want}: {e}");
    }
    write(&d, PROJECT_FILE, "{nope");
    assert!(load_config(&d).unwrap_err().to_string().contains("invalid JSON"));
}

#[test]
fn the_revision_of_a_project_covers_project_json_and_node_yaml() {
    let d = dir("rev");
    write(&d, PROJECT_FILE, project());
    let a = config_revision(&d);
    write(&d, "overrides.yaml", "devices: {}\n");
    assert_eq!(config_revision(&d), a, "v1 files are not part of a project's revision");
    write(&d, NODE_FILE, "instance: n\n");
    assert_ne!(config_revision(&d), a);
}
