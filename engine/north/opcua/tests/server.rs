//! The OPC UA server against the async-opcua client: address space, values and status codes from
//! the tag store, subscriptions, and every write-type service refused (ADR-0008).

use std::sync::Arc;
use std::time::Duration;

use opcua::client::{ClientBuilder, IdentityToken, Session};
use opcua::types::{
    AddNodesItem, AttributeId, DataValue, DeleteNodesItem, MessageSecurityMode, NodeClass, NodeId, ReadValueId,
    StatusCode, TimestampsToReturn, Variant, WriteValue,
};
use tokio_util::sync::CancellationToken;

use opencap_core::config::{DType, Device, OpcUaServerCfg, Tag};
use opencap_core::health::{HealthStatus, State};
use opencap_core::store::TagStore;
use opencap_core::value::{now_utc, Quality, TagValue, Value};
use opencap_north::plugin::{DeviceStatusReader, NorthContext, NorthPlugin};
use opencap_north_opcua::OpcUaNorth;

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn device() -> Device {
    let tags = vec![
        Tag::new("Pv", "40001", DType::Float64),
        Tag::new("G1.Count", "40002", DType::Uint16),
        Tag::new("G1.Deep.Flag", "40003", DType::Bool),
        Tag::new("Name", "40004", DType::String),
        Tag::new("Level", "40006", DType::Int16),
    ];
    Device::new("SITE/PLC", "SITE", "PLC", "modbus_tcp", "192.0.2.1", 502, tags)
}

struct Rig {
    port: u16,
    writer: opencap_core::store::DeviceWriter,
    shutdown: CancellationToken,
    task: tokio::task::JoinHandle<()>,
}

async fn rig(cfg_edit: impl FnOnce(&mut OpcUaServerCfg)) -> Rig {
    rig_env(cfg_edit, &|_| None).await
}

async fn rig_env(cfg_edit: impl FnOnce(&mut OpcUaServerCfg), env: &dyn Fn(&str) -> Option<String>) -> Rig {
    let port = free_port();
    let dev = device();
    let store = Arc::new(TagStore::with_options(64, None));
    let writer = store.register(&dev);
    let now = now_utc();
    let good = |v: Value| TagValue::new(Some(v), Quality::Good, Some(now));
    assert!(writer.update([
        ("Pv", &good(Value::Float(12.5))),
        ("G1.Count", &good(Value::Int(7))),
        ("G1.Deep.Flag", &good(Value::Bool(true))),
        ("Name", &good(Value::Str("pump".into()))),
        ("Level", &good(Value::Int(-3))),
    ]));
    // the store keeps the last value on a bad_comm update
    assert!(writer.update([("Level", &TagValue::new(None, Quality::BadComm, Some(now)))]));
    let mut cfg = OpcUaServerCfg {
        enabled: true,
        endpoint: format!("opc.tcp://127.0.0.1:{port}/opencap"),
        anonymous: true,
        update_ms: 20,
        ..OpcUaServerCfg::default()
    };
    cfg_edit(&mut cfg);
    let north = OpcUaNorth::new(cfg, vec![dev], env).unwrap();
    let status = DeviceStatusReader::new(|_| Some(HealthStatus { state: State::Ok, last_good: None, errors: 0 }));
    let mut ctx = NorthContext::new("opcua", store.reader(), status);
    let shutdown = CancellationToken::new();
    ctx.shutdown = shutdown.clone();
    let task = tokio::spawn(async move {
        if let Err(e) = Box::new(north).run(ctx).await {
            panic!("plugin: {e}")
        }
    });
    // wait for the port
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    Rig { port, writer, shutdown, task }
}

async fn connect(port: u16) -> (Arc<Session>, tokio::task::JoinHandle<opcua::types::StatusCode>) {
    let mut client = ClientBuilder::new()
        .application_name("t")
        .application_uri("urn:t")
        .product_uri("urn:t")
        .trust_server_certs(true)
        .create_sample_keypair(false)
        .session_retry_limit(0)
        .client()
        .unwrap();
    let url = format!("opc.tcp://127.0.0.1:{port}/opencap");
    let (session, event_loop) = client
        .connect_to_matching_endpoint((url.as_str(), "None", MessageSecurityMode::None), IdentityToken::Anonymous)
        .await
        .unwrap();
    let handle = event_loop.spawn();
    assert!(tokio::time::timeout(Duration::from_secs(10), session.wait_for_connection()).await.unwrap());
    (session, handle)
}

fn rv(id: &NodeId, attr: AttributeId) -> ReadValueId {
    ReadValueId { node_id: id.clone(), attribute_id: attr as u32, ..Default::default() }
}

