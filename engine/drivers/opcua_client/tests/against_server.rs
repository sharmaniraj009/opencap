//! The OPC UA client driver against our own OPC UA server (`opencap-north-opcua`): values, dtype
//! conversion, status mapping, a missing node, user authentication, a lost and a returning server.

use std::sync::Arc;
use std::time::Duration;

use serde_json::json;
use tokio_util::sync::CancellationToken;

use opencap_core::config::{DType, Device, OpcUaServerCfg, Tag};
use opencap_core::driver::{Driver, DriverError};
use opencap_core::health::{HealthStatus, State};
use opencap_core::store::TagStore;
use opencap_core::value::{now_utc, Quality, TagValue, Value};
use opencap_driver_opcua_client::{plan_tags, OpcUaClientDriver, OpcUaParams};
use opencap_north::plugin::{DeviceStatusReader, NorthContext, NorthPlugin};
use opencap_north_opcua::OpcUaNorth;

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn scratch(tag: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!(
        "opencap-uaclient-{tag}-{}-{}",
        std::process::id(),
        now_utc().timestamp_nanos_opt().unwrap_or(0)
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

struct Server {
    port: u16,
    shutdown: CancellationToken,
    task: tokio::task::JoinHandle<()>,
}

async fn serve(port: u16, edit: impl FnOnce(&mut OpcUaServerCfg), env: &dyn Fn(&str) -> Option<String>) -> Server {
    let tags = vec![
        Tag::new("Pv", "40001", DType::Float64),
        Tag::new("G1.Count", "40002", DType::Uint16),
        Tag::new("Flag", "40003", DType::Bool),
        Tag::new("Name", "40004", DType::String),
        Tag::new("Level", "40006", DType::Int16),
    ];
    let dev = Device::new("SITE/PLC", "SITE", "PLC", "modbus_tcp", "192.0.2.1", 502, tags);
    let store = Arc::new(TagStore::with_options(64, None));
    let writer = store.register(&dev);
    let now = now_utc();
    let good = |v: Value| TagValue::new(Some(v), Quality::Good, Some(now));
    writer.update([
        ("Pv", &good(Value::Float(12.5))),
        ("G1.Count", &good(Value::Int(7))),
        ("Flag", &good(Value::Bool(true))),
        ("Name", &good(Value::Str("pump".into()))),
        ("Level", &good(Value::Int(-3))),
    ]);
    writer.update([("Level", &TagValue::new(None, Quality::BadComm, Some(now)))]);
    let mut cfg = OpcUaServerCfg {
        enabled: true,
        endpoint: format!("opc.tcp://127.0.0.1:{port}/opencap"),
        anonymous: true,
        update_ms: 20,
        pki_dir: Some(scratch("server").to_string_lossy().to_string()),
        ..OpcUaServerCfg::default()
    };
    edit(&mut cfg);
    let north = OpcUaNorth::new(cfg, vec![dev], env).unwrap();
    let status = DeviceStatusReader::new(|_| Some(HealthStatus { state: State::Ok, last_good: None, errors: 0 }));
    let mut ctx = NorthContext::new("opcua", store.reader(), status);
    let shutdown = CancellationToken::new();
    ctx.shutdown = shutdown.clone();
    let task = tokio::spawn(async move {
        let _keep = writer;
        if let Err(e) = Box::new(north).run(ctx).await {
            panic!("server: {e}");
        }
    });
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(300)).await; // initial value sync
    Server { port, shutdown, task }
}

impl Server {
    async fn stop(self) {
        self.shutdown.cancel();
        let _ = self.task.await;
    }
}

fn client_device(port: u16, tags: Vec<Tag>) -> Device {
    let mut d = Device::new("S/UA", "S", "UA", "opcua_client", "127.0.0.1", i64::from(port), tags);
    d.timeout_ms = 3000;
    d.params.insert("endpoint".into(), json!(format!("opc.tcp://127.0.0.1:{port}/opencap")));
    d.params.insert("pki_dir".into(), json!(scratch("client").to_string_lossy()));
    d
}

fn tags() -> Vec<Tag> {
    vec![
        Tag::new("pv", "ns=2;s=SITE.PLC.Pv", DType::Float64),
        Tag::new("pv_int", "ns=2;s=SITE.PLC.Pv", DType::Int32), // same node, other dtype: one request item
        Tag::new("count", "ns=2;s=SITE.PLC.G1.Count", DType::Uint16),
        Tag::new("flag", "ns=2;s=SITE.PLC.Flag", DType::Bool),
        Tag::new("name", "ns=2;s=SITE.PLC.Name", DType::String),
        Tag::new("level", "ns=2;s=SITE.PLC.Level", DType::Int16),
        Tag::new("missing", "ns=2;s=SITE.PLC.Nope", DType::Int16),
    ]
}

async fn read_all(
    d: &mut OpcUaClientDriver,
    t: &[Tag],
) -> Result<std::collections::HashMap<String, TagValue>, DriverError> {
    let refs: Vec<&Tag> = t.iter().collect();
    let plan = d.plan(&refs);
    assert!(plan.invalid.is_empty(), "{:?}", plan.invalid);
    let mut out = std::collections::HashMap::new();
    for b in &plan.blocks {
        out.extend(d.read(b.as_ref()).await?);
    }
    Ok(out)
}

#[tokio::test(flavor = "multi_thread")]
async fn reads_values_statuses_and_a_missing_node() {
    let s = serve(free_port(), |_| {}, &|_| None).await;
    let t = tags();
    let mut d = OpcUaClientDriver::new(&client_device(s.port, t.clone())).unwrap();
    d.connect().await.unwrap();
    assert!(d.connected());
    let v = read_all(&mut d, &t).await.unwrap();
    assert_eq!(v["pv"].value, Some(Value::Float(12.5)));
    assert_eq!(v["pv"].quality, Quality::Good);
    assert!(v["pv"].source_ts.is_some());
    assert_eq!(v["pv_int"].value, Some(Value::Int(13))); // 12.5 rounds like Python's round()
    assert_eq!(v["count"].value, Some(Value::Int(7)));
    assert_eq!(v["flag"].value, Some(Value::Bool(true)));
    assert_eq!(v["name"].value, Some(Value::Str("pump".into())));
    // the server's BadCommunicationError (a bad_comm tag) arrives as bad_comm, without a value
    assert_eq!(v["level"].quality, Quality::BadComm);
    assert_eq!(v["level"].value, None);
    // a node the server does not have: BadNodeIdUnknown -> bad_config
    assert_eq!(v["missing"].quality, Quality::BadConfig);
    d.close().await;
    assert!(!d.connected());
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn plan_dedupes_nodes_chunks_by_max_items_and_reports_bad_addresses() {
    let dev = client_device(1, vec![]);
    let mut p = OpcUaParams::from_device(&dev).unwrap();
    p.max_items = 2;
    let mut t = tags();
    t.push(Tag::new("bad", "not a node", DType::Int16));
    let plan = plan_tags(&p, &t);
    assert_eq!(plan.invalid.len(), 1);
    assert!(plan.invalid["bad"].contains("NodeId"));
    // 6 distinct nodes (pv twice) in blocks of 2
    assert_eq!(plan.blocks.len(), 3);
    let first = &plan.blocks[0];
    assert!(first.tags().contains(&"pv".to_string()) && first.tags().contains(&"pv_int".to_string()));
}

#[tokio::test(flavor = "multi_thread")]
async fn users_come_from_environment_variables_and_a_wrong_password_is_refused() {
    let server_env = |n: &str| match n {
        "SRV_U" => Some("operator".to_string()),
        "SRV_P" => Some("s3cret".to_string()),
        _ => None,
    };
    let s = serve(
        free_port(),
        |c| {
            c.anonymous = false;
            c.username_env = Some("SRV_U".into());
            c.password_env = Some("SRV_P".into());
        },
        &server_env,
    )
    .await;
    let t = tags();
    let mut dev = client_device(s.port, t.clone());
    dev.params.insert("username_env".into(), json!("CLI_U"));
    dev.params.insert("password_env".into(), json!("CLI_P"));
    let good: opencap_driver_opcua_client::EnvFn = Arc::new(|n| match n {
        "CLI_U" => Some("operator".into()),
        "CLI_P" => Some("s3cret".into()),
        _ => None,
    });
    let bad: opencap_driver_opcua_client::EnvFn = Arc::new(|n| match n {
        "CLI_U" => Some("operator".into()),
        "CLI_P" => Some("wrong".into()),
        _ => None,
    });
    let mut d = OpcUaClientDriver::with_env(&dev, bad).unwrap();
    assert!(matches!(d.connect().await, Err(DriverError::ConnectionLost(_))));
    assert!(!d.connected());
    let mut d = OpcUaClientDriver::with_env(&dev, good).unwrap();
    d.connect().await.unwrap();
    assert_eq!(read_all(&mut d, &t).await.unwrap()["pv"].value, Some(Value::Float(12.5)));
    // a variable that is not set is a connect failure that says so
    let mut d = OpcUaClientDriver::with_env(&dev, Arc::new(|_| None)).unwrap();
    let e = d.connect().await.unwrap_err();
    assert!(e.to_string().contains("CLI_U"), "{e}");
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_lost_server_is_connection_lost_and_a_returning_one_reconnects() {
    let port = free_port();
    let s = serve(port, |_| {}, &|_| None).await;
    let t = tags();
    let mut d = OpcUaClientDriver::new(&client_device(port, t.clone())).unwrap();
    d.connect().await.unwrap();
    assert!(read_all(&mut d, &t).await.is_ok());
    s.stop().await;
    let mut err = None;
    for _ in 0..40 {
        match read_all(&mut d, &t).await {
            Err(e) => {
                err = Some(e);
                break;
            }
            Ok(_) => tokio::time::sleep(Duration::from_millis(100)).await,
        }
    }
    let err = err.expect("the lost server must surface as an error");
    assert!(matches!(err, DriverError::ConnectionLost(_) | DriverError::Comm(_)), "{err:?}");
    d.close().await;
    assert!(d.connect().await.is_err(), "no server: connect must fail");
    let s = serve(port, |_| {}, &|_| None).await;
    d.connect().await.unwrap();
    assert_eq!(read_all(&mut d, &t).await.unwrap()["pv"].value, Some(Value::Float(12.5)));
    s.stop().await;
}

#[test]
fn bad_params_are_refused_with_a_reason() {
    let mut dev = client_device(1, vec![]);
    dev.params.insert("password".into(), json!("x"));
    assert!(OpcUaParams::from_device(&dev).unwrap_err().contains("environment variable"));
    let mut dev = client_device(1, vec![]);
    dev.params.insert("security_policy".into(), json!("Basic256Sha256"));
    assert!(OpcUaParams::from_device(&dev).unwrap_err().contains("message_mode"));
    let mut dev = client_device(1, vec![]);
    dev.params.insert("endpoint".into(), json!("http://x"));
    assert!(OpcUaParams::from_device(&dev).unwrap_err().contains("opc.tcp://"));
    let mut dev = client_device(1, vec![]);
    dev.params.insert("max_items".into(), json!(0));
    assert!(OpcUaParams::from_device(&dev).unwrap_err().contains("max_items"));
}
