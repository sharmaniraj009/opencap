//! The engine's supervisor against the real Python driver host (`python -m
//! opencap.plugin_host`): the launch contract of proto/README.md end to end (ready line, Unix
//! socket and loopback TCP, token, Hello), planning, a refused connect, recovery from a killed
//! process, and a clean exit when the engine lets go.
//!
//! Needs a Python with `opencap[plugin]`: `$OPENCAP_TEST_PYTHON`, else the repository's
//! `.venv`. Skipped (with a note) when neither works; the Python end-to-end suites run it in CI.

use std::path::PathBuf;
use std::time::Duration;

use opencap_core::config::{Channel, DType, Device, PythonPluginCfg, Tag};
use opencap_core::driver::{DriverError, DriverFactory};
use opencap_core::registry::plan_device;
use opencap_plugin::PluginRegistry;

fn python() -> Option<String> {
    let venv = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../.venv/bin/python");
    let candidates = [std::env::var("OPENCAP_TEST_PYTHON").ok(), venv.exists().then(|| venv.display().to_string())];
    candidates.into_iter().flatten().find(|py| {
        std::process::Command::new(py)
            .args(["-c", "import grpc, opencap.plugin_host"])
            .output()
            .is_ok_and(|o| o.status.success())
    })
}

fn cfg(python: &str, transport: &str) -> PythonPluginCfg {
    serde_json::from_value(serde_json::json!({
        "command": [python, "-m", "opencap.plugin_host"],
        "transport": transport,
        "start_timeout_ms": 30_000,
    }))
    .unwrap()
}

/// A Modbus device on a port nothing listens on: planning is pure, connecting is refused.
fn device() -> Device {
    let tags = vec![
        Tag::new("A", "400001", DType::Uint16),
        Tag::new("B", "400002", DType::Float32),
        Tag::new("BAD", "999999", DType::Uint16),
    ];
    let mut d = Device::new("SIM/PLC", "SIM", "PLC", "modbus_tcp", "127.0.0.1", 9, tags);
    d.plugin = Some("python".into());
    d.timeout_ms = 500;
    d
}

fn base() -> DriverFactory {
    std::sync::Arc::new(|_: &Device| Err("not routed".into()))
}

async fn run(python: &str, transport: &str) {
    let d = device();
    let reg = PluginRegistry::new();
    let channels = [Channel { name: "SIM".into(), enabled: true, devices: vec![d.clone()] }];
    reg.prepare(&cfg(python, transport), &channels).await;
    let factory = reg.factory(base());
    let plan = plan_device(&d, &factory);
    assert!(plan.ok(), "{transport}: {:?}", plan.error);
    assert_eq!(plan.invalid().keys().collect::<Vec<_>>(), vec!["BAD"], "{transport}");
    assert!(plan.blocks() >= 1 && plan.groups[0].plan.blocks[0].describe().contains("FC"), "{transport}");

    let mut drv = factory(&d).unwrap();
    let e = drv.connect().await.unwrap_err();
    assert!(matches!(e, DriverError::ConnectionLost(_)), "{transport}: {e:?}");
    assert!(!drv.connected());

    let host = reg.host("SIM").unwrap();
    let pid = host.pid().await.expect("a process");
    #[cfg(unix)]
    {
        // The plugin is killed: the next call ends that generation, the next connect after the
        // backoff starts a new process (and opens the device in it again).
        let status = std::process::Command::new("kill").args(["-9", &pid.to_string()]).status().unwrap();
        assert!(status.success());
        tokio::time::sleep(Duration::from_millis(300)).await;
        let e = drv.connect().await.unwrap_err();
        assert!(e.to_string().contains("python plugin host"), "{transport}: {e}");
        tokio::time::sleep(opencap_plugin::host::RESTART_MIN + Duration::from_millis(200)).await;
        let e = drv.connect().await.unwrap_err();
        assert!(
            e.to_string().contains("connect failed")
                || e.to_string().contains("Connection refused")
                || matches!(e, DriverError::ConnectionLost(_)),
            "{transport}: {e}"
        );
        let new_pid = host.pid().await.expect("restarted");
        assert_ne!(new_pid, pid, "{transport}");
    }
    let last = host.pid().await.unwrap();
    reg.shutdown().await;
    assert!(host.pid().await.is_none());
    #[cfg(unix)]
    {
        // stdin EOF stopped it; no orphan is left behind.
        let alive = std::process::Command::new("kill")
            .args(["-0", &last.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap()
            .success();
        assert!(!alive, "{transport}: plugin {last} still running after shutdown");
    }
    let _ = last;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_python_host_over_a_unix_socket_and_loopback_tcp() {
    let Some(py) = python() else {
        eprintln!("skipped: no Python with opencap[plugin] (set OPENCAP_TEST_PYTHON)");
        return;
    };
    if cfg!(unix) {
        run(&py, "unix").await;
    }
    run(&py, "tcp").await;
}

#[tokio::test]
async fn a_command_that_does_not_start_is_a_plan_error() {
    let d = device();
    let reg = PluginRegistry::new();
    let channels = [Channel { name: "SIM".into(), enabled: true, devices: vec![d.clone()] }];
    reg.prepare(&cfg("/nonexistent/python3", "tcp"), &channels).await;
    let e = plan_device(&d, &reg.factory(base())).error.unwrap();
    assert!(e.starts_with("python plugin: plugin host could not be started: /nonexistent/python3:"), "{e}");
    reg.shutdown().await;
}