async fn read(s: &Session, id: &NodeId) -> DataValue {
    s.read(&[rv(id, AttributeId::Value)], TimestampsToReturn::Both, 0.0).await.unwrap().remove(0)
}

fn nid(path: &str) -> NodeId {
    NodeId::new(2, path.to_string())
}

#[tokio::test(flavor = "multi_thread")]
async fn values_status_and_namespace() {
    let r = rig(|_| {}).await;
    let (s, _h) = connect(r.port).await;
    // the tag namespace is ns=2, as in KEPServerEX
    let v = read(&s, &nid("SITE.PLC.Pv")).await;
    assert_eq!(v.value, Some(Variant::from(12.5f64)));
    assert!(v.status().is_good());
    assert!(v.source_timestamp.is_some());
    assert_eq!(read(&s, &nid("SITE.PLC.G1.Count")).await.value, Some(Variant::from(7u16)));
    assert_eq!(read(&s, &nid("SITE.PLC.G1.Deep.Flag")).await.value, Some(Variant::from(true)));
    assert_eq!(read(&s, &nid("SITE.PLC.Name")).await.value, Some(Variant::from("pump".to_string())));
    // bad_comm keeps the last value and maps to BadCommunicationError
    let lv = read(&s, &nid("SITE.PLC.Level")).await;
    assert_eq!(lv.status(), StatusCode::BadCommunicationError);
    assert_eq!(lv.value, Some(Variant::from(-3i16)));
    // a change reaches the address space within update_ms
    let now = now_utc();
    r.writer.update([("Pv", &TagValue::new(Some(Value::Float(99.0)), Quality::Good, Some(now)))]);
    let mut seen = false;
    for _ in 0..50 {
        if read(&s, &nid("SITE.PLC.Pv")).await.value == Some(Variant::from(99.0f64)) {
            seen = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(40)).await;
    }
    assert!(seen, "the new value never arrived");
    r.shutdown.cancel();
    let _ = r.task.await;
}

#[tokio::test(flavor = "multi_thread")]
async fn variables_are_current_read_only_and_every_write_service_is_refused() {
    let r = rig(|_| {}).await;
    let (s, _h) = connect(r.port).await;
    let pv = nid("SITE.PLC.Pv");
    let access = s
        .read(
            &[rv(&pv, AttributeId::AccessLevel), rv(&pv, AttributeId::UserAccessLevel)],
            TimestampsToReturn::Neither,
            0.0,
        )
        .await
        .unwrap();
    for a in &access {
        assert_eq!(a.value, Some(Variant::from(1u8)), "AccessLevel must be CurrentRead (1)");
    }
    // Write
    let w = s
        .write(&[WriteValue {
            node_id: pv.clone(),
            attribute_id: AttributeId::Value as u32,
            value: DataValue::value_only(1.0f64),
            ..Default::default()
        }])
        .await
        .unwrap();
    assert!(w[0].is_bad(), "a write must be refused, got {:?}", w[0]);
    assert_eq!(read(&s, &pv).await.value, Some(Variant::from(12.5f64)), "the value must not change");
    // AddNodes
    let add = s
        .add_nodes(&[AddNodesItem {
            parent_node_id: NodeId::new(2, "SITE.PLC".to_string()).into(),
            node_class: NodeClass::Object,
            ..Default::default()
        }])
        .await;
    assert!(add.map(|r| r.iter().all(|x| x.status_code.is_bad())).unwrap_or(true), "AddNodes must be refused");
    // DeleteNodes
    let del = s.delete_nodes(&[DeleteNodesItem { node_id: pv.clone(), delete_target_references: true }]).await;
    assert!(del.map(|r| r.iter().all(|x| x.is_bad())).unwrap_or(true), "DeleteNodes must be refused");
    assert!(read(&s, &pv).await.status().is_good(), "the node must still exist");
    r.shutdown.cancel();
    let _ = r.task.await;
}

#[tokio::test(flavor = "multi_thread")]
async fn call_and_history_update_are_refused() {
    use opcua::client::HistoryUpdateAction;
    use opcua::types::{CallMethodRequest, DeleteRawModifiedDetails};
    let r = rig(|_| {}).await;
    let (s, _h) = connect(r.port).await;
    let call = s
        .call(vec![CallMethodRequest {
            object_id: nid("SITE.PLC"),
            method_id: nid("SITE.PLC.Pv"),
            input_arguments: None,
        }])
        .await;
    assert!(call.map(|r| r.iter().all(|x| x.status_code.is_bad())).unwrap_or(true), "Call must be refused");
    let hist = s
        .history_update(&[HistoryUpdateAction::DeleteRawModifiedDetails(DeleteRawModifiedDetails {
            node_id: nid("SITE.PLC.Pv"),
            is_delete_modified: false,
            start_time: Default::default(),
            end_time: Default::default(),
        })])
        .await;
    assert!(hist.map(|r| r.iter().all(|x| x.status_code.is_bad())).unwrap_or(true), "HistoryUpdate must be refused");
    r.shutdown.cancel();
    let _ = r.task.await;
}

fn scratch_dir(tag: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!(
        "opencap-opcua-{tag}-{}-{}",
        std::process::id(),
        now_utc().timestamp_nanos_opt().unwrap_or(0)
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// True when a session with this client certificate store and user opens and reads a value.
async fn secure_read_works(port: u16, client_pki: &std::path::Path, user: &str, pass: &str) -> bool {
    tokio::time::timeout(Duration::from_secs(30), secure_read_inner(port, client_pki, user, pass))
        .await
        .unwrap_or(false)
}

async fn secure_read_inner(port: u16, client_pki: &std::path::Path, user: &str, pass: &str) -> bool {
    let mut client = ClientBuilder::new()
        .application_name("t")
        .application_uri("urn:t")
        .product_uri("urn:t")
        .trust_server_certs(true)
        .create_sample_keypair(true)
        .pki_dir(client_pki)
        .session_retry_limit(0)
        .client()
        .unwrap();
    let url = format!("opc.tcp://127.0.0.1:{port}/opencap");
    let res = client
        .connect_to_matching_endpoint(
            (url.as_str(), "Basic256Sha256", MessageSecurityMode::SignAndEncrypt),
            IdentityToken::UserName(user.into(), pass.into()),
        )
        .await;
    let Ok((session, event_loop)) = res else {
        return false;
    };
    let handle = event_loop.spawn();
    let connected = tokio::time::timeout(Duration::from_secs(20), session.wait_for_connection()).await.unwrap_or(false);
    let ok = connected && {
        let v = read(&session, &nid("SITE.PLC.Pv")).await;
        v.value == Some(Variant::from(12.5f64))
    };

    handle.abort();
    ok
}

/// Secure channel, fail-closed certificate trust and user authentication.
#[tokio::test(flavor = "multi_thread")]
async fn secure_channel_needs_a_trusted_client_certificate_and_a_valid_user() {
    let server_pki = scratch_dir("server");
    let client_pki = scratch_dir("client");
    let spki = server_pki.to_string_lossy().to_string();
    let r = rig_env(
        |c| {
            c.security_policies = vec!["Basic256Sha256_SignAndEncrypt".into()];
            c.anonymous = false;
            c.pki_dir = Some(spki.clone());
            c.username_env = Some("OPC_U".into());
            c.password_env = Some("OPC_P".into());
        },
        &|n| match n {
            "OPC_U" => Some("operator".into()),
            "OPC_P" => Some("s3cret".into()),
            _ => None,
        },
    )
    .await;
    // an unknown client certificate is refused (the server trusts nothing it was not given) and is written to
    // rejected/ before it answers. A loaded runner can make one attempt run out of time (RSA key generation in a
    // debug build) before the certificate is presented, so attempt again until the file is there.
    let mut rejected: Vec<_> = Vec::new();
    let deadline = std::time::Instant::now() + Duration::from_secs(120);
    while rejected.is_empty() && std::time::Instant::now() < deadline {
        assert!(
            !secure_read_works(r.port, &client_pki, "operator", "s3cret").await,
            "an untrusted client certificate must be refused"
        );
        rejected = std::fs::read_dir(server_pki.join("rejected")).map(|d| d.flatten().collect()).unwrap_or_default();
    }
    assert!(!rejected.is_empty(), "the refused certificate must land in rejected/");
    // the operator trusts it by moving the file from rejected/ to trusted/ (the name carries the thumbprint);
    // then the door is open, but the wrong password still is not
    std::fs::create_dir_all(server_pki.join("trusted")).unwrap();
    let file = rejected[0].path();
    std::fs::rename(&file, server_pki.join("trusted").join(file.file_name().unwrap())).unwrap();
    assert!(!secure_read_works(r.port, &client_pki, "operator", "wrong").await, "a wrong password must be refused");
    assert!(
        secure_read_works(r.port, &client_pki, "operator", "s3cret").await,
        "a trusted client with the right user must connect"
    );
    // anonymous sessions are not offered
    assert!(!secure_read_works(r.port, &client_pki, "nobody", "x").await);
    r.shutdown.cancel();
    let _ = r.task.await;
    let _ = std::fs::remove_dir_all(&server_pki);
    let _ = std::fs::remove_dir_all(&client_pki);
}
